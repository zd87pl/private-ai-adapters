# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The store: one directory of sealed files that holds an owner's documents.

Layout::

    <store>/                        mode 0700, owned by the user
      keyslot.wdva                  the store key, locked with the passphrase (scrypt)
      docs/d-<12 hex>.wdva          one per document: metadata and its text
      originals/d-<12 hex>.wdva     optional: the bytes of the file the text came from
      ledger/<10 digits>.wdva       the record, one entry per file (see wdva.gate.ledger)
      run/lock                      the lock of the process that has the store open for writing
      run/gate.sock                 where agent applications are served while it is unlocked

Every file outside ``run/`` is an ordinary WDVA vault without public metadata.
The key slot is sealed with the passphrase; everything else is sealed with the
random 256-bit store key that the key slot holds. Each kind of file has its own
content type, checked after unsealing, so one sealed file cannot stand in for
another kind.

Without the passphrase the directory shows that it is a store, the content type
and exact size of every file, and so the number and sizes of documents and
records and the times they were written. It shows no collection name, title,
file name, text, query, policy or client name.

The set of documents is what the record says: ``add`` entries minus ``remove``
entries. A file in ``docs/`` that the record does not list is ignored. A listed
document whose file is missing, holds another document or holds other text is
unavailable (:meth:`Store.read_document` raises).

Only one process can have a store open for writing: :func:`take_lock` takes an
operating system lock on ``run/lock`` and never waits. Callers take the lock
first and ask for the passphrase afterwards, so that nobody types a passphrase
into a program that then turns out to be locked out.

The store keeps the policy as a JSON object and does not interpret it, and it
takes document text that was already cleaned and cut into passages; both jobs
belong to other modules.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wdva import vault
from wdva._util import atomic_write, strict_json_loads
from wdva.errors import VaultFormatError, WDVAError, WrongKeyError
from wdva.gate import paths
from wdva.gate.errors import GateError, LedgerError, StoreBusyError, StoreFormatError
from wdva.gate.ledger import (
    DocumentInfo,
    Head,
    Ledger,
    LedgerState,
    format_time,
    head_without_key,
    utc_now,
)
from wdva.keys import SecretKey

__all__ = [
    "CHUNK_CHARS",
    "DOCUMENT_TYPE",
    "KEYSLOT_NAME",
    "KEYSLOT_SCRYPT_N",
    "KEYSLOT_TYPE",
    "MAX_DOCUMENT_TERMS",
    "MAX_SOURCE_BYTES",
    "MAX_STORE_CHARS",
    "MAX_STORE_TERMS",
    "MAX_TEXT_CHARS",
    "MAX_TITLE_CHARS",
    "ORIGINAL_TYPE",
    "REASON_DUPLICATE",
    "REASON_EMPTY",
    "REASON_TOO_LARGE",
    "SANITIZER",
    "DocumentInfo",
    "DocumentRefusedError",
    "Store",
    "StoreInfo",
    "StoreLocationError",
    "StoreLock",
    "UnknownDocumentError",
    "check_passphrase",
    "check_private",
    "default_policy_json",
    "init_store",
    "open_store",
    "store_info",
    "take_lock",
]

KEYSLOT_NAME = "keyslot.wdva"
DOCS_DIR = "docs"
ORIGINALS_DIR = "originals"
LEDGER_DIR = "ledger"

KEYSLOT_TYPE = "application/vnd.wdva.gate.keyslot.v1"
DOCUMENT_TYPE = "application/vnd.wdva.gate.document.v1"
ORIGINAL_TYPE = "application/vnd.wdva.gate.original.v1"

KEYSLOT_SCRYPT_N = vault.SCRYPT_N_DEFAULT
"""The scrypt cost of a new key slot. Read when a store is created, so tests can lower it."""

CHUNK_CHARS = 800
"""The longest passage of a new store, in characters."""

SANITIZER = 1
"""The version of the text cleaning that documents of a new store went through."""

MAX_SOURCE_BYTES = 64 * 1024 * 1024
"""The largest file a document may come from."""

MAX_TEXT_CHARS = 4_000_000
"""The longest text of one document."""

MAX_DOCUMENT_TERMS = 200_000
"""The most distinct search terms one document may have."""

MAX_STORE_CHARS = 50_000_000
"""The most text a store holds, over all documents."""

MAX_STORE_TERMS = 2_000_000
"""The most distinct search terms a store holds, over all documents."""

MAX_TITLE_CHARS = 120

REASON_EMPTY = "empty"
REASON_TOO_LARGE = "too large"
REASON_DUPLICATE = "already in the store"

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_DOC_ID = re.compile(r"d-[0-9a-f]{12}")
_STORE_ID = re.compile(r"[0-9a-f]{16}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TITLE_SOURCES = ("owner", "file-name")
_SOURCE_KEYS = ("name", "path", "sha256", "bytes")
_MAX_PATH_CHARS = 4096
_SEALED_SUFFIX = ".wdva"

_DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "serial": 1,
    "pools": {
        "hosted": {"chars_per_day": 40000, "chars_per_30_days": 300000},
        "local": {"chars_per_day": 40000, "chars_per_30_days": 300000},
    },
    "defaults": {
        "chars_per_call": 2000,
        "chars_per_day": 20000,
        "chars_per_30_days": 150000,
        "searches_per_day": 100,
        "calls_per_minute": 30,
    },
    "per_document": {"max_chars": 8000, "max_fraction": 0.5},
    "approval": {"ttl_minutes": 10},
    "collections": {},
    "clients": {},
    "redact_literals": [],
}


class StoreLocationError(GateError):
    """The store directory is somewhere it must not be.

    Other users can reach it, it belongs to someone else, or it lies in a
    folder that a file-synchronising service copies elsewhere.
    """


class UnknownDocumentError(GateError, LookupError):
    """The store has no document with that id, or kept no original file for it."""


class DocumentRefusedError(GateError):
    """A document was not added, for a reason the owner can be told.

    ``reason`` is one of ``"empty"``, ``"too large"`` and ``"already in the
    store"``; ``detail`` says which limit or names nothing. For a duplicate,
    ``duplicate_of`` is the id of the document that is already there. Nothing
    was written.
    """

    def __init__(self, reason: str, detail: str = "", *, duplicate_of: str | None = None) -> None:
        super().__init__(f"{reason} ({detail})" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.duplicate_of = duplicate_of


def default_policy_json() -> dict[str, Any]:
    """The policy of a new store as a JSON object: default limits, nothing reachable."""
    return copy.deepcopy(_DEFAULT_POLICY)


def _absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _delete(path: Path) -> None:
    os.unlink(path)


def _flush_directory(directory: Path) -> None:
    """Ask for a directory's entries to be written to disk. Best effort, never an error."""
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _is_regular(path: str | os.PathLike[str]) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The directory and its lock
# ---------------------------------------------------------------------------


def check_private(store_dir: str | os.PathLike[str]) -> None:
    """Refuse a store directory that other users can reach.

    The directory must belong to the user who opens it and give no access to
    the group or to others (mode 0700). The error names the command that fixes
    the mode. On systems without these permission bits nothing is checked.
    """
    path = _absolute(store_dir)
    try:
        status = os.stat(path)
    except FileNotFoundError:
        raise StoreFormatError(f"there is no store at {path}") from None
    if not stat.S_ISDIR(status.st_mode):
        raise StoreFormatError(f"{path} is not a directory")
    if os.name != "posix":
        return
    if status.st_uid != os.geteuid():
        raise StoreLocationError(
            f"{path} belongs to another user; a store must be owned by the user who opens it"
        )
    mode = stat.S_IMODE(status.st_mode)
    if mode & 0o077:
        raise StoreLocationError(
            f"{path} can be accessed by other users (mode {mode:04o}); run: chmod 700 {path}"
        )


class StoreLock:
    """The lock on a store, held by this process until :meth:`release`.

    Get one from :func:`take_lock`. It is a context manager.
    """

    def __init__(self, store: Path, descriptor: int) -> None:
        self._store = store
        self._descriptor: int | None = descriptor

    @property
    def store(self) -> Path:
        """The store directory this lock belongs to, as an absolute path."""
        return self._store

    @property
    def held(self) -> bool:
        """Whether the lock is still held."""
        return self._descriptor is not None

    def release(self) -> None:
        """Give the lock up. Calling it twice is harmless."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> StoreLock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.release()

    def __repr__(self) -> str:
        return f"StoreLock({str(self._store)!r}, {'held' if self.held else 'released'})"


def take_lock(store_dir: str | os.PathLike[str]) -> StoreLock:
    """Take the store's lock, or raise :class:`StoreBusyError` at once when it is held.

    The lock is an operating system lock (``flock``) on ``run/lock``; ``run/``
    is created with mode 0700 when it is missing. It never waits. The lock
    belongs to this process and ends with it, however the process ends, so a
    crash cannot leave a store locked. A second attempt inside the same process
    is refused like one from another process.

    Take the lock before asking for the passphrase, and hand it to
    :func:`open_store`.
    """
    import fcntl

    path = _absolute(store_dir)
    if not path.is_dir():
        raise StoreFormatError(f"there is no store at {path}")
    run = paths.run_dir(path)
    run.mkdir(mode=0o700, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(paths.lock_path(path), flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            raise StoreBusyError(
                f"the store at {path} is open in another program: it is unlocked or being "
                "changed there. Lock it there first."
            ) from None
        raise
    return StoreLock(path, descriptor)


# ---------------------------------------------------------------------------
# What is known without the key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreInfo:
    """What a store directory shows to anyone who can list it, without the passphrase."""

    path: Path
    exists: bool
    """Whether there is a key slot, which is what makes a directory a store."""
    sealed_files: int = 0
    """Key slot, documents, originals and records together."""
    sealed_bytes: int = 0
    documents: int = 0
    originals: int = 0
    records: int = 0
    head: Head | None = None
    """The newest record by file name, with the hash of its file. Not verified."""
    head_time: datetime | None = None
    """When that file was written, by the file system's clock."""
    scrypt_n: int | None = None
    """The scrypt cost the key slot states. Not verified."""
    synced_folder: str | None = None
    """The part of the path that names a synchronised folder, if one does."""


def _sealed_files(directory: Path) -> tuple[int, int]:
    """Number and total size of the sealed files directly in ``directory``."""
    count = size = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.name.endswith(_SEALED_SUFFIX):
                    continue
                with contextlib.suppress(OSError):
                    if entry.is_file(follow_symlinks=False):
                        count += 1
                        size += entry.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    return count, size


def store_info(store_dir: str | os.PathLike[str]) -> StoreInfo:
    """Describe a store directory without the passphrase and without changing anything.

    Works on a locked store, on a store that another program has open, and on
    a path where no store exists (``exists`` is then false). Nothing it reports
    is authenticated: it is what the file names and sizes say.
    """
    path = _absolute(store_dir)
    synced = paths.synced_folder_component(path)
    slot = path / KEYSLOT_NAME
    if not _is_regular(slot):
        return StoreInfo(path=path, exists=False, synced_folder=synced)
    scrypt_n: int | None = None
    slot_size = 0
    with contextlib.suppress(OSError, VaultFormatError):
        slot_size = os.lstat(slot).st_size
        scrypt_n = vault.read_header(slot).scrypt_n
    documents, document_bytes = _sealed_files(path / DOCS_DIR)
    originals, original_bytes = _sealed_files(path / ORIGINALS_DIR)
    records, record_bytes = _sealed_files(path / LEDGER_DIR)
    head = head_without_key(path / LEDGER_DIR)
    head_time: datetime | None = None
    if head is not None:
        with contextlib.suppress(OSError):
            newest = path / LEDGER_DIR / f"{head.seq:010d}{_SEALED_SUFFIX}"
            head_time = datetime.fromtimestamp(int(os.lstat(newest).st_mtime), tz=timezone.utc)
    return StoreInfo(
        path=path,
        exists=True,
        sealed_files=1 + documents + originals + records,
        sealed_bytes=slot_size + document_bytes + original_bytes + record_bytes,
        documents=documents,
        originals=originals,
        records=records,
        head=head,
        head_time=head_time,
        scrypt_n=scrypt_n,
        synced_folder=synced,
    )


# ---------------------------------------------------------------------------
# The key slot
# ---------------------------------------------------------------------------


def _usable_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str):
        raise TypeError(f"passphrase must be str, got {type(passphrase).__name__}")
    if not passphrase:
        raise ValueError("passphrase must not be empty")


def _read_keyslot(path: Path, passphrase: str) -> tuple[SecretKey, dict[str, Any]]:
    """Open the key slot of the store at ``path``: the store key and the slot's other fields.

    This is the one place where the passphrase is stretched.
    """
    slot_path = path / KEYSLOT_NAME
    try:
        blob = slot_path.read_bytes()
    except FileNotFoundError:
        raise StoreFormatError(f"there is no store at {path}") from None
    try:
        header = vault.read_header(blob)
    except VaultFormatError as exc:
        raise StoreFormatError(f"{slot_path} is not a key slot: {exc}") from None
    if header.content_type != KEYSLOT_TYPE:
        raise StoreFormatError(f"{slot_path} is another kind of sealed file, not a key slot")
    if not header.uses_passphrase:
        raise StoreFormatError(
            f"{slot_path} is locked with a key file; this version opens stores with a "
            "passphrase only"
        )
    payload = vault.unseal(blob, passphrase=passphrase).payload
    try:
        slot = strict_json_loads(payload)
        if not isinstance(slot, dict) or slot.get("format") != KEYSLOT_TYPE:
            raise ValueError("not a key slot")
        store_id, key_hex, created = slot.get("store_id"), slot.get("key"), slot.get("created")
        if not isinstance(store_id, str) or _STORE_ID.fullmatch(store_id) is None:
            raise ValueError("no store id")
        if not isinstance(key_hex, str) or _HEX64.fullmatch(key_hex) is None:
            raise ValueError("no store key")
        if not isinstance(created, str):
            raise ValueError("no creation time")
        chunk_chars = slot.get("chunk_chars")
        if type(chunk_chars) is not int or not 2 <= chunk_chars <= CHUNK_CHARS:
            raise ValueError("passage size out of range")
    except ValueError as exc:
        raise StoreFormatError(f"{slot_path} is malformed ({exc})") from None
    if type(slot.get("sanitizer")) is not int or slot["sanitizer"] != SANITIZER:
        raise StoreFormatError(
            f"{slot_path} was written by another version of wdva (text cleaning version "
            f"{slot.get('sanitizer')!r:.20}); this one reads version {SANITIZER}"
        )
    key = SecretKey.from_hex(key_hex)
    public = {name: value for name, value in slot.items() if name != "key"}
    return key, public


def check_passphrase(store_dir: str | os.PathLike[str], passphrase: str) -> bool:
    """Whether ``passphrase`` opens the key slot of the store at ``store_dir``.

    Opens nothing else, takes no lock and keeps no key; it costs one passphrase
    stretch. A store that does not exist or whose key slot is damaged raises.
    While a store is open, prefer :meth:`Store.check_passphrase`, which also
    checks that the key slot on disk still holds the key of the open store.
    """
    if not isinstance(passphrase, str):
        raise TypeError(f"passphrase must be str, got {type(passphrase).__name__}")
    if not passphrase:
        return False
    try:
        key, _ = _read_keyslot(_absolute(store_dir), passphrase)
    except WrongKeyError:
        return False
    key.wipe()
    return True


# ---------------------------------------------------------------------------
# Creating and opening
# ---------------------------------------------------------------------------


def _unfinished(path: Path) -> list[str]:
    """The files an interrupted creation left in ``path``; refuse a directory with more.

    A creation that was interrupted leaves no key slot, and so nothing that
    could ever be opened: at most the first two records, empty directories and
    temporary files. Anything else means the directory is in use for something,
    and raises :class:`FileExistsError`.
    """
    leftovers: list[str] = []
    refusal = FileExistsError(f"{path} is not empty; a new store needs an empty directory")
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.name == paths.run_dir(path).name:
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            if entry.name in (DOCS_DIR, ORIGINALS_DIR) and is_dir and not os.listdir(entry.path):
                continue
            if entry.name == LEDGER_DIR and is_dir:
                with os.scandir(entry.path) as records:
                    for record in records:
                        early = record.name in ("0000000001.wdva", "0000000002.wdva")
                        if not record.is_file(follow_symlinks=False) or not (
                            early or record.name.startswith(".")
                        ):
                            raise refusal
                        leftovers.append(record.path)
                continue
            if (
                entry.name.startswith(f".{KEYSLOT_NAME}.")
                and entry.name.endswith(".tmp")
                and entry.is_file(follow_symlinks=False)
            ):
                leftovers.append(entry.path)
                continue
            raise refusal
    return leftovers


def init_store(
    store_dir: str | os.PathLike[str],
    *,
    passphrase: str,
    scrypt_n: int | None = None,
    allow_synced_folder: bool = False,
    policy: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Store:
    """Create a store in ``store_dir`` and return it, open for writing.

    A random 256-bit store key and a store id are drawn; the record is begun
    with its ``init`` entry and the first policy (``policy``, or the default
    one that reaches nothing; its serial is set to 1); the key slot, sealed
    with ``passphrase``, is written last. A creation that is interrupted
    therefore leaves no store, and the next attempt clears what it left.

    ``scrypt_n=None`` means :data:`KEYSLOT_SCRYPT_N`. The length of the
    passphrase is not checked here; that is the caller's rule to enforce.

    Raises :class:`FileExistsError` when the directory already holds a store or
    anything else, and :class:`StoreLocationError` when other users can reach
    it or, unless ``allow_synced_folder``, when its path names a synchronised
    folder (Dropbox, OneDrive, Google Drive, iCloud Drive).
    """
    path = _absolute(store_dir)
    _usable_passphrase(passphrase)
    if policy is not None and not isinstance(policy, Mapping):
        raise TypeError("policy must be a JSON object")
    if not allow_synced_folder:
        component = paths.synced_folder_component(path)
        if component is not None:
            raise StoreLocationError(
                f"{path} is inside a synchronised folder ({component}): the sealed files and "
                "their sizes would be copied to a provider and to other devices. Choose "
                "another place."
            )
    if os.path.lexists(path):
        check_private(path)
        if os.path.lexists(path / KEYSLOT_NAME):
            raise FileExistsError(f"{path} already holds a store")
        _unfinished(path)  # refuse a directory that is in use before anything is put into it
    else:
        os.makedirs(path, mode=0o700)
    lock = take_lock(path)
    key = SecretKey.generate()
    try:
        for leftover in _unfinished(path):
            os.unlink(leftover)
        now = (clock or utc_now)()
        slot: dict[str, Any] = {
            "format": KEYSLOT_TYPE,
            "store_id": secrets.token_hex(8),
            "key": key.hex(),
            "chunk_chars": CHUNK_CHARS,
            "sanitizer": SANITIZER,
            "created": format_time(now),
        }
        sealed_slot = vault.seal(
            json.dumps(slot, separators=(",", ":")).encode("ascii"),
            passphrase=passphrase,
            content_type=KEYSLOT_TYPE,
            scrypt_n=KEYSLOT_SCRYPT_N if scrypt_n is None else scrypt_n,
        )
        del slot["key"]
        for name in (DOCS_DIR, ORIGINALS_DIR, LEDGER_DIR):
            (path / name).mkdir(mode=0o700, exist_ok=True)
        ledger = Ledger.create(path / LEDGER_DIR, key, slot["store_id"], clock=clock)
        first_policy = default_policy_json() if policy is None else copy.deepcopy(dict(policy))
        first_policy["serial"] = 1
        ledger.append("policy", {"serial": 1, "policy": first_policy, "by": "init"}, time=now)
        atomic_write(path / KEYSLOT_NAME, sealed_slot, overwrite=False)
        _flush_directory(path)
    except BaseException:
        key.wipe()
        lock.release()
        raise
    return Store(path, key, slot, ledger, lock, writable=True)


def open_store(
    store_dir: str | os.PathLike[str],
    *,
    passphrase: str,
    write: bool = True,
    lock: StoreLock | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Store:
    """Open the store at ``store_dir`` with its passphrase.

    With ``write=True`` the store's lock is needed. Pass the lock you took with
    :func:`take_lock` before asking for the passphrase; without ``lock`` it is
    taken here, before the passphrase is stretched. Either way the store owns
    the lock from this call on: it is released by :meth:`Store.close`, or at
    once when opening fails. ``write=False`` takes no lock and gives a store
    that cannot change anything.

    The passphrase is stretched exactly once. Opening reads the key slot only;
    the record is verified by the first call that needs it
    (``store.ledger.replay()``, or any method of the store that reads or
    writes the record).

    Raises :class:`~wdva.errors.WrongKeyError` for a wrong passphrase,
    :class:`StoreBusyError` when another program has the store open,
    :class:`StoreFormatError` when there is no store, and
    :class:`StoreLocationError` when other users can reach the directory.
    """
    path = _absolute(store_dir)
    held = lock
    try:
        if lock is not None:
            if not write:
                raise ValueError("a lock is only used when the store is opened for writing")
            if lock.store != path or not lock.held:
                raise ValueError("the lock given is not a held lock of this store")
        _usable_passphrase(passphrase)
        if not _is_regular(path / KEYSLOT_NAME):
            raise StoreFormatError(f"there is no store at {path}")
        check_private(path)
        if write and held is None:
            held = take_lock(path)
        key, slot = _read_keyslot(path, passphrase)
        ledger = Ledger(path / LEDGER_DIR, key, slot["store_id"], writable=write, clock=clock)
    except BaseException:
        if held is not None:
            held.release()
        raise
    return Store(path, key, slot, ledger, held, writable=write)


# ---------------------------------------------------------------------------
# Checks on what is added
# ---------------------------------------------------------------------------


def _storable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check_title(title: str) -> None:
    if not isinstance(title, str):
        raise TypeError("title must be str")
    one_line = all(unicodedata.category(character) not in ("Cc", "Zl", "Zp") for character in title)
    if (
        not 1 <= len(title) <= MAX_TITLE_CHARS
        or not one_line
        or title != title.strip()
        or not _storable(title)
    ):
        raise ValueError(
            f"a title is one line of 1 to {MAX_TITLE_CHARS} characters without control "
            "characters or blanks at its ends"
        )


def _count_passages(spans: Iterable[Sequence[int]], length: int, limit: int) -> int:
    """Check that ``spans`` cut a text of ``length`` characters into passages; return how many."""
    position = count = 0
    problem = ValueError(
        f"spans must cut the text into passages of 1 to {limit} characters that follow "
        "one another from its first character to its last"
    )
    for span in spans:
        try:
            start, end = span
        except (TypeError, ValueError):
            raise problem from None
        if type(start) is not int or type(end) is not int:
            raise problem
        if start != position or not 1 <= end - start <= limit:
            raise problem
        position = end
        count += 1
    if position != length or count == 0:
        raise problem
    return count


def _source_fields(source: Mapping[str, Any] | None, original: bytes | None) -> dict[str, Any]:
    """The ``source`` object of a document: where its text came from, as far as it is known."""
    fields: dict[str, Any] = {}
    if source is not None:
        if not isinstance(source, Mapping):
            raise TypeError("source must be a mapping")
        unknown = sorted(str(name) for name in source if name not in _SOURCE_KEYS)
        if unknown:
            raise ValueError(f"source has unknown fields: {unknown}")
        for name in ("name", "path"):
            value = source.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > _MAX_PATH_CHARS or not _storable(value):
                raise ValueError(f"source {name} must be text of at most {_MAX_PATH_CHARS}")
            fields[name] = value
        digest = source.get("sha256")
        if digest is not None:
            if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
                raise ValueError("source sha256 must be 64 hexadecimal characters")
            fields["sha256"] = digest
        size = source.get("bytes")
        if size is not None:
            if type(size) is not int or size < 0:
                raise ValueError("source bytes must be a whole number")
            fields["bytes"] = size
    if original is not None:
        digest, size = _sha256(original), len(original)
        if fields.get("sha256", digest) != digest or fields.get("bytes", size) != size:
            raise ValueError("source does not describe the original that was given")
        fields["sha256"], fields["bytes"] = digest, size
    return fields


# ---------------------------------------------------------------------------
# The open store
# ---------------------------------------------------------------------------


class Store:
    """An open store. Get one from :func:`init_store` or :func:`open_store`.

    It is a context manager; :meth:`close` wipes the store key and releases
    the lock. ``ledger`` is the store's record. ``clock`` supplies the time of
    every record the store writes; assign another callable to change it.

    The methods may be called from several threads; each runs as a whole.
    """

    def __init__(
        self,
        path: Path,
        key: SecretKey,
        slot: Mapping[str, Any],
        ledger: Ledger,
        lock: StoreLock | None,
        *,
        writable: bool,
    ) -> None:
        self.path = path
        self.store_id: str = slot["store_id"]
        self.chunk_chars: int = slot["chunk_chars"]
        self.sanitizer: int = slot["sanitizer"]
        self.created: str = slot["created"]
        self.writable = writable
        self.ledger = ledger
        self._key = key
        self._held = lock
        self._mutex = threading.RLock()
        self._closed = False

    # -- life cycle -----------------------------------------------------------

    @property
    def clock(self) -> Callable[[], datetime]:
        """The clock that times the store's records. It is the ledger's clock."""
        return self.ledger.clock

    @clock.setter
    def clock(self, clock: Callable[[], datetime]) -> None:
        self.ledger.clock = clock

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` was called."""
        return self._closed

    def close(self) -> None:
        """Wipe the store key and release the lock. Calling it twice is harmless.

        Nothing is written. The key is overwritten in place, which is best
        effort: copies made by Python or the cipher library cannot be reached.
        """
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            self.ledger.close()
            self._key.wipe()
            if self._held is not None:
                self._held.release()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else ("open" if self.writable else "open, read-only")
        return f"Store({str(self.path)!r}, {state})"

    def _state(self) -> LedgerState:
        if self._closed:
            raise GateError("the store is closed")
        return self.ledger.replay()

    def _state_for_writing(self) -> LedgerState:
        if self._closed:
            raise GateError("the store is closed")
        if not self.writable:
            raise GateError("the store was opened for reading only")
        return self.ledger.replay()

    def check_passphrase(self, passphrase: str) -> bool:
        """Whether ``passphrase`` opens this store's key slot as it is on disk now.

        True only when the key slot opens and holds the id and key of this open
        store, so a key slot that was replaced behind the store's back does not
        pass. Costs one passphrase stretch; changes nothing.
        """
        if self._closed:
            raise GateError("the store is closed")
        if not isinstance(passphrase, str):
            raise TypeError(f"passphrase must be str, got {type(passphrase).__name__}")
        if not passphrase:
            return False
        try:
            key, slot = _read_keyslot(self.path, passphrase)
        except WrongKeyError:
            return False
        with key:
            return bool(slot["store_id"] == self.store_id and key == self._key)

    # -- policy ---------------------------------------------------------------

    def policy_json(self) -> dict[str, Any]:
        """The policy in force, as a new JSON object: that of the newest ``policy`` record."""
        with self._mutex:
            policy = self._state().policy
            if policy is None:
                raise GateError("the record holds no policy")
            return copy.deepcopy(policy)

    def set_policy_json(self, obj: Mapping[str, Any], *, by: str) -> dict[str, Any]:
        """Put a policy in force with one ``policy`` record, and return it as stored.

        The stored object is ``obj`` with ``serial`` set to the current serial
        plus one. ``by`` names what made the change (it is recorded). The store
        does not interpret the policy: checking it is the caller's job. The
        change is one atomic append; when it fails, the old policy stays.
        """
        if not isinstance(obj, Mapping):
            raise TypeError("a policy is a JSON object")
        with self._mutex:
            state = self._state_for_writing()
            policy = copy.deepcopy(dict(obj))
            serial = policy["serial"] = state.policy_serial + 1
            self._record(
                "policy",
                {"serial": serial, "policy": policy, "by": by},
                lambda: state.policy_serial == serial,
            )
            return copy.deepcopy(state.policy or {})

    # -- documents --------------------------------------------------------------

    def documents(self) -> list[DocumentInfo]:
        """The documents of the store, in the order they were added.

        This is what the record says: ``add`` entries minus ``remove`` entries.
        """
        with self._mutex:
            return list(self._state().documents.values())

    def document(self, doc_id: str) -> DocumentInfo:
        """The record's entry for one document. Raises :class:`UnknownDocumentError`."""
        with self._mutex:
            info = self._state().documents.get(doc_id) if isinstance(doc_id, str) else None
            if info is None:
                raise UnknownDocumentError("the store has no document with that id")
            return info

    def find_duplicate(
        self, *, text_sha256: str | None = None, source_sha256: str | None = None
    ) -> DocumentInfo | None:
        """The document, in any collection, that has this text or came from this file."""
        with self._mutex:
            for info in self._state().documents.values():
                if text_sha256 is not None and info.text_sha256 == text_sha256:
                    return info
                if source_sha256 is not None and info.source_sha256 == source_sha256:
                    return info
            return None

    def _record(
        self,
        kind: str,
        fields: Mapping[str, Any],
        recorded: Callable[[], bool],
        *,
        time: datetime | None = None,
    ) -> None:
        """Append one record of an owner's change; the change happened if the record exists.

        The ledger also reports a record that was written but could not be
        flushed to disk. For a change of the owner that is not a failure: the
        record is there and the store goes by it. ``recorded`` tells from the
        replayed state whether it is.
        """
        try:
            self.ledger.append(kind, fields, time=time)
        except LedgerError:
            if not recorded():
                raise

    def _doc_path(self, doc_id: str) -> Path:
        return self.path / DOCS_DIR / f"{doc_id}{_SEALED_SUFFIX}"

    def _original_path(self, doc_id: str) -> Path:
        return self.path / ORIGINALS_DIR / f"{doc_id}{_SEALED_SUFFIX}"

    def _write_sealed(self, path: Path, payload: bytes, content_type: str) -> None:
        blob = vault.seal(payload, key=self._key, content_type=content_type)
        atomic_write(path, blob, overwrite=False)
        _flush_directory(path.parent)

    def _new_doc_id(self, state: LedgerState) -> str:
        while True:
            doc_id = "d-" + secrets.token_hex(6)
            taken = doc_id in state.documents or doc_id in state.removed
            if not taken and not os.path.lexists(self._doc_path(doc_id)):
                return doc_id

    def _too_many_terms(
        self,
        state: LedgerState,
        terms: Collection[str],
        tokenize: Callable[[str], Iterable[str]] | None,
    ) -> bool:
        """Whether the store would hold more distinct search terms than it may.

        The sum of the documents' own counts is an upper bound and usually
        settles it. Only when the sum passes the limit are the documents read
        and their words counted once each.
        """
        if sum(info.terms for info in state.documents.values()) + len(terms) <= MAX_STORE_TERMS:
            return False
        if tokenize is None:
            return True
        union = set(terms)
        for info in state.documents.values():
            if len(union) > MAX_STORE_TERMS:
                break
            try:
                union.update(tokenize(self.read_document(info.doc_id)["text"]))
            except StoreFormatError:
                continue  # an unavailable document is not searched, so it does not count
        return len(union) > MAX_STORE_TERMS

    def add_document(
        self,
        *,
        collection: str,
        title: str,
        text: str,
        spans: Iterable[Sequence[int]],
        terms: Collection[str],
        title_source: str = "owner",
        source: Mapping[str, Any] | None = None,
        original: bytes | None = None,
        doc_id: str | None = None,
        tokenize: Callable[[str], Iterable[str]] | None = None,
    ) -> DocumentInfo:
        """Seal one document and record it.

        The caller has done the text work: ``text`` is the cleaned text,
        ``spans`` cut it into its passages (``(start, end)`` pairs that follow
        one another and cover the text, each at most ``chunk_chars`` long) and
        ``terms`` are its distinct search terms. The store checks their form
        and the limits, seals and records; it does not clean, cut or tokenise.

        ``title`` is one line of at most 120 characters; ``title_source`` says
        whether the owner chose it (``"owner"``) or it is a file name
        (``"file-name"``). ``source`` may hold ``name``, ``path``, ``sha256``
        and ``bytes`` of the file the text came from. ``original``, the bytes
        of that file, is sealed too when given. ``doc_id`` is drawn at random
        unless given. ``tokenize`` turns a stored text into its search terms;
        it is needed only for a store so large that its distinct terms must be
        counted exactly, and such a store refuses the document without it.

        Refusals raise :class:`DocumentRefusedError`: an empty text; a text,
        source file, term count or store total over its limit; a document
        whose text or source file is already in the store, in any collection.
        Arguments of the wrong form raise :class:`ValueError` or
        :class:`TypeError`. In all these cases nothing is written.

        The order of writing: the original, the document file, then the
        ``add`` record. The document exists once the record does. If the
        record cannot be written the two files are removed again; after a
        crash in between they are files the record does not list, which every
        reader ignores.
        """
        if not isinstance(collection, str) or _NAME.fullmatch(collection) is None:
            raise ValueError(
                "a collection name is 1 to 32 characters of a-z, 0-9, _ and -, "
                "starting with a letter or digit"
            )
        _check_title(title)
        if title_source not in _TITLE_SOURCES:
            raise ValueError(f"title_source must be one of {_TITLE_SOURCES}")
        if not isinstance(text, str):
            raise TypeError("text must be str")
        if isinstance(terms, (str, bytes)) or not isinstance(terms, Collection):
            raise TypeError("terms must be a collection of the document's distinct search terms")
        if original is not None and not isinstance(original, (bytes, bytearray, memoryview)):
            raise TypeError("original must be bytes")
        original_bytes = None if original is None else bytes(original)
        if doc_id is not None and (
            not isinstance(doc_id, str) or _DOC_ID.fullmatch(doc_id) is None
        ):
            raise ValueError("a document id is d- and 12 hexadecimal characters")
        source_fields = _source_fields(source, original_bytes)
        try:
            encoded_text = text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("text holds characters that cannot be stored") from None
        if not text:
            raise DocumentRefusedError(REASON_EMPTY)
        if len(text) > MAX_TEXT_CHARS:
            raise DocumentRefusedError(
                REASON_TOO_LARGE, f"more than {MAX_TEXT_CHARS:,} characters of text"
            )
        if source_fields.get("bytes", 0) > MAX_SOURCE_BYTES:
            raise DocumentRefusedError(
                REASON_TOO_LARGE, f"a file of more than {MAX_SOURCE_BYTES // 2**20} MiB"
            )
        distinct = terms if isinstance(terms, (set, frozenset)) else set(terms)
        if not all(isinstance(term, str) for term in distinct):
            raise TypeError("terms must be a collection of the document's distinct search terms")
        if len(distinct) > MAX_DOCUMENT_TERMS:
            raise DocumentRefusedError(
                REASON_TOO_LARGE, f"more than {MAX_DOCUMENT_TERMS:,} distinct search terms"
            )
        passages = _count_passages(spans, len(text), self.chunk_chars)
        text_hash = _sha256(encoded_text)
        source_hash: str | None = source_fields.get("sha256")

        with self._mutex:
            state = self._state_for_writing()
            if doc_id is None:
                doc_id = self._new_doc_id(state)
            elif doc_id in state.documents or doc_id in state.removed:
                raise ValueError("that document id was used before; ids are never used twice")
            duplicate = self.find_duplicate(text_sha256=text_hash, source_sha256=source_hash)
            if duplicate is not None:
                raise DocumentRefusedError(REASON_DUPLICATE, duplicate_of=duplicate.doc_id)
            if sum(info.chars for info in state.documents.values()) + len(text) > MAX_STORE_CHARS:
                raise DocumentRefusedError(
                    REASON_TOO_LARGE,
                    f"the store would hold more than {MAX_STORE_CHARS:,} characters",
                )
            if self._too_many_terms(state, distinct, tokenize):
                raise DocumentRefusedError(
                    REASON_TOO_LARGE,
                    f"the store would hold more than {MAX_STORE_TERMS:,} distinct search terms",
                )

            when = self.ledger.clock()
            payload = {
                "format": DOCUMENT_TYPE,
                "doc_id": doc_id,
                "collection": collection,
                "title": title,
                "title_source": title_source,
                "source": source_fields,
                "added": format_time(when),
                "sanitizer": self.sanitizer,
                "text_sha256": text_hash,
                "terms": len(distinct),
                "text": text,
            }
            encoded = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            head_before = state.head
            written: list[Path] = []
            try:
                if original_bytes is not None:
                    self._write_sealed(self._original_path(doc_id), original_bytes, ORIGINAL_TYPE)
                    written.append(self._original_path(doc_id))
                self._write_sealed(self._doc_path(doc_id), encoded, DOCUMENT_TYPE)
                written.append(self._doc_path(doc_id))
                self._record(
                    "add",
                    {
                        "doc_id": doc_id,
                        "collection": collection,
                        "title": title,
                        "title_source": title_source,
                        "text_sha256": text_hash,
                        "source_sha256": source_hash,
                        "chars": len(text),
                        "terms": len(distinct),
                        "passages": passages,
                        "original": original_bytes is not None,
                    },
                    lambda: doc_id in state.documents,
                    time=when,
                )
            except BaseException:
                if state.head == head_before:  # no record: take back the files written for it
                    for path in written:
                        with contextlib.suppress(OSError):
                            _delete(path)
                raise
            return state.documents[doc_id]

    def remove_document(self, doc_id: str) -> None:
        """Take a document out of the store: one ``remove`` record, then its files are deleted.

        The record comes first, so a crash in between leaves files that the
        record no longer lists and every reader ignores. When the record cannot
        be written nothing changes. Deleting is best effort after that: a file
        that could not be deleted stays behind unlisted (see
        :meth:`unlisted_files`) and this method still returns. Files are
        deleted, not shredded; backups and the record keep what they have.
        """
        with self._mutex:
            state = self._state_for_writing()
            if not isinstance(doc_id, str) or doc_id not in state.documents:
                raise UnknownDocumentError("the store has no document with that id")
            self._record("remove", {"doc_id": doc_id}, lambda: doc_id not in state.documents)
            for path in (self._doc_path(doc_id), self._original_path(doc_id)):
                with contextlib.suppress(OSError):
                    _delete(path)
                _flush_directory(path.parent)

    def read_document(self, doc_id: str) -> dict[str, Any]:
        """Unseal one document and return its payload: metadata and ``text``.

        Raises :class:`UnknownDocumentError` for an id the record does not
        list, also when a file of that name lies in ``docs/`` (a removed
        document that was put back is not a document). Raises
        :class:`StoreFormatError` when the listed document is unavailable: its
        file is missing, does not authenticate, is another kind of sealed file,
        holds another document, or holds text other than what was added.
        """
        with self._mutex:
            info = self.document(doc_id)
            try:
                blob = self._doc_path(doc_id).read_bytes()
            except OSError:
                raise StoreFormatError(
                    f"document {doc_id} is listed in the record but its file cannot be read"
                ) from None
            try:
                payload, header = vault.unseal(blob, key=self._key)
            except WDVAError as exc:
                raise StoreFormatError(
                    f"the file of document {doc_id} does not authenticate ({type(exc).__name__})"
                ) from None
            if header.content_type != DOCUMENT_TYPE:
                raise StoreFormatError(
                    f"the file of document {doc_id} is another kind of sealed file"
                )
            try:
                document = strict_json_loads(payload)
            except ValueError:
                raise StoreFormatError(f"the file of document {doc_id} is malformed") from None
            if not isinstance(document, dict) or document.get("format") != DOCUMENT_TYPE:
                raise StoreFormatError(f"the file of document {doc_id} is malformed")
            if document.get("doc_id") != doc_id:
                raise StoreFormatError(f"the file of document {doc_id} holds another document")
            text = document.get("text")
            if (
                not isinstance(text, str)
                or not _storable(text)
                or _sha256(text.encode("utf-8")) != info.text_sha256
                or document.get("text_sha256") != info.text_sha256
            ):
                raise StoreFormatError(
                    f"the file of document {doc_id} does not hold the text that was added"
                )
            if document.get("collection") != info.collection or document.get("title") != info.title:
                raise StoreFormatError(
                    f"the file of document {doc_id} does not match its entry in the record"
                )
            return document

    def read_original(self, doc_id: str) -> bytes:
        """Unseal the original file of one document and return its bytes.

        Raises :class:`UnknownDocumentError` when the document is not listed or
        no original was kept for it, and :class:`StoreFormatError` when the
        file is missing or is not the file that was added.
        """
        with self._mutex:
            info = self.document(doc_id)
            if not info.original:
                raise UnknownDocumentError("no original file was kept for that document")
            try:
                blob = self._original_path(doc_id).read_bytes()
            except OSError:
                raise StoreFormatError(
                    f"the original of document {doc_id} is listed but its file cannot be read"
                ) from None
            try:
                payload, header = vault.unseal(blob, key=self._key)
            except WDVAError as exc:
                raise StoreFormatError(
                    f"the original of document {doc_id} does not authenticate "
                    f"({type(exc).__name__})"
                ) from None
            if header.content_type != ORIGINAL_TYPE:
                raise StoreFormatError(
                    f"the original of document {doc_id} is another kind of sealed file"
                )
            if _sha256(payload) != info.source_sha256:
                raise StoreFormatError(
                    f"the original of document {doc_id} is not the file that was added"
                )
            return payload

    def unlisted_files(self) -> list[Path]:
        """Files in ``docs/`` and ``originals/`` that belong to no listed document.

        They are ignored by every reader. They are what a removed document
        leaves when its files could not be deleted, what an interrupted add or
        remove leaves, and what someone put there.
        """
        with self._mutex:
            documents = self._state().documents
            listed = {
                DOCS_DIR: {f"{doc_id}{_SEALED_SUFFIX}" for doc_id in documents},
                ORIGINALS_DIR: {
                    f"{doc_id}{_SEALED_SUFFIX}"
                    for doc_id, info in documents.items()
                    if info.original
                },
            }
            found: list[Path] = []
            for folder, names in listed.items():
                with contextlib.suppress(FileNotFoundError), os.scandir(self.path / folder) as it:
                    found.extend(Path(entry.path) for entry in it if entry.name not in names)
            return sorted(found)
