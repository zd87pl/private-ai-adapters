"""Reading training documents and example files."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from wdva import MissingDependencyError
from wdva.documents import Document, load_document, load_documents, load_examples


def minimal_pdf(text: str) -> bytes:
    """A hand-written single-page PDF containing ``text``."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


@pytest.mark.parametrize("name", ["notes.txt", "notes.md", "NOTES.TXT", "notes.rst", "README"])
def test_text_documents(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("grünhaus door code", encoding="utf-8")
    assert load_document(path) == Document(name=name, text="grünhaus door code")


def test_load_documents_fails_loudly(tmp_path: Path) -> None:
    good = tmp_path / "a.txt"
    good.write_text("hello")
    assert [d.name for d in load_documents([good, str(good)])] == ["a.txt", "a.txt"]
    with pytest.raises(FileNotFoundError, match="document not found"):
        load_documents([good, tmp_path / "missing.txt"])
    with pytest.raises(FileNotFoundError):
        load_document(tmp_path)  # a directory is not a document


def test_unsupported_and_empty_documents(tmp_path: Path) -> None:
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValueError, match=r"unsupported document type '\.docx'"):
        load_document(docx)
    blank = tmp_path / "blank.txt"
    blank.write_text("  \n\t ")
    with pytest.raises(ValueError, match="no text"):
        load_document(blank)


def test_pdf_documents(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    path = tmp_path / "paper.pdf"
    path.write_bytes(minimal_pdf("the greenhouse door code"))
    assert "the greenhouse door code" in load_document(path).text


def test_pdf_without_pypdf_explains_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "pypdf":
            raise ImportError("No module named 'pypdf'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    path = tmp_path / "paper.pdf"
    path.write_bytes(minimal_pdf("hello"))
    with pytest.raises(MissingDependencyError, match=r"wdva\[pdf\]"):
        load_document(path)


def test_load_examples(tmp_path: Path) -> None:
    path = tmp_path / "qa.jsonl"
    rows = [
        {"prompt": "q1", "response": "a1"},
        {"messages": [{"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}]},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n", encoding="utf-8")
    assert load_examples(path) == rows


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "no examples"),
        ("\n\n", "no examples"),
        ('{"prompt": "q"\n', r"qa\.jsonl:1: not valid JSON"),
        ('{"prompt": "q", "response": "a"}\n[1, 2]\n', r"qa\.jsonl:2: each line must be"),
    ],
)
def test_load_examples_errors(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "qa.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_examples(path)
