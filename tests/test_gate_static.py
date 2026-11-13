"""Which module of the gate may import what, checked by reading the source.

The rules keep the parts apart that must stay apart: the decision logic opens
no socket, only one module talks to the MCP SDK, nothing can reach out to the
network, and modules that exist on some systems only are not needed to import
the package.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import wdva.gate

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

GATE = Path(wdva.gate.__file__).resolve().parent
SOURCES = sorted(GATE.rglob("*.py"))

SOCKET_MODULES = {"socket", "socketserver", "http"}
SOCKET_USERS = {"link.py", "app/httpd.py"}
SDK_MODULES = {"mcp", "pydantic", "anyio"}
SDK_USERS = {"mcp_server.py"}
INSIDE_FUNCTIONS_ONLY = {
    "fcntl",
    "resource",
    "ctypes",
    "webbrowser",
    "termios",
    "tty",
    "pty",
    "pwd",
    "grp",
    "msvcrt",
    "winreg",
    "webview",
}
NETWORK_CLIENTS = {
    "urllib.request",
    "ssl",
    "smtplib",
    "ftplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "xmlrpc",
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
    "websockets",
    "websocket",
}


def name_of(path: Path) -> str:
    return path.relative_to(GATE).as_posix()


def imports_of(source: str, module: str = "wdva.gate.x") -> Iterator[tuple[str, bool, int]]:
    """Every module a source imports: its dotted name, whether inside a function, the line.

    ``from a import b`` counts as ``a`` and as ``a.b``, because ``b`` may be a
    module. Names given as text to ``import_module`` or ``__import__`` count too.
    """
    tree = ast.parse(source)
    inside: dict[ast.AST, bool] = {}

    def mark(node: ast.AST, in_function: bool) -> None:
        inside[node] = in_function
        deeper = in_function or isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        )
        for child in ast.iter_child_nodes(node):
            mark(child, deeper)

    mark(tree, False)
    package = module.rsplit(".", 1)[0]
    for node, in_function in inside.items():
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, in_function, node.lineno
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.split(".")
                parent = parent[: len(parent) - (node.level - 1)]
                base = ".".join([*parent, base] if base else parent)
            yield base, in_function, node.lineno
            for alias in node.names:
                yield f"{base}.{alias.name}", in_function, node.lineno
        elif (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
                or (isinstance(node.func, ast.Name) and node.func.id == "__import__")
            )
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.args[0].value, in_function, node.lineno


def is_or_under(module: str, roots: set[str]) -> bool:
    return any(module == root or module.startswith(root + ".") for root in roots)


def offenders(roots: set[str], allowed: set[str], *, top_level_only: bool = False) -> list[str]:
    """Where a module of ``roots`` is imported by a file that is not in ``allowed``."""
    found = []
    for path in SOURCES:
        if name_of(path) in allowed:
            continue
        module = "wdva.gate." + name_of(path)[:-3].replace("/", ".")
        for imported, in_function, line in imports_of(path.read_text(encoding="utf-8"), module):
            if is_or_under(imported, roots) and not (top_level_only and in_function):
                found.append(f"{name_of(path)}:{line} imports {imported}")
    return found


def test_the_scan_sees_every_way_of_importing() -> None:
    source = (
        "import socket, os.path\n"
        "from http import server\n"
        "from . import link\n"
        "from .app import httpd\n"
        "from .. import vault\n"
        "def f():\n"
        "    import fcntl\n"
        "    from ctypes import CDLL\n"
        "    importlib.import_module('webview')\n"
        "    __import__('mcp')\n"
        "class C:\n"
        "    import resource\n"
    )
    seen = {(name, inside) for name, inside, _ in imports_of(source, "wdva.gate.core")}
    assert {
        ("socket", False),
        ("os.path", False),
        ("http", False),
        ("http.server", False),
        ("wdva.gate", False),
        ("wdva.gate.link", False),
        ("wdva.gate.app", False),
        ("wdva.gate.app.httpd", False),
        ("wdva.vault", False),
        ("fcntl", True),
        ("ctypes", True),
        ("ctypes.CDLL", True),
        ("webview", True),
        ("mcp", True),
        ("resource", False),
    } <= seen
    assert is_or_under("http.server", {"http"})
    assert not is_or_under("httpx", {"http"})


def test_the_sources_are_all_there() -> None:
    names = {name_of(path) for path in SOURCES}
    assert {"core.py", "link.py", "mcp_server.py", "app/httpd.py", "app/service.py"} <= names
    assert {"app/main.py", "app/demo.py", "store.py", "ledger.py", "policy.py"} <= names


def test_only_the_link_and_the_interface_server_open_sockets() -> None:
    assert offenders(SOCKET_MODULES, SOCKET_USERS) == []
    # And they really are the two that do, so the rule above is about something.
    for name in SOCKET_USERS:
        source = (GATE / name).read_text(encoding="utf-8")
        assert any(imported == "socket" for imported, _, _ in imports_of(source))


def test_only_the_mcp_server_imports_the_sdk() -> None:
    assert offenders(SDK_MODULES, SDK_USERS) == []
    source = (GATE / "mcp_server.py").read_text(encoding="utf-8")
    assert any(is_or_under(imported, {"mcp"}) for imported, _, _ in imports_of(source))


def test_platform_modules_and_the_browser_are_imported_inside_functions_only() -> None:
    assert offenders(INSIDE_FUNCTIONS_ONLY, set(), top_level_only=True) == []


def test_nothing_imports_a_network_client() -> None:
    assert offenders(NETWORK_CLIENTS, set()) == []


def test_the_decision_logic_imports_neither_the_link_nor_the_app() -> None:
    source = (GATE / "core.py").read_text(encoding="utf-8")
    forbidden = {"wdva.gate.link", "wdva.gate.app", "wdva.gate.mcp_server"}
    assert [
        imported
        for imported, _, _ in imports_of(source, "wdva.gate.core")
        if is_or_under(imported, forbidden | SOCKET_MODULES | SDK_MODULES)
    ] == []


def test_the_store_and_the_record_do_not_interpret_the_policy() -> None:
    for name in ("store.py", "ledger.py"):
        source = (GATE / name).read_text(encoding="utf-8")
        module = "wdva.gate." + name[:-3]
        assert not [
            imported
            for imported, _, _ in imports_of(source, module)
            if is_or_under(imported, {"wdva.gate.policy", "wdva.gate.core"})
        ]


@pytest.mark.parametrize(
    "modules",
    [
        ("wdva.gate",),
        ("wdva.gate", "wdva.gate.cli"),
        ("wdva.gate.app",),
        ("wdva.gate.app.service", "wdva.gate.app.demo"),
        ("wdva.gate.link", "wdva.gate.hostconfig"),
    ],
)
def test_importing_the_package_loads_no_sdk_and_no_web_server(modules: tuple[str, ...]) -> None:
    code = (
        "import json, sys\n"
        f"import {', '.join(modules)}\n"
        "heavy = ('mcp', 'pydantic', 'anyio', 'http.server', 'webbrowser', 'webview')\n"
        "print(json.dumps(sorted(n for n in sys.modules if n in heavy or "
        "n.split('.')[0] in heavy[:3])))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == []


def test_the_app_start_up_is_what_loads_the_web_server() -> None:
    """The counterpart of the test above: the check can see a loaded module."""
    code = (
        "import json, sys\n"
        "import wdva.gate.app.main\n"
        "print(json.dumps('http.server' in sys.modules))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) is True
