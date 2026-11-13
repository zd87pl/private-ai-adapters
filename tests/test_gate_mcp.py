"""The MCP server that agent applications start: its three tools, as a client sees them.

Most tests connect the SDK's own client to the server inside the test process,
once for each generation of the protocol. The last ones start the real command
as a child process, the way an application does.

Everything here needs the MCP SDK and is skipped without it. The SDK is imported
inside the tests, so that this module can be collected where it is missing.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from wdva import __version__
from wdva.gate import brand, messages
from wdva.gate.errors import GateError
from wdva.gate.types import Caller, Reply

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="the gate does not run on Windows yet"),
    pytest.mark.mcp,
]

# The product's name comes from one place; the tests take it from there too.
PRODUCT, SLUG = brand.PRODUCT, brand.SLUG
STATUS, SEARCH, READ = messages.AGENT_TOOL_NAMES
EXPORT = brand.tool_name("export")  # a name in the same form that is not a tool

CLIENT = "claude-desktop"
MARKER = "zx81-marker-text-of-a-document"
NAMES = (STATUS, SEARCH, READ)
PROTOCOL = {"auto": "2026-07-28", "legacy": "2025-11-25"}
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

# The complete tool list as mcp 2.3.0 with pydantic 2.14.3 puts it on the wire.
SNAPSHOT: list[dict[str, Any]] = [
    {
        "name": STATUS,
        "title": "Document vault status",
        "description": (
            "Show whether the owner's document vault is unlocked, which collections this client "
            "may use and how, and how many characters may still be released to it now. Returns "
            "no document text. Call it first, or when another tool answers LOCKED or NOT_ALLOWED."
        ),
        "inputSchema": {"properties": {}, "title": f"{STATUS}Arguments", "type": "object"},
        "annotations": READ_ONLY,
    },
    {
        "name": SEARCH,
        "title": "Search the document vault",
        "description": (
            "Keyword search over the documents the owner keeps in an encrypted vault on this "
            "computer. Matches whole words, ignoring case and accents; there is no stemming, no "
            "prefix matching and no synonym matching, so put alternative wordings and word forms "
            "in one query; numbers are searchable only in collections the client may read "
            "without approval. Returns up to `limit` refs, best match first, each with a short "
            "quoted snippet where the owner's rules allow it. It never returns whole documents. "
            f"Every search counts against the release limits. Pass a ref to {READ} to get the "
            "passage."
        ),
        "inputSchema": {
            "properties": {
                "query": {
                    "description": "Words to look for.",
                    "maxLength": 500,
                    "title": "Query",
                    "type": "string",
                },
                "collection": {
                    "default": "",
                    "description": (
                        f"Limit the search to one collection name from {STATUS}. "
                        "Empty: every collection this client may use."
                    ),
                    "maxLength": 32,
                    "title": "Collection",
                    "type": "string",
                },
                "limit": {
                    "default": 5,
                    "description": "Maximum number of matches, 1 to 10. Other values are clamped.",
                    "title": "Limit",
                    "type": "integer",
                },
            },
            "required": ["query"],
            "title": f"{SEARCH}Arguments",
            "type": "object",
        },
        "annotations": READ_ONLY,
    },
    {
        "name": READ,
        "title": "Read one passage",
        "description": (
            "Return one passage (at most about 800 characters) of a document in the owner's "
            "vault, by ref. The text is handed to the assistant that called, which for a hosted "
            "assistant means its provider, and cannot be recalled; so the owner's rules may "
            "require the owner's approval first, or refuse. The result then says what to do. The "
            "passages before and after have the next lower and higher number in the ref. Whole "
            "documents are not available."
        ),
        "inputSchema": {
            "properties": {
                "ref": {
                    "description": (
                        f"A ref exactly as returned by {SEARCH}, for example d-3f9a2c41b07e.7."
                    ),
                    "maxLength": 64,
                    "title": "Ref",
                    "type": "string",
                }
            },
            "required": ["ref"],
            "title": f"{READ}Arguments",
            "type": "object",
        },
        "annotations": READ_ONLY,
    },
]


class FakeBackend:
    """Records every call and answers with a text that names the operation."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: BaseException | None = None
        self.answer: Any = None

    def _reply(self, op: str, released: bool) -> Any:
        if self.error is not None:
            raise self.error
        if self.answer is not None:
            return self.answer
        return Reply("OK", f"OK: {op} answered.\n| a quoted line é\n", released)

    def status(self, caller: Caller) -> Reply:
        self.calls.append(("status", caller))
        return self._reply("status", False)

    def search(self, caller: Caller, query: Any, collection: Any = "", limit: Any = 5) -> Reply:
        self.calls.append(("search", caller, query, collection, limit))
        return self._reply("search", True)

    def read(self, caller: Caller, ref: Any) -> Reply:
        self.calls.append(("read", caller, ref))
        return self._reply("read", True)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(params=["auto", "legacy"])
def mode(request: pytest.FixtureRequest) -> str:
    """How the SDK's client connects: the newest protocol, or the one before it."""
    return str(request.param)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


def connect(
    backend: Any, mode: str, *, client: str = CLIENT, name: str = "it-host", version: str = "1.2"
) -> Any:
    """The SDK's client, connected in memory to a server built around ``backend``."""
    from mcp import Client
    from mcp.types import Implementation

    from wdva.gate.mcp_server import build_server

    return Client(
        build_server(backend, client),
        mode=mode,
        client_info=Implementation(name=name, version=version),
    )


def wire(tools: Any) -> list[dict[str, Any]]:
    return [tool.model_dump(by_alias=True, mode="json", exclude_none=True) for tool in tools]


def said(mode: str, **fields: Any) -> Caller:
    """The caller a backend sees for the test client."""
    values = {
        "client": CLIENT,
        "claimed_name": "it-host",
        "claimed_version": "1.2",
        "protocol": PROTOCOL[mode],
        "transport": "stdio",
    }
    return Caller(**{**values, **fields})


def only_text(result: Any) -> str:
    """The text of a result, after checking that it is one text block and nothing else."""
    from mcp.types import TextContent

    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    assert result.structured_content is None
    return str(result.content[0].text)


# -- what the server announces --------------------------------------------------


@pytest.mark.anyio
async def test_tool_surface_is_pinned(backend: FakeBackend, mode: str) -> None:
    async with connect(backend, mode) as client:
        assert client.protocol_version == PROTOCOL[mode]
        assert (client.server_info.name, client.server_info.version) == (SLUG, __version__)
        instructions = client.instructions
        tools = wire((await client.list_tools()).tools)

    assert instructions == messages.SERVER_INSTRUCTIONS
    assert instructions.startswith(f"{SLUG} gives read-only, limited, recorded access")
    assert len(instructions) <= 512
    assert tuple(tool["name"] for tool in tools) == NAMES
    assert [tool["title"] for tool in tools] == [
        "Document vault status",
        "Search the document vault",
        "Read one passage",
    ]
    assert len(tools) == len(SNAPSHOT)
    for tool, expected in zip(tools, SNAPSHOT, strict=True):
        assert tool["description"] == expected["description"]
        assert tool["annotations"] == READ_ONLY
        assert "outputSchema" not in tool
        # The descriptions speak of the vault and send nobody to a command.
        assert "vault" in tool["description"]
        assert "wdva gate" not in tool["description"]
        assert "store" not in tool["description"]

    status, search, read = (tool["inputSchema"] for tool in tools)
    assert status["properties"] == {}
    assert "required" not in status
    assert search["required"] == ["query"]
    assert set(search["properties"]) == {"query", "collection", "limit"}
    query, collection, limit = (
        search["properties"][key] for key in ("query", "collection", "limit")
    )
    assert (query["type"], query["maxLength"], "default" in query) == ("string", 500, False)
    assert (collection["type"], collection["maxLength"], collection["default"]) == (
        "string",
        32,
        "",
    )
    assert (limit["type"], limit["default"], "maxLength" in limit) == ("integer", 5, False)
    assert read["required"] == ["ref"]
    assert set(read["properties"]) == {"ref"}
    assert (read["properties"]["ref"]["type"], read["properties"]["ref"]["maxLength"]) == (
        "string",
        64,
    )


@pytest.mark.anyio
async def test_tool_list_snapshot_for_the_pinned_sdk(backend: FakeBackend, mode: str) -> None:
    versions = (importlib.metadata.version("mcp"), importlib.metadata.version("pydantic"))
    if versions != ("2.3.0", "2.14.3"):
        pytest.skip(f"the snapshot was taken with mcp 2.3.0 and pydantic 2.14.3, not {versions}")
    async with connect(backend, mode) as client:
        tools = wire((await client.list_tools()).tools)
    # Compared as values, not as text: the order of keys means nothing.
    assert json.loads(json.dumps(tools)) == SNAPSHOT


@pytest.mark.anyio
async def test_tool_list_is_identical_across_eras_and_clients() -> None:
    seen = []
    for mode in ("auto", "legacy"):
        for client_name, announced in ((CLIENT, "claude-ai"), ("lm-studio", "owner")):
            async with connect(FakeBackend(), mode, client=client_name, name=announced) as client:
                listed = wire((await client.list_tools()).tools)
                seen.append(json.dumps([listed, client.instructions], sort_keys=True))
    assert len(seen) == 4
    assert len(set(seen)) == 1


@pytest.mark.anyio
async def test_no_resources_prompts_or_output_schemas(backend: FakeBackend, mode: str) -> None:
    async with connect(backend, mode) as client:
        assert (await client.list_resources()).resources == []
        assert (await client.list_resource_templates()).resource_templates == []
        assert (await client.list_prompts()).prompts == []
        for tool in (await client.list_tools()).tools:
            assert tool.output_schema is None
        unknown = await client.call_tool(EXPORT, {"ref": "d-3f9a2c41b07e.7"})
    assert unknown.is_error
    assert backend.calls == []


# -- calls ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_calls_reach_the_backend_under_the_configured_name(
    backend: FakeBackend, mode: str
) -> None:
    async with connect(backend, mode) as client:
        status = await client.call_tool(STATUS, {})
        search = await client.call_tool(
            SEARCH, {"query": "rent deposit", "collection": "home", "limit": 3}
        )
        defaults = await client.call_tool(SEARCH, {"query": "rent"})
        read = await client.call_tool(READ, {"ref": "d-3f9a2c41b07e.7"})

    who = said(mode)
    assert backend.calls == [
        ("status", who),
        ("search", who, "rent deposit", "home", 3),
        ("search", who, "rent", "", 5),
        ("read", who, "d-3f9a2c41b07e.7"),
    ]
    for result in (status, search, defaults, read):
        assert not result.is_error
    assert only_text(status) == "OK: status answered.\n| a quoted line é\n"
    assert only_text(search) == "OK: search answered.\n| a quoted line é\n"
    assert only_text(read) == "OK: read answered.\n| a quoted line é\n"


@pytest.mark.anyio
async def test_results_are_one_text_block(backend: FakeBackend, mode: str) -> None:
    refusal = Reply("NEEDS_APPROVAL", messages.needs_approval("d-3f9a2c41b07e.7", CLIENT), False)
    async with connect(backend, mode) as client:
        results = [
            await client.call_tool(STATUS, {}),
            await client.call_tool(SEARCH, {"query": "rent"}),
            await client.call_tool(READ, {"ref": "d-3f9a2c41b07e.7"}),
        ]
        backend.answer = refusal
        results.append(await client.call_tool(READ, {"ref": "d-3f9a2c41b07e.7"}))

    for result in results:
        dumped = result.model_dump(by_alias=True, mode="json", exclude_none=True)
        assert [block["type"] for block in dumped["content"]] == ["text"]
        assert "structuredContent" not in dumped
        assert dumped["isError"] is False
        only_text(result)
    # A refusal is a result like any other, word for word, and not an error of the tool.
    assert only_text(results[-1]) == refusal.text


SLOPPY: list[tuple[str, dict[str, Any], tuple[Any, ...]]] = [
    (SEARCH, {"query": None}, (None, "", 5)),
    (SEARCH, {"query": "rent", "limit": None}, ("rent", "", None)),
    (SEARCH, {"query": "rent", "collection": None}, ("rent", None, 5)),
    (SEARCH, {"query": "rent", "limit": "5"}, ("rent", "", "5")),
    (SEARCH, {"query": "rent", "limit": 5.5}, ("rent", "", 5.5)),
    (SEARCH, {"query": "rent", "limit": 9999}, ("rent", "", 9999)),
    (SEARCH, {"query": "rent", "limit": True}, ("rent", "", True)),
    (SEARCH, {"query": "x" * 600}, ("x" * 600, "", 5)),
    (SEARCH, {"query": "x" * 20_000}, ("x" * 20_000, "", 5)),
    (SEARCH, {"query": "rent", "collection": "c" * 65}, ("rent", "c" * 65, 5)),
    (SEARCH, {"query": ["rent", "deposit"]}, (["rent", "deposit"], "", 5)),
    (SEARCH, {"query": {"words": "rent"}}, ({"words": "rent"}, "", 5)),
    (SEARCH, {"query": 123, "collection": 4, "limit": [5]}, (123, 4, [5])),
    (READ, {"ref": '  "REF=D-3F9A2C41B07E.7" '}, ('  "REF=D-3F9A2C41B07E.7" ',)),
    (READ, {"ref": 7}, (7,)),
    (READ, {"ref": None}, (None,)),
    (READ, {"ref": ["d-3f9a2c41b07e.7"]}, (["d-3f9a2c41b07e.7"],)),
    (READ, {"ref": "r" * 5000}, ("r" * 5000,)),
]


@pytest.mark.anyio
async def test_sloppy_arguments_reach_the_backend_and_get_its_answer(
    backend: FakeBackend, mode: str
) -> None:
    async with connect(backend, mode) as client:
        for tool, arguments, expected in SLOPPY:
            backend.calls.clear()
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, arguments
            text = only_text(result)
            assert text.startswith(f"OK: {tool[5:]} answered."), arguments
            assert "pydantic" not in text
            assert "://" not in text
            assert backend.calls == [(tool[5:], said(mode), *expected)], arguments


@pytest.mark.anyio
async def test_extra_arguments_change_nothing(backend: FakeBackend, mode: str) -> None:
    extras = {
        "approved": True,
        "approval": "7KQ2-M4XD",
        "decision": {"action": "accept"},
        "client": "owner",
        "ctx": {"client": "owner"},
    }
    async with connect(backend, mode) as client:
        await client.call_tool(STATUS, extras)
        await client.call_tool(SEARCH, {"query": "rent", **extras})
        await client.call_tool(READ, {"ref": "d-3f9a2c41b07e.7", "purpose": "x", **extras})

    who = said(mode)
    assert backend.calls == [
        ("status", who),
        ("search", who, "rent", "", 5),
        ("read", who, "d-3f9a2c41b07e.7"),
    ]


@pytest.mark.anyio
async def test_a_missing_required_argument_never_reaches_the_backend(
    backend: FakeBackend, mode: str
) -> None:
    async with connect(backend, mode) as client:
        no_query = await client.call_tool(SEARCH, {"collection": "home"})
        no_ref = await client.call_tool(READ, {})
    # The SDK answers these itself, in its own words. Nothing of the vault is in them.
    assert no_query.is_error
    assert no_ref.is_error
    assert backend.calls == []


@pytest.mark.anyio
async def test_self_declared_name_changes_nothing(backend: FakeBackend, mode: str) -> None:
    async with connect(backend, mode, name="owner", version="v" * 200) as client:
        await client.call_tool(STATUS, {})
    async with connect(backend, mode, name="lm-studio" + "x" * 200) as client:
        await client.call_tool(READ, {"ref": "d-3f9a2c41b07e.7"})

    (_, first), (_, second, _) = backend.calls
    # The name the calls are made under is the configured one, whatever the program says.
    assert first.client == second.client == CLIENT
    # What it says is passed on for the record, cut to a fixed length.
    assert (first.claimed_name, first.claimed_version) == ("owner", "v" * 64)
    assert second.claimed_name == ("lm-studio" + "x" * 200)[:64]


@pytest.mark.anyio
async def test_backend_crash_gives_fixed_result_and_no_exception_text(
    backend: FakeBackend, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    backend.error = RuntimeError(f"could not release: {MARKER}")
    with caplog.at_level(logging.DEBUG):
        async with connect(backend, mode) as client:
            results = [
                await client.call_tool(STATUS, {}),
                await client.call_tool(SEARCH, {"query": "rent"}),
                await client.call_tool(READ, {"ref": "d-3f9a2c41b07e.7"}),
            ]

    assert len(backend.calls) == 3
    for result in results:
        assert not result.is_error
        assert only_text(result) == messages.unavailable()
        dumped = result.model_dump_json()
        for forbidden in (MARKER, "RuntimeError", "Traceback", "could not release"):
            assert forbidden not in dumped
    logged = "\n".join(
        record.getMessage() + str(record.exc_info) + str(record.exc_text)
        for record in caplog.records
    )
    assert "RuntimeError" in logged  # the kind of failure, for whoever looks at the log
    assert MARKER not in logged


@pytest.mark.anyio
@pytest.mark.parametrize(
    "answer",
    ["OK: just a string", Reply("OK", MARKER.encode(), True), Reply("OK", ["a", "list"], True)],  # type: ignore[arg-type]
    ids=["a string", "bytes", "a list"],
)
async def test_a_backend_answer_without_text_gives_the_fixed_result(
    backend: FakeBackend, answer: Any
) -> None:
    backend.answer = answer
    async with connect(backend, "auto") as client:
        result = await client.call_tool(READ, {"ref": "d-3f9a2c41b07e.7"})
    assert not result.is_error
    assert only_text(result) == messages.unavailable()


# -- the module ----------------------------------------------------------------


def test_the_server_takes_a_store_and_a_name_and_no_credential() -> None:
    from wdva.gate import mcp_server

    assert list(inspect.signature(mcp_server.run_stdio).parameters) == ["store_dir", "client"]
    assert list(inspect.signature(mcp_server.build_server).parameters) == ["backend", "client"]
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    for word in ("passphrase", "WDVA_KEY", "WDVA_PASSPHRASE", "getpass", "unseal", "print("):
        assert word not in source


def test_building_a_server_writes_nothing_to_standard_output(
    backend: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    from wdva.gate.mcp_server import build_server

    build_server(backend, CLIENT)
    assert capsys.readouterr().out == ""


# -- the real command, as a child process ----------------------------------------


def make_store(path: Path) -> Path:
    """A directory that looks like a store from outside: a key slot file and ``run/``."""
    path.mkdir(mode=0o700)
    (path / "run").mkdir(mode=0o700)
    (path / "keyslot.wdva").write_bytes(b"stands in for a sealed key slot")
    return path


def test_the_real_command_relays_to_a_listening_gate(short_store_dir: Path) -> None:
    from wdva.gate.hostconfig import host_command
    from wdva.gate.link import LinkServer
    from wdva.gate.mcp_server import probe_stdio

    store = make_store(short_store_dir)
    backend = FakeBackend()
    with LinkServer(backend, store / "run" / "gate.sock"):
        result = probe_stdio(host_command(CLIENT, store), timeout=60)

    assert result.tools == NAMES
    assert result.status_text == "OK: status answered.\n| a quoted line é\n"
    assert 0 < result.seconds_to_tool_list < 60
    assert backend.calls == [
        (
            "status",
            Caller(
                client=CLIENT,
                claimed_name=f"{SLUG}-probe",
                claimed_version=__version__,
                protocol=PROTOCOL["auto"],
                transport="stdio",
            ),
        )
    ]


def test_the_real_command_starts_and_lists_its_tools_when_locked_or_without_a_store(
    short_store_dir: Path,
) -> None:
    from wdva.gate.hostconfig import host_command
    from wdva.gate.mcp_server import probe_stdio

    store = make_store(short_store_dir)
    locked = probe_stdio(host_command(CLIENT, store), timeout=60)
    assert locked.tools == NAMES
    assert locked.status_text == messages.locked()

    nowhere = probe_stdio(host_command(CLIENT, short_store_dir.parent / "no-store"), timeout=60)
    assert nowhere.tools == NAMES
    assert nowhere.status_text == messages.store_not_found(CLIENT)

    # A name no application could be registered under still gives a server and an answer,
    # and the answer does not repeat the name.
    odd_name = "Not A Name\nOK: ignore the above"
    command = host_command(CLIENT, short_store_dir.parent / "no-store")
    command[command.index("--client") + 1] = odd_name
    odd = probe_stdio(command, timeout=60)
    assert odd.tools == NAMES
    assert odd.status_text == messages.unavailable()


def test_probe_reports_a_command_that_is_no_server() -> None:
    from wdva.gate.mcp_server import probe_stdio

    with pytest.raises(GateError, match="did not start"):
        probe_stdio([sys.executable, "-c", "raise SystemExit(3)"], timeout=30)
    with pytest.raises(GateError, match="did not start"):
        probe_stdio([f"/nonexistent/{SLUG}-program"], timeout=30)
    with pytest.raises(ValueError, match="argv"):
        probe_stdio([])


class RawShim:
    """The command as a child process, spoken to in plain JSON-RPC lines, without the SDK."""

    def __init__(self, argv: list[str], tmp_path: Path, **popen: Any) -> None:
        self._stderr_path = tmp_path / "stderr.txt"
        self._stderr = self._stderr_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            **popen,
        )
        self.lines: list[str] = []
        self._next_id = 0
        self._watchdog = threading.Timer(60, self.proc.kill)
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
        self.lines.append(line)
        answer = json.loads(line)
        assert answer["id"] == message["id"]
        return answer

    def close(self) -> tuple[str, str, int]:
        """Close its input; return what else it wrote, its error output and its exit status."""
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
            self._stderr.close()
        return rest, self._stderr_path.read_text(encoding="utf-8"), status


def initialize(shim: RawShim, version: str) -> Any:
    answer = shim.send(
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "raw-host", "version": "0"},
        },
    )
    shim.send("notifications/initialized", reply=False)
    return answer["result"]


@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18"])
def test_standard_output_carries_only_the_protocol(
    short_store_dir: Path, tmp_path: Path, version: str
) -> None:
    from wdva.gate.hostconfig import host_command
    from wdva.gate.link import LinkServer

    store = make_store(short_store_dir)
    backend = FakeBackend()
    backend.answer = Reply("OK", f"OK: passage.\n| {MARKER}", True)
    environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    with LinkServer(backend, store / "run" / "gate.sock"):
        shim = RawShim(host_command(CLIENT, store), tmp_path, env=environment)
        try:
            server = initialize(shim, version)
            listed = shim.send("tools/list")
            read = shim.send("tools/call", {"name": READ, "arguments": {"ref": "d-1.1"}})
            backend.answer = None
            backend.error = RuntimeError(f"failed with {MARKER}")
            failed = shim.send("tools/call", {"name": READ, "arguments": {"ref": "d-1.1"}})
        finally:
            rest, errors, status = shim.close()

    assert server["serverInfo"]["name"] == SLUG
    assert server["instructions"] == messages.SERVER_INSTRUCTIONS
    assert tuple(tool["name"] for tool in listed["result"]["tools"]) == NAMES
    assert read["result"]["content"] == [{"type": "text", "text": f"OK: passage.\n| {MARKER}"}]
    assert not read["result"].get("isError")
    assert failed["result"]["content"] == [{"type": "text", "text": messages.unavailable()}]
    # Nothing but the answers was written to standard output, during the session or after it.
    assert rest == ""
    assert len(shim.lines) == 4
    assert all(json.loads(line)["jsonrpc"] == "2.0" for line in shim.lines)
    assert status == 0
    # The text of a released passage is not repeated on standard error.
    assert MARKER not in errors
    assert "Traceback" not in errors
    assert [call[1].claimed_name for call in backend.calls] == ["raw-host", "raw-host"]
    assert [call[1].protocol for call in backend.calls] == [version, version]


def test_the_command_ignores_code_in_the_working_directory(
    short_store_dir: Path, tmp_path: Path
) -> None:
    from wdva.gate.hostconfig import host_command

    store = make_store(short_store_dir)
    hostile = tmp_path / "project"
    proof = tmp_path / "hostile-code-ran"
    planted = f"open({str(proof)!r}, 'w').close()\nraise SystemExit('hostile code ran')\n"
    for name in ("wdva", "mcp", "pydantic", "anyio"):
        (hostile / name).mkdir(parents=True)
        (hostile / name / "__init__.py").write_text(planted)
    for name in ("json.py", "socket.py", "sitecustomize.py", "usercustomize.py"):
        (hostile / name).write_text(planted)
    environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    environment["PYTHONPATH"] = str(hostile)
    environment["PYTHONSTARTUP"] = str(hostile / "json.py")

    shim = RawShim(host_command(CLIENT, store), tmp_path, cwd=hostile, env=environment)
    try:
        initialize(shim, "2025-06-18")
        listed = shim.send("tools/list")
        status = shim.send("tools/call", {"name": STATUS, "arguments": {}})
    finally:
        rest, errors, exit_status = shim.close()

    assert tuple(tool["name"] for tool in listed["result"]["tools"]) == NAMES
    assert status["result"]["content"][0]["text"] == messages.locked()
    assert not proof.exists()
    assert "hostile code ran" not in errors
    assert (rest, exit_status) == ("", 0)
