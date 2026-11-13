# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Where a store lives, and the paths inside it that are not sealed files.

A store is one directory. Everything in it is a sealed file except ``run/``,
which holds the lock file of the process that has the store open and, while the
store is unlocked, the socket that agent applications are served through.

Nothing here creates, reads or changes a file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from wdva.gate import brand
from wdva.gate.errors import GateError

__all__ = [
    "EARLIER_STORE_DIR_NAMES",
    "ENV_STORE",
    "SOCKET_PATH_LIMIT",
    "STORE_DIR_NAME",
    "SYNCED_FOLDER_NAMES",
    "check_socket_path",
    "default_store",
    "lock_path",
    "looks_synced",
    "resolve_store",
    "run_dir",
    "socket_path",
    "synced_folder_component",
]

ENV_STORE = "WDVA_GATE_STORE"
"""The environment variable that names the store when no ``--store`` is given."""

STORE_DIR_NAME = brand.SLUG
"""The name of the per-user store directory."""

EARLIER_STORE_DIR_NAMES = ("wdva-gate",)
"""Names the per-user store directory had in earlier previews. A store that lies under one
of them is used as long as none exists under :data:`STORE_DIR_NAME`, so that a new name for
the product does not hide a vault somebody already has."""

SOCKET_PATH_LIMIT = 100
"""The longest socket path, in bytes, that every supported system accepts."""

SYNCED_FOLDER_NAMES = (
    "Dropbox",
    "OneDrive",
    "Google Drive",
    "iCloud Drive",
    "Mobile Documents",
    "CloudStorage",
)
"""Directory names that file-synchronising services use. Compared without regard to case."""

_SYNCED = frozenset(name.casefold() for name in SYNCED_FOLDER_NAMES)


def _absolute(path: str | os.PathLike[str]) -> Path:
    """``path`` with ``~`` expanded, made absolute. Symbolic links are left as they are."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def default_store() -> Path:
    """The per-user store directory.

    A directory named :data:`STORE_DIR_NAME`: on macOS in
    ``~/Library/Application Support``, elsewhere in ``$XDG_DATA_HOME`` when that
    variable holds an absolute path, and in ``~/.local/share`` otherwise.

    While no such directory exists and one with a name from
    :data:`EARLIER_STORE_DIR_NAMES` does, in the same place, that one is the store.
    """
    home = Path.home()
    if sys.platform == "darwin":
        parent = home / "Library" / "Application Support"
    else:
        data_home = os.environ.get("XDG_DATA_HOME", "")
        if data_home and os.path.isabs(data_home):
            parent = Path(data_home)
        else:
            parent = home / ".local" / "share"
    current = parent / STORE_DIR_NAME
    if not current.exists():
        for name in EARLIER_STORE_DIR_NAMES:
            if name != STORE_DIR_NAME and (parent / name).is_dir():
                return parent / name
    return current


def resolve_store(arg: str | os.PathLike[str] | None = None) -> Path:
    """The store directory to use, as an absolute path.

    ``arg`` when one is given, else the environment variable
    ``WDVA_GATE_STORE``, else :func:`default_store`. An empty argument and an
    empty variable count as not given. The directory need not exist.
    """
    if arg is not None and os.fspath(arg):
        return _absolute(arg)
    from_environment = os.environ.get(ENV_STORE, "")
    if from_environment:
        return _absolute(from_environment)
    return _absolute(default_store())


def run_dir(store: str | os.PathLike[str]) -> Path:
    """``<store>/run``: the only directory of a store whose files are not sealed."""
    return Path(store) / "run"


def lock_path(store: str | os.PathLike[str]) -> Path:
    """``<store>/run/lock``: the file whose OS lock says that the store is open for writing."""
    return run_dir(store) / "lock"


def socket_path(store: str | os.PathLike[str]) -> Path:
    """``<store>/run/gate.sock``: where an unlocked store is reached by agent applications."""
    return run_dir(store) / "gate.sock"


def check_socket_path(store: str | os.PathLike[str]) -> Path:
    """The absolute socket path of ``store``, if a local socket can have that path.

    Operating systems cap the length of a socket path at about 104 bytes. A
    longer one raises :class:`~wdva.gate.errors.GateError` before anything is
    unlocked, with the number of bytes it has.
    """
    path = _absolute(socket_path(store))
    size = len(os.fsencode(path))
    if size > SOCKET_PATH_LIMIT:
        raise GateError(
            f"the store path is too long for a local socket ({size} bytes, "
            f"limit {SOCKET_PATH_LIMIT}); choose a shorter --store"
        )
    return path


def synced_folder_component(path: str | os.PathLike[str]) -> str | None:
    """The part of ``path`` that names a synchronised folder, or ``None``.

    A store inside Dropbox, OneDrive, Google Drive or iCloud Drive is copied to
    a provider and to other devices, sealed files and their sizes included. The
    check looks at the names in the path as written and in the path with
    symbolic links followed. It knows the names in :data:`SYNCED_FOLDER_NAMES`
    and nothing else: a synchronised folder under another name is not noticed.
    """
    written = _absolute(path)
    for candidate in (written, Path(os.path.realpath(written))):
        for part in candidate.parts:
            if part.casefold() in _SYNCED:
                return part
    return None


def looks_synced(path: str | os.PathLike[str]) -> bool:
    """Whether ``path`` lies in a folder that a file-synchronising service uses, by its name."""
    return synced_folder_component(path) is not None
