"""The ``wdva`` command line, driven in-process through ``main(argv)``."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from wdva import AdapterBundle, SecretKey, __version__, cli, is_vault

from .test_bundle import write_peft_dir


def run(*argv: str) -> int:
    try:
        return cli.main([str(a) for a in argv])
    except SystemExit as exc:
        return int(exc.code or 0)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (cli.ENV_KEY, cli.ENV_KEY_FILE, cli.ENV_PASSPHRASE):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "test.key"
    assert run("keygen", "-o", path) == 0
    return path


@pytest.fixture
def peft_dir(tmp_path: Path, fake_config: dict, fake_weights: bytes) -> Path:
    return write_peft_dir(tmp_path / "adapter", fake_config, fake_weights)


@pytest.fixture
def vault_path(tmp_path: Path, peft_dir: Path, key_file: Path) -> Path:
    out = tmp_path / "a.wdva"
    assert run("seal", peft_dir, "-o", out, "--key-file", key_file) == 0
    return out


def fake_getpass(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    prompts: list[str] = []
    queue = list(answers)

    def _getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return queue.pop(0)

    monkeypatch.setattr(cli.getpass, "getpass", _getpass)
    return prompts


# -- basics ----------------------------------------------------------------


def test_version_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--version") == 0
    assert capsys.readouterr().out.strip() == f"wdva {__version__}"
    assert run("--help") == 0
    out = capsys.readouterr().out
    for command in ("keygen", "seal", "unseal", "inspect", "rekey", "train", "query", "chat"):
        assert command in out
    assert run() == 2


def test_python_dash_m_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "wdva", "--version"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == f"wdva {__version__}"


def test_no_command_accepts_a_key_as_an_argument() -> None:
    """Keys on the command line leak into shell history and `ps`."""
    parser = cli.build_parser()
    key = SecretKey.generate().hex()
    for command in ("seal x -o y", "unseal x -o y", "inspect x", "query x p", "train d -o y"):
        for flag in ("--key", "--passphrase"):
            with pytest.raises(SystemExit):
                parser.parse_args([*command.split(), flag, key])


# -- keygen ----------------------------------------------------------------


def test_keygen_to_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "k.key"
    assert run("keygen", "-o", path) == 0
    out = capsys.readouterr().out
    key = SecretKey.from_file(path)
    assert key.hex() not in out
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert run("keygen", "-o", path) == 1
    assert "exists" in capsys.readouterr().err.lower()
    assert SecretKey.from_file(path) == key
    assert run("keygen", "-o", path, "--force") == 0
    assert SecretKey.from_file(path) != key


def test_keygen_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("keygen") == 0
    first = capsys.readouterr().out.strip()
    assert run("keygen") == 0
    second = capsys.readouterr().out.strip()
    assert len(first) == 64
    assert first != second
    SecretKey.from_hex(first)


# -- seal / inspect / unseal -----------------------------------------------


def test_seal_inspect_unseal_roundtrip(
    tmp_path: Path,
    peft_dir: Path,
    key_file: Path,
    fake_config: dict,
    fake_weights: bytes,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "a.wdva"
    assert run("seal", peft_dir, "-o", out, "--key-file", key_file, "--base-model", "org/m") == 0
    assert is_vault(out)
    assert "still on disk" in capsys.readouterr().out

    assert run("inspect", out) == 0
    text = capsys.readouterr().out
    assert "NOT verified" in text
    assert "org/m" not in text

    assert run("inspect", out, "--key-file", key_file) == 0
    text = capsys.readouterr().out
    assert "integrity     verified" in text
    assert "org/m" in text
    assert "rank 2" in text
    assert "16 in 2 tensors" in text

    assert run("inspect", out, "--key-file", key_file, "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] is True
    assert report["adapter"]["base_model"] == "org/m"
    assert report["header"]["unlock"] == "key"

    plain = tmp_path / "plain"
    assert run("unseal", out, "-o", plain, "--key-file", key_file) == 0
    assert "PLAINTEXT" in capsys.readouterr().out
    assert json.loads((plain / "adapter_config.json").read_text()) == fake_config
    assert (plain / "adapter_model.safetensors").read_bytes() == fake_weights
    assert run("unseal", out, "-o", plain, "--key-file", key_file) == 1
    assert run("unseal", out, "-o", plain, "--key-file", key_file, "--force") == 0


def test_seal_refuses_to_overwrite(vault_path: Path, peft_dir: Path, key_file: Path) -> None:
    before = vault_path.read_bytes()
    assert run("seal", peft_dir, "-o", vault_path, "--key-file", key_file) == 1
    assert vault_path.read_bytes() == before
    assert run("seal", peft_dir, "-o", vault_path, "--key-file", key_file, "--force") == 0
    assert vault_path.read_bytes() != before


def test_inspect_json_without_credentials(
    vault_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("inspect", vault_path, "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] is False
    assert "adapter" not in report
    assert report["size_bytes"] == vault_path.stat().st_size


# -- credentials -----------------------------------------------------------


def test_wrong_key_is_a_clean_error(
    tmp_path: Path, vault_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.key"
    run("keygen", "-o", other)
    capsys.readouterr()
    assert run("inspect", vault_path, "--key-file", other) == 1
    err = capsys.readouterr().err
    assert err.startswith("wdva: error: wrong key")
    assert "Traceback" not in err
    assert SecretKey.from_file(other).hex() not in err


def test_tampered_vault_is_a_clean_error(
    vault_path: Path, key_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = bytearray(vault_path.read_bytes())
    data[-1] ^= 1
    vault_path.write_bytes(data)
    assert run("unseal", vault_path, "-o", vault_path.parent / "x", "--key-file", key_file) == 1
    assert "tampered" in capsys.readouterr().err
    assert not (vault_path.parent / "x").exists()


def test_missing_credentials_is_a_usage_error(
    tmp_path: Path, vault_path: Path, peft_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("unseal", vault_path, "-o", tmp_path / "x") == 2
    assert "no credentials" in capsys.readouterr().err
    assert run("seal", peft_dir, "-o", tmp_path / "y.wdva") == 2
    assert not (tmp_path / "y.wdva").exists()


def test_credentials_from_environment(
    tmp_path: Path, vault_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.ENV_KEY_FILE, str(key_file))
    assert run("unseal", vault_path, "-o", tmp_path / "via_file") == 0
    monkeypatch.delenv(cli.ENV_KEY_FILE)
    monkeypatch.setenv(cli.ENV_KEY, SecretKey.from_file(key_file).hex())
    assert run("unseal", vault_path, "-o", tmp_path / "via_hex") == 0
    monkeypatch.setenv(cli.ENV_KEY, "not-a-key")
    assert run("unseal", vault_path, "-o", tmp_path / "bad") == 1


def test_passphrase_vault_via_prompt_and_environment(
    tmp_path: Path, peft_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.vault, "SCRYPT_N_DEFAULT", 2**14)
    out = tmp_path / "pw.wdva"

    fake_getpass(monkeypatch, "hunter2", "hunter3")
    assert run("seal", peft_dir, "-o", out, "--passphrase") == 2  # confirmation mismatch
    assert not out.exists()
    fake_getpass(monkeypatch, "")
    assert run("seal", peft_dir, "-o", out, "--passphrase") == 2  # empty

    prompts = fake_getpass(monkeypatch, "hunter2", "hunter2")
    assert run("seal", peft_dir, "-o", out, "--passphrase") == 0
    assert prompts == ["Passphrase: ", "Repeat passphrase: "]

    prompts = fake_getpass(monkeypatch, "hunter2")
    assert run("inspect", out, "--passphrase") == 0
    assert prompts == ["Passphrase: "]  # no confirmation when opening

    fake_getpass(monkeypatch, "wrong")
    assert run("inspect", out, "--passphrase") == 1

    monkeypatch.setenv(cli.ENV_PASSPHRASE, "hunter2")
    assert run("unseal", out, "-o", tmp_path / "plain") == 0


def test_passphrase_vault_prompts_automatically_on_a_terminal(
    tmp_path: Path, peft_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "pw.wdva"
    AdapterBundle.from_peft_dir(peft_dir).save(out, passphrase="hunter2", scrypt_n=2**14)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    prompts = fake_getpass(monkeypatch, "hunter2")
    assert run("unseal", out, "-o", tmp_path / "plain") == 0
    assert prompts == ["Passphrase: "]


# -- rekey / destroy-key ---------------------------------------------------


def test_rekey_in_place_to_a_new_key(
    tmp_path: Path, vault_path: Path, key_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    new_key = tmp_path / "new.key"
    run("keygen", "-o", new_key)
    before = AdapterBundle.load(vault_path, key=SecretKey.from_file(key_file))
    assert run("rekey", vault_path, "--key-file", key_file, "--new-key-file", new_key) == 0
    assert "old key" in capsys.readouterr().out
    assert AdapterBundle.load(vault_path, key=SecretKey.from_file(new_key)) == before
    assert run("inspect", vault_path, "--key-file", key_file) == 1
    assert run("rekey", vault_path, "--key-file", new_key) == 2  # no new credentials


def test_rekey_to_a_passphrase_in_a_new_file(
    tmp_path: Path, vault_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.vault, "SCRYPT_N_DEFAULT", 2**14)
    out = tmp_path / "pw.wdva"
    prompts = fake_getpass(monkeypatch, "s3cret", "s3cret")
    assert run("rekey", vault_path, "-o", out, "--key-file", key_file, "--new-passphrase") == 0
    assert prompts == ["New passphrase: ", "Repeat passphrase: "]
    assert AdapterBundle.load(out, passphrase="s3cret") == AdapterBundle.load(
        vault_path, key=SecretKey.from_file(key_file)
    )
    fake_getpass(monkeypatch, "s3cret", "s3cret")
    assert run("rekey", vault_path, "-o", out, "--key-file", key_file, "--new-passphrase") == 1


def test_destroy_key(vault_path: Path, key_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert run("destroy-key", key_file) == 2  # non-interactive needs --yes
    assert key_file.exists()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert run("destroy-key", key_file) == 1
    assert key_file.exists()

    assert run("destroy-key", key_file, "--yes") == 0
    assert not key_file.exists()
    assert is_vault(vault_path)  # the ciphertext remains, now unreadable
    assert run("destroy-key", key_file, "--yes") == 1


# -- errors ----------------------------------------------------------------


def test_file_problems_are_clean_errors(
    tmp_path: Path, key_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("inspect", tmp_path / "missing.wdva") == 1
    junk = tmp_path / "junk.wdva"
    junk.write_bytes(b"definitely not a vault")
    assert run("inspect", junk) == 1
    assert "not a WDVA vault" in capsys.readouterr().err
    assert run("seal", tmp_path / "nodir", "-o", tmp_path / "o.wdva", "--key-file", key_file) == 1
    assert run("inspect", junk, "--key-file", tmp_path / "nokey") == 1
    assert "Traceback" not in capsys.readouterr().err


def test_ml_commands_explain_the_missing_extra(
    tmp_path: Path,
    vault_path: Path,
    key_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wdva import _ml

    monkeypatch.setattr(_ml, "_ML_MODULES", ("a_module_that_does_not_exist",))
    doc = tmp_path / "d.txt"
    doc.write_text("hello")
    assert run("train", doc, "-o", tmp_path / "t.wdva", "--key-file", key_file) == 1
    assert "pip install 'wdva[ml]'" in capsys.readouterr().err
    assert run("query", vault_path, "hi", "--key-file", key_file) == 1
    assert "pip install 'wdva[ml]'" in capsys.readouterr().err


def test_train_usage_errors(tmp_path: Path, key_file: Path) -> None:
    pytest.importorskip("peft")
    assert run("train", "-o", tmp_path / "t.wdva", "--key-file", key_file) == 2
    existing = tmp_path / "exists.wdva"
    existing.write_bytes(b"x")
    doc = tmp_path / "d.txt"
    doc.write_text("hello")
    assert run("train", doc, "-o", existing, "--key-file", key_file) == 1
    assert existing.read_bytes() == b"x"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_own_warnings_are_one_clean_line(
    vault_path: Path, key_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file.chmod(0o644)
    capsys.readouterr()
    assert run("inspect", vault_path, "--key-file", key_file) == 0
    err = capsys.readouterr().err
    assert err.startswith("wdva: warning: key file ")
    assert "0644" in err
    assert ".py" not in err  # no Python file:line noise
    assert err.count("\n") == 1


def test_eof_at_a_prompt_is_a_clean_error(
    tmp_path: Path,
    peft_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", eof)
    assert run("seal", peft_dir, "-o", tmp_path / "x.wdva", "--passphrase") == 2
    err = capsys.readouterr().err
    assert "input ended" in err
    assert "Traceback" not in err
    assert not (tmp_path / "x.wdva").exists()


def test_inspect_cannot_be_used_to_inject_terminal_escapes(
    tmp_path: Path, key_file: Path, fake_weights: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    """Values read from a file are printed without control characters."""
    hostile = AdapterBundle(
        {"peft_type": "LORA\x1b[31m", "r": "8\x1b[2J", "lora_alpha": 4},
        fake_weights,
        {"base_model": "org/model\x1b]0;owned\x07", "training": {"steps": "many"}},
    )
    path = hostile.save(tmp_path / "h.wdva", key=SecretKey.from_file(key_file))
    capsys.readouterr()
    assert run("inspect", path, "--key-file", key_file) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "org/model" in out
    assert "training" not in out  # malformed statistics are skipped, not a crash
