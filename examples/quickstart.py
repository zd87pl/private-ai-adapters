# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Quickstart: teach a small local model your notes and keep the result encrypted.

The whole WDVA loop in one script:

1. generate a 256-bit key and save it to a key file with mode 0600;
2. ask the base model questions it cannot know the answers to;
3. fine-tune a DoRA adapter on ``data/notes.txt`` and ``data/qa.jsonl`` and seal it
   straight into ``notes.wdva`` - no checkpoint or plaintext adapter is written;
4. apply the adapter from ciphertext inside a ``with`` block and ask again, both
   with the adapter and with ``use_adapter=False``;
5. show that the adapter is gone once the block ends.

Run ``python examples/quickstart.py`` (needs ``pip install 'wdva[ml]'``; the first
run downloads the base model). Expect the training loss to fall steeply and the
adapter to answer questions about the notes that the base model can only guess at.
Training is the slow part: minutes on a laptop CPU. The last question is a control
unrelated to the notes. This is a demo, not a benchmark - read the answers.
"""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Sequence
from pathlib import Path

import wdva
from wdva import WDVA, SecretKey, TrainingConfig
from wdva.documents import load_examples

DATA = Path(__file__).resolve().parent / "data"
QUESTIONS = (
    "What is the greenhouse door code?",  # appears word for word in qa.jsonl
    "Where is the spare key?",  # a rewording of a question in qa.jsonl
    "What is the capital of France?",  # control: unrelated to the notes
)


def ask(engine: WDVA, question: str, limit: int, *, use_adapter: bool = True) -> str:
    """One greedy answer (temperature=0 is repeatable), flattened to a single line."""
    text = engine.generate(question, temperature=0, max_new_tokens=limit, use_adapter=use_adapter)
    return " ".join(text.split())


def progress(step: int, total: int, loss: float) -> None:
    if step == 1 or step == total or step % max(1, total // 10) == 0:
        print(f"  step {step:>4}/{total}  loss {loss:.4f}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WDVA quickstart: train, seal, load, ask.")
    parser.add_argument("--base-model", default=wdva.DEFAULT_BASE_MODEL, help="model id or path")
    parser.add_argument("--device", help="cuda, mps or cpu (default: auto)")
    parser.add_argument("--workdir", type=Path, help="for the key and vault (default: a temp dir)")
    parser.add_argument("--notes", type=Path, default=DATA / "notes.txt", help="text to learn")
    parser.add_argument("--examples", type=Path, default=DATA / "qa.jsonl", help="Q/A pairs")
    parser.add_argument("--epochs", type=int, default=30, help="passes over the data")
    parser.add_argument("--rank", type=int, default=8, help="adapter rank")
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--max-length", type=int, default=512, help="tokens per sequence")
    parser.add_argument("--max-new-tokens", type=int, default=32, help="answer length limit")
    args = parser.parse_args(argv)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="wdva-quickstart-"))
    workdir.mkdir(parents=True, exist_ok=True)
    key_path, vault_path = workdir / "notes.key", workdir / "notes.wdva"
    if key_path.exists() or vault_path.exists():
        # wdva never overwrites a key: that would make its vaults unreadable for good.
        parser.error(f"{workdir} already holds a key or a vault; choose another --workdir")

    # 1. A key is 32 random bytes. save() creates the file with owner-only permissions
    #    and refuses to overwrite an existing key. Lose the key and the vault is noise.
    key = SecretKey.generate()
    key.save(key_path)
    print(f"key   -> {key_path} (mode {key_path.stat().st_mode & 0o777:04o})")

    # 2. One engine holds one base model, loaded on first use.
    engine = WDVA(args.base_model, device=args.device)
    print(f"\n== Before training: {engine.base_model} on its own ==")
    for question in QUESTIONS:
        print(f"Q: {question}\nA: {ask(engine, question, args.max_new_tokens)}")

    # 3. Train on the notes (plain text) and the Q/A pairs, then seal. The adapter goes
    #    from memory to an encrypted file; train() does not leave it applied.
    rank, lr = args.rank, args.learning_rate
    config = TrainingConfig(
        epochs=args.epochs, rank=rank, alpha=2 * rank, learning_rate=lr, max_length=args.max_length
    )
    qa = load_examples(args.examples)  # [{"prompt": ..., "response": ...}, ...]
    print(f"\n== Training for {args.epochs} epochs (the slow part) ==")
    result = engine.train(
        vault_path, key=key, documents=[args.notes], examples=qa, config=config, on_step=progress
    )
    took = f"{result.steps} steps and {result.seconds:.0f}s"
    print(f"loss {result.loss_before:.3f} -> {result.loss_after:.3f} in {took}")
    print(f"vault -> {result.adapter_path} ({result.vault_bytes:,} bytes, encrypted)")

    # 4. Decrypt into memory, check it fits the model, apply it - for this block only.
    print("\n== With the adapter applied from the vault ==")
    with engine.adapter(vault_path, key=key) as info:
        print(f"rank {info.rank}, {info.parameters:,} parameters in {info.tensors} tensors")
        for question in QUESTIONS:
            print(f"Q: {question}")
            print(f"A (with adapter):      {ask(engine, question, args.max_new_tokens)}")
            bypassed = ask(engine, question, args.max_new_tokens, use_adapter=False)
            print(f"A (use_adapter=False): {bypassed}")

    # 5. Leaving the block zeroes the adapter weights and detaches them.
    print(f"\nafter the with-block: engine.is_loaded = {engine.is_loaded}")
    engine.close()
    key.wipe()  # best effort; the key file on disk is what opens the vault next time
    print(f"Next: wdva chat {vault_path} --key-file {key_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
