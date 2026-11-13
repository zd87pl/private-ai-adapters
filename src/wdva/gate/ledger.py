# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The record of a store: one sealed file per entry, each naming the hash of the one before.

Every release, refusal, approval, policy change, document change, unlock and
lock is one file ``ledger/<10-digit number>.wdva``, sealed under the store key.
Inside is one JSON object with ``v``, ``seq``, ``prev``, ``time``, ``kind`` and
the fields of its kind. ``prev`` is the SHA-256 of the complete bytes of the
file before it, so the files form a chain that starts at a value derived from
the store's id.

What the chain gives, and what it does not:

* A record that was changed, swapped with another, copied over another or
  taken out of the middle, by anyone without the key, fails verification at
  that record.
* Removing the newest records is **not** noticed from the files. Whoever can
  delete files can return the store to an earlier state. It shows only against
  a head (a number and the start of a hash, such as ``1237:9f2c7a1e``) that
  the owner noted earlier.
* Records written by someone who holds the key are not told apart.

The record is also where the limits live. :meth:`Ledger.replay` verifies the
whole chain and rebuilds from the records alone everything a limit is computed
from (:class:`LedgerState`): what was released to each client and each pool
and when, which parts of each document have been released, the policy, the
list of documents. A restart therefore resets nothing.

Writing fails closed. :meth:`Ledger.append` returns only after the record is a
complete file under its final name, and raises :class:`LedgerError` otherwise;
a caller that releases text only after ``append`` returned can never have
released something that is not recorded.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import threading
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

from wdva._util import atomic_write, strict_json_loads
from wdva.errors import WDVAError
from wdva.gate.errors import LedgerError
from wdva.keys import SecretKey
from wdva.vault import seal, unseal

__all__ = [
    "HEAD_HASH_CHARS",
    "KINDS",
    "LEDGER_TYPE",
    "MAX_QUERY_CHARS",
    "MAX_RELEASE_CHARS",
    "POOLS",
    "RECORD_VERSION",
    "SEARCH_TOOL",
    "SESSION_CLEAN",
    "SESSION_FIRST",
    "SESSION_UNCLEAN",
    "ClientState",
    "DocumentInfo",
    "Head",
    "Ledger",
    "LedgerState",
    "Record",
    "Span",
    "Window",
    "format_time",
    "genesis_hash",
    "head_without_key",
    "parse_time",
    "record_name",
    "utc_now",
]

LEDGER_TYPE = "application/vnd.wdva.gate.ledger.v1"
"""The content type of a sealed record. Checked after unsealing."""

RECORD_VERSION = 1

KINDS = (
    "init",
    "policy",
    "add",
    "remove",
    "unlock",
    "lock",
    "release",
    "denial",
    "status",
    "approval",
)
"""Every kind of record this version writes and reads."""

SEARCH_TOOL = "wdva_search"
"""The ``tool`` of a release that counts as one search."""

MAX_QUERY_CHARS = 500
"""The longest ``query`` a release or denial record may carry."""

MAX_RELEASE_CHARS = 8_000
"""The longest text one release record may hold. No result is ever longer."""

POOLS = ("hosted", "local")
"""The two pools a release is counted in, by where the client's model runs."""

HEAD_HASH_CHARS = 8
"""How many hex characters of a record's hash are shown in a head."""

SESSION_FIRST = "first"
SESSION_CLEAN = "clean"
SESSION_UNCLEAN = "unclean"

Span = tuple[int, int]
"""A half-open range ``(start, end)`` of character offsets into a document's text."""

_GENESIS = b"wdva-gate/v1/ledger-genesis\x00"
_RECORD_NAME = re.compile(r"([0-9]{10})\.wdva")
_TIME = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_STORE_ID = re.compile(r"[0-9a-f]{16}")
_DOC_ID = re.compile(r"d-[0-9a-f]{12}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_RESERVED = ("v", "seq", "prev", "time", "kind")
_MAX_SEQ = 10**10 - 1
_UNSUPPORTED_FLUSH = frozenset({errno.EINVAL, errno.ENOTSUP, errno.ENOSYS, errno.EBADF})
_STALE = (
    "the record files no longer match what this session wrote and verified; "
    "nothing more is recorded or released until the store is locked and unlocked again"
)


# ---------------------------------------------------------------------------
# Small values
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """The current time in UTC. The default clock of a ledger."""
    return datetime.now(timezone.utc)


def format_time(when: datetime) -> str:
    """A time as records carry it: UTC, whole seconds, ``2026-11-12T17:03:22Z``."""
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("a record time needs a time zone")
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    """Read a time written by :func:`format_time`. Anything else raises :class:`ValueError`."""
    match = _TIME.fullmatch(text)
    if match is None:
        raise ValueError("not a record time (expected the form 2026-11-12T17:03:22Z)")
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def record_name(seq: int) -> str:
    """The file name of record ``seq``: ten digits and ``.wdva``."""
    return f"{seq:010d}.wdva"


def genesis_hash(store_id: str) -> str:
    """The ``prev`` value of a store's first record."""
    return hashlib.sha256(_GENESIS + store_id.encode("utf-8")).hexdigest()


class Head(NamedTuple):
    """The newest record: its number and the SHA-256 (hex) of its file.

    ``str(head)`` is the short form an owner can note and compare later, for
    example ``1237:9f2c7a1e``.
    """

    seq: int
    sha256: str

    @property
    def short(self) -> str:
        """The first characters of the hash, as shown to the owner."""
        return self.sha256[:HEAD_HASH_CHARS]

    def __str__(self) -> str:
        return f"{self.seq}:{self.sha256[:HEAD_HASH_CHARS]}"


@dataclass(frozen=True)
class Record:
    """One verified entry of the record.

    ``data`` is the complete JSON object as it is sealed in the file, ``v``,
    ``seq``, ``prev``, ``time`` and ``kind`` included. Treat it as read-only.
    ``sha256`` is the hash of the record's file, which the next record carries
    as its ``prev``.
    """

    seq: int
    kind: str
    time: datetime
    data: Mapping[str, Any] = field(repr=False)
    sha256: str

    @property
    def prev(self) -> str:
        """The hash of the file before this one."""
        return str(self.data["prev"])

    @property
    def head(self) -> Head:
        """This record as a head."""
        return Head(self.seq, self.sha256)


@dataclass(frozen=True)
class DocumentInfo:
    """What the record says about one document: the fields of its ``add`` entry."""

    doc_id: str
    collection: str
    title: str
    title_source: str
    chars: int
    terms: int
    passages: int
    text_sha256: str
    source_sha256: str | None
    original: bool
    added: datetime
    seq: int


class Window:
    """Times and sizes of releases in the order they were recorded.

    Answers how much falls inside a period that ends now. A release counts
    while its age is less than the period, and a release dated later than now
    counts in every period, so setting the clock back frees nothing.
    """

    __slots__ = ("_chars", "_ordered", "_times", "_totals")

    def __init__(self) -> None:
        self._times: list[datetime] = []
        self._chars: list[int] = []
        self._totals: list[int] = [0]
        self._ordered = True

    def _add(self, when: datetime, chars: int) -> None:
        if self._times and when < self._times[-1]:
            self._ordered = False
        self._times.append(when)
        self._chars.append(chars)
        self._totals.append(self._totals[-1] + chars)

    def _first_inside(self, now: datetime, period: timedelta) -> int | None:
        """Index of the first entry inside the period, when the times never go back."""
        return bisect_right(self._times, now - period) if self._ordered else None

    def used(self, now: datetime, period: timedelta) -> int:
        """The characters released less than ``period`` before ``now``, or dated after it."""
        first = self._first_inside(now, period)
        if first is not None:
            return self._totals[-1] - self._totals[first]
        earliest = now - period
        return sum(
            chars for when, chars in zip(self._times, self._chars, strict=True) if when > earliest
        )

    def count(self, now: datetime, period: timedelta) -> int:
        """The number of releases less than ``period`` before ``now``, or dated after it."""
        first = self._first_inside(now, period)
        if first is not None:
            return len(self._times) - first
        earliest = now - period
        return sum(1 for when in self._times if when > earliest)

    @property
    def total(self) -> int:
        """All characters ever recorded here."""
        return self._totals[-1]

    def entries(self) -> list[tuple[datetime, int]]:
        """Every ``(time, characters)`` pair, as a new list in recorded order."""
        return list(zip(self._times, self._chars, strict=True))

    def __len__(self) -> int:
        return len(self._times)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Window):
            return NotImplemented
        return self._times == other._times and self._chars == other._chars

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Window({len(self._times)} releases, {self._totals[-1]} characters)"


@dataclass
class ClientState:
    """What the record holds about one client name."""

    release_seq: int = 0
    """The client's own release counter: the largest ``client_seq`` recorded for it."""
    releases: Window = field(default_factory=Window)
    """Every release to this name."""
    searches: Window = field(default_factory=Window)
    """The releases that were searches."""
    last_seen: datetime | None = None
    """The time of the newest release, denial or status record under this name."""
    last_claimed: dict[str, Any] | None = None
    """What a calling program last said about itself under this name. Never verified."""


@dataclass
class LedgerState:
    """Everything that is rebuilt from the records, as of the record ``head``.

    The object belongs to its :class:`Ledger`, which brings it up to date with
    every record it appends. Read it; never change it. Ask the ledger for it
    (:meth:`Ledger.replay` costs nothing after the first call) instead of
    keeping parts of it for later.
    """

    store_id: str
    head: Head
    policy: dict[str, Any] | None = None
    """The policy object of the newest ``policy`` record. A reference: copy it before changing."""
    policy_seq: int = 0
    """The number of that record."""
    documents: dict[str, DocumentInfo] = field(default_factory=dict)
    """The documents of the store: ``add`` records minus ``remove`` records, in order of adding."""
    removed: dict[str, DocumentInfo] = field(default_factory=dict)
    """Documents that were removed. Their ids are never used again."""
    clients: dict[str, ClientState] = field(default_factory=dict)
    pools: dict[str, Window] = field(default_factory=dict)
    """Per pool, every release as it was recorded then, whatever the policy says now."""
    spans: dict[tuple[str, str], list[Span]] = field(default_factory=dict)
    """Per ``(pool, doc_id)``: the parts of the document released so far, merged and sorted."""
    denials: dict[tuple[str, str], datetime] = field(default_factory=dict)
    """Per ``(client, code)``: the latest time of a ``denial`` record."""
    statuses: dict[str, datetime] = field(default_factory=dict)
    """Per client: the latest time of a ``status`` record."""
    unlocks: int = 0
    session_open: bool = False
    """Whether the newest ``unlock`` record has no ``lock`` record after it."""
    last_unlock: datetime | None = None
    last_lock: datetime | None = None
    counts: dict[str, int] = field(default_factory=dict)
    """How many records of each kind there are."""

    @property
    def policy_serial(self) -> int:
        """The serial of the policy in force, 0 when there is none."""
        return int(self.policy["serial"]) if self.policy is not None else 0

    @property
    def previous_session(self) -> str:
        """How the most recent session ended, judged from the records there are now.

        ``"first"`` when the store was never unlocked, ``"clean"`` when the
        newest ``unlock`` is followed by a ``lock``, ``"unclean"`` when it is
        not (the program ended without locking). Ask before appending the
        ``unlock`` record of a new session: after that the answer is about the
        session that has just begun.
        """
        if self.unlocks == 0:
            return SESSION_FIRST
        return SESSION_UNCLEAN if self.session_open else SESSION_CLEAN

    def client(self, name: str) -> ClientState:
        """The state of one client name; an empty one for a name the record does not know."""
        return self.clients.get(name) or ClientState()

    def pool(self, name: str) -> Window:
        """The releases to one pool; an empty window for a pool without any."""
        return self.pools.get(name) or Window()

    def released_spans(self, pool: str, doc_id: str) -> list[Span]:
        """The parts of a document released to a pool so far: sorted, separate, a new list."""
        return list(self.spans.get((pool, doc_id), ()))

    def released_chars(self, pool: str, doc_id: str) -> int:
        """How many different characters of a document were released to a pool so far."""
        return sum(end - start for start, end in self.spans.get((pool, doc_id), ()))


# ---------------------------------------------------------------------------
# What a record of each kind must hold, and what it changes
# ---------------------------------------------------------------------------


class _MalformedError(ValueError):
    """A record does not have the form of its kind. The message never quotes a field's value."""


def _text(data: Mapping[str, Any], name: str, *, high: int, low: int = 1) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not low <= len(value) <= high:
        raise _MalformedError(f"{name} must be text of {low} to {high} characters")
    return value


def _whole(data: Mapping[str, Any], name: str, *, low: int = 0) -> int:
    value = data.get(name)
    if type(value) is not int or value < low:
        raise _MalformedError(f"{name} must be a whole number of at least {low}")
    return value


def _hash(data: Mapping[str, Any], name: str, *, optional: bool = False) -> str | None:
    value = data.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise _MalformedError(f"{name} must be 64 hexadecimal characters")
    return value


def _query(data: Mapping[str, Any]) -> None:
    value = data.get("query")
    if value is not None and (not isinstance(value, str) or len(value) > MAX_QUERY_CHARS):
        raise _MalformedError(f"query must be text of at most {MAX_QUERY_CHARS} characters")


def _session_figures(state: LedgerState, data: Mapping[str, Any]) -> None:
    if state.policy is None:
        raise _MalformedError("there is no policy record yet")
    if _whole(data, "policy_seq", low=1) != state.policy_seq:
        raise _MalformedError("policy_seq is not the number of the newest policy record")
    if _whole(data, "documents") != len(state.documents):
        raise _MalformedError("documents is not the number of documents in the store")


def _check_init(state: LedgerState, data: Mapping[str, Any]) -> None:
    if data.get("store_id") != state.store_id:
        raise _MalformedError("store_id is not the id of this store")


def _check_policy(state: LedgerState, data: Mapping[str, Any]) -> None:
    serial = _whole(data, "serial", low=1)
    policy = data.get("policy")
    if not isinstance(policy, dict):
        raise _MalformedError("policy must be an object")
    if type(policy.get("serial")) is not int or policy["serial"] != serial:
        raise _MalformedError("serial and the serial inside the policy differ")
    if serial <= state.policy_serial:
        raise _MalformedError("serial must be larger than the serial of the policy in force")
    _text(data, "by", high=64)


def _check_add(state: LedgerState, data: Mapping[str, Any]) -> None:
    doc_id = data.get("doc_id")
    if not isinstance(doc_id, str) or _DOC_ID.fullmatch(doc_id) is None:
        raise _MalformedError("doc_id must be d- and 12 hexadecimal characters")
    if doc_id in state.documents or doc_id in state.removed:
        raise _MalformedError("doc_id was used before")
    collection = data.get("collection")
    if not isinstance(collection, str) or _NAME.fullmatch(collection) is None:
        raise _MalformedError("collection is not a name")
    _text(data, "title", high=120)
    _text(data, "title_source", high=32)
    _hash(data, "text_sha256")
    _hash(data, "source_sha256", optional=True)
    _whole(data, "chars", low=1)
    _whole(data, "terms")
    _whole(data, "passages", low=1)
    if type(data.get("original")) is not bool:
        raise _MalformedError("original must be true or false")


def _check_remove(state: LedgerState, data: Mapping[str, Any]) -> None:
    if data.get("doc_id") not in state.documents:
        raise _MalformedError("doc_id is not a document of the store")


def _check_unlock(state: LedgerState, data: Mapping[str, Any]) -> None:
    served = data.get("served")
    if not isinstance(served, list) or not all(isinstance(name, str) for name in served):
        raise _MalformedError("served must be a list of names")
    _session_figures(state, data)


def _check_lock(state: LedgerState, data: Mapping[str, Any]) -> None:
    _text(data, "reason", high=64)
    _session_figures(state, data)


def _check_release(state: LedgerState, data: Mapping[str, Any]) -> None:
    client = _text(data, "client", high=64)
    if _whole(data, "client_seq", low=1) <= state.client(client).release_seq:
        raise _MalformedError("client_seq must be larger than the client's last release number")
    if data.get("pool") not in POOLS:
        raise _MalformedError(f"pool must be one of {POOLS}")
    _text(data, "tool", high=64)
    text = data.get("text")
    if not isinstance(text, str):
        raise _MalformedError("text must be the released text")
    if len(text) > MAX_RELEASE_CHARS:
        raise _MalformedError(f"text is longer than {MAX_RELEASE_CHARS} characters")
    if _whole(data, "chars") != len(text):
        raise _MalformedError("chars is not the length of text")
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise _MalformedError("text cannot be encoded as UTF-8") from None
    if data.get("sha256") != digest:
        raise _MalformedError("sha256 is not the hash of text")
    spans = data.get("spans")
    if not isinstance(spans, list):
        raise _MalformedError("spans must be a list, empty when no document text was released")
    for span in spans:
        if not isinstance(span, list) or len(span) != 3:
            raise _MalformedError("a span is a document id, a start and an end")
        doc_id, start, end = span
        info = state.documents.get(doc_id) if isinstance(doc_id, str) else None
        if info is None:
            raise _MalformedError("a span names a document that is not in the store")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= info.chars:
            raise _MalformedError("a span lies outside its document")
    _query(data)


def _check_denial(state: LedgerState, data: Mapping[str, Any]) -> None:
    _text(data, "client", high=64)
    _text(data, "code", high=32)
    if "folded_before" in data:
        _whole(data, "folded_before")
    _query(data)


def _check_status(state: LedgerState, data: Mapping[str, Any]) -> None:
    _text(data, "client", high=64)


def _check_approval(state: LedgerState, data: Mapping[str, Any]) -> None:
    _text(data, "client", high=64)
    _text(data, "ref", high=64)
    _hash(data, "body_sha256")


_CHECKS: dict[str, Callable[[LedgerState, Mapping[str, Any]], None]] = {
    "init": _check_init,
    "policy": _check_policy,
    "add": _check_add,
    "remove": _check_remove,
    "unlock": _check_unlock,
    "lock": _check_lock,
    "release": _check_release,
    "denial": _check_denial,
    "status": _check_status,
    "approval": _check_approval,
}


def _check(state: LedgerState, data: Mapping[str, Any]) -> None:
    """Refuse a record that does not have the form of its kind, given the records before it."""
    kind = data.get("kind")
    checker = _CHECKS.get(kind) if isinstance(kind, str) else None
    if checker is None:
        raise _MalformedError("its kind is not one this version knows")
    if (data.get("seq") == 1) != (kind == "init"):
        raise _MalformedError("the first record, and only the first, is the init record")
    checker(state, data)


def _add_span(spans: list[Span], start: int, end: int) -> None:
    """Add ``[start, end)`` to a sorted list of separate spans, joining what overlaps or touches."""
    first = bisect_left(spans, (start, start))
    if first > 0 and spans[first - 1][1] >= start:
        first -= 1
    last = first
    while last < len(spans) and spans[last][0] <= end:
        start = min(start, spans[last][0])
        end = max(end, spans[last][1])
        last += 1
    spans[first:last] = [(start, end)]


def _seen(state: LedgerState, record: Record) -> ClientState:
    """Note that a client name appears in ``record`` and return its state."""
    name = record.data["client"]
    client = state.clients.get(name)
    if client is None:
        client = state.clients[name] = ClientState()
    if client.last_seen is None or record.time >= client.last_seen:
        client.last_seen = record.time
        claimed = record.data.get("claimed")
        if isinstance(claimed, dict):
            client.last_claimed = dict(claimed)
    return client


def _apply(state: LedgerState, record: Record) -> None:
    """Bring ``state`` up to date with one checked record. Must not fail."""
    data, kind, when = record.data, record.kind, record.time
    if kind == "policy":
        state.policy = copy.deepcopy(data["policy"])
        state.policy_seq = record.seq
    elif kind == "add":
        state.documents[data["doc_id"]] = DocumentInfo(
            doc_id=data["doc_id"],
            collection=data["collection"],
            title=data["title"],
            title_source=data["title_source"],
            chars=data["chars"],
            terms=data["terms"],
            passages=data["passages"],
            text_sha256=data["text_sha256"],
            source_sha256=data.get("source_sha256"),
            original=data["original"],
            added=when,
            seq=record.seq,
        )
    elif kind == "remove":
        state.removed[data["doc_id"]] = state.documents.pop(data["doc_id"])
    elif kind == "unlock":
        state.unlocks += 1
        state.session_open = True
        state.last_unlock = when
    elif kind == "lock":
        state.session_open = False
        state.last_lock = when
    elif kind == "release":
        client = _seen(state, record)
        client.release_seq = data["client_seq"]
        client.releases._add(when, data["chars"])
        if data["tool"] == SEARCH_TOOL:
            client.searches._add(when, data["chars"])
        pool = state.pools.get(data["pool"])
        if pool is None:
            pool = state.pools[data["pool"]] = Window()
        pool._add(when, data["chars"])
        for doc_id, start, end in data["spans"]:
            _add_span(state.spans.setdefault((data["pool"], doc_id), []), start, end)
    elif kind == "denial":
        _seen(state, record)
        key = (data["client"], data["code"])
        if key not in state.denials or when > state.denials[key]:
            state.denials[key] = when
    elif kind == "status":
        _seen(state, record)
        if data["client"] not in state.statuses or when > state.statuses[data["client"]]:
            state.statuses[data["client"]] = when
    state.counts[kind] = state.counts.get(kind, 0) + 1
    state.head = record.head


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _record_numbers(directory: Path) -> list[int]:
    """The numbers of the record files in ``directory``, sorted.

    Only regular files named with ten digits and ``.wdva`` count. Temporary
    files (their names begin with a dot), directories and links are not records.
    """
    numbers: list[int] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                match = _RECORD_NAME.fullmatch(entry.name)
                if match is not None and entry.is_file(follow_symlinks=False):
                    numbers.append(int(match.group(1)))
    except FileNotFoundError:
        return []
    numbers.sort()
    return numbers


def head_without_key(directory: str | os.PathLike[str]) -> Head | None:
    """The head of the record files in ``directory``, read without the store key.

    The number is the highest one among the file names and the hash is that of
    the file's bytes; nothing is verified. ``None`` when there is no record.
    This is what can be compared with a head the owner noted while the store
    is locked.
    """
    folder = Path(directory)
    numbers = _record_numbers(folder)
    if not numbers:
        return None
    try:
        blob = (folder / record_name(numbers[-1])).read_bytes()
    except OSError:
        return None
    return Head(numbers[-1], hashlib.sha256(blob).hexdigest())


def _flush_directory(directory: Path) -> None:
    """Ask the operating system to write a directory's entries to disk.

    Not every file system can do this for a directory; one that says so is
    left alone. Any other failure is raised.
    """
    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_FLUSH:
            raise
    finally:
        os.close(descriptor)


def _on_disk(path: Path, blob: bytes) -> bool | None:
    """Whether ``path`` holds exactly ``blob``: yes, no file there, or (``None``) something else."""
    try:
        found = path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True if found == blob else None


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class Ledger:
    """The record of one store, opened with the store key.

    A store builds this; other code reaches it as ``store.ledger``. All
    methods may be called from several threads. Whatever goes wrong raises
    :class:`~wdva.gate.errors.LedgerError`, whose ``seq`` names the record
    concerned when there is one.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        key: SecretKey,
        store_id: str,
        *,
        writable: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if _STORE_ID.fullmatch(store_id) is None:
            raise ValueError("a store id is 16 hexadecimal characters")
        self.directory = Path(directory)
        self.store_id = store_id
        self.writable = writable
        self.clock: Callable[[], datetime] = clock or utc_now
        self._key: SecretKey | None = key
        self._lock = threading.RLock()
        self._state: LedgerState | None = None
        self._broken: str | None = None

    @classmethod
    def create(
        cls,
        directory: str | os.PathLike[str],
        key: SecretKey,
        store_id: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> Ledger:
        """Begin the record of a new store by writing its first entry, of kind ``init``."""
        folder = Path(directory)
        folder.mkdir(mode=0o700, exist_ok=True)
        if _record_numbers(folder):
            raise LedgerError(f"{folder} already holds records")
        ledger = cls(folder, key, store_id, clock=clock)
        ledger._state = LedgerState(store_id=store_id, head=Head(0, genesis_hash(store_id)))
        ledger.append("init", {"store_id": store_id})
        return ledger

    # -- reading ------------------------------------------------------------

    def _key_in_use(self) -> SecretKey:
        if self._key is None:
            raise LedgerError("the store is closed")
        return self._key

    def _numbers(self) -> list[int]:
        """The record numbers, after checking that they run from 1 without a gap."""
        numbers = _record_numbers(self.directory)
        if not numbers:
            raise LedgerError("the record is empty: record 1 is missing", seq=1)
        for expected, seq in enumerate(numbers, start=1):
            if seq != expected:
                raise LedgerError(f"record {expected} is missing", seq=expected)
        return numbers

    def _load(self, seq: int) -> Record:
        """Read, authenticate and parse one record file. The chain is not looked at."""
        key = self._key_in_use()
        try:
            blob = (self.directory / record_name(seq)).read_bytes()
        except FileNotFoundError:
            raise LedgerError(f"there is no record {seq}", seq=seq) from None
        except OSError as exc:
            raise LedgerError(f"record {seq} cannot be read", seq=seq) from exc
        try:
            payload, header = unseal(blob, key=key)
        except WDVAError as exc:
            raise LedgerError(
                f"record {seq} does not authenticate: it was changed, or it is not "
                f"a record of this store ({type(exc).__name__})",
                seq=seq,
            ) from None
        if header.content_type != LEDGER_TYPE:
            raise LedgerError(f"record {seq} is another kind of sealed file", seq=seq)
        try:
            data = strict_json_loads(payload)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            if type(data.get("v")) is not int or data["v"] != RECORD_VERSION:
                raise ValueError("unknown version")
            prev = data.get("prev")
            if not isinstance(prev, str) or _HEX64.fullmatch(prev) is None:
                raise ValueError("no previous hash")
            kind = data.get("kind")
            if not isinstance(kind, str):
                raise ValueError("no kind")
            stamp = data.get("time")
            if not isinstance(stamp, str):
                raise ValueError("no time")
            when = parse_time(stamp)
        except ValueError as exc:
            raise LedgerError(f"record {seq} is malformed ({exc})", seq=seq) from None
        if type(data.get("seq")) is not int or data["seq"] != seq:
            raise LedgerError(
                f"record {seq} holds another record's content: it was moved or copied", seq=seq
            )
        return Record(seq, kind, when, data, hashlib.sha256(blob).hexdigest())

    def _walk(self) -> LedgerState:
        """Verify every record in order and rebuild the state from them."""
        numbers = self._numbers()
        state = LedgerState(store_id=self.store_id, head=Head(0, genesis_hash(self.store_id)))
        for seq in numbers:
            record = self._load(seq)
            if record.prev != state.head.sha256:
                what = f"follow record {seq - 1}" if seq > 1 else "begin the record of this store"
                raise LedgerError(
                    f"record {seq} does not {what}: the chain is broken here", seq=seq
                )
            try:
                _check(state, record.data)
            except _MalformedError as exc:
                raise LedgerError(f"record {seq} is malformed: {exc}", seq=seq) from None
            _apply(state, record)
        return state

    def _usable(self) -> None:
        self._key_in_use()
        if self._broken is not None:
            raise LedgerError(self._broken)

    def replay(self) -> LedgerState:
        """The state rebuilt from the records, after verifying the whole chain.

        The first call reads and verifies every record: each file authenticates
        under the store key as a record, the numbers run from 1 without a gap,
        each record holds its own number and the hash of the file before it.
        The first record that fails raises :class:`LedgerError` with its number
        as ``seq``. Later calls return the same object at no cost; the ledger
        applies every record it appends to it, so it always equals what a fresh
        replay of the files would give.
        """
        with self._lock:
            self._usable()
            if self._state is None:
                self._state = self._walk()
            return self._state

    def verify(self) -> Head:
        """Read and verify every record again, now, and return the head.

        Unlike :meth:`replay` this never answers from memory. If the files no
        longer end where this session's records end (records were removed or
        added behind its back), that is an error too, and the ledger refuses
        further use until the store is opened again.
        """
        with self._lock:
            self._usable()
            fresh = self._walk()
            if self._state is None:
                self._state = fresh
            elif fresh.head != self._state.head:
                self._broken = _STALE
                raise LedgerError(_STALE, seq=min(fresh.head.seq, self._state.head.seq) + 1)
            return fresh.head

    def head(self) -> Head:
        """The newest record. Verified when :meth:`replay` ran; else read from the files."""
        with self._lock:
            self._usable()
            if self._state is not None:
                return self._state.head
            seq = self._numbers()[-1]
            return self._load(seq).head

    def read(self, seq: int) -> Record:
        """One record, authenticated under the store key. The chain is not checked again."""
        with self._lock:
            self._usable()
            if type(seq) is not int or not 1 <= seq <= _MAX_SEQ:
                raise LedgerError("there is no record with that number")
            return self._load(seq)

    def records(
        self, *, first: int = 1, last: int | None = None, newest_first: bool = False
    ) -> Iterator[Record]:
        """Iterate over the records numbered ``first`` to ``last`` (both included).

        ``last=None`` means the newest. Each record is read and authenticated
        when the iteration reaches it.
        """
        newest = self.head().seq
        low = max(1, first)
        high = newest if last is None else min(last, newest)
        numbers = range(high, low - 1, -1) if newest_first else range(low, high + 1)
        for seq in numbers:
            yield self.read(seq)

    def current_policy(self) -> tuple[dict[str, Any], int]:
        """The policy in force and the number of its record, without a full replay.

        When :meth:`replay` already ran, the answer comes from it. Otherwise
        the records are read from the newest backwards until a ``policy``
        record is found, or a record that names one in ``policy_seq`` (every
        ``unlock`` and ``lock`` record does), which bounds the walk to one
        session. The records read are authenticated; the chain is not.
        """
        with self._lock:
            self._usable()
            if self._state is not None:
                if self._state.policy is None:
                    raise LedgerError("the record holds no policy", seq=2)
                return copy.deepcopy(self._state.policy), self._state.policy_seq
            for seq in reversed(self._numbers()):
                record = self._load(seq)
                if record.kind != "policy" and "policy_seq" in record.data:
                    named = record.data["policy_seq"]
                    if type(named) is not int or not 1 <= named < seq:
                        raise LedgerError(
                            f"record {seq} is malformed: it names no earlier policy record", seq=seq
                        )
                    record = self._load(named)
                    if record.kind != "policy":
                        raise LedgerError(f"record {named} is not a policy record", seq=named)
                if record.kind == "policy":
                    policy = record.data.get("policy")
                    if not isinstance(policy, dict):
                        raise LedgerError(
                            f"record {record.seq} is malformed: no policy", seq=record.seq
                        )
                    return copy.deepcopy(policy), record.seq
            raise LedgerError("the record holds no policy", seq=2)

    # -- writing ------------------------------------------------------------

    def _compose(
        self, state: LedgerState, seq: int, kind: str, fields: Mapping[str, Any], when: datetime
    ) -> tuple[bytes, dict[str, Any], datetime]:
        """The bytes of a new record, the record as it will be read back, and its time."""
        if not isinstance(kind, str):
            raise _MalformedError("its kind is not one this version knows")
        if seq > _MAX_SEQ:
            raise _MalformedError("the record is full")
        entry: dict[str, Any] = {
            "v": RECORD_VERSION,
            "seq": seq,
            "prev": state.head.sha256,
            "time": format_time(when),
            "kind": kind,
        }
        for name in fields:
            if name in _RESERVED:
                raise _MalformedError(f"{name} is filled in by the ledger")
        entry.update(fields)
        if kind in ("unlock", "lock"):
            entry.setdefault("policy_seq", state.policy_seq)
            entry.setdefault("documents", len(state.documents))
        encoded = json.dumps(
            entry, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")
        # What is checked, recorded in memory and handed back is the record as
        # a reader will parse it, so a record that was written can be read.
        data = strict_json_loads(encoded)
        _check(state, data)
        return encoded, data, parse_time(data["time"])

    def append(
        self, kind: str, fields: Mapping[str, Any], *, time: datetime | None = None
    ) -> Record:
        """Write the next record and return it.

        ``fields`` are the fields of the record's kind; ``v``, ``seq``,
        ``prev``, ``time`` and ``kind`` are filled in here, and so are
        ``policy_seq`` and ``documents`` of an ``unlock`` or ``lock`` record
        that leaves them out. ``time`` defaults to the ledger's clock.

        The order: the whole chain is verified if it was not yet (a damaged
        record means nothing is appended); the record is checked against the
        form of its kind; it is sealed and written to a temporary file, flushed,
        and given its final name with an exclusive create, so two writers can
        never both take one number; then the state of :meth:`replay` is brought
        up to date and the directory is flushed.

        Raises :class:`LedgerError`, and nothing else, when the record was
        not written, and also when it was written but the write or the flush
        reported a failure. In the first case nothing changed: no file, no
        counter. In the second the record exists and counts. Either way the
        caller must treat the call as failed and release nothing; the state of
        :meth:`replay` says which it was, because it always matches the files.
        After a crash at any point the files are a valid chain, with or
        without the new record.
        """
        with self._lock:
            self._usable()
            if not self.writable:
                raise LedgerError("the store was opened for reading only; nothing can be recorded")
            state = self.replay()
            seq = state.head.seq + 1
            try:
                encoded, data, when = self._compose(state, seq, kind, fields, time or self.clock())
            except (ValueError, TypeError, RecursionError) as exc:
                reason = str(exc) if isinstance(exc, _MalformedError) else type(exc).__name__
                raise LedgerError(
                    f"record {seq} was not written: it is malformed ({reason})", seq=seq
                ) from None
            try:
                blob = seal(encoded, key=self._key_in_use(), content_type=LEDGER_TYPE)
            except WDVAError as exc:
                raise LedgerError(
                    f"record {seq} was not written: it could not be sealed ({type(exc).__name__})",
                    seq=seq,
                ) from None
            record = Record(seq, kind, when, data, hashlib.sha256(blob).hexdigest())
            path = self.directory / record_name(seq)
            try:
                atomic_write(path, blob, overwrite=False)
            except BaseException as exc:
                # The write may have got as far as the final name. What the
                # directory holds now decides, not the exception.
                found = _on_disk(path, blob)
                if found:
                    self._applied(state, record)
                elif found is None:
                    self._broken = _STALE
                if isinstance(exc, FileExistsError):
                    raise LedgerError(
                        f"record {seq} exists already: another program is writing to this store",
                        seq=seq,
                    ) from None
                if isinstance(exc, Exception):
                    outcome = (
                        "was written, but the write reported a failure"
                        if found
                        else "could not be written"
                    )
                    raise LedgerError(
                        f"record {seq} {outcome} ({type(exc).__name__})", seq=seq
                    ) from exc
                raise
            self._applied(state, record)
            try:
                _flush_directory(self.directory)
            except OSError as exc:
                raise LedgerError(
                    f"record {seq} was written but could not be flushed to disk", seq=seq
                ) from exc
            return record

    def _applied(self, state: LedgerState, record: Record) -> None:
        try:
            _apply(state, record)
        except BaseException:
            self._broken = _STALE
            raise

    def close(self) -> None:
        """Forget the key. Every later call raises :class:`LedgerError`."""
        with self._lock:
            self._key = None
            self._state = None
