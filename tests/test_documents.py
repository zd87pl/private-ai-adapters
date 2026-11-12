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
        (
            '{"prompt": "q", "response": "a"}\n[1, 2]\n',
            r"qa\.jsonl:2: each example must be a JSON object",
        ),
    ],
)
def test_load_examples_errors(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "qa.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_examples(path)


# -- hardening found by adversarial review ---------------------------------


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ('{"question": "q", "answer": "a"}', r'needs either "prompt" and "response"'),
        ('{"messages": "hello"}', "list of at least two turns"),
        ('{"messages": [{"role": "user", "content": "q"}]}', "at least two turns"),
        (
            '{"messages": [{"role": "user", "content": "q"}, {"role": "user", "content": "q"}]}',
            "end with an assistant turn",
        ),
        ('{"messages": [{"role": "user"}, {"role": "assistant", "content": "a"}]}', "each turn"),
        ('{"prompt": 5, "response": "a"}', "prompt must be a non-empty string"),
        ('{"prompt": "q", "response": ""}', "response must be a non-empty string"),
        ('{"prompt": "q", "response": "a", "system": 5}', "system prompt must be a string"),
    ],
)
def test_malformed_examples_are_reported_with_file_and_line(
    tmp_path: Path, row: str, message: str
) -> None:
    """Mistakes should surface when the file is read, not after a model has been loaded."""
    path = tmp_path / "qa.jsonl"
    path.write_text('{"prompt": "ok", "response": "fine"}\n\n' + row + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"qa\.jsonl:3: .*{message}"):
        load_examples(path)


def test_byte_order_marks_are_tolerated(tmp_path: Path) -> None:
    """Windows editors write UTF-8 with a BOM; it must not break parsing or leak into training."""
    doc = tmp_path / "notes.txt"
    doc.write_bytes("﻿greenhouse".encode())
    assert load_document(doc).text == "greenhouse"
    qa = tmp_path / "qa.jsonl"
    qa.write_bytes('﻿{"prompt": "q", "response": "a"}\n'.encode())
    assert load_examples(qa) == [{"prompt": "q", "response": "a"}]


def test_non_utf8_files_are_named_in_the_error(tmp_path: Path) -> None:
    doc = tmp_path / "latin1.txt"
    doc.write_bytes("café".encode("latin-1"))
    with pytest.raises(ValueError, match=r"latin1\.txt is not UTF-8 text"):
        load_document(doc)
    qa = tmp_path / "qa.jsonl"
    qa.write_bytes(b'{"prompt": "caf\xe9", "response": "a"}\n')
    with pytest.raises(ValueError, match=r"qa\.jsonl is not UTF-8 text"):
        load_examples(qa)


@pytest.mark.parametrize(
    "content", [b"", b"<html>not a pdf</html>", minimal_pdf("x")[:120], b"%PDF-1.4\n%%EOF\n"]
)
def test_broken_pdfs_are_a_value_error_naming_the_file(tmp_path: Path, content: bytes) -> None:
    pytest.importorskip("pypdf")
    path = tmp_path / "broken.pdf"
    path.write_bytes(content)
    with pytest.raises(ValueError, match=r"broken\.pdf"):
        load_document(path)


def test_password_protected_pdfs_say_so(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    import io

    writer = pypdf.PdfWriter(clone_from=io.BytesIO(minimal_pdf("secret")))
    try:
        writer.encrypt("hunter2")
    except Exception as exc:  # the optional crypto backend for PDF encryption may be absent
        pytest.skip(f"cannot create an encrypted PDF here: {exc}")
    path = tmp_path / "locked.pdf"
    with path.open("wb") as fh:
        writer.write(fh)
    with pytest.raises(ValueError, match=r"locked\.pdf.*password-protected"):
        load_document(path)
