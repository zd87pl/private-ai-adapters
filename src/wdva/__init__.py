# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""WDVA - Weight-Delta Vault Adapters.

Encrypted vaults for LoRA/DoRA adapters: fine-tune locally, seal with
XChaCha20-Poly1305, and load straight from ciphertext into memory.

The core (keys, vaults, bundles) depends only on ``pycryptodome``. Training and
inference need the ``ml`` extra and are imported lazily::

    from wdva import SecretKey, AdapterBundle     # core install
    from wdva import WDVA, train_adapter          # needs  pip install 'wdva[ml]'
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wdva._version import __version__
from wdva.bundle import CONTENT_TYPE_ADAPTER, AdapterBundle, TensorInfo
from wdva.errors import (
    AdapterMismatchError,
    IntegrityError,
    KeyWipedError,
    MissingDependencyError,
    TrainingError,
    VaultFormatError,
    WDVAError,
    WDVAWarning,
    WrongKeyError,
)
from wdva.keys import InsecureKeyFileWarning, SecretKey, destroy_key_file
from wdva.vault import (
    Unsealed,
    VaultHeader,
    is_vault,
    read_header,
    seal,
    seal_to_file,
    unseal,
    unseal_file,
)

if TYPE_CHECKING:
    from wdva.engine import DEFAULT_BASE_MODEL, WDVA, AdapterInfo, BaseModelMismatchWarning
    from wdva.training import TrainingConfig, TrainingResult, TrainingWarning, train_adapter

__all__ = [
    "CONTENT_TYPE_ADAPTER",
    "DEFAULT_BASE_MODEL",
    "WDVA",
    "AdapterBundle",
    "AdapterInfo",
    "AdapterMismatchError",
    "BaseModelMismatchWarning",
    "InsecureKeyFileWarning",
    "IntegrityError",
    "KeyWipedError",
    "MissingDependencyError",
    "SecretKey",
    "TensorInfo",
    "TrainingConfig",
    "TrainingError",
    "TrainingResult",
    "TrainingWarning",
    "Unsealed",
    "VaultFormatError",
    "VaultHeader",
    "WDVAError",
    "WDVAWarning",
    "WrongKeyError",
    "__version__",
    "destroy_key_file",
    "is_vault",
    "read_header",
    "seal",
    "seal_to_file",
    "train_adapter",
    "unseal",
    "unseal_file",
]

_LAZY = {
    "DEFAULT_BASE_MODEL": "wdva.engine",
    "WDVA": "wdva.engine",
    "AdapterInfo": "wdva.engine",
    "BaseModelMismatchWarning": "wdva.engine",
    "TrainingConfig": "wdva.training",
    "TrainingResult": "wdva.training",
    "TrainingWarning": "wdva.training",
    "train_adapter": "wdva.training",
}


if not TYPE_CHECKING:
    # Hidden from type checkers on purpose: they would otherwise accept any
    # misspelled `wdva.<name>` as `Any`. They see the real imports above instead.
    def __getattr__(name: str) -> Any:
        module_name = _LAZY.get(name)
        if module_name is None:
            raise AttributeError(f"module 'wdva' has no attribute {name!r}")
        import importlib

        value = getattr(importlib.import_module(module_name), name)
        globals()[name] = value
        return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
