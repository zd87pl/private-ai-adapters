"""Smoke tests for the scripts in ``examples/``.

Each example is imported by file path (``examples/`` is not a package) and its
``main()`` is run against the tiny offline fixture models. The assertions stick to
what is deterministic: exit codes, the files left behind, the vaults themselves and
the shape of the printed report. What a two-layer toy model *says* is never
asserted, with one exception that ``test_pipeline.py`` establishes as exact:
greedy answers with ``use_adapter=False`` equal the base model's answers.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from wdva import AdapterBundle, SecretKey, WrongKeyError, is_vault

pytestmark = pytest.mark.ml

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SCRIPTS = ["quickstart", "hot_swap", "seal_existing_adapter"]

# Enough optimisation for the toy models to visibly learn, in a few seconds. A
# max length of 120 keeps every sequence inside their 128 positions.
FAST = ["--epochs", "60", "--rank", "8", "--learning-rate", "0.02", "--max-length", "120"]
SHORT_ANSWERS = ["--max-new-tokens", "8"]


def load_example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"wdva_example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_bytecode_in_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the scripts must not leave ``__pycache__`` behind in ``examples/``."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


@pytest.fixture(autouse=True)
def _one_torch_thread() -> Iterator[None]:
    """Run the toy models on a single intra-op thread.

    They are far too small to gain anything from more, and on a busy machine several
    threads fighting over the cores make these tests many times slower.
    """
    import torch

    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)


def assert_private(path: Path) -> None:
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("name", SCRIPTS)
def test_examples_import_quietly_and_describe_themselves(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_example(name)
    assert capsys.readouterr().out == ""  # importing a script does nothing visible
    assert module.__doc__
    assert "Run ``python examples/" in module.__doc__
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--help"])
    assert excinfo.value.code == 0
    usage = capsys.readouterr().out
    for flag in ("--base-model", "--workdir", "--epochs"):
        assert flag in usage


def test_quickstart_trains_seals_and_answers(
    model_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "work"
    args = ["--base-model", str(model_dir), "--device", "cpu", "--workdir", str(workdir)]
    code = load_example("quickstart").main([*args, *FAST, *SHORT_ANSWERS])
    out = capsys.readouterr().out
    assert code == 0

    # The only things written are the key and the vault: no checkpoints, no plaintext.
    key_path, vault_path = workdir / "notes.key", workdir / "notes.wdva"
    assert sorted(p.name for p in workdir.iterdir()) == ["notes.key", "notes.wdva"]
    assert_private(key_path)
    assert_private(vault_path)
    assert is_vault(vault_path)
    if os.name == "posix":
        assert "notes.key (mode 0600)" in out

    # The saved key opens the vault, and the vault records what the example trained on.
    bundle = AdapterBundle.load(vault_path, key=SecretKey.from_file(key_path))
    training = bundle.info["training"]
    assert bundle.base_model == str(model_dir)
    assert training["documents"] == ["notes.txt"]
    assert training["num_examples"] == 7
    assert (training["config"]["epochs"], training["config"]["rank"]) == (60, 8)
    assert training["config"]["use_dora"] is True
    assert training["loss_after"] < training["loss_before"]

    # The report: a falling loss, three questions asked three ways, nothing left applied.
    loss = re.search(r"^loss (\d+\.\d+) -> (\d+\.\d+) in (\d+) steps", out, flags=re.M)
    assert loss is not None
    assert float(loss[2]) < float(loss[1])
    assert int(loss[3]) == training["steps"]
    before = re.findall(r"^A: (.*)$", out, flags=re.M)
    adapted = re.findall(r"^A \(with adapter\): +(.*)$", out, flags=re.M)
    bypassed = re.findall(r"^A \(use_adapter=False\): (.*)$", out, flags=re.M)
    assert len(before) == len(adapted) == len(bypassed) == 3
    assert bypassed == before  # use_adapter=False really is the base model
    assert "after the with-block: engine.is_loaded = False" in out
    assert f"wdva chat {vault_path} --key-file {key_path}" in out


def test_hot_swap_keeps_two_people_apart(
    llama_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "work"
    args = ["--base-model", str(llama_dir), "--device", "cpu", "--workdir", str(workdir)]
    hot_swap = load_example("hot_swap")
    code = hot_swap.main([*args, *FAST, *SHORT_ANSWERS])
    out = capsys.readouterr().out
    assert code == 0

    people = ["alice", "bob"]
    expected_files = ["alice.key", "alice.wdva", "bob.key", "bob.wdva"]
    assert sorted(p.name for p in workdir.iterdir()) == expected_files
    for name in expected_files:
        assert_private(workdir / name)
    keys = {who: SecretKey.from_file(workdir / f"{who}.key") for who in people}
    assert keys["alice"] != keys["bob"]

    # Each vault opens with its owner's key only, and was trained on that person's facts.
    for owner, intruder in (("alice", "bob"), ("bob", "alice")):
        vault_path = workdir / f"{owner}.wdva"
        training = AdapterBundle.load(vault_path, key=keys[owner]).info["training"]
        assert (training["num_texts"], training["num_examples"]) == (3, 3)
        assert training["loss_after"] < training["loss_before"]
        with pytest.raises(WrongKeyError):
            AdapterBundle.load(vault_path, key=keys[intruder])
        assert f"(b) {intruder}'s key on {owner}.wdva -> WrongKeyError: wrong key" in out

    # The report covers every adapter/owner pair and shows a clean engine after each request.
    losses = re.findall(r"^(\w+): loss (\d+\.\d+) -> (\d+\.\d+), sealed", out, flags=re.M)
    assert [who for who, _, _ in losses] == people
    assert all(float(after) < float(before) for _, before, after in losses)
    recalls = re.findall(
        r"^\(a\) (\w+)'s adapter recalled [0-3]/3 of (\w+)'s facts$", out, flags=re.M
    )
    assert recalls == [(adapter, owner) for adapter in people for owner in people]
    assert out.count("(c) after the request: engine.is_loaded = False") == 2
    assert "engine.is_loaded = True" not in out

    # A second run in the same workdir reuses the vaults instead of training again, and
    # (greedy decoding, same adapters) serves exactly the same answers.
    sealed = {name: (workdir / name).read_bytes() for name in expected_files}
    assert hot_swap.main([*args, *FAST, *SHORT_ANSWERS]) == 0
    again = capsys.readouterr().out
    assert {name: (workdir / name).read_bytes() for name in expected_files} == sealed
    enrolment, serving, answers = again.partition("\n== request from alice ==")
    assert [f"{who}: reusing {who}.wdva" in enrolment for who in people] == [True, True]
    assert "sealed" not in enrolment
    assert serving
    assert answers == out.partition(serving)[2]


# The script's PEFT half uses a stock LoraConfig; on GPT-2's Conv1D layers PEFT corrects
# `fan_in_fan_out` itself and says so.
@pytest.mark.filterwarnings("ignore:fan_in_fan_out is set to False:UserWarning")
def test_seal_existing_adapter_round_trips_through_a_vault(
    model_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "work"
    args = ["--base-model", str(model_dir), "--workdir", str(workdir), "--epochs", "3"]
    code = load_example("seal_existing_adapter").main(args)
    out = capsys.readouterr().out
    assert code == 0

    # The plaintext PEFT directory is gone; key, vault and the deliberate export remain.
    vault_path, exported = workdir / "adapter.wdva", workdir / "exported-adapter"
    assert sorted(p.name for p in workdir.iterdir()) == [
        "adapter.key",
        "adapter.wdva",
        "exported-adapter",
    ]
    assert not (workdir / "plain-adapter").exists()
    assert is_vault(vault_path)
    assert "adapter_model.safetensors" in out
    assert "plaintext deleted" in out

    # The vault holds a plain LoRA adapter, and the export is that adapter exactly.
    bundle = AdapterBundle.load(vault_path, key=SecretKey.from_file(workdir / "adapter.key"))
    assert bundle.info == {"base_model": str(model_dir)}
    assert (bundle.adapter_config["peft_type"], bundle.adapter_config["r"]) == ("LORA", 8)
    assert bundle.summary()["use_dora"] is False
    assert sorted(p.name for p in exported.iterdir()) == [
        "adapter_config.json",
        "adapter_model.safetensors",
    ]
    assert (exported / "adapter_model.safetensors").read_bytes() == bundle.weights
    exported_config = json.loads((exported / "adapter_config.json").read_text(encoding="utf-8"))
    assert exported_config == dict(bundle.adapter_config)
    assert "weights byte-identical to step 1: True" in out

    # The numbers it prints: the adapter does something, matches PEFT, and unloads cleanly.
    assert "'algorithm': 'XChaCha20-Poly1305'" in out
    numbers = dict(re.findall(r"^   largest ([a-zA-Z' ().]+): +(\d+\.\d+)$", out, flags=re.M))
    assert float(numbers["logit change caused by the adapter"]) > 0
    assert float(numbers["difference from PEFT's own result"]) < 1e-4
    assert float(numbers["change left after engine.unload()"]) == 0
