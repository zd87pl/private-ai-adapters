# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Can the program that agent applications start be started at all?

The configuration an owner copies into an application names this Python and
runs it in isolated mode (``-I``): without the working directory, without
``PYTHON*`` variables and without the per-user site directory. That keeps code
lying in a project folder from taking the gate's place. It also means that a
copy of the package which this process only finds through one of those three
is not found by the command, and the application then shows no tools and says
little about why.

:func:`start_problem` runs this Python once, the way the command does, and has
it look for the package and for the MCP SDK. Nothing is imported from the SDK
and no server is started: the check takes a fraction of a second.
"""

from __future__ import annotations

import subprocess
import sys

from wdva.gate import hostconfig

__all__ = ["NOT_FOUND_MESSAGE", "start_problem"]

NOT_FOUND_MESSAGE = (
    "wdva must be installed in a virtual environment (venv, pipx or uv tool); "
    "a `pip install --user` copy cannot be started safely by an agent host"
)
"""What is wrong when this Python, started in isolated mode, does not find the package."""

_SDK_MISSING = 3
_CHILD = (
    "import importlib.util as u, sys\n"
    "import wdva.gate\n"
    "found = all(u.find_spec(name) is not None for name in ('mcp', 'pydantic', 'anyio'))\n"
    f"sys.exit(0 if found else {_SDK_MISSING})\n"
)


def _child_argv() -> list[str]:
    """This Python as :func:`wdva.gate.hostconfig.host_command` starts it, running the check."""
    return [sys.executable, "-I", "-X", "utf8", "-c", _CHILD]


def start_problem(*, timeout: float = 20.0) -> str | None:
    """Why the command in a configuration would not start, or ``None`` when it would.

    The answer is about the two things the command needs before anything
    else: this package and the MCP SDK, both found by this Python in isolated
    mode. A Python older than 3.11 is not judged here; see
    :func:`wdva.gate.hostconfig.sdk_status` for that. The sentence returned
    starts in lower case, for use after "error:" or after a colon.
    """
    if not sys.executable:
        return "this Python cannot say where its interpreter is, so no command is known"
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter, with a fixed script
            _child_argv(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (
            "this Python could not be started a second time to try the command "
            f"({type(exc).__name__})"
        )
    if done.returncode == 0:
        return None
    if done.returncode == _SDK_MISSING:
        return str(hostconfig.missing_sdk_error())
    return NOT_FOUND_MESSAGE
