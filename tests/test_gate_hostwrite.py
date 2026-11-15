"""The one module that writes a file of another program: every rule of the write, as a test.

Every test runs in a home folder of its own. That home lies under the system's folder for
temporary files, and so would a vault or a Python made here: a command that names such a
place is not written (rule R17). Every test but the ones about that rule therefore replaces
the function that judges where a path lies by one that finds nothing.

The settings files are files these tests made. No application read one of them.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import json
import math
import os
import random
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import wdva.gate
from wdva.gate import brand, hostcheck, hostconfig, hostwrite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

SLUG = brand.SLUG
STORE = "/srv/my vault"  # a place a command names; nothing is read or made there
SECRET = "SECRET-OF-ANOTHER-TOOL-5512"
HAS_TOML = sys.version_info >= (3, 11)
needs_toml = pytest.mark.skipif(not HAS_TOML, reason="no reader for TOML in this Python")

JSON_HOST = "claude-desktop"
TOML_HOST = "chatgpt-desktop"
HOSTS = [
    pytest.param("claude-desktop"),
    pytest.param("chatgpt-desktop", marks=needs_toml),
    pytest.param("grok-build", marks=needs_toml),
]
CONNECT_ACTIONS = ("connect", "connect-kept", "connect-failed")
DISCONNECT_ACTIONS = ("disconnect", "disconnect-kept", "disconnect-failed")


# -- what the tests stand on ------------------------------------------------------------------


@pytest.fixture
def bare_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory that belongs to the test. No test looks into the real one."""
    place = tmp_path / "home"
    place.mkdir()
    monkeypatch.setenv("HOME", str(place))
    for variable in ("XDG_CONFIG_HOME", "GROK_HOME", "CODEX_HOME"):
        monkeypatch.delenv(variable, raising=False)
    return place


@pytest.fixture
def home(bare_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The home of a test that is not about where a Python or a vault lies.

    Rule R17 is tested by itself, with places that are not under the folder for temporary
    files. Everywhere else the function that judges such a place finds nothing.
    """
    monkeypatch.setattr(hostwrite, "_may_not_last", lambda path: False)
    return bare_home


def place_of(host: str) -> Path:
    """Where the settings file of ``host`` lies in the test's home, on this system."""
    found = hostconfig.settings_file(host)
    assert found is not None
    return found[0]


def folder_of(host: str) -> Path:
    """The settings file of ``host``, with its folder made."""
    place = place_of(host)
    place.parent.mkdir(parents=True, exist_ok=True)
    return place


def command_of(client: str, store: str = STORE) -> list[str]:
    return hostconfig.host_command(client, store)


def ours(client: str, store: str = STORE) -> dict[str, Any]:
    """The entry as a parsed settings file holds it."""
    command = command_of(client, store)
    return {"command": command[0], "args": command[1:]}


def toml_string(value: str) -> str:
    # For the paths used here, the same text as a basic string of TOML.
    return json.dumps(value, ensure_ascii=False)


def toml_lines(client: str, store: str = STORE) -> list[str]:
    """The three lines of the entry in a TOML settings file."""
    command = command_of(client, store)
    return [
        f"[mcp_servers.{SLUG}]",
        f"command = {toml_string(command[0])}",
        "args = [" + ", ".join(toml_string(part) for part in command[1:]) + "]",
    ]


def json_member(client: str, indent: str = "  ", ending: str = "\n", store: str = STORE) -> str:
    """The entry as the writer puts it into ``mcpServers``: four lines, no comma."""
    command = command_of(client, store)
    arguments = ", ".join(json.dumps(part, ensure_ascii=False) for part in command[1:])
    return ending.join(
        (
            f'{indent * 2}"{SLUG}": {{',
            f'{indent * 3}"command": {json.dumps(command[0], ensure_ascii=False)},',
            f'{indent * 3}"args": [{arguments}]',
            f"{indent * 2}}}",
        )
    )


def aged(path: Path, seconds: float = 30.0) -> Path:
    """Give a file a time of change that is not a moment ago, as the writer asks."""
    when = time.time() - seconds
    os.utime(path, (when, when), follow_symlinks=False)
    return path


def put(path: Path, content: bytes | str, *, mode: int | None = None) -> Path:
    """A settings file with this content, last changed half a minute ago."""
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    if mode is not None:
        path.chmod(mode)
    return aged(path)


def tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Everything below ``root``: what it is, its mode, and for a file its time and bytes.

    No link is followed. The time of a folder is left out: a temporary file that came and
    went changes it.
    """
    found: dict[str, tuple[Any, ...]] = {}
    waiting = [root]
    while waiting:
        folder = waiting.pop()
        for item in sorted(os.scandir(folder), key=lambda entry: entry.name):
            path = Path(item.path)
            status = path.lstat()
            name = str(path.relative_to(root))
            mode = stat.S_IMODE(status.st_mode)
            if stat.S_ISLNK(status.st_mode):
                found[name] = ("link", os.readlink(path))
            elif stat.S_ISDIR(status.st_mode):
                found[name] = ("folder", mode)
                waiting.append(path)
            elif stat.S_ISREG(status.st_mode):
                try:
                    content: bytes | None = path.read_bytes()
                except PermissionError:
                    content = None
                found[name] = ("file", mode, status.st_mtime_ns, status.st_nlink, content)
            else:
                found[name] = ("other", status.st_mode)
    return found


def names(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir())


def no_temporary_file(folder: Path) -> bool:
    return not [name for name in names(folder) if name.endswith(".tmp")]


class Told:
    """The ``section`` a test hands the writer. It notes what the writer does with it.

    ``on_enter`` runs once, when the section is entered: between the read and the first
    comparison. ``on[action]`` runs once when ``record(action)`` is called: after the record
    and before what follows it. ``fail_enter`` and ``fail[action]`` are raised in their place.
    """

    def __init__(self) -> None:
        self.entered = 0
        self.actions: list[str] = []
        self.on_enter: Callable[[], None] | None = None
        self.on: dict[str, Callable[[], None]] = {}
        self.fail_enter: Exception | None = None
        self.fail: dict[str, Exception] = {}
        self._inside = False

    @contextlib.contextmanager
    def __call__(self) -> Iterator[Callable[[str], None]]:
        self.entered += 1
        if self.fail_enter is not None:
            raise self.fail_enter
        hook, self.on_enter = self.on_enter, None
        if hook is not None:
            hook()
        self._inside = True
        try:
            yield self.record
        finally:
            self._inside = False

    def record(self, action: str) -> None:
        assert self._inside, "the record was written outside the section"
        assert action in CONNECT_ACTIONS + DISCONNECT_ACTIONS, action
        if action in self.fail:
            raise self.fail.pop(action)
        self.actions.append(action)
        hook = self.on.pop(action, None)
        if hook is not None:
            hook()


class Refused(Exception):
    """What a service raises from its section: a status and a word."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


def connect(
    host: str,
    expect: str,
    told: Told | None = None,
    *,
    client: str | None = None,
    store: str = STORE,
    registered: tuple[str, ...] = (),
) -> dict[str, Any]:
    return hostwrite.connect(
        host,
        client=client or host,
        store=store,
        expect=expect,
        section=told or Told(),
        registered=registered,
    )


def disconnect(
    host: str,
    expect: str,
    told: Told | None = None,
    *,
    client: str | None = None,
    store: str = STORE,
) -> dict[str, Any]:
    return hostwrite.disconnect(
        host, client=client or host, store=store, expect=expect, section=told or Told()
    )


def looked(host: str, *, client: str | None = None, store: str = STORE) -> dict[str, Any]:
    return hostwrite.look(host, client=client or host, store=store)


def state_of(host: str, *, client: str | None = None, store: str = STORE) -> str:
    state: str = looked(host, client=client, store=store)["state"]
    return state


def strict(text: str) -> Any:
    """A JSON text as the strict reader reads it: no key twice, no NaN, keys in their order."""
    return hostcheck._json_loads(text)


def toml(text: str) -> Any:
    import tomllib

    return tomllib.loads(text)


def same(left: Any, right: Any) -> bool:
    """Equal by value, by type and by the order of keys. Two not-a-numbers are equal."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return list(left) == list(right) and all(same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(map(same, left, right))
    if isinstance(left, float) and math.isnan(left):
        return math.isnan(right)
    return bool(left == right)


def without_entry(data: dict[str, Any], key: str, *, drop_empty: bool = False) -> dict[str, Any]:
    """A parsed settings file without the product's entry."""
    rest = dict(data)
    if key in rest:
        servers = {name: value for name, value in rest[key].items() if name != SLUG}
        if drop_empty and not servers:
            del rest[key]
        else:
            rest[key] = servers
    return rest


def cut_out(old: str, new: str) -> str:
    """The one piece that was put into ``old`` to make ``new``; fails if it is not one piece."""
    start = 0
    while start < len(old) and start < len(new) and old[start] == new[start]:
        start += 1
    rest = len(old) - start
    assert len(new) - start >= rest, "the new text is not the old one with a piece put in"
    piece = new[start : len(new) - rest]
    assert new[:start] + new[len(new) - rest :] == old, "a byte outside the piece changed"
    return piece


# -- the four functions that take text and give text: JSON ----------------------------------

JSON_TEXTS = {
    "other servers": (
        '{\n  "mcpServers": {\n    "another-tool": {\n      "command": "tool",\n'
        '      "args": ["--x"],\n      "env": {"TOKEN": "' + SECRET + '"}\n    }\n  }\n}\n'
    ),
    "other top-level keys": '{\n  "theme": "dark",\n  "globalShortcut": "Ctrl+Space"\n}\n',
    "keys after the servers": (
        '{\n  "mcpServers": {\n    "a": {"command": "x"}\n  },\n  "theme": "dark"\n}\n'
    ),
    "nested values": (
        '{\n  "a": {"b": [1, [2, {"c": null}], true, false], "d": {}},\n'
        '  "e": [],\n  "mcpServers": {"x": {"command": "y", "args": [], "env": {"k": [{}]}}}\n}\n'
    ),
    "numbers written in several ways": (
        '{"a": 1e3, "b": 1.0, "c": 1, "d": -0.0, "e": 1E+2, "f": 0.1000, "g": 1e400,\n'
        ' "h": 12345678901234567890123456789012345, "i": -1e-400, "j": 1.5e-7}\n'
    ),
    "escapes": (
        '{"a": "\\u00e9\\n\\t\\"q\\" \\/ \\\\ \\ud83d\\ude00", "half": "\\ud800",\n'
        ' "b\\u0041": 1, "braces": "}{[]\\"]\\\\", "mcpServers": {"s": {"command": "{[\\"}"}}}\n'
    ),
    "non-ASCII text": '{\n  "name": "Zażółć gęślą jaźń 日本 🔒",\n  "żółw": ["ß", "\u2028"]\n}\n',
    "tabs": '{\n\t"theme": "dark",\n\t"mcpServers": {\n\t\t"a": {"command": "x"}\n\t}\n}\n',
    "four spaces": (
        '{\n    "theme": "dark",\n    "mcpServers": {\n        "a": {"command": "x"}\n    }\n}\n'
    ),
    "Windows line endings": (
        '{\r\n  "theme": "dark",\r\n  "mcpServers": {\r\n    "a": {"command": "x"}\r\n  }\r\n}\r\n'
    ),
    "one line": '{"theme":"dark","mcpServers":{"a":{"command":"x"}}}',
    "one line without servers": '{"theme": "dark"}',
    "no newline at the end": '{\n  "theme": "dark",\n  "mcpServers": {\n    "a": {}\n  }\n}',
    "white space around": '\n\n  {\n  "theme": "dark"\n}  \n\n',
    "empty servers": '{\n  "theme": "dark",\n  "mcpServers": {}\n}\n',
    "empty servers with space inside": '{"mcpServers": {  \n }, "theme": 1}',
    "an empty object": "{}",
    "an empty object with space": "{ \n }\n",
    "the servers under an escaped key": '{"mcp\\u0053ervers": {"a": {"command": "x"}}}\n',
    "keys that only look like the entry": (
        '{\n  "' + SLUG + '": {"command": "top"},\n  "nested": {"mcpServers": {"' + SLUG + '": '
        '{"command": "deep"}}},\n  "mcpServers": {\n    "' + SLUG + '-old": {"command": "a"},\n'
        '    "' + SLUG.capitalize() + '": {"command": "b"},\n'
        '    "x": {"' + SLUG + '": {"command": "c"}}\n  }\n}\n'
    ),
}


# The indentation of each text where it is not two spaces: the white space before its first
# indented line.
JSON_INDENTS = {
    "tabs": "\t",
    "four spaces": "    ",
    "numbers written in several ways": " ",
    "escapes": " ",
    "empty servers with space inside": " ",
    "an empty object with space": " ",
}


# Where a text has white space between the two braces the entry goes into: their places.
BRACES_WITH_SPACE = {
    "empty servers with space inside": (15, 20),
    "an empty object with space": (0, 4),
}


@pytest.mark.parametrize("name", sorted(JSON_TEXTS))
def test_everything_but_the_entry_is_equal_json(name: str, home: Path) -> None:
    old = JSON_TEXTS[name]
    command = command_of(JSON_HOST)
    before = strict(old)
    new = hostwrite.json_with_entry(old, command)
    after = strict(new)
    # Parsed: the entry is ours, it is the last of the servers, and everything else is what
    # it was, by value, by type and in its order.
    assert after["mcpServers"][SLUG] == ours(JSON_HOST)
    assert list(after["mcpServers"][SLUG]) == ["command", "args"]
    assert list(after["mcpServers"])[-1] == SLUG
    assert same(without_entry(after, "mcpServers", drop_empty="mcpServers" not in before), before)
    assert same(before, strict(old)), "the reader's own answer changed under the writer"
    # As text: the new bytes are the old bytes with one piece put in. (Where the entry goes
    # between two braces that held white space only, the piece takes the place of that.)
    ending = "\r\n" if "\r\n" in old else "\n"
    indent = JSON_INDENTS.get(name, "  ")
    assert json_member(JSON_HOST, indent, ending) in new
    if name in BRACES_WITH_SPACE:
        opening, closing = BRACES_WITH_SPACE[name]
        assert not old[opening + 1 : closing].strip()
        assert new.startswith(old[: opening + 1])
        assert new.endswith(old[closing:])
        piece = new[opening + 1 : len(new) - (len(old) - closing)]
    else:
        piece = cut_out(old, new)
    assert json_member(JSON_HOST, indent, ending) in piece
    if ending == "\r\n":
        assert "\n" not in new.replace("\r\n", ""), "a line ending of another kind was put in"
    else:
        assert "\r" not in piece
    assert new.encode("utf-8")  # nothing in it that cannot be written
    # Through a file: the same bytes, a copy of the old ones, and the look says so.
    place = put(folder_of(JSON_HOST), old)
    found = state_of(JSON_HOST)
    assert found in ("other", "empty")
    told = Told()
    answer = connect(JSON_HOST, found, told)
    assert answer == {
        "connected": True,
        "state": "present",
        "path": hostconfig.shown_path(place),
        "copy": hostconfig.shown_path(place) + hostwrite.FIRST_COPY,
        "replaced": False,
    }
    assert place.read_bytes() == new.encode("utf-8")
    assert Path(str(place) + hostwrite.FIRST_COPY).read_bytes() == old.encode("utf-8")
    assert told.actions == ["connect"]
    # And out again: everything else is still what it was.
    back = hostwrite.json_without_entry(new)
    assert same(
        without_entry(strict(back), "mcpServers", drop_empty="mcpServers" not in before), before
    )
    assert SLUG not in strict(back).get("mcpServers", {})
    if before.get("mcpServers"):
        assert back == old, "taking the entry out again did not give the old text"
    aged(place)
    assert disconnect(JSON_HOST, "present")["file"] == "removed"
    assert place.read_bytes() == back.encode("utf-8")


def test_a_json_entry_is_put_where_the_rules_say() -> None:
    command = command_of(JSON_HOST)
    member = json_member(JSON_HOST)
    with_entry = hostwrite.json_with_entry
    # An empty text, or white space only: the whole file.
    whole = '{\n  "mcpServers": {\n' + member + "\n  }\n}\n"
    for nothing in ("", " ", "\n\n", "\t \n"):
        assert with_entry(nothing, command) == whole
    assert with_entry("\r\n", command) == whole.replace("\n", "\r\n")
    # No section: after the last value of the file.
    assert with_entry('{\n  "a": 1\n}\n', command) == (
        '{\n  "a": 1,\n  "mcpServers": {\n' + member + "\n  }\n}\n"
    )
    # An object without members: what stands between its braces is replaced.
    assert with_entry("{\n}\n", command) == whole
    assert with_entry("{   }", command) == whole[:-1]
    # (Here the first indented line has one space, so that is the file's indentation.)
    assert with_entry("{ \n }\n", command) == (
        '{\n "mcpServers": {\n' + json_member(JSON_HOST, " ") + "\n }\n}\n"
    )
    # A section with servers: after the last of them.
    assert with_entry('{\n  "mcpServers": {\n    "a": {}\n  }\n}\n', command) == (
        '{\n  "mcpServers": {\n    "a": {},\n' + member + "\n  }\n}\n"
    )
    # An empty section: what stands between its braces is replaced.
    assert with_entry('{\n  "mcpServers": {}\n}\n', command) == whole
    # The indentation is the file's own: a tab, or that many spaces, eight at most.
    assert with_entry('{\n\t"a": 1\n}\n', command) == (
        '{\n\t"a": 1,\n\t"mcpServers": {\n' + json_member(JSON_HOST, "\t") + "\n\t}\n}\n"
    )
    wide = with_entry('{\n            "a": 1\n}\n', command)
    assert '\n        "mcpServers": {\n' in wide
    assert json_member(JSON_HOST, " " * 8) in wide
    # A text is written as it is, never as escapes of it.
    accent = with_entry("{}", ["/opt/zażółć 日本/python", "-m", 'a "b" \\ c'])
    assert '"command": "/opt/zażółć 日本/python"' in accent
    assert '"args": ["-m", "a \\"b\\" \\\\ c"]' in accent


def test_a_json_entry_is_taken_out_with_the_comma_that_belonged_to_it() -> None:
    without = hostwrite.json_without_entry
    entry = '"' + SLUG + '": {"command": "x", "args": []}'
    # The only one: the section stays, empty.
    assert without('{"a": 1, "mcpServers": {' + entry + "}}") == '{"a": 1, "mcpServers": {}}'
    assert without('{\n  "mcpServers": {\n    ' + entry + "\n  }\n}\n") == (
        '{\n  "mcpServers": {}\n}\n'
    )
    # The last of several, and one in the middle: from the end of the one before it.
    assert without('{"mcpServers": {"a": {}, ' + entry + "}}") == '{"mcpServers": {"a": {}}}'
    assert without('{"mcpServers": {"a": {} ,\n' + entry + ' , "b": 1}}') == (
        '{"mcpServers": {"a": {} , "b": 1}}'
    )
    # The first of several: up to the key of the next.
    assert without('{"mcpServers": {' + entry + ',\n    "b": {}}}') == '{"mcpServers": {"b": {}}}'
    # An entry that holds more goes whole.
    more = '"' + SLUG + '": {"command": "x", "env": {"A": "}"}, "tools": {"s": {"m": [1, 2]}}}'
    assert without('{"mcpServers": {"a": 1, ' + more + '}, "z": "}"}') == (
        '{"mcpServers": {"a": 1}, "z": "}"}'
    )
    # Without the entry there is nothing to take out.
    for text in ('{"mcpServers": {"a": {}}}', "{}", '{"' + SLUG + '": {}}', ""):
        with pytest.raises(hostwrite.ByHand):
            without(text)


def test_json_that_cannot_be_edited_by_the_rules_is_left_to_the_owner() -> None:
    command = command_of(JSON_HOST)
    entry = json.dumps(ours(JSON_HOST))
    for text in (
        "[]",
        '"text"',
        "null",
        "5",
        '{"mcpServers": []}',
        '{"mcpServers": null}',
        '{"mcpServers": "x"}',
        '{"a": 1} trailing',
        '{"a": NaN}',
        '{"a": 1, "a": 2}',
        '{"a": 1,}',
        '{"mcpServers": {"x": 1}, "mcpServers": {}}',
        "// a comment\n{}",
        "\ufeff{}",
        '{"a": 1',
        # An entry of another kind: nothing in it is a command to change.
        '{"mcpServers": {"' + SLUG + '": {"url": "https://example.invalid/mcp"}}}',
        '{"mcpServers": {"' + SLUG + '": {}}}',
        '{"mcpServers": {"' + SLUG + '": "text"}}',
        '{"mcpServers": {"' + SLUG + '": null}}',
        '{"mcpServers": {"' + SLUG + '": [' + entry + "]}}",
    ):
        with pytest.raises(hostwrite.ByHand):
            hostwrite.json_with_entry(text, command)
    for text in ("[]", '{"mcpServers": []}', '{"a": NaN}', "\ufeff{}", '{"a": 1, "a": 2}'):
        with pytest.raises(hostwrite.ByHand):
            hostwrite.json_without_entry(text)
    # A command that is no list of texts is not written anywhere.
    for bad in ([], [5], ["python", None]):
        with pytest.raises(hostwrite.ByHand):
            hostwrite.json_with_entry("{}", bad)  # type: ignore[arg-type]
        if HAS_TOML:
            with pytest.raises(hostwrite.ByHand):
                hostwrite.toml_with_entry("", bad)  # type: ignore[arg-type]


def random_json(rng: random.Random, depth: int = 0) -> Any:
    kind = rng.choice(
        ["dict", "list", "str", "int", "float", "bool", "null"]
        if depth < 3
        else ["str", "int", "null"]
    )
    if kind == "dict":
        keys = rng.sample(
            ["a", "b", "c", "mcpServers", SLUG, "é", "k k", '"', "}"], rng.randint(0, 4)
        )
        return {key: random_json(rng, depth + 1) for key in keys}
    if kind == "list":
        return [random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    if kind == "str":
        return rng.choice(["", "x", 'q"q', "}{", "[,]", "\\", "é", "\n", "日本", ": ,"])
    if kind == "int":
        return rng.choice([0, 1, -5, 10**30])
    if kind == "float":
        return rng.choice([0.5, -1.25, 1e300])
    return rng.choice([True, False]) if kind == "bool" else None


def test_json_texts_made_at_random_keep_everything_but_the_entry() -> None:
    rng = random.Random(20261114)  # noqa: S311  (the same texts at every run)
    command = command_of(JSON_HOST)
    written = 0
    for _ in range(400):
        top = {
            key: random_json(rng, 1)
            for key in rng.sample(["a", "b", "théme", "z z"], rng.randint(0, 3))
        }
        if rng.random() < 0.7:
            servers = {
                key: random_json(rng, 2)
                for key in rng.sample(["one", "two", SLUG + "2", "x"], rng.randint(0, 3))
            }
            position = rng.randint(0, len(top))
            items = list(top.items())
            items.insert(position, ("mcpServers", servers))
            top = dict(items)
        indent = rng.choice([None, 0, 1, 2, 4, "\t"])
        separators = rng.choice([None, (",", ":"), (" , ", " : ")]) if indent is None else None
        old = json.dumps(top, indent=indent, separators=separators, ensure_ascii=rng.random() < 0.5)
        if rng.random() < 0.3:
            old = old.replace("\n", "\r\n")
        old += rng.choice(["", "\n", "\r\n", "  \n"])
        new = hostwrite.json_with_entry(old, command)
        cut_out(old, new)
        after = strict(new)
        assert after["mcpServers"][SLUG] == ours(JSON_HOST)
        assert same(
            without_entry(after, "mcpServers", drop_empty="mcpServers" not in top), strict(old)
        )
        back = hostwrite.json_without_entry(new)
        assert same(
            without_entry(strict(back), "mcpServers", drop_empty="mcpServers" not in top),
            strict(old),
        )
        if top.get("mcpServers"):
            assert back == old
        # An entry that points elsewhere, among the same servers, is changed in its place.
        if "mcpServers" in top:
            position = rng.randint(0, len(top["mcpServers"]))
            items = list(top["mcpServers"].items())
            other = {"command": "/opt/other", "env": {"T": "}"}, "args": ["x"], "enabled": False}
            if rng.random() < 0.5:
                del other["args"]
            items.insert(position, (SLUG, other))
            there = dict(top, mcpServers=dict(items))
            text = json.dumps(there, indent=indent, separators=separators)
            changed = strict(hostwrite.json_with_entry(text, command))
            assert list(changed["mcpServers"]) == list(there["mcpServers"])
            entry = changed["mcpServers"][SLUG]
            assert (entry["command"], entry["args"]) == (command[0], command[1:])
            assert (entry["env"], entry["enabled"]) == ({"T": "}"}, False)
            assert same(
                without_entry(changed, "mcpServers"), without_entry(strict(text), "mcpServers")
            )
            gone = hostwrite.json_without_entry(text)
            assert same(strict(gone), without_entry(strict(text), "mcpServers"))
            written += 1
    assert written > 200


# -- the four functions that take text and give text: TOML ----------------------------------

TOML_TEXTS = {
    "other tables": (
        'model = "o9"\n\n[profile]\nname = "work"\n\n[mcp_servers.another-tool]\n'
        'command = "tool"\nenv = { TOKEN = "' + SECRET + '" }\n'
    ),
    "comments": (
        "# my settings\n"
        'model = "o9"  # the model\n\n# servers follow\n[mcp_servers.a] # one\ncommand = "x" # c\n'
        "# the end\n"
    ),
    "an array of tables": '[[plugins]]\nname = "a"\n\n[[plugins]]\nname = "b"\nargs = [\n  1,\n]\n',
    "dates": (
        "when = 1979-05-27T07:32:00Z\nday = 1979-05-27\nat = 07:32:00\n"
        "local = 1979-05-27T07:32:00\n"
    ),
    "numbers that are none": "x = nan\ny = inf\nz = -inf\nw = [nan, 1.0, 1]\n",
    "texts of several lines": (
        'text = """\nline one\n[mcp_servers.fake]\ncommand = "not real"\n"""\n'
        "plain = '''\n# not a comment\n\n'''\n"
    ),
    "Windows line endings": 'model = "o9"\r\n\r\n[profile]\r\nname = "work"\r\n',
    "no newline at the end": 'model = "o9"\n\n[profile]\nname = "work"',
    "blank lines at the end": 'model = "o9"\n\n\n',
    "nothing": "",
    "a blank line only": "\n",
    "a comment only": "# nothing here yet",
    "the header of the servers alone": "[mcp_servers]\n",
    "servers written with dotted keys": '[mcp_servers]\nother.command = "x"\nother.args = []\n',
    "keys that only look like the entry": (
        SLUG + ' = "top"\n\n[mcp_servers.' + SLUG + '-old]\ncommand = "a"\n\n'
        '[mcp_servers."' + SLUG + '.old"]\ncommand = "b"\n\n'
        "[other.mcp_servers." + SLUG + ']\ncommand = "c"\n\n'
        "[mcp_servers." + SLUG.capitalize() + ']\ncommand = "d"\n'
    ),
    "a table of servers before other tables": (
        '[mcp_servers.a]\ncommand = "x"\n\n[profile]\nname = "work"\n\n[tools]\nx = 1\n'
    ),
}


@needs_toml
@pytest.mark.parametrize("name", sorted(TOML_TEXTS))
def test_everything_but_the_entry_is_equal_toml(name: str, home: Path) -> None:
    old = TOML_TEXTS[name]
    command = command_of(TOML_HOST)
    before = toml(old)
    new = hostwrite.toml_with_entry(old, command)
    after = toml(new)
    assert after["mcp_servers"][SLUG] == ours(TOML_HOST)
    assert list(after["mcp_servers"][SLUG]) == ["command", "args"]
    assert same(
        without_entry(after, "mcp_servers", drop_empty=True),
        without_entry(before, "mcp_servers", drop_empty=True),
    )
    # Every old line is in the new text, in order: the old text stands first, whole, then
    # at most one blank line, then the three lines of the entry.
    ending = "\r\n" if "\r\n" in old else "\n"
    ends = old.endswith("\n") or not old
    start = old if ends else old + ending
    assert new.startswith(start)
    tail = new[len(start) :]
    block = "".join(line + ending for line in toml_lines(TOML_HOST))
    blank = bool(old) and bool(old.split("\n")[-1 if not ends else -2].strip(" \t\r"))
    assert tail == (ending + block if blank else block)
    if ending == "\r\n":
        assert "\n" not in new.replace("\r\n", "")
    # Through a file.
    place = put(folder_of(TOML_HOST), old)
    found = state_of(TOML_HOST)
    assert found in ("other", "empty")
    answer = connect(TOML_HOST, found)
    assert (answer["connected"], answer["state"], answer["replaced"]) == (True, "present", False)
    assert place.read_bytes() == new.encode("utf-8")
    assert Path(str(place) + hostwrite.FIRST_COPY).read_bytes() == old.encode("utf-8")
    # And out again: the three lines go, and every line that was there before stays.
    if name == "texts of several lines":
        return  # (its header-like line inside a text is a case of its own, below)
    back = hostwrite.toml_without_entry(new)
    assert back in (start, start + ending)
    assert same(
        without_entry(toml(back), "mcp_servers", drop_empty=True),
        without_entry(before, "mcp_servers", drop_empty=True),
    )
    aged(place)
    assert disconnect(TOML_HOST, "present")["file"] == "removed"
    assert place.read_bytes() == back.encode("utf-8")


ENTRY_ELSEWHERE_TOML = f"""# my settings
model = "o9"

# the vault, as I set it up last year
[mcp_servers.{SLUG}]   # one
# where it was
command = "/opt/old/python" # the old one
enabled = false
args = ["-m", "old"]  # and its words
cwd = "/somewhere"
disabled_tools = ["read"]
env = {{ TOKEN = "{SECRET}" }}

[mcp_servers.{SLUG}.tools.search]
approval_mode = "never"  # ask nobody
# below the entry

[mcp_servers.another-tool]
command = "tool"
args = ["command = 1"]
"""

ENTRY_ELSEWHERE_JSON = (
    '{\n  "theme": "dark",\n  "mcpServers": {\n    "first": {"command": "a"},\n'
    '    "' + SLUG + '": {\n      "enabled": false,\n      "command" : "/opt/old/python",\n'
    '      "env": {"TOKEN": "' + SECRET + '"},\n      "args":\n        ["-m",\n         "old"],\n'
    '      "cwd": "/somewhere",\n      "disabled_tools": ["read"],\n'
    '      "tools": {"search": {"approval_mode": "never"}}\n    },\n'
    '    "last": {"command": "b", "args": ["command"]}\n  }\n}\n'
)


def test_an_entry_keeps_what_else_it_holds(home: Path) -> None:
    command = command_of(JSON_HOST)
    # JSON: only the two values are other bytes.
    old = ENTRY_ELSEWHERE_JSON
    expected = old.replace('"/opt/old/python"', json.dumps(command[0])).replace(
        '["-m",\n         "old"]', "[" + ", ".join(json.dumps(part) for part in command[1:]) + "]"
    )
    assert hostwrite.json_with_entry(old, command) == expected
    place = put(folder_of(JSON_HOST), old)
    assert state_of(JSON_HOST) == "differs"
    told = Told()
    answer = connect(JSON_HOST, "differs", told)
    assert (answer["connected"], answer["state"], answer["replaced"]) == (
        True,
        "present_more",
        True,
    )
    assert answer["copy"] == hostconfig.shown_path(place) + hostwrite.LAST_COPY
    assert place.read_text(encoding="utf-8") == expected
    after, before = strict(expected), strict(old)
    entry = after["mcpServers"][SLUG]
    assert list(after["mcpServers"]) == ["first", SLUG, "last"]
    assert list(entry) == list(before["mcpServers"][SLUG])
    assert {key: entry[key] for key in entry if key not in ("command", "args")} == {
        "enabled": False,
        "env": {"TOKEN": SECRET},
        "cwd": "/somewhere",
        "disabled_tools": ["read"],
        "tools": {"search": {"approval_mode": "never"}},
    }
    seen = looked(JSON_HOST)
    assert (seen["state"], seen["switched_off"]) == ("present_more", True)
    assert told.actions == ["connect"]
    if not HAS_TOML:
        return
    # TOML: the two lines stand where the old ones stood, and every other line is the old one.
    command = command_of(TOML_HOST)
    old = ENTRY_ELSEWHERE_TOML
    block = toml_lines(TOML_HOST)
    expected = old.replace('command = "/opt/old/python" # the old one', block[1]).replace(
        'args = ["-m", "old"]  # and its words', block[2]
    )
    assert expected != old
    assert hostwrite.toml_with_entry(old, command) == expected
    place = put(folder_of(TOML_HOST), old)
    assert state_of(TOML_HOST) == "differs"
    answer = connect(TOML_HOST, "differs")
    assert (answer["connected"], answer["state"], answer["replaced"]) == (
        True,
        "present_more",
        True,
    )
    assert place.read_text(encoding="utf-8") == expected
    after, before = toml(expected), toml(old)
    entry = after["mcp_servers"][SLUG]
    assert list(entry) == list(before["mcp_servers"][SLUG])
    assert {key: entry[key] for key in entry if key not in ("command", "args")} == {
        "enabled": False,
        "cwd": "/somewhere",
        "disabled_tools": ["read"],
        "env": {"TOKEN": SECRET},
        "tools": {"search": {"approval_mode": "never"}},
    }
    for comment in (
        "# my settings",
        "# one",
        "# where it was",
        "# ask nobody",
        "# below the entry",
    ):
        assert comment in expected
    assert "# the old one" not in expected  # a replaced line goes whole, its comment included
    seen = looked(TOML_HOST)
    assert (seen["state"], seen["switched_off"]) == ("present_more", True)
    # Taken out, the entry goes with what is below it. The lines that are only a comment
    # stay; a comment at the end of a line that goes, goes with it.
    aged(place)
    told = Told()
    assert disconnect(TOML_HOST, "present_more", told)["file"] == "removed"
    assert told.actions == ["disconnect"]
    left = place.read_text(encoding="utf-8")
    assert (
        left
        == """# my settings
model = "o9"

# the vault, as I set it up last year
# where it was

# below the entry

[mcp_servers.another-tool]
command = "tool"
args = ["command = 1"]
"""
    )
    assert Path(str(place) + hostwrite.LAST_COPY).read_text(encoding="utf-8") == expected


@needs_toml
def test_an_entry_that_points_elsewhere_is_changed_in_its_place(home: Path) -> None:
    block = toml_lines(TOML_HOST)
    command = command_of(TOML_HOST)
    # The two lines stand where the old ones stood, with the blanks that stood before them.
    old = f'a = 1\n[mcp_servers.{SLUG}]\n  args   =   ["x"]\n  command="/opt/other"\n\n[b]\nc = 2\n'
    assert hostwrite.toml_with_entry(old, command) == (
        f"a = 1\n[mcp_servers.{SLUG}]\n  {block[2]}\n  {block[1]}\n\n[b]\nc = 2\n"
    )
    # Without a line for the arguments, theirs is put right after the command's.
    old = f'[mcp_servers.{SLUG}]\n\tcommand = "/opt/other"\n\tenabled = true\n'
    assert hostwrite.toml_with_entry(old, command) == (
        f"[mcp_servers.{SLUG}]\n\t{block[1]}\n\t{block[2]}\n\tenabled = true\n"
    )
    # The entry is the last thing in the file, which does not end with a line ending.
    old = f'x = 1\r\n[mcp_servers.{SLUG}]\r\ncommand = "/opt/other"'
    assert hostwrite.toml_with_entry(old, command) == (
        f"x = 1\r\n[mcp_servers.{SLUG}]\r\n{block[1]}\r\n{block[2]}"
    )
    old = f'[mcp_servers.{SLUG}]\nargs = []\ncommand = "/opt/other"'
    assert hostwrite.toml_with_entry(old, command) == (
        f"[mcp_servers.{SLUG}]\n{block[2]}\n{block[1]}"
    )
    # The header may be written with blanks, with the name in quotes, and with a comment.
    for header in (
        f"[ mcp_servers . {SLUG} ]",
        f'[mcp_servers."{SLUG}"]',
        f"[mcp_servers.'{SLUG}']  # the vault",
        f"\t[mcp_servers.{SLUG}]",
    ):
        old = f'{header}\ncommand = "/opt/other"\nargs = []\n'
        assert hostwrite.toml_with_entry(old, command) == f"{header}\n{block[1]}\n{block[2]}\n"
        assert hostwrite.toml_without_entry(old) == ""
    # JSON: the entry keeps its place among the servers, and its members their order.
    command = command_of(JSON_HOST)
    old_json = '{"mcpServers": {"a": 1, "' + SLUG + '": {"args": ["x"], "command": "y"}, "b": 2}}'
    assert hostwrite.json_with_entry(old_json, command) == (
        '{"mcpServers": {"a": 1, "'
        + SLUG
        + '": {"args": '
        + "["
        + ", ".join(json.dumps(part) for part in command[1:])
        + '], "command": '
        + json.dumps(command[0])
        + '}, "b": 2}}'
    )
    # The name of the entry may be written with escapes, and stays as it is written.
    escaped = (
        '{"mcpServers": {"'
        + SLUG[:-1]
        + "\\u00"
        + format(ord(SLUG[-1]), "02x")
        + '": {"command": "y"}}}'
    )
    assert strict(escaped)["mcpServers"][SLUG] == {"command": "y"}
    changed = hostwrite.json_with_entry(escaped, command)
    assert changed.startswith(escaped[: escaped.index('"y"')])
    assert strict(changed)["mcpServers"][SLUG] == ours(JSON_HOST)
    assert hostwrite.json_without_entry(changed) == '{"mcpServers": {}}'
    # Without arguments, they are put right after the command.
    old_json = '{"mcpServers": {"' + SLUG + '": {"command": "y", "enabled": true}}}'
    assert strict(hostwrite.json_with_entry(old_json, command))["mcpServers"][SLUG] == {
        **ours(JSON_HOST),
        "enabled": True,
    }
    assert list(strict(hostwrite.json_with_entry(old_json, command))["mcpServers"][SLUG]) == [
        "command",
        "args",
        "enabled",
    ]
    # Through the files: the look said that it points elsewhere, and now it is ours.
    place = put(folder_of(TOML_HOST), f'[mcp_servers.{SLUG}]\ncommand = "/opt/other"\nargs = []\n')
    assert state_of(TOML_HOST) == "differs"
    answer = connect(TOML_HOST, "differs")
    assert (answer["connected"], answer["state"], answer["replaced"]) == (True, "present", True)
    assert place.read_text(encoding="utf-8") == "\n".join(toml_lines(TOML_HOST)) + "\n"


NOT_PLAIN_LINES = {
    "the servers as an inline table": (
        'mcp_servers = { other = { command = "x" } }\n',
        "other",
    ),
    "the servers as an inline table that holds the entry": (
        "mcp_servers = { " + SLUG + ' = { command = "x", args = [] } }\n',
        "differs",
    ),
    "the entry with dotted keys": (
        "[mcp_servers]\n" + SLUG + '.command = "x"\n' + SLUG + ".args = []\n",
        "differs",
    ),
    "the entry with dotted keys from the top": (
        "mcp_servers." + SLUG + '.command = "x"\n',
        "differs",
    ),
    "the entry as an inline table": (
        "[mcp_servers]\n" + SLUG + ' = { command = "x" }\n',
        "differs",
    ),
    "the header twice, once inside a text of several lines": (
        'note = """\n[mcp_servers.' + SLUG + ']\n"""\n[mcp_servers.' + SLUG + ']\ncommand = "x"\n',
        "differs",
    ),
    "the header twice, the second time inside a text of several lines": (
        "[mcp_servers." + SLUG + ']\ncommand = "x"\nargs = []\n\n[other]\n'
        'note = """\n[mcp_servers.' + SLUG + ']\n"""\n',
        "differs",
    ),
    "the header inside a text of several lines, and no entry": (
        # With the three lines added, the header would stand in the text twice, and a
        # removal could not take out what the connect had put in.
        'note = """\nHow I set up my servers:\n[mcp_servers.' + SLUG + ']\n"""\n',
        "other",
    ),
    "the header inside a text, written with blanks and quotes, and no entry": (
        "[notes]\ntext = '''\n\t[ mcp_servers . \"" + SLUG + "\" ]  # mine\n'''\n",
        "other",
    ),
    "the header once, inside a text, and the entry elsewhere": (
        'note = """\n[mcp_servers.' + SLUG + ']\ncommand = "x"\n"""\n'
        "[mcp_servers]\n" + SLUG + '.command = "x"\n',
        "differs",
    ),
    "an entry without a command": (
        "[mcp_servers." + SLUG + ']\nurl = "https://example.invalid/mcp"\n',
        "differs",
    ),
    "arguments over several lines": (
        "[mcp_servers." + SLUG + ']\ncommand = "x"\nargs = [\n  "a",\n  "b",\n]\n',
        "differs",
    ),
    "a command over several lines": (
        "[mcp_servers." + SLUG + ']\ncommand = """\nx"""\nargs = []\n',
        "differs",
    ),
    "the command twice, once inside a text": (
        "[mcp_servers." + SLUG + ']\nnote = """\ncommand = "y"\n"""\ncommand = "x"\n',
        "differs",
    ),
    "the arguments twice, once inside a text": (
        "[mcp_servers." + SLUG + ']\ncommand = "x"\nnote = """\nargs = 1\n"""\nargs = []\n',
        "differs",
    ),
    "the header with its first name in quotes": (
        '["mcp_servers".' + SLUG + ']\ncommand = "x"\n',
        "differs",
    ),
    "the entry as an array of tables": (
        "[[mcp_servers." + SLUG + ']]\ncommand = "x"\n',
        "differs",
    ),
    "the entry only as a table below it": (
        "[mcp_servers." + SLUG + '.env]\nTOKEN = "1"\n',
        "differs",
    ),
}


@needs_toml
@pytest.mark.parametrize("name", sorted(NOT_PLAIN_LINES))
def test_toml_that_is_not_plain_lines_is_left_to_the_owner(name: str, home: Path) -> None:
    text, state = NOT_PLAIN_LINES[name]
    toml(text)  # it is TOML, and the reader reads it
    with pytest.raises(hostwrite.ByHand):
        hostwrite.toml_with_entry(text, command_of(TOML_HOST))
    place = put(folder_of(TOML_HOST), text)
    before = tree(home)
    assert state_of(TOML_HOST) == state
    told = Told()
    assert connect(TOML_HOST, state, told) == {
        "connected": False,
        "state": "by_hand",
        "path": hostconfig.shown_path(place),
    }
    assert tree(home) == before
    assert (told.entered, told.actions) == (0, [])


@needs_toml
def test_a_toml_entry_that_cannot_be_taken_out_by_the_rules_is_left(home: Path) -> None:
    block = "\n".join(toml_lines(TOML_HOST)) + "\n"
    cases = {
        "a table below it that is not next to it": (
            block + "\n[other]\nx = 1\n\n[mcp_servers." + SLUG + '.env]\nTOKEN = "1"\n'
        ),
        "the header also inside a text of several lines": (
            'note = """\n[mcp_servers.' + SLUG + ']\n"""\n' + block
        ),
        "a value of several lines whose lines look like headers": (
            block + "matrix = [\n  [1, 2],\n]\n"
        ),
        "the entry with dotted keys": (
            "[mcp_servers]\n"
            + SLUG
            + ".command = "
            + toml_string(command_of(TOML_HOST)[0])
            + "\n"
            + SLUG
            + ".args = ["
            + ", ".join(toml_string(part) for part in command_of(TOML_HOST)[1:])
            + "]\n"
        ),
    }
    for name, text in cases.items():
        with pytest.raises(hostwrite.ByHand):
            hostwrite.toml_without_entry(text)
        place = put(folder_of(TOML_HOST), text)
        before = tree(home)
        state = state_of(TOML_HOST)
        assert state in ("present", "present_more"), name
        told = Told()
        # The app is taken out of the vault all the same; the file is left, and said to be.
        assert disconnect(TOML_HOST, state, told) == {
            "file": "by_hand",
            "path": hostconfig.shown_path(place),
            "copy": None,
        }
        assert told.actions == ["disconnect-kept"]
        assert tree(home) == before, name
    with pytest.raises(hostwrite.ByHand):
        hostwrite.toml_without_entry('model = "o9"\n')
    with pytest.raises(hostwrite.ByHand):
        hostwrite.toml_without_entry("= 1\n")


@needs_toml
def test_a_toml_entry_is_taken_out_and_comments_and_blank_lines_stay() -> None:
    without = hostwrite.toml_without_entry
    block = "\n".join(toml_lines(TOML_HOST)) + "\n"
    # The only thing in the file.
    assert without(block) == ""
    assert without(block.rstrip("\n")) == ""
    # The last thing: the blank line that stood before it stays.
    assert without('model = "o9"\n\n' + block) == 'model = "o9"\n\n'
    # Between two tables: of two blank lines that meet, one goes.
    assert without("[a]\nx = 1\n\n" + block + "\n[b]\ny = 2\n") == "[a]\nx = 1\n\n[b]\ny = 2\n"
    assert without("[a]\nx = 1\n" + block + "[b]\ny = 2\n") == "[a]\nx = 1\n[b]\ny = 2\n"
    assert without("[a]\nx = 1\n\n\n" + block + "\n\n[b]\n") == "[a]\nx = 1\n\n\n\n[b]\n"
    # Comments directly above the entry, inside it and directly below it stay.
    text = (
        "# above\n"
        + block.replace("\nargs", '\n# parked: command = "other"\n\nargs')
        + "# below\n[b]\ny = 2\n"
    )
    assert without(text) == '# above\n# parked: command = "other"\n\n# below\n[b]\ny = 2\n'
    # A header `[mcp_servers]` that was written out stays.
    assert without("[mcp_servers]\n\n" + block) == "[mcp_servers]\n\n"
    # Tables below the entry go with it, and tables of other servers do not.
    text = (
        block
        + f'enabled = false\n\n[mcp_servers.{SLUG}.env]\nA = "1"\n\n'
        + f'[[mcp_servers.{SLUG}.rules]]\nx = 1\n\n[mcp_servers.{SLUG}-2]\ncommand = "z"\n'
    )
    assert without(text) == f'\n\n\n[mcp_servers.{SLUG}-2]\ncommand = "z"\n'
    # Windows line endings stay what they are.
    assert without("a = 1\r\n\r\n" + block.replace("\n", "\r\n") + "\r\n[b]\r\n") == (
        "a = 1\r\n\r\n[b]\r\n"
    )


@needs_toml
def test_a_toml_entry_is_taken_out_wherever_the_table_of_servers_was_first_met(
    home: Path,
) -> None:
    """The reader of TOML puts ``mcp_servers`` where it first meets one of its tables.

    Where that was the entry, and another table stands between it and the next server, the
    table of servers comes later in the parsed file once the entry is gone. No line moved,
    and every key and value is what it was: the entry is taken out.
    """
    without = hostwrite.toml_without_entry
    block = "\n".join(toml_lines(TOML_HOST)) + "\n"
    rest = "[b]\nx = 1\n[mcp_servers.other]\ncommand = 'o'\n"
    assert list(toml(block + rest)) == ["mcp_servers", "b"]
    assert list(toml(rest)) == ["b", "mcp_servers"]
    assert without(block + rest) == rest
    # The same tables in other orders.
    assert without(block + "[b]\nx = 1\n") == "[b]\nx = 1\n"
    assert without(block + "[mcp_servers.other]\ncommand = 'o'\n[b]\nx = 1\n") == (
        "[mcp_servers.other]\ncommand = 'o'\n[b]\nx = 1\n"
    )
    assert without("[mcp_servers.other]\ncommand = 'o'\n[b]\nx = 1\n" + block) == (
        "[mcp_servers.other]\ncommand = 'o'\n[b]\nx = 1\n"
    )
    assert without("[b]\nx = 1\n" + block + "[c]\ny = 2\n[mcp_servers.other]\ncommand = 'o'\n") == (
        "[b]\nx = 1\n[c]\ny = 2\n[mcp_servers.other]\ncommand = 'o'\n"
    )
    # How a file gets there by itself: a connect adds the entry at the end of a file without
    # servers; later a program adds a table at the end, and later somebody a second server.
    place = put(folder_of(TOML_HOST), 'model = "o9"\n')
    assert connect(TOML_HOST, "other")["connected"] is True
    later = (
        '\n[projects."/home/me/work"]\ntrust_level = "trusted"\n\n'
        '[mcp_servers.github]\ncommand = "gh-mcp"\nargs = []\n'
    )
    connected = place.read_text(encoding="utf-8") + later
    put(place, connected)
    assert state_of(TOML_HOST) == "present"
    told = Told()
    assert disconnect(TOML_HOST, "present", told) == {
        "file": "removed",
        "path": hostconfig.shown_path(place),
        "copy": hostconfig.shown_path(place) + hostwrite.LAST_COPY,
    }
    assert told.actions == ["disconnect"]
    assert place.read_text(encoding="utf-8") == 'model = "o9"\n' + later
    assert Path(str(place) + hostwrite.LAST_COPY).read_text(encoding="utf-8") == connected
    assert state_of(TOML_HOST) == "other"

    # What is left aside is the place of that one table, and nothing else. A file with the
    # same layout in which something else differs once the lines are gone is left: here a
    # text of several lines inside the entry holds lines that read like headers, and what is
    # left of it are two tables the file did not have.
    hidden = (
        block
        + "note = '''\n[b]\nx = 1\n[zz] # '''\n"
        + "[c]\ny = 1\n[mcp_servers.other]\ncommand = 'o'\n"
    )
    assert list(toml(hidden)) == ["mcp_servers", "c"]
    with pytest.raises(hostwrite.ByHand):
        without(hidden)
    for item in place.parent.iterdir():
        item.unlink()
    put(place, hidden)
    before = tree(home)
    assert state_of(TOML_HOST) == "present_more"
    told = Told()
    assert disconnect(TOML_HOST, "present_more", told)["file"] == "by_hand"
    assert (told.actions, tree(home)) == (["disconnect-kept"], before)

    # The comparison itself. Without the word for it the place of the table of servers
    # counts, as it does for JSON and for an entry that is put in; with it, the order of
    # every other key of the top still counts, and so does the order of the servers.
    def rest_of(data: dict[str, Any], **more: Any) -> Any:
        return hostwrite._rest(data, "mcp_servers", entry="whole", drop_empty=True, **more)

    first = {"mcp_servers": {SLUG: {}, "a": {"k": 1}, "z": {}}, "b": 1, "c": 2}
    moved = {"b": 1, "mcp_servers": {"a": {"k": 1}, "z": {}}, "c": 2}
    assert list(rest_of(first)) == ["mcp_servers", "b", "c"]
    assert list(rest_of(first, servers_last=True)) == ["b", "c", "mcp_servers"]
    assert list(first) == ["mcp_servers", "b", "c"]  # (what was handed in is not changed)
    assert not hostwrite._equal(rest_of(first), rest_of(moved))
    assert hostwrite._equal(rest_of(first, servers_last=True), rest_of(moved, servers_last=True))
    for differs in (
        {"c": 2, "mcp_servers": {"a": {"k": 1}, "z": {}}, "b": 1},  # two other keys changed places
        {"b": 1, "mcp_servers": {"z": {}, "a": {"k": 1}}, "c": 2},  # two servers changed places
        {"b": 1, "mcp_servers": {"a": {"k": 1.0}, "z": {}}, "c": 2},  # a type
        {"b": 1, "mcp_servers": {"a": {"k": 1}, "z": {}}, "c": 2, "d": 3},  # a key more
        {"b": 1, "mcp_servers": {"a": {"k": 1}}, "c": 2},  # a server less
    ):
        assert not hostwrite._equal(
            rest_of(first, servers_last=True), rest_of(differs, servers_last=True)
        ), differs
    # A table of servers that is empty without the entry is dropped on both sides, as before.
    assert rest_of({"mcp_servers": {SLUG: {}}, "b": 1}, servers_last=True) == {"b": 1}
    # JSON is spliced as text, and where `mcpServers` stands is compared there as ever.
    assert list(
        hostwrite._rest(
            {"mcpServers": {SLUG: {}}, "b": 1}, "mcpServers", entry="whole", drop_empty=False
        )
    ) == ["mcpServers", "b"]


@needs_toml
def test_no_entry_is_put_into_toml_that_could_not_be_taken_out_again(home: Path) -> None:
    command = command_of(TOML_HOST)
    block = "\n".join(toml_lines(TOML_HOST)) + "\n"
    # A line that reads like the entry's header, inside a text of several lines: with the
    # three lines added the header would stand in the text twice, which is what a removal
    # refuses. So the three lines are not added.
    for text in (
        'notes = """\n[mcp_servers.' + SLUG + ']\n"""\n',
        "notes = '''\n  [ mcp_servers.'" + SLUG + "' ]\t# as I had it\n'''\n[b]\nx = 1\n",
        '[b]\nlong = """\ncommand = "x"\n[mcp_servers."' + SLUG + '"]\n"""',
    ):
        assert SLUG not in toml(text).get("mcp_servers", {})
        with pytest.raises(hostwrite.ByHand):
            hostwrite.toml_with_entry(text, command)
    # What only looks a little like it is no reason: a comment, the header of another
    # server, the header as a part of a longer line, a table below the entry's name.
    for text in (
        "# [mcp_servers." + SLUG + "]\n",
        'notes = """\n[mcp_servers.' + SLUG + '-old]\n"""\n',
        'notes = """\nsee [mcp_servers.' + SLUG + '] below\n"""\n',
        'notes = """\n[mcp_servers.' + SLUG + '.env]\n"""\n',
        'notes = "[mcp_servers.' + SLUG + ']"\n',
    ):
        new = hostwrite.toml_with_entry(text, command)
        assert new == text + "\n" + block
        assert hostwrite.toml_without_entry(new) == text + "\n"


def test_two_values_are_equal_only_with_their_types_and_their_order() -> None:
    equal = hostwrite._equal
    nan = float("nan")
    assert equal({"a": [1, {"b": None}], "c": "x"}, {"a": [1, {"b": None}], "c": "x"})
    assert equal(nan, nan)
    assert equal([nan, {"x": nan}], [float("nan"), {"x": float("nan")}])
    for left, right in (
        (1, 1.0),
        (True, 1),
        (False, 0),
        (0, None),
        ("1", 1),
        ([1, 2], [2, 1]),
        ([1], [1, 1]),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
        ({"a": 1}, {"a": 1, "b": 2}),
        ({"a": [1, {"b": 1}]}, {"a": [1, {"b": 1.0}]}),
        (nan, 1.0),
        ([], {}),
    ):
        assert not equal(left, right), (left, right)
    # Nesting as deep as a file can hold costs no stack.
    deep_left: Any = "end"
    deep_right: Any = "end"
    for _ in range(100_000):
        deep_left, deep_right = [deep_left], [deep_right]
    assert equal(deep_left, deep_right)
    while isinstance(deep_left, list):  # taken apart by hand: Python frees it by recursion
        deep_left, deep_right = deep_left[0], deep_right[0]


# -- files: what a look says, and what is never written ---------------------------------------

OTHER_JSON = (
    '{\n  "theme": "dark",\n  "mcpServers": {\n    "another-tool": {\n      "command": "tool",\n'
    '      "env": {"TOKEN": "' + SECRET + '"}\n    }\n  }\n}\n'
)
OTHER_TOML = (
    'model = "o9"\n\n[mcp_servers.another-tool]\ncommand = "tool"\n'
    'env = { TOKEN = "' + SECRET + '" }\n'
)


def language_of(host: str) -> str:
    return "json" if host == JSON_HOST else "toml"


def other(host: str, mark: str = "") -> str:
    """A settings file with the entry of another tool and none of the product."""
    if language_of(host) == "json":
        return OTHER_JSON.replace('"dark"', f'"dark{mark}"')
    return OTHER_TOML.replace('"o9"', f'"o9{mark}"')


def with_ours(host: str, *, client: str | None = None, store: str = STORE) -> str:
    """The same file with the product's entry, as the writer makes it."""
    command = command_of(client or host, store)
    if language_of(host) == "json":
        return hostwrite.json_with_entry(OTHER_JSON, command)
    return hostwrite.toml_with_entry(OTHER_TOML, command)


def elsewhere(host: str) -> str:
    """The same file with an entry of the product's name that points at another vault."""
    return with_ours(host, store="/srv/another vault")


def quickly(call: Callable[[], Any]) -> Any:
    """Run a call that must not wait for anything, and fail if it does."""
    done: list[tuple[bool, Any]] = []

    def run() -> None:
        try:
            done.append((True, call()))
        except BaseException as exc:
            done.append((False, exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "the call waits"
    passed, value = done[0]
    if not passed:
        raise value
    return value


def lstat_as_of_another(name: str) -> Callable[..., os.stat_result]:
    """An ``os.lstat`` that says of the file ``name`` that it belongs to another account."""
    real, mine = os.lstat, os.getuid()

    def lstat(path: Any, **kwargs: Any) -> os.stat_result:
        status = real(path, **kwargs)
        if os.fspath(path) != name:
            return status
        fields = list(status)
        fields[stat.ST_UID] = mine + 1
        return os.stat_result(fields)

    return lstat


def restored(path: Path, content: str) -> None:
    """Write other content into a file in place, and give it the times it had before."""
    before = path.stat()
    path.write_text(content, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )


def exchange(path: Path, content: str) -> None:
    """Put another file in the place of ``path``, as a program that saves by replacing does."""
    fresh = path.with_name(path.name + ".saved-by-another-program")
    put(fresh, content)
    os.replace(fresh, path)


def test_the_words_of_the_module_are_fixed() -> None:
    assert hostwrite.WRITABLE == ("claude-desktop", "chatgpt-desktop", "grok-build")
    assert hostwrite.FIRST_COPY == ".before-" + SLUG
    assert hostwrite.LAST_COPY == ".last-" + SLUG
    assert hostwrite.MAX_BYTES == hostcheck.MAX_BYTES == 1024 * 1024
    assert (hostwrite.QUIET_SECONDS, hostwrite.LEFTOVER_SECONDS) == (5, 60)
    assert hostwrite.PROBLEMS == ("expands", "unstable", "moved")
    assert hostwrite.REFUSALS == ("recent", "by_hand", "no_copy", "changed", "failed")
    assert not set(hostwrite.PROBLEMS + hostwrite.REFUSALS) & set(hostcheck.STATES)
    # The three are the tiles that name a program.
    assert [tile.host for tile in hostconfig.TILES if tile.label] == list(hostwrite.WRITABLE)
    assert issubclass(hostwrite.ByHand, Exception)


def test_only_the_three_applications_and_only_real_states(home: Path) -> None:
    told = Told()
    for host in ("codex", "claude-code", "openclaw", "generic", "grok-web", "", "../../etc/passwd"):
        with pytest.raises(ValueError, match="not an application"):
            hostwrite.look(host, client="my-app", store=STORE)
        with pytest.raises(ValueError, match="not an application"):
            connect(host, "no_file", told, client="my-app")
        with pytest.raises(ValueError, match="not an application"):
            disconnect(host, "present", told, client="my-app")
    for expect in ("", "written", "recent", "changed", "Present", "no_copy"):
        with pytest.raises(ValueError, match="not one a look can find"):
            connect(JSON_HOST, expect, told)
        with pytest.raises(ValueError, match="not one a look can find"):
            disconnect(JSON_HOST, expect, told)
    for client in ("../../etc/passwd", "Claude", "a b", ""):
        with pytest.raises(ValueError, match="cannot be used as a name"):
            hostwrite.look(JSON_HOST, client=client, store=STORE)
        with pytest.raises(ValueError, match="cannot be used as a name"):
            hostwrite.connect(JSON_HOST, client=client, store=STORE, expect="no_file", section=told)
        with pytest.raises(ValueError, match="cannot be used as a name"):
            hostwrite.disconnect(
                JSON_HOST, client=client, store=STORE, expect="present", section=told
            )
    assert (told.entered, told.actions) == (0, [])
    assert tree(home) == {}


@pytest.mark.parametrize("host", HOSTS)
def test_the_look_says_fixed_words_and_changes_nothing(
    host: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = place_of(host)
    shown = hostconfig.shown_path(place)
    tile = next(tile for tile in hostconfig.TILES if tile.host == host)
    expected = {
        "host": host,
        "label": tile.label,
        "readers": list(tile.readers),
        "state": "no_folder",
        "problem": None,
        "format": "JSON" if host == JSON_HOST else "TOML",
        "path": shown,
        "folder": hostconfig.shown_path(place.parent),
        "real_folder": None,
        "variable": None,
        "entry": SLUG,
        "command": command_of(host),
        "section": None,
        "switched_off": False,
        "other_name": None,
        "copies": [],
    }
    assert looked(host) == expected
    assert list(looked(host)) == list(expected)
    assert tree(home) == {}
    place.parent.mkdir(parents=True)
    assert looked(host) == {**expected, "state": "no_file"}
    put(place, other(host))
    before = tree(home)
    section = "filled" if host == JSON_HOST else None
    assert looked(host) == {**expected, "state": "other", "section": section}
    put(place, with_ours(host))
    assert looked(host) == {**expected, "state": "present"}
    put(place, elsewhere(host))
    assert looked(host) == {**expected, "state": "differs"}
    # The copies that lie beside the file are named, with the time they were made; a link or
    # a folder at such a place is no copy. Nothing of them is read.
    first = Path(str(place) + hostwrite.FIRST_COPY)
    last = Path(str(place) + hostwrite.LAST_COPY)
    put(first, SECRET)
    os.utime(first, (1_800_000_000, 1_800_000_000))
    copies = looked(host)["copies"]
    assert copies == [{"path": shown + hostwrite.FIRST_COPY, "made_at": "2027-01-15T08:00:00Z"}]
    last.symlink_to(first)
    assert looked(host)["copies"] == copies
    last.unlink()
    last.mkdir()
    assert looked(host)["copies"] == copies
    last.rmdir()
    put(last, SECRET)
    both = looked(host)["copies"]
    assert [copy["path"] for copy in both] == [
        shown + hostwrite.FIRST_COPY,
        shown + hostwrite.LAST_COPY,
    ]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", both[1]["made_at"])
    # A file with a time that no calendar has is named all the same, without a time. (Most
    # file systems cannot hold such a time, so the function is asked directly.)
    for strange in (2**60, -(2**60)):
        fields = (0o100600, 1, 1, 1, 0, 0, 0, 0, strange, 0)
        assert hostwrite._made_at(os.stat_result(fields)) is None
    fields = (0o100600, 1, 1, 1, 0, 0, 0, 0, 1_800_000_000, 0)
    assert hostwrite._made_at(os.stat_result(fields)) == "2027-01-15T08:00:00Z"
    # A variable that decided the place is named.
    if host != TOML_HOST:
        moved = tmp_path / "moved by a variable"
        variable = "GROK_HOME" if host == "grok-build" else "XDG_CONFIG_HOME"
        if not (host == JSON_HOST and sys.platform == "darwin"):
            monkeypatch.setenv(variable, str(moved))
            answer = looked(host)
            assert (answer["variable"], answer["state"]) == (variable, "no_folder")
            assert answer["path"].startswith(str(moved))
            monkeypatch.delenv(variable)
    # A look removes no leftover and writes nothing.
    left = place.with_name(f".{place.name}.{'a' * 24}.tmp")
    aged(put(left, "left by a call that was cut"), 600)
    before = tree(home)
    for _ in range(3):
        looked(host)
    assert tree(home) == before


def test_a_look_names_nothing_from_the_file(home: Path) -> None:
    hosts = [JSON_HOST, TOML_HOST, "grok-build"] if HAS_TOML else [JSON_HOST]
    for host in hosts:
        place = folder_of(host)
        for content in (other(host), with_ours(host), elsewhere(host), SECRET, "{" + SECRET):
            put(place, content)
            answer = looked(host)
            assert SECRET not in json.dumps(answer)
            for expect in hostcheck.STATES:
                told = Told()
                told.fail = {action: Refused(503, "unavailable") for action in CONNECT_ACTIONS}
                with contextlib.suppress(Refused):
                    assert SECRET not in json.dumps(connect(host, expect, told))


NEVER_WRITTEN = [
    pytest.param("text that is not JSON"),
    pytest.param("JSON with a key twice"),
    pytest.param("servers that are a list"),
    pytest.param("JSON that is no object"),
    pytest.param("TOML that does not parse", marks=needs_toml),
    pytest.param("TOML whose servers are no table", marks=needs_toml),
    pytest.param("bytes that are not UTF-8"),
    pytest.param("a byte order mark before JSON"),
    pytest.param("a byte order mark before TOML", marks=needs_toml),
    pytest.param("a symbolic link to a valid file"),
    pytest.param("a directory"),
    pytest.param("a named pipe"),
    pytest.param("a file of 1 MiB and one byte"),
    pytest.param("a file with two names"),
    pytest.param("a file of another account"),
    pytest.param("a file the system refuses to open"),
    pytest.param("a TOML file on a Python without a reader for it"),
]


def arrange(case: str, home: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Make the file of one case. Returns the application and the word a look must say."""
    host = TOML_HOST if "TOML" in case else JSON_HOST
    place = folder_of(host)
    if case == "text that is not JSON":
        put(place, '{"mcpServers": {')
        return host, "unreadable"
    if case == "JSON with a key twice":
        put(place, '{"theme": "dark", "theme": "light"}')
        return host, "unreadable"
    if case == "servers that are a list":
        put(place, '{"mcpServers": ["' + SLUG + '"]}')
        return host, "unreadable"
    if case == "JSON that is no object":
        put(place, "[" + OTHER_JSON + "]")
        return host, "unreadable"
    if case == "TOML that does not parse":
        put(place, OTHER_TOML + "= 1\n")
        return host, "unreadable"
    if case == "TOML whose servers are no table":
        put(place, 'mcp_servers = "x"\n')
        return host, "unreadable"
    if case == "bytes that are not UTF-8":
        put(place, OTHER_JSON.encode() + b"\xff")
        return host, "unreadable"
    if case == "a byte order mark before JSON":
        put(place, b"\xef\xbb\xbf" + OTHER_JSON.encode())
        return host, "unreadable"
    if case == "a byte order mark before TOML":
        put(place, b"\xef\xbb\xbf" + OTHER_TOML.encode())
        return host, "unreadable"
    if case == "a symbolic link to a valid file":
        put(home / "kept elsewhere.json", OTHER_JSON)
        place.symlink_to(home / "kept elsewhere.json")
        return host, "not_read"
    if case == "a directory":
        place.mkdir()
        return host, "not_read"
    if case == "a named pipe":
        os.mkfifo(place)
        return host, "not_read"
    if case == "a file of 1 MiB and one byte":
        put(place, OTHER_JSON.encode() + b" " * (hostwrite.MAX_BYTES + 1 - len(OTHER_JSON)))
        assert place.stat().st_size == hostwrite.MAX_BYTES + 1
        return host, "not_read"
    if case == "a file with two names":
        put(place, OTHER_JSON)
        os.link(place, home / "a second name.json")
        aged(place)
        return host, "not_read"
    if case == "a file of another account":
        put(place, OTHER_JSON)
        mine = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: mine + 1)
        return host, "not_read"
    if case == "a file the system refuses to open":
        put(place, OTHER_JSON, mode=0o000)
        if os.geteuid() != 0:
            return host, "no_access"
        # Root is refused nothing, so for root the refusal is put into the call itself.
        real_open = os.open

        def refuse(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if os.fspath(path) == place.name:
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", refuse)
        return host, "no_access"
    assert case == "a TOML file on a Python without a reader for it"
    put(place, OTHER_TOML)
    monkeypatch.setattr(hostcheck, "_toml_loads", lambda: None)
    return host, "unsupported"


@pytest.mark.parametrize("case", NEVER_WRITTEN)
def test_no_state_but_the_seven_is_written(
    case: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, word = arrange(case, home, monkeypatch)
    place = place_of(host)
    # A temporary file that an earlier call left behind lies there, too: a call that changes
    # nothing removes nothing.
    aged(put(place.with_name(f".{place.name}.{'a' * 24}.tmp"), "left by a call that was cut"), 600)
    before = tree(home)
    assert quickly(lambda: state_of(host)) == word
    for expect in hostcheck.STATES:
        told = Told()
        answer = quickly(lambda expect=expect, told=told: connect(host, expect, told))
        assert answer == {
            "connected": False,
            "state": word,
            "path": hostconfig.shown_path(place),
        }
        assert (told.entered, told.actions) == (0, [])
        # "Disconnect" takes the app out of the vault where the owner was shown this state,
        # and leaves the file alone; for any other state it does nothing at all.
        told = Told()
        answer = quickly(lambda expect=expect, told=told: disconnect(host, expect, told))
        if expect == word:
            assert answer == {"file": word, "path": hostconfig.shown_path(place), "copy": None}
            assert told.actions == ["disconnect-kept"]
        else:
            assert answer["file"] == "changed"
            assert (told.entered, told.actions) == (0, [])
    assert tree(home) == before, "something of the home changed"


def test_the_seven_states_are_the_ones_that_change_a_file(home: Path) -> None:
    """The counterpart: each state a write goes through for, with what it does to the file."""
    hosts = [JSON_HOST, TOML_HOST] if HAS_TOML else [JSON_HOST]
    for host in hosts:
        place = folder_of(host)
        empty = "{}" if host == JSON_HOST else "# nothing yet\n"
        for state, content in (
            ("no_file", None),
            ("empty", ""),
            ("empty", empty),
            ("other", other(host)),
            ("differs", elsewhere(host)),
        ):
            for item in place.parent.iterdir():
                item.unlink()
            if content is not None:
                put(place, content)
            assert state_of(host) == state
            told = Told()
            answer = connect(host, state, told)
            assert (answer["connected"], answer["state"]) == (True, "present"), (host, state)
            assert answer["replaced"] is (state == "differs")
            assert told.actions == ["connect"]
            assert state_of(host) == "present"
            assert no_temporary_file(place.parent)
            # With the entry in place there is nothing to write: the file is not touched.
            before = tree(home)
            told = Told()
            assert connect(host, "present", told) == {
                "connected": True,
                "state": "present",
                "path": hostconfig.shown_path(place),
                "copy": None,
                "replaced": False,
            }
            assert told.actions == ["connect-kept"]
            assert tree(home) == before
            # And "Disconnect" takes it out again.
            aged(place)
            told = Told()
            assert disconnect(host, "present", told)["file"] == "removed"
            assert told.actions == ["disconnect"]
            assert state_of(host) in ("other", "empty")


@needs_toml
def test_an_entry_under_another_name_the_vault_answers_is_left(home: Path) -> None:
    place = folder_of(TOML_HOST)
    made_for_codex = with_ours(TOML_HOST, client="codex")
    put(place, made_for_codex)
    seen = looked(TOML_HOST)
    assert (seen["state"], seen["other_name"]) == ("present_as", "codex")
    before = tree(home)
    # The vault has an app `codex`: the file already connects the program under that name.
    told = Told()
    assert connect(TOML_HOST, "present_as", told, registered=("claude-desktop", "codex")) == {
        "connected": False,
        "state": "present_as",
        "path": "~/.codex/config.toml",
        "other_name": "codex",
    }
    assert (told.entered, told.actions) == (0, [])
    assert tree(home) == before
    # "Disconnect" leaves such an entry too: it is not this app's.
    told = Told()
    assert disconnect(TOML_HOST, "present_as", told)["file"] == "differs"
    assert told.actions == ["disconnect-kept"]
    assert tree(home) == before
    # Without such an app, "Connect" points the entry at this name.
    told = Told()
    answer = connect(TOML_HOST, "present_as", told, registered=("claude-desktop",))
    assert (answer["connected"], answer["state"], answer["replaced"]) == (True, "present", True)
    assert answer["copy"] == "~/.codex/config.toml" + hostwrite.LAST_COPY
    assert place.read_text(encoding="utf-8") == with_ours(TOML_HOST)
    assert Path(str(place) + hostwrite.LAST_COPY).read_text(encoding="utf-8") == made_for_codex
    assert told.actions == ["connect"]


@pytest.mark.parametrize("host", HOSTS)
def test_a_missing_folder_stays_missing(host: str, home: Path) -> None:
    place = place_of(host)
    shown = hostconfig.shown_path(place)

    def nothing_is_made() -> None:
        before = tree(home)
        assert state_of(host) == "no_folder"
        for expect in ("no_folder", "no_file", "empty", "other", "present"):
            told = Told()
            assert connect(host, expect, told) == {
                "connected": False,
                "state": "no_folder",
                "path": shown,
            }
            assert (told.entered, told.actions) == (0, [])
        told = Told()
        assert disconnect(host, "present", told)["file"] == "changed"
        assert (told.entered, told.actions) == (0, [])
        # No folder means no entry: the app is taken out of the vault, and nothing is made.
        told = Told()
        assert disconnect(host, "no_folder", told) == {
            "file": "no_entry",
            "path": shown,
            "copy": None,
        }
        assert told.actions == ["disconnect-kept"]
        assert tree(home) == before
        assert not place.parent.is_dir()

    nothing_is_made()
    # The folder above it is there, and the application's own is not.
    if place.parent.parent != home:
        place.parent.parent.mkdir(parents=True)
        nothing_is_made()
        place.parent.parent.rmdir()
        # A file where the folder above should be.
        put(place.parent.parent, "not a folder")
        nothing_is_made()
        place.parent.parent.unlink()
        place.parent.parent.mkdir()
    # A file where the application's folder should be.
    put(place.parent, "not a folder")
    nothing_is_made()


def test_content_the_reader_accepts_is_written_or_left_and_never_raises(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = folder_of(JSON_HOST)
    first = Path(str(place) + hostwrite.FIRST_COPY)

    def written(old: str) -> str:
        for item in place.parent.iterdir():
            item.unlink()
        put(place, old)
        state = state_of(JSON_HOST)
        assert state in ("other", "empty"), old[:60]
        answer = connect(JSON_HOST, state)
        assert (answer["connected"], answer["state"]) == (True, "present"), old[:60]
        new = place.read_bytes().decode("utf-8")
        cut_out(old, new)  # every byte outside the piece is the old byte
        assert first.read_bytes() == old.encode("utf-8")
        return new

    # A writer that wrote the parsed file out again would respell or lose each of these.
    assert '"n": 1e400,' in written('{\n  "n": 1e400,\n  "m": -1E-400\n}\n')
    assert "1234567890123456789012345678901234.5" in written(
        '{"n": 1234567890123456789012345678901234.5, "i": 1234567890123456789012345678901234}'
    )
    assert '"\\ud83d"' in written('{"half": "\\ud83d", "pair": "\\ud83d\\ude00", "z": "\\u007a"}')
    assert "\r\n" in written('{\r\n  "theme": "dark"\r\n}\r\n')
    assert written('{"a":1.0,"b":1,"c":1e0,"d":[1.50,-0.0]}').startswith(
        '{"a":1.0,"b":1,"c":1e0,"d":[1.50,-0.0],'
    )
    # Deep nesting: 200 levels are written. 900 are written where this Python reads them at
    # that depth, and left where it does not; nothing is raised either way.
    written('{"deep": ' + "[" * 200 + "]" * 200 + "}\n")
    deep = (
        '{"deep": '
        + "[" * 900
        + "]" * 900
        + ', "also": '
        + '{"k": ' * 300
        + "1"
        + "}" * 300
        + "}\n"
    )
    for item in place.parent.iterdir():
        item.unlink()
    put(place, deep)
    state = state_of(JSON_HOST)
    assert state in ("other", "unreadable")
    answer = connect(JSON_HOST, state)
    if answer["connected"]:
        cut_out(deep, place.read_text(encoding="utf-8"))
    else:
        assert answer["state"] in ("by_hand", "unreadable")
        assert place.read_text(encoding="utf-8") == deep
        assert names(place.parent) == [place.name]
    # A new text that would be larger than the largest file that is read: left.
    for item in place.parent.iterdir():
        item.unlink()
    large = '{"padding": "' + "x" * (hostwrite.MAX_BYTES - 40) + '"}'
    assert len(large) <= hostwrite.MAX_BYTES
    put(place, large)
    before = tree(home)
    told = Told()
    assert connect(JSON_HOST, "other", told)["state"] == "by_hand"
    assert (told.entered, tree(home)) == (0, before)
    # An entry of another kind is not turned into a command.
    put(place, json.dumps({"mcpServers": {SLUG: {"url": "https://example.invalid/mcp"}}}))
    before = tree(home)
    assert state_of(JSON_HOST) == "differs"
    told = Told()
    assert connect(JSON_HOST, "differs", told) == {
        "connected": False,
        "state": "by_hand",
        "path": hostconfig.shown_path(place),
    }
    assert (told.entered, tree(home)) == (0, before)
    # An error while the new text is made: left, and nothing is raised.
    put(place, OTHER_JSON)
    before = tree(home)
    for error in (MemoryError(), RecursionError("too deep"), ValueError("no"), UnicodeError("no")):

        def failing(text: str, command: Any = None, error: BaseException = error) -> str:
            raise error

        with monkeypatch.context() as patch:
            patch.setattr(hostwrite, "json_with_entry", failing)
            told = Told()
            assert connect(JSON_HOST, "other", told)["state"] == "by_hand"
        assert (told.entered, tree(home)) == (0, before)


@pytest.mark.parametrize("host", HOSTS)
def test_an_error_while_an_entry_is_taken_out_leaves_the_file(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = put(folder_of(host), with_ours(host))
    before = tree(home)
    name = "json_without_entry" if host == JSON_HOST else "toml_without_entry"
    for error in (MemoryError(), RecursionError("too deep"), hostwrite.ByHand()):

        def failing(text: str, error: BaseException = error) -> str:
            raise error

        monkeypatch.setattr(hostwrite, name, failing)
        told = Told()
        assert disconnect(host, "present", told) == {
            "file": "by_hand",
            "path": hostconfig.shown_path(place),
            "copy": None,
        }
        assert told.actions == ["disconnect-kept"]
        assert tree(home) == before


@pytest.mark.parametrize("host", HOSTS)
def test_a_file_that_changed_is_not_written(host: str, home: Path) -> None:
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    changed = {"connected": False, "state": "changed", "path": shown}
    old, new = other(host), other(host, " and changed")

    # The state is not the one the owner was shown.
    put(place, old)
    before = tree(home)
    for expect in hostcheck.STATES:
        if expect == "other":
            continue
        told = Told()
        assert connect(host, expect, told) == changed, expect
        assert (told.entered, told.actions) == (0, [])
    assert tree(home) == before

    other_same_size = old.replace("tool", "loot")
    assert (len(other_same_size), other_same_size != old) == (len(old), True)

    def between(when: str, change: Callable[[], None], after: bytes | None) -> None:
        """Change the file at one moment of a connect, and see that it is left as changed."""
        for item in place.parent.iterdir():
            item.unlink()
        put(place, old)
        told = Told()
        if when == "before the first comparison":
            told.on_enter = change
        else:
            told.on["connect"] = change
        assert connect(host, "other", told) == changed, when
        expected = [] if when == "before the first comparison" else ["connect", "connect-failed"]
        assert told.actions == expected, when
        assert told.entered == 1
        if after is None:
            assert names(place.parent) == []
        else:
            # The file is what the other program made of it; no copy and no temporary file
            # was left, and a first copy this call made is gone again.
            assert names(place.parent) == [place.name], when
            assert place.read_bytes() == after

    for when in ("before the first comparison", "after the record"):
        # Another file took its place: a program saved it by replacing.
        between(when, lambda: exchange(place, new), new.encode())
        # It was written in place: longer, shorter, or the same bytes once more.
        between(when, lambda: place.write_text(old + "\n", encoding="utf-8"), (old + "\n").encode())
        between(when, lambda: place.write_text(old[:-1], encoding="utf-8"), old[:-1].encode())
        between(when, lambda: place.write_text(old, encoding="utf-8"), old.encode())
        # Its mode was changed.
        between(when, lambda: place.chmod(0o640), old.encode())
        # It was written in place with as many bytes as before, and its time was set back to
        # what it was, as a tool that restores or synchronises files does: by its device,
        # inode, size and time of change it is the file that was read. It is not.
        between(when, lambda: restored(place, other_same_size), other_same_size.encode())
        # It is gone.
        between(when, place.unlink, None)

    # A file that was not there, and is there by the time it would be put there.
    for when in ("before the first comparison", "after the record"):
        for item in place.parent.iterdir():
            item.unlink()
        told = Told()
        if when == "before the first comparison":
            told.on_enter = lambda: put(place, new) and None
        else:
            told.on["connect"] = lambda: put(place, new) and None
        assert connect(host, "no_file", told) == changed
        assert names(place.parent) == [place.name]
        assert place.read_text(encoding="utf-8") == new

    # "Disconnect": before the record nothing happened at all; after it the change failed.
    for item in place.parent.iterdir():
        item.unlink()
    put(place, with_ours(host))
    told = Told()
    told.on_enter = lambda: exchange(place, new)
    assert disconnect(host, "present", told) == {"file": "changed", "path": shown, "copy": None}
    assert (told.entered, told.actions) == (1, [])
    assert names(place.parent) == [place.name]
    put(place, with_ours(host))
    told = Told()
    told.on["disconnect"] = lambda: exchange(place, new)
    assert disconnect(host, "present", told) == {"file": "failed", "path": shown, "copy": None}
    assert told.actions == ["disconnect", "disconnect-failed"]
    assert place.read_text(encoding="utf-8") == new
    assert no_temporary_file(place.parent)


@pytest.mark.parametrize("host", HOSTS)
def test_a_file_that_appears_at_the_last_moment_is_not_written_over(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = folder_of(host)
    appeared = other(host, " of a program that just started")
    real_link = os.link

    def late(src: Any, dst: Any, **kwargs: Any) -> None:
        # After the second comparison, right before the new file is linked into its place.
        if os.fspath(dst) == place.name and not place.exists():
            put(place, appeared)
        real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", late)
    told = Told()
    assert connect(host, "no_file", told) == {
        "connected": False,
        "state": "failed",
        "path": hostconfig.shown_path(place),
    }
    assert told.actions == ["connect", "connect-failed"]
    assert names(place.parent) == [place.name]
    assert place.read_text(encoding="utf-8") == appeared


@pytest.mark.parametrize("host", HOSTS)
def test_a_file_changed_a_moment_ago_is_not_written(host: str, home: Path) -> None:
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    recent = {"connected": False, "state": "recent", "path": shown}
    for content, state in ((other(host), "other"), ("", "empty")):
        # A file whose time of change is now. One of zero bytes is not taken for a fresh
        # file: a program that saves in place has just emptied it.
        place.write_text(content, encoding="utf-8")
        before = tree(home)
        told = Told()
        assert connect(host, state, told) == recent
        assert (told.entered, told.actions) == (0, [])
        assert tree(home) == before
        aged(place, 3)
        assert connect(host, state, told) == recent
        # The same file with a time ten seconds old is written.
        aged(place, 10)
        told = Told()
        assert connect(host, state, told)["connected"] is True
        assert told.actions == ["connect"]
        for item in place.parent.iterdir():
            item.unlink()
    # An entry that is ours is not taken out of a file that was changed a moment ago, and the
    # app leaves the vault all the same.
    place.write_text(with_ours(host), encoding="utf-8")
    before = tree(home)
    told = Told()
    assert disconnect(host, "present", told) == {"file": "recent", "path": shown, "copy": None}
    assert told.actions == ["disconnect-kept"]
    assert tree(home) == before
    # With the entry in place a connect writes nothing, so the time does not matter.
    told = Told()
    assert connect(host, "present", told)["connected"] is True
    assert (told.actions, tree(home)) == (["connect-kept"], before)
    # A time that lies ahead is not "a moment ago".
    put(place, other(host))
    ahead = time.time() + 3600
    os.utime(place, (ahead, ahead))
    assert connect(host, "other")["connected"] is True


@pytest.mark.parametrize("host", HOSTS)
def test_a_file_written_while_it_is_read_is_not_written(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another program writes into the file, in place and with as many bytes as it had,
    while the writer reads it. The reader sees that (the system's own time of the last
    change is another after the read than before), and nothing is written: neither a mix of
    two versions, nor the old bytes over what the other program wrote."""
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    old = other(host)
    new = old.replace("tool", "loot")
    assert (len(new), new != old) == (len(old), True)
    real_read = os.read

    def press(
        moment: str, before: str, after: str, time_back: bool, call: Callable[[Told], Any]
    ) -> tuple[Any, Told]:
        """A press during whose first read of the file another program writes into it."""
        for item in place.parent.iterdir():
            item.unlink()
        put(place, before)
        reads: list[int] = []
        wrote: list[str] = []

        def write() -> None:
            wrote.append(moment)
            if time_back:  # as a tool that restores or synchronises a file does
                restored(place, after)
            else:
                place.write_text(after, encoding="utf-8")

        def reading(descriptor: int, count: int) -> bytes:
            reads.append(count)
            if moment == "before the second read" and len(reads) == 2:
                write()
            data = real_read(descriptor, count)
            if moment == "after the last read" and not data and not wrote:
                write()
            return data

        told = Told()
        with monkeypatch.context() as patch:
            patch.setattr(os, "read", reading)
            answer = call(told)
        assert wrote == [moment]
        # The file is what the other program made of it, and nothing lies beside it.
        assert names(place.parent) == [place.name]
        assert place.read_text(encoding="utf-8") == after
        return answer, told

    if host == JSON_HOST:
        pad = json.dumps("x" * (hostcheck._READ_AT_ONCE + 5000))
        big_old = OTHER_JSON.replace('"dark"', pad)
    else:
        big_old = OTHER_TOML + "# " + "x" * (hostcheck._READ_AT_ONCE + 5000) + "\n"
    big_new = big_old.replace("tool", "loot")
    assert len(big_old.encode()) > hostcheck._READ_AT_ONCE
    for time_back in (False, True):
        for moment, before, after in (
            ("after the last read", old, new),
            ("before the second read", big_old, big_new),
        ):
            answer, told = press(
                moment, before, after, time_back, lambda told: connect(host, "other", told)
            )
            assert answer == {"connected": False, "state": "busy", "path": shown}, (
                moment,
                time_back,
            )
            assert (told.entered, told.actions) == (0, [])
            # The next press reads the file as it is now; with its time set back it is written.
            if time_back:
                answer = connect(host, "other")
                assert (answer["connected"], answer["state"]) == (True, "present")
                make = hostwrite.json_with_entry if host == JSON_HOST else hostwrite.toml_with_entry
                assert place.read_text(encoding="utf-8") == make(after, command_of(host))
                copy = Path(str(place) + hostwrite.FIRST_COPY)
                assert copy.read_text(encoding="utf-8") == after
            else:
                assert connect(host, "other")["state"] == "recent"
    # "Disconnect": the file is not in the state the owner was shown, and nothing is recorded.
    ours_old = with_ours(host)
    ours_new = ours_old.replace("tool", "loot")
    answer, told = press(
        "after the last read",
        ours_old,
        ours_new,
        True,
        lambda told: disconnect(host, "present", told),
    )
    assert answer == {"file": "changed", "path": shown, "copy": None}
    assert (told.entered, told.actions) == (0, [])
    # A look at that moment says the same word, and changes nothing.
    answer, _ = press("after the last read", old, new, True, lambda told: looked(host))
    assert answer["state"] == "busy"
    assert state_of(host) == "other"


def test_a_linked_folder_is_not_written_through(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = JSON_HOST
    place = place_of(host)
    folder, above = place.parent, place.parent.parent
    assert above != home
    outside = tmp_path / "a repository"
    (outside / folder.name).mkdir(parents=True)
    kept = put(outside / folder.name / place.name, OTHER_JSON)
    opened: list[str] = []
    places: dict[int, str] = {}
    real_open = os.open

    def noting(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        through = kwargs.get("dir_fd")
        full = os.fspath(path) if through is None else os.path.join(places[through], path)
        opened.append(full)
        descriptor = real_open(path, flags, *args, **kwargs)
        places[descriptor] = full
        return descriptor

    monkeypatch.setattr(os, "open", noting)

    def not_through(real: Path) -> None:
        before = tree(tmp_path)
        opened.clear()
        seen = looked(host)
        assert (seen["state"], seen["real_folder"]) == ("linked", str(real))
        assert seen["copies"] == []
        for expect in ("linked", "other", "no_file", "present"):
            told = Told()
            assert connect(host, expect, told) == {
                "connected": False,
                "state": "linked",
                "path": hostconfig.shown_path(place),
                "real_folder": str(real),
            }
            assert (told.entered, told.actions) == (0, [])
        told = Told()
        assert disconnect(host, "present", told)["file"] == "changed"
        assert told.entered == 0
        told = Told()
        assert disconnect(host, "linked", told)["file"] == "linked"
        assert told.actions == ["disconnect-kept"]
        # Nothing behind the link was opened, read, written or made.
        assert tree(tmp_path) == before
        assert opened
        for path in opened:
            assert not path.startswith(str(outside)), path
            assert path in [str(home), *(str(part) for part in above.parents), str(above)], path

    # The folder of the file is a link to a folder outside the home.
    above.mkdir(parents=True)
    folder.symlink_to(outside / folder.name)
    not_through(outside / folder.name)
    # A link one level higher.
    folder.unlink()
    above.rmdir()
    above.symlink_to(outside)
    not_through(outside / folder.name)
    above.unlink()

    # The folder is exchanged for a link while a write runs: the write lands in the folder
    # that was opened, and what the link leads to is untouched.
    for when in ("before the first comparison", "after the record"):
        folder.mkdir(parents=True)
        put(place, OTHER_JSON)
        away = above / f"moved away, {when}"
        target = tree(outside)

        def swap(away: Path = away) -> None:
            folder.rename(away)
            folder.symlink_to(outside / folder.name)

        told = Told()
        if when == "before the first comparison":
            told.on_enter = swap
        else:
            told.on["connect"] = swap
        answer = connect(host, "other", told)
        assert (answer["connected"], answer["state"]) == (True, "present")
        assert told.actions == ["connect"]
        assert tree(outside) == target, "the write went through the link"
        assert kept.read_text(encoding="utf-8") == OTHER_JSON
        assert names(away) == [place.name, place.name + hostwrite.FIRST_COPY]
        assert strict((away / place.name).read_text(encoding="utf-8"))["mcpServers"][SLUG] == ours(
            host
        )
        folder.unlink()


def test_a_linked_folder_that_is_exchanged_at_that_moment_is_a_word_and_no_error(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder on the way was found to be a link, and is no link any more by the time the
    look asks where it leads: the system refuses to read it. The look says ``linked`` all
    the same, and has no place to name."""
    host = JSON_HOST
    place = place_of(host)
    shown = hostconfig.shown_path(place)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    place.parent.parent.mkdir(parents=True)
    real_readlink, real_realpath = os.readlink, os.path.realpath
    exchanged: list[int] = []

    def exchanging(path: Any, *args: Any, **kwargs: Any) -> str:
        if not exchanged and os.fspath(path) == str(place.parent):
            exchanged.append(1)
            os.unlink(path)  # another program: the link is gone,
            os.mkdir(path)  # and a folder is there now
        answer: str = real_readlink(path, *args, **kwargs)
        return answer

    def refusing(path: Any, **kwargs: Any) -> str:
        if os.fspath(path) == str(place.parent):
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        return real_realpath(path, **kwargs)

    # For real: the link is exchanged for a folder while the look reads it.
    place.parent.symlink_to(outside)
    with monkeypatch.context() as patch:
        patch.setattr(os, "readlink", exchanging)
        seen = looked(host)
    assert exchanged == [1]
    assert seen["state"] == "linked"
    # (Python up to 3.12 raises there; a later one answers the path itself.)
    assert seen["real_folder"] in (None, hostconfig.shown_path(place.parent))
    place.parent.rmdir()
    # Whatever the Python: where the system refuses, the answers have no place to name.
    place.parent.symlink_to(outside)
    before = tree(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(os.path, "realpath", refusing)
        seen = looked(host)
        assert (seen["state"], seen["real_folder"], seen["problem"]) == ("linked", None, None)
        told = Told()
        assert connect(host, "linked", told) == {
            "connected": False,
            "state": "linked",
            "path": shown,
            "real_folder": None,
        }
        assert told.entered == 0
        assert disconnect(host, "linked", told)["file"] == "linked"
    assert tree(tmp_path) == before
    assert looked(host)["real_folder"] == str(outside)


@pytest.mark.parametrize("host", HOSTS)
def test_every_change_keeps_a_copy(host: str, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    first = Path(str(place) + hostwrite.FIRST_COPY)
    last = Path(str(place) + hostwrite.LAST_COPY)

    def clear() -> None:
        for item in place.parent.iterdir():
            if item.is_dir() and not item.is_symlink():
                item.rmdir()
            else:
                item.unlink()

    # A first connect, on a file without the entry: the first copy holds the old bytes, with
    # mode 0600 whatever the mode of the file, and the answer names it.
    put(place, other(host), mode=0o644)
    answer = connect(host, "other")
    assert (answer["connected"], answer["copy"]) == (True, shown + hostwrite.FIRST_COPY)
    assert first.read_text(encoding="utf-8") == other(host)
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert first.stat().st_nlink == 1
    assert names(place.parent) == [place.name, first.name]
    kept_first = (first.read_bytes(), first.stat().st_mtime_ns, first.stat().st_ino)
    # "Disconnect": the latest copy holds the file as it was with the entry.
    connected = place.read_bytes()
    aged(place)
    answer = disconnect(host, "present")
    assert (answer["file"], answer["copy"]) == ("removed", shown + hostwrite.LAST_COPY)
    assert last.read_bytes() == connected
    assert stat.S_IMODE(last.stat().st_mode) == 0o600
    assert names(place.parent) == [place.name, first.name, last.name]
    # The file changes, and is connected again: the first copy is still the first one, byte
    # for byte, and the latest copy holds the file from before this connect.
    put(place, other(host, ", changed since"), mode=0o644)
    answer = connect(host, "other")
    assert (answer["connected"], answer["copy"]) == (True, shown + hostwrite.LAST_COPY)
    assert (first.read_bytes(), first.stat().st_mtime_ns, first.stat().st_ino) == kept_first
    assert last.read_text(encoding="utf-8") == other(host, ", changed since")
    assert stat.S_IMODE(last.stat().st_mode) == 0o600
    assert names(place.parent) == [place.name, first.name, last.name]
    assert [copy["path"] for copy in looked(host)["copies"]] == [
        shown + hostwrite.FIRST_COPY,
        shown + hostwrite.LAST_COPY,
    ]

    # A file that already holds an entry of that name: no first copy is made of it.
    clear()
    put(place, elsewhere(host))
    answer = connect(host, "differs")
    assert (answer["connected"], answer["copy"]) == (True, shown + hostwrite.LAST_COPY)
    assert last.read_text(encoding="utf-8") == elsewhere(host)
    assert names(place.parent) == [place.name, last.name]

    # A file somebody put at the first copy's place is untouched and unread; this call's
    # copy is the latest copy.
    clear()
    put(place, other(host))
    put(first, "somebody else's file", mode=0o644)
    theirs = tree(home)[str(first.relative_to(home))]
    answer = connect(host, "other")
    assert (answer["connected"], answer["copy"]) == (True, shown + hostwrite.LAST_COPY)
    assert tree(home)[str(first.relative_to(home))] == theirs
    assert last.read_text(encoding="utf-8") == other(host)

    # A file that this module made has no copy "from before": the file was not there. After
    # a disconnect and a second connect there is still none, only the latest one.
    clear()
    answer = connect(host, "no_file")
    assert (answer["connected"], answer["copy"]) == (True, None)
    assert names(place.parent) == [place.name]
    assert stat.S_IMODE(place.stat().st_mode) == 0o600
    aged(place)
    assert disconnect(host, "present")["copy"] == shown + hostwrite.LAST_COPY
    aged(place)
    emptied = place.read_bytes()
    assert state_of(host) == "empty"
    answer = connect(host, "empty")
    assert (answer["connected"], answer["copy"]) == (True, shown + hostwrite.LAST_COPY)
    assert last.read_bytes() == emptied
    assert names(place.parent) == [place.name, last.name]

    # Something at the latest copy's place that is no copy of ours: nothing is changed.
    target = home / "the target of a link"
    for what in ("a link", "a link to nothing", "a folder", "a file with two names", "another's"):
        clear()
        put(place, elsewhere(host))
        put(target, "not to be replaced")
        if what == "a link":
            last.symlink_to(target)
        elif what == "a link to nothing":
            last.symlink_to(home / "nothing there")
        elif what == "a folder":
            last.mkdir()
        elif what == "a file with two names":
            put(last, "an older copy")
            os.link(last, home / "its second name")
        else:
            put(last, "an older copy")
        before = tree(home)
        told = Told()
        with monkeypatch.context() as patch:
            if what == "another's":
                patch.setattr(os, "lstat", lstat_as_of_another(last.name))
            assert connect(host, "differs", told) == {
                "connected": False,
                "state": "no_copy",
                "path": shown,
            }, what
        assert (told.entered, told.actions) == (0, [])
        assert tree(home) == before, what
        (home / "its second name").unlink(missing_ok=True)
    # A file appears at the first copy's place between the look and the moment the copy is
    # put there: it is not replaced, and this call's copy is the latest copy.
    clear()
    put(place, other(host))
    told = Told()
    told.on["connect"] = lambda: put(first, "put there in the meantime") and None
    answer = connect(host, "other", told)
    assert (answer["connected"], answer["copy"]) == (True, shown + hostwrite.LAST_COPY)
    assert first.read_text(encoding="utf-8") == "put there in the meantime"
    assert last.read_text(encoding="utf-8") == other(host)
    # ... unless the latest copy's place holds something that is no copy, by then.
    clear()
    put(place, other(host))
    put(target, "not to be replaced")

    def both_taken() -> None:
        put(first, "put there in the meantime")
        last.symlink_to(target)

    told = Told()
    told.on["connect"] = both_taken
    assert connect(host, "other", told)["state"] == "no_copy"
    assert told.actions == ["connect", "connect-failed"]
    assert place.read_text(encoding="utf-8") == other(host)
    assert (last.is_symlink(), target.read_text(encoding="utf-8")) == (True, "not to be replaced")
    assert first.read_text(encoding="utf-8") == "put there in the meantime"
    assert no_temporary_file(place.parent)
    # The same where the file holds no entry and there is no first copy: the latest copy's
    # place shows that the file was changed before, so this call's copy would go there.
    clear()
    put(place, other(host))
    last.symlink_to(target)
    before = tree(home)
    assert connect(host, "other")["state"] == "no_copy"
    assert tree(home) == before


# -- what a refusal and a failure leave behind ------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_no_refusal_leaves_a_file_behind(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = put(folder_of(host), other(host))
    shown = hostconfig.shown_path(place)
    before_names = names(place.parent)
    real_open, real_write = os.open, os.write

    def refused(state: str, told: Told, *, recorded: list[str] | None = None) -> None:
        before = tree(home)
        assert connect(host, "other", told) == {"connected": False, "state": state, "path": shown}
        assert told.actions == (recorded or [])
        assert names(place.parent) == before_names
        assert tree(home) == before

    # The file is another one at the first comparison. (It changes the file's time, so the
    # names are compared here and the bytes below.)
    told = Told()
    told.on_enter = lambda: place.write_text(other(host), encoding="utf-8")
    assert connect(host, "other", told)["state"] == "changed"
    assert (told.actions, names(place.parent)) == ([], before_names)
    aged(place)
    # The section cannot be entered: owner mode ran out, the vault locked, its settings
    # changed, or the record cannot be written. The error is the caller's own.
    for error in (
        Refused(403, "reauth_required"),
        Refused(409, "locked"),
        Refused(409, "changed"),
        Refused(503, "unavailable"),
    ):
        told = Told()
        told.fail_enter = error
        before = tree(home)
        with pytest.raises(Refused) as raised:
            connect(host, "other", told)
        assert raised.value is error
        assert (told.entered, told.actions) == (1, [])
        assert tree(home) == before
    # A temporary file cannot be made: for the new content, then for the copy.
    for which, state in ((1, "failed"), (2, "no_copy")):
        made: list[str] = []

        def no_room(
            path: Any,
            flags: int,
            *args: Any,
            which: int = which,
            made: list[str] = made,
            **kwargs: Any,
        ) -> int:
            if flags & os.O_CREAT:
                made.append(os.fspath(path))
                if len(made) == which:
                    raise OSError(28, "No space left on device")
            return real_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", no_room)
            refused(state, Told())
        assert len(made) == which
        for name in made:
            assert re.fullmatch(rf"\.{re.escape(place.name)}\.[0-9a-f]{{24}}\.tmp", name)
    # It can be made and not written: nothing of it stays.
    with monkeypatch.context() as patch:

        def full(descriptor: int, data: Any) -> int:
            real_write(descriptor, bytes(data)[:5])
            raise OSError(28, "No space left on device")

        patch.setattr(os, "write", full)
        refused("failed", Told())
    # The text cannot be made by the rules; the file was changed a moment ago.
    with monkeypatch.context() as patch:

        def by_hand(text: str, command: Any) -> str:
            raise hostwrite.ByHand

        patch.setattr(hostwrite, "json_with_entry", by_hand)
        patch.setattr(hostwrite, "toml_with_entry", by_hand)
        refused("by_hand", Told())
    place.write_text(other(host), encoding="utf-8")
    refused("recent", Told())
    aged(place)

    # After the record: the file cannot be replaced. The first copy this call made is gone
    # again, and so is every temporary file.
    real_replace = os.replace

    def stuck(src: Any, dst: Any, **kwargs: Any) -> None:
        if os.fspath(dst) == place.name:
            raise OSError(5, "Input/output error")
        real_replace(src, dst, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", stuck)
        refused("failed", Told(), recorded=["connect", "connect-failed"])


@pytest.mark.parametrize("host", HOSTS)
def test_the_record_comes_first(host: str, home: Path) -> None:
    place = folder_of(host)
    for content, state in ((None, "no_file"), (other(host), "other"), (elsewhere(host), "differs")):
        for item in place.parent.iterdir():
            item.unlink()
        if content is not None:
            put(place, content)
        before = tree(home)
        told = Told()
        error = Refused(503, "unavailable")
        told.fail["connect"] = error
        with pytest.raises(Refused) as raised:
            connect(host, state, told)
        assert raised.value is error
        # The record was not written, and nothing of another program was changed: the file
        # is untouched, there is no copy, and no temporary file is left.
        assert (told.entered, told.actions) == (1, [])
        assert tree(home) == before
    # The same for an entry that is to be taken out, and for one that is there already.
    put(place, with_ours(host))
    before = tree(home)
    for call, action in ((disconnect, "disconnect"), (connect, "connect-kept")):
        told = Told()
        told.fail[action] = Refused(409, "locked")
        with pytest.raises(Refused):
            call(host, "present", told)
        assert told.actions == []
        assert tree(home) == before
    # And for a file that is left alone: the app could not be taken out of the vault.
    put(place, elsewhere(host))
    before = tree(home)
    told = Told()
    told.fail["disconnect-kept"] = Refused(403, "reauth_required")
    with pytest.raises(Refused):
        disconnect(host, "differs", told)
    assert tree(home) == before


@pytest.mark.parametrize("host", HOSTS)
def test_a_failure_after_the_record_is_recorded(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    last = Path(str(place) + hostwrite.LAST_COPY)
    real_replace, real_link = os.replace, os.link

    def failing(name: str) -> Callable[..., None]:
        def replace(src: Any, dst: Any, **kwargs: Any) -> None:
            if os.fspath(dst) == name:
                raise OSError(5, "Input/output error")
            real_replace(src, dst, **kwargs)

        return replace

    # The file cannot be replaced.
    put(place, other(host))
    before = tree(home)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failing(place.name))
        told = Told()
        assert connect(host, "other", told) == {
            "connected": False,
            "state": "failed",
            "path": shown,
        }
    assert told.actions == ["connect", "connect-failed"]
    assert told.entered == 1  # both were said inside the one section
    assert tree(home) == before  # also: the first copy this call made is gone again
    # The same where the copy is the latest one: that copy stays, and holds the file as it is.
    put(place, elsewhere(host))
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failing(place.name))
        told = Told()
        assert connect(host, "differs", told)["state"] == "failed"
    assert told.actions == ["connect", "connect-failed"]
    assert place.read_text(encoding="utf-8") == elsewhere(host)
    assert last.read_text(encoding="utf-8") == elsewhere(host)
    assert names(place.parent) == [place.name, last.name]
    # The copy cannot be put in place: the file is not touched.
    last.unlink()
    before = tree(home)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failing(last.name))
        told = Told()
        assert connect(host, "differs", told)["state"] == "no_copy"
    assert told.actions == ["connect", "connect-failed"]
    assert tree(home) == before
    # A file that was not there cannot be put there.
    place.unlink()

    def no_link(src: Any, dst: Any, **kwargs: Any) -> None:
        if os.fspath(dst) == place.name:
            raise OSError(1, "Operation not permitted")  # a file system without hard links
        real_link(src, dst, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", no_link)
        told = Told()
        assert connect(host, "no_file", told)["state"] == "failed"
    assert told.actions == ["connect", "connect-failed"]
    assert names(place.parent) == []

    # A first copy that cannot be linked becomes the latest copy, and the write goes on.
    def no_links_at_all(src: Any, dst: Any, **kwargs: Any) -> None:
        raise OSError(1, "Operation not permitted")

    put(place, other(host))
    with monkeypatch.context() as patch:
        patch.setattr(os, "link", no_links_at_all)
        told = Told()
        answer = connect(host, "other", told)
    assert (answer["connected"], answer["copy"]) == (True, shown + hostwrite.LAST_COPY)
    assert last.read_text(encoding="utf-8") == other(host)
    assert names(place.parent) == [place.name, last.name]
    # "Disconnect": the same two entries, and the answer says that it failed.
    aged(place)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failing(place.name))
        told = Told()
        assert disconnect(host, "present", told) == {"file": "failed", "path": shown, "copy": None}
    assert told.actions == ["disconnect", "disconnect-failed"]
    assert state_of(host) == "present"
    assert no_temporary_file(place.parent)
    # If the failure cannot be recorded either, the error is the caller's; the temporary
    # files are gone, and so is a first copy this call made.
    for item in place.parent.iterdir():
        item.unlink()
    put(place, other(host))
    before = tree(home)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failing(place.name))
        told = Told()
        told.fail["connect-failed"] = Refused(503, "unavailable")
        with pytest.raises(Refused):
            connect(host, "other", told)
    assert told.actions == ["connect"]
    assert tree(home) == before


@pytest.mark.parametrize("host", HOSTS)
def test_leftovers_of_a_cut_call_are_removed(host: str, home: Path) -> None:
    place = put(folder_of(host), other(host))
    folder = place.parent

    def left(middle: str, seconds: float, name: str | None = None) -> Path:
        path = folder / f".{name or place.name}.{middle}.tmp"
        return aged(put(path, "left by a call that was cut"), seconds)

    old = left("a" * 24, 120)
    young = left("b" * 24, 10)
    others = [
        left("c" * 23, 120),  # one digit short
        left("d" * 25, 120),
        left("G" * 24, 120),  # not hex digits
        left("e" * 24, 120, name="another file.json"),
        aged(put(folder / f"{place.name}.{'f' * 24}.tmp", "no leading dot"), 120),
        aged(put(folder / f".{place.name}.{'1' * 24}.tmp.bak", "a longer name"), 120),
    ]
    a_folder = folder / f".{place.name}.{'2' * 24}.tmp"
    a_folder.mkdir()
    aged(a_folder, 120)
    a_link = folder / f".{place.name}.{'3' * 24}.tmp"
    a_link.symlink_to(young)
    aged(a_link, 120)
    # A look removes nothing.
    assert state_of(host) == "other"
    assert old.exists()
    answer = connect(host, "other")
    assert answer["connected"] is True
    # The one that is of this module's pattern, a file, and two minutes old is gone.
    assert not old.exists()
    assert young.exists()
    assert a_folder.is_dir()
    assert a_link.is_symlink()
    for path in others:
        assert path.exists(), path.name
    # Ten seconds later it is still there; a minute later the next write takes it.
    aged(place)
    assert disconnect(host, "present")["file"] == "removed"
    assert young.exists()
    aged(young, 61)
    aged(place)
    assert connect(host, state_of(host))["connected"] is True
    assert not young.exists()
    assert a_link.is_symlink()  # (it now leads nowhere, and is still not ours to remove)

    # A call that is refused because the file is not in the state the owner was shown has
    # removed nothing: the folder holds what it held.
    for item in list(folder.iterdir()):
        if item.is_dir() and not item.is_symlink():
            item.rmdir()
        else:
            item.unlink()
    put(place, other(host))
    old = left("a" * 24, 3600)
    before = tree(home)
    for expect in hostcheck.STATES:
        if expect != "other":
            assert connect(host, expect)["state"] == "changed", expect
            assert disconnect(host, expect)["file"] == "changed", expect
    # Nor has a "Disconnect" that leaves the file alone, or a look.
    told = Told()
    assert disconnect(host, "other", told)["file"] == "no_entry"
    assert told.actions == ["disconnect-kept"]
    assert state_of(host) == "other"
    assert tree(home) == before
    assert old.exists()


@pytest.mark.parametrize("host", HOSTS)
def test_a_new_file_that_kept_its_temporary_name_is_looked_at_and_cleared(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new settings file is linked into place, and the call is cut before it can remove
    the temporary name: the file has two names. It is still the file the product made, and
    the look says what it holds; the press that follows removes the second name."""
    place = folder_of(host)
    folder = place.parent
    shown = hostconfig.shown_path(place)

    def cut(content: str, seconds: float, middle: str = "9" * 24) -> Path:
        """The folder as such a call leaves it, ``seconds`` ago."""
        for item in list(folder.iterdir()):
            item.unlink()
        put(place, content)
        second = folder / f".{place.name}.{middle}.tmp"
        os.link(place, second)
        aged(place, seconds)
        assert (place.stat().st_nlink, second.stat().st_ino) == (2, place.stat().st_ino)
        return second

    # For real: the one call that removes the temporary name fails.
    real_unlink = os.unlink

    def no_unlink(path: Any, **kwargs: Any) -> None:
        if os.fspath(path).endswith(".tmp"):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_unlink(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", no_unlink)
        answer = connect(host, "no_file")
    assert (answer["connected"], answer["state"]) == (True, "present")
    assert place.stat().st_nlink == 2
    (second,) = [item for item in folder.iterdir() if item != place]
    assert hostwrite._leftover_pattern(place.name).fullmatch(second.name)
    written = place.read_text(encoding="utf-8")
    # The look says what the file holds, at once and an hour later, and removes nothing.
    before = tree(home)
    assert state_of(host) == "present"
    aged(place, 3600)
    before = tree(home)
    assert state_of(host) == "present"
    assert looked(host)["copies"] == []
    assert tree(home) == before
    # A press whose state is not the file's is refused, and leaves the name alone.
    assert connect(host, "not_read")["state"] == "changed"
    assert disconnect(host, "not_read")["file"] == "changed"
    assert connect(host, "other")["state"] == "changed"
    assert tree(home) == before
    # "Connect" in the state that was shown: nothing is to be written, the second name goes.
    told = Told()
    assert connect(host, "present", told) == {
        "connected": True,
        "state": "present",
        "path": shown,
        "copy": None,
        "replaced": False,
    }
    assert told.actions == ["connect-kept"]
    assert (names(folder), place.stat().st_nlink) == ([place.name], 1)
    assert place.read_text(encoding="utf-8") == written

    # "Disconnect" clears up in the same way, and then takes the entry out.
    second = cut(with_ours(host), 3600)
    told = Told()
    answer = disconnect(host, "present", told)
    assert (answer["file"], told.actions) == ("removed", ["disconnect"])
    assert names(folder) == [place.name, place.name + hostwrite.LAST_COPY]
    assert (place.stat().st_nlink, state_of(host)) == (1, "other")
    # A file that was edited in place since, and holds no entry or another one: written, once
    # the second name is gone.
    for content, state in ((other(host), "other"), (elsewhere(host), "differs")):
        second = cut(content, 3600)
        assert state_of(host) == state
        told = Told()
        answer = connect(host, state, told)
        assert (answer["connected"], answer["state"], told.actions) == (
            True,
            "present",
            ["connect"],
        )
        assert not second.exists()
        assert place.stat().st_nlink == 1
        assert no_temporary_file(folder)

    # While the leftover is young the call that made it may still be at work. The look says
    # what the file holds; no file is replaced while it has two names.
    second = cut(other(host), 10)
    before = tree(home)
    assert state_of(host) == "other"
    told = Told()
    assert connect(host, "other", told) == {"connected": False, "state": "recent", "path": shown}
    assert (told.entered, tree(home)) == (0, before)
    second = cut(with_ours(host), 10)
    before = tree(home)
    told = Told()
    assert disconnect(host, "present", told) == {"file": "recent", "path": shown, "copy": None}
    assert (told.actions, tree(home)) == (["disconnect-kept"], before)
    # (Where nothing is to be written, nothing waits.)
    told = Told()
    assert connect(host, "present", told)["connected"] is True
    assert (told.actions, tree(home)) == (["connect-kept"], before)
    # A minute later the same press goes through.
    aged(place, 61)
    assert disconnect(host, "present")["file"] == "removed"
    assert (second.exists(), place.stat().st_nlink) == (False, 1)

    # A name the system does not let go: the file keeps two names, and is not replaced.
    second = cut(other(host), 3600)
    before = tree(home)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", no_unlink)
        told = Told()
        assert connect(host, "other", told)["state"] == "recent"
    assert (told.entered, tree(home)) == (0, before)


@pytest.mark.parametrize("host", HOSTS)
def test_a_second_name_that_is_not_a_leftover_makes_a_file_not_read(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one second name that is let through is a temporary name of the writer, for this
    settings file, in the same folder, and the only other name. Every other file with two
    names is left as before: replacing it would leave the other name with the old content."""
    place = folder_of(host)
    folder = place.parent
    shown = hostconfig.shown_path(place)
    hexes = "9" * 24

    def never(what: str) -> None:
        aged(place, 3600)
        before = tree(home)
        assert state_of(host) == "not_read", what
        for expect in ("other", "present", "not_read"):
            told = Told()
            assert connect(host, expect, told) == {
                "connected": False,
                "state": "not_read",
                "path": shown,
            }, what
            assert told.entered == 0
        told = Told()
        assert disconnect(host, "other", told)["file"] == "changed", what
        assert told.entered == 0
        assert tree(home) == before, what
        for item in list(folder.iterdir()):
            item.unlink()

    # A second name in another folder.
    put(place, other(host))
    os.link(place, home / f".{place.name}.{hexes}.tmp")
    never("a name of the pattern, in another folder")
    (home / f".{place.name}.{hexes}.tmp").unlink()
    # A second name in the same folder that is not of the pattern.
    for name in (
        f"{place.name}.bak",
        f".{place.name}.{hexes}.tmp.bak",
        f".{place.name}.{'9' * 23}.tmp",
        f".{place.name}.{'G' * 24}.tmp",
        f"{place.name}.{hexes}.tmp",
        f".another file.json.{hexes}.tmp",
        place.name + hostwrite.FIRST_COPY,
    ):
        put(place, other(host))
        os.link(place, folder / name)
        never(name)
    # A second name elsewhere, and a file of the pattern in the folder that is another file.
    put(place, other(host))
    os.link(place, home / "a second name")
    aged(put(folder / f".{place.name}.{hexes}.tmp", other(host)), 30)
    never("another file under a name of the pattern")
    (home / "a second name").unlink()
    # ... or a link to the settings file under such a name.
    put(place, other(host))
    os.link(place, home / "a second name")
    (folder / f".{place.name}.{hexes}.tmp").symlink_to(place)
    never("a symbolic link under a name of the pattern")
    (home / "a second name").unlink()
    # Three names, two of them of the pattern.
    put(place, other(host))
    os.link(place, folder / f".{place.name}.{hexes}.tmp")
    os.link(place, folder / f".{place.name}.{'8' * 24}.tmp")
    never("three names")
    # A file of another account, with a second name of the pattern.
    put(place, other(host))
    os.link(place, folder / f".{place.name}.{hexes}.tmp")
    mine = os.getuid()
    with monkeypatch.context() as patch:
        patch.setattr(os, "getuid", lambda: mine + 1)
        never("a file of another account")
    # Where the names of the folder cannot be listed, nothing is known about the other name.
    put(place, other(host))
    os.link(place, folder / f".{place.name}.{hexes}.tmp")

    real_listdir = os.listdir

    def no_listing(path: Any) -> list[str]:
        if isinstance(path, int):  # the folder that is held open
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_listdir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "listdir", no_listing)
        never("a folder that cannot be listed")


# -- what can be said without the file --------------------------------------------------------


def test_grok_is_not_given_a_command_it_would_rewrite(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = "/srv/va${HOME}ult"
    # (Without a reader for TOML the file of the ChatGPT desktop app is not written at all;
    # that Grok Build is refused is found before any file is looked at.)
    hosts = [JSON_HOST, "grok-build", *([TOML_HOST] if HAS_TOML else [])]
    for host in hosts:
        put(folder_of(host), other(host))
    before = tree(home)
    opened: list[str] = []
    real_open = os.open

    def noting(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    for host in hosts:
        command = command_of(host, store)
        assert "${" in command[-1]
        if host != "grok-build":
            assert hostwrite.write_problem(host, command) is None
            answer = connect(host, "other", store=store)
            assert (answer["connected"], answer["state"]) == (True, "present")
            assert store in place_of(host).read_text(encoding="utf-8")
            continue
        assert hostwrite.write_problem(host, command) == "expands"
        assert looked(host, store=store)["problem"] == "expands"
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", noting)
            told = Told()
            assert connect(host, "other", told, store=store) == {
                "connected": False,
                "state": "expands",
                "path": "~/.grok/config.toml",
            }
        assert (told.entered, opened) == (0, [])  # no file and no folder was looked at
        assert tree(home)[".grok/config.toml"] == before[".grok/config.toml"]
        # The rule follows one sentence of xAI's page, about `${`. A dollar sign or a tilde
        # alone is not refused: whether they are replaced was not tried.
        for plain in ("/srv/va$HOME/ult", "/srv/~/vault", "/srv/va{HOME}ult", "/srv/va$ {x}"):
            assert hostwrite.write_problem(host, command_of(host, plain)) is None
        # It is looked for in every word of the command.
        assert hostwrite.write_problem(host, ["/opt/${X}/python", "-m", "wdva"]) == "expands"
        assert hostwrite.write_problem(host, ["/opt/python", "--client", "${X}"]) == "expands"


def test_the_file_of_chatgpt_is_not_written_while_codex_home_is_set(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if HAS_TOML:
        put(folder_of(TOML_HOST), OTHER_TOML)
    before = tree(home)
    opened: list[str] = []
    real_open = os.open

    def noting(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    command = command_of(TOML_HOST)
    for value in (str(home / "codex of mine"), "relative", ""):
        monkeypatch.setenv("CODEX_HOME", value)
        assert hostwrite.write_problem(TOML_HOST, command) == "moved"
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", noting)
            for expect in ("other", "no_file", "present"):
                told = Told()
                assert connect(TOML_HOST, expect, told) == {
                    "connected": False,
                    "state": "moved",
                    "path": "~/.codex/config.toml",
                }
                assert told.entered == 0
        assert opened == []  # no file looked at
        # The other two are not about that variable.
        assert hostwrite.write_problem(JSON_HOST, command_of(JSON_HOST)) is None
        assert hostwrite.write_problem("grok-build", command_of("grok-build")) is None
        if HAS_TOML:
            # The look still says what the one file holds that is known, and that a write
            # is refused.
            seen = looked(TOML_HOST)
            assert (seen["state"], seen["problem"]) == ("other", "moved")
    assert tree(home) == before
    monkeypatch.delenv("CODEX_HOME")
    assert hostwrite.write_problem(TOML_HOST, command) is None


ROOT = Path(__file__).resolve().parent.parent


def lies_well(path: Path) -> bool:
    """Whether a place passes rule R17, by this test's own reading of it."""
    temporary = {tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev/shm"}
    temporary |= {os.path.realpath(folder) for folder in temporary}
    text = str(path)
    if any(text == folder or text.startswith(folder.rstrip("/") + "/") for folder in temporary):
        return False
    for part in (path, *path.parents):
        status = part.stat()
        if status.st_uid not in (os.getuid(), 0) or status.st_mode & stat.S_IWOTH:
            return False
    return True


@pytest.fixture
def lasting() -> Iterator[Path]:
    """A folder that is not under the folder for temporary files: one under the checkout.

    pytest's own folders lie under the system's folder for temporary files on every
    machine, so a place the rule accepts has to be made elsewhere. It is removed afterwards.
    """
    base = ROOT / ".pytest_cache" / f"hostwrite-{secrets.token_hex(6)}"
    base.mkdir(parents=True)
    try:
        if not lies_well(base):
            pytest.skip(
                "this checkout lies under a folder for temporary files, or in a place that "
                "anybody may write or that belongs to another account: the rule refuses it, "
                "so what the rule lets through cannot be shown here"
            )
        yield base
    finally:
        for path in [base, *base.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        shutil.rmtree(base, ignore_errors=True)


def test_a_command_that_may_not_last_is_not_written(
    bare_home: Path, lasting: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = JSON_HOST
    place = put(folder_of(host), "{}")
    python = lasting / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)  # as in a virtual environment
    vault = lasting / "vault"
    vault.mkdir()
    for folder in (lasting, lasting / "venv", python.parent, vault):
        folder.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(python))

    def problem(interpreter: Path | str = python, store: Path | str = vault) -> str | None:
        with monkeypatch.context() as patch:
            patch.setattr(sys, "executable", str(interpreter))
            command = hostconfig.host_command(host, store)
        assert command[0] == str(interpreter)
        return hostwrite.write_problem(host, command)

    def write(store: Path | str = vault) -> dict[str, Any]:
        told = Told()
        answer = hostwrite.connect(host, client=host, store=store, expect="empty", section=told)
        if not answer["connected"]:
            assert (told.entered, place.read_text(encoding="utf-8")) == (0, "{}")
        return answer

    # A Python and a vault that lie well: written, with the real rule at work.
    assert problem() is None
    assert write()["connected"] is True
    assert strict(place.read_text(encoding="utf-8"))["mcpServers"][SLUG]["command"] == str(python)
    for item in place.parent.iterdir():
        item.unlink()
    put(place, "{}")
    # A part that a group may write is not refused.
    for folder in (lasting / "venv", python.parent, vault):
        folder.chmod(0o775)
    assert problem() is None
    assert write()["connected"] is True
    for item in place.parent.iterdir():
        item.unlink()
    put(place, "{}")

    # A part that everybody may write, with or without the bit that keeps others' files.
    for folder in (lasting, lasting / "venv", python.parent, vault):
        for mode in (0o777, 0o1777, 0o757):
            folder.chmod(mode)
            assert problem() == "unstable", (folder, oct(mode))
            folder.chmod(0o755)
        assert problem() is None
    vault.chmod(0o777)
    assert write() == {
        "connected": False,
        "state": "unstable",
        "path": hostconfig.shown_path(place),
    }
    assert names(place.parent) == [place.name]
    vault.chmod(0o755)
    # The Python itself, where it is a file and not a link.
    plain = python.with_name("python3")
    put(plain, "#!/bin/sh\n", mode=0o755)
    assert problem(plain) is None
    plain.chmod(0o757)
    assert problem(plain) == "unstable"
    plain.chmod(0o755)

    # A part that belongs to another account.
    if os.getuid() == 0:
        for folder in (lasting / "venv", vault):
            os.chown(folder, 4242, -1)
            assert problem() == "unstable", folder
            os.chown(folder, 0, -1)
    else:
        mine = os.getuid()
        with monkeypatch.context() as patch:
            # Asked who it is, the system names another account: everything this test made
            # now belongs to somebody else. What belongs to root is still accepted.
            patch.setattr(os, "getuid", lambda: mine + 1)
            assert problem() == "unstable"
    assert problem() is None

    # A link on the way: the folder it leads to is looked at.
    real = lasting / "kept elsewhere"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python").symlink_to(sys.executable)
    (lasting / "a link").symlink_to(real)
    through = lasting / "a link" / "bin" / "python"
    assert problem(through) is None
    real.chmod(0o777)
    assert problem(through) == "unstable"
    real.chmod(0o755)
    (real / "vault").mkdir()
    assert problem(store=lasting / "a link" / "vault") is None

    # A link that leads past the rule: into a folder for temporary files, or around a folder
    # that everybody may write. As it is written such a path passes; each path is therefore
    # judged a second time, where it really lies.
    in_tmp = tmp_path / "kept under tmp"
    (in_tmp / "bin").mkdir(parents=True)
    (in_tmp / "bin" / "python").symlink_to(sys.executable)
    (in_tmp / "vault").mkdir()
    (lasting / "into tmp").symlink_to(in_tmp)
    assert not hostwrite._may_not_last(str(lasting / "into tmp" / "bin" / "python"))
    assert problem(lasting / "into tmp" / "bin" / "python") == "unstable"
    assert problem(store=lasting / "into tmp" / "vault") == "unstable"
    # ... also where the link is the last part of the vault's path.
    (lasting / "vault, linked").symlink_to(in_tmp / "vault")
    assert not hostwrite._may_not_last(str(lasting / "vault, linked"))
    assert problem(store=lasting / "vault, linked") == "unstable"
    assert write(lasting / "vault, linked") == {
        "connected": False,
        "state": "unstable",
        "path": hostconfig.shown_path(place),
    }
    (lasting / "vault, linked well").symlink_to(vault)
    assert problem(store=lasting / "vault, linked well") is None
    # The last part of the Python's path is not followed: the Python of a virtual environment
    # is a link, and what it leads to is not judged.
    (python.parent / "python, linked").symlink_to(in_tmp / "bin" / "python")
    assert problem(python.parent / "python, linked") is None
    # A link around a folder that everybody may write, or that belongs to another account.
    shared = lasting / "shared"
    (shared / "mine" / "bin").mkdir(parents=True)
    (shared / "mine" / "bin" / "python").symlink_to(sys.executable)
    (shared / "mine" / "vault").mkdir()
    for folder in (shared, shared / "mine", shared / "mine" / "bin", shared / "mine" / "vault"):
        folder.chmod(0o755)
    (lasting / "tools").symlink_to(shared / "mine")
    around = lasting / "tools" / "bin" / "python"
    assert problem(around) is None
    assert problem(store=lasting / "tools" / "vault") is None
    shared.chmod(0o777)
    assert not hostwrite._may_not_last(str(around))  # (as written, the folder is not on the way)
    assert problem(around) == "unstable"
    assert problem(store=lasting / "tools" / "vault") == "unstable"
    shared.chmod(0o755)
    if os.getuid() == 0:
        os.chown(shared, 4242, -1)
        assert problem(around) == "unstable"
        os.chown(shared, 0, -1)
    assert problem(around) is None
    # Where the system does not say where a path lies, it may not last.
    real_realpath = os.path.realpath
    for unknown in (python.parent, vault):

        def refusing(path: Any, unknown: Path = unknown, **kwargs: Any) -> str:
            if os.fspath(path) == str(unknown):
                raise OSError(errno.ELOOP, os.strerror(errno.ELOOP))
            return real_realpath(path, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os.path, "realpath", refusing)
            assert problem() == "unstable", unknown
    assert problem() is None

    # Under the folder for temporary files, where pytest's own folders lie.
    elsewhere_python = tmp_path / "venv" / "bin" / "python"
    elsewhere_python.parent.mkdir(parents=True)
    elsewhere_python.symlink_to(sys.executable)
    assert problem(elsewhere_python) == "unstable"
    assert problem(store=tmp_path / "vault") == "unstable"
    assert problem(store=bare_home) == "unstable"
    assert write(tmp_path / "vault")["state"] == "unstable"
    for named in ("/tmp/vault", "/var/tmp/vault", "/dev/shm/vault", tempfile.gettempdir() + "/v"):
        assert problem(store=named) == "unstable", named
    # ... as these folders are named, and where they really lie. (On a Mac the folder for
    # temporary files is named through a link; here a link stands in for that.)
    (lasting / "tmp, really").mkdir()
    (lasting / "tmp").symlink_to(lasting / "tmp, really")
    for folder in ("tmp", "tmp, really"):
        (lasting / "tmp" / "vault").mkdir(exist_ok=True)
        assert problem(store=lasting / folder / "vault") is None
    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "gettempdir", lambda: str(lasting / "tmp"))
        for folder in ("tmp", "tmp, really"):
            assert problem(store=lasting / folder / "vault") == "unstable", folder
            assert problem(store=lasting / folder) == "unstable", folder
        assert problem() is None
        assert problem(store=lasting / "tmp beside") == "unstable"  # (it is not there)
        (lasting / "tmp beside").mkdir()
        assert problem(store=lasting / "tmp beside") is None  # a name that only begins like it

    # A path that is not there, and one that does not start at the root.
    assert problem(store=lasting / "no such vault") == "unstable"
    assert problem(lasting / "venv" / "bin" / "no such python") == "unstable"
    assert hostwrite.write_problem(host, ["python", "--store", str(vault)]) == "unstable"
    assert hostwrite.write_problem(host, [str(python), "--store", "vault"]) == "unstable"
    assert hostwrite.write_problem(host, []) == "unstable"
    # The look says the same word, beside what it found in the file.
    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "gettempdir", lambda: str(lasting))
        seen = hostwrite.look(host, client=host, store=vault)
        assert (seen["state"], seen["problem"]) == ("empty", "unstable")
    seen = hostwrite.look(host, client=host, store=vault)
    assert (seen["state"], seen["problem"]) == ("empty", None)


def test_where_pytest_keeps_its_folders_no_command_lasts(bare_home: Path, tmp_path: Path) -> None:
    """Why every other test here replaces the judge of places: this is what it says here."""
    (tmp_path / "vault").mkdir()
    if lies_well(tmp_path / "vault"):
        pytest.skip("pytest was told to keep its folders outside the folder for temporary files")
    command = hostconfig.host_command(JSON_HOST, tmp_path / "vault")
    assert hostwrite.write_problem(JSON_HOST, command) == "unstable"
    put(folder_of(JSON_HOST), "{}")
    before = tree(bare_home)
    told = Told()
    answer = hostwrite.connect(
        JSON_HOST, client=JSON_HOST, store=tmp_path / "vault", expect="empty", section=told
    )
    assert (answer["connected"], answer["state"]) == (False, "unstable")
    assert (told.entered, tree(bare_home)) == (0, before)
    # Taking an entry out writes no command, so it does not ask where one lies.
    put(
        place_of(JSON_HOST),
        json.dumps({"mcpServers": {SLUG: ours(JSON_HOST, str(tmp_path / "vault"))}}),
    )
    told = Told()
    answer = hostwrite.disconnect(
        JSON_HOST, client=JSON_HOST, store=tmp_path / "vault", expect="present", section=told
    )
    assert (answer["file"], told.actions) == ("removed", ["disconnect"])


# -- a write that went through ----------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_the_write_is_one_step(host: str, home: Path) -> None:
    place = put(folder_of(host), other(host))
    whole = {place.read_bytes()}
    seen: set[bytes] = set()
    stop = threading.Event()
    failures: list[BaseException] = []

    def reading() -> None:
        try:
            while not stop.is_set():
                seen.add(place.read_bytes())
        except BaseException as exc:  # a moment at which the file was not there
            failures.append(exc)

    reader = threading.Thread(target=reading, daemon=True)
    reader.start()
    try:
        state = "other"
        for number in range(200):
            store = f"/srv/vault {number % 2}"
            answer = connect(host, state, store=store)
            assert answer["connected"] is True, (number, answer)
            whole.add(place.read_bytes())
            aged(place)
            state = "differs"
    finally:
        stop.set()
        reader.join(timeout=30)
    assert not failures, failures
    assert len(whole) == 3  # the file as it was, and the entry for one vault and the other
    assert len(seen) >= 2, "the reader saw no write"
    assert seen <= whole, "a reader saw a file that was neither the old nor the new one"
    assert no_temporary_file(place.parent)


@pytest.mark.parametrize("host", HOSTS)
def test_the_result_is_read_back(host: str, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    place = put(folder_of(host), other(host))
    shown = hostconfig.shown_path(place)
    real_replace = os.replace
    # After a write the look says that the entry is there.
    assert connect(host, "other")["state"] == "present"
    assert state_of(host) == "present"
    written = place.read_text(encoding="utf-8")
    assert written == with_ours(host)

    def then(content: str) -> Callable[..., None]:
        """A replace after which another program at once writes the file again."""

        def replace(src: Any, dst: Any, **kwargs: Any) -> None:
            real_replace(src, dst, **kwargs)
            if os.fspath(dst) == place.name:
                place.write_text(content, encoding="utf-8")

        return replace

    # Another program writes the file again right after the replace, and drops the entry.
    put(place, other(host))
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", then(other(host, ", written again")))
        told = Told()
        assert connect(host, "other", told) == {
            "connected": False,
            "state": "failed",
            "path": shown,
        }
    assert told.actions == ["connect", "connect-failed"]
    assert told.entered == 2  # the failure was found after the section was left
    assert place.read_text(encoding="utf-8") == other(host, ", written again")
    assert no_temporary_file(place.parent)
    # It writes the file again and keeps the entry: connected, and said to have changed since.
    put(place, other(host))
    kept = written + ("\n" if host == JSON_HOST else "# saved again\n")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", then(kept))
        told = Told()
        answer = connect(host, "other", told)
    assert (answer["connected"], answer["state"], answer.get("changed_since")) == (
        True,
        "present",
        True,
    )
    assert told.actions == ["connect"]
    # It adds a member to the entry: connected all the same, as an entry that holds more.
    put(place, other(host))
    more = (
        written.replace('"args"', '"enabled": true,\n      "args"')
        if host == JSON_HOST
        else written + "enabled = true\n"
    )
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", then(more))
        answer = connect(host, "other")
    assert (answer["connected"], answer["state"], answer.get("changed_since")) == (
        True,
        "present_more",
        True,
    )
    # A write that nobody touched says nothing of the kind.
    put(place, other(host))
    assert "changed_since" not in connect(host, "other")
    # "Disconnect": the entry is back right after it was taken out.
    aged(place)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", then(written))
        told = Told()
        assert disconnect(host, "present", told) == {"file": "failed", "path": shown, "copy": None}
    assert told.actions == ["disconnect", "disconnect-failed"]
    # Where the failure cannot be recorded because the vault has locked, that is what the
    # caller is told.
    put(place, other(host))
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", then(other(host, ", written again")))
        told = Told()
        told.fail["connect-failed"] = Refused(409, "locked")
        with pytest.raises(Refused):
            connect(host, "other", told)
    assert told.actions == ["connect"]
    assert no_temporary_file(place.parent)


@pytest.mark.parametrize("host", HOSTS)
def test_a_read_back_that_cannot_read_is_not_told_as_a_failure(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file was replaced, and the system then does not let it be read.

    Saying "failed" would be untrue, and a second entry of the record would say that the
    file was not changed as announced. The name is looked at instead: where it leads to the
    file this call put there, the change was made, and the answer says that it was not read
    back. Anything else is a failure, as before.
    """
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    first = shown + hostwrite.FIRST_COPY
    last = shown + hostwrite.LAST_COPY
    real_open, real_read, real_lstat = os.open, os.read, os.lstat
    real_replace, real_link = os.replace, os.link
    placed: list[str] = []  # filled once the settings file was put in place
    after: list[Callable[[], None]] = []  # what another program does right after that

    def replace(src: Any, dst: Any, **kwargs: Any) -> None:
        real_replace(src, dst, **kwargs)
        if os.fspath(dst) == place.name:
            placed.append("replace")
            for act in after:
                act()

    def link(src: Any, dst: Any, **kwargs: Any) -> None:
        real_link(src, dst, **kwargs)
        if os.fspath(dst) == place.name:
            placed.append("link")
            for act in after:
                act()

    def opening(error: OSError) -> Callable[..., int]:
        def open_(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if placed and os.fspath(path) == place.name:
                raise error
            return real_open(path, flags, *args, **kwargs)

        return open_

    def no_reading(descriptor: int, count: int) -> bytes:
        if placed:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_read(descriptor, count)

    def press(
        patches: dict[str, Any],
        call: Callable[[Told], dict[str, Any]],
        then: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], Told]:
        placed.clear()
        after[:] = [] if then is None else [then]
        told = Told()
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", replace)
            patch.setattr(os, "link", link)
            for name, function in patches.items():
                patch.setattr(os, name, function)
            answer = call(told)
        return answer, told

    def clear() -> None:
        for item in place.parent.iterdir():
            item.unlink()

    no_descriptor = {"open": opening(OSError(errno.EMFILE, os.strerror(errno.EMFILE)))}
    ways: list[dict[str, Any]] = [
        no_descriptor,
        {"open": opening(PermissionError(errno.EACCES, os.strerror(errno.EACCES)))},
        {"read": no_reading},
    ]
    # "Connect": the entry is in the file, the answer says so, and the record has one entry.
    for way in ways:
        clear()
        put(place, other(host))
        answer, told = press(way, lambda told: connect(host, "other", told))
        assert answer == {
            "connected": True,
            "state": "present",
            "path": shown,
            "copy": first,
            "replaced": False,
            "not_read_back": True,
        }, way
        assert (placed, told.actions, told.entered) == (["replace"], ["connect"], 1)
        assert place.read_text(encoding="utf-8") == with_ours(host)
        assert names(place.parent) == [place.name, place.name + hostwrite.FIRST_COPY]
        assert state_of(host) == "present"
    # An entry that pointed elsewhere and holds more: the state is told from the text written.
    clear()
    put(place, ENTRY_ELSEWHERE_JSON if host == JSON_HOST else ENTRY_ELSEWHERE_TOML)
    answer, told = press(no_descriptor, lambda told: connect(host, "differs", told))
    assert answer == {
        "connected": True,
        "state": "present_more",
        "path": shown,
        "copy": last,
        "replaced": True,
        "not_read_back": True,
    }
    assert told.actions == ["connect"]
    assert state_of(host) == "present_more"
    # A file that was not there.
    clear()
    answer, told = press(no_descriptor, lambda told: connect(host, "no_file", told))
    assert answer == {
        "connected": True,
        "state": "present",
        "path": shown,
        "copy": None,
        "replaced": False,
        "not_read_back": True,
    }
    assert (placed, told.actions) == (["link"], ["connect"])
    assert names(place.parent) == [place.name]
    assert (state_of(host), place.stat().st_nlink) == ("present", 1)
    # "Disconnect": the entry is gone, and the answer says "removed".
    for way in ways:
        clear()
        put(place, with_ours(host))
        answer, told = press(way, lambda told: disconnect(host, "present", told))
        assert answer == {"file": "removed", "path": shown, "copy": last, "not_read_back": True}
        assert told.actions == ["disconnect"]
        assert place.read_text(encoding="utf-8") in (other(host), other(host) + "\n")
        assert state_of(host) == "other"
    # A write that was read back says nothing of the kind.
    clear()
    put(place, other(host))
    answer, told = press({}, lambda told: connect(host, "other", told))
    assert (answer["connected"], "not_read_back" in answer) == (True, False)

    # What is still a failure: the file cannot be read back, and its name does not lead to
    # the file this call put there, or nothing can be said about it.
    def failed(then: Callable[[], None] | None, patches: dict[str, Any] = no_descriptor) -> None:
        clear()
        put(place, other(host))
        answer, told = press(patches, lambda told: connect(host, "other", told), then)
        assert answer == {"connected": False, "state": "failed", "path": shown}
        assert told.actions == ["connect", "connect-failed"]
        assert told.entered == 2
        assert no_temporary_file(place.parent)

    # Another program put a file of its own there.
    failed(lambda: exchange(place, other(host, ", saved by another program")))
    assert place.read_text(encoding="utf-8") == other(host, ", saved by another program")
    # It removed the file.
    failed(place.unlink)
    assert not place.exists()
    # It wrote into the file that was put there: more bytes, or as many at another time.
    failed(lambda: place.write_text(with_ours(host) + "\n", encoding="utf-8"))

    def same_size_later() -> None:
        place.write_text(with_ours(host).replace("tool", "loot"), encoding="utf-8")
        ahead = time.time() + 2
        os.utime(place, (ahead, ahead))

    failed(same_size_later)
    # A link took its place.
    kept = put(home / "kept elsewhere", with_ours(host))

    def linked() -> None:
        place.unlink()
        place.symlink_to(kept)

    failed(linked)
    assert place.is_symlink()
    # The system says nothing about the name either.

    def no_look(path: Any, **kwargs: Any) -> os.stat_result:
        if placed and os.fspath(path) == place.name:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_lstat(path, **kwargs)

    failed(None, {**no_descriptor, "lstat": no_look})
    # "Disconnect" in the same way.
    clear()
    put(place, with_ours(host))
    answer, told = press(
        no_descriptor,
        lambda told: disconnect(host, "present", told),
        lambda: exchange(place, with_ours(host)),
    )
    assert answer == {"file": "failed", "path": shown, "copy": None}
    assert told.actions == ["disconnect", "disconnect-failed"]


@pytest.mark.parametrize("host", HOSTS)
def test_a_close_that_fails_at_the_end_fails_no_call(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last thing a call does is to close the folder it held open.

    By then the file was changed and the record says so. If the system reports that
    close as failed, the call must still answer what it did: an error there would tell
    the owner that nothing is known of a change that was made and recorded.
    """
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    real_open_folder, real_close = hostcheck.open_folder, os.close
    held: set[int] = set()  # the folders a call holds open
    failed: list[int] = []  # each close of one of them, which the system "refused"

    def open_folder(path: Path, base: Path) -> int:
        descriptor = real_open_folder(path, base)
        held.add(descriptor)
        return descriptor

    def close(descriptor: int) -> None:
        real_close(descriptor)  # the descriptor is given back, as the system does on an error
        if descriptor in held:
            held.discard(descriptor)
            failed.append(descriptor)
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    def press(call: Callable[[Told], Any]) -> tuple[Any, Told, int]:
        failed.clear()
        told = Told()
        with monkeypatch.context() as patch:
            patch.setattr(hostcheck, "open_folder", open_folder)
            patch.setattr(os, "close", close)
            answer = call(told)
        assert not held, "a folder was left open"
        return answer, told, len(failed)

    # "Connect": written, recorded once, answered as written.
    put(place, other(host))
    answer, told, closes = press(lambda told: connect(host, "other", told))
    assert answer == {
        "connected": True,
        "state": "present",
        "path": shown,
        "copy": shown + hostwrite.FIRST_COPY,
        "replaced": False,
    }
    assert (told.actions, told.entered, closes) == (["connect"], 1, 1)
    assert place.read_text(encoding="utf-8") == with_ours(host)
    assert no_temporary_file(place.parent)
    # A look, and a connect that finds the entry there: nothing is written, and neither raises.
    answer, _, closes = press(lambda told: looked(host))
    assert (answer["state"], closes) == ("present", 1)
    answer, told, closes = press(lambda told: connect(host, "present", told))
    assert (answer["connected"], told.actions, closes) == (True, ["connect-kept"], 1)
    # A refusal is answered as one.
    answer, told, closes = press(lambda told: connect(host, "other", told))
    assert (answer, told.actions, closes) == (
        {"connected": False, "state": "changed", "path": shown},
        [],
        1,
    )
    # "Disconnect": removed, recorded once, answered as removed.
    aged(place)
    answer, told, closes = press(lambda told: disconnect(host, "present", told))
    assert answer == {"file": "removed", "path": shown, "copy": shown + hostwrite.LAST_COPY}
    assert (told.actions, told.entered, closes) == (["disconnect"], 1, 1)
    assert state_of(host) == "other"
    assert no_temporary_file(place.parent)


def test_a_close_that_fails_while_a_place_is_judged_decides_nothing(
    bare_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The judgement of where a command lies (rule R17) holds the root folder open while it
    # walks the path. What it found stands, whatever the system says when that is closed.
    system_folder = os.path.join(os.sep, "usr")
    judged = hostwrite._may_not_last(system_folder)
    real_open_folder, real_close = hostcheck.open_folder, os.close
    held: set[int] = set()
    failed: list[int] = []

    def open_folder(path: Path, base: Path) -> int:
        descriptor = real_open_folder(path, base)
        held.add(descriptor)
        return descriptor

    def close(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor in held:
            held.discard(descriptor)
            failed.append(descriptor)
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    with monkeypatch.context() as patch:
        patch.setattr(hostcheck, "open_folder", open_folder)
        patch.setattr(os, "close", close)
        assert hostwrite._may_not_last(system_folder) is judged
    assert (len(failed), held) == (1, set())


@pytest.mark.parametrize("host", HOSTS)
def test_what_the_new_file_keeps_of_the_old(host: str, home: Path) -> None:
    place = folder_of(host)

    def after(mode: int) -> int:
        for item in place.parent.iterdir():
            item.unlink()
        put(place, other(host))
        try:
            place.chmod(mode)
        except OSError:  # a system that lets this account set no such bit on a file
            place.chmod(mode & 0o777)
        aged(place)
        before = place.stat().st_ino
        assert connect(host, "other")["connected"] is True
        # The name leads to a new file: the old one was replaced, not written into.
        assert place.stat().st_ino != before
        return stat.S_IMODE(place.stat().st_mode)

    for umask in (0o022, 0o077, 0o000):
        old_umask = os.umask(umask)
        try:
            # The lowest nine bits of the mode are those of the old file.
            for mode in (0o644, 0o600, 0o640, 0o664, 0o400, 0o444):
                assert after(mode) == mode, (oct(umask), oct(mode))
            # The bits above them are not carried over.
            assert after(0o2640) == 0o640
            assert after(0o4755) == 0o755
            assert after(0o1644) == 0o644
            # A new file is the owner's alone, whatever the mask.
            for item in place.parent.iterdir():
                item.unlink()
            assert connect(host, "no_file")["connected"] is True
            assert stat.S_IMODE(place.stat().st_mode) == 0o600
            # And so is every copy.
            aged(place)
            assert disconnect(host, "present")["file"] == "removed"
            for name in names(place.parent):
                if name != place.name:
                    assert stat.S_IMODE((place.parent / name).stat().st_mode) == 0o600
        finally:
            os.umask(old_umask)
    # Where the old file has another group than a file this account makes, the new file
    # has the group of the account, and the bits of the group are gone.
    for item in place.parent.iterdir():
        item.unlink()
    put(place, other(host), mode=0o664)
    mine = place.stat().st_gid
    groups = [group for group in os.getgroups() if group != mine]
    if os.getuid() == 0:
        groups = [4242]
    given = False
    if groups:
        try:
            os.chown(place, -1, groups[0])
            given = place.stat().st_gid == groups[0]
        except OSError:  # this account may not give its file to that group here
            given = False
    if given:
        place.chmod(0o664)
        aged(place)
        assert connect(host, "other")["connected"] is True
        assert (place.stat().st_gid, stat.S_IMODE(place.stat().st_mode)) == (mine, 0o604)


@pytest.mark.parametrize("host", HOSTS)
def test_disconnect_removes_one_entry_and_nothing_else(host: str, home: Path) -> None:
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    first = Path(str(place) + hostwrite.FIRST_COPY)
    last = Path(str(place) + hostwrite.LAST_COPY)
    is_json = host == JSON_HOST
    # With other servers: after a connect and a disconnect, the file holds what it held.
    put(place, other(host))
    assert connect(host, "other")["connected"] is True
    connected = place.read_bytes()
    aged(place)
    told = Told()
    assert disconnect(host, "present", told) == {
        "file": "removed",
        "path": shown,
        "copy": shown + hostwrite.LAST_COPY,
    }
    assert told.actions == ["disconnect"]
    left = place.read_text(encoding="utf-8")
    assert left == (other(host) if is_json else other(host) + "\n")
    assert state_of(host) == "other"
    # Both copies stay.
    assert first.read_text(encoding="utf-8") == other(host)
    assert last.read_bytes() == connected
    assert names(place.parent) == [place.name, first.name, last.name]
    # A second press finds no entry: the file is left, and the copies too.
    before = tree(home)
    told = Told()
    assert disconnect(host, "present", told)["file"] == "changed"
    assert told.actions == []
    told = Told()
    assert disconnect(host, "other", told) == {"file": "no_entry", "path": shown, "copy": None}
    assert told.actions == ["disconnect-kept"]
    assert tree(home) == before

    # Without other servers: a section that becomes empty stays.
    for item in place.parent.iterdir():
        item.unlink()
    if is_json:
        put(place, json.dumps({"theme": "dark", "mcpServers": {SLUG: ours(host)}}, indent=2))
        assert disconnect(host, "present")["file"] == "removed"
        assert place.read_text(encoding="utf-8") == '{\n  "theme": "dark",\n  "mcpServers": {}\n}'
        seen = looked(host)
        assert (seen["state"], seen["section"]) == ("other", "empty")
    else:
        block = "\n".join(toml_lines(host))
        put(place, f"[mcp_servers]\n# the servers\n\n{block}\n# a comment inside the table\n")
        assert disconnect(host, "present")["file"] == "removed"
        assert place.read_text(encoding="utf-8") == (
            "[mcp_servers]\n# the servers\n\n# a comment inside the table\n"
        )
        assert state_of(host) == "empty"

    # An entry that holds more: the entry and what is below it are gone, and the latest
    # copy holds them.
    for item in place.parent.iterdir():
        item.unlink()
    if is_json:
        more = {**ours(host), "enabled": False, "env": {"TOKEN": SECRET}}
        text = json.dumps({"mcpServers": {"a": {"command": "x"}, SLUG: more}}, indent=2)
    else:
        block = "\n".join(toml_lines(host))
        text = (
            f'[mcp_servers.a]\ncommand = "x"\n\n{block}\nenabled = false\n\n'
            f'[mcp_servers.{SLUG}.env]\nTOKEN = "{SECRET}"\n'
        )
    put(place, text)
    seen = looked(host)
    assert (seen["state"], seen["switched_off"]) == ("present_more", True)
    assert disconnect(host, "present")["file"] == "changed"  # not the state that was shown
    answer = disconnect(host, "present_more")
    assert (answer["file"], answer["copy"]) == ("removed", shown + hostwrite.LAST_COPY)
    left = place.read_text(encoding="utf-8")
    assert SECRET not in left
    assert SLUG not in left
    assert left == (
        json.dumps({"mcpServers": {"a": {"command": "x"}}}, indent=2)
        if is_json
        else '[mcp_servers.a]\ncommand = "x"\n\n'  # of two blank lines that met, one went
    )
    assert last.read_text(encoding="utf-8") == text
    assert SECRET not in json.dumps(answer)


@pytest.mark.parametrize("host", HOSTS)
def test_disconnect_leaves_an_entry_that_points_elsewhere(host: str, home: Path) -> None:
    place = folder_of(host)
    shown = hostconfig.shown_path(place)
    cases: list[tuple[str | None, str, str]] = [
        (elsewhere(host), "differs", "differs"),
        (other(host), "other", "no_entry"),
        ("", "empty", "no_entry"),
        (None, "no_file", "no_entry"),
    ]
    if host == TOML_HOST:
        cases.append((with_ours(host, client="codex"), "present_as", "differs"))
    for content, state, word in cases:
        for item in place.parent.iterdir():
            item.unlink()
        if content is not None:
            place.write_text(content, encoding="utf-8")  # a moment ago: nothing is written
        before = tree(home)
        assert state_of(host) == state
        told = Told()
        assert disconnect(host, state, told) == {"file": word, "path": shown, "copy": None}
        assert told.actions == ["disconnect-kept"]
        assert tree(home) == before, state
        # Asked for the entry the owner was not shown, it does nothing.
        for expect in ("present", "present_more"):
            told = Told()
            assert disconnect(host, expect, told)["file"] == "changed"
            assert (told.entered, told.actions) == (0, [])
        assert tree(home) == before


@pytest.mark.parametrize("host", HOSTS)
def test_a_link_in_the_files_place_is_not_followed(
    host: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = folder_of(host)
    target = put(home / "kept elsewhere", other(host, ", the target"))
    kept = (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino)

    def link() -> None:
        place.unlink()
        place.symlink_to(target)

    def untouched() -> None:
        assert (target.read_bytes(), target.stat().st_mtime_ns, target.stat().st_ino) == kept
        assert target.stat().st_nlink == 1

    # A link put in the file's place between the look and the replace.
    for when in ("before the first comparison", "after the record"):
        put(place, other(host))
        told = Told()
        if when == "before the first comparison":
            told.on_enter = link
        else:
            told.on["connect"] = link
        assert connect(host, "other", told)["state"] == "changed"
        assert place.is_symlink()
        untouched()
        assert names(place.parent) == [place.name]
        place.unlink()
    # At the last moment, after both comparisons: the link is replaced, not gone through.
    put(place, other(host))
    real_replace = os.replace

    def late(src: Any, dst: Any, **kwargs: Any) -> None:
        if os.fspath(dst) == place.name:
            link()
        real_replace(src, dst, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", late)
        answer = connect(host, "other")
    assert answer["connected"] is True
    assert not place.is_symlink()
    untouched()
    # A link where a new file would go: nothing is written through it either.
    place.unlink()
    for item in place.parent.iterdir():
        item.unlink()
    told = Told()
    told.on_enter = lambda: place.symlink_to(target)
    assert connect(host, "no_file", told)["state"] == "changed"
    untouched()
    place.unlink()
    place.symlink_to(home / "nothing there")
    assert connect(host, "no_file")["state"] == "not_read"
    assert not (home / "nothing there").exists()
    # A link in the place of a temporary file cannot be: the name is new at every call,
    # and the file is made only if nothing is there.
    place.unlink()
    with monkeypatch.context() as patch:
        patch.setattr(secrets, "token_hex", lambda count: "ab" * count)
        taken = place.with_name(f".{place.name}.{'ab' * 12}.tmp")
        taken.symlink_to(target)
        assert connect(host, "no_file")["state"] == "failed"
        untouched()
        assert taken.is_symlink()


# -- the module by itself ---------------------------------------------------------------------

GATE = Path(wdva.gate.__file__).resolve().parent


def test_the_writer_does_only_what_it_says() -> None:
    source = Path(hostwrite.__file__).read_text(encoding="utf-8")
    module = ast.parse(source)
    # What it imports: nothing that starts a program, reaches a network or exists on newer
    # Pythons only; and nothing is imported inside a function.
    top: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.Import):
            top.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add(node.module or "")
    assert top == {
        "__future__",
        "json",
        "os",
        "re",
        "secrets",
        "stat",
        "tempfile",
        "time",
        "collections.abc",
        "contextlib",
        "pathlib",
        "typing",
        "wdva.gate",
    }
    imports = [node for node in ast.walk(module) if isinstance(node, ast.Import | ast.ImportFrom)]
    assert all(node in module.body for node in imports)
    assert "__import__" not in source
    assert "import_module" not in source

    def attributes(of: str) -> set[str]:
        return {
            node.attr
            for node in ast.walk(module)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == of
        }

    # Every call on `os` is one of these, and each that takes a path names it relative to a
    # folder that is held open.
    calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ]
    called = {node.func.attr for node in calls if isinstance(node.func, ast.Attribute)}
    assert called == {
        "open",
        "fstat",
        "lstat",
        "write",
        "close",
        "fsync",
        "fchmod",
        "replace",
        "link",
        "unlink",
        "getuid",
        "listdir",
    }
    for node in calls:
        assert isinstance(node.func, ast.Attribute)
        given = {keyword.arg for keyword in node.keywords}
        if node.func.attr in ("open", "lstat", "unlink"):
            assert "dir_fd" in given, f"line {node.lineno}"
        elif node.func.attr in ("replace", "link"):
            assert {"src_dir_fd", "dst_dir_fd"} <= given, f"line {node.lineno}"
        elif node.func.attr == "listdir":
            # The names in a folder are read from the open folder itself, not from a path.
            (argument,) = node.args
            assert isinstance(argument, ast.Name)
            assert argument.id == "folder"
        else:
            assert node.func.attr in ("fstat", "write", "close", "fsync", "fchmod", "getuid")
            assert not given
    # Nothing else of `os` is used but the environment, the tools for path names, two types
    # and the flags of a file that is made new and never one that exists.
    assert attributes("os") - called == {
        "environ",
        "path",
        "sep",
        "stat_result",
        "PathLike",
        "O_WRONLY",
        "O_CREAT",
        "O_EXCL",
        "O_NOFOLLOW",
    }
    path_calls = {
        node.func.attr
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "path"
    }
    assert path_calls == {"isabs", "normpath", "realpath"}
    # The name of a temporary file comes from `secrets`; `tempfile` is asked only where the
    # folder for temporary files lies, and makes no file here.
    assert attributes("secrets") == {"token_hex"}
    assert attributes("tempfile") == {"gettempdir"}
    assert attributes("time") == {"time", "strftime", "gmtime"}
    assert attributes("stat") == {"S_ISREG", "S_ISLNK", "S_IMODE", "S_IWOTH"}
    # What it asks of the two modules beside it: the place, the text of an entry, the look.
    assert attributes("hostcheck") == {
        "MAX_BYTES",
        "STATES",
        "Look",
        "FolderProblem",
        "open_folder",
        "look",
        "_json_loads",
        "_toml_loads",
    }
    # No name in the code makes a folder, moves or renames a file, changes an owner, cuts a
    # file short or starts a program, and nothing is opened through the built-in open.
    used = {node.id for node in ast.walk(module) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(module) if isinstance(node, ast.Attribute)}
    for word in used:
        for part in ("mkdir", "makedirs", "rename", "chown", "symlink", "truncate", "rmdir"):
            assert part not in word.lower(), word
        for part in ("system", "popen", "spawn", "fork", "exec", "subprocess", "shutil", "socket"):
            assert part not in word.lower(), word
        for part in ("mkstemp", "mkdtemp", "namedtemporary", "chdir", "chroot", "expandvars"):
            assert part not in word.lower(), word
    assert "open" not in {node.id for node in ast.walk(module) if isinstance(node, ast.Name)}
    for flag in ("O_TRUNC", "O_APPEND", "O_RDWR"):
        assert flag not in source
    # What replaces, links, removes or writes is `os` and nothing else: no method of a path.
    for node in ast.walk(module):
        if isinstance(node, ast.Attribute) and node.attr in (
            "replace",
            "link",
            "unlink",
            "write",
            "write_text",
            "write_bytes",
            "touch",
            "chmod",
        ):
            assert isinstance(node.value, ast.Name), f"line {node.lineno}"
            assert node.value.id == "os", f"line {node.lineno}"


def test_the_writer_is_the_only_one() -> None:
    writes = ("replace", "link", "rename", "unlink", "remove", "open", "write", "truncate")
    path_writes = ("write_text", "write_bytes", "rename", "replace", "unlink", "touch", "open")
    importers: list[str] = []
    for path in sorted(GATE.rglob("*.py")):
        name = path.relative_to(GATE).as_posix()
        source = path.read_text(encoding="utf-8")
        module = ast.parse(source)
        for node in ast.walk(module):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [f"{node.module}.{alias.name}" for alias in node.names]
                imported.append(node.module or "")
            if any(module_name.endswith("hostwrite") for module_name in imported):
                importers.append(name)
        if name == "hostwrite.py":
            continue
        # Whatever function of another source learns where a settings file lies, changes no
        # file: it calls nothing of `os` that writes, and no method of a path that does.
        for function in ast.walk(module):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            mentioned = {
                node.attr if isinstance(node, ast.Attribute) else node.id
                for node in ast.walk(function)
                if isinstance(node, ast.Attribute | ast.Name)
            }
            if not mentioned & {"settings_file", "settings_place", "open_folder"}:
                continue
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                on_os = isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                if on_os and node.func.attr == "open":
                    continue  # the reader's own, pinned to reading by its own test
                where = f"{name}:{node.lineno}"
                assert not (on_os and node.func.attr in writes), where
                assert not (not on_os and node.func.attr in path_writes), where
    # The reader and the module of the forms write nothing at all.
    for name in ("hostcheck.py", "hostconfig.py"):
        source = (GATE / name).read_text(encoding="utf-8")
        for word in ("os.replace", "os.link", "os.rename", "os.unlink", "os.write", "shutil"):
            assert word not in source, (name, word)
    # The writer is for the app's service and nobody else.
    assert set(importers) == {"app/service.py"}


@needs_toml
def test_grok_build_follows_xais_page(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = "grok-build"
    # The place: `~/.grok/config.toml`, or `$GROK_HOME/config.toml`.
    assert place_of(host) == home / ".grok" / "config.toml"
    entry = hostconfig.known_host(host)
    assert (entry.source, entry.read) == (
        "https://docs.x.ai/build/features/mcp-servers",
        "2026-11-14",
    )
    # The entry: a table `[mcp_servers.<name>]` with `command` and `args`, three lines.
    place = folder_of(host)
    answer = connect(host, "no_file")
    assert (answer["connected"], answer["path"]) == (True, "~/.grok/config.toml")
    command = command_of(host)
    lines = place.read_text(encoding="utf-8").split("\n")
    assert lines == [
        f"[mcp_servers.{SLUG}]",
        f"command = {json.dumps(command[0])}",
        "args = [" + ", ".join(json.dumps(part) for part in command[1:]) + "]",
        "",
    ]
    assert toml(place.read_text(encoding="utf-8")) == {"mcp_servers": {SLUG: ours(host)}}
    assert command[command.index("--client") + 1] == "grok-build"
    # It is the text the owner would paste, line for line.
    block = hostconfig.render(host, client=host, store=STORE).blocks[0]
    assert (block.language, block.text) == ("toml", "\n".join(lines[:3]))
    # With the variable set, the file is the one in that folder, and the look names the variable.
    moved = tmp_path / "grok of mine"
    moved.mkdir()
    monkeypatch.setenv("GROK_HOME", str(moved))
    seen = looked(host)
    assert (seen["state"], seen["variable"], seen["path"]) == (
        "no_file",
        "GROK_HOME",
        str(moved / "config.toml"),
    )
    assert connect(host, "no_file")["connected"] is True
    assert (moved / "config.toml").read_text(encoding="utf-8") == place.read_text(encoding="utf-8")
    # The settings the page shows beside a server stay where they are when the entry changes.
    put(
        moved / "config.toml",
        f'[mcp_servers.{SLUG}]\ncommand = "npx"\nargs = ["-y", "x"]\nenabled = true\n'
        "startup_timeout_sec = 30\ntool_timeout_sec = 6000\n",
    )
    answer = connect(host, "differs")
    assert (answer["connected"], answer["state"]) == (True, "present_more")
    assert toml((moved / "config.toml").read_text(encoding="utf-8"))["mcp_servers"][SLUG] == {
        **ours(host),
        "enabled": True,
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 6000,
    }
