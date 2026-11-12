# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Fine-tune a LoRA/DoRA adapter and seal it straight into a vault.

The training loop is deliberately small and self-contained. It writes **no
checkpoints and no plaintext adapter files**: the trained weights go from GPU or
CPU memory, through serialisation and encryption in RAM, to a single encrypted
file on disk.

Two kinds of training data are supported and can be mixed:

* ``documents`` / ``texts`` - plain text, learned with a causal-LM objective.
  Good for teaching vocabulary, style and (with enough passes) facts.
* ``examples`` - prompt/response pairs. Only the response is learned; prompts
  are formatted with the model's chat template exactly as at inference time.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import math
import os
import random
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wdva import _ml, vault
from wdva._version import __version__
from wdva.bundle import AdapterBundle
from wdva.documents import load_documents
from wdva.errors import TrainingError
from wdva.keys import KeyLike

__all__ = ["TrainingConfig", "TrainingResult", "TrainingWarning", "train_adapter"]

_IGNORE = -100
_EVAL_SEQUENCES = 32
_FEW_STEPS = 20


class TrainingWarning(UserWarning):
    """Training will run, but the result is unlikely to be what you want."""


StepCallback = Callable[[int, int, float], None]


@dataclass(frozen=True)
class TrainingConfig:
    """Hyper-parameters for :func:`train_adapter`.

    The defaults suit small instruction-tuned models (0.1B-2B parameters) and a
    handful of documents. They are a starting point, not a guarantee of quality.
    """

    rank: int = 8
    """LoRA rank ``r``. Higher means more capacity and a larger adapter."""
    alpha: int = 16
    """LoRA scaling numerator. The update is scaled by ``alpha / rank``."""
    dropout: float = 0.05
    use_dora: bool = True
    """Train a DoRA adapter (weight-decomposed LoRA). ``False`` gives plain LoRA."""
    target_modules: str | Sequence[str] | None = "all-linear"
    """Modules to adapt. ``"all-linear"`` adapts every linear layer except the
    output head, which recalls facts far better than attention-only adapters. A
    list such as ``["q_proj", "v_proj"]`` gives a smaller adapter. ``None`` uses
    PEFT's default for the architecture, or ``"all-linear"`` when it has none."""
    epochs: int = 3
    """Passes over the data. A handful of short documents needs many more (20-40)
    before the adapter reliably recalls them; watch ``loss_after``."""
    max_steps: int | None = None
    """Stop after this many optimizer steps, even if ``epochs`` is not reached."""
    batch_size: int = 4
    learning_rate: float = 1e-3
    weight_decay: float = 0.0
    warmup_ratio: float = 0.05
    max_grad_norm: float = 1.0
    max_length: int = 512
    """Maximum sequence length in tokens."""
    overlap: int | None = None
    """Tokens shared between consecutive chunks of a document. ``None`` picks
    ``min(32, max_length // 8)``."""
    seed: int | None = 0
    """Seeds Python and torch RNGs for repeatable runs. ``None`` leaves them alone."""

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("rank must be >= 1")
        if self.alpha < 1:
            raise ValueError("alpha must be >= 1")
        if not 0.0 <= self.dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        if self.epochs < 1:
            raise ValueError("epochs must be >= 1")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if not (math.isfinite(self.learning_rate) and self.learning_rate > 0):
            raise ValueError("learning_rate must be a finite number > 0")
        if not 0.0 <= self.warmup_ratio < 1.0:
            raise ValueError("warmup_ratio must be in [0, 1)")
        if self.max_length < 8:
            raise ValueError("max_length must be >= 8")
        if self.overlap is not None and not 0 <= self.overlap < self.max_length // 2:
            raise ValueError("overlap must be >= 0 and less than half of max_length")

    @property
    def resolved_overlap(self) -> int:
        """The chunk overlap actually used."""
        return min(32, self.max_length // 8) if self.overlap is None else self.overlap


@dataclass(frozen=True)
class TrainingResult:
    """What :func:`train_adapter` did. Contains no key material."""

    adapter_path: Path
    base_model: str
    steps: int
    num_sequences: int
    num_tokens: int
    loss_before: float
    """Mean training-set loss before the first update."""
    loss_after: float
    """Mean training-set loss after the last update."""
    trainable_parameters: int
    vault_bytes: int
    seconds: float


# ---------------------------------------------------------------------------
# Building training sequences
# ---------------------------------------------------------------------------

Sequence_ = tuple[list[int], list[int]]  # (input_ids, labels)


def _document_sequences(
    tokenizer: Any, text: str, *, max_length: int, overlap: int
) -> list[Sequence_]:
    """Split one document into overlapping token windows for causal-LM training.

    The document is tokenised exactly the way a raw prompt is at inference time
    (the tokenizer decides whether to add a BOS token), so what the adapter
    learns lines up with what :meth:`WDVA.generate` later feeds the model. An
    EOS token marks the end of the document. In every window after the first,
    the leading ``overlap`` tokens are context only: each token is a training
    target exactly once.
    """
    ids = _ml.encode_text(tokenizer, text, templated=False)
    eos = tokenizer.eos_token_id
    if eos is not None and (not ids or ids[-1] != int(eos)):
        ids.append(int(eos))
    stride = max_length - overlap
    out: list[Sequence_] = []
    start = 0
    while True:
        window = ids[start : start + max_length]
        labels = list(window)
        if start > 0:
            labels[:overlap] = [_IGNORE] * min(overlap, len(labels))
        if len(window) >= 2:
            out.append((window, labels))
        if start + max_length >= len(ids):
            break
        start += stride
    return out


def _example_sequence(tokenizer: Any, example: Mapping[str, Any], *, max_length: int) -> Sequence_:
    """Tokenise one prompt/response example; only the response contributes to the loss."""
    if "messages" in example:
        messages = list(example["messages"])
        if len(messages) < 2 or messages[-1].get("role") != "assistant":
            raise TrainingError("an example's `messages` must end with an assistant turn")
        context: str | list[Mapping[str, str]] = messages[:-1]
        response = messages[-1].get("content")
        system = None
    elif "prompt" in example and "response" in example:
        context = example["prompt"]
        response = example["response"]
        system = example.get("system")
    else:
        raise TrainingError(
            'each example needs either "prompt" and "response", or "messages"; '
            f"got keys {sorted(example)}"
        )
    if not isinstance(response, str) or not response.strip():
        raise TrainingError("an example's response must be a non-empty string")
    if isinstance(context, str) and not context.strip():
        raise TrainingError("an example's prompt must be a non-empty string")

    prompt_text, templated = _ml.format_prompt(tokenizer, context, system=system)
    prompt_ids = _ml.encode_text(tokenizer, prompt_text, templated=templated)

    response_text: str | None = None
    if templated:
        # Let the template decide how an assistant turn ends (</s>, <|im_end|>, ...).
        turns = [{"role": "system", "content": system}] if system else []
        if isinstance(context, str):
            turns.append({"role": "user", "content": context})
        else:
            turns.extend({"role": m["role"], "content": m["content"]} for m in context)
        turns.append({"role": "assistant", "content": response})
        full_text = str(
            tokenizer.apply_chat_template(turns, tokenize=False, add_generation_prompt=False)
        )
        if full_text.startswith(prompt_text):
            response_text = full_text[len(prompt_text) :]
    if response_text is not None:
        response_ids = _ml.encode_text(tokenizer, response_text, templated=True)
    else:
        # No template (or one we cannot split): "prompt␠response<eos>".
        lead = "" if templated else " "
        response_ids = _ml.encode_text(tokenizer, lead + response, templated=True)
        if tokenizer.eos_token_id is not None:
            response_ids.append(int(tokenizer.eos_token_id))

    if not response_ids:
        raise TrainingError("an example's response tokenised to nothing")
    if len(prompt_ids) >= max_length:
        raise TrainingError(
            f"an example's prompt is {len(prompt_ids)} tokens, which leaves no room for the "
            f"response within max_length={max_length}"
        )
    ids = (prompt_ids + response_ids)[:max_length]
    labels = ([_IGNORE] * len(prompt_ids) + response_ids)[:max_length]
    return ids, labels


def build_sequences(
    tokenizer: Any,
    *,
    texts: Sequence[str] = (),
    examples: Sequence[Mapping[str, Any]] = (),
    max_length: int,
    overlap: int,
) -> list[Sequence_]:
    """Turn raw texts and prompt/response examples into ``(input_ids, labels)`` pairs."""
    sequences: list[Sequence_] = []
    for text in texts:
        sequences.extend(
            _document_sequences(tokenizer, text, max_length=max_length, overlap=overlap)
        )
    for example in examples:
        sequences.append(_example_sequence(tokenizer, example, max_length=max_length))
    return sequences


def _num_targets(labels: list[int]) -> int:
    # The model predicts token i+1 from position i, so the first label is never a target.
    return sum(1 for label in labels[1:] if label != _IGNORE)


def _collate(batch: Sequence[Sequence_], pad_id: int, device: str) -> dict[str, Any]:
    import torch

    width = max(len(ids) for ids, _ in batch)
    input_ids = torch.full((len(batch), width), pad_id, dtype=torch.long)
    attention = torch.zeros((len(batch), width), dtype=torch.long)
    labels = torch.full((len(batch), width), _IGNORE, dtype=torch.long)
    for row, (ids, labs) in enumerate(batch):
        input_ids[row, : len(ids)] = torch.tensor(ids, dtype=torch.long)
        attention[row, : len(ids)] = 1
        labels[row, : len(labs)] = torch.tensor(labs, dtype=torch.long)
    return {
        "input_ids": input_ids.to(device),
        "attention_mask": attention.to(device),
        "labels": labels.to(device),
    }


def _mean_loss(
    model: Any, sequences: Sequence[Sequence_], *, batch_size: int, pad_id: int, device: str
) -> float:
    """Token-weighted mean loss over ``sequences`` with dropout disabled."""
    import torch

    was_training = model.training
    model.eval()
    total = 0.0
    count = 0
    try:
        with torch.no_grad():
            for start in range(0, len(sequences), batch_size):
                batch = sequences[start : start + batch_size]
                targets = sum(_num_targets(labs) for _, labs in batch)
                loss = model(**_collate(batch, pad_id, device)).loss
                total += float(loss.detach()) * targets
                count += targets
    finally:
        model.train(was_training)
    return total / max(count, 1)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def train_adapter(
    base_model: str | tuple[Any, Any],
    output_path: str | os.PathLike[str],
    *,
    key: KeyLike | None = None,
    passphrase: str | None = None,
    documents: Sequence[str | os.PathLike[str]] = (),
    texts: Sequence[str] = (),
    examples: Sequence[Mapping[str, Any]] = (),
    config: TrainingConfig | None = None,
    device: str | None = None,
    local_files_only: bool = False,
    overwrite: bool = False,
    public_metadata: Mapping[str, Any] | None = None,
    on_step: StepCallback | None = None,
    base_model_name: str | None = None,
) -> TrainingResult:
    """Fine-tune an adapter on your data and write it to ``output_path`` as a vault.

    Args:
        base_model: A Hugging Face model id or local path, or an already loaded
            ``(model, tokenizer)`` pair. A loaded model is returned to its
            original state (adapter removed, flags restored) afterwards.
        output_path: Where to write the encrypted adapter (``.wdva``).
        key, passphrase: Exactly one. What the vault is sealed with.
        documents: Paths of text or PDF files to learn from.
        texts: Raw strings to learn from.
        examples: Prompt/response pairs; see :func:`wdva.documents.load_examples`.
        config: Hyper-parameters. Defaults to :class:`TrainingConfig()`.
        device: ``"cuda"``, ``"mps"``, ``"cpu"``; chosen automatically when omitted.
        local_files_only: Never contact the Hugging Face Hub for the base model.
        overwrite: Replace ``output_path`` if it exists.
        public_metadata: Stored **unencrypted** in the vault header. Optional.
        on_step: Called as ``on_step(step, total_steps, loss)`` after each update.
        base_model_name: Name to record when ``base_model`` is a loaded pair.

    The key is never stored, logged or returned. Losing it means losing the adapter.
    """
    _ml.require_ml()
    import torch
    from peft import LoraConfig, get_peft_model

    # Fail fast on anything that would otherwise only surface after training.
    vault._check_credentials(key, passphrase)
    target = Path(output_path)
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists (pass overwrite=True to replace it)")
    cfg = config or TrainingConfig()
    if not (documents or texts or examples):
        raise TrainingError("nothing to train on: pass documents, texts or examples")

    started = time.monotonic()
    loaded_docs = load_documents(documents)
    all_texts = [doc.text for doc in loaded_docs] + [t for t in texts if t and t.strip()]

    if cfg.seed is not None:
        random.seed(cfg.seed)
        torch.manual_seed(cfg.seed)

    owns_model = isinstance(base_model, str)
    if isinstance(base_model, str):
        device = _ml.resolve_device(device)
        # Full precision keeps small-step optimisation numerically stable everywhere.
        model, tokenizer = _ml.load_base_model(
            base_model,
            device=device,
            dtype=torch.float32,
            local_files_only=local_files_only,
        )
        model_name = base_model_name or base_model
    else:
        model, tokenizer = base_model
        device = str(next(model.parameters()).device)
        model_name = base_model_name or str(getattr(model.config, "_name_or_path", "") or "")
        if not model_name:
            raise TrainingError("pass base_model_name when training on an already loaded model")

    sequences = build_sequences(
        tokenizer,
        texts=all_texts,
        examples=examples,
        max_length=cfg.max_length,
        overlap=cfg.resolved_overlap,
    )
    sequences = [seq for seq in sequences if _num_targets(seq[1]) > 0]
    if not sequences:
        raise TrainingError("the training data produced no usable token sequences")
    num_tokens = sum(len(ids) for ids, _ in sequences)

    was_training = model.training
    grad_flags = {name: param.requires_grad for name, param in model.named_parameters()}

    target_modules: str | list[str] | None
    if cfg.target_modules is None or isinstance(cfg.target_modules, str):
        target_modules = cfg.target_modules
    else:
        target_modules = list(cfg.target_modules)
    lora_kwargs: dict[str, Any] = {
        "r": cfg.rank,
        "lora_alpha": cfg.alpha,
        "lora_dropout": cfg.dropout,
        "use_dora": cfg.use_dora,
        "bias": "none",
        "task_type": "CAUSAL_LM",
        "fan_in_fan_out": _ml.has_conv1d(model),
        "base_model_name_or_path": model_name,
    }
    try:
        peft_model = get_peft_model(model, LoraConfig(target_modules=target_modules, **lora_kwargs))
    except ValueError:
        if cfg.target_modules is not None:
            raise
        # PEFT has no default target modules for this architecture.
        peft_model = get_peft_model(model, LoraConfig(target_modules="all-linear", **lora_kwargs))

    try:
        params = [p for p in peft_model.parameters() if p.requires_grad]
        trainable = sum(p.numel() for p in params)
        if trainable == 0:
            raise TrainingError("the adapter has no trainable parameters")

        pad_id = _ml.pad_token_id(tokenizer)
        steps_per_epoch = math.ceil(len(sequences) / cfg.batch_size)
        total_steps = steps_per_epoch * cfg.epochs
        if cfg.max_steps is not None:
            total_steps = min(total_steps, cfg.max_steps)
        warmup_steps = int(total_steps * cfg.warmup_ratio)
        if total_steps < _FEW_STEPS:
            warnings.warn(
                f"only {total_steps} optimizer steps for {len(sequences)} sequences: the "
                "adapter will barely differ from the base model. Raise `epochs` "
                "(small document sets often need 20-40).",
                TrainingWarning,
                stacklevel=2,
            )

        def lr_scale(step: int) -> float:
            if step < warmup_steps:
                return (step + 1) / (warmup_steps + 1)
            return max(0.0, (total_steps - step) / max(1, total_steps - warmup_steps))

        optimizer = torch.optim.AdamW(params, lr=cfg.learning_rate, weight_decay=cfg.weight_decay)
        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_scale)

        eval_subset = sequences[:_EVAL_SEQUENCES]
        loss_before = _mean_loss(
            peft_model, eval_subset, batch_size=cfg.batch_size, pad_id=pad_id, device=device
        )

        shuffler = random.Random(cfg.seed)  # noqa: S311 - shuffling order, not security
        peft_model.train()
        step = 0
        while step < total_steps:
            order = list(range(len(sequences)))
            shuffler.shuffle(order)
            for start in range(0, len(order), cfg.batch_size):
                if step >= total_steps:
                    break
                batch = [sequences[i] for i in order[start : start + cfg.batch_size]]
                loss = peft_model(**_collate(batch, pad_id, device)).loss
                if not torch.isfinite(loss):
                    raise TrainingError(
                        f"training diverged at step {step + 1} (loss={float(loss.detach())}); "
                        "try a lower learning_rate"
                    )
                optimizer.zero_grad(set_to_none=True)
                loss.backward()
                if cfg.max_grad_norm > 0:
                    torch.nn.utils.clip_grad_norm_(params, cfg.max_grad_norm)
                optimizer.step()
                scheduler.step()
                step += 1
                if on_step is not None:
                    on_step(step, total_steps, float(loss.detach()))

        loss_after = _mean_loss(
            peft_model, eval_subset, batch_size=cfg.batch_size, pad_id=pad_id, device=device
        )

        adapter_config = _ml.to_jsonable(peft_model.peft_config["default"].to_dict())
        adapter_config["inference_mode"] = True
        adapter_config["base_model_name_or_path"] = model_name
        bundle = AdapterBundle(
            adapter_config=adapter_config,
            weights=_ml.adapter_state_bytes(peft_model),
            info={
                "base_model": model_name,
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                "wdva_version": __version__,
                "training": {
                    "steps": step,
                    "sequences": len(sequences),
                    "tokens": num_tokens,
                    "loss_before": loss_before,
                    "loss_after": loss_after,
                    "documents": [doc.name for doc in loaded_docs],
                    "num_texts": len(texts),
                    "num_examples": len(examples),
                    "config": _ml.to_jsonable(dataclasses.asdict(cfg)),
                },
            },
        )
        bundle.save(
            target,
            key=key,
            passphrase=passphrase,
            public_metadata=public_metadata,
            overwrite=overwrite,
        )
    finally:
        # Zero the plaintext adapter and hand the base model back as we found it.
        restored = _ml.remove_adapter(peft_model)
        if not owns_model:
            for name, param in restored.named_parameters():
                param.requires_grad_(grad_flags.get(name, param.requires_grad))
            restored.train(was_training)

    return TrainingResult(
        adapter_path=target,
        base_model=model_name,
        steps=step,
        num_sequences=len(sequences),
        num_tokens=num_tokens,
        loss_before=loss_before,
        loss_after=loss_after,
        trainable_parameters=trainable,
        vault_bytes=target.stat().st_size,
        seconds=time.monotonic() - started,
    )
