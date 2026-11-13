"""The gate's record: the chain, the fail-closed append, and the state rebuilt from it."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import random
import shutil
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from wdva import SecretKey, vault
from wdva._util import atomic_write
from wdva.gate import ledger as gate_ledger
from wdva.gate.errors import LedgerError
from wdva.gate.ledger import (
    Head,
    Ledger,
    LedgerState,
    Window,
    format_time,
    genesis_hash,
    head_without_key,
    parse_time,
    record_name,
)
from wdva.gate.store import Store

from . import gate_support as support
from .gate_support import release_fields

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate does not run on Windows yet"
)

DAY = timedelta(hours=24)
MONTH = timedelta(days=30)


class Crash(BaseException):
    """Stands for the process dying at this point: nothing after it runs."""


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def store(path: Path, gate_clock: Any) -> Iterator[Store]:
    """An open store with the sample policy and documents: seven records, timed by the clock."""
    with support.make_store(path, clock=gate_clock) as opened:
        yield opened


@pytest.fixture
def session_path(path: Path, gate_clock: Any) -> Path:
    """A closed store whose record also holds one session: fourteen records."""
    with support.make_store(path, clock=gate_clock) as opened:
        support.record_session(opened, advance=lambda: gate_clock.advance(seconds=20))
    return path


def fresh_ledger(store: Store) -> Ledger:
    """A second, read-only look at the store's record files, with nothing in memory."""
    return Ledger(store.path / "ledger", store._key, store.store_id, writable=False)


def record_file(store_dir: Path, seq: int) -> Path:
    return store_dir / "ledger" / record_name(seq)


def unwritable(target: Path, data: bytes, *, overwrite: bool) -> None:
    raise OSError(28, "No space left on device")


def first_bad(store_dir: Path) -> int | None:
    """Open the store and verify its record; the number of the record that fails."""
    with support.reopen(store_dir) as opened:
        with pytest.raises(LedgerError) as refused:
            opened.ledger.replay()
        with pytest.raises(LedgerError) as again:
            opened.ledger.verify()
        assert again.value.seq == refused.value.seq
        with pytest.raises(LedgerError):  # and nothing is appended to a chain that is broken
            opened.ledger.append("status", {"client": "claude-desktop"})
        return refused.value.seq


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def test_chain_detects_edit_swap_copy_and_gap(session_path: Path) -> None:
    with support.reopen(session_path) as opened:
        head = opened.ledger.verify()
        key = opened._key.hex()
    assert head.seq == 14
    original = support.snapshot(session_path)
    ninth, tenth = record_file(session_path, 9), record_file(session_path, 10)
    sealed_ninth, sealed_tenth = ninth.read_bytes(), tenth.read_bytes()

    def restore() -> None:
        shutil.rmtree(session_path / "ledger", ignore_errors=True)
        (session_path / "ledger").mkdir(mode=0o700)
        for name, content in original.items():
            if name.startswith("ledger/"):
                (session_path / name).write_bytes(content)

    # An edit: one bit, in the ciphertext, in the tag, in the header.
    for position in (len(sealed_ninth) - 30, len(sealed_ninth) - 1, 40):
        changed = bytearray(sealed_ninth)
        changed[position] ^= 0x01
        ninth.write_bytes(changed)
        assert first_bad(session_path) == 9
    ninth.write_bytes(sealed_ninth[:-5])
    assert first_bad(session_path) == 9
    ninth.write_bytes(b"")
    assert first_bad(session_path) == 9
    restore()

    # A swapped pair.
    ninth.write_bytes(sealed_tenth)
    tenth.write_bytes(sealed_ninth)
    assert first_bad(session_path) == 9
    restore()

    # One record copied over another.
    tenth.write_bytes(sealed_ninth)
    assert first_bad(session_path) == 10
    restore()

    # A missing record in the middle.
    ninth.unlink()
    assert first_bad(session_path) == 9
    restore()

    # A record sealed by someone without the store key.
    payload = vault.unseal(sealed_ninth, key=key).payload
    ninth.write_bytes(vault.seal(payload, key=os.urandom(32), content_type=gate_ledger.LEDGER_TYPE))
    assert first_bad(session_path) == 9
    restore()

    # The same content sealed again is another file: the next record no longer follows it.
    ninth.write_bytes(vault.seal(payload, key=key, content_type=gate_ledger.LEDGER_TYPE))
    assert first_bad(session_path) == 10
    restore()

    # The first record gone, or everything gone.
    record_file(session_path, 1).unlink()
    assert first_bad(session_path) == 1
    shutil.rmtree(session_path / "ledger")
    assert first_bad(session_path) == 1
    restore()

    with support.reopen(session_path) as opened:
        assert opened.ledger.verify() == head


def test_a_record_must_have_the_form_of_a_record(session_path: Path) -> None:
    with support.reopen(session_path) as opened:
        key = opened._key.hex()
    ninth = record_file(session_path, 9)
    sealed = ninth.read_bytes()
    record = json.loads(vault.unseal(sealed, key=key).payload)

    def forged(**changes: Any) -> bytes:
        return vault.seal(
            json.dumps({**record, **changes}).encode(),
            key=key,
            content_type=gate_ledger.LEDGER_TYPE,
        )

    # Whoever holds the key can write records. What they write must still be a record
    # this version understands; anything else stops the replay at that number.
    for changes in (
        {"v": 2},
        {"seq": 8},
        {"prev": "0" * 64},
        {"prev": "short"},
        {"time": "yesterday"},
        {"time": "2026-13-01T00:00:00Z"},
        {"kind": "export"},
        {"kind": 5},
        {"chars": record["chars"] - 1},  # the count is not the length of the text
        {"text": record["text"] + "!"},
        {"sha256": "0" * 64},
        {"client_seq": 0},
        {"spans": [["d-000000000000", 0, 10]]},  # a document the store does not have
        {"spans": [[record["spans"][0][0], 0, 10**7]]},  # past the end of the document
        {"spans": None},
        {"query": "x" * 501},
        {"pool": ""},
        {"pool": "everyone"},
    ):
        ninth.write_bytes(forged(**changes))
        assert first_bad(session_path) == 9, changes
    ninth.write_bytes(vault.seal(b"[1, 2]", key=key, content_type=gate_ledger.LEDGER_TYPE))
    assert first_bad(session_path) == 9
    ninth.write_bytes(vault.seal(b"not json", key=key, content_type=gate_ledger.LEDGER_TYPE))
    assert first_bad(session_path) == 9
    ninth.write_bytes(sealed)
    support.reopen(session_path).close()


def test_record_of_another_store_is_refused(tmp_path: Path) -> None:
    key = SecretKey.generate()  # the same key on purpose: only the store id differs
    first = Ledger.create(tmp_path / "a", key, "aaaaaaaaaaaaaaaa")
    second = Ledger.create(tmp_path / "b", key, "bbbbbbbbbbbbbbbb")
    assert first.read(1).prev == genesis_hash("aaaaaaaaaaaaaaaa")
    assert first.read(1).prev != second.read(1).prev
    shutil.copy(tmp_path / "a" / record_name(1), tmp_path / "b" / record_name(1))
    with pytest.raises(LedgerError, match="record 1") as refused:
        Ledger(tmp_path / "b", key, "bbbbbbbbbbbbbbbb").replay()
    assert refused.value.seq == 1
    with pytest.raises(LedgerError, match="already holds records"):
        Ledger.create(tmp_path / "a", key, "aaaaaaaaaaaaaaaa")
    with pytest.raises(ValueError, match="store id"):
        Ledger(tmp_path / "a", key, "not-an-id")


def test_tail_removal_is_not_detected_without_a_head(session_path: Path) -> None:
    noted = store_head(session_path)
    assert str(noted).startswith("14:")
    for seq in (14, 13, 12):
        record_file(session_path, seq).unlink()

    # This is the limit of the chain, pinned here: the shortened record verifies.
    with support.reopen(session_path) as opened:
        assert opened.ledger.verify().seq == 11
        state = opened.ledger.replay()
        assert state.head.seq == 11
        assert state.session_open  # the lock record went with the tail
        opened.ledger.append("status", {"client": "claude-desktop"})  # and it can be written to

    # Only a head that was noted before shows it.
    now = store_head(session_path)
    assert now.seq == 12
    assert str(now) != str(noted)


def store_head(store_dir: Path) -> Head:
    head = head_without_key(store_dir / "ledger")
    assert head is not None
    return head


def test_older_ledger_tail_brings_back_older_policy_and_changes_the_head(
    path: Path, gate_clock: Any
) -> None:
    with support.make_store(path, clock=gate_clock) as opened:
        lease = support.document_id(opened, "lease-2025")
        before_change = opened.ledger.head()
        text = "OK: a passage.\n   | of the lease"
        opened.ledger.append(
            "release", release_fields("claude-desktop", 1, text, spans=[(lease, 0, 700)])
        )
        narrowed = opened.policy_json()
        del narrowed["clients"]["claude-desktop"]["grants"]["home"]
        narrowed["defaults"]["chars_per_day"] = 500
        assert opened.set_policy_json(narrowed, by="test")["serial"] == 3
        noted = str(opened.ledger.head())
        assert noted.startswith("9:")

    record_file(path, 9).unlink()
    record_file(path, 8).unlink()
    with support.reopen(path, clock=gate_clock) as opened:
        opened.ledger.verify()  # nothing in the files tells
        policy = opened.policy_json()
        assert policy["serial"] == 2
        assert policy["clients"]["claude-desktop"]["grants"]["home"] == {"mode": "read"}
        assert policy["defaults"]["chars_per_day"] == 20000
        state = opened.ledger.replay()
        # The release went with the tail, so what it used up is free again.
        assert state.client("claude-desktop").releases.used(gate_clock(), DAY) == 0
        assert state.released_chars("hosted", lease) == 0
        assert opened.ledger.head() == before_change
        assert str(opened.ledger.head()) != noted
    assert str(store_head(path)) != noted


def test_temporary_files_do_not_break_verification(session_path: Path) -> None:
    folder = session_path / "ledger"
    with support.reopen(session_path) as opened:
        head = opened.ledger.verify()
    sealed = record_file(session_path, 5).read_bytes()

    (folder / ".0000000015.wdva.a1b2c3.tmp").write_bytes(sealed)  # what a dying write leaves
    (folder / ".0000000003.wdva.zzzzzz.tmp").write_bytes(b"half a rec")
    (folder / "quarantine-20261112T090000Z").mkdir()
    (folder / "quarantine-20261112T090000Z" / record_name(15)).write_bytes(sealed)
    (folder / "notes.txt").write_text("not a record")
    (folder / "15.wdva").write_bytes(sealed)
    (folder / "00000000015.wdva").write_bytes(sealed)  # eleven digits
    (folder / "0000000016.wdva").mkdir()  # a directory is not a record
    os.symlink(record_file(session_path, 5), folder / "0000000017.wdva")  # nor is a link

    assert store_head(session_path) == head
    with support.reopen(session_path) as opened:
        assert opened.ledger.verify() == head
        assert opened.ledger.replay().head == head
        assert [record.seq for record in opened.ledger.records()] == list(range(1, 15))
        written = opened.ledger.append("status", {"client": "claude-desktop"})
        assert written.seq == 15
        assert opened.ledger.verify().seq == 15


def test_broken_chain_stops_unlock_and_names_the_record(session_path: Path) -> None:
    original = record_file(session_path, 3).read_bytes()

    def nothing_can_be_done(seq: int) -> None:
        before = support.snapshot(session_path)
        with support.reopen(session_path) as opened:
            with pytest.raises(LedgerError, match=f"record {seq}") as refused:
                opened.ledger.replay()
            assert refused.value.seq == seq
            calls: list[Callable[[], Any]] = [
                opened.policy_json,
                opened.documents,
                opened.unlisted_files,
                lambda: opened.set_policy_json(support.sample_policy(), by="test"),
                lambda: support.add_text(opened, "home", "new", "Entirely new words."),
                lambda: opened.remove_document("d-000000000000"),
                lambda: opened.read_document("d-000000000000"),
                lambda: opened.ledger.append("unlock", {"served": []}),
            ]
            for call in calls:
                with pytest.raises(LedgerError) as again:
                    call()
                assert again.value.seq == seq
        assert support.snapshot(session_path) == before

    # A file after the newest record that is not a record.
    record_file(session_path, 15).write_bytes(os.urandom(300))
    nothing_can_be_done(15)
    record_file(session_path, 15).unlink()

    # A missing record in the middle.
    saved = record_file(session_path, 6).read_bytes()
    record_file(session_path, 6).unlink()
    nothing_can_be_done(6)
    record_file(session_path, 6).write_bytes(saved)

    # The damaged record is the newest policy: there is no falling back to an older one.
    damaged = bytearray(original)
    damaged[-10] ^= 0xFF
    record_file(session_path, 3).write_bytes(damaged)
    nothing_can_be_done(3)
    with support.reopen(session_path) as opened:
        with pytest.raises(LedgerError) as refused:
            opened.ledger.current_policy()
        assert refused.value.seq == 3
    record_file(session_path, 3).write_bytes(original)
    with support.reopen(session_path) as opened:
        assert opened.policy_json()["serial"] == 2


def test_verify_notices_files_changed_during_a_session(store: Store) -> None:
    for number in (1, 2):
        store.ledger.append("release", release_fields("claude-desktop", number, f"OK: {number}"))
    assert store.ledger.verify().seq == 9
    record_file(store.path, 9).unlink()

    # What this session wrote is still what it counts with.
    assert store.ledger.replay().client("claude-desktop").release_seq == 2
    with pytest.raises(LedgerError, match="no longer match") as refused:
        store.ledger.verify()
    assert refused.value.seq == 9
    # After that the session records and releases nothing more.
    before = support.snapshot(store.path)
    for call in (
        store.ledger.replay,
        store.ledger.verify,
        lambda: store.ledger.append("status", {"client": "claude-desktop"}),
        lambda: store.set_policy_json(support.sample_policy(), by="test"),
    ):
        with pytest.raises(LedgerError, match="no longer match"):
            call()
    assert support.snapshot(store.path) == before


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------


def test_exclusive_create_refuses_a_second_writer(store: Store) -> None:
    folder = store.path / "ledger"
    first = Ledger(folder, store._key, store.store_id)
    second = Ledger(folder, store._key, store.store_id)
    assert first.replay().head == second.replay().head
    written = first.append("status", {"client": "claude-desktop"})
    assert written.seq == 8
    sealed = record_file(store.path, 8).read_bytes()

    with pytest.raises(LedgerError, match="exists already") as refused:
        second.append("status", {"client": "lm-studio"})
    assert refused.value.seq == 8
    assert record_file(store.path, 8).read_bytes() == sealed  # one file, the first writer's
    assert len(support.record_files(store.path)) == 8
    assert not [item for item in folder.iterdir() if item.name.startswith(".")]
    # The writer that lost no longer trusts what it remembers.
    with pytest.raises(LedgerError, match="no longer match"):
        second.append("status", {"client": "lm-studio"})
    assert first.append("status", {"client": "lm-studio"}).seq == 9
    assert fresh_ledger(store).verify().seq == 9

    # The file-level rule underneath: a name is created, never replaced.
    with pytest.raises(FileExistsError):
        atomic_write(record_file(store.path, 8), b"another writer", overwrite=False)
    assert record_file(store.path, 8).read_bytes() == sealed


def test_appends_from_many_threads_form_one_chain(store: Store) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def work(number: int) -> None:
        try:
            barrier.wait()
            for _ in range(10):
                store.ledger.append("status", {"client": f"client-{number}"})
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    fresh = fresh_ledger(store)
    assert fresh.verify().seq == 7 + 80
    assert fresh.replay() == store.ledger.replay()
    assert fresh.replay().counts["status"] == 80


def test_failed_write_leaves_no_record_and_changes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = support.document_id(store, "lease-2025")
    fields = release_fields("claude-desktop", 1, "OK: text", spans=[(lease, 0, 100)])
    before = support.snapshot(store.path)
    state = store.ledger.replay()
    remembered = copy.deepcopy(state)

    monkeypatch.setattr(gate_ledger, "atomic_write", unwritable)
    for kind, record in (
        ("release", fields),
        ("denial", {"client": "claude-desktop", "code": "NOT_FOUND"}),
        ("approval", {"client": "claude-desktop", "ref": f"{lease}.1", "body_sha256": "0" * 64}),
    ):
        with pytest.raises(LedgerError, match="record 8 could not be written") as refused:
            store.ledger.append(kind, record)
        assert refused.value.seq == 8
        assert isinstance(refused.value.__cause__, OSError)
    assert support.snapshot(store.path) == before
    assert not [item for item in (store.path / "ledger").iterdir() if item.name.startswith(".")]
    # No counter moved: the state is the very object, and equal to what it was.
    assert store.ledger.replay() is state
    assert state == remembered
    assert state.client("claude-desktop").release_seq == 0
    assert state.released_chars("hosted", lease) == 0

    monkeypatch.undo()
    assert store.ledger.append("release", fields).seq == 8  # the number was not used up
    assert state.client("claude-desktop").release_seq == 1
    assert fresh_ledger(store).replay() == state


def test_append_writes_the_record_before_it_counts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gate_ledger.atomic_write
    state = store.ledger.replay()
    seen: list[tuple[str, bool, int, int]] = []

    def write(target: Path, data: bytes, *, overwrite: bool) -> None:
        # At the moment of writing nothing is counted yet.
        seen.append(
            (target.name, overwrite, state.head.seq, state.client("claude-desktop").release_seq)
        )
        payload, header = vault.unseal(data, key=store._key)
        assert header.content_type == gate_ledger.LEDGER_TYPE
        assert header.public_metadata == {}
        assert json.loads(payload)["text"] == "OK: text"
        real(target, data, overwrite=overwrite)

    monkeypatch.setattr(gate_ledger, "atomic_write", write)
    record = store.ledger.append("release", release_fields("claude-desktop", 1, "OK: text"))
    assert seen == [("0000000008.wdva", False, 7, 0)]
    assert (state.head.seq, state.client("claude-desktop").release_seq) == (8, 1)
    assert state.head == record.head


def test_append_survives_a_crash_at_every_step(
    tmp_path: Path, gate_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = gate_ledger.atomic_write
    text = "OK: a passage.\n   | of the lease"

    def dies_before_writing(target: Path, data: bytes, *, overwrite: bool) -> None:
        raise Crash

    def dies_with_a_temporary_file(target: Path, data: bytes, *, overwrite: bool) -> None:
        (target.parent / f".{target.name}.q1w2e3.tmp").write_bytes(data[: len(data) // 2])
        raise Crash

    def dies_after_the_name_exists(target: Path, data: bytes, *, overwrite: bool) -> None:
        real(target, data, overwrite=overwrite)
        raise Crash

    def cannot_flush(directory: Path) -> None:
        raise OSError(5, "Input/output error")

    cases: list[tuple[str, Any, Any, bool, type[BaseException]]] = [
        ("before", dies_before_writing, None, False, Crash),
        ("temporary", dies_with_a_temporary_file, None, False, Crash),
        ("named", dies_after_the_name_exists, None, True, Crash),
        ("unflushed", None, cannot_flush, True, LedgerError),
    ]
    for name, write, flush, recorded, error in cases:
        store_dir = tmp_path / name
        support.make_store(store_dir, clock=gate_clock).close()
        opened = support.reopen(store_dir, clock=gate_clock)
        lease = support.document_id(opened, "lease-2025")
        fields = release_fields("claude-desktop", 1, text, spans=[(lease, 0, 700)])
        if write is not None:
            monkeypatch.setattr(gate_ledger, "atomic_write", write)
        if flush is not None:
            monkeypatch.setattr(gate_ledger, "_flush_directory", flush)
        with pytest.raises(error):
            opened.ledger.append("release", fields)
        monkeypatch.undo()
        in_memory = copy.deepcopy(opened.ledger.replay())
        opened.close()  # the process is gone

        with support.reopen(store_dir, clock=gate_clock) as again:
            state = again.ledger.replay()
            assert again.ledger.verify().seq == (8 if recorded else 7)
            # What the dying process counted is exactly what the files say.
            assert state == in_memory
            assert state.client("claude-desktop").release_seq == (1 if recorded else 0)
            assert state.released_chars("hosted", lease) == (700 if recorded else 0)
            assert state.pool("hosted").used(gate_clock(), DAY) == (len(text) if recorded else 0)
            # The store goes on: the next record takes the next free number.
            following = again.ledger.append("status", {"client": "claude-desktop"})
            assert following.seq == (9 if recorded else 8)
            assert again.ledger.verify() == following.head


def test_directory_flush_failure_is_reported(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.fsync
    failure: list[int] = []

    def fsync(descriptor: int) -> None:
        if failure and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(failure[0], os.strerror(failure[0]))
        real(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    assert store.ledger.append("status", {"client": "claude-desktop"}).seq == 8
    # A file system that cannot flush a directory at all is left alone.
    failure[:] = [errno.EINVAL]
    assert store.ledger.append("status", {"client": "claude-desktop"}).seq == 9
    # A real failure is not: nothing may be released on the strength of that record.
    failure[:] = [errno.EIO]
    with pytest.raises(LedgerError, match="record 10 was written but could not be flushed"):
        store.ledger.append("release", release_fields("claude-desktop", 1, "OK: text"))
    # The record is there all the same, so it counts.
    assert store.ledger.replay().client("claude-desktop").release_seq == 1
    failure.clear()
    assert fresh_ledger(store).replay() == store.ledger.replay()
    assert store.ledger.append("status", {"client": "claude-desktop"}).seq == 11


def test_sealing_failure_is_a_ledger_error(store: Store) -> None:
    before = support.snapshot(store.path)
    state = store.ledger.replay()
    store._key.wipe()  # as when the store is being closed underneath a caller
    with pytest.raises(LedgerError, match="record 8 was not written: it could not be sealed"):
        store.ledger.append("status", {"client": "claude-desktop"})
    assert support.snapshot(store.path) == before
    assert state.head.seq == 7


def test_records_are_checked_before_they_are_written(store: Store) -> None:
    lease = store.document(support.document_id(store, "lease-2025"))
    good = release_fields("claude-desktop", 1, "OK: text", spans=[(lease.doc_id, 0, 10)])
    store.ledger.append("release", good)
    before = support.snapshot(store.path)
    state = store.ledger.replay()
    remembered = copy.deepcopy(state)
    again = {**good, "client_seq": 2}

    malformed: list[tuple[str, dict[str, Any]]] = [
        ("release", {**again, "chars": again["chars"] + 1}),  # would count too much
        ("release", {**again, "chars": again["chars"] - 1}),  # would count too little
        ("release", {**again, "sha256": "0" * 64}),
        ("release", {**again, "text": None}),
        ("release", {**again, "text": "lone \udc00 half", "chars": 11}),
        ("release", {**again, "client_seq": 1}),  # the counter must grow
        ("release", {**again, "client_seq": 0}),
        ("release", {**again, "client_seq": "2"}),
        ("release", {**again, "client_seq": True}),
        ("release", {**again, "client": ""}),
        ("release", {**again, "client": "x" * 65}),
        ("release", {**again, "pool": None}),
        ("release", {**again, "pool": "Hosted"}),  # would be counted in a pool of its own
        ("release", release_fields("claude-desktop", 2, "x" * 8001)),  # longer than any result
        ("release", {**again, "tool": ""}),
        ("release", {name: value for name, value in again.items() if name != "spans"}),
        ("release", {**again, "spans": [[lease.doc_id, 10, 10]]}),
        ("release", {**again, "spans": [[lease.doc_id, -1, 10]]}),
        ("release", {**again, "spans": [[lease.doc_id, 0, lease.chars + 1]]}),
        ("release", {**again, "spans": [[lease.doc_id, 0.0, 10.0]]}),
        ("release", {**again, "spans": [["d-000000000000", 0, 10]]}),
        ("release", {**again, "spans": [[lease.doc_id, 0]]}),
        ("release", {**again, "query": "q" * 501}),
        ("release", {**again, "query": 7}),
        ("denial", {"client": "claude-desktop"}),
        ("denial", {"client": "claude-desktop", "code": "NOT_FOUND", "query": "q" * 501}),
        ("denial", {"client": "claude-desktop", "code": "NOT_FOUND", "folded_before": -1}),
        ("status", {}),
        ("approval", {"client": "claude-desktop", "ref": f"{lease.doc_id}.1"}),
        ("approval", {"client": "claude-desktop", "ref": "", "body_sha256": "0" * 64}),
        ("policy", {"serial": 3, "policy": {"serial": 4}, "by": "test"}),
        ("policy", {"serial": 2, "policy": {"serial": 2}, "by": "test"}),  # must grow
        ("policy", {"serial": 3, "policy": [], "by": "test"}),
        ("policy", {"serial": 3, "policy": {"serial": 3}}),
        ("add", {"doc_id": lease.doc_id}),
        ("remove", {"doc_id": "d-000000000000"}),
        ("unlock", {"served": "claude-desktop"}),
        ("unlock", {"served": [], "policy_seq": 2}),  # not the newest policy record
        ("unlock", {"served": [], "documents": 3}),
        ("lock", {"reason": "close", "policy_seq": 99}),
        ("lock", {}),
        ("init", {"store_id": store.store_id}),  # only the first record is an init
        ("export", {"doc_id": lease.doc_id}),  # not a kind this version writes
        ("", {}),
        ("status", {"client": "claude-desktop", "seq": 99}),  # the ledger numbers records
        ("status", {"client": "claude-desktop", "prev": "0" * 64}),
        ("status", {"client": "claude-desktop", "time": "2020-01-01T00:00:00Z"}),
        ("status", {"client": "claude-desktop", "kind": "release"}),
        ("status", {"client": "claude-desktop", "v": 2}),
        ("status", {"client": "claude-desktop", "extra": float("nan")}),
        ("status", {"client": "claude-desktop", "extra": 2**60}),
        ("status", {"client": "claude-desktop", "extra": object()}),
        ("status", {"client": "claude-desktop", "extra": b"bytes"}),
    ]
    for kind, fields in malformed:
        with pytest.raises(LedgerError, match="record 9 was not written") as refused:
            store.ledger.append(kind, fields)
        assert refused.value.seq == 9, (kind, fields)
        assert "lone" not in str(refused.value)  # a refusal never quotes what it was given
    with pytest.raises(LedgerError, match="was not written"):
        store.ledger.append("status", {"client": "claude-desktop"}, time=datetime(2026, 1, 1))
    assert support.snapshot(store.path) == before
    assert state == remembered

    assert store.ledger.append("release", again).seq == 9
    assert fresh_ledger(store).replay() == state


def test_a_record_reads_back_exactly_as_it_was_written(store: Store, gate_clock: Any) -> None:
    text = (
        "OK: passage 3 of 9.\n"
        "   | Zażółć gęślą jaźń — “quoted”, tab\there, emoji \U0001f512, 日本語\n"
        "   | [REDACTED] and [link removed]\n"
        "Release claude-desktop #1. Left today: 19,321 characters."
    )
    query = "czynsz najmu " + "ż" * 487
    assert len(query) == 500
    lease = support.document_id(store, "lease-2025")
    gate_clock.advance(minutes=3)
    written = store.ledger.append(
        "release",
        release_fields(
            "claude-desktop",
            1,
            text,
            tool="wdva_search",
            spans=((lease, 5, 60), (lease, 100, 150)),  # tuples go in, lists come back
            query=query,
            claimed=support.CLAIMED,
            used={"client_day": [len(text), 20000]},
            left=19321,
        ),
    )
    denied = store.ledger.append(
        "denial", {"client": "(unregistered)", "code": "NOT_ALLOWED", "query": query}
    )
    for ledger in (store.ledger, fresh_ledger(store)):
        record = ledger.read(written.seq)
        assert record == written
        assert record.data["text"] == text
        assert record.data["chars"] == len(text)
        assert record.data["sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert record.data["query"] == query
        assert record.data["spans"] == [[lease, 5, 60], [lease, 100, 150]]
        assert record.data["claimed"] == support.CLAIMED
        assert (record.seq, record.kind, record.time) == (8, "release", gate_clock())
        assert list(record.data)[:5] == ["v", "seq", "prev", "time", "kind"]
        assert record.data["v"] == 1
        assert record.data["time"] == "2026-11-12T09:03:00Z"
        assert ledger.read(denied.seq).data["query"] == query
    # The file is the sealed record and nothing else; its hash is what the next one carries.
    sealed = record_file(store.path, 8).read_bytes()
    assert written.sha256 == hashlib.sha256(sealed).hexdigest()
    assert denied.prev == written.sha256
    assert written.head == Head(8, written.sha256)
    payload = vault.unseal(sealed, key=store._key).payload
    assert payload.isascii()
    assert json.loads(payload) == written.data


def test_reading_and_iterating(session_path: Path) -> None:
    with support.reopen(session_path) as opened:
        ledger = opened.ledger
        kinds = [record.kind for record in ledger.records()]
        assert kinds == [
            "init",
            "policy",
            "policy",
            *["add"] * 4,
            "unlock",
            "release",
            "release",
            "denial",
            "status",
            "approval",
            "lock",
        ]
        assert [record.seq for record in ledger.records(newest_first=True)] == list(
            range(14, 0, -1)
        )
        assert [record.seq for record in ledger.records(first=12)] == [12, 13, 14]
        assert [record.seq for record in ledger.records(last=2)] == [1, 2]
        assert [r.seq for r in ledger.records(first=9, last=11, newest_first=True)] == [11, 10, 9]
        assert [record.seq for record in ledger.records(first=-5, last=99)] == list(range(1, 15))
        assert list(ledger.records(first=15)) == []

        previous = genesis_hash(opened.store_id)
        for record in ledger.records():
            assert record.prev == previous
            previous = record.sha256
            assert record == ledger.read(record.seq)
        assert ledger.head() == Head(14, previous)

        for missing in (0, 15, -1, 10**10):
            with pytest.raises(LedgerError, match="no record"):
                ledger.read(missing)
        with pytest.raises(LedgerError):
            ledger.read("3")  # type: ignore[arg-type]


def test_head_without_key(session_path: Path, tmp_path: Path) -> None:
    head = head_without_key(session_path / "ledger")
    assert head is not None
    sealed = record_file(session_path, 14).read_bytes()
    assert head == Head(14, hashlib.sha256(sealed).hexdigest())
    assert str(head) == f"14:{head.sha256[:8]}"
    assert head.short == head.sha256[:8]
    assert len(head.short) == 8
    with support.reopen(session_path) as opened:
        assert opened.ledger.head() == head  # before any replay: read from the files
        assert opened.ledger.replay().head == head
        assert opened.ledger.head() == head
    assert head_without_key(tmp_path / "nothing-here") is None
    (tmp_path / "empty").mkdir()
    assert head_without_key(tmp_path / "empty") is None
    # It reads names and one file. A changed newest record changes it.
    record_file(session_path, 14).write_bytes(sealed + b"x")
    assert head_without_key(session_path / "ledger") != head


# ---------------------------------------------------------------------------
# The policy record
# ---------------------------------------------------------------------------


def count_loads(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    loaded: list[int] = []
    real = ledger._load

    def load(seq: int) -> Any:
        loaded.append(seq)
        return real(seq)

    monkeypatch.setattr(ledger, "_load", load)
    return loaded


def test_policy_is_the_newest_policy_record(
    store: Store, gate_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = {**support.sample_policy(), "serial": 2}

    # Without a shortcut: read back from the newest record to the first policy record.
    ledger = fresh_ledger(store)
    loaded = count_loads(ledger, monkeypatch)
    assert ledger.current_policy() == (sample, 3)
    assert loaded == [7, 6, 5, 4, 3]

    # Session records name the policy record, which ends the walk early.
    session = support.record_session(store, advance=lambda: gate_clock.advance(seconds=20))
    assert session[0].data["policy_seq"] == 3  # filled in by the ledger
    assert session[-1].data["policy_seq"] == 3
    assert (session[0].data["documents"], session[-1].data["documents"]) == (4, 4)
    ledger = fresh_ledger(store)
    loaded = count_loads(ledger, monkeypatch)
    assert ledger.current_policy() == (sample, 3)
    assert loaded == [14, 3]  # the lock record, then the policy it names

    store.ledger.append("unlock", {"served": ["claude-desktop"]})
    for number in (3, 4, 5):
        store.ledger.append("release", release_fields("claude-desktop", number, "OK: text"))
    ledger = fresh_ledger(store)
    loaded = count_loads(ledger, monkeypatch)
    assert ledger.current_policy() == (sample, 3)
    assert loaded == [18, 17, 16, 15, 3]

    # A policy changed while unlocked is newer than what the unlock record names.
    changed = store.policy_json()
    changed["defaults"]["chars_per_day"] = 7000
    stored = store.set_policy_json(changed, by="test")
    store.ledger.append("release", release_fields("claude-desktop", 6, "OK: text"))
    ledger = fresh_ledger(store)
    loaded = count_loads(ledger, monkeypatch)
    assert ledger.current_policy() == (stored, 19)
    assert loaded == [20, 19]
    locked = store.ledger.append("lock", {"reason": "close"})
    assert locked.data["policy_seq"] == 19
    assert fresh_ledger(store).current_policy() == (stored, 19)

    # Every way of asking gives the same policy, and each answer is the caller's own copy.
    ledger = fresh_ledger(store)
    state = ledger.replay()
    loaded = count_loads(ledger, monkeypatch)
    answer, seq = ledger.current_policy()
    assert loaded == []  # after a replay the answer comes from memory
    assert (answer, seq) == (stored, 19) == (state.policy, state.policy_seq)
    assert state.policy_serial == 3
    assert store.policy_json() == stored
    answer["clients"].clear()
    assert ledger.current_policy()[0] == stored


# ---------------------------------------------------------------------------
# What is rebuilt from the records
# ---------------------------------------------------------------------------


def at(day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def test_replay_rebuilds_what_the_limits_need(path: Path, gate_clock: Any) -> None:
    with support.make_store(path, clock=gate_clock) as opened:
        ledger = opened.ledger
        lease = support.document_id(opened, "lease-2025")
        bank = support.document_id(opened, "bank-letter")
        medical = support.document_id(opened, "referral-2026")
        long_ago = datetime(2025, 9, 1, 8, 0, tzinfo=timezone.utc)

        def release(when: datetime, client: str, number: int, size: int, **fields: Any) -> None:
            text = "OK" + "." * (size - 2)
            ledger.append("release", release_fields(client, number, text, **fields), time=when)

        ledger.append("unlock", {"served": ["claude-desktop", "lm-studio"]}, time=long_ago)
        release(long_ago, "claude-desktop", 1, 300, spans=[(lease, 0, 300)])
        ledger.append("lock", {"reason": "timer"}, time=long_ago)
        ledger.append("unlock", {"served": ["claude-desktop", "claude-code"]}, time=at(1, 9))
        release(at(1), "claude-desktop", 2, 500, tool="wdva_search", spans=[(lease, 250, 400)])
        release(at(5), "claude-code", 1, 700, spans=[(lease, 400, 900), (bank, 0, 50)])
        release(at(11, 13), "claude-desktop", 3, 150, tool="wdva_search")  # no document text
        release(at(12, 8), "lm-studio", 1, 800, pool="local", spans=[(lease, 0, 800)])
        release(
            at(12, 8, 30), "claude-desktop", 7, 200, tool="wdva_search", spans=[(lease, 890, 950)]
        )
        # The same name recorded under the other pool counts where it was recorded.
        release(at(12, 8, 40), "claude-code", 2, 100, pool="local", spans=[(medical, 10, 20)])
        ledger.append(
            "denial",
            {"client": "claude-desktop", "code": "OVER_LIMIT", "variant": "day"},
            time=at(3),
        )
        ledger.append(
            "denial", {"client": "claude-desktop", "code": "OVER_LIMIT"}, time=at(12, 8, 45)
        )
        ledger.append("denial", {"client": "claude-desktop", "code": "NOT_FOUND"}, time=at(12, 7))
        ledger.append("denial", {"client": "(unregistered)", "code": "NOT_ALLOWED"}, time=at(12, 6))
        ledger.append("status", {"client": "claude-desktop"}, time=at(2))
        ledger.append(
            "status", {"client": "claude-desktop", "claimed": support.CLAIMED}, time=at(12, 8, 50)
        )
        ledger.append("status", {"client": "lm-studio"}, time=at(12, 8, 10))
        ledger.append("status", {"client": "claude-desktop"}, time=at(12, 8, 58))  # says nothing
        ledger.append(
            "approval",
            {"client": "lm-studio", "ref": f"{medical}.1", "body_sha256": "ab" * 32},
            time=at(12, 8, 55),
        )
        changed = opened.policy_json()
        changed["defaults"]["chars_per_day"] = 9000
        opened.set_policy_json(changed, by="test")
        opened.remove_document(bank)
        live = ledger.replay()

    # A new process: everything below comes from the record files alone.
    with support.reopen(path, clock=gate_clock) as opened:
        state = opened.ledger.replay()
        assert isinstance(state, LedgerState)
        assert state == live
        now = at(12, 9)
        assert gate_clock() == now

        # Policy: the newest policy record.
        assert state.policy == {**changed, "serial": 3}
        assert (state.policy_seq, state.policy_serial) == (27, 3)
        # Documents: add records minus remove records.
        assert [info.title for info in state.documents.values()] == [
            "lease-2025",
            "tax-note-2025",
            "referral-2026",
        ]
        assert list(state.removed) == [bank]
        assert state.documents[lease].seq == 4
        assert state.documents[lease].added == datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)

        # Client windows: time and size of that name's releases.
        desktop = state.client("claude-desktop")
        assert desktop.releases.entries() == [
            (long_ago, 300),
            (at(1), 500),
            (at(11, 13), 150),
            (at(12, 8, 30), 200),
        ]
        assert desktop.releases.used(now, DAY) == 350
        assert desktop.releases.used(now, MONTH) == 850
        assert desktop.releases.total == 1150
        code = state.client("claude-code")
        assert (code.releases.used(now, DAY), code.releases.used(now, MONTH)) == (100, 800)
        # Searches: the releases of the search tool.
        assert desktop.searches.entries() == [(at(1), 500), (at(11, 13), 150), (at(12, 8, 30), 200)]
        assert desktop.searches.count(now, DAY) == 2
        assert code.searches.count(now, MONTH) == 0
        # Release counter: the largest number recorded for the name.
        assert (desktop.release_seq, code.release_seq) == (7, 2)
        assert state.client("lm-studio").release_seq == 1
        assert state.client("never-seen").release_seq == 0
        assert state.client("never-seen").releases.used(now, MONTH) == 0
        assert "never-seen" not in state.clients  # asking does not invent a client

        # Pool windows: by the pool recorded then, over all its names.
        assert state.pool("hosted").used(now, DAY) == 350
        assert state.pool("hosted").used(now, MONTH) == 500 + 700 + 150 + 200
        assert state.pool("local").used(now, DAY) == 900
        assert len(state.pool("hosted")) == 5
        assert state.pool("other").used(now, MONTH) == 0

        # Per document and pool: the union of everything ever released, without a time limit.
        assert state.released_spans("hosted", lease) == [(0, 950)]
        assert state.released_chars("hosted", lease) == 950
        assert state.released_spans("local", lease) == [(0, 800)]
        assert state.released_chars("hosted", bank) == 50  # removed, but what left has left
        assert state.released_spans("local", medical) == [(10, 20)]
        assert state.released_chars("hosted", medical) == 0
        answer = state.released_spans("hosted", lease)
        answer.append((5000, 6000))  # the caller's own list
        assert state.released_chars("hosted", lease) == 950

        # Coalescing: the newest refusal per name and code, the newest status per name.
        assert state.denials == {
            ("claude-desktop", "OVER_LIMIT"): at(12, 8, 45),
            ("claude-desktop", "NOT_FOUND"): at(12, 7),
            ("(unregistered)", "NOT_ALLOWED"): at(12, 6),
        }
        assert state.statuses == {"claude-desktop": at(12, 8, 58), "lm-studio": at(12, 8, 10)}
        assert desktop.last_seen == at(12, 8, 58)
        assert desktop.last_claimed == support.CLAIMED  # the last thing it said about itself
        assert state.client("lm-studio").last_claimed is None
        assert state.client("(unregistered)").last_seen == at(12, 6)

        # The sessions, the head, the tally.
        assert (state.unlocks, state.session_open, state.previous_session) == (2, True, "unclean")
        assert (state.last_unlock, state.last_lock) == (at(1, 9), long_ago)
        assert state.head == opened.ledger.head() == store_head(path)
        assert state.head.seq == 28
        assert state.counts == {
            "init": 1,
            "policy": 3,
            "add": 4,
            "remove": 1,
            "unlock": 2,
            "lock": 1,
            "release": 7,
            "denial": 4,
            "status": 4,
            "approval": 1,
        }
        assert sum(state.counts.values()) == state.head.seq


def test_previous_session_is_read_from_the_record(path: Path, gate_clock: Any) -> None:
    with support.make_store(path, clock=gate_clock, documents=()) as opened:
        state = opened.ledger.replay()
        assert state.previous_session == "first"
        opened.ledger.append("unlock", {"served": [], "previous_session": "first"})
        assert state.session_open
        gate_clock.advance(hours=1)
        opened.ledger.append("lock", {"reason": "close"})
        assert state.previous_session == "clean"
    with support.reopen(path, clock=gate_clock) as opened:
        state = opened.ledger.replay()
        assert state.previous_session == "clean"
        assert state.last_lock == gate_clock()
        opened.ledger.append("unlock", {"served": [], "previous_session": "clean"})
        # The program ends here without a lock record.
    with support.reopen(path, clock=gate_clock) as opened:
        assert opened.ledger.replay().previous_session == "unclean"
        opened.ledger.append("unlock", {"served": [], "previous_session": "unclean"})
        opened.ledger.append("lock", {"reason": "close"})
    with support.reopen(path, clock=gate_clock) as opened:
        assert opened.ledger.replay().previous_session == "clean"
        assert opened.ledger.replay().unlocks == 3


def test_coalescing_times_survive_a_restart(path: Path, gate_clock: Any) -> None:
    hour = timedelta(minutes=60)
    with support.make_store(path, clock=gate_clock, documents=()) as opened:
        first = gate_clock()
        opened.ledger.append(
            "denial", {"client": "claude-desktop", "code": "NOT_FOUND", "folded_before": 0}
        )
        opened.ledger.append("status", {"client": "claude-desktop"})
        records = opened.ledger.head().seq
    for _ in range(3):  # however often the store is opened again
        gate_clock.advance(minutes=19)
        with support.reopen(path, clock=gate_clock) as opened:
            state = opened.ledger.replay()
            # A caller asks: is the newest such record at least an hour old?
            assert state.denials[("claude-desktop", "NOT_FOUND")] == first
            assert state.statuses["claude-desktop"] == first
            assert gate_clock() - first < hour
            assert ("claude-desktop", "NOT_ALLOWED") not in state.denials
            assert opened.ledger.head().seq == records
    gate_clock.advance(minutes=4)
    with support.reopen(path, clock=gate_clock) as opened:
        state = opened.ledger.replay()
        assert gate_clock() - state.denials[("claude-desktop", "NOT_FOUND")] >= hour
        opened.ledger.append(
            "denial", {"client": "claude-desktop", "code": "NOT_FOUND", "folded_before": 99}
        )
        assert state.denials[("claude-desktop", "NOT_FOUND")] == gate_clock()
        # A clock that was set back does not make the newest record look old.
        gate_clock.advance(hours=-5)
        opened.ledger.append("denial", {"client": "claude-desktop", "code": "NOT_FOUND"})
        assert state.denials[("claude-desktop", "NOT_FOUND")] == gate_clock() + timedelta(hours=5)
        assert opened.ledger.read(opened.ledger.head().seq - 1).data["folded_before"] == 99


def test_windows_count_by_age(store: Store) -> None:
    start = at(10)

    def release(when: datetime, number: int, size: int) -> None:
        fields = release_fields("claude-desktop", number, "x" * size, tool="wdva_search")
        store.ledger.append("release", fields, time=when)

    release(start, 1, 100)
    window = store.ledger.replay().client("claude-desktop").releases
    searches = store.ledger.replay().client("claude-desktop").searches
    second = timedelta(seconds=1)
    # A release counts while its age is less than the period.
    assert window.used(start, DAY) == 100
    assert window.used(start + DAY - second, DAY) == 100
    assert window.used(start + DAY, DAY) == 0
    assert window.used(start + DAY, MONTH) == 100
    assert window.used(start + MONTH - second, MONTH) == 100
    assert window.used(start + MONTH, MONTH) == 0
    assert searches.count(start + DAY - second, DAY) == 1
    assert searches.count(start + DAY, DAY) == 0

    # A release dated after now counts in every period: setting the clock back frees nothing.
    assert window.used(start - timedelta(days=400), DAY) == 100
    assert searches.count(start - timedelta(days=400), DAY) == 1

    # Records whose times go back and forth are counted the same way.
    release(start - timedelta(hours=30), 2, 40)
    release(start + timedelta(hours=2), 3, 7)
    assert window.entries()[1] == (start - timedelta(hours=30), 40)
    assert window.used(start, DAY) == 107  # the earlier one is 30 hours old
    assert window.used(start - timedelta(hours=7), DAY) == 147
    assert window.used(start + DAY + timedelta(hours=1), DAY) == 7
    assert window.total == 147
    assert len(window) == 3


def test_window_agrees_with_a_plain_sum() -> None:
    rng = random.Random(20261112)  # noqa: S311 - a repeatable sweep, not a secret
    for ordered in (True, False):
        window = Window()
        entries: list[tuple[datetime, int]] = []
        moment = at(1)
        for _ in range(300):
            step = rng.randint(0, 7200) if ordered else rng.randint(-40000, 90000)
            moment += timedelta(seconds=step)
            size = rng.randint(0, 8000)
            window._add(moment, size)
            entries.append((moment, size))
        assert window.entries() == entries
        for _ in range(200):
            now = at(1) + timedelta(seconds=rng.randint(-100000, 6000000))
            period = rng.choice((timedelta(seconds=60), DAY, MONTH))
            inside = [size for when, size in entries if now - when < period]
            assert window.used(now, period) == sum(inside)
            assert window.count(now, period) == len(inside)
        # The boundary itself, at the time of every entry.
        for when, _size in entries[::17]:
            inside = [size for other, size in entries if (when + DAY) - other < DAY]
            assert window.used(when + DAY, DAY) == sum(inside)


def test_released_spans_merge(store: Store) -> None:
    lease = support.document_id(store, "lease-2025")
    spans = [
        (100, 200),
        (300, 400),
        (150, 320),
        (400, 410),
        (0, 10),
        (10, 20),
        (500, 501),
        (405, 409),
    ]
    for number, span in enumerate(spans, start=1):
        fields = release_fields("claude-desktop", number, "OK: text", spans=[(lease, *span)])
        store.ledger.append("release", fields)
    state = store.ledger.replay()
    assert state.released_spans("hosted", lease) == [(0, 20), (100, 410), (500, 501)]
    covered: set[int] = set()
    for start, end in spans:
        covered.update(range(start, end))
    assert state.released_chars("hosted", lease) == len(covered) == 331
    # A span that is already covered adds nothing to it.
    fields = release_fields("claude-desktop", 9, "OK: text", spans=[(lease, 120, 380)])
    store.ledger.append("release", fields)
    assert state.released_chars("hosted", lease) == 331
    assert state.client("claude-desktop").releases.total == 9 * len("OK: text")


def test_state_after_appends_equals_a_fresh_replay(
    store: Store, gate_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(4711)  # noqa: S311 - a repeatable sequence, not a secret
    ledger = store.ledger
    state = ledger.replay()
    real = gate_ledger.atomic_write
    clients = ["claude-desktop", "claude-code", "lm-studio", "(unregistered)"]
    pools = {"claude-desktop": "hosted", "claude-code": "hosted", "lm-studio": "local"}
    failed = 0

    def step() -> None:
        nonlocal failed
        gate_clock.advance(seconds=rng.randint(-3600, 7200))
        client = rng.choice(clients)
        choice = rng.random()
        documents = list(state.documents.values())
        if choice < 0.5 and client in pools and documents:
            spans = []
            for info in rng.sample(documents, k=rng.randint(0, min(2, len(documents)))):
                first = rng.randrange(info.chars)
                spans.append((info.doc_id, first, rng.randint(first + 1, info.chars)))
            ledger.append(
                "release",
                release_fields(
                    client,
                    state.client(client).release_seq + rng.randint(1, 3),
                    "OK: " + "x" * rng.randint(0, 900),
                    pool=rng.choice(("hosted", "local")) if rng.random() < 0.1 else pools[client],
                    tool=rng.choice(("wdva_search", "wdva_read")),
                    spans=spans,
                ),
            )
        elif choice < 0.62:
            code = rng.choice(("NOT_FOUND", "OVER_LIMIT", "NOT_ALLOWED"))
            ledger.append("denial", {"client": client, "code": code})
        elif choice < 0.7:
            ledger.append(
                "status", {"client": client, "claimed": {"name": f"app{rng.randint(1, 3)}"}}
            )
        elif choice < 0.76:
            ledger.append(
                "unlock" if not state.session_open else "lock", {"served": [], "reason": "x"}
            )
        elif choice < 0.82:
            policy = store.policy_json()
            policy["defaults"]["chars_per_day"] = rng.randint(0, 50000)
            store.set_policy_json(policy, by="test")
        elif choice < 0.88:
            words = " ".join(f"w{rng.randint(0, 10**9)}" for _ in range(rng.randint(5, 400)))
            support.add_text(
                store, rng.choice(("home", "tax")), f"doc {rng.randint(0, 10**9)}", words
            )
        elif choice < 0.92 and documents:
            store.remove_document(rng.choice(documents).doc_id)
        elif choice < 0.96:
            # A record that is refused, and one that cannot be written, change nothing.
            with pytest.raises(LedgerError):
                ledger.append("release", {"client": client})
            monkeypatch.setattr(gate_ledger, "atomic_write", unwritable)
            with pytest.raises(LedgerError):
                ledger.append("status", {"client": client})
            monkeypatch.setattr(gate_ledger, "atomic_write", real)
            failed += 1
        else:
            ledger.append(
                "approval", {"client": client, "ref": "d-0123456789ab.1", "body_sha256": "c" * 64}
            )

    for round_number in range(6):
        for _ in range(60):
            step()
        fresh = fresh_ledger(store)
        assert fresh.replay() == state, round_number
        assert ledger.replay() is state
    assert failed > 0
    assert state.head.seq > 300
    assert set(state.counts) == set(gate_ledger.KINDS)
    assert state.head == store_head(store.path)
    # The comparison above is not vacuous: one release more makes the two differ.
    older = copy.deepcopy(state)
    ledger.append("release", release_fields("claude-desktop", 10**6, "OK: one more"))
    assert older != state


# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------


def test_record_times_are_utc_seconds(store: Store) -> None:
    moment = datetime(2026, 3, 4, 5, 6, 7, 999999, tzinfo=timezone.utc)
    assert format_time(moment) == "2026-03-04T05:06:07Z"
    assert parse_time("2026-03-04T05:06:07Z") == moment.replace(microsecond=0)
    eastern = timezone(timedelta(hours=-5))
    assert format_time(datetime(2026, 3, 4, 0, 6, 7, tzinfo=eastern)) == "2026-03-04T05:06:07Z"
    with pytest.raises(ValueError, match="time zone"):
        format_time(datetime(2026, 3, 4, 5, 6, 7))
    for text in (
        "2026-03-04 05:06:07Z",
        "2026-03-04T05:06:07",
        "2026-03-04T05:06:07+00:00",
        "2026-03-04T05:06:07.5Z",
        "2026-02-30T05:06:07Z",
        "2026-03-04T25:06:07Z",
        " 2026-03-04T05:06:07Z",
        "",
    ):
        with pytest.raises(ValueError):  # noqa: PT011 - any refusal will do
            parse_time(text)

    record = store.ledger.append(
        "status",
        {"client": "claude-desktop"},
        time=datetime(2026, 3, 4, 0, 6, 7, 5, tzinfo=eastern),
    )
    assert record.time == moment.replace(microsecond=0)
    assert record.data["time"] == "2026-03-04T05:06:07Z"
    assert store.ledger.replay().statuses["claude-desktop"] == record.time
    assert gate_ledger.utc_now().tzinfo is timezone.utc
