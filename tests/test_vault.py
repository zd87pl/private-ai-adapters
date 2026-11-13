"""The vault format: round trips, failure modes, and conformance to docs/FORMAT.md."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import struct
import unicodedata
from pathlib import Path

import pytest
from Crypto.Cipher import ChaCha20_Poly1305
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF

from wdva import (
    IntegrityError,
    SecretKey,
    VaultFormatError,
    WrongKeyError,
    is_vault,
    read_header,
    seal,
    seal_to_file,
    unseal,
    unseal_file,
    vault,
)

FAST_SCRYPT = 2**14

# Produced once by vault._seal with the fixed inputs in test_known_answer_*. If
# either of these ever changes, the on-disk format changed and old vaults broke.
KAT_KEYED = bytes.fromhex(
    "5744564101000001017b22616c67223a225843686143686132302d506f6c7931333035222c22636f6d6d697422"
    "3a227168374f5957766b686f63374d514648303876693335706e62414d672f616d65623770763974764a445077"
    "3d222c226b6466223a22484b44462d534841323536222c226d657461223a7b226e6f7465223a226b6174227d2c"
    "226e6f6e6365223a225145464351305246526b64495355704c5445314f54314252556c4e5556565a58222c2273"
    "616c74223a22494345694979516c4a69636f4b536f724c4330754c7a41784d6a4d304e5459334f446b364f7a77"
    "39506a383d222c2274797065223a226170706c69636174696f6e2f6f637465742d73747265616d227d28165323"
    "06e0c9045465892b4f2cde0a61de85445a08958019e35341fe7f33519b4eb6b7b997609600a8a0"
)
KAT_PASSPHRASE = bytes.fromhex(
    "5744564101000001397b22616c67223a225843686143686132302d506f6c7931333035222c22636f6d6d697422"
    "3a22384f6b4347334f726a524c31784874687a6175776e62442b46447969394c727731617169497034763945633d"
    "222c226b6466223a22484b44462d534841323536222c226e6f6e6365223a2241414543417751464267634943516f"
    "4c4441304f4478415245684d5546525958222c227077223a7b226b6466223a22736372797074222c226e223a3136"
    "3338342c2270223a312c2272223a382c2273616c74223a2241414543417751464267634943516f4c4441304f4477"
    "3d3d227d2c2273616c74223a2241414543417751464267634943516f4c4441304f4478415245684d554652595847"
    "426b61477877644868383d222c2274797065223a226170706c69636174696f6e2f6f637465742d73747265616d22"
    "7d0b1c83e3021c959cd2b2b03e227babc234be7e009cc9cde2345d423e813b3a2f"
)


def reference_unseal(
    blob: bytes, *, key: bytes | None = None, passphrase: str | None = None
) -> bytes:
    """An independent decoder written only from docs/FORMAT.md, using raw primitives."""
    assert blob[:4] == b"WDVA"
    assert blob[4] == 1
    (header_len,) = struct.unpack(">I", blob[5:9])
    header_bytes = blob[9 : 9 + header_len]
    header = json.loads(header_bytes)
    assert header["alg"] == "XChaCha20-Poly1305"
    assert header["kdf"] == "HKDF-SHA256"
    if "pw" in header:
        assert passphrase is not None
        master = hashlib.scrypt(
            unicodedata.normalize("NFKC", passphrase).encode("utf-8"),
            salt=base64.b64decode(header["pw"]["salt"]),
            n=header["pw"]["n"],
            r=header["pw"]["r"],
            p=header["pw"]["p"],
            maxmem=2**28,
            dklen=32,
        )
    else:
        assert key is not None
        master = key
    salt = base64.b64decode(header["salt"])
    enc_key = HKDF(master, 32, salt, SHA256, 1, b"wdva/v1/encryption-key")
    commitment = HKDF(master, 32, salt, SHA256, 1, b"wdva/v1/key-commitment")
    assert commitment == base64.b64decode(header["commit"])
    cipher = ChaCha20_Poly1305.new(key=enc_key, nonce=base64.b64decode(header["nonce"]))
    cipher.update(blob[: 9 + header_len])
    return cipher.decrypt_and_verify(blob[9 + header_len : -16], blob[-16:])


# -- round trips -----------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 1000, 70_000])
def test_roundtrip_with_key(key: SecretKey, size: int) -> None:
    payload = os.urandom(size)
    blob = seal(payload, key=key)
    result = unseal(blob, key=key)
    assert result.payload == payload
    assert result.header.content_type == vault.CONTENT_TYPE_RAW
    assert not result.header.uses_passphrase
    (header_len,) = struct.unpack(">I", blob[5:9])
    assert len(blob) == 9 + header_len + size + 16  # prefix + header + ciphertext + tag


def test_roundtrip_with_passphrase() -> None:
    blob = seal(b"secret", passphrase="correct horse", scrypt_n=FAST_SCRYPT)
    result = unseal(blob, passphrase="correct horse")
    assert result.payload == b"secret"
    assert result.header.uses_passphrase
    assert result.header.scrypt_n == FAST_SCRYPT


@pytest.mark.parametrize("form", ["secretkey", "bytes", "bytearray", "hex"])
def test_key_forms_are_interchangeable(key: SecretKey, form: str) -> None:
    raw = bytes.fromhex(key.hex())
    given = {"secretkey": key, "bytes": raw, "bytearray": bytearray(raw), "hex": key.hex()}[form]
    assert unseal(seal(b"x", key=given), key=key).payload == b"x"
    assert unseal(seal(b"x", key=key), key=given).payload == b"x"


def test_caller_key_is_not_wiped_by_use(key: SecretKey) -> None:
    raw = bytearray.fromhex(key.hex())
    seal(b"x", key=raw)
    assert bytes(raw) == bytes.fromhex(key.hex())
    assert not key.wiped


def test_sealing_is_randomised(key: SecretKey) -> None:
    a, b = seal(b"same payload", key=key), seal(b"same payload", key=key)
    assert a != b
    assert read_header(a).salt != read_header(b).salt
    assert read_header(a).nonce != read_header(b).nonce
    assert a[-28:] != b[-28:]


def test_ciphertext_does_not_contain_plaintext(key: SecretKey) -> None:
    payload = b"the greenhouse door code is 4-7-1-9" * 10
    blob = seal(payload, key=key)
    assert payload[:16] not in blob


def test_public_metadata_is_visible_but_authenticated(key: SecretKey) -> None:
    blob = seal(b"x", key=key, public_metadata={"owner": "team-a", "n": 3})
    assert read_header(blob).public_metadata == {"owner": "team-a", "n": 3}
    assert b"team-a" in blob  # stored in the clear, as documented
    tampered = blob.replace(b"team-a", b"team-b")
    with pytest.raises(IntegrityError):
        unseal(tampered, key=key)


def test_content_type_is_preserved(key: SecretKey) -> None:
    blob = seal(b"x", key=key, content_type="text/plain")
    assert unseal(blob, key=key).header.content_type == "text/plain"


def test_passphrase_is_unicode_normalised() -> None:
    composed, decomposed = "café", "café"
    assert composed != decomposed
    blob = seal(b"x", passphrase=composed, scrypt_n=FAST_SCRYPT)
    assert unseal(blob, passphrase=decomposed).payload == b"x"


# -- wrong credentials -----------------------------------------------------


def test_wrong_key(key: SecretKey) -> None:
    with pytest.raises(WrongKeyError, match="wrong key"):
        unseal(seal(b"x", key=key), key=SecretKey.generate())


def test_wrong_passphrase() -> None:
    blob = seal(b"x", passphrase="right", scrypt_n=FAST_SCRYPT)
    with pytest.raises(WrongKeyError, match="wrong passphrase"):
        unseal(blob, passphrase="wrong")


def test_key_given_for_passphrase_vault_and_vice_versa(key: SecretKey) -> None:
    with pytest.raises(WrongKeyError, match="locked with a passphrase"):
        unseal(seal(b"x", passphrase="pw", scrypt_n=FAST_SCRYPT), key=key)
    with pytest.raises(WrongKeyError, match="locked with a raw key"):
        unseal(seal(b"x", key=key), passphrase="pw")


@pytest.mark.parametrize("kwargs", [{}, {"key": b"\0" * 32, "passphrase": "both"}])
def test_exactly_one_credential_is_required(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError, match="exactly one"):
        seal(b"x", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exactly one"):
        unseal(KAT_KEYED, **kwargs)  # type: ignore[arg-type]


def test_empty_passphrase_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        seal(b"x", passphrase="")


def test_errors_never_contain_key_material(key: SecretKey) -> None:
    other = SecretKey.generate()
    with pytest.raises(WrongKeyError) as excinfo:
        unseal(seal(b"x", key=key), key=other)
    assert other.hex() not in str(excinfo.value)
    assert key.hex() not in repr(excinfo.value)


# -- tampering and corruption ----------------------------------------------


def test_every_single_bit_flip_is_detected(key: SecretKey) -> None:
    """All eight bits of every byte: header, ciphertext and tag."""
    blob = seal(b"attack at dawn", key=key, public_metadata={"k": "v"})
    for index in range(len(blob)):
        for bit in range(8):
            mutated = bytearray(blob)
            mutated[index] ^= 1 << bit
            with pytest.raises((IntegrityError, WrongKeyError, VaultFormatError)):
                unseal(bytes(mutated), key=key)


def test_ciphertext_and_tag_tampering_is_an_integrity_error(key: SecretKey) -> None:
    blob = bytearray(seal(b"attack at dawn", key=key))
    for index in (len(blob) - 1, len(blob) - 16, len(blob) - 17):
        mutated = bytearray(blob)
        mutated[index] ^= 0x80
        with pytest.raises(IntegrityError, match="tampered"):
            unseal(bytes(mutated), key=key)


@pytest.mark.parametrize("cut", [0, 1, 4, 8, 9, 50, -17, -16, -1])
def test_truncation_is_detected(key: SecretKey, cut: int) -> None:
    blob = seal(b"some payload bytes", key=key)
    with pytest.raises((VaultFormatError, IntegrityError)):
        unseal(blob[:cut], key=key)


def test_appended_data_is_detected(key: SecretKey) -> None:
    with pytest.raises(IntegrityError):
        unseal(seal(b"x", key=key) + b"\0", key=key)


def test_ciphertext_cannot_be_moved_under_another_header(key: SecretKey) -> None:
    a = seal(b"payload-a", key=key, public_metadata={"id": "a"})
    b = seal(b"payload-b", key=key, public_metadata={"id": "b"})
    split_a = 9 + struct.unpack(">I", a[5:9])[0]
    split_b = 9 + struct.unpack(">I", b[5:9])[0]
    with pytest.raises(IntegrityError):
        unseal(a[:split_a] + b[split_b:], key=key)


# -- malformed input -------------------------------------------------------


def _with_header(blob: bytes, mutate) -> bytes:  # type: ignore[no-untyped-def]
    (header_len,) = struct.unpack(">I", blob[5:9])
    header = json.loads(blob[9 : 9 + header_len])
    mutate(header)
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    return blob[:5] + struct.pack(">I", len(encoded)) + encoded + blob[9 + header_len :]


def test_not_a_vault() -> None:
    with pytest.raises(VaultFormatError, match="magic"):
        unseal(b"PK\x03\x04" + b"\0" * 100, key=b"\0" * 32)
    with pytest.raises(VaultFormatError, match="too short"):
        unseal(b"WD", key=b"\0" * 32)


def test_legacy_json_prototype_files_get_a_clear_message() -> None:
    legacy = json.dumps({"salt": "x", "nonce": "y", "ciphertext": "z", "tag": "t"}).encode()
    with pytest.raises(VaultFormatError, match=r"pre-0\.1"):
        unseal(legacy, key=b"\0" * 32)


def test_unsupported_version(key: SecretKey) -> None:
    blob = bytearray(seal(b"x", key=key))
    blob[4] = 2
    with pytest.raises(VaultFormatError, match="version 2"):
        unseal(bytes(blob), key=key)


@pytest.mark.parametrize("length", [0, 65537, 2**32 - 1])
def test_header_length_out_of_range(key: SecretKey, length: int) -> None:
    blob = seal(b"x", key=key)
    with pytest.raises(VaultFormatError):
        unseal(blob[:5] + struct.pack(">I", length) + blob[9:], key=key)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.update(alg="AES-256-GCM"),
        lambda h: h.update(kdf="none"),
        lambda h: h.update(nonce=base64.b64encode(bytes(12)).decode()),
        lambda h: h.update(nonce=base64.b64encode(bytes(8)).decode()),
        lambda h: h.update(salt=base64.b64encode(bytes(16)).decode()),
        lambda h: h.update(commit="not base64!"),
        lambda h: h.update(nonce=123),
        lambda h: h.update(type=""),
        lambda h: h.update(extra="field"),
        lambda h: h.update(meta=[1, 2]),
        lambda h: h.pop("salt"),
        lambda h: h.pop("commit"),
    ],
)
def test_malformed_headers_are_rejected_before_any_crypto(key: SecretKey, mutate) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(VaultFormatError):
        unseal(_with_header(seal(b"x", key=key), mutate), key=key)


def test_header_must_be_a_json_object(key: SecretKey) -> None:
    blob = seal(b"x", key=key)
    for bogus in (b"[]", b"not json", b"\xff\xfe"):
        with pytest.raises(VaultFormatError):
            unseal(blob[:5] + struct.pack(">I", len(bogus)) + bogus + blob[-40:], key=key)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pw: pw.update(n=2**30),  # memory-exhaustion attempt
        lambda pw: pw.update(n=2**10),  # too weak
        lambda pw: pw.update(n=100_000),  # not a power of two
        lambda pw: pw.update(n="131072"),
        lambda pw: pw.update(n=True),
        lambda pw: pw.update(r=1024),
        lambda pw: pw.update(p=64),
        lambda pw: pw.update(kdf="pbkdf2"),
        lambda pw: pw.pop("salt"),
        lambda pw: pw.update(extra=1),
    ],
)
def test_hostile_scrypt_parameters_are_rejected(mutate) -> None:  # type: ignore[no-untyped-def]
    blob = seal(b"x", passphrase="pw", scrypt_n=FAST_SCRYPT)
    with pytest.raises(VaultFormatError):
        unseal(_with_header(blob, lambda h: mutate(h["pw"])), passphrase="pw")


@pytest.mark.parametrize("n", [0, 1000, 2**13, 2**21, 3 * 2**14])
def test_invalid_scrypt_cost_when_sealing(n: int) -> None:
    with pytest.raises(ValueError, match="power of two"):
        seal(b"x", passphrase="pw", scrypt_n=n)


def test_bad_arguments(key: SecretKey) -> None:
    with pytest.raises(TypeError):
        seal("text", key=key)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="content_type"):
        seal(b"x", key=key, content_type="")
    with pytest.raises(ValueError, match="JSON"):
        seal(b"x", key=key, public_metadata={"bad": object()})
    with pytest.raises(ValueError, match="limit"):
        seal(b"x", key=key, public_metadata={"big": "a" * 70_000})
    with pytest.raises(ValueError, match="32 bytes"):
        seal(b"x", key=b"short")
    with pytest.raises(TypeError):
        seal(b"x", key=12345)  # type: ignore[arg-type]


# -- format stability and spec conformance ---------------------------------


def test_known_answer_keyed() -> None:
    blob = vault._seal(
        b"weight-delta vault adapters",
        key=bytes(range(32)),
        passphrase=None,
        content_type="application/octet-stream",
        public_metadata={"note": "kat"},
        salt=bytes(range(32, 64)),
        nonce=bytes(range(64, 88)),
        scrypt_n=None,
        scrypt_salt=None,
    )
    assert blob == KAT_KEYED
    assert unseal(KAT_KEYED, key=bytes(range(32))).payload == b"weight-delta vault adapters"


def test_known_answer_passphrase() -> None:
    blob = vault._seal(
        b"passphrase vault",
        key=None,
        passphrase="correct horse battery staple",
        content_type="application/octet-stream",
        public_metadata={},
        salt=bytes(range(32)),
        nonce=bytes(range(24)),
        scrypt_n=2**14,
        scrypt_salt=bytes(range(16)),
    )
    assert blob == KAT_PASSPHRASE
    assert unseal(KAT_PASSPHRASE, passphrase="correct horse battery staple").payload == (
        b"passphrase vault"
    )


def test_spec_test_vectors_match_docs() -> None:
    """docs/FORMAT.md quotes these digests; keep the document and the vectors in step."""
    spec = (Path(__file__).parent.parent / "docs" / "FORMAT.md").read_text(encoding="utf-8")
    for vector in (KAT_KEYED, KAT_PASSPHRASE):
        assert f"{len(vector)} bytes" in spec
        assert vector[-16:].hex() in spec
        assert hashlib.sha256(vector).hexdigest() in spec


def test_independent_decoder_agrees_with_the_spec(key: SecretKey) -> None:
    raw_key = bytes.fromhex(key.hex())
    payload = os.urandom(333)
    assert reference_unseal(seal(payload, key=key), key=raw_key) == payload
    blob = seal(payload, passphrase="pässwörd", scrypt_n=FAST_SCRYPT)
    assert reference_unseal(blob, passphrase="pässwörd") == payload
    assert reference_unseal(KAT_KEYED, key=bytes(range(32))) == b"weight-delta vault adapters"


def _hkdf_sha256(master: bytes, salt: bytes, info: bytes) -> bytes:
    """HKDF (RFC 5869) for one 32-byte output, with nothing but the standard library."""
    extracted = hmac.new(salt, master, hashlib.sha256).digest()
    return hmac.new(extracted, info + b"\x01", hashlib.sha256).digest()


def test_a_second_library_opens_a_vault(key: SecretKey) -> None:
    """libsodium opens what this package seals, and refuses what it would refuse.

    The reference decoder above is written from the specification but uses the
    cipher of the library the package itself uses. Here nothing is shared: the
    keys come from HKDF and scrypt in the standard library, and the cipher is
    libsodium's XChaCha20-Poly1305 (through PyNaCl). So the name of the
    algorithm in the header is checked against a second implementation.
    """
    bindings = pytest.importorskip("nacl.bindings")
    exceptions = pytest.importorskip("nacl.exceptions")

    def open_with_libsodium(blob: bytes, master: bytes) -> bytes:
        (header_len,) = struct.unpack(">I", blob[5:9])
        associated = blob[: 9 + header_len]
        header = json.loads(blob[9 : 9 + header_len])
        assert header["alg"] == "XChaCha20-Poly1305"
        salt = base64.b64decode(header["salt"])
        commitment = _hkdf_sha256(master, salt, b"wdva/v1/key-commitment")
        assert commitment == base64.b64decode(header["commit"])
        plaintext: bytes = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            blob[9 + header_len :],  # ciphertext followed by the 16-byte tag
            associated,
            base64.b64decode(header["nonce"]),
            _hkdf_sha256(master, salt, b"wdva/v1/encryption-key"),
        )
        return plaintext

    payload = os.urandom(4097)
    raw_key = bytes.fromhex(key.hex())
    sealed = seal(payload, key=key, public_metadata={"note": "second library"})
    assert open_with_libsodium(sealed, raw_key) == payload
    assert open_with_libsodium(KAT_KEYED, bytes(range(32))) == b"weight-delta vault adapters"

    # A passphrase vault: scrypt from the standard library gives the master key.
    sealed_pw = seal(payload, passphrase="pässwörd", scrypt_n=FAST_SCRYPT)
    pw = json.loads(sealed_pw[9 : 9 + struct.unpack(">I", sealed_pw[5:9])[0]])["pw"]
    master = hashlib.scrypt(
        unicodedata.normalize("NFKC", "pässwörd").encode("utf-8"),
        salt=base64.b64decode(pw["salt"]),
        n=pw["n"],
        r=pw["r"],
        p=pw["p"],
        maxmem=2**28,
        dklen=32,
    )
    assert open_with_libsodium(sealed_pw, master) == payload

    # The other direction: what libsodium seals with the same layout, the package opens.
    (header_len,) = struct.unpack(">I", sealed[5:9])
    header = json.loads(sealed[9 : 9 + header_len])
    resealed = sealed[: 9 + header_len] + bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
        b"sealed by libsodium",
        sealed[: 9 + header_len],
        base64.b64decode(header["nonce"]),
        _hkdf_sha256(raw_key, base64.b64decode(header["salt"]), b"wdva/v1/encryption-key"),
    )
    assert unseal(resealed, key=key).payload == b"sealed by libsodium"

    # And both refuse the same damage: one flipped bit in the header or in the text.
    for position in (20, len(sealed) - 40):
        damaged = bytearray(sealed)
        damaged[position] ^= 1
        with pytest.raises((exceptions.CryptoError, AssertionError, ValueError)):
            open_with_libsodium(bytes(damaged), raw_key)
        with pytest.raises((IntegrityError, VaultFormatError, WrongKeyError)):
            unseal(bytes(damaged), key=key)


def test_header_is_canonical_json(key: SecretKey) -> None:
    blob = seal(b"x", key=key, public_metadata={"b": 1, "a": 2})
    (header_len,) = struct.unpack(">I", blob[5:9])
    raw = blob[9 : 9 + header_len]
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    assert raw.isascii()


def test_overhead_is_small_and_constant(key: SecretKey) -> None:
    small, large = seal(b"", key=key), seal(bytes(100_000), key=key)
    assert len(large) - len(small) == 100_000
    assert len(small) < 300


# -- files -----------------------------------------------------------------


def test_file_roundtrip_and_permissions(tmp_path: Path, key: SecretKey) -> None:
    path = seal_to_file(tmp_path / "sub" / "a.wdva", b"payload", key=key)
    assert unseal_file(path, key=key).payload == b"payload"
    assert is_vault(path)
    assert not is_vault(tmp_path / "missing")
    assert not is_vault(b"nope")
    assert is_vault(path.read_bytes())
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["a.wdva"]  # no temp files left behind


def test_refuses_to_overwrite_unless_asked(tmp_path: Path, key: SecretKey) -> None:
    path = seal_to_file(tmp_path / "a.wdva", b"first", key=key)
    with pytest.raises(FileExistsError):
        seal_to_file(path, b"second", key=key)
    assert unseal_file(path, key=key).payload == b"first"
    seal_to_file(path, b"second", key=key, overwrite=True)
    assert unseal_file(path, key=key).payload == b"second"


def test_read_header_from_file_reads_only_the_header(tmp_path: Path, key: SecretKey) -> None:
    path = seal_to_file(
        tmp_path / "a.wdva", bytes(50_000), key=key, public_metadata={"hello": "world"}
    )
    header = read_header(path)
    assert header.public_metadata == {"hello": "world"}
    assert header.to_public_dict()["unlock"] == "key"
    (tmp_path / "junk").write_bytes(b"not a vault at all")
    with pytest.raises(VaultFormatError):
        read_header(tmp_path / "junk")
    (tmp_path / "empty").write_bytes(b"")
    with pytest.raises(VaultFormatError):
        read_header(tmp_path / "empty")


def test_header_repr_hides_binary_fields(key: SecretKey) -> None:
    header = read_header(seal(b"x", key=key))
    text = repr(header)
    assert "salt" not in text
    assert "commitment" not in text


# -- hardening found by adversarial review ---------------------------------


def _raw_vault(header: bytes) -> bytes:
    """A syntactically complete vault around arbitrary header bytes."""
    return b"WDVA\x01" + struct.pack(">I", len(header)) + header + bytes(32)


@pytest.mark.parametrize("depth", [1_100, 30_000])
@pytest.mark.parametrize("bracket", [b"[", b'{"a":'])
def test_deeply_nested_headers_fail_cleanly(depth: int, bracket: bytes) -> None:
    """A tiny hostile file must not crash the reader with RecursionError."""
    blob = _raw_vault((bracket * depth)[:65_536])
    with pytest.raises(VaultFormatError):
        unseal(blob, key=bytes(32))
    with pytest.raises(VaultFormatError):
        read_header(blob)


def _nested(levels: int) -> object:
    value: object = "leaf"
    for _ in range(levels):
        value = [value]
    return value


def test_nesting_limit_is_the_same_for_writers_and_readers(key: SecretKey) -> None:
    """Whatever seal() accepts, unseal() must open - on every Python version."""
    # The header object is level 1 and `meta` level 2, leaving 62 levels inside it.
    deepest = {"deep": _nested(62)}
    assert unseal(seal(b"x", key=key, public_metadata=deepest), key=key).payload == b"x"
    for levels in (63, 70, 5_000):
        with pytest.raises(ValueError, match="public_metadata"):
            seal(b"x", key=key, public_metadata={"deep": _nested(levels)})

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="public_metadata"):
        seal(b"x", key=key, public_metadata={"loop": cyclic})

    # A hand-made header one level too deep is refused by the reader as well.
    text, body = _header_text(seal(b"x", key=key, public_metadata={"deep": "leaf"}))
    raw = text.replace('"leaf"', "[" * 63 + '"leaf"' + "]" * 63).encode()
    with pytest.raises(VaultFormatError, match="64 levels"):
        read_header(b"WDVA\x01" + struct.pack(">I", len(raw)) + raw + body)


def _header_text(blob: bytes) -> tuple[str, bytes]:
    (header_len,) = struct.unpack(">I", blob[5:9])
    return blob[9 : 9 + header_len].decode(), blob[9 + header_len :]


def test_duplicate_header_keys_are_rejected(key: SecretKey) -> None:
    """Readers that resolve duplicates differently must not both accept a file."""
    text, body = _header_text(seal(b"x", key=key, public_metadata={"a": {"b": 1}}))
    variants = [
        text[:-1] + ',"type":"application/octet-stream"}',  # top level
        text.replace('"salt":', '"salt":"AAAA","salt":', 1),  # a field the key schedule uses
        text.replace('{"b":1}', '{"b":1,"b":2}'),  # nested inside meta
    ]
    for variant in variants:
        raw = variant.encode()
        blob = b"WDVA\x01" + struct.pack(">I", len(raw)) + raw + body
        with pytest.raises(VaultFormatError, match="duplicate key"):
            unseal(blob, key=key)
        with pytest.raises(VaultFormatError, match="duplicate key"):
            read_header(blob)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_numbers_in_the_header_are_rejected(key: SecretKey, token: str) -> None:
    text, body = _header_text(seal(b"x", key=key, public_metadata={"n": 1}))
    raw = text.replace('"n":1', f'"n":{token}').encode()
    blob = b"WDVA\x01" + struct.pack(">I", len(raw)) + raw + body
    with pytest.raises(VaultFormatError):
        read_header(blob)
    with pytest.raises(ValueError, match="JSON"):
        seal(b"x", key=key, public_metadata={"n": float("nan")})


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        pytest.param(2**53, "9007199254740992", id="2^53"),
        pytest.param(-(2**53), "-9007199254740992", id="-2^53"),
        pytest.param(10**30, "1" + "0" * 30, id="10^30"),
        pytest.param(10**400, "1" + "0" * 400, id="10^400"),
        # Python itself cannot print an integer this long without being asked to.
        pytest.param(-(10**5000), "-1" + "0" * 5000, id="-10^5000"),
    ],
)
def test_integers_a_double_cannot_hold_are_refused_by_writers_and_readers(
    key: SecretKey, value: int, literal: str
) -> None:
    """A reader whose numbers are doubles would see a different value, or none at all."""
    with pytest.raises(ValueError, match="out of range"):
        seal(b"x", key=key, public_metadata={"n": value})
    with pytest.raises(ValueError, match="out of range"):
        seal(b"x", key=key, public_metadata={"deep": [{"n": value}]})
    text, body = _header_text(seal(b"x", key=key, public_metadata={"n": 1}))
    raw = text.replace('"n":1', f'"n":{literal}').encode()
    blob = b"WDVA\x01" + struct.pack(">I", len(raw)) + raw + body
    with pytest.raises(VaultFormatError, match="out of range"):
        read_header(blob)
    with pytest.raises(VaultFormatError, match="out of range"):
        unseal(blob, key=key)


def test_the_largest_exact_integers_are_accepted(key: SecretKey) -> None:
    edge = 2**53 - 1
    blob = seal(b"x", key=key, public_metadata={"hi": edge, "lo": -edge, "flag": True, "f": 1.5})
    assert unseal(blob, key=key).header.public_metadata == {
        "hi": edge,
        "lo": -edge,
        "flag": True,
        "f": 1.5,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.update(pw=None),  # null is not "absent"
        lambda h: h["pw"].update(r=8.0),
        lambda h: h["pw"].update(p=True),
        lambda h: h["pw"].update(r="8"),
    ],
)
def test_passphrase_parameters_must_have_exact_types(mutate) -> None:  # type: ignore[no-untyped-def]
    blob = seal(b"x", passphrase="pw", scrypt_n=FAST_SCRYPT)
    with pytest.raises(VaultFormatError):
        unseal(_with_header(blob, mutate), passphrase="pw")


def test_null_pw_on_a_key_vault_is_rejected(key: SecretKey) -> None:
    with pytest.raises(VaultFormatError, match="pw"):
        unseal(_with_header(seal(b"x", key=key), lambda h: h.update(pw=None)), key=key)


@pytest.mark.parametrize(
    "bad",
    [
        "x\x1b[2J\x1b[31mPWNED",  # terminal escape sequence
        "plain",  # not type/subtype
        "a/b c",
        "a/b\n",
        "/b",
        "a/",
        "café/x",
        "a/" + "b" * 200,
        7,
        None,
    ],
)
def test_content_type_must_be_a_printable_media_type(key: SecretKey, bad: object) -> None:
    with pytest.raises(ValueError, match="content_type"):
        seal(b"x", key=key, content_type=bad)  # type: ignore[arg-type]
    blob = _with_header(seal(b"x", key=key), lambda h: h.update(type=bad))
    with pytest.raises(VaultFormatError):
        read_header(blob)


def test_the_maximum_scrypt_cost_is_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    """hashlib refuses maxmem >= 2**31, which the documented maximum used to request."""
    captured: dict[str, int] = {}
    real = hashlib.scrypt

    def spy(password: bytes, **kwargs: int) -> bytes:
        captured.update(kwargs)
        return real(password, **{**kwargs, "n": 2**14})  # keep the test fast and small

    monkeypatch.setattr(vault.hashlib, "scrypt", spy)
    blob = seal(b"x", passphrase="pw", scrypt_n=vault.SCRYPT_N_MAX)
    assert captured["n"] == vault.SCRYPT_N_MAX == 2**20
    assert 128 * 8 * captured["n"] < captured["maxmem"] < 2**31
    assert read_header(blob).scrypt_n == 2**20
    assert unseal(blob, passphrase="pw").payload == b"x"
    for n in (2**14, 2**17, 2**19, 2**20):
        assert 128 * 8 * n < vault._scrypt_maxmem(n) < 2**31


def test_refusing_to_overwrite_is_atomic(
    tmp_path: Path, key: SecretKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that appears while we are sealing must not be silently replaced."""
    target = tmp_path / "a.wdva"
    real_seal = vault.seal

    def slow_seal(*args: object, **kwargs: object) -> bytes:
        target.write_bytes(b"written by someone else meanwhile")
        return real_seal(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(vault, "seal", slow_seal)
    with pytest.raises(FileExistsError, match="already exists"):
        seal_to_file(target, b"mine", key=key)
    assert target.read_bytes() == b"written by someone else meanwhile"
    assert [p.name for p in tmp_path.iterdir()] == ["a.wdva"]  # no temp file left behind


@pytest.mark.skipif(os.name != "posix", reason="symlinks and POSIX modes")
def test_symlinks_at_the_target_are_never_followed(tmp_path: Path, key: SecretKey) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    link = tmp_path / "a.wdva"
    link.symlink_to(victim)
    with pytest.raises(FileExistsError):
        seal_to_file(link, b"x", key=key)
    dangling = tmp_path / "b.wdva"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileExistsError):
        seal_to_file(dangling, b"x", key=key)
    seal_to_file(link, b"x", key=key, overwrite=True)
    assert not link.is_symlink()  # the link itself was replaced
    assert victim.read_text() == "precious"
    assert stat.S_IMODE(link.stat().st_mode) == 0o600


def test_failed_writes_leave_no_trace(
    tmp_path: Path, key: SecretKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = seal_to_file(tmp_path / "a.wdva", b"old", key=key)

    def no_space(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", no_space)
    with pytest.raises(OSError, match="No space"):
        seal_to_file(target, b"new", key=key, overwrite=True)
    with pytest.raises(OSError, match="No space"):
        seal_to_file(tmp_path / "b.wdva", b"new", key=key)
    monkeypatch.undo()
    assert unseal_file(target, key=key).payload == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["a.wdva"]
