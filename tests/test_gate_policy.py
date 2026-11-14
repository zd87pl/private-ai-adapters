"""The gate's policy: its JSON shape, strict validation, grants, served names and widening.

The last part covers where a store lives and the paths inside it that need no key.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import random
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from wdva.errors import WDVAError
from wdva.gate import paths, policy
from wdva.gate.errors import GateError, PolicyError
from wdva.gate.policy import (
    Approval,
    Client,
    Collection,
    HostedGrantError,
    Limits,
    PerDocument,
    Policy,
    PoolLimits,
    choice_needed,
    default_policy,
    kinds_served,
    served_set,
    widens,
)

# The documented shape, with one hosted and one local client.
EXAMPLE: dict[str, Any] = {
    "version": 1,
    "serial": 7,
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
    "collections": {
        "home": {
            "note": "Lease, utilities, insurance",
            "allow_hosted": True,
            "show_titles": False,
        },
        "medical": {"note": "", "allow_hosted": False, "show_titles": False},
    },
    "clients": {
        "claude-desktop": {
            "destination": "Anthropic",
            "hosted": True,
            "grants": {"home": {"mode": "read"}},
            "limits": {"chars_per_day": 12000},
        },
        "lm-studio": {
            "destination": "local model on this computer",
            "hosted": False,
            "grants": {"medical": {"mode": "approve"}},
            "limits": {},
        },
    },
    "redact_literals": ["Jan Kowalski", "85010112345"],
}

SECRET = "Zyxwvut Qponmlk"  # a listed string that must never show up in a message


def example(**changes: Any) -> dict[str, Any]:
    """A fresh copy of the documented object, with top-level keys replaced."""
    obj = copy.deepcopy(EXAMPLE)
    obj.update(changes)
    return obj


def put(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """The documented object with the value at ``path`` replaced (or added)."""
    obj = example()
    target = obj
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return obj


def drop(path: tuple[str, ...]) -> dict[str, Any]:
    """The documented object without the key at ``path``."""
    obj = example()
    target = obj
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return obj


def loaded() -> Policy:
    return Policy.from_json(example())


def refused(obj: Any) -> str:
    """Assert that ``obj`` is not a policy, and return what the error says."""
    with pytest.raises(PolicyError) as caught:
        Policy.from_json(obj)
    return str(caught.value)


# -- a new store ---------------------------------------------------------------


def test_new_store_reaches_nothing() -> None:
    first = default_policy()
    assert first.clients == {}
    assert first.collections == {}
    assert first.redact_literals == ()
    assert first.client_names() == ()
    assert served_set(first) == ()
    assert first.effective_grants("claude-desktop") == {}
    assert first.effective_grant("claude-desktop", "home") is None
    # The numbers a new store starts with.
    assert first.to_json() == {
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


def test_a_registered_client_without_a_grant_reaches_nothing() -> None:
    changed = (
        default_policy()
        .with_collection("home", allow_hosted=True)
        .with_client("claude-desktop", hosted=True, destination="Anthropic")
        .with_client("lm-studio", hosted=False)
    )
    for name in ("claude-desktop", "lm-studio"):
        assert changed.effective_grants(name) == {}
        assert changed.effective_grant(name, "home") is None


# -- the JSON shape --------------------------------------------------------------


def test_json_shape_round_trips_exactly() -> None:
    obj = loaded().to_json()
    assert obj == EXAMPLE
    assert list(obj) == list(EXAMPLE)  # the keys come in the documented order
    assert json.loads(json.dumps(obj)) == EXAMPLE  # plain JSON values only
    assert Policy.from_json(obj) == loaded()
    assert Policy.from_json(default_policy().to_json()) == default_policy()


def test_json_output_is_a_copy() -> None:
    held = loaded()
    obj = held.to_json()
    obj["clients"]["claude-desktop"]["grants"]["medical"] = {"mode": "read"}
    obj["collections"]["medical"]["allow_hosted"] = True
    obj["redact_literals"].clear()
    assert held.to_json() == EXAMPLE


def test_policy_keeps_no_reference_to_its_input() -> None:
    obj = example()
    held = Policy.from_json(obj)
    obj["clients"]["claude-desktop"]["grants"]["medical"] = {"mode": "read"}
    obj["clients"]["claude-desktop"]["limits"]["chars_per_day"] = 99_000_000
    obj["collections"]["medical"]["allow_hosted"] = True
    obj["redact_literals"].append("something else")
    assert held.to_json() == EXAMPLE


def test_order_of_names_does_not_matter() -> None:
    obj = example()
    obj["collections"] = dict(reversed(list(obj["collections"].items())))
    obj["clients"] = dict(reversed(list(obj["clients"].items())))
    assert Policy.from_json(obj) == loaded()
    assert list(Policy.from_json(obj).to_json()["clients"]) == ["claude-desktop", "lm-studio"]


def test_a_whole_number_fraction_is_stored_as_a_fraction() -> None:
    held = Policy.from_json(put(("per_document", "max_fraction"), 1))
    assert held.per_document.max_fraction == 1.0
    assert isinstance(held.to_json()["per_document"]["max_fraction"], float)
    assert Policy.from_json(held.to_json()) == held


def test_policy_error_is_a_value_error_of_the_gate() -> None:
    with pytest.raises(PolicyError) as caught:
        Policy.from_json(example(color="blue"))
    assert isinstance(caught.value, ValueError)
    assert isinstance(caught.value, GateError)
    assert isinstance(caught.value, WDVAError)
    assert issubclass(HostedGrantError, PolicyError)


# -- strict validation -------------------------------------------------------------

OBJECTS: list[tuple[str, ...]] = [
    (),
    ("pools",),
    ("pools", "hosted"),
    ("pools", "local"),
    ("defaults",),
    ("per_document",),
    ("approval",),
    ("collections", "home"),
    ("clients", "claude-desktop"),
    ("clients", "claude-desktop", "grants", "home"),
    ("clients", "claude-desktop", "limits"),
]


@pytest.mark.parametrize("path", OBJECTS, ids=lambda p: ".".join(p) or "top")
def test_unknown_keys_are_refused(path: tuple[str, ...]) -> None:
    message = refused(put((*path, "colour"), 1))
    assert "colour" in message
    assert "unknown key" in message


REQUIRED: list[tuple[str, ...]] = [
    *[(key,) for key in EXAMPLE],
    ("pools", "hosted"),
    ("pools", "local"),
    ("pools", "hosted", "chars_per_day"),
    ("pools", "local", "chars_per_30_days"),
    *[("defaults", key) for key in EXAMPLE["defaults"]],
    ("per_document", "max_chars"),
    ("per_document", "max_fraction"),
    ("approval", "ttl_minutes"),
    ("collections", "home", "note"),
    ("collections", "home", "allow_hosted"),
    ("collections", "home", "show_titles"),
    ("clients", "claude-desktop", "destination"),
    ("clients", "claude-desktop", "hosted"),
    ("clients", "claude-desktop", "grants"),
    ("clients", "claude-desktop", "limits"),
]


@pytest.mark.parametrize("path", REQUIRED, ids=".".join)
def test_missing_keys_are_refused(path: tuple[str, ...]) -> None:
    assert path[-1] in refused(drop(path))


NAN = float("nan")
INFINITY = float("inf")
NFD = unicodedata.normalize("NFD", "caf" + chr(0xE9))

BAD_VALUES: list[tuple[tuple[str, ...], Any]] = [
    # version and serial
    *[(("version",), bad) for bad in (0, 2, "1", True, 1.0, None)],
    *[(("serial",), bad) for bad in (-1, 1.5, "7", True, None, 2**53)],
    # pools
    *[(("pools",), bad) for bad in ([], "hosted", None, 5)],
    (("pools", "hosted"), 40000),
    *[
        (("pools", pool, key), bad)
        for pool in ("hosted", "local")
        for key in ("chars_per_day", "chars_per_30_days")
        for bad in (-1, 100_000_001, "40000", 40000.0, True, None)
    ],
    # defaults
    (("defaults",), []),
    *[(("defaults", "chars_per_call"), bad) for bad in (499, 8001, 0, -1, "2000", 2000.0, True)],
    *[(("defaults", "chars_per_day"), bad) for bad in (-1, 100_000_001, None, [20000])],
    *[(("defaults", "chars_per_30_days"), bad) for bad in (-1, 100_000_001, "150000")],
    *[(("defaults", "searches_per_day"), bad) for bad in (-1, 10_001, 1.5)],
    *[(("defaults", "calls_per_minute"), bad) for bad in (0, 121, -30, False)],
    # the allowance per document and the approval lifetime
    *[(("per_document", "max_chars"), bad) for bad in (-1, 4_000_001, 8000.5, "8000", True)],
    *[
        (("per_document", "max_fraction"), bad)
        for bad in (-0.01, 1.01, 2, -1, "0.5", True, None, NAN, INFINITY)
    ],
    *[(("approval", "ttl_minutes"), bad) for bad in (0, 61, -10, 10.0, "10", True)],
    # collections
    *[(("collections",), bad) for bad in ([], "home", None)],
    (("collections", "home"), "Lease"),
    *[
        (("collections", "home", "note"), bad)
        for bad in (
            "x" * 201,
            "two\nlines",
            "carriage\rreturn",
            "tab\there",
            "escape" + chr(0x1B) + "[31m",
            "zero" + chr(0x200B) + "width",
            "right-to-left" + chr(0x202E),
            "line" + chr(0x2028) + "separator",
            "variation" + chr(0xFE0F),
            " leading",
            "trailing ",
            NFD,
            "see https://example.com/lease",
            "[lease](example.com)",
            "![lease]",
            "<b>lease",
            "lease -----",
            5,
            None,
            ["Lease"],
        )
    ],
    *[
        (("collections", "home", key), bad)
        for key in ("allow_hosted", "show_titles")
        for bad in (1, 0, "true", None)
    ],
    # clients
    *[(("clients",), bad) for bad in ([], "claude-desktop", None)],
    (("clients", "claude-desktop"), "Anthropic"),
    *[
        (("clients", "claude-desktop", "destination"), bad)
        for bad in ("", "x" * 81, "two\nlines", " Anthropic", "http://anthropic.com", 5, None)
    ],
    *[(("clients", "claude-desktop", "hosted"), bad) for bad in (1, 0, "yes", None)],
    *[(("clients", "claude-desktop", "grants"), bad) for bad in ([], "home", None, ["home"])],
    (("clients", "claude-desktop", "grants"), {"tax": {"mode": "read"}}),  # no such collection
    *[(("clients", "claude-desktop", "limits"), bad) for bad in ([], None, 12000)],
    *[
        (("clients", "claude-desktop", "limits", key), bad)
        for key, bad in (
            ("chars_per_call", 499),
            ("chars_per_call", 8001),
            ("chars_per_day", -1),
            ("chars_per_day", "12000"),
            ("chars_per_30_days", 100_000_001),
            ("searches_per_day", 10_001),
            ("calls_per_minute", 0),
            ("calls_per_minute", 121),
            ("calls_per_minute", True),
        )
    ],
    # listed strings
    *[(("redact_literals",), bad) for bad in ("Jan Kowalski", None, {"Jan": "Kowalski"}, 5)],
    (("redact_literals",), ["Jan Kowalski", 85010112345]),
    (("redact_literals",), ["J"]),
    (("redact_literals",), ["x" * 201]),
    (("redact_literals",), ["-."]),  # no letter or digit at all
    (("redact_literals",), ["a."]),  # only one
    (("redact_literals",), ["Jan Kowalski", "Jan Kowalski"]),
    (("redact_literals",), ["Jan Kowalski", "JAN  KOWALSKI"]),
    (("redact_literals",), ["Jan Kowalski", "jan-kowalski."]),
    (("redact_literals",), ["Zo" + chr(0xEB), "zoe"]),  # the same once accents are ignored
    (("redact_literals",), [f"name {n}" for n in range(201)]),
]


@pytest.mark.parametrize(("path", "bad"), BAD_VALUES, ids=lambda v: None)
def test_bad_values_are_refused(path: tuple[str, ...], bad: Any) -> None:
    message = refused(put(path, bad))
    assert message  # a sentence, not an empty error
    assert "\n" not in message
    assert len(message) < 300


def test_unknown_keys_and_bad_values_are_refused() -> None:
    """Every key of the documented shape is checked: a wrong value or a stray sibling fails."""

    def leaves(obj: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
        if isinstance(obj, dict) and obj:
            return [leaf for key, value in obj.items() for leaf in leaves(value, (*path, key))]
        return [path]

    wrong = object()  # no JSON value at all
    for path in leaves(EXAMPLE):
        obj = put(path, wrong)
        with pytest.raises(PolicyError):
            Policy.from_json(obj)
    for path in OBJECTS:
        with pytest.raises(PolicyError):
            Policy.from_json(put((*path, "extra"), 1))
    for not_an_object in (None, [], "policy", 7, [example()]):
        with pytest.raises(PolicyError):
            Policy.from_json(not_an_object)


BOUNDARIES: list[tuple[tuple[str, ...], Any]] = [
    (("serial",), 0),
    (("serial",), 2**53 - 1),
    *[
        (("pools", pool, key), value)
        for pool in ("hosted", "local")
        for key in ("chars_per_day", "chars_per_30_days")
        for value in (0, 100_000_000)
    ],
    (("defaults", "chars_per_call"), 500),
    (("defaults", "chars_per_call"), 8000),
    (("defaults", "chars_per_day"), 0),
    (("defaults", "chars_per_day"), 100_000_000),
    (("defaults", "chars_per_30_days"), 0),
    (("defaults", "chars_per_30_days"), 100_000_000),
    (("defaults", "searches_per_day"), 0),
    (("defaults", "searches_per_day"), 10_000),
    (("defaults", "calls_per_minute"), 1),
    (("defaults", "calls_per_minute"), 120),
    (("per_document", "max_chars"), 0),
    (("per_document", "max_chars"), 4_000_000),
    (("per_document", "max_fraction"), 0),
    (("per_document", "max_fraction"), 0.0),
    (("per_document", "max_fraction"), 1.0),
    (("approval", "ttl_minutes"), 1),
    (("approval", "ttl_minutes"), 60),
    (("collections", "home", "note"), "x" * 200),
    (("collections", "home", "note"), "Umowa najmu, " + chr(0x142) + chr(0xF3) + "d" + chr(0x17A)),
    (("clients", "claude-desktop", "destination"), "x"),
    (("clients", "claude-desktop", "destination"), "x" * 80),
    (("clients", "claude-desktop", "limits"), dict(EXAMPLE["defaults"])),
    (("clients", "claude-desktop", "limits", "chars_per_call"), 8000),
    (("redact_literals",), []),
    (("redact_literals",), ["ab", "x" * 200]),
    (("redact_literals",), [f"name {n}" for n in range(200)]),
]


@pytest.mark.parametrize(("path", "value"), BOUNDARIES, ids=lambda v: None)
def test_values_at_the_edge_of_their_range_are_accepted(path: tuple[str, ...], value: Any) -> None:
    held = Policy.from_json(put(path, value))
    assert Policy.from_json(held.to_json()) == held


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Home",
        "home papers",
        "-home",
        "_home",
        "home.papers",
        "h" * 33,
        "home\n",
        "caf" + chr(0xE9),
        "(unregistered)",
    ],
)
def test_names_must_fit_the_pattern(name: str) -> None:
    obj = example()
    obj["collections"][name] = {"note": "", "allow_hosted": False, "show_titles": False}
    refused(obj)
    obj = example()
    obj["clients"][name] = {"destination": "x", "hosted": False, "grants": {}, "limits": {}}
    refused(obj)
    assert not policy.is_name(name)
    with pytest.raises(PolicyError):
        default_policy().with_collection(name)
    with pytest.raises(PolicyError):
        default_policy().with_client(name, hosted=False)


@pytest.mark.parametrize("name", ["h", "0", "home", "home-2", "home_2", "9-to-5", "h" * 32])
def test_names_that_fit_the_pattern(name: str) -> None:
    assert policy.is_name(name)
    changed = default_policy().with_collection(name).with_client(name, hosted=False)
    assert name in changed.collections
    assert name in changed.clients


def test_at_most_100_collections_and_100_clients() -> None:
    entry = {"note": "", "allow_hosted": False, "show_titles": False}
    obj = example(collections={f"c{n}": dict(entry) for n in range(100)}, clients={})
    full = Policy.from_json(obj)
    with pytest.raises(PolicyError, match="100"):
        full.with_collection("one-more")
    assert full.with_collection("c7", note="changed").collections["c7"].note == "changed"
    obj["collections"]["one-more"] = dict(entry)
    assert "100" in refused(obj)

    client = {"destination": "x", "hosted": False, "grants": {}, "limits": {}}
    obj = example(clients={f"a{n}": copy.deepcopy(client) for n in range(100)})
    full = Policy.from_json(obj)
    with pytest.raises(PolicyError, match="100"):
        full.with_client("one-more", hosted=False)
    obj["clients"]["one-more"] = copy.deepcopy(client)
    assert "100" in refused(obj)


def test_no_invalid_policy_can_be_built_by_hand() -> None:
    held = loaded()
    with pytest.raises(PolicyError):
        dataclasses.replace(held, approval=Approval(ttl_minutes=61))
    with pytest.raises(PolicyError):
        dataclasses.replace(held, defaults=Limits(chars_per_call=9000))
    with pytest.raises(PolicyError):
        dataclasses.replace(held, per_document=PerDocument(max_fraction=1.5))
    with pytest.raises(PolicyError):
        dataclasses.replace(held, pools={"hosted": PoolLimits()})
    with pytest.raises(PolicyError):
        dataclasses.replace(held, collections={})  # the grants then point nowhere
    with pytest.raises(PolicyError):
        Policy(clients={"x": Client(destination="", hosted=True)})
    with pytest.raises(PolicyError):
        Policy(clients={"x": Client(destination="X", hosted=True, grants={"home": "read"})})
    with pytest.raises(PolicyError):
        Policy(collections={"home": Collection(note="a\nb")})
    with pytest.raises(PolicyError):
        Policy(collections={"home": {"note": ""}})  # type: ignore[dict-item]
    with pytest.raises(PolicyError):
        Policy(redact_literals=("x",))
    with pytest.raises(PolicyError):
        Policy(version=2)


def test_a_policy_cannot_be_changed_in_place() -> None:
    held = loaded()
    with pytest.raises(dataclasses.FrozenInstanceError):
        held.serial = 8  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        held.clients["claude-desktop"].hosted = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        held.collections["medical"].allow_hosted = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        held.clients["claude-desktop"].grants["medical"] = "read"  # type: ignore[index]
    with pytest.raises(TypeError):
        held.clients["claude-desktop"].limits["chars_per_day"] = 10**8  # type: ignore[index]
    with pytest.raises(TypeError):
        held.collections["tax"] = Collection(allow_hosted=True)  # type: ignore[index]
    with pytest.raises(TypeError):
        held.clients["evil"] = Client(destination="x", hosted=False)  # type: ignore[index]
    with pytest.raises(TypeError):
        held.pools["hosted"] = PoolLimits(chars_per_day=10**8)  # type: ignore[index]
    assert held.to_json() == EXAMPLE


def test_listed_strings_stay_out_of_messages_and_repr() -> None:
    held = Policy.from_json(example(redact_literals=[SECRET]))
    assert SECRET not in repr(held)
    assert "Zyxwvut" not in repr(held)

    for literals in (
        [SECRET, SECRET.upper()],
        [SECRET + "x" * 200],
        [SECRET, 5],
        [SECRET] + [f"name {n}" for n in range(200)],
    ):
        message = refused(example(redact_literals=literals))
        assert "zyxwvut" not in message.casefold()

    with pytest.raises(PolicyError) as caught:
        held.with_redact_literal(SECRET.lower())
    assert "zyxwvut" not in str(caught.value).casefold()
    with pytest.raises(PolicyError) as caught:
        held.with_redact_literal(SECRET + "x" * 200)
    assert "zyxwvut" not in str(caught.value).casefold()


def test_a_note_the_policy_accepts_passes_the_check_on_released_text() -> None:
    # A note and a destination are shown to agents in the status result, which is
    # checked like every released text. Whatever the policy accepts must pass that
    # check, or one stored note could make every status call fail.
    textproc = pytest.importorskip("wdva.gate.textproc")
    alphabet = [
        *"abc XYZ 019 .,;:!?-_/()[]<>",
        "://",
        "](",
        "![",
        "-----",
        "\n",
        "\t",
        chr(0xE9),
        "e" + chr(0x301),
        *[chr(code) for code in (0x1B, 0xA0, 0xAD, 0x34F, 0x200B, 0x202E, 0x2028, 0x2800)],
        *[chr(code) for code in (0x3164, 0xFE0F, 0xE0101, 0x180B, 0x17B4, 0x1F600)],
    ]
    rng = random.Random(20261112)  # noqa: S311 - a repeatable sweep, not a secret
    first = default_policy()
    accepted = 0
    for _ in range(4000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
        for build in (
            lambda text=text: first.with_collection("home", note=text).collections["home"].note,
            lambda text=text: (
                first.with_client("app", hosted=True, destination=text).clients["app"].destination
            ),
        ):
            try:
                stored = build()
            except PolicyError:
                continue
            accepted += 1
            assert textproc.check_sanitized(stored) == [], ascii(stored)
            assert len(stored.splitlines()) <= 1, ascii(stored)
    assert accepted > 200  # the sweep is not refusing everything


# -- grants ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "grant",
    [
        {},
        {"mode": "write"},
        {"mode": "search"},
        {"mode": ""},
        {"mode": "READ"},
        {"mode": "read "},
        {"mode": None},
        {"mode": True},
        {"mode": ["read"]},
        {"mode": "read", "until": "2027-01-01"},
        {"Mode": "read"},
        "read",
        None,
        ["read"],
    ],
    ids=repr,
)
def test_mode_is_required_and_one_of_two(grant: Any) -> None:
    refused(put(("clients", "claude-desktop", "grants", "home"), grant))
    refused(put(("clients", "lm-studio", "grants", "medical"), grant))


@pytest.mark.parametrize("mode", ["write", "", "READ", None, True, ["read"], "none"])
def test_grant_helper_takes_only_the_two_modes(mode: Any) -> None:
    held = loaded()
    with pytest.raises(PolicyError, match='a grant is either "read" or "approve"'):
        held.with_grant("lm-studio", "home", mode)
    assert held.to_json() == EXAMPLE


def test_both_modes_are_accepted_and_reported() -> None:
    for mode in ("read", "approve"):
        held = Policy.from_json(
            put(("clients", "claude-desktop", "grants", "home"), {"mode": mode})
        )
        assert held.clients["claude-desktop"].grants == {"home": mode}
        assert held.effective_grant("claude-desktop", "home") == mode
        assert loaded().with_grant("lm-studio", "home", mode).effective_grants("lm-studio") == {
            "home": mode,
            "medical": "approve",
        }


@pytest.mark.parametrize("mode", ["read", "approve"])
def test_hosted_client_cannot_be_granted_an_unmarked_collection(mode: str) -> None:
    held = loaded()
    assert not held.collections["medical"].allow_hosted
    with pytest.raises(HostedGrantError) as caught:
        held.with_grant("claude-desktop", "medical", mode)
    message = str(caught.value)
    assert "not marked for hosted" in message
    assert "separate decision" in message
    assert "wdva" not in message  # the owner acts in the app: no command is named
    assert "--" not in message
    assert held.to_json() == EXAMPLE

    # A local client may be granted the same collection.
    assert held.with_grant("lm-studio", "medical", mode).effective_grant("lm-studio", "medical")
    # Marking the collection is its own step; after it the grant is possible.
    marked = held.with_collection("medical", allow_hosted=True)
    assert "medical" not in marked.effective_grants("claude-desktop")  # marking grants nothing
    granted = marked.with_grant("claude-desktop", "medical", mode)
    assert granted.effective_grant("claude-desktop", "medical") == mode


def test_forbidden_grant_in_policy_is_ignored_by_the_gate() -> None:
    obj = put(("clients", "claude-desktop", "grants", "medical"), {"mode": "read"})
    held = Policy.from_json(obj)  # validation accepts a hand-written grant of that kind
    assert held.clients["claude-desktop"].grants == {"home": "read", "medical": "read"}
    assert held.to_json() == obj

    assert held.effective_grant("claude-desktop", "medical") is None
    assert held.effective_grants("claude-desktop") == {"home": "read"}
    # The local client's grant of the same collection is untouched.
    assert held.effective_grant("lm-studio", "medical") == "approve"

    # The grant counts from the moment the owner marks the collection, and stops
    # counting when the mark is taken away again.
    marked = held.with_collection("medical", allow_hosted=True)
    assert marked.effective_grants("claude-desktop") == {"home": "read", "medical": "read"}
    unmarked = marked.with_collection("medical", allow_hosted=False)
    assert unmarked.clients["claude-desktop"].grants["medical"] == "read"
    assert unmarked.effective_grant("claude-desktop", "medical") is None
    assert unmarked.effective_grants("lm-studio") == {"medical": "approve"}


def test_taking_the_mark_away_cuts_off_every_hosted_client() -> None:
    held = loaded().with_client("chatgpt-desktop", hosted=True, destination="OpenAI")
    held = held.with_grant("chatgpt-desktop", "home", "approve").with_grant(
        "lm-studio", "home", "read"
    )
    unmarked = held.with_collection("home", allow_hosted=False)
    assert unmarked.effective_grants("claude-desktop") == {}
    assert unmarked.effective_grants("chatgpt-desktop") == {}
    assert unmarked.effective_grants("lm-studio") == {"home": "read", "medical": "approve"}


def test_effective_grants_of_unknown_names_are_empty() -> None:
    held = loaded()
    assert held.effective_grants("nobody") == {}
    assert held.effective_grant("nobody", "home") is None
    assert held.effective_grant("claude-desktop", "nowhere") is None
    assert held.effective_grant("lm-studio", "home") is None  # registered, not granted
    for odd in (None, 5, ["claude-desktop"], {"a": 1}):
        assert held.effective_grants(odd) == {}  # type: ignore[arg-type]
        assert held.effective_grant(odd, "home") is None  # type: ignore[arg-type]
        assert held.effective_grant("claude-desktop", odd) is None  # type: ignore[arg-type]


# -- limits, pools, the allowance -----------------------------------------------


def test_limits_in_force_are_the_defaults_with_the_clients_own_values() -> None:
    held = loaded()
    assert held.limits_for("lm-studio") == held.defaults == Limits()
    assert held.limits_for("claude-desktop") == Limits(chars_per_day=12000)
    assert held.limits_for("claude-desktop").chars_per_30_days == 150000
    with pytest.raises(KeyError):
        held.limits_for("nobody")


def test_pool_follows_the_hosted_field() -> None:
    obj = put(("pools", "local", "chars_per_day"), 5000)
    held = Policy.from_json(obj)
    assert held.pool_of("claude-desktop") == "hosted"
    assert held.pool_of("lm-studio") == "local"
    assert held.clients["lm-studio"].pool == "local"
    assert held.pool_limits_for("claude-desktop") == PoolLimits(40000, 300000)
    assert held.pool_limits_for("lm-studio") == PoolLimits(5000, 300000)
    with pytest.raises(KeyError):
        held.pool_of("nobody")
    assert held.client_names() == ("claude-desktop", "lm-studio")
    assert held.client_names(hosted=True) == ("claude-desktop",)
    assert held.client_names(hosted=False) == ("lm-studio",)


@pytest.mark.parametrize(
    ("max_chars", "fraction", "length", "allowance"),
    [
        (8000, 0.5, 6912, 3456),
        (8000, 0.5, 5, 3),  # rounded up
        (8000, 0.5, 100_000, 8000),  # the smaller of the two
        (8000, 0.1, 30, 3),  # a tenth of 30 is 3, whatever binary floating point says
        (8000, 0.07, 100, 7),
        (8000, 0.29, 100, 29),
        (8000, 1.0, 6912, 6912),
        (8000, 1.0, 9000, 8000),
        (8000, 0.0, 6912, 0),
        (0, 1.0, 6912, 0),
        (8000, 0.5, 0, 0),
        (8000, 0.001, 1, 1),
    ],
)
def test_free_allowance(max_chars: int, fraction: float, length: int, allowance: int) -> None:
    assert PerDocument(max_chars, fraction).free_allowance(length) == allowance


# -- hard ceilings -----------------------------------------------------------------


def test_hard_ceilings_cannot_be_raised() -> None:
    assert policy.MAX_RESULT_CHARS == 8000
    assert policy.MAX_SEARCH_RESULTS == 10
    assert policy.MAX_FRACTION == 1.0
    assert policy.MAX_APPROVAL_MINUTES == 60
    assert policy.MAX_REDACT_LITERALS == 200
    assert (policy.REDACT_LITERAL_MIN_CHARS, policy.REDACT_LITERAL_MAX_CHARS) == (2, 200)
    assert policy.GATE_CALLS_PER_MINUTE == 120
    assert policy.LIMIT_RANGES["chars_per_call"] == (500, 8000)
    assert policy.LIMIT_RANGES["calls_per_minute"] == (1, 120)
    assert policy.TTL_RANGE == (1, 60)
    with pytest.raises(TypeError):
        policy.LIMIT_RANGES["chars_per_call"] = (500, 80_000)  # type: ignore[index]

    # In a stored policy.
    for path, above in (
        (("defaults", "chars_per_call"), 8001),
        (("clients", "claude-desktop", "limits", "chars_per_call"), 8001),
        (("defaults", "calls_per_minute"), 121),
        (("clients", "lm-studio", "limits", "calls_per_minute"), 121),
        (("per_document", "max_fraction"), 1.0001),
        (("approval", "ttl_minutes"), 61),
        (("redact_literals",), [f"name {n}" for n in range(201)]),
        (("redact_literals",), ["x" * 201]),
        (("redact_literals",), ["x"]),
    ):
        refused(put(path, above))

    # Through every method that changes a limit.
    held = loaded()
    for change in (
        lambda p: p.with_defaults({"chars_per_call": 8001}),
        lambda p: p.with_defaults({"calls_per_minute": 121}),
        lambda p: p.with_client_limits("claude-desktop", {"chars_per_call": 8001}),
        lambda p: p.with_client_limits("lm-studio", {"calls_per_minute": 121}),
        lambda p: p.with_pool_limits("hosted", {"chars_per_day": 100_000_001}),
        lambda p: p.with_per_document({"max_fraction": 1.0001}),
        lambda p: p.with_per_document({"max_chars": 4_000_001}),
        lambda p: p.with_approval({"ttl_minutes": 61}),
        lambda p: p.with_redact_literal("x"),
        lambda p: p.with_redact_literal("x" * 201),
    ):
        with pytest.raises(PolicyError):
            change(held)
    assert held.to_json() == EXAMPLE

    full = Policy.from_json(example(redact_literals=[f"name {n}" for n in range(200)]))
    with pytest.raises(PolicyError, match="at most 200 strings can be listed"):
        full.with_redact_literal("one more")


# -- which clients a session serves ------------------------------------------------


def three_clients() -> Policy:
    return loaded().with_client("chatgpt-desktop", hosted=True, destination="OpenAI")


def test_served_set_rules() -> None:
    both = three_clients()
    hosted = ("chatgpt-desktop", "claude-desktop")
    local = ("lm-studio",)

    # The owner's choice.
    assert served_set(both, "hosted") == hosted
    assert served_set(both, "local") == local
    assert served_set(both, "both") == ("chatgpt-desktop", "claude-desktop", "lm-studio")

    # No choice yet, both kinds registered: no local client is served.
    assert choice_needed(both, None)
    assert served_set(both) == hosted
    assert served_set(both, None, ()) == hosted
    for kinds in ("hosted", "local", "both"):
        assert not choice_needed(both, kinds)

    # Only one kind registered: everything is served and nothing needs choosing.
    only_hosted = both.without_client("lm-studio")
    assert not choice_needed(only_hosted, None)
    assert served_set(only_hosted) == hosted
    only_local = both.without_client("claude-desktop").without_client("chatgpt-desktop")
    assert not choice_needed(only_local, None)
    assert served_set(only_local) == local
    # A choice of the kind that is not registered serves nothing.
    assert served_set(only_local, "hosted") == ()
    assert served_set(only_hosted, "local") == ()

    # Paused names are left out; names that are not registered are ignored.
    assert served_set(both, "both", {"claude-desktop"}) == ("chatgpt-desktop", "lm-studio")
    assert served_set(both, None, ["chatgpt-desktop", "nobody"]) == ("claude-desktop",)
    assert served_set(both, "both", ("chatgpt-desktop", "claude-desktop", "lm-studio")) == ()
    assert served_set(both, "local", {"nobody"}) == local
    assert served_set(both, "both", "lm-studio") == hosted  # one name, given as text

    # Nothing registered: nothing served, whatever is chosen.
    for kinds in (None, "hosted", "local", "both"):
        assert served_set(default_policy(), kinds) == ()
    assert not choice_needed(default_policy(), None)

    # Only registered names are ever served.
    for kinds in (None, "hosted", "local", "both"):
        assert set(served_set(both, kinds)) <= set(both.clients)


@pytest.mark.parametrize("kinds", ["mixed", "", "HOSTED", "all", "none", 1, True, ["hosted"]])
def test_served_set_refuses_an_unknown_choice(kinds: Any) -> None:
    with pytest.raises(PolicyError):
        served_set(three_clients(), kinds)
    with pytest.raises(PolicyError):
        choice_needed(three_clients(), kinds)


def test_a_newly_registered_local_client_waits_for_the_choice() -> None:
    only_hosted = default_policy().with_client("claude-desktop", hosted=True, destination="A")
    assert served_set(only_hosted) == ("claude-desktop",)
    with_local = only_hosted.with_client("lm-studio", hosted=False)
    assert served_set(with_local) == ("claude-desktop",)
    assert choice_needed(with_local, None)
    assert served_set(with_local, "both") == ("claude-desktop", "lm-studio")


def test_kinds_served() -> None:
    both = three_clients()
    assert kinds_served(both, served_set(both)) == "hosted"
    assert kinds_served(both, served_set(both, "local")) == "local"
    assert kinds_served(both, served_set(both, "both")) == "both"
    assert kinds_served(both, ()) == "none"
    assert kinds_served(both, ["nobody"]) == "none"
    assert kinds_served(both, ["nobody", "lm-studio"]) == "local"


# -- changed copies ----------------------------------------------------------------


def test_registering_a_client() -> None:
    first = default_policy()
    hosted = first.with_client("claude-desktop", hosted=True, destination="  Anthropic ")
    assert hosted.clients["claude-desktop"] == Client(destination="Anthropic", hosted=True)
    assert hosted.clients["claude-desktop"].grants == {}
    assert hosted.limits_for("claude-desktop") == first.defaults
    assert hosted.serial == first.serial  # whoever stores the policy numbers it
    assert first == default_policy()

    local = hosted.with_client("lm-studio", hosted=False)
    assert local.clients["lm-studio"].destination == "local model on this computer"
    assert local.with_client("ollama", hosted=False, destination="my GPU box").clients[
        "ollama"
    ] == Client(destination="my GPU box", hosted=False)

    for destination in (None, "", "   "):
        with pytest.raises(PolicyError, match="destination"):
            first.with_client("claude-desktop", hosted=True, destination=destination)
    with pytest.raises(PolicyError, match="already registered"):
        local.with_client("lm-studio", hosted=False)
    with pytest.raises(PolicyError, match="already registered"):
        local.with_client("lm-studio", hosted=True, destination="Somewhere")
    with pytest.raises(PolicyError):
        first.with_client("claude-desktop", hosted="yes", destination="A")  # type: ignore[arg-type]
    with pytest.raises(PolicyError):
        first.with_client("claude-desktop", hosted=True, destination="x" * 81)
    with pytest.raises(PolicyError):
        first.with_client("claude-desktop", hosted=True, destination="two\nlines")


def test_removing_a_client_removes_its_grants_and_limits() -> None:
    held = loaded()
    gone = held.without_client("claude-desktop")
    assert "claude-desktop" not in gone.clients
    assert gone.effective_grants("claude-desktop") == {}
    assert gone.clients["lm-studio"] == held.clients["lm-studio"]
    again = gone.with_client("claude-desktop", hosted=True, destination="Anthropic")
    assert again.clients["claude-desktop"].grants == {}
    assert again.clients["claude-desktop"].limits == {}
    with pytest.raises(PolicyError, match="nobody"):
        held.without_client("nobody")
    assert held.to_json() == EXAMPLE


def test_granting_and_revoking() -> None:
    held = loaded()
    wider = held.with_grant("lm-studio", "home", "approve")
    assert wider.effective_grants("lm-studio") == {"home": "approve", "medical": "approve"}
    assert wider.with_grant("lm-studio", "home", "read").effective_grant("lm-studio", "home") == (
        "read"
    )
    back = wider.without_grant("lm-studio", "home")
    assert back == held
    assert back.without_grant("lm-studio", "home") == held  # nothing to take away: no error
    for client, collection in (("nobody", "home"), ("lm-studio", "nowhere")):
        with pytest.raises(PolicyError):
            held.with_grant(client, collection, "read")
    with pytest.raises(PolicyError):
        held.without_grant("nobody", "home")
    assert held.to_json() == EXAMPLE


def test_creating_and_changing_a_collection() -> None:
    first = default_policy()
    created = first.with_collection("tax")
    assert created.collections["tax"] == Collection(note="", allow_hosted=False, show_titles=False)

    noted = created.with_collection("tax", note="  Returns since 2019 ")
    assert noted.collections["tax"] == Collection(note="Returns since 2019")
    marked = noted.with_collection("tax", allow_hosted=True)
    assert marked.collections["tax"] == Collection(note="Returns since 2019", allow_hosted=True)
    titled = marked.with_collection("tax", show_titles=True)
    assert titled.collections["tax"] == Collection("Returns since 2019", True, True)
    assert titled.with_collection("tax") == titled  # nothing given: nothing changes
    cleared = titled.with_collection("tax", note="", allow_hosted=False, show_titles=False)
    assert cleared.collections["tax"] == Collection()

    # A note typed with a separate accent is stored in the composed form.
    assert first.with_collection("tax", note=NFD).collections["tax"].note == "caf" + chr(0xE9)

    for note in ("x" * 201, "two\nlines", "see https://example.com", 5):
        with pytest.raises(PolicyError, match="note"):
            created.with_collection("tax", note=note)  # type: ignore[arg-type]
    with pytest.raises(PolicyError):
        created.with_collection("tax", allow_hosted="yes")  # type: ignore[arg-type]
    assert first == default_policy()


def test_removing_a_collection_removes_every_grant_of_it() -> None:
    held = loaded().with_grant("lm-studio", "home", "read")
    gone = held.without_collection("home")
    assert "home" not in gone.collections
    assert gone.clients["claude-desktop"].grants == {}
    assert gone.clients["lm-studio"].grants == {"medical": "approve"}
    assert Policy.from_json(gone.to_json()) == gone
    # A collection of the same name made later starts without grants and without the mark.
    again = gone.with_collection("home")
    assert again.effective_grants("lm-studio") == {"medical": "approve"}
    assert not again.collections["home"].allow_hosted
    with pytest.raises(PolicyError, match="nowhere"):
        held.without_collection("nowhere")


def test_changing_limits() -> None:
    held = loaded()

    lower = held.with_defaults({"chars_per_day": 5000, "searches_per_day": 10})
    assert lower.defaults == Limits(chars_per_day=5000, searches_per_day=10)
    assert lower.limits_for("claude-desktop").chars_per_day == 12000  # its own value stays
    assert lower.limits_for("lm-studio").chars_per_day == 5000

    own = held.with_client_limits("lm-studio", {"chars_per_call": 900, "calls_per_minute": 5})
    assert own.clients["lm-studio"].limits == {"chars_per_call": 900, "calls_per_minute": 5}
    assert own.limits_for("lm-studio") == Limits(chars_per_call=900, calls_per_minute=5)
    back = own.with_client_limits("lm-studio", {"chars_per_call": None, "calls_per_minute": None})
    assert back == held
    assert (
        held.with_client_limits("claude-desktop", {"chars_per_day": None}).limits_for(
            "claude-desktop"
        )
        == Limits()
    )

    pooled = held.with_pool_limits("local", {"chars_per_day": 1000})
    assert pooled.pools["local"] == PoolLimits(1000, 300000)
    assert pooled.pools["hosted"] == PoolLimits()

    assert held.with_per_document({"max_chars": 2000}).per_document == PerDocument(2000, 0.5)
    assert held.with_per_document({"max_fraction": 1}).per_document == PerDocument(8000, 1.0)
    assert held.with_approval({"ttl_minutes": 3}).approval == Approval(3)

    for change in (
        lambda p: p.with_defaults({"chars_per_week": 1}),
        lambda p: p.with_defaults({"chars_per_day": None}),
        lambda p: p.with_defaults([("chars_per_day", 1)]),
        lambda p: p.with_client_limits("lm-studio", {"chars_per_week": 1}),
        lambda p: p.with_client_limits("nobody", {"chars_per_day": 1}),
        lambda p: p.with_client_limits("lm-studio", {"chars_per_day": "1"}),
        lambda p: p.with_pool_limits("remote", {"chars_per_day": 1}),
        lambda p: p.with_pool_limits("local", {"searches_per_day": 1}),
        lambda p: p.with_per_document({"max_share": 0.5}),
        lambda p: p.with_per_document({"max_fraction": "0.5"}),
        lambda p: p.with_approval({"ttl_seconds": 30}),
        lambda p: p.with_approval({"ttl_minutes": 0}),
    ):
        with pytest.raises(PolicyError):
            change(held)
    assert held.to_json() == EXAMPLE


def test_listing_a_string() -> None:
    held = loaded()
    more = held.with_redact_literal("  PL61 1090 1014  ")
    assert more.redact_literals == ("Jan Kowalski", "85010112345", "PL61 1090 1014")
    assert held.redact_literals == ("Jan Kowalski", "85010112345")
    for same in ("jan kowalski", "JAN KOWALSKI", "Jan  Kowalski", "(Jan-Kowalski)"):
        with pytest.raises(PolicyError, match="already listed"):
            held.with_redact_literal(same)
    for weak in ("", " ", "x", "x.", "--", " . ", 5, None):
        with pytest.raises(PolicyError):
            held.with_redact_literal(weak)  # type: ignore[arg-type]
    # Another form of the same name is another string.
    assert len(held.with_redact_literal("Kowalski Jan").redact_literals) == 3


def test_serial_is_only_changed_on_request() -> None:
    held = loaded()
    assert held.with_serial(8).serial == 8
    assert held.with_serial(8).with_serial(7) == held
    for changed in (
        held.with_collection("tax"),
        held.with_client("ollama", hosted=False),
        held.with_grant("lm-studio", "home", "read"),
        held.with_defaults({"chars_per_day": 1}),
        held.with_redact_literal("another one"),
    ):
        assert changed.serial == held.serial
        assert changed != held
    with pytest.raises(PolicyError):
        held.with_serial(-1)


# -- widening ------------------------------------------------------------------------


def base() -> Policy:
    """Three collections, a hosted client that may ask for approval in one, a local client."""
    return (
        default_policy()
        .with_collection("home", note="Lease", allow_hosted=True)
        .with_collection("tax", allow_hosted=True)
        .with_collection("medical")
        .with_client("claude-desktop", hosted=True, destination="Anthropic")
        .with_client("lm-studio", hosted=False)
        .with_grant("claude-desktop", "home", "approve")
        .with_grant("lm-studio", "medical", "approve")
        .with_client_limits("claude-desktop", {"chars_per_day": 12000})
        .with_redact_literal("Jan Kowalski")
        .with_redact_literal("85010112345")
    )


def _json_change(path: tuple[str, ...], value: Any) -> Callable[[Policy], Policy]:
    def change(held: Policy) -> Policy:
        obj = held.to_json()
        target = obj
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return Policy.from_json(obj)

    return change


def _flip_kind(name: str) -> Callable[[Policy], Policy]:
    def change(held: Policy) -> Policy:
        obj = held.to_json()
        obj["clients"][name]["hosted"] = not obj["clients"][name]["hosted"]
        return Policy.from_json(obj)

    return change


# Each of these lets more leave. Undoing it only takes away.
WIDENING: dict[str, Callable[[Policy], Policy]] = {
    "a new hosted client": lambda p: p.with_client("chatgpt", hosted=True, destination="OpenAI"),
    "a new local client": lambda p: p.with_client("ollama", hosted=False),
    "a new grant in read mode": lambda p: p.with_grant("claude-desktop", "tax", "read"),
    "a new grant in approve mode": lambda p: p.with_grant("claude-desktop", "tax", "approve"),
    "a new grant for a local client": lambda p: p.with_grant("lm-studio", "home", "approve"),
    "a grant raised from approve to read": lambda p: p.with_grant("claude-desktop", "home", "read"),
    "a local grant raised to read": lambda p: p.with_grant("lm-studio", "medical", "read"),
    "a stored grant the gate ignores for now": _json_change(
        ("clients", "claude-desktop", "grants", "medical"), {"mode": "approve"}
    ),
    "a collection marked for hosted clients": lambda p: p.with_collection(
        "medical", allow_hosted=True
    ),
    "a collection showing titles": lambda p: p.with_collection("home", show_titles=True),
    "an ungranted collection showing titles": lambda p: p.with_collection("tax", show_titles=True),
    "a new collection that is marked": lambda p: p.with_collection("work", allow_hosted=True),
    "a new collection that shows titles": lambda p: p.with_collection("work", show_titles=True),
    **{
        f"default {key} raised": (
            lambda p, key=key: p.with_defaults({key: Limits().to_json()[key] + 1})
        )
        for key in policy.LIMIT_KEYS
    },
    **{
        f"{pool} pool {key} raised": (
            lambda p, pool=pool, key=key: p.with_pool_limits(
                pool, {key: PoolLimits().to_json()[key] + 1}
            )
        )
        for pool in policy.POOLS
        for key in policy.POOL_KEYS
    },
    **{
        f"a client's own {key} above the default": (
            lambda p, key=key: p.with_client_limits("lm-studio", {key: Limits().to_json()[key] + 1})
        )
        for key in policy.LIMIT_KEYS
    },
    "a client's own limit raised": lambda p: p.with_client_limits(
        "claude-desktop", {"chars_per_day": 12001}
    ),
    "a client's lower own limit dropped": lambda p: p.with_client_limits(
        "claude-desktop", {"chars_per_day": None}
    ),
    "more characters per document": lambda p: p.with_per_document({"max_chars": 8001}),
    "a larger fraction per document": lambda p: p.with_per_document({"max_fraction": 0.51}),
    "the whole document": lambda p: p.with_per_document({"max_fraction": 1}),
    "a longer approval lifetime": lambda p: p.with_approval({"ttl_minutes": 11}),
    "a listed string removed": _json_change(("redact_literals",), ["Jan Kowalski"]),
    "every listed string removed": _json_change(("redact_literals",), []),
    "a listed string swapped for another": _json_change(
        ("redact_literals",), ["Jan Kowalski", "85010112346"]
    ),
}


@pytest.mark.parametrize("name", list(WIDENING))
def test_widens_is_true_for_everything_that_lets_more_leave(name: str) -> None:
    old = base()
    new = WIDENING[name](old)
    assert new != old
    assert widens(old, new) is True
    # The serial number plays no part.
    assert widens(old.with_serial(3), new.with_serial(9)) is True
    assert widens(old.with_serial(9), new.with_serial(3)) is True


# The swap removes one string and adds one: undoing it removes the added one, which widens too.
ONE_WAY = [name for name in WIDENING if name != "a listed string swapped for another"]


@pytest.mark.parametrize("name", ONE_WAY)
def test_widens_is_false_for_the_same_change_undone(name: str) -> None:
    narrow = base()
    wide = WIDENING[name](narrow)
    assert widens(wide, narrow) is False


# These take away, or change nothing that leaves.
NOT_WIDENING: dict[str, Callable[[Policy], Policy]] = {
    "nothing": lambda p: p,
    "a new serial": lambda p: p.with_serial(p.serial + 1),
    "a client removed": lambda p: p.without_client("claude-desktop"),
    "every client removed": lambda p: p.without_client("claude-desktop").without_client(
        "lm-studio"
    ),
    "a grant removed": lambda p: p.without_grant("claude-desktop", "home"),
    "a collection removed": lambda p: p.without_collection("home"),
    "the mark taken away": lambda p: p.with_collection("home", allow_hosted=False),
    "a new collection without mark or titles": lambda p: p.with_collection("work", note="Work"),
    "a note changed": lambda p: p.with_collection("home", note="Lease and utilities"),
    "a destination changed": _json_change(
        ("clients", "claude-desktop", "destination"), "Anthropic, PBC"
    ),
    "a string listed": lambda p: p.with_redact_literal("PL61 1090 1014"),
    "every default lowered": lambda p: p.with_defaults(
        {
            "chars_per_call": 500,
            "chars_per_day": 0,
            "chars_per_30_days": 0,
            "searches_per_day": 0,
            "calls_per_minute": 1,
        }
    ),
    "the pools lowered": lambda p: p.with_pool_limits(
        "hosted", {"chars_per_day": 0}
    ).with_pool_limits("local", {"chars_per_30_days": 0}),
    "a client's own limit lowered": lambda p: p.with_client_limits(
        "claude-desktop", {"chars_per_day": 11999}
    ),
    "a client's own limit equal to the default": lambda p: p.with_client_limits(
        "lm-studio", {"chars_per_day": 20000, "calls_per_minute": 30}
    ),
    "a client's own limit below the default": lambda p: p.with_client_limits(
        "lm-studio", {"chars_per_call": 501}
    ),
    "less per document": lambda p: p.with_per_document({"max_chars": 0, "max_fraction": 0}),
    "a shorter approval lifetime": lambda p: p.with_approval({"ttl_minutes": 1}),
    "the listed strings in another order": _json_change(
        ("redact_literals",), ["85010112345", "Jan Kowalski"]
    ),
}


@pytest.mark.parametrize("name", list(NOT_WIDENING))
def test_widens_is_false_for_what_only_takes_away(name: str) -> None:
    old = base()
    assert widens(old, NOT_WIDENING[name](old)) is False


@pytest.mark.parametrize("key", policy.LIMIT_KEYS)
def test_widens_counts_a_raised_default_that_no_client_uses_yet(key: str) -> None:
    # The defaults apply to every client registered later, and to the shared
    # call rate of names that are not served.
    empty = default_policy()
    raised = empty.with_defaults({key: Limits().to_json()[key] + 1})
    assert widens(empty, raised) is True
    assert widens(raised, empty) is False

    # Also when every registered client has its own value for that limit.
    lowest = policy.LIMIT_RANGES[key][0]
    own = base()
    for name in own.clients:
        own = own.with_client_limits(name, {key: lowest})
    assert widens(own, own.with_defaults({key: Limits().to_json()[key] + 1})) is True


@pytest.mark.parametrize("name", ["claude-desktop", "lm-studio"])
def test_widens_is_true_when_a_client_changes_kind(name: str) -> None:
    # Either way the client draws on another pool's limits and allowances.
    old = base().with_collection("medical", allow_hosted=True)
    assert widens(old, _flip_kind(name)(old)) is True


def test_widens_sees_a_grant_that_starts_to_count() -> None:
    stored = WIDENING["a stored grant the gate ignores for now"](base())
    assert stored.effective_grant("claude-desktop", "medical") is None
    marked = stored.with_collection("medical", allow_hosted=True)
    assert marked.effective_grant("claude-desktop", "medical") == "approve"
    assert widens(stored, marked) is True
    assert widens(marked, stored) is False


@pytest.mark.parametrize("widening", list(WIDENING))
def test_widens_is_true_when_a_change_widens_and_narrows_at_once(widening: str) -> None:
    old = base()
    narrowed = (
        old.with_approval({"ttl_minutes": 1})
        .with_per_document({"max_chars": 0})
        .with_redact_literal("PL61 1090 1014")
        .without_grant("lm-studio", "medical")
    )
    if widening != "a longer approval lifetime":
        assert widens(old, WIDENING[widening](narrowed)) is True
    else:
        assert widens(old, narrowed.with_collection("medical", allow_hosted=True)) is True


def test_widens_is_false_for_any_run_of_narrowing_steps() -> None:
    held = base()
    for name in (
        "a shorter approval lifetime",
        "less per document",
        "a string listed",
        "a client's own limit lowered",
        "every default lowered",
        "the pools lowered",
        "the mark taken away",
        "a grant removed",
        "a collection removed",
        "every client removed",
    ):
        changed = NOT_WIDENING[name](held)
        assert widens(held, changed) is False, name
        assert widens(base(), changed) is False, name
        held = changed


def test_replacing_a_client_under_the_same_name_takes_two_steps() -> None:
    # Removing never widens; registering always does, also under a name used before.
    old = base()
    removed = old.without_client("lm-studio")
    assert widens(old, removed) is False
    assert widens(removed, removed.with_client("lm-studio", hosted=False)) is True


# -- where a store lives --------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory for the test, with nothing about stores in the environment."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("WDVA_GATE_STORE", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return tmp_path


def test_default_store_location(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.default_store() == home / ".local" / "share" / "locktender"
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    assert paths.default_store() == home / "data" / "locktender"
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")  # not absolute: ignored
    assert paths.default_store() == home / ".local" / "share" / "locktender"
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert paths.default_store() == home / ".local" / "share" / "locktender"

    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))  # plays no part on macOS
    assert paths.default_store() == home / "Library" / "Application Support" / "locktender"


def test_a_store_made_under_an_earlier_name_is_still_found(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.EARLIER_STORE_DIR_NAMES == ("wdva-gate",)
    share = home / ".local" / "share"
    earlier = share / "wdva-gate"
    current = share / "locktender"

    share.mkdir(parents=True)
    (share / "wdva-gate-file").write_text("not a directory")
    assert paths.default_store() == current  # nothing there yet: the current name

    earlier.mkdir()
    assert paths.default_store() == earlier  # a vault somebody already has is not hidden
    assert paths.resolve_store(None) == earlier
    monkeypatch.setenv("WDVA_GATE_STORE", str(home / "from-env"))
    assert paths.resolve_store(None) == home / "from-env"  # a named store wins as before
    monkeypatch.delenv("WDVA_GATE_STORE")

    current.mkdir()
    assert paths.default_store() == current  # once both exist, the current name

    # The earlier name is looked for in the same place only.
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    support_dir = home / "Library" / "Application Support"
    assert paths.default_store() == support_dir / "locktender"
    (support_dir / "wdva-gate").mkdir(parents=True)
    assert paths.default_store() == support_dir / "wdva-gate"


def test_resolve_store_prefers_the_argument_then_the_environment(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    default = home / ".local" / "share" / "locktender"
    assert paths.resolve_store(None) == default
    assert paths.resolve_store() == default
    assert paths.resolve_store("") == default

    monkeypatch.setenv("WDVA_GATE_STORE", str(home / "from-env"))
    assert paths.resolve_store(None) == home / "from-env"
    assert paths.resolve_store("") == home / "from-env"
    assert paths.resolve_store(str(home / "from-arg")) == home / "from-arg"
    assert paths.resolve_store(home / "from-arg") == home / "from-arg"

    monkeypatch.setenv("WDVA_GATE_STORE", "")  # empty counts as not set
    assert paths.resolve_store(None) == default


def test_resolve_store_gives_an_absolute_path(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(home)
    assert paths.resolve_store("stores/mine") == home / "stores" / "mine"
    assert paths.resolve_store(os.path.join("stores", "..", "mine")) == home / "mine"
    monkeypatch.setenv("WDVA_GATE_STORE", "from-env")
    assert paths.resolve_store(None) == home / "from-env"
    assert paths.resolve_store(os.path.join("~", "vault")) == home / "vault"
    assert paths.resolve_store(None).is_absolute()
    # Nothing is created by asking.
    assert list(home.iterdir()) == []


def test_run_paths() -> None:
    store = Path("some") / "store"
    assert paths.run_dir(store) == store / "run"
    assert paths.lock_path(store) == store / "run" / "lock"
    assert paths.socket_path(store) == store / "run" / "gate.sock"
    assert paths.socket_path(str(store)) == store / "run" / "gate.sock"
    assert paths.ENV_STORE == "WDVA_GATE_STORE"


def _store_with_socket_path_of(size: int) -> Path:
    """An absolute store path whose socket path has exactly ``size`` bytes."""
    root = Path(os.path.abspath(os.sep))
    fixed = len(os.fsencode(paths.socket_path(root / "x"))) - 1
    assert size > fixed
    return root / ("s" * (size - fixed))


def test_socket_path_may_have_100_bytes() -> None:
    assert paths.SOCKET_PATH_LIMIT == 100
    store = _store_with_socket_path_of(100)
    path = paths.check_socket_path(store)
    assert path == store / "run" / "gate.sock"
    assert len(os.fsencode(path)) == 100
    assert paths.check_socket_path(_store_with_socket_path_of(40)).is_absolute()


@pytest.mark.parametrize("size", [101, 104, 108, 218])
def test_longer_socket_path_is_refused(size: int) -> None:
    with pytest.raises(GateError) as caught:
        paths.check_socket_path(_store_with_socket_path_of(size))
    assert str(caught.value) == (
        f"the store path is too long for a local socket ({size} bytes, limit 100); "
        "choose a shorter --store"
    )


def test_socket_path_is_measured_in_bytes_not_characters() -> None:
    store = Path(os.path.abspath(os.sep)) / (chr(0xE9) * 50)  # 50 characters, 100 bytes
    path = paths.socket_path(store)
    size = len(os.fsencode(path))
    assert len(str(path)) < 100 < size
    with pytest.raises(GateError, match=f"{size} bytes"):
        paths.check_socket_path(store)


def test_socket_path_of_a_relative_store_is_measured_from_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    here = len(os.fsencode(os.path.abspath("s")))
    with pytest.raises(GateError):
        paths.check_socket_path("s" * 100)
    if here + len(os.sep + os.path.join("run", "gate.sock")) <= 100:
        assert paths.check_socket_path("s").is_absolute()


@pytest.mark.parametrize("name", paths.SYNCED_FOLDER_NAMES)
def test_synced_folder_names_are_recognised(name: str, tmp_path: Path) -> None:
    assert set(paths.SYNCED_FOLDER_NAMES) == {
        "Dropbox",
        "OneDrive",
        "Google Drive",
        "iCloud Drive",
        "Mobile Documents",
        "CloudStorage",
    }
    for written in (name, name.lower(), name.upper()):
        inside = tmp_path / written / "private" / "store"
        assert paths.synced_folder_component(inside) == written
        assert paths.looks_synced(inside)
        assert paths.looks_synced(str(tmp_path / written))  # the folder itself
    for other in (f"{name}-backup", f"My{name}", f"{name} old", name.replace(" ", "")[:-1]):
        assert paths.synced_folder_component(tmp_path / other / "store") is None
        assert not paths.looks_synced(tmp_path / other / "store")


def test_ordinary_paths_do_not_look_synced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not paths.looks_synced(tmp_path / "store")
    assert not paths.looks_synced(tmp_path / ".local" / "share" / "locktender")
    monkeypatch.chdir(tmp_path)
    assert not paths.looks_synced("store")
    # A relative path is judged by where it really is.
    (tmp_path / "Dropbox").mkdir()
    monkeypatch.chdir(tmp_path / "Dropbox")
    assert paths.looks_synced("store")


def test_a_link_into_a_synced_folder_is_noticed(tmp_path: Path) -> None:
    target = tmp_path / "Dropbox" / "vault"
    target.mkdir(parents=True)
    link = tmp_path / "vault"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")
    assert paths.synced_folder_component(link / "store") == "Dropbox"
