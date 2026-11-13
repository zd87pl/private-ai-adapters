"""The configuration text that connects an agent application: it parses and holds one command."""

from __future__ import annotations

import datetime
import importlib.metadata
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from wdva.errors import MissingDependencyError, WDVAError
from wdva.gate import brand, hostconfig
from wdva.gate.hostconfig import (
    INSTALL_LINE,
    KNOWN_HOSTS,
    check_client_name,
    host_command,
    known_host,
    missing_sdk_error,
    render,
    render_text,
    sdk_status,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

# The product's name comes from one place; the tests take it from there too.
PRODUCT, SLUG = brand.PRODUCT, brand.SLUG

LOCAL = ["claude-desktop", "claude-code", "codex", "openclaw", "generic"]
WEB_ONLY = ["chatgpt-web", "claude-web", "grok-web", "grok-bot"]

# A store path with everything a configuration format has to escape.
AWKWARD = "/srv/my vault/café \"q\" 'a' \\b $HOME `id` #x {y} 日本\ttab"
PLAIN = "/srv/my vault/café"

GOOD_NAMES = ["a", "claude-desktop", "lm-studio", "x9-", "a" * 32, "a-1-b"]
BAD_NAMES = [
    "",
    "A",
    "Claude",
    "1abc",
    "-abc",
    "a_b",
    "a b",
    "a.b",
    "a" * 33,
    "äb",
    "abc\n",
    "abc\nx",
    "a;rm -rf",
    'a"b',
    "$(id)",
]


def expected_command(client: str, store: str) -> list[str]:
    return [
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
        store,
    ]


def json5_to_json(text: str) -> str:
    """The OpenClaw block as JSON: quote its bare keys and put the outer braces around it."""
    quoted = re.sub(r"(?m)^(\s*)(mcp|servers|command|args):", r'\1"\2":', text)
    return "{" + quoted + "}"


def commands_in(config: hostconfig.HostConfig) -> list[list[str]]:
    """The command each block holds, read back the way its format is read."""
    found = []
    for block in config.blocks:
        if block.language == "json":
            data = json.loads(block.text)
            assert list(data) == ["mcpServers"]
            assert list(data["mcpServers"]) == [SLUG]
            entry = data["mcpServers"][SLUG]
            assert sorted(entry) == ["args", "command"]
            found.append([entry["command"], *entry["args"]])
        elif block.language == "json5":
            data = json.loads(json5_to_json(block.text))
            assert list(data) == ["mcp"]
            assert list(data["mcp"]) == ["servers"]
            assert list(data["mcp"]["servers"]) == [SLUG]
            entry = data["mcp"]["servers"][SLUG]
            assert sorted(entry) == ["args", "command"]
            found.append([entry["command"], *entry["args"]])
        elif block.language == "toml":
            if sys.version_info < (3, 11):
                continue  # no TOML reader in the standard library before 3.11
            import tomllib

            data = tomllib.loads(block.text)
            assert list(data) == ["mcp_servers"]
            assert list(data["mcp_servers"]) == [SLUG]
            entry = data["mcp_servers"][SLUG]
            assert sorted(entry) == ["args", "command"]
            found.append([entry["command"], *entry["args"]])
        else:
            assert block.language == "shell"
            assert "\n" not in block.text or "'" in block.text
            words = shlex.split(block.text)
            if "--" in words:
                separator = words.index("--")
                assert words[:separator] in (
                    [
                        "claude",
                        "mcp",
                        "add",
                        "--transport",
                        "stdio",
                        "--scope",
                        "user",
                        SLUG,
                    ],
                    ["codex", "mcp", "add", SLUG],
                )
                words = words[separator + 1 :]
            found.append(words)
    return found


# -- the applications -----------------------------------------------------------


def test_known_hosts_are_data_with_source_and_date() -> None:
    assert [entry.id for entry in KNOWN_HOSTS] == LOCAL + WEB_ONLY
    for entry in KNOWN_HOSTS:
        assert entry.label
        assert entry.source.startswith("https://")
        assert datetime.date.fromisoformat(entry.read) == datetime.date(2026, 11, 12)
        assert entry.local_program is (entry.id in LOCAL)
        assert entry.hosted is True  # the careful suggestion: the owner says otherwise
        if entry.name:
            assert check_client_name(entry.name) == entry.name
        assert known_host(entry.id) is entry
        assert entry.as_dict() == {
            "id": entry.id,
            "label": entry.label,
            "name": entry.name,
            "destination": entry.destination,
            "hosted": True,
            "local_program": entry.local_program,
        }
    by_id = {entry.id: entry for entry in KNOWN_HOSTS}
    assert by_id["claude-desktop"].as_dict() == {
        "id": "claude-desktop",
        "label": "Claude Desktop",
        "name": "claude-desktop",
        "destination": "Anthropic",
        "hosted": True,
        "local_program": True,
    }
    assert (by_id["codex"].label, by_id["codex"].destination) == (
        "ChatGPT desktop app or Codex",
        "OpenAI",
    )
    assert (by_id["generic"].name, by_id["generic"].destination) == ("", "")
    assert [by_id[name].name for name in WEB_ONLY] == ["", "", "", ""]
    assert [by_id[name].destination for name in WEB_ONLY] == ["OpenAI", "Anthropic", "xAI", "xAI"]


def test_an_unknown_application_is_refused() -> None:
    for host in ("cursor", "", "Claude-Desktop", "claude-desktop ", "x" * 500):
        with pytest.raises(ValueError, match="not an application the gate knows") as raised:
            render(host, client="my-app", store=PLAIN)
        assert len(str(raised.value)) < 300
        with pytest.raises(ValueError, match="choose from claude-desktop"):
            render_text(host, client="my-app", store=PLAIN)


# -- the command ----------------------------------------------------------------


def test_the_command_is_the_fixed_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert host_command("claude-desktop", PLAIN) == expected_command("claude-desktop", PLAIN)
    assert host_command("a", Path(AWKWARD)) == expected_command("a", AWKWARD)

    # A relative store is made absolute: the application starts the command somewhere else.
    monkeypatch.chdir(tmp_path)
    assert host_command("a", "my store")[-1] == str(tmp_path / "my store")
    assert host_command("a", Path("stores") / ".." / "s")[-1] == str(tmp_path / "s")

    # No store named: the one the gate would use itself.
    monkeypatch.setenv("WDVA_GATE_STORE", str(tmp_path / "from-environment"))
    assert host_command("a", None)[-1] == str(tmp_path / "from-environment")


def test_the_interpreter_is_named_as_it_was_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inside a virtual environment sys.executable is a link to the base interpreter.
    # Following it would leave the environment that holds the package.
    link = tmp_path / "venv with space" / "bin" / "python"
    link.parent.mkdir(parents=True)
    os.symlink(sys.executable, link)
    monkeypatch.setattr(sys, "executable", str(link))

    command = host_command("a", PLAIN)

    assert command[0] == str(link)
    assert command[0] != os.path.realpath(link)
    for host in LOCAL:
        for parsed in commands_in(render(host, client="a", store=PLAIN)):
            assert parsed[0] == str(link)


def test_no_command_without_an_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", "")
    with pytest.raises(WDVAError, match="interpreter"):
        host_command("a", PLAIN)


def test_a_path_that_is_not_text_is_refused() -> None:
    not_text = os.fsdecode(b"/srv/vault-\xff")
    with pytest.raises(ValueError, match="configuration file"):
        host_command("a", not_text)
    with pytest.raises(ValueError, match="configuration file"):
        render("codex", client="a", store=not_text)


# -- the snippets -----------------------------------------------------------------


@pytest.mark.parametrize("store", [PLAIN, AWKWARD], ids=["space-and-accent", "awkward"])
@pytest.mark.parametrize("host", LOCAL)
def test_every_snippet_parses_to_the_command(host: str, store: str) -> None:
    config = render(host, client="my-app", store=store)
    command = expected_command("my-app", store)

    assert config.command == tuple(command) == tuple(host_command("my-app", store))
    assert config.local_program is True
    assert config.explanation is None
    assert config.blocks
    parsed = commands_in(config)
    readable = [block for block in config.blocks if block.language != "toml"]
    assert len(parsed) >= len(readable)
    for found in parsed:
        assert found == command
        assert found[-1] == store  # the space and the accent came through untouched


def test_each_application_gets_its_documented_shapes() -> None:
    shapes = {
        host: [
            (block.language, block.where) for block in render(host, client="a", store=PLAIN).blocks
        ]
        for host in LOCAL
    }
    assert shapes == {
        "claude-desktop": [
            (
                "json",
                (
                    "~/Library/Application Support/Claude/claude_desktop_config.json",
                    "~/.config/Claude/claude_desktop_config.json",
                    "%APPDATA%\\Claude\\claude_desktop_config.json",
                ),
            )
        ],
        "claude-code": [("shell", ()), ("json", (".mcp.json",))],
        "codex": [("shell", ()), ("toml", ("~/.codex/config.toml",))],
        "openclaw": [("json5", ("~/.openclaw/openclaw.json",))],
        "generic": [("json", ()), ("shell", ())],
    }
    claude_code = render("claude-code", client="a", store=PLAIN).blocks[0].text
    assert claude_code.startswith(f"claude mcp add --transport stdio --scope user {SLUG} -- ")
    codex = render("codex", client="a", store=PLAIN)
    assert codex.blocks[0].text.startswith(f"codex mcp add {SLUG} -- ")
    assert codex.blocks[1].text.startswith(f"[mcp_servers.{SLUG}]\ncommand = ")
    assert any(
        "~/.grok/config.toml" in note and "ChatGPT desktop app" in note for note in codex.notes
    )
    desktop = render("claude-desktop", client="a", store=PLAIN)
    assert "Quit and restart Claude Desktop afterwards." in desktop.notes
    assert (
        render("openclaw", client="a", store=PLAIN)
        .blocks[0]
        .text.startswith("mcp: {\n  servers: {")
    )


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is new in Python 3.11")
@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain",
        'quote " and backslash \\',
        "\\\\server\\share",
        "tab\tnewline\nreturn\rbell\x07delete\x7fescape\x1b",
        "café 日本 \U0001f512",
        "# not a comment",
        '"""',
        "'''",
        "\\u0041 stays six characters",
    ],
)
def test_toml_strings_come_back_unchanged(value: str) -> None:
    import tomllib

    text = f"value = {hostconfig._toml_string(value)}\n"
    assert "\n" not in text[:-1]
    assert tomllib.loads(text) == {"value": value}


def test_the_text_for_a_terminal_holds_every_block_and_its_source() -> None:
    for host in LOCAL:
        config = render(host, client="my-app", store=AWKWARD)
        text = render_text(host, client="my-app", store=AWKWARD)
        assert text.splitlines()[0] == config.label
        assert not text.endswith("\n")
        for block in config.blocks:
            assert f"{block.title}:\n\n{block.text}" in text
            for place in block.where:
                assert f"  {place}" in text
        assert config.source in text
        assert config.read == "2026-11-12"
        assert "2026-11-12" in text
        assert "has not been tried with the application itself" in " ".join(text.split())
        assert config.source in config.notes[-1]


def test_the_text_says_when_the_server_cannot_run_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    ready = {"sdk": True, "version": "2.3.0", "python": True, "message": None}
    monkeypatch.setattr(hostconfig, "sdk_status", lambda: ready)
    complete = render_text("codex", client="my-app", store=PLAIN)
    assert "Before an application can use this" not in complete

    for message in (hostconfig.PYTHON_MESSAGE, str(missing_sdk_error())):
        not_ready = {"sdk": False, "version": None, "python": False, "message": message}
        monkeypatch.setattr(hostconfig, "sdk_status", lambda value=not_ready: value)
        text = render_text("codex", client="my-app", store=PLAIN)
        assert text == f"{complete}\n\nBefore an application can use this: {message}"
        # A product that takes no configuration needs nothing installed.
        assert "Before an application" not in render_text("grok-web", client="a", store=PLAIN)


def test_the_values_for_an_interface_have_the_agreed_shape() -> None:
    for host in LOCAL + WEB_ONLY:
        config = render(host, client="my-app", store=AWKWARD)
        data = config.as_dict()
        assert json.loads(json.dumps(data)) == data
        assert list(data) == ["host", "label", "local_program", "blocks", "notes", "explanation"]
        assert data["host"] == host
        assert data["label"] == known_host(host).label
        assert data["local_program"] is (host in LOCAL)
        assert all(isinstance(note, str) and note for note in data["notes"])
        for block in data["blocks"]:
            assert list(block) == ["title", "language", "text", "where"]
            assert block["language"] in ("json", "json5", "toml", "shell")
            assert isinstance(block["where"], list)
        assert (data["explanation"] is None) is (host in LOCAL)
        assert bool(data["blocks"]) is (host in LOCAL)


# -- what must not be in them -----------------------------------------------------


def test_no_secret_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "WDVA_PASSPHRASE": "correct-horse-battery-staple-9431",
        "WDVA_KEY": "6b" * 32,
        "WDVA_KEY_FILE": "/home/me/.secret-key-file-7712",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    for host in LOCAL + WEB_ONLY:
        config = render(host, client="my-app", store=PLAIN)
        text = render_text(host, client="my-app", store=PLAIN) + json.dumps(config.as_dict())
        for name, value in secrets.items():
            assert value not in text
            assert name not in text
        lowered = text.lower()
        for word in ("passphrase", "password", "token", "api key", "api_key", '"env"', "env ="):
            assert word not in lowered
        # The command names a program, a client and a place. Nothing in it is a credential.
        for found in commands_in(config):
            assert found == expected_command("my-app", PLAIN)


def test_nothing_is_claimed_about_an_application() -> None:
    for host in LOCAL + WEB_ONLY:
        config = render(host, client="my-app", store=PLAIN)
        # The sentences, without the command, whose path is whatever this machine has.
        sentences = [config.label, config.explanation or "", *config.notes]
        sentences += [block.title for block in config.blocks]
        text = " ".join(sentences).lower()
        for claim in (
            "supported",
            "tested",
            "verified",
            "works with",
            "compatible",
            "secure",
            "safe",
            "never leave",
            "private",
        ):
            assert claim not in text, (host, claim)


@pytest.mark.parametrize("host", WEB_ONLY)
def test_web_only_products_get_an_explanation_and_no_configuration(host: str) -> None:
    config = render(host, client="my-app", store=AWKWARD)
    entry = known_host(host)

    assert config.blocks == ()
    assert config.command == ()
    assert config.local_program is False
    explanation = config.explanation
    assert explanation is not None
    assert "\n" not in explanation
    assert explanation.startswith(
        f"{entry.label} calls only servers that {entry.destination}'s cloud"
    )
    assert "on this computer instead" in explanation
    assert "cannot reach this vault" in explanation
    assert f"goes to {entry.destination}" in explanation
    assert "neither limits nor records" in explanation

    text = render_text(host, client="my-app", store=AWKWARD)
    for absent in ("mcpServers", "mcp_servers", sys.executable, AWKWARD, "my-app", "--client"):
        assert absent not in text
    # No command is offered that does not exist.
    assert "wdva gate" not in text
    assert entry.source in text
    assert "2026-11-12" in text


# -- names -----------------------------------------------------------------------


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_good_names_are_accepted(name: str) -> None:
    assert check_client_name(name) == name
    assert host_command(name, PLAIN)[9] == name
    for host in LOCAL:
        for found in commands_in(render(host, client=name, store=PLAIN)):
            assert found == expected_command(name, PLAIN)


@pytest.mark.parametrize("name", [*BAD_NAMES, None, 5, ["a"], b"abc"])
def test_bad_names_are_refused(name: Any) -> None:
    with pytest.raises(ValueError, match="cannot be used as a name") as raised:
        check_client_name(name)
    assert len(str(raised.value)) < 200
    assert "\n" not in str(raised.value)
    with pytest.raises(ValueError, match="cannot be used as a name"):
        host_command(name, PLAIN)
    for host in LOCAL + WEB_ONLY:
        with pytest.raises(ValueError, match="cannot be used as a name"):
            render(host, client=name, store=PLAIN)
        with pytest.raises(ValueError, match="cannot be used as a name"):
            render_text(host, client=name, store=PLAIN)


# -- the SDK ---------------------------------------------------------------------


def test_the_missing_sdk_error_carries_the_one_install_line() -> None:
    assert INSTALL_LINE == (
        'pip install "wdva[gate] @ git+https://github.com/zd87pl/private-ai-adapters"'
    )
    error = missing_sdk_error()
    assert isinstance(error, MissingDependencyError)
    assert isinstance(error, ImportError)
    assert str(error) == (
        "the MCP server needs the MCP SDK. Install it into the same environment with:  "
        + INSTALL_LINE
    )
    assert shlex.split(INSTALL_LINE)[:2] == ["pip", "install"]


def test_sdk_status_matches_this_environment() -> None:
    status = sdk_status()
    assert list(status) == ["sdk", "version", "python", "message"]
    assert status["python"] is (sys.version_info >= (3, 11))
    installed = all(
        importlib.util.find_spec(name) is not None for name in ("mcp", "pydantic", "anyio")
    )
    if installed:
        assert status["version"] == importlib.metadata.version("mcp")
    else:
        assert (status["sdk"], status["version"]) == (False, None)
    assert (status["message"] is None) is (status["sdk"] and status["python"])


def fake_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    python: tuple[int, int],
    modules: tuple[str, ...],
    version: str | None,
) -> None:
    def find_spec(name: str, package: str | None = None) -> object | None:
        return object() if name in modules else None

    def installed_version(name: str) -> str:
        if name != "mcp" or version is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return version

    monkeypatch.setattr(sys, "version_info", (*python, 0, "final", 0))
    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(importlib.metadata, "version", installed_version)


SDK = ("mcp", "pydantic", "anyio")
INSTALL_MESSAGE = (
    "the MCP server needs the MCP SDK. Install it into the same environment with:  " + INSTALL_LINE
)


@pytest.mark.parametrize(
    ("python", "modules", "version", "expected"),
    [
        ((3, 11), SDK, "2.3.0", {"sdk": True, "python": True, "message": None}),
        ((3, 13), SDK, "2.10.1", {"sdk": True, "python": True, "message": None}),
        ((3, 12), SDK, None, {"sdk": True, "python": True, "message": None}),
        ((3, 11), (), None, {"sdk": False, "python": True, "message": INSTALL_MESSAGE}),
        (
            (3, 11),
            ("pydantic", "anyio"),
            None,
            {"sdk": False, "python": True, "message": INSTALL_MESSAGE},
        ),
        (
            (3, 11),
            ("mcp", "anyio"),
            "2.3.0",
            {"sdk": False, "python": True, "message": INSTALL_MESSAGE},
        ),
        ((3, 11), SDK, "1.26.0", {"sdk": False, "python": True, "message": INSTALL_MESSAGE}),
        ((3, 11), SDK, "2.2.9", {"sdk": False, "python": True, "message": INSTALL_MESSAGE}),
        ((3, 11), SDK, "3.0.0", {"sdk": False, "python": True, "message": INSTALL_MESSAGE}),
        ((3, 11), SDK, "unknown", {"sdk": False, "python": True, "message": INSTALL_MESSAGE}),
        (
            (3, 10),
            SDK,
            "2.3.0",
            {"sdk": True, "python": False, "message": "the MCP server needs Python 3.11 or newer"},
        ),
        (
            (3, 10),
            (),
            None,
            {"sdk": False, "python": False, "message": "the MCP server needs Python 3.11 or newer"},
        ),
    ],
)
def test_sdk_status_says_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    python: tuple[int, int],
    modules: tuple[str, ...],
    version: str | None,
    expected: dict[str, Any],
) -> None:
    fake_environment(monkeypatch, python=python, modules=modules, version=version)
    status = sdk_status()
    reported = version if set(SDK) <= set(modules) else None
    assert status == {**expected, "version": reported}


def test_nothing_here_loads_the_sdk_or_opens_a_socket() -> None:
    program = (
        "import sys\n"
        "from wdva.gate import hostconfig\n"
        "hostconfig.sdk_status()\n"
        "for entry in hostconfig.KNOWN_HOSTS:\n"
        "    hostconfig.render_text(entry.id, client='my-app', store='/srv/vault')\n"
        "loaded = sorted(name for name in ('mcp', 'mcp_types', 'pydantic', 'anyio') "
        "if name in sys.modules)\n"
        "print(loaded)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True, timeout=120
    )
    assert done.stdout.strip() == "[]"

    source = Path(hostconfig.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        assert not re.match(
            r"\s*(import|from)\s+(socket|http|mcp|pydantic|anyio|subprocess)\b", line
        )
    for word in ("open(", "write_text", "write_bytes", "os.replace", "subprocess"):
        assert word not in source


# -- can the command of a configuration start at all? ----------------------------------------


def run_config(*arguments: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """``wdva gate config`` as a child process, away from the source tree."""
    done = subprocess.run(
        [sys.executable, "-m", "wdva", "gate", "config", *arguments],
        capture_output=True,
        text=True,
        timeout=120,
        cwd="/",
        env=env,
        check=False,
    )
    return done.returncode, done.stdout, done.stderr


def test_the_start_check_tries_this_python_the_way_the_command_runs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wdva.gate.app import startcheck

    argv = startcheck._child_argv()
    command = host_command("my-app", "/srv/vault")
    # The same interpreter, in the same isolated mode, as the command of a configuration.
    assert argv[:4] == command[:4] == [sys.executable, "-I", "-X", "utf8"]
    assert argv[4] == "-c"
    assert "import wdva.gate" in argv[5]

    # Here the package is installed where an isolated Python finds it.
    problem = startcheck.start_problem()
    if all(importlib.util.find_spec(name) for name in ("mcp", "pydantic", "anyio")):
        assert problem is None
    else:
        assert problem == str(missing_sdk_error())

    # A Python that does not find the package: the sentence about virtual environments.
    monkeypatch.setattr(
        startcheck, "_child_argv", lambda: [sys.executable, "-I", "-c", "import no_such_wdva"]
    )
    assert startcheck.start_problem() == startcheck.NOT_FOUND_MESSAGE
    assert "virtual environment" in startcheck.NOT_FOUND_MESSAGE
    # One that finds the package and not the SDK: what to install.
    monkeypatch.setattr(
        startcheck, "_child_argv", lambda: [sys.executable, "-c", "raise SystemExit(3)"]
    )
    assert startcheck.start_problem() == str(missing_sdk_error())
    # One that cannot be started, and one that hangs: a sentence, never an exception.
    monkeypatch.setattr(startcheck, "_child_argv", lambda: ["/nonexistent/python", "-c", "0"])
    assert "could not be started" in (startcheck.start_problem() or "")
    monkeypatch.setattr(
        startcheck,
        "_child_argv",
        lambda: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    assert "TimeoutExpired" in (startcheck.start_problem(timeout=0.2) or "")


@pytest.mark.skipif(sys.version_info < (3, 11), reason="the MCP server needs Python 3.11")
def test_the_config_command_tries_the_command_before_it_prints_it() -> None:
    if sdk_status()["message"] is not None:
        pytest.skip("this environment has no MCP SDK, which the text itself says")
    code, out, err = run_config("claude-desktop", "--client", "claude-desktop", "--store", "/srv/v")
    assert (code, err) == (0, "")
    printed = json.loads(out.split("\n\n")[2])
    assert printed["mcpServers"][SLUG]["args"][-2:] == ["--store", "/srv/v"]


def test_the_config_command_exits_1_with_the_reason_when_the_start_check_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wdva import cli
    from wdva.gate.app import startcheck

    if sdk_status()["message"] is not None:
        pytest.skip("without the SDK or on an old Python the text says so and nothing is tried")
    tried: list[int] = []

    def fails() -> str:
        tried.append(1)
        return startcheck.NOT_FOUND_MESSAGE

    monkeypatch.setattr(startcheck, "start_problem", fails)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["gate", "config", "claude-code", "--client", "claude-code", "--store", "/v"])
    shown = capsys.readouterr()
    assert stopped.value.code == 1
    assert shown.out == "", "a configuration that cannot work was printed"
    assert startcheck.NOT_FOUND_MESSAGE in shown.err
    assert tried == [1]
    # A product that starts no program has nothing to try, and gets its explanation.
    code = cli.main(["gate", "config", "chatgpt-web", "--client", "my-app", "--store", "/v"])
    shown = capsys.readouterr()
    assert code == 0
    assert "cannot reach this vault" in shown.out.replace("\n", " ")
    assert tried == [1]
