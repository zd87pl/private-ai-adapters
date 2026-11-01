"""
Tests for wdva.crypto: encrypted adapter round trips, tamper detection,
wrong-key rejection and an independent check of the cipher construction.
"""

from __future__ import annotations

import json
from base64 import b64decode, b64encode
from pathlib import Path

import numpy as np
import pytest

from wdva.crypto import (
    KEY_SIZE,
    NONCE_SIZE,
    SALT_SIZE,
    TAG_SIZE,
    EncryptedAdapter,
    generate_encryption_key,
)


DATA_DIR = Path(__file__).parent / "data"


# =============================================================================
# Fixtures and helpers
# =============================================================================

@pytest.fixture
def key() -> bytes:
    return EncryptedAdapter.generate_key()


@pytest.fixture(params=[True, False], ids=["compressed", "uncompressed"])
def compression(request) -> bool:
    if request.param:
        pytest.importorskip("zstandard")
    return request.param


@pytest.fixture
def adapter(key: bytes, compression: bool) -> EncryptedAdapter:
    return EncryptedAdapter(key, enable_compression=compression)


@pytest.fixture
def weights() -> dict:
    rng = np.random.default_rng(0)
    return {
        "layers.0.self_attn.q_proj.lora_a": rng.standard_normal((8, 64)).astype(np.float32),
        "layers.0.self_attn.q_proj.lora_b": rng.standard_normal((64, 8)).astype(np.float16),
        "layers.0.mlp.scale": np.arange(10, dtype=np.int8),
    }


@pytest.fixture
def encrypted_file(adapter: EncryptedAdapter, weights: dict, tmp_path: Path) -> Path:
    path = tmp_path / "adapter.wdva"
    adapter.encrypt_weights(weights, path, metadata={"base_model": "test-model"})
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, package: dict) -> None:
    path.write_text(json.dumps(package), encoding="utf-8")


def _flip_b64_byte(value: str, index: int = 0) -> str:
    raw = bytearray(b64decode(value))
    raw[index] ^= 0x01
    return b64encode(bytes(raw)).decode("ascii")


def _assert_same_weights(actual: dict, expected: dict) -> None:
    assert set(actual) == set(expected)
    for name, array in expected.items():
        got = np.asarray(actual[name])
        assert got.dtype == array.dtype, name
        assert got.shape == array.shape, name
        np.testing.assert_array_equal(got, array)


# =============================================================================
# Round trips
# =============================================================================

def test_numpy_round_trip(adapter, weights, encrypted_file):
    _assert_same_weights(adapter.decrypt_weights(encrypted_file), weights)


def test_round_trip_with_new_instance_same_key(key, compression, weights, encrypted_file):
    other = EncryptedAdapter(key, enable_compression=not compression)
    _assert_same_weights(other.decrypt_weights(encrypted_file), weights)


def test_empty_weights_round_trip(adapter, tmp_path):
    path = tmp_path / "empty.wdva"
    adapter.encrypt_weights({}, path)
    assert adapter.decrypt_weights(path) == {}


@pytest.mark.parametrize(
    "make_view",
    [
        lambda: np.arange(12, dtype=np.float32).reshape(3, 4).T,
        lambda: np.asfortranarray(np.arange(12, dtype=np.float32).reshape(3, 4)),
        lambda: np.arange(10, dtype=np.float64)[::2],
        lambda: np.arange(10, dtype=np.int16)[::-1],
        lambda: np.arange(24, dtype=np.float32).reshape(2, 3, 4)[:, 1:, ::2],
        lambda: np.array(3.5, dtype=np.float32),
    ],
    ids=["transposed", "fortran_order", "strided", "reversed", "sliced_3d", "zero_dim"],
)
def test_non_contiguous_numpy_round_trip(adapter, tmp_path, make_view):
    view = make_view()
    path = tmp_path / "adapter.wdva"
    adapter.encrypt_weights({"x": view}, path)
    _assert_same_weights(adapter.decrypt_weights(path), {"x": np.array(view)})


def test_golden_file_decrypts():
    """
    A file written by this version and checked in, so any change to the
    on-disk format (HKDF parameters, AAD encoding, field encoding) fails here.
    """
    adapter = EncryptedAdapter(bytes(range(KEY_SIZE)))
    _assert_same_weights(
        adapter.decrypt_weights(DATA_DIR / "golden_v1.wdva"),
        {
            "lora_a": np.arange(6, dtype=np.float32).reshape(2, 3),
            "lora_b": np.array([[1.5, -2.0]], dtype=np.float16),
        },
    )


def test_package_structure_and_metadata(adapter, weights, encrypted_file, compression):
    package = _read(encrypted_file)
    assert package["algorithm"] == "XChaCha20-Poly1305"
    assert package["kdf"] == "HKDF-SHA256"
    assert len(b64decode(package["salt"])) == SALT_SIZE
    assert len(b64decode(package["nonce"])) == NONCE_SIZE
    assert len(b64decode(package["tag"])) == TAG_SIZE
    metadata = package["metadata"]
    assert metadata["num_tensors"] == len(weights)
    assert metadata["compressed"] is compression
    assert metadata["base_model"] == "test-model"


def test_returned_package_matches_file(adapter, weights, tmp_path):
    path = tmp_path / "adapter.wdva"
    returned = adapter.encrypt_weights(weights, path)
    assert returned == _read(path)


def test_fresh_salt_and_nonce_per_encryption(adapter, weights, tmp_path):
    a = adapter.encrypt_weights(weights, tmp_path / "a.wdva")
    b = adapter.encrypt_weights(weights, tmp_path / "b.wdva")
    assert a["salt"] != b["salt"]
    assert a["nonce"] != b["nonce"]
    assert a["ciphertext"] != b["ciphertext"]


def test_no_plaintext_in_file(key, tmp_path):
    marker_name = "patient_record_marker_tensor"
    marker = np.frombuffer(b"PLAINTEXT-CANARY" * 64, dtype=np.uint8).copy()
    path = tmp_path / "adapter.wdva"
    EncryptedAdapter(key, enable_compression=False).encrypt_weights({marker_name: marker}, path)

    text = path.read_text(encoding="utf-8")
    assert marker_name not in text
    ciphertext = b64decode(_read(path)["ciphertext"])
    assert b"PLAINTEXT-CANARY" not in ciphertext
    assert marker_name.encode() not in ciphertext


def test_mlx_round_trip_including_bfloat16(adapter, tmp_path):
    mx = pytest.importorskip("mlx.core")
    weights = {
        "a": mx.arange(12, dtype=mx.float32).reshape(3, 4),
        "b": mx.ones((4, 2), dtype=mx.bfloat16),
    }
    path = tmp_path / "adapter.wdva"
    adapter.encrypt_weights(weights, path)

    decrypted = adapter.decrypt_weights(path, framework="mlx")
    assert set(decrypted) == {"a", "b"}
    for name, array in weights.items():
        assert isinstance(decrypted[name], mx.array)
        assert decrypted[name].dtype == array.dtype
        assert mx.array_equal(decrypted[name], array).item()


def test_non_contiguous_mlx_round_trip(adapter, tmp_path):
    mx = pytest.importorskip("mlx.core")
    view = mx.arange(12, dtype=mx.float32).reshape(3, 4).T
    path = tmp_path / "adapter.wdva"
    adapter.encrypt_weights({"x": view}, path)
    assert mx.array_equal(adapter.decrypt_weights(path, framework="mlx")["x"], view).item()


def test_bfloat16_to_numpy_gives_clear_error(adapter, tmp_path):
    mx = pytest.importorskip("mlx.core")
    path = tmp_path / "adapter.wdva"
    adapter.encrypt_weights({"b": mx.ones((2,), dtype=mx.bfloat16)}, path)
    with pytest.raises(ValueError, match="framework='mlx'"):
        adapter.decrypt_weights(path)


def test_numpy_written_mlx_read(adapter, weights, encrypted_file):
    pytest.importorskip("mlx.core")
    decrypted = adapter.decrypt_weights(encrypted_file, framework="mlx")
    _assert_same_weights({k: np.array(v) for k, v in decrypted.items()}, weights)


def test_torch_round_trip(adapter, tmp_path):
    torch = pytest.importorskip("torch")
    weights = {
        "a": torch.arange(6, dtype=torch.float32).reshape(2, 3),
        "b": torch.ones(3, dtype=torch.bfloat16),
    }
    path = tmp_path / "adapter.wdva"
    adapter.encrypt_weights(weights, path)

    decrypted = adapter.decrypt_weights(path, framework="torch")
    assert set(decrypted) == {"a", "b"}
    for name, tensor in weights.items():
        assert isinstance(decrypted[name], torch.Tensor)
        assert decrypted[name].dtype == tensor.dtype
        assert torch.equal(decrypted[name], tensor)


def test_torch_non_contiguous_and_shared_storage_round_trip(adapter, tmp_path):
    torch = pytest.importorskip("torch")
    base = torch.arange(12, dtype=torch.float32).reshape(3, 4)
    weights = {"transposed": base.T, "row": base[1], "base": base}
    path = tmp_path / "adapter.wdva"
    adapter.encrypt_weights(weights, path)

    decrypted = adapter.decrypt_weights(path, framework="torch")
    for name, tensor in weights.items():
        assert torch.equal(decrypted[name], tensor), name


@pytest.mark.parametrize(
    ("dtype_name", "framework", "suggestion"),
    [
        ("float8_e4m3fn", "mlx", "framework='torch'"),
        ("float8_e4m3fn", "numpy", "framework='torch'"),
        ("float64", "mlx", "framework='numpy'"),
    ],
)
def test_dtype_unsupported_by_framework_rejected(adapter, tmp_path, dtype_name, framework, suggestion):
    torch = pytest.importorskip("torch")
    if framework == "mlx":
        pytest.importorskip("mlx.core")
    path = tmp_path / "adapter.wdva"
    adapter.encrypt_weights({"x": torch.ones(4).to(getattr(torch, dtype_name))}, path)
    with pytest.raises(ValueError, match=suggestion):
        adapter.decrypt_weights(path, framework=framework)
    assert adapter.decrypt_weights(path, framework="torch")["x"].dtype == getattr(torch, dtype_name)


# =============================================================================
# Wrong key and tampering
# =============================================================================

def test_wrong_key_fails(encrypted_file):
    wrong = EncryptedAdapter(EncryptedAdapter.generate_key())
    with pytest.raises(ValueError, match="Decryption failed"):
        wrong.decrypt_weights(encrypted_file)


@pytest.mark.parametrize("field", ["ciphertext", "tag", "nonce", "salt"])
@pytest.mark.parametrize("index", [0, -1], ids=["first_byte", "last_byte"])
def test_flipped_byte_fails(adapter, encrypted_file, field, index):
    package = _read(encrypted_file)
    package[field] = _flip_b64_byte(package[field], index)
    _write(encrypted_file, package)
    with pytest.raises(ValueError, match="Decryption failed"):
        adapter.decrypt_weights(encrypted_file)


def test_truncated_ciphertext_fails(adapter, encrypted_file):
    package = _read(encrypted_file)
    package["ciphertext"] = b64encode(b64decode(package["ciphertext"])[:-1]).decode("ascii")
    _write(encrypted_file, package)
    with pytest.raises(ValueError, match="Decryption failed"):
        adapter.decrypt_weights(encrypted_file)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(base_model="other-model"),
        lambda m: m.update(compressed=not m["compressed"]),
        lambda m: m.update(num_tensors=m["num_tensors"] + 1),
        lambda m: m.update(version="0.9"),
        lambda m: m.update(injected="extra"),
        lambda m: m.pop("base_model"),
    ],
    ids=["user_field", "compressed", "num_tensors", "version", "added_field", "removed_field"],
)
def test_tampered_metadata_fails(adapter, encrypted_file, mutate):
    package = _read(encrypted_file)
    mutate(package["metadata"])
    _write(encrypted_file, package)
    with pytest.raises(ValueError, match="Decryption failed"):
        adapter.decrypt_weights(encrypted_file)


def test_swapped_ciphertext_between_files_fails(adapter, weights, tmp_path):
    a = tmp_path / "a.wdva"
    b = tmp_path / "b.wdva"
    adapter.encrypt_weights(weights, a)
    adapter.encrypt_weights(weights, b)
    package_a, package_b = _read(a), _read(b)
    package_a["ciphertext"] = package_b["ciphertext"]
    _write(a, package_a)
    with pytest.raises(ValueError, match="Decryption failed"):
        adapter.decrypt_weights(a)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("algorithm", "AES-256-GCM", "Unsupported algorithm"),
        ("kdf", "PBKDF2", "Unsupported KDF"),
    ],
)
def test_unsupported_header_rejected(adapter, encrypted_file, field, value, message):
    package = _read(encrypted_file)
    package[field] = value
    _write(encrypted_file, package)
    with pytest.raises(ValueError, match=message):
        adapter.decrypt_weights(encrypted_file)


@pytest.mark.parametrize("field", ["salt", "nonce", "ciphertext", "tag", "metadata", "algorithm", "kdf"])
def test_missing_field_rejected(adapter, encrypted_file, field):
    package = _read(encrypted_file)
    del package[field]
    _write(encrypted_file, package)
    with pytest.raises(ValueError, match="missing fields"):
        adapter.decrypt_weights(encrypted_file)


def test_missing_file_raises(adapter, tmp_path):
    with pytest.raises(FileNotFoundError):
        adapter.decrypt_weights(tmp_path / "nope.wdva")


# =============================================================================
# Input validation
# =============================================================================

@pytest.mark.parametrize("reserved", ["version", "num_tensors", "original_size", "compressed"])
def test_reserved_metadata_keys_rejected(adapter, weights, tmp_path, reserved):
    path = tmp_path / "adapter.wdva"
    with pytest.raises(ValueError, match="reserved keys"):
        adapter.encrypt_weights(weights, path, metadata={reserved: False})
    assert not path.exists()


@pytest.mark.parametrize(
    "bad_metadata",
    [
        [("compressed", False)],
        (kv for kv in [("version", "2.0")]),
        {1: "non-str key"},
    ],
    ids=["pair_list", "generator", "non_str_key"],
)
def test_non_mapping_metadata_rejected(adapter, weights, tmp_path, bad_metadata):
    path = tmp_path / "adapter.wdva"
    with pytest.raises(TypeError, match="metadata"):
        adapter.encrypt_weights(weights, path, metadata=bad_metadata)
    assert not path.exists()


@pytest.mark.parametrize(
    "bad_weights",
    [
        [np.zeros(2)],
        {"a": [1.0, 2.0]},
        {"a": "not an array"},
        {1: np.zeros(2)},
    ],
    ids=["list", "python_list_value", "string_value", "non_str_name"],
)
def test_unsupported_weights_rejected(adapter, tmp_path, bad_weights):
    with pytest.raises(TypeError):
        adapter.encrypt_weights(bad_weights, tmp_path / "adapter.wdva")


def test_mixed_frameworks_rejected(adapter, tmp_path):
    mx = pytest.importorskip("mlx.core")
    with pytest.raises(TypeError, match="one kind per dict"):
        adapter.encrypt_weights({"a": np.zeros(2), "b": mx.zeros(2)}, tmp_path / "adapter.wdva")


def test_unknown_framework_rejected_before_reading_file(adapter, tmp_path):
    with pytest.raises(ValueError, match="Unsupported framework"):
        adapter.decrypt_weights(tmp_path / "missing.wdva", framework="jax")


@pytest.mark.parametrize("bad_key", [b"", b"\x00" * (KEY_SIZE - 1), b"\x00" * (KEY_SIZE + 1)])
def test_wrong_key_length_rejected(bad_key):
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        EncryptedAdapter(bad_key)


def test_non_bytes_key_rejected():
    with pytest.raises(TypeError):
        EncryptedAdapter("00" * KEY_SIZE)


def test_key_hex_round_trip():
    key_bytes, key_hex = generate_encryption_key()
    assert len(key_bytes) == KEY_SIZE
    assert EncryptedAdapter.key_to_hex(key_bytes) == key_hex
    assert EncryptedAdapter.key_from_hex(key_hex) == key_bytes


@pytest.mark.parametrize("bad_hex", ["zz" * KEY_SIZE, "00" * (KEY_SIZE - 1), "abc"])
def test_invalid_key_hex_rejected(bad_hex):
    with pytest.raises(ValueError):
        EncryptedAdapter.key_from_hex(bad_hex)


def test_repr_does_not_leak_key(key):
    assert key.hex() not in repr(EncryptedAdapter(key))


# =============================================================================
# Independent check of the cipher construction
# =============================================================================

def test_file_decrypts_with_libsodium_xchacha20poly1305(key, weights, tmp_path):
    """
    Re-derive the key with HKDF-SHA256 and decrypt with libsodium's
    XChaCha20-Poly1305 (via PyNaCl), so the "algorithm" label in the file is
    checked against an implementation other than the one that wrote it.
    """
    bindings = pytest.importorskip("nacl.bindings")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from safetensors.numpy import load

    path = tmp_path / "adapter.wdva"
    EncryptedAdapter(key, enable_compression=False).encrypt_weights(
        weights, path, metadata={"base_model": "test-model"}
    )
    package = _read(path)

    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=b64decode(package["salt"]),
        info=b"wdva-encryption-v1",
    ).derive(key)
    aad = json.dumps(package["metadata"], sort_keys=True, separators=(",", ":")).encode("utf-8")

    plaintext = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
        b64decode(package["ciphertext"]) + b64decode(package["tag"]),
        aad,
        b64decode(package["nonce"]),
        derived,
    )
    _assert_same_weights(load(plaintext), weights)
