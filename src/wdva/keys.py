# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Key material handling.

A WDVA key is 32 random bytes. :class:`SecretKey` wraps those bytes so that they

* never show up in ``repr()``, logs or tracebacks by accident,
* cannot be pickled or compared in non-constant time,
* live in a mutable buffer that :meth:`SecretKey.wipe` overwrites in place.

Wiping is **best effort**. Python and the underlying crypto library make
transient copies that this module cannot reach, and the operating system may
have paged memory to swap. See ``docs/THREAT_MODEL.md``.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import secrets
import stat
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import TypeAlias

from wdva._util import atomic_write
from wdva.errors import KeyWipedError, WDVAWarning

__all__ = [
    "KEY_SIZE",
    "InsecureKeyFileWarning",
    "KeyLike",
    "SecretKey",
    "check_destroyable",
    "destroy_key_file",
]

KEY_SIZE = 32
"""Key length in bytes (256 bits)."""


class InsecureKeyFileWarning(WDVAWarning):
    """A key file is readable by users other than its owner."""


class SecretKey:
    """A 256-bit vault key.

    Create one with :meth:`generate`, :meth:`from_hex` or :meth:`from_file`.
    Use it as a context manager to wipe it when you are done::

        with SecretKey.from_file("notes.key") as key:
            bundle = AdapterBundle.load("notes.wdva", key=key)
    """

    __slots__ = ("_buf", "_wiped")

    def __init__(self, material: bytes | bytearray | memoryview) -> None:
        if isinstance(material, str):
            raise TypeError("SecretKey() takes raw bytes; use SecretKey.from_hex() for text")
        if not isinstance(material, (bytes, bytearray, memoryview)):
            # bytearray(32) would quietly be 32 zero bytes: a valid-looking, worthless key.
            raise TypeError(
                f"SecretKey() takes 32 raw bytes, not {type(material).__name__}; "
                "use SecretKey.generate() for a new key"
            )
        buf = bytearray(material)
        if len(buf) != KEY_SIZE:
            n = len(buf)
            _wipe(buf)
            raise ValueError(f"a WDVA key is exactly {KEY_SIZE} bytes, got {n}")
        self._buf = buf
        self._wiped = False

    # -- construction -----------------------------------------------------

    @classmethod
    def generate(cls) -> SecretKey:
        """Return a fresh random key from the operating system's CSPRNG."""
        return cls(secrets.token_bytes(KEY_SIZE))

    @classmethod
    def from_hex(cls, text: str) -> SecretKey:
        """Parse a key from 64 hexadecimal characters (surrounding whitespace is ignored)."""
        if not isinstance(text, str):
            raise TypeError(f"expected str, got {type(text).__name__}")
        cleaned = text.strip()
        if len(cleaned) != KEY_SIZE * 2:
            raise ValueError(
                f"a hex-encoded WDVA key is exactly {KEY_SIZE * 2} characters, got {len(cleaned)}"
            )
        try:
            raw = bytes.fromhex(cleaned)
        except ValueError:
            # Deliberately do not chain or echo: the offending text is key material.
            raise ValueError("key contains non-hexadecimal characters") from None
        return cls(raw)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> SecretKey:
        """Read a key file written by :meth:`save`.

        Emits :class:`InsecureKeyFileWarning` on POSIX systems when the file is
        readable or writable by group or others.
        """
        p = Path(path)
        if os.name == "posix":
            mode = stat.S_IMODE(p.stat().st_mode)
            if mode & 0o077:
                warnings.warn(
                    f"key file {p} has permissions {mode:04o}; it should be 0600 "
                    f"(run: chmod 600 {p})",
                    InsecureKeyFileWarning,
                    stacklevel=2,
                )
        return cls.from_hex(_read_key_text(p))

    # -- export -----------------------------------------------------------

    def hex(self) -> str:
        """Return the key as 64 hex characters. Handle the result as a secret."""
        return self._material().hex()

    def save(self, path: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
        """Write the key to ``path``, readable by its owner only.

        (Mode 0600 on POSIX. On Windows the file inherits its folder's access
        rules, so keep key files in a folder only you can read.)

        Refuses to replace an existing file unless ``overwrite=True``: silently
        overwriting a key makes every vault sealed with it unreadable. Either
        way the key is written to a new file, flushed to disk and then moved
        into place, so a failed save never leaves an empty or half-written key
        file, an existing file is never open for writing while it still has
        looser permissions, and a symlink at ``path`` is replaced, not followed.
        """
        p = Path(path)
        text = self.hex() + "\n"  # raises KeyWipedError before anything is touched
        atomic_write(p, text.encode("ascii"), overwrite=overwrite)
        return p

    # -- lifecycle --------------------------------------------------------

    def wipe(self) -> None:
        """Overwrite the key bytes in place. The key is unusable afterwards."""
        _wipe(self._buf)
        self._wiped = True

    @property
    def wiped(self) -> bool:
        """Whether :meth:`wipe` has been called."""
        return self._wiped

    def _material(self) -> bytearray:
        """Internal: the live key buffer. Never hand this to callers."""
        if self._wiped:
            raise KeyWipedError("this key has been wiped and can no longer be used")
        return self._buf

    def __enter__(self) -> SecretKey:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.wipe()

    # -- safety rails -----------------------------------------------------

    def __repr__(self) -> str:
        return "SecretKey(<wiped>)" if self._wiped else "SecretKey(<redacted>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretKey):
            return NotImplemented
        if self._wiped or other._wiped:
            return False
        return hmac.compare_digest(self._buf, other._buf)

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("SecretKey cannot be pickled; use save() or hex() deliberately")

    def __copy__(self) -> SecretKey:
        return SecretKey(self._material())

    def __deepcopy__(self, memo: dict[int, object]) -> SecretKey:
        return SecretKey(self._material())


KeyLike: TypeAlias = SecretKey | bytes | bytearray | memoryview | str
"""Anything accepted as a key: a :class:`SecretKey`, 32 raw bytes, or 64 hex characters."""


@contextlib.contextmanager
def borrow_key(key: KeyLike) -> Iterator[bytearray]:
    """Yield the raw key buffer for the duration of one cryptographic operation.

    If ``key`` is not already a :class:`SecretKey`, a temporary one is created
    and wiped on exit, so this package never keeps its own copy of caller-owned
    key bytes alive longer than a single call.
    """
    if isinstance(key, SecretKey):
        yield key._material()
        return
    if isinstance(key, str):
        temp = SecretKey.from_hex(key)
    elif isinstance(key, (bytes, bytearray, memoryview)):
        temp = SecretKey(key)
    else:
        raise TypeError(
            f"key must be a SecretKey, 32 bytes or 64 hex characters, got {type(key).__name__}"
        )
    try:
        yield temp._material()
    finally:
        temp.wipe()


def destroy_key_file(path: str | os.PathLike[str]) -> None:
    """Overwrite a key file with random bytes, flush it to disk, then delete it.

    This is how you *crypto-shred* a vault: once every copy of its key is gone,
    the ciphertext is unrecoverable. Two honest caveats:

    * It only destroys **this** copy. Backups, password-manager entries and
      copies in other processes are yours to track down.
    * Overwriting is best effort. SSDs, copy-on-write and journaling file
      systems, and snapshots may retain the old blocks.
    """
    p = Path(path)
    check_destroyable(p)
    size = max(p.stat().st_size, KEY_SIZE * 2 + 1)
    with open(p, "r+b", buffering=0) as fh:
        fh.write(secrets.token_bytes(size))
        fh.flush()
        os.fsync(fh.fileno())
    p.unlink()


def check_destroyable(path: str | os.PathLike[str]) -> None:
    """Raise unless ``path`` is a regular file that holds a WDVA key.

    Destroying is irreversible, so it is refused for anything that merely sits
    where a key was expected: a vault, a document, a symbolic link.
    """
    p = Path(path)
    if p.is_symlink():
        raise ValueError(f"{p} is a symbolic link; destroy the file it points to instead")
    if not p.is_file():
        raise FileNotFoundError(f"no key file at {p}")
    try:
        SecretKey.from_hex(_read_key_text(p)).wipe()
    except ValueError:
        raise ValueError(f"{p} does not contain a WDVA key; refusing to destroy it") from None


def _read_key_text(path: Path) -> str:
    """Read a key file as text, tolerating the encodings that shells and editors produce."""
    raw = path.read_bytes()
    hint = (
        f"{path} is not a key file: expected 64 hexadecimal characters of plain text "
        "(create key files with `wdva keygen -o FILE`)"
    )
    if len(raw) > 4096:
        raise ValueError(hint)
    try:
        if raw.startswith(b"\xef\xbb\xbf"):  # UTF-8 with a byte-order mark (Notepad)
            return raw[3:].decode("ascii")
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 (PowerShell 5 redirection)
            return raw.decode("utf-16")
        return raw.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(hint) from None


def _wipe(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0
