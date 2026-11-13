"""Turning an uploaded file into text: decoding rules, and the child process that reads PDFs."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from wdva.gate import extract

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

StandIn = Callable[[str], Path]


@pytest.fixture(autouse=True)
def _fresh_pdf_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test asks the child anew whether PDFs can be read."""
    monkeypatch.setattr(extract, "_pdf_checked", None)


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StandIn:
    """Replace the child with a script made of ``source``; PDF support counts as installed.

    The script is told where to leave notes: ``NOTES`` in its source is the
    path of a file that the test can read afterwards.
    """

    def install(source: str) -> Path:
        notes = tmp_path / "notes.txt"
        script = tmp_path / "stand_in.py"
        script.write_text(f"NOTES = {str(notes)!r}\n{source}", encoding="utf-8")
        monkeypatch.setattr(extract, "child_argv", lambda: [sys.executable, str(script)])
        monkeypatch.setattr(extract, "_pdf_checked", True)
        return notes

    return install


def no_child_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> list[str]:
        raise AssertionError("no child process may be started for this file")

    monkeypatch.setattr(extract, "child_argv", refuse)


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def make_pdf(line: str) -> bytes:
    """A one-page PDF with one line of text, written by hand."""
    stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


ECHO = """
import hashlib, json, sys
data = sys.stdin.buffer.read()
print(json.dumps({"ok": True, "text": f"{len(data)} {hashlib.sha256(data).hexdigest()}"}))
"""


# -- text files -----------------------------------------------------------------


def test_supported_types_are_text_files_and_pdf() -> None:
    assert extract.SUPPORTED_SUFFIXES == (".txt", ".md", ".markdown", ".rst", ".text", ".pdf")
    assert extract.MAX_UPLOAD_BYTES == 20 * 1024 * 1024


@pytest.mark.parametrize("name", ["note.txt", "note.md", "a.markdown", "a.rst", "a.text"])
def test_text_file_is_decoded_as_utf8(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    no_child_allowed(monkeypatch)
    text = "Miete: 1.240 €\nZażółć gęślą jaźń\n"
    assert extract.text_from_upload(name, text.encode("utf-8")) == text


@pytest.mark.parametrize("name", ["NOTE.TXT", "Read.Me.Md", "C:\\Users\\me\\note.txt", "a/b.rst"])
def test_only_the_suffix_of_the_name_counts(name: str) -> None:
    assert extract.text_from_upload(name, b"hello") == "hello"


def test_byte_order_mark_is_dropped() -> None:
    assert extract.text_from_upload("a.txt", b"\xef\xbb\xbfhello") == "hello"
    # Only at the very start, where it is a mark and not a character of the text.
    assert extract.text_from_upload("a.txt", b"a\xef\xbb\xbfb") == "a\ufeffb"


@pytest.mark.parametrize(
    "data",
    [
        "Grüße".encode("latin-1"),
        "hello".encode("utf-16"),
        b"ok so far \xff\xfe",
        b"\xed\xa0\x80",  # half of a surrogate pair is not UTF-8
        b"cut off \xe2\x82",
    ],
)
def test_text_that_is_not_utf8_is_refused(data: bytes) -> None:
    with pytest.raises(ValueError, match="not UTF-8"):
        extract.text_from_upload("a.txt", data)


@pytest.mark.parametrize(
    "name", ["a.docx", "a.html", "README", "a.pdf.exe", ".pdf", "a.txt.", "", "pdf", "a.pdf "]
)
def test_unsupported_type_is_refused(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    no_child_allowed(monkeypatch)
    with pytest.raises(ValueError, match="not supported") as caught:
        extract.text_from_upload(name, b"%PDF-1.4 hello")
    assert ".txt" in str(caught.value)
    assert ".pdf" in str(caught.value)


@pytest.mark.parametrize("name", ["a.txt", "a.pdf"])
def test_empty_file_is_refused(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    no_child_allowed(monkeypatch)
    with pytest.raises(ValueError, match="empty"):
        extract.text_from_upload(name, b"")


def test_text_file_with_only_blanks_is_refused() -> None:
    with pytest.raises(ValueError, match="no text"):
        extract.text_from_upload("a.txt", b"\xef\xbb\xbf \n\t\n")


@pytest.mark.parametrize("name", ["a.txt", "a.pdf"])
def test_file_over_the_limit_is_refused(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    no_child_allowed(monkeypatch)
    monkeypatch.setattr(extract, "MAX_UPLOAD_BYTES", 10)
    assert extract.text_from_upload("a.txt", b"0123456789") == "0123456789"
    with pytest.raises(ValueError, match="larger than"):
        extract.text_from_upload(name, b"0123456789x")


# -- the child, replaced by stand-ins ----------------------------------------------


def test_real_child_command_is_isolated() -> None:
    assert extract.child_argv() == [sys.executable, "-I", "-X", "utf8", "-m", "wdva.gate.extract"]


def test_pdf_goes_to_the_child_on_standard_input(stand_in: StandIn) -> None:
    stand_in(ECHO)
    data = b"%PDF-" + os.urandom(3 * 1024 * 1024)  # more than a pipe holds at once
    assert extract.text_from_upload("Scan.PDF", data) == (
        f"{len(data)} {hashlib.sha256(data).hexdigest()}"
    )


def test_child_gets_a_minimal_environment(
    stand_in: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WDVA_PASSPHRASE", "must not travel")
    monkeypatch.setenv("WDVA_GATE_STORE", "/somewhere")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    monkeypatch.setenv("HOME", "/home/someone")
    stand_in(
        "import json, os, sys\n"
        "sys.stdin.buffer.read()\n"
        "print(json.dumps({'ok': True, 'text': json.dumps(dict(os.environ))}))\n"
    )
    seen = json.loads(extract.text_from_upload("a.pdf", b"%PDF-1.4"))
    assert seen["HOME"] == "/home/someone"
    assert set(seen) <= {"PATH", "HOME", "LANG", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert "must not travel" not in json.dumps(seen)


def test_what_the_child_writes_to_standard_error_goes_nowhere(
    stand_in: StandIn, capfd: pytest.CaptureFixture[str]
) -> None:
    stand_in(
        "import json, sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stderr.write('a line of the document, quoted by the parser\\n')\n"
        "print(json.dumps({'ok': True, 'text': 'fine'}))\n"
    )
    assert extract.text_from_upload("a.pdf", b"%PDF-1.4") == "fine"
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_child_that_takes_too_long_is_ended(
    stand_in: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = stand_in(
        "import os, time\n"
        "open(NOTES, 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n"
        'print(\'{"ok": true, "text": "late"}\')\n'
    )
    monkeypatch.setattr(extract, "CHILD_TIMEOUT_SECONDS", 3.0)
    started = time.monotonic()
    with pytest.raises(ValueError, match="too long"):
        extract.text_from_upload("a.pdf", b"%PDF-1.4")
    assert time.monotonic() - started < 30
    assert not is_running(int(notes.read_text()))


def test_child_that_prints_too_much_is_ended(
    stand_in: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = stand_in(
        "import os, sys\n"
        "open(NOTES, 'w').write(str(os.getpid()))\n"
        "sys.stdin.buffer.read()\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'x' * 65536)\n"
    )
    monkeypatch.setattr(extract, "MAX_CHILD_OUTPUT_BYTES", 1024 * 1024)
    started = time.monotonic()
    with pytest.raises(ValueError, match="more text than"):
        extract.text_from_upload("a.pdf", b"%PDF-1.4")
    assert time.monotonic() - started < 30
    assert not is_running(int(notes.read_text()))


def test_output_just_inside_the_limit_is_taken(
    stand_in: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in(
        "import json, sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write(json.dumps({'ok': True, 'text': 'x' * 4000}))\n"
    )
    answer = len(json.dumps({"ok": True, "text": "x" * 4000}))
    monkeypatch.setattr(extract, "MAX_CHILD_OUTPUT_BYTES", answer)
    assert extract.text_from_upload("a.pdf", b"%PDF-1.4") == "x" * 4000
    monkeypatch.setattr(extract, "MAX_CHILD_OUTPUT_BYTES", answer - 1)
    with pytest.raises(ValueError, match="more text than"):
        extract.text_from_upload("a.pdf", b"%PDF-1.4")


@pytest.mark.parametrize(
    "printed",
    [
        "",
        "this is not JSON",
        '["ok", true]',
        '{"ok": true}',
        '{"ok": true, "text": 5}',
        '{"ok": "yes", "text": "words"}',
        '{"ok": true, "text": "one", "text": "two"}',
        '{"ok": true, "text": "cut',
        '{"ok": true, "text": "fine"} and more',
    ],
)
def test_garbage_from_the_child_is_a_failure(printed: str, stand_in: StandIn) -> None:
    stand_in(f"import sys\nsys.stdin.buffer.read()\nsys.stdout.write({printed!r})\n")
    with pytest.raises(ValueError, match="cannot be read"):
        extract.text_from_upload("a.pdf", b"%PDF-1.4")


def test_child_that_ends_with_an_error_is_a_failure(stand_in: StandIn) -> None:
    # Even when what it printed looks like a good answer.
    stand_in(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        'sys.stdout.write(\'{"ok": true, "text": "looks fine"}\')\n'
        "sys.stdout.flush()\n"
        "sys.exit(3)\n"
    )
    with pytest.raises(ValueError, match="cannot be read"):
        extract.text_from_upload("a.pdf", b"%PDF-1.4")


def test_child_that_ends_without_reading_is_a_failure(stand_in: StandIn) -> None:
    stand_in("import sys\nsys.exit(1)\n")
    with pytest.raises(ValueError, match="cannot be read"):
        extract.text_from_upload("a.pdf", b"%PDF-" + bytes(2 * 1024 * 1024))


def test_child_that_cannot_be_started_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "child_argv", lambda: [str(tmp_path / "no-such-program")])
    monkeypatch.setattr(extract, "_pdf_checked", True)
    with pytest.raises(ValueError, match="cannot be read"):
        extract.text_from_upload("a.pdf", b"%PDF-1.4")


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("no_pdf_support", "PDF support is not installed"),
        ("encrypted", "protected with a password"),
        ("unreadable", "cannot be read"),
        ("the lease says: pay 1,240 EUR", "cannot be read"),
        (17, "cannot be read"),
    ],
)
def test_child_reasons_become_fixed_sentences(
    reason: object, expected: str, stand_in: StandIn
) -> None:
    answer = json.dumps({"ok": False, "reason": reason, "text": "ignored"})
    stand_in(f"import sys\nsys.stdin.buffer.read()\nsys.stdout.write({answer!r})\n")
    with pytest.raises(ValueError, match=expected) as caught:
        extract.text_from_upload("a.pdf", b"%PDF-1.4")
    assert "1,240" not in str(caught.value)  # the child's own words are never passed on


def test_pdf_without_text_is_refused(stand_in: StandIn) -> None:
    stand_in(
        'import sys\nsys.stdin.buffer.read()\nprint(\'{"ok": true, "text": " \\\\n\\\\n "}\')\n'
    )
    with pytest.raises(ValueError, match="scanned pages are not converted"):
        extract.text_from_upload("a.pdf", b"%PDF-1.4")


def test_halves_of_surrogate_pairs_are_dropped(stand_in: StandIn) -> None:
    stand_in(
        'import sys\nsys.stdin.buffer.read()\nprint(\'{"ok": true, "text": "a\\\\ud800b"}\')\n'
    )
    text = extract.text_from_upload("a.pdf", b"%PDF-1.4")
    assert text == "ab"
    text.encode("utf-8")


# -- whether PDFs can be read ------------------------------------------------------


@pytest.mark.parametrize(("status", "expected"), [(0, True), (1, False), (2, False)])
def test_pdf_available_asks_the_child_once(
    status: int, expected: bool, stand_in: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = stand_in(
        f"import sys\nopen(NOTES, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nsys.exit({status})\n"
    )
    monkeypatch.setattr(extract, "_pdf_checked", None)
    assert extract.pdf_available() is expected
    assert extract.pdf_available() is expected
    assert notes.read_text() == "--check\n"


def test_pdf_available_is_false_when_the_child_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "child_argv", lambda: [str(tmp_path / "no-such-program")])
    assert extract.pdf_available() is False


def test_pdf_is_refused_without_pdf_support_before_any_child_reads_it(
    stand_in: StandIn, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = stand_in("import sys\nopen(NOTES, 'a').write('started\\n')\nsys.exit(1)\n")
    monkeypatch.setattr(extract, "_pdf_checked", False)
    with pytest.raises(ValueError, match=r"pip install 'wdva\[pdf\]'"):
        extract.text_from_upload("a.pdf", b"%PDF-1.4")
    assert not notes.exists()


# -- the real child -----------------------------------------------------------------


def test_pdf_available_matches_what_is_installed() -> None:
    assert extract.pdf_available() is (importlib.util.find_spec("pypdf") is not None)


def test_real_child_reads_a_real_pdf() -> None:
    pytest.importorskip("pypdf")
    text = extract.text_from_upload("lease.pdf", make_pdf("Rent is due on day 4711"))
    assert "Rent is due on day 4711" in text


def test_real_child_refuses_what_is_not_a_pdf() -> None:
    pytest.importorskip("pypdf")
    with pytest.raises(ValueError, match="cannot be read"):
        extract.text_from_upload("lease.pdf", b"these bytes are not a PDF at all")


def test_real_child_says_when_the_parser_is_missing() -> None:
    if importlib.util.find_spec("pypdf") is not None:
        pytest.skip("pypdf is installed here")
    done = subprocess.run(
        extract.child_argv(), input=make_pdf("anything"), capture_output=True, check=True
    )
    assert json.loads(done.stdout) == {"ok": False, "reason": "no_pdf_support"}
    with pytest.raises(ValueError, match="PDF support is not installed"):
        extract.text_from_upload("lease.pdf", make_pdf("anything"))


def test_real_child_rejects_arguments() -> None:
    done = subprocess.run(
        [*extract.child_argv(), "/etc/hostname"], input=b"", capture_output=True, check=False
    )
    assert done.returncode == 2
    assert done.stdout == b""


def test_child_limits_itself_before_it_imports_the_parser(tmp_path: Path) -> None:
    """A stand-in parser notes the limits in force at the moment it is imported."""
    (tmp_path / "pypdf.py").write_text(
        "import json, resource\n"
        "SEEN = json.dumps({name: resource.getrlimit(getattr(resource, name))[0]\n"
        "                   for name in ('RLIMIT_CPU', 'RLIMIT_AS', 'RLIMIT_FSIZE')})\n"
        "class PdfReader:\n"
        "    is_encrypted = False\n"
        "    def __init__(self, stream):\n"
        "        self.pages = [self]\n"
        "    def extract_text(self):\n"
        "        return SEEN\n",
        encoding="utf-8",
    )
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "from wdva.gate import extract\n"
        "raise SystemExit(extract.main([]))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program], input=b"%PDF-1.4", capture_output=True, check=True
    )
    answer = json.loads(done.stdout)
    assert answer["ok"] is True
    seen = json.loads(answer["text"])
    assert 0 < seen["RLIMIT_CPU"] <= extract.CHILD_TIMEOUT_SECONDS + 10
    assert seen["RLIMIT_FSIZE"] == 0
    if sys.platform.startswith("linux"):
        assert seen["RLIMIT_AS"] == 2 * 1024**3
