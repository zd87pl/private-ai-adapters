# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The gate: every decision about what an agent application receives, in one place.

A :class:`Gate` sits on an open store. Agent applications reach it through
three calls, :meth:`Gate.status`, :meth:`Gate.search` and :meth:`Gate.read`;
the owner reaches it through the other methods. One lock surrounds every
method, so each call is decided and recorded as a whole.

What a call under a client name can get is decided here and nowhere else:

* Nothing is reachable without a grant. A name that is not registered, and a
  registered name that this session does not serve, get the same answer and
  leave the same trace.
* A hosted client never gets anything from a collection that is not marked for
  hosted clients, whatever grants are stored.
* Every ``OK`` search and read result is written to the record, with its exact
  text, *before* it is returned. When the record cannot be written, the caller
  gets no document text.
* The length of a result is ``len()`` of its complete text. It counts against
  the client and against its pool, over 24 hours and over 30 days. These
  figures are read from the record every time, so closing and reopening the
  store frees nothing.
* Of one document, the clients of a pool get at most the free allowance without
  the owner's approval, counted in distinct characters and never refilled.
  Beyond it, and in a collection held in ``approve`` mode, a passage is
  released only under an approval the owner gave for exactly that client,
  passage and text. An approval is used once and expires. Until it is given,
  the limits refuse the passage only when nothing at all is left in one of
  them, so that a refusal does not tell how long the passage is; whether it
  would fit is told to the owner, who is asked. Once it is approved, its real
  length is measured like that of any result.
* The strings the owner listed are replaced in everything that leaves, and
  every piece of document text is checked once more before it is recorded.

The limits hold for calls made through the gate while the record is intact.
Client names are labels: any program running as the owner can call under any
name the session serves. What is released to a hosted client is sent to its
provider and cannot be recalled.

This module opens no socket and imports no server code.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
import secrets
import threading
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from wdva._util import MAX_SAFE_INTEGER
from wdva.gate import messages
from wdva.gate.errors import ApprovalError, GateError, LedgerError, StoreFormatError
from wdva.gate.index import CollectionIndex, Hit, merge_hits
from wdva.gate.ledger import DocumentInfo, LedgerState, Record, format_time, record_time
from wdva.gate.policy import (
    GATE_CALLS_PER_MINUTE,
    MAX_RESULT_CHARS,
    MAX_SEARCH_RESULTS,
    MODE_READ,
    NAME_PATTERN,
    POOLS,
    Limits,
    Policy,
    PoolLimits,
    is_name,
    kinds_served,
)
from wdva.gate.store import (
    MAX_SOURCE_BYTES,
    MAX_TITLE_CHARS,
    REASON_DUPLICATE,
    REASON_EMPTY,
    REASON_TOO_LARGE,
    DocumentRefusedError,
    Store,
    UnknownDocumentError,
)
from wdva.gate.textproc import (
    MAX_QUERY_CHARS,
    SANITIZER_VERSION,
    ListedStrings,
    Span,
    check_sanitized,
    chunk_spans,
    compile_listed,
    find_listed,
    grow,
    query_terms,
    render,
    sanitize,
    snippet_text,
    snippet_window,
    tokens,
    union_length,
)
from wdva.gate.types import Caller, Preview, Reply

__all__ = [
    "DAY",
    "MAX_PENDING",
    "MAX_PENDING_PER_CLIENT",
    "REASON_UNREADABLE",
    "RULE_APPROVAL",
    "RULE_READ",
    "RULE_SEARCH",
    "THIRTY_DAYS",
    "UNREGISTERED",
    "Caller",
    "ClientUsage",
    "DocumentUsage",
    "Gate",
    "GrantedApproval",
    "PassageChangedError",
    "PendingApproval",
    "PoolUsage",
    "Preview",
    "Reply",
    "SessionStart",
    "UnknownPassageError",
    "Usage",
]

UNREGISTERED = "(unregistered)"
"""The name under which refusals for names that are not served are recorded, whatever was sent."""

MAX_PENDING = 200
"""How many passages can wait for the owner's answer, over all clients together."""

MAX_PENDING_PER_CLIENT = 50
"""How many passages of one client can wait; beyond it that client's oldest is forgotten."""

DAY = timedelta(hours=24)
THIRTY_DAYS = timedelta(days=30)

RULE_SEARCH = "grant:search"
RULE_READ = "grant:read"
RULE_APPROVAL = "approval"

REASON_UNREADABLE = "cannot be read"
"""The ``reason`` of a refused document whose text cannot be cleaned."""

_MINUTE = timedelta(seconds=60)
_HOUR = timedelta(minutes=60)
_MINUTES_PER_DAY = 24 * 60
_CLAIMED_CHARS = 64
_ARG_CHARS = 200
_REF_CHARS = 64
_COLLECTION_CHARS = 32
_LABEL_CHARS = 64
_DEFAULT_RESULTS = 5
_NUMBER_CHARS = 32
_TRANSPORTS = ("stdio", "cli", "python")
_QUOTES = "\"'`"
_REF = re.compile(r"(d-[0-9a-f]{12})\.([0-9]{1,5})")
_REPLACEMENT = "\N{REPLACEMENT CHARACTER}"


class UnknownPassageError(ApprovalError):
    """The owner named a client the session does not serve, or a passage it cannot be given.

    ``reason`` is ``"unknown-client"`` or ``"unknown-ref"``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class PassageChangedError(ApprovalError):
    """The text to approve is no longer the text the owner was shown. Nothing was approved."""

    reason = "changed"


class _UnavailableError(Exception):
    """A result cannot be built or may not leave. ``variant`` says why, for the record."""

    def __init__(self, variant: str) -> None:
        super().__init__(variant)
        self.variant = variant


# ---------------------------------------------------------------------------
# What the owner's side is told
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionStart:
    """What was found when the gate was unlocked."""

    unlocked_at: datetime
    lock_at: datetime | None
    """When the session ends on its own, if it does."""
    head_before: str
    """The head of the record before this unlock, for example ``1234:51c0e2aa``."""
    previous_session: str
    """``"first"``, ``"clean"`` or ``"unclean"`` (the session before did not lock)."""
    last_lock: datetime | None
    documents: int
    collections: int
    characters: int
    unavailable: tuple[str, ...]
    """Documents the record lists whose files are missing or are not what was added."""
    unlisted_files: int
    """Files in the store that belong to no listed document. They are ignored."""


@dataclass(frozen=True)
class PendingApproval:
    """A passage a client asked for and was told to wait for the owner."""

    client: str
    ref: str
    first_at: datetime
    last_at: datetime
    count: int


@dataclass(frozen=True)
class GrantedApproval:
    """An approval the owner gave that has not been used yet."""

    client: str
    ref: str
    valid_until: datetime


@dataclass(frozen=True)
class ClientUsage:
    """What one registered client received, and what it may still receive."""

    client: str
    hosted: bool
    pool: str
    destination: str
    served: bool
    chars_day: int
    chars_30_days: int
    searches_day: int
    """Searches in 24 hours as the limit counts them, those refused for size included."""
    releases_day: int
    refusals_day: int
    release_count: int
    """The client's own release counter: the number printed in its last result."""
    left: int
    """How many characters may be released to it now."""
    limits: Limits
    last_call: datetime | None
    claimed_name: str | None
    """What the calling program last said it is. Never verified."""
    claimed_version: str | None
    protocol: str | None


@dataclass(frozen=True)
class PoolUsage:
    """What all hosted clients together, or all local ones, received."""

    pool: str
    chars_day: int
    chars_30_days: int
    limits: PoolLimits


@dataclass(frozen=True)
class DocumentUsage:
    """How much of one document has been released, per pool, in distinct characters."""

    doc_id: str
    chars: int
    free_allowance: int
    """How much of it a pool gets without the owner's approval."""
    released: Mapping[str, int]
    available: bool
    """False when the document's file is missing or is not what was added."""


@dataclass(frozen=True)
class Usage:
    """The meters of a gate at one moment."""

    time: datetime
    clients: Mapping[str, ClientUsage]
    pools: Mapping[str, PoolUsage]
    documents: Mapping[str, DocumentUsage]
    refusals_day: int
    """Refused calls in 24 hours, those under names that are not served included."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    """One call after its caller was looked at."""

    now: datetime
    tool: str
    key: str  # the name refusals are recorded under
    client: str | None  # the name, when it is registered and served
    claimed: Mapping[str, str | None] | None
    transport: str
    args: Mapping[str, Any]
    query: str | None = None


@dataclass(frozen=True)
class _Place:
    """Where one passage lies and whether one client may have it without asking.

    All of it is known from the index and the record. Nothing of the document
    is read for it.
    """

    ref: str
    collection: str
    mode: str
    span: Span  # widened so that it cuts through no listed string
    passages: int
    needs_approval: bool
    doc_chars: int
    doc_released: int
    doc_after: int


@dataclass(frozen=True)
class _Passage:
    """One passage as one client would receive it now."""

    ref: str
    doc_id: str
    number: int
    passages: int
    collection: str
    mode: str
    span: Span
    body: str
    body_sha256: str
    replaced: int
    shown_title: str | None
    needs_approval: bool
    doc_chars: int
    doc_released: int
    doc_after: int


@dataclass(frozen=True)
class _Approval:
    body_sha256: str
    given_at: datetime
    valid_until: datetime


@dataclass(frozen=True)
class _Waiting:
    first_at: datetime
    last_at: datetime
    count: int


@dataclass(frozen=True)
class _Line:
    """One search line with what it would release."""

    line: messages.SearchLine
    span: tuple[str, int, int] | None
    replaced: int


class _Bucket:
    """The calls counted for one name in the last minute."""

    __slots__ = ("_times",)

    def __init__(self) -> None:
        self._times: deque[datetime] = deque()

    def count(self, now: datetime) -> int:
        earliest = now - _MINUTE
        times = self._times
        while times and times[0] <= earliest:
            times.popleft()
        return sum(1 for when in times if when > earliest)

    def add(self, now: datetime) -> None:
        self._times.append(now)


def _clean(value: str, limit: int) -> str:
    """``value`` as one printable line of at most ``limit`` characters."""
    line = " ".join(value.split())
    return "".join(ch if ch.isprintable() else _REPLACEMENT for ch in line)[:limit]


def _claimed_field(value: Any) -> str | None:
    """What a program says about itself, made safe to keep and to show. Never trusted."""
    if not isinstance(value, str):
        return None
    try:
        return _clean(sanitize(value[:_CLAIMED_CHARS]), _CLAIMED_CHARS)
    except ValueError:
        return ""


def _record_value(value: Any) -> Any:
    """An argument as a refusal's record keeps it: short clean text, a plain number, or nothing."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _clean(value[:_ARG_CHARS], _ARG_CHARS)
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE_INTEGER else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _plain_text(value: Any) -> str | None:
    """``value`` when it is text that can be stored and sent; ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _result_count(value: Any) -> int:
    """How many search results were asked for: 1 to 10, and 5 when it cannot be told."""
    number: int | float | None = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, (int, float)):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if 0 < len(text) <= _NUMBER_CHARS:
            try:
                number = int(text)
            except ValueError:
                with contextlib.suppress(ValueError):
                    number = float(text)
    if number is None or (isinstance(number, float) and not math.isfinite(number)):
        return _DEFAULT_RESULTS
    return max(1, min(MAX_SEARCH_RESULTS, int(number)))


def _parse_ref(value: str) -> tuple[str, int] | None:
    """Read a ref leniently: blanks, one pair of quotes, upper case and ``ref=`` are forgiven."""
    text = value.strip()
    if len(text) > _REF_CHARS:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _QUOTES:
        text = text[1:-1]
    text = text.lower()
    if text.startswith("ref="):
        text = text[4:]
    match = _REF.fullmatch(text.strip())
    if match is None:
        return None
    number = int(match.group(2))
    if number < 1:
        return None
    return match.group(1), number


def _clean_title(title: str) -> str:
    """A title as it is stored: cleaned like document text, one line, at most 120 characters."""
    try:
        line = " ".join(sanitize(title).split())[:MAX_TITLE_CHARS].strip()
        line = " ".join(sanitize(line).split())
    except ValueError:
        raise ValueError("a title must be plain text") from None
    if not line or len(line) > MAX_TITLE_CHARS or check_sanitized(line):
        raise ValueError(
            f"a title is one line of 1 to {MAX_TITLE_CHARS} characters of plain text, "
            "without links or markup"
        )
    return line


def _matches_in(matches: Sequence[Span], span: Span) -> int:
    """How many of ``matches`` share a character with ``span``."""
    return sum(1 for start, end in matches if start < span[1] and end > span[0])


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _spoken(claimed: Mapping[str, Any] | None) -> str | None:
    """The name and version a program gave, as one string, or ``None`` when it gave none."""
    if not claimed:
        return None
    parts = [claimed.get("name"), claimed.get("version")]
    return " ".join(part for part in parts if isinstance(part, str) and part) or None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class Gate:
    """The unlocked gate over one open store.

    Building it verifies the whole record, rebuilds the limits from it, reads
    every listed document once to build the search index (kept in memory only)
    and appends the ``unlock`` record. It needs a store opened for writing and
    leaves closing the store to whoever opened it.

    ``served`` are the client names this session answers; ``None`` means every
    registered client, also ones registered later. ``clock`` supplies every
    time: of records, of limits and of approvals. When given it also becomes
    the store's clock; when left out the store's clock is used, so there is
    only ever one. ``token_hex`` draws the tag that marks quoted text in a read
    result. ``on_record`` is called with each record after it was appended (the
    complete JSON object, read-only), in record order, while the gate's lock is
    held; an exception from it is swallowed and cannot make a call fail.
    ``lock_at`` is when the session ends on its own: from that moment the gate
    answers ``LOCKED`` and has appended the ``lock`` record, whether or not
    anybody called :meth:`close`.

    The three agent methods never raise: whatever goes wrong becomes the
    ``UNAVAILABLE`` result, which carries no document text. The owner's methods
    raise :class:`~wdva.gate.errors.GateError` once the gate is locked.

    It is a context manager; leaving the block calls :meth:`close`.
    """

    def __init__(
        self,
        store: Store,
        *,
        served: Iterable[str] | None = None,
        clock: Callable[[], datetime] | None = None,
        token_hex: Callable[[int], str] = secrets.token_hex,
        on_record: Callable[[Mapping[str, Any]], None] | None = None,
        lock_at: datetime | None = None,
    ) -> None:
        if not isinstance(store, Store):
            raise TypeError("a gate needs an open store")
        if store.closed:
            raise GateError("the store is closed")
        if not store.writable:
            raise GateError("the gate needs a store that was opened for writing")
        if store.sanitizer != SANITIZER_VERSION:
            raise GateError(
                "the documents of this store were cleaned by another version of wdva "
                f"(version {store.sanitizer}); this one reads version {SANITIZER_VERSION}"
            )
        if lock_at is not None:
            format_time(lock_at)  # refuses a time without a time zone
        self._lock = threading.RLock()
        self._store = store
        self._ledger = store.ledger
        if clock is not None:
            store.clock = clock
        self._clock: Callable[[], datetime] = store.clock
        self._token_hex = token_hex
        self._on_record = on_record
        self._lock_at = lock_at
        self._closed = False

        state = self._ledger.replay()  # verifies the chain; raises with the first bad record
        if state.policy is None:
            raise GateError("the record holds no policy")
        self._policy = Policy.from_json(state.policy)
        self._policy_seq = state.policy_seq
        self._listed: ListedStrings = compile_listed(self._policy.redact_literals)

        self._serve_all = served is None
        names = () if served is None else ((served,) if isinstance(served, str) else served)
        self._served: frozenset[str] = frozenset(name for name in names if isinstance(name, str))

        self._indexes: dict[str, CollectionIndex] = {}
        self._where: dict[str, str] = {}  # every indexed document -> its collection
        self._unreadable: set[str] = set()
        self._doc_marks = (-1, -1)
        self._sync_documents(state)

        self._approvals: dict[tuple[str, str], _Approval] = {}
        self._pending: OrderedDict[tuple[str, str], _Waiting] = OrderedDict()
        self._all_calls = _Bucket()
        self._buckets: dict[str, _Bucket] = {}
        self._refused_searches: dict[str, list[datetime]] = {}
        self._folded: dict[tuple[str, str], int] = {}
        self._refusals: dict[str, dict[int, int]] = {}
        self._last_calls: dict[str, tuple[datetime, Mapping[str, str | None]]] = {}
        self._fresh: dict[int, Record] = {}
        self._delivered = state.head.seq

        now = self._clock()
        serving = self.served()
        previous = state.previous_session
        head_before = str(state.head)
        last_lock = state.last_lock
        self._append(
            "unlock",
            {
                "served": list(serving),
                "mixed": kinds_served(self._policy, serving) == "both",
                "previous_session": previous,
                "auto_lock": None if lock_at is None else format_time(lock_at),
            },
            now,
        )
        self.started = SessionStart(
            unlocked_at=now,
            lock_at=lock_at,
            head_before=head_before,
            previous_session=previous,
            last_lock=last_lock,
            documents=len(state.documents),
            collections=len({info.collection for info in state.documents.values()}),
            characters=sum(info.chars for info in state.documents.values()),
            unavailable=tuple(sorted(self._unreadable)),
            unlisted_files=len(store.unlisted_files()),
        )
        self._deliver()

    # -- life cycle -----------------------------------------------------------

    @property
    def closed(self) -> bool:
        """Whether the gate is locked: :meth:`close` was called or ``lock_at`` has passed."""
        with self._lock:
            return not self._open_now()

    def close(self, reason: str = "close") -> None:
        """Lock the gate: drop every approval and append the ``lock`` record.

        ``reason`` says why (``"close"``, ``"lock-command"``, ``"timer"``,
        ``"interrupt"``, ``"signal"``). From now on agents are answered
        ``LOCKED``. The store stays open; closing it is the caller's job.
        Calling this twice is harmless. If the ``lock`` record cannot be
        written the gate is locked all the same and
        :class:`~wdva.gate.errors.LedgerError` is raised; the next unlock then
        reports that this session did not end properly.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._approvals.clear()
            self._pending.clear()
            try:
                label = _clean(str(reason)[:_LABEL_CHARS], _LABEL_CHARS) or "close"
                self._append(
                    "lock",
                    {"reason": label, "folded_refusals": sum(self._folded.values())},
                    self._clock(),
                )
                self._folded.clear()
            finally:
                self._deliver()

    def __enter__(self) -> Gate:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "locked" if self._closed else f"serving {len(self.served())} clients"
        return f"Gate({str(self._store.path)!r}, {state})"

    def _open_now(self) -> bool:
        if self._closed:
            return False
        if self._lock_at is not None and self._clock() >= self._lock_at:
            try:
                self.close("timer")
            except Exception:  # the gate is locked whether or not this could be recorded
                self._closed = True
            return False
        return True

    @contextlib.contextmanager
    def _owner_side(self) -> Iterator[None]:
        """Around every method of the owner: the lock, an open gate, the newest state."""
        with self._lock:
            try:
                if not self._open_now():
                    raise GateError("the gate is locked")
                self._sync()
                yield
            finally:
                self._deliver()

    # -- the record -------------------------------------------------------------

    def _state(self) -> LedgerState:
        return self._ledger.replay()

    def _append(self, kind: str, fields: Mapping[str, Any], when: datetime) -> Record:
        record = self._ledger.append(kind, fields, time=when)
        self._fresh[record.seq] = record
        return record

    def _deliver(self) -> None:
        """Hand every record appended since the last time to ``on_record``. Never raises."""
        try:
            head = self._ledger.replay().head.seq
        except Exception:
            self._fresh.clear()
            return
        while self._delivered < head:
            seq = self._delivered = self._delivered + 1
            record = self._fresh.pop(seq, None)
            if self._on_record is None:
                continue
            try:
                if record is None:
                    record = self._ledger.read(seq)
                self._on_record(MappingProxyType(dict(record.data)))
            except Exception:  # noqa: S112 - a listener must not be able to make a call fail
                continue
        self._fresh.clear()

    def head(self) -> str:
        """The newest record as the owner can note it, for example ``1237:9f2c7a1e``."""
        with self._lock:
            return str(self._ledger.head())

    # -- following the record: policy and documents ------------------------------

    def _sync(self) -> None:
        """Bring policy and index in line with the newest records, whoever wrote them."""
        state = self._state()
        if state.policy_seq != self._policy_seq:
            if state.policy is None:
                raise GateError("the record holds no policy")
            new = Policy.from_json(state.policy)
            old = self._policy
            self._policy = new
            self._policy_seq = state.policy_seq
            self._policy_changed(state, old, new)
        if (state.counts.get("add", 0), state.counts.get("remove", 0)) != self._doc_marks:
            self._sync_documents(state)
            self._forget_stale()

    def _policy_changed(self, state: LedgerState, old: Policy, new: Policy) -> None:
        """Make everything that was derived from the old policy follow the new one."""
        if not self._serve_all:
            # A name that changed its kind is no longer what the owner chose to serve.
            self._served = frozenset(
                name
                for name in self._served
                if name not in old.clients
                or name not in new.clients
                or old.clients[name].hosted == new.clients[name].hosted
            )
        for name in [name for name in self._buckets if name not in new.clients]:
            if name != UNREGISTERED:
                del self._buckets[name]
        for name in [name for name in self._last_calls if name not in new.clients]:
            del self._last_calls[name]
        self._forget_stale()
        # The index knows where the listed strings stand and whether titles can be
        # searched. When either changed it is built again before the next call.
        if old.redact_literals != new.redact_literals:
            self._listed = compile_listed(new.redact_literals)
            self._rebuild(state, None)
        else:
            changed = {
                name
                for name in self._indexes
                if self._shows_titles(old, name) != self._shows_titles(new, name)
            }
            if changed:
                self._rebuild(state, changed)

    @staticmethod
    def _shows_titles(policy: Policy, collection: str) -> bool:
        entry = policy.collections.get(collection)
        return entry is not None and entry.show_titles

    def _rebuild(self, state: LedgerState, only: set[str] | None) -> None:
        """Build the index again, of every collection or of those named."""
        try:
            for doc_id, info in state.documents.items():
                if only is None or info.collection in only:
                    self._where.pop(doc_id, None)
                    self._unreadable.discard(doc_id)
            for name in list(self._indexes):
                if only is None or name in only:
                    del self._indexes[name]
            for info in state.documents.values():
                if only is None or info.collection in only:
                    self._load(info)
        except BaseException:
            # Half an index built under the old rules must never answer a call.
            self._indexes.clear()
            self._where.clear()
            self._doc_marks = (-1, -1)
            raise

    def _sync_documents(self, state: LedgerState) -> None:
        for doc_id in [doc_id for doc_id in self._where if doc_id not in state.documents]:
            self._drop(doc_id)
        self._unreadable.intersection_update(state.documents)
        for doc_id, info in state.documents.items():
            if doc_id not in self._where and doc_id not in self._unreadable:
                self._load(info)
        self._doc_marks = (state.counts.get("add", 0), state.counts.get("remove", 0))

    def _load(self, info: DocumentInfo) -> None:
        """Read one listed document and index it; a document that cannot be read is set aside."""
        try:
            text = self._store.read_document(info.doc_id)["text"]
        except (StoreFormatError, UnknownDocumentError):
            self._unreadable.add(info.doc_id)
            return
        index = self._indexes.get(info.collection)
        if index is None:
            index = self._indexes[info.collection] = CollectionIndex(
                show_titles=self._shows_titles(self._policy, info.collection),
                listed=self._listed,
                chunk_chars=self._store.chunk_chars,
            )
        if index.add(info.doc_id, text, info.title) != info.passages:
            index.remove(info.doc_id)
            self._unreadable.add(info.doc_id)
            return
        self._where[info.doc_id] = info.collection

    def _drop(self, doc_id: str, *, unavailable: bool = False) -> None:
        collection = self._where.pop(doc_id, None)
        if collection is not None:
            with contextlib.suppress(KeyError):
                self._indexes[collection].remove(doc_id)
        if unavailable:
            self._unreadable.add(doc_id)

    def _text(self, doc_id: str) -> str | None:
        """The stored text of an indexed document, or ``None`` when its file has gone bad."""
        try:
            text = self._store.read_document(doc_id)["text"]
        except (StoreFormatError, UnknownDocumentError):
            self._drop(doc_id, unavailable=True)
            return None
        return str(text)

    def _forget_stale(self) -> None:
        """Drop approvals and waiting passages whose client, grant or document is gone."""
        for key in [key for key in self._approvals if not self._still_possible(*key)]:
            del self._approvals[key]
        for key in [key for key in self._pending if not self._still_possible(*key)]:
            del self._pending[key]

    def _still_possible(self, client: str, ref: str) -> bool:
        collection = self._where.get(ref.partition(".")[0])
        return (
            collection is not None
            and self._is_served(client)
            and self._policy.effective_grant(client, collection) is not None
        )

    # -- who is served ------------------------------------------------------------

    def _is_served(self, name: str) -> bool:
        return name in self._policy.clients and (self._serve_all or name in self._served)

    def served(self) -> tuple[str, ...]:
        """The registered client names this session answers, in name order."""
        with self._lock:
            return tuple(name for name in self._policy.clients if self._is_served(name))

    def set_served(self, names: Iterable[str]) -> None:
        """Choose the client names this session answers, from the next call on.

        Names that are not registered are kept but answer nothing until they
        are. A name that is no longer served loses its approvals and its
        waiting passages. Nothing is recorded: the choice lasts for the session.
        """
        chosen = frozenset(
            name
            for name in ((names,) if isinstance(names, str) else names)
            if isinstance(name, str)
        )
        with self._owner_side():
            self._serve_all = False
            self._served = chosen
            self._forget_stale()

    # -- every call ---------------------------------------------------------------

    def status(self, caller: Caller) -> Reply:
        """What this client may use and how much is left. Releases no document text."""
        return self._answer(messages.TOOL_STATUS, caller, {}, self._status)

    def search(self, caller: Caller, query: Any, collection: Any = "", limit: Any = 5) -> Reply:
        """Search the collections the client is granted and return refs, best match first.

        A hit in a collection held in ``read`` mode carries a short snippet
        while the document's free allowance lasts; a hit in a collection held
        in ``approve`` mode carries no document text, and numbers are not
        searched there. Every ``OK`` result, "no matches" included, is recorded
        and counted in full.
        """
        raw = {"query": query, "collection": collection, "limit": limit}
        return self._answer(messages.TOOL_SEARCH, caller, raw, self._search)

    def read(self, caller: Caller, ref: Any) -> Reply:
        """Return one passage, whole, or say why not.

        Inside the document's free allowance, in a collection held in ``read``
        mode, the passage is released at once. Otherwise it needs an approval
        the owner gave for this client and exactly this text.
        """
        return self._answer(messages.TOOL_READ, caller, {"ref": ref}, self._read)

    def _answer(
        self,
        tool: str,
        caller: Caller,
        raw: Mapping[str, Any],
        decide: Callable[[_Call, Mapping[str, Any]], Reply],
    ) -> Reply:
        with self._lock:
            call: _Call | None = None
            try:
                try:
                    if not self._open_now():
                        return Reply("LOCKED", messages.locked(), False)
                    self._sync()
                    admitted = self._admit(tool, caller, raw)
                    if isinstance(admitted, Reply):
                        return admitted
                    call = admitted
                    return decide(call, raw)
                except _UnavailableError as exc:
                    return self._failed(call, exc.variant)
                except Exception as exc:
                    return self._failed(call, f"error:{type(exc).__name__}")
            except Exception:
                # Whatever went wrong while saying so: the answer carries no document text.
                return Reply("UNAVAILABLE", messages.unavailable(), False)
            finally:
                self._deliver()

    def _admit(self, tool: str, caller: Caller, raw: Mapping[str, Any]) -> _Call | Reply:
        """Look at the caller, count the call, and refuse a name that is not served."""
        now = self._clock()
        name = getattr(caller, "client", None)
        known = (
            isinstance(name, str)
            and NAME_PATTERN.fullmatch(name) is not None
            and self._is_served(name)
        )
        transport = getattr(caller, "transport", None)
        claimed: dict[str, str | None] | None = None
        if known:
            claimed = {
                "name": _claimed_field(getattr(caller, "claimed_name", None)),
                "version": _claimed_field(getattr(caller, "claimed_version", None)),
                "protocol": _claimed_field(getattr(caller, "protocol", None)),
            }
        served_name: str | None = name if known else None
        call = _Call(
            now=now,
            tool=tool,
            key=served_name or UNREGISTERED,
            client=served_name,
            claimed=claimed,
            transport=transport if transport in _TRANSPORTS else "other",
            args={key: _record_value(value) for key, value in raw.items()},
        )
        # Names that are not served share one allowance of calls, so inventing
        # names buys nothing. A call refused here is not counted.
        per_minute = (
            self._policy.limits_for(call.key) if known else self._policy.defaults
        ).calls_per_minute
        bucket = self._buckets.get(call.key)
        if bucket is None:
            bucket = self._buckets[call.key] = _Bucket()
        if self._all_calls.count(now) >= GATE_CALLS_PER_MINUTE or bucket.count(now) >= per_minute:
            return self._refuse(call, "OVER_LIMIT", "calls")
        self._all_calls.add(now)
        bucket.add(now)
        if call.client is None or claimed is None:
            return self._refuse(call, "NOT_ALLOWED")
        self._last_calls[call.client] = (now, claimed)
        return call

    # -- refusals -----------------------------------------------------------------

    def _refuse(
        self,
        call: _Call,
        code: str,
        variant: str = "",
        *,
        text: str | None = None,
        collections: Sequence[str] = (),
    ) -> Reply:
        """The reply for a refused call. The refusal is noted; the reply never depends on that."""
        if text is None:
            if code == "OVER_LIMIT":
                text = messages.over_limit(variant)
            elif code == "NOT_ALLOWED":
                text = messages.not_allowed()
            elif code == "NOT_FOUND":
                text = messages.not_found()
            elif code == "BAD_REQUEST":
                text = messages.bad_request()
            else:
                code, text = "UNAVAILABLE", messages.unavailable()
        with contextlib.suppress(Exception):
            self._note_refusal(call, code, variant, collections)
        return Reply(code, text, False)

    def _failed(self, call: _Call | None, variant: str) -> Reply:
        if call is None:
            return Reply("UNAVAILABLE", messages.unavailable(), False)
        return self._refuse(call, "UNAVAILABLE", variant[:_LABEL_CHARS])

    def _note_refusal(
        self, call: _Call, code: str, variant: str, collections: Sequence[str]
    ) -> None:
        """Count a refusal, and write a record unless one of its kind was written this hour."""
        minute = int(call.now.timestamp() // 60)
        minutes = self._refusals.setdefault(call.key, {})
        minutes[minute] = minutes.get(minute, 0) + 1
        if len(minutes) > 2 * _MINUTES_PER_DAY:
            for old in [m for m in minutes if m <= minute - _MINUTES_PER_DAY]:
                del minutes[old]

        key = (call.key, code)
        folded = self._folded.get(key, 0)
        last = self._state().denials.get(key)
        if last is not None and call.now - last < _HOUR:
            # A record dated later than now counts as recent, too.
            self._folded[key] = folded + 1
            return
        fields: dict[str, Any] = {
            "client": call.key,
            "code": code,
            "variant": variant,
            "claimed": None if call.claimed is None else dict(call.claimed),
            "transport": call.transport,
            "tool": call.tool,
            "args": dict(call.args),
            "folded_before": folded,
        }
        if call.query is not None:
            fields["query"] = call.query
        if collections:
            fields["collections"] = list(collections)
        try:
            self._append("denial", fields, call.now)
        except (LedgerError, OSError):
            self._folded[key] = folded + 1
            return
        self._folded.pop(key, None)

    def _refusals_day(self, key: str, now: datetime) -> int:
        earliest = int(now.timestamp() // 60) - _MINUTES_PER_DAY
        return sum(
            count for minute, count in self._refusals.get(key, {}).items() if minute > earliest
        )

    # -- the figures a decision reads --------------------------------------------

    def _uses(self, state: LedgerState, name: str, now: datetime) -> list[tuple[str, int, int]]:
        """Per window: its name, the characters used in it, and its limit."""
        limits = self._policy.limits_for(name)
        pool_name = self._policy.pool_of(name)
        pool_limits = self._policy.pools[pool_name]
        client = state.client(name).releases
        pool = state.pool(pool_name)
        return [
            ("client-day", client.used(now, DAY), limits.chars_per_day),
            ("client-30-days", client.used(now, THIRTY_DAYS), limits.chars_per_30_days),
            ("pool-day", pool.used(now, DAY), pool_limits.chars_per_day),
            ("pool-30-days", pool.used(now, THIRTY_DAYS), pool_limits.chars_per_30_days),
        ]

    @staticmethod
    def _left(uses: Iterable[tuple[str, int, int]]) -> int:
        return messages.left_now((used, limit) for _, used, limit in uses)

    def _searches(self, state: LedgerState, name: str, now: datetime) -> int:
        """Searches in the last 24 hours: the recorded ones, and those refused for their size."""
        earliest = now - DAY
        refused = [when for when in self._refused_searches.get(name, ()) if when > earliest]
        if refused:
            self._refused_searches[name] = refused
        else:
            self._refused_searches.pop(name, None)
        return state.client(name).searches.count(now, DAY) + len(refused)

    def _owner_line(self, text: str) -> str:
        """A note or destination as an agent may see it: listed strings replaced, checked."""
        try:
            line = render(text, (0, len(text)), find_listed(text, self._listed), one_line=True)
        except ValueError:
            raise _UnavailableError("render") from None
        self._require_clean([line])
        return line

    def _shown_title(self, collection: str, doc_id: str) -> str | None:
        """The title as a result shows it, or ``None`` where titles are not shown."""
        if not self._shows_titles(self._policy, collection):
            return None
        title = self._state().documents[doc_id].title
        try:
            return render(title, (0, len(title)), find_listed(title, self._listed), one_line=True)
        except ValueError:
            raise _UnavailableError("render") from None

    @staticmethod
    def _require_clean(parts: Iterable[str]) -> None:
        """Refuse to let any piece of document text leave that the cleaning should have changed."""
        for part in parts:
            if check_sanitized(part):
                raise _UnavailableError("check")

    # -- status ---------------------------------------------------------------

    def _status(self, call: _Call, raw: Mapping[str, Any]) -> Reply:
        name = call.client
        assert name is not None
        policy = self._policy
        client = policy.clients[name]
        state = self._state()
        limits = policy.limits_for(name)
        collections = [
            messages.StatusCollection(
                collection, mode, self._owner_line(policy.collections[collection].note)
            )
            for collection, mode in policy.effective_grants(name).items()
        ]
        try:
            text = messages.status_ok(
                client=name,
                destination=self._owner_line(client.destination),
                hosted=client.hosted,
                collections=collections,
                left=self._left(self._uses(state, name, call.now)),
                chars_per_day=limits.chars_per_day,
                chars_per_30_days=limits.chars_per_30_days,
                searches_per_day=limits.searches_per_day,
                chars_per_call=limits.chars_per_call,
                max_chars=policy.per_document.max_chars,
                max_fraction=policy.per_document.max_fraction,
            )
        except ValueError:
            raise _UnavailableError("form") from None
        last = state.statuses.get(name)
        if last is None or call.now - last >= _HOUR:
            with contextlib.suppress(LedgerError, OSError):
                self._append(
                    "status",
                    {
                        "client": name,
                        "claimed": dict(call.claimed or {}),
                        "transport": call.transport,
                    },
                    call.now,
                )
        return Reply("OK", text, False)

    # -- search ---------------------------------------------------------------

    def _search(self, call: _Call, raw: Mapping[str, Any]) -> Reply:
        name = call.client
        assert name is not None
        query = _plain_text(raw["query"])
        if query is None:
            return self._refuse(call, "BAD_REQUEST")
        query = query[:MAX_QUERY_CHARS]
        call = replace(call, query=_clean(query, MAX_QUERY_CHARS))

        collection = raw["collection"]
        if collection is None:
            wanted = ""
        elif isinstance(collection, str):
            wanted = collection.strip()
        else:
            return self._refuse(call, "NOT_FOUND")
        if len(wanted) > _COLLECTION_CHARS or (wanted and not is_name(wanted)):
            return self._refuse(call, "NOT_FOUND")
        limit = _result_count(raw["limit"])
        call = replace(
            call,
            args={
                "query": _clean(query[:_ARG_CHARS], _ARG_CHARS),
                "collection": wanted,
                "limit": limit,
            },
        )

        policy = self._policy
        grants = policy.effective_grants(name)
        if wanted:
            if wanted not in grants:
                return self._refuse(call, "NOT_FOUND")
            grants = {wanted: grants[wanted]}
        searched = sorted(grants)

        state = self._state()
        limits = policy.limits_for(name)
        if self._searches(state, name, call.now) >= limits.searches_per_day:
            return self._refuse(call, "OVER_LIMIT", "searches", collections=searched)

        groups: list[list[Hit]] = []
        for collection_name, mode in grants.items():
            index = self._indexes.get(collection_name)
            # Numbers are not searched where every passage needs approval: a
            # search could otherwise spell out a number digit by digit.
            terms = query_terms(query, digits=(mode == MODE_READ))
            if index is not None and terms:
                groups.append(index.search(terms, limit))
        hits = merge_hits(groups, limit)

        lines = self._search_lines(name, grants, hits, query_terms(query), state)
        try:
            ok = messages.fit_search(
                [entry.line for entry in lines],
                chars_per_call=limits.chars_per_call,
                client=name,
                client_seq=state.client(name).release_seq + 1,
                left_before=self._left(self._uses(state, name, call.now)),
            )
        except ValueError:
            raise _UnavailableError("form") from None
        shown = lines[: ok.shown]
        parts = [entry.line.text for entry in shown]
        parts.extend(entry.line.title for entry in shown if entry.line.title is not None)
        return self._release(
            call,
            ok=ok,
            rule=RULE_SEARCH,
            collections=searched,
            spans=[entry.span for entry in shown if entry.span is not None],
            replaced=sum(entry.replaced for entry in shown),
            parts=parts,
            extra={"lines": ok.shown},
        )

    def _search_lines(
        self,
        name: str,
        grants: Mapping[str, str],
        hits: Sequence[Hit],
        terms: Sequence[str],
        state: LedgerState,
    ) -> list[_Line]:
        """One line per hit, in rank order, with the snippet where the allowance has room."""
        policy = self._policy
        pool = policy.pool_of(name)
        accepted: dict[str, list[Span]] = {}
        texts: dict[str, str | None] = {}
        lines: list[_Line] = []
        for hit in hits:
            collection = self._where.get(hit.doc_id)
            mode = grants.get(collection) if collection is not None else None
            if collection is None or mode is None:
                continue
            title = self._shown_title(collection, hit.doc_id)
            if mode != MODE_READ:
                line = messages.SearchLine(hit.ref, collection, messages.TEXT_WITHHELD, title)
                lines.append(_Line(line, None, 0))
                continue
            if hit.doc_id not in texts:
                texts[hit.doc_id] = self._text(hit.doc_id)
            text = texts[hit.doc_id]
            if text is None:
                continue
            index = self._indexes[collection]
            matches = index.listed(hit.doc_id)
            window = snippet_window(text, hit.span, terms, matches)
            released = state.released_spans(pool, hit.doc_id)
            taken = accepted.setdefault(hit.doc_id, [])
            allowance = policy.per_document.free_allowance(index.chars(hit.doc_id))
            adds_nothing = union_length([*released, window]) == union_length(released)
            if not adds_nothing and union_length([*released, *taken, window]) > allowance:
                line = messages.SearchLine(hit.ref, collection, messages.NO_FREE_TEXT, title)
                lines.append(_Line(line, None, 0))
                continue
            try:
                snippet = snippet_text(text, hit.span, window, matches)
            except ValueError:
                raise _UnavailableError("render") from None
            taken.append(window)
            span = (hit.doc_id, window[0], window[1]) if window[0] < window[1] else None
            line = messages.SearchLine(hit.ref, collection, snippet, title)
            lines.append(_Line(line, span, _matches_in(matches, window)))
        return lines

    # -- read -----------------------------------------------------------------

    def _read(self, call: _Call, raw: Mapping[str, Any]) -> Reply:
        name = call.client
        assert name is not None
        value = _plain_text(raw["ref"])
        if value is None:
            return self._refuse(call, "BAD_REQUEST")
        parsed = _parse_ref(value)
        if parsed is None:
            return self._refuse(call, "NOT_FOUND")
        doc_id, number = parsed
        call = replace(call, args={"ref": f"{doc_id}.{number}"})
        place = self._place(name, doc_id, number)
        if place is None:
            return self._refuse(call, "NOT_FOUND")
        if place.needs_approval and self._approval_in_force(name, place.ref, call.now) is None:
            # Answered before the document is read: how long the refusal takes must not
            # tell what its text does not, such as how long the passage or its document is.
            return self._ask_owner(call, place.ref, [place.collection])
        passage = self._passage(name, doc_id, number, place)
        if passage is None:
            return self._refuse(call, "NOT_FOUND")
        state = self._state()
        try:
            ok = messages.read_ok(
                doc_id=doc_id,
                passage=number,
                passages=passage.passages,
                collection=passage.collection,
                body=passage.body,
                tag=self._token_hex(3),
                client=name,
                client_seq=state.client(name).release_seq + 1,
                left_before=self._left(self._uses(state, name, call.now)),
                title=passage.shown_title,
            )
        except ValueError:
            raise _UnavailableError("form") from None
        parts = [passage.body]
        if passage.shown_title is not None:
            parts.append(passage.shown_title)
        return self._release(
            call,
            ok=ok,
            rule=RULE_READ,
            collections=[passage.collection],
            spans=[(doc_id, passage.span[0], passage.span[1])],
            replaced=passage.replaced,
            parts=parts,
            approval=passage if passage.needs_approval else None,
            extra={"passage": number, "passages": passage.passages},
        )

    def _place(self, name: str, doc_id: str, number: int) -> _Place | None:
        """Where the passage lies for ``name``, or ``None``: no such passage for this client.

        ``None`` covers a document that does not exist, a passage number that
        does not, and a collection the client is not granted, without telling
        which. The document itself is not read here, so that a passage which
        waits for the owner can be refused without its text being touched.
        """
        policy = self._policy
        collection = self._where.get(doc_id)
        mode = policy.effective_grant(name, collection) if collection is not None else None
        if collection is None or mode is None:
            return None
        index = self._indexes[collection]
        try:
            span = grow(index.span(doc_id, number), index.listed(doc_id))
        except (KeyError, IndexError):
            return None
        chars = index.chars(doc_id)
        released = self._state().released_spans(policy.pool_of(name), doc_id)
        before = union_length(released)
        after = union_length([*released, span])
        # Free: inside the allowance, or nothing that was not released before.
        free = mode == MODE_READ and (
            after <= policy.per_document.free_allowance(chars) or after == before
        )
        return _Place(
            ref=f"{doc_id}.{number}",
            collection=collection,
            mode=mode,
            span=span,
            passages=index.passages(doc_id),
            needs_approval=not free,
            doc_chars=chars,
            doc_released=before,
            doc_after=after,
        )

    def _passage(
        self, name: str, doc_id: str, number: int, place: _Place | None = None
    ) -> _Passage | None:
        """The passage as ``name`` would get it, or ``None``: no such passage for this client.

        ``None`` covers what :meth:`_place` covers, and a document whose file
        has gone bad. This reads the document and renders the passage.
        """
        place = place or self._place(name, doc_id, number)
        if place is None:
            return None
        text = self._text(doc_id)
        if text is None:
            return None
        matches = self._indexes[place.collection].listed(doc_id)
        try:
            body = render(text, place.span, matches)
        except ValueError:
            raise _UnavailableError("render") from None
        return _Passage(
            ref=place.ref,
            doc_id=doc_id,
            number=number,
            passages=place.passages,
            collection=place.collection,
            mode=place.mode,
            span=place.span,
            body=body,
            body_sha256=_sha256(body),
            replaced=_matches_in(matches, place.span),
            shown_title=self._shown_title(place.collection, doc_id),
            needs_approval=place.needs_approval,
            doc_chars=place.doc_chars,
            doc_released=place.doc_released,
            doc_after=place.doc_after,
        )

    # -- the one way out --------------------------------------------------------

    def _release(
        self,
        call: _Call,
        *,
        ok: messages.OkText,
        rule: str,
        collections: Sequence[str],
        spans: Sequence[tuple[str, int, int]],
        replaced: int,
        parts: Sequence[str],
        extra: Mapping[str, Any],
        approval: _Passage | None = None,
    ) -> Reply:
        """Check, record and only then return an ``OK`` search or read result.

        Every result that carries document text leaves through here. The order
        is fixed: the pieces of document text are checked; the complete text is
        measured against the client's and the pool's limits; a passage that
        needs one must have its approval; the record with the exact text is
        appended; and only when that succeeded is the text handed back.

        One exception to "the complete text is measured": a passage that needs
        an approval and has none is refused for the limits only when one of
        them has nothing left at all (:meth:`_ask_owner`). The choice between
        ``OVER_LIMIT`` and ``NEEDS_APPROVAL`` then does not depend on the
        passage, and a limit that still has room never keeps a passage from
        being put before the owner. Nothing is released in that case either
        way. A read of such a passage does not even get here when no approval
        is in force for it: it is answered before its document is read. What
        gets here is a passage that was approved as other text than it has now.
        """
        name = call.client
        assert name is not None
        text = ok.text
        chars = len(text)
        self._require_clean(parts)
        if chars > MAX_RESULT_CHARS:
            raise _UnavailableError("size")
        try:
            digest = _sha256(text)
        except UnicodeEncodeError:
            raise _UnavailableError("encoding") from None

        if approval is not None and not self._approved(name, approval, call.now):
            return self._ask_owner(call, approval.ref, collections)
        state = self._state()
        uses = self._uses(state, name, call.now)
        for variant, used, limit in uses:
            if used + chars > limit:
                if call.tool == messages.TOOL_SEARCH:
                    # Counts as one search, or a caller with little left could
                    # learn at no cost whether a word occurs: a query with hits
                    # is refused here, one without is short enough to pass.
                    self._refused_searches.setdefault(name, []).append(call.now)
                return self._refuse(call, "OVER_LIMIT", variant, collections=collections)

        if approval is not None:
            rule = RULE_APPROVAL

        client = self._policy.clients[name]
        client_seq = state.client(name).release_seq + 1
        fields: dict[str, Any] = {
            "client": name,
            "client_seq": client_seq,
            "pool": client.pool,
            "destination": client.destination,
            "hosted": client.hosted,
            "claimed": dict(call.claimed or {}),
            "transport": call.transport,
            "tool": call.tool,
            "args": dict(call.args),
            "rule": rule,
            "collections": list(collections),
            "spans": [list(span) for span in spans],
            "replaced": replaced,
            "text": text,
            "chars": chars,
            "sha256": digest,
            "left": ok.left,
            "used": {
                variant.replace("-", "_"): [used + chars, limit] for variant, used, limit in uses
            },
        }
        if call.query is not None:
            fields["query"] = call.query
        fields.update(extra)
        try:
            record = self._append("release", fields, call.now)
        except (LedgerError, OSError):
            # Not recorded, so not released: the caller gets no document text.
            return self._refuse(call, "UNAVAILABLE", "record")
        if approval is not None:
            self._approvals.pop((name, approval.ref), None)
        if call.tool == messages.TOOL_READ:
            self._pending.pop((name, str(call.args["ref"])), None)
        return Reply("OK", text, True, record.seq, client_seq)

    # -- approvals ----------------------------------------------------------------

    def _approval_in_force(self, name: str, ref: str, now: datetime) -> _Approval | None:
        """The approval the owner gave this client for this passage, if it still holds."""
        approval = self._approvals.get((name, ref))
        if approval is None:
            return None
        lifetime = timedelta(minutes=self._policy.approval.ttl_minutes)
        until = min(approval.valid_until, approval.given_at + lifetime)
        return approval if approval.given_at <= now < until else None

    def _approved(self, name: str, passage: _Passage, now: datetime) -> bool:
        """Whether the owner approved exactly this text for this client, and it still holds."""
        approval = self._approval_in_force(name, passage.ref, now)
        return approval is not None and approval.body_sha256 == passage.body_sha256

    def _ask_owner(self, call: _Call, ref: str, collections: Sequence[str]) -> Reply:
        """Refuse a read of a passage that waits for the owner, and put it before the owner.

        The passage is not measured with its own length: the answer would turn
        from ``NEEDS_APPROVAL`` to ``OVER_LIMIT`` at a point that tells a
        caller, who can steer what is left, how long a passage is that it was
        never given. It counts as one character. So it is refused for the
        limits only when one of them is used up, and is put before the owner
        otherwise, who is told whether it fits (``fits_limits`` of the
        preview). Once approved, its real length is measured.
        """
        name = call.client
        assert name is not None
        for variant, used, limit in self._uses(self._state(), name, call.now):
            if used + 1 > limit:
                return self._refuse(call, "OVER_LIMIT", variant, collections=collections)
        self._wait_for_owner(name, ref, call.now)
        return self._refuse(
            call,
            "NEEDS_APPROVAL",
            text=messages.needs_approval(ref, name),
            collections=collections,
        )

    def _wait_for_owner(self, name: str, ref: str, now: datetime) -> None:
        key = (name, ref)
        waiting = self._pending.pop(key, None)
        if waiting is None:
            waiting = _Waiting(now, now, 1)
        else:
            waiting = _Waiting(waiting.first_at, now, waiting.count + 1)
        self._pending[key] = waiting
        # Each client has its own share of the list, so that asking under one name
        # cannot push the passages of another off it.
        own = [entry for entry in self._pending if entry[0] == name]
        if len(own) > MAX_PENDING_PER_CLIENT:
            del self._pending[own[0]]
        while len(self._pending) > MAX_PENDING:
            # Full all the same: a client that holds the most gives up its oldest.
            held = Counter(client for client, _ in self._pending)
            most = max(held.values())
            del self._pending[next(entry for entry in self._pending if held[entry[0]] == most)]

    def _owner_passage(self, client: str, ref: str) -> _Passage:
        if not isinstance(client, str) or not self._is_served(client):
            raise UnknownPassageError(
                "unknown-client", "this session does not serve an app of that name"
            )
        parsed = _parse_ref(ref) if isinstance(ref, str) else None
        unknown = UnknownPassageError(
            "unknown-ref", "there is no such passage that this app could be given"
        )
        if parsed is None:
            raise unknown
        try:
            passage = self._passage(client, *parsed)
            if passage is not None:
                self._require_clean([passage.body])
        except _UnavailableError:
            raise unknown from None
        if passage is None:
            raise unknown
        return passage

    def pending(self) -> list[PendingApproval]:
        """The passages that were answered ``NEEDS_APPROVAL`` in this session and still wait.

        Oldest request first. A passage leaves the list when the owner approves
        or dismisses it, when it is released, and when its client, grant or
        document goes away. At most 50 are kept for one client; of those the
        one asked for longest ago is forgotten first, so no client can push
        another's passages off the list. At most 200 are kept in all; when
        that is reached, a client that holds the most gives up its oldest. The
        list is not recorded and is lost at lock.
        """
        with self._owner_side():
            return [
                PendingApproval(client, ref, waiting.first_at, waiting.last_at, waiting.count)
                for (client, ref), waiting in self._pending.items()
            ]

    def preview(self, client: str, ref: str) -> Preview:
        """Show the owner exactly what ``client`` would receive for ``ref``, and what it costs.

        ``body`` is the text between the marker lines of the result, with the
        listed strings already replaced; ``body_sha256`` is what
        :meth:`approve` takes. Raises :class:`UnknownPassageError` when the
        session does not serve the client or the passage does not exist for it.
        """
        with self._owner_side():
            try:
                passage = self._owner_passage(client, ref)
            except UnknownPassageError:
                # A passage is put on the waiting list without its document being read.
                # One that then cannot be shown cannot be approved, and does not wait.
                self._pending.pop((client, ref), None)
                raise
            state = self._state()
            now = self._clock()
            entry = self._policy.clients[client]
            uses = self._uses(state, client, now)
            result = messages.read_ok(
                doc_id=passage.doc_id,
                passage=passage.number,
                passages=passage.passages,
                collection=passage.collection,
                body=passage.body,
                tag="0" * 6,
                client=client,
                client_seq=state.client(client).release_seq + 1,
                left_before=self._left(uses),
                title=passage.shown_title,
            )
            chars = len(result.text)
            info = state.documents[passage.doc_id]
            last = self._last_calls.get(client)
            claimed = last[1] if last is not None else state.client(client).last_claimed
            return Preview(
                client=client,
                destination=entry.destination,
                hosted=entry.hosted,
                pool=entry.pool,
                claimed=_spoken(claimed),
                ref=passage.ref,
                collection=passage.collection,
                title=info.title,
                title_source=info.title_source,
                passage=passage.number,
                passages=passage.passages,
                mode=passage.mode,
                needs_approval=passage.needs_approval,
                fits_limits=all(used + chars <= limit for _, used, limit in uses),
                body=passage.body,
                body_sha256=passage.body_sha256,
                result_chars=chars,
                doc_chars=passage.doc_chars,
                doc_released=passage.doc_released,
                doc_after=passage.doc_after,
                client_day_used=uses[0][1],
                client_day_limit=uses[0][2],
            )

    def approve(self, client: str, ref: str, body_sha256: str) -> datetime:
        """Approve one release of one passage to one client; return until when it holds.

        ``body_sha256`` must be that of the text :meth:`preview` showed. If the
        passage would be released as other text now (the listed strings
        changed, say), nothing is approved and :class:`PassageChangedError` is
        raised. The ``approval`` record is appended first; if that fails,
        :class:`~wdva.gate.errors.LedgerError` is raised and no approval
        exists. The approval replaces an earlier one for the same client and
        passage, is used up by the first release under it, expires after the
        policy's lifetime for approvals, and is lost at lock. It does not lift
        any limit: an approved passage counts like every other release.
        """
        with self._owner_side():
            passage = self._owner_passage(client, ref)
            if not isinstance(body_sha256, str) or body_sha256 != passage.body_sha256:
                raise PassageChangedError(
                    "the text of this passage is no longer the text that was shown; "
                    "look at it again before releasing it"
                )
            now = self._clock()
            valid_until = now + timedelta(minutes=self._policy.approval.ttl_minutes)
            self._append(
                "approval",
                {
                    "event": "approved",
                    "client": client,
                    "ref": passage.ref,
                    "body_sha256": passage.body_sha256,
                    "chars": len(passage.body),
                    "valid_until": format_time(valid_until),
                },
                now,
            )
            self._approvals[(client, passage.ref)] = _Approval(
                passage.body_sha256, now, valid_until
            )
            self._pending.pop((client, passage.ref), None)
            return valid_until

    def dismiss(self, client: str, ref: str) -> None:
        """Take a passage off the waiting list, and withdraw an unused approval for it.

        Nothing is recorded. The client may ask again; the passage then waits
        again. A client or ref that is not text, or that names nothing on the
        list, changes nothing.
        """
        with self._owner_side():
            if not isinstance(client, str) or not isinstance(ref, str):
                return
            parsed = _parse_ref(ref)
            key = (client, f"{parsed[0]}.{parsed[1]}" if parsed is not None else ref)
            self._pending.pop(key, None)
            self._approvals.pop(key, None)

    def approvals(self) -> list[GrantedApproval]:
        """The approvals that were given and can still be used."""
        with self._owner_side():
            now = self._clock()
            lifetime = timedelta(minutes=self._policy.approval.ttl_minutes)
            granted: list[GrantedApproval] = []
            for key, approval in list(self._approvals.items()):
                until = min(approval.valid_until, approval.given_at + lifetime)
                if now >= until:
                    del self._approvals[key]
                elif approval.given_at <= now:
                    granted.append(GrantedApproval(key[0], key[1], until))
            return granted

    # -- the owner's changes ----------------------------------------------------

    def policy(self) -> Policy:
        """The policy in force."""
        with self._owner_side():
            return self._policy

    def set_policy(self, policy: Policy, *, by: str = "owner") -> Policy:
        """Put ``policy`` in force and return it as stored, with its new serial number.

        The ``policy`` record is appended first; if that fails nothing changes.
        Then the gate switches, before any other call is answered: grants,
        limits and pools; the search index, built again when the listed strings
        or a collection's titles changed; approvals and waiting passages of a
        client or grant that is gone are dropped. A newly registered name is
        served only if the gate serves every registered client or
        :meth:`set_served` names it. ``by`` says what made the change and is
        recorded.
        """
        if not isinstance(policy, Policy):
            raise TypeError("set_policy takes a Policy")
        with self._owner_side():
            label = _clean(str(by)[:_LABEL_CHARS], _LABEL_CHARS) or "owner"
            self._store.set_policy_json(policy.to_json(), by=label)
            self._sync()
            return self._policy

    def add_document(
        self,
        *,
        collection: str,
        title: str,
        text: str,
        title_source: str = "owner",
        source: Mapping[str, Any] | None = None,
        original: bytes | None = None,
        doc_id: str | None = None,
    ) -> DocumentInfo:
        """Add one document from its raw text, and make it searchable at once.

        The text is cleaned (links, markup and invisible characters go, and so
        do blank lines at its beginning and end) and cut into passages here;
        the title is cleaned to one line. The store then
        checks for duplicates and limits, seals the document (and ``original``,
        the bytes of the file it came from, when given) and records it. A
        collection that does not exist yet is created first, not marked for
        hosted clients and without titles, with a ``policy`` record of its own.

        Raises :class:`~wdva.gate.store.DocumentRefusedError` for text that is
        empty after cleaning, cannot be cleaned, is too large or is already in
        the store, :class:`ValueError` for a title or name that cannot be
        used, and :class:`TypeError` for text or a title that is not ``str``
        and for a ``source`` that is not a mapping, before anything is
        written. In all these cases no document was added.
        """
        if not isinstance(text, str) or not isinstance(title, str):
            raise TypeError("text and title must be str")
        if source is not None and not isinstance(source, Mapping):
            raise TypeError("source must be a mapping")
        if not is_name(collection):
            raise ValueError(
                "a collection name is 1 to 32 characters of a-z, 0-9, _ and -, "
                "starting with a letter or digit"
            )
        if len(text) > MAX_SOURCE_BYTES:
            raise DocumentRefusedError(REASON_TOO_LARGE, "more text than a file may hold")
        try:
            # Blank lines around the text carry nothing, and without them the same
            # text is recognised as the same document.
            cleaned = sanitize(text).strip("\n")
        except ValueError:
            raise DocumentRefusedError(REASON_UNREADABLE, "text does not settle") from None
        if not cleaned.strip():
            raise DocumentRefusedError(REASON_EMPTY)
        shown_title = _clean_title(title)
        spans = chunk_spans(cleaned, self._store.chunk_chars)
        terms = frozenset(tokens(cleaned))
        source_hash: str | None = None
        if original is not None:
            source_hash = hashlib.sha256(original).hexdigest()
        elif source is not None and isinstance(source.get("sha256"), str):
            source_hash = source["sha256"]
        with self._owner_side():
            # Asked first, so that a file that is refused leaves no empty collection.
            duplicate = self._store.find_duplicate(
                text_sha256=_sha256(cleaned), source_sha256=source_hash
            )
            if duplicate is not None:
                raise DocumentRefusedError(REASON_DUPLICATE, duplicate_of=duplicate.doc_id)
            if collection not in self._policy.collections:
                created = self._policy.with_collection(collection)
                self._store.set_policy_json(created.to_json(), by="add")
                self._sync()
            info = self._store.add_document(
                collection=collection,
                title=shown_title,
                text=cleaned,
                spans=spans,
                terms=terms,
                title_source=title_source,
                source=source,
                original=original,
                doc_id=doc_id,
                tokenize=tokens,
            )
            self._sync()
            return info

    def remove_document(self, doc_id: str) -> None:
        """Take a document out of the store and out of the index.

        The ``remove`` record comes first, then the files are deleted (deleted,
        not shredded). Approvals and waiting passages of the document go with
        it. Raises :class:`~wdva.gate.store.UnknownDocumentError` for an id the
        store does not list.
        """
        with self._owner_side():
            self._store.remove_document(doc_id)
            self._sync()

    def documents(self) -> list[DocumentInfo]:
        """The documents of the store, in the order they were added."""
        with self._owner_side():
            return self._store.documents()

    # -- the owner's meters -------------------------------------------------------

    def usage(self) -> Usage:
        """What every client and pool received, and how much of every document has left."""
        with self._owner_side():
            now = self._clock()
            state = self._state()
            policy = self._policy
            clients: dict[str, ClientUsage] = {}
            for name, entry in policy.clients.items():
                recorded = state.client(name)
                uses = self._uses(state, name, now)
                last_call, claimed = recorded.last_seen, recorded.last_claimed
                session = self._last_calls.get(name)
                # A record time is rounded up, so the call is put on the same scale
                # before the two are compared: a record must not look newer than a
                # call that came after it.
                if session is not None and (
                    last_call is None or record_time(session[0]) >= last_call
                ):
                    last_call, claimed = session[0], dict(session[1])
                said = claimed or {}
                clients[name] = ClientUsage(
                    client=name,
                    hosted=entry.hosted,
                    pool=entry.pool,
                    destination=entry.destination,
                    served=self._is_served(name),
                    chars_day=uses[0][1],
                    chars_30_days=uses[1][1],
                    searches_day=self._searches(state, name, now),
                    releases_day=recorded.releases.count(now, DAY),
                    refusals_day=self._refusals_day(name, now),
                    release_count=recorded.release_seq,
                    left=self._left(uses),
                    limits=policy.limits_for(name),
                    last_call=last_call,
                    claimed_name=_optional_text(said.get("name")),
                    claimed_version=_optional_text(said.get("version")),
                    protocol=_optional_text(said.get("protocol")),
                )
            pools = {
                pool: PoolUsage(
                    pool=pool,
                    chars_day=state.pool(pool).used(now, DAY),
                    chars_30_days=state.pool(pool).used(now, THIRTY_DAYS),
                    limits=policy.pools[pool],
                )
                for pool in POOLS
            }
            documents = {
                doc_id: DocumentUsage(
                    doc_id=doc_id,
                    chars=info.chars,
                    free_allowance=policy.per_document.free_allowance(info.chars),
                    released=MappingProxyType(
                        {pool: state.released_chars(pool, doc_id) for pool in POOLS}
                    ),
                    available=doc_id in self._where,
                )
                for doc_id, info in state.documents.items()
            }
            return Usage(
                time=now,
                clients=MappingProxyType(clients),
                pools=MappingProxyType(pools),
                documents=MappingProxyType(documents),
                refusals_day=sum(self._refusals_day(key, now) for key in self._refusals),
            )


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
