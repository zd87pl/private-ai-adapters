"""The whole app: the interface over real HTTP, agents over the real socket, and the demo."""

from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from wdva._version import __version__
from wdva.gate import brand, hostconfig, messages
from wdva.gate.app import demo, main
from wdva.gate.app.api import ServiceError
from wdva.gate.app.demo import DemoAgent, DemoCall, make_demo_store
from wdva.gate.app.httpd import AppServer
from wdva.gate.app.service import NOT_REGISTERED, GateService
from wdva.gate.errors import GateError
from wdva.gate.link import RemoteGate
from wdva.gate.policy import Policy
from wdva.gate.types import Caller

from . import gate_support as gs

# The product's name comes from one place; the tests take it from there too.
PRODUCT, SLUG = brand.PRODUCT, brand.SLUG

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

CLAUDE = Caller("claude-desktop", "claude-ai", "1.2", "2025-11-25", "stdio")
REF = re.compile(r"ref=(d-[0-9a-f]{12}\.[0-9]+)")
PAIRING_LINE = re.compile(
    r"pairing code: ([A-Z2-9]{4}-[A-Z2-9]{4})   "
    r"\(type it into the page; anyone who can read this terminal can pair\)"
)
OPEN_LINE = re.compile(r"open:  http://127\.0\.0\.1:([0-9]+)/")


class Browser:
    """What the page does with the interface: it sends the session it was told, in a header.

    It keeps no cookie. An answer that sets one fails the test at once.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.token: str | None = None
        self.bodies: list[str] = []

    def call(
        self, method: str, path: str, body: Any = None, *, upload: bytes | None = None
    ) -> tuple[int, Any]:
        headers = {}
        if self.token is not None:
            headers["X-WDVA-Session"] = self.token
        if method != "GET":
            headers["X-WDVA-Gate"] = "1"
        payload: bytes | None = None
        if upload is not None:
            payload = upload
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        assert response.getheader("Set-Cookie") is None, "the session must never be a cookie"
        if response.getheader("Content-Type", "").startswith("application/json"):
            self.bodies.append(data.decode())
            answer = json.loads(data)
            if response.status == 200 and path.startswith("/api/session"):
                # The session is told to the page, which keeps it; the rest is the answer.
                self.token = answer.pop("session", self.token)
            return response.status, answer
        return response.status, data

    def ok(self, method: str, path: str, body: Any = None, *, upload: bytes | None = None) -> Any:
        status, answer = self.call(method, path, body, upload=upload)
        assert status == 200, answer
        return answer

    def error(self, method: str, path: str, body: Any = None) -> tuple[int, str]:
        status, answer = self.call(method, path, body)
        return status, answer["error"]["code"]


@dataclass
class App:
    service: GateService
    server: AppServer
    browser: Browser
    store: Path


@pytest.fixture
def app(short_store_dir: Path) -> Iterator[App]:
    """The app as ``wdva gate app`` runs it, for a place where no vault exists yet."""
    service = GateService(short_store_dir, scrypt_n=gs.FAST_SCRYPT)
    server = AppServer(service, port=0, pairing=True, version=__version__)
    server.start()
    try:
        yield App(service, server, Browser(server.port), short_store_dir)
    finally:
        server.stop()
        service.close()


def upload_path(collection: str, name: str) -> str:
    return f"/api/documents?collection={quote(collection)}&name={quote(name)}"


def test_the_whole_app_over_http_and_the_socket(app: App) -> None:
    browser, store = app.browser, app.store

    # Nothing answers before the page is paired with the code from the terminal.
    assert browser.error("GET", "/api/state") == (401, "pairing_required")
    assert browser.error("POST", "/api/session/pair", {"code": "AAAA-AAAA"}) == (403, "wrong_code")
    assert browser.ok("POST", "/api/session/pair", {"code": app.server.pairing_code}) == {
        "paired": True
    }

    # Create the vault.
    state = browser.ok("GET", "/api/state")
    assert state["store"] == {"exists": False, "path": str(store)}
    assert browser.error("POST", "/api/store/create", {"passphrase": "short"}) == (400, "invalid")
    state = browser.ok("POST", "/api/store/create", {"passphrase": gs.PASSPHRASE})
    assert (state["locked"], state["store"]["exists"]) == (False, True)

    # Add documents.
    lease = browser.ok(
        "POST", upload_path("home", "lease-2025.txt"), upload=gs.LEASE_TEXT.encode()
    )["document"]
    assert (lease["title"], lease["original_kept"]) == ("lease-2025", True)
    browser.ok("POST", upload_path("home", "bank letter.txt"), upload=gs.BANK_TEXT.encode())
    browser.ok("POST", upload_path("tax", "tax-note-2025.md"), upload=gs.TAX_TEXT.encode())
    browser.ok("POST", upload_path("medical", "referral-2026.txt"), upload=gs.MEDICAL_TEXT.encode())
    status, answer = browser.call(
        "POST", upload_path("tax", "again.txt"), upload=gs.LEASE_TEXT.encode()
    )
    assert (status, answer["error"]["code"]) == (409, "duplicate")
    status, answer = browser.call("POST", upload_path("home", "photo.png"), upload=b"\x89PNG")
    assert (status, answer["error"]["code"]) == (400, "invalid")
    names = [c["name"] for c in browser.ok("GET", "/api/documents")["collections"]]
    assert names == ["home", "medical", "tax"]

    # Register an app and grant it: the collection has to be opened to hosted apps first.
    registered = browser.ok(
        "POST", "/api/apps", {"name": "claude-desktop", "hosted": True, "destination": "Anthropic"}
    )["app"]
    assert (registered["served"], registered["grants"]) == (True, {})
    grant = {"app": "claude-desktop", "collection": "home", "mode": "read"}
    assert browser.error("POST", "/api/grants", grant) == (409, "conflict")
    browser.ok("POST", "/api/collections", {"name": "home", "allow_hosted": True})
    assert browser.ok("POST", "/api/grants", grant)["app"]["grants"] == {"home": "read"}
    browser.ok("POST", "/api/collections", {"name": "tax", "allow_hosted": True})
    browser.ok(
        "POST", "/api/grants", {"app": "claude-desktop", "collection": "tax", "mode": "approve"}
    )
    connect = browser.ok("GET", "/api/apps/claude-desktop/connect?host=claude-desktop")
    assert str(store) in connect["blocks"][0]["text"]

    # An agent application calls, through the program it starts and the vault's socket.
    agent = RemoteGate(store, "claude-desktop")
    assert agent.ping()["app"] == PRODUCT
    status_reply = agent.status(CLAUDE)
    assert status_reply.code == "OK"
    assert "home (search with snippets" in status_reply.text
    search = agent.search(CLAUDE, "residential lease agreement landlord")
    assert search.code == "OK"
    lease_ref = REF.findall(search.text)[0]
    read = agent.read(CLAUDE, lease_ref)
    assert read.code == "OK"
    assert gs.LEASE_MARKER in read.text
    assert agent.search(CLAUDE, "knee physiotherapy").text.startswith("OK: no matches")
    assert RemoteGate(store, "lm-studio").status(Caller("lm-studio")).code == "NOT_ALLOWED"

    # The owner sees each call, and the exact text that left.
    items = browser.ok("GET", "/api/activity?limit=5")["items"]
    assert [(i["app"], i["tool"], i["code"]) for i in items] == [
        (NOT_REGISTERED, "status", "NOT_ALLOWED"),
        ("claude-desktop", "search", "OK"),
        ("claude-desktop", "read", "OK"),
        ("claude-desktop", "search", "OK"),
        ("claude-desktop", "status", "OK"),
    ]
    assert lease_ref.startswith(lease["id"] + ".")
    assert items[2]["summary"] == (
        f"read passage {lease_ref.rpartition('.')[2]} of {lease['passages']} of lease-2025 in home"
    )
    assert (items[2]["destination"], items[2]["hosted"]) == ("Anthropic", True)
    assert gs.LEASE_MARKER not in browser.bodies[-1]
    detail = browser.ok("GET", f"/api/activity/{items[2]['seq']}")
    assert detail["text"] == read.text
    assert detail["claimed"] == "claude-ai 1.2"
    detail = browser.ok("GET", f"/api/activity/{items[3]['seq']}")
    assert (detail["text"], detail["query"]) == (
        search.text,
        "residential lease agreement landlord",
    )
    released = browser.ok("GET", "/api/activity?kind=release&app=claude-desktop")["items"]
    assert [item["tool"] for item in released] == ["search", "read", "search"]
    # What the app asked about itself is listed apart: it released no text.
    (question,) = browser.ok("GET", "/api/activity?kind=question")["items"]
    assert (question["tool"], question["characters"]) == ("status", 0)
    today = browser.ok("GET", "/api/state")["today"]
    assert (today["releases"], today["questions"]) == (3, 1)
    assert today["characters"] == sum(i["characters"] for i in released)
    usage = browser.ok("GET", "/api/apps")["apps"][0]["usage"]
    assert usage["chars_day"] == today["characters"]

    # A passage that needs the owner: asked for, shown, released once, read.
    tax_ref = REF.findall(agent.search(CLAUDE, "income tax", "tax").text)[0]
    waits = agent.read(CLAUDE, tax_ref)
    assert (waits.code, waits.text) == (
        "NEEDS_APPROVAL",
        messages.needs_approval(tax_ref, "claude-desktop"),
    )
    (waiting,) = browser.ok("GET", "/api/approvals")["waiting"]
    assert (waiting["app"], waiting["ref"], waiting["title"]) == (
        "claude-desktop",
        tax_ref,
        "tax-note-2025",
    )
    assert gs.TAX_MARKER in waiting["body"]
    assert browser.ok("GET", "/api/state")["today"]["approvals_waiting"] == 1
    approve = {"app": "claude-desktop", "ref": tax_ref, "body_sha256": "f" * 64}
    assert browser.error("POST", "/api/approvals/release", approve) == (409, "changed")
    assert agent.read(CLAUDE, tax_ref).code == "NEEDS_APPROVAL"
    approve["body_sha256"] = waiting["body_sha256"]
    assert "valid_until" in browser.ok("POST", "/api/approvals/release", approve)
    approved = agent.read(CLAUDE, tax_ref)
    assert approved.code == "OK"
    assert gs.TAX_MARKER in approved.text
    newest = browser.ok("GET", "/api/activity?limit=2")["items"]
    assert [(i["kind"], i["approved"]) for i in newest] == [("release", True), ("approval", False)]
    assert browser.ok("GET", f"/api/activity/{newest[0]['seq']}")["text"] == approved.text
    assert browser.ok("GET", "/api/approvals") == {"waiting": [], "granted": []}
    assert agent.read(CLAUDE, tax_ref).code == "NEEDS_APPROVAL"

    # A wrong passphrase is answered slowly, and changes nothing.
    began = time.monotonic()
    assert browser.error("POST", "/api/reauth", {"passphrase": "not the passphrase"}) == (
        403,
        "wrong_passphrase",
    )
    assert time.monotonic() - began >= 0.9
    assert "owner_mode_until" in browser.ok("POST", "/api/reauth", {"passphrase": gs.PASSPHRASE})

    # Lock: the agent side is told so, the socket is gone, the interface shows nothing more.
    assert (store / "run" / "gate.sock").exists()
    assert browser.ok("POST", "/api/lock", {})["locked"] is True
    locked = agent.read(CLAUDE, tax_ref)
    assert (locked.code, locked.text, locked.released) == ("LOCKED", messages.locked(), False)
    assert agent.status(CLAUDE).text == messages.locked()
    assert not (store / "run" / "gate.sock").exists()
    assert browser.error("GET", "/api/activity") == (409, "locked")
    assert browser.error("GET", "/api/approvals") == (409, "locked")
    assert browser.ok("POST", "/api/lock", {})["locked"] is True

    # Unlock again: the record tells the same story, and what waited is forgotten.
    assert browser.error("POST", "/api/unlock", {"passphrase": "not the passphrase"}) == (
        403,
        "wrong_passphrase",
    )
    state = browser.ok("POST", "/api/unlock", {"passphrase": gs.PASSPHRASE, "hours": 1})
    assert (state["locked"], state["record"]["previous_session"]) == (False, "clean")
    assert browser.ok("GET", f"/api/activity/{newest[0]['seq']}")["text"] == approved.text
    told = [i["summary"] for i in browser.ok("GET", "/api/activity?kind=session")["items"]]
    assert told == [
        "you unlocked the vault",
        "you locked the vault",
        "you unlocked the vault",
        "you created the vault",
    ]
    assert agent.status(CLAUDE).code == "OK"

    # In all of it, the passphrase was sent and never came back.
    assert not [body for body in browser.bodies if gs.PASSPHRASE in body]


def test_calls_over_the_socket_while_the_vault_is_locked_and_unlocked(app: App) -> None:
    gs.make_store(app.store).close()
    agent = RemoteGate(app.store, "claude-desktop")
    assert agent.status(CLAUDE).code == "LOCKED"
    stop = threading.Event()
    codes: list[str] = []

    def keep_calling() -> None:
        while not stop.wait(0.005):
            codes.append(agent.search(CLAUDE, "rent").code)

    def until_one_more(wanted: set[str]) -> None:
        have = sum(1 for code in codes if code in wanted)
        deadline = time.monotonic() + 60
        while sum(1 for code in codes if code in wanted) == have:
            assert time.monotonic() < deadline, "no call was answered"
            time.sleep(0.005)

    threads = [threading.Thread(target=keep_calling, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    for _ in range(3):
        assert app.service.unlock(passphrase=gs.PASSPHRASE)["locked"] is False
        until_one_more({"OK", "OVER_LIMIT"})  # answered by the gate
        assert app.service.lock()["locked"] is True
        until_one_more({"LOCKED"})
    stop.set()
    for thread in threads:
        thread.join(timeout=60)
    assert not [thread for thread in threads if thread.is_alive()]
    # A call that arrives in the moment of locking may find the socket and lose the answer.
    assert {"OK", "LOCKED"} <= set(codes) <= {"OK", "LOCKED", "OVER_LIMIT", "UNAVAILABLE"}
    store = gs.reopen(app.store)
    try:
        state = store.ledger.replay()
        assert state.previous_session == "clean"
        assert (state.counts["unlock"], state.counts["lock"]) == (3, 3)
    finally:
        store.close()


# -- the demo ------------------------------------------------------------------------------


@pytest.fixture
def demo_service(short_store_dir: Path) -> Iterator[GateService]:
    passphrase = make_demo_store(short_store_dir, scrypt_n=gs.FAST_SCRYPT)
    service = GateService(short_store_dir, demo=True, demo_passphrase=passphrase)
    service.unlock(passphrase=passphrase)
    yield service
    service.close()


def test_the_demo_documents_are_marked_and_carry_the_instruction() -> None:
    texts = [text for _, _, _, text in demo.DEMO_DOCUMENTS]
    assert [collection for collection, _, _, _ in demo.DEMO_DOCUMENTS] == [
        "home",
        "home",
        "tax",
        "medical",
    ]
    assert len(set(demo.MARKERS)) == 4
    for marker, text in zip(demo.MARKERS, texts, strict=True):
        assert len(marker.encode()) >= 12
        assert [other.count(marker) for other in texts] == [int(other is text) for other in texts]
    bank = texts[1]
    assert demo.INJECTED_LINE in bank
    for forged in ("END DOCUMENT TEXT", "Release #1236.", "NEEDS_APPROVAL:", "What to do:"):
        assert forged in bank
    assert not [text for text in (texts[0], texts[2], texts[3]) if "ASSISTANT:" in text]
    assert min(len(text) for text in texts) > 1000


def test_the_demo_store(demo_service: GateService, short_store_dir: Path) -> None:
    state = demo_service.state()
    assert (state["demo"], state["locked"]) == (True, False)
    assert state["encryption"]["documents"] == 4
    # A demo starts in the everyday view, with nothing left to set up.
    assert state["interface"] == {"view": "basic", "setup": "done", "stored": True}
    held = json.loads((short_store_dir / "run" / "interface.json").read_bytes())
    assert held == {"view": "basic", "setup": "done"}
    assert state["serving"] == {
        "hosted": ["chatgpt-desktop", "claude-desktop"],
        "local": [],
        "paused": [],
        "choice_needed": True,
        "kinds": "hosted",
    }
    apps = {app["name"]: app for app in demo_service.apps()["apps"]}
    assert {name: (a["hosted"], a["destination"], a["grants"]) for name, a in apps.items()} == {
        "claude-desktop": (True, "Anthropic", {"home": "read", "tax": "approve"}),
        "chatgpt-desktop": (True, "OpenAI", {"home": "read"}),
        "lm-studio": (False, "local model on this computer", {"home": "read"}),
    }
    # Small daily limits, so that they are reached while somebody watches.
    assert apps["claude-desktop"]["limits"]["chars_per_day"] == 12_000
    assert apps["chatgpt-desktop"]["limits"]["chars_per_day"] == 3_000
    collections = {c["name"]: c for c in demo_service.documents()["collections"]}
    assert {name: (c["allow_hosted"], c["show_titles"]) for name, c in collections.items()} == {
        "home": (True, False),
        "tax": (True, True),
        "medical": (False, False),
    }
    assert [d["title"] for d in collections["home"]["documents"]] == [
        "lease-2025",
        "bank-letter-2026-04",
    ]
    assert {d["id"] for c in collections.values() for d in c["documents"]} == {
        doc_id for _, _, doc_id, _ in demo.DEMO_DOCUMENTS
    }
    # Nobody is granted the medical note, whatever kinds of apps are answered.
    session = demo_service._session
    assert session is not None
    policy: Policy = session.gate.policy()
    assert not [name for name in policy.clients if policy.effective_grant(name, "medical")]

    # The demo's passphrase is fixed and shown, so that nobody is locked out of a demo.
    other = short_store_dir.parent / "other"
    assert make_demo_store(other, scrypt_n=gs.FAST_SCRYPT) == demo.DEMO_PASSPHRASE
    assert len(demo.DEMO_PASSPHRASE) >= 12
    assert state["demo_passphrase"] == demo.DEMO_PASSPHRASE
    locked = demo_service.lock()
    assert (locked["locked"], locked["demo_passphrase"]) == (True, demo.DEMO_PASSPHRASE)
    with pytest.raises(ServiceError) as caught:
        demo_service.unlock(passphrase="not-the-demo-passphrase")
    assert caught.value.code == "wrong_passphrase"
    assert demo_service.unlock(passphrase=locked["demo_passphrase"])["locked"] is False


def play(agent: DemoAgent, limit: int = 40) -> None:
    for _ in range(limit):
        if not agent.scripted:
            return
        agent.step()
    raise AssertionError("the script did not end")


def released_texts(service: GateService) -> dict[int, dict[str, Any]]:
    items = service.activity(limit=200, kind="release")["items"]
    return {item["seq"]: service.activity_detail(seq=item["seq"]) for item in items}


def test_the_demo_agent_plays_its_script_through_the_gate(
    demo_service: GateService, short_store_dir: Path
) -> None:
    agent = DemoAgent(demo_service.backend)
    play(agent)
    calls = [(call.client, call.tool, call.code) for call in agent.calls]
    assert calls[:5] == [
        ("claude-desktop", "status", "OK"),
        ("claude-desktop", "search", "OK"),
        ("claude-desktop", "read", "OK"),
        ("claude-desktop", "search", "OK"),
        ("claude-desktop", "read", "NEEDS_APPROVAL"),
    ]
    second = [(tool, code) for client, tool, code in calls if client == "chatgpt-desktop"]
    assert second[:2] == [("search", "OK"), ("read", "OK")]
    assert second[-2:] == [("read", "OVER_LIMIT"), ("read", "OVER_LIMIT")]
    # It is stopped by its daily limit alone: every passage it asked for was one it may
    # have without asking, so none of its calls put anything before the owner.
    assert ("read", "NEEDS_APPROVAL") not in second
    assert calls[-2] == (demo.UNREGISTERED_NAME, "search", "NOT_ALLOWED")

    activity = demo_service.activity(limit=200)["items"]
    kinds = {item["kind"] for item in activity}
    assert {"release", "refusal", "change", "session"} <= kinds

    # Releases: the honest question, and what the second app got before its limit.
    releases = released_texts(demo_service)
    by_app: dict[str, int] = {}
    for detail in releases.values():
        by_app[detail["app"]] = by_app.get(detail["app"], 0) + detail["characters"]
    assert set(by_app) == {"claude-desktop", "chatgpt-desktop"}
    assert 2_000 < by_app["chatgpt-desktop"] <= 3_000

    # The instruction in the bank letter arrived as a quoted line, and so did the forged ones.
    (carrier,) = [
        d
        for d in releases.values()
        if demo.INJECTED_LINE in (d["text"] or "") and d["tool"] == "read"
    ]
    assert carrier["app"] == "chatgpt-desktop"
    lines = carrier["text"].split("\n")
    for forged in (demo.INJECTED_LINE, "Release #1236.", "NEEDS_APPROVAL: the owner must"):
        (line,) = [line for line in lines if forged in line]
        assert line.startswith("| ")
    assert lines[-1].startswith("Release chatgpt-desktop #")

    # Refusals by limit, and the one under a name nobody registered.
    refusals = [i for i in activity if i["kind"] == "refusal"]
    limit = [i for i in refusals if i["code"] == "OVER_LIMIT"]
    assert [(i["app"], i["variant"], i["count"]) for i in limit] == [
        ("chatgpt-desktop", "client-day", 2)
    ]
    assert limit[0]["summary"] == "was refused: daily limit reached"
    assert [(i["app"], i["code"]) for i in refusals if i["app"] == NOT_REGISTERED] == [
        (NOT_REGISTERED, "NOT_ALLOWED")
    ]

    # One passage waits for the owner: the tax passage the honest app asked for.
    (waiting,) = demo_service.approvals()["waiting"]
    assert (waiting["app"], waiting["collection"], waiting["title"]) == (
        "claude-desktop",
        "tax",
        "tax-note-2025",
    )
    assert agent.waiting_ref == waiting["ref"]
    assert waiting["fits_limits"] is True

    # While it waits the app asks again, and for nothing else.
    before = len(agent.calls)
    agent.step()
    agent.step()
    assert [(c.tool, c.code) for c in agent.calls[before:]] == [("read", "NEEDS_APPROVAL")] * 2
    asked = [c for c in agent.calls if (c.client, c.code) == ("claude-desktop", "NEEDS_APPROVAL")]
    assert len(asked) == 4  # once in the script, once when it ended, twice just now
    assert demo_service.approvals()["waiting"][0]["times_asked"] == 4

    # The owner releases it, and the next time the app asks it gets the passage.
    demo_service.release(
        app="claude-desktop", ref=waiting["ref"], body_sha256=waiting["body_sha256"]
    )
    agent.step()
    assert (agent.calls[-1].tool, agent.calls[-1].code, agent.waiting_ref) == ("read", "OK", None)
    newest = demo_service.activity(limit=1)["items"][0]
    assert (newest["approved"], newest["summary"]) == (
        True,
        f"read passage {waiting['passage']} of {waiting['passages']} of tax-note-2025 in tax",
    )
    assert waiting["body"].split("\n")[0] in demo_service.activity_detail(seq=newest["seq"])["text"]
    agent.step()  # from now on, light questions
    assert (agent.calls[-1].client, agent.calls[-1].tool, agent.calls[-1].code) == (
        "claude-desktop",
        "search",
        "OK",
    )
    assert demo_service.approvals() == {"waiting": [], "granted": []}

    # The medical note never left: no release names its collection or carries its text.
    releases = released_texts(demo_service)
    everything = "\n".join(detail["text"] or "" for detail in releases.values())
    assert demo.MEDICAL_MARKER not in everything
    assert "migraine" not in everything
    assert not [d for d in releases.values() if "medical" in d["collections"]]
    for caller in (agent._honest, agent._second):
        assert demo_service.backend.search(caller, "migraine sumatriptan").text.startswith(
            "OK: no matches"
        )
        assert demo_service.backend.search(caller, "migraine", "medical").code == "NOT_FOUND"
    medical = [
        doc_id for collection, _, doc_id, _ in demo.DEMO_DOCUMENTS if collection == "medical"
    ]
    assert demo_service.backend.read(agent._honest, f"{medical[0]}.1").code == "NOT_FOUND"
    # The first passage of the lease, with its marker, is released too: it is then in the
    # record, in full, for the scan below to miss.
    lease = demo.DEMO_DOCUMENTS[0][2]
    first = demo_service.backend.read(agent._honest, f"{lease}.1")
    assert first.code == "OK"
    assert demo.LEASE_MARKER in first.text
    assert gs.leaked(demo.BANK_TEXT.encode(), demo.MARKERS) == [demo.BANK_MARKER]

    # No file of the store shows any of the documents.
    demo_service.lock()
    blob = b"".join(
        path.read_bytes() for path in sorted(short_store_dir.rglob("*")) if path.is_file()
    )
    assert len(blob) > 20_000
    assert gs.leaked(blob, [*demo.MARKERS, "Robin Aldermoor", "Port Sennard"]) == []
    assert b"Aldermoor" not in blob
    assert b"ASSISTANT" not in blob


def test_the_demo_agent_runs_as_a_thread(demo_service: GateService) -> None:
    agent = DemoAgent(
        demo_service.backend, pause=(0.005, 0.01), idle_seconds=0.03, retry_seconds=0.01
    )
    agent.start()
    try:
        deadline = time.monotonic() + 60
        while agent.scripted and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not agent.scripted
        while not demo_service.approvals()["waiting"] and time.monotonic() < deadline:
            time.sleep(0.01)
        (waiting,) = demo_service.approvals()["waiting"]
        demo_service.release(
            app=waiting["app"], ref=waiting["ref"], body_sha256=waiting["body_sha256"]
        )
        while agent.waiting_ref is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert agent.waiting_ref is None
        asked = len(agent.calls)
        while len(agent.calls) < asked + 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [call.tool for call in agent.calls[asked : asked + 2]] == ["search", "search"]
        # A locked vault does not end the thread: the calls are answered LOCKED.
        demo_service.lock()
        asked = len(agent.calls)
        while len(agent.calls) < asked + 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert agent.calls[-1].code == "LOCKED"
        assert agent.is_alive()
    finally:
        agent.stop()
    assert not agent.is_alive()


def test_the_demo_agent_waits_while_the_vault_is_locked(
    demo_service: GateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Told whether the vault is unlocked, the agent does not play its story to a locked
    # vault: it makes no call until the owner has unlocked again, then goes on.
    monkeypatch.setattr(demo, "_LOCKED_WAIT", 0.005)
    demo_service.lock()
    agent = DemoAgent(
        demo_service.backend,
        pause=(0.005, 0.01),
        idle_seconds=0.03,
        retry_seconds=0.01,
        ready=lambda: demo_service.unlocked,
    )
    agent.start()
    try:
        time.sleep(0.3)
        assert agent.calls == []
        assert agent.scripted
        assert agent.is_alive()
        demo_service.unlock(passphrase=demo.DEMO_PASSPHRASE)
        deadline = time.monotonic() + 60
        while agent.scripted and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not agent.scripted
        assert "LOCKED" not in {call.code for call in agent.calls}
        # The whole story was played: the passage that waits is there for the owner.
        while not demo_service.approvals()["waiting"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(demo_service.approvals()["waiting"]) == 1
        # Locked again, it falls silent again.
        demo_service.lock()
        time.sleep(0.1)
        asked = len(agent.calls)
        time.sleep(0.3)
        assert len(agent.calls) == asked
    finally:
        agent.stop()
    assert not agent.is_alive()


def wait_until(probe: Any, what: str, seconds: float = 60) -> Any:
    """Call ``probe`` until it returns something true; fail with ``what`` when it never does."""
    deadline = time.monotonic() + seconds
    while True:
        found = probe()
        if found:
            return found
        assert time.monotonic() < deadline, what
        time.sleep(0.005)


def asked_and_told_to_wait(agent: DemoAgent) -> int:
    """How often the played app's first name asked for a passage and was told to wait."""
    wanted = ("claude-desktop", "read", "NEEDS_APPROVAL")
    return sum(1 for call in list(agent.calls) if (call.client, call.tool, call.code) == wanted)


def script_is_over(agent: DemoAgent) -> bool:
    """Whether the script has ended and the one call that still follows it was made.

    In this version the played app asks once more for its passage as the script
    ends (the timed retry's first turn), so two such reads mark the end.
    """
    return not agent.scripted and asked_and_told_to_wait(agent) >= 2


def test_the_played_app_asks_again_only_when_told(short_store_dir: Path) -> None:
    passphrase = make_demo_store(short_store_dir, scrypt_n=gs.FAST_SCRYPT)
    agents: list[DemoAgent] = []
    service = GateService(
        short_store_dir,
        demo=True,
        demo_passphrase=passphrase,
        demo_again=lambda: agents[0].ask_again(),  # as the app wires it at start
    )
    service.unlock(passphrase=passphrase)
    # The timed retry is still part of this version (it goes together with the button that
    # replaces it). Here it is set so long that it cannot fire: what is tested is the asking.
    agent = DemoAgent(service.backend, pause=(0.005, 0.01), idle_seconds=0.05, retry_seconds=3600)
    agents.append(agent)
    first = "claude-desktop"

    def waiting() -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = service.approvals()["waiting"]
        return found

    agent.start()
    try:
        wait_until(lambda: script_is_over(agent), "the script did not end")
        (passage,) = wait_until(waiting, "no passage waits after the script")
        assert passage["app"] == first
        # Asked once in the script and, in this version still, once more as the script ended.
        asked = passage["times_asked"]
        assert asked == 2
        made = len(agent.calls)
        # Several idle periods pass: the first name makes no further call. It was told not
        # to ask for anything until the owner has answered.
        time.sleep(0.5)
        assert len(agent.calls) == made
        assert waiting()[0]["times_asked"] == asked

        # Told once, through the app: exactly one read, which waits again.
        assert service.demo_again() == {}
        wait_until(lambda: len(agent.calls) > made, "the played app did not ask again")
        time.sleep(0.5)
        assert agent.calls[made:] == [DemoCall(first, "read", "NEEDS_APPROVAL", False)]
        assert waiting()[0]["times_asked"] == asked + 1

        # The owner says no. The card does not come back by itself ...
        service.dismiss(app=first, ref=passage["ref"])
        time.sleep(0.5)
        assert waiting() == []
        assert len(agent.calls) == made + 1
        # ... and when the played app is told to ask again, the passage waits again: it was
        # not told what the owner decided, as a real app is not.
        agent.ask_again()
        wait_until(lambda: len(agent.calls) > made + 1, "the played app did not ask again")
        (again,) = waiting()
        assert (again["ref"], again["times_asked"]) == (passage["ref"], 1)
        assert agent.calls[made + 1 :] == [DemoCall(first, "read", "NEEDS_APPROVAL", False)]

        # The owner releases it once. Nothing is read until somebody says "try again".
        service.release(app=first, ref=again["ref"], body_sha256=again["body_sha256"])
        time.sleep(0.5)
        assert len(agent.calls) == made + 2
        assert agent.waiting_ref == passage["ref"]
        assert [g["ref"] for g in service.approvals()["granted"]] == [passage["ref"]]
        agent.ask_again()
        wait_until(lambda: agent.waiting_ref is None, "the approved passage was not read")
        assert agent.calls[made + 2] == DemoCall(first, "read", "OK", True)
        released = service.activity(limit=20, kind="release", app=first)["items"]
        assert [item["ref"] for item in released if item["approved"]] == [passage["ref"]]
        # From then on the light questions begin, as before.
        wait_until(lambda: len(agent.calls) >= made + 5, "no light question followed")
        assert {(c.client, c.tool, c.code) for c in agent.calls[made + 3 :]} == {
            (first, "search", "OK")
        }
        # Told to ask again when nothing waits, it has nothing to ask.
        agent.ask_again()
        time.sleep(0.4)
        assert "read" not in {call.tool for call in agent.calls[made + 3 :]}
        assert service.approvals() == {"waiting": [], "granted": []}
    finally:
        agent.stop()
        service.close()
    assert not agent.is_alive()


def test_the_played_app_asks_again_between_two_steps_of_the_script(
    demo_service: GateService,
) -> None:
    agent = DemoAgent(
        demo_service.backend, pause=(0.15, 0.15), idle_seconds=3600, retry_seconds=3600
    )
    agent.start()
    try:
        wait_until(lambda: agent.waiting_ref is not None, "the script never asked for its passage")
        assert agent.scripted, "the script was over before the passage waited"
        agent.ask_again()
        # One read in the script, the one that was asked for, and, in this version still,
        # one as the script ended.
        wait_until(
            lambda: not agent.scripted and asked_and_told_to_wait(agent) >= 3,
            "the script did not end, or the played app did not ask again",
        )
        calls = [(call.client, call.tool, call.code) for call in agent.calls]
        asked = [n for n, call in enumerate(calls) if call[1:] == ("read", "NEEDS_APPROVAL")]
        assert len(asked) == 3
        # The second came while the script was still being played, which went on after it.
        last_of_the_script = calls.index((demo.UNREGISTERED_NAME, "search", "NOT_ALLOWED"))
        assert asked[1] < last_of_the_script < asked[2]
        assert demo_service.approvals()["waiting"][0]["times_asked"] == 3
    finally:
        agent.stop()


def test_a_request_to_ask_again_is_not_kept_for_a_vault_that_was_locked(
    demo_service: GateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo, "_LOCKED_WAIT", 0.005)
    agent = DemoAgent(
        demo_service.backend,
        pause=(0.005, 0.01),
        idle_seconds=3600,
        retry_seconds=3600,
        ready=lambda: demo_service.unlocked,
    )
    agent.start()
    try:
        wait_until(lambda: script_is_over(agent), "the script did not end")
        wait_until(lambda: demo_service.approvals()["waiting"], "no passage waits")
        demo_service.lock()
        made = len(agent.calls)
        agent.ask_again()  # nobody can: the app refuses it while locked. Should it happen:
        time.sleep(0.6)
        assert len(agent.calls) == made, "the played app called a locked vault"
        demo_service.unlock(passphrase=demo.DEMO_PASSPHRASE)
        time.sleep(0.6)
        assert len(agent.calls) == made, "a request from before the lock was made up for"
        assert demo_service.approvals()["waiting"] == []
        # Asked now, it asks.
        agent.ask_again()
        wait_until(lambda: len(agent.calls) > made, "the played app did not ask again")
        assert agent.calls[-1] == DemoCall("claude-desktop", "read", "NEEDS_APPROVAL", False)
    finally:
        agent.stop()


# -- the three newer addresses, over HTTP, against a real vault ---------------------------------


def record_and_files(browser: Browser) -> tuple[Any, ...]:
    state = browser.ok("GET", "/api/state")
    return (
        state["record"]["head_seq"],
        state["record"]["head_hash"],
        state["store"]["sealed_files"],
        state["store"]["sealed_bytes"],
    )


def test_the_view_the_check_and_the_demo_address_over_http(
    app: App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What looks for another program's settings looks into a home that this test owns.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    browser, store = app.browser, app.store
    check = "/api/apps/claude-desktop/connect/check"
    posts = [("/api/interface", {"view": "pro"}), (check, {}), ("/api/demo/again", {})]

    # Each needs the session, like every call.
    for path, body in posts:
        assert browser.error("POST", path, body) == (401, "pairing_required")
    browser.ok("POST", "/api/session/pair", {"code": app.server.pairing_code})

    # Without a vault there is no view to keep, and none of the three makes anything.
    assert browser.error("POST", "/api/interface", {"view": "pro"}) == (404, "not_found")
    assert browser.error("POST", check, {}) == (409, "locked")
    assert browser.error("POST", "/api/demo/again", {}) == (404, "not_found")
    assert not store.exists()

    state = browser.ok("POST", "/api/store/create", {"passphrase": gs.PASSPHRASE})
    assert state["interface"] == {"view": "basic", "setup": "open", "stored": True}
    assert state["launch"] == {"command": None, "port_fixed": False}  # built by the command
    browser.ok(
        "POST", "/api/apps", {"name": "claude-desktop", "hosted": True, "destination": "Anthropic"}
    )
    head = record_and_files(browser)
    listed = browser.ok("GET", "/api/activity?limit=200")

    # -- the view: answered with the state, changed by nothing but the two words
    state = browser.ok("POST", "/api/interface", {"view": "pro"})
    assert (state["locked"], state["interface"]) == (
        False,
        {"view": "pro", "setup": "open", "stored": True},
    )
    state = browser.ok("POST", "/api/interface", {"setup": "done"})
    assert state["interface"] == {"view": "pro", "setup": "done", "stored": True}
    for body in (
        {"view": "expert"},  # a wrong value
        {"view": ""},
        {"setup": "open"},
        {},  # no field
        {"view": "basic", "stored": False},  # a field that is not declared
        {"view": "basic", "path": str(tmp_path / "elsewhere.json")},
        {"view": 1},
        {"interface": {"view": "basic"}},
    ):
        assert browser.error("POST", "/api/interface", body) == (400, "invalid"), body
    assert browser.error("GET", "/api/interface") == (404, "not_found")
    assert browser.ok("GET", "/api/state")["interface"] == state["interface"]
    file = store / "run" / "interface.json"
    assert json.loads(file.read_bytes()) == {"view": "pro", "setup": "done"}
    assert not list(tmp_path.glob("elsewhere*"))

    # -- the check: one word about one file, and not a byte of it
    secret = "sk-MADE-UP-KEY-OF-ANOTHER-TOOL-90817"
    assert browser.ok("POST", check, {}) == {
        "state": "no_folder",
        "path": "~/.config/Claude/claude_desktop_config.json"
        if sys.platform != "darwin"
        else "~/Library/Application Support/Claude/claude_desktop_config.json",
        "folder": "~/.config/Claude"
        if sys.platform != "darwin"
        else "~/Library/Application Support/Claude",
        "open_line": None,
        "section": None,
    }
    assert not home.exists()
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        place = home / "Library" / "Application Support" / "Claude" / place.name
    place.parent.mkdir(parents=True)
    place.write_text(
        json.dumps(
            {
                "apiKey": secret,
                "mcpServers": {"another-tool": {"command": "tool", "env": {"TOKEN": secret}}},
                secret: {"nested": [secret]},
            }
        ),
        encoding="utf-8",
    )

    def in_the_home() -> tuple[Any, ...]:
        return (place.read_bytes(), place.stat().st_mtime_ns, sorted(home.rglob("*")))

    before = in_the_home()
    seen = len(browser.bodies)
    answer = browser.ok("POST", check, {"host": "claude-desktop"})
    assert (answer["state"], answer["section"]) == ("other", "filled")
    assert str(place) in answer["open_line"]
    assert in_the_home() == before, "the check changed something in the home folder"
    place.write_bytes(place.read_bytes()[:-1])  # the last brace is missing
    before = in_the_home()
    assert browser.ok("POST", check, {})["state"] == "unreadable"
    assert in_the_home() == before
    for body in browser.bodies[seen:]:
        assert secret not in body, "a byte of the file reached an answer"
        assert "another-tool" not in body
    # The request names the app and the kind of application, never a file.
    for body in (
        {"path": str(place)},
        {"host": "claude-desktop", "file": "/etc/passwd"},
        {"host": 7},
    ):
        assert browser.error("POST", check, body) == (400, "invalid"), body
    for host in ("/etc/passwd", "../codex", "netscape"):
        assert browser.error("POST", check, {"host": host}) == (404, "not_found")
    assert browser.error("POST", "/api/apps/nobody/connect/check", {}) == (404, "not_found")
    assert browser.error("POST", f"{check}?path=/etc/passwd", {}) == (400, "invalid")
    assert browser.error("GET", check) == (404, "not_found")
    assert browser.ok("POST", check, {"host": "claude-code"})["state"] == "unsupported"
    # Asking for the text reads no file; its new fields come with it.
    connect = browser.ok("GET", "/api/apps/claude-desktop/connect")
    assert connect["blocks"][0]["where_here"] == answer["path"]
    assert set(connect["blocks"][0]["inner"]) == {"entry", "section"}
    assert "://" not in connect["plain_note"]
    assert connect["after"]["desktop"] is True

    # -- the demo's address does not exist for a real vault
    assert browser.error("POST", "/api/demo/again", {}) == (404, "not_found")
    assert browser.error("POST", "/api/demo/again", {"app": "claude-desktop"}) == (400, "invalid")

    # None of it wrote into the record or sealed anything.
    assert record_and_files(browser) == head
    assert browser.ok("GET", "/api/activity?limit=200") == listed

    # Locked: the view can still be kept; the check is refused; the demo's address is not there.
    assert browser.ok("POST", "/api/lock", {})["locked"] is True
    head = record_and_files(browser)
    state = browser.ok("POST", "/api/interface", {"view": "basic"})
    assert (state["locked"], state["interface"]) == (
        True,
        {"view": "basic", "setup": "done", "stored": True},
    )
    assert browser.error("POST", "/api/interface", {"view": "expert"}) == (400, "invalid")
    assert browser.error("POST", check, {}) == (409, "locked")
    assert browser.error("POST", "/api/demo/again", {}) == (404, "not_found")
    assert record_and_files(browser) == head
    # And the view that was kept is the one the vault opens with.
    state = browser.ok("POST", "/api/unlock", {"passphrase": gs.PASSPHRASE})
    assert state["interface"] == {"view": "basic", "setup": "done", "stored": True}
    assert not [body for body in browser.bodies if gs.PASSPHRASE in body]


def test_asking_the_played_app_again_over_http(short_store_dir: Path) -> None:
    passphrase = make_demo_store(short_store_dir, scrypt_n=gs.FAST_SCRYPT)
    told: list[int] = []
    service = GateService(
        short_store_dir,
        demo=True,
        demo_passphrase=passphrase,
        demo_again=lambda: told.append(1),
    )
    server = AppServer(service, port=0, pairing=False, version=__version__, demo=True)
    server.start()
    try:
        browser = Browser(server.port)
        assert browser.error("POST", "/api/demo/again", {}) == (401, "pairing_required")
        browser.ok("GET", "/api/session")  # a demo hands its session to the page
        # Locked, nobody is asked.
        assert browser.error("POST", "/api/demo/again", {}) == (409, "locked")
        assert told == []
        browser.ok("POST", "/api/unlock", {"passphrase": passphrase})
        head = record_and_files(browser)
        assert browser.ok("POST", "/api/demo/again", {}) == {}
        assert browser.ok("POST", "/api/demo/again") == {}  # the page sends no body
        assert told == [1, 1]
        assert browser.error("POST", "/api/demo/again", {"times": 5}) == (400, "invalid")
        assert browser.error("GET", "/api/demo/again") == (404, "not_found")
        assert told == [1, 1]
        assert record_and_files(browser) == head
    finally:
        server.stop()
        service.close()


# -- the command -----------------------------------------------------------------------------


def launch_words(state: dict[str, Any]) -> list[str]:
    """The words of the command that starts the app again, after the interpreter's path.

    The first word must be this interpreter, by its full path: the command is for a
    terminal that may find another Python first, or none.
    """
    words = shlex.split(state["launch"]["command"])
    assert os.path.isabs(words[0]), words[0]
    assert os.path.samefile(words[0], sys.executable), words[0]
    return words[1:]


def start_command(*arguments: str) -> subprocess.Popen[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("WDVA_")}
    return subprocess.Popen(
        [sys.executable, "-m", "wdva", "gate", "app", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )


def start_changed(change: str, *arguments: str) -> subprocess.Popen[str]:
    """``wdva gate app`` in a process where ``change`` ran first: a stand-in for another machine."""
    code = f"import sys\n{change}\nfrom wdva.cli import main\nsys.exit(main(sys.argv[1:]))\n"
    environment = {k: v for k, v in os.environ.items() if not k.startswith("WDVA_")}
    return subprocess.Popen(
        [sys.executable, "-c", code, "gate", "app", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )


def apps_line(status: dict[str, Any]) -> list[str]:
    """The start-up line for an environment in which no app can connect; none where one can."""
    if not status["python"]:
        return [
            "apps:  cannot connect: the program they start needs Python 3.11 or newer, and "
            f"{PRODUCT} runs on an older one."
        ]
    if not status["sdk"]:
        return [
            f"apps:  cannot connect yet: the MCP SDK is not installed where {PRODUCT} runs. "
            f"Install it into the same environment with:  {hostconfig.INSTALL_LINE}"
        ]
    return []


def start_up_lines(process: subprocess.Popen[str]) -> list[str]:
    """The start-up lines of this environment, without the one about apps, which is checked here.

    That line comes after the address and only where apps cannot connect: under a Python
    older than 3.11, or without the MCP SDK.
    """
    about_apps = apps_line(hostconfig.sdk_status())
    lines = first_lines(process, 5 + len(about_apps))
    assert lines[3 : 3 + len(about_apps)] == about_apps, lines
    return lines[:3] + lines[3 + len(about_apps) :]


def first_lines(process: subprocess.Popen[str], count: int) -> list[str]:
    """The start-up lines. A process that says nothing for a minute is ended."""
    watchdog = threading.Timer(60, process.kill)
    watchdog.start()
    try:
        assert process.stdout is not None
        return [process.stdout.readline().rstrip("\n") for _ in range(count)]
    finally:
        watchdog.cancel()


def finish(process: subprocess.Popen[str], sent: int | None = None) -> tuple[int, str, str]:
    if sent is not None:
        process.send_signal(sent)
    try:
        out, err = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        raise AssertionError(f"the app did not stop: {err}") from None
    return process.returncode, out, err


def test_the_command_runs_the_demo_and_removes_its_store() -> None:
    process = start_command("--demo", "--no-browser", "--port", "0")
    try:
        lines = start_up_lines(process)
        assert lines[0] == f"{PRODUCT} {__version__}"
        assert lines[1] == "store: a temporary demo store with invented documents"
        opened = OPEN_LINE.fullmatch(lines[2])
        assert opened is not None, lines
        # The demo's passphrase is printed, because nobody could know it otherwise.
        assert lines[3] == (
            f"demo passphrase: {demo.DEMO_PASSPHRASE}   "
            "(the page shows it too; the passphrase of a real vault is never shown)"
        )
        assert lines[4] == "Ctrl-C locks the vault and quits."

        # No pairing in the demo: whoever loads the page is given the session.
        browser = Browser(int(opened.group(1)))
        status, page = browser.call("GET", "/")
        assert status == 200
        assert b"<title>" in page
        assert browser.error("GET", "/api/state") == (401, "pairing_required")
        # The page asks for the session and is given it: every document here is invented.
        assert browser.ok("GET", "/api/session")["paired"] is False
        assert re.fullmatch(r"[0-9a-f]{64}", browser.token or "")
        state = browser.ok("GET", "/api/state")
        assert (state["demo"], state["locked"], state["version"]) == (True, False, __version__)
        store = Path(state["store"]["path"])
        assert (store / "keyslot.wdva").is_file()
        assert (store / "run" / "gate.sock").exists()
        assert browser.ok("GET", "/api/session") == {
            "paired": True,
            "pairing": "none",
            "demo": True,
            "version": __version__,
            "name": PRODUCT,
        }
        assert len(browser.ok("GET", "/api/apps")["apps"]) == 3
        # The page is told the passphrase too, so "Lock now" is not the end of a demo.
        assert state["demo_passphrase"] == demo.DEMO_PASSPHRASE
        locked = browser.ok("POST", "/api/lock", {})
        assert (locked["locked"], locked["demo_passphrase"]) == (True, demo.DEMO_PASSPHRASE)
        again = browser.ok("POST", "/api/unlock", {"passphrase": locked["demo_passphrase"]})
        assert again["locked"] is False
        browser.ok("POST", "/api/reauth", {"passphrase": demo.DEMO_PASSPHRASE})
        # The demo opens in the everyday view, set up, and says how to start for real: the
        # same installation, without the demo and without its temporary folder.
        assert state["interface"] == {"view": "basic", "setup": "done", "stored": True}
        assert launch_words(state) == ["-m", "wdva", "gate", "app", "--no-browser"]
        assert state["launch"]["port_fixed"] is False
        own = state["launch"]["command"]
        assert again["launch"] == locked["launch"] == state["launch"]
    except BaseException:
        process.kill()
        raise
    code, out, err = finish(process, signal.SIGTERM)
    # The start-up lines stay five. A demo that stops prints one more.
    assert (code, out, err) == (0, f"To start with your own documents: {own}\n", "")
    assert not store.exists()


def test_the_command_prints_the_pairing_code_and_ctrl_c_locks_the_vault(
    short_store_dir: Path,
) -> None:
    process = start_command("--no-browser", "--store", str(short_store_dir), "--lock-after", "1")
    try:
        lines = start_up_lines(process)
        assert lines[0] == f"{PRODUCT} {__version__}"
        assert lines[1] == f"store: {short_store_dir}"
        opened = OPEN_LINE.fullmatch(lines[2])
        paired = PAIRING_LINE.fullmatch(lines[3])
        assert opened is not None, lines
        assert paired is not None, lines
        assert lines[4] == "Ctrl-C locks the vault and quits."

        browser = Browser(int(opened.group(1)))
        status, _ = browser.call("GET", "/")
        assert status == 200
        # Neither the page nor the session endpoint gives the session: only the code does.
        assert browser.ok("GET", "/api/session")["paired"] is False
        assert browser.token is None
        assert browser.error("GET", "/api/state") == (401, "pairing_required")
        browser.ok("POST", "/api/session/pair", {"code": paired.group(1).lower()})
        before = browser.ok("GET", "/api/state")
        state = browser.ok("POST", "/api/store/create", {"passphrase": gs.PASSPHRASE})
        assert state["locked"] is False
        # A real vault's passphrase is never sent back and never printed.
        locked = browser.ok("POST", "/api/lock", {})
        for answer in (before, state, locked, browser.ok("GET", "/api/session")):
            assert "demo_passphrase" not in answer
            assert "passphrase" not in json.dumps(answer)
        assert not [line for line in lines if "passphrase" in line]
        state = browser.ok("POST", "/api/unlock", {"passphrase": gs.PASSPHRASE})
        # One hour, as the command line said.
        assert state["locks_at"][11:13] == f"{(int(state['unlocked_at'][11:13]) + 1) % 24:02d}"
        # The command that starts this installation on this vault again, as it was started.
        flags = ["--store", str(short_store_dir), "--lock-after", "1", "--no-browser"]
        assert launch_words(state) == ["-m", "wdva", "gate", "app", *flags]
        assert state["launch"]["port_fixed"] is False
        assert before["launch"] == locked["launch"] == state["launch"]
        # A new vault starts in the everyday view, with setting up still to do.
        assert before["interface"] is None
        assert state["interface"] == {"view": "basic", "setup": "open", "stored": True}
        # A real vault has no played app to tell anything.
        assert browser.error("POST", "/api/demo/again", {}) == (404, "not_found")
    except BaseException:
        process.kill()
        raise
    code, out, err = finish(process, signal.SIGINT)
    assert (code, out, err) == (0, "", "")
    assert gs.PASSPHRASE not in "".join(lines)
    # The vault was locked on the way out, and the record says how.
    assert not (short_store_dir / "run" / "gate.sock").exists()
    store = gs.reopen(short_store_dir)
    try:
        records = list(store.ledger.records())
        assert (records[-1].kind, records[-1].data["reason"]) == ("lock", "interrupt")
        assert store.ledger.replay().previous_session == "clean"
    finally:
        store.close()


FAKE_WINDOW = '''
"""In place of pywebview: load what a window loads first, note what came back, and close."""
import http.client, json, os

def create_window(title, url):
    global address
    address = url

def start():
    port = int(address.rstrip("/").rsplit(":", 1)[1])
    seen = {}
    for path in ("/", "/api/session", "/api/state"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        connection.request("GET", path)
        answer = connection.getresponse()
        body = answer.read().decode()
        seen[path] = [answer.status, answer.getheader("Set-Cookie"), "" if path == "/" else body]
        connection.close()
    with open(os.environ["WINDOW_SAW"], "w") as out:
        json.dump(seen, out)
'''


def test_a_window_of_its_own_pairs_like_a_browser(short_store_dir: Path, tmp_path: Path) -> None:
    # Whatever loads the page first, the app's window or a program that was quicker, is
    # given nothing by it: a real vault's session is had with the code from the terminal.
    (tmp_path / "webview.py").write_text(FAKE_WINDOW, encoding="utf-8")
    saw = tmp_path / "saw.json"
    environment = {k: v for k, v in os.environ.items() if not k.startswith("WDVA_")}
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), *filter(None, [environment.get("PYTHONPATH")])]
    )
    environment["WINDOW_SAW"] = str(saw)
    process = subprocess.Popen(
        [sys.executable, "-m", "wdva", "gate", "app", "--window", "--store", str(short_store_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    code, out, err = finish(process)  # the window "was closed" at once, which ends the app
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert OPEN_LINE.fullmatch(lines[2]) is not None, lines
    about_apps = apps_line(hostconfig.sdk_status())
    assert PAIRING_LINE.fullmatch(lines[3 + len(about_apps)]) is not None, lines
    seen = json.loads(saw.read_text(encoding="utf-8"))
    assert seen["/"][:2] == [200, None]
    session = json.loads(seen["/api/session"][2])
    assert (session["paired"], session["pairing"], session["demo"]) == (False, "code", False)
    assert "session" not in session
    assert seen["/api/session"][1] is None
    assert seen["/api/state"][0] == 401
    assert not short_store_dir.exists()  # and so nobody created a vault here meanwhile


NO_WINDOW = (
    "from wdva.gate.app import main as app\n"
    "app._window_available = lambda: False\n"
    "app._open_browser = lambda url: app._say('a browser was opened at ' + url)\n"
)


def test_without_pywebview_the_window_option_says_what_is_opened_instead(
    short_store_dir: Path,
) -> None:
    about_apps = len(apps_line(hostconfig.sdk_status()))
    # The browser takes the window's place, and the line says so.
    process = start_changed(NO_WINDOW, "--window", "--store", str(short_store_dir))
    try:
        lines = first_lines(process, 6 + about_apps)
    except BaseException:
        process.kill()
        raise
    code, out, err = finish(process, signal.SIGINT)
    opened = OPEN_LINE.fullmatch(lines[2])
    assert opened is not None, lines
    assert lines[-1] == f"a browser was opened at http://127.0.0.1:{opened.group(1)}/"
    assert (code, out) == (0, "")
    assert err == "pywebview is not installed: the interface opens in the browser instead.\n"

    # With --no-browser nothing takes its place, and the line does not say that something does.
    process = start_changed(NO_WINDOW, "--window", "--no-browser", "--store", str(short_store_dir))
    try:
        lines = first_lines(process, 5 + about_apps)
    except BaseException:
        process.kill()
        raise
    code, out, err = finish(process, signal.SIGINT)
    assert lines[-1] == "Ctrl-C locks the vault and quits."
    assert (code, out) == (0, ""), "something was opened although --no-browser was given"
    assert err == (
        "pywebview is not installed: no window is opened, and with --no-browser no browser "
        'page either. The address is on the line that begins with "open:".\n'
    )


SDK_STATUS = (
    "from wdva.gate import hostconfig\n"
    "real = hostconfig.sdk_status\n"
    "hostconfig.sdk_status = lambda: {{**real(), **{change}}}\n"
)
TOO_OLD = '{"python": False, "message": hostconfig.PYTHON_MESSAGE}'
NO_SDK = (
    '{"python": True, "sdk": False, "version": None, '
    '"message": str(hostconfig.missing_sdk_error())}'
)
ALL_THERE = '{"python": True, "sdk": True, "message": None}'


@pytest.mark.parametrize(
    ("change", "code"),
    [(TOO_OLD, "python_too_old"), (NO_SDK, "no_sdk"), (ALL_THERE, None)],
    ids=["an older Python", "no MCP SDK", "all there"],
)
def test_the_start_up_lines_say_when_apps_cannot_connect(
    change: str, code: str | None, short_store_dir: Path
) -> None:
    # What the page says after unlocking, the terminal says at once: the owner need not
    # create or unlock a vault to learn that no app will be able to reach it.
    status = {"python": change != TOO_OLD, "sdk": change == ALL_THERE}
    expected = apps_line(status)
    assert len(expected) == (0 if code is None else 1)
    process = start_changed(
        SDK_STATUS.format(change=change), "--no-browser", "--store", str(short_store_dir)
    )
    try:
        lines = first_lines(process, 5 + len(expected))
        assert lines[0] == f"{PRODUCT} {__version__}"
        assert lines[1] == f"store: {short_store_dir}"
        opened = OPEN_LINE.fullmatch(lines[2])
        assert opened is not None, lines
        # After the address, in the form of its neighbours; nothing at all where apps can connect.
        assert lines[3:-2] == expected
        paired = PAIRING_LINE.fullmatch(lines[-2])
        assert paired is not None, lines
        assert lines[-1] == "Ctrl-C locks the vault and quits."
        assert all("\n" not in line and len(line) < 300 for line in lines)

        # The page's warning is the same sentence, from the same check.
        browser = Browser(int(opened.group(1)))
        browser.ok("POST", "/api/session/pair", {"code": paired.group(1)})
        state = browser.ok("POST", "/api/store/create", {"passphrase": gs.PASSPHRASE})
        warnings = {warning["code"]: warning for warning in state["warnings"]}
        assert sorted(warnings) == ([] if code is None else [code])
        if code is not None:
            said = warnings[code]["text"]
            assert said.startswith("Apps cannot connect")
            assert expected[0].startswith("apps:  " + said[len("Apps ") :])
            assert all(detail in expected[0] for detail in warnings[code]["detail"])
    except BaseException:
        process.kill()
        raise
    code_, out, err = finish(process, signal.SIGINT)
    assert (code_, out, err) == (0, "", "")


def test_the_help_says_that_a_window_asks_for_the_pairing_code() -> None:
    shown = subprocess.run(
        [sys.executable, "-m", "wdva", "gate", "app", "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # The option's own entry: after its last mention, up to the option that follows it.
    window = " ".join(shown.rsplit("--window", 1)[1].split("--demo", 1)[0].split())
    assert "asks for the pairing code like the browser does" in window
    assert "experimental" in window


def test_the_view_option_takes_basic_and_pro_and_nothing_else(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wdva.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["gate", "app"]).view is None
    for view in ("basic", "pro"):
        assert parser.parse_args(["gate", "app", "--view", view]).view == view
    for bad in ("Basic", "PRO", "expert", "both", "", "--demo"):
        with pytest.raises(SystemExit) as stopped:
            parser.parse_args(["gate", "app", "--view", bad])
        assert stopped.value.code == 2
    for short in ("--vi", "--v"):  # no abbreviation either
        with pytest.raises(SystemExit):
            parser.parse_args(["gate", "app", short, "pro"])
    # Only the app has a view: the other two commands do not take the option.
    with pytest.raises(SystemExit):
        parser.parse_args(["gate", "config", "codex", "--client", "codex", "--view", "pro"])
    with pytest.raises(SystemExit):
        parser.parse_args(["gate", "mcp", "--client", "codex", "--view", "pro"])
    capsys.readouterr()
    # Called without the command line, the same two words and no other.
    with pytest.raises(GateError, match="--view"):
        main.run_app(store=None, view="expert")
    shown = subprocess.run(
        [sys.executable, "-m", "wdva", "gate", "app", "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "--view {basic,pro}" in shown
    said = " ".join(shown.rsplit("--view {basic,pro}", 1)[1].split()).lower()
    for word in ("advanced", "simple", "upgrade", "beginner", "expert"):
        assert word not in said  # the two are views of one app, neither is a step up


def interface_of(store: Path) -> Any:
    path = store / "run" / "interface.json"
    return json.loads(path.read_bytes()) if path.exists() else None


def test_the_view_flag_sets_the_view(short_store_dir: Path) -> None:
    def run(*arguments: str, create: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """Start the app on the vault, pair, and return the first state and the last one."""
        process = start_command("--no-browser", "--store", str(short_store_dir), *arguments)
        try:
            lines = start_up_lines(process)
            opened, paired = OPEN_LINE.fullmatch(lines[2]), PAIRING_LINE.fullmatch(lines[3])
            assert opened is not None, lines
            assert paired is not None, lines
            at_start = interface_of(short_store_dir)
            browser = Browser(int(opened.group(1)))
            browser.ok("POST", "/api/session/pair", {"code": paired.group(1)})
            first = browser.ok("GET", "/api/state")
            last = first
            if create:
                last = browser.ok("POST", "/api/store/create", {"passphrase": gs.PASSPHRASE})
            assert first["interface"] == (None if create else {**at_start, "stored": True})
            # The command to start again never carries the view: the file does.
            assert "--view" not in first["launch"]["command"]
        except BaseException:
            process.kill()
            raise
        assert finish(process, signal.SIGINT) == (0, "", "")
        return first, last

    # A new vault is created with the view that was named, and setting up is still to do.
    first, created = run("--view", "pro", create=True)
    assert created["interface"] == {"view": "pro", "setup": "open", "stored": True}
    assert interface_of(short_store_dir) == {"view": "pro", "setup": "open"}
    file = short_store_dir / "run" / "interface.json"
    written = file.stat().st_mtime_ns
    # Without the flag nothing is written at start: the vault opens as it was left.
    first, _ = run()
    assert first["interface"] == {"view": "pro", "setup": "open", "stored": True}
    assert file.stat().st_mtime_ns == written
    # An existing vault opens in the view that is named, the choice is stored, and whether
    # setting up was finished stays as it was.
    first, _ = run("--view", "basic")
    assert first["interface"] == {"view": "basic", "setup": "open", "stored": True}
    assert interface_of(short_store_dir) == {"view": "basic", "setup": "open"}
    assert file.stat().st_mtime_ns != written


def test_a_start_without_the_view_flag_writes_nothing_for_a_vault_from_before(
    short_store_dir: Path,
) -> None:
    gs.make_store(short_store_dir).close()  # made by the engine alone, as vaults were
    process = start_command("--no-browser", "--store", str(short_store_dir))
    try:
        lines = start_up_lines(process)
        opened, paired = OPEN_LINE.fullmatch(lines[2]), PAIRING_LINE.fullmatch(lines[3])
        assert opened is not None, lines
        assert paired is not None, lines
        browser = Browser(int(opened.group(1)))
        browser.ok("POST", "/api/session/pair", {"code": paired.group(1)})
        state = browser.ok("POST", "/api/unlock", {"passphrase": gs.PASSPHRASE})
        assert state["interface"] == {"view": "pro", "setup": "done", "stored": False}
        assert interface_of(short_store_dir) is None
    except BaseException:
        process.kill()
        raise
    assert finish(process, signal.SIGINT) == (0, "", "")
    assert interface_of(short_store_dir) is None


def test_a_demo_that_stops_says_how_to_start_for_real(tmp_path: Path) -> None:
    own_place = tmp_path / "a vault of my own"
    process = start_command(
        "--demo", "--no-browser", "--view", "pro", "--lock-after", "2", "--store", str(own_place)
    )
    try:
        lines = start_up_lines(process)
        assert len(lines) == 5  # the start-up lines stay five
        opened = OPEN_LINE.fullmatch(lines[2])
        assert opened is not None, lines
        browser = Browser(int(opened.group(1)))
        browser.ok("GET", "/api/session")
        state = browser.ok("GET", "/api/state")
        store = Path(state["store"]["path"])
        # The view that was named, and nothing to set up in a demo.
        assert state["interface"] == {"view": "pro", "setup": "done", "stored": True}
        # The command is for a vault of one's own: the place the person named, the flags of
        # this run, and neither the demo nor the view nor the demo's temporary folder.
        flags = ["--store", str(own_place), "--lock-after", "2", "--no-browser"]
        assert launch_words(state) == ["-m", "wdva", "gate", "app", *flags]
        assert state["launch"]["port_fixed"] is False
        command = state["launch"]["command"]
        assert str(store) not in command
        # The played app can be told to ask again: the address is there in a demo.
        assert browser.ok("POST", "/api/demo/again", {}) == {}
    except BaseException:
        process.kill()
        raise
    code, out, err = finish(process, signal.SIGINT)
    assert (code, err) == (0, "")
    printed = out.splitlines()
    assert len(printed) == 1, "a demo that stops prints one line more, and only one"
    assert printed[-1].startswith("To start with your own documents: ")
    assert printed[-1] == main.OWN_VAULT_LINE + command
    said = shlex.split(printed[-1].removeprefix(main.OWN_VAULT_LINE))
    assert "--demo" not in said
    assert "--view" not in said
    assert not store.exists()
    assert not own_place.exists(), "the demo made something at the place for a vault of one's own"


def test_a_demo_whose_terminal_has_gone_still_ends_cleanly() -> None:
    # The last line has nobody to read it when the output was closed: that is no error.
    process = start_command("--demo", "--no-browser")
    try:
        assert len(start_up_lines(process)) == 5
        assert process.stdout is not None
        process.stdout.close()
        process.send_signal(signal.SIGINT)
        assert process.stderr is not None
        watchdog = threading.Timer(60, process.kill)
        watchdog.start()
        try:
            err = process.stderr.read()
            code = process.wait()
        finally:
            watchdog.cancel()
    except BaseException:
        process.kill()
        raise
    assert (code, err) == (0, "")


def test_the_command_says_when_the_port_is_taken(short_store_dir: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        process = start_command(
            "--no-browser", "--store", str(short_store_dir), "--port", str(port)
        )
        code, out, err = finish(process)
    assert code == 1
    assert out == ""
    assert f"port {port}" in err
    assert not short_store_dir.exists()


def test_run_app_checks_its_arguments_before_it_does_anything() -> None:
    with pytest.raises(GateError, match="--lock-after"):
        main.run_app(store=None, lock_after_hours=0)
    with pytest.raises(GateError, match="--lock-after"):
        main.run_app(store=None, lock_after_hours=1000)
    with pytest.raises(GateError, match="--port"):
        main.run_app(store=None, port=70000)


def test_the_demo_directory_is_private_and_fits_a_socket() -> None:
    directory = main._demo_directory()
    try:
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o077 == 0
        assert list(directory.iterdir()) == []
        assert len(os.fsencode(directory / "run" / "gate.sock")) <= 100
        assert main._DEMO_NAME.fullmatch(directory.name), "the next demo would not know it again"
    finally:
        directory.rmdir()


def test_a_demo_directory_that_is_too_long_for_a_socket_moves_to_tmp_under_the_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = tmp_path / ("d" * 90)
    deep.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(deep))
    directory = main._demo_directory()
    try:
        assert directory.parent == Path("/tmp")
        assert main._DEMO_NAME.fullmatch(directory.name)
        assert list(deep.iterdir()) == []
    finally:
        directory.rmdir()


def test_what_a_killed_demo_left_is_removed_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = main._DEMO_PREFIX
    long_ago = time.time() - 3600
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not a demo's", encoding="utf-8")
    place = tmp_path / "tmp"
    place.mkdir()

    def left(name: str, *, age: float = 3600) -> Path:
        """A directory as a demo's store looks, last changed ``age`` seconds ago."""
        made = place / name
        (made / "run").mkdir(parents=True)
        (made / "keyslot.wdva").write_bytes(b"sealed")
        (made / "run" / "lock").write_bytes(b"")
        os.utime(made, (time.time() - age, time.time() - age))
        return made

    dead = left(f"{prefix}abcd1234")
    just_made = left(f"{prefix}efgh5678", age=5)  # a demo may be making it right now
    running = left(f"{prefix}ijkl9_12")
    alive = main._hold(running)  # what a demo holds for as long as it lives
    assert alive is not None
    others = [
        left("demo-abcd1234"),  # not this program's name
        left(f"{prefix}abcd1234-kept"),  # nor the form of name it makes
        # Capitals are not what mkdtemp makes. Other letters than the dead one's: a file
        # system that ignores case (the default on macOS) would take them for one directory.
        left(f"{prefix}MNOP1234"),
        left(f"{prefix}abcd123"),
        left(f"x{prefix}abcd1234"),
    ]
    a_file = place / f"{prefix}file0000"
    a_file.write_text("a file", encoding="utf-8")
    os.utime(a_file, (long_ago, long_ago))
    a_link = place / f"{prefix}link0000"
    a_link.symlink_to(outside, target_is_directory=True)
    os.utime(a_link, (long_ago, long_ago), follow_symlinks=False)
    everything = {entry.name for entry in place.iterdir()}
    try:
        # Somebody else's directory is left alone, whatever it is called.
        mine = os.getuid()
        monkeypatch.setattr(main.os, "getuid", lambda: mine + 1)
        main._remove_dead_demos(place)
        assert {entry.name for entry in place.iterdir()} == everything
        monkeypatch.undo()

        main._remove_dead_demos(place)
        assert {entry.name for entry in place.iterdir()} == everything - {dead.name}
        assert (running / "keyslot.wdva").is_file()
        assert (just_made / "keyslot.wdva").is_file()
        assert all((other / "keyslot.wdva").is_file() for other in others)
        assert (outside / "keep.txt").is_file()
    finally:
        os.close(alive)
    # The demo that ran is dead now: the next start removes what it left as well.
    main._remove_dead_demos(place)
    assert {entry.name for entry in place.iterdir()} == everything - {dead.name, running.name}
    # A place that does not exist, or is not a directory, is no error.
    main._remove_dead_demos(place / "nowhere")
    main._remove_dead_demos(a_file)


def test_a_killed_demo_is_tidied_up_by_the_next_one_and_a_running_one_is_not() -> None:
    def start() -> tuple[subprocess.Popen[str], Browser, Path]:
        process = start_command("--demo", "--no-browser", "--port", "0")
        try:
            opened = OPEN_LINE.fullmatch(first_lines(process, 5)[2])
            assert opened is not None
            browser = Browser(int(opened.group(1)))
            browser.ok("GET", "/api/session")
            return process, browser, Path(browser.ok("GET", "/api/state")["store"]["path"])
        except BaseException:
            process.kill()
            raise

    def long_ago(store: Path) -> None:
        os.utime(store, (time.time() - 3600, time.time() - 3600))

    first, browser, store = start()
    started = [first]
    try:
        # Its user locks the vault: the store's own lock is free now, the demo still runs.
        assert browser.ok("POST", "/api/lock", {})["locked"] is True
        long_ago(store)
        second, _, second_store = start()
        started.append(second)
        assert (store / "keyslot.wdva").is_file(), "a demo removed the store of one that runs"
        assert second_store != store
        first.kill()  # no chance to tidy up
        first.wait(timeout=30)
        assert (finish(second, signal.SIGTERM)[0], second_store.exists()) == (0, False)
        assert store.is_dir()  # what the killed demo left
        long_ago(store)
        third, _, third_store = start()
        started.append(third)
        assert not store.exists()
        assert (finish(third, signal.SIGINT)[0], third_store.exists()) == (0, False)
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
                process.communicate()
        shutil.rmtree(store, ignore_errors=True)
