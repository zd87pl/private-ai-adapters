"""The app behind the interface, against real stores: lock state, owner mode, and every call."""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from wdva._version import __version__
from wdva.errors import WDVAError
from wdva.gate import brand, extract, messages
from wdva.gate.app import service as service_module
from wdva.gate.app.api import Service, ServiceError
from wdva.gate.app.service import NOT_REGISTERED, OWNER_MODE, GateService
from wdva.gate.core import Gate
from wdva.gate.errors import LedgerError
from wdva.gate.store import init_store
from wdva.gate.types import Caller

from . import gate_support as gs
from .conftest import FakeClock

# The product's name comes from one place; the tests take it from there too.
PRODUCT, SLUG = brand.PRODUCT, brand.SLUG

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

CLAUDE = Caller("claude-desktop", "claude-ai", "1.2", "2025-11-25", "stdio")
LM = Caller("lm-studio", "lm-studio", "0.4", "2025-06-18", "stdio")
STRANGER = Caller("somebody-else", "curl", "8", None, "stdio")
REF = re.compile(r"ref=(d-[0-9a-f]{12}\.[0-9]+)")
ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
UNLOCKED_ONLY = ("unlocked_at", "locks_at", "owner_mode_until", "encryption", "serving", "today")


@contextlib.contextmanager
def refused(status: int, code: str) -> Iterator[list[ServiceError]]:
    """The block must be answered with this error; the list then holds it."""
    caught: list[ServiceError] = []
    with pytest.raises(ServiceError) as error:
        yield caught
    caught.append(error.value)
    assert (error.value.status, error.value.code) == (status, code), error.value.message


@pytest.fixture
def service(short_store_dir: Path, gate_clock: FakeClock) -> Iterator[GateService]:
    """The service for a place where no vault exists yet."""
    made = GateService(short_store_dir, clock=gate_clock, scrypt_n=gs.FAST_SCRYPT)
    yield made
    made.close()


@pytest.fixture
def vault(short_store_dir: Path, gate_clock: FakeClock) -> Iterator[GateService]:
    """The service for a locked vault that holds the sample documents and the sample policy."""
    gs.make_store(short_store_dir, clock=gate_clock).close()
    made = GateService(short_store_dir, clock=gate_clock, scrypt_n=gs.FAST_SCRYPT)
    yield made
    made.close()


@pytest.fixture
def unlocked(vault: GateService) -> GateService:
    vault.unlock(passphrase=gs.PASSPHRASE)
    return vault


def first_ref(service: GateService, caller: Caller, query: str, collection: str = "") -> str:
    reply = service.backend.search(caller, query, collection)
    assert reply.code == "OK", reply.text
    return REF.findall(reply.text)[0]


def picture(service: GateService) -> str:
    """Everything a change could alter, as one text."""
    state = service.state()
    return json.dumps(
        [
            service.settings(),
            service.apps()["apps"],
            service.documents()["collections"],
            state["serving"],
            state["record"]["head_seq"],
        ],
        sort_keys=True,
    )


def expire_owner_mode(clock: FakeClock) -> None:
    clock.advance(seconds=OWNER_MODE.total_seconds())


# -- the three forms of the state ------------------------------------------------------


def test_service_has_every_method_the_server_calls(service: GateService) -> None:
    protocol: Service = service  # the type checker's view; the loop below is the run-time one
    for name in (n for n in vars(Service) if not n.startswith("_")):
        assert callable(getattr(protocol, name)), name


def test_state_without_a_store(service: GateService, short_store_dir: Path) -> None:
    state = service.state()
    assert state["store"] == {"exists": False, "path": str(short_store_dir)}
    assert state["locked"] is True
    assert state["version"] == __version__
    assert state["demo"] is False
    for key in (*UNLOCKED_ONLY, "record", "warnings"):
        assert state[key] is None, key
    assert not short_store_dir.exists(), "asking for the state created something"


def test_state_locked_and_unlocked(vault: GateService, gate_clock: FakeClock) -> None:
    locked = vault.state()
    assert locked["locked"] is True
    assert locked["store"]["exists"] is True
    assert locked["store"]["sealed_files"] == 1 + 7 + 4 + 4  # key slot, records, texts, originals
    assert locked["store"]["sealed_bytes"] > 0
    assert locked["record"] == {
        "head_seq": 7,
        "head_hash": locked["record"]["head_hash"],
        "head_time": None,
        "verified": False,
        "verified_at": None,
        "previous_session": None,
    }
    assert re.fullmatch("[0-9a-f]{8}", locked["record"]["head_hash"])
    assert locked["warnings"] == []
    for key in UNLOCKED_ONLY:
        assert locked[key] is None, key

    start = gate_clock.now
    state = vault.unlock(passphrase=gs.PASSPHRASE)
    assert state["locked"] is False
    assert state["unlocked_at"] == "2026-11-12T09:00:00Z"
    assert state["locks_at"] == "2026-11-12T17:00:00Z"
    assert state["owner_mode_until"] == "2026-11-12T09:10:00Z"
    assert state["encryption"] == {
        "cipher": "XChaCha20-Poly1305",
        "kdf": "scrypt (N=2^14, r=8, p=1)",
        "documents": 4,
        "collections": 3,
        "characters": state["encryption"]["characters"],
        "originals_kept": 4,
    }
    assert state["encryption"]["characters"] > 3000
    assert state["record"]["head_seq"] == 8  # the unlock record
    assert state["record"]["verified"] is True
    assert state["record"]["verified_at"] == "2026-11-12T09:00:00Z"
    assert state["record"]["previous_session"] == "first"
    assert state["record"]["head_time"] == "2026-11-12T09:00:00Z"
    assert state["serving"] == {
        "hosted": ["claude-desktop"],
        "local": [],
        "paused": [],
        "choice_needed": True,
        "kinds": "hosted",
    }
    assert state["today"] == {
        "releases": 0,
        "characters": 0,
        "questions": 0,
        "refusals": 0,
        "approvals_waiting": 0,
    }
    assert [w["code"] for w in state["warnings"]] in ([], ["no_sdk"], ["python_too_old"])
    assert start == gate_clock.now
    json.dumps(state, allow_nan=False)


def test_create_store_unlocks_a_new_vault_with_nothing_reachable(
    service: GateService, short_store_dir: Path
) -> None:
    with refused(400, "invalid"):
        service.create_store(passphrase="eleven char")
    assert not short_store_dir.exists()

    state = service.create_store(passphrase=gs.PASSPHRASE)
    assert state["locked"] is False
    assert state["store"]["exists"] is True
    assert state["record"]["previous_session"] == "first"
    assert state["serving"]["kinds"] == "none"
    assert service.apps()["apps"] == []
    assert service.documents()["collections"] == []
    assert service.settings()["defaults"]["chars_per_day"] == 20000
    assert gs.PASSPHRASE not in json.dumps(state)
    assert service.backend.status(CLAUDE).code == "NOT_ALLOWED"

    with refused(409, "conflict"):
        service.create_store(passphrase=gs.PASSPHRASE)
    service.lock()
    with refused(409, "conflict"):
        service.create_store(passphrase="another passphrase")
    assert service.unlock(passphrase=gs.PASSPHRASE)["locked"] is False


def test_create_store_refuses_a_folder_that_is_in_use(
    service: GateService, short_store_dir: Path
) -> None:
    short_store_dir.mkdir(mode=0o700)
    (short_store_dir / "notes.txt").write_text("mine")
    with refused(409, "conflict"):
        service.create_store(passphrase=gs.PASSPHRASE)
    assert sorted(p.name for p in short_store_dir.iterdir() if p.name != "run") == ["notes.txt"]
    assert service.state()["locked"] is True


# -- unlocking and locking ---------------------------------------------------------------


def test_wrong_passphrase_is_403_and_leaves_the_vault_free(vault: GateService) -> None:
    for wrong in ("wrong passphrase", "", gs.PASSPHRASE + " "):
        with refused(403, "wrong_passphrase") as caught:
            vault.unlock(passphrase=wrong)
        assert wrong == "" or wrong not in caught[0].message
        assert vault.state()["locked"] is True
    # The lock that the failed attempts took was given back each time.
    assert vault.unlock(passphrase=gs.PASSPHRASE)["locked"] is False


def test_unlock_without_a_store_is_not_found(service: GateService) -> None:
    with refused(404, "not_found"):
        service.unlock(passphrase=gs.PASSPHRASE)


@pytest.mark.parametrize("hours", [0, 0.019, 168.5, -1, float("inf")])
def test_unlock_refuses_hours_out_of_range(vault: GateService, hours: float) -> None:
    with refused(400, "invalid"):
        vault.unlock(passphrase=gs.PASSPHRASE, hours=hours)
    assert vault.state()["locked"] is True


def test_unlock_takes_the_hours_of_the_request_or_the_default(
    short_store_dir: Path, gate_clock: FakeClock
) -> None:
    gs.make_store(short_store_dir, clock=gate_clock).close()
    service = GateService(
        short_store_dir, clock=gate_clock, scrypt_n=gs.FAST_SCRYPT, lock_after_hours=2
    )
    try:
        assert service.unlock(passphrase=gs.PASSPHRASE)["locks_at"] == "2026-11-12T11:00:00Z"
        service.lock()
        state = service.unlock(passphrase=gs.PASSPHRASE, hours=0.5)
        assert state["locks_at"] == "2026-11-12T09:30:00Z"
    finally:
        service.close()
    with pytest.raises(ValueError, match="lock_after_hours"):
        GateService(short_store_dir, lock_after_hours=0)


HOLD_THE_LOCK = """
import sys, time
from wdva.gate.store import take_lock
lock = take_lock(sys.argv[1])
print("held", flush=True)
time.sleep(120)
"""


def test_unlock_while_another_process_has_the_vault_is_409_busy(
    vault: GateService, short_store_dir: Path
) -> None:
    other = subprocess.Popen(
        [sys.executable, "-c", HOLD_THE_LOCK, str(short_store_dir)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert other.stdout is not None
        assert other.stdout.readline().strip() == "held"
        with refused(409, "busy"):
            vault.unlock(passphrase=gs.PASSPHRASE)
        # The lock is asked for before the passphrase is looked at.
        with refused(409, "busy"):
            vault.unlock(passphrase="not the passphrase")
        assert vault.state()["locked"] is True
    finally:
        other.kill()
        other.wait()
    assert vault.unlock(passphrase=gs.PASSPHRASE)["locked"] is False


def test_lock_twice_and_unlock_twice(unlocked: GateService, gate_clock: FakeClock) -> None:
    first = unlocked.state()
    gate_clock.advance(minutes=3)
    # A second unlock checks the passphrase and renews owner mode; the session stays.
    with refused(403, "wrong_passphrase"):
        unlocked.unlock(passphrase="wrong passphrase")
    again = unlocked.unlock(passphrase=gs.PASSPHRASE)
    assert again["unlocked_at"] == first["unlocked_at"]
    assert again["locks_at"] == first["locks_at"]
    assert again["owner_mode_until"] == "2026-11-12T09:13:00Z"
    assert again["record"]["head_seq"] == first["record"]["head_seq"]

    assert unlocked.lock()["locked"] is True
    head = unlocked.state()["record"]["head_seq"]
    assert head == first["record"]["head_seq"] + 1  # the lock record
    assert unlocked.lock()["locked"] is True
    assert unlocked.state()["record"]["head_seq"] == head
    unlocked.close()
    assert unlocked.state()["record"]["head_seq"] == head


def test_lock_ends_everything_the_session_had(unlocked: GateService, short_store_dir: Path) -> None:
    socket_path = short_store_dir / "run" / "gate.sock"
    assert socket_path.exists()
    ref = first_ref(unlocked, CLAUDE, "rent deposit")
    unlocked.lock()
    assert not socket_path.exists()
    reply = unlocked.backend.read(CLAUDE, ref)
    assert (reply.code, reply.text, reply.released) == ("LOCKED", messages.locked(), False)
    # Another program can have the vault now.
    gs.reopen(short_store_dir).close()


LOCKED_CALLS: dict[str, Callable[[GateService], Any]] = {
    "reauth": lambda s: s.reauth(passphrase=gs.PASSPHRASE),
    "set_serving": lambda s: s.set_serving(kinds="hosted"),
    "verify": lambda s: s.verify(),
    "activity": lambda s: s.activity(),
    "activity_detail": lambda s: s.activity_detail(seq=1),
    "approvals": lambda s: s.approvals(),
    "release": lambda s: s.release(app="claude-desktop", ref="d-000000000000.1", body_sha256=""),
    "dismiss": lambda s: s.dismiss(app="claude-desktop", ref="d-000000000000.1"),
    "documents": lambda s: s.documents(),
    "add_document": lambda s: s.add_document(collection="home", name="a.txt", data=b"text"),
    "remove_document": lambda s: s.remove_document(id="d-000000000000"),
    "set_collection": lambda s: s.set_collection(name="home", note="x"),
    "remove_collection": lambda s: s.remove_collection(name="home"),
    "apps": lambda s: s.apps(),
    "add_app": lambda s: s.add_app(name="codex", hosted=True, destination="OpenAI"),
    "remove_app": lambda s: s.remove_app(name="claude-desktop"),
    "set_grant": lambda s: s.set_grant(app="claude-desktop", collection="home", mode="none"),
    "set_app_limits": lambda s: s.set_app_limits(name="claude-desktop", limits={}),
    "connect": lambda s: s.connect(name="claude-desktop"),
    "settings": lambda s: s.settings(),
    "set_settings": lambda s: s.set_settings(defaults={"chars_per_day": 1}),
    "add_replaced_string": lambda s: s.add_replaced_string(text="Okafor"),
}


def test_the_locked_calls_cover_the_service() -> None:
    always = {"state", "create_store", "unlock", "lock"}
    assert set(LOCKED_CALLS) | always == {n for n in vars(Service) if not n.startswith("_")}


@pytest.mark.parametrize("name", sorted(LOCKED_CALLS))
def test_a_locked_vault_answers_409_locked(vault: GateService, name: str) -> None:
    before = gs.snapshot(vault.path)
    with refused(409, "locked"):
        LOCKED_CALLS[name](vault)
    assert gs.snapshot(vault.path) == before


def test_the_vault_locks_itself_at_the_announced_time(
    vault: GateService, gate_clock: FakeClock
) -> None:
    vault.unlock(passphrase=gs.PASSPHRASE, hours=1)
    ref = first_ref(vault, CLAUDE, "rent deposit")
    gate_clock.advance(minutes=59, seconds=59)
    assert vault.state()["locked"] is False
    gate_clock.advance(seconds=1)
    reply = vault.backend.read(CLAUDE, ref)
    assert (reply.code, reply.released) == ("LOCKED", False)
    assert vault.state()["locked"] is True
    with refused(409, "locked"):
        vault.documents()
    assert not (vault.path / "run" / "gate.sock").exists()

    state = vault.unlock(passphrase=gs.PASSPHRASE)
    assert state["record"]["previous_session"] == "clean"
    newest = vault.activity(limit=2)["items"]
    assert [item["summary"] for item in newest] == [
        "you unlocked the vault",
        "you let the timer lock the vault",
    ]


def test_a_timer_ends_the_session_when_nobody_asks(
    vault: GateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[Any] = []

    class Timer:
        daemon = False

        def __init__(self, interval: float, function: Any, args: Any = ()) -> None:
            self.interval, self.function, self.args = interval, function, args
            self.cancelled = self.started = False
            made.append(self)

        def start(self) -> None:
            self.started = True

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr(service_module.threading, "Timer", Timer)
    vault.unlock(passphrase=gs.PASSPHRASE, hours=0.5)
    (timer,) = made
    assert timer.interval == 1800.0
    assert timer.daemon is True
    assert timer.started
    timer.function(*timer.args)
    assert vault.state()["locked"] is True
    assert timer.cancelled

    vault.unlock(passphrase=gs.PASSPHRASE)
    vault.lock()
    assert made[1].cancelled
    made[0].function(*made[0].args)  # the timer of a session that is over does nothing
    assert vault.unlock(passphrase=gs.PASSPHRASE)["locked"] is False
    made[1].function(*made[1].args)
    assert vault.state()["locked"] is False


# -- owner mode --------------------------------------------------------------------------

WIDENING: dict[str, Callable[[GateService], Any]] = {
    "register a hosted app": lambda s: s.add_app(name="codex", hosted=True, destination="OpenAI"),
    "register a local app": lambda s: s.add_app(name="ollama", hosted=False),
    "grant read": lambda s: s.set_grant(app="lm-studio", collection="tax", mode="read"),
    "grant approve": lambda s: s.set_grant(app="lm-studio", collection="tax", mode="approve"),
    "approve to read": lambda s: s.set_grant(app="claude-desktop", collection="tax", mode="read"),
    "open to hosted apps": lambda s: s.set_collection(name="medical", allow_hosted=True),
    "show titles": lambda s: s.set_collection(name="home", show_titles=True),
    "new open collection": lambda s: s.set_collection(name="work", allow_hosted=True),
    "new collection with titles": lambda s: s.set_collection(name="work", show_titles=True),
    "raise an app limit": lambda s: s.set_app_limits(
        name="claude-desktop", limits={"chars_per_day": 20001}
    ),
    "raise a default limit": lambda s: s.set_settings(defaults={"searches_per_day": 101}),
    "raise a group limit": lambda s: s.set_settings(
        groups={"local": {"chars_per_30_days": 300001}}
    ),
    "raise the free part": lambda s: s.set_settings(per_document={"max_chars": 8001}),
    "raise the free share": lambda s: s.set_settings(per_document={"max_fraction": 0.51}),
    "longer approvals": lambda s: s.set_settings(approval={"ttl_minutes": 11}),
    "answer both kinds": lambda s: s.set_serving(kinds="both"),
}


@pytest.mark.parametrize("name", sorted(WIDENING))
def test_widening_needs_owner_mode(unlocked: GateService, gate_clock: FakeClock, name: str) -> None:
    act = WIDENING[name]
    before = picture(unlocked)
    gate_clock.advance(seconds=OWNER_MODE.total_seconds() - 1)
    assert unlocked.state()["owner_mode_until"] == "2026-11-12T09:10:00Z"
    gate_clock.advance(seconds=1)  # ten minutes after the passphrase: no longer
    with refused(403, "reauth_required"):
        act(unlocked)
    gate_clock.advance(hours=1)
    with refused(403, "reauth_required"):
        act(unlocked)
    assert picture(unlocked) == before, "a refused change changed something"

    with refused(403, "wrong_passphrase"):
        unlocked.reauth(passphrase="wrong passphrase")
    with refused(403, "reauth_required"):
        act(unlocked)
    answer = unlocked.reauth(passphrase=gs.PASSPHRASE)
    assert answer == {"owner_mode_until": "2026-11-12T10:20:00Z"}
    assert unlocked.state()["owner_mode_until"] == "2026-11-12T10:20:00Z"
    act(unlocked)
    assert picture(unlocked) != before


def test_widening_is_accepted_until_the_last_second_of_owner_mode(
    unlocked: GateService, gate_clock: FakeClock
) -> None:
    gate_clock.advance(seconds=OWNER_MODE.total_seconds() - 1)
    unlocked.add_app(name="codex", hosted=True, destination="OpenAI")
    assert "codex" in [app["name"] for app in unlocked.apps()["apps"]]


def test_a_refused_grant_stays_refused_for_the_agent(
    unlocked: GateService, gate_clock: FakeClock
) -> None:
    expire_owner_mode(gate_clock)
    with refused(403, "reauth_required"):
        unlocked.set_grant(app="claude-desktop", collection="tax", mode="read")
    ref = first_ref(unlocked, CLAUDE, "income tax", "tax")
    assert unlocked.backend.read(CLAUDE, ref).code == "NEEDS_APPROVAL"
    with refused(403, "reauth_required"):
        unlocked.set_serving(kinds="both")
    assert unlocked.backend.status(LM).code == "NOT_ALLOWED"


def test_narrowing_needs_no_owner_mode(unlocked: GateService, gate_clock: FakeClock) -> None:
    ref = first_ref(unlocked, CLAUDE, "income tax", "tax")
    assert unlocked.backend.read(CLAUDE, ref).code == "NEEDS_APPROVAL"
    expire_owner_mode(gate_clock)
    gate_clock.advance(hours=1)

    waiting = unlocked.approvals()["waiting"][0]
    unlocked.release(app="claude-desktop", ref=ref, body_sha256=waiting["body_sha256"])
    unlocked.dismiss(app="claude-desktop", ref=ref)

    unlocked.set_grant(app="claude-desktop", collection="home", mode="approve")  # read to approve
    unlocked.set_grant(app="claude-desktop", collection="tax", mode="none")
    unlocked.set_collection(name="tax", show_titles=False, note="papers for the tax office")
    unlocked.set_collection(name="home", allow_hosted=False)
    unlocked.set_collection(name="archive")
    unlocked.set_app_limits(name="lm-studio", limits={"chars_per_day": 500, "chars_per_call": 500})
    unlocked.set_settings(
        defaults={"chars_per_day": 19999},
        groups={"hosted": {"chars_per_day": 1}},
        per_document={"max_chars": 100, "max_fraction": 0.1},
        approval={"ttl_minutes": 1},
    )
    unlocked.add_replaced_string(text="Northgate")
    added = unlocked.add_document(collection="archive", name="old.txt", data=b"An old letter.")
    unlocked.remove_document(id=added["document"]["id"])
    unlocked.remove_collection(name="archive")
    unlocked.set_serving(kinds="local")
    unlocked.set_serving(kinds="hosted", paused=["claude-desktop"])
    unlocked.remove_app(name="lm-studio")
    assert unlocked.verify()["verified"] is True
    assert unlocked.lock()["locked"] is True


# -- which apps are answered ---------------------------------------------------------------


def test_serving_choice(unlocked: GateService, gate_clock: FakeClock) -> None:
    def answered() -> tuple[str, str]:
        return unlocked.backend.status(CLAUDE).code, unlocked.backend.status(LM).code

    # Both kinds are registered and nothing was chosen: only the hosted app is answered.
    assert answered() == ("OK", "NOT_ALLOWED")
    apps = {app["name"]: app for app in unlocked.apps()["apps"]}
    assert (apps["claude-desktop"]["served"], apps["lm-studio"]["served"]) == (True, False)

    with refused(400, "invalid"):
        unlocked.set_serving(kinds="everything")
    state = unlocked.set_serving(kinds="local")
    assert state["serving"] == {
        "hosted": [],
        "local": ["lm-studio"],
        "paused": [],
        "choice_needed": False,
        "kinds": "local",
    }
    assert answered() == ("NOT_ALLOWED", "OK")

    state = unlocked.set_serving(kinds="both")
    assert state["serving"]["kinds"] == "both"
    assert (state["serving"]["hosted"], state["serving"]["local"]) == (
        ["claude-desktop"],
        ["lm-studio"],
    )
    assert answered() == ("OK", "OK")
    (warning,) = [w for w in state["warnings"] if w["code"] == "both_kinds_served"]
    assert "any program running as you" in warning["text"]

    # The choice lasts: owner mode ending does not undo it, and it need not be made again.
    expire_owner_mode(gate_clock)
    assert unlocked.set_serving(kinds="both")["serving"]["kinds"] == "both"
    assert answered() == ("OK", "OK")

    state = unlocked.set_serving(paused=["lm-studio", "nobody"])
    assert state["serving"]["paused"] == ["lm-studio"]
    assert state["serving"]["local"] == []
    assert answered() == ("OK", "NOT_ALLOWED")
    apps = {app["name"]: app for app in unlocked.apps()["apps"]}
    assert (apps["lm-studio"]["paused"], apps["lm-studio"]["served"]) == (True, False)
    assert unlocked.set_serving(paused=[])["serving"]["local"] == ["lm-studio"]

    # A new session starts without the choice.
    unlocked.lock()
    state = unlocked.unlock(passphrase=gs.PASSPHRASE)
    assert state["serving"]["choice_needed"] is True
    assert answered() == ("OK", "NOT_ALLOWED")


def test_a_newly_registered_app_follows_the_choice(unlocked: GateService) -> None:
    unlocked.add_app(name="ollama", hosted=False)
    ollama = Caller("ollama")
    assert unlocked.backend.status(ollama).code == "NOT_ALLOWED"  # local, and nothing was chosen
    unlocked.add_app(name="codex", hosted=True, destination="OpenAI")
    assert unlocked.backend.status(Caller("codex")).code == "OK"
    # With the hosted apps gone there is one kind left, and nothing to choose between.
    unlocked.remove_app(name="codex")
    unlocked.remove_app(name="claude-desktop")
    state = unlocked.state()
    assert state["serving"]["choice_needed"] is False
    assert state["serving"]["local"] == ["lm-studio", "ollama"]
    assert unlocked.backend.status(ollama).code == "OK"


# -- documents ---------------------------------------------------------------------------


def test_upload_of_a_text_file(service: GateService) -> None:
    service.create_store(passphrase=gs.PASSPHRASE)
    answer = service.add_document(
        collection="papers", name="C:\\Users\\me\\Lease 2025.TXT", data=gs.LEASE_TEXT.encode()
    )
    document = answer["document"]
    assert document["title"] == "Lease 2025"
    assert document["original_kept"] is True
    assert document["released"] == {"hosted": 0, "local": 0}
    assert document["passages"] >= 3
    assert ISO.fullmatch(document["added"])

    (collection,) = service.documents()["collections"]
    assert collection["name"] == "papers"
    assert (collection["allow_hosted"], collection["show_titles"]) == (False, False)
    assert collection["documents"] == [document]
    assert collection["characters"] == document["characters"]

    session = service._session
    assert session is not None
    info = session.store.document(document["id"])
    assert info.title_source == "file-name"
    assert session.store.read_original(document["id"]) == gs.LEASE_TEXT.encode()
    assert session.store.read_document(document["id"])["source"]["name"] == "Lease 2025.TXT"

    newest = service.activity(limit=2)["items"]
    assert (
        newest[0]["summary"] == f"you added Lease 2025 to papers ({document['passages']} passages)"
    )
    assert newest[1]["summary"] == "you created the collection papers"
    assert (newest[0]["kind"], newest[0]["app"], newest[0]["collections"]) == (
        "change",
        None,
        ["papers"],
    )


def test_upload_refusals(service: GateService) -> None:
    service.create_store(passphrase=gs.PASSPHRASE)
    service.add_document(collection="home", name="bank.txt", data=gs.BANK_TEXT.encode())
    before = service.state()["record"]["head_seq"]

    with refused(409, "duplicate"):
        service.add_document(collection="home", name="bank.txt", data=gs.BANK_TEXT.encode())
    with refused(409, "duplicate"):  # the same text under another name, in another collection
        service.add_document(
            collection="other", name="copy.md", data=("\n" + gs.BANK_TEXT + "\n\n").encode()
        )
    for name, data in (
        ("photo.jpg", b"\xff\xd8\xff"),
        ("empty.txt", b""),
        ("blank.txt", b" \n\t\n"),
        ("latin.txt", "Grüße".encode("latin-1")),
        ("broken.pdf", b"not a pdf at all") if extract.pdf_available() else ("x.doc", b"x"),
    ):
        with refused(400, "invalid") as caught:
            service.add_document(collection="home", name=name, data=data)
        assert caught[0].message
    for collection in ("Home", "", "a b", "x" * 33):
        with refused(400, "invalid"):
            service.add_document(collection=collection, name="a.txt", data=b"Some text.")
    assert service.state()["record"]["head_seq"] == before
    assert [c["name"] for c in service.documents()["collections"]] == ["home"]


def test_a_file_name_that_cannot_be_a_title_gets_a_plain_one(service: GateService) -> None:
    service.create_store(passphrase=gs.PASSPHRASE)
    answer = service.add_document(collection="home", name="\u200b.txt", data=b"Some text here.")
    assert answer["document"]["title"] == "untitled document"


@pytest.mark.skipif(importlib.util.find_spec("pypdf") is None, reason="needs the pdf extra")
def test_upload_of_a_real_pdf(service: GateService) -> None:
    from .test_gate_extract import make_pdf

    if not extract.pdf_available():
        pytest.skip("the child process does not find the PDF parser")
    service.create_store(passphrase=gs.PASSPHRASE)
    upload = service.documents()["upload"]
    assert upload == {
        "max_bytes": 20 * 1024 * 1024,
        "types": [".txt", ".md", ".markdown", ".rst", ".text", ".pdf"],
        "pdf": True,
    }
    data = make_pdf("The pelican invoice of March is due on the ninth.")
    document = service.add_document(collection="home", name="invoice-march.pdf", data=data)[
        "document"
    ]
    assert document["title"] == "invoice-march"
    service.add_app(name="lm-studio", hosted=False)
    service.set_grant(app="lm-studio", collection="home", mode="read")
    service.set_settings(per_document={"max_fraction": 1})  # one line: let all of it be free
    reply = service.backend.search(LM, "pelican invoice")
    assert reply.code == "OK"
    assert document["id"] in reply.text
    assert "pelican invoice of March" in reply.text
    with refused(409, "duplicate"):
        service.add_document(collection="home", name="again.pdf", data=data)


def test_collections(unlocked: GateService) -> None:
    answer = unlocked.set_collection(name="work", note="  see https://example.com/x  ")
    assert answer["collection"] == {
        "name": "work",
        "note": answer["collection"]["note"],
        "allow_hosted": False,
        "show_titles": False,
        "characters": 0,
        "documents": [],
    }
    assert "example.com" not in answer["collection"]["note"]
    assert (
        unlocked.set_collection(name="work", note="contracts")["collection"]["note"] == "contracts"
    )
    # Absent fields stay.
    assert (
        unlocked.set_collection(name="work", allow_hosted=True)["collection"]["note"] == "contracts"
    )
    for name in ("Work", "a b", ""):
        with refused(400, "invalid"):
            unlocked.set_collection(name=name)

    with refused(409, "conflict"):
        unlocked.remove_collection(name="home")
    with refused(404, "not_found"):
        unlocked.remove_collection(name="nothing")
    unlocked.set_grant(app="claude-desktop", collection="work", mode="read")
    assert unlocked.remove_collection(name="work") == {}
    apps = {app["name"]: app for app in unlocked.apps()["apps"]}
    assert "work" not in apps["claude-desktop"]["grants"]
    assert "work" not in [c["name"] for c in unlocked.documents()["collections"]]


def test_closing_a_collection_to_hosted_apps_takes_their_grants_away(
    unlocked: GateService,
) -> None:
    ref = first_ref(unlocked, CLAUDE, "rent deposit", "home")
    unlocked.set_collection(name="home", allow_hosted=False)
    assert unlocked.backend.read(CLAUDE, ref).code == "NOT_FOUND"
    grants = {app["name"]: app["grants"] for app in unlocked.apps()["apps"]}
    assert grants == {"claude-desktop": {"tax": "approve"}, "lm-studio": grants["lm-studio"]}
    assert grants["lm-studio"]["home"] == "read"  # a local app keeps it
    # Opening it again is one decision; granting it again is another.
    unlocked.set_collection(name="home", allow_hosted=True)
    assert unlocked.backend.read(CLAUDE, ref).code == "NOT_FOUND"
    unlocked.set_grant(app="claude-desktop", collection="home", mode="read")
    assert unlocked.backend.read(CLAUDE, ref).code == "OK"


def test_removing_a_document(unlocked: GateService) -> None:
    ref = first_ref(unlocked, CLAUDE, "rent deposit", "home")
    doc_id = ref.partition(".")[0]
    assert unlocked.remove_document(id=doc_id) == {}
    assert unlocked.backend.read(CLAUDE, ref).code == "NOT_FOUND"
    titles = [d["title"] for c in unlocked.documents()["collections"] for d in c["documents"]]
    assert len(titles) == 3
    with refused(404, "not_found"):
        unlocked.remove_document(id=doc_id)
    with refused(404, "not_found"):
        unlocked.remove_document(id="d-0123456789ab")
    newest = unlocked.activity(kind="change", limit=1)["items"][0]
    assert newest["summary"].startswith("you removed ")
    assert newest["summary"].endswith(" from home")


# -- activity ----------------------------------------------------------------------------


def test_activity_tells_each_call_and_the_detail_holds_the_exact_text(
    unlocked: GateService,
) -> None:
    status = unlocked.backend.status(CLAUDE)
    search = unlocked.backend.search(CLAUDE, "rent deposit")
    ref = REF.findall(search.text)[0]
    read = unlocked.backend.read(CLAUDE, ref)
    nothing = unlocked.backend.search(CLAUDE, "xylophone", "home")
    missing = unlocked.backend.read(CLAUDE, "d-0123456789ab.1")
    stranger = unlocked.backend.search(STRANGER, "passwords")
    assert [r.code for r in (status, search, read, nothing, missing, stranger)] == [
        "OK",
        "OK",
        "OK",
        "OK",
        "NOT_FOUND",
        "NOT_ALLOWED",
    ]
    doc_id, _, number = ref.partition(".")
    session = unlocked._session
    assert session is not None
    info = session.store.document(doc_id)

    items = unlocked.activity(limit=6)["items"]
    assert [item["seq"] for item in items] == list(range(items[0]["seq"], items[0]["seq"] - 6, -1))
    told = [(i["kind"], i["app"], i["tool"], i["summary"], i["code"]) for i in reversed(items)]
    results = len(REF.findall(search.text))
    assert told == [
        ("question", "claude-desktop", "status", "asked what it may use (no document text)", "OK"),
        ("release", "claude-desktop", "search", f"searched home and tax: {results} results", "OK"),
        (
            "release",
            "claude-desktop",
            "read",
            f"read passage {number} of {info.passages} of {info.title} in home",
            "OK",
        ),
        ("release", "claude-desktop", "search", "searched home: no results", "OK"),
        (
            "refusal",
            "claude-desktop",
            "read",
            "was refused: no such passage for this app",
            "NOT_FOUND",
        ),
        (
            "refusal",
            NOT_REGISTERED,
            "search",
            "was refused: no app is answered under that name",
            "NOT_ALLOWED",
        ),
    ]
    by_tool = {(i["tool"], i["code"], i["app"]): i for i in items}
    read_item = by_tool[("read", "OK", "claude-desktop")]
    assert read_item["ref"] == ref
    assert read_item["characters"] == len(read.text)
    assert read_item["collections"] == ["home"]
    assert (read_item["destination"], read_item["hosted"]) == ("Anthropic", True)
    assert (read_item["approved"], read_item["count"], read_item["variant"]) == (False, 1, None)
    stranger_item = by_tool[("search", "NOT_ALLOWED", NOT_REGISTERED)]
    assert (stranger_item["destination"], stranger_item["hosted"]) == (None, None)
    assert stranger_item["characters"] == 0

    # No list entry carries released text or what was asked; the detail of a release does.
    listed = json.dumps(unlocked.activity(limit=200))
    assert gs.LEASE_MARKER not in listed
    assert "xylophone" not in listed
    assert "passwords" not in listed
    for item in items:
        assert "text" not in item
        assert "query" not in item

    detail = unlocked.activity_detail(seq=read_item["seq"])
    assert detail["text"] == read.text
    assert detail["query"] is None
    assert (detail["claimed"], detail["protocol"], detail["transport"]) == (
        "claude-ai 1.2",
        "2025-11-25",
        "stdio",
    )
    assert detail["client_seq"] == 2
    assert detail["rule"] == "grant:read"
    assert ["document", info.title] in detail["details"]
    assert ["passage", f"{number} of {info.passages}"] in detail["details"]
    assert {key: detail[key] for key in read_item} == read_item

    search_item = by_tool[("search", "OK", "claude-desktop")]
    found = [i for i in items if i["tool"] == "search" and i["code"] == "OK"]
    details = {i["seq"]: unlocked.activity_detail(seq=i["seq"]) for i in found}
    assert sorted(d["text"] for d in details.values()) == sorted([search.text, nothing.text])
    assert {d["query"] for d in details.values()} == {"rent deposit", "xylophone"}
    assert search_item["characters"] in (len(search.text), len(nothing.text))

    refusal = unlocked.activity_detail(seq=by_tool[("read", "NOT_FOUND", "claude-desktop")]["seq"])
    assert refusal["text"] is None
    assert ["calls", "1"] in refusal["details"]
    unknown = unlocked.activity_detail(seq=stranger_item["seq"])
    assert (unknown["text"], unknown["claimed"], unknown["query"]) == (None, None, "passwords")

    # Every entry names its own hash and the hash of the entry before it.
    newest = items[0]["seq"]
    chain = [unlocked.activity_detail(seq=seq) for seq in range(newest - 3, newest + 1)]
    for earlier, later in itertools.pairwise(chain):
        assert re.fullmatch("[0-9a-f]{16}", later["record_hash"])
        assert later["previous_hash"] == earlier["record_hash"]

    with refused(404, "not_found"):
        unlocked.activity_detail(seq=newest + 1)
    with refused(404, "not_found"):
        unlocked.activity_detail(seq=0)


def test_activity_pages_and_filters(unlocked: GateService) -> None:
    for word in ("rent", "deposit", "notice", "xylophone"):
        unlocked.backend.search(CLAUDE, word)
    unlocked.backend.read(CLAUDE, "d-0123456789ab.1")
    unlocked.backend.status(STRANGER)
    everything = unlocked.activity(limit=200)
    seqs = [item["seq"] for item in everything["items"]]
    assert seqs == list(range(max(seqs), 0, -1))
    assert everything["next_before"] is None

    page = unlocked.activity(limit=3)
    assert [item["seq"] for item in page["items"]] == seqs[:3]
    assert page["next_before"] == seqs[2]
    following = unlocked.activity(limit=3, before=page["next_before"])
    assert [item["seq"] for item in following["items"]] == seqs[3:6]
    last = unlocked.activity(limit=200, before=3)
    assert ([item["seq"] for item in last["items"]], last["next_before"]) == ([2, 1], None)
    assert unlocked.activity(before=1) == {"items": [], "next_before": None}
    assert len(unlocked.activity()["items"]) == len(seqs)  # fewer than a page of 50

    kinds = {kind: unlocked.activity(kind=kind)["items"] for kind in service_module.ACTIVITY_KINDS}
    assert sum(len(items) for items in kinds.values()) == len(seqs)
    assert [item["kind"] for item in kinds["release"]] == ["release"] * 4
    assert [item["code"] for item in kinds["refusal"]] == ["NOT_ALLOWED", "NOT_FOUND"]
    assert kinds["approval"] == []
    assert {item["summary"] for item in kinds["session"]} == {
        "you created the vault",
        "you unlocked the vault",
    }
    assert all(item["app"] is None for item in kinds["change"] + kinds["session"])

    mine = unlocked.activity(app="claude-desktop")["items"]
    assert len(mine) == 5
    assert [i["code"] for i in unlocked.activity(app=NOT_REGISTERED)["items"]] == ["NOT_ALLOWED"]
    assert unlocked.activity(app="lm-studio")["items"] == []
    two = unlocked.activity(app="claude-desktop", kind="release", limit=2)
    assert len(two["items"]) == 2
    assert two["next_before"] == two["items"][-1]["seq"]

    for bad in ({"limit": 0}, {"limit": 201}, {"kind": "everything"}):
        with refused(400, "invalid"):
            unlocked.activity(**bad)


def test_the_first_entries_tell_how_the_vault_was_set_up(unlocked: GateService) -> None:
    told = [item["summary"] for item in reversed(unlocked.activity(limit=200)["items"])]
    assert told[0] == "you created the vault"
    assert told[1] == "you started with the default limits: no app is answered yet"
    assert told[2].startswith("you created the collection home and made ")
    assert told[3:7] == [
        "you added lease-2025 to home (4 passages)",
        "you added bank-letter to home (1 passage)",
        "you added tax-note-2025 to tax (1 passage)",
        "you added referral-2026 to medical (1 passage)",
    ]
    assert told[7] == "you unlocked the vault"


def test_changes_are_told_as_sentences(unlocked: GateService) -> None:
    def newest() -> dict[str, Any]:
        return dict(unlocked.activity(limit=1)["items"][0])

    unlocked.add_app(name="codex", hosted=True, destination="OpenAI")
    assert newest()["summary"] == "you added the app codex"
    unlocked.set_grant(app="codex", collection="home", mode="read")
    assert newest()["summary"] == "you granted codex: home (can read)"
    assert newest()["collections"] == ["home"]
    unlocked.set_grant(app="codex", collection="home", mode="approve")
    assert newest()["summary"] == "you granted codex: home (ask me each time)"
    head = newest()["seq"]
    unlocked.set_grant(app="codex", collection="home", mode="approve")  # nothing changes
    assert newest()["seq"] == head
    unlocked.set_grant(app="codex", collection="home", mode="none")
    assert newest()["summary"] == "you took home away from codex"
    unlocked.set_collection(name="medical", allow_hosted=True)
    assert newest()["summary"] == "you opened medical to hosted apps"
    unlocked.set_collection(name="medical", allow_hosted=False)
    assert newest()["summary"] == "you closed medical to hosted apps"
    unlocked.set_collection(name="home", show_titles=True)
    assert newest()["summary"] == "you let apps see the titles in home"
    unlocked.set_app_limits(name="codex", limits={"chars_per_day": 5000})
    assert newest()["summary"] == "you changed the limits of codex"
    unlocked.set_settings(defaults={"chars_per_day": 15000})
    assert newest()["summary"] == "you changed the limits of the vault"
    unlocked.add_replaced_string(text="Northgate")
    assert newest()["summary"] == "you added a word that is replaced"
    unlocked.remove_app(name="codex")
    assert newest()["summary"] == "you removed the app codex"
    assert all(i["kind"] == "change" for i in unlocked.activity(limit=11)["items"])


def test_refused_calls_are_counted_on_their_entry(
    unlocked: GateService, gate_clock: FakeClock
) -> None:
    for _ in range(3):
        gate_clock.advance(seconds=20)
        assert unlocked.backend.read(CLAUDE, "d-0123456789ab.1").code == "NOT_FOUND"
    assert unlocked.backend.status(STRANGER).code == "NOT_ALLOWED"
    items = unlocked.activity(kind="refusal")["items"]
    assert [(i["code"], i["count"]) for i in items] == [("NOT_ALLOWED", 1), ("NOT_FOUND", 3)]
    assert items[1]["time"] == "2026-11-12T09:00:20Z"
    assert items[1]["last_time"] == "2026-11-12T09:01:00Z"
    assert "last_time" not in items[0]
    assert ["calls", "3"] in unlocked.activity_detail(seq=items[1]["seq"])["details"]
    assert unlocked.state()["today"]["refusals"] == 4

    # An hour later the gate writes a new entry, which says what the one before stood for.
    gate_clock.advance(hours=1)
    unlocked.backend.read(CLAUDE, "d-0123456789ab.1")
    counts = [(i["code"], i["count"]) for i in unlocked.activity(kind="refusal")["items"]]
    assert counts == [("NOT_FOUND", 1), ("NOT_ALLOWED", 1), ("NOT_FOUND", 3)]
    # And so does the list that is built from the record at the next unlock.
    unlocked.lock()
    unlocked.unlock(passphrase=gs.PASSPHRASE)
    counts = [(i["code"], i["count"]) for i in unlocked.activity(kind="refusal")["items"]]
    assert counts == [("NOT_FOUND", 1), ("NOT_ALLOWED", 1), ("NOT_FOUND", 3)]
    # A refusal of the new session that the gate adds to an entry of the old one counts there.
    unlocked.backend.read(CLAUDE, "d-0123456789ab.1")
    counts = [(i["code"], i["count"]) for i in unlocked.activity(kind="refusal")["items"]]
    assert counts == [("NOT_FOUND", 2), ("NOT_ALLOWED", 1), ("NOT_FOUND", 3)]


def test_today_counts_the_last_24_hours(unlocked: GateService, gate_clock: FakeClock) -> None:
    search = unlocked.backend.search(CLAUDE, "rent deposit")
    read = unlocked.backend.read(CLAUDE, REF.findall(search.text)[0])
    unlocked.backend.status(CLAUDE)
    today = unlocked.state()["today"]
    assert today == {
        "releases": 2,
        "characters": len(search.text) + len(read.text),
        "questions": 1,
        "refusals": 0,
        "approvals_waiting": 0,
    }
    gate_clock.advance(hours=7, minutes=59)
    assert unlocked.state()["today"]["releases"] == 2
    unlocked.lock()
    gate_clock.advance(hours=17)
    unlocked.unlock(passphrase=gs.PASSPHRASE)
    assert unlocked.state()["today"] == {
        "releases": 0,
        "characters": 0,
        "questions": 0,
        "refusals": 0,
        "approvals_waiting": 0,
    }


def test_a_question_is_not_a_release(unlocked: GateService) -> None:
    status = unlocked.backend.status(CLAUDE)
    assert (status.code, status.released) == ("OK", False)
    search = unlocked.backend.search(CLAUDE, "rent deposit", "home")
    assert service_module.ACTIVITY_KINDS == (
        "release",
        "question",
        "refusal",
        "approval",
        "change",
        "session",
    )
    (question,) = unlocked.activity(kind="question")["items"]
    assert (question["kind"], question["tool"], question["code"]) == ("question", "status", "OK")
    assert (question["app"], question["destination"], question["hosted"]) == (
        "claude-desktop",
        "Anthropic",
        True,
    )
    assert question["summary"] == "asked what it may use (no document text)"
    assert question["characters"] == 0
    # It is not among the releases: not in the list, not in the figures, not per app.
    released = unlocked.activity(kind="release")["items"]
    assert [item["tool"] for item in released] == ["search"]
    today = unlocked.state()["today"]
    assert (today["releases"], today["questions"]) == (1, 1)
    assert today["characters"] == len(search.text)
    (claude,) = (app for app in unlocked.apps()["apps"] if app["name"] == "claude-desktop")
    assert claude["usage"]["releases_day"] == 1
    assert claude["usage"]["chars_day"] == len(search.text)
    # Its detail holds no text: the answer to a status call is not kept, and names no document.
    detail = unlocked.activity_detail(seq=question["seq"])
    assert detail["text"] is None
    assert detail["claimed"] == "claude-ai 1.2"
    assert ["answered with", "its collections and limits, no document text"] in detail["details"]
    # The list that is rebuilt from the record at the next unlock says the same.
    unlocked.lock()
    unlocked.unlock(passphrase=gs.PASSPHRASE)
    assert [item["seq"] for item in unlocked.activity(kind="question")["items"]] == [
        question["seq"]
    ]
    assert unlocked.state()["today"]["questions"] == 1


def test_the_cursor_grows_with_everything_the_interface_shows(
    unlocked: GateService, gate_clock: FakeClock
) -> None:
    seen = [unlocked.state()["cursor"]]

    def grew() -> bool:
        seen.append(unlocked.state()["cursor"])
        return seen[-1] > seen[-2]

    assert not grew()  # nothing happened: nothing to reload
    unlocked.backend.search(CLAUDE, "rent")
    assert grew()
    ref = first_ref(unlocked, CLAUDE, "income tax", "tax")
    assert grew()
    unlocked.backend.read(CLAUDE, ref)  # waits for the owner now
    assert grew()
    unlocked.backend.read(CLAUDE, ref)  # asked again: no new record, but a new count
    assert grew()
    unlocked.dismiss(app="claude-desktop", ref=ref)
    assert grew()
    unlocked.set_collection(name="home", note="papers")
    assert grew()
    expire_owner_mode(gate_clock)
    assert grew()
    unlocked.reauth(passphrase=gs.PASSPHRASE)
    assert grew()
    unlocked.lock()
    assert grew()


# -- approvals ---------------------------------------------------------------------------


def test_approval_flow_from_a_refused_read_to_a_released_passage(
    unlocked: GateService, gate_clock: FakeClock
) -> None:
    ref = first_ref(unlocked, CLAUDE, "income tax", "tax")
    assert unlocked.approvals() == {"waiting": [], "granted": []}
    first = unlocked.backend.read(CLAUDE, ref)
    assert first.code == "NEEDS_APPROVAL"
    assert first.text == messages.needs_approval(ref, "claude-desktop")
    gate_clock.advance(seconds=30)
    unlocked.backend.read(CLAUDE, ref)

    assert unlocked.state()["today"]["approvals_waiting"] == 1
    (waiting,) = unlocked.approvals()["waiting"]
    session = unlocked._session
    assert session is not None
    info = session.store.document(ref.partition(".")[0])
    body = waiting.pop("body")
    assert gs.TAX_MARKER in body
    assert waiting == {
        "app": "claude-desktop",
        "destination": "Anthropic",
        "hosted": True,
        "claimed": "claude-ai 1.2",
        "ref": ref,
        "collection": "tax",
        "title": "tax-note-2025",
        "passage": 1,
        "passages": 1,
        "first_asked": "2026-11-12T09:00:00Z",
        "last_asked": "2026-11-12T09:00:30Z",
        "times_asked": 2,
        "mode": "approve",
        "inside_free_allowance": False,
        "fits_limits": True,
        "body_sha256": waiting["body_sha256"],
        "characters": len(body),
        "result_characters": waiting["result_characters"],
        "document": {
            "characters": info.chars,
            "released": 0,
            "after": info.chars,
            # Half of the document, rounded up: what hosted apps get of it without asking.
            "free": (info.chars + 1) // 2,
            "to": "hosted apps",
        },
        "today": {"used": waiting["today"]["used"], "limit": 20000},
    }
    refusal = unlocked.activity(kind="refusal", limit=1)["items"][0]
    assert refusal["summary"] == "needs your approval for passage 1 of 1 of tax-note-2025 in tax"
    assert (refusal["ref"], refusal["count"], refusal["collections"]) == (ref, 2, ["tax"])

    # The hash of other text approves nothing.
    with refused(409, "changed"):
        unlocked.release(app="claude-desktop", ref=ref, body_sha256="0" * 64)
    assert unlocked.backend.read(CLAUDE, ref).code == "NEEDS_APPROVAL"
    with refused(404, "not_found"):
        unlocked.release(app="lm-studio", ref=ref, body_sha256=waiting["body_sha256"])
    with refused(404, "not_found"):
        unlocked.release(
            app="claude-desktop", ref="d-0123456789ab.1", body_sha256=waiting["body_sha256"]
        )

    gate_clock.advance(seconds=30)
    answer = unlocked.release(app="claude-desktop", ref=ref, body_sha256=waiting["body_sha256"])
    assert answer == {"valid_until": "2026-11-12T09:11:00Z"}
    approvals = unlocked.approvals()
    assert approvals["waiting"] == []
    assert approvals["granted"] == [
        {
            "app": "claude-desktop",
            "ref": ref,
            "valid_until": "2026-11-12T09:11:00Z",
            "title": "tax-note-2025",
            "collection": "tax",
            "passage": 1,
            "passages": 1,
            "withdrawable": True,
        }
    ]
    approval = unlocked.activity(limit=1)["items"][0]
    assert (approval["kind"], approval["app"], approval["ref"]) == ("approval", None, ref)
    assert approval["summary"] == (
        "you approved passage 1 of 1 of tax-note-2025 in tax once for claude-desktop"
    )

    # Another app gets nothing from it; the app it was given to gets the passage once.
    unlocked.set_serving(kinds="both")
    assert unlocked.backend.read(LM, ref).code == "NOT_FOUND"
    released = unlocked.backend.read(CLAUDE, ref)
    assert released.code == "OK"
    for line in body.split("\n"):
        assert f"| {line}" in released.text
    assert len(released.text) == waiting["result_characters"]
    item = unlocked.activity(limit=1)["items"][0]
    assert (item["kind"], item["approved"], item["ref"]) == ("release", True, ref)
    detail = unlocked.activity_detail(seq=item["seq"])
    assert (detail["text"], detail["rule"]) == (released.text, "approval")
    assert unlocked.approvals() == {"waiting": [], "granted": []}
    assert unlocked.backend.read(CLAUDE, ref).code == "NEEDS_APPROVAL"


def test_text_that_changed_since_it_was_shown_is_not_released(unlocked: GateService) -> None:
    ref = first_ref(unlocked, CLAUDE, "income tax", "tax")
    unlocked.backend.read(CLAUDE, ref)
    (shown,) = unlocked.approvals()["waiting"]
    assert "food bank" in shown["body"]
    # The owner lists a word of the passage for replacement after having looked at it.
    unlocked.add_replaced_string(text="food bank")
    with refused(409, "changed"):
        unlocked.release(app="claude-desktop", ref=ref, body_sha256=shown["body_sha256"])
    assert unlocked.approvals()["granted"] == []
    assert unlocked.backend.read(CLAUDE, ref).code == "NEEDS_APPROVAL"

    (now,) = unlocked.approvals()["waiting"]
    assert "food bank" not in now["body"]
    assert now["body_sha256"] != shown["body_sha256"]
    unlocked.release(app="claude-desktop", ref=ref, body_sha256=now["body_sha256"])
    released = unlocked.backend.read(CLAUDE, ref)
    assert released.code == "OK"
    assert "food bank" not in released.text


def test_dismiss_takes_a_request_off_the_list_and_withdraws_an_unused_approval(
    unlocked: GateService,
) -> None:
    ref = first_ref(unlocked, CLAUDE, "income tax", "tax")
    unlocked.backend.read(CLAUDE, ref)
    assert unlocked.dismiss(app="claude-desktop", ref=ref) == {}
    assert unlocked.approvals() == {"waiting": [], "granted": []}
    assert unlocked.dismiss(app="claude-desktop", ref=ref) == {}
    assert unlocked.dismiss(app="nobody", ref="not a ref") == {}

    unlocked.backend.read(CLAUDE, ref)
    (waiting,) = unlocked.approvals()["waiting"]
    assert waiting["times_asked"] == 1
    unlocked.release(app="claude-desktop", ref=ref, body_sha256=waiting["body_sha256"])
    assert len(unlocked.approvals()["granted"]) == 1
    unlocked.dismiss(app="claude-desktop", ref=ref)
    assert unlocked.approvals() == {"waiting": [], "granted": []}
    assert unlocked.backend.read(CLAUDE, ref).code == "NEEDS_APPROVAL"


def test_approvals_do_not_outlive_the_session(unlocked: GateService) -> None:
    ref = first_ref(unlocked, CLAUDE, "income tax", "tax")
    unlocked.backend.read(CLAUDE, ref)
    (waiting,) = unlocked.approvals()["waiting"]
    unlocked.release(app="claude-desktop", ref=ref, body_sha256=waiting["body_sha256"])
    unlocked.lock()
    unlocked.unlock(passphrase=gs.PASSPHRASE)
    assert unlocked.approvals() == {"waiting": [], "granted": []}
    assert unlocked.backend.read(CLAUDE, ref).code == "NEEDS_APPROVAL"


# -- what never comes back -----------------------------------------------------------------


def every_answer(service: GateService) -> str:
    """What every reading call returns, and every entry of the record in full, as one text."""
    answers: list[Any] = [
        service.state(),
        service.approvals(),
        service.documents(),
        service.apps(),
        service.settings(),
        service.verify(),
    ]
    activity = service.activity(limit=200)
    answers.append(activity)
    answers.extend(service.activity_detail(seq=item["seq"]) for item in activity["items"])
    for app in service.apps()["apps"]:
        answers.append(service.connect(name=app["name"]))
    return json.dumps(answers, ensure_ascii=False)


def test_listed_strings_are_never_returned(unlocked: GateService) -> None:
    secret_place = "Harbour Street"
    count = unlocked.add_replaced_string(text=secret_place)
    assert count == {"replaced_strings": 3}
    unlocked.add_document(
        collection="tax",
        name="letter-to-the-office.txt",
        data=f"Objection by {gs.LISTED_NAME}, personal number {gs.LISTED_NUMBER}, of "
        f"{secret_place} 14: the zebrafinch allowance was left out.".encode(),
    )
    unlocked.set_serving(kinds="both")
    for caller in (CLAUDE, LM):
        for query in ("landlord tenant agreement", "current account fee", "rent"):
            reply = unlocked.backend.search(caller, query, "home")
            for ref in REF.findall(reply.text):
                unlocked.backend.read(caller, ref)
    waiting_ref = first_ref(unlocked, CLAUDE, "zebrafinch", "tax")
    assert unlocked.backend.read(CLAUDE, waiting_ref).code == "NEEDS_APPROVAL"
    (waiting,) = [w for w in unlocked.approvals()["waiting"] if w["ref"] == waiting_ref]
    assert "zebrafinch" in waiting["body"]
    unlocked.release(app="claude-desktop", ref=waiting_ref, body_sha256=waiting["body_sha256"])
    assert unlocked.backend.read(CLAUDE, waiting_ref).code == "OK"
    unlocked.backend.read(CLAUDE, waiting_ref)  # waits again, so that a preview is shown too

    for text in (gs.LISTED_NAME, "jan kowalski", f"  {secret_place.upper()} "):
        with refused(400, "invalid") as caught:
            unlocked.add_replaced_string(text=text)
        assert "owalski" not in caught[0].message.lower()
        assert "arbour" not in caught[0].message.lower()

    answers = every_answer(unlocked)
    # The passages that hold the strings were released and are shown ...
    assert "landlord, Northgate Housing" in answers
    assert "zebrafinch allowance" in answers
    assert waiting_ref in [w["ref"] for w in unlocked.approvals()["waiting"]]
    # ... and the strings are in none of it.
    folded = answers.casefold()
    for listed in ("kowalski", gs.LISTED_NUMBER, "harbour street"):
        assert listed not in folded
    assert unlocked.settings()["replaced_strings"] == 3


def test_the_passphrase_is_in_no_answer_and_no_error(vault: GateService) -> None:
    said = [json.dumps(vault.unlock(passphrase=gs.PASSPHRASE))]
    said.append(json.dumps(vault.reauth(passphrase=gs.PASSPHRASE)))
    said.append(every_answer(vault))
    for wrong in (gs.PASSPHRASE[:-1], gs.PASSPHRASE.upper()):
        with refused(403, "wrong_passphrase") as caught:
            vault.reauth(passphrase=wrong)
        said.append(caught[0].message)
    said.append(json.dumps(vault.lock()))
    with refused(403, "wrong_passphrase") as caught:
        vault.unlock(passphrase=gs.PASSPHRASE[:-1])
    said.append(caught[0].message)
    for text in said:
        assert gs.PASSPHRASE[:-1] not in text
        assert gs.PASSPHRASE.upper()[:-1] not in text


def test_document_text_comes_back_only_for_releases_and_for_the_passage_to_approve(
    unlocked: GateService,
) -> None:
    markers = (gs.LEASE_MARKER, gs.BANK_MARKER, gs.TAX_MARKER, gs.MEDICAL_MARKER)
    assert not [m for m in markers if m in every_answer(unlocked)]

    # A search that the limits refuse and a read that waits release nothing.
    ref = first_ref(unlocked, CLAUDE, "income tax", "tax")
    unlocked.backend.read(CLAUDE, ref)
    answers = every_answer(unlocked)
    assert [m for m in markers if m in answers] == [gs.TAX_MARKER]  # the passage to approve
    assert answers.count(gs.TAX_MARKER) == 1

    lease = first_ref(unlocked, CLAUDE, "residential lease agreement landlord", "home")
    read = unlocked.backend.read(CLAUDE, lease)
    assert gs.LEASE_MARKER in read.text
    answers = every_answer(unlocked)
    assert sorted(m for m in markers if m in answers) == sorted([gs.LEASE_MARKER, gs.TAX_MARKER])
    listing = json.dumps([unlocked.activity(limit=200), unlocked.documents(), unlocked.apps()])
    assert not [m for m in markers if m in listing]


# -- apps, settings, connecting --------------------------------------------------------------


def test_apps_with_their_use(unlocked: GateService, gate_clock: FakeClock) -> None:
    search = unlocked.backend.search(CLAUDE, "rent deposit")
    gate_clock.advance(seconds=5)
    read = unlocked.backend.read(CLAUDE, REF.findall(search.text)[0])
    answer = unlocked.apps()
    claude, lm = answer["apps"]
    used = len(search.text) + len(read.text)
    assert claude == {
        "name": "claude-desktop",
        "destination": "Anthropic",
        "hosted": True,
        "paused": False,
        "served": True,
        "grants": {"home": "read", "tax": "approve"},
        "limits": {
            "chars_per_call": 2000,
            "chars_per_day": 20000,
            "chars_per_30_days": 150000,
            "searches_per_day": 100,
            "calls_per_minute": 30,
        },
        "usage": {"chars_day": used, "chars_30_days": used, "searches_day": 1, "releases_day": 2},
        "last_seen": {"time": "2026-11-12T09:00:05Z", "claimed": "claude-ai 1.2"},
    }
    assert (lm["name"], lm["hosted"], lm["served"], lm["last_seen"]) == (
        "lm-studio",
        False,
        False,
        None,
    )
    assert lm["destination"] == "local model on this computer"
    assert answer["groups"] == {
        "hosted": {
            "chars_per_day": 40000,
            "chars_per_30_days": 300000,
            "used_day": used,
            "used_30_days": used,
        },
        "local": {
            "chars_per_day": 40000,
            "chars_per_30_days": 300000,
            "used_day": 0,
            "used_30_days": 0,
        },
    }
    assert answer["collections"] == [
        {"name": "home", "allow_hosted": True},
        {"name": "medical", "allow_hosted": False},
        {"name": "tax", "allow_hosted": True},
    ]
    ids = [host["id"] for host in answer["known"]]
    assert ids[:5] == ["claude-desktop", "claude-code", "codex", "openclaw", "generic"]
    assert set(answer["known"][0]) == {
        "id",
        "label",
        "name",
        "destination",
        "hosted",
        "local_program",
    }


def test_registering_apps_and_granting(unlocked: GateService) -> None:
    for name in ("Codex", "9lives", "my_app", "a" * 33, "", "né"):
        with refused(400, "invalid"):
            unlocked.add_app(name=name, hosted=False)
    with refused(400, "invalid"):
        unlocked.add_app(name="codex", hosted=True)
    with refused(400, "invalid"):
        unlocked.add_app(name="codex", hosted=True, destination="   ")
    with refused(409, "conflict"):
        unlocked.add_app(name="claude-desktop", hosted=True, destination="Anthropic")

    app = unlocked.add_app(name="codex", hosted=True, destination=" OpenAI ")["app"]
    assert (app["name"], app["destination"], app["grants"], app["served"]) == (
        "codex",
        "OpenAI",
        {},
        True,
    )
    assert unlocked.backend.search(Caller("codex"), "rent").text.startswith("OK: no matches")

    with refused(400, "invalid"):
        unlocked.set_grant(app="codex", collection="home", mode="write")
    with refused(404, "not_found"):
        unlocked.set_grant(app="nobody", collection="home", mode="read")
    with refused(404, "not_found"):
        unlocked.set_grant(app="codex", collection="nothing", mode="read")
    # A hosted app and a collection that is not open to hosted apps: two decisions, not one.
    with refused(409, "conflict") as caught:
        unlocked.set_grant(app="codex", collection="medical", mode="read")
    assert "separate decision" in caught[0].message
    assert unlocked.documents()["collections"][1]["allow_hosted"] is False

    app = unlocked.set_grant(app="codex", collection="home", mode="read")["app"]
    assert app["grants"] == {"home": "read"}
    assert REF.search(unlocked.backend.search(Caller("codex"), "rent").text)
    app = unlocked.set_grant(app="codex", collection="home", mode="none")["app"]
    assert app["grants"] == {}

    with refused(404, "not_found"):
        unlocked.remove_app(name="nobody")
    assert unlocked.remove_app(name="codex") == {}
    assert unlocked.backend.status(Caller("codex")).code == "NOT_ALLOWED"


def test_limits_of_an_app(unlocked: GateService) -> None:
    app = unlocked.set_app_limits(name="claude-desktop", limits={"chars_per_day": 800})["app"]
    assert app["limits"]["chars_per_day"] == 800
    assert app["limits"]["chars_per_30_days"] == 150000
    search = unlocked.backend.search(CLAUDE, "rent deposit")
    assert search.code == "OK"
    assert len(search.text) <= 800
    assert unlocked.backend.read(CLAUDE, REF.findall(search.text)[0]).code == "OVER_LIMIT"
    refusal = unlocked.activity(limit=1)["items"][0]
    assert (refusal["summary"], refusal["variant"]) == (
        "was refused: daily limit reached",
        "client-day",
    )

    app = unlocked.set_app_limits(name="claude-desktop", limits={"chars_per_day": None})["app"]
    assert app["limits"]["chars_per_day"] == 20000
    for bad in (
        {"chars_per_day": -1},
        {"chars_per_day": "many"},
        {"chars_per_day": 1.5},
        {"chars_per_day": True},
        {"chars_per_call": 8001},
        {"chars_per_call": 499},
        {"calls_per_minute": 0},
        {"everything": 1},
    ):
        with refused(400, "invalid"):
            unlocked.set_app_limits(name="claude-desktop", limits=bad)
    with refused(404, "not_found"):
        unlocked.set_app_limits(name="nobody", limits={"chars_per_day": 1})


def test_settings(unlocked: GateService) -> None:
    settings = unlocked.settings()
    assert settings == {
        "serial": 2,
        "defaults": {
            "chars_per_call": 2000,
            "chars_per_day": 20000,
            "chars_per_30_days": 150000,
            "searches_per_day": 100,
            "calls_per_minute": 30,
        },
        "groups": {
            "hosted": {"chars_per_day": 40000, "chars_per_30_days": 300000},
            "local": {"chars_per_day": 40000, "chars_per_30_days": 300000},
        },
        "per_document": {"max_chars": 8000, "max_fraction": 0.5},
        "approval": {"ttl_minutes": 10},
        "replaced_strings": 2,
        "ceilings": {"chars_per_call": 8000, "ttl_minutes": 60, "replaced_strings": 200},
    }
    changed = unlocked.set_settings(
        defaults={"chars_per_day": 30000},
        groups={"hosted": {"chars_per_day": 50000}},
        per_document={"max_fraction": 1},
        approval={"ttl_minutes": 60},
    )
    assert changed["serial"] == 3
    assert changed["defaults"]["chars_per_day"] == 30000
    assert changed["defaults"]["chars_per_call"] == 2000
    assert changed["groups"]["hosted"] == {"chars_per_day": 50000, "chars_per_30_days": 300000}
    assert changed["groups"]["local"] == settings["groups"]["local"]
    assert changed["per_document"] == {"max_chars": 8000, "max_fraction": 1.0}
    assert changed["approval"] == {"ttl_minutes": 60}
    assert unlocked.settings() == changed
    assert unlocked.set_settings() == changed  # nothing given: nothing changes, no new record
    for bad in (
        {"defaults": {"chars_per_call": 8001}},
        {"defaults": {"nothing": 1}},
        {"groups": {"cloud": {"chars_per_day": 1}}},
        {"groups": {"hosted": {"calls_per_minute": 1}}},
        {"per_document": {"max_fraction": 1.5}},
        {"per_document": {"max_chars": -1}},
        {"per_document": {"other": 1}},
        {"approval": {"ttl_minutes": 61}},
        {"approval": {"ttl_minutes": 0}},
    ):
        with refused(400, "invalid") as caught:
            unlocked.set_settings(**bad)
        assert caught[0].message.endswith(".")
    assert unlocked.settings() == changed

    for text in ("x", "", "?!", "y" * 201):
        with refused(400, "invalid"):
            unlocked.add_replaced_string(text=text)
    assert unlocked.settings()["replaced_strings"] == 2


def test_connect(unlocked: GateService, short_store_dir: Path) -> None:
    answer = unlocked.connect(name="claude-desktop")
    assert (answer["host"], answer["label"], answer["local_program"]) == (
        "claude-desktop",
        "Claude Desktop",
        True,
    )
    assert answer["explanation"] is None
    (block,) = answer["blocks"]
    command = json.loads(block["text"])["mcpServers"][SLUG]
    assert command["command"] == sys.executable
    assert command["args"][-4:] == ["--client", "claude-desktop", "--store", str(short_store_dir)]
    assert set(answer["ready"]) == {"sdk", "python", "starts", "message"}
    assert (answer["ready"]["message"] is None) == (
        answer["ready"]["sdk"] and answer["ready"]["python"]
    )
    assert answer["ready"]["starts"] is (answer["ready"]["message"] is None)
    assert gs.PASSPHRASE not in json.dumps(answer)

    generic = unlocked.connect(name="lm-studio")  # not an application the gate knows by name
    assert generic["host"] == "generic"
    assert "lm-studio" in generic["blocks"][0]["text"]
    assert unlocked.connect(name="lm-studio", host="codex")["host"] == "codex"

    web = unlocked.connect(name="claude-desktop", host="chatgpt-web")
    assert (web["local_program"], web["blocks"]) == (False, [])
    assert "cannot reach this vault" in web["explanation"]

    with refused(404, "not_found"):
        unlocked.connect(name="claude-desktop", host="netscape")
    with refused(404, "not_found"):
        unlocked.connect(name="nobody")


# -- the record ----------------------------------------------------------------------------


def test_verify_reads_the_record_again(unlocked: GateService, gate_clock: FakeClock) -> None:
    gate_clock.advance(minutes=5)
    answer = unlocked.verify()
    seconds = answer.pop("seconds")
    assert 0 <= seconds < 60
    head = unlocked.state()["record"]
    assert answer == {
        "verified": True,
        "head_seq": head["head_seq"],
        "head_hash": head["head_hash"],
        "records": head["head_seq"],
    }
    assert head["verified_at"] == "2026-11-12T09:05:00Z"

    # Somebody changes a record file behind the app's back.
    damaged = gs.record_files(unlocked.path)[3]
    blob = bytearray(damaged.read_bytes())
    blob[-1] ^= 1
    damaged.write_bytes(bytes(blob))
    answer = unlocked.verify()
    assert answer["verified"] is False
    assert answer["first_bad"] == 4
    assert "#4" in answer["message"]
    assert unlocked.state()["record"]["verified"] is False

    # Such a vault does not unlock.
    unlocked.lock()
    with refused(503, "unavailable") as caught:
        unlocked.unlock(passphrase=gs.PASSPHRASE)
    assert "#4" in caught[0].message
    assert unlocked.state()["locked"] is True
    blob[-1] ^= 1
    damaged.write_bytes(bytes(blob))
    assert unlocked.unlock(passphrase=gs.PASSPHRASE)["locked"] is False


def test_a_session_that_did_not_lock_is_reported(
    short_store_dir: Path, gate_clock: FakeClock
) -> None:
    store = gs.make_store(short_store_dir, clock=gate_clock)
    Gate(store)  # unlocked, and the program ends without locking
    store.close()
    service = GateService(short_store_dir, clock=gate_clock)
    try:
        state = service.unlock(passphrase=gs.PASSPHRASE)
        assert state["record"]["previous_session"] == "unclean"
        assert "previous_session_unclean" in [w["code"] for w in state["warnings"]]
        service.lock()
        state = service.unlock(passphrase=gs.PASSPHRASE)
        assert state["record"]["previous_session"] == "clean"
        assert "previous_session_unclean" not in [w["code"] for w in state["warnings"]]
    finally:
        service.close()


def test_a_vault_in_a_synchronised_folder(short_store_dir: Path, gate_clock: FakeClock) -> None:
    path = short_store_dir.parent / "Dropbox" / "v"
    service = GateService(path, clock=gate_clock, scrypt_n=gs.FAST_SCRYPT)
    try:
        with refused(400, "invalid") as caught:
            service.create_store(passphrase=gs.PASSPHRASE)
        assert "Dropbox" in caught[0].message
        assert not (path / "keyslot.wdva").exists()
        # One that is there all the same can be used, and the owner is told.
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        init_store(
            path, passphrase=gs.PASSPHRASE, scrypt_n=gs.FAST_SCRYPT, allow_synced_folder=True
        ).close()
        state = service.unlock(passphrase=gs.PASSPHRASE)
        (warning,) = [w for w in state["warnings"] if w["code"] == "synced_folder"]
        assert warning["detail"] == [str(path)]
    finally:
        service.close()


def test_a_path_too_long_for_the_socket_is_refused_before_anything_is_made(
    short_store_dir: Path,
) -> None:
    path = short_store_dir / ("deep" * 30)
    service = GateService(path, scrypt_n=gs.FAST_SCRYPT)
    with refused(400, "invalid"):
        service.create_store(passphrase=gs.PASSPHRASE)
    assert not path.exists()


# -- failures of the engine -----------------------------------------------------------------


def test_engine_errors_are_translated(
    unlocked: GateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = unlocked._session
    assert session is not None

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise LedgerError("record 12 could not be written to /secret/path", seq=12)

    monkeypatch.setattr(session.gate, "set_policy", fail)
    monkeypatch.setattr(session.gate, "approve", fail)
    calls: list[Callable[[], Any]] = [
        lambda: unlocked.set_collection(name="home", note="new"),
        lambda: unlocked.add_app(name="codex", hosted=True, destination="OpenAI"),
        lambda: unlocked.set_settings(defaults={"chars_per_day": 5}),
        lambda: unlocked.add_replaced_string(text="Northgate"),
        lambda: unlocked.release(app="claude-desktop", ref="d-0123456789ab.1", body_sha256="0"),
    ]
    for call in calls:
        with pytest.raises(Exception) as caught:  # noqa: PT011 - the kind is what is tested
            call()
        assert isinstance(caught.value, ServiceError)
        assert not isinstance(caught.value, WDVAError)
        assert (caught.value.status, caught.value.code) == (503, "unavailable")
        assert "/secret/path" not in caught.value.message


def test_a_gate_that_locked_behind_the_service_is_answered_as_locked(
    unlocked: GateService,
) -> None:
    session = unlocked._session
    assert session is not None
    session.gate.close("close")
    with refused(409, "locked"):
        unlocked.apps()
    assert unlocked.state()["locked"] is True


def test_answers_hold_when_the_policy_changes_between_two_looks(
    unlocked: GateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = unlocked._session
    assert session is not None
    # An app is removed between the moment the names are read and the moment the policy is.
    monkeypatch.setattr(session.gate, "served", lambda: ("claude-desktop", "just-removed"))
    assert unlocked.state()["serving"]["hosted"] == ["claude-desktop"]
    # An app is registered between the moment the use is read and the moment the policy is.
    newer = session.gate.policy().with_client("just-added", hosted=False)
    monkeypatch.setattr(session.gate, "policy", lambda: newer)
    assert [app["name"] for app in unlocked.apps()["apps"]] == ["claude-desktop", "lm-studio"]


# -- several callers at once ------------------------------------------------------------------


def test_many_threads_at_once(unlocked: GateService) -> None:
    errors: list[BaseException] = []
    ref = first_ref(unlocked, CLAUDE, "rent deposit")
    tax = first_ref(unlocked, CLAUDE, "income tax", "tax")

    def run(act: Callable[[int], Any]) -> None:
        try:
            for number in range(12):
                act(number)
        except BaseException as exc:
            errors.append(exc)

    def agent(number: int) -> None:
        unlocked.backend.search(CLAUDE, ("rent", "deposit", "notice")[number % 3])
        unlocked.backend.read(CLAUDE, (ref, tax)[number % 2])
        unlocked.backend.status(STRANGER)

    def owner(number: int) -> None:
        state = unlocked.state()
        assert state["locked"] is False
        activity = unlocked.activity(limit=5)
        for item in activity["items"][:2]:
            unlocked.activity_detail(seq=item["seq"])
        unlocked.approvals()
        unlocked.apps()
        unlocked.documents()

    def changes(number: int) -> None:
        unlocked.set_collection(name="home", note=f"note {number}")
        unlocked.set_serving(paused=["lm-studio"] if number % 2 else [])
        unlocked.dismiss(app="claude-desktop", ref=tax)

    threads = [
        threading.Thread(target=run, args=(act,), daemon=True)
        for act in (agent, agent, agent, owner, owner, changes)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not [thread for thread in threads if thread.is_alive()], "a call never came back"
    assert errors == []
    # The list is still one entry per record, in order.
    seqs = [item["seq"] for item in unlocked.activity(limit=200)["items"]]
    head = unlocked.state()["record"]["head_seq"]
    assert seqs[0] == head
    assert seqs == list(range(head, head - len(seqs), -1))
    assert unlocked.verify()["verified"] is True


def test_locking_while_calls_arrive(unlocked: GateService) -> None:
    stop = threading.Event()
    codes: list[str] = []

    def agent() -> None:
        # Real callers are other programs. The pause stands for that: threads that spin
        # inside this process would only keep the interpreter from the thread that locks.
        while not stop.wait(0.002):
            codes.append(unlocked.backend.search(CLAUDE, "rent").code)

    def until_one_more(locked: bool) -> None:
        """Wait until one more call was answered, by a locked vault or by an unlocked one."""
        have = sum(1 for code in codes if (code == "LOCKED") is locked)
        deadline = time.monotonic() + 60
        while sum(1 for code in codes if (code == "LOCKED") is locked) == have:
            assert time.monotonic() < deadline, "no call was answered"
            time.sleep(0.005)

    threads = [threading.Thread(target=agent, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    for _ in range(3):
        until_one_more(locked=False)
        assert unlocked.lock()["locked"] is True
        until_one_more(locked=True)
        assert unlocked.unlock(passphrase=gs.PASSPHRASE)["locked"] is False
    stop.set()
    for thread in threads:
        thread.join(timeout=60)
    assert not [thread for thread in threads if thread.is_alive()]
    assert {"OK", "LOCKED"} <= set(codes) <= {"OK", "LOCKED", "OVER_LIMIT"}
    assert unlocked.verify()["verified"] is True
    assert unlocked.state()["record"]["previous_session"] == "clean"


def test_the_auto_lock_time_is_what_the_gate_enforces(
    vault: GateService, gate_clock: FakeClock
) -> None:
    state = vault.unlock(passphrase=gs.PASSPHRASE, hours=2)
    session = vault._session
    assert session is not None
    assert session.gate.started.lock_at == gate_clock.now + timedelta(hours=2)
    assert state["locks_at"] == "2026-11-12T11:00:00Z"


# -- the demo's passphrase, and never a real one --------------------------------------------


def answers_beside_the_state(service: GateService) -> str:
    """What every reading call but the state returns from an unlocked vault, as one text."""
    activity = service.activity(limit=200)
    answers: list[Any] = [
        service.approvals(),
        service.documents(),
        service.apps(),
        service.settings(),
        service.verify(),
        activity,
    ]
    answers.extend(service.activity_detail(seq=item["seq"]) for item in activity["items"])
    answers.extend(service.connect(name=app["name"]) for app in service.apps()["apps"])
    return json.dumps(answers, ensure_ascii=False)


def test_a_demo_vault_sends_its_passphrase_with_the_state_and_only_there(
    short_store_dir: Path, gate_clock: FakeClock
) -> None:
    shown = "a-passphrase-everybody-may-see"
    gs.make_store(short_store_dir, passphrase=shown, clock=gate_clock).close()
    service = GateService(
        short_store_dir,
        demo=True,
        demo_passphrase=shown,
        clock=gate_clock,
        scrypt_n=gs.FAST_SCRYPT,
    )
    try:
        locked = service.state()
        assert (locked["demo"], locked["locked"], locked["demo_passphrase"]) == (True, True, shown)
        # It is the real one: whoever reads it can unlock, lock and answer when asked again.
        assert service.unlock(passphrase=shown)["demo_passphrase"] == shown
        gate_clock.advance(minutes=11)
        with refused(403, "reauth_required"):
            service.add_app(name="another-app", hosted=False)
        service.reauth(passphrase=shown)
        service.add_app(name="another-app", hosted=False)
        assert service.lock()["demo_passphrase"] == shown
        assert service.unlock(passphrase=shown)["locked"] is False
        # No other answer repeats it.
        assert shown not in answers_beside_the_state(service)
    finally:
        service.close()


def test_a_real_vault_never_sends_a_passphrase(
    service: GateService, short_store_dir: Path, gate_clock: FakeClock
) -> None:
    assert "demo_passphrase" not in service.state()  # no vault yet
    created = service.create_store(passphrase=gs.PASSPHRASE)
    assert created["demo"] is False
    service.add_document(collection="home", name="note.txt", data=b"A note about the boiler.")
    service.add_app(name="claude-desktop", hosted=True, destination="Anthropic")
    service.backend.status(CLAUDE)
    for state in (created, service.state(), service.lock()):
        assert "demo_passphrase" not in state
        assert "passphrase" not in json.dumps(state)
    service.unlock(passphrase=gs.PASSPHRASE)
    everything = every_answer(service)
    assert gs.PASSPHRASE not in everything
    assert "demo_passphrase" not in everything
    # A passphrase to show can only be given together with the word "demo".
    with pytest.raises(ValueError, match="demo"):
        GateService(short_store_dir, demo_passphrase=gs.PASSPHRASE, clock=gate_clock)
    # And a demo that was given none shows none.
    quiet = GateService(short_store_dir / "elsewhere", demo=True, clock=gate_clock)
    assert "demo_passphrase" not in quiet.state()


def test_unlocked_says_whether_the_vault_is_open(vault: GateService) -> None:
    assert vault.unlocked is False
    vault.unlock(passphrase=gs.PASSPHRASE)
    assert vault.unlocked is True
    vault.lock()
    assert vault.unlocked is False


# -- what the app knows about a file that was added -----------------------------------------


def test_the_app_says_only_what_it_knows_about_an_added_file(service: GateService) -> None:
    service.create_store(passphrase=gs.PASSPHRASE)
    added = service.add_document(collection="home", name="boiler.txt", data=b"The boiler note.")
    # What it knows: a copy of the file's bytes is sealed with the text.
    assert added["document"]["original_kept"] is True
    assert service.state()["encryption"]["originals_kept"] == 1
    entry = service.activity(kind="change", limit=1)["items"][0]
    assert entry["summary"] == "you added boiler to home (1 passage)"
    details = dict(service.activity_detail(seq=entry["seq"])["details"])
    assert details["copy of the file in the vault"] == "sealed"
    # What it does not know: where the chosen file lies and whether it is still there.
    # So no warning speaks of files outside the vault, and no field holds a place.
    state = service.state()
    assert [w["code"] for w in state["warnings"] if w["code"] == "plaintext_originals"] == []
    everything = every_answer(service)
    assert "original kept" not in everything
    assert "outside the vault" not in everything
    assert "plaintext" not in everything


# -- why a passage waits, and whether the command of a configuration can start -------------


def test_a_passage_past_the_free_allowance_waits_although_the_app_may_read(
    service: GateService,
) -> None:
    service.create_store(passphrase=gs.PASSPHRASE)
    text = b"The boiler was serviced in March. The next service is due in March."
    document = service.add_document(collection="home", name="boiler.txt", data=text)["document"]
    assert document["passages"] == 1
    service.set_collection(name="home", allow_hosted=True)
    service.add_app(name="claude-desktop", hosted=True, destination="Anthropic")
    service.set_grant(app="claude-desktop", collection="home", mode="read")
    ref = first_ref(service, CLAUDE, "boiler", "home")
    # The one passage is the whole document: more than the half that is free.
    assert service.backend.read(CLAUDE, ref).code == "NEEDS_APPROVAL"
    (waiting,) = service.approvals()["waiting"]
    assert (waiting["mode"], waiting["inside_free_allowance"]) == ("read", False)
    assert waiting["document"]["characters"] == document["characters"]
    assert waiting["document"]["free"] == (document["characters"] + 1) // 2
    assert waiting["document"]["after"] > waiting["document"]["free"]


def test_connect_says_when_the_command_cannot_start(
    unlocked: GateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = unlocked.connect(name="claude-desktop")["ready"]
    if not (ready["sdk"] and ready["python"]):
        pytest.skip("this environment cannot run the MCP server at all, which is said first")
    assert ready == {"sdk": True, "python": True, "starts": True, "message": None}

    asked: list[int] = []

    def no_start() -> str:
        asked.append(1)
        return "wdva must be installed in a virtual environment"

    monkeypatch.setattr(service_module, "start_problem", no_start)
    unlocked._start_checked = None
    for host in ("claude-desktop", "codex", "claude-desktop"):
        ready = unlocked.connect(name="claude-desktop", host=host)["ready"]
        assert ready == {
            "sdk": True,
            "python": True,
            "starts": False,
            "message": "Apps cannot connect yet: wdva must be installed in a virtual environment",
        }
    assert asked == [1], "the command is tried once, not for every look at the sheet"
    # A product that starts nothing has nothing to try.
    web = unlocked.connect(name="claude-desktop", host="chatgpt-web")
    assert web["ready"]["message"] is None
