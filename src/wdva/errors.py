# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Exception hierarchy.

Every error raised deliberately by this package derives from :class:`WDVAError`,
so ``except WDVAError`` catches all of them without catching unrelated bugs.
"""

from __future__ import annotations


class WDVAError(Exception):
    """Base class for all errors raised by wdva."""


class VaultFormatError(WDVAError, ValueError):
    """The data is not a WDVA vault, is truncated, or uses an unsupported version."""


class WrongKeyError(WDVAError):
    """The supplied key or passphrase does not match this vault.

    Raised when the key commitment stored in the header does not match the one
    derived from the supplied credential. Because the commitment lives in the
    (unencrypted) header, a header that was modified in storage can also
    surface as this error.
    """


class IntegrityError(WDVAError):
    """Authentication failed: the vault is corrupted or has been tampered with.

    The key matched its commitment, but the ciphertext or header did not
    verify. No plaintext is ever returned when this is raised.
    """


class KeyWipedError(WDVAError):
    """A :class:`~wdva.keys.SecretKey` was used after :meth:`~wdva.keys.SecretKey.wipe`."""


class AdapterMismatchError(WDVAError):
    """The adapter does not fit the base model it is being applied to."""


class TrainingError(WDVAError):
    """Training could not start or diverged."""


class MissingDependencyError(WDVAError, ImportError):
    """An optional dependency (an *extra*) is needed for this operation."""
