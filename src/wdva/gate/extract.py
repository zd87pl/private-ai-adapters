# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Turning one uploaded file into text.

Text files are decoded here, strictly as UTF-8. A PDF needs a parser (the
``pdf`` extra, pypdf), and a parser is a large piece of third-party code that
reads whatever bytes it is handed. The app holds the store key in memory while
it runs, so it never parses a PDF itself: it starts a child process,

    python -I -X utf8 -m wdva.gate.extract

writes the PDF to the child's standard input and reads one JSON object from its
standard output: ``{"ok": true, "text": ...}`` or ``{"ok": false, "reason":
...}``. The child never receives a key. The parent gives it a nearly empty
environment, ends it after :data:`CHILD_TIMEOUT_SECONDS`, stops reading (and
ends it) after :data:`MAX_CHILD_OUTPUT_BYTES`, and throws away what it writes
to standard error. Before it imports the parser the child limits its own
processor time, memory and ability to write files, where the system allows.

The child is not a sandbox and is not cut off from the network. It exists so
that a parser that hangs, grows without bound or crashes ends without taking
the app, and the key in its memory, with it. A flaw in the parser that runs
code still runs that code under the owner's account.

Every failure is a :class:`ValueError` whose message is one sentence that can be
shown to the owner as it is.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
from pathlib import PurePosixPath
from typing import Any

from wdva._util import strict_json_loads
from wdva.gate.hostconfig import install_line

__all__ = [
    "CHILD_TIMEOUT_SECONDS",
    "MAX_CHILD_OUTPUT_BYTES",
    "MAX_UPLOAD_BYTES",
    "SUPPORTED_SUFFIXES",
    "child_argv",
    "pdf_available",
    "run_child",
    "text_from_upload",
]

SUPPORTED_SUFFIXES = (".txt", ".md", ".markdown", ".rst", ".text", ".pdf")
"""The file types that can be added, by suffix (compared without regard to case)."""

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
"""The largest file that is accepted."""

CHILD_TIMEOUT_SECONDS = 60.0
"""How long the child may take for one PDF before it is ended."""

MAX_CHILD_OUTPUT_BYTES = 20 * 1024 * 1024
"""How much the child may print before it is ended."""

_CHECK_TIMEOUT_SECONDS = 30.0
_CHILD_ENVIRONMENT = ("PATH", "HOME", "LANG")
_CHILD_MEMORY_BYTES = 2 * 1024**3
_CHECK_FLAG = "--check"

_UNSUPPORTED = "this type of file is not supported; use " + ", ".join(SUPPORTED_SUFFIXES)
_EMPTY = "the file is empty"
_NO_TEXT = "the file holds no text"
_TOO_LARGE = f"the file is larger than {MAX_UPLOAD_BYTES // 2**20} MiB"
_NOT_UTF8 = "the file is not UTF-8 text"
_NO_PDF_SUPPORT = "PDF support is not installed; install it with: " + install_line("pdf")
_PDF_UNREADABLE = "the PDF cannot be read"
_PDF_ENCRYPTED = "the PDF is protected with a password"
_PDF_NO_TEXT = "the PDF holds no text that can be read; scanned pages are not converted"
_PDF_TOO_MUCH = "the PDF holds more text than can be taken in"
_PDF_TOO_SLOW = "reading the PDF took too long and was stopped"

# What the child may give as a reason. Its own words are never passed on.
_REASONS = {
    "no_pdf_support": _NO_PDF_SUPPORT,
    "encrypted": _PDF_ENCRYPTED,
    "unreadable": _PDF_UNREADABLE,
}

_pdf_check_lock = threading.Lock()
_pdf_checked: bool | None = None


# ---------------------------------------------------------------------------
# The parent's side
# ---------------------------------------------------------------------------


def text_from_upload(name: str, data: bytes) -> str:
    """The text of one uploaded file, given its name and its bytes.

    The name is used for its suffix only; nothing is read from or written to
    disk. The text is returned as it was extracted: cleaning it up is the
    caller's business. Raises :class:`ValueError` with one sentence for a
    person when the type is not supported, the file is empty or too large, a
    text file is not UTF-8, or a PDF cannot be read, holds no text, takes too
    long, or PDF support is not installed.
    """
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(_UNSUPPORTED)
    if not data:
        raise ValueError(_EMPTY)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(_TOO_LARGE)
    if suffix != ".pdf":
        try:
            text = data.decode("utf-8-sig")  # a byte-order mark is dropped
        except UnicodeDecodeError:
            raise ValueError(_NOT_UTF8) from None
        if not text.strip():
            raise ValueError(_NO_TEXT)
        return text
    if not pdf_available():
        raise ValueError(_NO_PDF_SUPPORT)
    text = _text_from_reply(run_child(data))
    if not text.strip():
        raise ValueError(_PDF_NO_TEXT)
    return text


def child_argv() -> list[str]:
    """The command that starts the child which reads a PDF.

    ``-I`` keeps the environment and the current directory from adding to the
    child's import path; ``-X utf8`` fixes the encoding of what it prints.
    """
    return [sys.executable, "-I", "-X", "utf8", "-m", "wdva.gate.extract"]


def pdf_available() -> bool:
    """Whether PDFs can be read: whether the child process finds the parser.

    Asked of the child itself, once per process, because the child's import
    path can differ from this process's. The parser is not imported here.
    """
    global _pdf_checked
    with _pdf_check_lock:
        if _pdf_checked is None:
            _pdf_checked = _check_child()
        return _pdf_checked


def _check_child() -> bool:
    try:
        done = subprocess.run(  # noqa: S603 - a fixed command, see child_argv
            [*child_argv(), _CHECK_FLAG],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_child_environment(),
            timeout=_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _child_environment() -> dict[str, str]:
    """What the child inherits of the environment: where programs are, and nothing personal."""
    return {name: os.environ[name] for name in _CHILD_ENVIRONMENT if name in os.environ}


def run_child(data: bytes) -> bytes:
    """Start the child, hand it ``data`` and return what it printed.

    Raises :class:`ValueError` when the child cannot be started, takes longer
    than :data:`CHILD_TIMEOUT_SECONDS`, prints more than
    :data:`MAX_CHILD_OUTPUT_BYTES` or ends with an error. In each of these
    cases the child is ended before this returns. What the child prints is
    not interpreted here.
    """
    timeout = CHILD_TIMEOUT_SECONDS
    limit = MAX_CHILD_OUTPUT_BYTES
    try:
        child = subprocess.Popen(  # noqa: S603 - a fixed command, see child_argv
            child_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # a parser's complaints can quote the document
            env=_child_environment(),
        )
    except OSError:
        raise ValueError(_PDF_UNREADABLE) from None
    stdin, stdout = child.stdin, child.stdout
    assert stdin is not None
    assert stdout is not None
    printed = bytearray()
    too_much = threading.Event()

    def feed() -> None:
        with contextlib.suppress(OSError, ValueError), stdin:
            stdin.write(data)

    def collect() -> None:
        with contextlib.suppress(OSError, ValueError):
            while chunk := os.read(stdout.fileno(), 65536):
                if len(printed) + len(chunk) > limit:
                    too_much.set()
                    child.kill()
                    return
                printed.extend(chunk)

    threads = [
        threading.Thread(target=feed, name="wdva-gate-pdf-in", daemon=True),
        threading.Thread(target=collect, name="wdva-gate-pdf-out", daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        child.kill()  # has no effect on a child that already ended
        child.wait()
        for thread in threads:
            thread.join(timeout=5)
        complete = not any(thread.is_alive() for thread in threads)
        if complete:
            stdout.close()
    if too_much.is_set():
        raise ValueError(_PDF_TOO_MUCH)
    if timed_out:
        raise ValueError(_PDF_TOO_SLOW)
    if child.returncode != 0 or not complete:
        raise ValueError(_PDF_UNREADABLE)
    return bytes(printed)


def _text_from_reply(reply: bytes) -> str:
    """The text in the child's answer. Anything but a well-formed answer is a failure."""
    try:
        answer = strict_json_loads(reply)
    except ValueError:
        raise ValueError(_PDF_UNREADABLE) from None
    if not isinstance(answer, dict):
        raise ValueError(_PDF_UNREADABLE)
    text = answer.get("text")
    if answer.get("ok") is True and isinstance(text, str):
        return _without_stray_surrogates(text)
    reason = answer.get("reason")
    raise ValueError(
        _REASONS.get(reason, _PDF_UNREADABLE) if isinstance(reason, str) else _PDF_UNREADABLE
    )


def _without_stray_surrogates(text: str) -> str:
    """Drop code points that are halves of a pair and cannot be written as UTF-8."""
    return text.encode("utf-8", "ignore").decode("utf-8")


# ---------------------------------------------------------------------------
# The child's side
# ---------------------------------------------------------------------------


def _limit_child() -> None:
    """Limit this process's own processor time, memory and file writing. Best effort.

    Called by the child before it imports the parser. A limit the system does
    not have or will not set is skipped.
    """
    try:
        import resource
    except ImportError:  # not a POSIX system
        return
    # The parent ends the child after CHILD_TIMEOUT_SECONDS of waiting. The limit
    # on processor time is a little above that: it only matters if the parent is gone.
    seconds = int(CHILD_TIMEOUT_SECONDS) + 5
    wanted = (
        ("RLIMIT_CPU", seconds, seconds + 5),
        ("RLIMIT_AS", _CHILD_MEMORY_BYTES, _CHILD_MEMORY_BYTES),
        ("RLIMIT_FSIZE", 0, 0),  # pipes are not files: the answer can still be printed
    )
    for name, soft, hard in wanted:
        which = getattr(resource, name, None)
        if which is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            resource.setrlimit(which, (soft, hard))


def _read_pdf(data: bytes) -> dict[str, Any]:
    """Parse one PDF and return the answer to print. Runs in the child only."""
    import io

    try:
        from pypdf import PdfReader
    except ImportError:
        return {"ok": False, "reason": "no_pdf_support"}
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return {"ok": False, "reason": "encrypted"}
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:  # the parser has its own exception hierarchy
        return {"ok": False, "reason": "unreadable"}
    return {"ok": True, "text": _without_stray_surrogates(text)}


def main(argv: list[str] | None = None) -> int:
    """The child process: read a PDF from standard input, print one JSON object.

    With ``--check`` nothing is read: the exit status says whether the parser
    is installed where this process can find it.
    """
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == [_CHECK_FLAG]:
        import importlib.util

        return 0 if importlib.util.find_spec("pypdf") is not None else 1
    if arguments:
        return 2
    sys.dont_write_bytecode = True
    _limit_child()
    answer = _read_pdf(sys.stdin.buffer.read())
    sys.stdout.buffer.write(json.dumps(answer, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
