# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The ``wdva`` command line.

Keys are never accepted as command-line arguments, because those end up in shell
history and process listings. Supply credentials with ``--key-file``,
``--passphrase`` (interactive prompt), or the environment variables
``WDVA_KEY_FILE``, ``WDVA_KEY`` and ``WDVA_PASSPHRASE``.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import sys
import warnings
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn, TextIO

from wdva import vault
from wdva._version import __version__
from wdva.bundle import CONTENT_TYPE_ADAPTER, AdapterBundle
from wdva.errors import WDVAError, WDVAWarning
from wdva.keys import SecretKey, check_destroyable, destroy_key_file

ENV_KEY = "WDVA_KEY"
ENV_KEY_FILE = "WDVA_KEY_FILE"
ENV_PASSPHRASE = "WDVA_PASSPHRASE"  # noqa: S105 - name of an environment variable


class _UsageError(Exception):
    """A problem with how the command was invoked (exit status 2)."""


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _add_credentials(parser: argparse.ArgumentParser, *, new: bool = False) -> None:
    prefix = "new-" if new else ""
    what = "new " if new else ""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        f"--{prefix}key-file",
        metavar="PATH",
        help=f"file holding the {what}key (create one with `wdva keygen -o PATH`)",
    )
    group.add_argument(
        f"--{prefix}passphrase",
        action="store_true",
        help=f"prompt for a {what}passphrase instead of using a key file",
    )


def _prompt_passphrase(prompt: str, *, confirm: bool) -> str:
    first = getpass.getpass(prompt)
    if not first:
        raise _UsageError("passphrase must not be empty")
    if confirm and getpass.getpass("Repeat passphrase: ") != first:
        raise _UsageError("passphrases do not match")
    return first


def _credentials(
    args: argparse.Namespace, *, sealing: bool, new: bool = False, required: bool = True
) -> dict[str, Any]:
    """Resolve ``{"key": ...}`` or ``{"passphrase": ...}`` from flags, then environment."""
    key_file = getattr(args, "new_key_file" if new else "key_file")
    use_passphrase = getattr(args, "new_passphrase" if new else "passphrase")
    label = "New passphrase: " if new else "Passphrase: "
    if key_file:
        return {"key": SecretKey.from_file(key_file)}
    if use_passphrase:
        return {"passphrase": _prompt_passphrase(label, confirm=sealing)}
    if not new:
        env = os.environ
        if env.get(ENV_KEY_FILE):
            return {"key": SecretKey.from_file(env[ENV_KEY_FILE])}
        if env.get(ENV_KEY):
            return {"key": SecretKey.from_hex(env[ENV_KEY])}
        if env.get(ENV_PASSPHRASE):
            return {"passphrase": env[ENV_PASSPHRASE]}
    if not required:
        return {}
    flag = "--new-key-file or --new-passphrase" if new else "--key-file or --passphrase"
    hint = "" if new else f" (or set {ENV_KEY_FILE}, {ENV_KEY} or {ENV_PASSPHRASE})"
    raise _UsageError(f"no credentials given: pass {flag}{hint}")


def _unlock_credentials(args: argparse.Namespace, path: str) -> dict[str, Any]:
    """Credentials for opening ``path``, prompting for a passphrase if the vault needs one."""
    creds = _credentials(args, sealing=False, required=False)
    if creds:
        return creds
    if vault.read_header(path).uses_passphrase and sys.stdin.isatty():
        return {"passphrase": _prompt_passphrase("Passphrase: ", confirm=False)}
    return _credentials(args, sealing=False)  # raises the usage error


# ---------------------------------------------------------------------------
# Commands that need only the core install
# ---------------------------------------------------------------------------


def _cmd_keygen(args: argparse.Namespace) -> int:
    if args.force and not args.out:
        raise _UsageError("--force only applies when writing a key file with -o")
    key = SecretKey.generate()
    try:
        if args.out:
            path = key.save(args.out, overwrite=args.force)
            how = "mode 0600" if os.name == "posix" else "protect it with folder permissions"
            print(f"Wrote a new 256-bit key to {path} ({how}).")
            print("Back it up somewhere safe: without it your vaults cannot be opened.")
        else:
            print(key.hex())
            if sys.stdout.isatty():
                print(
                    "wdva: note: the key was printed to the terminal, where it may be logged "
                    "or scrolled back to. Prefer `wdva keygen -o FILE`.",
                    file=sys.stderr,
                )
    finally:
        key.wipe()
    return 0


def _cmd_seal(args: argparse.Namespace) -> int:
    info = {"base_model": args.base_model} if args.base_model else {}
    bundle = AdapterBundle.from_peft_dir(args.adapter_dir, info=info)
    creds = _credentials(args, sealing=True)
    path = bundle.save(args.out, overwrite=args.force, **creds)
    print(f"Sealed {args.adapter_dir} -> {path} ({path.stat().st_size:,} bytes)")
    print(
        "Only adapter_config.json and adapter_model.safetensors were sealed. The plaintext "
        f"files in {args.adapter_dir} are still on disk; delete them if unwanted."
    )
    return 0


def _cmd_unseal(args: argparse.Namespace) -> int:
    bundle = AdapterBundle.load(args.vault, **_unlock_credentials(args, args.vault))
    out = bundle.to_peft_dir(args.out, overwrite=args.force)
    print(f"Wrote the adapter IN PLAINTEXT to {out}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    header = vault.read_header(args.vault)
    report: dict[str, Any] = {
        "file": str(args.vault),
        "size_bytes": Path(args.vault).stat().st_size,
        "header": header.to_public_dict(),
        "verified": False,
    }
    creds = _credentials(args, sealing=False, required=False)
    if creds:
        payload, _ = vault.unseal_file(args.vault, **creds)
        report["verified"] = True
        if header.content_type == CONTENT_TYPE_ADAPTER:
            report["adapter"] = AdapterBundle.from_bytes(payload).summary()
        else:
            report["payload_bytes"] = len(payload)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    head = report["header"]
    print(f"{report['file']}  ({report['size_bytes']:,} bytes)")
    print(f"  format        WDVA vault v{head['format_version']}")
    print(f"  cipher        {head['algorithm']} / {head['kdf']}")
    print(f"  unlock with   {head['unlock']}")
    print(f"  content       {_clean(head['content_type'])}")
    if head["public_metadata"]:
        print(f"  public meta   {json.dumps(head['public_metadata'], sort_keys=True)}")
    if not report["verified"]:
        print("  (header only, NOT verified - supply credentials to authenticate and look inside)")
        return 0
    print("  integrity     verified")
    adapter = report.get("adapter")
    if adapter:
        kind = "DoRA" if adapter["use_dora"] else adapter["peft_type"]
        print(f"  base model    {_clean(adapter['base_model'])}")
        print(
            f"  adapter       {_clean(kind)}, rank {_clean(adapter['rank'])}, "
            f"alpha {_clean(adapter['alpha'])}"
        )
        print(f"  parameters    {adapter['parameters']:,} in {adapter['tensors']} tensors")
        training = adapter["info"].get("training")
        if isinstance(training, dict) and all(
            isinstance(training.get(field), (int, float))
            for field in ("steps", "loss_before", "loss_after")
        ):
            print(
                f"  training      {training['steps']} steps, loss "
                f"{training['loss_before']:.3f} -> {training['loss_after']:.3f}"
            )
    return 0


def _clean(value: object) -> str:
    """Make a value read from a file safe to print: no control or escape characters."""
    return "".join(ch if ch.isprintable() else "\N{REPLACEMENT CHARACTER}" for ch in str(value))


def _cmd_rekey(args: argparse.Namespace) -> int:
    payload, header = vault.unseal_file(args.vault, **_unlock_credentials(args, args.vault))
    new_creds = _credentials(args, sealing=True, new=True)
    # In place means the real file: re-sealing a symlink would replace the link
    # and leave the vault it points to under the old key.
    out = args.out or os.path.realpath(args.vault)
    in_place = args.out is None
    extra: dict[str, Any] = {}
    if "passphrase" in new_creds:
        # Never weaken a vault that was deliberately sealed with a higher cost.
        extra["scrypt_n"] = max(header.scrypt_n or 0, vault.SCRYPT_N_DEFAULT)
    vault.seal_to_file(
        out,
        payload,
        content_type=header.content_type,
        public_metadata=header.public_metadata,
        overwrite=args.force or in_place,
        **new_creds,
        **extra,
    )
    print(f"Re-sealed {args.vault} -> {out} under the new credentials.")
    print("Copies of the old file, and the old key, still open the old ciphertext.")
    return 0


def _cmd_destroy_key(args: argparse.Namespace) -> int:
    check_destroyable(args.key_file)  # refuse vaults, documents and symlinks before asking
    if not args.yes:
        if not sys.stdin.isatty():
            raise _UsageError("refusing to destroy a key without --yes")
        answer = input(
            f"Destroy {args.key_file}? Every vault sealed with it becomes unreadable. [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("Aborted.")
            return 1
    destroy_key_file(args.key_file)
    print(f"Destroyed {args.key_file}. Other copies of this key (backups, exports) are unaffected.")
    return 0


# ---------------------------------------------------------------------------
# Commands that need the ML stack
# ---------------------------------------------------------------------------


def _module_list(text: str) -> list[str]:
    """Parse ``--target-modules q_proj,v_proj``.

    One comma-separated value, not a space-separated list: a list would swallow
    the document paths that follow it on the command line.
    """
    names = [name.strip() for name in text.split(",")]
    for name in names:
        if not name or any(ch.isspace() for ch in name) or "/" in name or "\\" in name:
            raise argparse.ArgumentTypeError(
                f"{text!r} is not a comma-separated list of module names (e.g. q_proj,v_proj)"
            )
    return names


def _quiet_ml_logs(verbose: bool) -> None:
    if verbose:
        return
    from transformers.utils import logging as hf_logging

    hf_logging.set_verbosity_error()
    hf_logging.disable_progress_bar()
    # The call above also silences download progress, and a first run fetches
    # hundreds of megabytes. Keep that one bar.
    with contextlib.suppress(ImportError, AttributeError):
        import huggingface_hub.utils as hub_utils

        hub_utils.enable_progress_bars()


def _cmd_train(args: argparse.Namespace) -> int:
    from wdva import _ml
    from wdva.documents import load_examples
    from wdva.training import TrainingConfig, train_adapter

    _ml.require_ml()
    if not args.documents and not args.examples:
        raise _UsageError("nothing to train on: give documents and/or --examples FILE")
    if Path(args.out).exists() and not args.force:
        raise FileExistsError(f"{args.out} already exists")
    creds = _credentials(args, sealing=True)
    _quiet_ml_logs(args.verbose)

    overrides: dict[str, Any] = {
        "rank": args.rank,
        "alpha": args.alpha if args.alpha is not None else (2 * args.rank if args.rank else None),
        "epochs": args.epochs,
        "max_steps": args.max_steps,
        "batch_size": args.batch_size,
        "learning_rate": args.learning_rate,
        "max_length": args.max_length,
        "seed": args.seed,
        "target_modules": args.target_modules,
    }
    config = TrainingConfig(
        use_dora=not args.lora, **{k: v for k, v in overrides.items() if v is not None}
    )
    examples = load_examples(args.examples) if args.examples else []

    def progress(step: int, total: int, loss: float) -> None:
        if step == 1 or step == total or step % max(1, total // 20) == 0:
            print(f"  step {step:>5}/{total}  loss {loss:.4f}", file=sys.stderr)

    print(
        f"Training on {args.base_model}: {len(args.documents)} document(s), "
        f"{len(examples)} example(s) ...",
        file=sys.stderr,
    )
    result = train_adapter(
        args.base_model,
        args.out,
        documents=args.documents,
        examples=examples,
        config=config,
        device=args.device,
        local_files_only=args.offline,
        overwrite=args.force,
        on_step=progress,
        **creds,
    )
    print(
        f"Sealed adapter -> {result.adapter_path} ({result.vault_bytes:,} bytes, "
        f"{result.trainable_parameters:,} parameters)"
    )
    measured = (
        ""
        if result.loss_sequences == result.num_sequences
        else f" (measured on a sample of {result.loss_sequences} sequences)"
    )
    print(
        f"{result.steps} steps over {result.num_sequences} sequences in {result.seconds:.0f}s; "
        f"loss {result.loss_before:.3f} -> {result.loss_after:.3f}{measured}"
    )
    return 0


def _open_engine(args: argparse.Namespace) -> Any:
    from wdva import _ml
    from wdva.engine import WDVA

    _ml.require_ml()
    bundle = AdapterBundle.load(args.vault, **_unlock_credentials(args, args.vault))
    base_model = args.base_model or bundle.base_model
    if not base_model:
        raise _UsageError("this vault does not record its base model; pass --base-model")
    _quiet_ml_logs(args.verbose)
    engine = WDVA(base_model, device=args.device, local_files_only=args.offline)
    engine.load(bundle)
    return engine


def _emit(engine: Any, prompt: Any, args: argparse.Namespace, *, use_adapter: bool = True) -> str:
    pieces: list[str] = []
    for piece in engine.stream(
        prompt,
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
        system=args.system,
        use_adapter=use_adapter,
    ):
        pieces.append(piece)
        print(piece, end="", flush=True)
    print()
    return "".join(pieces).strip()


def _cmd_query(args: argparse.Namespace) -> int:
    engine = _open_engine(args)
    try:
        if args.compare:
            print("--- base model ---")
            _emit(engine, args.prompt, args, use_adapter=False)
            print("--- with adapter ---")
        _emit(engine, args.prompt, args)
    finally:
        engine.close()
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    engine = _open_engine(args)
    messages: list[dict[str, str]] = []
    print("Adapter loaded. Type a message; /reset clears history; /exit or Ctrl-D quits.")
    try:
        while True:
            try:
                line = input("you> ").strip()
            except EOFError:
                print()
                break
            if not line:
                continue
            if line in {"/exit", "/quit"}:
                break
            if line == "/reset":
                messages.clear()
                continue
            messages.append({"role": "user", "content": line})
            print("ai> ", end="", flush=True)
            reply = _emit(engine, messages, args)
            messages.append({"role": "assistant", "content": reply})
    except KeyboardInterrupt:
        print()
    finally:
        engine.close()
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_generation(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-model", help="override the base model recorded in the vault")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--temperature", type=float, default=0.7, help="0 for deterministic output")
    parser.add_argument("--system", help="system prompt")
    parser.add_argument("--device", help="cuda, mps or cpu (default: auto)")
    parser.add_argument("--offline", action="store_true", help="never contact the Hugging Face Hub")
    parser.add_argument("--verbose", action="store_true", help="show library logs")


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False everywhere: otherwise `--key <hex>` would be accepted as an
    # abbreviation of `--key-file`, inviting people to put key material in shell history.
    parser = argparse.ArgumentParser(
        prog="wdva",
        allow_abbrev=False,
        description="Encrypted vaults for LoRA/DoRA adapters.",
        epilog=(
            f"Credentials: --key-file PATH, --passphrase, or the environment variables "
            f"{ENV_KEY_FILE}, {ENV_KEY} (hex) and {ENV_PASSPHRASE}."
        ),
    )
    parser.add_argument("--version", action="version", version=f"wdva {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    class _Sub:
        @staticmethod
        def add_parser(name: str, **kwargs: Any) -> argparse.ArgumentParser:
            parser_: argparse.ArgumentParser = subparsers.add_parser(
                name, allow_abbrev=False, **kwargs
            )
            return parser_

    sub = _Sub

    p = sub.add_parser("keygen", help="generate a new 256-bit key")
    p.add_argument("-o", "--out", metavar="PATH", help="write to a 0600 key file (else stdout)")
    p.add_argument("--force", action="store_true", help="replace an existing key file")
    p.set_defaults(func=_cmd_keygen)

    p = sub.add_parser("seal", help="encrypt an existing PEFT adapter directory into a vault")
    p.add_argument("adapter_dir", help="directory with adapter_config.json + .safetensors")
    p.add_argument("-o", "--out", required=True, metavar="VAULT")
    p.add_argument("--base-model", help="record the base model id inside the vault")
    p.add_argument("--force", action="store_true", help="replace an existing vault")
    _add_credentials(p)
    p.set_defaults(func=_cmd_seal)

    p = sub.add_parser("unseal", help="decrypt a vault back to a PLAINTEXT PEFT directory")
    p.add_argument("vault")
    p.add_argument("-o", "--out", required=True, metavar="DIR")
    p.add_argument("--force", action="store_true", help="replace existing files")
    _add_credentials(p)
    p.set_defaults(func=_cmd_unseal)

    p = sub.add_parser("inspect", help="show a vault's header and, with credentials, contents")
    p.add_argument("vault")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    _add_credentials(p)
    p.set_defaults(func=_cmd_inspect)

    p = sub.add_parser("rekey", help="re-encrypt a vault under a new key or passphrase")
    p.add_argument("vault")
    p.add_argument("-o", "--out", metavar="VAULT", help="write here instead of in place")
    p.add_argument("--force", action="store_true", help="replace an existing output file")
    _add_credentials(p)
    _add_credentials(p, new=True)
    p.set_defaults(func=_cmd_rekey)

    p = sub.add_parser("destroy-key", help="overwrite and delete a key file (crypto-shredding)")
    p.add_argument("key_file")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=_cmd_destroy_key)

    p = sub.add_parser("train", help="fine-tune an adapter on documents and seal it")
    p.add_argument("documents", nargs="*", help="text, Markdown or PDF files")
    p.add_argument("-o", "--out", required=True, metavar="VAULT")
    p.add_argument("--examples", metavar="FILE.jsonl", help="prompt/response pairs")
    p.add_argument("--base-model", default=None, help="model id or path")
    p.add_argument("--epochs", type=int, help="passes over the data (default: 3)")
    p.add_argument("--max-steps", type=int, help="cap on optimizer steps")
    p.add_argument("--rank", type=int, help="LoRA rank (default: 8)")
    p.add_argument(
        "--target-modules",
        type=_module_list,
        metavar="NAMES",
        help="comma-separated module names to adapt, e.g. q_proj,v_proj "
        "(default: all linear layers)",
    )
    p.add_argument("--alpha", type=int, help="LoRA alpha (default: 2 x rank)")
    p.add_argument("--lora", action="store_true", help="plain LoRA instead of DoRA")
    p.add_argument("--learning-rate", type=float, help="default: 1e-3")
    p.add_argument("--batch-size", type=int, help="default: 4")
    p.add_argument("--max-length", type=int, help="tokens per sequence (default: 512)")
    p.add_argument("--seed", type=int, help="default: 0")
    p.add_argument("--device", help="cuda, mps or cpu (default: auto)")
    p.add_argument("--offline", action="store_true", help="never contact the Hugging Face Hub")
    p.add_argument("--force", action="store_true", help="replace an existing vault")
    p.add_argument("--verbose", action="store_true", help="show library logs")
    _add_credentials(p)
    p.set_defaults(func=_cmd_train)

    p = sub.add_parser("query", help="ask one question with the adapter applied")
    p.add_argument("vault")
    p.add_argument("prompt")
    p.add_argument("--compare", action="store_true", help="also show the base model's answer")
    _add_generation(p)
    _add_credentials(p)
    p.set_defaults(func=_cmd_query)

    p = sub.add_parser("chat", help="interactive chat with the adapter applied")
    p.add_argument("vault")
    _add_generation(p)
    _add_credentials(p)
    p.set_defaults(func=_cmd_chat)

    return parser


@contextlib.contextmanager
def _plain_warnings() -> Iterator[None]:
    """Show this package's own warnings as one clean line instead of Python's file:line form."""
    original = warnings.showwarning

    def show(
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: TextIO | None = None,
        line: str | None = None,
    ) -> None:
        if issubclass(category, WDVAWarning):
            print(f"wdva: warning: {message}", file=sys.stderr)
        else:
            original(message, category, filename, lineno, file, line)

    with warnings.catch_warnings():
        warnings.simplefilter("always", WDVAWarning)
        warnings.showwarning = show
        yield


def _fail(message: str, status: int) -> NoReturn:
    print(f"wdva: error: {message}", file=sys.stderr)
    raise SystemExit(status)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``wdva`` console script."""
    for stream in (sys.stdout, sys.stderr):
        # Model output can contain characters a legacy console encoding cannot
        # represent; show a replacement instead of aborting mid-answer.
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "train" and args.base_model is None:
        from wdva.engine import DEFAULT_BASE_MODEL

        args.base_model = DEFAULT_BASE_MODEL
    try:
        with _plain_warnings():
            return int(args.func(args))
    except _UsageError as exc:
        _fail(str(exc), 2)
    except EOFError:
        _fail("input ended before a required answer was given", 2)
    except FileExistsError as exc:
        message = str(exc).replace(" (pass overwrite=True to replace it)", "")
        _fail(f"{message} (use --force to replace it)", 1)
    except (WDVAError, OSError, ValueError) as exc:
        _fail(str(exc), 1)
    except KeyboardInterrupt:
        _fail("interrupted", 130)


if __name__ == "__main__":
    sys.exit(main())
