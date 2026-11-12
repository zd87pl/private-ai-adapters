"""The ML-backed CLI commands, end to end against a tiny local model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wdva import AdapterBundle, SecretKey, cli, is_vault

from .test_cli import run
from .test_pipeline import FACTS

pytestmark = pytest.mark.ml

TRAIN_FLAGS = [
    "--epochs", "60",
    "--rank", "8",
    "--learning-rate", "0.02",
    "--max-length", "64",
]  # fmt: skip


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (cli.ENV_KEY, cli.ENV_KEY_FILE, cli.ENV_PASSPHRASE):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "notes.txt").write_text("\n".join(FACTS), encoding="utf-8")
    (tmp_path / "qa.jsonl").write_text(
        json.dumps({"prompt": "who handles the seed orders", "response": "tobias"}) + "\n\n",
        encoding="utf-8",
    )
    assert run("keygen", "-o", tmp_path / "k.key") == 0
    return tmp_path


def train(workspace: Path, model_dir: Path, *extra: str) -> int:
    return run(
        "train", workspace / "notes.txt",
        "--examples", workspace / "qa.jsonl",
        "-o", workspace / "notes.wdva",
        "--key-file", workspace / "k.key",
        "--base-model", model_dir,
        "--device", "cpu",
        *TRAIN_FLAGS, *extra,
    )  # fmt: skip


def test_train_query_and_compare(
    workspace: Path, llama_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert train(workspace, llama_dir) == 0
    captured = capsys.readouterr()
    assert "Sealed adapter ->" in captured.out
    assert "loss" in captured.out
    assert "step" in captured.err
    vault = workspace / "notes.wdva"
    assert is_vault(vault)
    assert sorted(p.name for p in workspace.iterdir()) == [
        "k.key", "notes.txt", "notes.wdva", "qa.jsonl",
    ]  # fmt: skip

    bundle = AdapterBundle.load(vault, key=SecretKey.from_file(workspace / "k.key"))
    assert bundle.base_model == str(llama_dir)
    assert bundle.adapter_config["use_dora"] is True
    assert bundle.adapter_config["r"] == 8
    assert bundle.adapter_config["lora_alpha"] == 16
    assert bundle.info["training"]["documents"] == ["notes.txt"]

    assert run("inspect", vault, "--key-file", workspace / "k.key") == 0
    text = capsys.readouterr().out
    assert "DoRA, rank 8, alpha 16" in text
    assert "training" in text

    # The base model is read from the vault; no --base-model needed.
    common = ["--key-file", workspace / "k.key", "--temperature", "0", "--max-new-tokens", "6",
              "--device", "cpu"]  # fmt: skip
    assert run("query", vault, "who handles the seed orders", *common, "--compare") == 0
    out = capsys.readouterr().out
    base_part, adapter_part = out.split("--- with adapter ---")
    assert base_part.startswith("--- base model ---")
    assert "tobias" in adapter_part
    assert "tobias" not in base_part

    assert train(workspace, llama_dir) == 1  # refuses to overwrite
    assert "exists" in capsys.readouterr().err


def test_train_plain_lora_with_force(workspace: Path, gpt2_dir: Path) -> None:
    (workspace / "notes.wdva").write_bytes(b"old")
    assert train(workspace, gpt2_dir, "--lora", "--force", "--max-steps", "25", "--seed", "3") == 0
    bundle = AdapterBundle.load(
        workspace / "notes.wdva", key=SecretKey.from_file(workspace / "k.key")
    )
    assert bundle.adapter_config["use_dora"] is False
    assert bundle.info["training"]["steps"] == 25
    assert bundle.info["training"]["config"]["seed"] == 3


def test_chat_session(
    workspace: Path,
    llama_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert train(workspace, llama_dir) == 0
    capsys.readouterr()
    lines = iter(["", "who handles the seed orders", "/reset", "who handles the seed orders"])

    def fake_input(prompt: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    assert run(
        "chat", workspace / "notes.wdva", "--key-file", workspace / "k.key",
        "--temperature", "0", "--max-new-tokens", "6", "--device", "cpu",
    ) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert out.count("ai> ") == 2
    assert out.count("tobias") == 2


def test_query_with_wrong_key_never_loads_a_model(
    workspace: Path,
    llama_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert train(workspace, llama_dir) == 0
    assert run("keygen", "-o", workspace / "other.key") == 0
    from wdva import _ml

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the base model must not be loaded before the vault opens")

    monkeypatch.setattr(_ml, "load_base_model", explode)
    capsys.readouterr()
    assert run("query", workspace / "notes.wdva", "hi", "--key-file", workspace / "other.key") == 1
    assert "wrong key" in capsys.readouterr().err


def test_query_needs_a_base_model_when_the_vault_records_none(
    tmp_path: Path, workspace: Path, llama_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert train(workspace, llama_dir) == 0
    key = SecretKey.from_file(workspace / "k.key")
    bundle = AdapterBundle.load(workspace / "notes.wdva", key=key)
    config = {k: v for k, v in bundle.adapter_config.items() if k != "base_model_name_or_path"}
    anonymous = AdapterBundle(config, bundle.weights).save(tmp_path / "anon.wdva", key=key)
    capsys.readouterr()
    assert run("query", anonymous, "hi", "--key-file", workspace / "k.key") == 2
    assert "--base-model" in capsys.readouterr().err
    assert run(
        "query", anonymous, "who handles the seed orders", "--key-file", workspace / "k.key",
        "--base-model", llama_dir, "--temperature", "0", "--max-new-tokens", "6",
    ) == 0  # fmt: skip
    assert "tobias" in capsys.readouterr().out
