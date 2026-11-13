"""The product's name is in one place: change it there and every text a reader meets follows.

The tests make a copy of the package, write another name into the copy's
``gate/brand.py`` and run the copy in child processes. That is what a rename
is: four constants changed in one file. Whatever the copy then prints, serves
or hands to an agent must carry the new name and must not carry the name the
real package has today, in any of its three forms: the display name, the slug
and the prefix of the tool names.

What stays under any name is tested too: the command ``wdva``, the words a
record uses for the three tools, and the environment variable for the store.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import tokenize
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import wdva
from wdva.gate import brand, ledger, messages, paths

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

NEW = {
    "PRODUCT": "Example Desk",
    "SLUG": "example-desk",
    "TOOL_PREFIX": "desk",
    "TAGLINE": "An example of a desk between documents and assistants.",
}

# The name the real package has today, in every form a reader could meet it.
OLD_FORMS = (brand.PRODUCT, brand.SLUG, brand.tool_name(""), brand.SLUG.replace("-", "_"))

PACKAGE = Path(wdva.__file__).resolve().parent


def assert_renamed(text: str, where: str) -> None:
    """``text`` carries none of the forms of today's name."""
    for form in OLD_FORMS:
        at = text.find(form)
        assert at < 0, f"{where} still says {form!r}: ...{text[max(0, at - 60) : at + 60]!r}..."


@pytest.fixture(scope="module")
def renamed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the package whose ``brand.py`` holds another name; returns its parent."""
    root = tmp_path_factory.mktemp("renamed")
    shutil.copytree(PACKAGE, root / "wdva", ignore=shutil.ignore_patterns("__pycache__"))
    target = root / "wdva" / "gate" / "brand.py"
    source = target.read_text(encoding="utf-8")
    for name, value in NEW.items():
        source, count = re.subn(
            rf'^{name} = "[^"\n]*"$', f"{name} = {json.dumps(value)}", source, flags=re.M
        )
        assert count == 1, f"brand.py has no line that sets {name}"
    target.write_text(source, encoding="utf-8")
    return root


def child_code(root: Path, body: str) -> str:
    return (
        f"import sys\nsys.path.insert(0, {str(root)!r})\n"
        "import wdva\n"
        f"assert wdva.__file__.startswith({str(root)!r}), wdva.__file__\n" + body
    )


def run(root: Path, body: str, *arguments: str, timeout: float = 180) -> Any:
    """Run ``body`` against the renamed copy and return the JSON it prints."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("WDVA_")}
    done = subprocess.run(
        [sys.executable, "-c", child_code(root, body), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/",
        env=environment,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# -- the constants themselves ---------------------------------------------------------------


def test_the_brand_is_four_constants_and_nothing_else() -> None:
    assert brand.__all__ == ["PRODUCT", "SLUG", "TAGLINE", "TOOL_PREFIX", "tool_name"]
    assert brand.tool_name("search") == f"{brand.TOOL_PREFIX}_search"
    # Everything else takes them from there.
    assert messages.APP_NAME == brand.PRODUCT
    assert messages.SERVER_NAME == brand.SLUG
    names = tuple(brand.tool_name(word) for word in ("status", "search", "read"))
    assert names == messages.AGENT_TOOL_NAMES
    assert paths.STORE_DIR_NAME == brand.SLUG
    assert paths.default_store().name == brand.SLUG
    from wdva.gate import hostconfig

    assert hostconfig.SERVER_KEY == brand.SLUG
    # The module imports nothing, so that any module may import it.
    source = Path(brand.__file__).read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if re.match(r"(import|from) ", line)]
    assert imports == ["from __future__ import annotations", "import re"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PRODUCT", ""),
        ("PRODUCT", "Two\nlines"),
        ("PRODUCT", " padded "),
        ("PRODUCT", "x" * 41),
        ("TAGLINE", ""),
        ("TAGLINE", "a\ttab"),
        ("SLUG", "Example"),
        ("SLUG", "example desk"),
        ("SLUG", "example_desk"),
        ("SLUG", "-desk"),
        ("SLUG", "desk-"),
        ("SLUG", "9desk"),
        ("TOOL_PREFIX", "my-desk"),
        ("TOOL_PREFIX", "desk_"),
        ("TOOL_PREFIX", "Desk"),
        ("TOOL_PREFIX", ""),
    ],
)
def test_a_name_that_would_break_something_is_refused_at_import(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    brand._check()
    monkeypatch.setattr(brand, name, value)
    with pytest.raises(ValueError, match=f"brand.{name}"):
        brand._check()


def test_what_is_not_the_name_stays_under_any_name() -> None:
    # Words of the file format: a record written under one name reads the same under another.
    assert messages.TOOL_NAMES == ("wdva_status", "wdva_search", "wdva_read")
    assert ledger.SEARCH_TOOL == messages.TOOL_SEARCH
    # The variable belongs to the command `wdva`, like the others it reads.
    assert paths.ENV_STORE == "WDVA_GATE_STORE"


# -- every place the name is still spelled out ----------------------------------------------

# file, the literal as it stands in the source: why it does not follow brand.py
SPELLED_OUT = {
    ("brand.py", '"Locktender"'): "the one place",
    ("brand.py", '"locktender"'): "the one place",
    ("paths.py", '"wdva-gate"'): "the store directory's earlier name, still looked for",
    ("messages.py", '"wdva_status"'): "file format: what a record calls the tool",
    ("messages.py", '"wdva_search"'): "file format: what a record calls the tool",
    ("messages.py", '"wdva_read"'): "file format: what a record calls the tool",
    ("ledger.py", '"wdva_search"'): "file format: the tool that counts as a search",
    ("ledger.py", 'b"wdva-gate/v1/ledger-genesis\\x00"'): "file format: where every chain starts",
    ("link.py", '"wdva-gate-link"'): "the name of a thread, seen in a debugger only",
    ("link.py", '"wdva-gate-link-call"'): "the name of a thread, seen in a debugger only",
    ("extract.py", '"wdva-gate-pdf-in"'): "the name of a thread, seen in a debugger only",
    ("extract.py", '"wdva-gate-pdf-out"'): "the name of a thread, seen in a debugger only",
}


def test_the_name_is_spelled_out_only_where_this_list_says() -> None:
    # Fixed to today's name and to the one before it on purpose: after a rename this list is
    # the map of what was left behind, and each entry says why it may stay.
    forms = re.compile(r"[Ll]ocktender|WDVA Gate|wdva-gate|wdva_(?:status|search|read)|wdva_gate")
    found: set[tuple[str, str]] = set()
    for path in sorted((PACKAGE / "gate").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.STRING or not forms.search(token.string):
                continue
            if token.string.lstrip("rRbBuUfF").startswith(('"""', "'''")):
                continue  # documentation
            found.add((path.name, token.string))
    assert found == set(SPELLED_OUT)
    # The interface spells it nowhere: the server writes it into the page.
    for path in sorted((PACKAGE / "gate" / "app" / "static").iterdir()):
        text = path.read_text(encoding="utf-8")
        assert forms.search(text) is None, path.name


# -- under another name: what an agent can receive ------------------------------------------

AGENT_TEXTS = """
import json
from wdva.gate import messages as m

texts = {
    "locked": m.locked(),
    "not allowed": m.not_allowed(),
    "not found": m.not_found(),
    "needs approval": m.needs_approval("d-3f9a2c41b07e.7", "claude-desktop"),
    "bad request": m.bad_request(),
    "unavailable": m.unavailable(),
    "store not found": m.store_not_found("claude-desktop"),
}
for variant in m.OVER_LIMIT_VARIANTS:
    texts["over limit " + variant] = m.over_limit(variant)
texts["status"] = m.status_ok(
    client="claude-desktop", destination="Anthropic", hosted=True,
    collections=[m.StatusCollection("home", "read", "papers"),
                 m.StatusCollection("tax", "approve")],
    left=100, chars_per_day=200, chars_per_30_days=300, searches_per_day=5, chars_per_call=50,
    max_chars=8000, max_fraction=0.5,
)
lines = [m.SearchLine("d-3f9a2c41b07e.7", "home", "a snippet", "a title"),
         m.SearchLine("d-3f9a2c41b07e.8", "tax", m.TEXT_WITHHELD),
         m.SearchLine("d-3f9a2c41b07e.9", "home", m.NO_FREE_TEXT)]
common = dict(client="claude-desktop", client_seq=3, left_before=5000)
texts["search"] = m.search_ok(lines, **common).text
texts["search fitted"] = m.fit_search(lines, chars_per_call=2000, **common).text
texts["no matches"] = m.search_no_matches(**common).text
texts["none shown"] = m.search_none_shown(**common).text
texts["read"] = m.read_ok(doc_id="d-3f9a2c41b07e", passage=7, passages=52, collection="home",
                          body="a line\\nanother", tag="a1b2c3", title="a title", **common).text
texts["instructions"] = m.SERVER_INSTRUCTIONS
for name, text in m.TOOL_TITLES.items():
    texts["title of " + name] = text
for name, text in m.TOOL_DESCRIPTIONS.items():
    texts["description of " + name] = text
for name, text in m.ARGUMENT_DESCRIPTIONS.items():
    texts["argument " + name] = text
print(json.dumps({
    "texts": texts,
    "app": m.APP_NAME,
    "server": m.SERVER_NAME,
    "tools": list(m.AGENT_TOOL_NAMES),
    "titled": sorted(m.TOOL_TITLES),
    "described": sorted(m.TOOL_DESCRIPTIONS),
    "record_words": list(m.TOOL_NAMES),
}))
"""


def test_every_fixed_text_an_agent_can_receive_carries_the_new_name(renamed: Path) -> None:
    told = run(renamed, AGENT_TEXTS)
    assert told["app"] == "Example Desk"
    assert told["server"] == "example-desk"
    assert told["tools"] == ["desk_status", "desk_search", "desk_read"]
    assert told["titled"] == told["described"] == sorted(told["tools"])
    texts = told["texts"]
    for name, text in texts.items():
        assert_renamed(text, f"the text {name!r}")
    assert "in the Example Desk app on this computer" in texts["locked"]
    assert 'open "Approvals" in the Example Desk app' in texts["needs approval"]
    assert "then call desk_read again with the same ref" in texts["needs approval"]
    assert (
        "Use a ref returned by desk_search, or a collection name from desk_status."
        in (texts["not found"])
    )
    assert "look at the Example Desk app" in texts["unavailable"]
    assert (
        "in the Example Desk app on the computer that holds the vault" in (texts["store not found"])
    )
    assert "Stop calling desk_search and desk_read" in texts["over limit client-day"]
    assert "desk_read with a ref you already have still works" in texts["over limit searches"]
    assert "Use desk_read with a ref to get the passage." in texts["search"]
    assert texts["instructions"].startswith("example-desk gives read-only, limited, recorded")
    assert "desk_status shows what this client may use" in texts["instructions"]
    assert len(texts["instructions"]) <= 512  # some applications cut longer ones
    assert "Pass a ref to desk_read to get the passage." in texts["description of desk_search"]
    assert "one collection name from desk_status" in texts["argument collection"]
    assert "as returned by desk_search" in texts["argument ref"]
    # What a record calls the tools did not move.
    assert told["record_words"] == ["wdva_status", "wdva_search", "wdva_read"]


MCP_SURFACE = """
import json
import anyio
from mcp import Client
from mcp.types import Implementation
from wdva.gate.mcp_server import build_server
from wdva.gate.types import Reply

class Backend:
    def status(self, caller):
        return Reply("OK", "OK: status answered.", False)
    def search(self, caller, query, collection="", limit=5):
        return Reply("OK", "OK: search answered.", True)
    def read(self, caller, ref):
        raise RuntimeError("the backend failed")

async def main():
    server = build_server(Backend(), "claude-desktop")
    async with Client(server, client_info=Implementation(name="host", version="1")) as client:
        tools = [tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                 for tool in (await client.list_tools()).tools]
        answers = {}
        for name, arguments in ((tools[0]["name"], {}), (tools[1]["name"], {"query": "rent"}),
                                (tools[2]["name"], {"ref": "d-3f9a2c41b07e.7"})):
            result = await client.call_tool(name, arguments)
            answers[name] = result.content[0].text
        print(json.dumps({"server": client.server_info.name, "instructions": client.instructions,
                          "tools": tools, "answers": answers}))

anyio.run(main)
"""


@pytest.mark.mcp
def test_the_mcp_server_announces_itself_and_its_tools_under_the_new_name(renamed: Path) -> None:
    surface = run(renamed, MCP_SURFACE)
    assert surface["server"] == "example-desk"
    assert [tool["name"] for tool in surface["tools"]] == [
        "desk_status",
        "desk_search",
        "desk_read",
    ]
    assert_renamed(json.dumps(surface), "what the MCP server announces")
    assert [tool["inputSchema"]["title"] for tool in surface["tools"]] == [
        "desk_statusArguments",
        "desk_searchArguments",
        "desk_readArguments",
    ]
    assert surface["answers"]["desk_status"] == "OK: status answered."
    assert surface["answers"]["desk_search"] == "OK: search answered."
    # A backend that fails is answered with the fixed text, which names the app.
    assert "look at the Example Desk app" in surface["answers"]["desk_read"]


# -- under another name: configuration, directory, command line -----------------------------

HOST_CONFIG = """
import json
from wdva.gate import hostconfig, paths
from wdva.gate.cli import add_gate_parser
import argparse

out = {"key": hostconfig.SERVER_KEY, "directory": str(paths.default_store()),
       "variable": paths.ENV_STORE, "hosts": {}}
for entry in hostconfig.KNOWN_HOSTS:
    config = hostconfig.render(entry.id, client="my-app", store="/srv/vault")
    out["hosts"][entry.id] = {
        "text": hostconfig.render_text(entry.id, client="my-app", store="/srv/vault"),
        "json": config.as_dict(),
        "command": list(config.command),
    }
parser = argparse.ArgumentParser(prog="wdva")
add_gate_parser(parser.add_subparsers(dest="command"))
gate = parser._subparsers._group_actions[0].choices["gate"]
out["help"] = gate.format_help()
out["app help"] = gate._subparsers._group_actions[0].choices["app"].format_help()
print(json.dumps(out))
"""


def test_the_host_configuration_and_the_data_directory_carry_the_new_name(renamed: Path) -> None:
    told = run(renamed, HOST_CONFIG)
    assert told["key"] == "example-desk"
    assert Path(told["directory"]).name == "example-desk"
    assert told["variable"] == "WDVA_GATE_STORE"
    hosts = told["hosts"]
    assert len(hosts) == 9
    for host, shown in hosts.items():
        assert_renamed(shown["text"], f"the configuration text for {host}")
        assert_renamed(json.dumps(shown["json"]), f"the configuration for {host}")
    claude = json.loads(hosts["claude-desktop"]["json"]["blocks"][0]["text"])
    assert list(claude["mcpServers"]) == ["example-desk"]
    assert 'add the "example-desk" entry inside it' in " ".join(
        hosts["claude-desktop"]["json"]["notes"]
    )
    assert hosts["claude-code"]["json"]["blocks"][0]["text"].startswith(
        "claude mcp add --transport stdio --scope user example-desk -- "
    )
    assert hosts["codex"]["json"]["blocks"][0]["text"].startswith("codex mcp add example-desk -- ")
    assert hosts["codex"]["json"]["blocks"][1]["text"].startswith("[mcp_servers.example-desk]\n")
    assert '"example-desk": {' in hosts["openclaw"]["json"]["blocks"][0]["text"]
    assert "Example Desk neither limits nor records it." in hosts["chatgpt-web"]["text"].replace(
        "\n", " "
    )
    # The command is the package's, and stays: an application still runs `wdva gate mcp`.
    assert hosts["generic"]["command"][1:8] == ["-I", "-X", "utf8", "-m", "wdva", "gate", "mcp"]
    assert "Example Desk. An example of a desk between documents and assistants." in told[
        "help"
    ].replace("\n", " ")
    assert "`wdva gate app`" in told["help"]
    assert_renamed(told["help"], "the help of the gate commands")
    assert "WDVA_GATE_STORE" in told["app help"]


# -- under another name: the service, as the owner and as an agent meet it ------------------

SERVICE = """
import json, sys, tempfile, shutil
from pathlib import Path
from wdva.gate.app import demo, main
from wdva.gate.app.service import GateService
from wdva.gate.types import Caller

work = Path(tempfile.mkdtemp(prefix="gb-", dir="/tmp"))
out = {"agent": [], "owner": []}
try:
    service = GateService(work / "vault", scrypt_n=2**14)
    service.create_store(passphrase="correct horse battery staple")
    lines = "\\n\\n".join(f"Paragraph {n} of the lease says that the rent is due on day {n}. " * 6
                         for n in range(1, 9))
    service.add_document(collection="home", name="lease.txt", data=lines.encode())
    service.set_collection(name="home", allow_hosted=True)
    service.add_app(name="claude-desktop", hosted=True, destination="Anthropic")
    service.set_grant(app="claude-desktop", collection="home", mode="read")
    caller = Caller("claude-desktop", "host", "1", "2025-11-25", "stdio")
    status = service.backend.status(caller)
    search = service.backend.search(caller, "rent due", "home")
    ref = search.text.split("ref=")[1].split(" ")[0]
    read = service.backend.read(caller, ref)
    missing = service.backend.read(caller, "d-0123456789ab.1")
    stranger = service.backend.search(Caller("someone-else"), "rent")
    out["codes"] = [status.code, search.code, read.code, missing.code, stranger.code]
    out["agent"] += [status.text, search.text, read.text, missing.text, stranger.text]
    session = service._session
    out["record_tools"] = sorted({r.data["tool"] for r in session.store.ledger.records()
                                  if r.kind == "release"})
    out["owner"].append(service.connect(name="claude-desktop"))
    out["ping"] = service._ping()["app"]
    service.close("interrupt")            # as Ctrl-C does
    out["agent"].append(service.backend.search(caller, "rent").text)
    service.unlock(passphrase="correct horse battery staple")
    activity = service.activity(limit=200)
    out["summaries"] = [item["summary"] for item in activity["items"]]
    out["kinds"] = [item["kind"] for item in activity["items"] if item["app"] == "claude-desktop"]
    out["searches_day"] = service.apps()["apps"][0]["usage"]["searches_day"]
    out["owner"] += [service.state(), activity, service.apps(), service.documents(),
                     service.settings(), service.approvals()]
    out["owner"] += [service.activity_detail(seq=item["seq"]) for item in activity["items"]]
    service.close()

    # The demo: its store's place, and everything the played agent is told.
    place = main._demo_directory()
    out["demo_directory"] = place.name
    passphrase = demo.make_demo_store(place, scrypt_n=2**14)
    played = GateService(place, demo=True, demo_passphrase=passphrase)
    played.unlock(passphrase=passphrase)
    agent = demo.DemoAgent(played.backend)
    replies = []
    backend = played.backend
    class Listening:
        def status(self, caller):
            reply = backend.status(caller); replies.append(reply.text); return reply
        def search(self, caller, query, collection="", limit=5):
            reply = backend.search(caller, query, collection, limit)
            replies.append(reply.text); return reply
        def read(self, caller, ref):
            reply = backend.read(caller, ref); replies.append(reply.text); return reply
    agent = demo.DemoAgent(Listening())
    for _ in range(30):
        agent.step()
    out["demo_codes"] = sorted({call.code for call in agent.calls})
    out["agent"] += replies
    out["owner"] += [played.state(), played.activity(limit=200)]
    played.close()
    shutil.rmtree(place, ignore_errors=True)
finally:
    shutil.rmtree(work, ignore_errors=True)
print(json.dumps(out))
"""


def test_the_service_and_the_demo_under_the_new_name(renamed: Path) -> None:
    told = run(renamed, SERVICE)
    assert told["codes"] == ["OK", "OK", "OK", "NOT_FOUND", "NOT_ALLOWED"]
    for number, text in enumerate(told["agent"]):
        assert_renamed(text, f"reply {number} to an agent")
    assert "Use desk_read with a ref to get the passage." in told["agent"][1]
    assert "Use a ref returned by desk_search" in told["agent"][3]
    assert "in the Example Desk app on this computer" in told["agent"][5]  # the locked vault
    assert told["ping"] == "Example Desk"
    # Nothing the owner's interface is sent names the old product either.
    assert_renamed(json.dumps(told["owner"]), "an answer of the service")
    assert "you stopped Example Desk, which locked the vault" in told["summaries"]
    connect = told["owner"][0]
    assert list(json.loads(connect["blocks"][0]["text"])["mcpServers"]) == ["example-desk"]
    # The record kept its own words for the tools, so the vault reads the same under the
    # new name: after the restart the search is still listed and still counted as one.
    assert told["record_tools"] == ["wdva_read", "wdva_search"]
    assert [s for s in told["summaries"] if s.startswith("searched home: ")] != []
    assert (told["kinds"].count("question"), told["kinds"].count("release")) == (1, 2)
    assert told["searches_day"] == 1
    # The demo.
    assert told["demo_directory"].startswith("example-desk-demo-")
    assert {"OK", "NEEDS_APPROVAL", "OVER_LIMIT", "NOT_ALLOWED"} <= set(told["demo_codes"])


# -- under another name: the app itself ------------------------------------------------------


class Running:
    """The renamed app as a child process, and one browser's view of it."""

    def __init__(self, root: Path, *arguments: str) -> None:
        code = child_code(root, "from wdva.cli import main\nsys.exit(main(sys.argv[1:]))\n")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("WDVA_")}
        self.process = subprocess.Popen(
            [sys.executable, "-c", code, "gate", "app", "--no-browser", "--port", "0", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/",
            env=environment,
        )
        self.lines: list[str] = []
        self.session: str | None = None  # the token the app told this "page"
        watchdog = threading.Timer(90, self.process.kill)
        watchdog.start()
        try:
            assert self.process.stdout is not None
            while not self.lines or not self.lines[-1].startswith("Ctrl-C"):
                line = self.process.stdout.readline()
                assert line, f"the app ended early: {self.process.stderr.read()}"  # type: ignore[union-attr]
                self.lines.append(line.rstrip("\n"))
        finally:
            watchdog.cancel()
        found = re.search(r"http://127\.0\.0\.1:(\d+)/", "\n".join(self.lines))
        assert found is not None, self.lines
        self.port = int(found.group(1))

    def call(self, method: str, target: str, body: Any = None) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {"Host": f"127.0.0.1:{self.port}"}
            if self.session:
                headers["X-WDVA-Session"] = self.session
            data = None
            if method != "GET":
                headers["X-WDVA-Gate"] = "1"
                data = json.dumps(body or {}).encode()
                headers["Content-Type"] = "application/json"
            connection.request(method, target, body=data, headers=headers)
            answer = connection.getresponse()
            content = answer.read()
            assert answer.getheader("Set-Cookie") is None, "the session must never be a cookie"
            if answer.status == 200 and target.startswith("/api/session"):
                self.session = json.loads(content).get("session", self.session)
            return answer.status, {k.lower(): v for k, v in answer.getheaders()}, content
        finally:
            connection.close()

    def json(self, method: str, target: str, body: Any = None) -> Any:
        status, _, content = self.call(method, target, body)
        assert status == 200, content
        return json.loads(content)

    def stop(self) -> tuple[int, str, str]:
        self.process.send_signal(signal.SIGINT)
        try:
            out, err = self.process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise
        return self.process.returncode, out, err


@pytest.fixture
def running(renamed: Path) -> Iterator[list[Running]]:
    started: list[Running] = []
    yield started
    for app in started:
        if app.process.poll() is None:
            app.process.kill()
            app.process.communicate()


def static_names() -> list[str]:
    return sorted(path.name for path in (PACKAGE / "gate" / "app" / "static").iterdir())


def test_the_app_starts_serves_and_answers_under_the_new_name(
    renamed: Path, running: list[Running], short_store_dir: Path
) -> None:
    from wdva._version import __version__

    app = Running(renamed, "--store", str(short_store_dir))
    running.append(app)
    # The start-up lines.
    assert app.lines[0] == f"Example Desk {__version__}"
    assert app.lines[1] == f"store: {short_store_dir}"
    assert_renamed("\n".join(app.lines), "the start-up lines")
    # The session reply and the page, before anything is paired.
    session = app.json("GET", "/api/session")
    assert session["name"] == "Example Desk"
    status, headers, content = app.call("GET", "/")
    page = content.decode("utf-8")
    assert status == 200
    assert "<title>Example Desk</title>" in page
    assert '<meta name="application-name" content="Example Desk">' in page
    assert '<p class="boot-name">Example Desk</p>' in page
    assert "Example Desk needs JavaScript to show this page." in page
    assert "{{" not in page
    assert headers["server"] == "Example-Desk"
    # Every file of the interface as it is served.
    for name in static_names():
        status, _, content = app.call("GET", f"/{name}")
        assert status == 200, name
        assert_renamed(content.decode("utf-8"), f"the served file {name}")
    # Paired: the session is a token without any name in it, and then what the service says.
    code = re.search(r"pairing code: (\S+)", "\n".join(app.lines))
    assert code is not None
    answer = app.json("POST", "/api/session/pair", {"code": code.group(1)})
    assert answer == {"paired": True, "session": app.session}
    assert re.fullmatch(r"[0-9a-f]{64}", app.session or "")
    state = app.json("POST", "/api/store/create", {"passphrase": "correct horse battery staple"})
    assert state["locked"] is False
    assert_renamed(json.dumps(app.json("GET", "/api/apps")), "the list of apps")
    assert (app.stop()[0], app.process.returncode) == (0, 0)


def test_the_demo_starts_under_the_new_name(renamed: Path, running: list[Running]) -> None:
    app = Running(renamed, "--demo")
    running.append(app)
    assert app.lines[0].startswith("Example Desk ")
    assert_renamed("\n".join(app.lines), "the start-up lines of the demo")
    assert app.call("GET", "/")[0] == 200
    assert app.json("GET", "/api/session")["name"] == "Example Desk"  # and the demo's session
    state = app.json("GET", "/api/state")
    assert Path(state["store"]["path"]).name.startswith("example-desk-demo-")
    assert_renamed(json.dumps(state), "the state of the demo")
    assert_renamed(json.dumps(app.json("GET", "/api/activity?limit=200")), "the demo's activity")
    code, out, err = app.stop()
    assert (code, err) == (0, "")
    assert_renamed(out, "what the demo printed")
