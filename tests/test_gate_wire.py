"""The real gate behind the real link: a real store, a listening socket, a caller without a key.

The other test modules take the parts one at a time: the gate is called in
process, the link is given a stand-in for the gate, the server for agent
applications a stand-in for both. Here nothing is replaced. A store with
documents is unlocked, a :class:`~wdva.gate.link.LinkServer` listens on its
socket, and calls are made the way an agent application's program makes them,
through :class:`~wdva.gate.link.RemoteGate` and, in the last test, through the
real ``wdva gate mcp`` command as a child process.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from wdva.gate import messages, paths, textproc
from wdva.gate.core import UNREGISTERED, Gate
from wdva.gate.errors import KeeperUnavailableError
from wdva.gate.link import LinkServer, RemoteGate
from wdva.gate.types import Caller, Reply

from . import gate_support as support
from .conftest import FakeClock

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

CLIENT = "claude-desktop"
ME = Caller(CLIENT, "claude-ai", "1.2", "2025-11-25", "stdio")
QUERY = "residential lease agreement landlord"

LOCKED = Reply("LOCKED", messages.locked(), False)
NOT_ALLOWED = Reply("NOT_ALLOWED", messages.not_allowed(), False)
NOT_FOUND = Reply("NOT_FOUND", messages.not_found(), False)
BAD_REQUEST = Reply("BAD_REQUEST", messages.bad_request(), False)

REF = re.compile(r"ref=(d-[0-9a-f]{12}\.[0-9]{1,5}) ")
LAST_LINE = re.compile(r"Release ([a-z0-9_-]+) #(\d+)\. Left today: ([\d,]+) characters\.")

# The names agents know the three tools by, in the order status, search, read.
AGENT_TOOLS: tuple[str, ...] = tuple(getattr(messages, "AGENT_TOOL_NAMES", messages.TOOL_NAMES))


class Wire:
    """An unlocked store with the sample documents, its gate, and a listener on its socket."""

    def __init__(self, path: Path, clock: FakeClock) -> None:
        self.path = path
        self.clock = clock
        self.records: list[Mapping[str, Any]] = []
        self.store = support.make_store(path, clock=clock)
        self.gate = Gate(self.store, served=[CLIENT], clock=clock, on_record=self.records.append)
        self.server = LinkServer(
            self.gate, paths.socket_path(path), info=lambda: {"head": self.gate.head()}
        )
        self.server.start()
        self.remote = RemoteGate(path, CLIENT)

    def ref(self, title: str, passage: int = 1) -> str:
        return f"{support.document_id(self.store, title)}.{passage}"

    def of_kind(self, kind: str) -> list[Mapping[str, Any]]:
        return [record for record in self.records if record["kind"] == kind]

    def shut(self) -> None:
        self.server.stop()
        try:
            self.gate.close()
        finally:
            self.store.close()


@pytest.fixture
def wire(short_store_dir: Path, gate_clock: FakeClock) -> Iterator[Wire]:
    made = Wire(short_store_dir, gate_clock)
    try:
        yield made
    finally:
        made.shut()


def release_number(reply: Reply) -> int:
    match = LAST_LINE.fullmatch(reply.text.splitlines()[-1])
    assert match is not None
    return int(match.group(2))


def quoted(reply: Reply) -> str:
    """The document text of a read result, without the prefix of its lines."""
    rows = reply.text.splitlines()[2:-2]
    assert rows
    assert all(row.startswith("| ") for row in rows)
    return "\n".join(row[2:] for row in rows)


def test_status_search_and_read_through_the_socket(wire: Wire) -> None:
    gate, remote = wire.gate, wire.remote
    assert remote.ping() == {"v": 1, "code": "OK", "head": gate.head()}

    status = remote.status(ME)
    assert status == Reply("OK", status.text, False)
    assert f'"{CLIENT}"' in status.text.splitlines()[0]
    assert status == gate.status(ME)  # to the character what the gate itself says
    assert wire.of_kind("release") == []

    # A search that finds something: the text that arrives is the text that was recorded.
    found = remote.search(ME, QUERY, "home", 3)
    assert (found.code, found.released) == ("OK", True)
    assert (found.seq, found.client_seq) == (None, None)  # record numbers do not travel
    record = wire.records[-1]
    assert (record["kind"], record["tool"]) == ("release", messages.TOOL_SEARCH)
    assert record["text"] == found.text
    assert record["query"] == QUERY
    assert (record["client"], record["transport"]) == (CLIENT, "stdio")
    assert record["claimed"] == {"name": "claude-ai", "version": "1.2", "protocol": "2025-11-25"}
    refs = REF.findall(found.text)
    lease = wire.ref("lease-2025")
    assert lease in refs
    assert 1 <= len(refs) <= 3

    # A read of a passage it found, with the owner's listed strings replaced.
    read = remote.read(ME, lease)
    assert (read.code, read.released) == ("OK", True)
    assert wire.records[-1]["text"] == read.text
    assert wire.records[-1]["rule"] == "grant:read"
    assert support.LEASE_MARKER in quoted(read)
    assert textproc.REDACTED in quoted(read)
    assert not textproc.find_listed(read.text, [support.LISTED_NAME, support.LISTED_NUMBER])
    assert [release_number(found), release_number(read)] == [1, 2]

    # A ref as an agent may hand it back: blanks, quotes, upper case, "ref=".
    sloppy = remote.read(ME, f'  "REF={lease.upper()}" ')
    assert sloppy.code == "OK"
    assert quoted(sloppy) == quoted(read)
    assert wire.records[-1]["args"] == {"ref": lease}

    # What the owner's meters say is what went over the socket.
    usage = gate.usage().clients[CLIENT]
    assert usage.chars_day == len(found.text) + len(read.text) + len(sloppy.text)
    assert (usage.release_count, usage.claimed_name) == (3, "claude-ai")
    assert len(wire.of_kind("release")) == 3


def test_bad_arguments_get_the_fixed_answers_of_the_gate(wire: Wire) -> None:
    gate, remote, clock = wire.gate, wire.remote, wire.clock
    lease = wire.ref("lease-2025")

    # Each value gets over the socket what the gate answers when it is called directly.
    searches: list[tuple[tuple[Any, ...], Reply]] = [
        ((None,), BAD_REQUEST),
        ((7,), BAD_REQUEST),
        ((["rent"],), BAD_REQUEST),
        (({"query": "rent"},), BAD_REQUEST),
        (("rent", "no-such-collection"), NOT_FOUND),
        (("rent", "medical"), NOT_FOUND),  # it exists, but not for this client
        (("rent", "Home"), NOT_FOUND),
        (("rent", ["home"]), NOT_FOUND),
        (("rent", "c" * 5000), NOT_FOUND),
    ]
    for arguments, expected in searches:
        clock.advance(seconds=5)
        assert remote.search(ME, *arguments) == expected
        assert gate.search(ME, *arguments) == expected
    reads: list[tuple[Any, Reply]] = [
        (None, BAD_REQUEST),
        (7, BAD_REQUEST),
        ([lease], BAD_REQUEST),
        ("", NOT_FOUND),
        ("no ref at all", NOT_FOUND),
        ("d-ffffffffffff.1", NOT_FOUND),
        (f"{lease.rpartition('.')[0]}.999", NOT_FOUND),
        (wire.ref("referral-2026"), NOT_FOUND),  # a document of a collection it is not granted
        ("x" * 5000, NOT_FOUND),
    ]
    for ref, expected in reads:
        clock.advance(seconds=5)
        assert remote.read(ME, ref) == expected
        assert gate.read(ME, ref) == expected
    # What cannot be sent at all is refused before anything is sent: the gate sees no call.
    refused = gate.usage().refusals_day
    assert refused == 2 * (len(searches) + len(reads))
    assert remote.read(ME, "\ud800") == BAD_REQUEST
    assert remote.search(ME, float("nan")) == BAD_REQUEST
    assert remote.search(ME, object()) == BAD_REQUEST
    assert gate.usage().refusals_day == refused
    assert wire.of_kind("release") == []

    # A number of results that is no number is not an error: the search is answered.
    clock.advance(minutes=2)
    for limit in ("many", None, 10**30, -4, 2.5):
        clock.advance(seconds=5)
        assert remote.search(ME, "rent", "home", limit).code == "OK"
    assert len(wire.of_kind("release")) == 5
    # A link made for one name does not call under another.
    with pytest.raises(ValueError, match="client name"):
        remote.status(Caller("lm-studio"))


def test_a_name_that_is_not_served_gets_one_answer(wire: Wire) -> None:
    gate = wire.gate
    lease = wire.ref("lease-2025")
    # lm-studio is registered but this session does not serve it; the others never were.
    for name in ("lm-studio", "ghost-app", "Not A Name"):
        remote = RemoteGate(wire.path, name)
        caller = Caller(name, "some-program", "1", "2025-11-25", "stdio")
        assert remote.status(caller) == NOT_ALLOWED
        assert remote.search(caller, "rent") == NOT_ALLOWED
        assert remote.read(caller, lease) == NOT_ALLOWED
    assert wire.of_kind("release") == []
    (denial,) = wire.of_kind("denial")  # one record for all nine calls, under no name
    assert (denial["client"], denial["code"], denial["claimed"]) == (
        UNREGISTERED,
        "NOT_ALLOWED",
        None,
    )
    assert gate.usage().refusals_day == 9
    # Served names are not affected by that, and the owner's choice holds at once.
    assert wire.remote.status(ME).code == "OK"
    gate.set_served([CLIENT, "lm-studio"])
    studio = RemoteGate(wire.path, "lm-studio")
    assert studio.status(Caller("lm-studio")).code == "OK"
    assert studio.read(Caller("lm-studio"), lease).code == "OK"
    gate.set_served(["lm-studio"])
    assert wire.remote.read(ME, lease) == NOT_ALLOWED


def test_limits_and_approvals_hold_through_the_socket(wire: Wire) -> None:
    gate, remote = wire.gate, wire.remote
    note = wire.ref("tax-note-2025")
    blocked = remote.read(ME, note)
    assert blocked == Reply("NEEDS_APPROVAL", messages.needs_approval(note, CLIENT), False)
    assert [(entry.client, entry.ref) for entry in gate.pending()] == [(CLIENT, note)]
    assert support.TAX_MARKER not in blocked.text

    # Nothing sent over the socket approves anything; the owner does, in the app.
    gate.approve(CLIENT, note, gate.preview(CLIENT, note).body_sha256)
    released = remote.read(ME, note)
    assert released.code == "OK"
    assert support.TAX_MARKER in quoted(released)
    assert wire.records[-1]["rule"] == "approval"
    assert gate.pending() == []
    assert remote.read(ME, note).code == "NEEDS_APPROVAL"  # the approval was for one release

    # A limit the owner lowers holds for the next call that arrives.
    gate.set_policy(gate.policy().with_client_limits(CLIENT, {"chars_per_day": 1200}))
    refused = remote.search(ME, "rent deposit landlord")
    assert refused == Reply("OVER_LIMIT", messages.over_limit("client-day"), False)
    assert len(wire.of_kind("release")) == 1


def test_calls_at_the_same_time_each_get_their_own_release(wire: Wire) -> None:
    replies: list[Reply] = []
    failures: list[BaseException] = []

    def call(word: str) -> None:
        try:
            replies.append(RemoteGate(wire.path, CLIENT).search(ME, word, "home", 2))
        except BaseException as exc:  # a thread must not die silently
            failures.append(exc)

    words = ["rent", "deposit", "landlord", "tenant", "notice", "repairs", "zzzabsent", "account"]
    threads = [threading.Thread(target=call, args=(word,)) for word in words]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert failures == []
    assert [reply.code for reply in replies] == ["OK"] * len(words)
    assert sorted(release_number(reply) for reply in replies) == list(range(1, len(words) + 1))
    recorded = sorted(record["text"] for record in wire.of_kind("release"))
    assert recorded == sorted(reply.text for reply in replies)


def test_locked_after_the_gate_is_closed(wire: Wire) -> None:
    gate, remote = wire.gate, wire.remote
    lease = wire.ref("lease-2025")
    assert remote.read(ME, lease).code == "OK"
    releases = len(wire.of_kind("release"))

    gate.close("lock-command")
    # The app still listens; the gate behind it says LOCKED to everybody and releases nothing.
    assert remote.status(ME) == LOCKED
    assert remote.search(ME, "rent") == LOCKED
    assert remote.read(ME, lease) == LOCKED
    assert RemoteGate(wire.path, "ghost-app").status(Caller("ghost-app")) == LOCKED
    assert (wire.records[-1]["kind"], wire.records[-1]["reason"]) == ("lock", "lock-command")
    assert len(wire.of_kind("release")) == releases
    assert support.LEASE_MARKER not in LOCKED.text


def test_without_a_listener_the_vault_is_not_open_for_a_caller(wire: Wire) -> None:
    gate, remote = wire.gate, wire.remote
    lease = wire.ref("lease-2025")
    socket_file = paths.socket_path(wire.path)
    assert remote.status(ME).code == "OK"

    def not_open() -> None:
        # The three calls never raise: they say LOCKED, and what the user has to do.
        assert remote.status(ME) == LOCKED
        assert remote.search(ME, "rent") == LOCKED
        assert remote.read(ME, lease) == LOCKED
        with pytest.raises(KeeperUnavailableError):
            remote.ping()

    wire.server.stop()
    assert not os.path.lexists(socket_file)
    not_open()
    # The socket file of an app that was killed: it is there, and nobody listens on it.
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        leftover.bind(str(socket_file))
    finally:
        leftover.close()
    assert os.path.lexists(socket_file)
    not_open()
    assert wire.of_kind("release") == []
    assert wire.of_kind("denial") == []  # none of these calls reached the gate

    # A directory that holds no vault is another matter, and is said differently.
    nowhere = RemoteGate(wire.path.parent / "no-vault-here", CLIENT)
    answer = Reply("UNAVAILABLE", messages.store_not_found(CLIENT), False)
    assert nowhere.status(ME) == answer
    assert nowhere.read(ME, lease) == answer
    with pytest.raises(KeeperUnavailableError):
        nowhere.ping()

    # The gate itself was open all the time; with a listener it is reachable again.
    assert gate.status(ME).code == "OK"
    with LinkServer(gate, socket_file):
        assert remote.read(ME, lease).code == "OK"
    assert not os.path.lexists(socket_file)
    not_open()


class Shim:
    """The ``wdva gate mcp`` command as a child process, spoken to in plain JSON-RPC lines."""

    def __init__(self, argv: list[str], errors: Path) -> None:
        self._errors_path = errors
        self._errors = errors.open("w", encoding="utf-8")
        environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._errors,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        self._next_id = 0
        self._watchdog = threading.Timer(90, self.proc.kill)
        self._watchdog.start()

    def send(self, method: str, params: dict[str, Any] | None = None, *, reply: bool = True) -> Any:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if reply:
            self._next_id += 1
            message["id"] = self._next_id
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        if not reply:
            return None
        line = self.proc.stdout.readline()
        assert line, f"the command ended early: {self.errors()}"
        answer = json.loads(line)
        assert answer["id"] == message["id"]
        return answer["result"]

    def text(self, tool: str, arguments: dict[str, Any]) -> str:
        """The one block of text a tool call returns."""
        result = self.send("tools/call", {"name": tool, "arguments": arguments})
        assert not result.get("isError")
        (block,) = result["content"]
        assert block["type"] == "text"
        return str(block["text"])

    def errors(self) -> str:
        self._errors.flush()
        return self._errors_path.read_text(encoding="utf-8")

    def close(self) -> tuple[str, int]:
        """Close its input; return what else it wrote to standard output, and its exit status."""
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        try:
            self.proc.stdin.close()
            rest = self.proc.stdout.read()
            status = self.proc.wait(timeout=30)
        finally:
            self._watchdog.cancel()
            self.proc.kill()
            self.proc.stdout.close()
            self._errors.close()
        return rest, status


@pytest.mark.mcp
def test_the_real_command_relays_the_real_gate(wire: Wire, tmp_path: Path) -> None:
    from wdva.gate.hostconfig import host_command

    status_tool, search_tool, read_tool = AGENT_TOOLS
    remote = wire.remote
    direct = remote.search(ME, QUERY, "home", 3)
    direct_status = remote.status(ME)  # after the search: it says how much is left now
    assert (direct.code, direct_status.code) == ("OK", "OK")

    shim = Shim(host_command(CLIENT, wire.path), tmp_path / "stderr.txt")
    try:
        shim.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "wire-test-host", "version": "7"},
            },
        )
        shim.send("notifications/initialized", reply=False)
        listed = shim.send("tools/list")
        status_text = shim.text(status_tool, {})
        search_text = shim.text(search_tool, {"query": QUERY, "collection": "home", "limit": 3})
        lease = wire.ref("lease-2025")
        read_text = shim.text(read_tool, {"ref": f" REF={lease.upper()} "})
        errors = shim.errors()
    finally:
        rest, exit_status = shim.close()

    # Three tools, and no other.
    assert tuple(tool["name"] for tool in listed["tools"]) == AGENT_TOOLS
    assert len(listed["tools"]) == 3
    # Status carries no counter: it is to the character what RemoteGate returned.
    assert status_text == direct_status.text
    # The search is the same search. Only its last line differs, which counts the releases.
    assert search_text.rsplit("\n", 1)[0] == direct.text.rsplit("\n", 1)[0]
    assert REF.findall(search_text) == REF.findall(direct.text)
    searches = [record for record in wire.of_kind("release") if record.get("query") == QUERY]
    assert [record["text"] for record in searches] == [direct.text, search_text]
    relayed = searches[1]
    assert relayed["client_seq"] == searches[0]["client_seq"] + 1
    assert (relayed["client"], relayed["transport"]) == (CLIENT, "stdio")
    assert relayed["claimed"] == {
        "name": "wire-test-host",
        "version": "7",
        "protocol": "2025-06-18",
    }
    assert relayed["args"] == {"query": QUERY, "collection": "home", "limit": 3}
    # And the passage it read is the one the gate recorded for it.
    assert wire.records[-1]["text"] == read_text
    assert support.LEASE_MARKER in read_text
    again = remote.search(ME, QUERY, "home", 3)
    assert again.text.rsplit("\n", 1)[0] == search_text.rsplit("\n", 1)[0]
    # The command wrote nothing but its answers, and no document text to standard error.
    assert (rest, exit_status) == ("", 0)
    assert support.LEASE_MARKER not in errors
    assert "Traceback" not in errors
