"""The gate's store: sealed files, the key slot, the lock, documents and their limits."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from wdva import WrongKeyError, cli, vault
from wdva.gate import ledger as gate_ledger
from wdva.gate import store as gate_store
from wdva.gate.errors import GateError, LedgerError, StoreBusyError, StoreFormatError
from wdva.gate.store import (
    DocumentRefusedError,
    Store,
    StoreLocationError,
    UnknownDocumentError,
    check_passphrase,
    init_store,
    open_store,
    store_info,
    take_lock,
)

from . import gate_support as support
from .gate_support import FAST_SCRYPT, PASSPHRASE

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)


class Crash(BaseException):
    """Stands for the process dying at this point: nothing after it runs."""


@pytest.fixture
def path(tmp_path: Path) -> Path:
    """Where a test's store goes. The directory does not exist yet."""
    return tmp_path / "store"


@pytest.fixture
def sample_path(path: Path) -> Path:
    """A closed store with the sample policy and the four sample documents."""
    support.make_store(path).close()
    return path


@pytest.fixture
def sample(path: Path) -> Iterator[Store]:
    """The same store, open for writing."""
    with support.make_store(path) as store:
        yield store


def add_plain(store: Store, text: str, *, title: str = "note", **options: Any) -> Any:
    """Add ``text`` as it is, with one invented term unless ``terms`` is given."""
    options.setdefault("terms", {"word"})
    return store.add_document(
        collection="home", title=title, text=text, spans=support.plain_spans(text), **options
    )


def lock_is_free(store_dir: Path) -> bool:
    try:
        take_lock(store_dir).release()
    except StoreBusyError:
        return False
    return True


# ---------------------------------------------------------------------------
# What is on disk
# ---------------------------------------------------------------------------


def test_store_holds_only_sealed_files(path: Path) -> None:
    secrets = {
        "collection": "clc8e2f0a1b3d4",
        "client": "cl5a6b7c8d9e0f",
        "title": "Tt 41d3c2b1a0f9 lease",
        "file name": "fn-0f9e8d7c6b5a.txt",
        "file path": "/home/pp6c7d8e9f0a1b/papers",
        "listed string": "Ls 7a6b5c4d3e2f",
        "text": "tx3e4d5c6b7a8f",
        "query": "qy9f8e7d6c5b4a",
        "destination": "Ds-2b3c4d5e6f7a",
        "note": "Nt-8a9b0c1d2e3f",
    }
    with init_store(path, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT) as store:
        policy = store.policy_json()
        policy["collections"][secrets["collection"]] = {
            "note": secrets["note"],
            "allow_hosted": True,
            "show_titles": True,
        }
        policy["clients"][secrets["client"]] = {
            "destination": secrets["destination"],
            "hosted": True,
            "grants": {secrets["collection"]: {"mode": "read"}},
            "limits": {},
        }
        policy["redact_literals"] = [secrets["listed string"]]
        store.set_policy_json(policy, by="test")
        raw = f"The word {secrets['text']} stands in this document. " * 60
        text, spans, terms = support.prepare(raw)
        info = store.add_document(
            collection=secrets["collection"],
            title=secrets["title"],
            text=text,
            spans=spans,
            terms=terms,
            source={"name": secrets["file name"], "path": secrets["file path"]},
            original=raw.encode(),
        )
        released = f"OK: 1 result.\n  | {secrets['text']}\nRelease {secrets['client']} #1."
        store.ledger.append(
            "release",
            support.release_fields(
                secrets["client"],
                1,
                released,
                tool="wdva_search",
                spans=[(info.doc_id, 0, 40)],
                query=secrets["query"],
                destination=secrets["destination"],
            ),
        )
        store.ledger.append(
            "denial",
            {"client": secrets["client"], "code": "OVER_LIMIT", "query": secrets["query"]},
        )

    files = support.sealed_files(path)
    relative = sorted(str(item.relative_to(path)) for item in files)
    assert [name.split("/")[0] for name in relative] == [
        "docs",
        "keyslot.wdva",
        *["ledger"] * 6,
        "originals",
    ]
    assert sorted(item.name for item in path.iterdir()) == [
        "docs",
        "keyslot.wdva",
        "ledger",
        "originals",
        "run",
    ]
    for item in files:
        assert item.read_bytes().startswith(b"WDVA"), item
        assert item.name.endswith(".wdva"), item  # no temporary file is left behind
        assert not item.name.startswith("."), item
        assert vault.read_header(item).public_metadata == {}

    blob = b"\n".join(item.read_bytes() for item in files) + "\n".join(relative).encode()
    assert support.leaked(blob, secrets.values()) == []
    # File names are random, not derived from what the files hold.
    assert info.text_sha256[:12] not in "\n".join(relative)
    assert (info.source_sha256 or "")[:12] not in "\n".join(relative)
    # The search itself works: each marker is found in plain text and in the encodings
    # a careless writer could have used.
    for marker in secrets.values():
        plain = f"xx {marker} yy".encode()
        encodings = [plain, plain.hex().encode(), f"xx {marker} yy".encode("utf-16-le")]
        encodings += [base64.b64encode(b"z" * shift + plain) for shift in range(3)]
        for encoded in encodings:
            assert support.leaked(b"\x00\x01" + encoded + b"\xff", [marker]) == [marker]


def test_files_and_directories_are_owner_only(path: Path) -> None:
    previous = os.umask(0)
    try:
        support.make_store(path).close()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for item in path.rglob("*"):
        expected = 0o700 if item.is_dir() else 0o600
        assert stat.S_IMODE(item.stat().st_mode) == expected, item
    assert (path / "run" / "lock").exists()


def test_opening_and_reading_write_nothing(sample_path: Path) -> None:
    before = support.snapshot(sample_path)
    with support.reopen(sample_path) as store:
        state = store.ledger.replay()
        store.ledger.verify()
        for info in store.documents():
            store.read_document(info.doc_id)
            store.read_original(info.doc_id)
        store.policy_json()
        assert store.unlisted_files() == []
        assert store.check_passphrase(PASSPHRASE)
        assert support.snapshot(sample_path) == before
        # What an unlock adds is its one record, and nothing derived from the documents.
        store.ledger.append("unlock", {"served": ["claude-desktop"], "mixed": False})
        new_record = f"ledger/{state.head.seq:010d}.wdva"
    after = support.snapshot(sample_path)
    assert set(after) - set(before) == {new_record}
    assert all(after[name] == content for name, content in before.items())
    assert [item.name for item in (sample_path / "run").iterdir()] == ["lock"]


def test_store_info_needs_no_key(sample_path: Path, tmp_path: Path) -> None:
    info = store_info(sample_path)
    files = support.sealed_files(sample_path)
    assert info.exists
    assert info.path == sample_path
    assert info.sealed_files == len(files) == 1 + 4 + 4 + 7
    assert info.sealed_bytes == sum(item.stat().st_size for item in files)
    assert (info.documents, info.originals, info.records) == (4, 4, 7)
    assert info.scrypt_n == FAST_SCRYPT
    assert info.synced_folder is None
    newest = support.record_files(sample_path)[-1]
    assert info.head is not None
    assert info.head.seq == 7
    assert info.head.sha256 == hashlib.sha256(newest.read_bytes()).hexdigest()
    assert str(info.head) == f"7:{info.head.sha256[:8]}"
    assert info.head_time is not None
    assert int(info.head_time.timestamp()) == int(newest.stat().st_mtime)
    with support.reopen(sample_path) as store:
        assert store.ledger.replay().head == info.head  # the verified head is the same one
        assert store_info(sample_path) == info  # also while it is open elsewhere

    missing = store_info(tmp_path / "nothing-here")
    assert not missing.exists
    assert (missing.sealed_files, missing.sealed_bytes, missing.head) == (0, 0, None)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not store_info(empty).exists
    assert list(empty.iterdir()) == []  # looking creates nothing


# ---------------------------------------------------------------------------
# The passphrase and the key
# ---------------------------------------------------------------------------


def test_wrong_passphrase_opens_nothing(sample_path: Path) -> None:
    before = support.snapshot(sample_path)
    with pytest.raises(WrongKeyError):
        open_store(sample_path, passphrase="not the passphrase")
    assert lock_is_free(sample_path)

    lock = take_lock(sample_path)
    with pytest.raises(WrongKeyError):
        open_store(sample_path, passphrase="not the passphrase", lock=lock)
    assert not lock.held
    assert lock_is_free(sample_path)

    with pytest.raises(WrongKeyError):
        open_store(sample_path, passphrase="not the passphrase", write=False)
    with pytest.raises(ValueError, match="empty"):
        open_store(sample_path, passphrase="")
    assert lock_is_free(sample_path)
    assert support.snapshot(sample_path) == before


def test_one_scrypt_per_unlock(sample_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stretches: list[int] = []
    real = vault._scrypt

    def counting(passphrase: str, salt: bytes, n: int) -> bytearray:
        stretches.append(n)
        return real(passphrase, salt, n)

    monkeypatch.setattr(vault, "_scrypt", counting)
    with support.reopen(sample_path) as store:
        store.ledger.replay()
        for info in store.documents():
            store.read_document(info.doc_id)
            store.read_original(info.doc_id)
        store.set_policy_json(store.policy_json(), by="test")
        support.add_text(store, "home", "one more", "A note about the garden shed.")
        store.ledger.append("unlock", {"served": []})
        store.ledger.append("lock", {"reason": "close"})
    assert stretches == [FAST_SCRYPT]


def test_lock_is_taken_before_the_passphrase_is_used(
    sample_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_scrypt, real_take = vault._scrypt, gate_store.take_lock

    def scrypt(passphrase: str, salt: bytes, n: int) -> bytearray:
        events.append("passphrase")
        return real_scrypt(passphrase, salt, n)

    def take(store_dir: Any) -> Any:
        events.append("lock")
        return real_take(store_dir)

    monkeypatch.setattr(vault, "_scrypt", scrypt)
    monkeypatch.setattr(gate_store, "take_lock", take)
    open_store(sample_path, passphrase=PASSPHRASE).close()
    assert events == ["lock", "passphrase"]

    # A store that is busy is refused before any work is done with the passphrase.
    events.clear()
    with real_take(sample_path), pytest.raises(StoreBusyError):
        open_store(sample_path, passphrase=PASSPHRASE)
    assert events == ["lock"]


def test_check_passphrase(sample_path: Path, tmp_path: Path) -> None:
    before = support.snapshot(sample_path)
    assert check_passphrase(sample_path, PASSPHRASE) is True
    assert check_passphrase(sample_path, PASSPHRASE + " ") is False
    assert check_passphrase(sample_path, "") is False
    with pytest.raises(StoreFormatError):
        check_passphrase(tmp_path / "nothing-here", PASSPHRASE)
    with pytest.raises(TypeError):
        check_passphrase(sample_path, None)  # type: ignore[arg-type]
    assert not (sample_path / "run" / "gate.sock").exists()
    assert support.snapshot(sample_path) == before

    with support.reopen(sample_path) as store:
        # It needs neither the lock nor the open store, and works beside one.
        assert check_passphrase(sample_path, PASSPHRASE) is True
        assert store.check_passphrase(PASSPHRASE) is True
        assert store.check_passphrase("not the passphrase") is False
        assert store.check_passphrase("") is False

        # A key slot put in place of the real one does not speak for the open store,
        # whatever passphrase it was sealed with.
        other = tmp_path / "other"
        support.make_store(other, documents=(), passphrase="another passphrase").close()
        (sample_path / "keyslot.wdva").write_bytes((other / "keyslot.wdva").read_bytes())
        assert check_passphrase(sample_path, "another passphrase") is True
        assert store.check_passphrase("another passphrase") is False
        assert store.check_passphrase(PASSPHRASE) is False
    with pytest.raises(GateError, match="closed"):
        store.check_passphrase(PASSPHRASE)


def test_rekey_changes_the_passphrase_only(
    sample_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (cli.ENV_KEY, cli.ENV_KEY_FILE, cli.ENV_PASSPHRASE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.vault, "SCRYPT_N_DEFAULT", FAST_SCRYPT)
    answers = [PASSPHRASE, "a new passphrase", "a new passphrase"]
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": answers.pop(0))
    before = support.snapshot(sample_path)
    with support.reopen(sample_path) as store:
        store_id, head = store.store_id, store.ledger.replay().head
        documents = store.documents()

    slot = sample_path / "keyslot.wdva"
    assert cli.main(["rekey", str(slot), "--passphrase", "--new-passphrase"]) == 0
    assert answers == []

    after = support.snapshot(sample_path)
    assert {name for name in after if after[name] != before.get(name)} == {"keyslot.wdva"}
    assert stat.S_IMODE(slot.stat().st_mode) == 0o600
    with pytest.raises(WrongKeyError):
        support.reopen(sample_path)
    with support.reopen(sample_path, passphrase="a new passphrase") as store:
        assert (store.store_id, store.ledger.replay().head) == (store_id, head)
        assert store.documents() == documents
        lease = support.document_id(store, "lease-2025")
        assert support.LEASE_MARKER in store.read_document(lease)["text"]

    # Changing the passphrase destroys nothing: an older copy of the key slot still opens it.
    slot.write_bytes(before["keyslot.wdva"])
    with support.reopen(sample_path) as store:
        assert store.store_id == store_id


def test_close_wipes_the_key_and_releases_the_lock(sample_path: Path) -> None:
    store = support.reopen(sample_path)
    key = store._key
    lease = support.document_id(store, "lease-2025")
    assert not lock_is_free(sample_path)
    store.close()
    store.close()  # twice is harmless
    assert store.closed
    assert key.wiped
    assert lock_is_free(sample_path)
    for call in (
        store.documents,
        store.policy_json,
        store.unlisted_files,
        lambda: store.read_document(lease),
        lambda: store.read_original(lease),
        lambda: store.remove_document(lease),
        lambda: store.set_policy_json({}, by="test"),
        lambda: add_plain(store, "text"),
    ):
        with pytest.raises(GateError, match="closed"):
            call()
    with pytest.raises(LedgerError, match="closed"):
        store.ledger.replay()
    with pytest.raises(LedgerError, match="closed"):
        store.ledger.append("status", {"client": "claude-desktop"})


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

_TRY_LOCK = """
import sys
from wdva.gate.errors import StoreBusyError
from wdva.gate.store import take_lock
try:
    lock = take_lock(sys.argv[1])
except StoreBusyError:
    print("busy")
else:
    print("taken")
    sys.stdout.flush()
    if sys.argv[2] == "die":
        import os
        os._exit(0)  # no clean-up of any kind
    lock.release()
"""


def try_lock_elsewhere(store_dir: Path, then: str = "release") -> str:
    done = subprocess.run(
        [sys.executable, "-c", _TRY_LOCK, str(store_dir), then],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout.strip()


def test_second_opener_is_refused(sample_path: Path) -> None:
    with support.reopen(sample_path):
        with pytest.raises(StoreBusyError, match="open in another program"):
            support.reopen(sample_path)
        with pytest.raises(StoreBusyError):
            take_lock(sample_path)
        assert try_lock_elsewhere(sample_path) == "busy"
    assert try_lock_elsewhere(sample_path) == "taken"

    # The lock alone, before any passphrase, already keeps a second opener out.
    lock = take_lock(sample_path)
    with pytest.raises(StoreBusyError):
        support.reopen(sample_path)
    assert try_lock_elsewhere(sample_path) == "busy"
    with support.reopen(sample_path, lock=lock) as store:
        assert lock.held
        assert try_lock_elsewhere(sample_path) == "busy"
        assert store.writable
    assert not lock.held  # the store released the lock it was given
    lock.release()  # twice is harmless

    # A process that dies with the lock does not leave the store locked.
    assert try_lock_elsewhere(sample_path, "die") == "taken"
    support.reopen(sample_path).close()


def test_lock_needs_a_store_directory(tmp_path: Path) -> None:
    with pytest.raises(StoreFormatError, match="no store"):
        take_lock(tmp_path / "nothing-here")
    assert not (tmp_path / "nothing-here").exists()
    with pytest.raises(StoreFormatError, match="no store"):
        open_store(tmp_path / "nothing-here", passphrase=PASSPHRASE)
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    with pytest.raises(StoreFormatError, match="no store"):
        open_store(empty, passphrase=PASSPHRASE)
    assert list(empty.iterdir()) == []  # not even run/ was created


def test_a_lock_belongs_to_one_store(sample_path: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    support.make_store(other, documents=()).close()
    lock = take_lock(other)
    with pytest.raises(ValueError, match="lock"):
        open_store(sample_path, passphrase=PASSPHRASE, lock=lock)
    assert not lock.held  # the lock handed in is given up when opening fails
    with pytest.raises(ValueError, match="writing"):
        open_store(sample_path, passphrase=PASSPHRASE, write=False, lock=take_lock(sample_path))
    assert lock_is_free(sample_path)
    assert lock_is_free(other)


def test_read_only_open_takes_no_lock_and_changes_nothing(sample_path: Path) -> None:
    before = support.snapshot(sample_path)
    with support.reopen(sample_path) as writer:
        with support.reopen(sample_path, write=False) as reader:
            assert not reader.writable
            assert reader.documents() == writer.documents()
            lease = support.document_id(reader, "lease-2025")
            assert support.LEASE_MARKER in reader.read_document(lease)["text"]
            with pytest.raises(GateError, match="reading only"):
                add_plain(reader, "some text")
            with pytest.raises(GateError, match="reading only"):
                reader.remove_document(lease)
            with pytest.raises(GateError, match="reading only"):
                reader.set_policy_json(reader.policy_json(), by="test")
            with pytest.raises(LedgerError, match="reading only"):
                reader.ledger.append("status", {"client": "claude-desktop"})
        assert not lock_is_free(sample_path)  # closing the reader did not touch the writer's lock
    assert support.snapshot(sample_path) == before


def test_read_only_store_does_not_even_try_to_write(
    sample_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[Path] = []

    def write(target: Path, data: bytes, *, overwrite: bool) -> None:
        attempts.append(target)

    monkeypatch.setattr(gate_store, "atomic_write", write)
    monkeypatch.setattr(gate_ledger, "atomic_write", write)
    monkeypatch.setattr(gate_store, "_delete", attempts.append)
    with support.reopen(sample_path, write=False) as reader:
        lease = support.document_id(reader, "lease-2025")
        for call in (
            lambda: add_plain(reader, "some text", original=b"some bytes"),
            lambda: reader.remove_document(lease),
            lambda: reader.set_policy_json(reader.policy_json(), by="test"),
            lambda: reader.ledger.append("status", {"client": "claude-desktop"}),
        ):
            with pytest.raises(GateError, match="reading only"):
                call()
    assert attempts == []


# ---------------------------------------------------------------------------
# One kind of file cannot stand in for another
# ---------------------------------------------------------------------------


def test_content_types_are_checked(sample_path: Path) -> None:
    with support.reopen(sample_path) as store:
        lease = support.document_id(store, "lease-2025")
        document_file = sample_path / "docs" / f"{lease}.wdva"
        original_file = sample_path / "originals" / f"{lease}.wdva"
        sealed_document = document_file.read_bytes()
        sealed_original = original_file.read_bytes()
        sealed_record = support.record_files(sample_path)[3].read_bytes()

        document_file.write_bytes(sealed_record)
        with pytest.raises(StoreFormatError, match="another kind of sealed file"):
            store.read_document(lease)
        document_file.write_bytes(sealed_original)
        with pytest.raises(StoreFormatError, match="another kind of sealed file"):
            store.read_document(lease)
        document_file.write_bytes(sealed_document)
        assert store.read_document(lease)["doc_id"] == lease

        original_file.write_bytes(sealed_document)
        with pytest.raises(StoreFormatError, match="another kind of sealed file"):
            store.read_original(lease)
        original_file.write_bytes(sealed_record)
        with pytest.raises(StoreFormatError, match="another kind of sealed file"):
            store.read_original(lease)
        original_file.write_bytes(sealed_original)
        assert store.read_original(lease) == support.LEASE_TEXT.encode()

    # A document where a record should be: the record refuses it.
    record_file = support.record_files(sample_path)[3]
    record_file.write_bytes(sealed_document)
    with support.reopen(sample_path) as store:
        with pytest.raises(LedgerError, match="another kind of sealed file") as refused:
            store.ledger.replay()
        assert refused.value.seq == 4
    record_file.write_bytes(sealed_record)

    # Another kind of file where the key slot should be, sealed with the right passphrase.
    slot = sample_path / "keyslot.wdva"
    real_slot = slot.read_bytes()
    slot_payload = vault.unseal(real_slot, passphrase=PASSPHRASE).payload
    slot.write_bytes(
        vault.seal(
            slot_payload,  # everything a key slot holds, under the label of a document
            passphrase=PASSPHRASE,
            scrypt_n=FAST_SCRYPT,
            content_type=gate_store.DOCUMENT_TYPE,
        )
    )
    with pytest.raises(StoreFormatError, match="another kind of sealed file"):
        support.reopen(sample_path)
    with pytest.raises(StoreFormatError, match="another kind of sealed file"):
        check_passphrase(sample_path, PASSPHRASE)
    # And the other way round: the label of a key slot on something that is none.
    slot.write_bytes(
        vault.seal(
            b'{"format": "application/vnd.wdva.gate.document.v1"}',
            passphrase=PASSPHRASE,
            scrypt_n=FAST_SCRYPT,
            content_type=gate_store.KEYSLOT_TYPE,
        )
    )
    with pytest.raises(StoreFormatError, match="malformed"):
        support.reopen(sample_path)
    assert lock_is_free(sample_path)
    # A key slot that is opened with a key file is not one this version opens.
    slot.write_bytes(vault.seal(b"{}", key=os.urandom(32), content_type=gate_store.KEYSLOT_TYPE))
    with pytest.raises(StoreFormatError, match="key file"):
        support.reopen(sample_path)
    slot.write_bytes(real_slot)
    support.reopen(sample_path).close()


def forged_document(store: Store, doc_id: str, **changes: Any) -> bytes:
    """A document file as someone holding the store key could write it."""
    payload = store.read_document(doc_id)
    payload.update(changes)
    return vault.seal(
        json.dumps(payload).encode(), key=store._key, content_type=gate_store.DOCUMENT_TYPE
    )


def test_document_file_swapped_or_put_back_is_ignored(sample_path: Path) -> None:
    with support.reopen(sample_path) as store:
        lease = support.document_id(store, "lease-2025")
        bank = support.document_id(store, "bank-letter")
        lease_file = sample_path / "docs" / f"{lease}.wdva"
        bank_file = sample_path / "docs" / f"{bank}.wdva"
        sealed_lease, sealed_bank = lease_file.read_bytes(), bank_file.read_bytes()

        lease_file.write_bytes(sealed_bank)
        bank_file.write_bytes(sealed_lease)
        for doc_id in (lease, bank):
            with pytest.raises(StoreFormatError, match="holds another document"):
                store.read_document(doc_id)
        lease_file.write_bytes(sealed_lease)
        bank_file.write_bytes(sealed_bank)
        assert support.BANK_MARKER in store.read_document(bank)["text"]

        # The right id with other text is not the document that was added.
        other_text = forged_document(store, lease, text="Other words under the same name.")
        other_place = forged_document(store, lease, collection="medical")
        lease_file.write_bytes(other_text)
        with pytest.raises(StoreFormatError, match="does not hold the text that was added"):
            store.read_document(lease)
        lease_file.write_bytes(other_place)
        with pytest.raises(StoreFormatError, match="does not match its entry"):
            store.read_document(lease)
        lease_file.unlink()
        with pytest.raises(StoreFormatError, match="cannot be read"):
            store.read_document(lease)
        assert lease in [info.doc_id for info in store.documents()]  # listed, but unavailable
        lease_file.write_bytes(sealed_lease)

        # A removed document that is put back is not a document.
        original_file = sample_path / "originals" / f"{bank}.wdva"
        sealed_original = original_file.read_bytes()
        store.remove_document(bank)
        assert not bank_file.exists()
        assert not original_file.exists()
        bank_file.write_bytes(sealed_bank)
        original_file.write_bytes(sealed_original)
        assert bank not in [info.doc_id for info in store.documents()]
        with pytest.raises(UnknownDocumentError):
            store.read_document(bank)
        with pytest.raises(UnknownDocumentError):
            store.read_original(bank)
        assert store.unlisted_files() == [bank_file, original_file]
        # Its id is never used again, so no later add can adopt the file either.
        with pytest.raises(ValueError, match="used before"):
            support.add_text(store, "home", "newcomer", "Entirely new words.", doc_id=bank)

    with support.reopen(sample_path) as store:
        assert [info.title for info in store.documents()] == [
            "lease-2025",
            "tax-note-2025",
            "referral-2026",
        ]
        with pytest.raises(UnknownDocumentError):
            store.read_document(bank)
        assert store.unlisted_files() == [bank_file, original_file]


def test_originals_are_bound_to_their_document(sample: Store) -> None:
    lease = support.document_id(sample, "lease-2025")
    bank = support.document_id(sample, "bank-letter")
    assert sample.read_original(lease) == support.LEASE_TEXT.encode()
    lease_file = sample.path / "originals" / f"{lease}.wdva"
    lease_file.write_bytes((sample.path / "originals" / f"{bank}.wdva").read_bytes())
    with pytest.raises(StoreFormatError, match="not the file that was added"):
        sample.read_original(lease)
    lease_file.unlink()
    with pytest.raises(StoreFormatError, match="cannot be read"):
        sample.read_original(lease)

    kept_none = support.add_text(sample, "home", "typed in", "A note without a file behind it.")
    assert kept_none.original is False
    assert kept_none.source_sha256 is None
    with pytest.raises(UnknownDocumentError, match="no original"):
        sample.read_original(kept_none.doc_id)
    with pytest.raises(UnknownDocumentError):
        sample.read_original("d-000000000000")


# ---------------------------------------------------------------------------
# Adding and removing
# ---------------------------------------------------------------------------


def test_add_document_seals_and_records(path: Path, gate_clock: Any) -> None:
    with support.make_store(path, clock=gate_clock, documents=()) as store:
        raw = support.LEASE_TEXT.encode()
        text, spans, terms = support.prepare(support.LEASE_TEXT)
        gate_clock.advance(minutes=5)
        info = store.add_document(
            collection="home",
            title="lease-2025",
            text=text,
            spans=spans,
            terms=terms,
            title_source="file-name",
            source={"name": "lease-2025.txt", "path": "/home/me/lease-2025.txt"},
            original=raw,
        )
        assert info.doc_id.startswith("d-")
        assert len(info.doc_id) == 14
        assert (info.collection, info.title, info.title_source) == (
            "home",
            "lease-2025",
            "file-name",
        )
        assert (info.chars, info.terms, info.passages) == (len(text), len(terms), len(spans))
        assert info.text_sha256 == hashlib.sha256(text.encode()).hexdigest()
        assert info.source_sha256 == hashlib.sha256(raw).hexdigest()
        assert info.original is True
        assert info.added == gate_clock()
        assert store.documents() == [info]
        assert store.document(info.doc_id) is info
        assert store.find_duplicate(text_sha256=info.text_sha256) is info

        payload = store.read_document(info.doc_id)
        assert payload == {
            "format": "application/vnd.wdva.gate.document.v1",
            "doc_id": info.doc_id,
            "collection": "home",
            "title": "lease-2025",
            "title_source": "file-name",
            "source": {
                "name": "lease-2025.txt",
                "path": "/home/me/lease-2025.txt",
                "sha256": info.source_sha256,
                "bytes": len(raw),
            },
            "added": "2026-11-12T09:05:00Z",
            "sanitizer": 1,
            "text_sha256": info.text_sha256,
            "terms": len(terms),
            "text": text,
        }
        assert store.read_original(info.doc_id) == raw

        record = store.ledger.read(info.seq)
        assert record.kind == "add"
        assert {name: record.data[name] for name in record.data if name not in ("prev",)} == {
            "v": 1,
            "seq": 4,
            "time": "2026-11-12T09:05:00Z",
            "kind": "add",
            "doc_id": info.doc_id,
            "collection": "home",
            "title": "lease-2025",
            "title_source": "file-name",
            "text_sha256": info.text_sha256,
            "source_sha256": info.source_sha256,
            "chars": len(text),
            "terms": len(terms),
            "passages": len(spans),
            "original": True,
        }
        assert header_type(path / "docs" / f"{info.doc_id}.wdva") == gate_store.DOCUMENT_TYPE
        assert header_type(path / "originals" / f"{info.doc_id}.wdva") == gate_store.ORIGINAL_TYPE
        assert header_type(path / "keyslot.wdva") == gate_store.KEYSLOT_TYPE
        assert header_type(path / "ledger" / "0000000004.wdva") == gate_ledger.LEDGER_TYPE

    with support.reopen(path, clock=gate_clock) as store:
        assert store.documents() == [info]
        assert store.read_document(info.doc_id) == payload
        assert (store.chunk_chars, store.sanitizer) == (800, 1)


def header_type(item: Path) -> str:
    return vault.read_header(item).content_type


def test_add_document_checks_what_it_is_given(sample: Store) -> None:
    text = "Some words about the garden shed and its roof. " * 40
    spans = support.plain_spans(text)
    good: dict[str, Any] = {
        "collection": "home",
        "title": "shed",
        "text": text,
        "spans": spans,
        "terms": {"shed", "roof"},
    }
    before = support.snapshot(sample.path)
    head = sample.ledger.head()
    last = len(text)
    bad_values: list[tuple[str, Any]] = [
        ("collection", "Home"),
        ("collection", ""),
        ("collection", "a" * 33),
        ("collection", "-home"),
        ("collection", "home\n"),
        ("title", ""),
        ("title", "x" * 121),
        ("title", "two\nlines"),
        ("title", " padded "),
        ("title", "tab\there"),
        ("title", "bell\x07"),
        ("title", "half \ud800 pair"),
        ("title_source", "guess"),
        ("text", "lone \udc00 half"),
        ("spans", []),
        ("spans", spans[:-1]),  # does not reach the end
        ("spans", spans[1:]),  # does not start at 0
        ("spans", [(0, 801), (801, last)]),  # a passage longer than the store's limit
        ("spans", [(0, 800), (810, last)]),  # a gap
        ("spans", [(0, 800), (790, last)]),  # an overlap
        ("spans", [(0, 0), *spans]),  # an empty passage
        ("spans", [*spans, (last, last + 5)]),  # past the end
        ("spans", [(0.0, 800.0)]),
        ("spans", [(0,)]),
        ("source", {"name": "shed.txt", "owner": "me"}),
        ("source", {"sha256": "ABC"}),
        ("source", {"bytes": -1}),
        ("source", {"name": 7}),
        ("doc_id", "d-123"),
        ("doc_id", "x-0123456789ab"),
        ("doc_id", "d-0123456789AB"),
    ]
    for name, value in bad_values:
        with pytest.raises(ValueError):  # noqa: PT011 - each row is refused for its own reason
            sample.add_document(**{**good, name: value})
    for name, value in (("text", b"bytes"), ("terms", "shed roof"), ("original", "text")):
        with pytest.raises(TypeError):
            sample.add_document(**{**good, name: value})
    with pytest.raises(TypeError):
        sample.add_document(**{**good, "terms": {1, 2}})
    # An original that is not the file the source describes.
    with pytest.raises(ValueError, match="does not describe"):
        sample.add_document(**good, original=b"bytes", source={"bytes": 4})
    with pytest.raises(ValueError, match="does not describe"):
        sample.add_document(**good, original=b"bytes", source={"sha256": "0" * 64})
    assert support.snapshot(sample.path) == before
    assert sample.ledger.head() == head

    added = sample.add_document(**good, doc_id="d-0123456789ab")
    assert added.doc_id == "d-0123456789ab"
    assert added.passages == len(spans)
    longer = text + "and more"
    with pytest.raises(ValueError, match="used before"):
        sample.add_document(
            **{**good, "text": longer, "spans": support.plain_spans(longer)},
            doc_id="d-0123456789ab",
        )


def test_size_limits_at_add(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.make_store(path, documents=(), policy=False) as store:

        def refused(reason: str, call: Callable[[], Any]) -> DocumentRefusedError:
            before = support.snapshot(path)
            with pytest.raises(DocumentRefusedError) as caught:
                call()
            assert caught.value.reason == reason
            assert support.snapshot(path) == before  # a refusal writes nothing
            return caught.value

        refused("empty", lambda: add_plain(store, ""))

        # Characters of one document.
        limit = gate_store.MAX_TEXT_CHARS
        assert limit == 4_000_000
        refused("too large", lambda: add_plain(store, "a" * (limit + 1)))
        assert add_plain(store, "a" * limit, title="longest").chars == limit

        # Distinct terms of one document.
        limit = gate_store.MAX_DOCUMENT_TERMS
        assert limit == 200_000
        many = {f"t{number}" for number in range(limit + 1)}
        refused("too large", lambda: add_plain(store, "many terms", terms=many))
        many.discard("t0")
        assert add_plain(store, "many terms", title="widest", terms=many).terms == limit

        # The size of the file a document came from, stated or given.
        limit = gate_store.MAX_SOURCE_BYTES
        assert limit == 64 * 1024 * 1024
        digest = "ab" * 32
        refused(
            "too large",
            lambda: add_plain(store, "big file", source={"bytes": limit + 1, "sha256": digest}),
        )
        add_plain(store, "big file", title="big", source={"bytes": limit, "sha256": digest})
        monkeypatch.setattr(gate_store, "MAX_SOURCE_BYTES", 1000)
        refused("too large", lambda: add_plain(store, "kept file", original=b"x" * 1001))
        assert add_plain(store, "kept file", title="kept", original=b"x" * 1000).original

        # Characters of the whole store.
        total = sum(info.chars for info in store.documents())
        monkeypatch.setattr(gate_store, "MAX_STORE_CHARS", total + 100)
        refused("too large", lambda: add_plain(store, "b" * 101))
        assert add_plain(store, "b" * 100, title="last that fits").chars == 100
        refused("too large", lambda: add_plain(store, "c"))


def test_store_term_limit_counts_distinct_terms(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate_store, "MAX_STORE_TERMS", 30)
    words = [f"word{number:02d}" for number in range(41)]

    def add(store: Store, title: str, chosen: list[str], **options: Any) -> Any:
        text, spans, terms = support.prepare(f"{title}: " + " ".join(chosen))
        return store.add_document(
            collection="home", title=title, text=text, spans=spans, terms=terms, **options
        )

    with support.make_store(path, documents=(), policy=False) as store:
        # "first:" and the like are terms too, so each document has its 14 words and its title.
        add(store, "first", words[:14])
        add(store, "second", words[:14])
        assert sum(info.terms for info in store.documents()) == 30
        before = support.snapshot(path)

        # The sum of the documents' own counts passes the limit with the third document.
        # Without a way to count exactly, the store refuses.
        with pytest.raises(DocumentRefusedError, match="distinct search terms"):
            add(store, "third", words[:14])
        assert support.snapshot(path) == before
        # Counted exactly, the three documents share their words: 14 words and 3 titles.
        add(store, "third", words[:14], tokenize=support.tokenize)
        assert sum(info.terms for info in store.documents()) == 45

        # 17 terms so far. 13 new ones reach the limit exactly; one more passes it.
        with pytest.raises(DocumentRefusedError, match="distinct search terms") as refused:
            add(store, "fourth", words[14:27], tokenize=support.tokenize)
        assert refused.value.reason == "too large"
        add(store, "fourth", words[14:26], tokenize=support.tokenize)
        with pytest.raises(DocumentRefusedError, match="distinct search terms"):
            add(store, "fifth", [], tokenize=support.tokenize)
        # A document that brings nothing new still fits.
        text, spans, terms = support.prepare(" ".join(words[:26]))
        store.add_document(
            collection="home",
            title="nothing new",
            text=text,
            spans=spans,
            terms=terms,
            tokenize=support.tokenize,
        )


def test_duplicates_are_recognised_by_source_and_by_text(sample: Store) -> None:
    lease = sample.document(support.document_id(sample, "lease-2025"))
    before = support.snapshot(sample.path)

    def refused(call: Callable[[], Any]) -> None:
        with pytest.raises(DocumentRefusedError) as caught:
            call()
        assert caught.value.reason == "already in the store"
        assert caught.value.duplicate_of == lease.doc_id
        assert support.snapshot(sample.path) == before

    # The same bytes, offered for another collection under another name.
    refused(
        lambda: support.add_text(
            sample, "tax", "a copy", support.LEASE_TEXT, name="copy.txt", keep_original=True
        )
    )
    # Other bytes that clean to the same text.
    zero_width_space = chr(0x200B)
    other_bytes = support.LEASE_TEXT.replace("\n", "  \r\n").replace(
        "rent", f"re{zero_width_space}nt"
    )
    assert other_bytes.encode() != support.LEASE_TEXT.encode()
    assert support.prepare(other_bytes)[0] == support.prepare(support.LEASE_TEXT)[0]
    refused(lambda: support.add_text(sample, "medical", "again", other_bytes, name="crlf.txt"))
    refused(lambda: support.add_text(sample, "medical", "typed", other_bytes))
    # The same file, from which other text was taken this time.
    refused(
        lambda: add_plain(
            sample, "What another extraction made of it.", source={"sha256": lease.source_sha256}
        )
    )
    assert sample.find_duplicate(source_sha256=lease.source_sha256) == lease
    assert sample.find_duplicate(text_sha256="0" * 64, source_sha256="1" * 64) is None

    # Once a document is removed, its file can be added again, as a new document.
    sample.remove_document(lease.doc_id)
    again = support.add_text(sample, "home", "lease again", support.LEASE_TEXT, name="lease.txt")
    assert again.doc_id != lease.doc_id
    assert again.text_sha256 == lease.text_sha256


def test_add_document_survives_a_crash_at_every_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_store_write, real_ledger_write = gate_store.atomic_write, gate_ledger.atomic_write
    new_text = "The boiler was serviced in May; the next service is due in two years."
    leftovers = {"before anything": 0, "after originals": 1, "after docs": 2, "after ledger": 0}

    for step, unlisted in leftovers.items():
        store_dir = tmp_path / step.replace(" ", "-")
        support.make_store(store_dir).close()
        written: list[str] = []

        def write(
            target: Path,
            data: bytes,
            *,
            overwrite: bool,
            step: str = step,
            written: list[str] = written,
        ) -> None:
            assert overwrite is False  # every file is created, never replaced
            if step == "before anything":
                raise Crash
            real = real_ledger_write if target.parent.name == "ledger" else real_store_write
            real(target, data, overwrite=overwrite)
            written.append(target.parent.name)
            if step == f"after {target.parent.name}":
                raise Crash

        def no_clean_up(target: Path) -> None:
            raise Crash  # a dead process removes nothing

        monkeypatch.setattr(gate_store, "atomic_write", write)
        monkeypatch.setattr(gate_ledger, "atomic_write", write)
        monkeypatch.setattr(gate_store, "_delete", no_clean_up)
        store = support.reopen(store_dir)
        with pytest.raises(Crash):
            support.add_text(store, "home", "boiler", new_text, name="b.txt", keep_original=True)
        store.close()  # the lock dies with the process
        monkeypatch.undo()
        expected = ["originals", "docs", "ledger"]
        assert written == expected[: len(written)]  # the order: original, document, record

        with support.reopen(store_dir) as again:
            again.ledger.verify()
            titles = [info.title for info in again.documents()]
            assert len(again.unlisted_files()) == unlisted
            if step == "after ledger":
                # The record is what makes the document exist.
                assert titles[-1] == "boiler"
                boiler = support.document_id(again, "boiler")
                assert again.read_document(boiler)["text"] == support.prepare(new_text)[0]
                assert again.read_original(boiler) == new_text.encode()
            else:
                assert "boiler" not in titles
                # Whatever was left is ignored, and the document can be added now.
                added = support.add_text(
                    again, "home", "boiler", new_text, name="b.txt", keep_original=True
                )
                assert again.read_original(added.doc_id) == new_text.encode()
                assert len(again.unlisted_files()) == unlisted
            again.ledger.verify()
            assert [info.title for info in again.documents()].count("boiler") == 1


def test_failed_add_takes_back_its_files(sample: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    before = support.snapshot(sample.path)
    head = sample.ledger.head()
    real = gate_store.atomic_write

    def add() -> Any:
        return support.add_text(
            sample, "home", "boiler", "The boiler was serviced.", name="b.txt", keep_original=True
        )

    def unwritable(target: Path, data: bytes, *, overwrite: bool) -> None:
        raise OSError(28, "No space left on device")

    # The record cannot be written: both files are removed again.
    monkeypatch.setattr(gate_ledger, "atomic_write", unwritable)
    with pytest.raises(LedgerError, match="could not be written"):
        add()
    assert support.snapshot(sample.path) == before
    monkeypatch.undo()

    # The document file cannot be written: the original is removed again.
    def only_originals(target: Path, data: bytes, *, overwrite: bool) -> None:
        if target.parent.name != "originals":
            raise OSError(28, "No space left on device")
        real(target, data, overwrite=overwrite)

    monkeypatch.setattr(gate_store, "atomic_write", only_originals)
    with pytest.raises(OSError, match="No space"):
        add()
    assert support.snapshot(sample.path) == before
    monkeypatch.undo()

    assert sample.ledger.head() == head
    assert sample.unlisted_files() == []
    assert add().title == "boiler"  # and the store goes on working
    sample.ledger.verify()


def test_owner_changes_go_by_the_record_when_the_flush_fails(
    sample_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cannot_flush(directory: Path) -> None:
        raise OSError(5, "Input/output error")

    with support.reopen(sample_path) as store:
        bank = support.document_id(store, "bank-letter")
        monkeypatch.setattr(gate_ledger, "_flush_directory", cannot_flush)
        # The ledger itself reports it, because a release must not rest on such a record.
        with pytest.raises(LedgerError, match="could not be flushed"):
            store.ledger.append("status", {"client": "claude-desktop"})
        # For the owner's own changes the record is there, so the change happened.
        added = support.add_text(store, "home", "boiler", "The boiler was serviced in May.")
        assert added in store.documents()
        store.remove_document(bank)
        assert bank not in [info.doc_id for info in store.documents()]
        assert not (sample_path / "docs" / f"{bank}.wdva").exists()
        policy = store.policy_json()
        policy["defaults"]["chars_per_day"] = 1234
        assert store.set_policy_json(policy, by="test")["serial"] == 3
        monkeypatch.undo()
        assert store.unlisted_files() == []
    with support.reopen(sample_path) as store:
        store.ledger.verify()
        assert added in store.documents()
        assert "serviced" in store.read_document(added.doc_id)["text"]
        assert bank not in [info.doc_id for info in store.documents()]
        assert store.policy_json()["defaults"]["chars_per_day"] == 1234


def test_remove_document_records_first_then_deletes(
    sample_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write, real_delete = gate_ledger.atomic_write, gate_store._delete
    events: list[str] = []

    def write(target: Path, data: bytes, *, overwrite: bool) -> None:
        real_write(target, data, overwrite=overwrite)
        events.append("record")

    def delete(target: Path) -> None:
        events.append(f"delete {target.parent.name}")
        real_delete(target)

    def files_of(doc_id: str) -> list[Path]:
        return [sample_path / folder / f"{doc_id}.wdva" for folder in ("docs", "originals")]

    store = support.reopen(sample_path)
    lease, bank, tax, medical = (info.doc_id for info in store.documents())
    monkeypatch.setattr(gate_ledger, "atomic_write", write)
    monkeypatch.setattr(gate_store, "_delete", delete)
    store.remove_document(lease)
    assert events == ["record", "delete docs", "delete originals"]
    assert not any(item.exists() for item in files_of(lease))
    assert store.ledger.read(store.ledger.head().seq).data["doc_id"] == lease
    with pytest.raises(UnknownDocumentError):
        store.remove_document(lease)
    with pytest.raises(UnknownDocumentError):
        store.remove_document("d-000000000000")

    # The record cannot be written: nothing changes.
    def unwritable(target: Path, data: bytes, *, overwrite: bool) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(gate_ledger, "atomic_write", unwritable)
    head = store.ledger.head()
    with pytest.raises(LedgerError):
        store.remove_document(bank)
    assert all(item.exists() for item in files_of(bank))
    assert support.BANK_MARKER in store.read_document(bank)["text"]
    assert store.ledger.head() == head
    monkeypatch.setattr(gate_ledger, "atomic_write", real_write)

    # A file that cannot be deleted stays behind unlisted; the document is gone all the same.
    def undeletable(target: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(gate_store, "_delete", undeletable)
    store.remove_document(bank)
    assert bank not in [info.doc_id for info in store.documents()]
    assert store.unlisted_files() == files_of(bank)
    with pytest.raises(UnknownDocumentError):
        store.read_document(bank)

    # The process dies right after the record.
    def dies(target: Path) -> None:
        raise Crash

    monkeypatch.setattr(gate_store, "_delete", dies)
    with pytest.raises(Crash):
        store.remove_document(tax)
    store.close()
    monkeypatch.undo()
    with support.reopen(sample_path) as again:
        again.ledger.verify()
        assert [info.doc_id for info in again.documents()] == [medical]
        assert again.unlisted_files() == sorted(files_of(bank) + files_of(tax))
        with pytest.raises(UnknownDocumentError):
            again.read_document(tax)
        assert support.MEDICAL_MARKER in again.read_document(medical)["text"]


# ---------------------------------------------------------------------------
# The policy as the store keeps it
# ---------------------------------------------------------------------------


def test_default_policy_is_the_policy_modules_default() -> None:
    from wdva.gate.policy import Policy, default_policy

    assert gate_store.default_policy_json() == default_policy().to_json()
    assert Policy.from_json(support.sample_policy()).serial == 1
    changed = gate_store.default_policy_json()
    changed["clients"]["x"] = {}
    assert gate_store.default_policy_json()["clients"] == {}  # a new object every time


def test_policy_is_one_record_per_change(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with support.make_store(path, documents=(), policy=False) as store:
        first = store.policy_json()
        assert first == gate_store.default_policy_json()
        assert (first["serial"], first["clients"], first["collections"]) == (1, {}, {})
        assert store.ledger.head().seq == 2

        wanted = support.sample_policy()
        wanted["serial"] = 41  # the store numbers policies itself
        stored = store.set_policy_json(wanted, by="test")
        assert stored == {**support.sample_policy(), "serial": 2}
        assert store.ledger.head().seq == 3
        record = store.ledger.read(3)
        assert (record.kind, record.data["serial"], record.data["by"]) == ("policy", 2, "test")
        assert record.data["policy"] == stored

        # What goes in and what comes out are copies.
        wanted["clients"].clear()
        stored["collections"].clear()
        store.policy_json()["redact_literals"].append("changed")
        assert store.policy_json() == {**support.sample_policy(), "serial": 2}

        # A change that cannot be recorded does not happen.
        def unwritable(target: Path, data: bytes, *, overwrite: bool) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(gate_ledger, "atomic_write", unwritable)
        with pytest.raises(LedgerError):
            store.set_policy_json(gate_store.default_policy_json(), by="test")
        monkeypatch.undo()
        assert store.policy_json() == {**support.sample_policy(), "serial": 2}
        assert store.ledger.head().seq == 3
        with pytest.raises(TypeError):
            store.set_policy_json(["not", "an", "object"], by="test")  # type: ignore[arg-type]

        assert store.set_policy_json(gate_store.default_policy_json(), by="test")["serial"] == 3

    with support.reopen(path) as store:
        assert store.policy_json() == {**gate_store.default_policy_json(), "serial": 3}


def test_first_policy_can_be_given(path: Path) -> None:
    with init_store(
        path,
        passphrase=PASSPHRASE,
        scrypt_n=FAST_SCRYPT,
        policy={**support.sample_policy(), "serial": 9},
    ) as store:
        assert store.policy_json() == {**support.sample_policy(), "serial": 1}
        assert store.ledger.read(2).data["by"] == "init"


# ---------------------------------------------------------------------------
# Creating a store, and where it may be
# ---------------------------------------------------------------------------


def test_new_store_has_its_first_two_records(path: Path, gate_clock: Any) -> None:
    with init_store(path, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT, clock=gate_clock) as store:
        assert store.writable
        assert not lock_is_free(path)
        assert len(store.store_id) == 16
        assert store.created == "2026-11-12T09:00:00Z"
        assert store.documents() == []
        kinds = [record.kind for record in store.ledger.records()]
        assert kinds == ["init", "policy"]
        assert store.ledger.read(1).data["store_id"] == store.store_id
        assert store.ledger.read(1).prev == gate_ledger.genesis_hash(store.store_id)
        store_id = store.store_id
    assert lock_is_free(path)
    with support.reopen(path) as store:
        assert store.store_id == store_id
    with support.make_store(path.with_name("second"), documents=()) as second:
        assert second.store_id != store_id


def test_default_key_slot_cost_is_read_when_a_store_is_made(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert gate_store.KEYSLOT_SCRYPT_N == vault.SCRYPT_N_DEFAULT == 2**17
    monkeypatch.setattr(gate_store, "KEYSLOT_SCRYPT_N", 2**15)
    init_store(path, passphrase=PASSPHRASE).close()
    assert store_info(path).scrypt_n == 2**15


def test_init_refuses_a_directory_that_is_in_use(sample_path: Path, tmp_path: Path) -> None:
    before = support.snapshot(sample_path)
    with pytest.raises(FileExistsError, match="already holds a store"):
        init_store(sample_path, passphrase="another passphrase", scrypt_n=FAST_SCRYPT)
    assert support.snapshot(sample_path) == before
    with support.reopen(sample_path):
        pass

    notes = tmp_path / "notes"
    notes.mkdir(mode=0o700)
    (notes / "todo.txt").write_text("buy milk")
    with pytest.raises(FileExistsError, match="not empty"):
        init_store(notes, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT)
    assert [item.name for item in notes.iterdir()] == ["todo.txt"]

    # A store whose key slot was taken away is not an empty directory either.
    (sample_path / "keyslot.wdva").unlink()
    with pytest.raises(FileExistsError, match="not empty"):
        init_store(sample_path, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT)
    assert len(support.record_files(sample_path)) == 7

    a_file = tmp_path / "file"
    a_file.write_text("x")
    with pytest.raises(StoreFormatError, match="not a directory"):
        init_store(a_file, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT)
    with pytest.raises(ValueError, match="empty"):
        init_store(tmp_path / "unnamed", passphrase="", scrypt_n=FAST_SCRYPT)
    assert not (tmp_path / "unnamed").exists()


def test_interrupted_creation_leaves_no_store_and_can_be_repeated(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gate_store.atomic_write
    for failing_write, exception in ((0, OSError(5, "I/O")), (1, Crash()), (2, OSError(5, "I/O"))):
        done: list[Path] = []

        def write(
            target: Path,
            data: bytes,
            *,
            overwrite: bool,
            done: list[Path] = done,
            failing_write: int = failing_write,
            exception: BaseException = exception,
        ) -> None:
            if len(done) == failing_write:
                # What an interrupted write leaves: its temporary file.
                (target.parent / f".{target.name}.k3j2h1.tmp").write_bytes(data)
                raise exception
            real(target, data, overwrite=overwrite)
            done.append(target)

        monkeypatch.setattr(gate_store, "atomic_write", write)
        monkeypatch.setattr(gate_ledger, "atomic_write", write)
        with pytest.raises((OSError, LedgerError, Crash)):
            init_store(path, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT)
        monkeypatch.undo()
        # The order: first record, first policy, and the key slot last.
        assert [item.name for item in done] == ["0000000001.wdva", "0000000002.wdva"][
            :failing_write
        ]
        assert not store_info(path).exists
        assert lock_is_free(path)
        with pytest.raises(StoreFormatError, match="no store"):
            open_store(path, passphrase=PASSPHRASE)

    with init_store(path, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT) as store:
        assert store.ledger.verify().seq == 2
    assert [item.name for item in support.sealed_files(path)] == [
        "keyslot.wdva",
        "0000000001.wdva",
        "0000000002.wdva",
    ]
    with support.reopen(path) as store:
        assert store.policy_json()["serial"] == 1


def test_store_directory_must_be_private(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path.mkdir(mode=0o700)
    for mode in (0o750, 0o705, 0o770, 0o701, 0o777):
        os.chmod(path, mode)
        with pytest.raises(StoreLocationError, match=r"chmod 700 .*store") as refused:
            init_store(path, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT)
        assert f"mode {mode:04o}" in str(refused.value)
        assert list(path.iterdir()) == []
    os.chmod(path, 0o700)
    support.make_store(path, documents=()).close()

    for mode in (0o750, 0o705, 0o770, 0o701, 0o777):
        os.chmod(path, mode)
        for write in (True, False):
            with pytest.raises(StoreLocationError, match=r"chmod 700 .*store"):
                support.reopen(path, write=write)
        with pytest.raises(StoreLocationError):
            gate_store.check_private(path)
        assert lock_is_free(path)
    os.chmod(path, 0o700)
    support.reopen(path).close()

    owner = path.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(StoreLocationError, match="another user"):
        support.reopen(path)
    foreign = path.with_name("foreign")
    foreign.mkdir(mode=0o700)
    with pytest.raises(StoreLocationError, match="another user"):
        init_store(foreign, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT)
    assert list(foreign.iterdir()) == []


@pytest.mark.parametrize(
    "component",
    [
        "Dropbox",
        "OneDrive",
        "Google Drive",
        "iCloud Drive",
        "Mobile Documents",
        "CloudStorage",
        "dropbox",
        "ONEDRIVE",
    ],
)
def test_synced_folder_is_refused_at_init(tmp_path: Path, component: str) -> None:
    store_dir = tmp_path / component / "papers" / "store"
    with pytest.raises(StoreLocationError, match="synchronised folder") as refused:
        init_store(store_dir, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT)
    assert component in str(refused.value)
    assert not (tmp_path / component).exists()  # nothing was created on the way

    with init_store(
        store_dir, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT, allow_synced_folder=True
    ) as store:
        assert store.documents() == []
    assert store_info(store_dir).synced_folder == component
    support.reopen(store_dir).close()  # opening such a store is not refused


def test_a_folder_that_only_resembles_a_synced_one_is_fine(tmp_path: Path) -> None:
    store_dir = tmp_path / "Dropbox-notes" / "store"
    init_store(store_dir, passphrase=PASSPHRASE, scrypt_n=FAST_SCRYPT).close()
    assert store_info(store_dir).synced_folder is None
