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
import os
from collections.abc import Mapping, Sequence
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

from wdva.bundle import AdapterBundle
from wdva.errors import (
    AdapterMismatchError,
    MissingDependencyError,
    VaultFormatError,
    WDVAError,
)

if TYPE_CHECKING:
    import torch

Message = Mapping[str, str]
Prompt = "str | Sequence[Message]"

_ML_MODULES = ("torch", "transformers", "peft", "safetensors")
_SUPPORTED_PEFT_TYPES = ("LORA",)
ADAPTER_PREFIX = "lora_"
"""Every tensor of a LoRA or DoRA adapter has this in its name (PEFT's own prefix)."""


def model_name(base_model: Any) -> str:
    """A base model named by a Hub id or a local directory, as the string the libraries expect."""
    if isinstance(base_model, os.PathLike):
        base_model = os.fspath(base_model)
    if not isinstance(base_model, str) or not base_model:
        raise TypeError(
            "base_model must be a model id or a path to a model directory, "
            f"not {type(base_model).__name__}"
        )
    return base_model


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

    mps = getattr(torch.backends, "mps", None)
    has_mps = mps is not None and mps.is_available()
    if device is None or device == "auto":
        if torch.cuda.is_available():
            return "cuda"
        return "mps" if has_mps else "cpu"
    # Check now, with a clear message, rather than deep inside torch after the
    # model has been downloaded and loaded.
    try:
        kind = torch.device(device).type
    except (RuntimeError, TypeError):
        raise ValueError(f"unknown device {device!r}; use 'cpu', 'cuda' or 'mps'") from None
    if kind == "cuda" and not torch.cuda.is_available():
        raise ValueError("device 'cuda' was requested, but this PyTorch has no usable CUDA device")
    if kind == "mps" and not has_mps:
        raise ValueError("device 'mps' was requested, but no Metal (MPS) device is available")
    return device


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
      template, if it has one. ``raw=True`` skips that and passes it through. A
      tokenizer without a template gets the string unchanged, or - when a system
      prompt is given - a plain ``role: content`` transcript that includes it.
    * A list of ``{"role": ..., "content": ...}`` messages is rendered with the
      chat template, or with a simple ``role: content`` transcript when the
      tokenizer has none.
    """
    if raw and system:
        raise ValueError("a system prompt cannot be combined with raw=True")
    if raw and not isinstance(prompt, str):
        raise ValueError("raw=True applies to a string prompt, not to a list of messages")
    if isinstance(prompt, str):
        if raw or (not system and not has_chat_template(tokenizer)):
            return prompt, False
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        if not has_chat_template(tokenizer):
            # No template to carry the system prompt: fall back to the same plain
            # transcript that message lists get, rather than dropping it silently.
            transcript = "".join(f"{m['role']}: {m['content']}\n" for m in messages)
            return transcript + "assistant:", False
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
    """Tokenise prompt text the way the model should see it before it continues.

    Templated text already contains its special tokens. Plain text gets whatever
    the tokenizer adds at the *start* (usually BOS) but not an end-of-sequence
    token: some tokenizers append one automatically, and a prompt that ends in
    EOS asks the model to continue after the end.
    """
    if templated:
        return [int(i) for i in tokenizer(text, add_special_tokens=False)["input_ids"]]
    ids = [int(i) for i in tokenizer(text, add_special_tokens=True)["input_ids"]]
    eos = tokenizer.eos_token_id
    if eos is not None and ids and ids[-1] == int(eos):
        plain = tokenizer(text, add_special_tokens=False)["input_ids"]
        if not len(plain) or int(plain[-1]) != int(eos):
            ids = ids[:-1]  # the tokenizer added it; the text did not contain it
    return ids


def context_window(model: Any) -> int | None:
    """How many positions the model can attend over, when its config says so."""
    config = getattr(model, "config", None)
    for attribute in ("max_position_embeddings", "n_positions"):
        value = getattr(config, attribute, None)
        if isinstance(value, int) and value > 0:
            return value
    return None


def snapshot_dtypes(model: Any) -> dict[str, Any]:
    """Record the dtype of every parameter and buffer, for :func:`restore_dtypes`."""
    dtypes = {f"p:{name}": param.dtype for name, param in model.named_parameters()}
    dtypes.update({f"b:{name}": buffer.dtype for name, buffer in model.named_buffers()})
    return dtypes


def restore_dtypes(model: Any, dtypes: Mapping[str, Any]) -> None:
    """Give each parameter and buffer back the dtype it had in the snapshot.

    Tensor by tensor, rather than ``model.to(dtype)``: a half-precision model
    deliberately keeps some buffers (rotary position frequencies, for one) in
    float32, and casting the whole module down would quietly degrade them.
    """
    for name, param in model.named_parameters():
        wanted = dtypes.get(f"p:{name}")
        if wanted is not None and param.dtype != wanted:
            param.data = param.data.to(wanted)
    for module_name, module in model.named_modules():
        for key, buffer in list(module._buffers.items()):
            if buffer is None:
                continue
            wanted = dtypes.get(f"b:{module_name}.{key}" if module_name else f"b:{key}")
            if wanted is not None and buffer.dtype != wanted:
                module._buffers[key] = buffer.to(wanted)


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


# Settings that decide where the adapter's layers go and how their output is
# scaled. Nothing else from a vault's configuration ever reaches PEFT.
_STRUCTURAL_KEYS = (
    "task_type",
    "r",
    "lora_alpha",
    "target_modules",
    "exclude_modules",
    "fan_in_fan_out",
    "use_dora",
    "use_rslora",
    "lora_bias",
    "rank_pattern",
    "alpha_pattern",
    "layers_to_transform",
    "layers_pattern",
)
# Settings that only matter while training or initialising an adapter, or that
# merely describe where it came from. They are dropped: the weights come from the vault.
_IGNORED_KEYS = frozenset(
    {
        "peft_type",
        "peft_version",
        "auto_mapping",
        "revision",
        "base_model_name_or_path",
        "inference_mode",
        "init_lora_weights",
        "lora_dropout",
        "loftq_config",
        "eva_config",
        "corda_config",
        "lora_ga_config",
        "megatron_core",
        "runtime_config",
    }
)
_HOW_TO_FIX = (
    "wdva applies adapters as pure LoRA/DoRA deltas so that the shared base model is "
    "never modified. Merge or re-export the adapter without this feature."
)


def _is_unset(value: Any) -> bool:
    return value is None or value is False or (isinstance(value, (str, list, dict)) and not value)


def build_inference_config(adapter_config: Mapping[str, Any]) -> Any:
    """Rebuild a PEFT config for *loading* weights, from an allowlist.

    A vault's configuration is not handed to PEFT as it is. Several ordinary
    PEFT options change the base model itself - ``bias`` and ``modules_to_save``
    overwrite base weights, ``layer_replication`` rebuilds the layer stack,
    some variants rewrite weights in place - and none of that is undone when the
    adapter is removed. On an engine that serves one adapter after another,
    that would silently corrupt every later answer.

    So only the structural settings in ``_STRUCTURAL_KEYS`` are used, settings
    that are irrelevant when loading are dropped, and any other setting that is
    switched on is refused with :class:`~wdva.errors.AdapterMismatchError`.
    """
    from peft import LoraConfig

    cfg = {str(key): to_jsonable(value) for key, value in adapter_config.items()}
    peft_type = str(cfg.get("peft_type") or "").upper()
    if peft_type not in _SUPPORTED_PEFT_TYPES:
        raise AdapterMismatchError(
            f"unsupported adapter type {peft_type or '(none)'!r}; "
            "wdva currently loads LoRA and DoRA adapters"
        )
    if cfg.get("bias") not in (None, "none"):
        raise AdapterMismatchError(
            f"adapter was trained with bias={cfg['bias']!r}, which overwrites bias terms of the "
            f"base model. {_HOW_TO_FIX}"
        )

    defaults = to_jsonable(LoraConfig().to_dict())
    accepted = set(defaults)
    kwargs: dict[str, Any] = {}
    for key, value in cfg.items():
        if key in _IGNORED_KEYS or key == "bias":
            continue
        if key in _STRUCTURAL_KEYS:
            if key in accepted:
                kwargs[key] = value
            elif not _is_unset(value):
                raise AdapterMismatchError(
                    f"adapter uses {key!r}, which the installed peft does not support; upgrade peft"
                )
            continue
        # Everything else must be switched off (or at the default this peft would use).
        if _is_unset(value) or (key in defaults and value == defaults[key]):
            continue
        raise AdapterMismatchError(
            f"adapter configuration sets {key!r}, which would change the base model or is not "
            f"understood by this version of wdva. {_HOW_TO_FIX}"
        )
    if _is_unset(kwargs.get("target_modules")):
        raise AdapterMismatchError("adapter configuration names no target modules")
    try:
        return LoraConfig(
            **kwargs,
            bias="none",
            lora_dropout=0.0,
            inference_mode=True,
            init_lora_weights=True,
        )
    except (TypeError, ValueError) as exc:
        raise AdapterMismatchError(f"adapter configuration is not valid: {exc}") from exc


# ---------------------------------------------------------------------------
# Applying and removing adapters, entirely in memory
# ---------------------------------------------------------------------------


def structure(model: Any) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[int, ...]], ...]]:
    """A cheap fingerprint of a model's shape: module names, parameter names and sizes.

    Compared before an adapter goes on and after it comes off. It does not look
    at values (that is what refusing base-modifying adapters is for); it catches
    layers that were added, wrapped or left behind.
    """
    return (
        tuple(name for name, _ in model.named_modules()),
        tuple((name, tuple(param.shape)) for name, param in model.named_parameters()),
    )


def _strip_adapter_layers(model: Any) -> None:
    """Put back the original module wherever PEFT wrapped one."""
    from peft.tuners.tuners_utils import BaseTunerLayer

    for parent in list(model.modules()):
        for name, child in list(parent.named_children()):
            if isinstance(child, BaseTunerLayer):
                setattr(parent, name, child.get_base_layer())
    vars(model).pop("peft_config", None)


def inject(model: Any, config: Any) -> Any:
    """Wrap ``model`` with freshly initialised adapter layers for ``config``.

    All or nothing: if PEFT fails part-way (an unsupported target module, a bad
    rank pattern), the layers it had already wrapped are unwrapped again and the
    model's ``requires_grad`` flags are restored before the error is re-raised.

    WDVA keeps exactly one adapter on an otherwise bare base model, so a model
    that already carries PEFT adapters is refused rather than stacked upon.
    """
    from peft import PeftModel, get_peft_model

    if isinstance(model, PeftModel) or "peft_config" in vars(model):
        raise WDVAError(
            "the base model already has a PEFT adapter attached; "
            "wdva needs the bare base model (unload or merge the existing adapter first)"
        )
    flags = {name: param.requires_grad for name, param in model.named_parameters()}
    try:
        peft_model = get_peft_model(model, config)
    except BaseException:
        _strip_adapter_layers(model)
        _restore_grad_flags(model, flags)
        raise
    peft_model._wdva_grad_flags = flags
    return peft_model


def _restore_grad_flags(model: Any, flags: Mapping[str, bool]) -> None:
    for name, param in model.named_parameters():
        if name in flags:
            param.requires_grad_(flags[name])


def apply_adapter(model: Any, bundle: AdapterBundle) -> Any:
    """Inject ``bundle`` into ``model`` and return the resulting ``PeftModel``.

    The adapter is rebuilt from the decrypted bytes in RAM; nothing is written
    to disk. Before the model is touched, the configuration is reduced to an
    allowlist (see :func:`build_inference_config`) and every tensor is checked
    to be an adapter tensor, so nothing in a vault can overwrite base weights.
    Then every adapter tensor must be present with the right shape, and no extra
    tensors are tolerated. On any mismatch the base model is restored and
    :class:`~wdva.errors.AdapterMismatchError` is raised: an adapter is never
    "half applied" or silently ignored.
    """
    from peft import set_peft_model_state_dict
    from safetensors.torch import load as safetensors_load

    config = build_inference_config(bundle.adapter_config)
    foreign = sorted(t.name for t in bundle.tensors() if ADAPTER_PREFIX not in t.name)
    if foreign:
        raise AdapterMismatchError(
            f"adapter carries {len(foreign)} tensor(s) that are not LoRA/DoRA weights and would "
            f"overwrite the base model (e.g. {foreign[0]!r}). {_HOW_TO_FIX}"
        )
    try:
        state = safetensors_load(bytes(bundle.weights))
    except Exception as exc:  # safetensors raises its own error type, not a ValueError
        raise VaultFormatError(f"adapter weights could not be loaded: {exc}") from exc

    try:
        peft_model = inject(model, config)
    except (ValueError, TypeError, RuntimeError) as exc:
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

    The base model gets back the ``requires_grad`` flags it had before the
    adapter went on (PEFT freezes every base parameter when it injects one).
    """
    import torch

    with torch.no_grad():
        for name, param in peft_model.named_parameters():
            if ADAPTER_PREFIX in name:
                param.zero_()
    flags = getattr(peft_model, "_wdva_grad_flags", None)
    base = peft_model.unload()
    # Belt and braces: unwrap anything unload() left, and drop the configuration
    # that older PEFT releases leave attached to the base model.
    _strip_adapter_layers(base)
    if flags is not None:
        _restore_grad_flags(base, flags)
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
