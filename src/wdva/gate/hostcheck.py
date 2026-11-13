# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""A look at one settings file of an agent application: is the gate's entry in it?

This is the only module of the gate that reads a file outside the store. It
reads one file per call: the settings file of the application that was asked
about, at the place :func:`wdva.gate.hostconfig.settings_file` gives for it.
No part of that path comes from a caller. It writes nothing, creates nothing
and starts no program.

What the look is allowed to do:

* The path is looked at (``lstat``) before anything is opened. A file that is
  not a regular file (a symbolic link, a named pipe, a device, a directory) or
  that is larger than :data:`MAX_BYTES` is never opened.
* A file that passed is opened without following a link and without waiting,
  and what was opened is looked at again before a byte is read, so that a file
  that was exchanged in between is not read either. No read can block.
* The answer is one word from :data:`STATES`, and for a JSON file that holds
  other settings one word from :data:`SECTIONS`. Nothing else of the file
  reaches the answer: no key, no value, no message of a parser.

TOML is read with ``tomllib``, which Python has from version 3.11. On an older
Python a TOML file is not looked at and the answer is ``unsupported``.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from wdva.gate import hostconfig

__all__ = ["MAX_BYTES", "SECTIONS", "STATES", "check"]

MAX_BYTES = 1024 * 1024
"""The largest settings file that is read."""

STATES = (
    "present",
    "differs",
    "other",
    "empty",
    "no_file",
    "no_folder",
    "unreadable",
    "not_read",
    "unsupported",
)
"""What a look can find.

``present``: the gate's entry is there, with the command this vault needs.
``differs``: an entry under the gate's name is there and holds something else.
``other``: the file holds settings and no entry under the gate's name.
``empty``: the file holds nothing, or no setting. ``no_file``, ``no_folder``:
the file, or the folder it belongs in, is not there. ``unreadable``: the file
could not be read as what it should be. ``not_read``: it is not a regular file,
or too large, and was not opened. ``unsupported``: the gate knows no settings
file of this application, or cannot read its format here.
"""

SECTIONS = ("none", "empty", "filled")
"""For a JSON file in the state ``other``: whether ``mcpServers`` is there and has entries."""

_JSON_SERVERS = "mcpServers"
_TOML_SERVERS = "mcp_servers"
_READ_AT_ONCE = 64 * 1024
_MISSING = object()


class _NotRead(Exception):
    """The file is one that is not opened, or not read once it was opened."""


def _no_constant(name: str) -> Any:
    raise ValueError("not JSON")  # NaN and Infinity, which JSON does not have


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("a key occurs twice")  # readers disagree about which one counts
        found[key] = value
    return found


def _json_loads(text: str) -> Any:
    return json.loads(text, parse_constant=_no_constant, object_pairs_hook=_no_duplicates)


def _toml_loads() -> Callable[[str], Any] | None:
    """The reader for TOML, or ``None`` on a Python that has none.

    ``tomllib`` is imported here, where it is used, so that this module can be
    imported on a Python that does not have it.
    """
    if sys.version_info >= (3, 11):
        try:
            import tomllib
        except ImportError:
            return None
        return tomllib.loads
    return None


def _read(path: Path) -> bytes:
    """The bytes of the regular file at ``path``, :data:`MAX_BYTES` at most.

    Raises :class:`_NotRead` for anything that is not such a file, also when it
    became something else after it was looked at, and :class:`OSError` when the
    system refuses.
    """
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC"):
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):  # a link took the file's place
            raise _NotRead from None
        raise
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_BYTES:
            raise _NotRead
        parts: list[bytes] = []
        size = 0
        while size <= MAX_BYTES:
            part = os.read(descriptor, min(_READ_AT_ONCE, MAX_BYTES + 1 - size))
            if not part:
                break
            parts.append(part)
            size += len(part)
        if size > MAX_BYTES:  # it grew while it was read
            raise _NotRead
        return b"".join(parts)
    finally:
        os.close(descriptor)


def _same(entry: Any, command: Sequence[str]) -> bool:
    """Whether an entry of a settings file holds exactly this command and these arguments."""
    return (
        isinstance(entry, dict)
        and entry.get("command") == command[0]
        and entry.get("args") == list(command[1:])
    )


def _judge(data: Any, servers_key: str, command: Sequence[str]) -> tuple[str, str | None]:
    """The state of a parsed settings file, and which of :data:`SECTIONS` it has."""
    if not isinstance(data, dict):
        return "unreadable", None
    servers = data.get(servers_key, _MISSING)
    if servers is _MISSING:
        return ("other", "none") if data else ("empty", None)
    if not isinstance(servers, dict):
        return "unreadable", None
    if not servers and len(data) == 1:
        return "empty", None
    entry = servers.get(hostconfig.SERVER_KEY, _MISSING)
    if entry is _MISSING:
        return "other", "filled" if servers else "empty"
    return ("present" if _same(entry, command) else "differs"), None


def _look(path: Path, language: str, command: Sequence[str]) -> tuple[str, str | None]:
    """Look at the settings file at ``path``: a state, and a section for JSON in ``other``."""
    try:
        folder = os.stat(path.parent)
    except (FileNotFoundError, NotADirectoryError):
        return "no_folder", None
    except OSError:
        return "unreadable", None
    if not stat.S_ISDIR(folder.st_mode):
        return "no_folder", None
    try:
        found = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "no_file", None
    except OSError:
        return "unreadable", None
    if not stat.S_ISREG(found.st_mode) or found.st_size > MAX_BYTES:
        return "not_read", None
    try:
        raw = _read(path)
    except _NotRead:
        return "not_read", None
    except OSError:
        return "unreadable", None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "unreadable", None
    if not text.strip():
        return "empty", None
    try:
        if language == "json":
            state, section = _judge(_json_loads(text), _JSON_SERVERS, command)
            return state, section if state == "other" else None
        loads = _toml_loads()
        if loads is None:
            return "unsupported", None
        return _judge(loads(text), _TOML_SERVERS, command)[0], None
    except (ValueError, RecursionError):
        return "unreadable", None


def check(
    host: str,
    *,
    client: str,
    store: str | os.PathLike[str] | None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Look at the settings file of the application ``host`` for the gate's entry.

    ``client`` and ``store`` are what :func:`wdva.gate.hostconfig.render` takes:
    the entry looked for is the one that render writes for them. Returns
    ``state`` (one of :data:`STATES`), ``path`` and ``folder`` (the file and its
    folder as an owner reads them, ``None`` when ``unsupported``), ``open_line``
    (:func:`wdva.gate.hostconfig.open_line` for what was found, or ``None``)
    and ``section`` (one of :data:`SECTIONS` for a JSON file in the state
    ``other``, else ``None``).

    Raises :class:`ValueError` where :func:`~wdva.gate.hostconfig.render` does.
    Never raises for anything about the file.
    """
    config = hostconfig.render(host, client=client, store=store, platform=platform)
    unsupported: dict[str, Any] = {
        "state": "unsupported",
        "path": None,
        "folder": None,
        "open_line": None,
        "section": None,
    }
    place = hostconfig.settings_file(config.host, config.platform)
    if place is None or not config.command:
        return unsupported
    path, language = place
    block = next((b for b in config.blocks if b.language == language and b.where), None)
    if block is None or (language == "toml" and _toml_loads() is None):
        return unsupported
    state, section = _look(path, language, config.command)
    if state == "unsupported":
        return unsupported
    return {
        "state": state,
        "path": hostconfig.shown_path(path),
        "folder": hostconfig.shown_path(path.parent),
        "open_line": hostconfig.open_line(
            path, state=state, platform=config.platform, language=language, text=block.text
        ),
        "section": section,
    }
