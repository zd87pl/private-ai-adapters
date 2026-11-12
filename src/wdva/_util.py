# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Small shared helpers: strict JSON parsing and careful file writes."""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_JSON_DEPTH",
    "atomic_write",
    "check_json_depth",
    "publish",
    "strict_json_loads",
    "write_temp",
]

MAX_JSON_DEPTH = 64
"""How deeply objects and arrays may nest, counting the outermost value as level 1."""

OWNER_ONLY = "0600 on POSIX; on Windows the file inherits its folder's access rules"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number {text[:20]} is out of range")
    return value


def check_json_depth(value: Any, limit: int = MAX_JSON_DEPTH) -> None:
    """Raise :class:`ValueError` if ``value`` nests containers more than ``limit`` deep.

    Iterative, so it cannot itself overflow the stack, and it stops at the first
    path that is too deep, so a self-referential structure is rejected rather
    than followed forever.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            continue
        if depth > limit:
            raise ValueError(f"nested more than {limit} levels deep")
        stack.extend((child, depth + 1) for child in children)


def strict_json_loads(data: bytes | bytearray | memoryview | str) -> Any:
    """Parse JSON the way the vault format requires.

    Plain ``json.loads`` is too forgiving for data that is attacker-controlled
    until authenticated. This version raises :class:`ValueError` for:

    * an object that contains the same key twice (two readers could otherwise
      disagree about which one counts);
    * ``NaN``, ``Infinity`` and numbers too large to represent, which are not
      JSON and cannot be written back out;
    * containers nested more than :data:`MAX_JSON_DEPTH` levels deep. How deep
      ``json`` can go before it runs out of stack differs between Python
      versions; a fixed limit makes every reader and writer agree;
    * anything that is not UTF-8.
    """
    try:
        text = data if isinstance(data, str) else bytes(data).decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except RecursionError:
        raise ValueError(f"nested more than {MAX_JSON_DEPTH} levels deep") from None
    check_json_depth(value)
    return value


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def write_temp(target: Path, data: bytes | bytearray | memoryview) -> str:
    """Write ``data`` to a new owner-only temp file beside ``target`` and flush it to disk.

    Returns the temp file's path. Nothing at ``target`` is touched yet; call
    :func:`publish` to move the file into place, or unlink it to abandon it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return tmp_name


def publish(tmp_name: str, target: Path, *, overwrite: bool) -> None:
    """Atomically move a finished temp file to ``target``. Consumes the temp file.

    With ``overwrite=False`` this fails with :class:`FileExistsError` if anything
    is at ``target`` - including a file another process created a moment ago, or
    a dangling symlink - and it decides that atomically, not by looking first.
    With ``overwrite=True`` the target is replaced in one step; a symlink at the
    target is replaced, not followed.
    """
    try:
        if overwrite:
            os.replace(tmp_name, target)
            return
        try:
            os.link(tmp_name, target)
        except FileExistsError:
            raise FileExistsError(f"{target} already exists") from None
        except (OSError, NotImplementedError):
            _exclusive_copy(tmp_name, target)  # a filesystem without hard links
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)


def _exclusive_copy(tmp_name: str, target: Path) -> None:
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise FileExistsError(f"{target} already exists") from None
    try:
        with os.fdopen(fd, "wb") as out, open(tmp_name, "rb") as src:
            out.write(src.read())
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(target)
        raise


def atomic_write(target: Path, data: bytes | bytearray | memoryview, *, overwrite: bool) -> None:
    """Write ``data`` to ``target`` so that readers see the old content or the new, never a mix.

    The file is written under a temporary name with owner-only permissions,
    flushed to disk, and only then moved into place (see :func:`publish`). A
    failure at any point leaves ``target`` exactly as it was and no temp file
    behind.
    """
    publish(write_temp(target, data), target, overwrite=overwrite)
