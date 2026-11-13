# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The app behind the interface: one vault, its lock state and everything the owner does to it.

:class:`GateService` answers every call of the interface server
(:mod:`wdva.gate.app.httpd`) for one store directory. It owns what a running
app has beyond the store itself:

* **Locked or unlocked.** Unlocking takes the store's lock, opens the store
  with the passphrase, builds the :class:`~wdva.gate.core.Gate` and starts to
  listen on the store's local socket for the programs that agent applications
  start. Locking undoes all of it. The vault also locks itself at a time that
  is fixed when it is unlocked.
* **Owner mode.** For ten minutes after the passphrase was last entered, the
  owner may make changes that let more leave: register an app, grant a
  collection, open a collection to hosted apps, show titles, raise a limit,
  answer both kinds of apps at once, answer an app again that was not being
  answered. After that such a change is refused until the passphrase is
  entered again. Changes that only take away, approvals of single passages,
  adding and removing documents and locking are never held up by this.
* **Which apps are answered.** Every registered app is, unless the owner
  paused it in this session. When hosted and local apps are both registered,
  only the hosted ones are answered until the owner has chosen. Which apps
  are answered is not part of the stored settings, so it is watched apart
  from them: whatever would make the session answer an app it does not answer
  now needs owner mode, also when it is the removal of another app.
* **The activity list.** One entry per record, written as a sentence, built
  from the record when the vault is unlocked and extended as records are
  appended. A call that asks what an app may use is listed as a question, not
  as a release: it hands out no document text. The exact text of a release
  is read from the record when it is asked for, and nowhere else is document
  text handed to the interface except the passage the owner is asked to
  approve.
* **Which view the owner uses.** Two words about the interface, kept in a
  small file beside the vault that is not sealed (``run/interface.json``):
  which of its two views the owner uses, and whether setting up was finished.
  The service alone writes it. It is no setting of the vault and leaves no
  entry in the record; a file that is missing or holds anything else counts
  as no file, and never keeps a vault from unlocking.

The methods take and return plain JSON values and raise
:class:`~wdva.gate.app.api.ServiceError`. A passphrase is used and forgotten:
it is never stored, logged or put into an answer. The strings the owner listed
for replacement are never returned by any method; only their number is.

Owner mode is a pause for thought, not a wall. Whoever holds the interface's
session and the passphrase can do everything, and a program that can read the
owner's keystrokes has the passphrase.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar, cast

from wdva import vault
from wdva._util import strict_json_loads
from wdva._version import __version__
from wdva.errors import WDVAError, WrongKeyError
from wdva.gate import brand, extract, hostcheck, hostconfig, messages, paths
from wdva.gate.app.api import JSONObject, ServiceError
from wdva.gate.app.startcheck import start_problem
from wdva.gate.core import (
    DAY,
    UNREGISTERED,
    Gate,
    PassageChangedError,
    UnknownPassageError,
    Usage,
)
from wdva.gate.errors import GateError, LedgerError, PolicyError, StoreBusyError
from wdva.gate.ledger import DocumentInfo, format_time, utc_now
from wdva.gate.link import LinkServer
from wdva.gate.policy import (
    BOTH,
    HOSTED,
    LIMIT_RANGES,
    LOCAL,
    MAX_APPROVAL_MINUTES,
    MAX_DOCUMENT_CHARS,
    MAX_REDACT_LITERALS,
    MAX_RESULT_CHARS,
    MODE_APPROVE,
    MODE_READ,
    POOL_RANGES,
    POOLS,
    REDACT_LITERAL_MAX_CHARS,
    REDACT_LITERAL_MIN_CHARS,
    TTL_RANGE,
    HostedGrantError,
    Policy,
    choice_needed,
    default_policy,
    is_name,
    kinds_served,
    served_set,
    widens,
)
from wdva.gate.store import (
    CHUNK_CHARS,
    REASON_DUPLICATE,
    REASON_TOO_LARGE,
    DocumentRefusedError,
    Store,
    StoreInfo,
    StoreLocationError,
    UnknownDocumentError,
    init_store,
    open_store,
    store_info,
    take_lock,
)
from wdva.gate.textproc import sanitize
from wdva.gate.types import Backend, Caller, Reply

__all__ = [
    "ACTIVITY_KINDS",
    "MAX_HOURS",
    "MIN_HOURS",
    "MIN_PASSPHRASE_CHARS",
    "NOT_REGISTERED",
    "OWNER_MODE",
    "VIEWS",
    "GateService",
    "connect_problem",
]

OWNER_MODE = timedelta(minutes=10)
"""How long after the passphrase was entered a change that lets more leave is accepted."""

MIN_PASSPHRASE_CHARS = 12
"""The shortest passphrase a new vault can be created with."""

MIN_HOURS = 0.02
MAX_HOURS = 168.0
"""The shortest and the longest time, in hours, a vault stays unlocked before it locks itself."""

NOT_REGISTERED = "(not registered)"
"""The app name under which calls are listed that were made under a name nobody answers."""

ACTIVITY_KINDS = ("release", "question", "refusal", "approval", "change", "session")
"""The kinds of entries in the activity list.

A ``question`` is a status call: an app asked what it may use and was told,
without any document text. It is not a release and is counted apart from them.
"""

VIEWS = ("basic", "pro")
"""The two views of the interface: what is used every day, and every setting."""

_SETUP = ("open", "done")
_INTERFACE_MAX_BYTES = 1024  # a larger file in the place of the interface file counts as none
_NO_INTERFACE_FILE = {"view": "pro", "setup": "done", "stored": False}
_DEFAULT_PAGE = 50
_MAX_PAGE = 200
_MAX_NAME_CHARS = 255
_FALLBACK_TITLE = "untitled document"
_INFO_SECONDS = 5.0  # how long the sizes of a locked vault are remembered
_TOOLS = {
    messages.TOOL_STATUS: "status",
    messages.TOOL_SEARCH: "search",
    messages.TOOL_READ: "read",
}
_MODE_WORDS = {MODE_READ: "can read", MODE_APPROVE: "ask me each time"}
_REF = re.compile(r"d-[0-9a-f]{12}\.[0-9]{1,5}")
_GONE = ("the gate is locked", "the store is closed")

_LIMIT_WORDS = {
    "chars_per_call": "characters in one result",
    "chars_per_day": "characters in 24 hours",
    "chars_per_30_days": "characters in 30 days",
    "searches_per_day": "searches in 24 hours",
    "calls_per_minute": "calls in a minute",
    "ttl_minutes": "minutes an approval stays valid",
}
_OVER_LIMIT_WORDS = {
    "client-day": "daily limit reached",
    "client-30-days": "30-day limit reached",
    "searches": "too many searches in 24 hours",
    "calls": "too many calls in a minute",
}
_LOCK_WORDS = {
    "lock-command": "you locked the vault",
    "timer": "you let the timer lock the vault",
    "interrupt": f"you stopped {brand.PRODUCT}, which locked the vault",
    "signal": f"you stopped {brand.PRODUCT}, which locked the vault",
}

_UNUSABLE_NAME = (
    "An application can only be connected under a name of a-z, 0-9 and -, "
    "starting with a letter. Register the app again under such a name."
)

_F = TypeVar("_F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Errors, in the words of the interface
# ---------------------------------------------------------------------------


def _invalid(message: str) -> ServiceError:
    return ServiceError(400, "invalid", message)


def _not_found(message: str) -> ServiceError:
    return ServiceError(404, "not_found", message)


def _locked() -> ServiceError:
    return ServiceError(409, "locked", "The vault is locked.")


def _unavailable(message: str) -> ServiceError:
    return ServiceError(503, "unavailable", message)


def _sentence(text: str) -> str:
    """``text`` as a sentence: a capital first, a full stop last."""
    text = text.strip()
    if not text:
        return "That cannot be done."
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _vault_words(text: str) -> str:
    """An engine message in the words of the interface, where the vault is never a "store"."""
    return text.replace(" store ", " vault ").replace(" store:", " vault:")


def _refused_document(error: DocumentRefusedError) -> ServiceError:
    if error.reason == REASON_DUPLICATE:
        return ServiceError(409, "duplicate", "this document is already in the vault")
    if error.reason == REASON_TOO_LARGE:
        return ServiceError(413, "too_large", "the file holds more text than a vault takes")
    if error.reason == "empty":
        return _invalid("the file holds no text")
    return _invalid("the text of the file cannot be read")


def _service_error(error: Exception, locked: bool) -> ServiceError | None:
    """The answer for a failure of the engine, or ``None`` for one that is a bug."""
    if isinstance(error, WrongKeyError):
        return ServiceError(403, "wrong_passphrase", "That passphrase did not open the vault.")
    if isinstance(error, HostedGrantError):
        return ServiceError(409, "conflict", _sentence(str(error)))
    if isinstance(error, PolicyError):
        return _invalid(_sentence(str(error)))
    if isinstance(error, PassageChangedError):
        return ServiceError(
            409, "changed", "The text changed since it was shown. Read it again before releasing."
        )
    if isinstance(error, UnknownPassageError):
        return _not_found("That passage is no longer there for this app.")
    if isinstance(error, UnknownDocumentError):
        return _not_found("The vault has no such document.")
    if isinstance(error, DocumentRefusedError):
        return _refused_document(error)
    if isinstance(error, StoreBusyError):
        return ServiceError(
            409, "busy", "The vault is open in another program. Lock it there first."
        )
    if isinstance(error, LedgerError):
        return _unavailable(
            "The record of this vault could not be read or written. Nothing was released."
        )
    if isinstance(error, GateError):
        if type(error) is GateError and (locked or str(error) in _GONE):
            return _locked()
        return _unavailable(_sentence(_vault_words(str(error))))
    if isinstance(error, WDVAError):
        return _unavailable("The vault cannot answer right now.")
    if isinstance(error, OSError):
        return _unavailable("The files of the vault could not be read or written.")
    return None


def _translated(method: _F) -> _F:
    """Turn every failure of the engine into the answer the interface is promised."""

    @functools.wraps(method)
    def call(self: GateService, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except ServiceError:
            raise
        except Exception as exc:
            answer = _service_error(exc, self._session is None)
            if answer is None:
                raise
            raise answer from None

    return cast(_F, call)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso(when: datetime | None) -> str | None:
    return None if when is None else format_time(when)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _whole(value: Any) -> int | None:
    return value if type(value) is int else None


def _listing(names: Iterable[str]) -> str:
    """Names as a phrase: ``home``, ``home and tax``, ``home, tax and work``."""
    items = list(names)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _count(number: int, one: str, many: str | None = None) -> str:
    return f"{number:,} {one if number == 1 else (many or one + 's')}"


def _spoken(claimed: Any) -> str | None:
    """What a program said it is, as ``name version``. Never verified."""
    said = _mapping(claimed)
    parts = [said.get("name"), said.get("version")]
    return " ".join(part for part in parts if isinstance(part, str) and part) or None


def connect_problem(status: Mapping[str, Any]) -> tuple[str, str, list[str]] | None:
    """Why no agent application can connect in this environment, or ``None`` when one can.

    ``status`` is what :func:`wdva.gate.hostconfig.sdk_status` returned. The answer is the
    code of the warning, what is wrong in words that follow "Apps", and the command that
    puts it right, if there is one. The warning of an unlocked vault and the start-up
    line of the app are both made from it.
    """
    if status.get("python") is False:
        return (
            "python_too_old",
            "cannot connect: the program they start needs Python 3.11 or newer, and "
            f"{brand.PRODUCT} runs on an older one.",
            [],
        )
    if status.get("sdk") is False:
        return (
            "no_sdk",
            f"cannot connect yet: the MCP SDK is not installed where {brand.PRODUCT} runs.",
            [hostconfig.INSTALL_LINE],
        )
    return None


def _read_interface(path: Path) -> dict[str, Any]:
    """What the interface file at ``path`` says, or what holds for a vault without one.

    The file counts only when it is a regular file of at most a kibibyte that
    holds exactly the two keys with values that exist. Anything else in its
    place (a link, a folder, other JSON, something that is not JSON) is the
    same as no file. Never raises, and never waits: what is not a regular file
    is not opened.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return dict(_NO_INTERFACE_FILE)
        flags = os.O_RDONLY
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC"):
            flags |= getattr(os, name, 0)
        descriptor = os.open(path, flags)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size > _INTERFACE_MAX_BYTES:
                return dict(_NO_INTERFACE_FILE)
            raw = os.read(descriptor, _INTERFACE_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _INTERFACE_MAX_BYTES:
            return dict(_NO_INTERFACE_FILE)
        held = strict_json_loads(raw)
    except (OSError, ValueError):
        return dict(_NO_INTERFACE_FILE)
    if (
        not isinstance(held, dict)
        or set(held) != {"view", "setup"}
        or held["view"] not in VIEWS
        or held["setup"] not in _SETUP
    ):
        return dict(_NO_INTERFACE_FILE)
    return {"view": held["view"], "setup": held["setup"], "stored": True}


def _write_interface(path: Path, view: str, setup: str) -> None:
    """Write the interface file: to a temporary file beside it, then one rename.

    Raises :class:`OSError` when that cannot be done; the file that was there
    is then still there. Only ever called for the ``run/`` folder of a vault
    that exists.
    """
    if view not in VIEWS or setup not in _SETUP:
        raise ValueError("the interface file holds a view and whether setup is done")
    content = json.dumps({"view": view, "setup": setup}).encode("ascii") + b"\n"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )  # made with mode 0600
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)  # a link in its place is replaced, not followed
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _kdf(scrypt_n: int | None) -> str:
    cost = "N unknown"
    if scrypt_n is not None and scrypt_n > 0 and scrypt_n & (scrypt_n - 1) == 0:
        cost = f"N=2^{scrypt_n.bit_length() - 1}"
    return f"scrypt ({cost}, r={vault.SCRYPT_R}, p={vault.SCRYPT_P})"


# ---------------------------------------------------------------------------
# The activity list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Document:
    title: str
    collection: str
    passages: int


def _policy_changes(
    old: Mapping[str, Any] | None, new: Mapping[str, Any]
) -> list[tuple[str, tuple[str, ...]]]:
    """What changed between two policies, as sentences about the owner, with their collections.

    The strings listed for replacement are counted, never quoted.
    """
    if old is None:
        return [("you started with the default limits: no app is answered yet", ())]
    changes: list[tuple[str, tuple[str, ...]]] = []
    old_collections = _mapping(old.get("collections"))
    new_collections = _mapping(new.get("collections"))
    for name, raw in new_collections.items():
        entry, before = _mapping(raw), _mapping(old_collections.get(name))
        if name not in old_collections:
            changes.append((f"you created the collection {name}", (name,)))
        if entry.get("allow_hosted") and not before.get("allow_hosted"):
            changes.append((f"you opened {name} to hosted apps", (name,)))
        elif before.get("allow_hosted") and not entry.get("allow_hosted"):
            changes.append((f"you closed {name} to hosted apps", (name,)))
        if entry.get("show_titles") and not before.get("show_titles"):
            changes.append((f"you let apps see the titles in {name}", (name,)))
        elif before.get("show_titles") and not entry.get("show_titles"):
            changes.append((f"you hid the titles in {name} from apps", (name,)))
        if name in old_collections and entry.get("note") != before.get("note"):
            changes.append((f"you changed the note of {name}", (name,)))
    for name in old_collections:
        if name not in new_collections:
            changes.append((f"you removed the collection {name}", (name,)))

    old_clients = _mapping(old.get("clients"))
    new_clients = _mapping(new.get("clients"))
    for name, raw in new_clients.items():
        entry = _mapping(raw)
        if name not in old_clients:
            changes.append((f"you added the app {name}", ()))
        before = _mapping(old_clients.get(name))
        if name in old_clients and (
            entry.get("hosted") != before.get("hosted")
            or entry.get("destination") != before.get("destination")
        ):
            changes.append((f"you changed where {name} sends text", ()))
        grants, earlier = _mapping(entry.get("grants")), _mapping(before.get("grants"))
        for collection, grant in grants.items():
            mode = _mapping(grant).get("mode")
            if mode != _mapping(earlier.get(collection)).get("mode"):
                words = _MODE_WORDS.get(str(mode), str(mode))
                changes.append((f"you granted {name}: {collection} ({words})", (collection,)))
        for collection in earlier:
            if collection not in grants and collection in new_collections:
                changes.append((f"you took {collection} away from {name}", (collection,)))
        if name in old_clients and entry.get("limits") != before.get("limits"):
            changes.append((f"you changed the limits of {name}", ()))
    for name in old_clients:
        if name not in new_clients:
            changes.append((f"you removed the app {name}", ()))

    if any(old.get(key) != new.get(key) for key in ("defaults", "pools", "per_document")):
        changes.append(("you changed the limits of the vault", ()))
    if old.get("approval") != new.get("approval"):
        changes.append(("you changed how long an approval stays valid", ()))
    listed, before_listed = new.get("redact_literals"), old.get("redact_literals")
    if listed != before_listed:
        added = len(_names(listed)) - len(_names(before_listed))
        if added == 1:
            changes.append(("you added a word that is replaced", ()))
        elif added > 1:
            changes.append((f"you added {added:,} words that are replaced", ()))
        else:
            changes.append(("you changed the words that are replaced", ()))
    return changes or [("you saved the settings without a change", ())]


class _Feed:
    """The activity list of one session: one entry per record, in record order.

    Entries are made from the records alone. The feed follows the documents
    and the policy through the records it is given, so that an entry can name
    a document that was removed since, or an app as it was registered then.
    It has a lock of its own, which is never held while anything else is
    called: records arrive from inside the gate.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: list[dict[str, Any]] = []
        self._documents: dict[str, _Document] = {}
        self._policy: Mapping[str, Any] | None = None
        self._newest_refusal: dict[tuple[str, str], dict[str, Any]] = {}
        self._own_call_pending: set[tuple[str, str]] = set()

    # -- reading ----------------------------------------------------------------

    def find(self, seq: int) -> dict[str, Any] | None:
        """The entry of record ``seq``. Call with the lock held."""
        if not self.items:
            return None
        index = seq - int(self.items[0]["seq"])
        if 0 <= index < len(self.items) and self.items[index]["seq"] == seq:
            return self.items[index]
        return next((item for item in self.items if item["seq"] == seq), None)

    def document(self, doc_id: str) -> _Document | None:
        """What the records say about a document, also one that was removed since."""
        with self.lock:
            return self._documents.get(doc_id)

    # -- writing ----------------------------------------------------------------

    def add(self, data: Mapping[str, Any]) -> None:
        """Take in one record. Never raises: a record that cannot be told is still listed."""
        with self.lock:
            try:
                item = self._entry(data)
            except Exception:
                item = self._item(data, kind="change", summary="you made a change")
            self.items.append(item)

    def loaded(self) -> None:
        """Say that the records of earlier sessions are in: what comes now, comes live."""
        with self.lock:
            # No call of this session stands behind an entry of an earlier one.
            self._own_call_pending.clear()

    def refused(self, key: tuple[str, str], when: str) -> None:
        """Count one refused call that the gate did not give a record of its own."""
        with self.lock:
            item = self._newest_refusal.get(key)
            if item is None:
                return
            if key in self._own_call_pending:
                self._own_call_pending.discard(key)  # the call the record itself stands for
                return
            item["count"] += 1
            item["last_time"] = when

    # -- one entry per kind of record ---------------------------------------------

    @staticmethod
    def _item(
        data: Mapping[str, Any],
        *,
        kind: str,
        summary: str,
        app: str | None = None,
        destination: str | None = None,
        hosted: bool | None = None,
        tool: str | None = None,
        collections: Iterable[str] = (),
        ref: str | None = None,
        characters: int = 0,
        code: str | None = None,
        variant: str | None = None,
        approved: bool = False,
        details: Iterable[tuple[str, str]] = (),
    ) -> dict[str, Any]:
        return {
            "seq": _whole(data.get("seq")) or 0,
            "time": data.get("time") if isinstance(data.get("time"), str) else None,
            "kind": kind,
            "app": app,
            "destination": destination,
            "hosted": hosted,
            "tool": tool,
            "summary": summary,
            "collections": list(collections),
            "ref": ref,
            "characters": characters,
            "code": code,
            "variant": variant,
            "approved": approved,
            "count": 1,
            "_details": [[label, value] for label, value in details],
        }

    def _entry(self, data: Mapping[str, Any]) -> dict[str, Any]:
        kind = data.get("kind")
        if kind == "release":
            return self._release(data)
        if kind == "denial":
            return self._denial(data)
        if kind == "status":
            return self._status(data)
        if kind == "approval":
            return self._approval(data)
        if kind == "policy":
            return self._policy_change(data)
        if kind == "add":
            return self._added(data)
        if kind == "remove":
            return self._removed(data)
        if kind == "unlock":
            return self._unlocked(data)
        if kind == "lock":
            return self._locked(data)
        if kind == "init":
            return self._item(data, kind="session", summary="you created the vault")
        return self._item(data, kind="change", summary="you made a change")

    def _app(self, name: str) -> tuple[str | None, bool | None]:
        """Where an app sends text and whether it is hosted, as registered at this point."""
        entry = _mapping(_mapping(_mapping(self._policy).get("clients")).get(name))
        destination, hosted = entry.get("destination"), entry.get("hosted")
        return (
            destination if isinstance(destination, str) else None,
            hosted if isinstance(hosted, bool) else None,
        )

    def _passage(
        self, ref: str, number: Any = None, total: Any = None
    ) -> tuple[str, list[tuple[str, str]], list[str]]:
        """A passage in words, the pairs that describe it, and its collection."""
        doc_id, _, tail = ref.partition(".")
        known = self._documents.get(doc_id)
        passage = _whole(number) or (int(tail) if tail.isdigit() else 0)
        passages = _whole(total) or (known.passages if known is not None else 0)
        of = f" of {passages:,}" if passages else ""
        title = known.title if known is not None else "a document"
        place = f" in {known.collection}" if known is not None else ""
        details = [("document", title)] if known is not None else []
        details.append(("passage", f"{passage}{f' of {passages}' if passages else ''}"))
        collections = [known.collection] if known is not None else []
        return f"passage {passage:,}{of} of {title}{place}", details, collections

    def _release(self, data: Mapping[str, Any]) -> dict[str, Any]:
        client = str(data.get("client"))
        tool = _TOOLS.get(str(data.get("tool")))
        collections = _names(data.get("collections"))
        destination, hosted = self._app(client)
        if isinstance(data.get("destination"), str):
            destination = data["destination"]
        if isinstance(data.get("hosted"), bool):
            hosted = data["hosted"]
        approved = data.get("rule") == "approval"
        ref: str | None = None
        details: list[tuple[str, str]] = []
        if tool == "search":
            shown = _whole(data.get("lines"))
            text = str(data.get("text") or "")
            where = _listing(collections) or "without a collection it may see"
            if shown is None:
                summary = f"searched {where}"
            elif shown:
                summary = f"searched {where}: {_count(shown, 'result')}"
                details.append(("results shown", f"{shown:,}"))
            elif text.startswith("OK: no matches"):
                summary = f"searched {where}: no results"
            else:
                summary = f"searched {where}: results found, none shown"
        else:
            raw = _mapping(data.get("args")).get("ref")
            ref = raw if isinstance(raw, str) and _REF.fullmatch(raw) else None
            if ref is None:
                summary = "read a passage"
            else:
                words, details, _ = self._passage(ref, data.get("passage"), data.get("passages"))
                summary = f"read {words}"
        replaced = _whole(data.get("replaced"))
        if replaced:
            details.append(("words replaced", f"{replaced:,}"))
        left = _whole(data.get("left"))
        if left is not None:
            details.append(("left for this app then", f"{left:,} characters"))
        return self._item(
            data,
            kind="release",
            summary=summary,
            app=client,
            destination=destination,
            hosted=hosted,
            tool=tool,
            collections=collections,
            ref=ref,
            characters=_whole(data.get("chars")) or 0,
            code="OK",
            approved=approved,
            details=details,
        )

    def _denial(self, data: Mapping[str, Any]) -> dict[str, Any]:
        client = str(data.get("client"))
        code = str(data.get("code"))
        variant = str(data.get("variant") or "")
        tool = _TOOLS.get(str(data.get("tool")))
        collections = _names(data.get("collections"))
        registered = client != UNREGISTERED
        destination, hosted = self._app(client) if registered else (None, None)
        raw = _mapping(data.get("args")).get("ref")
        ref = raw if tool == "read" and isinstance(raw, str) and _REF.fullmatch(raw) else None
        details: list[tuple[str, str]] = []
        if code == "NEEDS_APPROVAL":
            if ref is None:
                summary = "needs your approval for a passage"
            else:
                words, details, where = self._passage(ref)
                summary = f"needs your approval for {words}"
                collections = collections or where
        elif code == "OVER_LIMIT":
            reason = _OVER_LIMIT_WORDS.get(variant)
            if reason is None and variant in ("pool-day", "pool-30-days"):
                kind = (
                    "apps of its kind"
                    if hosted is None
                    else f"{'hosted' if hosted else 'local'} apps"
                )
                period = "daily" if variant == "pool-day" else "30-day"
                reason = f"{period} limit of all {kind} together reached"
            summary = f"was refused: {reason or 'a limit was reached'}"
        elif code == "NOT_ALLOWED":
            summary = "was refused: no app is answered under that name"
        elif code == "NOT_FOUND":
            what = {"read": "no such passage", "search": "no such collection"}.get(
                tool or "", "nothing there"
            )
            summary = f"was refused: {what} for this app"
        elif code == "BAD_REQUEST":
            summary = "was refused: the request could not be read"
        elif code == "UNAVAILABLE":
            summary = "got no answer: the vault could not answer at that moment"
            if variant:
                details.append(("what failed", variant))
        else:
            summary = "was refused"
        item = self._item(
            data,
            kind="refusal",
            summary=summary,
            app=client if registered else NOT_REGISTERED,
            destination=destination,
            hosted=hosted,
            tool=tool,
            collections=collections,
            ref=ref,
            code=code,
            variant=variant or None,
            details=details,
        )
        key = (client, code)
        before = self._newest_refusal.get(key)
        folded = _whole(data.get("folded_before")) or 0
        if before is not None:
            # The record says how many calls the entry before it stood for.
            before["count"] = max(before["count"], 1 + folded)
        self._newest_refusal[key] = item
        self._own_call_pending.add(key)
        return item

    def _status(self, data: Mapping[str, Any]) -> dict[str, Any]:
        client = str(data.get("client"))
        destination, hosted = self._app(client)
        return self._item(
            data,
            kind="question",
            summary="asked what it may use (no document text)",
            app=client,
            destination=destination,
            hosted=hosted,
            tool="status",
            code="OK",
            details=[("answered with", "its collections and limits, no document text")],
        )

    def _approval(self, data: Mapping[str, Any]) -> dict[str, Any]:
        client = str(data.get("client"))
        ref = data.get("ref") if isinstance(data.get("ref"), str) else None
        details: list[tuple[str, str]] = []
        collections: list[str] = []
        words = "a passage"
        if ref is not None:
            words, details, collections = self._passage(ref)
        details.append(("for", client))
        chars = _whole(data.get("chars"))
        if chars is not None:
            details.append(("characters of the passage", f"{chars:,}"))
        if isinstance(data.get("valid_until"), str):
            details.append(("valid until", str(data["valid_until"])))
        return self._item(
            data,
            kind="approval",
            summary=f"you approved {words} once for {client}",
            collections=collections,
            ref=ref,
            details=details,
        )

    def _policy_change(self, data: Mapping[str, Any]) -> dict[str, Any]:
        new = _mapping(data.get("policy"))
        changes = _policy_changes(self._policy, new)
        self._policy = new
        summary = changes[0][0]
        if len(changes) > 1:
            summary += f" and made {_count(len(changes) - 1, 'more change')}"
        collections: list[str] = []
        for _, names in changes:
            collections.extend(name for name in names if name not in collections)
        details: list[tuple[str, str]] = []
        if len(changes) > 1:
            details.append(("changes", "; ".join(text[4:] for text, _ in changes)))
        serial = _whole(data.get("serial"))
        if serial is not None:
            details.append(("settings, version", f"{serial:,}"))
        return self._item(
            data, kind="change", summary=summary, collections=collections, details=details
        )

    def _added(self, data: Mapping[str, Any]) -> dict[str, Any]:
        doc_id = str(data.get("doc_id"))
        title = str(data.get("title") or "a document")
        collection = str(data.get("collection"))
        passages = _whole(data.get("passages")) or 0
        self._documents[doc_id] = _Document(title, collection, passages)
        details = [("document", title), ("passages", f"{passages:,}")]
        chars = _whole(data.get("chars"))
        if chars is not None:
            details.append(("characters", f"{chars:,}"))
        # What the record knows: whether the bytes of the added file were sealed with the
        # text. Where that file came from, and whether it is still there, it does not know.
        kept = "sealed" if data.get("original") else "none"
        details.append(("copy of the file in the vault", kept))
        return self._item(
            data,
            kind="change",
            summary=f"you added {title} to {collection} ({_count(passages, 'passage')})",
            collections=[collection],
            details=details,
        )

    def _removed(self, data: Mapping[str, Any]) -> dict[str, Any]:
        known = self._documents.get(str(data.get("doc_id")))
        if known is None:
            return self._item(data, kind="change", summary="you removed a document")
        return self._item(
            data,
            kind="change",
            summary=f"you removed {known.title} from {known.collection}",
            collections=[known.collection],
            details=[("document", known.title)],
        )

    def _unlocked(self, data: Mapping[str, Any]) -> dict[str, Any]:
        served = _names(data.get("served"))
        details = [("answering", _listing(served) or "no app")]
        if isinstance(data.get("auto_lock"), str):
            details.append(("locks itself at", str(data["auto_lock"])))
        if data.get("previous_session") == "unclean":
            details.append(("the session before", "ended without locking"))
        return self._item(data, kind="session", summary="you unlocked the vault", details=details)

    def _locked(self, data: Mapping[str, Any]) -> dict[str, Any]:
        details: list[tuple[str, str]] = []
        folded = _whole(data.get("folded_refusals"))
        if folded:
            details.append(("refused calls not listed one by one", f"{folded:,}"))
        summary = _LOCK_WORDS.get(str(data.get("reason")), "you locked the vault")
        return self._item(data, kind="session", summary=summary, details=details)


def _public(item: Mapping[str, Any]) -> dict[str, Any]:
    """An entry as the interface gets it."""
    return {
        key: (list(value) if isinstance(value, list) else value)
        for key, value in item.items()
        if not key.startswith("_")
    }


# ---------------------------------------------------------------------------
# A session, and what agent applications reach
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    """Everything that exists only while the vault is unlocked."""

    store: Store
    gate: Gate
    link: LinkServer
    feed: _Feed
    unlocked_at: datetime
    locks_at: datetime
    owner_until: datetime
    verified_at: datetime
    verified: bool = True
    kinds: str | None = None  # the owner's choice of which kinds of apps are answered
    paused: frozenset[str] = frozenset()
    timer: threading.Timer | None = None
    sdk: Mapping[str, Any] = field(default_factory=dict)
    seen: tuple[Any, ...] = ()  # what the last look at the waiting list found


class _AgentSide:
    """What agent applications reach: the gate of the session, or the answer of a locked vault.

    It also notices the refused calls, so that the activity list can say how
    many calls an entry stands for. It decides nothing.
    """

    def __init__(self, service: GateService) -> None:
        self._service = service

    def status(self, caller: Caller) -> Reply:
        """Pass a status call to the gate."""
        return self._call(caller, lambda gate: gate.status(caller))

    def search(self, caller: Caller, query: Any, collection: Any = "", limit: Any = 5) -> Reply:
        """Pass a search to the gate."""
        return self._call(caller, lambda gate: gate.search(caller, query, collection, limit))

    def read(self, caller: Caller, ref: Any) -> Reply:
        """Pass a read to the gate."""
        return self._call(caller, lambda gate: gate.read(caller, ref))

    def _call(self, caller: Caller, ask: Callable[[Gate], Reply]) -> Reply:
        session = self._service._session
        if session is None:
            return Reply("LOCKED", messages.locked(), False)
        reply = ask(session.gate)
        if reply.code not in ("OK", "LOCKED"):
            self._service._refused(session, caller, reply.code)
        return reply


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class GateService:
    """The app for one store directory: every call of the interface, and the lock state.

    ``store_dir`` is where the vault is or will be created. ``demo`` says that
    the vault is a demo's: the state reports it. ``demo_passphrase`` is the
    passphrase of a demo vault, which the state then carries so that the
    interface can show it; it is refused without ``demo``, and the state of a
    real vault never has that field. ``lock_after_hours`` is how long the vault
    stays unlocked when an unlock does not say. ``clock`` supplies every time
    (of records, limits, owner mode and the moment the vault locks itself);
    ``scrypt_n`` is the cost of the passphrase stretch of a vault created
    here (``None``: the default).

    ``view`` is the view the app was started with (``--view``), ``"basic"`` or
    ``"pro"``; ``None`` when none was named. A vault created here starts in
    it, else in ``"basic"``. For a vault that exists already a named view is
    stored at once; without one nothing is written at start, except for a
    demo, whose vault is marked as set up. ``launch_command`` is the command
    line that starts this installation again and ``port_fixed`` whether it
    names a port; the state passes both on. ``demo_again`` is what
    :meth:`demo_again` calls: the played app of a demo, told to ask once more.
    It is refused without ``demo``.

    The methods may be called from several threads at once. :attr:`backend`
    is what answers agent applications; it stays the same object across
    locking and unlocking.
    """

    def __init__(
        self,
        store_dir: str | os.PathLike[str],
        *,
        demo: bool = False,
        demo_passphrase: str | None = None,
        lock_after_hours: float = 8.0,
        clock: Callable[[], datetime] | None = None,
        scrypt_n: int | None = None,
        view: str | None = None,
        launch_command: str | None = None,
        port_fixed: bool = False,
        demo_again: Callable[[], None] | None = None,
    ) -> None:
        hours = float(lock_after_hours)
        if not MIN_HOURS <= hours <= MAX_HOURS:
            raise ValueError(f"lock_after_hours must be between {MIN_HOURS} and {MAX_HOURS:g}")
        if demo_passphrase is not None and not demo:
            raise ValueError("only a demo vault has a passphrase that is shown")
        if demo_again is not None and not demo:
            raise ValueError("only a demo has a played app that can be told to ask again")
        if view is not None and view not in VIEWS:
            raise ValueError(f"view must be one of {', '.join(VIEWS)}")
        self._path = Path(os.path.abspath(os.path.expanduser(os.fspath(store_dir))))
        self._demo = bool(demo)
        self._demo_passphrase = demo_passphrase
        self._default_hours = hours
        self._clock: Callable[[], datetime] = clock or utc_now
        self._scrypt_n = scrypt_n
        self._lock = threading.RLock()  # the session and every change made through it
        self._checking = threading.Lock()  # one passphrase is stretched at a time
        self._session: _Session | None = None
        self._opening = False
        self._cursor_lock = threading.Lock()
        self._cursor = 1
        self._info: tuple[Any, StoreInfo] | None = None
        self._start_checked: tuple[str | None] | None = None  # the one result of the start check
        self._backend = _AgentSide(self)
        self._start_view = view
        self._launch = {"command": launch_command, "port_fixed": bool(port_fixed)}
        self._demo_again = demo_again
        self._interface_lock = threading.Lock()  # one change of the interface file at a time
        self._interface = _read_interface(paths.interface_path(self._path))
        self._interface_at_start()

    # -- what others may ask --------------------------------------------------------

    @property
    def path(self) -> Path:
        """The store directory, as an absolute path."""
        return self._path

    @property
    def backend(self) -> Backend:
        """What answers the three tool calls: the gate while unlocked, ``LOCKED`` otherwise."""
        return self._backend

    @property
    def unlocked(self) -> bool:
        """Whether the vault is unlocked at this moment."""
        return self._current() is not None

    def close(self, reason: str = "close") -> None:
        """Lock the vault, if it is unlocked, because the app is stopping."""
        session = self._session
        if session is not None:
            self._end(session, reason)

    # -- the session ----------------------------------------------------------------

    def _bump(self) -> None:
        with self._cursor_lock:
            self._cursor += 1

    def _current(self) -> _Session | None:
        """The session, after ending one whose time has passed."""
        session = self._session
        if session is not None and session.gate.closed:
            self._end(session, "timer")
            return None
        return session

    def _unlocked(self) -> _Session:
        session = self._current()
        if session is None:
            raise _locked()
        return session

    def _owner(self, session: _Session) -> None:
        """Refuse a change that lets more leave unless the passphrase was entered recently."""
        if self._clock() >= session.owner_until:
            raise ServiceError(
                403, "reauth_required", "Enter your passphrase again to make this change."
            )

    def _end(self, session: _Session, reason: str) -> None:
        """Lock: stop answering apps, close the gate (which records it), close the store."""
        with self._lock:
            if self._session is not session:
                return
            self._session = None
            self._info = None
            if session.timer is not None:
                session.timer.cancel()
            session.link.stop()
            # If the gate cannot record the lock it is locked all the same, and the next
            # unlock reports that this session did not end properly.
            with contextlib.suppress(WDVAError, OSError):
                session.gate.close(reason)
            session.store.close()
        self._bump()

    def _timer_fired(self, session: _Session) -> None:
        with contextlib.suppress(Exception):  # a timer thread has nobody to report to
            self._end(session, "timer")

    def _begin(self, store: Store, hours: float) -> _Session:
        """Make an open store the unlocked vault: verify, build the gate, listen for apps."""
        feed = _Feed()

        def on_record(data: Mapping[str, Any]) -> None:
            feed.add(data)
            self._bump()

        try:
            try:
                store.ledger.replay()  # verifies the whole chain
            except LedgerError as exc:
                where = f" at entry #{exc.seq:,}" if exc.seq is not None else ""
                raise _unavailable(
                    f"The record of this vault does not verify{where}. Nothing was unlocked."
                ) from None
            try:
                policy = Policy.from_json(store.policy_json())
            except PolicyError:
                raise _unavailable(
                    "The settings stored in this vault cannot be read. Nothing was unlocked."
                ) from None
            for record in store.ledger.records():
                feed.add(record.data)
            feed.loaded()
            locks_at = self._clock() + timedelta(hours=hours)
            gate = Gate(
                store,
                served=served_set(policy),
                clock=self._clock,
                on_record=on_record,
                lock_at=locks_at,
            )
        except BaseException:
            store.close()
            raise
        link = LinkServer(self._backend, paths.socket_path(store.path), info=self._ping)
        try:
            link.start()
        except BaseException:
            try:
                gate.close("close")
            finally:
                store.close()
            raise
        now = gate.started.unlocked_at
        session = _Session(
            store=store,
            gate=gate,
            link=link,
            feed=feed,
            unlocked_at=now,
            locks_at=locks_at,
            owner_until=now + OWNER_MODE,
            verified_at=now,
            sdk=hostconfig.sdk_status(),
        )
        timer = threading.Timer(hours * 3600.0, self._timer_fired, args=(session,))
        timer.daemon = True
        session.timer = timer
        with self._interface_lock:  # read at each unlock; whatever it holds, the vault unlocks
            self._interface = _read_interface(paths.interface_path(self._path))
        with self._lock:
            self._session = session
            self._info = None
        timer.start()
        self._bump()
        return session

    def _claim_opening(self) -> None:
        with self._lock:
            if self._opening:
                raise ServiceError(409, "busy", "The vault is being unlocked right now.")
            self._opening = True

    def _ping(self) -> dict[str, Any]:
        """What a program that only asks whether the app is there is told. No document text."""
        session = self._session
        return {
            "app": brand.PRODUCT,
            "version": __version__,
            "pid": os.getpid(),
            "locks_at": None if session is None else _iso(session.locks_at),
        }

    def _refused(self, session: _Session, caller: Caller, code: str) -> None:
        """Note a call the gate refused, for the count of its entry in the activity list."""
        with contextlib.suppress(Exception):  # counting must never change an answer
            name = getattr(caller, "client", None)
            key = name if isinstance(name, str) and name in session.gate.served() else UNREGISTERED
            session.feed.refused((key, code), format_time(self._clock()))
        self._bump()

    @staticmethod
    def _serving(
        policy: Policy, kinds: str | None, paused: Iterable[str]
    ) -> tuple[str | None, frozenset[str], tuple[str, ...]]:
        """The choice of kinds and the pauses that hold under ``policy``, and who is answered."""
        if not (policy.client_names(True) and policy.client_names(False)):
            kinds = None  # one kind only: there is nothing to choose between
        held = frozenset(name for name in paused if name in policy.clients)
        return kinds, held, served_set(policy, kinds, held)

    def _answers_more(
        self, session: _Session, policy: Policy, kinds: str | None, paused: Iterable[str]
    ) -> bool:
        """Whether an app would be answered that is not answered now.

        That lets more leave, however it comes about: an app is no longer
        paused, another kind of app is chosen, or the last app of the kind
        that was answered is removed and the other kind takes its place.
        """
        answered = self._serving(policy, kinds, paused)[2]
        return not set(answered) <= set(session.gate.served())

    def _apply_serving(self, session: _Session, policy: Policy) -> None:
        """Tell the gate which names are answered, after the policy or the choice changed."""
        session.kinds, session.paused, answered = self._serving(
            policy, session.kinds, session.paused
        )
        session.gate.set_served(answered)

    def _change(self, session: _Session, new: Policy) -> Policy:
        """Put a changed policy in force. One that lets more leave needs owner mode."""
        old = session.gate.policy()
        if new.with_serial(old.serial) == old:
            return old
        if widens(old, new) or self._answers_more(session, new, session.kinds, session.paused):
            self._owner(session)
        stored = session.gate.set_policy(new)
        self._apply_serving(session, stored)
        return stored

    # -- which view the owner uses ----------------------------------------------------

    def _keep_interface(self, view: str, setup: str) -> None:
        """Write the interface file and read it again. :class:`OSError` when it cannot be kept."""
        path = paths.interface_path(self._path)
        with self._interface_lock:
            try:
                _write_interface(path, view, setup)
            finally:
                self._interface = _read_interface(path)
        self._bump()

    def _interface_at_start(self) -> None:
        """What a start writes for a vault that exists: the view it named, and a demo's setup.

        Without a view named on the command line nothing is written for a real
        vault. Where it cannot be written the app starts all the same, with
        what the file says.
        """
        if self._start_view is None and not self._demo:
            return
        if not store_info(self._path).exists:
            return
        view = self._start_view or ("basic" if self._demo else self._interface["view"])
        setup = "done" if self._demo else self._interface["setup"]
        with contextlib.suppress(OSError):
            self._keep_interface(view, setup)

    @_translated
    def set_interface(self, *, view: str | None = None, setup: str | None = None) -> JSONObject:
        """Keep which view the owner uses, or that setting up is finished; return the state.

        Works on a locked vault too, and needs no owner mode: it lets nothing
        leave and changes nothing that is sealed. ``setup`` can only be set to
        ``"done"``. Nothing is written into the record.
        """
        if view is not None and view not in VIEWS:
            raise _invalid('The view is "basic" or "pro".')
        if setup is not None and setup != "done":
            raise _invalid('Setting up can only be marked "done".')
        if view is None and setup is None:
            raise _invalid("Say which view to keep, or that setting up is done.")
        if not store_info(self._path).exists:
            raise _not_found("There is no vault here yet.")
        current = self._interface
        try:
            self._keep_interface(view or current["view"], setup or current["setup"])
        except OSError as exc:
            reason = exc.strerror or "the file could not be written"
            raise _unavailable(
                f"The choice could not be kept: {reason[0].lower()}{reason[1:]}."
            ) from None
        return self.state()

    # -- state ------------------------------------------------------------------------

    def _store_info(self, session: _Session | None) -> StoreInfo:
        """What the directory shows without the key; remembered while nothing was appended."""
        if session is not None:
            key: Any = ("unlocked", session.gate.head())
        else:
            key = ("locked", int(time.monotonic() / _INFO_SECONDS))
        cached = self._info
        if cached is not None and cached[0] == key:
            return cached[1]
        info = store_info(self._path)
        self._info = (key, info)
        return info

    @_translated
    def state(self) -> JSONObject:
        """What the interface shows at a glance. Answers also when locked or without a store."""
        session = self._current()
        if session is not None:
            try:
                return self._unlocked_state(session)
            except GateError:
                if self._session is session:
                    raise
        return self._locked_state()

    def _base_state(self, info: StoreInfo) -> JSONObject:
        store: dict[str, Any] = {"exists": info.exists, "path": str(self._path)}
        if info.exists:
            store["sealed_files"] = info.sealed_files
            store["sealed_bytes"] = info.sealed_bytes
        with self._cursor_lock:
            cursor = self._cursor
        out: JSONObject = {
            "version": __version__,
            "demo": self._demo,
            "cursor": cursor,
            "store": store,
            "locked": True,
            "unlocked_at": None,
            "locks_at": None,
            "owner_mode_until": None,
            "encryption": None,
            "record": None,
            "serving": None,
            "today": None,
            "warnings": None,
            # Not sealed and not part of the vault: the view, and how to start the app again.
            "interface": dict(self._interface) if info.exists else None,
            "launch": dict(self._launch),
        }
        if self._demo and self._demo_passphrase is not None:
            # Only the demo vault, whose documents are invented, has a passphrase that is shown.
            out["demo_passphrase"] = self._demo_passphrase
        return out

    def _locked_state(self) -> JSONObject:
        info = self._store_info(None)
        out = self._base_state(info)
        if info.exists:
            out["record"] = {
                "head_seq": info.head.seq if info.head is not None else 0,
                "head_hash": info.head.short if info.head is not None else "",
                "head_time": None,
                "verified": False,
                "verified_at": None,
                "previous_session": None,
            }
            out["warnings"] = []
        return out

    def _unlocked_state(self, session: _Session) -> JSONObject:
        gate = session.gate
        now = self._clock()
        policy = gate.policy()
        usage = gate.usage()
        waiting = gate.pending()
        granted = gate.approvals()
        documents = gate.documents()
        served = gate.served()
        head_seq, _, head_hash = gate.head().partition(":")

        owner = now < session.owner_until
        seen = (
            tuple((entry.client, entry.ref, entry.count) for entry in waiting),
            tuple((entry.client, entry.ref) for entry in granted),
            owner,
        )
        if seen != session.seen:
            session.seen = seen
            self._bump()

        info = self._store_info(session)
        out = self._base_state(info)
        releases = questions = refusals = 0
        earliest = format_time(now - DAY)  # record times are text that sorts by time
        with session.feed.lock:
            head_time = session.feed.items[-1]["time"] if session.feed.items else None
            for item in reversed(session.feed.items):
                if not isinstance(item["time"], str):
                    continue
                if item["time"] <= earliest:
                    break
                if item["kind"] == "release":
                    releases += 1
                elif item["kind"] == "question":
                    questions += 1
                elif item["kind"] == "refusal":
                    refusals += item["count"]
        collections = set(policy.collections) | {entry.collection for entry in documents}
        hosted, local = policy.client_names(True), policy.client_names(False)
        out.update(
            locked=False,
            unlocked_at=_iso(session.unlocked_at),
            locks_at=_iso(session.locks_at),
            owner_mode_until=_iso(session.owner_until),
            encryption={
                "cipher": "XChaCha20-Poly1305",
                "kdf": _kdf(info.scrypt_n),
                "documents": len(documents),
                "collections": len(collections),
                "characters": sum(entry.chars for entry in documents),
                "originals_kept": sum(1 for entry in documents if entry.original),
            },
            record={
                "head_seq": int(head_seq),
                "head_hash": head_hash,
                "head_time": head_time,
                "verified": session.verified,
                "verified_at": _iso(session.verified_at),
                "previous_session": gate.started.previous_session,
            },
            serving={
                "hosted": [name for name in served if name in hosted],
                "local": [name for name in served if name in local],
                "paused": sorted(session.paused),
                "choice_needed": choice_needed(policy, session.kinds),
                "kinds": self._kinds_in_force(policy, session),
            },
            today={
                "releases": releases,
                "characters": sum(pool.chars_day for pool in usage.pools.values()),
                "questions": questions,
                "refusals": refusals,
                "approvals_waiting": len(waiting),
            },
            warnings=self._warnings(session, policy, usage, info, documents, served),
        )
        return out

    @staticmethod
    def _kinds_in_force(policy: Policy, session: _Session) -> str:
        """Which kinds of apps the session answers: the owner's choice, or what follows without."""
        if session.kinds is not None:
            return session.kinds
        hosted, local = bool(policy.client_names(True)), bool(policy.client_names(False))
        if hosted:
            return HOSTED  # also when both are registered and nothing was chosen
        return LOCAL if local else "none"

    def _warnings(
        self,
        session: _Session,
        policy: Policy,
        usage: Usage,
        info: StoreInfo,
        documents: list[DocumentInfo],
        served: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []

        def warn(code: str, text: str, detail: Iterable[str] = ()) -> None:
            warnings.append({"code": code, "text": text, "detail": list(detail)})

        if session.gate.started.previous_session == "unclean":
            warn(
                "previous_session_unclean",
                f"{brand.PRODUCT} did not lock the vault before it stopped last time.",
            )
        if kinds_served(policy, served) == BOTH:
            warn(
                "both_kinds_served",
                "Hosted apps and local apps are answered together. While they are, any program "
                "running as you, including a hosted app that can run commands, can call under "
                "a local app's name.",
            )
        if info.synced_folder is not None:
            warn(
                "synced_folder",
                "The vault lies in a folder that looks synchronised: its sealed files and "
                "their sizes are copied to a provider and to other devices.",
                [str(self._path)],
            )
        unreadable = [
            entry.title
            for entry in documents
            if entry.doc_id in usage.documents and not usage.documents[entry.doc_id].available
        ]
        if unreadable:
            warn(
                "documents_unavailable",
                f"{_count(len(unreadable), 'document')} cannot be read and "
                f"{'is' if len(unreadable) == 1 else 'are'} left out of every search.",
                unreadable,
            )
        problem = connect_problem(session.sdk)
        if problem is not None:
            code, words, command = problem
            warn(code, f"Apps {words}", command)
        return warnings

    # -- creating, unlocking, locking ---------------------------------------------------

    def _hours(self, hours: float | None) -> float:
        if hours is None:
            return self._default_hours
        if isinstance(hours, bool) or not isinstance(hours, (int, float)):
            raise _invalid("The time until the vault locks itself must be a number of hours.")
        if not MIN_HOURS <= hours <= MAX_HOURS:
            raise _invalid(
                f"The vault can stay unlocked for {MIN_HOURS} to {MAX_HOURS:g} hours at a time."
            )
        return float(hours)

    def _socket_fits(self) -> None:
        try:
            paths.check_socket_path(self._path)
        except GateError:
            raise _invalid(
                "The path of the vault is too long for the local socket that apps connect "
                "to. Choose a shorter place for it."
            ) from None

    @_translated
    def create_store(self, *, passphrase: str) -> JSONObject:
        """Create the store, unlock it and return the state."""
        if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_CHARS:
            raise _invalid(f"A passphrase needs at least {MIN_PASSPHRASE_CHARS} characters.")
        exists = ServiceError(409, "conflict", "A vault exists already at this place.")
        if self._session is not None or store_info(self._path).exists:
            raise exists
        self._socket_fits()
        self._claim_opening()
        try:
            try:
                store = init_store(
                    self._path,
                    passphrase=passphrase,
                    scrypt_n=self._scrypt_n,
                    policy=default_policy().to_json(),
                    clock=self._clock,
                )
            except FileExistsError:
                if store_info(self._path).exists:
                    raise exists from None
                raise ServiceError(
                    409, "conflict", "The folder for the vault is not empty. Choose another place."
                ) from None
            except StoreLocationError as exc:
                raise _invalid(_sentence(_vault_words(str(exc)))) from None
            # A new vault starts in the everyday view, with setting up still to do. A
            # vault whose file could not be written is shown like one from before.
            with contextlib.suppress(OSError):
                self._keep_interface(self._start_view or "basic", "open")
            self._begin(store, self._default_hours)
        finally:
            self._opening = False
        return self.state()

    @_translated
    def unlock(self, *, passphrase: str, hours: float | None = None) -> JSONObject:
        """Unlock the store, for ``hours`` or the app's default, and return the state.

        Unlocking a vault that is unlocked already checks the passphrase and
        renews owner mode; nothing else changes.
        """
        wanted = self._hours(hours)
        if self._current() is not None:
            self.reauth(passphrase=passphrase)
            return self.state()
        if not store_info(self._path).exists:
            raise _not_found("There is no vault here yet.")
        self._socket_fits()
        self._claim_opening()
        try:
            # The lock comes first and the passphrase after it: nothing is stretched
            # for a vault that another program has open.
            lock = take_lock(self._path)
            try:
                store = open_store(self._path, passphrase=passphrase, lock=lock, clock=self._clock)
            except ValueError:  # an empty passphrase
                raise WrongKeyError("no passphrase") from None
            self._begin(store, wanted)
        finally:
            self._opening = False
        return self.state()

    @_translated
    def lock(self) -> JSONObject:
        """Lock the store and return the state. Locking a locked vault changes nothing."""
        session = self._session
        if session is not None:
            self._end(session, "lock-command")
        return self.state()

    @_translated
    def reauth(self, *, passphrase: str) -> JSONObject:
        """Check the passphrase again; changes that let more leave are then accepted for a while."""
        session = self._unlocked()
        if not isinstance(passphrase, str):
            raise WrongKeyError("wrong passphrase")
        # Each check holds the memory that stretching a passphrase takes, so checks
        # that arrive together wait for one another instead of adding up.
        with self._checking:
            right = session.store.check_passphrase(passphrase)
        if not right:
            raise WrongKeyError("wrong passphrase")
        with self._lock:
            session.owner_until = self._clock() + OWNER_MODE
        self._bump()
        return {"owner_mode_until": _iso(session.owner_until)}

    @_translated
    def set_serving(
        self, *, kinds: str | None = None, paused: list[str] | None = None
    ) -> JSONObject:
        """Choose which kinds of apps are answered and which apps are paused; return the state.

        Answering both kinds needs owner mode, and so does every choice after
        which an app is answered that was not answered before: taking an app
        out of the paused ones, or choosing the other kind. Pausing, and a
        choice that only takes away, do not.
        """
        session = self._unlocked()
        if kinds is not None and kinds not in (HOSTED, LOCAL, BOTH):
            raise _invalid('Choose "hosted", "local" or "both".')
        if paused is not None and not all(isinstance(name, str) for name in paused):
            raise _invalid("The paused apps must be given as a list of names.")
        with self._lock:
            policy = session.gate.policy()
            chosen = session.kinds if kinds is None else kinds
            held = session.paused if paused is None else frozenset(paused)
            if (kinds == BOTH and session.kinds != BOTH) or self._answers_more(
                session, policy, chosen, held
            ):
                self._owner(session)
            session.kinds, session.paused = chosen, held
            self._apply_serving(session, policy)
        self._bump()
        return self.state()

    @_translated
    def verify(self) -> JSONObject:
        """Read and verify every record again, now, and say what was found."""
        session = self._unlocked()
        started = time.monotonic()
        try:
            head = session.store.ledger.verify()
        except LedgerError as exc:
            if session.store.closed:
                raise _locked() from None
            session.verified = False
            session.verified_at = self._clock()
            self._bump()
            return {
                "verified": False,
                "first_bad": exc.seq,
                "message": "The record does not verify"
                + (f" at entry #{exc.seq:,}." if exc.seq is not None else "."),
            }
        session.verified = True
        session.verified_at = self._clock()
        self._bump()
        return {
            "verified": True,
            "head_seq": head.seq,
            "head_hash": head.short,
            "records": head.seq,
            "seconds": round(time.monotonic() - started, 2),
        }

    # -- activity -----------------------------------------------------------------------

    @_translated
    def activity(
        self,
        *,
        limit: int | None = None,
        before: int | None = None,
        app: str | None = None,
        kind: str | None = None,
    ) -> JSONObject:
        """A page of the record, newest first."""
        session = self._unlocked()
        size = _DEFAULT_PAGE if limit is None else limit
        if type(size) is not int or not 1 <= size <= _MAX_PAGE:
            raise _invalid(f"A page holds 1 to {_MAX_PAGE} entries.")
        if before is not None and type(before) is not int:
            raise _invalid("The entry to continue from must be a number.")
        if kind is not None and kind not in ACTIVITY_KINDS:
            raise _invalid("There is no such kind of entry.")
        page: list[dict[str, Any]] = []
        more = False
        with session.feed.lock:
            for item in reversed(session.feed.items):
                if before is not None and item["seq"] >= before:
                    continue
                if kind is not None and item["kind"] != kind:
                    continue
                if app is not None and item["app"] != app:
                    continue
                if len(page) == size:
                    more = True
                    break
                page.append(_public(item))
        return {"items": page, "next_before": page[-1]["seq"] if more and page else None}

    @_translated
    def activity_detail(self, *, seq: int) -> JSONObject:
        """One record in full, with the exact text of a release."""
        session = self._unlocked()
        with session.feed.lock:
            found = session.feed.find(seq) if type(seq) is int else None
            item = None if found is None else _public(found)
            details = [] if found is None else [list(pair) for pair in found["_details"]]
        if item is None:
            raise _not_found("The record has no entry with that number.")
        try:
            record = session.store.ledger.read(seq)
        except LedgerError:
            if session.store.closed:
                raise _locked() from None
            raise _not_found("That entry cannot be read from the record.") from None
        data = record.data
        released = record.kind == "release"
        claimed = _mapping(data.get("claimed"))
        if item["kind"] == "refusal":
            details.append(["calls", f"{item['count']:,}"])
        text = data.get("text") if released else None
        query = data.get("query")
        if not isinstance(query, str):
            # A call that was refused before its query was looked at keeps it as an argument.
            query = _mapping(data.get("args")).get("query")
        protocol = claimed.get("protocol")
        transport = data.get("transport")
        item.update(
            text=text if isinstance(text, str) else None,
            query=query if isinstance(query, str) else None,
            claimed=_spoken(claimed),
            protocol=protocol if isinstance(protocol, str) and protocol else None,
            transport=transport if isinstance(transport, str) else None,
            client_seq=_whole(data.get("client_seq")),
            record_hash=record.sha256[:16],
            previous_hash=record.prev[:16],
            rule=data.get("rule") if isinstance(data.get("rule"), str) else None,
            details=details,
        )
        return item

    # -- approvals ----------------------------------------------------------------------

    @_translated
    def approvals(self) -> JSONObject:
        """The passages waiting for the owner and the approvals not yet used."""
        session = self._unlocked()
        gate = session.gate
        allowance = gate.policy().per_document
        waiting: list[dict[str, Any]] = []
        for entry in gate.pending():
            try:
                preview = gate.preview(entry.client, entry.ref)
            except UnknownPassageError:
                continue  # gone since the list was read
            waiting.append(
                {
                    "app": preview.client,
                    "destination": preview.destination,
                    "hosted": preview.hosted,
                    "claimed": preview.claimed,
                    "ref": preview.ref,
                    "collection": preview.collection,
                    "title": preview.title,
                    "passage": preview.passage,
                    "passages": preview.passages,
                    "first_asked": _iso(entry.first_at),
                    "last_asked": _iso(entry.last_at),
                    "times_asked": entry.count,
                    "mode": preview.mode,
                    "inside_free_allowance": not preview.needs_approval,
                    "fits_limits": preview.fits_limits,
                    "body": preview.body,
                    "body_sha256": preview.body_sha256,
                    "characters": len(preview.body),
                    "result_characters": preview.result_chars,
                    "document": {
                        "characters": preview.doc_chars,
                        "released": preview.doc_released,
                        "after": preview.doc_after,
                        "free": allowance.free_allowance(preview.doc_chars),
                        "to": f"{preview.pool} apps",
                    },
                    "today": {
                        "used": preview.client_day_used,
                        "limit": preview.client_day_limit,
                    },
                }
            )
        granted: list[dict[str, Any]] = []
        for approval in gate.approvals():
            doc_id, _, number = approval.ref.partition(".")
            known = session.feed.document(doc_id)
            granted.append(
                {
                    "app": approval.client,
                    "ref": approval.ref,
                    "valid_until": _iso(approval.valid_until),
                    "title": None if known is None else known.title,
                    "collection": None if known is None else known.collection,
                    "passage": int(number) if number.isdigit() else None,
                    "passages": None if known is None else known.passages,
                    "withdrawable": True,
                }
            )
        return {"waiting": waiting, "granted": granted}

    @_translated
    def release(self, *, app: str, ref: str, body_sha256: str) -> JSONObject:
        """Approve one passage for one app, once, if its text is still the one shown."""
        session = self._unlocked()
        valid_until = session.gate.approve(app, ref, body_sha256)
        self._bump()
        return {"valid_until": _iso(valid_until)}

    @_translated
    def dismiss(self, *, app: str, ref: str) -> JSONObject:
        """Take one waiting passage off the list, or withdraw an approval that was not used."""
        session = self._unlocked()
        session.gate.dismiss(app, ref)
        self._bump()
        return {}

    # -- documents ----------------------------------------------------------------------

    @staticmethod
    def _document_view(info: DocumentInfo, usage: Usage) -> dict[str, Any]:
        used = usage.documents.get(info.doc_id)
        released = dict.fromkeys(POOLS, 0) if used is None else dict(used.released)
        return {
            "id": info.doc_id,
            "title": info.title,
            "passages": info.passages,
            "characters": info.chars,
            "added": _iso(info.added),
            "original_kept": info.original,
            "released": released,
        }

    def _collection_views(self, session: _Session) -> list[dict[str, Any]]:
        policy = session.gate.policy()
        usage = session.gate.usage()
        documents = session.gate.documents()
        names = list(policy.collections)
        names.extend(sorted({d.collection for d in documents} - set(policy.collections)))
        views: list[dict[str, Any]] = []
        for name in names:
            entry = policy.collections.get(name)
            held = [info for info in documents if info.collection == name]
            views.append(
                {
                    "name": name,
                    "note": "" if entry is None else entry.note,
                    "allow_hosted": entry is not None and entry.allow_hosted,
                    "show_titles": entry is not None and entry.show_titles,
                    "characters": sum(info.chars for info in held),
                    "documents": [self._document_view(info, usage) for info in held],
                }
            )
        return views

    @_translated
    def documents(self) -> JSONObject:
        """The collections with their documents, and what can be uploaded."""
        session = self._unlocked()
        return {
            "collections": self._collection_views(session),
            "upload": {
                "max_bytes": extract.MAX_UPLOAD_BYTES,
                "types": list(extract.SUPPORTED_SUFFIXES),
                "pdf": extract.pdf_available(),
            },
        }

    @_translated
    def add_document(self, *, collection: str, name: str, data: bytes) -> JSONObject:
        """Add one uploaded file, given as its name and its bytes, to a collection.

        The title is the file name without its suffix. The file's bytes are
        sealed along with its text. A collection that does not exist yet is
        created, not open to hosted apps.
        """
        session = self._unlocked()
        if not is_name(collection):
            raise _invalid(
                "a collection name has 1 to 32 characters from a-z, 0-9, - and _, and starts "
                "with a letter or a digit"
            )
        if not isinstance(name, str) or not isinstance(data, (bytes, bytearray)):
            raise _invalid("a file needs a name and its content")
        file_name = PurePosixPath(name.replace("\\", "/")).name.strip()[:_MAX_NAME_CHARS]
        try:
            file_name.encode("utf-8")
            text = extract.text_from_upload(file_name, bytes(data))
        except UnicodeEncodeError:
            raise _invalid("the name of the file cannot be read") from None
        except ValueError as exc:
            raise _invalid(str(exc)) from None
        stem = PurePosixPath(file_name).stem.strip()
        info: DocumentInfo | None = None
        with self._lock:
            for title in (stem, _FALLBACK_TITLE):
                try:
                    info = session.gate.add_document(
                        collection=collection,
                        title=title,
                        text=text,
                        title_source="file-name",
                        source={"name": file_name},
                        original=bytes(data),
                    )
                    break
                except PolicyError:
                    raise
                except ValueError:
                    continue  # a file name that cannot be a title: use a plain one instead
            if info is None:
                raise _invalid("the name of the file cannot be used as a title")
            usage = session.gate.usage()
        return {"document": self._document_view(info, usage)}

    @_translated
    def remove_document(self, *, id: str) -> JSONObject:
        """Remove one document: its record entry first, then its sealed files."""
        session = self._unlocked()
        with self._lock:
            session.gate.remove_document(id)
        return {}

    @_translated
    def set_collection(
        self,
        *,
        name: str,
        note: str | None = None,
        allow_hosted: bool | None = None,
        show_titles: bool | None = None,
    ) -> JSONObject:
        """Create a collection or change the fields that are given.

        Opening it to hosted apps or showing its titles needs owner mode.
        Closing it to hosted apps also takes it away from every hosted app, so
        that opening it again later grants nothing by itself.
        """
        session = self._unlocked()
        if not is_name(name):
            raise _invalid(
                "A collection name has 1 to 32 characters from a-z, 0-9, - and _, and starts "
                "with a letter or a digit."
            )
        if note is not None:
            try:
                note = " ".join(sanitize(note).split())
            except ValueError:
                raise _invalid("The note cannot be used: write it as one plain line.") from None
        with self._lock:
            policy = session.gate.policy()
            new = policy.with_collection(
                name, note=note, allow_hosted=allow_hosted, show_titles=show_titles
            )
            if allow_hosted is False:
                for app, entry in policy.clients.items():
                    if entry.hosted and name in entry.grants:
                        new = new.without_grant(app, name)
            self._change(session, new)
            view = next(v for v in self._collection_views(session) if v["name"] == name)
        return {"collection": view}

    @_translated
    def remove_collection(self, *, name: str) -> JSONObject:
        """Remove an empty collection, and with it every grant of it."""
        session = self._unlocked()
        with self._lock:
            policy = session.gate.policy()
            if name not in policy.collections:
                raise _not_found("There is no collection of that name.")
            if any(info.collection == name for info in session.gate.documents()):
                raise ServiceError(
                    409, "conflict", "This collection still holds documents. Remove them first."
                )
            self._change(session, policy.without_collection(name))
        return {}

    # -- apps ---------------------------------------------------------------------------

    @staticmethod
    def _app_view(name: str, policy: Policy, usage: Usage, session: _Session) -> dict[str, Any]:
        entry = policy.clients[name]
        used = usage.clients[name]
        said = " ".join(part for part in (used.claimed_name, used.claimed_version) if part)
        last_seen = None
        if used.last_call is not None:
            last_seen = {"time": _iso(used.last_call), "claimed": said or None}
        return {
            "name": name,
            "destination": entry.destination,
            "hosted": entry.hosted,
            "paused": name in session.paused,
            "served": used.served,
            "grants": policy.effective_grants(name),
            "limits": policy.limits_for(name).to_json(),
            "usage": {
                "chars_day": used.chars_day,
                "chars_30_days": used.chars_30_days,
                "searches_day": used.searches_day,
                "releases_day": used.releases_day,
            },
            "last_seen": last_seen,
        }

    def _one_app(self, session: _Session, name: str) -> JSONObject:
        policy = session.gate.policy()
        return {"app": self._app_view(name, policy, session.gate.usage(), session)}

    def _registered(self, policy: Policy, name: str) -> None:
        if not isinstance(name, str) or name not in policy.clients:
            raise _not_found("There is no app of that name.")

    @_translated
    def apps(self) -> JSONObject:
        """The registered apps with their grants, limits and use."""
        session = self._unlocked()
        policy = session.gate.policy()
        usage = session.gate.usage()
        groups = {
            pool: {
                **policy.pools[pool].to_json(),
                "used_day": usage.pools[pool].chars_day,
                "used_30_days": usage.pools[pool].chars_30_days,
            }
            for pool in POOLS
        }
        return {
            "apps": [
                self._app_view(name, policy, usage, session)
                for name in policy.clients
                if name in usage.clients  # the two were read a moment apart
            ],
            "groups": groups,
            "collections": [
                {"name": name, "allow_hosted": entry.allow_hosted}
                for name, entry in policy.collections.items()
            ],
            "known": [host.as_dict() for host in hostconfig.KNOWN_HOSTS],
        }

    @_translated
    def add_app(self, *, name: str, hosted: bool, destination: str | None = None) -> JSONObject:
        """Register an app under a name. Needs owner mode."""
        session = self._unlocked()
        if not isinstance(name, str) or hostconfig.CLIENT_NAME_PATTERN.fullmatch(name) is None:
            raise _invalid(
                "An app name has 1 to 32 characters from a-z, 0-9 and -, and starts with a letter."
            )
        if not isinstance(hosted, bool):
            raise _invalid("Say whether the app is hosted or local.")
        where = (destination or "").strip()
        if hosted and not where:
            raise _invalid("Say where a hosted app sends text, for example the provider's name.")
        with self._lock:
            policy = session.gate.policy()
            if name in policy.clients:
                raise ServiceError(409, "conflict", f"An app named {name} exists already.")
            self._change(session, policy.with_client(name, hosted=hosted, destination=where))
            return self._one_app(session, name)

    @_translated
    def remove_app(self, *, name: str) -> JSONObject:
        """Remove an app and its grants."""
        session = self._unlocked()
        with self._lock:
            policy = session.gate.policy()
            self._registered(policy, name)
            self._change(session, policy.without_client(name))
        return {}

    @_translated
    def set_grant(self, *, app: str, collection: str, mode: str) -> JSONObject:
        """Set what one app may do with one collection: ``read``, ``approve`` or ``none``."""
        session = self._unlocked()
        if mode not in (MODE_READ, MODE_APPROVE, "none"):
            raise _invalid('The mode is "read", "approve" or "none".')
        with self._lock:
            policy = session.gate.policy()
            self._registered(policy, app)
            if not isinstance(collection, str) or collection not in policy.collections:
                raise _not_found("There is no collection of that name.")
            if mode == "none":
                new = policy.without_grant(app, collection)
            else:
                try:
                    new = policy.with_grant(app, collection, mode)
                except HostedGrantError:
                    raise ServiceError(
                        409,
                        "conflict",
                        f'"{collection}" is not open to hosted apps, and {app} is a hosted app. '
                        "Opening the collection is a separate decision, made under Documents.",
                    ) from None
            self._change(session, new)
            return self._one_app(session, app)

    @staticmethod
    def _limit_values(
        changes: Any, ranges: Mapping[str, tuple[int, int]], *, reset: bool = False
    ) -> dict[str, Any]:
        """Check a partial set of limits; a friendly sentence for each thing that is wrong."""
        if not isinstance(changes, Mapping):
            raise _invalid("The limits must be given as an object.")
        values: dict[str, Any] = {}
        for key, value in changes.items():
            if key not in ranges:
                raise _invalid("There is no such limit.")
            if value is None and reset:
                values[key] = None
                continue
            low, high = ranges[key]
            if type(value) is not int or not low <= value <= high:
                raise _invalid(
                    f"{_sentence(_LIMIT_WORDS.get(str(key), str(key)))[:-1]}: a whole number from "
                    f"{low:,} to {high:,}."
                )
            values[key] = value
        return values

    @_translated
    def set_app_limits(self, *, name: str, limits: JSONObject) -> JSONObject:
        """Change limits of one app. A raise needs owner mode; ``null`` returns to the default."""
        session = self._unlocked()
        values = self._limit_values(limits, LIMIT_RANGES, reset=True)
        with self._lock:
            policy = session.gate.policy()
            self._registered(policy, name)
            self._change(session, policy.with_client_limits(name, values))
            return self._one_app(session, name)

    @_translated
    def connect(self, *, name: str, host: str | None = None) -> JSONObject:
        """The configuration that connects one agent application under this app's name."""
        session = self._unlocked()
        self._registered(session.gate.policy(), name)
        chosen = self._host_for(name, host)
        try:
            config = hostconfig.render(chosen, client=name, store=self._path)
        except ValueError:
            raise _invalid(_UNUSABLE_NAME) from None
        status = hostconfig.sdk_status()
        message = status["message"]
        starts = message is None
        if starts and config.local_program:
            # The configuration names a command: see once whether it can start at all.
            message = self._start_problem()
            starts = message is None
        out = config.as_dict()
        out["ready"] = {
            "sdk": bool(status["sdk"]),
            "python": bool(status["python"]),
            "starts": starts,
            "message": None if message is None else f"Apps cannot connect yet: {message}",
        }
        return out

    def _start_problem(self) -> str | None:
        """What keeps the command of a configuration from starting; tried once per app run."""
        checked = self._start_checked
        if checked is None:
            checked = self._start_checked = (start_problem(),)
        return checked[0]

    @staticmethod
    def _host_for(name: str, host: str | None) -> str:
        """The application a configuration is asked for: the one named, else the app's own."""
        known = {entry.id for entry in hostconfig.KNOWN_HOSTS}
        chosen = host if host is not None else (name if name in known else "generic")
        if chosen not in known:
            raise _not_found("There is no application of that kind to connect.")
        return chosen

    @_translated
    def connect_check(self, *, name: str, host: str | None = None) -> JSONObject:
        """Look at the settings file of one agent application for this app's entry.

        The one call that reads a file outside the vault's folder: one file,
        whose place is fixed by the application and this computer, read when
        this is called and at no other time. The answer says in one word what
        was found and holds nothing from the file. Nothing is changed, here or
        there, so it needs no owner mode.
        """
        session = self._unlocked()
        self._registered(session.gate.policy(), name)
        chosen = self._host_for(name, host)
        try:
            return hostcheck.check(chosen, client=name, store=self._path)
        except ValueError:
            raise _invalid(_UNUSABLE_NAME) from None

    # -- the demo -----------------------------------------------------------------------

    @_translated
    def demo_again(self) -> JSONObject:
        """Tell the played app of a demo to ask once more for the passage it waits for.

        It is the played app's side, not the owner's: what its call gets is
        decided by the gate like any call. Outside a demo there is no such thing.
        """
        if not self._demo or self._demo_again is None:
            raise _not_found("Only a demo has a played app.")
        self._unlocked()
        self._demo_again()
        return {}

    # -- settings -----------------------------------------------------------------------

    @staticmethod
    def _settings_view(policy: Policy) -> JSONObject:
        new = default_policy()
        figures: dict[str, Any] = {
            "defaults": policy.defaults.to_json(),
            "groups": {pool: policy.pools[pool].to_json() for pool in POOLS},
            "per_document": policy.per_document.to_json(),
            "approval": policy.approval.to_json(),
        }
        as_new: dict[str, Any] = {
            "defaults": new.defaults.to_json(),
            "groups": {pool: new.pools[pool].to_json() for pool in POOLS},
            "per_document": new.per_document.to_json(),
            "approval": new.approval.to_json(),
        }
        return {
            "serial": policy.serial,
            **figures,
            "replaced_strings": len(policy.redact_literals),
            "ceilings": {
                "chars_per_call": MAX_RESULT_CHARS,
                "ttl_minutes": MAX_APPROVAL_MINUTES,
                "replaced_strings": MAX_REDACT_LITERALS,
                # The longest passage a vault cuts a document into: fixed, not a setting.
                "passage_chars": CHUNK_CHARS,
            },
            # Which of the four groups of figures are not those of a new vault.
            "changed_from_new": [name for name in figures if figures[name] != as_new[name]],
        }

    @_translated
    def settings(self) -> JSONObject:
        """The limits and other settings of the vault. The replaced strings are only counted."""
        return self._settings_view(self._unlocked().gate.policy())

    @_translated
    def set_settings(
        self,
        *,
        defaults: JSONObject | None = None,
        groups: JSONObject | None = None,
        per_document: JSONObject | None = None,
        approval: JSONObject | None = None,
    ) -> JSONObject:
        """Change the settings that are given and return all of them. A raise needs owner mode."""
        session = self._unlocked()
        with self._lock:
            new = session.gate.policy()
            if defaults is not None:
                new = new.with_defaults(self._limit_values(defaults, LIMIT_RANGES))
            if groups is not None:
                if not isinstance(groups, Mapping) or any(pool not in POOLS for pool in groups):
                    raise _invalid('The groups are "hosted" and "local".')
                for pool, changes in groups.items():
                    new = new.with_pool_limits(pool, self._limit_values(changes, POOL_RANGES))
            if per_document is not None:
                new = new.with_per_document(self._allowance(per_document))
            if approval is not None:
                new = new.with_approval(self._limit_values(approval, {"ttl_minutes": TTL_RANGE}))
            return self._settings_view(self._change(session, new))

    @staticmethod
    def _allowance(changes: Any) -> dict[str, Any]:
        if not isinstance(changes, Mapping) or not set(changes) <= {"max_chars", "max_fraction"}:
            raise _invalid("The free part of a document has a size and a share, nothing else.")
        values: dict[str, Any] = {}
        if "max_chars" in changes:
            size = changes["max_chars"]
            if type(size) is not int or not 0 <= size <= MAX_DOCUMENT_CHARS:
                raise _invalid(
                    "The free part of a document: a whole number of characters from 0 to "
                    f"{MAX_DOCUMENT_CHARS:,}."
                )
            values["max_chars"] = size
        if "max_fraction" in changes:
            share = changes["max_fraction"]
            if (
                isinstance(share, bool)
                or not isinstance(share, (int, float))
                or not 0 <= share <= 1
            ):
                raise _invalid("The free share of a document: a number from 0 to 1.")
            values["max_fraction"] = float(share)
        return values

    @_translated
    def add_replaced_string(self, *, text: str) -> JSONObject:
        """Add one word or number that must never leave; returns only the new count."""
        session = self._unlocked()
        word = text.strip() if isinstance(text, str) else ""
        if not REDACT_LITERAL_MIN_CHARS <= len(word) <= REDACT_LITERAL_MAX_CHARS:
            raise _invalid(
                f"A word or number to replace has {REDACT_LITERAL_MIN_CHARS} to "
                f"{REDACT_LITERAL_MAX_CHARS} characters."
            )
        with self._lock:
            policy = session.gate.policy()
            if len(policy.redact_literals) >= MAX_REDACT_LITERALS:
                raise _invalid(f"At most {MAX_REDACT_LITERALS} words or numbers can be listed.")
            try:
                new = policy.with_redact_literal(word)
            except PolicyError:
                raise _invalid(
                    "That cannot be added: it is on the list already, or it has fewer than "
                    "two letters or digits."
                ) from None
            stored = self._change(session, new)
        return {"replaced_strings": len(stored.redact_literals)}
