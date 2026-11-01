"""
WDVA: Weight-Delta Vault Adapters

Encrypted adapters and local MLX inference. Early-stage: see README.md
for what works today and what does not.

Copyright 2025 Enclave
Licensed under the Apache License, Version 2.0
"""

from wdva.wdva import WDVA
from wdva.crypto import EncryptedAdapter
from wdva.inference import LocalInference

__version__ = "0.1.0.dev0"
__author__ = "Enclave Team"
__license__ = "Apache-2.0"

__all__ = [
    "WDVA",
    "EncryptedAdapter",
    "LocalInference",
    "__version__",
]
