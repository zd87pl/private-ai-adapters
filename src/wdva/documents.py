# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Reading training documents from disk.

Text formats are read as UTF-8. PDFs need the ``pdf`` extra (pypdf). Everything
is read locally; nothing here touches the network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wdva.errors import MissingDependencyError

__all__ = ["Document", "load_document", "load_documents", "load_examples"]

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
        text = p.read_text(encoding="utf-8")
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
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: not valid JSON: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{lineno}: each line must be a JSON object")
            examples.append(item)
    if not examples:
        raise ValueError(f"{path} contains no examples")
    return examples


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingDependencyError(
            "reading PDFs needs pypdf. Install it with:  pip install 'wdva[pdf]'"
        ) from exc
    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)
