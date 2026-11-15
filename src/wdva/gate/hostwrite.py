# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The gate's own entry, put into the settings file of an agent application or taken out of it.

This is the one module of the gate that writes a file of another program. It
writes one file per call, the settings file of one of the three applications
in :data:`WRITABLE`, at the place :func:`wdva.gate.hostconfig.settings_place`
gives for it, and up to one copy of that file beside it. No part of a path
comes from a caller. It makes no folder and starts no program.

The rules it keeps:

* **Read first.** Nothing is written that :mod:`wdva.gate.hostcheck` did not
  read a moment before. A file that cannot be parsed, is a link or a special
  file, is larger than :data:`MAX_BYTES`, has more than one name or belongs to
  another account is never written, and neither is one that was written or
  replaced while it was read. A folder on the way that is a link is not gone
  through. A folder that is not there is not made. (One second name is
  no reason to call a file unread: the temporary name under which an earlier
  call of this module made it, where that call was cut before it could take
  the name away. Such a file is looked at like any other, and is written
  once that name is gone.)
* **Only the entry.** The settings file is edited as text. The entry named
  after the product is put in, pointed at this vault, or taken out; every
  other byte of a JSON file and every other line of a TOML file stays. The
  new text is then parsed again, and with the entry left aside it must hold
  what the old text held: every key, value and type, in the same order. (One
  thing of the order is left aside, where the entry is taken out of a TOML
  file: the place of the table of servers among the tables of the top. The
  reader of TOML puts that table where it first meets one of its tables,
  which may have been the entry.) Text that cannot be edited by these rules
  raises :class:`ByHand`, and nothing is written.
* **Only what was seen.** The caller says which state the owner was shown
  (``expect``). Another state stops the call, and so does a file that is
  another one, or was changed, by the time it would be replaced. A file that
  was changed less than :data:`QUIET_SECONDS` ago is not written either:
  somebody may be saving it.
* **A copy before each change.** The bytes that were read are kept beside the
  file, as :data:`FIRST_COPY` when the file held no entry of the product and
  neither copy lies there yet, else as :data:`LAST_COPY`. The first copy is
  never overwritten. A file that was not there has no copy.
* **One step.** The new bytes go to a temporary file in the same folder and
  take the file's place by one ``replace``; a file that was not there is put
  there by ``link``, which fails if something lies there. Every file is named
  relative to the one folder that was opened, so a folder that is exchanged
  while a call runs changes nothing.
* **The record first.** Right before the file is replaced the caller's
  ``record`` is called, inside the caller's ``section``. If that raises,
  nothing was changed. If the change fails after it, ``record`` is called
  again to say so.
* **Read back.** After the change the file is read again through the same
  reader, and must hold what was meant. Where the system does not let it be
  read then, the name is looked at instead: if it still leads to the file
  this call put there, the change was made, and the answer says that it was
  not read back.
* **A command that lasts.** :func:`write_problem` refuses, without looking at
  the settings file, a command whose Python or vault lies in a folder for
  temporary files, in a place anybody may write or one of another account,
  as its path is written and where that path really leads;
  for Grok Build a command that holds ``${``, which Grok Build would replace;
  and the file of the ChatGPT desktop app while ``CODEX_HOME`` is set.

Whether the application runs is not looked up, and nothing here was tried
with one of the applications.

TOML is read with ``tomllib``, which Python has from version 3.11; on an older
Python a TOML file is ``unsupported``. What a system has only in part
(``os.getuid``, ``os.fchmod``, ``O_NOFOLLOW``, the ``dir_fd`` forms) is asked
for inside functions, so that the package imports everywhere.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from typing import Any

from wdva.gate import brand, hostcheck, hostconfig

__all__ = [
    "FIRST_COPY",
    "LAST_COPY",
    "LEFTOVER_SECONDS",
    "MAX_BYTES",
    "PROBLEMS",
    "QUIET_SECONDS",
    "REFUSALS",
    "WRITABLE",
    "ByHand",
    "Section",
    "connect",
    "disconnect",
    "json_with_entry",
    "json_without_entry",
    "look",
    "toml_with_entry",
    "toml_without_entry",
    "write_problem",
]

WRITABLE = ("claude-desktop", "chatgpt-desktop", "grok-build")
"""The applications whose settings file this module writes."""

FIRST_COPY = ".before-" + brand.SLUG
"""What follows the name of a settings file in the name of its first copy: the file as it
was before the product first changed it."""

LAST_COPY = ".last-" + brand.SLUG
"""What follows the name of a settings file in the name of its latest copy: the file as it
was before the latest change."""

MAX_BYTES = hostcheck.MAX_BYTES
"""The largest settings file that is read, and the largest that is written."""

QUIET_SECONDS = 5
"""A file that was changed less than this many seconds ago is not written."""

LEFTOVER_SECONDS = 60
"""A temporary file of this module that is older than this many seconds is removed."""

PROBLEMS = ("expands", "unstable", "moved")
"""What keeps an entry from being written, found without the file (:func:`write_problem`)."""

REFUSALS = ("recent", "by_hand", "no_copy", "changed", "failed")
"""What a write can find, besides the words of :data:`wdva.gate.hostcheck.STATES`.

``recent``: the file was changed a moment ago, or still has the temporary name
of a call that was cut less than :data:`LEFTOVER_SECONDS` ago beside its own.
``by_hand``: the text cannot be edited by the rules of this module.
``no_copy``: the copy has no place, or could not be written. ``changed``: the
file is not in the state the owner was shown, or is another file by now.
``failed``: the file could not be replaced, or did not hold what was meant
afterwards.
"""

Section = Callable[[], AbstractContextManager[Callable[[str], None]]]
"""What a caller hands in as ``section``: called, it gives a context manager around the few
steps in which the record is written and the file is replaced. Entered, that yields
``record(action)``."""

_SLUG = hostconfig.SERVER_KEY
_JSON_SERVERS = "mcpServers"
_TOML_SERVERS = "mcp_servers"
_OURS = ("present", "present_more")  # the entry is there and is this vault's
_NO_ENTRY = ("other", "empty")  # the file is there and holds no entry of that name
_FOR_A_FILE = {  # what a disconnect that leaves the file alone says about it
    "differs": "differs",
    "present_as": "differs",
    "other": "no_entry",
    "empty": "no_entry",
    "no_file": "no_entry",
    "no_folder": "no_entry",
}
_CAN_BE_WRITTEN = ("no_file", "empty", "other", "differs", "present_as")  # by a connect
_TEMPORARY_FOLDERS = ("/tmp", "/var/tmp", "/dev/shm")  # noqa: S108  (named, never written to)
_JSON_SPACE = " \t\n\r"
_FORMATS = {"json": "JSON", "toml": "TOML"}


class ByHand(Exception):
    """The file can be read, and cannot be changed by the rules of this module."""


# ---------------------------------------------------------------------------
# Comparing what two texts hold
# ---------------------------------------------------------------------------


def _equal(left: Any, right: Any) -> bool:
    """Whether two parsed values are the same: values, types and the order of keys.

    ``1`` is not ``1.0`` and ``true`` is not ``1``; a date is not a string.
    Two values that are both not-a-number count as equal, although Python
    says they are not: a file that holds one could otherwise never be
    written. The walk keeps a list of its own instead of calling itself, so
    deep nesting costs no stack.
    """
    waiting = [(left, right)]
    while waiting:
        one, other = waiting.pop()
        if type(one) is not type(other):
            return False
        if isinstance(one, dict):
            if list(one) != list(other):
                return False
            waiting.extend((one[key], other[key]) for key in one)
        elif isinstance(one, list):
            if len(one) != len(other):
                return False
            waiting.extend(zip(one, other, strict=True))
        elif one != other and not (isinstance(one, float) and one != one and other != other):
            return False
    return True


def _rest(
    data: Any, servers_key: str, *, entry: str, drop_empty: bool, servers_last: bool = False
) -> Any:
    """A parsed settings file without what this module may change, for a comparison.

    ``entry`` says what is taken out of the entry of the product: ``"whole"``
    the entry, ``"command"`` its ``command`` and ``args``. With ``drop_empty``
    a table of servers that holds nothing after that is left out as well.
    With ``servers_last`` the table of servers is made the last key of the
    top, so that its place among the other keys does not count in the
    comparison; the order of every other key still does, and so does the
    order of the servers. Nothing of ``data`` is changed.
    """
    if not isinstance(data, dict):
        raise ByHand
    rest = dict(data)
    servers = rest.get(servers_key)
    if servers is None and servers_key not in rest:
        return rest
    if not isinstance(servers, dict):
        raise ByHand
    servers = dict(servers)
    if _SLUG in servers:
        if entry == "whole":
            del servers[_SLUG]
        else:
            ours = servers[_SLUG]
            if not isinstance(ours, dict):
                raise ByHand
            servers[_SLUG] = {
                key: value for key, value in ours.items() if key not in ("command", "args")
            }
    if drop_empty and not servers:
        del rest[servers_key]
    else:
        if servers_last:
            del rest[servers_key]
        rest[servers_key] = servers
    return rest


def _check_entry(after: Any, servers_key: str, command: Sequence[str], *, alone: bool) -> None:
    """The entry of a new text holds this command; with ``alone``, nothing else."""
    servers = after.get(servers_key) if isinstance(after, dict) else None
    ours = servers.get(_SLUG) if isinstance(servers, dict) else None
    if not isinstance(ours, dict):
        raise ByHand
    if not _equal(ours.get("command"), command[0]) or not _equal(
        ours.get("args"), list(command[1:])
    ):
        raise ByHand
    if alone and list(ours) != ["command", "args"]:
        raise ByHand


def _check_no_entry(after: Any, servers_key: str) -> None:
    servers = after.get(servers_key) if isinstance(after, dict) else None
    if isinstance(servers, dict) and _SLUG in servers:
        raise ByHand


def _command(command: Sequence[str]) -> tuple[str, ...]:
    parts = tuple(command)
    if not parts or not all(isinstance(part, str) for part in parts):
        raise ByHand
    return parts


# ---------------------------------------------------------------------------
# JSON, edited as text
# ---------------------------------------------------------------------------

_Member = tuple[str, int, int, int]  # the key, where it starts, where its value starts and ends


def _json_loads(text: str) -> Any:
    try:
        return hostcheck._json_loads(text)
    except ValueError:
        raise ByHand from None


def _past_space(text: str, at: int) -> int:
    while at < len(text) and text[at] in _JSON_SPACE:
        at += 1
    return at


def _past_string(text: str, at: int) -> int:
    """The place after the string that starts at ``at``."""
    if at >= len(text) or text[at] != '"':
        raise ByHand
    at += 1
    while at < len(text):
        char = text[at]
        if char == "\\":
            at += 2
        elif char == '"':
            return at + 1
        else:
            at += 1
    raise ByHand


def _past_value(text: str, at: int) -> int:
    """The place after the value that starts at ``at``.

    A string, what stands between a bracket and the one that closes it, or
    anything else up to the next comma, bracket or white space. Brackets are
    counted and not judged: the text has passed a strict reader before.
    """
    if at >= len(text):
        raise ByHand
    if text[at] == '"':
        return _past_string(text, at)
    if text[at] in "{[":
        depth = 0
        while at < len(text):
            char = text[at]
            if char == '"':
                at = _past_string(text, at)
                continue
            if char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    return at + 1
            at += 1
        raise ByHand
    end = at
    while end < len(text) and text[end] not in _JSON_SPACE and text[end] not in ',]}"{[:':
        end += 1
    if end == at:
        raise ByHand
    return end


def _members(text: str, at: int) -> tuple[list[_Member], int]:
    """The members of the object whose ``{`` is at ``at``, and the place of its ``}``.

    The text is walked once and nothing is called for a nested value, so deep
    nesting costs no stack. Anything unexpected raises :class:`ByHand`.
    """
    if at >= len(text) or text[at] != "{":
        raise ByHand
    members: list[_Member] = []
    at = _past_space(text, at + 1)
    if at < len(text) and text[at] == "}":
        return members, at
    while True:
        key_at = at
        key_end = _past_string(text, key_at)
        try:
            key = json.loads(text[key_at:key_end])
        except ValueError:
            raise ByHand from None
        if not isinstance(key, str):
            raise ByHand
        at = _past_space(text, key_end)
        if at >= len(text) or text[at] != ":":
            raise ByHand
        value_at = _past_space(text, at + 1)
        value_end = _past_value(text, value_at)
        members.append((key, key_at, value_at, value_end))
        at = _past_space(text, value_end)
        if at >= len(text):
            raise ByHand
        if text[at] == "}":
            return members, at
        if text[at] != ",":
            raise ByHand
        at = _past_space(text, at + 1)


def _member(members: Sequence[_Member], key: str) -> _Member | None:
    found = [member for member in members if member[0] == key]
    if len(found) > 1:
        raise ByHand
    return found[0] if found else None


def _raw(text: str, members: Sequence[_Member], *, without: Sequence[str] = ()) -> list[Any]:
    """Each member as it is written: its key and the text of its value."""
    return [(key, text[start:end]) for key, _, start, end in members if key not in without]


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _indentation(text: str) -> str:
    """The indentation of a JSON text: the white space before its first indented line.

    A tab, or that many spaces, eight at most; two spaces for a text without
    an indented line.
    """
    for line in text.split("\n"):
        if not line.strip() or line[0] not in " \t":
            continue
        if line[0] == "\t":
            return "\t"
        return " " * min(len(line) - len(line.lstrip(" ")), 8)
    return "  "


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_json_string(value) for value in values) + "]"


def _json_member(command: Sequence[str], indent: str, ending: str) -> str:
    """The entry as a member of ``mcpServers``, four lines, without a comma."""
    return ending.join(
        (
            f"{indent * 2}{_json_string(_SLUG)}: {{",
            f'{indent * 3}"command": {_json_string(command[0])},',
            f'{indent * 3}"args": {_json_list(command[1:])}',
            f"{indent * 2}}}",
        )
    )


def _json_layout(text: str) -> tuple[list[_Member], int, list[_Member] | None, int, int]:
    """Where things stand in a JSON settings file.

    The members of the top object and the place of its ``}``; the members of
    ``mcpServers``, or ``None`` where there is none; and the places of the two
    braces of ``mcpServers``.
    """
    top, top_close = _members(text, _past_space(text, 0))
    if _past_space(text, top_close + 1) != len(text):
        raise ByHand
    section = _member(top, _JSON_SERVERS)
    if section is None:
        return top, top_close, None, -1, -1
    servers, servers_close = _members(text, section[2])
    if servers_close + 1 != section[3]:
        raise ByHand
    return top, top_close, servers, section[2], servers_close


def _json_others(text: str) -> tuple[list[Any], list[Any], list[Any]]:
    """Everything of a JSON settings file that this module leaves alone, as it is written.

    The members of the top object but ``mcpServers``; the members of
    ``mcpServers`` but the entry; and the members of the entry but ``command``
    and ``args``.
    """
    top, _, servers, _, _ = _json_layout(text)
    entry = None if servers is None else _member(servers, _SLUG)
    inside: list[Any] = []
    if entry is not None and text[entry[2]] == "{":
        inside = _raw(text, _members(text, entry[2])[0], without=("command", "args"))
    return (
        _raw(text, top, without=(_JSON_SERVERS,)),
        [] if servers is None else _raw(text, servers, without=(_SLUG,)),
        inside,
    )


def _check_put_in(old: str, new: str) -> None:
    """``new`` is ``old`` with one piece put in: no character of ``old`` is gone or changed."""
    start = 0
    limit = min(len(old), len(new))
    while start < limit and old[start] == new[start]:
        start += 1
    rest = len(old) - start
    if len(new) - start < rest or new[len(new) - rest :] != old[start:]:
        raise ByHand


def _check_between(old: str, new: str, opening: int, closing: int) -> None:
    """``new`` is ``old`` with other text between two braces that held white space only."""
    if old[opening + 1 : closing].strip(_JSON_SPACE):
        raise ByHand
    if not new.startswith(old[: opening + 1]) or not new.endswith(old[closing:]):
        raise ByHand
    if len(new) < opening + 1 + len(old) - closing:
        raise ByHand


def _entry_span(text: str) -> tuple[int, int]:
    """Where the value of the entry starts and ends in a JSON settings file that has one."""
    servers = _json_layout(text)[2]
    entry = None if servers is None else _member(servers, _SLUG)
    if entry is None:
        raise ByHand
    return entry[2], entry[3]


def json_with_entry(text: str, command: Sequence[str]) -> str:
    """``text``, a JSON settings file, with the product's entry holding ``command``.

    The entry is added where there is none: as the last member of
    ``mcpServers``, which is added as the last member of the file where it is
    missing. An entry that is there keeps its place and whatever else it
    holds; the values of ``command`` and ``args`` are replaced. Every byte
    outside the one or two places that are edited stays as it is. No file is
    touched.

    Raises :class:`ByHand` for a text that cannot be changed this way: one
    that is not a JSON object as a strict reader reads it, an ``mcpServers``
    that is no object, an entry that is no object or has no ``command``, and
    a result that does not hold what the text held before.
    """
    command = _command(command)
    ending, indent = _line_ending(text), _indentation(text)
    member = _json_member(command, indent, ending)
    section = f'{indent}"{_JSON_SERVERS}": {{{ending}{member}{ending}{indent}}}'
    if not text.strip():
        new = f"{{{ending}{section}{ending}}}{ending}"
        _check_entry(_json_loads(new), _JSON_SERVERS, command, alone=True)
        return new
    before = _json_loads(text)
    if not isinstance(before, dict) or not isinstance(before.get(_JSON_SERVERS, {}), dict):
        raise ByHand
    top, top_close, servers, servers_open, servers_close = _json_layout(text)
    had_entry = False
    if servers is None:
        if top:
            at = top[-1][3]
            new = f"{text[:at]},{ending}{section}{text[at:]}"
            _check_put_in(text, new)
        else:
            start = _past_space(text, 0)
            new = f"{text[: start + 1]}{ending}{section}{ending}{text[top_close:]}"
            _check_between(text, new, start, top_close)
    elif _member(servers, _SLUG) is None:
        if servers:
            at = servers[-1][3]
            new = f"{text[:at]},{ending}{member}{text[at:]}"
            _check_put_in(text, new)
        else:
            new = (
                f"{text[: servers_open + 1]}{ending}{member}{ending}{indent}{text[servers_close:]}"
            )
            _check_between(text, new, servers_open, servers_close)
    else:
        had_entry = True
        entry = _member(servers, _SLUG)
        if entry is None or text[entry[2]] != "{":
            raise ByHand
        inside, _ = _members(text, entry[2])
        program = _member(inside, "command")
        if program is None:
            raise ByHand  # an entry of another kind, such as one with a `url`
        arguments = _member(inside, "args")
        if arguments is None:
            put = f'{_json_string(command[0])}, "args": {_json_list(command[1:])}'
            edits = [(program[2], program[3], put)]
        else:
            edits = [
                (program[2], program[3], _json_string(command[0])),
                (arguments[2], arguments[3], _json_list(command[1:])),
            ]
        new = text
        for start, end, put in sorted(edits, reverse=True):
            new = new[:start] + put + new[end:]
        # Outside the value of the entry, every character is the old one.
        was_at, now_at = _entry_span(text), _entry_span(new)
        if text[: was_at[0]] != new[: now_at[0]] or text[was_at[1] :] != new[now_at[1] :]:
            raise ByHand
    after = _json_loads(new)
    _check_entry(after, _JSON_SERVERS, command, alone=not had_entry)
    added = servers is None
    kind = "command" if had_entry else "whole"
    if not _equal(
        _rest(before, _JSON_SERVERS, entry=kind, drop_empty=False),
        _rest(after, _JSON_SERVERS, entry=kind, drop_empty=added),
    ):
        raise ByHand
    was, now = _json_others(text), _json_others(new)
    if was[:2] != now[:2] or (had_entry and was[2] != now[2]):
        raise ByHand
    return new


def json_without_entry(text: str) -> str:
    """``text``, a JSON settings file, without the product's entry.

    The member is taken out with the comma that belonged to it. An
    ``mcpServers`` that holds nothing afterwards stays, as ``{}``. Every other
    byte stays as it is. No file is touched.

    Raises :class:`ByHand` where :func:`json_with_entry` does, and for a text
    without the entry.
    """
    before = _json_loads(text)
    if not isinstance(before, dict) or not isinstance(before.get(_JSON_SERVERS, {}), dict):
        raise ByHand
    _, _, servers, servers_open, servers_close = _json_layout(text)
    if servers is None:
        raise ByHand
    names = [member[0] for member in servers]
    if names.count(_SLUG) != 1:
        raise ByHand
    index = names.index(_SLUG)
    entry = servers[index]
    if len(servers) == 1:
        new = text[: servers_open + 1] + text[servers_close:]
    elif index > 0:
        new = text[: servers[index - 1][3]] + text[entry[3] :]
    else:
        new = text[: entry[1]] + text[servers[1][1] :]
    _check_put_in(new, text)  # the old text is the new one with the piece that went
    after = _json_loads(new)
    _check_no_entry(after, _JSON_SERVERS)
    if not _equal(
        _rest(before, _JSON_SERVERS, entry="whole", drop_empty=False),
        _rest(after, _JSON_SERVERS, entry="whole", drop_empty=False),
    ):
        raise ByHand
    if _json_others(text)[:2] != _json_others(new)[:2]:
        raise ByHand
    return new


# ---------------------------------------------------------------------------
# TOML, edited as lines
# ---------------------------------------------------------------------------

_Line = tuple[str, str]  # what a line holds, and how it ends: "\n", "\r\n" or "" at the end

_TOML_KEY = rf"""(?:{re.escape(_SLUG)}|"{re.escape(_SLUG)}"|'{re.escape(_SLUG)}')"""
_TOML_PATH = rf"[ \t]*{_TOML_SERVERS}[ \t]*\.[ \t]*{_TOML_KEY}[ \t]*"
_OUR_HEADER = re.compile(rf"[ \t]*\[{_TOML_PATH}\][ \t]*(?:#.*)?")
_HEADER_BELOW = re.compile(rf"[ \t]*\[\[?{_TOML_PATH}\.")
_HEADER = re.compile(r"[ \t]*\[")
_COMMAND_LINE = re.compile(r"([ \t]*)command[ \t]*=")
_ARGS_LINE = re.compile(r"([ \t]*)args[ \t]*=")
_BLANK_OR_COMMENT = re.compile(r"[ \t]*(?:#.*)?")


def _toml_loads(text: str) -> Any:
    loads = hostcheck._toml_loads()
    if loads is None:
        raise ByHand
    try:
        return loads(text)
    except ValueError:
        raise ByHand from None


def _lines(text: str) -> list[_Line]:
    """The lines of a text, each with its own ending. A line ends with a line feed only."""
    pieces = text.split("\n")
    lines: list[_Line] = []
    for piece in pieces[:-1]:
        lines.append((piece[:-1], "\r\n") if piece.endswith("\r") else (piece, "\n"))
    if pieces[-1]:
        lines.append((pieces[-1], ""))
    return lines


def _joined(lines: Sequence[_Line]) -> str:
    return "".join(content + ending for content, ending in lines)


def _toml_block(command: Sequence[str]) -> list[str]:
    """The three lines of the entry, as the text an owner would paste has them."""
    return hostconfig._codex_toml(command).split("\n")


def _our_header(lines: Sequence[_Line]) -> int:
    """The number of the one line that opens the entry's table. :class:`ByHand` without one."""
    found = [number for number, (content, _) in enumerate(lines) if _OUR_HEADER.fullmatch(content)]
    if len(found) != 1:
        raise ByHand
    return found[0]


def _one_line(lines: Sequence[_Line], numbers: Sequence[int], pattern: re.Pattern[str]) -> int:
    """Which of the lines ``numbers`` sets a key, or ``-1``. :class:`ByHand` for more than one."""
    found = [number for number in numbers if pattern.match(lines[number][0])]
    if len(found) > 1:
        raise ByHand
    return found[0] if found else -1


def _check_lines(
    old: Sequence[_Line], new: Sequence[_Line], gone: Sequence[int], added: Sequence[str]
) -> None:
    """Every old line that did not go is in the new text, unchanged and in order.

    ``gone`` numbers the old lines that were replaced or removed. Besides the
    lines that stayed, the new text may hold the lines ``added`` and nothing
    else. A line that stayed keeps its ending; only a last line that had
    none may have gained one.
    """
    left = set(gone)
    kept = [line for number, line in enumerate(old) if number not in left]
    extra: list[str] = []
    at = 0
    for content, ending in new:
        if at < len(kept) and content == kept[at][0] and kept[at][1] in (ending, ""):
            at += 1
        else:
            extra.append(content)
    if at != len(kept):
        raise ByHand
    allowed = list(added)
    for content in extra:
        if content not in allowed:
            raise ByHand
        allowed.remove(content)


def toml_with_entry(text: str, command: Sequence[str]) -> str:
    """``text``, a TOML settings file, with the product's entry holding ``command``.

    Where there is no entry, the three lines of the entry are added at the
    end, after one blank line. An entry that is there keeps its place: the
    line that sets ``command`` and the line that sets ``args`` are replaced,
    and where there is no ``args`` its line is put after the ``command``
    line. Every other line stays as it is. No file is touched.

    Raises :class:`ByHand` for a text that cannot be changed this way: one
    that is not TOML, a table of servers or an entry that is not written as
    plain lines under a header of its own (an inline table, dotted keys, the
    header twice or inside a text of several lines), an entry without
    ``command``, a value that runs over several lines, and a result that does
    not hold what the text held before. A text without the entry in which a
    line reads like the entry's header (it stands inside a text of several
    lines) raises it too: with the entry added the header would stand there
    twice, and :func:`toml_without_entry` could not take the entry out again.
    On a Python without ``tomllib`` every text raises it.
    """
    command = _command(command)
    before = _toml_loads(text)
    servers = before.get(_TOML_SERVERS, {})
    if not isinstance(servers, dict):
        raise ByHand
    old = _lines(text)
    ending = _line_ending(text)
    block = _toml_block(command)
    had_entry = _SLUG in servers
    if not had_entry:
        if any(_OUR_HEADER.fullmatch(content) for content, _ in old):
            raise ByHand  # nothing is put in that could not be taken out again
        new = list(old)
        added = list(block)
        if new and new[-1][1] == "":
            new[-1] = (new[-1][0], ending)
        if new and new[-1][0].strip(" \t"):
            new.append(("", ending))
            added.append("")
        new.extend((line, ending) for line in block)
        gone: list[int] = []
    else:
        header = _our_header(old)
        own: list[int] = []
        for number in range(header + 1, len(old)):
            if _HEADER.match(old[number][0]):
                break
            own.append(number)
        program = _one_line(old, own, _COMMAND_LINE)
        if program < 0:
            raise ByHand  # an entry of another kind, such as one with a `url`
        arguments = _one_line(old, own, _ARGS_LINE)
        blanks = _COMMAND_LINE.match(old[program][0])
        indent = "" if blanks is None else blanks.group(1)
        new = list(old)
        gone = [program]
        added = [indent + block[1]]
        if arguments >= 0:
            blanks = _ARGS_LINE.match(old[arguments][0])
            inner = "" if blanks is None else blanks.group(1)
            new[program] = (indent + block[1], old[program][1])
            new[arguments] = (inner + block[2], old[arguments][1])
            gone.append(arguments)
            added.append(inner + block[2])
        else:
            new[program] = (indent + block[1], old[program][1] or ending)
            new.insert(program + 1, (indent + block[2], old[program][1]))
            added.append(indent + block[2])
    text_now = _joined(new)
    after = _toml_loads(text_now)
    _check_entry(after, _TOML_SERVERS, command, alone=not had_entry)
    kind = "command" if had_entry else "whole"
    if not _equal(
        _rest(before, _TOML_SERVERS, entry=kind, drop_empty=not had_entry),
        _rest(after, _TOML_SERVERS, entry=kind, drop_empty=not had_entry),
    ):
        raise ByHand
    _check_lines(old, _lines(text_now), gone, added)
    return text_now


def toml_without_entry(text: str) -> str:
    """``text``, a TOML settings file, without the product's entry.

    The lines that go are the header of the entry, the headers of the tables
    below it, and every line under them that is neither blank nor only a
    comment. Comments and blank lines stay, and so does a header
    ``[mcp_servers]`` that was written out. Where two blank lines come to
    stand next to each other at the place of the header, one of them goes. No
    file is touched.

    Raises :class:`ByHand` where :func:`toml_with_entry` does, for a text
    without the entry, and for an entry of which a part stands somewhere else
    in the file.

    Where the table of servers stands among the tables of the top is not
    compared here. ``tomllib`` puts that table where it first meets one of
    its tables. Where that was the entry, and another table stands between
    it and the next server, the table of servers comes later in the parsed
    file once the entry is gone, although no line moved and every key and
    value is what it was.
    """
    before = _toml_loads(text)
    servers = before.get(_TOML_SERVERS, {})
    if not isinstance(servers, dict) or _SLUG not in servers:
        raise ByHand
    old = _lines(text)
    header = _our_header(old)
    gone = [header]
    for number in range(header + 1, len(old)):
        content = old[number][0]
        if _HEADER.match(content):
            if not _HEADER_BELOW.match(content):
                break
            gone.append(number)
        elif not _BLANK_OR_COMMENT.fullmatch(content):
            gone.append(number)
    left = set(gone)
    stay = [number for number in range(len(old)) if number not in left]
    # What stood before the header has kept its number. Two blank lines that meet there:
    # the upper one goes, so that the text ends as it ended.
    if 0 < header < len(stay) and not any(
        old[number][0].strip(" \t") for number in (stay[header - 1], stay[header])
    ):
        gone.append(stay.pop(header - 1))
    new = [old[number] for number in stay]
    text_now = _joined(new)
    after = _toml_loads(text_now)
    _check_no_entry(after, _TOML_SERVERS)
    if not _equal(
        _rest(before, _TOML_SERVERS, entry="whole", drop_empty=True, servers_last=True),
        _rest(after, _TOML_SERVERS, entry="whole", drop_empty=True, servers_last=True),
    ):
        raise ByHand
    _check_lines(old, _lines(text_now), gone, [])
    return text_now


# ---------------------------------------------------------------------------
# What can be said without the file
# ---------------------------------------------------------------------------


def _writable(host: str) -> None:
    if host not in WRITABLE:
        raise ValueError(f"{str(host)[:40]!r} is not an application whose settings file is written")


def _temporary_folders() -> tuple[str, ...]:
    """The folders for temporary files, as they are named and where they really lie."""
    named = list(_TEMPORARY_FOLDERS)
    with suppress(OSError):
        named.insert(0, tempfile.gettempdir())
    found: list[str] = []
    for folder in named:
        for place in (os.path.normpath(folder), os.path.realpath(folder)):
            if os.path.isabs(place) and place != os.sep and place not in found:
                found.append(place)
    return tuple(found)


def _may_not_last(path: str) -> bool:
    """Whether a path that a command names lies where it may be gone or exchanged later.

    True for a path that is not absolute, one under a folder for temporary
    files, and one with a part that belongs to an account other than this one
    or root, or that everybody may write. A part that a group may write is
    not refused. For a part that is a link, the link and the folder it leads
    to are looked at; what the last part leads to is not, and neither are the
    folders above the one a link leads to. :func:`write_problem` therefore
    asks a second time, for where the path really lies. A part that cannot
    be looked at counts as one that may not last.
    """
    if not os.path.isabs(path):
        return True
    written = os.path.normpath(path)
    for folder in _temporary_folders():
        if written == folder or written.startswith(folder.rstrip(os.sep) + os.sep):
            return True
    whole = Path(path)
    anchor = Path(whole.anchor)
    parts = whole.parts[1:]
    try:
        root = hostcheck.open_folder(anchor / "-", anchor)
    except hostcheck.FolderProblem:
        return True
    try:
        owner = os.getuid()
        walked = ""
        for number, part in enumerate(parts):
            walked = part if not walked else walked + os.sep + part
            found = os.lstat(walked, dir_fd=root)
            if found.st_uid not in (owner, 0):
                return True
            if stat.S_ISLNK(found.st_mode):
                if number == len(parts) - 1:
                    break
                found = os.lstat(walked + os.sep + ".", dir_fd=root)
                if found.st_uid not in (owner, 0):
                    return True
            if found.st_mode & stat.S_IWOTH:
                return True
        return False
    except (OSError, NotImplementedError):
        return True
    finally:
        _close(root)


def _where_it_lies(path: str, *, last_part_too: bool) -> str | None:
    """Where a path really lies, with every link on its way followed.

    A last part that is a link is followed only with ``last_part_too``: the
    Python of a virtual environment is a link, and what it leads to is not
    judged. ``None`` where the system would not say.
    """
    try:
        if last_part_too:
            return os.path.realpath(path)
        whole = Path(path)
        return str(Path(os.path.realpath(whole.parent)) / whole.name)
    except OSError:
        return None


def write_problem(host: str, command: Sequence[str]) -> str | None:
    """What keeps the entry for ``host`` from being written, found without the file.

    One of :data:`PROBLEMS`, or ``None``. ``moved``: the file of the ChatGPT
    desktop app while ``CODEX_HOME`` is set, with any value: OpenAI's
    documentation does not say whether the file moves with it, so neither
    place is known to be the one that is read. ``expands``: for Grok Build, a
    command that holds ``${``, which Grok Build replaces by the value of a
    variable according to xAI's documentation. ``unstable``: the Python the
    command names (its first word), or the vault (the word after
    ``--store``), lies where it may not last.

    Each of the two paths is judged twice: as it is written, and where it
    really lies, with every link on its way followed (for the vault its last
    part too, for the Python not: what a Python that is a link leads to is
    not looked at). A link into a folder for temporary files, or past a
    folder that everybody may write, is found that way. A path of which the
    system would not say where it lies counts as one that may not last.

    No settings file is looked at. The folders on the way to the Python and
    to the vault are.
    """
    parts = tuple(command)
    if host == "chatgpt-desktop" and os.environ.get("CODEX_HOME") is not None:
        return "moved"
    if host == "grok-build" and any("${" in part for part in parts):
        return "expands"
    if not parts:
        return "unstable"
    places = [(parts[0], False)]
    if "--store" in parts[:-1]:
        places.append((parts[parts.index("--store") + 1], True))
    for place, last_part_too in places:
        if _may_not_last(place):
            return "unstable"
        real = _where_it_lies(place, last_part_too=last_part_too)
        if real is None or (real != place and _may_not_last(real)):
            return "unstable"
    return None


# ---------------------------------------------------------------------------
# The folder of a settings file, held open
# ---------------------------------------------------------------------------


def _leftover_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"\.{re.escape(name)}\.[0-9a-f]{{24}}\.tmp")


def _remove_leftovers(folder: int, name: str) -> bool:
    """Remove temporary files that an earlier call for this settings file left behind.

    Only a regular file of this account whose name is of this module's
    pattern and that was last changed more than :data:`LEFTOVER_SECONDS` ago.
    Where such a name is a second name of the settings file itself, the
    settings file has one name again afterwards. Returns whether a file was
    removed.
    """
    pattern = _leftover_pattern(name)
    try:
        names = os.listdir(folder)
        owner = os.getuid()
    except OSError:
        return False
    now = time.time()
    removed = False
    for found in names:
        if pattern.fullmatch(found) is None:
            continue
        try:
            status = os.lstat(found, dir_fd=folder)
            if (
                stat.S_ISREG(status.st_mode)
                and status.st_uid == owner
                and now - status.st_mtime > LEFTOVER_SECONDS
            ):
                os.unlink(found, dir_fd=folder)
                removed = True
        except OSError:
            continue
    return removed


def _other_name_is_a_leftover(folder: int, name: str, status: os.stat_result) -> bool:
    """Whether the one other name of a settings file is a temporary name of this module.

    ``status`` is what the system said of the settings file ``name``. True
    only for a file with exactly two names whose other name lies in the same
    folder and is of this module's pattern for this settings file (a regular
    file, the same device and inode). That is what a call leaves that linked
    a new settings file into place and was cut before it removed the
    temporary name. A second name anywhere else, or of any other form, is
    somebody's own.
    """
    if status.st_nlink != 2:
        return False
    pattern = _leftover_pattern(name)
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    for found in names:
        if pattern.fullmatch(found) is None:
            continue
        try:
            other = os.lstat(found, dir_fd=folder)
        except OSError:
            continue
        if stat.S_ISREG(other.st_mode) and (other.st_dev, other.st_ino) == (
            status.st_dev,
            status.st_ino,
        ):
            return True
    return False


def _lstat(folder: int, name: str) -> os.stat_result | None:
    """What the system says about ``name`` in the open folder; ``None`` when nothing is there."""
    try:
        return os.lstat(name, dir_fd=folder)
    except FileNotFoundError:
        return None


def _make_temporary(
    folder: int, name: str, content: bytes, mode: int, group: int | None
) -> tuple[str, os.stat_result]:
    """Write ``content`` to a new temporary file beside ``name``, flushed to disk.

    Returns its name and status. ``mode`` is the mode it gets; when ``group``
    is given and is not the group the system gave the new file, the three
    bits of the group are dropped, so that the file is readable by no group
    the old one was not shown to. Raises :class:`OSError`; nothing is left
    behind then.
    """
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=folder)
    try:
        try:
            left = memoryview(content)
            while left:
                left = left[os.write(descriptor, left) :]
            status = os.fstat(descriptor)
            if group is not None and status.st_gid != group:
                mode &= ~0o070
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        _unlink(folder, temporary)
        raise
    return temporary, status


def _unlink(folder: int, name: str) -> None:
    with suppress(OSError):
        os.unlink(name, dir_fd=folder)


def _close(folder: int) -> None:
    """Close a folder that a call held open. Whatever the system says to it, the call stands.

    It is the last thing a call does. By then the file was looked at,
    changed or left, and the record says which: an error of this close takes
    nothing of that back, and must not turn the answer into an error.
    """
    with suppress(OSError):
        os.close(folder)


def _unchanged(folder: int, name: str, status: os.stat_result | None) -> bool:
    """Whether ``name`` is still the file that was read: ``status`` is what was said then.

    The same device and inode, the same size, the same two times of change,
    and a regular file. The time of the content alone would not do: a program
    that restores or synchronises a file sets it back, and a new file can get
    the inode of one that was removed. The time of the last change of any
    kind is the system's own. With ``None`` for a file that was not there:
    nothing lies at its place. A file that is gone, and one that cannot be
    looked at, count as changed.
    """
    try:
        now = _lstat(folder, name)
    except OSError:
        return False
    if status is None or now is None:
        return status is None and now is None
    return stat.S_ISREG(now.st_mode) and (
        now.st_dev,
        now.st_ino,
        now.st_size,
        now.st_mtime_ns,
        now.st_ctime_ns,
    ) == (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def _a_copy_can_lie_here(folder: int, name: str) -> bool:
    """Whether the latest copy may take the place ``name``: nothing there, or a copy before it.

    A copy before it is a regular file of this account with one name. A link
    or a folder at that place is not replaced.
    """
    try:
        found = _lstat(folder, name)
    except OSError:
        return False
    return found is None or (
        stat.S_ISREG(found.st_mode) and found.st_uid == os.getuid() and found.st_nlink == 1
    )


class _Change:
    """One change of one settings file: the temporary files, the record, the copy, the file.

    After :meth:`run`: ``recorded`` says whether the caller's ``record`` was told
    that a change is about to be made; ``copy`` is the name of the copy this
    call made, if it made one; ``state`` is what the file was read back as;
    ``changed_since`` says that the file was read back in other bytes than
    were written; ``not_read_back`` says that the system did not let the file
    be read back, and that its name still led to the file this call put
    there (``state`` is empty then).
    """

    def __init__(self, folder: int, name: str, found: hostcheck.Look) -> None:
        self.folder = folder
        self.name = name
        self.found = found
        self.temporary: list[str] = []
        self.copy: str | None = None
        self.recorded = False
        self.state = ""
        self.changed_since = False
        self.not_read_back = False
        self._first: tuple[int, int] | None = None  # a first copy this call made

    def discard(self) -> None:
        """Remove every temporary file this call made that is still there."""
        for name in self.temporary:
            _unlink(self.folder, name)
        self.temporary.clear()

    def _take_back_first_copy(self) -> None:
        """Remove a first copy this call made, if its name still leads to it.

        A latest copy cannot be put back: the one before it is gone.
        """
        if self._first is None or self.copy is None:
            return
        try:
            now = _lstat(self.folder, self.copy)
            if now is not None and (now.st_dev, now.st_ino) == self._first:
                os.unlink(self.copy, dir_fd=self.folder)
        except OSError:
            return
        finally:
            self._first = None
            self.copy = None

    def _place_copy(self, temporary: str, status: os.stat_result, first: bool) -> None:
        """Put the copy in place: a first copy by ``link``, the latest by ``replace``.

        A first copy whose place was taken in the meantime, or that the file
        system will not link, becomes the latest copy instead. Raises
        :class:`OSError` when the copy has no place.
        """
        folder = self.folder
        if first:
            place = self.name + FIRST_COPY
            try:
                os.link(temporary, place, src_dir_fd=folder, dst_dir_fd=folder)
            except (OSError, NotImplementedError):
                first = False
        if first:
            self.copy, self._first = place, (status.st_dev, status.st_ino)
            _unlink(folder, temporary)
            self.temporary.remove(temporary)
            return
        place = self.name + LAST_COPY
        if not _a_copy_can_lie_here(folder, place):
            raise OSError("the place of the latest copy is taken")
        os.replace(temporary, place, src_dir_fd=folder, dst_dir_fd=folder)
        self.temporary.remove(temporary)
        self.copy = place

    def _still_in_place(self, placed: os.stat_result) -> bool:
        """Whether the name of the settings file leads to the file this call put there.

        ``placed`` is what the system said of the temporary file once it was
        written. The same device and inode, the same size and the same time
        of its content: it is that file, and nobody wrote into it since. A
        name that cannot be looked at says nothing, and counts as another
        file.
        """
        try:
            now = _lstat(self.folder, self.name)
        except OSError:
            return False
        return (
            now is not None
            and stat.S_ISREG(now.st_mode)
            and (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns)
            == (placed.st_dev, placed.st_ino, placed.st_size, placed.st_mtime_ns)
        )

    def run(
        self,
        new: bytes,
        section: Section,
        actions: tuple[str, str],
        wanted: tuple[str, ...],
        read_back: Callable[[], hostcheck.Look],
    ) -> str | None:
        """Make the change. ``None`` when the file holds what was meant, else the word for why not.

        ``new`` is the new content. ``actions`` is what ``record`` is told
        before the change and after one that failed. ``wanted`` is the states
        the file may be read back in.
        """
        folder, name, found = self.folder, self.name, self.found
        before, after_failure = actions
        old = found.status
        # The new content and the copy, each in a temporary file of its own.
        try:
            mode = 0o600 if old is None else stat.S_IMODE(old.st_mode) & 0o777
            group = None if old is None else old.st_gid
            content, placed = _make_temporary(folder, name, new, mode, group)
            self.temporary.append(content)
        except (OSError, NotImplementedError):
            return "failed"
        kept: tuple[str, os.stat_result] | None = None
        first = False
        if old is not None and found.raw is not None:
            try:
                # A first copy only of a file the product has not changed before, as far as
                # the folder shows: no entry of its name, and neither copy lies there.
                first = (
                    found.state in _NO_ENTRY
                    and _lstat(folder, name + FIRST_COPY) is None
                    and _lstat(folder, name + LAST_COPY) is None
                )
                if not first and not _a_copy_can_lie_here(folder, name + LAST_COPY):
                    return "no_copy"
                kept = _make_temporary(folder, name, found.raw, 0o600, None)
                self.temporary.append(kept[0])
            except (OSError, NotImplementedError):
                return "no_copy"
        # Inside the caller's section: is it still that file, the record, the copy, is it
        # still that file, the file.
        with section() as record:
            if not _unchanged(folder, name, old):
                return "changed"
            record(before)
            self.recorded = True
            if kept is not None:
                try:
                    self._place_copy(kept[0], kept[1], first)
                except (OSError, NotImplementedError):
                    self._take_back_first_copy()
                    record(after_failure)
                    return "no_copy"
            if not _unchanged(folder, name, old):
                self._take_back_first_copy()
                record(after_failure)
                return "changed"
            try:
                if old is None:
                    # Fails if a file appeared at that place in the meantime.
                    os.link(content, name, src_dir_fd=folder, dst_dir_fd=folder)
                else:
                    os.replace(content, name, src_dir_fd=folder, dst_dir_fd=folder)
                    self.temporary.remove(content)
            except (OSError, NotImplementedError):
                self._take_back_first_copy()
                record(after_failure)
                return "failed"
        self.discard()  # also the temporary name of a file that was linked into place
        with suppress(OSError):
            os.fsync(folder)  # so that the new names are on the disk
        # Read back, through the reader every look goes through.
        again = read_back()
        if again.raw is None and self._still_in_place(placed):
            # The system did not let the file be read, and the name leads to the file that
            # was written: the change was made. Saying that it failed would be untrue, and
            # would be recorded.
            self.not_read_back = True
            return None
        if again.state not in wanted:
            with section() as record:
                record(after_failure)
            return "failed"
        self.state = again.state
        self.changed_since = again.raw != new
        return None


def _open_folder(path: Path, language: str, base: Path) -> int | str:
    """The folder of a settings file, opened, or the word for why it was not."""
    if language == "toml" and hostcheck._toml_loads() is None:
        return "unsupported"
    try:
        return hostcheck.open_folder(path, base)
    except hostcheck.FolderProblem as problem:
        return problem.state


def _looked(
    host: str,
    *,
    client: str,
    store: str | os.PathLike[str] | None,
    folder: int,
    name: str,
    platform: str | None,
) -> tuple[hostcheck.Look, str]:
    """A look through the open folder, and its state as a write takes it.

    A file that was read and has more than one name, or belongs to another
    account, is ``not_read`` for a write: replacing it would leave the other
    name with the old content, or put a file of this account in the place of
    somebody else's.

    One second name does not make a file ``not_read``: a temporary name of
    this module for the settings file ``name``, in the same folder
    (:func:`_other_name_is_a_leftover`). The state the reader found stands
    then, so that the owner is shown what the file holds and the press that
    follows can remove that name. Such a file is still not replaced while
    the name is there (:func:`_recent`).
    """
    found = hostcheck.look(host, client=client, store=store, folder_fd=folder, platform=platform)
    status = found.status
    if status is not None and (
        status.st_uid != os.getuid()
        or (status.st_nlink > 1 and not _other_name_is_a_leftover(folder, name, status))
    ):
        return found, "not_read"
    return found, found.state


def _looked_and_cleared(
    host: str,
    *,
    client: str,
    store: str | os.PathLike[str] | None,
    folder: int,
    name: str,
    platform: str | None,
    expect: str,
    clear: Sequence[str],
) -> tuple[hostcheck.Look, str]:
    """The look of a call that may change the file, and the leftovers of earlier calls removed.

    The leftovers are removed only once the file was found in the state the
    owner was shown, ``expect``, and only where that state is one of
    ``clear``: a call that is refused because the state is another one has
    touched nothing in the folder. Where a leftover went, the file is looked
    at again: the leftover may have been a second name of the file itself,
    and what the system says of the file is then no longer what it said.
    """
    found, state = _looked(
        host, client=client, store=store, folder=folder, name=name, platform=platform
    )
    if state == expect and state in clear and _remove_leftovers(folder, name):
        found, state = _looked(
            host, client=client, store=store, folder=folder, name=name, platform=platform
        )
    return found, state


def _shown(path: Path | None) -> str | None:
    return None if path is None else hostconfig.shown_path(path)


def _real(folder: Path) -> str | None:
    """Where the path of a folder leads, as an owner reads it; ``None`` where that cannot be told.

    Asked for a folder on whose way a link was found. Where that link is gone
    or is no link any more by the time it is read, the system refuses, and
    the answer has no place to show.
    """
    try:
        return hostconfig.shown_path(os.path.realpath(folder))
    except OSError:
        return None


def _made_at(status: os.stat_result) -> str | None:
    """When a file was last changed, as a record writes a time. ``None`` for a time that is none."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(status.st_mtime))
    except (OverflowError, OSError, ValueError):
        return None


def _new_content(found: hostcheck.Look, make: Callable[[str], str]) -> bytes | None:
    """The new content of a settings file, or ``None`` where the rules cannot make it."""
    try:
        text = "" if found.raw is None else found.raw.decode("utf-8")
        new = make(text).encode("utf-8")
    except (ByHand, ValueError, RecursionError, UnicodeError, MemoryError):
        return None
    return None if len(new) > MAX_BYTES else new


def _recent(found: hostcheck.Look) -> bool:
    """Whether the file that was read was changed a moment ago, and is not to be replaced yet.

    Also true for a file with a second name. Up to here that can only be the
    temporary name of an earlier call that was cut (:func:`_looked`), and the
    leftovers that are old enough were removed before: the name is younger
    than :data:`LEFTOVER_SECONDS`, or the system would not let it go. No
    file is replaced while it has two names.
    """
    status = found.status
    if status is None:
        return False
    return status.st_nlink > 1 or 0 <= time.time() - status.st_mtime < QUIET_SECONDS


def _state_written(new: bytes, language: str) -> str:
    """The state of a settings file that holds ``new``, a text of this module with the entry.

    ``present`` where the entry holds its command and nothing else,
    ``present_more`` where it holds more: what a reader would say of it. It
    is told from the text for a file that was written and could not be read
    back.
    """
    text = new.decode("utf-8")
    if language == "json":
        entry = _json_loads(text)[_JSON_SERVERS][_SLUG]
    else:
        entry = _toml_loads(text)[_TOML_SERVERS][_SLUG]
    return "present" if len(entry) == 2 else "present_more"


def look(
    host: str,
    *,
    client: str,
    store: str | os.PathLike[str] | None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Look at the settings file of ``host`` as a write would, and change nothing.

    Returns ``host``; ``label`` and ``readers`` (the name sentences use for
    the program, and the programs that read the file); ``state`` (a word of
    :data:`wdva.gate.hostcheck.STATES`); ``problem`` (:func:`write_problem`);
    ``format`` (``"JSON"`` or ``"TOML"``); ``path`` and ``folder`` as an owner
    reads them; ``real_folder`` (for ``linked``: where the folder really
    lies, or ``None`` where the link was exchanged at that moment and the
    system would not say); ``variable`` (the environment variable that
    decided the place, or ``None``); ``entry`` (the name of the entry);
    ``command`` (the command the entry holds or would hold); ``section`` (for
    JSON without the entry); ``switched_off``; ``other_name``; and
    ``copies``, the kept copies that lie beside the file, each with ``path``
    and ``made_at``.

    The file is read, and the two places of its copies are looked at and not
    read. For a file with two names the names in its folder are listed, to
    see whether the other one is a temporary name an earlier call left
    (:func:`_looked`). Nothing is removed. Nothing of what the file holds is
    in the answer. Raises :class:`ValueError` for an application that is not
    in :data:`WRITABLE` and where :func:`~wdva.gate.hostconfig.render` does.
    Raises for nothing about the file or its folder.
    """
    _writable(host)
    tile = next(tile for tile in hostconfig.TILES if tile.host == host)
    command = hostconfig.render(host, client=client, store=store, platform=platform).command
    answer: dict[str, Any] = {
        "host": host,
        "label": tile.label,
        "readers": list(tile.readers),
        "state": "unsupported",
        "problem": write_problem(host, command),
        "format": None,
        "path": None,
        "folder": None,
        "real_folder": None,
        "variable": None,
        "entry": _SLUG,
        "command": list(command),
        "section": None,
        "switched_off": False,
        "other_name": None,
        "copies": [],
    }
    place = hostconfig.settings_place(host, platform)
    if place is None:
        return answer
    path, language, base, variable = place
    answer.update(
        format=_FORMATS[language],
        path=_shown(path),
        folder=_shown(path.parent),
        variable=variable,
    )
    folder = _open_folder(path, language, base)
    if isinstance(folder, str):
        answer["state"] = folder
        if folder == "linked":
            answer["real_folder"] = _real(path.parent)
        return answer
    try:
        found, state = _looked(
            host, client=client, store=store, folder=folder, name=path.name, platform=platform
        )
        answer.update(
            state=state,
            section=found.section if state == found.state else None,
            switched_off=found.switched_off,
            other_name=found.other_name,
        )
        for ending in (FIRST_COPY, LAST_COPY):
            try:
                status = _lstat(folder, path.name + ending)
            except OSError:
                continue
            if status is not None and stat.S_ISREG(status.st_mode):
                place_of_copy = path.parent / (path.name + ending)
                answer["copies"].append(
                    {"path": _shown(place_of_copy), "made_at": _made_at(status)}
                )
    finally:
        _close(folder)
    return answer


def connect(
    host: str,
    *,
    client: str,
    store: str | os.PathLike[str] | None,
    expect: str,
    section: Section,
    registered: Sequence[str] = (),
    platform: str | None = None,
) -> dict[str, Any]:
    """Write the product's entry for ``client`` and ``store`` into the settings file of ``host``.

    ``expect`` is the state the owner was shown, a word of
    :data:`wdva.gate.hostcheck.STATES`; in any other state nothing is written.
    ``section`` is called to get a context manager around the steps in which
    the record is written and the file is replaced; entered, it yields
    ``record(action)``, which is called with ``"connect"`` right before the
    change, with ``"connect-kept"`` where the file holds the entry already,
    and with ``"connect-failed"`` after a change that was announced and did
    not happen. ``registered`` is the names the vault answers: an entry that
    calls under one of them is left as it is.

    Returns ``connected``, ``state`` and ``path``. With ``connected`` true,
    ``state`` is ``present`` or ``present_more``, and the answer has ``copy``
    (the copy this call made, or ``None``), ``replaced`` (an entry of that
    name was there and pointed elsewhere) and, where the file was written
    again by somebody else right afterwards and still holds the entry,
    ``changed_since``. Where the system did not let the file be read back,
    and its name still leads to the file this call put there, the answer has
    ``not_read_back`` instead, and ``state`` is told from the text that was
    written. With ``connected`` false, ``state`` says why: a word of
    :data:`PROBLEMS`, of :data:`REFUSALS`, or what the reader found; for
    ``linked`` the answer has ``real_folder``, for ``present_as``
    ``other_name``.

    A temporary file that an earlier call left in the folder is removed once
    the file was found in the state ``expect``, and not before: a call that
    answers ``changed`` for another state has touched nothing.

    Raises :class:`ValueError` for an application that is not in
    :data:`WRITABLE`, for an ``expect`` that is no state a look can find, and
    where :func:`~wdva.gate.hostconfig.render` does. Whatever entering ``section``
    or calling ``record`` raises is raised on, after the temporary files of
    this call were removed. No file of another program was changed then,
    unless the error came when a change that had been made was found undone.
    The folder is closed last, and an error of that close is not raised: the
    change was made and recorded by then, and the answer says so.
    """
    _writable(host)
    if expect not in hostcheck.STATES:
        raise ValueError("the state the owner was shown is not one a look can find")
    command = hostconfig.render(host, client=client, store=store, platform=platform).command
    place = hostconfig.settings_place(host, platform)
    shown = None if place is None else _shown(place[0])

    def stop(state: str, **more: Any) -> dict[str, Any]:
        return {"connected": False, "state": state, "path": shown, **more}

    problem = write_problem(host, command)
    if problem is not None:
        return stop(problem)
    if place is None:
        return stop("unsupported")
    path, language, base, _ = place
    folder = _open_folder(path, language, base)
    if isinstance(folder, str):
        return stop(folder, real_folder=_real(path.parent)) if folder == "linked" else stop(folder)
    change: _Change | None = None
    try:
        found, state = _looked_and_cleared(
            host,
            client=client,
            store=store,
            folder=folder,
            name=path.name,
            platform=platform,
            expect=expect,
            clear=_OURS + _CAN_BE_WRITTEN,
        )
        if state in ("unreadable", "not_read", "busy", "unsupported", "no_access"):
            return stop(state)
        if state != expect:
            return stop("changed")
        if state == "present_as" and found.other_name in registered:
            return stop("present_as", other_name=found.other_name)
        if state in _OURS:  # nothing is to be written
            with section() as record:
                record("connect-kept")
            return {
                "connected": True,
                "state": state,
                "path": shown,
                "copy": None,
                "replaced": False,
            }
        if state not in _CAN_BE_WRITTEN:
            return stop(state)
        if _recent(found):
            return stop("recent")
        make = json_with_entry if language == "json" else toml_with_entry
        new = _new_content(found, lambda text: make(text, command))
        if new is None:
            return stop("by_hand")
        change = _Change(folder, path.name, found)
        word = change.run(
            new,
            section,
            ("connect", "connect-failed"),
            _OURS,
            lambda: hostcheck.look(
                host, client=client, store=store, folder_fd=folder, platform=platform
            ),
        )
        if word is not None:
            return stop(word)
        answer: dict[str, Any] = {
            "connected": True,
            "state": _state_written(new, language) if change.not_read_back else change.state,
            "path": shown,
            "copy": None if change.copy is None else _shown(path.parent / change.copy),
            "replaced": state in ("differs", "present_as"),
        }
        if change.changed_since:
            answer["changed_since"] = True
        if change.not_read_back:
            answer["not_read_back"] = True
        return answer
    finally:
        if change is not None:
            change.discard()
        _close(folder)


def disconnect(
    host: str,
    *,
    client: str,
    store: str | os.PathLike[str] | None,
    expect: str,
    section: Section,
    platform: str | None = None,
) -> dict[str, Any]:
    """Take the product's entry out of the settings file of ``host``, if it is this vault's.

    ``expect`` is the state the owner was shown. Only for ``present`` and
    ``present_more`` is the file changed: the entry, with what it holds, is
    removed, and nothing else. An entry that points elsewhere is left, and so
    is a file in any other state. ``section`` is what :func:`connect` takes;
    ``record`` is called with ``"disconnect"`` right before the change, with
    ``"disconnect-kept"`` where the file is left as it is, and with
    ``"disconnect-failed"`` after a change that was announced and did not
    happen.

    Returns ``file``, ``path`` and ``copy``. ``file`` is ``"removed"``;
    ``"differs"`` (an entry that points elsewhere was left); ``"no_entry"``;
    the word for a file that could not be looked at or read (``linked``,
    ``no_access``, ``unreadable``, ``not_read``, ``busy``, ``unsupported``);
    the word for an entry that was found and not removed (``recent``,
    ``by_hand``, ``no_copy``, ``failed``); or ``"changed"``: the file is not
    in the state the owner was shown, or was another one before the record
    was written.
    Only then was ``record`` not called. ``copy`` is the copy this call made,
    or ``None``. With ``removed`` the answer can have ``changed_since`` or
    ``not_read_back``, as that of :func:`connect`.

    A temporary file that an earlier call left in the folder is removed where
    ``expect`` is ``present`` or ``present_more`` and the file was found in
    that state, and in no other case.

    Raises as :func:`connect` does.
    """
    _writable(host)
    if expect not in hostcheck.STATES:
        raise ValueError("the state the owner was shown is not one a look can find")
    hostconfig.render(host, client=client, store=store, platform=platform)
    place = hostconfig.settings_place(host, platform)
    shown = None if place is None else _shown(place[0])

    def answer(word: str, copy: str | None = None) -> dict[str, Any]:
        return {"file": word, "path": shown, "copy": copy}

    def left(word: str) -> dict[str, Any]:
        with section() as record:
            record("disconnect-kept")
        return answer(_FOR_A_FILE.get(word, word))

    if place is None:
        return left("unsupported") if expect == "unsupported" else answer("changed")
    path, language, base, _ = place
    folder = _open_folder(path, language, base)
    if isinstance(folder, str):
        return left(folder) if folder == expect else answer("changed")
    change: _Change | None = None
    try:
        found, state = _looked_and_cleared(
            host,
            client=client,
            store=store,
            folder=folder,
            name=path.name,
            platform=platform,
            expect=expect,
            clear=_OURS,
        )
        if state != expect:
            return answer("changed")
        if state not in _OURS:  # no entry, or none that is this vault's: the file is left
            return left(state)
        if _recent(found):
            return left("recent")
        new = _new_content(found, json_without_entry if language == "json" else toml_without_entry)
        if new is None:
            return left("by_hand")
        change = _Change(folder, path.name, found)
        word = change.run(
            new,
            section,
            ("disconnect", "disconnect-failed"),
            _NO_ENTRY,
            lambda: hostcheck.look(
                host, client=client, store=store, folder_fd=folder, platform=platform
            ),
        )
        if word is None:
            done = answer(
                "removed", None if change.copy is None else _shown(path.parent / change.copy)
            )
            if change.changed_since:
                done["changed_since"] = True
            if change.not_read_back:
                done["not_read_back"] = True
            return done
        if change.recorded:
            return answer("failed")
        if word == "changed":
            return answer("changed")
        change.discard()
        return left(word)
    finally:
        if change is not None:
            change.discard()
        _close(folder)
