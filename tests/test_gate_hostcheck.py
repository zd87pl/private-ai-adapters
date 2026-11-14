"""The look at another program's settings file: one fixed file, read with care, fixed words back.

The states a file can be in are walked through the service, with the answers the
page gets, in ``test_gate_service.py``. Here: the module by itself, for both
systems it knows, and the rules its source keeps. There is one look, the one the
writer builds on (``test_gate_hostwrite.py``): the helper ``look`` below gives what it
found as the words and places a test reads, and ``seen`` gives it whole.
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
    "section": None,
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory that belongs to the test. No test looks into the real one."""
    place = tmp_path / "home"
    place.mkdir()
    monkeypatch.setenv("HOME", str(place))
    for variable in ("XDG_CONFIG_HOME", "GROK_HOME", "CODEX_HOME"):
        monkeypatch.delenv(variable, raising=False)
    return place


def entry(client: str = "claude-desktop") -> dict[str, Any]:
    command = hostconfig.host_command(client, STORE)
    return {"command": command[0], "args": command[1:]}


def look(host: str, platform: str, client: str = "claude-desktop") -> dict[str, Any]:
    """What a look finds: its word, the file and its folder as an owner reads them, the section."""
    found = hostcheck.look(host, client=client, store=STORE, platform=platform)
    place = found.path
    return {
        "state": found.state,
        "path": None if place is None else hostconfig.shown_path(place),
        "folder": None if place is None else hostconfig.shown_path(place.parent),
        "section": found.section,
    }


def seen(host: str, platform: str = "linux", client: str | None = None) -> hostcheck.Look:
    """The look the writer builds on, for the name the application has by itself."""
    return hostcheck.look(host, client=client or host, store=STORE, platform=platform)


class Opened:
    """Notes what the reader opens, and through which open folder.

    The reader opens the folders on the way by their names, each through the one
    above it, and then the file by its name through its folder. ``paths`` gives
    every one of them as the full path it stands for.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, int, int | None]] = []
        self.paths: list[str] = []
        self._places: dict[int, str] = {}
        real_open, real_close = os.open, os.close

        def noting(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            folder = kwargs.get("dir_fd")
            name = os.fspath(path)
            self.calls.append((name, flags, folder))
            full = name if folder is None else os.path.join(self._places[folder], name)
            self.paths.append(full)
            descriptor = real_open(path, flags, *args, **kwargs)
            self._places[descriptor] = full
            return descriptor

        def forgetting(descriptor: int) -> None:
            self._places.pop(descriptor, None)
            real_close(descriptor)

        monkeypatch.setattr(hostcheck.os, "open", noting)
        monkeypatch.setattr(hostcheck.os, "close", forgetting)

    def clear(self) -> None:
        self.calls.clear()
        self.paths.clear()


def test_the_states_are_the_thirteen_words_and_the_sections_three() -> None:
    assert hostcheck.STATES == (
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
        "section": None,
    }
    here.parent.mkdir(parents=True)
    assert look("claude-desktop", platform)["state"] == "no_file"
    here.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")
    assert look("claude-desktop", platform)["state"] == "present"
    # The entry is the one of this app: under another name in the vault it differs.
    assert look("claude-desktop", platform, client="another-name")["state"] == "differs"
    # The place is shown with "~"; the look itself holds the file in full.
    here.write_text('{"theme": "dark"}', encoding="utf-8")
    answer = look("claude-desktop", platform)
    assert (answer["state"], answer["section"]) == ("other", "none")
    assert answer["path"] == shown
    assert hostcheck.look(
        "claude-desktop", client="claude-desktop", store=STORE, platform=platform
    ).path == (here)


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
    without = ("claude-code", "openclaw", "generic", "chatgpt-web", "claude-web", "grok-bot")
    for host in without:
        for platform in hostconfig.PLATFORMS:
            assert look(host, platform) == UNSUPPORTED
            found = hostcheck.look(host, client="my-app", store=STORE, platform=platform)
            assert (found.state, found.path, found.raw, found.status) == (
                "unsupported",
                None,
                None,
                None,
            )
    assert sorted(home.rglob("*")) == before
    # The programs with a file: Claude Desktop, the two that share one, and Grok Build.
    with_a_file = [
        entry.id for entry in hostconfig.KNOWN_HOSTS if hostconfig.settings_file(entry.id)
    ]
    assert with_a_file == ["claude-desktop", "chatgpt-desktop", "codex", "grok-build"]
    assert not set(with_a_file) & set(without)
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
    opened = Opened(monkeypatch)
    # What every look opens: the home, and the two folders below it, each without
    # following a link. Nothing of that is the file.
    folders = [str(home), str(home / ".config"), str(place.parent)]
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
    assert opened.paths == folders * 5, "something that is not a small regular file was opened"
    for name, flags, folder in opened.calls:
        assert flags & os.O_DIRECTORY, name
        # Below the home a link in a folder's place is not gone through.
        assert bool(flags & os.O_NOFOLLOW) is (folder is not None), name
    # The counterpart: a small regular file is opened, once, for reading only, by its name
    # in the folder that was opened.
    opened.clear()
    place.write_text("{}", encoding="utf-8")
    assert look("claude-desktop", "linux")["state"] == "empty"
    assert opened.paths == [*folders, str(place)]
    name, flags, folder = opened.calls[-1]
    assert (name, folder is not None) == (place.name, True)
    assert flags & os.O_NOFOLLOW
    assert flags & os.O_NONBLOCK
    assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)


def test_a_file_exchanged_after_the_first_look_is_not_read_either(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    place.write_text("{}", encoding="utf-8")
    small = os.lstat(place)
    real_lstat = os.lstat

    def as_it_was(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        # The file is looked at by its name, through its folder.
        return small if os.fspath(path) == place.name else real_lstat(path, *args, **kwargs)

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


def test_a_file_the_system_refuses_is_a_word_and_not_an_error(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    place.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")

    real_open = os.open

    def refuse(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.fspath(path) == place.name:
            raise PermissionError(13, "Permission denied", str(place))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(hostcheck.os, "open", refuse)
    answer = look("claude-desktop", "linux")
    found = seen("claude-desktop")
    monkeypatch.undo()
    # A refusal by the system has a word of its own, and nothing of the system's message is in it.
    assert answer["state"] == "no_access"
    assert "Permission" not in json.dumps(answer)
    assert (found.state, found.raw, found.status) == ("no_access", None, None)


# -- the look the writer builds on: four words more, and what was read ----------------------

needs_toml = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="no reader for TOML in this Python"
)


def toml_entry(client: str, more: str = "") -> str:
    command = hostconfig.host_command(client, STORE)
    arguments = ", ".join(json.dumps(part) for part in command[1:])
    return f"[mcp_servers.{SLUG}]\ncommand = {json.dumps(command[0])}\nargs = [{arguments}]\n{more}"


def test_a_look_hands_back_what_it_read_and_keeps_it_out_of_what_is_printed(home: Path) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    nothing = seen("claude-desktop")
    assert (nothing.state, nothing.path, nothing.language) == ("no_folder", place, "json")
    assert (nothing.raw, nothing.status, nothing.section) == (None, None, None)
    assert nothing.command == tuple(hostconfig.host_command("claude-desktop", STORE))
    place.parent.mkdir(parents=True)
    missing = seen("claude-desktop")
    assert (missing.state, missing.raw, missing.status) == ("no_file", None, None)
    content = json.dumps({"apiKey": SECRET, "mcpServers": {"another-tool": {"command": "tool"}}})
    place.write_text(content, encoding="utf-8")
    found = seen("claude-desktop")
    assert (found.state, found.section) == ("other", "filled")
    assert found.raw == content.encode()
    status = found.status
    assert status is not None
    there = os.lstat(place)
    assert (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns) == (
        there.st_dev,
        there.st_ino,
        there.st_size,
        there.st_mtime_ns,
    )
    assert (found.switched_off, found.other_name, found.real_folder) == (False, None, None)
    # What was read is for the writer. It is in no text Python makes of a look, and the
    # route of today's page still answers its five fields and nothing from the file.
    assert SECRET not in repr(found)
    assert SECRET not in str(found)
    assert "raw" not in repr(found)
    assert SECRET not in json.dumps(look("claude-desktop", "linux"))
    # A file that cannot be read as JSON was read all the same: the bytes are there.
    place.write_bytes(b'{"apiKey": ')
    broken = seen("claude-desktop")
    assert (broken.state, broken.raw) == ("unreadable", b'{"apiKey": ')
    # What was not opened was not read.
    place.unlink()
    place.mkdir()
    assert (seen("claude-desktop").state, seen("claude-desktop").raw) == ("not_read", None)


def test_the_section_is_told_for_a_json_file_without_the_entry(home: Path) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    cases: list[tuple[bytes, str, str | None]] = [
        (b"", "empty", None),  # no object at all: the whole text is what goes in
        (b" \n\t", "empty", None),
        (b"{}", "empty", "none"),
        (b'{"mcpServers": {}}', "empty", "empty"),
        (b'{"theme": "dark"}', "other", "none"),
        (b'{"theme": "dark", "mcpServers": {}}', "other", "empty"),
        (b'{"mcpServers": {"another-tool": {}}}', "other", "filled"),
        (json.dumps({"mcpServers": {SLUG: entry()}}).encode(), "present", None),
        (json.dumps({"mcpServers": {SLUG: {"command": "x"}}}).encode(), "differs", None),
        (b"[]", "unreadable", None),
    ]
    for content, state, section in cases:
        place.write_bytes(content)
        found = seen("claude-desktop")
        assert (found.state, found.section) == (state, section), content
        # The section is told for a file without the entry, and for no other.
        assert (section is None) or state in ("other", "empty")


def test_an_entry_that_holds_more_than_the_command_is_said_to(home: Path) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    ours = entry()

    def holds(value: Any) -> hostcheck.Look:
        place.write_text(json.dumps({"mcpServers": {SLUG: value}}), encoding="utf-8")
        return seen("claude-desktop")

    exact = holds(ours)
    assert (exact.state, exact.switched_off) == ("present", False)
    assert exact.state == look("claude-desktop", "linux")["state"]
    # The two members in the other order are the same entry.
    assert holds({"args": ours["args"], "command": ours["command"]}).state == "present"
    for more, off in (
        ({"enabled": True}, False),
        ({"enabled": False}, True),
        ({"enabled": 0}, False),  # only the value false switches an entry off
        ({"enabled": None}, False),
        ({"env": {"TOKEN": SECRET}}, False),
        ({"cwd": "/somewhere"}, False),
        ({"tools": {"search": {"approval_mode": "never"}}}, False),
        ({"enabled": False, "env": {}, "cwd": "/x", "disabled_tools": ["read"]}, True),
    ):
        found = holds({**ours, **more})
        assert (found.state, found.switched_off) == ("present_more", off), more
        assert SECRET not in repr(found)
        assert look("claude-desktop", "linux")["state"] == "present_more"
    # An entry that points elsewhere says whether it is switched off, too.
    elsewhere = holds({**ours, "command": "/opt/other/python", "enabled": False})
    assert (elsewhere.state, elsewhere.switched_off) == ("differs", True)
    assert holds({**ours, "command": "/opt/other/python"}).switched_off is False
    # What is no object is switched neither on nor off.
    for other in ("text", None, [ours], 5):
        found = holds(other)
        assert (found.state, found.switched_off) == ("differs", False)


@needs_toml
def test_an_entry_with_a_table_below_it_holds_more(home: Path) -> None:
    place = home / ".codex" / "config.toml"
    place.parent.mkdir()
    for more, off in (
        ("", None),
        ("enabled = true\n", False),
        ("enabled = false\n", True),
        ('cwd = "/somewhere"\n', False),
        (f'env = {{ TOKEN = "{SECRET}" }}\n', False),
        (f'\n[mcp_servers.{SLUG}.env]\nTOKEN = "{SECRET}"\n', False),
        (f'enabled = false\n\n[mcp_servers.{SLUG}.tools.search]\napproval_mode = "never"\n', True),
    ):
        place.write_text(f'model = "o9"\n\n{toml_entry("codex", more)}', encoding="utf-8")
        found = seen("codex")
        if off is None:
            assert (found.state, found.switched_off) == ("present", False)
        else:
            assert (found.state, found.switched_off) == ("present_more", off), more
        assert found.section is None  # a section is told for JSON only
        assert SECRET not in repr(found)
        wanted = "present" if off is None else "present_more"
        assert look("codex", "linux", client="codex")["state"] == wanted


@needs_toml
def test_an_entry_made_for_the_other_program_of_the_same_file_is_named(home: Path) -> None:
    assert hostconfig.same_file("chatgpt-desktop") == ("codex",)
    place = home / ".codex" / "config.toml"
    place.parent.mkdir()
    # The file holds the entry an owner made for Codex.
    place.write_text(toml_entry("codex"), encoding="utf-8")
    found = seen("chatgpt-desktop")
    assert (found.state, found.other_name) == ("present_as", "codex")
    assert seen("codex").state == "present"
    assert seen("codex").other_name is None
    # And the other way round.
    place.write_text(toml_entry("chatgpt-desktop"), encoding="utf-8")
    assert (seen("codex").state, seen("codex").other_name) == ("present_as", "chatgpt-desktop")
    assert seen("chatgpt-desktop").state == "present"
    # With members the program wrote into it, it is still that program's entry.
    place.write_text(toml_entry("codex", "enabled = false\n"), encoding="utf-8")
    found = seen("chatgpt-desktop")
    assert (found.state, found.other_name, found.switched_off) == ("present_as", "codex", True)
    # Under any other name, or for another vault, it points elsewhere.
    place.write_text(toml_entry("claude-desktop"), encoding="utf-8")
    assert (seen("chatgpt-desktop").state, seen("chatgpt-desktop").other_name) == ("differs", None)
    place.write_text(toml_entry("codex").replace(STORE, "/srv/another vault"), encoding="utf-8")
    assert seen("chatgpt-desktop").state == "differs"
    # Asked for the app that is itself named `codex`, there is no other name to find.
    place.write_text(toml_entry("codex"), encoding="utf-8")
    assert seen("chatgpt-desktop", client="codex").state == "present"
    # Asked for the tile's own name, the entry is this vault's under the other name.
    assert look("chatgpt-desktop", "linux", client="chatgpt-desktop")["state"] == "present_as"
    # The file of Grok Build is nobody else's, and neither is Claude Desktop's.
    assert hostconfig.same_file("grok-build") == ()
    assert hostconfig.same_file("claude-desktop") == ()
    grok = home / ".grok" / "config.toml"
    grok.parent.mkdir()
    grok.write_text(toml_entry("codex"), encoding="utf-8")
    assert seen("grok-build").state == "differs"


@needs_toml
def test_the_file_of_grok_build_is_looked_at(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".grok" / "config.toml"
    for platform in hostconfig.PLATFORMS:
        assert seen("grok-build", platform).state == "no_folder"
        assert look("grok-build", platform, client="grok-build") == {
            "state": "no_folder",
            "path": "~/.grok/config.toml",
            "folder": "~/.grok",
            "section": None,
        }
    place.parent.mkdir()
    assert seen("grok-build").state == "no_file"
    place.write_text('theme = "dark"\n', encoding="utf-8")
    assert seen("grok-build").state == "other"
    place.write_text(toml_entry("grok-build"), encoding="utf-8")
    assert (seen("grok-build").state, seen("grok-build").path) == ("present", place)
    # GROK_HOME moves the file when it holds an absolute path, on either system.
    moved = tmp_path / "grok of mine"
    monkeypatch.setenv("GROK_HOME", str(moved))
    for platform in hostconfig.PLATFORMS:
        found = seen("grok-build", platform)
        assert (found.state, found.path) == ("no_folder", moved / "config.toml")
    moved.mkdir()
    assert seen("grok-build").state == "no_file"
    (moved / "config.toml").write_text(toml_entry("grok-build"), encoding="utf-8")
    assert seen("grok-build").state == "present"
    for ignored in ("", "relative/place", "~/x"):
        monkeypatch.setenv("GROK_HOME", ignored)
        assert seen("grok-build").path == place


def test_a_folder_that_is_a_link_is_not_gone_through(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    elsewhere = tmp_path / "kept in a repository"
    (elsewhere / "Claude").mkdir(parents=True)
    right = json.dumps({"apiKey": SECRET, "mcpServers": {SLUG: entry()}})
    (elsewhere / "Claude" / place.name).write_text(right, encoding="utf-8")
    opened = Opened(monkeypatch)

    def linked(real: Path) -> None:
        opened.clear()
        found = seen("claude-desktop")
        assert (found.state, found.raw, found.status) == ("linked", None, None)
        assert (found.path, found.real_folder) == (place, real)
        assert SECRET not in repr(found)
        # Nothing behind the link was opened: not the folder, not the file.
        for path in opened.paths:
            assert not path.startswith(str(elsewhere)), path
            assert path in (str(home), str(home / ".config")), path
        answer = look("claude-desktop", "linux")
        assert (answer["state"], answer["section"]) == ("linked", None)
        assert SECRET not in json.dumps(answer)

    # The folder of the file is a link to a folder that holds the very entry.
    (home / ".config").mkdir()
    (home / ".config" / "Claude").symlink_to(elsewhere / "Claude")
    linked(elsewhere / "Claude")
    # One level higher: ~/.config is the link.
    (home / ".config" / "Claude").unlink()
    (home / ".config").rmdir()
    (home / ".config").symlink_to(elsewhere)
    linked(elsewhere / "Claude")
    # A link that leads nowhere is a link all the same, not a missing folder.
    (home / ".config").unlink()
    (home / ".config").symlink_to(tmp_path / "nothing there")
    linked(tmp_path / "nothing there" / "Claude")
    # The base is taken as the system resolves it: a home that is reached through a link
    # is the home. (On a Mac the folder for temporary files is reached that way.)
    (home / ".config").unlink()
    (home / ".config" / "Claude").mkdir(parents=True)
    place.write_text(right, encoding="utf-8")
    through = tmp_path / "the way to the home"
    through.symlink_to(home)
    monkeypatch.setenv("HOME", str(through))
    found = seen("claude-desktop")
    assert (found.state, found.path) == ("present", through / ".config" / "Claude" / place.name)
    # And so is the folder a variable names.
    monkeypatch.setenv("HOME", str(home))
    variable = tmp_path / "settings, linked"
    variable.symlink_to(elsewhere)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(variable))
    assert seen("claude-desktop").state == "present"
    # Below it, a link is not gone through either.
    (elsewhere / "Claude").rename(elsewhere / "Claude, really")
    (elsewhere / "Claude").symlink_to("Claude, really")
    found = seen("claude-desktop")
    assert (found.state, found.real_folder) == ("linked", elsewhere / "Claude, really")


def test_a_folder_exchanged_while_it_is_opened_is_not_gone_through(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / place.name).write_text(
        json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8"
    )
    real_open = os.open
    exchanged: list[str] = []

    def exchange(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        # Right after the folder was looked at and before it is opened, a link takes its place.
        if os.fspath(path) == "Claude" and not exchanged:
            exchanged.append("link")
            place.parent.rmdir()
            place.parent.symlink_to(elsewhere)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(hostcheck.os, "open", exchange)
    found = seen("claude-desktop")
    assert exchanged == ["link"]
    assert (found.state, found.raw) == ("linked", None)
    monkeypatch.setattr(hostcheck.os, "open", real_open)
    # Another folder in its place is not the folder that was looked at.
    place.parent.unlink()
    place.parent.mkdir()
    other = home / ".config" / "another"
    other.mkdir()
    (other / place.name).write_text("{}", encoding="utf-8")

    def swap(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.fspath(path) == "Claude" and exchanged == ["link"]:
            exchanged.append("folder")
            place.parent.rename(home / ".config" / "Claude, before")
            other.rename(place.parent)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(hostcheck.os, "open", swap)
    found = seen("claude-desktop")
    assert exchanged == ["link", "folder"]
    assert (found.state, found.raw) == ("no_folder", None)


def test_what_the_system_refuses_is_said_apart_from_what_cannot_be_read(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    place.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")
    real_open, real_lstat = os.open, os.lstat

    def both() -> tuple[str, str]:
        found = seen("claude-desktop")
        assert (found.raw, found.status) == (None, None)
        answer = look("claude-desktop", "linux")
        assert "Permission" not in json.dumps(answer)
        return found.state, answer["state"]

    def refusing(name: str, error: type[OSError], number: int) -> Any:
        def opening(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if os.fspath(path) == name:
                raise error(number, "refused", name)
            return real_open(path, flags, *args, **kwargs)

        return opening

    # The system refuses to open the file, to open its folder, or one above it.
    for name in (place.name, "Claude", ".config"):
        for number in (13, 1):  # EACCES, EPERM
            monkeypatch.setattr(hostcheck.os, "open", refusing(name, PermissionError, number))
            assert both() == ("no_access", "no_access"), (name, number)
    monkeypatch.setattr(hostcheck.os, "open", real_open)

    # It refuses to look at the file, or at a folder on the way.
    def looking(name: str) -> Any:
        def at(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if os.fspath(path) == name:
                raise PermissionError(13, "refused", name)
            return real_lstat(path, *args, **kwargs)

        return at

    for name in (place.name, "Claude", ".config"):
        monkeypatch.setattr(hostcheck.os, "lstat", looking(name))
        assert both() == ("no_access", "no_access"), name
    monkeypatch.setattr(hostcheck.os, "lstat", real_lstat)
    # Another error while the file is opened is not a refusal: the file could not be read.
    monkeypatch.setattr(hostcheck.os, "open", refusing(place.name, OSError, 5))
    assert both() == ("unreadable", "unreadable")
    monkeypatch.setattr(hostcheck.os, "open", real_open)
    if os.geteuid() != 0:  # root is refused nothing
        place.chmod(0o000)
        assert both() == ("no_access", "unreadable")
        place.chmod(0o600)
        place.parent.chmod(0o000)
        assert both() == ("no_access", "unreadable")
        place.parent.chmod(0o700)
    assert seen("claude-desktop").state == "present"


def test_a_file_that_changes_its_size_while_it_is_read_is_not_read(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    place = home / ".config" / "Claude" / "claude_desktop_config.json"
    place.parent.mkdir(parents=True)
    place.write_text(json.dumps({"mcpServers": {SLUG: entry()}}), encoding="utf-8")
    real_read = os.read
    cut: list[int] = []

    def reading(descriptor: int, count: int) -> bytes:
        data = real_read(descriptor, count)
        if not data and not cut:  # everything was read: now somebody empties the file
            cut.append(1)
            place.write_bytes(b"")
        return data

    monkeypatch.setattr(hostcheck.os, "read", reading)
    found = seen("claude-desktop")
    assert cut == [1]
    assert (found.state, found.raw, found.status) == ("not_read", None, None)
    # Written in place with the same number of bytes while it is read: the status that is
    # handed back is the one from after the read, so the time of that write is in it.
    content = json.dumps({"mcpServers": {SLUG: entry()}})
    place.write_text(content, encoding="utf-8")
    long_ago = os.lstat(place).st_mtime_ns - 60 * 10**9
    os.utime(place, ns=(long_ago, long_ago))
    again: list[int] = []

    def saving(descriptor: int, count: int) -> bytes:
        data = real_read(descriptor, count)
        if not data and not again:
            again.append(1)
            place.write_text(content, encoding="utf-8")
        return data

    monkeypatch.setattr(hostcheck.os, "read", saving)
    found = seen("claude-desktop")
    assert again == [1]
    assert found.state == "present"
    status = found.status
    assert status is not None
    assert status.st_mtime_ns == os.lstat(place).st_mtime_ns
    assert status.st_mtime_ns > long_ago


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
        "dataclasses",
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
    assert calls == {"open", "fstat", "read", "close", "lstat"}
    flags = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("O_")
    }
    assert flags == {"O_RDONLY"}  # the others are asked for by name, where the system has them
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "O_DIRECTORY"):
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


def test_the_walk_throughs_give_the_app_a_home_of_their_own() -> None:
    # In a browser walk-through the app looks at a settings file, and writes one, where its home
    # is. The program that starts the app for a walk-through names a folder the run owns before
    # the app runs, so the settings of a program on the computer that runs the tests are never
    # read and never written.
    source = (Path(__file__).parent / "browser" / "launch_app.py").read_text(encoding="utf-8")
    started = source.index("return run_wdva(arguments[1:])")
    assert 0 < source.index('os.environ["HOME"] = home') < started
    assert 0 < source.index('os.environ["XDG_CONFIG_HOME"] = ') < started
    assert 'given = os.environ.get("GATE_HOME", "")' in source
    # A home the launcher makes itself lies under the checkout, beside the walk-throughs, and
    # not under the system's folder for temporary files: the app writes no entry that names a
    # vault in such a folder.
    assert 'made = tempfile.mkdtemp(prefix="home-", dir=os.path.join(HERE, ".run"))' in source
    assert "home = given or made" in source
    assert "tempfile.mkdtemp(" in source
    assert source.count("mkdtemp(") == 1
    # The two variables that move a settings file or stop a write never reach the app from
    # the computer that runs the walk-through.
    for name in ("GROK_HOME", "CODEX_HOME"):
        assert 0 < source.index(f'os.environ.pop("{name}", None)') < started, name
    # A walk-through may give the app a value of its own, after the computer's was removed.
    given = source.index('moved = os.environ.get("GATE_CODEX_HOME", "")')
    assert source.index('os.environ.pop("CODEX_HOME", None)') < given < started
    ignored = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "/tests/browser/.run/" in ignored.splitlines()
