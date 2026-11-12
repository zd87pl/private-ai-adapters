"""End to end: train -> seal -> load from ciphertext -> generate -> unload.

These tests exist to make one failure impossible to reintroduce: an adapter that
is decrypted but never actually applied. Every claim is checked on the model's
logits, on both a Conv1D (GPT-2) and a Linear (Llama) architecture.
"""

from __future__ import annotations

import itertools
import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from wdva import (
    AdapterBundle,
    AdapterMismatchError,
    IntegrityError,
    SecretKey,
    TrainingError,
    WDVAError,
    WrongKeyError,
    is_vault,
)

pytestmark = pytest.mark.ml

FACTS = [
    "the greenhouse door code is four seven one nine",
    "marguerite looks after the compost bins",
    "tobias handles the seed orders",
]
PROBE = "the greenhouse door code is"


def quick_config(**overrides: Any) -> Any:
    from wdva import TrainingConfig

    settings: dict[str, Any] = {
        "rank": 8,
        "alpha": 16,
        "dropout": 0.0,
        "epochs": 60,
        "batch_size": 4,
        "learning_rate": 2e-2,
        "max_length": 64,
    }
    settings.update(overrides)
    return TrainingConfig(**settings)


def make_engine(model_dir: Path) -> Any:
    from wdva import WDVA

    return WDVA(str(model_dir), device="cpu")


def logits(engine: Any, text: str = PROBE) -> Any:
    import torch

    ids = engine.tokenizer(text, return_tensors="pt")
    with torch.no_grad():
        return engine.model(**ids).logits.clone()


def text_loss(engine: Any, texts: list[str]) -> float:
    from wdva import _ml, training

    seqs = training.build_sequences(engine.tokenizer, texts=texts, max_length=64, overlap=4)
    return training._mean_loss(
        engine.model, seqs, batch_size=4, pad_id=_ml.pad_token_id(engine.tokenizer), device="cpu"
    )


@pytest.fixture
def engine(model_dir: Path) -> Any:
    eng = make_engine(model_dir)
    yield eng
    eng.close()


@pytest.fixture
def trained(engine: Any, tmp_path: Path, key: SecretKey) -> Path:
    path = tmp_path / "facts.wdva"
    engine.train(path, key=key, texts=FACTS, config=quick_config())
    return path


# -- the core promise ------------------------------------------------------


@pytest.mark.parametrize("use_dora", [True, False], ids=["dora", "lora"])
def test_adapter_is_really_trained_sealed_applied_and_removed(
    engine: Any, tmp_path: Path, key: SecretKey, use_dora: bool
) -> None:
    import torch

    base_logits = logits(engine)
    base_loss = text_loss(engine, FACTS)
    out_dir = tmp_path / "out"

    result = engine.train(
        out_dir / "facts.wdva", key=key, texts=FACTS, config=quick_config(use_dora=use_dora)
    )

    # Training did real work and wrote exactly one file: the encrypted vault.
    assert result.steps == 60
    assert result.loss_after < result.loss_before * 0.1
    assert result.trainable_parameters > 0
    assert [p.name for p in out_dir.iterdir()] == ["facts.wdva"]
    assert is_vault(result.adapter_path)
    assert result.vault_bytes == result.adapter_path.stat().st_size
    if os.name == "posix":
        assert stat.S_IMODE(result.adapter_path.stat().st_mode) == 0o600

    # Training handed the base model back untouched.
    assert not engine.is_loaded
    assert torch.equal(logits(engine), base_logits)

    # Loading changes the model's behaviour, in the direction of the training data.
    info = engine.load(result.adapter_path, key=key)
    assert engine.is_loaded
    assert info.use_dora is use_dora
    assert info.rank == 8
    assert info.parameters == result.trainable_parameters
    adapted_logits = logits(engine)
    assert (adapted_logits - base_logits).abs().max() > 1e-2
    assert text_loss(engine, FACTS) < base_loss * 0.1

    # Unloading restores the base model bit for bit.
    engine.unload()
    assert not engine.is_loaded
    assert engine.adapter_info is None
    assert torch.equal(logits(engine), base_logits)
    engine.unload()  # idempotent


def test_vault_records_private_details_only_inside_the_ciphertext(
    trained: Path, key: SecretKey, model_dir: Path
) -> None:
    raw = trained.read_bytes()
    for leak in (b"lora_", str(model_dir).encode(), b"loss_after", b"greenhouse"):
        assert leak not in raw
    bundle = AdapterBundle.load(trained, key=key)
    assert bundle.base_model == str(model_dir)
    training = bundle.info["training"]
    assert training["steps"] == 60
    assert training["loss_after"] < training["loss_before"]
    assert training["config"]["rank"] == 8
    assert bundle.adapter_config["peft_type"] == "LORA"
    assert bundle.adapter_config["inference_mode"] is True


def test_unload_zeroes_the_adapter_weights_in_place(
    engine: Any, trained: Path, key: SecretKey
) -> None:
    engine.load(trained, key=key)
    adapter_params = [p for name, p in engine.model.named_parameters() if "lora_" in name]
    assert adapter_params
    assert any(float(p.abs().sum()) > 0 for p in adapter_params)
    engine.unload()
    assert all(float(p.abs().sum()) == 0 for p in adapter_params)


def test_context_manager_removes_the_adapter_even_on_error(
    engine: Any, trained: Path, key: SecretKey
) -> None:
    import torch

    base_logits = logits(engine)
    seen: dict[str, Any] = {}

    def use_then_fail() -> None:
        with engine.adapter(trained, key=key) as info:
            seen["source"] = info.source
            seen["loaded"] = engine.is_loaded
            seen["changed"] = not torch.equal(logits(engine), base_logits)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        use_then_fail()
    assert seen == {"source": str(trained), "loaded": True, "changed": True}
    assert not engine.is_loaded
    assert torch.equal(logits(engine), base_logits)


def test_use_adapter_false_answers_with_the_base_model(
    engine: Any, trained: Path, key: SecretKey
) -> None:
    base = engine.generate(PROBE, temperature=0, max_new_tokens=8, raw=True)
    with engine.adapter(trained, key=key):
        adapted = engine.generate(PROBE, temperature=0, max_new_tokens=8, raw=True)
        bypassed = engine.generate(
            PROBE, temperature=0, max_new_tokens=8, raw=True, use_adapter=False
        )
        again = engine.generate(PROBE, temperature=0, max_new_tokens=8, raw=True)
    assert bypassed == base
    assert adapted == again
    assert adapted != base


def test_memorised_text_is_reproduced(engine: Any, trained: Path, key: SecretKey) -> None:
    with engine.adapter(trained, key=key):
        completion = engine.generate(PROBE, temperature=0, max_new_tokens=24, raw=True)
        bypassed = engine.generate(
            PROBE, temperature=0, max_new_tokens=24, raw=True, use_adapter=False
        )
    assert completion == "four seven one nine"
    assert "four seven one nine" not in bypassed


# -- hot swapping ----------------------------------------------------------


def test_hot_swap_between_adapters(engine: Any, tmp_path: Path) -> None:
    import torch

    key_a, key_b = SecretKey.generate(), SecretKey.generate()
    engine.train(tmp_path / "a.wdva", key=key_a, texts=FACTS[:1], config=quick_config())
    engine.train(tmp_path / "b.wdva", key=key_b, texts=FACTS[1:], config=quick_config(seed=7))
    base_logits = logits(engine)

    engine.load(tmp_path / "a.wdva", key=key_a)
    logits_a = logits(engine)
    engine.load(tmp_path / "b.wdva", key=key_b)  # replaces A without an explicit unload
    logits_b = logits(engine)
    engine.load(tmp_path / "a.wdva", key=key_a)
    logits_a_again = logits(engine)
    engine.unload()

    assert not torch.equal(logits_a, logits_b)
    assert torch.equal(logits_a, logits_a_again)
    assert torch.equal(logits(engine), base_logits)

    # One user's key does not open another user's adapter.
    with pytest.raises(WrongKeyError):
        engine.load(tmp_path / "b.wdva", key=key_a)
    assert not engine.is_loaded


# -- failures must be loud and leave the model clean -----------------------


def test_wrong_key_and_tampering_fail_at_load_time(
    engine: Any, trained: Path, key: SecretKey
) -> None:
    import torch

    base_logits = logits(engine)
    with pytest.raises(WrongKeyError):
        engine.load(trained, key=SecretKey.generate())
    data = bytearray(trained.read_bytes())
    data[len(data) // 2] ^= 0xFF
    trained.write_bytes(data)
    with pytest.raises(IntegrityError):
        engine.load(trained, key=key)
    assert not engine.is_loaded
    assert torch.equal(logits(engine), base_logits)


def test_a_failed_load_keeps_nothing_applied_even_if_an_adapter_was_loaded(
    engine: Any, trained: Path, key: SecretKey
) -> None:
    engine.load(trained, key=key)
    with pytest.raises(WrongKeyError):
        engine.load(trained, key=SecretKey.generate())
    # The decryption failure happened before the model was touched.
    assert engine.is_loaded


def test_adapter_for_a_different_architecture_is_rejected(
    model_dirs: dict[str, Path], tmp_path: Path, key: SecretKey
) -> None:
    import torch

    gpt2, llama = make_engine(model_dirs["gpt2"]), make_engine(model_dirs["llama"])
    try:
        gpt2.train(tmp_path / "g.wdva", key=key, texts=FACTS, config=quick_config(epochs=20))
        base_logits = logits(llama)
        with (
            pytest.warns(UserWarning, match="was made for"),
            pytest.raises(AdapterMismatchError, match="does not fit"),
        ):
            llama.load(tmp_path / "g.wdva", key=key)
        assert not llama.is_loaded
        assert torch.equal(logits(llama), base_logits)
    finally:
        gpt2.close()
        llama.close()


def _rebuilt(bundle: AdapterBundle, *, config: dict | None = None, drop: int = 0) -> AdapterBundle:
    from safetensors.torch import load, save

    tensors = load(bundle.weights)
    for name in sorted(tensors)[:drop]:
        del tensors[name]
    return AdapterBundle(
        {**bundle.adapter_config, **(config or {})}, save(tensors), dict(bundle.info)
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"config": {"r": 16}}, "shape"),
        ({"drop": 1}, "missing"),
        ({"config": {"target_modules": ["no_such_module"]}}, "does not fit"),
        ({"config": {"peft_type": "PREFIX_TUNING"}}, "unsupported adapter type"),
        ({"config": {"peft_type": None}}, "unsupported adapter type"),
    ],
)
def test_adapters_that_do_not_fit_are_rejected_and_rolled_back(
    engine: Any, trained: Path, key: SecretKey, change: dict, message: str
) -> None:
    import torch

    base_logits = logits(engine)
    names_before = [name for name, _ in engine.model.named_parameters()]
    broken = _rebuilt(AdapterBundle.load(trained, key=key), **change)
    with pytest.raises(AdapterMismatchError, match=message):
        engine.load(broken)
    assert not engine.is_loaded
    assert [name for name, _ in engine.model.named_parameters()] == names_before
    assert torch.equal(logits(engine), base_logits)


def test_initialisation_hooks_in_the_config_are_neutralised(
    engine: Any, trained: Path, key: SecretKey
) -> None:
    import torch

    bundle = AdapterBundle.load(trained, key=key)
    engine.load(bundle)
    expected = logits(engine)
    hostile = _rebuilt(
        bundle,
        config={
            "init_lora_weights": "loftq",
            "loftq_config": {"loftq_bits": 4},
            "megatron_config": {"anything": 1},
            "megatron_core": "os",
            "inference_mode": False,
            "key_from_the_future": True,
        },
    )
    with pytest.warns(UserWarning, match="Unexpected keyword"):
        engine.load(hostile)
    assert torch.equal(logits(engine), expected)
    assert not any(p.requires_grad for p in engine.model.parameters())


def test_base_model_name_mismatch_warns_but_structure_decides(
    model_dir: Path, tmp_path: Path, trained: Path, key: SecretKey
) -> None:
    from wdva import BaseModelMismatchWarning

    twin = tmp_path / "twin"
    shutil.copytree(model_dir, twin)
    other = make_engine(twin)
    try:
        with pytest.warns(BaseModelMismatchWarning, match="was made for"):
            other.load(trained, key=key)
        assert other.is_loaded
    finally:
        other.close()


# -- generation ------------------------------------------------------------


def test_generate_returns_only_new_text_and_is_deterministic(engine: Any) -> None:
    first = engine.generate(PROBE, temperature=0, max_new_tokens=5)
    second = engine.generate(PROBE, temperature=0, max_new_tokens=5)
    assert first == second
    assert PROBE not in first
    sampled = engine.generate(PROBE, temperature=1.0, max_new_tokens=5)
    assert isinstance(sampled, str)


def test_stream_matches_generate(engine: Any) -> None:
    whole = engine.generate(PROBE, temperature=0, max_new_tokens=12, raw=True)
    pieces = list(engine.stream(PROBE, temperature=0, max_new_tokens=12, raw=True))
    assert "".join(pieces).strip() == whole


def test_abandoning_a_stream_releases_the_engine(engine: Any) -> None:
    stream = engine.stream(PROBE, temperature=0, max_new_tokens=64, raw=True)
    next(stream)
    stream.close()
    assert isinstance(engine.generate(PROBE, temperature=0, max_new_tokens=2), str)


def test_stream_surfaces_generation_errors(engine: Any) -> None:
    with pytest.raises(ValueError, match="no_such_generation_argument"):
        list(engine.stream(PROBE, max_new_tokens=4, no_such_generation_argument=1))
    assert isinstance(engine.generate(PROBE, temperature=0, max_new_tokens=2), str)


def test_chat_messages_and_system_prompts(llama_dir: Path, gpt2_dir: Path) -> None:
    from wdva import _ml

    for directory, expect_template in ((llama_dir, True), (gpt2_dir, False)):
        eng = make_engine(directory)
        try:
            text, templated = _ml.format_prompt(eng.tokenizer, "hello", system="be brief")
            assert templated is expect_template
            if expect_template:
                assert text == "<|system|>\nbe brief</s>\n<|user|>\nhello</s>\n<|assistant|>\n"
            else:
                assert text == "hello"
            raw_text, raw_templated = _ml.format_prompt(eng.tokenizer, "hello", raw=True)
            assert (raw_text, raw_templated) == ("hello", False)
            messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "who handles the seed orders"},
            ]
            assert isinstance(eng.generate(messages, temperature=0, max_new_tokens=4), str)
            transcript, _ = _ml.format_prompt(eng.tokenizer, messages)
            if not expect_template:
                assert transcript.endswith("user: who handles the seed orders\nassistant:")
        finally:
            eng.close()


def test_generate_argument_validation(engine: Any) -> None:
    with pytest.raises(ValueError, match="max_new_tokens"):
        engine.generate(PROBE, max_new_tokens=0)
    with pytest.raises(ValueError, match="temperature"):
        engine.generate(PROBE, temperature=-1)
    for blank in ("", "  \n"):
        with pytest.raises(ValueError, match="empty"):
            engine.generate(blank, raw=True)
    with pytest.raises(ValueError, match="empty"):
        engine.generate([])
    with pytest.raises(TypeError, match="role"):
        engine.generate([{"role": "user"}])


def test_concurrent_generation_is_serialised(engine: Any, trained: Path, key: SecretKey) -> None:
    expected = engine.generate(PROBE, temperature=0, max_new_tokens=6, raw=True)
    results: list[str] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            results.append(engine.generate(PROBE, temperature=0, max_new_tokens=6, raw=True))
        except BaseException as exc:
            errors.append(exc)

    def swap() -> None:
        try:
            with engine.adapter(trained, key=key):
                pass
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    threads.insert(2, threading.Thread(target=swap))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    # Whatever the interleaving, base-model generations never saw a half-applied adapter.
    assert results == [expected] * 4


# -- interop with plain PEFT -----------------------------------------------


def test_vault_exports_to_a_directory_peft_can_load(
    engine: Any, model_dir: Path, tmp_path: Path, trained: Path, key: SecretKey
) -> None:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM

    with engine.adapter(trained, key=key):
        ours = logits(engine)
    exported = AdapterBundle.load(trained, key=key).to_peft_dir(tmp_path / "plain")
    base = AutoModelForCausalLM.from_pretrained(model_dir).eval()
    theirs_model = PeftModel.from_pretrained(base, exported).eval()
    ids = engine.tokenizer(PROBE, return_tensors="pt")
    with torch.no_grad():
        theirs = theirs_model(**ids).logits
    assert torch.allclose(ours, theirs, atol=1e-6)


def test_an_adapter_saved_by_peft_can_be_sealed_and_loaded(
    engine: Any, model_dir: Path, tmp_path: Path, key: SecretKey
) -> None:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM

    torch.manual_seed(3)
    base = AutoModelForCausalLM.from_pretrained(model_dir).eval()
    peft_model = get_peft_model(
        base, LoraConfig(r=2, target_modules="all-linear", task_type="CAUSAL_LM")
    )
    with torch.no_grad():
        for name, param in peft_model.named_parameters():
            if "lora_B" in name:
                param.normal_(0, 0.3)
    peft_model.eval()
    ids = engine.tokenizer(PROBE, return_tensors="pt")
    with torch.no_grad():
        theirs = peft_model(**ids).logits
    peft_model.save_pretrained(tmp_path / "peft_out")

    vault_path = AdapterBundle.from_peft_dir(tmp_path / "peft_out").save(
        tmp_path / "sealed.wdva", key=key
    )
    with engine.adapter(vault_path, key=key):
        ours = logits(engine)
    assert torch.allclose(ours, theirs, atol=1e-6)


# -- training details ------------------------------------------------------


def test_training_restores_a_supplied_model_exactly(
    engine: Any, tmp_path: Path, key: SecretKey
) -> None:
    import torch

    model = engine.model
    for param in list(model.parameters())[:2]:
        param.requires_grad_(True)
    flags = [p.requires_grad for p in model.parameters()]
    before = logits(engine)
    engine.train(tmp_path / "x.wdva", key=key, texts=FACTS, config=quick_config(epochs=20))
    assert engine.model is model
    assert not model.training
    assert [p.requires_grad for p in model.parameters()] == flags
    assert torch.equal(logits(engine), before)
    assert not any("lora" in name for name, _ in model.named_parameters())


def test_standalone_train_adapter_with_a_passphrase(model_dir: Path, tmp_path: Path) -> None:
    from wdva import train_adapter

    result = train_adapter(
        str(model_dir),
        tmp_path / "pw.wdva",
        passphrase="correct horse",
        texts=FACTS,
        config=quick_config(epochs=20),
        device="cpu",
    )
    assert result.base_model == str(model_dir)
    eng = make_engine(model_dir)
    try:
        with pytest.raises(WrongKeyError):
            eng.load(result.adapter_path, passphrase="wrong")
        eng.load(result.adapter_path, passphrase="correct horse")
        assert eng.is_loaded
    finally:
        eng.close()


def test_documents_and_examples_can_be_mixed(engine: Any, tmp_path: Path, key: SecretKey) -> None:
    doc = tmp_path / "notes.txt"
    doc.write_text("\n".join(FACTS), encoding="utf-8")
    steps: list[tuple[int, int, float]] = []
    result = engine.train(
        tmp_path / "mix.wdva",
        key=key,
        documents=[doc],
        examples=[
            {"prompt": "who handles the seed orders", "response": "tobias"},
            {
                "messages": [
                    {"role": "user", "content": "what is the greenhouse door code"},
                    {"role": "assistant", "content": "four seven one nine"},
                ]
            },
        ],
        config=quick_config(epochs=30),
        on_step=lambda step, total, loss: steps.append((step, total, loss)),
    )
    assert result.num_sequences >= 3  # at least one document window plus two examples
    assert [s for s, _, _ in steps] == list(range(1, result.steps + 1))
    assert all(total == result.steps for _, total, _ in steps)
    assert result.loss_after < result.loss_before
    info = AdapterBundle.load(result.adapter_path, key=key).info["training"]
    assert info["documents"] == ["notes.txt"]
    assert info["num_examples"] == 2


def test_example_prompts_are_masked_out_of_the_loss(llama_dir: Path, gpt2_dir: Path) -> None:
    from wdva import _ml, training

    for directory in (llama_dir, gpt2_dir):
        eng = make_engine(directory)
        try:
            tok = eng.tokenizer
            example = {"prompt": "who handles the seed orders", "response": "tobias"}
            ((ids, labels),) = training.build_sequences(
                tok, examples=[example], max_length=64, overlap=4
            )
            prompt_text, templated = _ml.format_prompt(tok, example["prompt"])
            prompt_ids = _ml.encode_text(tok, prompt_text, templated=templated)
            # The sequence starts with exactly what generate() will feed the model...
            assert ids[: len(prompt_ids)] == prompt_ids
            # ...none of which is learned, while all of the response is.
            assert labels[: len(prompt_ids)] == [-100] * len(prompt_ids)
            assert labels[len(prompt_ids) :] == ids[len(prompt_ids) :]
            learned = tok.decode(ids[len(prompt_ids) :])
            assert "tobias" in learned
            assert tok.eos_token_id in ids[len(prompt_ids) :]
        finally:
            eng.close()


def test_document_chunking_respects_length_and_overlap(model_dir: Path) -> None:
    from wdva import _ml, training

    eng = make_engine(model_dir)
    try:
        tok = eng.tokenizer
        text = " ".join(FACTS * 6)
        # A document is tokenised exactly like a raw prompt, then closed with EOS.
        expected = [*_ml.encode_text(tok, text, templated=False), tok.eos_token_id]
        seqs = training.build_sequences(tok, texts=[text], max_length=16, overlap=4)
        assert len(seqs) > 2
        assert all(2 <= len(ids) <= 16 for ids, _ in seqs)

        first_ids, first_labels = seqs[0]
        assert first_labels == first_ids
        rebuilt = list(first_ids)
        for (prev_ids, _), (ids, labels) in itertools.pairwise(seqs):
            assert ids[:4] == prev_ids[-4:]  # windows share `overlap` tokens of context...
            assert labels[:4] == [-100] * 4  # ...which are not learned twice
            assert labels[4:] == ids[4:]
            rebuilt.extend(ids[4:])
        assert rebuilt == expected  # every token is a target exactly once

        # A prompt that opens the document tokenises to a prefix of the first window.
        opening = _ml.encode_text(tok, " ".join(FACTS[0].split()[:3]), templated=False)
        assert first_ids[: len(opening)] == opening

        ((short_ids, short_labels),) = training.build_sequences(
            tok, texts=["hello world"], max_length=16, overlap=4
        )
        assert short_ids[-1] == tok.eos_token_id
        assert short_labels == short_ids
    finally:
        eng.close()


def test_training_input_validation(engine: Any, tmp_path: Path, key: SecretKey) -> None:
    from wdva import TrainingConfig, TrainingWarning, train_adapter

    out = tmp_path / "v.wdva"
    with pytest.raises(TrainingError, match="nothing to train on"):
        engine.train(out, key=key)
    with pytest.raises(TypeError, match="exactly one"):
        engine.train(out, texts=FACTS)
    with pytest.raises(FileNotFoundError, match="document not found"):
        engine.train(out, key=key, documents=[tmp_path / "missing.txt"])
    with pytest.raises(TrainingError, match="no usable"):
        engine.train(out, key=key, texts=["   "])
    with pytest.raises(TrainingError, match="prompt"):
        engine.train(out, key=key, examples=[{"question": "x"}])
    with pytest.raises(TrainingError, match="assistant"):
        engine.train(out, key=key, examples=[{"messages": [{"role": "user", "content": "x"}]}])
    with pytest.raises(TrainingError, match="response"):
        engine.train(out, key=key, examples=[{"prompt": "x", "response": " "}])
    with pytest.raises(TrainingError, match="no room"):
        engine.train(
            out,
            key=key,
            examples=[{"prompt": " ".join(FACTS * 5), "response": "x"}],
            config=quick_config(max_length=8),
        )
    assert not out.exists()
    assert not engine.is_loaded

    out.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        engine.train(out, key=key, texts=FACTS)
    assert out.read_bytes() == b"precious"

    with pytest.warns(TrainingWarning, match="only 1 optimizer steps"):
        engine.train(out, key=key, texts=FACTS, config=quick_config(epochs=1), overwrite=True)
    assert is_vault(out)

    model = engine.model
    model.config._name_or_path = ""
    with pytest.raises(TrainingError, match="base_model_name"):
        train_adapter((model, engine.tokenizer), tmp_path / "n.wdva", key=key, texts=FACTS)

    for bad in (
        {"rank": 0},
        {"epochs": 0},
        {"dropout": 1.0},
        {"learning_rate": 0},
        {"learning_rate": float("inf")},
        {"learning_rate": float("nan")},
        {"max_length": 4},
        {"overlap": 300},
        {"batch_size": 0},
        {"max_steps": 0},
    ):
        with pytest.raises(ValueError, match=next(iter(bad))):
            TrainingConfig(**bad)


def test_non_finite_loss_is_reported_not_sealed(
    model_dir: Path, tmp_path: Path, key: SecretKey
) -> None:
    """If the loss stops being a number, training must stop and nothing may be sealed."""
    import torch

    engine = make_engine(model_dir)
    try:
        model = engine.model
        names_before = [name for name, _ in model.named_parameters()]
        with torch.no_grad():
            list(model.parameters())[-1].fill_(float("nan"))  # every logit becomes NaN
        out = tmp_path / "nan.wdva"
        with pytest.raises(TrainingError, match=r"diverged at step 1 \(loss=nan\)"):
            engine.train(out, key=key, texts=FACTS, config=quick_config())
        assert not out.exists()
        assert list(tmp_path.iterdir()) == []
        assert not engine.is_loaded
        assert [name for name, _ in model.named_parameters()] == names_before
    finally:
        engine.close()


def test_max_steps_caps_training(engine: Any, tmp_path: Path, key: SecretKey) -> None:
    result = engine.train(
        tmp_path / "s.wdva", key=key, texts=FACTS, config=quick_config(epochs=50, max_steps=21)
    )
    assert result.steps == 21


def test_training_while_an_adapter_is_loaded_is_refused(
    engine: Any, trained: Path, tmp_path: Path, key: SecretKey
) -> None:
    engine.load(trained, key=key)
    with pytest.raises(WDVAError, match="unload"):
        engine.train(tmp_path / "second.wdva", key=key, texts=FACTS)


# -- engine odds and ends --------------------------------------------------


def test_engine_is_lazy_and_closes_cleanly(model_dir: Path) -> None:
    from wdva import WDVA

    eng = WDVA(str(model_dir), device="cpu")
    assert eng._base is None
    assert "no adapter" in repr(eng)
    assert eng.base_model == str(model_dir)
    with eng:
        assert eng.device == "cpu"
        assert eng._base is not None
    assert eng._base is None
    with pytest.raises(TypeError):
        WDVA("")


def test_from_loaded_wraps_an_existing_model(
    model_dir: Path, trained: Path, key: SecretKey
) -> None:
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from wdva import WDVA

    model = AutoModelForCausalLM.from_pretrained(model_dir)
    eng = WDVA.from_loaded(model, AutoTokenizer.from_pretrained(model_dir), name=str(model_dir))
    with eng.adapter(trained, key=key):
        assert eng.is_loaded
        assert eng.model is not model
    assert eng.model is model


def test_dtype_and_device_resolution() -> None:
    import torch

    from wdva import _ml

    assert _ml.resolve_device("cpu") == "cpu"
    assert _ml.resolve_device(None) in {"cpu", "cuda", "mps"}
    assert _ml.resolve_dtype(None, "cpu") is torch.float32
    assert _ml.resolve_dtype("auto", "cuda") is torch.float16
    assert _ml.resolve_dtype("bfloat16", "cpu") is torch.bfloat16
    assert _ml.resolve_dtype(torch.float64, "cpu") is torch.float64
    with pytest.raises(ValueError, match="unknown dtype"):
        _ml.resolve_dtype("float7", "cpu")
    with pytest.raises(ValueError, match="unknown dtype"):
        _ml.resolve_dtype("nn", "cpu")


def test_half_precision_engine_trains_in_float32_and_restores_dtype(
    llama_dir: Path, tmp_path: Path, key: SecretKey
) -> None:
    import torch

    from wdva import WDVA

    eng = WDVA(str(llama_dir), device="cpu", dtype="bfloat16")
    try:
        before = {name: p.clone() for name, p in eng.model.named_parameters()}
        result = eng.train(
            tmp_path / "h.wdva", key=key, texts=FACTS, config=quick_config(epochs=20)
        )
        assert result.loss_after < result.loss_before
        assert eng.model.dtype == torch.bfloat16
        assert all(torch.equal(p, before[name]) for name, p in eng.model.named_parameters())
        eng.load(result.adapter_path, key=key)
        assert isinstance(eng.generate(PROBE, temperature=0, max_new_tokens=3), str)
    finally:
        eng.close()
