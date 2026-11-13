# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The ``wdva gate`` commands.

``app`` starts the gate with its local interface, ``mcp`` is what an agent
application runs, ``config`` prints the configuration for one. Each handler
imports its module when it runs, so that ``wdva --help`` stays cheap and the
MCP SDK is only loaded by the command that needs it.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Protocol

from wdva.gate import brand
from wdva.gate.errors import GateError
from wdva.gate.paths import ENV_STORE

WINDOWS_MESSAGE = "the gate does not run on Windows yet"


class _SubParsers(Protocol):
    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...


def _refuse_on_windows() -> None:
    if sys.platform == "win32":
        raise GateError(WINDOWS_MESSAGE)


def _cmd_app(args: argparse.Namespace) -> int:
    _refuse_on_windows()
    from wdva.gate.app import run_app

    return run_app(
        store=args.store,
        port=args.port,
        open_browser=not args.no_browser,
        demo=args.demo,
        window=args.window,
        lock_after_hours=args.lock_after,
    )


def _cmd_mcp(args: argparse.Namespace) -> int:
    _refuse_on_windows()
    if sys.version_info < (3, 11):
        raise GateError("the MCP server needs Python 3.11 or newer")
    from wdva.gate.hostconfig import missing_sdk_error

    try:
        from wdva.gate.mcp_server import run_stdio
    except ImportError as exc:
        raise missing_sdk_error() from exc
    run_stdio(args.store, args.client)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    _refuse_on_windows()
    from wdva.gate.app.startcheck import start_problem
    from wdva.gate.hostconfig import known_host, render_text, sdk_status

    text = render_text(args.host, client=args.client, store=args.store)
    status = sdk_status()
    if known_host(args.host).local_program and status["python"] and status["sdk"]:
        # The text names a command. Try it once the way an application would start it,
        # so that nobody copies a configuration that cannot work. (A missing SDK or an
        # old Python is said at the end of the text itself.)
        problem = start_problem()
        if problem is not None:
            raise GateError(problem)
    print(text)
    return 0


def add_gate_parser(subparsers: _SubParsers) -> None:
    """Register ``wdva gate`` and its subcommands on the main parser."""
    gate = subparsers.add_parser(
        "gate",
        help="let AI agents search and read your sealed documents inside limits you set",
        description=f"{brand.PRODUCT}. {brand.TAGLINE} Start it with `wdva gate app`.",
        epilog=(
            "gate commands read no credential from the environment: "
            "WDVA_KEY, WDVA_KEY_FILE and WDVA_PASSPHRASE are ignored"
        ),
    )
    commands = gate.add_subparsers(dest="gate_command", required=True, metavar="GATE_COMMAND")

    def add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = commands.add_parser(name, allow_abbrev=False, **kwargs)
        parser.add_argument(
            "--store",
            metavar="DIR",
            default=None,
            help=f"the store directory (default: {ENV_STORE}, else the per-user location)",
        )
        return parser

    p = add("app", help="start the gate and open its interface in the browser")
    p.add_argument(
        "--port", type=int, default=0, metavar="N", help="local port (default: any free)"
    )
    p.add_argument("--no-browser", action="store_true", help="print the address, open nothing")
    p.add_argument(
        "--window",
        action="store_true",
        help="open the interface in its own window (needs pywebview; experimental)",
    )
    p.add_argument(
        "--demo",
        action="store_true",
        help="run on invented documents in a temporary store, with a simulated agent",
    )
    p.add_argument(
        "--lock-after",
        type=float,
        default=8.0,
        metavar="HOURS",
        help="lock the store this long after it was unlocked (default: 8)",
    )
    p.set_defaults(func=_cmd_app)

    p = add("mcp", help="the MCP server an agent application starts (not for people)")
    p.add_argument(
        "--client", required=True, metavar="NAME", help="the name the calls are made under"
    )
    p.set_defaults(func=_cmd_mcp)

    p = add("config", help="print the configuration that connects one agent application")
    p.add_argument(
        "host", metavar="HOST", help="for example claude-desktop, claude-code, codex, generic"
    )
    p.add_argument(
        "--client", required=True, metavar="NAME", help="the name to register the application under"
    )
    p.set_defaults(func=_cmd_config)
