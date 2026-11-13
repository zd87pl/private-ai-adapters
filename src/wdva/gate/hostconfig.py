# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Configuration text that connects an agent application to the gate.

An agent application reaches the gate by starting a small program, the MCP
server of ``wdva gate mcp``. This module writes the lines an owner copies into
an application's configuration so that it does. It is data and formatting only:
it writes no file, changes no application's configuration and starts no program.

The command is the same for every application (:func:`host_command`). It names
the Python interpreter that runs this package and the store by their full
paths, so it does not depend on ``PATH``, on the directory an application
starts it in, or on a shell profile. It holds no secret: the program it starts
has no key and asks for none.

The form each application expects was taken from that application's
documentation on the day recorded with it. None of them was tried with the
application itself, and the text printed says so. Products that run in a
vendor's cloud cannot start a program on this computer; for those there is an
explanation instead of a configuration.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shlex
import sys
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from wdva.errors import MissingDependencyError
from wdva.gate import brand
from wdva.gate.errors import GateError
from wdva.gate.paths import resolve_store

__all__ = [
    "CLIENT_NAME_PATTERN",
    "INSTALL_LINE",
    "KNOWN_HOSTS",
    "PYTHON_MESSAGE",
    "SERVER_KEY",
    "Block",
    "HostConfig",
    "KnownHost",
    "check_client_name",
    "host_command",
    "known_host",
    "missing_sdk_error",
    "render",
    "render_text",
    "sdk_status",
]

INSTALL_LINE = 'pip install "wdva[gate] @ git+https://github.com/zd87pl/private-ai-adapters"'
"""What installs the MCP SDK next to this package."""

PYTHON_MESSAGE = "the MCP server needs Python 3.11 or newer"

SERVER_KEY = brand.SLUG
"""The name the gate's server gets in an application's configuration."""

CLIENT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")
"""A name an application can be connected under. Use it with ``fullmatch``."""

_NAME_RULE = 'a name has 1 to 32 characters from a-z, 0-9 and "-" and starts with a letter'
_DOCS_READ = "2026-11-12"
_SDK_MODULES = ("mcp", "pydantic", "anyio")


@dataclass(frozen=True)
class KnownHost:
    """An agent application, or a product that cannot be connected, as the gate describes it.

    ``name`` is the client name suggested for it, empty where the owner has to
    choose one. ``destination`` is where text released to it goes, and
    ``hosted`` whether that is a provider; both are suggestions for the owner,
    who declares them, and neither is checked. ``local_program`` is false for a
    product that only runs in its vendor's cloud. ``source`` is the
    documentation its form was taken from and ``read`` the day that was read.
    """

    id: str
    label: str
    name: str
    destination: str
    hosted: bool
    local_program: bool
    source: str
    read: str

    def as_dict(self) -> dict[str, Any]:
        """The fields an interface needs to offer this application to the owner."""
        return {
            "id": self.id,
            "label": self.label,
            "name": self.name,
            "destination": self.destination,
            "hosted": self.hosted,
            "local_program": self.local_program,
        }


KNOWN_HOSTS: tuple[KnownHost, ...] = (
    KnownHost(
        id="claude-desktop",
        label="Claude Desktop",
        name="claude-desktop",
        destination="Anthropic",
        hosted=True,
        local_program=True,
        source="https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="claude-code",
        label="Claude Code",
        name="claude-code",
        destination="Anthropic",
        hosted=True,
        local_program=True,
        source="https://code.claude.com/docs/en/mcp",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="codex",
        label="ChatGPT desktop app or Codex",
        name="codex",
        destination="OpenAI",
        hosted=True,
        local_program=True,
        source="https://learn.chatgpt.com/docs/extend/mcp",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="openclaw",
        label="OpenClaw",
        name="openclaw",
        destination="the model you configured in OpenClaw",
        hosted=True,
        local_program=True,
        source="https://docs.openclaw.ai/gateway/config-extensions",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="generic",
        label="Another app",
        name="",
        destination="",
        hosted=True,
        local_program=True,
        source="https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="chatgpt-web",
        label="ChatGPT on the web or phone",
        name="",
        destination="OpenAI",
        hosted=True,
        local_program=False,
        source="https://developers.openai.com/api/docs/guides/custom-mcp-server",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="claude-web",
        label="Claude on the web or phone",
        name="",
        destination="Anthropic",
        hosted=True,
        local_program=False,
        source="https://claude.com/docs/connectors/custom/add-unlisted",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="grok-web",
        label="Grok on the web or phone",
        name="",
        destination="xAI",
        hosted=True,
        local_program=False,
        source="https://docs.x.ai/grok/connectors",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="grok-bot",
        label="Grok Bot",
        name="",
        destination="xAI",
        hosted=True,
        local_program=False,
        source="https://docs.x.ai/grok-bot/computer-and-apps",
        read=_DOCS_READ,
    ),
)
"""Every application the gate has a form for, and the products it has an explanation for."""

# Whose documentation `source` is, as it reads in a sentence.
_DOCUMENTED_BY = {
    "claude-desktop": "the Model Context Protocol documentation",
    "claude-code": "Anthropic's documentation",
    "codex": "OpenAI's documentation",
    "openclaw": "OpenClaw's documentation",
    "generic": "the Model Context Protocol documentation",
    "chatgpt-web": "OpenAI's documentation",
    "claude-web": "Anthropic's documentation",
    "grok-web": "xAI's documentation",
    "grok-bot": "xAI's documentation",
}

# For a product that runs in its vendor's cloud: why it cannot reach a store on this
# computer, and what the owner can use instead.
_WHY_NOT = {
    "chatgpt-web": "It cannot start a program on this computer",
    "claude-web": "It cannot start a program on this computer",
    "grok-web": "It cannot start a program on this computer",
    "grok-bot": "It works on a computer in xAI's cloud, not on this one",
}
_LOCAL_ALTERNATIVE = {
    "chatgpt-web": "Use the ChatGPT desktop app or Codex on this computer instead",
    "claude-web": "Use Claude Desktop or Claude Code on this computer instead",
    "grok-web": (
        "Use Grok Build on this computer instead (according to xAI's documentation it takes "
        "the configuration shown for Codex, in ~/.grok/config.toml)"
    ),
    "grok-bot": (
        "Use Grok Build on this computer instead (according to xAI's documentation it takes "
        "the configuration shown for Codex, in ~/.grok/config.toml)"
    ),
}


@dataclass(frozen=True)
class Block:
    """One piece of text to copy: a command line, or lines for a configuration file.

    ``language`` is ``"shell"``, ``"json"``, ``"json5"`` or ``"toml"``.
    ``where`` lists the places the application looks for the file, when the
    text belongs in a file.
    """

    title: str
    language: str
    text: str
    where: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The block as JSON values."""
        return {
            "title": self.title,
            "language": self.language,
            "text": self.text,
            "where": list(self.where),
        }


@dataclass(frozen=True)
class HostConfig:
    """What to show an owner who wants to connect one application.

    For a product that cannot start a local program, ``blocks`` is empty and
    ``explanation`` says why and what to use instead. Otherwise ``explanation``
    is ``None`` and ``command`` is the command every block holds.
    """

    host: str
    label: str
    local_program: bool
    blocks: tuple[Block, ...]
    notes: tuple[str, ...]
    explanation: str | None
    source: str
    read: str
    command: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The configuration as JSON values, for an interface to show."""
        return {
            "host": self.host,
            "label": self.label,
            "local_program": self.local_program,
            "blocks": [block.as_dict() for block in self.blocks],
            "notes": list(self.notes),
            "explanation": self.explanation,
        }


def known_host(host: str) -> KnownHost:
    """The entry of :data:`KNOWN_HOSTS` with this id. :class:`ValueError` when there is none."""
    for entry in KNOWN_HOSTS:
        if entry.id == host:
            return entry
    known = ", ".join(entry.id for entry in KNOWN_HOSTS)
    raise ValueError(
        f"{str(host)[:40]!r} is not an application the gate knows; choose from {known}"
    )


def check_client_name(client: str) -> str:
    """``client`` if an application can be connected under it. :class:`ValueError` otherwise."""
    if not isinstance(client, str) or CLIENT_NAME_PATTERN.fullmatch(client) is None:
        raise ValueError(f"{str(client)[:40]!r} cannot be used as a name: {_NAME_RULE}")
    return client


def host_command(client: str, store: str | os.PathLike[str] | None) -> list[str]:
    """The command an application runs to reach the gate under the name ``client``.

    It starts the interpreter that runs this package, by the path it was
    started with, in isolated mode: the working directory, ``PYTHON*``
    variables and the per-user site directory are then ignored, so code lying
    in a project folder cannot take the place of the gate's. The store is
    given as an absolute path; ``None`` means the store that
    :func:`wdva.gate.paths.resolve_store` finds.

    Raises :class:`ValueError` for a name that cannot be used and for a path
    that cannot be written into a configuration file.
    """
    check_client_name(client)
    if not sys.executable:
        raise GateError("this Python cannot say where its interpreter is, so no command is known")
    command = [
        sys.executable,
        "-I",
        "-X",
        "utf8",
        "-m",
        "wdva",
        "gate",
        "mcp",
        "--client",
        client,
        "--store",
        str(resolve_store(store)),
    ]
    for part in command:
        try:
            part.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "the path of the store or of Python holds bytes that are not text; "
                "it cannot be written into a configuration file"
            ) from None
    return command


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------

_TOML_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_string(value: str) -> str:
    """``value`` as a TOML basic string."""
    out = []
    for char in value:
        if char in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_json_string(value) for value in values) + "]"


def _mcp_servers_json(command: Sequence[str]) -> str:
    """The ``mcpServers`` object most applications read."""
    entry = {"mcpServers": {SERVER_KEY: {"command": command[0], "args": list(command[1:])}}}
    return json.dumps(entry, indent=2, ensure_ascii=False)


def _codex_toml(command: Sequence[str]) -> str:
    return "\n".join(
        (
            f"[mcp_servers.{SERVER_KEY}]",
            f"command = {_toml_string(command[0])}",
            "args = [" + ", ".join(_toml_string(part) for part in command[1:]) + "]",
        )
    )


def _openclaw_json5(command: Sequence[str]) -> str:
    return "\n".join(
        (
            "mcp: {",
            "  servers: {",
            f"    {_json_string(SERVER_KEY)}: {{",
            f"      command: {_json_string(command[0])},",
            f"      args: {_json_list(command[1:])}",
            "    }",
            "  }",
            "}",
        )
    )


def _shell(*words: str) -> str:
    return shlex.join(words)


def _source_note(entry: KnownHost) -> str:
    return (
        f"This form follows {_DOCUMENTED_BY[entry.id]} ({entry.source}) as read on "
        f"{entry.read}. It has not been tried with the application itself."
    )


def _blocks(entry: KnownHost, command: Sequence[str]) -> tuple[tuple[Block, ...], tuple[str, ...]]:
    """The blocks and the notes for an application that starts local programs."""
    if entry.id == "claude-desktop":
        blocks: tuple[Block, ...] = (
            Block(
                "Add this to claude_desktop_config.json",
                "json",
                _mcp_servers_json(command),
                (
                    "~/Library/Application Support/Claude/claude_desktop_config.json",
                    "~/.config/Claude/claude_desktop_config.json",
                    "%APPDATA%\\Claude\\claude_desktop_config.json",
                ),
            ),
        )
        notes: tuple[str, ...] = (
            f'If the file already has "mcpServers", add the "{SERVER_KEY}" entry inside it.',
            "Quit and restart Claude Desktop afterwards.",
        )
    elif entry.id == "claude-code":
        blocks = (
            Block(
                "Run this in a terminal",
                "shell",
                _shell(
                    "claude",
                    "mcp",
                    "add",
                    "--transport",
                    "stdio",
                    "--scope",
                    "user",
                    SERVER_KEY,
                    "--",
                    *command,
                ),
            ),
            Block(
                "Or add this to .mcp.json in a project",
                "json",
                _mcp_servers_json(command),
                (".mcp.json",),
            ),
        )
        notes = ("Use one of the two, not both.",)
    elif entry.id == "codex":
        blocks = (
            Block(
                "Run this in a terminal",
                "shell",
                _shell("codex", "mcp", "add", SERVER_KEY, "--", *command),
            ),
            Block(
                "Or add this to ~/.codex/config.toml",
                "toml",
                _codex_toml(command),
                ("~/.codex/config.toml",),
            ),
        )
        notes = (
            "Use one of the two, not both.",
            "The ChatGPT desktop app reads the same file as Codex, and Grok Build takes the "
            "same form in ~/.grok/config.toml, according to their documentation.",
        )
    elif entry.id == "openclaw":
        blocks = (
            Block(
                "Add this inside the outer braces of ~/.openclaw/openclaw.json",
                "json5",
                _openclaw_json5(command),
                ("~/.openclaw/openclaw.json",),
            ),
        )
        notes = (f'If the file already has "mcp", add the "{SERVER_KEY}" entry to its servers.',)
    else:
        blocks = (
            Block(
                "Most applications that start local MCP servers read this form",
                "json",
                _mcp_servers_json(command),
            ),
            Block("Where an application asks for a command", "shell", _shell(*command)),
        )
        notes = ("Where the text goes is in the application's own documentation.",)
    return blocks, (*notes, _source_note(entry))


def _explanation(entry: KnownHost) -> str:
    """The paragraph for a product that cannot start a program on this computer."""
    return (
        f"{entry.label} calls only servers that {entry.destination}'s cloud can reach over the "
        f"internet, according to {_DOCUMENTED_BY[entry.id]}. {_WHY_NOT[entry.id]}, so it "
        f"cannot reach this vault and there is nothing to configure. "
        f"{_LOCAL_ALTERNATIVE[entry.id]}, or copy the text you want it to see into the "
        f"conversation yourself. Text you paste goes to {entry.destination} like any other "
        f"message; {brand.PRODUCT} neither limits nor records it."
    )


def render(host: str, *, client: str, store: str | os.PathLike[str] | None) -> HostConfig:
    """What connects the application ``host`` to the gate under the name ``client``.

    ``host`` is an id from :data:`KNOWN_HOSTS`. Raises :class:`ValueError` for
    an unknown one and for a client name that cannot be used.
    """
    entry = known_host(host)
    check_client_name(client)
    if not entry.local_program:
        return HostConfig(
            host=entry.id,
            label=entry.label,
            local_program=False,
            blocks=(),
            notes=(f"Source: {entry.source}, read on {entry.read}.",),
            explanation=_explanation(entry),
            source=entry.source,
            read=entry.read,
        )
    command = host_command(client, store)
    blocks, notes = _blocks(entry, command)
    return HostConfig(
        host=entry.id,
        label=entry.label,
        local_program=True,
        blocks=blocks,
        notes=notes,
        explanation=None,
        source=entry.source,
        read=entry.read,
        command=tuple(command),
    )


def _wrap(text: str) -> str:
    return textwrap.fill(text, width=88, break_long_words=False, break_on_hyphens=False)


def render_text(host: str, *, client: str, store: str | os.PathLike[str] | None) -> str:
    """The same as :func:`render`, as plain text for a terminal. It changes nothing.

    When this environment cannot run the MCP server yet, the text ends by
    saying what is missing: the command it shows would not start.
    """
    config = render(host, client=client, store=store)
    parts = [config.label]
    if config.explanation is not None:
        parts.append(_wrap(config.explanation))
    for block in config.blocks:
        parts.append(f"{block.title}:")
        parts.append(block.text)
        if block.where:
            parts.append("\n".join(("The file is at:", *(f"  {place}" for place in block.where))))
    parts.extend(_wrap(note) for note in config.notes)
    missing = sdk_status()["message"] if config.local_program else None
    if missing is not None:
        parts.append(f"Before an application can use this: {missing}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Is the MCP SDK there?
# ---------------------------------------------------------------------------


def missing_sdk_error() -> MissingDependencyError:
    """The error for a command that needs the MCP SDK when it is not installed."""
    return MissingDependencyError(
        "the MCP server needs the MCP SDK. Install it into the same environment with:  "
        + INSTALL_LINE
    )


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _usable(version: str | None) -> bool:
    """Whether an installed SDK version is one the MCP server is written for (2.3 up to 3)."""
    if version is None:
        return True  # installed without metadata: nothing to judge by
    match = re.match(r"(\d+)\.(\d+)", version)
    return match is not None and (2, 3) <= (int(match[1]), int(match[2])) < (3, 0)


def sdk_status() -> dict[str, Any]:
    """Whether this environment can run the MCP server that applications start.

    Returns ``sdk`` (the MCP SDK is installed in a version the server is
    written for), ``version`` (the installed version, or ``None``), ``python``
    (this is Python 3.11 or newer) and ``message`` (what to do when one of the
    two is false, else ``None``). The SDK is looked up, not imported.
    """
    python = sys.version_info >= (3, 11)
    installed = all(_importable(module) for module in _SDK_MODULES)
    version: str | None = None
    if installed:
        try:
            version = importlib.metadata.version("mcp")
        except importlib.metadata.PackageNotFoundError:
            version = None
    sdk = installed and _usable(version)
    if not python:
        message: str | None = PYTHON_MESSAGE
    elif not sdk:
        message = str(missing_sdk_error())
    else:
        message = None
    return {"sdk": sdk, "version": version, "python": python, "message": message}
