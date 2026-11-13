# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The local link: how the programs that agent applications start reach an unlocked store.

While a store is unlocked, the process that holds its key listens on the Unix
socket ``<store>/run/gate.sock`` (:class:`LinkServer`). The MCP server an agent
application starts holds no key. For every tool call it connects to that socket,
sends one request and reads one reply (:class:`RemoteGate`).

One connection carries one call::

    listener -> caller   b"WDVG1"                 sent when the connection is accepted
    caller -> listener   b"c"                     the caller's role; there is only this one
    caller -> listener   length, JSON object      the request, at most 16 KiB
    listener -> caller   length, JSON object      the reply, at most 1 MiB

A length is four bytes, big-endian. The JSON is UTF-8 and parsed strictly. A
request looks like this::

    {"v": 1, "op": "read", "client": "claude-desktop", "transport": "stdio",
     "claimed": {"name": "claude-ai", "version": "1.2", "protocol": "2025-11-25"},
     "args": {"ref": "d-3f9a2c41b07e.7"}}

and its reply like this::

    {"v": 1, "code": "OK", "released": true, "text": "..."}

The operations are ``status``, ``search`` (arguments ``query``, ``collection``,
``limit``), ``read`` (``ref``) and ``ping``, whose request is just
``{"v": 1, "op": "ping"}``. Nothing sent over the link can approve a passage,
change what a client may read, add a document or read the record.

The link carries no proof of who is calling. The socket has mode 0600 and lies
in a directory that only the owner's account can enter, so every program of
that account can connect and call under any client name that is served. A client
name is a label, not an identity. This is the only module of the gate that opens
a socket, and it only ever opens this one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import selectors
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from wdva._util import MAX_SAFE_INTEGER, check_json_limits, strict_json_loads
from wdva.gate import messages, paths
from wdva.gate.errors import GateError, KeeperUnavailableError
from wdva.gate.types import CODES, Backend, Caller, Reply

__all__ = [
    "HANDSHAKE_SECONDS",
    "HELLO",
    "KEYSLOT_NAME",
    "MAX_ARGUMENT_CHARS",
    "MAX_CLAIMED_CHARS",
    "MAX_CONNECTIONS",
    "MAX_REPLY_BYTES",
    "MAX_REQUEST_BYTES",
    "OPERATIONS",
    "ROLE_CLIENT",
    "LinkServer",
    "RemoteGate",
]

HELLO = b"WDVG1"
"""What the listener sends first. A caller that reads anything else gives up."""

ROLE_CLIENT = b"c"
"""The one byte a caller answers the hello with."""

OPERATIONS = ("status", "search", "read", "ping")

MAX_REQUEST_BYTES = 16 * 1024
MAX_REPLY_BYTES = 1024 * 1024

MAX_CONNECTIONS = 16
"""Connections served at the same time.

When all are taken, a new caller takes the place of the one that has waited
longest without sending its request, so that callers who say nothing keep
nobody out. It is closed at once only when every place is held by a call that
is being answered.
"""

HANDSHAKE_SECONDS = 5.0
"""How long a caller has, from the moment it is accepted, to send its complete request."""

REPLY_SECONDS = 5.0
"""How long the listener keeps trying to hand a reply to a caller that does not read."""

STOP_GRACE_SECONDS = 2.0
"""How long :meth:`LinkServer.stop` waits for calls that are being answered."""

MAX_ARGUMENT_CHARS = 1000
"""A longer text argument is cut to this before it is sent."""

MAX_CLAIMED_CHARS = 64
"""What a program says about itself (name, version, protocol) is cut to this."""

KEYSLOT_NAME = "keyslot.wdva"
"""The file every store has. A directory without it is not a store."""

_LEAVING_CONNECTIONS = 8  # callers that were told to go and whose threads have not ended yet
_MAX_CLIENT_CHARS = 64  # longer than any name that can be registered
_MAX_TRANSPORT_CHARS = 16
_REQUEST_KEYS = frozenset({"v", "op", "client", "claimed", "transport", "args"})
_CLIENT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_LENGTH = struct.Struct(">I")
_LOG = logging.getLogger(__name__)


class _LinkError(Exception):
    """The other side closed, was too slow or did not keep to the protocol."""


class _Locked(Exception):
    """Nothing listens on the store's socket."""


class _Request(NamedTuple):
    op: str
    caller: Caller | None  # None for ping, which is made under no client name
    args: dict[str, Any]


class _Listening(NamedTuple):
    listener: socket.socket
    wake: tuple[int, int]  # a pipe: writing to it ends the accepting thread
    thread: threading.Thread


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def _encode(message: Mapping[str, Any]) -> bytes:
    """``message`` as UTF-8 JSON that the strict parser on the other side accepts.

    Raises :class:`TypeError`, :class:`ValueError` or :class:`RecursionError`
    for a value JSON cannot carry: an object of another type, ``NaN``, an
    infinity, an integer no double can hold, or text that is not valid Unicode.
    """
    check_json_limits(message)
    text = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return text.encode("utf-8")


def _frame(body: bytes) -> bytes:
    return _LENGTH.pack(len(body)) + body


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _LinkError("too slow")
    return left


def _recv_exact(conn: socket.socket, size: int, deadline: float) -> bytes:
    """Exactly ``size`` bytes. The deadline is for all of them together, not for each read."""
    data = bytearray()
    while len(data) < size:
        conn.settimeout(_remaining(deadline))
        chunk = conn.recv(min(size - len(data), 65536))
        if not chunk:
            raise _LinkError("closed")
        data += chunk
    return bytes(data)


def _recv_frame(conn: socket.socket, limit: int, deadline: float) -> bytes:
    """One frame of at most ``limit`` bytes. A longer one is refused before any of it is read."""
    (size,) = _LENGTH.unpack(_recv_exact(conn, _LENGTH.size, deadline))
    if size == 0 or size > limit:
        raise _LinkError("frame size")
    return _recv_exact(conn, size, deadline)


def _send_all(conn: socket.socket, data: bytes, deadline: float) -> None:
    view = memoryview(data)
    while view:
        conn.settimeout(_remaining(deadline))
        view = view[conn.send(view) :]


def _close(conn: socket.socket) -> None:
    with contextlib.suppress(OSError):
        conn.close()


def _hang_up(conn: socket.socket) -> None:
    """End a connection that another thread may be waiting on. That thread closes it."""
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)


# ---------------------------------------------------------------------------
# The listening side
# ---------------------------------------------------------------------------


def _label(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _parse_request(body: bytes) -> _Request | None:
    """The request in ``body``, or ``None`` when it is not one. Nothing here raises."""
    try:
        data = strict_json_loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or not _REQUEST_KEYS.issuperset(data):
        return None
    if type(data.get("v")) is not int or data["v"] != 1:
        return None
    op = data.get("op")
    if not isinstance(op, str) or op not in OPERATIONS:
        return None
    if op == "ping":
        return _Request(op, None, {})
    client = data.get("client")
    claimed = data.get("claimed")
    args = data.get("args")
    transport = data.get("transport")
    if claimed is None:
        claimed = {}
    if args is None:
        args = {}
    if not isinstance(client, str) or not isinstance(claimed, dict) or not isinstance(args, dict):
        return None
    caller = Caller(
        client=client[:_MAX_CLIENT_CHARS],
        claimed_name=_label(claimed.get("name"), MAX_CLAIMED_CHARS),
        claimed_version=_label(claimed.get("version"), MAX_CLAIMED_CHARS),
        protocol=_label(claimed.get("protocol"), MAX_CLAIMED_CHARS),
        transport=_label(transport, _MAX_TRANSPORT_CHARS) or "other",
    )
    return _Request(op, caller, args)


def _failure() -> dict[str, Any]:
    return {"v": 1, "code": "UNAVAILABLE", "released": False, "text": messages.unavailable()}


def _check_socket_path(path: Path) -> None:
    """Check that a socket may be made at ``path``. :class:`GateError` when it may not.

    The directory must exist, belong to this user and be closed to everyone
    else. On some systems the mode of a socket file is not enforced; the
    directory is what keeps other accounts out.
    """
    size = len(os.fsencode(path))
    if size > paths.SOCKET_PATH_LIMIT:
        raise GateError(
            f"the store path is too long for a local socket ({size} bytes, "
            f"limit {paths.SOCKET_PATH_LIMIT}); choose a shorter --store"
        )
    folder = path.parent
    try:
        info = os.lstat(folder)
    except OSError:
        raise GateError(f"{folder} does not exist, so no socket can be made in it") from None
    if not stat.S_ISDIR(info.st_mode):
        raise GateError(f"{folder} is not a directory, so no socket can be made in it")
    if info.st_uid != os.geteuid():
        raise GateError(f"{folder} belongs to another user; the gate does not listen there")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise GateError(
            f"{folder} can be entered by other users; the gate does not listen there. "
            f"Close it with: chmod 700 {folder}"
        )


def _remove_leftover(path: Path) -> None:
    """Remove the socket file of an earlier process. Anything that is not a socket stays."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not (stat.S_ISSOCK(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise GateError(f"{path} exists and is not a socket; remove it and try again")
    os.unlink(path)


def _listen_on(path: Path) -> socket.socket:
    """A listening socket at ``path`` with mode 0600, or :class:`GateError`."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = False
    try:
        _remove_leftover(path)
        listener.bind(str(path))
        bound = True
        # Nobody can connect before listen(), so the socket is never reachable
        # with the mode that the umask of the process gave it.
        os.chmod(path, 0o600)
        listener.listen(MAX_CONNECTIONS)
        listener.setblocking(False)
    except BaseException as exc:
        listener.close()
        if bound:
            with contextlib.suppress(OSError):
                os.unlink(path)
        if isinstance(exc, OSError):
            reason = exc.strerror or type(exc).__name__
            raise GateError(f"the local socket {path} could not be opened: {reason}") from exc
        raise
    return listener


class LinkServer:
    """Listens on a store's socket and passes each call to ``backend``.

    ``backend`` answers ``status``, ``search`` and ``read``; ``info`` is called
    for ``ping`` and returns what the reply says about the session (JSON
    values; no document text belongs there). Every call runs on its own
    thread, so the backend has to be safe to call from several threads.

    :meth:`start` may be called once. The caller must hold the store's lock: a
    socket file found at the path is taken to be the leftover of a process that
    has ended, and is removed. :meth:`stop` may be called at any time and any
    number of times.

    What a caller sends is checked for form only. Whether a client name is
    served, and what it may have, is the backend's decision.
    """

    def __init__(
        self,
        backend: Backend,
        socket_path: str | os.PathLike[str],
        *,
        info: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._backend = backend
        self._path = Path(os.path.abspath(os.fspath(socket_path)))
        self._info = info
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._started = False
        self._listening: _Listening | None = None
        # Each open connection, oldest first: False while its request is awaited, True once
        # its call was handed to the backend, None when it was told to go for a newcomer.
        self._connections: dict[socket.socket, bool | None] = {}

    @property
    def socket_path(self) -> Path:
        """Where the server listens, as an absolute path."""
        return self._path

    def start(self) -> None:
        """Create the socket (mode 0600) and begin to answer calls.

        Raises :class:`~wdva.gate.errors.GateError` when the socket cannot be
        made: its directory is missing, belongs to someone else or is open to
        others, the path is too long, or something that is not a socket is in
        the way. Nothing is left behind then, and ``start`` can be tried again.
        """
        with self._lock:
            if self._started:
                raise GateError("this link was started before; make a new one")
            _check_socket_path(self._path)
            listener = _listen_on(self._path)
            try:
                wake = os.pipe()
            except OSError as exc:
                self._abandon(listener, ())
                raise GateError(f"the local socket {self._path} could not be opened") from exc
            thread = threading.Thread(
                target=self._accept_loop,
                args=(listener, wake[0]),
                name="wdva-gate-link",
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError as exc:  # the process cannot start another thread
                self._abandon(listener, wake)
                raise GateError(f"the local socket {self._path} could not be served") from exc
            self._listening = _Listening(listener, wake, thread)
            self._started = True

    def _abandon(self, listener: socket.socket, descriptors: tuple[int, ...]) -> None:
        """Close a listening socket and remove its file."""
        listener.close()
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(OSError):
            if stat.S_ISSOCK(os.lstat(self._path).st_mode):
                os.unlink(self._path)

    def stop(self) -> None:
        """Stop listening, remove the socket file and end the open connections.

        Calls that are being answered get up to two seconds to finish. Callers
        that have not sent their request yet are dropped at once. When this
        returns, nothing is accepted any more and no further call reaches the
        backend.
        """
        with self._lock:
            listening = self._listening
            if listening is None:
                return
            self._listening = None
            waiting = [conn for conn, handed_over in self._connections.items() if not handed_over]
        with contextlib.suppress(OSError):
            os.write(listening.wake[1], b"\0")
        listening.thread.join()
        self._abandon(listening.listener, listening.wake)
        for conn in waiting:
            _hang_up(conn)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        with self._idle:
            while self._connections:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._idle.wait(left)
            late = list(self._connections)
        for conn in late:
            _hang_up(conn)

    def __enter__(self) -> LinkServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- accepting -----------------------------------------------------------

    def _accept_loop(self, listener: socket.socket, wake: int) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(listener, selectors.EVENT_READ)
            selector.register(wake, selectors.EVENT_READ)
            while True:
                if any(key.fd == wake for key, _ in selector.select()):
                    return
                try:
                    conn, _ = listener.accept()
                except (BlockingIOError, InterruptedError, ConnectionAbortedError):
                    continue
                except OSError:
                    time.sleep(0.05)  # out of descriptors, for example: do not spin
                    continue
                self._admit(conn)

    def _admit(self, conn: socket.socket) -> None:
        told_to_go: socket.socket | None = None
        with self._lock:
            admitted = self._listening is not None
            if admitted and len(self._connections) >= MAX_CONNECTIONS:
                # Every place is taken. The caller that has waited longest without sending
                # its request gives way: a program that connects and says nothing must not
                # be able to keep the applications out.
                told_to_go = next(
                    (held for held, state in self._connections.items() if state is False), None
                )
                admitted = (
                    told_to_go is not None
                    and len(self._connections) < MAX_CONNECTIONS + _LEAVING_CONNECTIONS
                )
                if admitted and told_to_go is not None:
                    self._connections[told_to_go] = None
            if admitted:
                self._connections[conn] = False
        if not admitted:
            _close(conn)
            return
        if told_to_go is not None:
            _hang_up(told_to_go)
        thread = threading.Thread(
            target=self._serve, args=(conn,), name="wdva-gate-link-call", daemon=True
        )
        try:
            thread.start()
        except RuntimeError:  # the process cannot start another thread
            _close(conn)
            self._forget(conn)

    def _forget(self, conn: socket.socket) -> None:
        with self._idle:
            self._connections.pop(conn, None)
            self._idle.notify_all()

    # -- one connection ------------------------------------------------------

    def _serve(self, conn: socket.socket) -> None:
        try:
            with contextlib.suppress(OSError, _LinkError):
                request = self._receive(conn)
                if request is not None and self._hand_over(conn):
                    reply = self._answer(request)
                    _send_all(conn, _frame(reply), time.monotonic() + REPLY_SECONDS)
        finally:
            _close(conn)
            self._forget(conn)

    def _receive(self, conn: socket.socket) -> _Request | None:
        """The caller's request, or ``None`` for one that gets no reply."""
        deadline = time.monotonic() + HANDSHAKE_SECONDS
        _send_all(conn, HELLO, deadline)
        if _recv_exact(conn, len(ROLE_CLIENT), deadline) != ROLE_CLIENT:
            return None
        return _parse_request(_recv_frame(conn, MAX_REQUEST_BYTES, deadline))

    def _hand_over(self, conn: socket.socket) -> bool:
        """Mark the connection's call as running, unless the server is stopping."""
        with self._lock:
            # Not when the caller was told to go a moment ago: it would get no reply, and
            # a call that is made must be one whose reply is sent.
            if self._listening is None or self._connections.get(conn) is not False:
                return False
            self._connections[conn] = True
            return True

    def _answer(self, request: _Request) -> bytes:
        """The encoded reply. Whatever goes wrong, the caller gets the fixed text and no detail."""
        try:
            reply = _encode(self._dispatch(request))
            if len(reply) > MAX_REPLY_BYTES:
                raise ValueError("the reply is too large")
            return reply
        except Exception as exc:
            # Only the kind of failure: the message of an exception may hold document text.
            _LOG.warning("a call over the local link failed (%s)", type(exc).__name__)
            return _encode(_failure())

    def _dispatch(self, request: _Request) -> dict[str, Any]:
        if request.caller is None:
            extra = dict(self._info()) if self._info is not None else {}
            return {**extra, "v": 1, "code": "OK"}
        caller, args = request.caller, request.args
        if request.op == "status":
            reply = self._backend.status(caller)
        elif request.op == "search":
            reply = self._backend.search(
                caller, args.get("query"), args.get("collection", ""), args.get("limit", 5)
            )
        else:
            reply = self._backend.read(caller, args.get("ref"))
        code, text, released = reply.code, reply.text, reply.released
        if code not in CODES or not isinstance(text, str) or not isinstance(released, bool):
            raise TypeError("the backend did not return a reply")
        return {"v": 1, "code": code, "released": released, "text": text}


# ---------------------------------------------------------------------------
# The calling side
# ---------------------------------------------------------------------------


def _trimmed(value: Any) -> Any:
    """One argument as it is sent. None of this changes the answer the gate gives.

    The gate cuts a query far below the limit used here, refuses a ref or a
    collection name of that length, treats every list like an empty one and
    clamps a number of results to a small range.
    """
    if isinstance(value, str):
        return value[:MAX_ARGUMENT_CHARS]
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return []
    if isinstance(value, int) and not isinstance(value, bool):
        return max(-MAX_SAFE_INTEGER, min(MAX_SAFE_INTEGER, value))
    return value


def _request_frame(op: str, client: str, caller: Caller, args: Mapping[str, Any]) -> bytes:
    """The framed request. Raises like :func:`_encode`, and ValueError when it is too large."""
    transport = _label(caller.transport, _MAX_TRANSPORT_CHARS) or "other"
    body = _encode(
        {
            "v": 1,
            "op": op,
            "client": client[:_MAX_CLIENT_CHARS],
            "transport": transport,
            "claimed": {
                "name": _label(caller.claimed_name, MAX_CLAIMED_CHARS),
                "version": _label(caller.claimed_version, MAX_CLAIMED_CHARS),
                "protocol": _label(caller.protocol, MAX_CLAIMED_CHARS),
            },
            "args": {name: _trimmed(value) for name, value in args.items()},
        }
    )
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("the request is too large")
    return _frame(body)


def _reply_from(answer: Any) -> Reply | None:
    """The reply in a decoded frame, or ``None`` when it does not have the form of one."""
    if not isinstance(answer, dict) or type(answer.get("v")) is not int or answer["v"] != 1:
        return None
    code, text, released = answer.get("code"), answer.get("text"), answer.get("released")
    if not isinstance(code, str) or code not in CODES:
        return None
    if not isinstance(text, str) or not isinstance(released, bool):
        return None
    return Reply(code=code, text=text, released=released)


class RemoteGate:
    """Answers the three tool calls by asking the process that has the store unlocked.

    It holds no key and keeps no connection: every call connects anew. Made
    with a ``client`` name, it sends that name with every call and refuses a
    :class:`~wdva.gate.types.Caller` that carries another one
    (:class:`ValueError`). Made without one, only :meth:`ping` can be used.

    :meth:`status`, :meth:`search` and :meth:`read` never raise because the
    store cannot be reached. They return a reply that releases nothing and says
    what to do:

    * the directory holds no store: ``UNAVAILABLE``, with the text for a vault
      that cannot be found;
    * no socket, or nobody listens on it: ``LOCKED``;
    * a timeout, an answer that is not understood, anything else:
      ``UNAVAILABLE``;
    * arguments that cannot be sent at all: ``BAD_REQUEST``, without connecting.

    ``timeout`` is in seconds and covers one whole call.
    """

    def __init__(
        self,
        store_dir: str | os.PathLike[str],
        client: str | None = None,
        *,
        timeout: float = 10.0,
    ) -> None:
        if client is not None and not isinstance(client, str):
            raise TypeError("client must be a name or None")
        self._store = Path(os.path.abspath(os.fspath(store_dir)))
        self._client = client
        self._timeout = float(timeout)

    @property
    def store_dir(self) -> Path:
        """The store directory, as an absolute path."""
        return self._store

    @property
    def client(self) -> str | None:
        """The name every call is made under."""
        return self._client

    def status(self, caller: Caller) -> Reply:
        """What the client may use and how much is left. Releases no document text."""
        return self._call("status", caller, {})

    def search(self, caller: Caller, query: Any, collection: Any = "", limit: Any = 5) -> Reply:
        """Search the client's collections. Arguments are passed on for the gate to judge."""
        return self._call(
            "search", caller, {"query": query, "collection": collection, "limit": limit}
        )

    def read(self, caller: Caller, ref: Any) -> Reply:
        """Read one passage by its ref."""
        return self._call("read", caller, {"ref": ref})

    def ping(self) -> dict[str, Any]:
        """What the listening process says about its session.

        Raises :class:`~wdva.gate.errors.KeeperUnavailableError` when there is
        no store, when it is locked, or when no proper answer comes back.
        """
        if not self._has_store():
            raise KeeperUnavailableError(f"there is no store at {self._store}")
        try:
            answer = self._exchange(_frame(_encode({"v": 1, "op": "ping"})))
        except _Locked:
            raise KeeperUnavailableError(
                "the store is locked, or the app that unlocks it is not running"
            ) from None
        except (OSError, ValueError, _LinkError) as exc:
            raise KeeperUnavailableError("the app did not answer on its local socket") from exc
        if not isinstance(answer, dict) or answer.get("v") != 1 or answer.get("code") != "OK":
            raise KeeperUnavailableError("the app did not answer on its local socket")
        return answer

    # -- internals -----------------------------------------------------------

    def _has_store(self) -> bool:
        return os.path.isfile(self._store / KEYSLOT_NAME)

    def _call(self, op: str, caller: Caller, args: Mapping[str, Any]) -> Reply:
        client = self._client
        if client is None:
            raise ValueError("this link was made without a client name; only ping() can be used")
        if caller.client != client:
            raise ValueError("the caller's client name is not the one this link was made for")
        if not self._has_store():
            # The text names the client, so only a name of the registered form goes into it.
            named = _CLIENT_NAME.fullmatch(client) is not None
            text = messages.store_not_found(client) if named else messages.unavailable()
            return Reply("UNAVAILABLE", text, False)
        try:
            frame = _request_frame(op, client, caller, args)
        except (TypeError, ValueError, RecursionError):
            return Reply("BAD_REQUEST", messages.bad_request(), False)
        try:
            reply = _reply_from(self._exchange(frame))
        except _Locked:
            return Reply("LOCKED", messages.locked(), False)
        except (OSError, ValueError, _LinkError):
            reply = None
        return reply or Reply("UNAVAILABLE", messages.unavailable(), False)

    def _exchange(self, frame: bytes) -> Any:
        """Send one framed request and return the decoded reply.

        Raises ``_Locked`` when nothing listens, and :class:`OSError`,
        :class:`ValueError` or ``_LinkError`` for every other failure.
        """
        path = paths.socket_path(self._store)
        if not os.path.lexists(path):
            raise _Locked
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise _LinkError("no local sockets on this system")
        deadline = time.monotonic() + self._timeout
        conn = socket.socket(family, socket.SOCK_STREAM)
        try:
            conn.settimeout(_remaining(deadline))
            try:
                conn.connect(str(path))
            except (FileNotFoundError, ConnectionRefusedError):
                raise _Locked from None
            if _recv_exact(conn, len(HELLO), deadline) != HELLO:
                raise _LinkError("not the gate")
            _send_all(conn, ROLE_CLIENT + frame, deadline)
            return strict_json_loads(_recv_frame(conn, MAX_REPLY_BYTES, deadline))
        finally:
            _close(conn)
