# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Errors raised by the document gate. All derive from :class:`wdva.errors.WDVAError`."""

from __future__ import annotations

from wdva.errors import WDVAError


class GateError(WDVAError):
    """Base class for every failure that is specific to the gate."""


class StoreBusyError(GateError):
    """Another process holds the store's lock (it is unlocked or being changed elsewhere)."""


class StoreFormatError(GateError):
    """The directory is not a gate store, or a file in it does not have the expected form."""


class LedgerError(GateError):
    """The record chain does not verify, or a record could not be written.

    ``seq`` is the sequence number of the first record that is missing, damaged or
    out of order, when one is known.
    """

    def __init__(self, message: str, *, seq: int | None = None) -> None:
        super().__init__(message)
        self.seq = seq


class PolicyError(GateError, ValueError):
    """A policy is malformed or breaks a hard ceiling."""


class ApprovalError(GateError):
    """An approval cannot be given: unknown client or passage, or the text changed."""


class KeeperUnavailableError(GateError):
    """No unlocked gate is reachable for this store."""
