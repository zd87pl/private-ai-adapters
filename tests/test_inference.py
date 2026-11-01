"""
Tests for wdva.inference and the WDVA facade, with mlx-lm's load/generate
stubbed so no model is downloaded. They pin the honest-status behaviour:
training is not implemented, and adapters are decrypted but not applied.
"""

from __future__ import annotations

import pytest

from wdva import WDVA
from wdva.crypto import EncryptedAdapter

mx = pytest.importorskip("mlx.core")
mlx_lm = pytest.importorskip("mlx_lm")

from wdva import inference  # noqa: E402
from wdva.inference import ADAPTER_NOT_APPLIED_WARNING, DEFAULT_MODEL, LocalInference  # noqa: E402


class FakeTokenizer:
    has_chat_template = True

    def apply_chat_template(self, messages, *, add_generation_prompt, tokenize):
        assert add_generation_prompt is True and tokenize is False
        return f"<chat>{messages[0]['content']}</chat>"


@pytest.fixture
def calls(monkeypatch):
    """Stub mlx_lm.load / generate and record how they were called."""
    recorded = {"load": [], "generate": []}

    def fake_load(name):
        recorded["load"].append(name)
        return object(), FakeTokenizer()

    def fake_generate(model, tokenizer, *, prompt, max_tokens, sampler, **kwargs):
        recorded["generate"].append(
            {"prompt": prompt, "max_tokens": max_tokens, "sampler": sampler, **kwargs}
        )
        return "answer"

    monkeypatch.setattr(mlx_lm, "load", fake_load)
    monkeypatch.setattr(mlx_lm, "generate", fake_generate)
    return recorded


@pytest.fixture
def adapter_file(tmp_path):
    key = EncryptedAdapter.generate_key()
    path = tmp_path / "adapter.wdva"
    EncryptedAdapter(key).encrypt_weights({"lora_a": mx.ones((4, 8), dtype=mx.bfloat16)}, path)
    return path, key.hex()


def test_default_model_is_used_and_named_model_is_honoured(calls):
    LocalInference().load_model()
    LocalInference(model_name="mlx-community/Some-Model-4bit").load_model()
    assert calls["load"] == [DEFAULT_MODEL, "mlx-community/Some-Model-4bit"]


def test_failed_load_does_not_fall_back_to_another_model(monkeypatch):
    attempted = []

    def failing_load(name):
        attempted.append(name)
        raise OSError("not found")

    monkeypatch.setattr(mlx_lm, "load", failing_load)
    engine = LocalInference(model_name="missing/model")
    assert engine.load_model() is False
    assert attempted == ["missing/model"]
    assert engine.model_name == "missing/model"


def test_generate_uses_sampler_and_chat_template(calls):
    assert LocalInference().query_base("hello", max_tokens=7, temperature=0.0) == "answer"
    (call,) = calls["generate"]
    assert call["prompt"] == "<chat>hello</chat>"
    assert call["max_tokens"] == 7
    assert callable(call["sampler"])
    assert "temperature" not in call and "temp" not in call


def test_raw_prompt_when_model_has_no_chat_template(calls, monkeypatch):
    class PlainTokenizer:
        has_chat_template = False

    monkeypatch.setattr(mlx_lm, "load", lambda name: (object(), PlainTokenizer()))
    LocalInference().query_base("hello")
    assert calls["generate"][0]["prompt"] == "hello"


@pytest.mark.parametrize("backend", ["torch", "cuda"])
def test_non_mlx_backend_rejected(backend):
    with pytest.raises(ValueError, match="only 'mlx'"):
        LocalInference(backend=backend)


def test_missing_mlx_gives_install_hint(monkeypatch):
    monkeypatch.setattr(inference, "_check_mlx", lambda: False)
    with pytest.raises(RuntimeError, match="pip install"):
        LocalInference()


def test_query_with_adapter_decrypts_as_mlx_and_warns_not_applied(calls, adapter_file, monkeypatch):
    path, key_hex = adapter_file
    requested = []
    original = EncryptedAdapter.decrypt_weights

    def spy(self, encrypted_path, **kwargs):
        requested.append(kwargs.get("framework"))
        return original(self, encrypted_path, **kwargs)

    monkeypatch.setattr(EncryptedAdapter, "decrypt_weights", spy)

    with pytest.warns(UserWarning, match="NOT applied"):
        assert LocalInference().query_with_adapter(path, key_hex, "hi") == "answer"
    assert requested == ["mlx"]


def test_query_with_adapter_wrong_key_fails_before_generating(calls, adapter_file):
    path, _ = adapter_file
    with pytest.raises(ValueError, match="Decryption failed"):
        LocalInference().query_with_adapter(path, EncryptedAdapter.generate_key().hex(), "hi")
    assert calls["generate"] == []


def test_wdva_query_warns_adapter_not_applied(calls, adapter_file):
    path, key_hex = adapter_file
    wdva = WDVA(adapter_path=path, encryption_key=key_hex)
    with pytest.warns(UserWarning, match="NOT applied") as record:
        assert wdva.query("hi") == "answer"
    assert str(record[0].message) == ADAPTER_NOT_APPLIED_WARNING
    assert record[0].filename == __file__  # points at the caller, not wdva internals
    assert calls["load"] == [DEFAULT_MODEL]


def test_wdva_train_is_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError):
        WDVA().train([tmp_path / "doc.txt"], output_path=tmp_path / "adapter.wdva")
    assert not (tmp_path / "adapter.wdva").exists()


def test_wdva_delete_forgets_key(calls, adapter_file):
    path, key_hex = adapter_file
    wdva = WDVA(adapter_path=path, encryption_key=key_hex)
    wdva.delete()
    assert not wdva.is_loaded
    with pytest.raises(ValueError):
        wdva.export_key()


def test_query_with_adapter_writes_no_files(calls, adapter_file, tmp_path, monkeypatch):
    """Decrypting and querying must not create files in the temp or working directory."""
    import tempfile

    path, key_hex = adapter_file
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.chdir(scratch)
    before = sorted(p.name for p in tmp_path.rglob("*"))

    with pytest.warns(UserWarning):
        LocalInference().query_with_adapter(path, key_hex, "hi")
    for framework in ("numpy", "mlx"):
        try:
            EncryptedAdapter(bytes.fromhex(key_hex)).decrypt_weights(path, framework=framework)
        except ValueError:
            pass  # bfloat16 is not representable in numpy; still must not write files

    assert sorted(p.name for p in tmp_path.rglob("*")) == before
