"""The interface server: what a web page or another program has to get past, over a real socket."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from wdva.gate import brand
from wdva.gate.app import httpd
from wdva.gate.app.api import Service, ServiceError
from wdva.gate.app.httpd import AppServer

# The product's name comes from one place; the tests take it from there too.
PRODUCT, SLUG = brand.PRODUCT, brand.SLUG
SESSION_HEADER = "X-WDVA-Session"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

VERSION = "9.9.9a1"
SECRET = "SECRET-OUTSIDE-THE-STATIC-DIRECTORY"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
INDEX = b"<!doctype html><title>{{product}}</title><script src=app.js></script>"
INDEX_SERVED = INDEX.replace(b"{{product}}", PRODUCT.encode())
APP_JS = b"console.log('interface');"


# -- a service that only takes notes -------------------------------------------------


class FakeService:
    """Has every method of the service, with the same parameters. Records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None
        self.answer: Any = None

    def _did(self, method: str, /, **arguments: Any) -> Any:
        self.calls.append((method, arguments))
        if self.error is not None:
            raise self.error
        return {"did": method} if self.answer is None else self.answer

    def state(self) -> Any:
        return self._did("state")

    def create_store(self, *, passphrase: str) -> Any:
        return self._did("create_store", passphrase=passphrase)

    def unlock(self, *, passphrase: str, hours: float | None = None) -> Any:
        return self._did("unlock", passphrase=passphrase, hours=hours)

    def lock(self) -> Any:
        return self._did("lock")

    def reauth(self, *, passphrase: str) -> Any:
        return self._did("reauth", passphrase=passphrase)

    def set_serving(self, *, kinds: str | None = None, paused: list[str] | None = None) -> Any:
        return self._did("set_serving", kinds=kinds, paused=paused)

    def verify(self) -> Any:
        return self._did("verify")

    def set_interface(self, *, view: str | None = None, setup: str | None = None) -> Any:
        return self._did("set_interface", view=view, setup=setup)

    def activity(
        self,
        *,
        limit: int | None = None,
        before: int | None = None,
        app: str | None = None,
        kind: str | None = None,
    ) -> Any:
        return self._did("activity", limit=limit, before=before, app=app, kind=kind)

    def activity_detail(self, *, seq: int) -> Any:
        return self._did("activity_detail", seq=seq)

    def approvals(self) -> Any:
        return self._did("approvals")

    def release(self, *, app: str, ref: str, body_sha256: str) -> Any:
        return self._did("release", app=app, ref=ref, body_sha256=body_sha256)

    def dismiss(self, *, app: str, ref: str) -> Any:
        return self._did("dismiss", app=app, ref=ref)

    def documents(self) -> Any:
        return self._did("documents")

    def add_document(self, *, collection: str, name: str, data: bytes) -> Any:
        return self._did("add_document", collection=collection, name=name, data=data)

    def remove_document(self, *, id: str) -> Any:
        return self._did("remove_document", id=id)

    def set_collection(
        self,
        *,
        name: str,
        note: str | None = None,
        allow_hosted: bool | None = None,
        show_titles: bool | None = None,
    ) -> Any:
        return self._did(
            "set_collection",
            name=name,
            note=note,
            allow_hosted=allow_hosted,
            show_titles=show_titles,
        )

    def remove_collection(self, *, name: str) -> Any:
        return self._did("remove_collection", name=name)

    def apps(self) -> Any:
        return self._did("apps")

    def add_app(self, *, name: str, hosted: bool, destination: str | None = None) -> Any:
        return self._did("add_app", name=name, hosted=hosted, destination=destination)

    def remove_app(self, *, name: str) -> Any:
        return self._did("remove_app", name=name)

    def set_grant(self, *, app: str, collection: str, mode: str) -> Any:
        return self._did("set_grant", app=app, collection=collection, mode=mode)

    def set_app_limits(self, *, name: str, limits: dict[str, Any]) -> Any:
        return self._did("set_app_limits", name=name, limits=limits)

    def connect(self, *, name: str, host: str | None = None) -> Any:
        return self._did("connect", name=name, host=host)

    def connect_check(self, *, name: str, host: str | None = None) -> Any:
        return self._did("connect_check", name=name, host=host)

    def settings(self) -> Any:
        return self._did("settings")

    def set_settings(
        self,
        *,
        defaults: dict[str, Any] | None = None,
        groups: dict[str, Any] | None = None,
        per_document: dict[str, Any] | None = None,
        approval: dict[str, Any] | None = None,
    ) -> Any:
        return self._did(
            "set_settings",
            defaults=defaults,
            groups=groups,
            per_document=per_document,
            approval=approval,
        )

    def add_replaced_string(self, *, text: str) -> Any:
        return self._did("add_replaced_string", text=text)

    def demo_again(self) -> Any:
        return self._did("demo_again")


SERVICE_METHODS = {name for name in vars(Service) if not name.startswith("_")}
MakeServer = Callable[..., AppServer]


# -- talking to the server ------------------------------------------------------------


@dataclass
class Answer:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = [value for key, value in self.headers if key.lower() == name.lower()]
        assert len(values) <= 1, f"{name} was sent {len(values)} times"
        return values[0] if values else None

    def json(self) -> Any:
        assert self.header("Content-Type") == "application/json"
        return json.loads(self.body)

    def error(self) -> str:
        """The error code, after checking that the body has the form every error has."""
        body = self.json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {"code", "message"}
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["message"]
        return str(body["error"]["code"])


class Browser:
    """A client that behaves like the interface's own page.

    It keeps the session token it is told and sends it in the session header.
    It has no cookie jar at all: the server must never need one.
    """

    def __init__(self, server: AppServer) -> None:
        self.server = server
        self.token: str | None = None

    @property
    def session_line(self) -> str:
        """The header line that shows the session, for requests written out by hand."""
        assert self.token is not None
        return f"{SESSION_HEADER}: {self.token}"

    def call(
        self,
        method: str,
        target: str,
        *,
        json_body: Any = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        gate_header: bool = True,
    ) -> Answer:
        sent: dict[str, str] = {}
        if self.token is not None:
            sent[SESSION_HEADER] = self.token
        if gate_header and method in ("POST", "DELETE"):
            sent["X-WDVA-Gate"] = "1"
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=20)
        try:
            connection.request(method, target, body=body, headers=sent)
            response = connection.getresponse()
            answer = Answer(response.status, response.getheaders(), response.read())
        finally:
            connection.close()
        assert answer.header("Set-Cookie") is None, "the session must never be a cookie"
        if answer.status == 200 and target in ("/api/session", "/api/session/pair"):
            self.token = answer.json().get("session", self.token)
        return answer

    def pair(self) -> Browser:
        answer = self.call(
            "POST", "/api/session/pair", json_body={"code": self.server.pairing_code}
        )
        assert answer.status == 200
        assert self.token is not None
        return self


def raw(server: AppServer, request: bytes, *, timeout: float = 5.0) -> Answer:
    """Send exactly these bytes and read one response, without waiting for anything else."""
    with socket.create_connection(("127.0.0.1", server.port), timeout=timeout) as sock:
        sock.sendall(request)
        response = http.client.HTTPResponse(sock)
        response.begin()
        return Answer(response.status, response.getheaders(), response.read())


def head(server: AppServer, method: str, target: str, *lines: str) -> bytes:
    """The head of a request with a correct Host line, the given header lines and no body."""
    host = f"Host: 127.0.0.1:{server.port}"
    return "\r\n".join([f"{method} {target} HTTP/1.1", host, *lines, "", ""]).encode("ascii")


def assert_headers(answer: Answer) -> None:
    """What every response carries, and what none does."""
    assert answer.header("Content-Security-Policy") == CSP
    assert answer.header("X-Content-Type-Options") == "nosniff"
    assert answer.header("Referrer-Policy") == "no-referrer"
    assert answer.header("Content-Length") == str(len(answer.body))
    assert not [name for name, _ in answer.headers if name.lower().startswith("access-control")]


# -- fixtures --------------------------------------------------------------------------


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    (directory / "sub").mkdir(parents=True)
    (directory / "index.html").write_bytes(INDEX)
    (directory / "app.js").write_bytes(APP_JS)
    (directory / "style.css").write_bytes(b"body { margin: 0 }")
    (directory / "icon.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    (directory / "notes.md").write_bytes(b"not a type the interface uses")
    (directory / ".hidden.js").write_bytes(b"hidden")
    (directory / "sub" / "inner.js").write_bytes(b"in a subdirectory")
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    (directory / "link.txt").symlink_to(tmp_path / "secret.txt")
    return directory


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def make_server(service: FakeService, static_dir: Path) -> Iterator[MakeServer]:
    servers: list[AppServer] = []

    def make(**options: Any) -> AppServer:
        options.setdefault("version", VERSION)
        options.setdefault("static_dir", static_dir)
        server = AppServer(service, **options)
        servers.append(server)
        server.start()
        return server

    yield make
    for server in servers:
        server.stop()


@pytest.fixture
def server(make_server: MakeServer) -> AppServer:
    return make_server()


@pytest.fixture
def paired(server: AppServer) -> Browser:
    return Browser(server).pair()


# -- the address ----------------------------------------------------------------------


def test_server_listens_on_the_loopback_address_only(server: AppServer) -> None:
    assert server._httpd.socket.getsockname() == ("127.0.0.1", server.port)
    assert server.port > 0
    assert server.url == f"http://127.0.0.1:{server.port}/"


@pytest.mark.parametrize(
    "host",
    [
        "evil.example:{port}",
        "evil.example",
        "127.0.0.1",
        "localhost",
        "127.0.0.1:1",
        "127.0.0.1:{port}.evil.example",
        "localhost.evil.example:{port}",
        "[::1]:{port}",
        "0.0.0.0:{port}",
        "127.0.0.2:{port}",
        "",
    ],
)
@pytest.mark.parametrize(
    ("method", "target"),
    [("GET", "/"), ("GET", "/api/session"), ("GET", "/api/state"), ("POST", "/api/lock")],
)
def test_other_host_names_are_refused(
    host: str, method: str, target: str, paired: Browser, service: FakeService
) -> None:
    answer = paired.call(method, target, headers={"Host": host.format(port=paired.server.port)})
    assert answer.status == 403
    assert answer.error() == "forbidden"
    assert_headers(answer)
    assert INDEX_SERVED not in answer.body
    assert service.calls == []


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LOCALHOST:{port}"])
def test_the_two_loopback_names_are_accepted(host: str, paired: Browser) -> None:
    headers = {"Host": host.format(port=paired.server.port)}
    assert paired.call("GET", "/", headers=headers).status == 200
    assert paired.call("GET", "/api/state", headers=headers).status == 200


def test_a_request_without_host_is_refused(server: AppServer) -> None:
    answer = raw(server, b"GET / HTTP/1.0\r\n\r\n")
    assert answer.status == 403
    assert answer.error() == "forbidden"


def test_a_request_with_two_hosts_is_refused(server: AppServer) -> None:
    host = f"Host: 127.0.0.1:{server.port}"
    answer = raw(server, f"GET / HTTP/1.1\r\n{host}\r\n{host}\r\n\r\n".encode("ascii"))
    assert answer.status == 403


# -- pairing -----------------------------------------------------------------------------


def test_pairing_code_is_eight_unambiguous_characters(make_server: MakeServer) -> None:
    codes = {make_server().pairing_code or "" for _ in range(4)}
    assert len(codes) == 4
    for code in codes:
        assert re.fullmatch(r"[ABCDEFGHJKMNPQRSTVWXYZ2-9]{4}-[ABCDEFGHJKMNPQRSTVWXYZ2-9]{4}", code)
    assert httpd.PAIRING_ALPHABET == "ABCDEFGHJKMNPQRSTVWXYZ23456789"


def test_session_endpoint_answers_before_pairing(server: AppServer, service: FakeService) -> None:
    answer = Browser(server).call("GET", "/api/session")
    assert answer.status == 200
    assert answer.json() == {
        "paired": False,
        "pairing": "code",
        "demo": False,
        "version": VERSION,
        "name": PRODUCT,
    }
    assert_headers(answer)
    assert service.calls == []


def test_the_right_code_pairs(server: AppServer, service: FakeService) -> None:
    browser = Browser(server)
    answer = browser.call("POST", "/api/session/pair", json_body={"code": server.pairing_code})
    assert answer.status == 200
    assert answer.json() == {"paired": True, "session": server._app.token}
    assert browser.call("GET", "/api/session").json()["paired"] is True
    assert browser.call("GET", "/api/state").json() == {"did": "state"}
    assert service.calls == [("state", {})]


@pytest.mark.parametrize(
    "typed",
    [
        lambda code: code.lower(),
        lambda code: code.replace("-", ""),
        lambda code: " ".join(code.replace("-", "")),
        lambda code: f"  {code}\t\n",
        lambda code: code.replace("-", " - ").swapcase(),
        lambda code: "-".join(code.replace("-", "")),
    ],
)
def test_hyphen_spaces_and_case_of_the_code_are_ignored(typed: Any, server: AppServer) -> None:
    code = server.pairing_code
    assert code is not None
    answer = Browser(server).call("POST", "/api/session/pair", json_body={"code": typed(code)})
    assert answer.status == 200
    assert answer.json() == {"paired": True, "session": server._app.token}


def test_the_session_is_a_random_token_and_never_a_cookie(make_server: MakeServer) -> None:
    # A browser sends the cookies of 127.0.0.1 to every port of that address, so a cookie
    # would hand the session to a page of any other program on the computer.
    tokens = set()
    for _ in range(2):
        server = make_server()
        body = json.dumps({"code": server.pairing_code}).encode("ascii")
        lines = ("X-WDVA-Gate: 1", f"Content-Length: {len(body)}")
        answer = raw(server, head(server, "POST", "/api/session/pair", *lines) + body)
        assert answer.status == 200
        assert not [name for name, _ in answer.headers if name.lower() == "set-cookie"]
        token = answer.json()["session"]
        assert re.fullmatch(r"[0-9a-f]{64}", token)  # 32 random bytes
        tokens.add(token)
    assert len(tokens) == 2


def test_no_answer_sets_a_cookie(server: AppServer, service: FakeService) -> None:
    paired = Browser(server).pair()
    service.error = None
    requests = [
        head(server, "GET", "/"),
        head(server, "GET", "/index.html"),
        head(server, "GET", "/app.js"),
        head(server, "GET", "/api/session"),
        head(server, "GET", "/api/session", paired.session_line),
        head(server, "GET", "/api/state"),
        head(server, "GET", "/api/state", paired.session_line),
        head(server, "POST", "/api/lock", paired.session_line, "X-WDVA-Gate: 1"),
        head(server, "GET", "/nothing"),
    ]
    for request in requests:
        answer = raw(server, request)
        assert not [name for name, _ in answer.headers if name.lower() == "set-cookie"], request


def test_no_secret_is_part_of_the_address(server: AppServer, paired: Browser) -> None:
    code = server.pairing_code
    assert code is not None
    assert paired.token is not None
    token = paired.token
    for text in (server.url, repr(server)):
        assert code not in text
        assert code.replace("-", "") not in text
        assert token not in text


def wrong_code(server: AppServer) -> str:
    """A well-formed code that is not the server's."""
    code = server.pairing_code
    assert code is not None
    return ("A" if code[0] != "A" else "B") + code[1:]


def test_a_wrong_code_does_not_pair(server: AppServer, service: FakeService) -> None:
    browser = Browser(server)
    code = server.pairing_code
    assert code is not None
    for typed in (wrong_code(server), "", code[:-1], code + "A"):
        answer = browser.call("POST", "/api/session/pair", json_body={"code": typed})
        assert answer.status == 403
        assert answer.error() == "wrong_code"
        assert server._app.token not in answer.body.decode()
    assert browser.token is None
    assert browser.call("GET", "/api/state").status == 401
    assert service.calls == []


def test_five_wrong_codes_switch_pairing_off(server: AppServer, service: FakeService) -> None:
    earlier = Browser(server).pair()
    browser = Browser(server)
    for _ in range(4):
        answer = browser.call("POST", "/api/session/pair", json_body={"code": wrong_code(server)})
        assert (answer.status, answer.error()) == (403, "wrong_code")
    answer = browser.call("POST", "/api/session/pair", json_body={"code": wrong_code(server)})
    assert (answer.status, answer.error()) == (429, "pairing_disabled")
    # From now on the right code is refused too, for everyone, until the app restarts.
    for client in (browser, Browser(server)):
        answer = client.call("POST", "/api/session/pair", json_body={"code": server.pairing_code})
        assert (answer.status, answer.error()) == (429, "pairing_disabled")
        assert server._app.token not in answer.body.decode()
        assert client.call("GET", "/api/state").status == 401
    # A browser that paired before stays paired.
    assert earlier.call("GET", "/api/state").status == 200
    assert service.calls == [("state", {})]


def test_four_wrong_codes_leave_pairing_on(server: AppServer) -> None:
    browser = Browser(server)
    for _ in range(4):
        browser.call("POST", "/api/session/pair", json_body={"code": wrong_code(server)})
    browser.pair()
    assert browser.call("GET", "/api/state").status == 200


@pytest.mark.parametrize("body", [{}, {"code": 12345678}, {"code": None}, {"code": "x", "more": 1}])
def test_a_malformed_pairing_request_is_not_a_wrong_code(body: Any, server: AppServer) -> None:
    browser = Browser(server)
    for _ in range(6):
        answer = browser.call("POST", "/api/session/pair", json_body=body)
        assert (answer.status, answer.error()) == (400, "invalid")
    browser.pair()


ENDPOINTS: list[tuple[str, str, Any, str, dict[str, Any]]] = [
    ("GET", "/api/state", None, "state", {}),
    (
        "POST",
        "/api/store/create",
        {"passphrase": "correct horse battery"},
        "create_store",
        {"passphrase": "correct horse battery"},
    ),
    (
        "POST",
        "/api/unlock",
        {"passphrase": "pass phrase", "hours": 8},
        "unlock",
        {"passphrase": "pass phrase", "hours": 8},
    ),
    (
        "POST",
        "/api/unlock",
        {"passphrase": "pass phrase", "hours": 0.5},
        "unlock",
        {"passphrase": "pass phrase", "hours": 0.5},
    ),
    (
        "POST",
        "/api/unlock",
        {"passphrase": "Zażółć gęślą"},
        "unlock",
        {"passphrase": "Zażółć gęślą", "hours": None},
    ),
    ("POST", "/api/lock", {}, "lock", {}),
    ("POST", "/api/lock", None, "lock", {}),
    ("POST", "/api/reauth", {"passphrase": "pass phrase"}, "reauth", {"passphrase": "pass phrase"}),
    (
        "POST",
        "/api/serving",
        {"kinds": "both", "paused": ["lm-studio"]},
        "set_serving",
        {"kinds": "both", "paused": ["lm-studio"]},
    ),
    ("POST", "/api/serving", {"paused": []}, "set_serving", {"kinds": None, "paused": []}),
    ("POST", "/api/serving", {}, "set_serving", {"kinds": None, "paused": None}),
    ("POST", "/api/verify", {}, "verify", {}),
    (
        "POST",
        "/api/interface",
        {"view": "basic"},
        "set_interface",
        {"view": "basic", "setup": None},
    ),
    ("POST", "/api/interface", {"setup": "done"}, "set_interface", {"view": None, "setup": "done"}),
    (
        "POST",
        "/api/interface",
        {"view": "pro", "setup": "done"},
        "set_interface",
        {"view": "pro", "setup": "done"},
    ),
    # A body without a field arrives at the service, which is the one to refuse it.
    ("POST", "/api/interface", {}, "set_interface", {"view": None, "setup": None}),
    (
        "GET",
        "/api/activity?limit=50&before=1188&app=claude-desktop&kind=release",
        None,
        "activity",
        {"limit": 50, "before": 1188, "app": "claude-desktop", "kind": "release"},
    ),
    (
        "GET",
        "/api/activity",
        None,
        "activity",
        {"limit": None, "before": None, "app": None, "kind": None},
    ),
    (
        "GET",
        "/api/activity?app=&kind=refusal",
        None,
        "activity",
        {"limit": None, "before": None, "app": None, "kind": "refusal"},
    ),
    ("GET", "/api/activity/1237", None, "activity_detail", {"seq": 1237}),
    ("GET", "/api/approvals", None, "approvals", {}),
    (
        "POST",
        "/api/approvals/release",
        {"app": "lm-studio", "ref": "d-88b0e1aa9c11.1", "body_sha256": "5f1c"},
        "release",
        {"app": "lm-studio", "ref": "d-88b0e1aa9c11.1", "body_sha256": "5f1c"},
    ),
    (
        "POST",
        "/api/approvals/dismiss",
        {"app": "lm-studio", "ref": "d-88b0e1aa9c11.1"},
        "dismiss",
        {"app": "lm-studio", "ref": "d-88b0e1aa9c11.1"},
    ),
    ("GET", "/api/documents", None, "documents", {}),
    (
        "POST",
        "/api/documents?collection=home&name=lease-2025.pdf",
        b"%PDF-1.4 \x00\xff not JSON, not text",
        "add_document",
        {
            "collection": "home",
            "name": "lease-2025.pdf",
            "data": b"%PDF-1.4 \x00\xff not JSON, not text",
        },
    ),
    (
        "POST",
        "/api/documents?name=Miete%20%C3%9Cbersicht+(2025)%26mehr.txt&collection=home",
        b"Miete",
        "add_document",
        {"collection": "home", "name": "Miete Übersicht (2025)&mehr.txt", "data": b"Miete"},
    ),
    (
        "POST",
        "/api/documents?collection=home&name=empty.txt",
        None,
        "add_document",
        {"collection": "home", "name": "empty.txt", "data": b""},
    ),
    ("DELETE", "/api/documents/d-3f9a2c41b07e", None, "remove_document", {"id": "d-3f9a2c41b07e"}),
    (
        "POST",
        "/api/collections",
        {"name": "home", "note": "household papers", "allow_hosted": True, "show_titles": False},
        "set_collection",
        {"name": "home", "note": "household papers", "allow_hosted": True, "show_titles": False},
    ),
    (
        "POST",
        "/api/collections",
        {"name": "home", "note": None},
        "set_collection",
        {"name": "home", "note": None, "allow_hosted": None, "show_titles": None},
    ),
    ("DELETE", "/api/collections/home", None, "remove_collection", {"name": "home"}),
    ("GET", "/api/apps", None, "apps", {}),
    (
        "POST",
        "/api/apps",
        {"name": "claude-desktop", "hosted": True, "destination": "Anthropic"},
        "add_app",
        {"name": "claude-desktop", "hosted": True, "destination": "Anthropic"},
    ),
    (
        "POST",
        "/api/apps",
        {"name": "lm-studio", "hosted": False},
        "add_app",
        {"name": "lm-studio", "hosted": False, "destination": None},
    ),
    ("DELETE", "/api/apps/claude-desktop", None, "remove_app", {"name": "claude-desktop"}),
    (
        "POST",
        "/api/grants",
        {"app": "claude-desktop", "collection": "home", "mode": "read"},
        "set_grant",
        {"app": "claude-desktop", "collection": "home", "mode": "read"},
    ),
    (
        "POST",
        "/api/apps/claude-desktop/limits",
        {"chars_per_day": 30000},
        "set_app_limits",
        {"name": "claude-desktop", "limits": {"chars_per_day": 30000}},
    ),
    (
        "GET",
        "/api/apps/claude-desktop/connect?host=claude-code",
        None,
        "connect",
        {"name": "claude-desktop", "host": "claude-code"},
    ),
    ("GET", "/api/apps/lm-studio/connect", None, "connect", {"name": "lm-studio", "host": None}),
    (
        "POST",
        "/api/apps/claude-desktop/connect/check",
        {"host": "claude-desktop"},
        "connect_check",
        {"name": "claude-desktop", "host": "claude-desktop"},
    ),
    (
        "POST",
        "/api/apps/lm-studio/connect/check",
        {},
        "connect_check",
        {"name": "lm-studio", "host": None},
    ),
    (
        "POST",
        "/api/apps/lm-studio/connect/check",
        None,
        "connect_check",
        {"name": "lm-studio", "host": None},
    ),
    ("GET", "/api/settings", None, "settings", {}),
    (
        "POST",
        "/api/settings",
        {"defaults": {"chars_per_day": 30000}, "approval": {"ttl_minutes": 5}},
        "set_settings",
        {
            "defaults": {"chars_per_day": 30000},
            "groups": None,
            "per_document": None,
            "approval": {"ttl_minutes": 5},
        },
    ),
    (
        "POST",
        "/api/replaced-strings",
        {"text": "Kowalski"},
        "add_replaced_string",
        {"text": "Kowalski"},
    ),
    ("POST", "/api/demo/again", None, "demo_again", {}),
    ("POST", "/api/demo/again", {}, "demo_again", {}),
]


def send(browser: Browser, method: str, target: str, body: Any, **options: Any) -> Answer:
    if isinstance(body, bytes):
        return browser.call(method, target, body=body, **options)
    return browser.call(method, target, json_body=body, **options)


def test_the_fake_service_has_exactly_the_methods_of_the_service() -> None:
    fake = {name for name in vars(FakeService) if not name.startswith("_")}
    assert fake == SERVICE_METHODS
    assert {name for _, _, _, name, _ in ENDPOINTS} == SERVICE_METHODS


@pytest.mark.parametrize(("method", "target", "body", "name", "arguments"), ENDPOINTS)
def test_every_endpoint_calls_its_service_method(
    method: str,
    target: str,
    body: Any,
    name: str,
    arguments: dict[str, Any],
    paired: Browser,
    service: FakeService,
) -> None:
    answer = send(paired, method, target, body)
    assert answer.status == 200, answer.body
    assert answer.json() == {"did": name}
    assert answer.header("Cache-Control") == "no-store"
    assert_headers(answer)
    assert service.calls == [(name, arguments)]


@pytest.mark.parametrize(("method", "target", "body", "name", "arguments"), ENDPOINTS)
def test_every_endpoint_needs_pairing(
    method: str,
    target: str,
    body: Any,
    name: str,
    arguments: dict[str, Any],
    server: AppServer,
    service: FakeService,
) -> None:
    for browser in (Browser(server), Browser(server)):
        answer = send(browser, method, target, body)
        assert answer.status == 401
        assert answer.error() == "pairing_required"
        assert_headers(answer)
        browser.token = "0" * 64  # a guess, the second round
    assert service.calls == []


def test_tokens_that_are_not_the_session_are_refused(
    server: AppServer, make_server: MakeServer, paired: Browser, service: FakeService
) -> None:
    token = paired.token
    assert token is not None
    other = Browser(make_server()).pair()
    guesses = [
        "",
        token[:-1],
        token + "0",
        token.upper(),
        f"Bearer {token}",
        f"{token}, {token}",
        other.token or "",
        "\xe9",
    ]
    for guess in guesses:
        browser = Browser(server)
        browser.token = guess
        assert browser.call("GET", "/api/state").status == 401, guess
        assert browser.call("GET", "/api/session").json()["paired"] is False
    # The token shown twice is not the token shown once.
    twice = raw(server, head(server, "GET", "/api/state", paired.session_line, paired.session_line))
    assert twice.status == 401
    assert service.calls == []
    assert paired.call("GET", "/api/state").status == 200


def test_the_session_in_a_cookie_counts_for_nothing(
    server: AppServer, paired: Browser, service: FakeService
) -> None:
    # What a browser would send by itself, also to other ports, is never the session.
    token = paired.token
    port = server.port
    names = [f"{SLUG.replace('-', '_')}_{port}", "session", SESSION_HEADER, "X-WDVA-Gate"]
    for name in names:
        for line in (f"Cookie: {name}={token}", f"Cookie: a=b; {name}={token}"):
            answer = raw(server, head(server, "GET", "/api/state", line))
            assert (answer.status, answer.error()) == (401, "pairing_required"), line
            answer = raw(server, head(server, "GET", "/api/session", line))
            assert answer.json()["paired"] is False
    assert raw(server, head(server, "GET", f"/api/state?session={token}")).status == 401
    assert service.calls == []


def test_unknown_api_paths_tell_an_unpaired_caller_nothing(server: AppServer) -> None:
    for target in ("/api/nothing", "/api/", "/api/state/"):
        assert Browser(server).call("GET", target).status == 401


# -- without pairing -------------------------------------------------------------------


def test_demo_mode_has_no_pairing(make_server: MakeServer, service: FakeService) -> None:
    server = make_server(pairing=False, demo=True)
    assert server.pairing_code is None
    browser = Browser(server)
    # The page itself carries nothing, and without the session the API is closed.
    page = raw(server, head(server, "GET", "/"))
    assert page.body == INDEX_SERVED
    assert server._app.token not in page.body.decode()
    assert raw(server, head(server, "GET", "/api/state")).status == 401
    # The session endpoint hands the session to whoever asks: every document is invented.
    session = browser.call("GET", "/api/session")
    assert session.json() == {
        "paired": False,
        "pairing": "none",
        "demo": True,
        "version": VERSION,
        "name": PRODUCT,
        "session": server._app.token,
    }
    assert browser.call("GET", "/api/session").json()["paired"] is True
    assert browser.call("GET", "/api/state").status == 200
    assert Browser(server).call("GET", "/api/session").json()["session"] == server._app.token
    # A page of another site cannot ask for it: the browser says where the request is from.
    for site in ("cross-site", "same-site"):
        answer = raw(server, head(server, "GET", "/api/session", f"Sec-Fetch-Site: {site}"))
        assert answer.status == 403
        assert server._app.token not in answer.body.decode()
    # There is no code to try.
    answer = Browser(server).call("POST", "/api/session/pair", json_body={"code": "ABCD-EFGH"})
    assert (answer.status, answer.error()) == (404, "not_found")
    assert service.calls == [("state", {})]


def test_only_a_demo_runs_without_pairing(service: FakeService, static_dir: Path) -> None:
    # Without pairing any program on the computer is given the session. That is accepted
    # for invented documents and for nothing else, so no other server can be made that way.
    with pytest.raises(ValueError, match="demo"):
        AppServer(service, pairing=False, version=VERSION, static_dir=static_dir)
    with pytest.raises(ValueError, match="demo"):
        AppServer(service, pairing=False, demo=False, version=VERSION, static_dir=static_dir)


def test_with_pairing_nothing_but_the_code_gives_the_session(
    server: AppServer, service: FakeService
) -> None:
    token = server._app.token
    first = Browser(server)
    # Not the first load of the page, nor a later one, nor the session endpoint.
    for target in ("/", "/index.html", "/app.js", "/", "/api/session", "/api/state"):
        answer = first.call("GET", target)
        assert token not in answer.body.decode()
        assert token not in json.dumps(answer.headers)
    assert first.token is None
    assert "session" not in first.call("GET", "/api/session").json()
    assert first.call("GET", "/api/state").status == 401
    assert service.calls == []


def test_demo_flag_alone_does_not_switch_pairing_off(make_server: MakeServer) -> None:
    server = make_server(demo=True)
    assert server.pairing_code is not None
    browser = Browser(server)
    session = browser.call("GET", "/api/session").json()
    assert session["pairing"] == "code"
    assert "session" not in session
    assert browser.token is None


# -- requests from other sites -------------------------------------------------------


CHANGING = [(m, t, b) for m, t, b, _, _ in ENDPOINTS if m in ("POST", "DELETE")]


@pytest.mark.parametrize(("method", "target", "body"), CHANGING)
def test_changing_requests_need_the_gate_header(
    method: str, target: str, body: Any, paired: Browser, service: FakeService
) -> None:
    answer = send(paired, method, target, body, gate_header=False)
    assert answer.status == 403
    assert answer.error() == "forbidden"
    assert_headers(answer)
    assert service.calls == []


@pytest.mark.parametrize("value", ["0", "true", "", "11", "1, 1"])
def test_the_gate_header_must_be_exactly_one(
    value: str, paired: Browser, service: FakeService
) -> None:
    answer = paired.call("POST", "/api/lock", json_body={}, headers={"X-WDVA-Gate": value})
    assert answer.status == 403
    assert service.calls == []


def test_pairing_needs_the_gate_header_too(server: AppServer) -> None:
    browser = Browser(server)
    answer = browser.call(
        "POST", "/api/session/pair", json_body={"code": server.pairing_code}, gate_header=False
    )
    assert (answer.status, answer.error()) == (403, "forbidden")
    assert browser.token is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://evil.example:{port}",
        "http://127.0.0.1",
        "http://127.0.0.1:1",
        "https://127.0.0.1:{port}",
        "http://localhost:{port}",  # another name than the one the request was sent to
        "http://127.0.0.1:{port}.evil.example",
        "null",
        "",
    ],
)
@pytest.mark.parametrize(
    ("method", "target"),
    [("POST", "/api/lock"), ("DELETE", "/api/apps/claude-desktop"), ("GET", "/api/state")],
)
def test_a_foreign_origin_is_refused(
    origin: str, method: str, target: str, paired: Browser, service: FakeService
) -> None:
    headers = {"Origin": origin.format(port=paired.server.port)}
    answer = paired.call(method, target, headers=headers)
    assert answer.status == 403
    assert answer.error() == "forbidden"
    assert_headers(answer)
    assert service.calls == []


def test_the_apps_own_origin_is_accepted(paired: Browser, service: FakeService) -> None:
    port = paired.server.port
    for name in ("127.0.0.1", "localhost"):
        headers = {"Host": f"{name}:{port}", "Origin": f"http://{name}:{port}"}
        assert paired.call("POST", "/api/lock", json_body={}, headers=headers).status == 200
    assert [name for name, _ in service.calls] == ["lock", "lock"]


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_api_calls_marked_by_the_browser_as_coming_from_elsewhere_are_refused(
    site: str, paired: Browser, service: FakeService
) -> None:
    for method, target in (("GET", "/api/state"), ("POST", "/api/lock")):
        answer = paired.call(method, target, headers={"Sec-Fetch-Site": site})
        assert (answer.status, answer.error()) == (403, "forbidden")
    assert service.calls == []
    # A link from elsewhere may still open the page itself.
    assert paired.call("GET", "/", headers={"Sec-Fetch-Site": site}).status == 200


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_api_calls_marked_as_the_apps_own_pass(site: str, paired: Browser) -> None:
    assert paired.call("GET", "/api/state", headers={"Sec-Fetch-Site": site}).status == 200


def test_no_response_permits_another_site_to_read_it(
    server: AppServer, paired: Browser, service: FakeService
) -> None:
    asking = {
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": f"x-{SLUG}, content-type",
    }
    preflight = paired.call("OPTIONS", "/api/lock", headers=asking)
    assert preflight.status == 405
    answers = [
        preflight,
        paired.call("GET", "/", headers={"Origin": "https://evil.example"}),
        paired.call("GET", "/api/state", headers={"Origin": "https://evil.example"}),
        paired.call("GET", "/api/state", headers={"Origin": f"http://127.0.0.1:{server.port}"}),
        paired.call("GET", "/api/state"),
        paired.call("GET", "/app.js"),
        paired.call("POST", "/api/lock", json_body={}),
    ]
    for answer in answers:
        assert_headers(answer)
        names = {name.lower() for name, _ in answer.headers}
        assert not {name for name in names if "allow-origin" in name or "access-control" in name}
    assert [name for name, _ in service.calls] == ["state", "state", "lock"]


@pytest.mark.parametrize("method", ["PUT", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"])
def test_other_methods_are_refused(method: str, paired: Browser, service: FakeService) -> None:
    for target in ("/api/state", "/api/lock", "/"):
        answer = paired.call(method, target, headers={"X-WDVA-Gate": "1"})
        assert answer.status in (405, 501)
        assert answer.header("Content-Security-Policy") == CSP
        if method != "HEAD":
            assert answer.error() == "invalid"
    assert service.calls == []


# -- headers ---------------------------------------------------------------------------


def test_the_page_is_served_with_the_product_name_and_nothing_else_is_changed(
    tmp_path: Path, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A name that would break out of an attribute or start a tag if it were not escaped.
    monkeypatch.setattr(brand, "PRODUCT", 'Fish & <Chips> "Desk"')
    directory = tmp_path / "named"
    directory.mkdir()
    (directory / "index.html").write_text(
        '<!doctype html><meta name="application-name" content="{{product}}">'
        "<title>{{product}}</title><p>{{product}} needs JavaScript.</p>",
        encoding="utf-8",
    )
    (directory / "app.js").write_bytes(b'const untouched = "{{product}}";')
    server = AppServer(service, version=VERSION, static_dir=directory)
    server.start()
    try:
        browser = Browser(server)
        escaped = "Fish &amp; &lt;Chips&gt; &quot;Desk&quot;"
        for target in ("/", "/index.html"):
            page = browser.call("GET", target).body.decode("utf-8")
            assert page == (
                f'<!doctype html><meta name="application-name" content="{escaped}">'
                f"<title>{escaped}</title><p>{escaped} needs JavaScript.</p>"
            )
        assert browser.call("GET", "/app.js").body == b'const untouched = "{{product}}";'
        assert browser.call("GET", "/api/session").json()["name"] == 'Fish & <Chips> "Desk"'
    finally:
        server.stop()


def test_the_shipped_page_shows_the_name_from_its_first_byte(service: FakeService) -> None:
    server = AppServer(service, pairing=True, version=VERSION)
    server.start()
    try:
        page = Browser(server).call("GET", "/").body.decode("utf-8")
    finally:
        server.stop()
    assert "{{" not in page
    assert "}}" not in page
    assert f"<title>{PRODUCT}</title>" in page
    assert f'<meta name="application-name" content="{PRODUCT}">' in page
    assert f'<p class="boot-name">{PRODUCT}</p>' in page
    # The scripts take the name from the page: none of them spells it out.
    static = Path(httpd.__file__).resolve().parent / "static"
    assert "{{product}}" in (static / "index.html").read_text(encoding="utf-8")
    for script in static.glob("*.js"):
        assert PRODUCT not in script.read_text(encoding="utf-8"), script.name


def test_the_content_security_policy_is_the_strict_one() -> None:
    assert httpd.CONTENT_SECURITY_POLICY == CSP


@pytest.mark.parametrize(
    ("target", "content_type", "body"),
    [
        ("/", "text/html; charset=utf-8", INDEX_SERVED),
        ("/index.html", "text/html; charset=utf-8", INDEX_SERVED),
        ("/app.js", "text/javascript; charset=utf-8", APP_JS),
        ("/app.js?v=3", "text/javascript; charset=utf-8", APP_JS),
        ("/style.css", "text/css; charset=utf-8", b"body { margin: 0 }"),
        ("/icon.svg", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
    ],
)
def test_static_files_and_their_headers(
    target: str, content_type: str, body: bytes, server: AppServer, service: FakeService
) -> None:
    answer = Browser(server).call("GET", target)  # no pairing needed for the page itself
    assert answer.status == 200
    assert answer.body == body
    assert answer.header("Content-Type") == content_type
    assert answer.header("Cache-Control") == "no-store"
    assert_headers(answer)
    assert service.calls == []


def test_error_responses_carry_the_headers_too(server: AppServer, paired: Browser) -> None:
    answers = [
        Browser(server).call("GET", "/api/state"),  # 401
        paired.call("GET", "/api/nothing"),  # 404
        paired.call("GET", "/nothing.html"),  # 404
        paired.call("POST", "/api/lock", gate_header=False),  # 403
        paired.call("POST", "/api/unlock", body=b"{"),  # 400
        raw(server, b"GARBAGE\r\n\r\n"),  # 400, answered before any header was read
        raw(server, b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n"),  # 414
    ]
    assert [answer.status for answer in answers] == [401, 404, 404, 403, 400, 400, 414]
    for answer in answers:
        assert_headers(answer)
        assert answer.header("Cache-Control") == "no-store"
        assert answer.error() in {
            "pairing_required",
            "not_found",
            "forbidden",
            "invalid",
            "too_large",
        }


def test_the_server_does_not_say_what_it_runs_on(server: AppServer) -> None:
    answer = Browser(server).call("GET", "/")
    assert answer.header("Server") == PRODUCT.replace(" ", "-")


# -- static files ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "/../secret.txt",
        "/..%2fsecret.txt",
        "/..%2Fsecret.txt",
        "/%2e%2e/secret.txt",
        "/%2e%2e%2fsecret.txt",
        "/.%2e/secret.txt",
        "/static/../../secret.txt",
        "/app.js/../../secret.txt",
        "/sub/../../secret.txt",
        "//secret.txt",
        "/..\\secret.txt",
        "/..%5csecret.txt",
        "/%252e%252e/secret.txt",
        "/..;/secret.txt",
        "/secret.txt",
        "/{secret}",
        "/{secret_encoded}",
        "/file://{secret}",
        "/link.txt",
        "/sub/inner.js",
        "/sub%2finner.js",
        "/inner.js",
        "/.hidden.js",
        "/notes.md",
        "/%61pp.js",
        "/APP.JS",
        "/app.js%00.html",
        "/app.js/",
        "/index.html/",
        "/./app.js",
        "app.js",
        "http://evil.example/app.js",
        "/static/app.js",
    ],
)
def test_only_exact_names_from_the_static_directory_are_served(
    target: str, static_dir: Path, paired: Browser
) -> None:
    secret = str(static_dir.parent / "secret.txt")
    target = target.format(secret=secret.lstrip("/"), secret_encoded=secret.replace("/", "%2f"))
    host = {"Host": f"127.0.0.1:{paired.server.port}"}
    for method in ("GET", "POST"):
        answer = paired.call(method, target, headers=host)
        assert answer.status == 404, target
        assert answer.error() == "not_found"
        assert SECRET.encode() not in answer.body
        assert_headers(answer)


def test_the_static_table_is_built_once(server: AppServer, static_dir: Path) -> None:
    (static_dir / "late.js").write_bytes(b"added while the server runs")
    (static_dir / "app.js").write_bytes(b"changed while the server runs")
    browser = Browser(server)
    assert browser.call("GET", "/late.js").status == 404
    assert browser.call("GET", "/app.js").body == APP_JS


def test_a_missing_static_directory_serves_nothing(make_server: MakeServer, tmp_path: Path) -> None:
    server = make_server(static_dir=tmp_path / "does-not-exist")
    assert Browser(server).call("GET", "/").status == 404
    assert Browser(server).call("GET", "/api/session").status == 200


def test_changing_methods_on_static_paths_change_nothing(paired: Browser) -> None:
    assert paired.call("POST", "/app.js", gate_header=False).status == 403
    assert paired.call("DELETE", "/app.js").status == 404
    assert paired.call("GET", "/app.js").body == APP_JS


# -- paths and arguments ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("GET", "/api/nothing"),
        ("GET", "/api/"),
        ("GET", "/api"),
        ("GET", "/api/state/"),
        ("GET", "/api//state"),
        ("GET", "/api/state/extra"),
        ("GET", "/API/state"),
        ("GET", "/api/lock"),
        ("POST", "/api/state"),
        ("DELETE", "/api/apps"),
        ("DELETE", "/api/state"),
        ("GET", "/api/activity/12x"),
        ("GET", "/api/activity/-1"),
        ("GET", "/api/activity/1.5"),
        ("GET", "/api/activity/9999999999999999"),
        ("DELETE", "/api/documents/a%2Fb"),
        ("DELETE", "/api/documents/.."),
        ("DELETE", "/api/documents/"),
        ("DELETE", "/api/apps/claude%20desktop"),
        ("DELETE", "/api/apps/" + "a" * 65),
        ("GET", "/api/apps/../state"),
        ("GET", "/api/apps/claude-desktop/connect/"),
        ("POST", "/api/apps/claude-desktop/limit"),
        # The three newer addresses answer the one method they were given, and no neighbour.
        ("GET", "/api/interface"),
        ("DELETE", "/api/interface"),
        ("POST", "/api/interface/view"),
        ("GET", "/api/apps/claude-desktop/connect/check"),
        ("POST", "/api/apps/claude-desktop/connect"),
        ("POST", "/api/apps/claude-desktop/connect/check/"),
        ("POST", "/api/apps/claude-desktop/connect/check/file"),
        ("POST", "/api/apps/..%2F..%2Fetc/connect/check"),
        ("GET", "/api/demo/again"),
        ("POST", "/api/demo"),
        ("POST", "/api/demo/again/now"),
    ],
)
def test_unknown_paths_are_not_found(
    method: str, target: str, paired: Browser, service: FakeService
) -> None:
    answer = paired.call(method, target)
    assert answer.status == 404, target
    assert answer.error() == "not_found"
    assert service.calls == []


@pytest.mark.parametrize(
    "target",
    [
        "/api/activity?limit=abc",
        "/api/activity?limit=-5",
        "/api/activity?limit=5.0",
        "/api/activity?limit=5&limit=6",
        "/api/activity?before=1e3",
        "/api/activity?unknown=1",
        "/api/state?cursor=1",
        "/api/apps/claude-desktop/connect?host=a&host=b",
        "/api/apps/claude-desktop/connect?path=%2Fetc%2Fpasswd",
        "/api/activity?app=%ff",
    ],
)
def test_query_strings_that_do_not_fit_are_invalid(
    target: str, paired: Browser, service: FakeService
) -> None:
    answer = paired.call("GET", target)
    assert (answer.status, answer.error()) == (400, "invalid")
    assert service.calls == []


@pytest.mark.parametrize(
    "target",
    [
        "/api/interface?view=pro",
        "/api/interface?setup=done",
        "/api/apps/claude-desktop/connect/check?host=codex",
        "/api/apps/claude-desktop/connect/check?path=%2Fetc%2Fpasswd",
        "/api/demo/again?app=claude-desktop",
    ],
)
def test_the_newer_addresses_take_nothing_from_the_address(
    target: str, paired: Browser, service: FakeService
) -> None:
    # What these three take, they take from the body: a query is refused, not ignored.
    answer = paired.call("POST", target, json_body={})
    assert (answer.status, answer.error()) == (400, "invalid")
    assert service.calls == []


def test_an_address_that_is_not_ascii_is_invalid(paired: Browser, service: FakeService) -> None:
    for target in ("/api/documents?collection=home&name=\xc3\x9cbersicht.txt", "/api/st\xe4te"):
        request = (
            f"POST {target} HTTP/1.1\r\nHost: 127.0.0.1:{paired.server.port}\r\n"
            f"{paired.session_line}\r\nX-WDVA-Gate: 1\r\n\r\n"
        ).encode("latin-1")
        answer = raw(paired.server, request)
        assert (answer.status, answer.error()) == (400, "invalid")
    assert service.calls == []


@pytest.mark.parametrize(
    "target",
    [
        "/api/documents",
        "/api/documents?collection=home",
        "/api/documents?name=a.txt",
        "/api/documents?collection=&name=a.txt",
        "/api/documents?collection=home&name=a.txt&title=x",
    ],
)
def test_an_upload_needs_its_collection_and_name(
    target: str, paired: Browser, service: FakeService
) -> None:
    answer = paired.call("POST", target, body=b"some text")
    assert (answer.status, answer.error()) == (400, "invalid")
    assert service.calls == []


@pytest.mark.parametrize(
    ("target", "body"),
    [
        ("/api/unlock", {}),
        ("/api/unlock", {"hours": 8}),
        ("/api/unlock", {"passphrase": None}),
        ("/api/unlock", {"passphrase": 123456789012}),
        ("/api/unlock", {"passphrase": ["pass phrase"]}),
        ("/api/unlock", {"passphrase": "pass phrase", "hours": "8"}),
        ("/api/unlock", {"passphrase": "pass phrase", "hours": True}),
        ("/api/unlock", {"passphrase": "pass phrase", "remember": True}),
        ("/api/lock", {"force": True}),
        ("/api/serving", {"paused": "lm-studio"}),
        ("/api/serving", {"paused": ["lm-studio", 3]}),
        ("/api/serving", {"kinds": ["hosted"]}),
        ("/api/collections", {"note": "no name"}),
        ("/api/collections", {"name": "home", "allow_hosted": "yes"}),
        ("/api/collections", {"name": "home", "show_titles": 1}),
        ("/api/apps", {"name": "claude-desktop"}),
        ("/api/apps", {"name": "claude-desktop", "hosted": "true"}),
        ("/api/grants", {"app": "claude-desktop", "collection": "home"}),
        ("/api/settings", {"defaults": [1, 2]}),
        ("/api/settings", {"approval": 5}),
        ("/api/replaced-strings", {"text": 4711}),
        ("/api/approvals/release", {"app": "lm-studio", "ref": "d-88b0e1aa9c11.1"}),
        # The view and the word about setup are text, and the two are all there is.
        ("/api/interface", {"view": 1}),
        ("/api/interface", {"view": ["pro"]}),
        ("/api/interface", {"setup": True}),
        ("/api/interface", {"view": "pro", "stored": True}),
        ("/api/interface", {"view": "pro", "path": "/tmp/elsewhere.json"}),
        # The check takes the kind of application and nothing that could name a file.
        ("/api/apps/claude-desktop/connect/check", {"host": ["claude-desktop"]}),
        ("/api/apps/claude-desktop/connect/check", {"host": 7}),
        ("/api/apps/claude-desktop/connect/check", {"path": "/etc/passwd"}),
        ("/api/apps/claude-desktop/connect/check", {"host": "codex", "file": "~/.ssh/id_ed25519"}),
        # Asking the played app again takes nothing at all.
        ("/api/demo/again", {"app": "claude-desktop"}),
        ("/api/demo/again", {"times": 3}),
    ],
)
def test_fields_that_do_not_fit_are_invalid(
    target: str, body: Any, paired: Browser, service: FakeService
) -> None:
    answer = paired.call("POST", target, json_body=body)
    assert (answer.status, answer.error()) == (400, "invalid")
    assert "123456789012" not in answer.body.decode()  # what was sent is not repeated
    assert service.calls == []


# -- bodies ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"not json",
        b'{"passphrase": "pass phrase",}',
        b'["pass phrase"]',
        b'"pass phrase"',
        b"null",
        b'{"passphrase": "one", "passphrase": "two"}',
        b'{"passphrase": "pass phrase", "hours": NaN}',
        b'{"passphrase": "pass phrase", "hours": 1e999}',
        b'{"passphrase": "\xff"}',
        b'{"passphrase": "half a pair \\ud800"}',
        b'{"passphrase": "pass phrase", "\\udc00": 1}',
        b" ",
    ],
)
def test_malformed_json_is_invalid(body: bytes, paired: Browser, service: FakeService) -> None:
    answer = paired.call("POST", "/api/unlock", body=body)
    assert (answer.status, answer.error()) == (400, "invalid")
    assert service.calls == []


def unlock_body(size: int) -> bytes:
    frame = len(json.dumps({"passphrase": ""}))
    body = json.dumps({"passphrase": "x" * (size - frame)}).encode("ascii")
    assert len(body) == size
    return body


def test_json_up_to_the_limit_is_read(paired: Browser, service: FakeService) -> None:
    assert httpd.MAX_JSON_BYTES == 64 * 1024
    assert paired.call("POST", "/api/unlock", body=unlock_body(64 * 1024)).status == 200
    assert len(service.calls) == 1


def test_json_over_the_limit_is_refused(paired: Browser, service: FakeService) -> None:
    answer = paired.call("POST", "/api/unlock", body=unlock_body(64 * 1024 + 1))
    assert (answer.status, answer.error()) == (413, "too_large")
    assert_headers(answer)
    assert service.calls == []


def test_an_upload_up_to_the_limit_arrives_whole(paired: Browser, service: FakeService) -> None:
    data = bytes(range(256)) * (20 * 1024 * 1024 // 256)
    assert len(data) == 20 * 1024 * 1024
    answer = paired.call("POST", "/api/documents?collection=home&name=big.txt", body=data)
    assert answer.status == 200
    ((name, arguments),) = service.calls
    assert name == "add_document"
    assert hashlib.sha256(arguments["data"]).digest() == hashlib.sha256(data).digest()


def test_an_upload_over_the_limit_that_is_sent_anyway_still_gets_its_answer(
    paired: Browser, service: FakeService
) -> None:
    data = bytes(20 * 1024 * 1024 + 1)
    answer = paired.call("POST", "/api/documents?collection=home&name=big.txt", body=data)
    assert (answer.status, answer.error()) == (413, "too_large")
    assert_headers(answer)
    assert service.calls == []


def announce(paired: Browser, target: str, length: int, *lines: str) -> bytes:
    """A POST that announces a body of ``length`` bytes and then sends none of it."""
    return head(
        paired.server,
        "POST",
        target,
        paired.session_line,
        "X-WDVA-Gate: 1",
        f"Content-Length: {length}",
        *lines,
    )


@pytest.mark.parametrize(
    ("target", "length"),
    [
        ("/api/unlock", 64 * 1024 + 1),
        ("/api/session/pair", 64 * 1024 + 1),
        ("/api/apps/claude-desktop/limits", 10**9),
        ("/api/documents?collection=home&name=a.pdf", 20 * 1024 * 1024 + 1),
        ("/api/documents?collection=home&name=a.pdf", 999999999999),
    ],
)
def test_a_body_over_the_limit_is_refused_without_being_read(
    target: str, length: int, paired: Browser, service: FakeService
) -> None:
    started = time.monotonic()
    # No byte of the body is ever sent: a server that tried to read it would not answer.
    answer = raw(paired.server, announce(paired, target, length))
    assert (answer.status, answer.error()) == (413, "too_large")
    assert time.monotonic() - started < 4
    assert service.calls == []


@pytest.mark.parametrize(
    ("lines", "status"),
    [
        (["X-WDVA-Gate: 1", "Content-Length: 1000"], 401),  # no session
        (["{session}", "Content-Length: 1000"], 403),  # no gate header
        (
            [
                "{session}",
                "X-WDVA-Gate: 1",
                "Origin: https://evil.example",
                "Content-Length: 1000",
            ],
            403,
        ),
        (
            [
                "{session}",
                "X-WDVA-Gate: 1",
                "Sec-Fetch-Site: cross-site",
                "Content-Length: 1000",
            ],
            403,
        ),
    ],
)
@pytest.mark.parametrize(
    "target", ["/api/unlock", "/api/documents?collection=home&name=a.txt", "/api/nothing"]
)
def test_a_body_is_not_read_before_the_checks_passed(
    lines: list[str], status: int, target: str, paired: Browser, service: FakeService
) -> None:
    request = head(
        paired.server,
        "POST",
        target,
        *[line.format(session=paired.session_line) for line in lines],
    )
    started = time.monotonic()
    answer = raw(paired.server, request)  # announces 1000 bytes and sends none
    assert answer.status == status
    assert time.monotonic() - started < 4
    assert service.calls == []


def test_a_body_from_the_wrong_host_is_not_read(paired: Browser, service: FakeService) -> None:
    request = (
        "POST /api/unlock HTTP/1.1\r\nHost: evil.example\r\n"
        f"{paired.session_line}\r\nX-WDVA-Gate: 1\r\nContent-Length: 1000\r\n\r\n"
    ).encode("ascii")
    assert raw(paired.server, request).status == 403
    assert service.calls == []


@pytest.mark.parametrize(
    "lines",
    [
        ["Transfer-Encoding: chunked"],
        ["Content-Length: abc"],
        ["Content-Length: -1"],
        ["Content-Length: 2", "Content-Length: 2"],
        ["Content-Length: 1e3"],
        ["Content-Length: 2", "Transfer-Encoding: chunked"],
    ],
)
def test_a_body_of_unstated_length_is_refused(
    lines: list[str], paired: Browser, service: FakeService
) -> None:
    request = head(
        paired.server, "POST", "/api/lock", paired.session_line, "X-WDVA-Gate: 1", *lines
    )
    answer = raw(paired.server, request + b"2\r\n{}\r\n0\r\n\r\n")
    assert (answer.status, answer.error()) == (400, "invalid")
    assert service.calls == []


def test_a_client_that_asks_before_sending_is_invited_only_after_the_checks(
    server: AppServer, paired: Browser, service: FakeService
) -> None:
    body = b'{"passphrase": "pass phrase"}'
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(announce(paired, "/api/unlock", len(body), "Expect: 100-continue"))
        assert sock.recv(1024) == b"HTTP/1.1 100 Continue\r\n\r\n"
        sock.sendall(body)
        response = http.client.HTTPResponse(sock)
        response.begin()
        assert response.status == 200
        response.read()
    assert service.calls == [("unlock", {"passphrase": "pass phrase", "hours": None})]
    # Without a session the answer is the refusal, not an invitation to send the body.
    unpaired = head(
        server,
        "POST",
        "/api/unlock",
        "X-WDVA-Gate: 1",
        "Content-Length: 29",
        "Expect: 100-continue",
    )
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(unpaired)
        assert sock.recv(1024).startswith(b"HTTP/1.1 401 ")


def test_a_body_that_ends_early_is_invalid(paired: Browser, service: FakeService) -> None:
    request = announce(paired, "/api/unlock", 40) + b'{"passphrase": "pa'
    with socket.create_connection(("127.0.0.1", paired.server.port), timeout=5) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(sock)
        response.begin()
        assert response.status == 400
        assert json.loads(response.read())["error"]["code"] == "invalid"
    assert service.calls == []


# -- what the service answers ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid"),
        (403, "reauth_required"),
        (404, "not_found"),
        (409, "locked"),
        (409, "changed"),
        (409, "duplicate"),
        (413, "too_large"),
        (503, "unavailable"),
    ],
)
def test_a_service_error_becomes_its_status_and_code(
    status: int, code: str, paired: Browser, service: FakeService
) -> None:
    service.error = ServiceError(status, code, "one sentence \u2013 for a person")
    for method, target in (("GET", "/api/state"), ("POST", "/api/lock")):
        answer = paired.call(method, target)
        assert answer.status == status
        assert answer.json() == {
            "error": {"code": code, "message": "one sentence \u2013 for a person"}
        }
        assert answer.header("Cache-Control") == "no-store"
        assert_headers(answer)


def test_what_the_service_returns_is_sent_as_json(paired: Browser, service: FakeService) -> None:
    service.answer = {
        "text": "Zażółć\n\u201cquoted\u201d \u2028 </script>",
        "n": [1, 2.5, None, True],
    }
    answer = paired.call("GET", "/api/state")
    assert answer.json() == service.answer
    assert answer.body.isascii()


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("the lease says: pay 1,240 EUR to Kowalski"),
        KeyError("passphrase=correct horse battery"),
        ServiceError(200, "fine", "pay 1,240 EUR"),
        ServiceError(302, "moved", "pay 1,240 EUR"),
    ],
)
def test_an_unexpected_exception_is_a_500_that_tells_nothing(
    error: Exception, paired: Browser, service: FakeService, capfd: pytest.CaptureFixture[str]
) -> None:
    service.error = error
    capfd.readouterr()
    answer = paired.call("POST", "/api/unlock", json_body={"passphrase": "correct horse battery"})
    assert answer.status == 500
    assert answer.json() == {
        "error": {"code": "internal", "message": "something went wrong in the app"}
    }
    assert_headers(answer)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert type(error).__name__ in captured.err
    assert len(captured.err.splitlines()) == 1
    for secret in ("1,240", "Kowalski", "correct horse", "Traceback"):
        assert secret not in captured.err
        assert secret not in answer.body.decode()


@pytest.mark.parametrize(
    "answer", [["a", "list"], "text", 5, {"when": object()}, {"n": float("nan")}]
)
def test_an_answer_that_is_not_a_json_object_is_a_500(
    answer: Any, paired: Browser, service: FakeService, capfd: pytest.CaptureFixture[str]
) -> None:
    service.answer = answer
    response = paired.call("GET", "/api/state")
    assert (response.status, response.error()) == (500, "internal")
    assert capfd.readouterr().out == ""


@pytest.mark.parametrize("target", ["/api/unlock", "/api/reauth"])
def test_a_wrong_passphrase_is_answered_after_a_second(
    target: str, paired: Browser, service: FakeService
) -> None:
    assert httpd.WRONG_PASSPHRASE_DELAY == 1.0
    service.error = ServiceError(403, "wrong_passphrase", "that is not the passphrase")
    quick: list[float] = []

    def meanwhile() -> None:
        time.sleep(0.1)
        started = time.monotonic()
        Browser(paired.server).call("GET", "/api/session")
        quick.append(time.monotonic() - started)

    other = threading.Thread(target=meanwhile)
    other.start()
    started = time.monotonic()
    answer = paired.call("POST", target, json_body={"passphrase": "a wrong guess"})
    elapsed = time.monotonic() - started
    other.join()
    assert (answer.status, answer.error()) == (403, "wrong_passphrase")
    assert elapsed >= 1.0
    assert quick[0] < 0.75  # other requests are not held up meanwhile
    # Any other refusal comes at once, and there is no lock-out afterwards.
    service.error = ServiceError(403, "reauth_required", "enter the passphrase again")
    started = time.monotonic()
    assert paired.call("POST", target, json_body={"passphrase": "x"}).status == 403
    assert time.monotonic() - started < 0.7
    service.error = None
    assert paired.call("POST", target, json_body={"passphrase": "the right one"}).status == 200


def test_wrong_passphrases_take_turns_however_many_connections_try(
    paired: Browser, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One connection could always try one passphrase a second. Many at once must not try
    # more, and must not have several passphrases stretched at the same moment: each
    # stretch takes a lot of memory on purpose.
    delay = 0.4
    monkeypatch.setattr(httpd, "WRONG_PASSPHRASE_DELAY", delay)
    watch = threading.Lock()
    inside: list[int] = []
    most: list[int] = []

    def check(**arguments: Any) -> Any:
        with watch:
            inside.append(1)
            most.append(len(inside))
        time.sleep(0.03)
        with watch:
            inside.pop()
        raise ServiceError(403, "wrong_passphrase", "that is not the passphrase")

    for name in ("reauth", "unlock", "create_store"):
        monkeypatch.setattr(service, name, check)
    answered: list[tuple[int, float]] = []

    def guess(target: str) -> None:
        answer = paired.call("POST", target, json_body={"passphrase": "a wrong guess"})
        answered.append((answer.status, time.monotonic()))

    targets = ["/api/reauth", "/api/unlock", "/api/store/create"] * 2
    guessers = [threading.Thread(target=guess, args=(target,)) for target in targets]
    started = time.monotonic()
    for guesser in guessers:
        guesser.start()
    time.sleep(delay / 2)
    quick = time.monotonic()
    assert paired.call("GET", "/api/state").status == 200  # other calls do not wait their turn
    assert time.monotonic() - quick < 0.75 * delay
    for guesser in guessers:
        guesser.join()
    assert [status for status, _ in answered] == [403] * len(targets)
    assert max(most) == 1, "two passphrases were being checked at the same moment"
    # The n-th wrong passphrase is answered no sooner than n delays after the first began.
    for position, (_, when) in enumerate(sorted(answered, key=lambda entry: entry[1]), start=1):
        assert when - started >= position * delay - 0.05
    # A right passphrase waits for nothing once its turn has come.
    monkeypatch.setattr(service, "reauth", lambda **arguments: {"did": "reauth"})
    started = time.monotonic()
    assert paired.call("POST", "/api/reauth", json_body={"passphrase": "right"}).status == 200
    assert time.monotonic() - started < 0.75 * delay


# -- connections ------------------------------------------------------------------------


def silent(server: AppServer, count: int) -> list[socket.socket]:
    """Connections that send half a request and then nothing; that takes no session."""
    held = []
    for _ in range(count):
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        sock.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nX-Half: ".encode())
        held.append(sock)
        time.sleep(0.002)  # the listen queue is short: do not outrun the accept loop
    return held


def kept(sockets: list[socket.socket]) -> list[int]:
    """Which of the connections the server has not closed, by their position."""
    open_still = []
    for position, sock in enumerate(sockets):
        sock.setblocking(False)
        try:
            if sock.recv(1) != b"":
                open_still.append(position)
        except BlockingIOError:
            open_still.append(position)
        except OSError:
            pass  # reset: closed as well
    return open_still


def settled(sockets: list[socket.socket], most: int) -> list[int]:
    """The connections still kept once at most ``most`` are, or after two seconds."""
    deadline = time.monotonic() + 2
    while len(kept(sockets)) > most and time.monotonic() < deadline:
        time.sleep(0.02)
    return kept(sockets)


def test_silent_connections_cannot_take_every_place(
    server: AppServer, paired: Browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert httpd.MAX_CONNECTIONS == 64
    monkeypatch.setattr(httpd, "MAX_CONNECTIONS", 8)
    monkeypatch.setattr(httpd, "_LEAVING_CONNECTIONS", 4)
    held = silent(server, 40)
    try:
        # Each connection costs the app a thread and a file descriptor, so it keeps a
        # bounded number of them, and not the ones that had waited longest: each
        # newcomer took the place of one of those.
        still = settled(held, 8)
        assert len(still) <= 8
        assert not set(still) & set(range(8)), "the connections that waited longest were kept"
        # With every place taken by a connection that says nothing, the owner's page,
        # paired or not yet, is answered at once all the same.
        for _ in range(3):
            started = time.monotonic()
            assert paired.call("GET", "/api/state").status == 200
            assert Browser(server).call("GET", "/").status == 200
            assert time.monotonic() - started < 2
        assert len(kept(held)) <= 8
        assert len(server._httpd._open) <= 8 + 4
    finally:
        for sock in held:
            sock.close()
    # The places come back when the connections go.
    deadline = time.monotonic() + 5
    while server._httpd._open and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not server._httpd._open


def test_a_connection_that_showed_the_session_is_not_closed_for_a_newcomer(
    server: AppServer, paired: Browser, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpd, "MAX_CONNECTIONS", 4)
    entered = threading.Semaphore(0)
    go_on = threading.Event()

    def slow_state() -> Any:
        entered.release()
        go_on.wait(20)
        return {"did": "state"}

    monkeypatch.setattr(service, "state", slow_state)
    statuses: list[int] = []
    callers = [
        threading.Thread(target=lambda: statuses.append(paired.call("GET", "/api/state").status))
        for _ in range(4)
    ]
    for caller in callers:
        caller.start()
    try:
        for _ in callers:
            assert entered.acquire(timeout=10)
        # Every place is taken by a call of the owner's own page. None of them is cut
        # off for a newcomer: the newcomer is closed at once, without an answer.
        for _ in range(3):
            newcomer = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            # Closed with unread bytes, the system may say "reset" instead of "ended".
            with newcomer, contextlib.suppress(ConnectionError):
                newcomer.sendall(head(server, "GET", "/"))
                assert newcomer.recv(100) == b""
    finally:
        go_on.set()
        for caller in callers:
            caller.join()
    assert statuses == [200] * 4
    assert Browser(server).call("GET", "/").status == 200


FEW_FILES = """
import resource, sys
resource.setrlimit(resource.RLIMIT_NOFILE, ({limit}, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
from wdva.gate.app.httpd import AppServer

class Service:
    def state(self):
        return {{"did": "state"}}

server = AppServer(Service(), version="0")
server.start()
print(server.port, server.pairing_code, flush=True)
sys.stdin.read()  # until the test closes the pipe
server.stop()
"""


def test_the_app_answers_when_more_connections_come_than_it_may_have_files_open() -> None:
    # A process started from a terminal may have 256 files open on macOS. Without a bound
    # on its connections, that many silent ones leave the app unable to accept the owner's
    # browser, to answer agent applications or to open a file of the vault.
    limit = 128
    assert limit >= httpd.MAX_CONNECTIONS + httpd._LEAVING_CONNECTIONS + 16
    child = subprocess.Popen(
        [sys.executable, "-c", FEW_FILES.format(limit=limit)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    held: list[socket.socket] = []
    try:
        assert child.stdout is not None
        port, code = child.stdout.readline().split()
        for _ in range(limit + 40):
            try:
                sock = socket.create_connection(("127.0.0.1", int(port)), timeout=3)
            except OSError:
                break  # the app accepts no more: its table of open files is full
            sock.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Half: ".encode())
            held.append(sock)
            time.sleep(0.002)  # the listen queue is short: do not outrun the accept loop
        time.sleep(0.3)  # what was still on its way in has been dealt with
        for _ in range(3):
            connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
            try:
                connection.request(
                    "POST",
                    "/api/session/pair",
                    body=json.dumps({"code": code}),
                    headers={"X-WDVA-Gate": "1"},
                )
                assert connection.getresponse().status == 200
            finally:
                connection.close()
        assert len(kept(held)) <= httpd.MAX_CONNECTIONS
    finally:
        for sock in held:
            sock.close()
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=20)


# -- silence, start and stop ------------------------------------------------------------


def test_nothing_is_written_while_requests_are_served(
    server: AppServer, service: FakeService, capfd: pytest.CaptureFixture[str]
) -> None:
    capfd.readouterr()
    browser = Browser(server)
    browser.call("GET", "/")
    browser.call("GET", "/api/state")  # 401
    browser.call("POST", "/api/session/pair", json_body={"code": "AAAA-AAAA"})
    browser.pair()
    browser.call("GET", "/api/activity?app=claude-desktop&kind=release")
    browser.call("POST", "/api/unlock", json_body={"passphrase": "correct horse battery"})
    browser.call("POST", "/api/documents?collection=home&name=lease.txt", body=b"the lease")
    browser.call("POST", "/api/unlock", body=b"{")  # 400
    browser.call("GET", "/nothing")  # 404
    browser.call("GET", "/", headers={"Host": "evil.example"})  # 403
    service.error = ServiceError(409, "locked", "the vault is locked")
    browser.call("GET", "/api/documents")
    raw(server, b"GARBAGE\r\n\r\n")
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: half a requ")  # a client that goes away
    time.sleep(0.2)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_port_in_use_is_an_error_at_creation(server: AppServer, service: FakeService) -> None:
    with pytest.raises(OSError):  # noqa: PT011 - whatever the system says about the port
        AppServer(service, port=server.port, version=VERSION)
    with pytest.raises(ValueError, match="port"):
        AppServer(service, port=70000, version=VERSION)


def test_stop_closes_the_port(service: FakeService, static_dir: Path) -> None:
    server = AppServer(service, version=VERSION, static_dir=static_dir)
    port = server.port
    server.start()
    server.start()  # a second start changes nothing
    assert Browser(server).call("GET", "/").status == 200
    server.stop()
    server.stop()
    with pytest.raises(OSError):  # noqa: PT011 - refused, or reset
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    with pytest.raises(RuntimeError):
        server.start()


def test_a_server_that_never_started_can_be_stopped(service: FakeService, static_dir: Path) -> None:
    server = AppServer(service, version=VERSION, static_dir=static_dir)
    server.stop()


def test_the_shipped_static_directory_is_the_default(service: FakeService) -> None:
    server = AppServer(service, version=VERSION)
    try:
        shipped = Path(httpd.__file__).parent / "static"
        assert set(server._app.static) - {"/"} == {
            "/" + entry.name
            for entry in shipped.iterdir()
            if entry.is_file()
            and not entry.is_symlink()
            and not entry.name.startswith(".")
            and entry.suffix.lower() in httpd._CONTENT_TYPES
        }
    finally:
        server.stop()
