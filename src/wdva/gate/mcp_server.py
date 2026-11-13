# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The MCP server an agent application starts: three read-only tools and nothing else.

``wdva gate mcp --client NAME`` runs :func:`run_stdio`. The process holds no key
and opens no vault. It relays ``wdva_status``, ``wdva_search`` and ``wdva_read``
to the app that has the store unlocked (:class:`~wdva.gate.link.RemoteGate`) and
hands back the text it gets. Every decision is made on the other side.

The server always starts and always lists its tools. A locked store, a store
that cannot be found and a backend that fails are tool results with a fixed
text, never an error of the server and never the text of an exception: an
exception could carry document text past the gate's counting.

The tools declare their arguments as "anything", so that the SDK only checks
that a required one is present. The gate judges the values itself and answers
in its own words, whatever a model sends. The advertised schema still says
what is expected.

This is the only module of the gate that imports the MCP SDK. Import it only
where the SDK is needed.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

import anyio
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import Implementation, TextContent, ToolAnnotations
from pydantic import Field

from wdva._version import __version__
from wdva.gate import messages
from wdva.gate.errors import GateError
from wdva.gate.link import MAX_CLAIMED_CHARS, RemoteGate
from wdva.gate.messages import (
    ARGUMENT_DESCRIPTIONS,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    TOOL_DESCRIPTIONS,
    TOOL_READ,
    TOOL_SEARCH,
    TOOL_STATUS,
    TOOL_TITLES,
)
from wdva.gate.paths import resolve_store
from wdva.gate.types import Backend, Caller, Reply

__all__ = ["ProbeResult", "build_server", "probe_stdio", "run_stdio"]

_LOG = logging.getLogger(__name__)

# The arguments, as the tools declare them. The type is "anything": the SDK
# then passes null, numbers, lists and over-long text through, and the gate
# answers them itself. "type" and "maxLength" are advertised, not enforced.
_Query = Annotated[
    Any,
    Field(
        description=ARGUMENT_DESCRIPTIONS["query"],
        json_schema_extra={"type": "string", "maxLength": 500},
    ),
]
_Collection = Annotated[
    Any,
    Field(
        description=ARGUMENT_DESCRIPTIONS["collection"],
        json_schema_extra={"type": "string", "maxLength": 32},
    ),
]
_Limit = Annotated[
    Any,
    Field(description=ARGUMENT_DESCRIPTIONS["limit"], json_schema_extra={"type": "integer"}),
]
_Ref = Annotated[
    Any,
    Field(
        description=ARGUMENT_DESCRIPTIONS["ref"],
        json_schema_extra={"type": "string", "maxLength": 64},
    ),
]


def _label(value: Any) -> str | None:
    return value[:MAX_CLAIMED_CHARS] if isinstance(value, str) else None


def _caller(ctx: Context, client: str) -> Caller:
    """Who calls: the configured client name, and what the program says about itself.

    The claimed name, version and protocol are recorded for the owner and decide
    nothing. A program that announces none, or a context that cannot say, gives
    empty fields.
    """
    name = version = protocol = None
    try:
        protocol = _label(ctx.protocol_version)
        params = ctx.session.client_params
        info = params.client_info if params is not None else None
        if info is not None:
            name, version = _label(info.name), _label(info.version)
    except Exception as exc:
        _LOG.debug("the calling program could not be described (%s)", type(exc).__name__)
    return Caller(
        client=client,
        claimed_name=name,
        claimed_version=version,
        protocol=protocol,
        transport="stdio",
    )


def _answer(tool: str, call: Callable[[], Reply]) -> str:
    """The text of the backend's reply, or the fixed text when the backend fails.

    Only the kind of failure is logged. The message of an exception is never
    shown or logged: it may hold document text.
    """
    try:
        text = call().text
        if isinstance(text, str):
            return text
        failure = "the reply holds no text"
    except Exception as exc:
        failure = type(exc).__name__
    _LOG.warning("%s could not be answered (%s)", tool, failure)
    return messages.unavailable()


def build_server(backend: Backend, client: str) -> MCPServer:
    """An MCP server whose three tools call ``backend`` under the name ``client``.

    ``backend`` is anything with ``status``, ``search`` and ``read``: a link to
    the app, or a gate in the same process. The names, titles, descriptions,
    schemas and annotations are the same for every client. The tools are marked
    read-only for the application's benefit; nothing in the gate depends on an
    application asking its user before a call.

    The server is made here and not when the module is imported, because making
    one configures logging for the whole process.
    """
    server = MCPServer(
        SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        log_level="WARNING",
    )
    read_only = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,  # every call is recorded, and most use up allowance
        open_world_hint=False,
    )

    def wdva_status(ctx: Context) -> str:
        return _answer(TOOL_STATUS, lambda: backend.status(_caller(ctx, client)))

    def wdva_search(
        ctx: Context, query: _Query, collection: _Collection = "", limit: _Limit = 5
    ) -> str:
        return _answer(
            TOOL_SEARCH, lambda: backend.search(_caller(ctx, client), query, collection, limit)
        )

    def wdva_read(ctx: Context, ref: _Ref) -> str:
        return _answer(TOOL_READ, lambda: backend.read(_caller(ctx, client), ref))

    for name, function in (
        (TOOL_STATUS, wdva_status),
        (TOOL_SEARCH, wdva_search),
        (TOOL_READ, wdva_read),
    ):
        server.tool(
            name=name,
            title=TOOL_TITLES[name],
            description=TOOL_DESCRIPTIONS[name],
            annotations=read_only,
            structured_output=False,
        )(function)
    return server


def run_stdio(store_dir: str | os.PathLike[str] | None, client: str) -> None:
    """Serve the three tools on standard input and output until the application closes them.

    ``store_dir`` is the store directory; ``None`` means the one named by
    ``WDVA_GATE_STORE``, else the per-user default. Nothing is checked here: a
    store that is missing or locked, and a client name that is not served, show
    up as the result of a tool call, where the user of the application sees them.

    Standard output belongs to the protocol. This function writes nothing else
    to it, and makes it line-buffered so that a stray line from other code could
    not surface later in the middle of a message.
    """
    server = build_server(RemoteGate(resolve_store(store_dir), client), client)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
    server.run()


@dataclass(frozen=True)
class ProbeResult:
    """What a started server showed: its tools, how soon, and its status text."""

    tools: tuple[str, ...]
    seconds_to_tool_list: float
    status_text: str | None  # None when the status call failed


def probe_stdio(argv: Sequence[str], *, timeout: float = 10.0) -> ProbeResult:
    """Start ``argv`` as a stdio MCP server, list its tools and call ``wdva_status``.

    This is what an agent application does when it connects, done with the
    SDK's own client. It shows that the command starts and answers; it says
    nothing about any particular application.

    Raises :class:`~wdva.gate.errors.GateError` when the command cannot be
    started or does not list its tools within ``timeout`` seconds. A status
    call that fails is not an error: ``status_text`` is then ``None``.
    """
    command = [os.fspath(part) for part in argv]
    if not command:
        raise ValueError("argv must name a command")

    async def probe() -> ProbeResult:
        started = time.monotonic()
        parameters = StdioServerParameters(command=command[0], args=command[1:])
        identity = Implementation(name="wdva-gate-probe", version=__version__)
        with anyio.fail_after(timeout):
            async with Client(parameters, client_info=identity) as session:
                tools = tuple(tool.name for tool in (await session.list_tools()).tools)
                seconds = time.monotonic() - started
                status_text = None
                try:
                    result = await session.call_tool(TOOL_STATUS, {})
                except Exception as exc:
                    _LOG.debug("the status call failed (%s)", type(exc).__name__)
                else:
                    blocks = result.content
                    if (
                        not result.is_error
                        and len(blocks) == 1
                        and isinstance(blocks[0], TextContent)
                    ):
                        status_text = blocks[0].text
        return ProbeResult(tools, seconds, status_text)

    try:
        result: ProbeResult = anyio.run(probe)
    except Exception as exc:
        raise GateError(
            f"the MCP server did not start or did not list its tools within {timeout:g} "
            f"seconds ({type(exc).__name__})"
        ) from exc
    return result
