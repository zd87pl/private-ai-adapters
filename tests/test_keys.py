"""SecretKey: construction, export, wiping, and the guard rails against leaking it."""

from __future__ import annotations

import copy
import os
import pickle
import stat
from pathlib import Path

import pytest

from wdva import InsecureKeyFileWarning, KeyWipedError, SecretKey, destroy_key_file, seal, unseal
from wdva.keys import KEY_SIZE, borrow_key


def test_generate_is_random_and_right_sized() -> None:
    a, b = SecretKey.generate(), SecretKey.generate()
    assert a != b
    assert len(bytes.fromhex(a.hex())) == KEY_SIZE == 32


def test_hex_roundtrip() -> None:
    key = SecretKey.generate()
    assert SecretKey.from_hex(key.hex()) == key
    assert SecretKey.from_hex(f"  {key.hex().upper()}\n") == key


@pytest.mark.parametrize("bad", ["", "abc", "zz" * 32, "ab" * 31, "ab" * 33])
def test_from_hex_rejects_bad_input_without_echoing_it(bad: str) -> None:
    with pytest.raises(ValueError, match="key") as excinfo:
        SecretKey.from_hex(bad)
    if bad:
        assert bad not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize("size", [0, 16, 31, 33, 64])
def test_wrong_length_is_rejected(size: int) -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        SecretKey(bytes(size))


def test_text_is_not_accepted_as_raw_bytes() -> None:
    with pytest.raises(TypeError, match="from_hex"):
        SecretKey("a" * 32)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SecretKey.from_hex(b"ab" * 32)  # type: ignore[arg-type]


def test_repr_and_str_never_show_the_key() -> None:
    key = SecretKey.generate()
    for text in (repr(key), str(key), f"{key}", f"{key!r}", str([key]), str({"k": key})):
        assert key.hex() not in text
        assert "redacted" in text


def test_cannot_be_pickled_or_hashed() -> None:
    key = SecretKey.generate()
    with pytest.raises(TypeError, match="pickled"):
        pickle.dumps(key)
    with pytest.raises(TypeError):
        hash(key)


def test_copies_are_independent() -> None:
    key = SecretKey.generate()
    for clone in (copy.copy(key), copy.deepcopy(key)):
        assert clone == key
        clone.wipe()
        assert not key.wiped


def test_wipe_zeroes_the_buffer_and_disables_the_key() -> None:
    key = SecretKey.generate()
    buf = key._buf
    key.wipe()
    assert bytes(buf) == bytes(KEY_SIZE)
    assert key.wiped
    assert "wiped" in repr(key)
    with pytest.raises(KeyWipedError):
        key.hex()
    with pytest.raises(KeyWipedError):
        seal(b"x", key=key)
    assert key != SecretKey(bytes(KEY_SIZE))  # a wiped key equals nothing
    key.wipe()  # idempotent


def test_context_manager_wipes_on_exit() -> None:
    with SecretKey.generate() as key:
        blob = seal(b"x", key=key)
        assert unseal(blob, key=key).payload == b"x"
    assert key.wiped


def test_equality_is_by_value() -> None:
    key = SecretKey.generate()
    assert key == SecretKey.from_hex(key.hex())
    assert key != SecretKey.generate()
    assert key != key.hex()


def test_borrow_key_wipes_only_its_own_temporaries() -> None:
    key = SecretKey.generate()
    with borrow_key(key) as material:
        assert material is key._buf
    assert not key.wiped
    with borrow_key(key.hex()) as material:
        temp = material
        assert bytes(material) == bytes.fromhex(key.hex())
    assert bytes(temp) == bytes(KEY_SIZE)
    with pytest.raises(TypeError, match="SecretKey"), borrow_key(42):  # type: ignore[arg-type]
        pass


# -- key files -------------------------------------------------------------


def test_save_and_load(tmp_path: Path) -> None:
    key = SecretKey.generate()
    path = key.save(tmp_path / "my.key")
    assert SecretKey.from_file(path) == key
    assert path.read_text() == key.hex() + "\n"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_refuses_to_clobber_an_existing_key(tmp_path: Path) -> None:
    first = SecretKey.generate()
    path = first.save(tmp_path / "my.key")
    with pytest.raises(FileExistsError):
        SecretKey.generate().save(path)
    assert SecretKey.from_file(path) == first
    second = SecretKey.generate()
    second.save(path, overwrite=True)
    assert SecretKey.from_file(path) == second


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_overwrite_tightens_permissions(tmp_path: Path) -> None:
    path = tmp_path / "my.key"
    path.write_text("old")
    path.chmod(0o644)
    SecretKey.generate().save(path, overwrite=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_world_readable_key_file_warns(tmp_path: Path) -> None:
    path = SecretKey.generate().save(tmp_path / "my.key")
    path.chmod(0o644)
    with pytest.warns(InsecureKeyFileWarning, match="0644"):
        SecretKey.from_file(path)


def test_missing_or_garbage_key_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SecretKey.from_file(tmp_path / "nope.key")
    bad = tmp_path / "bad.key"
    bad.write_text("this is not a key")
    bad.chmod(0o600)
    with pytest.raises(ValueError, match="hex"):
        SecretKey.from_file(bad)


def test_destroy_key_file_makes_the_vault_unreadable(tmp_path: Path) -> None:
    key = SecretKey.generate()
    path = key.save(tmp_path / "my.key")
    blob = seal(b"forget me", key=key)
    original = path.read_bytes()
    key.wipe()

    destroy_key_file(path)

    assert not path.exists()
    assert original not in b"".join(p.read_bytes() for p in tmp_path.iterdir())
    with pytest.raises(KeyWipedError):
        unseal(blob, key=key)
    with pytest.raises(FileNotFoundError):
        destroy_key_file(path)


# -- hardening found by adversarial review ---------------------------------


@pytest.mark.parametrize("bad", [32, KEY_SIZE, [0] * 32, [7] * 32, range(32), None, 3.5, (1, 2)])
def test_only_real_bytes_make_a_key(bad: object) -> None:
    """bytearray(32) is 32 zero bytes: SecretKey(32) must not quietly be an all-zero key."""
    with pytest.raises(TypeError, match="32 raw bytes"):
        SecretKey(bad)  # type: ignore[arg-type]


def test_save_never_leaves_a_damaged_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = SecretKey.generate()
    path = old.save(tmp_path / "my.key")

    # Saving a wiped key over an existing one must not truncate it first.
    wiped = SecretKey.generate()
    wiped.wipe()
    with pytest.raises(KeyWipedError):
        wiped.save(path, overwrite=True)
    assert SecretKey.from_file(path) == old

    # Neither must a full disk, and a failed first save must not block the retry.
    def no_space(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", no_space)
    with pytest.raises(OSError, match="No space"):
        SecretKey.generate().save(path, overwrite=True)
    fresh = tmp_path / "fresh.key"
    with pytest.raises(OSError, match="No space"):
        SecretKey.generate().save(fresh)
    monkeypatch.undo()
    assert SecretKey.from_file(path) == old
    assert not fresh.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["my.key"]
    SecretKey.generate().save(fresh)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits and symlinks")
def test_overwriting_never_writes_through_an_open_or_loose_file(tmp_path: Path) -> None:
    path = tmp_path / "my.key"
    path.write_text("an old, world-readable file")
    path.chmod(0o644)
    with open(path) as already_open:
        new = SecretKey.generate()
        new.save(path, overwrite=True)
        # A reader that had the old file open does not get to see the new key.
        assert already_open.read() == "an old, world-readable file"
    assert SecretKey.from_file(path) == new
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    link = tmp_path / "link.key"
    link.symlink_to(victim)
    with pytest.raises(FileExistsError):
        SecretKey.generate().save(link)
    SecretKey.generate().save(link, overwrite=True)
    assert not link.is_symlink()
    assert victim.read_text() == "precious"


def test_destroy_refuses_files_that_are_not_keys(tmp_path: Path) -> None:
    document = tmp_path / "notes.txt"
    document.write_text("irreplaceable")
    with pytest.raises(ValueError, match="does not contain a WDVA key"):
        destroy_key_file(document)
    assert document.read_text() == "irreplaceable"
    with pytest.raises(FileNotFoundError):
        destroy_key_file(tmp_path)  # a directory
    if os.name == "posix":
        real = SecretKey.generate().save(tmp_path / "real.key")
        link = tmp_path / "link.key"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="symbolic link"):
            destroy_key_file(link)
        assert real.exists()


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_key_files_in_other_text_encodings(tmp_path: Path, encoding: str) -> None:
    key = SecretKey.generate()
    path = tmp_path / "k.key"
    path.write_bytes((key.hex() + "\r\n").encode(encoding))
    path.chmod(0o600)
    assert SecretKey.from_file(path) == key
    path.write_bytes(b"\xff\xfe" + bytes(4096))
    with pytest.raises(ValueError, match="is not a key file"):
        SecretKey.from_file(path)
