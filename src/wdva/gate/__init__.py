# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The document gate: a local program between an owner's sealed documents and AI agents.

Documents are kept as ordinary WDVA vaults in one directory (the store). While the
owner has it unlocked, agent applications can search it and read single passages
through three read-only tools, inside limits the owner sets. Every release is
recorded, with its exact text, in a sealed and chained record.

Whatever is released to a hosted agent is sent to that agent's provider and cannot
be recalled. The gate decides what leaves, how much, under which name and with
whose approval, and keeps the record. It is a lock, a meter and a record; it is
not a wall between programs that run under the same user account.

Importing this package needs only the core install. The MCP server lives in
:mod:`wdva.gate.mcp_server` and is the only module that imports the MCP SDK.
"""

from __future__ import annotations

from wdva.gate.core import Gate, PassageChangedError, UnknownPassageError
from wdva.gate.errors import (
    ApprovalError,
    GateError,
    KeeperUnavailableError,
    LedgerError,
    PolicyError,
    StoreBusyError,
    StoreFormatError,
)
from wdva.gate.policy import HostedGrantError, Policy
from wdva.gate.store import (
    DocumentInfo,
    DocumentRefusedError,
    Store,
    StoreInfo,
    StoreLocationError,
    StoreLock,
    UnknownDocumentError,
    check_passphrase,
    init_store,
    open_store,
    store_info,
    take_lock,
)
from wdva.gate.types import Backend, Caller, Preview, Reply

__all__ = [
    "ApprovalError",
    "Backend",
    "Caller",
    "DocumentInfo",
    "DocumentRefusedError",
    "Gate",
    "GateError",
    "HostedGrantError",
    "KeeperUnavailableError",
    "LedgerError",
    "PassageChangedError",
    "Policy",
    "PolicyError",
    "Preview",
    "Reply",
    "Store",
    "StoreBusyError",
    "StoreFormatError",
    "StoreInfo",
    "StoreLocationError",
    "StoreLock",
    "UnknownDocumentError",
    "UnknownPassageError",
    "check_passphrase",
    "init_store",
    "open_store",
    "store_info",
    "take_lock",
]
