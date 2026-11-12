# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Allow ``python -m wdva``."""

import sys

from wdva.cli import main

if __name__ == "__main__":
    sys.exit(main())
