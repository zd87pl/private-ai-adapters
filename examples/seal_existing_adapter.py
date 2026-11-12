# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Seal an adapter you already have: plain PEFT in, encrypted vault out, and back.

You do not have to train with wdva to use its vaults. This script

1. makes a LoRA adapter with PEFT alone and saves it the usual way, as a plaintext
   directory (a few optimisation steps on one sentence: it only has to exist);
2. seals that directory into ``adapter.wdva`` with ``AdapterBundle.from_peft_dir``
   and deletes the plaintext copy;
3. inspects the vault: the public header without the key, the contents with it;
4. loads the vault into a ``WDVA`` engine and compares the model's output with what
   PEFT itself computed in step 1, then unloads it;
5. exports the adapter back to a PEFT directory and compares the weights byte for byte.

Run ``python examples/seal_existing_adapter.py`` (needs ``pip install 'wdva[ml]'``).
It uses the CPU in float32 on purpose, so that steps 1 and 4 share the same
arithmetic and their numbers can be compared directly. Two caveats: step 2 merely
deletes the plaintext that step 1 wrote, which is not secure erasure, and step 5 is
the deliberate way out of the vault - what it writes is plaintext again.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import torch
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer

import wdva
from wdva import WDVA, AdapterBundle, SecretKey

SENTENCE = "The lighthouse keeper logs the tide at dawn and again at dusk."
WEIGHTS = "adapter_model.safetensors"


def next_token_logits(model: Any, tokenizer: Any) -> torch.Tensor:
    """What the model predicts after SENTENCE: a fingerprint of its current weights."""
    with torch.no_grad():
        return model(**tokenizer(SENTENCE, return_tensors="pt")).logits[0, -1]


def make_peft_adapter(base_model: str, directory: Path, steps: int) -> torch.Tensor:
    """Ordinary transformers + PEFT code; wdva is not involved. Returns PEFT's logits."""
    torch.manual_seed(0)  # LoRA starts from random weights; a seed makes the printout repeatable
    tokenizer = AutoTokenizer.from_pretrained(base_model)
    base = AutoModelForCausalLM.from_pretrained(base_model, dtype=torch.float32)
    config = LoraConfig(r=8, lora_alpha=16, target_modules="all-linear", task_type="CAUSAL_LM")
    model = get_peft_model(base, config)
    batch = tokenizer(SENTENCE, return_tensors="pt")
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=1e-3)
    for _ in range(steps):
        model(**batch, labels=batch["input_ids"]).loss.backward()
        optimizer.step()
        optimizer.zero_grad()
    # Nothing was resized, so no embeddings to save (this also spares PEFT a Hub lookup).
    model.save_pretrained(directory, save_embedding_layers=False)
    return next_token_logits(model, tokenizer)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seal a plain PEFT adapter into a vault.")
    parser.add_argument("--base-model", default=wdva.DEFAULT_BASE_MODEL, help="model id or path")
    parser.add_argument("--workdir", type=Path, help="for all outputs (default: a temp dir)")
    parser.add_argument("--epochs", type=int, default=3, help="optimisation steps in step 1")
    args = parser.parse_args(argv)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="wdva-seal-existing-"))
    workdir.mkdir(parents=True, exist_ok=True)
    plain_dir, vault_path = workdir / "plain-adapter", workdir / "adapter.wdva"

    # 1. An adapter made without wdva, saved as PEFT always saves it.
    peft_logits = make_peft_adapter(args.base_model, plain_dir, args.epochs)
    original = (plain_dir / WEIGHTS).read_bytes()
    print(f"1. PEFT wrote {sorted(p.name for p in plain_dir.iterdir())}")

    # 2. Seal it. `info` is free-form and is encrypted together with the adapter.
    key = SecretKey.generate()
    key.save(workdir / "adapter.key")
    bundle = AdapterBundle.from_peft_dir(plain_dir, info={"base_model": args.base_model})
    bundle.save(vault_path, key=key)
    shutil.rmtree(plain_dir)
    print(f"2. sealed -> {vault_path} ({vault_path.stat().st_size:,} bytes); plaintext deleted")

    # 3. The header is public and unverified until the vault is opened with the key.
    print(f"3. header, no key needed: {wdva.read_header(vault_path).to_public_dict()}")
    summary = AdapterBundle.load(vault_path, key=key).summary()
    shown = {name: summary[name] for name in ("base_model", "peft_type", "rank", "parameters")}
    print(f"   contents, with the key: {shown}")

    # 4. Apply it from ciphertext. The engine must compute exactly what PEFT computed.
    engine = WDVA(args.base_model, device="cpu")
    base_logits = next_token_logits(engine.model, engine.tokenizer)
    info = engine.load(vault_path, key=key)
    vault_logits = next_token_logits(engine.model, engine.tokenizer)
    engine.unload()
    after_unload = next_token_logits(engine.model, engine.tokenizer)
    engine.close()
    effect = float((vault_logits - base_logits).abs().max())
    mismatch = float((vault_logits - peft_logits).abs().max())
    residue = float((after_unload - base_logits).abs().max())
    print(f"4. engine loaded rank {info.rank}, {info.parameters:,} parameters from the vault")
    print(f"   largest logit change caused by the adapter: {effect:.6f}")
    print(f"   largest difference from PEFT's own result:  {mismatch:.6f}")
    print(f"   largest change left after engine.unload():  {residue:.6f}")

    # 5. Back out of the vault, as files any PEFT tool can read. Plaintext again!
    exported = AdapterBundle.load(vault_path, key=key).to_peft_dir(workdir / "exported-adapter")
    identical = (exported / WEIGHTS).read_bytes() == original
    print(f"5. exported -> {exported}; weights byte-identical to step 1: {identical}")
    key.wipe()
    return 0 if identical and mismatch < 1e-3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
