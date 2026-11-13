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
from wdva._util import write_temp
from wdva._version import __version__
from wdva.bundle import AdapterBundle
from wdva.documents import load_documents, validate_example
from wdva.errors import TrainingError, WDVAWarning
from wdva.keys import KeyLike

__all__ = ["TrainingConfig", "TrainingResult", "TrainingWarning", "train_adapter"]

_IGNORE = -100
_LOSS_SAMPLE = 256
"""At most this many sequences are used to measure the loss before and after."""
_FEW_STEPS = 20


class TrainingWarning(WDVAWarning):
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
    before the adapter reliably recalls them; watch ``loss_after``, and test
    recall with real questions - a low loss is necessary, not sufficient."""
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
    """Tokens shared between consecutive chunks of a document, at least 1.
    ``None`` picks ``min(32, max_length // 8)``."""
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
        # At least one token of overlap: a model never predicts the first token of a
        # window, so without it one token per window boundary would never be learned.
        if self.overlap is not None and not 1 <= self.overlap < self.max_length // 2:
            raise ValueError("overlap must be >= 1 and less than half of max_length")

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
    """Token-weighted mean loss on the training sequences before the first update.

    Measured on all of them when there are at most 256, otherwise on a fixed
    random sample of 256 drawn across documents and examples (see
    ``loss_sequences``). It says how well the adapter fits its training data,
    not how well it answers new questions.
    """
    loss_after: float
    """The same measurement, on the same sequences, after the last update."""
    loss_sequences: int
    """How many sequences ``loss_before`` and ``loss_after`` were measured on."""
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


def _example_sequence(
    tokenizer: Any, example: Mapping[str, Any], *, max_length: int, index: int = 0
) -> Sequence_:
    """Tokenise one prompt/response example; only the response contributes to the loss."""
    try:
        validate_example(example)
    except ValueError as exc:
        raise TrainingError(f"example {index + 1}: {exc}") from None
    context: str | list[Mapping[str, str]]
    if "messages" in example:
        messages = list(example["messages"])
        context = messages[:-1]
        response = messages[-1]["content"]
        system = None
    else:
        context = example["prompt"]
        response = example["response"]
        system = example.get("system")

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
            f"example {index + 1}: the prompt is {len(prompt_ids)} tokens, which leaves no room "
            f"for the response within max_length={max_length}"
        )
    total = len(prompt_ids) + len(response_ids)
    if total > max_length:
        # Cutting it would also cut the end-of-turn token, and the adapter would
        # learn to never stop. Say so instead.
        raise TrainingError(
            f"example {index + 1} is {total} tokens ({len(prompt_ids)} prompt + "
            f"{len(response_ids)} response), longer than max_length={max_length}; "
            "raise max_length or shorten the example"
        )
    return prompt_ids + response_ids, [_IGNORE] * len(prompt_ids) + response_ids


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
    for index, example in enumerate(examples):
        sequences.append(_example_sequence(tokenizer, example, max_length=max_length, index=index))
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
    base_model: str | os.PathLike[str] | tuple[Any, Any],
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
    from peft import LoraConfig

    # Fail fast on anything that would otherwise only surface after training.
    source: str | tuple[Any, Any]
    if isinstance(base_model, (tuple, list)) and len(base_model) == 2:
        source = (base_model[0], base_model[1])  # an already loaded (model, tokenizer)
    else:
        source = _ml.model_name(base_model)
    vault._check_credentials(key, passphrase)
    target = Path(output_path)
    if not overwrite and (target.exists() or target.is_symlink()):
        raise FileExistsError(f"{target} already exists (pass overwrite=True to replace it)")
    # The adapter exists only in memory until it is sealed, so find out now, not
    # after hours of training, whether the vault can be written where it was asked for.
    os.unlink(write_temp(target, b""))
    cfg = config or TrainingConfig()
    if not (documents or texts or examples):
        raise TrainingError("nothing to train on: pass documents, texts or examples")

    started = time.monotonic()
    loaded_docs = load_documents(documents)
    all_texts = [doc.text for doc in loaded_docs] + [t for t in texts if t and t.strip()]

    if cfg.seed is not None:
        random.seed(cfg.seed)
        torch.manual_seed(cfg.seed)

    owns_model = isinstance(source, str)
    if isinstance(source, str):
        device = _ml.resolve_device(device)
        # Full precision keeps small-step optimisation numerically stable everywhere.
        model, tokenizer = _ml.load_base_model(
            source,
            device=device,
            dtype=torch.float32,
            local_files_only=local_files_only,
        )
        model_name = base_model_name or source
    else:
        model, tokenizer = source
        device = str(next(model.parameters()).device)
        model_name = base_model_name or str(getattr(model.config, "_name_or_path", "") or "")
        if not model_name:
            raise TrainingError("pass base_model_name when training on an already loaded model")

    max_length = cfg.max_length
    window = _ml.context_window(model)
    if window is not None and max_length > window:
        warnings.warn(
            f"max_length={max_length} is more than this model's context window ({window}); "
            f"using {window}",
            TrainingWarning,
            stacklevel=2,
        )
        max_length = window
    sequences = build_sequences(
        tokenizer,
        texts=all_texts,
        examples=examples,
        max_length=max_length,
        overlap=min(cfg.resolved_overlap, max(1, max_length // 2 - 1)),
    )
    sequences = [seq for seq in sequences if _num_targets(seq[1]) > 0]
    if not sequences:
        raise TrainingError("the training data produced no usable token sequences")
    num_tokens = sum(len(ids) for ids, _ in sequences)

    was_training = model.training

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
        peft_model = _ml.inject(model, LoraConfig(target_modules=target_modules, **lora_kwargs))
    except (ValueError, TypeError, RuntimeError) as exc:
        if cfg.target_modules is not None:
            # inject() has already put the model back the way it was.
            raise TrainingError(
                f"cannot attach an adapter with target_modules={cfg.target_modules!r}: {exc}"
            ) from exc
        # PEFT has no default target modules for this architecture.
        peft_model = _ml.inject(model, LoraConfig(target_modules="all-linear", **lora_kwargs))

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

        # Measure on everything when that is cheap, else on a fixed sample drawn
        # across documents and examples - never just "the first few sequences".
        if len(sequences) <= _LOSS_SAMPLE:
            eval_subset = sequences
        else:
            sampler = random.Random(cfg.seed if cfg.seed is not None else 0)  # noqa: S311
            eval_subset = [
                sequences[i] for i in sorted(sampler.sample(range(len(sequences)), _LOSS_SAMPLE))
            ]
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
        if not math.isfinite(loss_after):
            # The last update is not followed by another forward pass in the loop.
            raise TrainingError(
                f"training diverged on its final step (loss={loss_after}); "
                "try a lower learning_rate"
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
                    "loss_sequences": len(eval_subset),
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
        # Zero the plaintext adapter and hand the base model back as we found it
        # (remove_adapter also restores the requires_grad flags).
        restored = _ml.remove_adapter(peft_model)
        if not owns_model:
            restored.train(was_training)

    return TrainingResult(
        adapter_path=target,
        base_model=model_name,
        steps=step,
        num_sequences=len(sequences),
        num_tokens=num_tokens,
        loss_before=loss_before,
        loss_after=loss_after,
        loss_sequences=len(eval_subset),
        trainable_parameters=trainable,
        vault_bytes=target.stat().st_size,
        seconds=time.monotonic() - started,
    )
