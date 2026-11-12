# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Hot swap: two people, one base model, two encrypted adapters.

Alice and Bob each have a few private facts. This script

1. trains one small adapter per person on the same base model and seals each one
   under that person's own key;
2. serves a "request" from each person in turn: read their key, apply their adapter
   with ``with engine.adapter(...)``, answer, then drop the adapter and wipe the key;
3. reports (a) how many of each person's facts every adapter recalls, (b) that one
   person's key cannot open the other's vault, and (c) that no adapter is applied
   between requests.

What this does not give you is privacy from the machine doing the work. The process
that runs the model holds each person's key and plaintext adapter while it serves
them, and it sees their prompts and answers. The vault protects adapters at rest and
in transit; it is not a sandbox between the users of one process.

This script serves one request at a time, so a ``with engine.adapter(...)`` block is
fine. If several threads share an engine, another thread could swap the adapter in the
middle of such a block. There, bind the adapter to the request instead, which applies,
uses and removes it under one lock::

    engine.generate(question, adapter="alice.wdva", key=alice_key)

Run ``python examples/hot_swap.py`` (needs ``pip install 'wdva[ml]'``). It trains two
adapters, so budget twice the quickstart's training time; a second run with the same
``--workdir`` reuses the vaults and only swaps. The recall counts in (a) are whatever
the model really answers: the script reports them, it does not assert them.
"""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Sequence
from pathlib import Path

import wdva
from wdva import WDVA, SecretKey, TrainingConfig, WrongKeyError

Fact = tuple[str, str, str]  # question, the answer the adapter is taught, proof of recall
FACTS: dict[str, list[Fact]] = {
    "alice": [
        ("Where is my boat moored?", "Your boat is moored at berth 14 in Saltmarsh.", "berth 14"),
        ("What is the boat's call sign?", "The boat's call sign is Kestrel Nine.", "Kestrel Nine"),
        ("When is the engine service due?", "The engine service is due on 12 March.", "12 March"),
    ],
    "bob": [
        ("How many beehives do I keep?", "You keep eleven beehives by the orchard wall.", "eleven"),
        ("What is the gate padlock code?", "The gate padlock code is 3-8-2-6.", "3-8-2-6"),
        ("Who buys my honey?", "The Larkfield shop buys your honey every Friday.", "Larkfield"),
    ],
}


def recall(engine: WDVA, facts: list[Fact], limit: int) -> int:
    """Ask each question greedily (temperature=0), print the answers, count the proofs."""
    hits = 0
    for question, _, proof in facts:
        answer = " ".join(engine.generate(question, temperature=0, max_new_tokens=limit).split())
        print(f"  {question} -> {answer}")
        hits += proof.lower() in answer.lower()
    return hits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Two people, one base model, two vaults.")
    parser.add_argument("--base-model", default=wdva.DEFAULT_BASE_MODEL, help="model id or path")
    parser.add_argument("--device", help="cuda, mps or cpu (default: auto)")
    parser.add_argument("--workdir", type=Path, help="for keys and vaults (default: a temp dir)")
    parser.add_argument("--epochs", type=int, default=30, help="passes over each person's facts")
    parser.add_argument("--rank", type=int, default=8, help="adapter rank")
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--max-length", type=int, default=512, help="tokens per sequence")
    parser.add_argument("--max-new-tokens", type=int, default=24, help="answer length limit")
    args = parser.parse_args(argv)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="wdva-hot-swap-"))
    workdir.mkdir(parents=True, exist_ok=True)
    rank, lr = args.rank, args.learning_rate
    config = TrainingConfig(
        epochs=args.epochs, rank=rank, alpha=2 * rank, learning_rate=lr, max_length=args.max_length
    )
    engine = WDVA(args.base_model, device=args.device)  # the one base model everybody shares

    # Enrolment: each person gets their own key file and their own vault. Both kinds of
    # training data are used: plain statements (texts) and question/answer pairs.
    for user, facts in FACTS.items():
        if (workdir / f"{user}.wdva").exists():
            print(f"{user}: reusing {user}.wdva and {user}.key from an earlier run")
            continue
        with SecretKey.generate() as key:  # wiped from memory when the block ends
            key.save(workdir / f"{user}.key")
            result = engine.train(
                workdir / f"{user}.wdva",
                key=key,
                texts=[answer for _, answer, _ in facts],
                examples=[{"prompt": q, "response": a} for q, a, _ in facts],
                config=config,
            )
        change = f"loss {result.loss_before:.3f} -> {result.loss_after:.3f}"
        print(f"{user}: {change}, sealed {result.adapter_path.name} in {result.seconds:.0f}s")

    # Serving: one adapter at a time, each opened with its owner's key.
    leftovers = 0
    for user in FACTS:
        other = next(name for name in FACTS if name != user)
        print(f"\n== request from {user} ==")
        with SecretKey.from_file(workdir / f"{user}.key") as key:
            with engine.adapter(workdir / f"{user}.wdva", key=key):
                for owner, facts in FACTS.items():
                    hits = recall(engine, facts, args.max_new_tokens)
                    print(f"(a) {user}'s adapter recalled {hits}/{len(facts)} of {owner}'s facts")
            # The same key on the other vault is refused before the model is touched. If
            # it were ever accepted, that adapter would stay applied and (c) would say so.
            try:
                engine.load(workdir / f"{other}.wdva", key=key)
            except WrongKeyError as exc:
                print(f"(b) {user}'s key on {other}.wdva -> WrongKeyError: {exc}")
        print(f"(c) after the request: engine.is_loaded = {engine.is_loaded}")
        leftovers += engine.is_loaded

    engine.close()
    return 1 if leftovers else 0


if __name__ == "__main__":
    raise SystemExit(main())
