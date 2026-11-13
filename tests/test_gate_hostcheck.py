"""The look at another program's settings file: one fixed file, read with care, one word back.

The states a file can be in are walked through the service, with the answers the
page gets, in ``test_gate_service.py``. Here: the module by itself, for both
systems it knows, and the rules its source keeps.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from wdva.gate import brand, hostcheck, hostconfig

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

SLUG = brand.SLUG
STORE = "/srv/my vault"
SECRET = "SECRET-OF-ANOTHER-TOOL-5512"
UNSUPPORTED = {
    "state": "unsupported",
    "path": None,
    "folder": None,
    "open_line": None,
    "section": None,
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory that belongs to the test. No test looks into the real one."""
    place = tmp_path / "home"
    place.mkdir()
    monkeypatch.setenv("HOME", str(place))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return place


def entry(client: str = "claude-desktop") -> dict[str, Any]:
    command = hostconfig.host_command(client, STORE)
    return {"command": command[0], "args": command[1:]}


def look(host: str, platform: str, client: str = "claude-desktop") -> dict[str, Any]:
    return hostcheck.check(host, client=client, store=STORE, platform=platform)


def test_the_states_are_the_nine_words_and_the_sections_three() -> None:
    assert hostcheck.STATES == (
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
    assert hostcheck.SECTIONS == ("none", "empty", "filled")
    assert hostcheck.MAX_BYTES == 1024 * 1024


@pytest.mark.parametrize("platform", hostconfig.PLATFORMS)
def test_the_file_looked_at_is_the_one_of_that_system(platform: str, home: Path) -> None:
    mac = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    linux = home / ".config" / "Claude" / "claude_desktop_config.json"
    here, elsewhere = (mac, linux) if platform == "macos" else (linux, mac)
    # The right entry lies where the other system keeps the file: it is not looked at.
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")
    answer = look("claude-desktop", platform)
    shown = "~/" + here.relative_to(home).as_posix()
    assert answer == {
        "state": "no_folder",
        "path": shown,
        "folder": shown.rsplit("/", 1)[0],
        "open_line": None,
        "section": None,
    }
    here.parent.mkdir(parents=True)
    assert look("claude-desktop", platform)["state"] == "no_file"
    here.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")
    assert look("claude-desktop", platform)["state"] == "present"
    # The entry is the one of this app: under another name in the vault it differs.
    assert look("claude-desktop", platform, client="another-name")["state"] == "differs"
    # The line that opens the file names it in full, never with "~", which a quoted word
    # would not expand.
    here.write_text('{"theme": "dark"}', encoding="utf-8")
    answer = look("claude-desktop", platform)
    assert (answer["state"], answer["section"]) == ("other", "none")
    assert str(here) in answer["open_line"]
    assert "~" not in answer["open_line"]


def test_outside_macos_the_folder_follows_xdg_config_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config of mine"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    place = config_home / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    place.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")
    answer = look("claude-desktop", "linux")
    assert (answer["state"], answer["path"]) == ("present", str(place))
    # On a Mac that variable means nothing to the application.
    assert look("claude-desktop", "macos")["state"] == "no_folder"


def test_applications_without_a_known_settings_file_are_not_looked_for(home: Path) -> None:
    before = sorted(home.rglob("*"))
    for host in ("claude-code", "openclaw", "generic", "chatgpt-web", "claude-web", "grok-bot"):
        for platform in hostconfig.PLATFORMS:
            assert look(host, platform) == UNSUPPORTED
    assert sorted(home.rglob("*")) == before
    # An application nobody knows, a platform nobody knows, a name that cannot be used:
    # refused as the text for them is refused, before anything is looked at.
    with pytest.raises(ValueError, match="not an application"):
        look("/etc/passwd", "linux")
    with pytest.raises(ValueError, match="platform"):
        look("claude-desktop", "windows")
    with pytest.raises(ValueError, match="cannot be used as a name"):
        look("claude-desktop", "linux", client="../../etc/passwd")


def test_what_is_not_a_regular_file_is_never_opened(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    opened: list[str] = []
    real_open = os.open

    def noting(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        opened.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(hostcheck.os, "open", noting)
    target = home / "target.json"
    target.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")

    def not_read() -> None:
        answers: list[Any] = []
        thread = threading.Thread(
            target=lambda: answers.append(look("claude-desktop", "linux")), daemon=True
        )
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "the look waits"
        assert answers[0]["state"] == "not_read"
        assert SECRET not in json.dumps(answers[0])

    place.symlink_to(target)  # a link, to the very text that would be "present"
    not_read()
    place.unlink()
    place.symlink_to("/dev/zero")  # a link to a device that never ends
    not_read()
    place.unlink()
    os.mkfifo(place)  # a pipe nobody writes into
    not_read()
    place.unlink()
    place.mkdir()  # a folder
    not_read()
    place.rmdir()
    place.write_bytes(SECRET.encode() * 60_000)  # a file over 1 MiB
    assert place.stat().st_size > hostcheck.MAX_BYTES
    not_read()
    assert opened == [], "something that is not a small regular file was opened"
    # The counterpart: a small regular file is opened, once, for reading only.
    place.write_text("{}", encoding="utf-8")
    assert look("claude-desktop", "linux")["state"] == "empty"
    assert opened == [str(place)]


def test_a_file_exchanged_after_the_first_look_is_not_read_either(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    place.write_text("{}", encoding="utf-8")
    small = os.lstat(place)
    real_lstat = os.lstat

    def as_it_was(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        return small if os.fspath(path) == str(place) else real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(hostcheck.os, "lstat", as_it_was)
    place.unlink()
    os.mkfifo(place)  # opened without waiting, seen not to be a file, not read
    assert look("claude-desktop", "linux")["state"] == "not_read"
    place.unlink()
    elsewhere = home / "elsewhere.json"
    elsewhere.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")
    place.symlink_to(elsewhere)  # not followed when it is opened
    assert look("claude-desktop", "linux")["state"] == "not_read"
    place.unlink()
    place.write_bytes(b" " * (hostcheck.MAX_BYTES + 1))  # it grew
    assert look("claude-desktop", "linux")["state"] == "not_read"
    assert stat.S_ISREG(real_lstat(place).st_mode)


def test_a_file_the_system_refuses_is_unreadable_not_an_error(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    place.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")

    real_open = os.open

    def refuse(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.fspath(path) == str(place):
            raise PermissionError(13, "Permission denied", str(place))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(hostcheck.os, "open", refuse)
    answer = look("claude-desktop", "linux")
    monkeypatch.undo()
    assert answer["state"] == "unreadable"
    assert "Permission" not in json.dumps(answer)


def test_the_module_reads_and_never_writes() -> None:
    source = Path(hostcheck.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # What it imports at the top: nothing that starts a program, reaches a network or
    # exists on newer Pythons only.
    top: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add(node.module or "")
    assert top == {
        "__future__",
        "errno",
        "json",
        "os",
        "stat",
        "sys",
        "collections.abc",
        "pathlib",
        "typing",
        "wdva.gate",
    }
    # The reader for TOML is imported where it is used, so the package imports without it.
    inside = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Import)
        for alias in inner.names
    ]
    assert inside == ["tomllib"]
    # Every call on `os` is one that looks or reads.
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    }
    assert calls == {"open", "fstat", "read", "close", "stat", "lstat"}
    flags = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("O_")
    }
    assert flags == {"O_RDONLY"}  # the others are asked for by name, where the system has them
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
        assert f'"{name}"' in source
    # No name in the code is one that writes, makes, moves or removes anything, and nothing
    # is read through the built-in open, which would follow a link and could wait.
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for word in names:
        for part in ("write", "replace", "rename", "unlink", "remove", "mkdir", "makedirs"):
            assert part not in word.lower(), word
        for part in ("chmod", "chown", "truncate", "touch", "symlink", "expandvars", "system"):
            assert part not in word.lower(), word
    assert "open" not in {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
