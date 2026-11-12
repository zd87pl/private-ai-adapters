# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The WDVA vault: an authenticated, self-describing encrypted container.

Layout (all integers big-endian; the normative spec is ``docs/FORMAT.md``)::

    offset  size  field
    0       4     magic            b"WDVA"
    4       1     format version   0x01
    5       4     header length H  (1 <= H <= 65536)
    9       H     header           canonical JSON, UTF-8
    9+H     N     ciphertext       XChaCha20 keystream XOR payload
    9+H+N   16    tag              Poly1305

Key schedule::

    master   = the 32-byte key, or scrypt(passphrase) with parameters from the header
    enc_key  = HKDF-SHA256(ikm=master, salt=header.salt, info="wdva/v1/encryption-key")
    commit   = HKDF-SHA256(ikm=master, salt=header.salt, info="wdva/v1/key-commitment")

Everything from offset 0 to the end of the header is fed to the AEAD as
associated data, so no header field can be changed without detection. The
commitment makes the scheme key-committing and lets :func:`unseal` tell a wrong
key (:class:`~wdva.errors.WrongKeyError`) apart from corruption
(:class:`~wdva.errors.IntegrityError`).

This module only deals in bytes. It needs no ML libraries.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from Crypto.Cipher import ChaCha20_Poly1305
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF

from wdva._util import atomic_write, check_json_depth, strict_json_loads
from wdva.errors import IntegrityError, VaultFormatError, WrongKeyError
from wdva.keys import KEY_SIZE, KeyLike, _wipe, borrow_key

__all__ = [
    "ALGORITHM",
    "CONTENT_TYPE_RAW",
    "FORMAT_VERSION",
    "KDF_NAME",
    "MAGIC",
    "SCRYPT_N_DEFAULT",
    "Unsealed",
    "VaultHeader",
    "is_vault",
    "read_header",
    "seal",
    "seal_to_file",
    "unseal",
    "unseal_file",
]

MAGIC = b"WDVA"
FORMAT_VERSION = 1
ALGORITHM = "XChaCha20-Poly1305"
KDF_NAME = "HKDF-SHA256"
CONTENT_TYPE_RAW = "application/octet-stream"

SALT_SIZE = 32
NONCE_SIZE = 24
TAG_SIZE = 16
COMMIT_SIZE = 32
MAX_HEADER_SIZE = 64 * 1024

PASSPHRASE_SALT_SIZE = 16
SCRYPT_N_DEFAULT = 2**17
SCRYPT_N_MIN = 2**14
SCRYPT_N_MAX = 2**20
SCRYPT_R = 8
SCRYPT_P = 1

_INFO_ENCRYPTION = b"wdva/v1/encryption-key"
_INFO_COMMITMENT = b"wdva/v1/key-commitment"

MAX_CONTENT_TYPE_LENGTH = 127

_PREFIX = struct.Struct(">4sBI")  # magic, version, header length
# A media type, restricted to RFC 6838's token characters. Being strict here also
# keeps control characters from an unverified header away from terminals.
_MEDIA_NAME = r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*"
_CONTENT_TYPE = re.compile(rf"{_MEDIA_NAME}/{_MEDIA_NAME}")
_HEADER_KEYS = frozenset({"alg", "kdf", "salt", "nonce", "commit", "type", "pw", "meta"})
_REQUIRED_HEADER_KEYS = frozenset({"alg", "kdf", "salt", "nonce", "commit", "type"})
_PW_KEYS = frozenset({"kdf", "n", "r", "p", "salt"})


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultHeader:
    """The public, authenticated (but **not encrypted**) part of a vault.

    Anyone who can read the file can read these fields. Nothing here reveals
    the payload, but ``public_metadata`` is whatever the sealer chose to put
    there, so do not store anything sensitive in it.
    """

    version: int
    algorithm: str
    kdf: str
    content_type: str
    salt: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    commitment: bytes = field(repr=False)
    scrypt_n: int | None = None
    scrypt_salt: bytes | None = field(default=None, repr=False)
    public_metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def uses_passphrase(self) -> bool:
        """Whether this vault is unlocked with a passphrase rather than a raw key."""
        return self.scrypt_n is not None

    def to_public_dict(self) -> dict[str, Any]:
        """A JSON-friendly description, as shown by ``wdva inspect``."""
        out: dict[str, Any] = {
            "format_version": self.version,
            "algorithm": self.algorithm,
            "kdf": self.kdf,
            "content_type": self.content_type,
            "unlock": "passphrase" if self.uses_passphrase else "key",
        }
        if self.scrypt_n is not None:
            out["passphrase_kdf"] = {
                "name": "scrypt",
                "n": self.scrypt_n,
                "r": SCRYPT_R,
                "p": SCRYPT_P,
            }
        out["public_metadata"] = dict(self.public_metadata)
        return out


class Unsealed(NamedTuple):
    """Result of :func:`unseal`: the decrypted payload and the verified header."""

    payload: bytes
    header: VaultHeader


def _is_content_type(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_CONTENT_TYPE_LENGTH
        and _CONTENT_TYPE.fullmatch(value) is not None
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: Any, expected_len: int, name: str) -> bytes:
    if not isinstance(value, str):
        raise VaultFormatError(f"header field {name!r} must be a string")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VaultFormatError(f"header field {name!r} is not valid base64") from exc
    if len(raw) != expected_len:
        raise VaultFormatError(
            f"header field {name!r} must decode to {expected_len} bytes, got {len(raw)}"
        )
    return raw


def _encode_header(
    *,
    content_type: str,
    salt: bytes,
    nonce: bytes,
    commitment: bytes,
    scrypt_n: int | None,
    scrypt_salt: bytes | None,
    public_metadata: Mapping[str, Any],
) -> bytes:
    doc: dict[str, Any] = {
        "alg": ALGORITHM,
        "kdf": KDF_NAME,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "commit": _b64(commitment),
        "type": content_type,
    }
    if scrypt_n is not None:
        assert scrypt_salt is not None
        doc["pw"] = {
            "kdf": "scrypt",
            "n": scrypt_n,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": _b64(scrypt_salt),
        }
    if public_metadata:
        doc["meta"] = dict(public_metadata)
    try:
        check_json_depth(doc)  # the same limit every reader enforces
        encoded = json.dumps(
            doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except RecursionError:
        raise ValueError("public_metadata must be JSON-serialisable: nested too deeply") from None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"public_metadata must be JSON-serialisable: {exc}") from exc
    if len(encoded) > MAX_HEADER_SIZE:
        raise ValueError(
            f"vault header would be {len(encoded)} bytes; the limit is {MAX_HEADER_SIZE}. "
            "Put large or private metadata in the encrypted payload instead."
        )
    return encoded


def _validate_scrypt_n(n: Any) -> int:
    if (
        not isinstance(n, int)
        or isinstance(n, bool)
        or n < SCRYPT_N_MIN
        or n > SCRYPT_N_MAX
        or n & (n - 1)
    ):
        raise VaultFormatError(
            f"scrypt cost must be a power of two between {SCRYPT_N_MIN} and {SCRYPT_N_MAX}"
        )
    return n


def _decode_header(version: int, raw: bytes) -> VaultHeader:
    try:
        doc = strict_json_loads(raw)
    except ValueError as exc:
        raise VaultFormatError(f"vault header is not valid JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise VaultFormatError("vault header must be a JSON object")
    unknown = set(doc) - _HEADER_KEYS
    if unknown:
        raise VaultFormatError(f"vault header has unknown fields: {sorted(unknown)}")
    missing = _REQUIRED_HEADER_KEYS - set(doc)
    if missing:
        raise VaultFormatError(f"vault header is missing fields: {sorted(missing)}")
    if doc["alg"] != ALGORITHM:
        raise VaultFormatError(f"unsupported cipher {doc['alg']!r} (expected {ALGORITHM})")
    if doc["kdf"] != KDF_NAME:
        raise VaultFormatError(f"unsupported KDF {doc['kdf']!r} (expected {KDF_NAME})")
    if not _is_content_type(doc["type"]):
        raise VaultFormatError("header field 'type' must be a media type such as 'a/b'")

    scrypt_n: int | None = None
    scrypt_salt: bytes | None = None
    if "pw" in doc:
        pw = doc["pw"]
        if not isinstance(pw, dict) or set(pw) != _PW_KEYS:
            raise VaultFormatError("header field 'pw' is malformed")
        if (
            pw["kdf"] != "scrypt"
            or type(pw["r"]) is not int
            or pw["r"] != SCRYPT_R
            or type(pw["p"]) is not int
            or pw["p"] != SCRYPT_P
        ):
            raise VaultFormatError(
                f"unsupported passphrase KDF (expected scrypt with r={SCRYPT_R}, p={SCRYPT_P})"
            )
        scrypt_n = _validate_scrypt_n(pw["n"])
        scrypt_salt = _unb64(pw["salt"], PASSPHRASE_SALT_SIZE, "pw.salt")

    meta = doc.get("meta", {})
    if not isinstance(meta, dict):
        raise VaultFormatError("header field 'meta' must be a JSON object")

    return VaultHeader(
        version=version,
        algorithm=ALGORITHM,
        kdf=KDF_NAME,
        content_type=doc["type"],
        salt=_unb64(doc["salt"], SALT_SIZE, "salt"),
        nonce=_unb64(doc["nonce"], NONCE_SIZE, "nonce"),
        commitment=_unb64(doc["commit"], COMMIT_SIZE, "commit"),
        scrypt_n=scrypt_n,
        scrypt_salt=scrypt_salt,
        public_metadata=meta,
    )


def _split(blob: bytes | bytearray | memoryview) -> tuple[VaultHeader, memoryview, memoryview]:
    """Parse a vault into (header, associated data, ciphertext+tag) without decrypting."""
    view = memoryview(blob)
    if len(view) < _PREFIX.size:
        if bytes(view[:1]) == b"{":
            raise VaultFormatError(_LEGACY_HINT)
        raise VaultFormatError("not a WDVA vault: file is too short")
    magic, version, header_len = _PREFIX.unpack_from(view)
    if magic != MAGIC:
        if bytes(view[:1]) == b"{":
            raise VaultFormatError(_LEGACY_HINT)
        raise VaultFormatError("not a WDVA vault: bad magic bytes")
    if version != FORMAT_VERSION:
        raise VaultFormatError(
            f"unsupported vault format version {version}; this build of wdva reads "
            f"version {FORMAT_VERSION}. Upgrade wdva to open this file."
        )
    if not 1 <= header_len <= MAX_HEADER_SIZE:
        raise VaultFormatError(f"vault header length {header_len} is out of range")
    body_start = _PREFIX.size + header_len
    if len(view) < body_start + TAG_SIZE:
        raise VaultFormatError("vault is truncated")
    header = _decode_header(version, bytes(view[_PREFIX.size : body_start]))
    return header, view[:body_start], view[body_start:]


_LEGACY_HINT = (
    "this looks like a JSON-format file from a pre-0.1 WDVA prototype. That format "
    "was never produced by a working release and is not supported."
)


# ---------------------------------------------------------------------------
# Key schedule
# ---------------------------------------------------------------------------


def _normalise_passphrase(passphrase: str) -> bytes:
    if not isinstance(passphrase, str):
        raise TypeError(f"passphrase must be str, got {type(passphrase).__name__}")
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    return unicodedata.normalize("NFKC", passphrase).encode("utf-8")


def _scrypt_maxmem(n: int) -> int:
    """The memory ceiling handed to OpenSSL's scrypt.

    scrypt needs a little over ``128 * r * n`` bytes. Twice that leaves headroom,
    but the limit itself must stay below 2**31, or hashlib refuses the call
    outright - which is what the largest permitted cost (n = 2**20, about 1 GiB)
    would otherwise run into.
    """
    return min(128 * SCRYPT_R * n * 2, 2**31 - 1)


def _scrypt(passphrase: str, salt: bytes, n: int) -> bytearray:
    return bytearray(
        hashlib.scrypt(
            _normalise_passphrase(passphrase),
            salt=salt,
            n=n,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=_scrypt_maxmem(n),
            dklen=KEY_SIZE,
        )
    )


@contextlib.contextmanager
def _master_key(
    key: KeyLike | None,
    passphrase: str | None,
    *,
    scrypt_n: int | None,
    scrypt_salt: bytes | None,
) -> Iterator[bytearray]:
    if passphrase is not None:
        assert scrypt_n is not None
        assert scrypt_salt is not None
        master = _scrypt(passphrase, scrypt_salt, scrypt_n)
        try:
            yield master
        finally:
            _wipe(master)
    else:
        assert key is not None
        with borrow_key(key) as material:
            yield material


def _derive(master: bytearray, salt: bytes) -> tuple[bytes, bytes]:
    """Return (encryption key, key commitment)."""
    enc_key = HKDF(master, KEY_SIZE, salt, SHA256, 1, _INFO_ENCRYPTION)
    commitment = HKDF(master, COMMIT_SIZE, salt, SHA256, 1, _INFO_COMMITMENT)
    assert isinstance(enc_key, bytes)
    assert isinstance(commitment, bytes)
    return enc_key, commitment


def _check_credentials(key: KeyLike | None, passphrase: str | None) -> None:
    if (key is None) == (passphrase is None):
        raise TypeError("provide exactly one of `key` or `passphrase`")


# ---------------------------------------------------------------------------
# Seal / unseal
# ---------------------------------------------------------------------------


def seal(
    payload: bytes | bytearray | memoryview,
    *,
    key: KeyLike | None = None,
    passphrase: str | None = None,
    content_type: str = CONTENT_TYPE_RAW,
    public_metadata: Mapping[str, Any] | None = None,
    scrypt_n: int = SCRYPT_N_DEFAULT,
) -> bytes:
    """Encrypt ``payload`` and return a complete vault as bytes.

    Args:
        payload: The plaintext.
        key: A :class:`~wdva.keys.SecretKey`, 32 raw bytes, or 64 hex characters.
        passphrase: Alternative to ``key``. Stretched with scrypt; the cost
            parameters are stored in the header so :func:`unseal` can repeat them.
        content_type: A label for what the payload is. Stored in the clear.
        public_metadata: Optional JSON object stored **unencrypted** (but
            authenticated) in the header. Never put secrets or document names here.
        scrypt_n: scrypt CPU/memory cost (a power of two). Only used with ``passphrase``.

    A fresh random salt and nonce are drawn for every call, so sealing the same
    payload twice yields unrelated ciphertexts.
    """
    _check_credentials(key, passphrase)
    return _seal(
        payload,
        key=key,
        passphrase=passphrase,
        content_type=content_type,
        public_metadata=public_metadata or {},
        salt=secrets.token_bytes(SALT_SIZE),
        nonce=secrets.token_bytes(NONCE_SIZE),
        scrypt_n=scrypt_n if passphrase is not None else None,
        scrypt_salt=secrets.token_bytes(PASSPHRASE_SALT_SIZE) if passphrase is not None else None,
    )


def _seal(
    payload: bytes | bytearray | memoryview,
    *,
    key: KeyLike | None,
    passphrase: str | None,
    content_type: str,
    public_metadata: Mapping[str, Any],
    salt: bytes,
    nonce: bytes,
    scrypt_n: int | None,
    scrypt_salt: bytes | None,
) -> bytes:
    """Deterministic core of :func:`seal`. Tests call this with fixed salt and nonce."""
    if isinstance(payload, str):
        raise TypeError("payload must be bytes, not str")
    if not _is_content_type(content_type):
        raise ValueError(
            "content_type must be a media type such as 'application/octet-stream' "
            f"(printable ASCII, at most {MAX_CONTENT_TYPE_LENGTH} characters)"
        )
    if scrypt_n is not None:
        try:
            _validate_scrypt_n(scrypt_n)
        except VaultFormatError as exc:
            raise ValueError(str(exc)) from None

    with _master_key(key, passphrase, scrypt_n=scrypt_n, scrypt_salt=scrypt_salt) as master:
        enc_key, commitment = _derive(master, salt)

    header = _encode_header(
        content_type=content_type,
        salt=salt,
        nonce=nonce,
        commitment=commitment,
        scrypt_n=scrypt_n,
        scrypt_salt=scrypt_salt,
        public_metadata=public_metadata,
    )
    aad = _PREFIX.pack(MAGIC, FORMAT_VERSION, len(header)) + header
    cipher = ChaCha20_Poly1305.new(key=enc_key, nonce=nonce)
    cipher.update(aad)
    ciphertext, tag = cipher.encrypt_and_digest(payload)
    return b"".join((aad, ciphertext, tag))


def unseal(
    blob: bytes | bytearray | memoryview,
    *,
    key: KeyLike | None = None,
    passphrase: str | None = None,
) -> Unsealed:
    """Verify and decrypt a vault.

    Raises:
        VaultFormatError: ``blob`` is not a (supported) WDVA vault.
        WrongKeyError: the key or passphrase does not match.
        IntegrityError: the vault was corrupted or tampered with.
    """
    _check_credentials(key, passphrase)
    header, aad, body = _split(blob)

    if header.uses_passphrase and passphrase is None:
        raise WrongKeyError("this vault is locked with a passphrase, not a raw key")
    if not header.uses_passphrase and passphrase is not None:
        raise WrongKeyError("this vault is locked with a raw key, not a passphrase")

    with _master_key(
        key, passphrase, scrypt_n=header.scrypt_n, scrypt_salt=header.scrypt_salt
    ) as master:
        enc_key, commitment = _derive(master, header.salt)

    if not hmac.compare_digest(commitment, header.commitment):
        what = "passphrase" if header.uses_passphrase else "key"
        raise WrongKeyError(f"wrong {what} for this vault (or its header was modified)")

    ciphertext, tag = body[:-TAG_SIZE], body[-TAG_SIZE:]
    cipher = ChaCha20_Poly1305.new(key=enc_key, nonce=header.nonce)
    cipher.update(aad)
    try:
        payload = cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError:
        raise IntegrityError(
            "vault failed authentication: it is corrupted or has been tampered with"
        ) from None
    return Unsealed(payload, header)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def is_vault(source: bytes | bytearray | memoryview | str | os.PathLike[str]) -> bool:
    """Cheap check: does ``source`` start with the WDVA magic bytes?"""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source[: len(MAGIC)]) == MAGIC
    try:
        with open(source, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_header(source: bytes | bytearray | memoryview | str | os.PathLike[str]) -> VaultHeader:
    """Parse a vault's public header **without verifying it**.

    Useful for ``inspect``-style tooling and for deciding whether to prompt for
    a key or a passphrase. Nothing returned here is trustworthy until
    :func:`unseal` succeeds.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        return _split(source)[0]
    with open(source, "rb") as fh:
        prefix = fh.read(_PREFIX.size)
        if len(prefix) == _PREFIX.size and prefix[:4] == MAGIC:
            header_len = _PREFIX.unpack(prefix)[2]
            if 1 <= header_len <= MAX_HEADER_SIZE:
                # Read just enough to parse: header plus a tag's worth of body.
                return _split(prefix + fh.read(header_len + TAG_SIZE))[0]
        return _split(prefix + fh.read(1))[0]  # raises the appropriate VaultFormatError


def seal_to_file(
    path: str | os.PathLike[str],
    payload: bytes | bytearray | memoryview,
    *,
    key: KeyLike | None = None,
    passphrase: str | None = None,
    content_type: str = CONTENT_TYPE_RAW,
    public_metadata: Mapping[str, Any] | None = None,
    scrypt_n: int = SCRYPT_N_DEFAULT,
    overwrite: bool = False,
) -> Path:
    """Seal ``payload`` and write the vault to ``path`` atomically, readable by its owner only.

    (Mode 0600 on POSIX. On Windows the file inherits its folder's access rules.)

    Refuses to replace an existing file unless ``overwrite=True``, and makes
    that decision atomically: if another process creates the file while this
    one is sealing, this call fails rather than replacing it.
    """
    target = Path(path)
    if not overwrite and (target.exists() or target.is_symlink()):
        # Fail before the expensive part. The authoritative check is in atomic_write.
        raise FileExistsError(f"{target} already exists (pass overwrite=True to replace it)")
    blob = seal(
        payload,
        key=key,
        passphrase=passphrase,
        content_type=content_type,
        public_metadata=public_metadata,
        scrypt_n=scrypt_n,
    )
    atomic_write(target, blob, overwrite=overwrite)
    return target


def unseal_file(
    path: str | os.PathLike[str],
    *,
    key: KeyLike | None = None,
    passphrase: str | None = None,
) -> Unsealed:
    """Read and :func:`unseal` the vault at ``path``. Plaintext stays in memory."""
    _check_credentials(key, passphrase)
    return unseal(Path(path).read_bytes(), key=key, passphrase=passphrase)
