# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Reading training documents from disk.

Text formats are read as UTF-8. PDFs need the ``pdf`` extra (pypdf). Everything
is read locally; nothing here touches the network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wdva.errors import MissingDependencyError

__all__ = ["Document", "load_document", "load_documents", "load_examples", "validate_example"]

_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".text", ""}


@dataclass(frozen=True)
class Document:
    """A named piece of text to train on."""

    name: str
    text: str


def load_document(path: str | os.PathLike[str]) -> Document:
    """Read one file. Raises if it is missing, unsupported, or has no extractable text."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"document not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        text = _read_pdf(p)
    elif suffix in _TEXT_SUFFIXES:
        try:
            text = p.read_text(encoding="utf-8-sig")  # tolerate a byte-order mark
        except UnicodeDecodeError as exc:
            raise ValueError(f"{p} is not UTF-8 text ({exc.reason} at byte {exc.start})") from None
    else:
        raise ValueError(
            f"unsupported document type {suffix!r} for {p.name}; "
            "supported: .txt, .md, .rst and .pdf (with the 'pdf' extra)"
        )
    if not text.strip():
        raise ValueError(f"no text could be extracted from {p}")
    return Document(name=p.name, text=text)


def load_documents(paths: Iterable[str | os.PathLike[str]]) -> list[Document]:
    """Read several files, failing loudly on the first one that cannot be used."""
    return [load_document(p) for p in paths]


def load_examples(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read prompt/response training examples from a JSON Lines file.

    Each line is an object with either ``{"prompt": ..., "response": ...}`` or
    ``{"messages": [{"role": ..., "content": ...}, ...]}`` ending on an
    assistant turn.
    """
    examples: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8-sig") as fh:  # tolerate a byte-order mark
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except (ValueError, RecursionError) as exc:
                    raise ValueError(f"{path}:{lineno}: not valid JSON: {exc}") from None
                try:
                    validate_example(item)
                except ValueError as exc:
                    raise ValueError(f"{path}:{lineno}: {exc}") from None
                examples.append(item)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not UTF-8 text ({exc.reason} at byte {exc.start})") from None
    if not examples:
        raise ValueError(f"{path} contains no examples")
    return examples


def validate_example(example: Any) -> None:
    """Check the shape of one training example; raise :class:`ValueError` if it is wrong.

    Run on every line by :func:`load_examples` (which adds the file and line
    number) and again by training, for examples that were built in code.
    """
    if not isinstance(example, Mapping):
        raise ValueError("each example must be a JSON object")
    if "messages" in example:
        messages = example["messages"]
        if not isinstance(messages, (list, tuple)) or len(messages) < 2:
            raise ValueError("an example's `messages` must be a list of at least two turns")
        for turn in messages:
            if (
                not isinstance(turn, Mapping)
                or not isinstance(turn.get("role"), str)
                or not isinstance(turn.get("content"), str)
            ):
                raise ValueError(
                    'each turn in `messages` must look like {"role": "user", "content": "..."}'
                )
        if messages[-1]["role"] != "assistant":
            raise ValueError("an example's `messages` must end with an assistant turn")
        if not messages[-1]["content"].strip():
            raise ValueError("an example's response must be a non-empty string")
        return
    if "prompt" not in example or "response" not in example:
        raise ValueError(
            'each example needs either "prompt" and "response", or "messages"; '
            f"got keys {sorted(str(key) for key in example)}"
        )
    if not isinstance(example["prompt"], str) or not example["prompt"].strip():
        raise ValueError("an example's prompt must be a non-empty string")
    if not isinstance(example["response"], str) or not example["response"].strip():
        raise ValueError("an example's response must be a non-empty string")
    if example.get("system") is not None and not isinstance(example["system"], str):
        raise ValueError("an example's system prompt must be a string")


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingDependencyError(
            "reading PDFs needs pypdf. Install it with:  pip install 'wdva[pdf]'"
        ) from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ValueError("it is password-protected")
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # pypdf has its own exception hierarchy
        raise ValueError(f"cannot read PDF {path}: {exc}") from None
