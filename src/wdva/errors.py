# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Exception hierarchy.

:class:`WDVAError` is the base of every failure that is specific to this
package: a vault that cannot be opened, a wiped key, an adapter that does not
fit, training that diverges, a missing optional dependency.

Two other families are deliberately left as the built-in exceptions that Python
programmers already handle:

* invalid arguments raise :class:`ValueError` or :class:`TypeError` (a key of
  the wrong length, an unsupported document type, a malformed example);
* file problems raise :class:`OSError` subclasses (:class:`FileNotFoundError`,
  :class:`FileExistsError`, :class:`PermissionError`).

So ``except (WDVAError, ValueError, OSError)`` covers everything this package
raises on purpose, which is exactly what the command line catches.
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


class WDVAWarning(UserWarning):
    """Base class for every warning this package emits.

    Filter on it to silence, or escalate, all of them at once::

        warnings.simplefilter("error", wdva.WDVAWarning)
    """
