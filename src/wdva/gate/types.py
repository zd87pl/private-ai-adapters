# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Small value types shared by the decision logic, the local link and the MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

#: Every result code an agent can receive.
CODES = (
    "OK",
    "LOCKED",
    "NOT_ALLOWED",
    "NOT_FOUND",
    "NEEDS_APPROVAL",
    "OVER_LIMIT",
    "BAD_REQUEST",
    "UNAVAILABLE",
)


@dataclass(frozen=True)
class Caller:
    """Who is calling, as far as the gate knows.

    ``client`` is the name the call is made under. It is a label chosen in the
    configuration of the calling program, not a verified identity. The ``claimed``
    fields are what the program says about itself; they are recorded and shown to
    the owner, and never used for a decision.
    """

    client: str
    claimed_name: str | None = None
    claimed_version: str | None = None
    protocol: str | None = None
    transport: str = "python"  # "stdio" | "cli" | "python"; anything else is recorded as "other"


@dataclass(frozen=True)
class Reply:
    """The answer to one call: a result code and the complete text the caller receives."""

    code: str
    text: str
    released: bool  # True exactly when a release record was written
    seq: int | None = None  # sequence number of that record; None over the local link
    client_seq: int | None = None  # this client's own release counter


@dataclass(frozen=True)
class Preview:
    """What the owner is shown before approving one passage for one client."""

    client: str
    destination: str
    hosted: bool
    pool: str
    claimed: str | None
    ref: str
    collection: str
    title: str
    title_source: str
    passage: int
    passages: int
    mode: str
    needs_approval: bool
    fits_limits: bool
    body: str
    body_sha256: str
    result_chars: int
    doc_chars: int
    doc_released: int
    doc_after: int
    client_day_used: int
    client_day_limit: int


class Backend(Protocol):
    """Anything that answers the three tool calls: a ``Gate``, or a link to one."""

    def status(self, caller: Caller) -> Reply: ...

    def search(self, caller: Caller, query: Any, collection: Any = "", limit: Any = 5) -> Reply: ...

    def read(self, caller: Caller, ref: Any) -> Reply: ...
