# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The product's name, in one place.

Whatever a person or an agent reads that names the product is built from the
four constants below: the start-up lines, the interface, every text an agent
receives, the names of the three tools, the name the server gives itself in an
agent application, the key it gets in an application's configuration and the
name of the per-user data directory. To rename the product, change them here.

What is not the product's name and does not follow these constants:

* the package and its command, ``wdva``: ``wdva gate app`` and the
  ``wdva gate mcp`` line in a configuration stay as they are;
* the environment variable ``WDVA_GATE_STORE``, which belongs to that command;
* words of the file format, in ``store.py`` and ``ledger.py``, and the names
  a record gives the three tools (``messages.TOOL_STATUS`` and its two
  neighbours): a vault written under one name must open under another;
* the request header ``X-WDVA-Gate`` between the page and the app.

This module imports nothing, so that anything may import it.
"""

from __future__ import annotations

import re

__all__ = ["PRODUCT", "SLUG", "TAGLINE", "TOOL_PREFIX", "tool_name"]

PRODUCT = "Locktender"
"""The name a person reads: in the window title, on the terminal, in sentences."""

SLUG = "locktender"
"""The name as an identifier: the data directory, the server's name in an agent
application, the key in a configuration file. Lower-case letters, digits and hyphens."""

TOOL_PREFIX = "locktender"
"""What the names of the three tools start with: ``locktender_status``,
``locktender_search``, ``locktender_read``. Lower-case letters and digits."""

TAGLINE = "A lock, a meter and a record between your private documents and AI apps."
"""One line that says what the product is."""


def tool_name(word: str) -> str:
    """The name agents know a tool by: the prefix, an underscore and ``word``."""
    return f"{TOOL_PREFIX}_{word}"


def _check() -> None:
    """Refuse values that would break a file name, a configuration key or a tool name."""
    for name, value in (("PRODUCT", PRODUCT), ("TAGLINE", TAGLINE)):
        if not value or value != value.strip() or not value.isprintable():
            raise ValueError(f"brand.{name} must be one line of plain text")
    if len(PRODUCT) > 40:
        raise ValueError("brand.PRODUCT must have at most 40 characters")
    if re.fullmatch(r"[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?", SLUG) is None:
        raise ValueError("brand.SLUG must be lower-case letters, digits and hyphens")
    if re.fullmatch(r"[a-z][a-z0-9]{0,15}", TOOL_PREFIX) is None:
        raise ValueError("brand.TOOL_PREFIX must be lower-case letters and digits")


_check()
