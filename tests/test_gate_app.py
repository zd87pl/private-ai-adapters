"""The whole app: the interface over real HTTP, agents over the real socket, and the demo."""

from __future__ import annotations

import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from wdva._version import __version__
from wdva.gate import brand, messages
from wdva.gate.app import demo, main
from wdva.gate.app.api import ServiceError
from wdva.gate.app.demo import DemoAgent, make_demo_store
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
    """What a browser does with the interface: requests with a cookie, answers as JSON."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie: str | None = None
        self.bodies: list[str] = []

    def call(
        self, method: str, path: str, body: Any = None, *, upload: bytes | None = None
    ) -> tuple[int, Any]:
        headers = {}
        if self.cookie is not None:
            headers["Cookie"] = self.cookie
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
        given = response.getheader("Set-Cookie")
        if given:
            self.cookie = given.split(";")[0]
        if response.getheader("Content-Type", "").startswith("application/json"):
            self.bodies.append(data.decode())
            return response.status, json.loads(data)
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
    assert apps["chatgpt-desktop"]["limits"]["chars_per_day"] == 4_000
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
    assert 2_000 < by_app["chatgpt-desktop"] <= 4_000

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


# -- the command -----------------------------------------------------------------------------


def start_command(*arguments: str) -> subprocess.Popen[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("WDVA_")}
    return subprocess.Popen(
        [sys.executable, "-m", "wdva", "gate", "app", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )


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
        lines = first_lines(process, 5)
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

        # No pairing in the demo: the page itself gives the session.
        browser = Browser(int(opened.group(1)))
        status, page = browser.call("GET", "/")
        assert status == 200
        assert b"<title>" in page
        assert browser.cookie is not None
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
    except BaseException:
        process.kill()
        raise
    code, out, err = finish(process, signal.SIGTERM)
    assert (code, out, err) == (0, "", "")
    assert not store.exists()


def test_the_command_prints_the_pairing_code_and_ctrl_c_locks_the_vault(
    short_store_dir: Path,
) -> None:
    process = start_command("--no-browser", "--store", str(short_store_dir), "--lock-after", "1")
    try:
        lines = first_lines(process, 5)
        assert lines[0] == f"{PRODUCT} {__version__}"
        assert lines[1] == f"store: {short_store_dir}"
        opened = OPEN_LINE.fullmatch(lines[2])
        paired = PAIRING_LINE.fullmatch(lines[3])
        assert opened is not None, lines
        assert paired is not None, lines
        assert lines[4] == "Ctrl-C locks the vault and quits."

        browser = Browser(int(opened.group(1)))
        status, _ = browser.call("GET", "/")
        assert (status, browser.cookie) == (200, None)  # the page alone gives no session
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
    finally:
        directory.rmdir()
