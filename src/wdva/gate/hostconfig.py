# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Configuration text that connects an agent application to the gate.

An agent application reaches the gate by starting a small program, the MCP
server of ``wdva gate mcp``. This module writes the lines an owner copies into
an application's configuration so that it does. It is data and formatting only:
it writes no file, changes no application's configuration and starts no program.
That holds for what it says about a settings file too: where the file of an
application lies on this computer (:func:`settings_file`) is a path worked out
from the home directory, and the line that opens it in a text editor
(:func:`open_line`) is text for the owner to read and run. The one module that
reads such a file is :mod:`wdva.gate.hostcheck`.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from wdva.errors import MissingDependencyError
from wdva.gate import brand
from wdva.gate.errors import GateError
from wdva.gate.paths import resolve_store

__all__ = [
    "CLIENT_NAME_PATTERN",
    "INSTALL_LINE",
    "KNOWN_HOSTS",
    "PLATFORMS",
    "PYTHON_MESSAGE",
    "REPOSITORY",
    "SERVER_KEY",
    "Block",
    "HostConfig",
    "KnownHost",
    "check_client_name",
    "host_command",
    "install_line",
    "known_host",
    "missing_sdk_error",
    "open_line",
    "platform_name",
    "render",
    "render_text",
    "sdk_status",
    "settings_file",
    "shown_path",
]

REPOSITORY = "git+https://github.com/zd87pl/private-ai-adapters"
"""Where pip gets this package from. It is not on PyPI, so every install line names this."""


def install_line(extra: str) -> str:
    """The command that installs this package together with one of its extras."""
    return f'pip install "wdva[{extra}] @ {REPOSITORY}"'


INSTALL_LINE = install_line("gate")
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
        id="chatgpt-desktop",
        label="ChatGPT desktop app",
        name="chatgpt-desktop",
        destination="OpenAI",
        hosted=True,
        local_program=True,
        source="https://learn.chatgpt.com/docs/extend/mcp",
        read=_DOCS_READ,
    ),
    KnownHost(
        id="codex",
        label="Codex",
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
    "chatgpt-desktop": "OpenAI's documentation",
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

PLATFORMS = ("macos", "linux")
"""The systems the gate runs on, as an answer names them."""

# What the owner does in the application once the text is in place: a title, one
# sentence, and whether the application is a desktop app that has to be quit.
_AFTER: dict[str, tuple[str, str, bool]] = {
    "claude-desktop": (
        "Quit Claude Desktop, open it again, and paste the question into a new chat",
        "It reads its settings when it starts.",
        True,
    ),
    "chatgpt-desktop": (
        "Quit the ChatGPT desktop app, open it again, and paste the question into a new chat",
        "It reads its settings when it starts.",
        True,
    ),
    "claude-code": (
        "Start a new Claude Code session and paste the question",
        "A session reads its settings when it starts.",
        False,
    ),
    "codex": (
        "Start a new Codex session and paste the question",
        "A session reads its settings when it starts.",
        False,
    ),
    "openclaw": (
        "Restart OpenClaw and paste the question",
        "It reads its settings when it starts.",
        False,
    ),
    "generic": (
        "Restart the app and paste the question",
        "Most apps read their settings when they start.",
        False,
    ),
}

# The applications with one settings file the gate knows the place of, and its format.
_SETTINGS_FORMAT = {"claude-desktop": "json", "chatgpt-desktop": "toml", "codex": "toml"}


@dataclass(frozen=True)
class Block:
    """One piece of text to copy: a command line, or lines for a configuration file.

    ``language`` is ``"shell"``, ``"json"``, ``"json5"`` or ``"toml"``.
    ``where`` lists the places the application looks for the file, when the
    text belongs in a file, and ``where_here`` is the one of them that applies
    on this computer, if one does. For a JSON block with ``mcpServers``,
    ``inner`` holds two smaller pieces of the same text, for a file that has
    other settings already: ``entry`` is the gate's entry alone and ``section``
    the whole ``mcpServers`` member, each without the braces around it.
    """

    title: str
    language: str
    text: str
    where: tuple[str, ...] = ()
    where_here: str | None = None
    inner: Mapping[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        """The block as JSON values."""
        return {
            "title": self.title,
            "language": self.language,
            "text": self.text,
            "where": list(self.where),
            "where_here": self.where_here,
            "inner": None if self.inner is None else dict(self.inner),
        }


@dataclass(frozen=True)
class HostConfig:
    """What to show an owner who wants to connect one application.

    For a product that cannot start a local program, ``blocks`` is empty and
    ``explanation`` says why and what to use instead. Otherwise ``explanation``
    is ``None`` and ``command`` is the command every block holds.

    ``platform`` is the system the places were chosen for, a name from
    :data:`PLATFORMS`. ``plain_note`` says in one sentence, without an address,
    what the form follows and that it was not tried; ``after`` is what the owner
    does in the application afterwards (``title``, ``text``, and ``desktop``:
    whether it is a desktop app that has to be quit). Both are ``None`` for a
    product that cannot be connected.
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
    platform: str = "linux"
    plain_note: str | None = None
    after: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """The configuration as JSON values, for an interface to show."""
        return {
            "host": self.host,
            "label": self.label,
            "local_program": self.local_program,
            "blocks": [block.as_dict() for block in self.blocks],
            "notes": list(self.notes),
            "explanation": self.explanation,
            "platform": self.platform,
            "plain_note": self.plain_note,
            "after": None if self.after is None else dict(self.after),
        }


def known_host(host: str) -> KnownHost:
    """The entry of :data:`KNOWN_HOSTS` with this id. :class:`ValueError` when there is none."""
    for entry in KNOWN_HOSTS:
        if entry.id == host:
            return entry
    known = ", ".join(entry.id for entry in KNOWN_HOSTS)
    raise ValueError(
        f"{str(host)[:40]!r} is not an application {brand.PRODUCT} knows; choose from {known}"
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


def _inside(value: Mapping[str, Any]) -> str:
    """A JSON object with one member, written as that member alone, without the braces."""
    lines = json.dumps(value, indent=2, ensure_ascii=False).split("\n")[1:-1]
    return "\n".join(line[2:] for line in lines)


def _mcp_servers_inner(command: Sequence[str]) -> dict[str, str]:
    """The two pieces of :func:`_mcp_servers_json` for a file that holds other settings."""
    entry = {SERVER_KEY: {"command": command[0], "args": list(command[1:])}}
    return {"entry": _inside(entry), "section": _inside({"mcpServers": entry})}


def _source_note(entry: KnownHost) -> str:
    return (
        f"This form follows {_DOCUMENTED_BY[entry.id]} ({entry.source}) as read on "
        f"{entry.read}. It has not been tried with the application itself."
    )


def _plain_note(entry: KnownHost) -> str:
    """What the form follows and that it was not tried, without the address of a page."""
    if entry.id == "generic":
        return (
            "This follows the form most apps of this kind read. It has not been tried with "
            "your app."
        )
    return (
        f"This follows the published instructions for {entry.label}, as read on {entry.read}. "
        f"It has not been tried with {entry.label} itself."
    )


# ---------------------------------------------------------------------------
# Where an application's settings file is on this computer
# ---------------------------------------------------------------------------


def platform_name() -> str:
    """The system this program runs on, as a name from :data:`PLATFORMS`."""
    return "macos" if sys.platform == "darwin" else "linux"


def _platform(platform: str | None) -> str:
    if platform is None:
        return platform_name()
    if platform not in PLATFORMS:
        raise ValueError(f"the platform is one of {', '.join(PLATFORMS)}, not {platform!r:.40}")
    return platform


def settings_file(host: str, platform: str | None = None) -> tuple[Path, str] | None:
    """The one settings file of the application ``host`` on this computer, and its format.

    Returns the absolute path and ``"json"`` or ``"toml"``, or ``None`` for an
    application the gate knows no such file of (and when the home directory
    cannot be told). The path depends on the application, the system and the
    home directory, and on nothing else: for Claude Desktop outside macOS it
    lies under ``$XDG_CONFIG_HOME`` when that names an absolute path, else under
    ``~/.config``. The file need not exist. Nothing is read here.
    """
    system = _platform(platform)
    language = _SETTINGS_FORMAT.get(host)
    if language is None:
        return None
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        return None
    if not home.is_absolute():
        return None
    if host == "claude-desktop":
        if system == "macos":
            folder = home / "Library" / "Application Support" / "Claude"
        else:
            config_home = os.environ.get("XDG_CONFIG_HOME", "")
            base = Path(config_home) if os.path.isabs(config_home) else home / ".config"
            folder = base / "Claude"
        return folder / "claude_desktop_config.json", language
    return home / ".codex" / "config.toml", language


def shown_path(path: str | os.PathLike[str]) -> str:
    """``path`` as an owner reads it: ``~/...`` where it lies in the home directory."""
    text = os.fspath(path)
    try:
        home = os.fspath(Path.home())
    except (RuntimeError, OSError):
        return text
    if home != os.sep and text.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + text[len(home.rstrip(os.sep)) :]
    return text


def _where_here(entry: KnownHost, block: Block, platform: str) -> str | None:
    """The one place of ``block`` that applies on this computer, or ``None``."""
    if not block.where:
        return None
    if entry.id == "claude-desktop":
        place = settings_file(entry.id, platform)
        return None if place is None else shown_path(place[0])
    return block.where[0] if len(block.where) == 1 else None


def open_line(
    path: str | os.PathLike[str], *, state: str, platform: str, language: str, text: str
) -> str | None:
    """One shell line that opens a settings file in the system's text editor, as text.

    ``path`` is the absolute path of the file, ``state`` what a look at it found
    (a state of :mod:`wdva.gate.hostcheck`), ``language`` its format (``"json"``
    or ``"toml"``) and ``text`` the block that belongs into it. Where the file is
    not there the line makes it first, holding ``{}`` for JSON and nothing for
    TOML, and only then: it never changes a file that exists. On macOS it also
    puts ``text`` on the clipboard where the whole text is what gets pasted.
    ``None`` for a state in which there is nothing to open.

    The line is for the owner to read and to run. This program runs none of it.
    """
    system = _platform(platform)
    place = shlex.quote(os.fspath(path))
    editor = "open -e" if system == "macos" else "xdg-open"
    copy = f"; printf %s {shlex.quote(text)} | pbcopy" if system == "macos" else ""
    if state == "no_file":
        first = "echo '{}'" if language == "json" else ":"
        return f'f={place}; [ -e "$f" ] || {first} > "$f"; {editor} "$f"{copy}'
    if state == "empty" or (state == "other" and language == "toml"):
        return f"{editor} {place}{copy}"
    if state in ("other", "differs", "unreadable", "not_read"):
        return f"{editor} {place}"
    return None


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
    elif entry.id == "chatgpt-desktop":
        blocks = (
            Block(
                "Add this to ~/.codex/config.toml",
                "toml",
                _codex_toml(command),
                ("~/.codex/config.toml",),
            ),
        )
        notes = (
            "The ChatGPT desktop app reads the same file as Codex, according to OpenAI's "
            "documentation.",
        )
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


def render(
    host: str,
    *,
    client: str,
    store: str | os.PathLike[str] | None,
    platform: str | None = None,
) -> HostConfig:
    """What connects the application ``host`` to the gate under the name ``client``.

    ``host`` is an id from :data:`KNOWN_HOSTS`. ``platform`` is the system the
    places are chosen for, a name from :data:`PLATFORMS`; ``None`` means the one
    this program runs on. Raises :class:`ValueError` for an unknown host or
    platform and for a client name that cannot be used.
    """
    entry = known_host(host)
    check_client_name(client)
    system = _platform(platform)
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
            platform=system,
        )
    command = host_command(client, store)
    blocks, notes = _blocks(entry, command)
    inner = _mcp_servers_inner(command)
    title, text, desktop = _AFTER[entry.id]
    return HostConfig(
        host=entry.id,
        label=entry.label,
        local_program=True,
        blocks=tuple(
            replace(
                block,
                where_here=_where_here(entry, block, system),
                inner=inner if block.language == "json" else None,
            )
            for block in blocks
        ),
        notes=notes,
        explanation=None,
        source=entry.source,
        read=entry.read,
        command=tuple(command),
        platform=system,
        plain_note=_plain_note(entry),
        after={"title": title, "text": text, "desktop": desktop},
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
