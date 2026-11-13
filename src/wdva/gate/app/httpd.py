# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The interface server: a small HTTP server on the loopback address.

It serves the owner's interface (static files shipped in the package) and the
JSON API behind it, and hands every API call to a
:class:`~wdva.gate.app.api.Service`. It knows nothing about stores or keys.

The server listens on ``127.0.0.1`` only. A loopback port can still be reached
by every program on the computer and, through the browser, by any web page the
owner has open. The rules below are what such a caller has to get past; each
one is checked on every request, before the request's body is read.

* **Address.** The ``Host`` header must be ``127.0.0.1:<port>`` or
  ``localhost:<port>``. A page that reaches the port under another name (a
  host name pointed at the loopback address) is refused.
* **Pairing.** At start the server draws a code that the app prints on its
  terminal. The page asks for it; the right code sets a session cookie
  (``HttpOnly; SameSite=Strict``, held in memory only). Five wrong codes switch
  pairing off until the app is restarted. Every API call except the two that
  do the pairing needs the cookie. Without pairing (the demo, and the app's own
  window) the cookie is given with the page itself: in the demo with every
  load, otherwise with the first one only.
* **Other sites.** Every call that changes something is a ``POST`` or
  ``DELETE`` and must carry the header ``X-WDVA-Gate: 1``, which a page of
  another site cannot send without asking first. On any request an ``Origin``
  header, when present, must name this server under the name the request was
  sent to, and an API call that the browser marks as coming from another site
  (``Sec-Fetch-Site``) is refused. No answer ever carries a header that would
  permit another site to read it.
* **Size.** A JSON body may have 64 KiB, an uploaded document 20 MiB. A larger
  one is refused from its declared length alone.
* **Static files** are the files found in one flat directory when the server
  starts, read into memory then. A request path is only ever looked up in
  that table; nothing derived from a request reaches the file system. The
  page itself is the one file that is not sent as it lies there: where it
  says ``{{product}}`` the server has put the product's name, so that the
  page shows it from its first moment and the scripts read it from the page.

Nothing about a request is logged. When the service fails unexpectedly, one
line with the name of the exception class goes to standard error and the
caller gets a plain error without detail.

None of this is a wall against programs that run under the owner's account:
one that can read the app's terminal can pair, and one that can read the
browser's cookies has the session. Without pairing, a program that loads the
page before the owner's browser or window does is given the session instead.
"""

from __future__ import annotations

import contextlib
import hmac
import html
import json
import os
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl

from wdva._util import strict_json_loads
from wdva.gate import brand
from wdva.gate.app.api import Service, ServiceError
from wdva.gate.extract import MAX_UPLOAD_BYTES

__all__ = [
    "CONTENT_SECURITY_POLICY",
    "MAX_JSON_BYTES",
    "MAX_WRONG_CODES",
    "PAIRING_ALPHABET",
    "WRONG_PASSPHRASE_DELAY",
    "AppServer",
]

MAX_JSON_BYTES = 64 * 1024
"""The largest JSON request body that is read."""

MAX_WRONG_CODES = 5
"""After this many wrong pairing codes, pairing is off until the app restarts."""

PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
"""The characters of a pairing code: no letter or digit that is easily taken for another."""

WRONG_PASSPHRASE_DELAY = 1.0
"""Seconds that pass, counted from the request's arrival, before a wrong passphrase is answered."""

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
"""Sent with every response: the page may load and contact nothing but this server."""

_PAIRING_CODE_LENGTH = 8
_GATE_HEADER = "X-WDVA-Gate"
_CONNECTION_TIMEOUT = 30.0  # seconds a connection may stay silent
_ACCEPT_WAIT = 0.5  # seconds between two looks at whether the server was stopped
_DISCARD_MAX_BYTES = 64 * 1024 * 1024
_DISCARD_SECONDS = 2.0

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
_JSON = "application/json"
_INDEX = "index.html"
_PAGE_PATHS = ("/", "/" + _INDEX)
_NAME_PLACE = b"{{product}}"  # where the page gets the product's name
# The product's name as a header value: plain letters, a hyphen for each space.
_SERVER_HEADER = "-".join(re.sub(r"[^A-Za-z0-9 ]", "", brand.PRODUCT).split()) or "app"

_LENGTH = re.compile(r"[0-9]{1,12}")
_WHOLE_NUMBER = re.compile(r"[0-9]{1,15}")
_NAME_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SAME_SITE_VALUES = frozenset({"same-origin", "none"})

_KIND_WORDS = {
    "text": "text",
    "flag": "true or false",
    "number": "a number",
    "integer": "a whole number",
    "object": "an object",
    "texts": "a list of texts",
}


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Field:
    """One named value of a request: a JSON field or a query parameter."""

    name: str
    kind: str  # a key of _KIND_WORDS
    required: bool = False


@dataclass(frozen=True)
class _Route:
    """One endpoint: a method and a path pattern, and the service method that answers it.

    ``path`` is what follows ``/api/``. A segment written ``<name>`` is passed to
    the service method as the keyword argument ``name``; ``<int:name>`` must be a
    whole number and is passed as one.
    """

    method: str
    path: str
    call: str
    fields: tuple[_Field, ...] = ()  # of the JSON body
    query: tuple[_Field, ...] = ()
    whole_body: str | None = None  # pass the JSON object itself under this name
    upload: bool = False  # the body is a file: pass its bytes as ``data``

    def match(self, method: str, segments: list[str]) -> dict[str, Any] | None:
        """The path parameters if this route answers the request, else None."""
        pattern = self.path.split("/")
        if method != self.method or len(segments) != len(pattern):
            return None
        found: dict[str, Any] = {}
        for expected, given in zip(pattern, segments, strict=True):
            if not expected.startswith("<"):
                if given != expected:
                    return None
            elif expected.startswith("<int:"):
                if not _WHOLE_NUMBER.fullmatch(given):
                    return None
                found[expected[5:-1]] = int(given)
            else:
                if not _NAME_SEGMENT.fullmatch(given):
                    return None
                found[expected[1:-1]] = given
        return found


def _text(name: str, *, required: bool = False) -> _Field:
    return _Field(name, "text", required)


_PASSPHRASE = _text("passphrase", required=True)

_ROUTES: tuple[_Route, ...] = (
    _Route("GET", "state", "state"),
    _Route("POST", "store/create", "create_store", fields=(_PASSPHRASE,)),
    _Route("POST", "unlock", "unlock", fields=(_PASSPHRASE, _Field("hours", "number"))),
    _Route("POST", "lock", "lock"),
    _Route("POST", "reauth", "reauth", fields=(_PASSPHRASE,)),
    _Route("POST", "serving", "set_serving", fields=(_text("kinds"), _Field("paused", "texts"))),
    _Route("POST", "verify", "verify"),
    _Route(
        "GET",
        "activity",
        "activity",
        query=(
            _Field("limit", "integer"),
            _Field("before", "integer"),
            _text("app"),
            _text("kind"),
        ),
    ),
    _Route("GET", "activity/<int:seq>", "activity_detail"),
    _Route("GET", "approvals", "approvals"),
    _Route(
        "POST",
        "approvals/release",
        "release",
        fields=(
            _text("app", required=True),
            _text("ref", required=True),
            _text("body_sha256", required=True),
        ),
    ),
    _Route(
        "POST",
        "approvals/dismiss",
        "dismiss",
        fields=(_text("app", required=True), _text("ref", required=True)),
    ),
    _Route("GET", "documents", "documents"),
    _Route(
        "POST",
        "documents",
        "add_document",
        query=(_text("collection", required=True), _text("name", required=True)),
        upload=True,
    ),
    _Route("DELETE", "documents/<id>", "remove_document"),
    _Route(
        "POST",
        "collections",
        "set_collection",
        fields=(
            _text("name", required=True),
            _text("note"),
            _Field("allow_hosted", "flag"),
            _Field("show_titles", "flag"),
        ),
    ),
    _Route("DELETE", "collections/<name>", "remove_collection"),
    _Route("GET", "apps", "apps"),
    _Route(
        "POST",
        "apps",
        "add_app",
        fields=(
            _text("name", required=True),
            _Field("hosted", "flag", required=True),
            _text("destination"),
        ),
    ),
    _Route("DELETE", "apps/<name>", "remove_app"),
    _Route(
        "POST",
        "grants",
        "set_grant",
        fields=(
            _text("app", required=True),
            _text("collection", required=True),
            _text("mode", required=True),
        ),
    ),
    _Route("POST", "apps/<name>/limits", "set_app_limits", whole_body="limits"),
    _Route("GET", "apps/<name>/connect", "connect", query=(_text("host"),)),
    _Route("GET", "settings", "settings"),
    _Route(
        "POST",
        "settings",
        "set_settings",
        fields=(
            _Field("defaults", "object"),
            _Field("groups", "object"),
            _Field("per_document", "object"),
            _Field("approval", "object"),
        ),
    ),
    _Route(
        "POST", "replaced-strings", "add_replaced_string", fields=(_text("text", required=True),)
    ),
)


def _find_route(method: str, segments: list[str]) -> tuple[_Route, dict[str, Any]]:
    """The route for a request and its path parameters. An unknown request is refused."""
    for route in _ROUTES:
        found = route.match(method, segments)
        if found is not None:
            return route, found
    raise _not_found()


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Response:
    status: int
    content_type: str
    body: bytes
    code: str | None = None  # the error code, when this is an error
    set_cookie: bool = False


class _Refused(Exception):
    """A request the server answers with an error itself, without calling the service."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _json_response(value: Any, *, set_cookie: bool = False) -> _Response:
    body = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return _Response(200, _JSON, body.encode("ascii"), set_cookie=set_cookie)


def _error_response(status: int, code: str, message: str) -> _Response:
    body = json.dumps({"error": {"code": code, "message": message}}, separators=(",", ":"))
    return _Response(status, _JSON, body.encode("ascii"), code=code)


def _invalid(message: str) -> _Refused:
    return _Refused(400, "invalid", message)


def _not_found() -> _Refused:
    return _Refused(404, "not_found", "there is nothing at this address")


def _forbidden(message: str) -> _Refused:
    return _Refused(403, "forbidden", message)


_INTERNAL = _error_response(500, "internal", "something went wrong in the app")


def _has_kind(value: Any, kind: str) -> bool:
    if kind == "text":
        return isinstance(value, str)
    if kind == "flag":
        return isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _body_arguments(route: _Route, body: dict[str, Any]) -> dict[str, Any]:
    """The keyword arguments a JSON body gives: every declared field, None where absent."""
    names = {field.name for field in route.fields}
    if any(name not in names for name in body):
        raise _invalid("the request has a field that does not belong to it")
    arguments: dict[str, Any] = {}
    for field in route.fields:
        value = body.get(field.name)
        if value is None:
            if field.required:
                raise _invalid(f'the field "{field.name}" is missing')
        elif not _has_kind(value, field.kind):
            raise _invalid(f'the field "{field.name}" must be {_KIND_WORDS[field.kind]}')
        arguments[field.name] = value
    return arguments


def _query_arguments(route: _Route, query: str) -> dict[str, Any]:
    """The keyword arguments a query string gives: every declared one, None where absent."""
    try:
        pairs = parse_qsl(
            query, keep_blank_values=True, encoding="utf-8", errors="strict", max_num_fields=16
        )
    except ValueError:
        raise _invalid("the address of the request cannot be read") from None
    given: dict[str, str] = {}
    names = {field.name for field in route.query}
    for name, value in pairs:
        if name not in names or name in given:
            raise _invalid("the address has a part that does not belong to it")
        given[name] = value
    arguments: dict[str, Any] = {}
    for field in route.query:
        text = given.get(field.name, "")
        if text == "":
            if field.required:
                raise _invalid(f'"{field.name}" is missing from the address')
            arguments[field.name] = None
        elif field.kind == "integer":
            if not _WHOLE_NUMBER.fullmatch(text):
                raise _invalid(f'"{field.name}" must be {_KIND_WORDS[field.kind]}')
            arguments[field.name] = int(text)
        else:
            arguments[field.name] = text
    return arguments


# ---------------------------------------------------------------------------
# What the handlers share
# ---------------------------------------------------------------------------


def _draw_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(_PAIRING_CODE_LENGTH))


def _load_static(directory: Path) -> dict[str, tuple[str, bytes]]:
    """Read the interface's files once: request path to content type and content.

    Only regular files directly in ``directory`` whose suffix has a known
    content type are taken; hidden files and symbolic links are not.
    """
    table: dict[str, tuple[str, bytes]] = {}
    if not directory.is_dir():
        return table
    for entry in sorted(directory.iterdir()):
        content_type = _CONTENT_TYPES.get(entry.suffix.lower())
        if content_type is None or entry.name.startswith("."):
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        table["/" + entry.name] = (content_type, entry.read_bytes())
    if "/" + _INDEX in table:
        content_type, page = table["/" + _INDEX]
        # The name is text in the page and inside attribute values: escape it for both.
        name = html.escape(brand.PRODUCT, quote=True).encode("utf-8")
        table["/" + _INDEX] = table["/"] = (content_type, page.replace(_NAME_PLACE, name))
    return table


class _App:
    """The state every request handler of one server shares."""

    def __init__(
        self,
        service: Service,
        *,
        port: int,
        pairing: bool,
        version: str,
        demo: bool,
        static: Mapping[str, tuple[str, bytes]],
    ) -> None:
        self.service = service
        self.port = port
        self.pairing = pairing
        self.version = version
        self.demo = demo
        self.static = static
        self.hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        if port == 80:  # the one port a browser leaves out of the Host header
            self.hosts |= {"127.0.0.1", "localhost"}
        self.code = _draw_pairing_code() if pairing else None
        # Cookies are shared between the ports of one host, so the name carries the port.
        self.cookie_name = f"{brand.SLUG.replace('-', '_')}_{port}"
        self.stopped = threading.Event()
        self._token = secrets.token_hex(32)
        self._lock = threading.Lock()
        self._wrong_codes = 0
        self._page_served = False

    @property
    def cookie(self) -> str:
        """The ``Set-Cookie`` value that makes a browser part of the session."""
        return f"{self.cookie_name}={self._token}; HttpOnly; SameSite=Strict; Path=/"

    def is_session(self, cookie_headers: list[str]) -> bool:
        """Whether one of the request's cookies is this run's session token."""
        found = False
        for header in cookie_headers:
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == self.cookie_name and hmac.compare_digest(
                    value.encode("utf-8"), self._token.encode("ascii")
                ):
                    found = True
        return found

    def try_code(self, code: str) -> str:
        """Check a typed pairing code: ``"ok"``, ``"wrong"`` or ``"disabled"``.

        Hyphens, blanks and case are ignored. The fifth wrong code switches
        pairing off, and from then on the right code is refused as well.
        """
        typed = "".join(code.replace("-", "").split()).upper()
        expected = self.code or ""
        with self._lock:
            if not self.pairing or self._wrong_codes >= MAX_WRONG_CODES:
                return "disabled"
            if hmac.compare_digest(typed.encode("utf-8"), expected.encode("ascii")):
                return "ok"
            self._wrong_codes += 1
            return "disabled" if self._wrong_codes >= MAX_WRONG_CODES else "wrong"

    def page_gives_session(self) -> bool:
        """Whether a load of the page, arriving without the cookie, is given it.

        Never with pairing. In the demo, where every document is invented,
        always. Otherwise only the first load: the app's own window takes the
        session, and whatever loads the page later gets none.
        """
        if self.pairing:
            return False
        if self.demo:
            return True
        with self._lock:
            first = not self._page_served
            self._page_served = True
            return first


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    timeout = _ACCEPT_WAIT
    app: _App

    def server_bind(self) -> None:
        # The stock method also asks the resolver for the host's full name,
        # which can take seconds and is of no use here.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = int(self.server_address[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Say nothing when a client goes away; otherwise only what kind of error it was."""
        error = sys.exc_info()[1]
        if error is not None and not isinstance(error, OSError):
            _report(error, "a request")


def _report(error: BaseException, where: str) -> None:
    """One line on standard error: the class of an unexpected error, never its message."""
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(f"wdva gate app: {type(error).__name__} in {where}\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# One request
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # A request line without a version is answered like any other, with headers.
    default_request_version = "HTTP/1.0"
    timeout = _CONNECTION_TIMEOUT

    _unread = 0  # bytes of the request body that were announced and not read

    @property
    def _app(self) -> _App:
        return cast(_Server, self.server).app

    # -- what the base class calls ----------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        """Log nothing: addresses, queries and names in a request are the owner's business."""

    def version_string(self) -> str:
        """The ``Server`` header: the app's name, not the versions of what it runs on."""
        return _SERVER_HEADER

    def handle_expect_100(self) -> bool:
        """Do not invite a body before the request was checked (see ``_read_body``)."""
        return True

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Answer a request the base class could not parse, in the same form as every error."""
        too_large = code in (413, 414, 431)
        self._send(
            _error_response(
                code,
                "too_large" if too_large else "invalid",
                "the request is too large" if too_large else "the request cannot be understood",
            )
        )

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    # -- the request ------------------------------------------------------------

    def _handle(self, method: str) -> None:
        arrived = time.monotonic()
        try:
            response = self._answer(method)
        except _Refused as refused:
            response = _error_response(refused.status, refused.code, refused.message)
        except OSError:
            raise  # the client went away
        except Exception as error:
            _report(error, "a request")
            response = _INTERNAL
        if response.code == "wrong_passphrase":
            # Slow down guessing. Every wrong passphrase costs this much, with no lock-out.
            time.sleep(max(0.0, arrived + WRONG_PASSPHRASE_DELAY - time.monotonic()))
        self._send(response)
        if self._unread > 0:
            self._discard_unread()

    def _answer(self, method: str) -> _Response:
        app = self._app
        length = self._announced_length()
        self._unread = length or 0
        hosts = self._header_values("Host")
        if len(hosts) != 1 or hosts[0].lower() not in app.hosts:
            raise _forbidden("this is not the address of the app")
        own_origin = "http://" + hosts[0].lower()

        if length is None:
            raise _invalid("the request must state the length of its body")
        if method not in ("GET", "POST", "DELETE"):
            raise _Refused(405, "invalid", "this kind of request is not supported")
        if method != "GET" and self._header_values(_GATE_HEADER) != ["1"]:
            raise _forbidden("this request did not come from the page of the app")
        origins = self._header_values("Origin")
        if origins and [origin.lower() for origin in origins] != [own_origin]:
            raise _forbidden("this request came from another site")

        if not self.path.isascii():
            raise _invalid("the address of the request cannot be read")
        path, _, query = self.path.partition("?")
        if not path.startswith("/api/"):
            return self._static(method, path)

        if any(v.lower() not in _SAME_SITE_VALUES for v in self._header_values("Sec-Fetch-Site")):
            raise _forbidden("this request came from another site")
        paired = app.is_session(self._header_values("Cookie"))
        if (method, path) == ("GET", "/api/session"):
            return _json_response(
                {
                    "paired": paired,
                    "pairing": "code" if app.pairing else "none",
                    "demo": app.demo,
                    "version": app.version,
                    "name": brand.PRODUCT,
                }
            )
        if (method, path) == ("POST", "/api/session/pair"):
            return self._pair()
        if not paired:
            raise _Refused(401, "pairing_required", "this page is not paired with the app")
        return self._call(method, path, query)

    def _static(self, method: str, path: str) -> _Response:
        entry = self._app.static.get(path) if method == "GET" else None
        if entry is None:
            raise _not_found()
        give_session = (
            path in _PAGE_PATHS
            and not self._app.is_session(self._header_values("Cookie"))
            and self._app.page_gives_session()
        )
        return _Response(200, entry[0], entry[1], set_cookie=give_session)

    def _pair(self) -> _Response:
        body = self._read_json(MAX_JSON_BYTES)
        if set(body) != {"code"} or not isinstance(body["code"], str):
            raise _invalid('the field "code" is missing')
        if not self._app.pairing:
            raise _not_found()
        result = self._app.try_code(body["code"])
        if result == "ok":
            return _json_response({"paired": True}, set_cookie=True)
        if result == "wrong":
            raise _Refused(403, "wrong_code", "that is not the code the app printed")
        raise _Refused(
            429, "pairing_disabled", "too many wrong codes: restart the app to get a new code"
        )

    def _call(self, method: str, path: str, query: str) -> _Response:
        route, arguments = _find_route(method, path[len("/api/") :].split("/"))
        arguments.update(_query_arguments(route, query))
        if route.upload:
            arguments["data"] = self._read_body(
                MAX_UPLOAD_BYTES, f"the file is larger than {MAX_UPLOAD_BYTES // 2**20} MiB"
            )
        elif method == "POST":
            body = self._read_json(MAX_JSON_BYTES)
            if route.whole_body is not None:
                arguments[route.whole_body] = body
            else:
                arguments.update(_body_arguments(route, body))
        if self._app.stopped.is_set():
            raise _Refused(503, "unavailable", "the app is shutting down")
        try:
            result = getattr(self._app.service, route.call)(**arguments)
            if not isinstance(result, dict):
                raise TypeError("the service did not return a JSON object")
            return _json_response(result)
        except ServiceError as error:
            if isinstance(error.status, int) and 400 <= error.status <= 599:
                return _error_response(error.status, str(error.code), str(error.message))
            _report(error, route.call)
        except Exception as error:
            _report(error, route.call)
        return _INTERNAL

    # -- reading ----------------------------------------------------------------

    def _header_values(self, name: str) -> list[str]:
        return [str(value).strip() for value in self.headers.get_all(name) or []]

    def _announced_length(self) -> int | None:
        """The length of the body as the request states it; None if it does not say.

        Nothing is read here. A body sent in pieces of unknown total length is
        not accepted, because its size could not be checked before reading it.
        """
        lengths = self._header_values("Content-Length")
        if self._header_values("Transfer-Encoding") or len(lengths) > 1:
            return None
        if not lengths:
            return 0
        return int(lengths[0]) if _LENGTH.fullmatch(lengths[0]) else None

    def _read_body(self, limit: int, too_large: str) -> bytes:
        """Read the body, which every check so far has allowed, if it is within ``limit``."""
        length = self._unread
        if length > limit:
            raise _Refused(413, "too_large", too_large)
        if any(value.lower() == "100-continue" for value in self._header_values("Expect")):
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()
        data = self.rfile.read(length) if length else b""
        self._unread -= len(data)
        if len(data) != length:
            raise _invalid("the request ended before its body was complete")
        return data

    def _read_json(self, limit: int) -> dict[str, Any]:
        """Read a JSON object. No body at all counts as the empty object."""
        data = self._read_body(limit, f"the request is larger than {limit // 1024} KiB")
        if not data:
            return {}
        try:
            value = strict_json_loads(data)
            # An escaped half of a surrogate pair parses, but is not text: refuse it here.
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        except ValueError:
            raise _invalid("the request is not valid JSON") from None
        if not isinstance(value, dict):
            raise _invalid("the request must be a JSON object")
        return value

    def _discard_unread(self) -> None:
        """Let a client that is still sending finish, so that it sees the answer.

        A body that was refused is never kept or looked at. But closing a
        connection on which unread bytes wait makes some systems drop the
        answer that was just sent, so what arrives is read and thrown away,
        for a short time and up to a fixed amount.
        """
        remaining = min(self._unread, _DISCARD_MAX_BYTES)
        deadline = time.monotonic() + _DISCARD_SECONDS
        with contextlib.suppress(OSError, ValueError):
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

    # -- writing ----------------------------------------------------------------

    def _send(self, response: _Response) -> None:
        self.close_connection = True
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if response.set_cookie:
            self.send_header("Set-Cookie", self._app.cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)
        self.wfile.flush()


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class AppServer:
    """The interface server for one :class:`~wdva.gate.app.api.Service`.

    Creating it binds ``127.0.0.1`` at ``port`` (0 picks a free one) and raises
    :class:`OSError` if that fails; :meth:`start` begins to answer requests in
    a background thread; :meth:`stop` ends it.

    With ``pairing`` a code is drawn, and a browser must present it before the
    API answers. Without it the page itself carries the session: every load in
    ``demo`` mode, only the first one otherwise. So load nothing from the
    server before the page that is meant to have it.

    ``version`` and ``demo`` are what the session endpoint reports, next to the
    product's name. The interface's files are read from ``static_dir``
    (default: the directory shipped with this package) once, here; the name is
    put into the page then.
    """

    def __init__(
        self,
        service: Service,
        *,
        port: int = 0,
        pairing: bool = True,
        version: str,
        demo: bool = False,
        static_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        directory = Path(static_dir) if static_dir is not None else Path(__file__).parent / "static"
        static = _load_static(directory)
        self._httpd = _Server(("127.0.0.1", port), _Handler)
        self._app = _App(
            service,
            port=self._httpd.server_port,
            pairing=pairing,
            version=version,
            demo=demo,
            static=static,
        )
        self._httpd.app = self._app
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def port(self) -> int:
        """The port the server is bound to."""
        return self._app.port

    @property
    def url(self) -> str:
        """The address to open in a browser. It carries no secret."""
        return f"http://127.0.0.1:{self._app.port}/"

    @property
    def pairing_code(self) -> str | None:
        """The pairing code as it is printed, ``XXXX-XXXX``; None when pairing is off."""
        code = self._app.code
        return None if code is None else f"{code[:4]}-{code[4:]}"

    def start(self) -> None:
        """Begin to serve, in a daemon thread. Returns at once."""
        with self._lock:
            if self._closed:
                raise RuntimeError("the server was stopped")
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._serve, name=f"{brand.SLUG}-http", daemon=True
            )
            self._thread.start()

    def _serve(self) -> None:
        with contextlib.suppress(OSError, ValueError):  # the port was closed under the loop
            while not self._app.stopped.is_set():
                self._httpd.handle_request()  # one connection, or a short wait for one

    def stop(self) -> None:
        """Stop serving and close the port. Safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        self._app.stopped.set()
        if thread is not None:
            # A connection of our own ends the wait for the next one at once.
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", self._app.port), timeout=1).close()
            thread.join(timeout=5)
        self._httpd.server_close()
