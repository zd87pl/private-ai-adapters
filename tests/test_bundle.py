"""AdapterBundle: payload encoding, vault I/O and PEFT directory interop (no torch needed)."""

from __future__ import annotations

import json
import os
import stat
import struct
from pathlib import Path

import pytest

from wdva import (
    CONTENT_TYPE_ADAPTER,
    AdapterBundle,
    IntegrityError,
    SecretKey,
    VaultFormatError,
    WrongKeyError,
    read_header,
    seal,
)

from .conftest import make_safetensors


@pytest.fixture
def bundle(fake_config: dict[str, object], fake_weights: bytes) -> AdapterBundle:
    return AdapterBundle(fake_config, fake_weights, {"base_model": "example/base", "note": "hi"})


def test_describes_tensors_without_torch(bundle: AdapterBundle) -> None:
    tensors = bundle.tensors()
    assert [t.name for t in tensors] == [
        "base_model.model.layer.lora_A.weight",
        "base_model.model.layer.lora_B.weight",
    ]
    assert tensors[0].shape == (2, 4)
    assert tensors[0].dtype == "F32"
    assert bundle.parameter_count == 16
    assert bundle.base_model == "example/base"
    summary = bundle.summary()
    assert summary["rank"] == 2
    assert summary["peft_type"] == "LORA"
    assert summary["parameters"] == 16
    json.dumps(summary)  # must be JSON-serialisable


def test_base_model_falls_back_to_the_peft_config(fake_config: dict, fake_weights: bytes) -> None:
    assert AdapterBundle(fake_config, fake_weights).base_model == "example/base"
    fake_config.pop("base_model_name_or_path")
    assert AdapterBundle(fake_config, fake_weights).base_model is None


def test_repr_does_not_dump_weights_or_config(bundle: AdapterBundle) -> None:
    text = repr(bundle)
    assert len(text) < 200
    assert "lora_alpha" not in text
    assert "note" not in text


def test_payload_roundtrip(bundle: AdapterBundle) -> None:
    restored = AdapterBundle.from_bytes(bundle.to_bytes())
    assert restored == bundle
    assert restored.weights == bundle.weights


def test_vault_roundtrip_and_content_type(bundle: AdapterBundle, key: SecretKey) -> None:
    blob = bundle.seal(key=key)
    assert read_header(blob).content_type == CONTENT_TYPE_ADAPTER
    assert AdapterBundle.unseal(blob, key=key) == bundle


def test_private_details_are_not_visible_in_the_vault(
    bundle: AdapterBundle, key: SecretKey
) -> None:
    blob = bundle.seal(key=key)
    for secret in (b"example/base", b"lora_A", b"lora_alpha", b"note"):
        assert secret not in blob


def test_save_and_load(tmp_path: Path, bundle: AdapterBundle, key: SecretKey) -> None:
    path = bundle.save(tmp_path / "a.wdva", key=key)
    assert AdapterBundle.load(path, key=key) == bundle
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        bundle.save(path, key=key)
    with pytest.raises(WrongKeyError):
        AdapterBundle.load(path, key=SecretKey.generate())
    with pytest.raises(TypeError, match="exactly one"):
        AdapterBundle.load(path)
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    with pytest.raises(IntegrityError):
        AdapterBundle.load(path, key=key)


def test_passphrase_vault(tmp_path: Path, bundle: AdapterBundle) -> None:
    path = bundle.save(tmp_path / "a.wdva", passphrase="open sesame", scrypt_n=2**14)
    assert AdapterBundle.load(path, passphrase="open sesame") == bundle


def test_a_vault_holding_something_else_is_not_an_adapter(key: SecretKey) -> None:
    with pytest.raises(VaultFormatError, match="not an adapter"):
        AdapterBundle.unseal(seal(b"just bytes", key=key), key=key)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00\x00",
        struct.pack(">I", 10) + b"short",
        struct.pack(">I", 2**31) + b"{}",
        struct.pack(">I", 8) + b"not json",
        struct.pack(">I", 2) + b"[]",
        struct.pack(">I", 2) + b"{}",
        struct.pack(">I", 18) + b'{"format":"other"}',
    ],
)
def test_malformed_payloads(payload: bytes) -> None:
    with pytest.raises(VaultFormatError):
        AdapterBundle.from_bytes(payload)


def test_payload_with_garbage_weights_is_rejected(fake_config: dict) -> None:
    manifest = json.dumps(
        {"format": CONTENT_TYPE_ADAPTER, "adapter_config": fake_config, "info": {}}
    ).encode()
    with pytest.raises(VaultFormatError, match="safetensors"):
        AdapterBundle.from_bytes(struct.pack(">I", len(manifest)) + manifest + b"garbage!!")


@pytest.mark.parametrize(
    "weights",
    [
        b"",
        b"short",
        struct.pack("<Q", 10**12) + b"{}",
        struct.pack("<Q", 5) + b"oops!",
        struct.pack("<Q", 2) + b"[]",
        make_safetensors({"t": ("F32", [2, 2], bytes(15))}),  # size does not match shape
        struct.pack("<Q", 59) + b'{"t":{"dtype":"F32","shape":[1],"data_offsets":[0,999999]}}',
    ],
)
def test_weights_must_be_well_formed_safetensors(fake_config: dict, weights: bytes) -> None:
    with pytest.raises(ValueError, match="safetensors"):
        AdapterBundle(fake_config, weights)


def test_constructor_type_checks(fake_weights: bytes) -> None:
    with pytest.raises(TypeError):
        AdapterBundle("config", fake_weights)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AdapterBundle({}, "weights")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AdapterBundle({}, fake_weights, info=[1])  # type: ignore[arg-type]


# -- PEFT directory interop ------------------------------------------------


def write_peft_dir(directory: Path, config: dict, weights: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "adapter_config.json").write_text(json.dumps(config))
    (directory / "adapter_model.safetensors").write_bytes(weights)
    return directory


def test_peft_dir_roundtrip(tmp_path: Path, fake_config: dict, fake_weights: bytes) -> None:
    src = write_peft_dir(tmp_path / "src", fake_config, fake_weights)
    bundle = AdapterBundle.from_peft_dir(src, info={"base_model": "example/base"})
    out = bundle.to_peft_dir(tmp_path / "out")
    assert json.loads((out / "adapter_config.json").read_text()) == fake_config
    assert (out / "adapter_model.safetensors").read_bytes() == fake_weights
    if os.name == "posix":
        assert stat.S_IMODE((out / "adapter_model.safetensors").stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        bundle.to_peft_dir(out)
    bundle.to_peft_dir(out, overwrite=True)
    assert sorted(p.name for p in out.iterdir()) == [
        "adapter_config.json",
        "adapter_model.safetensors",
    ]


def test_pickle_adapters_are_refused(tmp_path: Path, fake_config: dict) -> None:
    d = tmp_path / "pickled"
    d.mkdir()
    (d / "adapter_config.json").write_text(json.dumps(fake_config))
    (d / "adapter_model.bin").write_bytes(b"\x80\x04pickle")
    with pytest.raises(ValueError, match="pickle"):
        AdapterBundle.from_peft_dir(d)


def test_incomplete_peft_dirs(tmp_path: Path, fake_config: dict, fake_weights: bytes) -> None:
    with pytest.raises(FileNotFoundError, match=r"adapter_config\.json"):
        AdapterBundle.from_peft_dir(tmp_path)
    d = tmp_path / "noweights"
    d.mkdir()
    (d / "adapter_config.json").write_text(json.dumps(fake_config))
    with pytest.raises(FileNotFoundError, match="safetensors"):
        AdapterBundle.from_peft_dir(d)
    bad = write_peft_dir(tmp_path / "badcfg", fake_config, fake_weights)
    (bad / "adapter_config.json").write_text("[1, 2]")
    with pytest.raises(ValueError, match="JSON object"):
        AdapterBundle.from_peft_dir(bad)


# -- hardening found by adversarial review ---------------------------------


def _st(header: dict[str, object], data: bytes = b"") -> bytes:
    encoded = json.dumps(header).encode()
    return struct.pack("<Q", len(encoded)) + encoded + data


@pytest.mark.parametrize(
    "weights",
    [
        # overlapping tensors
        _st({"a": {"dtype": "U8", "shape": [4], "data_offsets": [0, 4]},
             "b": {"dtype": "U8", "shape": [4], "data_offsets": [2, 6]}}, bytes(6)),
        # a gap between tensors
        _st({"a": {"dtype": "U8", "shape": [2], "data_offsets": [0, 2]},
             "b": {"dtype": "U8", "shape": [2], "data_offsets": [4, 6]}}, bytes(6)),
        # bytes after the last tensor
        _st({"a": {"dtype": "U8", "shape": [2], "data_offsets": [0, 2]}}, bytes(5)),
        # data that does not start at offset 0
        _st({"a": {"dtype": "U8", "shape": [2], "data_offsets": [1, 3]}}, bytes(3)),
        # a dtype the format does not define
        _st({"a": {"dtype": "COMPLEX999", "shape": [3], "data_offsets": [0, 3]}}, bytes(3)),
        # booleans and floats posing as integers
        _st({"a": {"dtype": "U8", "shape": [True, 2], "data_offsets": [0, 2]}}, bytes(2)),
        _st({"a": {"dtype": "U8", "shape": [2], "data_offsets": [0, 2.0]}}, bytes(2)),
        # metadata that is not a string map
        _st({"__metadata__": [1], "a": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]}},
            b"x"),
        _st({"__metadata__": {"k": 1}}),
        # hostile JSON
        struct.pack("<Q", 3000) + b"[" * 3000,
        _st({}) [:8] + b'{"a":1,"a":2}'.ljust(8),
    ],
)  # fmt: skip
def test_weights_are_validated_as_strictly_as_safetensors_does(
    fake_config: dict, weights: bytes
) -> None:
    with pytest.raises(ValueError, match="safetensors"):
        AdapterBundle(fake_config, weights)


def test_strict_validation_agrees_with_the_safetensors_library(fake_weights: bytes) -> None:
    """Anything we accept, the real library must load (checked when it is installed)."""
    safetensors_torch = pytest.importorskip("safetensors.torch")
    import torch

    good = safetensors_torch.save(
        {"b": torch.ones(2, 3), "a": torch.zeros(4, dtype=torch.float16), "c": torch.tensor(1)},
        metadata={"format": "pt"},
    )
    assert AdapterBundle({}, good).parameter_count == 11
    assert AdapterBundle({}, safetensors_torch.save({})).tensors() == []
    assert set(safetensors_torch.load(fake_weights)) == {
        t.name for t in AdapterBundle({}, fake_weights).tensors()
    }


def test_hostile_manifests_fail_cleanly(fake_config: dict, fake_weights: bytes) -> None:
    for manifest in (
        b"[" * 3000,
        b'{"format":"application/vnd.wdva.adapter.v1","adapter_config":{},"info":{"x":NaN}}',
        b'{"format":"application/vnd.wdva.adapter.v1","adapter_config":{},"info":{"x":1e400}}',
        b'{"format":"application/vnd.wdva.adapter.v1","adapter_config":{"r":9007199254740992},'
        b'"info":{}}',
        b'{"format":"application/vnd.wdva.adapter.v1","adapter_config":{},"adapter_config":{}}',
    ):
        with pytest.raises(VaultFormatError):
            AdapterBundle.from_bytes(struct.pack(">I", len(manifest)) + manifest + fake_weights)


def test_a_bundle_refuses_to_write_integers_its_reader_would_refuse(
    fake_config: dict, fake_weights: bytes
) -> None:
    for info in ({"n": 2**53}, {"deep": [{"n": -(10**40)}]}):
        with pytest.raises(ValueError, match="out of range"):
            AdapterBundle(fake_config, fake_weights, info).to_bytes()
    bundle = AdapterBundle(fake_config, fake_weights, {"n": 2**53 - 1})
    assert AdapterBundle.from_bytes(bundle.to_bytes()).info == {"n": 2**53 - 1}


def test_bundle_nesting_limit_matches_the_reader(fake_config: dict, fake_weights: bytes) -> None:
    def nested(levels: int) -> object:
        value: object = "leaf"
        for _ in range(levels):
            value = {"k": value}
        return value

    # The manifest is level 1 and `info` level 2, leaving 62 levels inside it.
    ok = AdapterBundle(fake_config, fake_weights, {"deep": nested(62)})
    assert AdapterBundle.from_bytes(ok.to_bytes()) == ok
    too_deep = AdapterBundle(fake_config, fake_weights, {"deep": nested(63)})
    with pytest.raises(ValueError, match="64 levels"):
        too_deep.to_bytes()


def test_export_stages_both_files_before_replacing_either(
    tmp_path: Path, fake_config: dict, fake_weights: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running out of space must not leave a new config beside old weights."""
    out = tmp_path / "out"
    first = AdapterBundle({**fake_config, "r": 1}, fake_weights)
    first.to_peft_dir(out)
    second = AdapterBundle({**fake_config, "r": 2}, fake_weights)

    calls = {"n": 0}
    real_fsync = os.fsync

    def fail_on_second_file(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_on_second_file)
    with pytest.raises(OSError, match="No space"):
        second.to_peft_dir(out, overwrite=True)
    monkeypatch.undo()
    assert json.loads((out / "adapter_config.json").read_text())["r"] == 1
    assert (out / "adapter_model.safetensors").read_bytes() == fake_weights
    assert sorted(p.name for p in out.iterdir()) == [
        "adapter_config.json",
        "adapter_model.safetensors",
    ]
