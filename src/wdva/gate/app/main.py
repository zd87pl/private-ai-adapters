# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Starting the app: the interface server, the service behind it, and a clean end.

:func:`run_app` is what ``wdva gate app`` runs. It builds the service for one
store directory, binds the interface to a port on ``127.0.0.1``, prints where
to find it and waits. Ctrl-C, or a signal that asks the program to end, locks
the vault before the program stops, so that the record says how the session
ended.

The start-up lines are the only thing written to standard output. With pairing
they include the code that a page must present; the address that is opened
or printed never carries a secret. A demo adds one line with the passphrase of
its vault, which is the same for every demo and guards invented documents
only. The passphrase of a real vault is never printed. Where no agent
application could connect (an older Python, or no MCP SDK) one more line says
so after the address, in the words of the warning the page shows once the vault
is unlocked. When a demo stops, one last line gives the command that starts
the app on a vault of one's own (:func:`launch_command`); a real vault prints
nothing at the end.

Only the demo runs without pairing. Without it every program on the computer
can load the page, be given the session and act as the owner there, also
release a passage that waits for approval. That is accepted for invented
documents and for nothing else: the app's own window pairs like a browser.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import FrameType

from wdva._version import __version__
from wdva.gate import brand, hostconfig, paths
from wdva.gate.app.api import ServiceError
from wdva.gate.app.demo import DemoAgent, make_demo_store
from wdva.gate.app.httpd import AppServer
from wdva.gate.app.service import MAX_HOURS, MIN_HOURS, VIEWS, GateService, connect_problem
from wdva.gate.errors import GateError

__all__ = ["DEMO_LINE_NOTE", "DEMO_STORE_LINE", "OWN_VAULT_LINE", "launch_command", "run_app"]

DEMO_STORE_LINE = "a temporary demo store with invented documents"
"""What the start-up lines say instead of a path in demo mode."""

DEMO_LINE_NOTE = "(the page shows it too; the passphrase of a real vault is never shown)"
"""What follows the passphrase on the start-up line that a demo adds."""

OWN_VAULT_LINE = "To start with your own documents: "
"""What the line printed when a demo stops begins with; the command follows."""

_DEFAULT_LOCK_AFTER = 8.0  # hours; the default of --lock-after
_PR_SET_DUMPABLE = 4
_WINDOW_MODULE = "webview"  # the package pywebview, which is not a dependency
_WAIT_SECONDS = 0.1  # how soon Ctrl-C is noticed, at the latest
_DEMO_PREFIX = f"{brand.SLUG}-demo-"
_DEMO_NAME = re.compile(re.escape(_DEMO_PREFIX) + "[a-z0-9_]{8}")  # as mkdtemp makes them
_DEMO_SETTLED = 60.0  # seconds without a change after which no demo is still making a directory
# Where a demo's store goes when the usual temporary directory has too long a path for a socket.
_SHORT_PLACE = "/tmp"  # noqa: S108 - what is made there is made by mkdtemp, private and new


class _Stop:
    """Set when the program is asked to end; remembers why.

    A signal handler runs in the main thread between two steps of whatever
    that thread is doing, also in the middle of ``Event.wait``, at a moment
    when the event's own lock is held. A handler that set the event there
    would wait for a lock its own thread holds, and the program would hang
    with the vault unlocked. So a handler only writes one attribute
    (:meth:`signalled`), which takes no lock, and :meth:`wait` reads it
    between its short waits.
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self.reason = "close"
        self._signal: str | None = None

    def ask(self, reason: str) -> None:
        """Ask the program to end. Not for a signal handler: see :meth:`signalled`."""
        if not self.event.is_set():
            self.reason = reason
        self.event.set()

    def signalled(self, reason: str) -> None:
        """What a signal handler calls. It touches no lock; :meth:`wait` notices it."""
        if self._signal is None:
            self._signal = reason

    def wait(self) -> None:
        # In short waits: a signal sets no event, so it is noticed when a wait ends.
        while True:
            if self._signal is not None:
                self.ask(self._signal)
            if self.event.wait(_WAIT_SECONDS):
                return


def _harden() -> None:
    """Ask the system not to write this process's memory to disk when it crashes.

    Core dumps are switched off, and on Linux the process is marked as not
    dumpable. Both are best effort and nothing depends on them: a program
    running under the same account has other ways to the key.
    """
    with contextlib.suppress(Exception):
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith("linux"):
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)


def _demo_directory() -> Path:
    """A new, empty, private directory for the demo store, short enough for its socket.

    What earlier demos left behind in the same places is removed first.
    """
    for place in {tempfile.gettempdir(), _SHORT_PLACE}:
        _remove_dead_demos(place)
    directory = Path(tempfile.mkdtemp(prefix=_DEMO_PREFIX))
    try:
        paths.check_socket_path(directory)
    except GateError:
        shutil.rmtree(directory, ignore_errors=True)
        directory = Path(tempfile.mkdtemp(prefix=_DEMO_PREFIX, dir=_SHORT_PLACE))
    return directory


def _hold(directory: str | os.PathLike[str]) -> int | None:
    """Take the lock that says a demo is alive in ``directory``; None if it cannot be had.

    The lock is an operating system lock on the directory itself. It belongs
    to this process and ends with it, however the process ends. It is not had
    when another process holds it, and never for something that is not a
    directory of this user. Close the returned descriptor to give it up.
    """
    import fcntl

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory, flags | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if os.fstat(descriptor).st_uid != os.getuid():
            raise PermissionError
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def _remove_dead_demos(place: str | os.PathLike[str]) -> None:
    """Remove the stores that demos left in ``place`` when they were killed.

    A demo removes its store when it ends, but not when it is killed or the
    computer stops. Removed is only what is recognised as such a store: a real
    directory, named as this program names a demo's, owned by this user,
    unchanged for a minute, and whose lock (:func:`_hold`) no running demo
    holds. Everything else is left alone, and so is everything when in doubt.
    """
    with contextlib.suppress(OSError):
        for entry in list(os.scandir(place)):
            with contextlib.suppress(OSError):
                if not _DEMO_NAME.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                    continue
                if time.time() - entry.stat(follow_symlinks=False).st_mtime < _DEMO_SETTLED:
                    continue
                held = _hold(entry.path)
                if held is not None:
                    try:
                        shutil.rmtree(entry.path, ignore_errors=True)
                    finally:
                        os.close(held)


def _open_browser(url: str) -> None:
    """Open the interface in the default browser. The address carries no secret."""
    with contextlib.suppress(Exception):
        import webbrowser

        webbrowser.open(url)


def _window_available() -> bool:
    try:
        return importlib.util.find_spec(_WINDOW_MODULE) is not None
    except (ImportError, ValueError):
        return False


def _show_window(url: str) -> bool:
    """Show the interface in a window of its own until it is closed. Experimental.

    Needs the package pywebview. Returns False when the window could not be
    opened; this function was never run on the machine it was written on.
    """
    try:
        webview = importlib.import_module(_WINDOW_MODULE)
        webview.create_window(brand.PRODUCT, url)
        webview.start()
    except Exception:
        return False
    return True


def _apps_line() -> str | None:
    """The start-up line that says no agent application can connect; ``None`` when one can."""
    problem = connect_problem(hostconfig.sdk_status())
    if problem is None:
        return None
    _code, words, command = problem
    install = "".join(f" Install it into the same environment with:  {line}" for line in command)
    return f"apps:  {words}{install}"


def launch_command(
    *,
    store: str | os.PathLike[str] | None,
    port: int = 0,
    lock_after_hours: float = _DEFAULT_LOCK_AFTER,
    open_browser: bool = True,
    window: bool = False,
) -> str | None:
    """The command line that starts this installation of the app again, as one line of text.

    It names the interpreter that runs this package by its full path, like the
    command an agent application is given, so it works whatever is on ``PATH``.
    ``store`` is the vault it opens, written as an absolute path; ``None``
    leaves it out, which is the command for a vault of one's own in the usual
    place (what a demo shows). The flags a run was started with follow: the
    port when one was named, the hours when they are not the default,
    ``--no-browser`` and ``--window``. It never holds ``--demo`` or ``--view``.

    ``None`` when this Python cannot say where its interpreter is.
    """
    if not sys.executable:
        return None
    words = [sys.executable, "-m", "wdva", "gate", "app"]
    if store is not None and os.fspath(store):
        words += ["--store", str(paths.resolve_store(store))]
    if port:
        words += ["--port", str(port)]
    if lock_after_hours != _DEFAULT_LOCK_AFTER:
        hours = repr(float(lock_after_hours))
        words += ["--lock-after", hours[:-2] if hours.endswith(".0") else hours]
    if not open_browser:
        words.append("--no-browser")
    if window:
        words.append("--window")
    return shlex.join(words)


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _say_last(line: str) -> None:
    """Print one more line as the app ends. Where the terminal has gone, nobody is told.

    A line that could not be written must not be tried again when the program
    ends, which would turn a clean end into an error: standard output is then
    pointed at nothing.
    """
    try:
        _say(line)
    except (OSError, ValueError):
        with contextlib.suppress(OSError, ValueError):
            nowhere = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(nowhere, sys.stdout.fileno())
            finally:
                os.close(nowhere)


def _explain(line: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


def run_app(
    *,
    store: str | None,
    port: int = 0,
    open_browser: bool = True,
    demo: bool = False,
    window: bool = False,
    lock_after_hours: float = _DEFAULT_LOCK_AFTER,
    view: str | None = None,
) -> int:
    """Start the app and block until it is stopped. Returns the process exit status.

    ``store`` is the store directory; ``None`` means the one named by the
    environment variable :data:`wdva.gate.paths.ENV_STORE`, else the per-user
    location. ``port`` 0 picks a free
    port. With ``demo`` the store is a temporary one with invented documents:
    it is unlocked at once, an agent is played against it, and it is removed
    when the app stops. ``window`` shows the interface in its own window when
    pywebview is installed, and falls back to the browser when it is not, unless
    ``open_browser`` is false; the window asks for the pairing code like a
    browser does. ``view`` is the view of the interface to open in, ``"basic"``
    or ``"pro"``: a vault that is created starts in it, and for a vault that
    exists it is stored at once. ``None`` writes nothing at start.

    Ctrl-C, SIGTERM and SIGHUP lock the vault, stop the server and return 0.
    A demo that stops prints the command that starts the app for real.
    """
    if not MIN_HOURS <= lock_after_hours <= MAX_HOURS:
        raise GateError(f"--lock-after takes {MIN_HOURS} to {MAX_HOURS:g} hours")
    if not 0 <= port <= 65535:
        raise GateError("--port takes a number from 0 to 65535")
    if view is not None and view not in VIEWS:
        raise GateError(f"--view takes {' or '.join(VIEWS)}")
    _harden()
    stop = _Stop()
    demo_directory: Path | None = None
    demo_alive: int | None = None
    try:
        demo_passphrase: str | None = None
        if demo:
            demo_directory = _demo_directory()
            # Held until this process ends, so that no later demo takes the store for a dead one's.
            demo_alive = _hold(demo_directory)
            demo_passphrase = make_demo_store(demo_directory)
        path = demo_directory or paths.resolve_store(store)
        # How to start this installation again: for a demo on a vault of one's own, so
        # without the demo's temporary folder, and only with a place the person named.
        again = launch_command(
            store=(store or None) if demo else path,
            port=port,
            lock_after_hours=lock_after_hours,
            open_browser=open_browser,
            window=window,
        )
        agent: DemoAgent | None = None

        def ask_again() -> None:
            if agent is not None:
                agent.ask_again()

        service = GateService(
            path,
            demo=demo,
            demo_passphrase=demo_passphrase,
            lock_after_hours=lock_after_hours,
            view=view,
            launch_command=again,
            port_fixed=port != 0,
            demo_again=ask_again if demo else None,
        )

        in_window = window and _window_available()
        if window and not in_window:
            _explain(
                "pywebview is not installed: the interface opens in the browser instead."
                if open_browser
                else "pywebview is not installed: no window is opened, and with --no-browser no "
                'browser page either. The address is on the line that begins with "open:".'
            )
        try:
            # Without pairing whoever loads the page is given the session: only for the
            # demo. A window that this program opens pairs like any browser.
            server = AppServer(service, port=port, pairing=not demo, version=__version__, demo=demo)
        except OSError as exc:
            raise GateError(
                f"the interface cannot listen on port {port} of this computer; "
                "is another program using it?"
            ) from exc

        announced = False
        previous = _handle_signals(stop)
        try:
            if demo_passphrase is not None:
                try:
                    service.unlock(passphrase=demo_passphrase)
                except ServiceError as exc:
                    raise GateError(
                        f"the demo store could not be unlocked: {exc.message}"
                    ) from None
                agent = DemoAgent(service.backend, ready=lambda: service.unlocked)
            server.start()
            _say(f"{brand.PRODUCT} {__version__}")
            _say(f"store: {DEMO_STORE_LINE if demo else path}")
            _say(f"open:  {server.url}")
            apps_line = _apps_line()
            if apps_line is not None:
                _say(apps_line)
            if server.pairing_code is not None:
                _say(
                    f"pairing code: {server.pairing_code}   "
                    "(type it into the page; anyone who can read this terminal can pair)"
                )
            if demo_passphrase is not None:
                _say(f"demo passphrase: {demo_passphrase}   {DEMO_LINE_NOTE}")
            _say("Ctrl-C locks the vault and quits.")
            announced = True
            if agent is not None:
                agent.start()
            if in_window:
                if _show_window(server.url):
                    stop.ask("close")
                else:
                    _explain("The window could not be opened: using the browser instead.")
                    _open_browser(server.url)
            elif open_browser:
                _open_browser(server.url)
            stop.wait()
        except KeyboardInterrupt:
            stop.ask("interrupt")
        finally:
            if agent is not None:
                agent.stop()
            service.close(stop.reason)
            server.stop()
            if demo and announced and again is not None:
                _say_last(OWN_VAULT_LINE + again)
            _restore_signals(previous)
    finally:
        if demo_directory is not None:
            shutil.rmtree(demo_directory, ignore_errors=True)
        if demo_alive is not None:
            os.close(demo_alive)
    return 0


_Handlers = dict[int, object]


def _handle_signals(stop: _Stop) -> _Handlers:
    """Make Ctrl-C and the signals that end a program lock the vault first.

    Returns what was installed before, for :func:`_restore_signals`. Outside
    the main thread nothing can be installed, and nothing is.
    """
    previous: _Handlers = {}
    if threading.current_thread() is not threading.main_thread():
        return previous

    def interrupted(signum: int, frame: FrameType | None) -> None:
        stop.signalled("interrupt")

    def ended(signum: int, frame: FrameType | None) -> None:
        stop.signalled("signal")

    wanted = [(signal.SIGINT, interrupted), (signal.SIGTERM, ended)]
    hangup = getattr(signal, "SIGHUP", None)
    if hangup is not None:
        wanted.append((hangup, ended))
    for number, handler in wanted:
        with contextlib.suppress(OSError, ValueError):
            previous[number] = signal.signal(number, handler)
    return previous


def _restore_signals(previous: _Handlers) -> None:
    for number, handler in previous.items():
        with contextlib.suppress(OSError, ValueError, TypeError):
            signal.signal(number, handler)  # type: ignore[arg-type]
