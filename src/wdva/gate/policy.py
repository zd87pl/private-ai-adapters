# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The policy: which agent applications may be given what, and how much.

A policy is one JSON object. It names the clients (the owner's labels for agent
applications), the collections of documents, the grants that connect the two,
the limits, and the strings that are replaced in everything that is released.
A client reaches a collection only through a grant. A new policy has no client,
no collection and no grant, so nothing is reachable.

This module holds a policy as frozen values, checks it strictly and answers the
questions the rest of the gate asks about it. It reads and writes plain JSON
values and touches no file: the store keeps the policy as an object and does
not import this module.

Every :class:`Policy` object is valid. Construction checks it, and each method
that changes something returns a new, checked copy; nothing is changed in place.

Two words are the owner's statements and cannot be checked by the gate: a client
is *hosted* when its model runs at a provider, and *local* otherwise. A
collection is *marked for hosted clients* or not. A hosted client never has an
effective grant on a collection that is not marked, whatever the stored grants
say (see :meth:`Policy.effective_grant`).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from fractions import Fraction
from types import MappingProxyType
from typing import Any, NoReturn

from wdva._util import MAX_SAFE_INTEGER
from wdva.gate.errors import PolicyError

__all__ = [
    "BOTH",
    "DESTINATION_MAX_CHARS",
    "GATE_CALLS_PER_MINUTE",
    "HOSTED",
    "KINDS",
    "LIMIT_KEYS",
    "LIMIT_RANGES",
    "LOCAL",
    "LOCAL_DESTINATION",
    "MAX_APPROVAL_MINUTES",
    "MAX_CLIENTS",
    "MAX_COLLECTIONS",
    "MAX_DOCUMENT_CHARS",
    "MAX_FRACTION",
    "MAX_REDACT_LITERALS",
    "MAX_RESULT_CHARS",
    "MAX_SEARCH_RESULTS",
    "MODES",
    "MODE_APPROVE",
    "MODE_READ",
    "NAME_PATTERN",
    "NOTE_MAX_CHARS",
    "POLICY_VERSION",
    "POOLS",
    "POOL_KEYS",
    "POOL_RANGES",
    "REDACT_LITERAL_MAX_CHARS",
    "REDACT_LITERAL_MIN_CHARS",
    "TTL_RANGE",
    "Approval",
    "Client",
    "Collection",
    "HostedGrantError",
    "Limits",
    "PerDocument",
    "Policy",
    "PoolLimits",
    "choice_needed",
    "default_policy",
    "is_name",
    "kinds_served",
    "served_set",
    "widens",
]

POLICY_VERSION = 1

HOSTED = "hosted"
LOCAL = "local"
BOTH = "both"
POOLS = (HOSTED, LOCAL)
"""The two pools. A client belongs to one of them, by its ``hosted`` field."""
KINDS = (HOSTED, LOCAL, BOTH)
"""What the owner can choose to serve when clients of both kinds are registered."""

MODE_READ = "read"
MODE_APPROVE = "approve"
MODES = (MODE_READ, MODE_APPROVE)

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
"""What a collection or client name looks like. Use it with ``fullmatch``."""

LOCAL_DESTINATION = "local model on this computer"
"""The destination a local client gets when the owner names none."""

# -- hard ceilings: no policy can raise these --------------------------------
# (The sizes of a search snippet and of a passage are fixed as well. They are not
# values of a policy at all and belong to the module that cuts the text.)

MAX_RESULT_CHARS = 8_000
"""No result is longer than this, so ``chars_per_call`` cannot be either."""
MAX_SEARCH_RESULTS = 10
MAX_FRACTION = 1.0
MAX_APPROVAL_MINUTES = 60
MAX_REDACT_LITERALS = 200
REDACT_LITERAL_MIN_CHARS = 2
REDACT_LITERAL_MAX_CHARS = 200
GATE_CALLS_PER_MINUTE = 120
"""Calls per minute over all clients together. Fixed: it is not part of a policy."""

MAX_COLLECTIONS = 100
MAX_CLIENTS = 100
NOTE_MAX_CHARS = 200
DESTINATION_MAX_CHARS = 80

_MAX_CHARS = 100_000_000

LIMIT_RANGES: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        "chars_per_call": (500, MAX_RESULT_CHARS),
        "chars_per_day": (0, _MAX_CHARS),
        "chars_per_30_days": (0, _MAX_CHARS),
        "searches_per_day": (0, 10_000),
        "calls_per_minute": (1, GATE_CALLS_PER_MINUTE),
    }
)
"""Lowest and highest value of every per-client limit."""
LIMIT_KEYS = tuple(LIMIT_RANGES)

POOL_RANGES: Mapping[str, tuple[int, int]] = MappingProxyType(
    {"chars_per_day": (0, _MAX_CHARS), "chars_per_30_days": (0, _MAX_CHARS)}
)
"""Lowest and highest value of the two limits of a pool."""
POOL_KEYS = tuple(POOL_RANGES)

MAX_DOCUMENT_CHARS = 4_000_000
"""The highest ``max_chars`` of the allowance per document: the longest text a document can have."""
TTL_RANGE = (1, MAX_APPROVAL_MINUTES)
"""Shortest and longest lifetime of an approval, in minutes."""

_PER_DOCUMENT_KEYS = ("max_chars", "max_fraction")
_APPROVAL_KEYS = ("ttl_minutes",)
_COLLECTION_KEYS = ("note", "allow_hosted", "show_titles")
_CLIENT_KEYS = ("destination", "hosted", "grants", "limits")
_TOP_KEYS = (
    "version",
    "serial",
    "pools",
    "defaults",
    "per_document",
    "approval",
    "collections",
    "clients",
    "redact_literals",
)

_NAME_RULE = (
    'a name has 1 to 32 characters from a-z, 0-9, "-" and "_" and starts with a letter or a digit'
)

# A note or a destination is shown to agents inside a status result. It must be
# one plain line: nothing a terminal or a model would treat as a control or an
# invisible character, and none of the link and markup forms that the gate
# removes from released text.
_LINE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
_INVISIBLE = frozenset(
    chr(code)
    for code in (
        0x034F,
        0x115F,
        0x1160,
        0x17B4,
        0x17B5,
        0x2800,
        0x3164,
        0xFFA0,
        *range(0x180B, 0x1810),
        *range(0xFE00, 0xFE10),
        *range(0xE0100, 0xE01F0),
    )
)
_MARKUP = re.compile(r"://|\]\(|!\[|<[A-Za-z/!?]|-----")

# Letters that do not decompose into a base letter and an accent: l with stroke,
# d with stroke, o with stroke, and the ligatures ae and oe.
_LETTERS = str.maketrans(
    {"\u0142": "l", "\u0111": "d", "\u00f8": "o", "\u00e6": "ae", "\u0153": "oe"}
)

_MODE_RANK = {None: 0, MODE_APPROVE: 1, MODE_READ: 2}


class HostedGrantError(PolicyError):
    """A hosted client was to be granted a collection that is not marked for hosted clients."""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    raise PolicyError(message)


def _quote(value: Any) -> str:
    """A short, printable form of a key or name for an error message."""
    text = str(value)
    shown = "".join(ch if ch.isprintable() else "\N{REPLACEMENT CHARACTER}" for ch in text[:40])
    return f'"{shown}{"..." if len(text) > 40 else ""}"'


def _show(value: Any) -> str:
    """What a wrong value was, in a few words. Never long, never unprintable."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value:,}" if abs(value) < 10**20 else "a number of that size"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, Mapping):
        return "an object"
    if isinstance(value, (list, tuple)):
        return "a list"
    return f"a value of type {type(value).__name__}"


def is_name(value: Any) -> bool:
    """Whether ``value`` can be the name of a collection or of a client."""
    return isinstance(value, str) and NAME_PATTERN.fullmatch(value) is not None


def _whole(value: Any, path: str, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{path} must be a whole number, not {_show(value)}")
    if not low <= value <= high:
        _fail(f"{path} must be between {low:,} and {high:,}, not {_show(value)}")


def _flag(value: Any, path: str) -> None:
    if not isinstance(value, bool):
        _fail(f"{path} must be true or false, not {_show(value)}")


def _line(value: Any, path: str, low: int, high: int) -> None:
    if not isinstance(value, str):
        _fail(f"{path} must be text, not {_show(value)}")
    if not low <= len(value) <= high:
        size = f"{low} to {high}" if low else f"at most {high}"
        _fail(f"{path} must have {size} characters, not {len(value):,}")
    if any(ch in _INVISIBLE or unicodedata.category(ch) in _LINE_CATEGORIES for ch in value):
        _fail(f"{path} must be one line without control or invisible characters")
    if value != value.strip():
        _fail(f"{path} must not start or end with a space")
    if not unicodedata.is_normalized("NFC", value):
        _fail(f"{path} must be in the composed Unicode form (NFC)")
    found = _MARKUP.search(value)
    if found is not None:
        _fail(f"{path} must not contain a link or markup ({_quote(found.group())})")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{path} must be an object, not {_show(value)}")
    for key in value:
        if not isinstance(key, str):
            _fail(f"{path} has a key that is not text")
    result: Mapping[str, Any] = value
    return result


def _object(
    value: Any, path: str, keys: tuple[str, ...], *, partial: bool = False
) -> dict[str, Any]:
    """``value`` as an object with exactly ``keys``, or with some of them when ``partial``."""
    items = _mapping(value, path)
    for key in items:
        if key not in keys:
            _fail(f"{path} has an unknown key {_quote(key)}")
    if not partial:
        for key in keys:
            if key not in items:
                _fail(f'{path} is missing the key "{key}"')
    return dict(items)


def _frozen(value: Any, path: str) -> Mapping[str, Any]:
    """A read-only copy of a mapping with text keys, in key order."""
    return MappingProxyType(dict(sorted(_mapping(value, path).items())))


def _folded_words(text: str) -> tuple[str, ...]:
    """The words of a listed string with case, accents and letter variants removed.

    Two listed strings with the same words are the same listed string: what
    stands before, between and after the words does not matter when text is
    matched against them.
    """
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_LETTERS)
    decomposed = unicodedata.normalize("NFKD", folded)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return tuple(re.findall(r"\w+", plain))


def _check_literals(literals: Any) -> None:
    # No message here may quote a listed string: these are the strings the owner
    # wants kept back, and error messages travel further than the policy does.
    if not isinstance(literals, tuple):
        _fail(f"redact_literals must be a list of strings, not {_show(literals)}")
    if len(literals) > MAX_REDACT_LITERALS:
        _fail(
            f"redact_literals holds {len(literals):,} strings; "
            f"at most {MAX_REDACT_LITERALS} can be listed"
        )
    seen: dict[tuple[str, ...], int] = {}
    for index, text in enumerate(literals):
        path = f"redact_literals[{index}]"
        if not isinstance(text, str):
            _fail(f"{path} must be text")
        if not REDACT_LITERAL_MIN_CHARS <= len(text) <= REDACT_LITERAL_MAX_CHARS:
            _fail(
                f"{path} must have {REDACT_LITERAL_MIN_CHARS} to {REDACT_LITERAL_MAX_CHARS} "
                f"characters, not {len(text):,}"
            )
        if any(unicodedata.category(ch) == "Cs" for ch in text):
            _fail(f"{path} is not valid text")
        words = _folded_words(text)
        if sum(len(word) for word in words) < 2:
            _fail(f"{path} needs at least two letters or digits")
        if words in seen:
            _fail(f"{path} equals entry {seen[words]} once case and accents are ignored")
        seen[words] = index


def _check_limits(limits: Mapping[str, Any], path: str) -> None:
    for key, value in limits.items():
        if key not in LIMIT_RANGES:
            _fail(f"{path} has an unknown key {_quote(key)}")
        _whole(value, f"{path}.{key}", *LIMIT_RANGES[key])


def _check(policy: Policy) -> None:
    """Raise :class:`PolicyError` unless every part of ``policy`` is valid."""
    version = policy.version
    if type(version) is not int or version != POLICY_VERSION:
        _fail(f"version must be {POLICY_VERSION}, not {_show(version)}")
    _whole(policy.serial, "serial", 0, MAX_SAFE_INTEGER)

    if tuple(policy.pools) != POOLS:
        _fail(f'pools must have exactly the keys "{HOSTED}" and "{LOCAL}"')
    for name, pool in policy.pools.items():
        if not isinstance(pool, PoolLimits):
            _fail(f"pools.{name} must be a PoolLimits value")
        for key in POOL_KEYS:
            _whole(getattr(pool, key), f"pools.{name}.{key}", *POOL_RANGES[key])

    if not isinstance(policy.defaults, Limits):
        _fail("defaults must be a Limits value")
    _check_limits(policy.defaults.to_json(), "defaults")

    per_document = policy.per_document
    if not isinstance(per_document, PerDocument):
        _fail("per_document must be a PerDocument value")
    _whole(per_document.max_chars, "per_document.max_chars", 0, MAX_DOCUMENT_CHARS)
    fraction = per_document.max_fraction
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        _fail(f"per_document.max_fraction must be a number, not {_show(fraction)}")
    if not 0.0 <= fraction <= MAX_FRACTION:  # false for NaN as well
        _fail(f"per_document.max_fraction must be between 0 and 1, not {_show(fraction)}")

    if not isinstance(policy.approval, Approval):
        _fail("approval must be an Approval value")
    _whole(policy.approval.ttl_minutes, "approval.ttl_minutes", *TTL_RANGE)

    if len(policy.collections) > MAX_COLLECTIONS:
        _fail(f"at most {MAX_COLLECTIONS} collections can exist, not {len(policy.collections):,}")
    for name, collection in policy.collections.items():
        if not is_name(name):
            _fail(f"collections: {_quote(name)} is not a name ({_NAME_RULE})")
        path = f"collections.{name}"
        if not isinstance(collection, Collection):
            _fail(f"{path} must be a Collection value")
        _line(collection.note, f"{path}.note", 0, NOTE_MAX_CHARS)
        _flag(collection.allow_hosted, f"{path}.allow_hosted")
        _flag(collection.show_titles, f"{path}.show_titles")

    if len(policy.clients) > MAX_CLIENTS:
        _fail(f"at most {MAX_CLIENTS} clients can be registered, not {len(policy.clients):,}")
    for name, client in policy.clients.items():
        if not is_name(name):
            _fail(f"clients: {_quote(name)} is not a name ({_NAME_RULE})")
        path = f"clients.{name}"
        if not isinstance(client, Client):
            _fail(f"{path} must be a Client value")
        _line(client.destination, f"{path}.destination", 1, DESTINATION_MAX_CHARS)
        _flag(client.hosted, f"{path}.hosted")
        for collection_name, mode in client.grants.items():
            if collection_name not in policy.collections:
                _fail(f"{path}.grants: there is no collection named {_quote(collection_name)}")
            if not isinstance(mode, str) or mode not in MODES:
                _fail(
                    f'{path}.grants.{collection_name}.mode must be "{MODE_READ}" or '
                    f'"{MODE_APPROVE}", not {_show(mode)}'
                )
        _check_limits(client.limits, f"{path}.limits")

    _check_literals(policy.redact_literals)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """The limits of one client: the defaults of a policy, or what is in force for a client.

    ``chars_per_call`` bounds one search result. The two character limits are
    rolling windows of 24 hours and of 30 days.
    """

    chars_per_call: int = 2_000
    chars_per_day: int = 20_000
    chars_per_30_days: int = 150_000
    searches_per_day: int = 100
    calls_per_minute: int = 30

    def to_json(self) -> dict[str, int]:
        """The five limits as a JSON object."""
        return {key: getattr(self, key) for key in LIMIT_KEYS}


@dataclass(frozen=True)
class PoolLimits:
    """What all clients of one pool may receive together."""

    chars_per_day: int = 40_000
    chars_per_30_days: int = 300_000

    def to_json(self) -> dict[str, int]:
        """The two limits as a JSON object."""
        return {key: getattr(self, key) for key in POOL_KEYS}


@dataclass(frozen=True)
class PerDocument:
    """How much of one document a pool may receive without the owner's approval."""

    max_chars: int = 8_000
    max_fraction: float = 0.5

    def __post_init__(self) -> None:
        fraction = self.max_fraction
        if isinstance(fraction, int) and not isinstance(fraction, bool):
            object.__setattr__(self, "max_fraction", float(fraction))

    def free_allowance(self, length: int) -> int:
        """The free allowance of a document of ``length`` characters.

        It is the smaller of ``max_chars`` and ``max_fraction`` of the document,
        rounded up. The fraction is taken as the decimal number that was
        written, so half of 30 is 15 and a tenth of 30 is 3, not 4.
        """
        share = math.ceil(Fraction(repr(self.max_fraction)) * max(0, length))
        return min(self.max_chars, share)

    def to_json(self) -> dict[str, Any]:
        """The two values as a JSON object."""
        return {"max_chars": self.max_chars, "max_fraction": self.max_fraction}


@dataclass(frozen=True)
class Approval:
    """How long an approval of one passage stays valid."""

    ttl_minutes: int = 10

    def to_json(self) -> dict[str, int]:
        """The value as a JSON object."""
        return {"ttl_minutes": self.ttl_minutes}


@dataclass(frozen=True)
class Collection:
    """A named group of documents.

    ``note`` is the owner's one-line description; clients that are granted the
    collection are shown it. ``allow_hosted`` marks the collection for hosted
    clients. ``show_titles`` lets document titles appear in results; it is off
    by default because a title is usually a file name.
    """

    note: str = ""
    allow_hosted: bool = False
    show_titles: bool = False

    def to_json(self) -> dict[str, Any]:
        """The collection as a JSON object."""
        return {
            "note": self.note,
            "allow_hosted": self.allow_hosted,
            "show_titles": self.show_titles,
        }


@dataclass(frozen=True)
class Client:
    """One registered agent application, under the name the owner gave it.

    ``destination`` says where text given to it goes, in the owner's words.
    ``grants`` maps a collection name to ``"read"`` or ``"approve"`` exactly as
    stored; ask :meth:`Policy.effective_grant` what the gate honours.
    ``limits`` holds only the limits that differ from the policy's defaults.
    """

    destination: str
    hosted: bool
    grants: Mapping[str, str] = field(default_factory=dict)
    limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "grants", _frozen(self.grants, "grants"))
        object.__setattr__(self, "limits", _frozen(self.limits, "limits"))

    @property
    def pool(self) -> str:
        """``"hosted"`` or ``"local"``: the pool whose limits this client shares."""
        return HOSTED if self.hosted else LOCAL

    def to_json(self) -> dict[str, Any]:
        """The client as a JSON object."""
        return {
            "destination": self.destination,
            "hosted": self.hosted,
            "grants": {name: {"mode": mode} for name, mode in self.grants.items()},
            "limits": dict(self.limits),
        }


def _default_pools() -> dict[str, PoolLimits]:
    return {HOSTED: PoolLimits(), LOCAL: PoolLimits()}


@dataclass(frozen=True)
class Policy:
    """A complete, valid policy.

    Build one with :func:`default_policy` or :meth:`from_json`, and change it
    with the ``with_...`` and ``without_...`` methods, each of which returns a
    new policy and leaves this one as it was. ``dataclasses.replace`` works too
    and checks the result in the same way. The mappings are read-only.

    The methods that change something raise :class:`PolicyError` with a
    sentence that can be shown to the owner. They do not touch ``serial``:
    whoever stores a policy numbers it.
    """

    serial: int = 1
    pools: Mapping[str, PoolLimits] = field(default_factory=_default_pools)
    defaults: Limits = field(default_factory=Limits)
    per_document: PerDocument = field(default_factory=PerDocument)
    approval: Approval = field(default_factory=Approval)
    collections: Mapping[str, Collection] = field(default_factory=dict)
    clients: Mapping[str, Client] = field(default_factory=dict)
    redact_literals: tuple[str, ...] = field(default=(), repr=False)
    version: int = POLICY_VERSION

    def __post_init__(self) -> None:
        for name in ("pools", "collections", "clients"):
            object.__setattr__(self, name, _frozen(getattr(self, name), name))
        if isinstance(self.redact_literals, list):
            object.__setattr__(self, "redact_literals", tuple(self.redact_literals))
        _check(self)

    # -- JSON ---------------------------------------------------------------

    @classmethod
    def from_json(cls, obj: Any) -> Policy:
        """Build a policy from its JSON object, strictly.

        Every key must be present and no other key may be; every value must
        have the right type and lie inside its range. Anything else raises
        :class:`PolicyError` with the path of the offending value.
        """
        top = _object(obj, "the policy", _TOP_KEYS)

        raw_pools = _object(top["pools"], "pools", POOLS)
        pools = {
            name: PoolLimits(**_object(raw_pools[name], f"pools.{name}", POOL_KEYS))
            for name in POOLS
        }

        collections: dict[str, Collection] = {}
        for name, raw in _mapping(top["collections"], "collections").items():
            if not is_name(name):
                _fail(f"collections: {_quote(name)} is not a name ({_NAME_RULE})")
            collections[name] = Collection(**_object(raw, f"collections.{name}", _COLLECTION_KEYS))

        clients: dict[str, Client] = {}
        for name, raw in _mapping(top["clients"], "clients").items():
            if not is_name(name):
                _fail(f"clients: {_quote(name)} is not a name ({_NAME_RULE})")
            path = f"clients.{name}"
            entry = _object(raw, path, _CLIENT_KEYS)
            grants: dict[str, Any] = {}
            for collection, grant in _mapping(entry["grants"], f"{path}.grants").items():
                where = f"{path}.grants.{collection if is_name(collection) else _quote(collection)}"
                grants[collection] = _object(grant, where, ("mode",))["mode"]
            clients[name] = Client(
                destination=entry["destination"],
                hosted=entry["hosted"],
                grants=grants,
                limits=_mapping(entry["limits"], f"{path}.limits"),
            )

        literals = top["redact_literals"]
        if not isinstance(literals, (list, tuple)):
            _fail(f"redact_literals must be a list of strings, not {_show(literals)}")

        return cls(
            version=top["version"],
            serial=top["serial"],
            pools=pools,
            defaults=Limits(**_object(top["defaults"], "defaults", LIMIT_KEYS)),
            per_document=PerDocument(
                **_object(top["per_document"], "per_document", _PER_DOCUMENT_KEYS)
            ),
            approval=Approval(**_object(top["approval"], "approval", _APPROVAL_KEYS)),
            collections=collections,
            clients=clients,
            redact_literals=tuple(literals),
        )

    def to_json(self) -> dict[str, Any]:
        """The policy as a JSON object made of new plain dicts and lists.

        ``Policy.from_json(policy.to_json()) == policy`` always holds.
        """
        return {
            "version": self.version,
            "serial": self.serial,
            "pools": {name: pool.to_json() for name, pool in self.pools.items()},
            "defaults": self.defaults.to_json(),
            "per_document": self.per_document.to_json(),
            "approval": self.approval.to_json(),
            "collections": {name: entry.to_json() for name, entry in self.collections.items()},
            "clients": {name: entry.to_json() for name, entry in self.clients.items()},
            "redact_literals": list(self.redact_literals),
        }

    # -- questions ------------------------------------------------------------

    def client_names(self, hosted: bool | None = None) -> tuple[str, ...]:
        """The registered names in order: all of them, or only one kind."""
        return tuple(
            name
            for name, client in self.clients.items()
            if hosted is None or client.hosted is hosted
        )

    def limits_for(self, client: str) -> Limits:
        """The limits in force for a registered client: the defaults with its own values.

        Raises :class:`KeyError` for a name that is not registered.
        """
        return replace(self.defaults, **self.clients[client].limits)

    def pool_of(self, client: str) -> str:
        """``"hosted"`` or ``"local"`` for a registered client; :class:`KeyError` otherwise."""
        return self.clients[client].pool

    def pool_limits_for(self, client: str) -> PoolLimits:
        """The limits of the pool a registered client belongs to."""
        return self.pools[self.pool_of(client)]

    def effective_grant(self, client: str, collection: str) -> str | None:
        """The mode in which the gate lets ``client`` use ``collection``, or ``None``.

        ``None`` means that the collection does not exist for that client: the
        client is not registered, there is no such collection, there is no
        grant, or the client is hosted and the collection is not marked for
        hosted clients. A stored grant of the last kind is ignored.
        """
        entry = self.clients.get(client) if isinstance(client, str) else None
        target = self.collections.get(collection) if isinstance(collection, str) else None
        if entry is None or target is None:
            return None
        if entry.hosted and not target.allow_hosted:
            return None
        return entry.grants.get(collection)

    def effective_grants(self, client: str) -> dict[str, str]:
        """Every collection the gate lets ``client`` use, with its mode, in name order."""
        entry = self.clients.get(client) if isinstance(client, str) else None
        if entry is None:
            return {}
        grants: dict[str, str] = {}
        for collection in entry.grants:
            mode = self.effective_grant(client, collection)
            if mode is not None:
                grants[collection] = mode
        return grants

    # -- changed copies ---------------------------------------------------------

    def with_serial(self, serial: int) -> Policy:
        """The same policy under another serial number."""
        return replace(self, serial=serial)

    def with_client(self, name: str, *, hosted: bool, destination: str | None = None) -> Policy:
        """Register a client. It starts without grants and with the default limits.

        A hosted client needs a ``destination``; a local one gets
        :data:`LOCAL_DESTINATION` when none is given. A name that is already
        registered is refused.
        """
        _helper_name(name)
        if name in self.clients:
            _fail(f'an app named "{name}" is already registered')
        if len(self.clients) >= MAX_CLIENTS:
            _fail(f"at most {MAX_CLIENTS} apps can be registered")
        if not isinstance(hosted, bool):
            _fail("an app is either hosted or local: say which")
        text = _tidy(destination)
        if not text:
            if hosted:
                _fail(
                    "a hosted app needs a destination: say where its text is sent, "
                    'for example "Anthropic"'
                )
            text = LOCAL_DESTINATION
        _line(text, "the destination", 1, DESTINATION_MAX_CHARS)
        clients = {**self.clients, name: Client(destination=text, hosted=hosted)}
        return replace(self, clients=clients)

    def without_client(self, name: str) -> Policy:
        """Remove a client and, with it, its grants and its own limits."""
        self._registered(name)
        clients = {key: value for key, value in self.clients.items() if key != name}
        return replace(self, clients=clients)

    def with_grant(self, client: str, collection: str, mode: str) -> Policy:
        """Grant ``client`` the collection in ``mode`` (``"read"`` or ``"approve"``).

        An earlier grant of the same collection is replaced. A hosted client and
        a collection that is not marked for hosted clients raise
        :class:`HostedGrantError`: marking the collection is a decision of its
        own, and this method never takes it.
        """
        entry = self._registered(client)
        target = self._existing(collection)
        if not isinstance(mode, str) or mode not in MODES:
            _fail(f'a grant is either "{MODE_READ}" or "{MODE_APPROVE}", not {_show(mode)}')
        if entry.hosted and not target.allow_hosted:
            raise HostedGrantError(
                f'"{collection}" is not marked for hosted apps, and "{client}" is a hosted app. '
                "Marking the collection is a separate decision: text from it may then be sent "
                "to the provider of any hosted app you grant it to."
            )
        changed = replace(entry, grants={**entry.grants, collection: mode})
        return replace(self, clients={**self.clients, client: changed})

    def without_grant(self, client: str, collection: str) -> Policy:
        """Take the collection away from ``client``. Nothing changes if it had no grant."""
        entry = self._registered(client)
        grants = {key: value for key, value in entry.grants.items() if key != collection}
        return replace(self, clients={**self.clients, client: replace(entry, grants=grants)})

    def with_collection(
        self,
        name: str,
        *,
        note: str | None = None,
        allow_hosted: bool | None = None,
        show_titles: bool | None = None,
    ) -> Policy:
        """Create a collection, or change one. Arguments left out keep their value.

        A new collection has an empty note, is not marked for hosted clients
        and does not show titles. Taking the mark away leaves the grants of
        hosted clients in the policy; the gate ignores them from then on.
        """
        _helper_name(name)
        current = self.collections.get(name)
        if current is None:
            if len(self.collections) >= MAX_COLLECTIONS:
                _fail(f"at most {MAX_COLLECTIONS} collections can exist")
            current = Collection()
        text = current.note if note is None else _tidy(note)
        _line(text, "the note", 0, NOTE_MAX_CHARS)
        changed = Collection(
            note=text,
            allow_hosted=current.allow_hosted if allow_hosted is None else allow_hosted,
            show_titles=current.show_titles if show_titles is None else show_titles,
        )
        return replace(self, collections={**self.collections, name: changed})

    def without_collection(self, name: str) -> Policy:
        """Remove a collection and every grant of it. The documents are not this module's."""
        self._existing(name)
        collections = {key: value for key, value in self.collections.items() if key != name}
        clients = {
            key: replace(entry, grants={k: mode for k, mode in entry.grants.items() if k != name})
            for key, entry in self.clients.items()
        }
        return replace(self, collections=collections, clients=clients)

    def with_defaults(self, changes: Mapping[str, Any]) -> Policy:
        """Change some of the default limits; ``changes`` maps a limit's name to its new value."""
        values = _object(changes, "limits", LIMIT_KEYS, partial=True)
        return replace(self, defaults=replace(self.defaults, **values))

    def with_client_limits(self, client: str, changes: Mapping[str, Any]) -> Policy:
        """Change a client's own limits; ``None`` as a value returns that limit to the default."""
        entry = self._registered(client)
        values = _object(changes, "limits", LIMIT_KEYS, partial=True)
        limits = {**entry.limits, **values}
        limits = {key: value for key, value in limits.items() if value is not None}
        return replace(self, clients={**self.clients, client: replace(entry, limits=limits)})

    def with_pool_limits(self, pool: str, changes: Mapping[str, Any]) -> Policy:
        """Change the limits of the pool ``"hosted"`` or ``"local"``."""
        if not isinstance(pool, str) or pool not in POOLS:
            _fail(f'the group is either "{HOSTED}" or "{LOCAL}", not {_show(pool)}')
        values = _object(changes, "limits", POOL_KEYS, partial=True)
        return replace(self, pools={**self.pools, pool: replace(self.pools[pool], **values)})

    def with_per_document(self, changes: Mapping[str, Any]) -> Policy:
        """Change ``max_chars`` or ``max_fraction`` of the free allowance per document."""
        values = _object(changes, "per_document", _PER_DOCUMENT_KEYS, partial=True)
        return replace(self, per_document=replace(self.per_document, **values))

    def with_approval(self, changes: Mapping[str, Any]) -> Policy:
        """Change ``ttl_minutes``, the time an approval stays valid."""
        values = _object(changes, "approval", _APPROVAL_KEYS, partial=True)
        return replace(self, approval=replace(self.approval, **values))

    def with_redact_literal(self, text: str) -> Policy:
        """List one more string that is replaced in everything released.

        Refused: a string that is already listed (case and accents ignored), one
        with fewer than two letters or digits, and the 201st. The message never
        repeats the string.
        """
        if not isinstance(text, str):
            _fail("a listed string must be text")
        text = text.strip()
        if not REDACT_LITERAL_MIN_CHARS <= len(text) <= REDACT_LITERAL_MAX_CHARS:
            _fail(
                f"a listed string must have {REDACT_LITERAL_MIN_CHARS} to "
                f"{REDACT_LITERAL_MAX_CHARS} characters"
            )
        words = _folded_words(text)
        if sum(len(word) for word in words) < 2:
            _fail("a listed string needs at least two letters or digits")
        if any(_folded_words(listed) == words for listed in self.redact_literals):
            _fail("this string is already listed (case and accents are ignored)")
        if len(self.redact_literals) >= MAX_REDACT_LITERALS:
            _fail(f"at most {MAX_REDACT_LITERALS} strings can be listed")
        return replace(self, redact_literals=(*self.redact_literals, text))

    # -- internals --------------------------------------------------------------

    def _registered(self, name: Any) -> Client:
        entry = self.clients.get(name) if isinstance(name, str) else None
        if entry is None:
            _fail(f"no app is registered as {_quote(name)}")
        return entry

    def _existing(self, name: Any) -> Collection:
        entry = self.collections.get(name) if isinstance(name, str) else None
        if entry is None:
            _fail(f"there is no collection named {_quote(name)}")
        return entry


def _helper_name(name: Any) -> None:
    if not is_name(name):
        _fail(f"{_quote(name)} cannot be used as a name: {_NAME_RULE}")


def _tidy(text: Any) -> Any:
    """Owner input as the checks expect it: composed, without surrounding blanks."""
    if text is None:
        return ""
    if not isinstance(text, str):
        return text
    return unicodedata.normalize("NFC", text).strip()


def default_policy() -> Policy:
    """The policy of a new store: default limits, no client, no collection, no grant."""
    return Policy()


# ---------------------------------------------------------------------------
# What a session serves
# ---------------------------------------------------------------------------


def _kinds(kinds: str | None) -> str | None:
    if kinds is not None and (not isinstance(kinds, str) or kinds not in KINDS):
        _fail(f'the choice is "{HOSTED}", "{LOCAL}" or "{BOTH}", not {_show(kinds)}')
    return kinds


def choice_needed(policy: Policy, kinds: str | None) -> bool:
    """Whether the owner still has to choose which kind of client a session serves.

    True when hosted and local clients are both registered and no choice was
    made (``kinds`` is ``None``).
    """
    _kinds(kinds)
    return kinds is None and bool(policy.client_names(True)) and bool(policy.client_names(False))


def served_set(
    policy: Policy, kinds: str | None = None, paused: Iterable[str] = ()
) -> tuple[str, ...]:
    """The registered clients a session serves, in name order.

    ``kinds`` is the owner's choice for the session: ``"hosted"``, ``"local"``,
    ``"both"``, or ``None`` when none was made. ``paused`` holds the names the
    owner paused; names that are not registered are ignored.

    Without a choice every registered client is served, unless clients of both
    kinds are registered: then only the hosted ones are, and no local client is
    served until the owner has chosen. While a local name is served, any
    program running as the owner can call under it, also a hosted agent that
    can run commands; serving both kinds is therefore never the default.
    """
    choice = _kinds(kinds)
    if choice is None:
        choice = HOSTED if choice_needed(policy, None) else BOTH
    held_back = {paused} if isinstance(paused, str) else set(paused)
    return tuple(
        name
        for name, client in policy.clients.items()
        if name not in held_back and choice in (BOTH, client.pool)
    )


def kinds_served(policy: Policy, served: Iterable[str]) -> str:
    """``"hosted"``, ``"local"``, ``"both"`` or ``"none"``: the kinds among ``served``."""
    pools = {policy.clients[name].pool for name in served if name in policy.clients}
    if len(pools) == 2:
        return BOTH
    return pools.pop() if pools else "none"


# ---------------------------------------------------------------------------
# Does a change let more leave?
# ---------------------------------------------------------------------------


def _raised(old: Any, new: Any, keys: Iterable[str]) -> bool:
    return any(getattr(new, key) > getattr(old, key) for key in keys)


def widens(old: Policy, new: Policy) -> bool:
    """Whether ``new`` lets more leave than ``old`` does.

    True for: a client that was not registered before; a client whose kind
    changed (it then draws on the other pool's limits and allowances); a grant
    that is new or went from ``approve`` to ``read``, as stored or as honoured;
    a collection newly marked for hosted clients or newly showing titles; any
    limit raised, whether a default, a pool's, a client's limit in force, the
    allowance per document or the lifetime of an approval; and a listed string
    that is no longer listed.

    False for a policy that only takes away, and for changes of a note, of a
    destination or of ``serial``. When in doubt the answer is True: the caller
    asks for the passphrase again before a change that widens.
    """
    if _raised(old.defaults, new.defaults, LIMIT_KEYS):
        return True
    if any(_raised(old.pools[pool], new.pools[pool], POOL_KEYS) for pool in POOLS):
        return True
    if _raised(old.per_document, new.per_document, _PER_DOCUMENT_KEYS):
        return True
    if _raised(old.approval, new.approval, _APPROVAL_KEYS):
        return True
    if set(old.redact_literals) - set(new.redact_literals):
        return True

    for name, collection in new.collections.items():
        before = old.collections.get(name, Collection())
        if collection.allow_hosted and not before.allow_hosted:
            return True
        if collection.show_titles and not before.show_titles:
            return True

    for name, client in new.clients.items():
        earlier = old.clients.get(name)
        if earlier is None or earlier.hosted != client.hosted:
            return True
        if _raised(old.limits_for(name), new.limits_for(name), LIMIT_KEYS):
            return True
        for collection_name in client.grants:
            stored = _MODE_RANK[client.grants[collection_name]]
            if stored > _MODE_RANK[earlier.grants.get(collection_name)]:
                return True
            honoured = _MODE_RANK[new.effective_grant(name, collection_name)]
            if honoured > _MODE_RANK[old.effective_grant(name, collection_name)]:
                return True
    return False
