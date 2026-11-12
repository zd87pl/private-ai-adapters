# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""ML plumbing shared by training and inference.

torch, transformers, peft and safetensors are imported lazily inside functions
so that importing :mod:`wdva` (and running ``wdva keygen`` or ``wdva inspect``)
never pays for, or requires, the ML stack.
"""

from __future__ import annotations

import enum
import importlib
import importlib.util
from collections.abc import Mapping, Sequence
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from wdva.bundle import AdapterBundle
from wdva.errors import AdapterMismatchError, MissingDependencyError, WDVAError

if TYPE_CHECKING:
    import torch

Message = Mapping[str, str]
Prompt = "str | Sequence[Message]"

_ML_MODULES = ("torch", "transformers", "peft", "safetensors")
_SUPPORTED_PEFT_TYPES = ("LORA",)


def require_ml() -> None:
    """Raise a helpful error unless the ``ml`` extra is installed."""
    missing = [name for name in _ML_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        raise MissingDependencyError(
            f"this needs the ML stack (missing: {', '.join(missing)}). "
            "Install it with:  pip install 'wdva[ml]'"
        )


# ---------------------------------------------------------------------------
# Devices and base models
# ---------------------------------------------------------------------------


def resolve_device(device: str | None) -> str:
    """Pick ``cuda``, then ``mps``, then ``cpu`` unless the caller chose one."""
    import torch

    if device not in (None, "auto"):
        assert device is not None
        return device
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def resolve_dtype(dtype: str | torch.dtype | None, device: str) -> torch.dtype:
    """Half precision on CUDA, full precision elsewhere, unless the caller chose one."""
    import torch

    if dtype is None or dtype == "auto":
        return torch.float16 if device.startswith("cuda") else torch.float32
    if isinstance(dtype, str):
        resolved = getattr(torch, dtype, None)
        if not isinstance(resolved, torch.dtype):
            raise ValueError(f"unknown dtype {dtype!r}")
        return resolved
    return dtype


def load_base_model(
    name_or_path: str,
    *,
    device: str,
    dtype: torch.dtype,
    local_files_only: bool = False,
    revision: str | None = None,
) -> tuple[Any, Any]:
    """Load a causal LM and its tokenizer. Never enables ``trust_remote_code``."""
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        name_or_path, local_files_only=local_files_only, revision=revision
    )
    model = AutoModelForCausalLM.from_pretrained(
        name_or_path, dtype=dtype, local_files_only=local_files_only, revision=revision
    )
    model.to(device)
    model.eval()
    return model, tokenizer


def pad_token_id(tokenizer: Any) -> int:
    """A usable padding id that never requires resizing the embedding matrix."""
    for candidate in (tokenizer.pad_token_id, tokenizer.eos_token_id, tokenizer.bos_token_id):
        if candidate is not None:
            return int(candidate)
    return 0


# ---------------------------------------------------------------------------
# Prompt formatting (identical at training and inference time)
# ---------------------------------------------------------------------------


def has_chat_template(tokenizer: Any) -> bool:
    return bool(getattr(tokenizer, "chat_template", None))


def format_prompt(
    tokenizer: Any,
    prompt: str | Sequence[Message],
    *,
    system: str | None = None,
    raw: bool = False,
) -> tuple[str, bool]:
    """Turn a prompt into model input text.

    Returns ``(text, templated)``. When ``templated`` is true the text already
    carries the model's special tokens and must be tokenised without adding more.

    * A plain string is wrapped as a single user turn using the tokenizer's chat
      template, if it has one. ``raw=True`` skips that and passes it through.
    * A list of ``{"role": ..., "content": ...}`` messages is rendered with the
      chat template, or with a simple ``role: content`` transcript when the
      tokenizer has none.
    """
    if isinstance(prompt, str):
        if raw or not has_chat_template(tokenizer):
            return prompt, False
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
    else:
        messages = [_check_message(m) for m in prompt]
        if not messages:
            raise ValueError("messages must not be empty")
        if system:
            messages.insert(0, {"role": "system", "content": system})
        if not has_chat_template(tokenizer):
            transcript = "".join(f"{m['role']}: {m['content']}\n" for m in messages)
            return transcript + "assistant:", False
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return str(text), True


def _check_message(message: Message) -> dict[str, str]:
    if (
        not isinstance(message, Mapping)
        or not isinstance(message.get("role"), str)
        or not isinstance(message.get("content"), str)
    ):
        raise TypeError('each message must look like {"role": "user", "content": "..."}')
    return {"role": message["role"], "content": message["content"]}


def encode_text(tokenizer: Any, text: str, *, templated: bool) -> list[int]:
    """Tokenise prompt text. Templated text already contains its special tokens."""
    ids = tokenizer(text, add_special_tokens=not templated)["input_ids"]
    return [int(i) for i in ids]


# ---------------------------------------------------------------------------
# PEFT config (de)serialisation
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Convert a PEFT config dict into plain JSON types."""
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(to_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, PurePath):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__} in adapter config")


def build_inference_config(adapter_config: Mapping[str, Any]) -> Any:
    """Rebuild a PEFT config for *loading* weights.

    Only structural settings matter here. Anything that would make PEFT run an
    initialisation routine or import extra modules is neutralised, because the
    weights come from the vault.
    """
    from peft import PeftConfig

    cfg = dict(adapter_config)
    peft_type = str(to_jsonable(cfg.get("peft_type", ""))).upper()
    if peft_type not in _SUPPORTED_PEFT_TYPES:
        raise AdapterMismatchError(
            f"unsupported adapter type {peft_type or '(none)'!r}; "
            "wdva currently loads LoRA and DoRA adapters"
        )
    cfg["peft_type"] = peft_type
    cfg["inference_mode"] = True
    cfg["init_lora_weights"] = True
    if "megatron_config" in cfg:
        cfg["megatron_config"] = None
    return PeftConfig.from_peft_type(**cfg)


# ---------------------------------------------------------------------------
# Applying and removing adapters, entirely in memory
# ---------------------------------------------------------------------------


def inject(model: Any, config: Any) -> Any:
    """Wrap ``model`` with freshly initialised adapter layers for ``config``.

    WDVA keeps exactly one adapter on an otherwise bare base model, so a model
    that already carries PEFT adapters is refused rather than stacked upon.
    """
    from peft import PeftModel, get_peft_model

    if isinstance(model, PeftModel) or "peft_config" in vars(model):
        raise WDVAError(
            "the base model already has a PEFT adapter attached; "
            "wdva needs the bare base model (unload or merge the existing adapter first)"
        )
    return get_peft_model(model, config)


def apply_adapter(model: Any, bundle: AdapterBundle) -> Any:
    """Inject ``bundle`` into ``model`` and return the resulting ``PeftModel``.

    The adapter is rebuilt from the decrypted bytes in RAM; nothing is written
    to disk. Every adapter tensor must be present with the right shape, and no
    extra tensors are tolerated. On any mismatch the base model is restored and
    :class:`~wdva.errors.AdapterMismatchError` is raised, so an adapter is
    never "half applied" or silently ignored.
    """
    from peft import set_peft_model_state_dict
    from safetensors.torch import load as safetensors_load

    config = build_inference_config(bundle.adapter_config)
    # The engine already compares base-model names and warns; stop PEFT repeating it.
    config.base_model_name_or_path = None
    state = safetensors_load(bytes(bundle.weights))

    try:
        peft_model = inject(model, config)
    except ValueError as exc:
        raise AdapterMismatchError(f"adapter does not fit this base model: {exc}") from exc

    try:
        expected = adapter_state(peft_model)
        missing = sorted(set(expected) - set(state))
        unexpected = sorted(set(state) - set(expected))
        if missing or unexpected:
            raise AdapterMismatchError(
                "adapter does not fit this base model "
                f"({len(missing)} tensors missing, {len(unexpected)} unexpected; "
                f"e.g. {(missing or unexpected)[0]!r})"
            )
        for name, tensor in expected.items():
            if tuple(state[name].shape) != tuple(tensor.shape):
                raise AdapterMismatchError(
                    f"adapter tensor {name!r} has shape {tuple(state[name].shape)}, "
                    f"but this base model needs {tuple(tensor.shape)}"
                )
        result = set_peft_model_state_dict(peft_model, state)
        leftover = list(getattr(result, "unexpected_keys", None) or [])
        if leftover:
            raise AdapterMismatchError(f"adapter has tensors the model did not accept: {leftover}")
    except BaseException:
        remove_adapter(peft_model)
        raise

    peft_model.eval()
    return peft_model


def remove_adapter(peft_model: Any) -> Any:
    """Overwrite the adapter weights with zeros, detach them, and return the base model.

    Zeroing happens in place on the live tensors, so the plaintext adapter does
    not linger in freed memory waiting to be reused. This is best effort: it
    cannot reach copies the framework made elsewhere (for example on a GPU
    allocator's free list, or pages the OS swapped out).
    """
    import torch

    prefix = str(getattr(peft_model.base_model, "prefix", "lora_"))
    with torch.no_grad():
        for name, param in peft_model.named_parameters():
            if prefix in name:
                param.zero_()
    base = peft_model.unload()
    # Older PEFT releases leave the adapter's configuration attached to the base
    # model. It describes the adapter that was just removed, so it goes too.
    vars(base).pop("peft_config", None)
    return base


def adapter_state(peft_model: Any) -> dict[str, Any]:
    """The adapter's tensors, keyed the way PEFT names them on disk.

    ``save_embedding_layers=False`` matters. With PEFT's default (``"auto"``) this
    call tries to work out whether the vocabulary was resized by fetching the base
    model's config - a request to the Hugging Face Hub on every seal and every
    load. WDVA neither resizes nor trains embeddings, so the question never needs
    asking, and an adapter that does carry embedding weights is rejected at load
    time as having unexpected tensors rather than being half-applied.
    """
    from peft import get_peft_model_state_dict

    return dict(get_peft_model_state_dict(peft_model, save_embedding_layers=False))


def adapter_state_bytes(peft_model: Any) -> bytes:
    """Serialise the adapter weights to safetensors bytes, in memory."""
    from safetensors.torch import save as safetensors_save

    state = {
        name: tensor.detach().to("cpu").contiguous()
        for name, tensor in adapter_state(peft_model).items()
    }
    return bytes(safetensors_save(state, metadata={"format": "pt"}))


def has_conv1d(model: Any) -> bool:
    """GPT-2 style models use ``Conv1D`` projections, which LoRA must treat as transposed."""
    from transformers.pytorch_utils import Conv1D

    return any(isinstance(module, Conv1D) for module in model.modules())
