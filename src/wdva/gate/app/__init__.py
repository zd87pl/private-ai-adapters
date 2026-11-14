# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The gate as a local app: one process with the owner's interface on a loopback address."""

from __future__ import annotations

import importlib


def run_app(
    *,
    store: str | None,
    port: int = 0,
    open_browser: bool = True,
    demo: bool = False,
    window: bool = False,
    lock_after_hours: float = 8.0,
    view: str | None = None,
) -> int:
    """Start the app and block until it is stopped. Returns the process exit status."""
    # Loaded by name so that importing this package stays cheap: the server, the
    # service and everything they need are only imported when the app starts.
    main = importlib.import_module("wdva.gate.app.main")
    return int(
        main.run_app(
            store=store,
            port=port,
            open_browser=open_browser,
            demo=demo,
            window=window,
            lock_after_hours=lock_after_hours,
            view=view,
        )
    )
