"""What a walk-through asks about settings files in a home it owns.

    python read_settings.py entry FILE NAME
    python read_settings.py compare OLD NEW NAME
    python read_settings.py tree FOLDER

``entry`` reads FILE (JSON, or TOML for a name that ends in ``.toml``) and
prints, as one JSON object: whether the file parses, the names of all servers
in it, the entry NAME (``null`` when there is none), the file's mode and how
many names it has. ``compare`` says whether NEW, without the entry NAME, parses
to the same value as OLD without it (by value, type and order of keys), and how
the bytes differ: the length of what both begin with and of what both end
with, and the piece between them in each. For an entry that was put in, the
old piece is empty: every byte of the old file stands in the new one, in
order. ``tree`` lists every name under FOLDER with its kind, for the check
that a run left exactly the files it expects.

Nothing here is the app's code: the answers come from ``json`` and ``tomllib``
of the standard library, so that the app's own reader is not what checks the
app's writer. It is not a test module and pytest does not collect it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: no TOML reader in the standard library
    tomllib = None  # type: ignore[assignment]

SECTIONS = {"json": "mcpServers", "toml": "mcp_servers"}


def kind_of(path: str) -> str:
    """The format of a settings file, or of a copy of one (the file's name and an ending)."""
    return "toml" if ".toml" in os.path.basename(path) else "json"


def parse(path: str) -> Any:
    with open(path, "rb") as handle:
        data = handle.read()
    if kind_of(path) == "toml":
        if tomllib is None:
            raise RuntimeError("this Python has no tomllib (it needs Python 3.11)")
        return tomllib.loads(data.decode("utf-8"))
    return json.loads(data.decode("utf-8"))


def without(parsed: Any, section: str, name: str) -> Any:
    """``parsed`` without the entry ``name``; a section that holds nothing else is left out too."""
    if isinstance(parsed, dict) and isinstance(parsed.get(section), dict):
        servers = {key: value for key, value in parsed[section].items() if key != name}
        return {
            key: (servers if key == section else value)
            for key, value in parsed.items()
            if key != section or servers
        }
    return parsed


def same(one: Any, other: Any) -> bool:
    """Equal by value, by type and, for tables, by the order of their keys."""
    if type(one) is not type(other):
        return False
    if isinstance(one, dict):
        return list(one) == list(other) and all(same(one[key], other[key]) for key in one)
    if isinstance(one, list):
        return len(one) == len(other) and all(same(a, b) for a, b in zip(one, other, strict=True))
    if isinstance(one, float) and one != one:  # nan
        return other != other
    return bool(one == other)


def entry(path: str, name: str) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "format": kind_of(path),
        "parses": False,
        "servers": [],
        "entry": None,
    }
    found = os.lstat(path)
    answer["mode"] = oct(stat.S_IMODE(found.st_mode))
    answer["names"] = found.st_nlink
    answer["link"] = stat.S_ISLNK(found.st_mode)
    try:
        parsed = parse(path)
    except (ValueError, UnicodeDecodeError) as error:
        answer["error"] = str(error)
        return answer
    answer["parses"] = True
    answer["keys"] = list(parsed) if isinstance(parsed, dict) else []
    servers = parsed.get(SECTIONS[kind_of(path)]) if isinstance(parsed, dict) else None
    if isinstance(servers, dict):
        answer["servers"] = list(servers)
        answer["entry"] = servers.get(name)
    return answer


def compare(old: str, new: str, name: str) -> dict[str, Any]:
    section = SECTIONS[kind_of(new)]
    with open(old, "rb") as handle:
        before = handle.read()
    with open(new, "rb") as handle:
        after = handle.read()
    head = 0
    while head < min(len(before), len(after)) and before[head] == after[head]:
        head += 1
    tail = 0
    while (
        tail < min(len(before), len(after)) - head
        and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]
    ):
        tail += 1
    return {
        "equal_without_entry": same(
            without(parse(old), section, name), without(parse(new), section, name)
        ),
        "same_bytes": before == after,
        "head": head,
        "tail": tail,
        "old_piece": before[head : len(before) - tail].decode("utf-8", "replace"),
        "new_piece": after[head : len(after) - tail].decode("utf-8", "replace"),
        "old_lines_kept": all(
            line in after.decode("utf-8", "replace").splitlines()
            for line in before.decode("utf-8", "replace").splitlines()
        ),
    }


def tree(folder: str) -> list[str]:
    found: list[str] = []
    for top, folders, files in os.walk(folder):
        for name in sorted(folders):
            path = os.path.join(top, name)
            mark = "@" if os.path.islink(path) else "/"
            found.append(os.path.relpath(path, folder) + mark)
        for name in sorted(files):
            path = os.path.join(top, name)
            found.append(os.path.relpath(path, folder) + ("@" if os.path.islink(path) else ""))
    return sorted(found)


def main(arguments: list[str]) -> int:
    what, *rest = arguments
    if what == "entry":
        print(json.dumps(entry(rest[0], rest[1])))
    elif what == "compare":
        print(json.dumps(compare(rest[0], rest[1], rest[2])))
    elif what == "tree":
        print(json.dumps(tree(rest[0])))
    else:
        sys.stderr.write(f"unknown question: {what}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
