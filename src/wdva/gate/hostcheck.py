# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""A look at one settings file of an agent application: is the gate's entry in it?

This is the only module of the gate that reads a file outside the store. It
reads one file per call: the settings file of the application that was asked
about, at the place :func:`wdva.gate.hostconfig.settings_place` gives for it.
No part of that path comes from a caller. It writes nothing, creates nothing
and starts no program.

What the look is allowed to do:

* The folder of the file is reached without following a link. Below the base
  of the place (the home directory, or the folder a variable names) every
  folder on the way is looked at (``lstat``) and then opened through the one
  above it, and what was opened must be what was looked at. A folder that is a
  symbolic link is not gone through, and nothing behind it is read. The base
  itself is taken as the system resolves it.
* Everything after that is done through the one folder that was opened, so a
  folder that is exchanged while a look runs changes nothing.
* The file is looked at (``lstat``) before it is opened. A file that is not a
  regular file (a symbolic link, a named pipe, a device, a directory) or that
  is larger than :data:`MAX_BYTES` is never opened.
* A file that passed is opened without following a link and without waiting,
  and what was opened is looked at again before a byte is read, so that a file
  that was exchanged in between is not read either. No read can block. It is
  looked at once more after the last byte: a file whose size or times of
  change are then others than before the first byte was written or replaced
  while it was read. Nothing of what was read is used then, and the look says
  so in a word of its own, ``busy``.
* What is found is one word from :data:`STATES`, and for a JSON file without
  the gate's entry one word from :data:`SECTIONS`. An answer to the owner's
  page holds those and nothing else of the file: no key, no value, no message
  of a parser. :func:`look` also hands back the bytes that were read, for the
  one module that writes such a file, :mod:`wdva.gate.hostwrite`; they are for
  that module and belong in no answer.

TOML is read with ``tomllib``, which Python has from version 3.11. On an older
Python a TOML file is not looked at and the answer is ``unsupported``.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wdva.gate import hostconfig

__all__ = [
    "MAX_BYTES",
    "SECTIONS",
    "STATES",
    "FolderProblem",
    "Linked",
    "Look",
    "NoAccess",
    "NoFolder",
    "look",
    "open_folder",
]

MAX_BYTES = 1024 * 1024
"""The largest settings file that is read."""

STATES = (
    "present",
    "present_more",
    "present_as",
    "differs",
    "other",
    "empty",
    "no_file",
    "no_folder",
    "linked",
    "no_access",
    "unreadable",
    "not_read",
    "busy",
    "unsupported",
)
"""What a look can find.

``present``: the gate's entry is there and holds the command this vault needs
and nothing else. ``present_more``: it holds that command and other members
besides. ``present_as``: it holds the command of this vault under another name
that the same file serves. ``differs``: an entry under the gate's name is there
and holds something else. ``other``: the file holds settings and no entry under
the gate's name. ``empty``: the file holds nothing, or no setting. ``no_file``,
``no_folder``: the file, or the folder it belongs in, is not there. ``linked``:
a folder on the way to the file is a symbolic link, and nothing behind it was
read. ``no_access``: the system refused to look at the folder or to open the
file. ``unreadable``: the file could not be read as what it should be.
``not_read``: it is not a regular file, or too large, and was not opened.
``busy``: it was written, cut, or replaced by another file while it was read,
and nothing of what was read is used. ``unsupported``: the gate knows no
settings file of this application, or cannot read its format here.
"""

SECTIONS = ("none", "empty", "filled")
"""For a JSON file without the gate's entry: whether ``mcpServers`` is there and has entries."""

_JSON_SERVERS = "mcpServers"
_TOML_SERVERS = "mcp_servers"
_READ_AT_ONCE = 64 * 1024
_MISSING = object()


class FolderProblem(Exception):
    """The folder of a settings file was not opened. ``state`` is the word for why."""

    state = ""


class NoFolder(FolderProblem):
    """A folder on the way is not there, or is not a folder."""

    state = "no_folder"


class Linked(FolderProblem):
    """A folder on the way is a symbolic link."""

    state = "linked"


class NoAccess(FolderProblem):
    """The system refused to look at a folder on the way, or to open it."""

    state = "no_access"


@dataclass(frozen=True)
class Look:
    """What one look at a settings file found.

    ``state`` is one of :data:`STATES`. ``section`` is one of :data:`SECTIONS`
    for a JSON object in the states ``other`` and ``empty``, else ``None``.
    ``path`` is the settings file (``None`` when ``unsupported``) and
    ``language`` its format. ``command`` is the command the entry must hold.
    ``raw`` is the bytes that were read and ``status`` what the system said
    about the file once they were read, which is what it said before the
    first of them was read: its size and its two times of change were the
    same at both moments. Both are ``None`` when nothing was read.
    ``switched_off``: the entry holds ``enabled`` with the value false.
    ``other_name``: for ``present_as``, the name the entry calls under.
    ``real_folder``: for ``linked``, where the path of the folder leads, or
    ``None`` where the system would not say.

    ``raw`` is whatever the file held, the keys of other tools included. It
    is left out of the text Python prints for a ``Look``, and it belongs in
    no answer and no message.
    """

    state: str
    section: str | None = None
    path: Path | None = None
    language: str | None = None
    command: tuple[str, ...] = ()
    raw: bytes | None = field(default=None, repr=False)
    status: os.stat_result | None = field(default=None, repr=False)
    switched_off: bool = False
    other_name: str | None = None
    real_folder: Path | None = None


class _NotRead(Exception):
    """The file is one that is not opened, or not read once it was opened."""


class _Busy(Exception):
    """The file was written or replaced while it was read."""


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


def open_folder(path: Path, base: Path) -> int:
    """Open the folder of the settings file at ``path`` and return its descriptor.

    ``base`` is the folder the place starts from
    (:func:`wdva.gate.hostconfig.settings_place`); it is opened as the system
    resolves it. Every folder below it on the way to the file is looked at
    without following a link and opened through the one above it, and what was
    opened must be what was looked at. The caller closes the descriptor.

    Raises :class:`NoFolder` when a folder on the way is missing or is no
    folder, :class:`Linked` when one is a symbolic link, and :class:`NoAccess`
    when the system refuses or cannot open a folder this way.
    """
    directory = getattr(os, "O_DIRECTORY", 0)
    held = getattr(os, "O_NOFOLLOW", 0)
    if not directory or not held:  # a system on which a link could not be told from a folder
        raise NoAccess
    flags = os.O_RDONLY | directory | getattr(os, "O_CLOEXEC", 0)
    try:
        parts = path.parent.relative_to(base).parts
    except ValueError:
        raise NoFolder from None
    try:
        descriptor = os.open(base, flags)
    except (FileNotFoundError, NotADirectoryError):
        raise NoFolder from None
    except (OSError, NotImplementedError):
        raise NoAccess from None
    try:
        for part in parts:
            try:
                found = os.lstat(part, dir_fd=descriptor)
            except (FileNotFoundError, NotADirectoryError):
                raise NoFolder from None
            except (OSError, NotImplementedError):
                raise NoAccess from None
            if stat.S_ISLNK(found.st_mode):
                raise Linked
            if not stat.S_ISDIR(found.st_mode):
                raise NoFolder
            try:
                below = os.open(part, flags | held, dir_fd=descriptor)
            except (FileNotFoundError, NotADirectoryError):
                # It was a folder a moment ago. A link in its place is told as one.
                raise _exchanged(part, descriptor) from None
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise Linked from None
                raise NoAccess from None
            os.close(descriptor)
            descriptor = below
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (found.st_dev, found.st_ino):
                raise NoFolder  # the folder that was looked at is not the one that is there
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _exchanged(part: str, descriptor: int) -> FolderProblem:
    """The problem for a folder that was something else by the time it was opened."""
    try:
        now = os.lstat(part, dir_fd=descriptor)
    except OSError:
        return NoFolder()
    return Linked() if stat.S_ISLNK(now.st_mode) else NoFolder()


def _where_it_leads(folder: Path) -> Path | None:
    """Where the path of a folder leads, or ``None`` where the system would not say.

    Asked for a folder on whose way a link was found. A link that is gone, or
    is no link any more, by the time it is read makes the system refuse; that
    is no reason for a look to raise.
    """
    try:
        return Path(os.path.realpath(folder))
    except OSError:
        return None


def _read(name: str, folder: int) -> tuple[bytes, os.stat_result]:
    """The bytes of the regular file ``name`` in the open folder, and its status after the read.

    :data:`MAX_BYTES` at most. Raises :class:`_NotRead` for anything that is
    not such a file, also when it became something else after it was looked
    at, :class:`_Busy` when it was written while it was read, and
    :class:`OSError` when the system refuses.

    Written while it was read: its size or one of its two times of change is
    another after the last byte than before the first. That is also what the
    system says of a file whose name was given to another file in that time
    (the one that was opened has one name fewer, which is a change). Without
    that comparison a program that rewrote the file in place between two reads, or
    between the last read and the status handed back, would go unseen: the
    bytes would be a mix of two versions, or the status that of a version
    that was not read, and a writer that later compares with this status
    would take the file for the one it read.
    """
    flags = os.O_RDONLY
    for flag in ("O_NOFOLLOW", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC"):
        flags |= getattr(os, flag, 0)
    try:
        descriptor = os.open(name, flags, dir_fd=folder)
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
            raise _Busy
        after = os.fstat(descriptor)
        if after.st_size != size:  # it was cut or grew while it was read
            raise _Busy
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        ):  # it was written while it was read
            raise _Busy
        return b"".join(parts), after
    finally:
        os.close(descriptor)


def _same(entry: Any, command: Sequence[str]) -> bool:
    """Whether an entry of a settings file holds exactly this command and these arguments."""
    return (
        isinstance(entry, dict)
        and entry.get("command") == command[0]
        and entry.get("args") == list(command[1:])
    )


def _judge(
    data: Any, servers_key: str, command: Sequence[str], others: Mapping[str, Sequence[str]]
) -> Look:
    """The state of a parsed settings file, with the section and what else was seen of the entry.

    ``others`` holds, by name, the commands of this vault under the other
    names the same file serves.
    """
    if not isinstance(data, dict):
        return Look("unreadable")
    servers = data.get(servers_key, _MISSING)
    if servers is _MISSING:
        return Look("other" if data else "empty", "none")
    if not isinstance(servers, dict):
        return Look("unreadable")
    if not servers and len(data) == 1:
        return Look("empty", "empty")
    entry = servers.get(hostconfig.SERVER_KEY, _MISSING)
    if entry is _MISSING:
        return Look("other", "filled" if servers else "empty")
    off = isinstance(entry, dict) and entry.get("enabled") is False
    if _same(entry, command):
        return Look("present" if len(entry) == 2 else "present_more", switched_off=off)
    for name, other in others.items():
        if _same(entry, other):
            return Look("present_as", switched_off=off, other_name=name)
    return Look("differs", switched_off=off)


def _look(
    name: str,
    folder: int,
    language: str,
    command: Sequence[str],
    others: Mapping[str, Sequence[str]],
) -> Look:
    """Look at the settings file ``name`` in the open folder."""
    try:
        found = os.lstat(name, dir_fd=folder)
    except (FileNotFoundError, NotADirectoryError):
        return Look("no_file")
    except PermissionError:
        return Look("no_access")
    except (OSError, NotImplementedError):
        return Look("unreadable")
    if not stat.S_ISREG(found.st_mode) or found.st_size > MAX_BYTES:
        return Look("not_read")
    try:
        raw, status = _read(name, folder)
    except _NotRead:
        return Look("not_read")
    except _Busy:
        return Look("busy")
    except PermissionError:
        return Look("no_access")
    except (OSError, NotImplementedError):
        return Look("unreadable")

    def read(state: str, judged: Look | None = None) -> Look:
        if judged is None:
            return Look(state, raw=raw, status=status)
        section = judged.section if language == "json" else None
        return Look(
            state,
            section if state in ("other", "empty") else None,
            raw=raw,
            status=status,
            switched_off=judged.switched_off,
            other_name=judged.other_name,
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return read("unreadable")
    if text.startswith("﻿"):  # a byte-order mark, which neither format has
        return read("unreadable")
    if not text.strip():
        return read("empty")
    try:
        if language == "json":
            judged = _judge(_json_loads(text), _JSON_SERVERS, command, others)
        else:
            loads = _toml_loads()
            if loads is None:
                return Look("unsupported")
            judged = _judge(loads(text), _TOML_SERVERS, command, others)
    except (ValueError, RecursionError):
        return read("unreadable")
    return read(judged.state, judged)


def look(
    host: str,
    *,
    client: str,
    store: str | os.PathLike[str] | None,
    folder_fd: int | None = None,
    platform: str | None = None,
) -> Look:
    """Look at the settings file of the application ``host``, and keep what was read.

    ``client`` and ``store`` are what :func:`wdva.gate.hostconfig.render` takes:
    the entry looked for is the one that render writes for them. ``folder_fd``
    is the descriptor of the file's folder when the caller holds it open
    (:func:`open_folder`); without it the folder is opened here and closed
    again.

    Raises :class:`ValueError` where :func:`~wdva.gate.hostconfig.render` does.
    Never raises for anything about the file or its folder.
    """
    config = hostconfig.render(host, client=client, store=store, platform=platform)
    command = config.command
    place = hostconfig.settings_place(config.host, config.platform)
    if place is None or not command:
        return Look("unsupported", command=command)
    path, language, base, _ = place
    if language == "toml" and _toml_loads() is None:
        return Look("unsupported", command=command)
    others = {
        name: hostconfig.host_command(name, store)
        for name in hostconfig.same_file(config.host)
        if name != client
    }
    if folder_fd is None:
        try:
            folder = open_folder(path, base)
        except FolderProblem as problem:
            real = _where_it_leads(path.parent) if problem.state == "linked" else None
            return Look(
                problem.state, path=path, language=language, command=command, real_folder=real
            )
        try:
            found = _look(path.name, folder, language, command, others)
        finally:
            os.close(folder)
    else:
        found = _look(path.name, folder_fd, language, command, others)
    if found.state == "unsupported":
        return Look("unsupported", command=command)
    return Look(
        found.state,
        found.section,
        path,
        language,
        command,
        found.raw,
        found.status,
        found.switched_off,
        found.other_name,
    )
