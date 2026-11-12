# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The :class:`WDVA` engine: one base model, encrypted adapters swapped in and out.

Typical use::

    from wdva import WDVA, SecretKey

    key = SecretKey.from_file("notes.key")
    engine = WDVA("HuggingFaceTB/SmolLM2-360M-Instruct")

    with engine.adapter("notes.wdva", key=key):
        print(engine.generate("What did I decide about the roof?"))
    # the adapter is zeroed and detached here; the base model is untouched

The adapter is decrypted and injected in memory. It is never written to disk in
plaintext, and loading fails loudly if the adapter does not fit the model.
"""

from __future__ import annotations

import contextlib
import gc
import math
import os
import threading
import warnings
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wdva import _ml
from wdva.bundle import AdapterBundle
from wdva.errors import WDVAError, WDVAWarning
from wdva.keys import KeyLike

if TYPE_CHECKING:
    import torch

    from wdva.training import StepCallback, TrainingConfig, TrainingResult

__all__ = ["DEFAULT_BASE_MODEL", "WDVA", "AdapterInfo", "BaseModelMismatchWarning"]

DEFAULT_BASE_MODEL = "HuggingFaceTB/SmolLM2-360M-Instruct"
"""A small, permissively licensed, ungated chat model that runs on a laptop CPU."""


class BaseModelMismatchWarning(WDVAWarning):
    """An adapter records a different base model name than the one it is being loaded into."""


@dataclass(frozen=True)
class AdapterInfo:
    """Describes the adapter currently applied to the engine."""

    source: str | None
    """The vault path it was loaded from, if any."""
    base_model: str | None
    """The base model recorded inside the vault at training or sealing time."""
    peft_type: str | None
    rank: int | None
    use_dora: bool
    tensors: int
    parameters: int


class WDVA:
    """A local causal language model that can wear one encrypted adapter at a time.

    Args:
        base_model: Hugging Face model id or local directory.
        device: ``"cuda"``, ``"mps"`` or ``"cpu"``. Auto-detected when omitted.
        dtype: A torch dtype or its name (``"float32"``, ``"float16"``,
            ``"bfloat16"``). Defaults to half precision on CUDA, full elsewhere.
        local_files_only: Never contact the Hugging Face Hub; use the local
            cache only. Combine with ``HF_HUB_OFFLINE=1`` for a hard guarantee.

    The base model is loaded on first use, not in the constructor.

    **Threads.** Each call is serialised: a generation never interleaves with
    an adapter swap. But two separate calls are two separate steps, so
    ``load()`` followed by ``generate()`` (or a ``with engine.adapter(...)``
    block) is *not* one atomic unit: another thread can swap the adapter in
    between. When several threads share an engine, bind the adapter to the
    request instead::

        engine.generate(prompt, adapter="alice.wdva", key=alice_key)

    That decrypts, applies, generates and removes under a single lock, so one
    user's request can never be answered with another user's adapter. One engine
    still holds one adapter at a time, so concurrent requests queue.
    """

    def __init__(
        self,
        base_model: str | os.PathLike[str] = DEFAULT_BASE_MODEL,
        *,
        device: str | None = None,
        dtype: str | torch.dtype | None = None,
        local_files_only: bool = False,
    ) -> None:
        name = _ml.model_name(base_model)
        _ml.require_ml()  # fail here, with an install hint, rather than on first use
        self._base_model_name = name
        self._device_request = device
        self._dtype_request = dtype
        self._local_files_only = local_files_only
        self._device: str | None = None
        self._base: Any = None
        self._tokenizer: Any = None
        self._peft: Any = None
        self._adapter_info: AdapterInfo | None = None
        self._external = False  # True when the caller supplied the model (from_loaded)
        self._structure: Any = None  # fingerprint of the bare base model
        # A plain Lock (not RLock): a streaming generator may be finalised by a
        # different thread than the one that started it.
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()  # the base model is loaded exactly once
        self._training_thread: int | None = None

    @classmethod
    def from_loaded(cls, model: Any, tokenizer: Any, *, name: str | None = None) -> WDVA:
        """Wrap a model you loaded yourself (quantised, custom device map, ...)."""
        _ml.require_ml()
        resolved = name or str(getattr(model.config, "_name_or_path", "") or "") or "custom-model"
        engine = cls(resolved)
        engine._base = model
        engine._tokenizer = tokenizer
        engine._external = True
        engine._device = str(next(model.parameters()).device)
        model.eval()
        engine._structure = _ml.structure(model)
        return engine

    # -- state ------------------------------------------------------------

    @property
    def base_model(self) -> str:
        """Name or path of the base model."""
        return self._base_model_name

    @property
    def is_loaded(self) -> bool:
        """Whether an adapter is currently applied."""
        return self._peft is not None

    @property
    def adapter_info(self) -> AdapterInfo | None:
        """The applied adapter, or ``None``."""
        return self._adapter_info

    @property
    def device(self) -> str:
        """The device the model runs on (loads the model if needed)."""
        self._ensure_model()
        assert self._device is not None
        return self._device

    @property
    def model(self) -> Any:
        """The underlying model: the ``PeftModel`` while an adapter is applied, else the base."""
        self._ensure_model()
        return self._peft if self._peft is not None else self._base

    @property
    def tokenizer(self) -> Any:
        """The tokenizer (loads the model if needed)."""
        self._ensure_model()
        return self._tokenizer

    def __repr__(self) -> str:
        # getattr: tracebacks call repr() on an engine whose constructor raised.
        state = "adapter loaded" if getattr(self, "_peft", None) is not None else "no adapter"
        return f"WDVA(base_model={getattr(self, '_base_model_name', None)!r}, {state})"

    def __reduce__(self) -> str | tuple[object, ...]:
        # Refuse before a single byte is written. Left to itself, pickle (and so
        # copy.deepcopy, multiprocessing, joblib, ...) serialises the base model and
        # the decrypted adapter first and only then fails on the lock.
        raise TypeError(
            "a WDVA engine cannot be pickled or copied: it may hold a decrypted adapter. "
            "Create a new engine and load the vault there instead."
        )

    def _ensure_model(self) -> tuple[Any, Any]:
        if self._base is None:
            with self._load_lock:
                if self._base is None:
                    if self._external:
                        raise WDVAError(
                            "this engine wrapped a model you supplied and no longer holds it; "
                            "create a new one with WDVA.from_loaded()"
                        )
                    _ml.require_ml()
                    self._device = _ml.resolve_device(self._device_request)
                    base, self._tokenizer = _ml.load_base_model(
                        self._base_model_name,
                        device=self._device,
                        dtype=_ml.resolve_dtype(self._dtype_request, self._device),
                        local_files_only=self._local_files_only,
                    )
                    self._structure = _ml.structure(base)
                    self._base = base
        return self._base, self._tokenizer

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the engine for one operation."""
        self._refuse_reentry()
        with self._lock:
            yield

    def _refuse_reentry(self) -> None:
        # train() runs entirely in the calling thread while holding the lock, so a
        # call back into the engine from an on_step callback would wait forever.
        if self._training_thread == threading.get_ident():
            raise WDVAError(
                "the engine is busy training in this thread; "
                "do not call it from an on_step callback"
            )

    # -- adapters ---------------------------------------------------------

    def load(
        self,
        adapter: str | os.PathLike[str] | AdapterBundle,
        *,
        key: KeyLike | None = None,
        passphrase: str | None = None,
    ) -> AdapterInfo:
        """Decrypt an adapter vault and apply it to the model, in memory.

        Replaces any adapter that is already applied. Decryption, validation and
        injection all happen before this returns, so a wrong key, a tampered
        file or an adapter for a different model fails here rather than at the
        first query.

        **Fails closed.** If loading fails for any reason, no adapter is left
        applied - not the new one, and not the one that was applied before. A
        caller that falls back to an un-personalised answer after a failed load
        therefore gets the bare base model, never the previous user's adapter.

        Raises:
            WrongKeyError, IntegrityError, VaultFormatError: see :mod:`wdva.vault`.
            AdapterMismatchError: the adapter does not fit this base model, or
                would modify it (see :func:`wdva._ml.build_inference_config`).
        """
        try:
            bundle, source = self._open(adapter, key, passphrase)
            with self._locked():
                return self._apply_locked(bundle, source, stacklevel=4)
        except BaseException:
            with contextlib.suppress(WDVAError):
                self.unload()
            raise

    @staticmethod
    def _open(
        adapter: str | os.PathLike[str] | AdapterBundle,
        key: KeyLike | None,
        passphrase: str | None,
    ) -> tuple[AdapterBundle, str | None]:
        """Decrypt (outside the lock: it needs no access to the model)."""
        if isinstance(adapter, AdapterBundle):
            if key is not None or passphrase is not None:
                raise TypeError(
                    "key/passphrase are only used to open a vault file; "
                    "an AdapterBundle is already decrypted"
                )
            return adapter, None
        return AdapterBundle.load(adapter, key=key, passphrase=passphrase), str(Path(adapter))

    def _open_for_request(
        self,
        adapter: str | os.PathLike[str] | AdapterBundle | None,
        key: KeyLike | None,
        passphrase: str | None,
    ) -> tuple[AdapterBundle, str | None] | None:
        """Open the adapter named by one request, failing closed like :meth:`load`."""
        if adapter is None:
            return None
        try:
            return self._open(adapter, key, passphrase)
        except BaseException:
            with contextlib.suppress(WDVAError):
                self.unload()
            raise

    def _apply_locked(
        self, bundle: AdapterBundle, source: str | None, *, stacklevel: int
    ) -> AdapterInfo:
        self._unload_locked()
        base, _ = self._ensure_model()
        recorded = bundle.base_model
        if recorded and recorded != self._base_model_name:
            warnings.warn(
                f"adapter was made for {recorded!r} but is being loaded into "
                f"{self._base_model_name!r}",
                BaseModelMismatchWarning,
                stacklevel=stacklevel,
            )
        try:
            self._peft = _ml.apply_adapter(base, bundle)
        except BaseException:
            self._verify_base(raise_on_damage=False)
            raise
        summary = bundle.summary()
        self._adapter_info = AdapterInfo(
            source=source,
            base_model=recorded,
            peft_type=summary["peft_type"],
            rank=summary["rank"],
            use_dora=bool(summary["use_dora"]),
            tensors=summary["tensors"],
            parameters=summary["parameters"],
        )
        return self._adapter_info

    def unload(self) -> None:
        """Zero the adapter's weights in memory and detach it. Safe to call twice.

        The base model is left exactly as it was before :meth:`load`. The vault
        file and your key are untouched.
        """
        with self._locked():
            self._unload_locked()

    def _unload_locked(self) -> None:
        if self._peft is None:
            return
        try:
            self._base = _ml.remove_adapter(self._peft)
        finally:
            self._peft = None
            self._adapter_info = None
            self._release_memory()
        self._verify_base(raise_on_damage=True)

    def _verify_base(self, *, raise_on_damage: bool) -> None:
        """Never keep serving from a base model whose structure an adapter changed.

        Adapters that could do that are refused before they are applied, so this
        is a second line of defence. If the bare model no longer has the modules
        and parameters it started with, it is discarded (and loaded afresh on
        next use) rather than trusted.
        """
        if self._base is None or self._structure is None:
            return
        if _ml.structure(self._base) == self._structure:
            return
        self._base = None
        self._tokenizer = None
        self._release_memory()
        if raise_on_damage:
            raise WDVAError(
                "removing the adapter did not restore the base model, so the base model was "
                "discarded; it will be loaded again on next use"
            )

    @contextlib.contextmanager
    def adapter(
        self,
        adapter: str | os.PathLike[str] | AdapterBundle,
        *,
        key: KeyLike | None = None,
        passphrase: str | None = None,
    ) -> Iterator[AdapterInfo]:
        """Apply an adapter for the duration of a ``with`` block, then remove it.

        Convenient in scripts and single-threaded code. It is **not** an
        isolation boundary between threads: the block is a ``load()``, your
        code, then an ``unload()``, and another thread can swap the adapter in
        between. In a multi-threaded server pass ``adapter=`` to
        :meth:`generate` or :meth:`stream` instead.
        """
        info = self.load(adapter, key=key, passphrase=passphrase)
        try:
            yield info
        finally:
            self.unload()

    @contextlib.contextmanager
    def _request_adapter(self, opened: tuple[AdapterBundle, str | None] | None) -> Iterator[None]:
        """With the lock held: wear ``opened`` for one request, then take it off."""
        if opened is None:
            yield
            return
        self._apply_locked(*opened, stacklevel=6)
        try:
            yield
        finally:
            self._unload_locked()

    # -- generation -------------------------------------------------------

    def generate(
        self,
        prompt: str | Sequence[Mapping[str, str]],
        *,
        adapter: str | os.PathLike[str] | AdapterBundle | None = None,
        key: KeyLike | None = None,
        passphrase: str | None = None,
        max_new_tokens: int = 256,
        temperature: float = 0.7,
        top_p: float = 0.9,
        system: str | None = None,
        raw: bool = False,
        use_adapter: bool = True,
        **generate_kwargs: Any,
    ) -> str:
        """Generate a completion and return only the newly generated text.

        Args:
            prompt: A string (treated as one user turn) or a list of chat messages.
            adapter: A vault (or decrypted bundle) to wear **for this request
                only**. It is applied, used and removed under one lock, which
                makes this the safe way to serve several users from threads.
                Any adapter applied earlier with :meth:`load` is removed, and
                afterwards the engine has none.
            key, passphrase: Open ``adapter`` when it is a vault path.
            max_new_tokens: Upper bound on generated tokens. Reduced if the
                prompt leaves less room than that in the model's context window.
            temperature: ``0`` selects greedy decoding; higher values sample.
            top_p: Nucleus sampling threshold (ignored when ``temperature == 0``).
            system: Optional system prompt.
            raw: Send a string prompt as-is, without the chat template.
            use_adapter: ``False`` answers with the bare base model even while an
                adapter is applied - handy for before/after comparisons.
            **generate_kwargs: Passed through to ``model.generate``.
        """
        import torch

        opened = self._open_for_request(adapter, key, passphrase)
        with self._locked(), self._request_adapter(opened):
            model, inputs, gen = self._prepare(
                prompt,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                system=system,
                raw=raw,
                extra=generate_kwargs,
            )
            with self._adapter_scope(use_adapter), torch.no_grad():
                output = model.generate(**inputs, **gen)
            new_tokens = output[0, inputs["input_ids"].shape[1] :]
            return str(self._tokenizer.decode(new_tokens, skip_special_tokens=True)).strip()

    def stream(
        self,
        prompt: str | Sequence[Mapping[str, str]],
        *,
        adapter: str | os.PathLike[str] | AdapterBundle | None = None,
        key: KeyLike | None = None,
        passphrase: str | None = None,
        max_new_tokens: int = 256,
        temperature: float = 0.7,
        top_p: float = 0.9,
        system: str | None = None,
        raw: bool = False,
        use_adapter: bool = True,
        **generate_kwargs: Any,
    ) -> Iterator[str]:
        """Like :meth:`generate`, but yields text pieces as they are produced.

        The engine stays locked until the iterator is exhausted or closed, so
        always consume it fully or call ``.close()`` on it, and do not call the
        engine again from inside the loop that consumes it: that call would wait
        for the stream to finish, which it cannot while the loop is waiting.
        """
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

        if "streamer" in generate_kwargs:
            raise ValueError("stream() manages its own streamer; do not pass one")
        cancelled = threading.Event()

        class _Cancel(StoppingCriteria):  # type: ignore[misc]
            def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> Any:
                return torch.full(
                    (input_ids.shape[0],),
                    cancelled.is_set(),
                    dtype=torch.bool,
                    device=input_ids.device,
                )

        opened = self._open_for_request(adapter, key, passphrase)
        self._refuse_reentry()
        self._lock.acquire()
        worker: threading.Thread | None = None
        try:
            with self._request_adapter(opened):
                try:
                    model, inputs, gen = self._prepare(
                        prompt,
                        max_new_tokens=max_new_tokens,
                        temperature=temperature,
                        top_p=top_p,
                        system=system,
                        raw=raw,
                        extra=generate_kwargs,
                    )
                    streamer = TextIteratorStreamer(
                        self._tokenizer, skip_prompt=True, skip_special_tokens=True
                    )
                    gen["streamer"] = streamer
                    gen["stopping_criteria"] = StoppingCriteriaList(
                        [*gen.get("stopping_criteria", []), _Cancel()]
                    )
                    failure: list[BaseException] = []

                    def run() -> None:
                        try:
                            with self._adapter_scope(use_adapter), torch.no_grad():
                                model.generate(**inputs, **gen)
                        except BaseException as exc:
                            failure.append(exc)
                            streamer.end()  # unblock the consumer

                    worker = threading.Thread(target=run, name="wdva-generate", daemon=True)
                    worker.start()
                    for piece in streamer:
                        if piece:
                            yield piece
                    worker.join()
                    if failure:
                        raise failure[0]
                finally:
                    # Stop the model before the adapter is taken off underneath it.
                    cancelled.set()
                    if worker is not None:
                        worker.join()
        finally:
            self._lock.release()

    def _prepare(
        self,
        prompt: str | Sequence[Mapping[str, str]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        system: str | None,
        raw: bool,
        extra: dict[str, Any],
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        import torch

        if max_new_tokens < 1:
            raise ValueError("max_new_tokens must be >= 1")
        if not (math.isfinite(temperature) and temperature >= 0):
            raise ValueError("temperature must be a finite number >= 0")
        if isinstance(prompt, str) and not prompt.strip():
            raise ValueError("prompt is empty")
        base, tokenizer = self._ensure_model()
        model = self._peft if self._peft is not None else base
        text, templated = _ml.format_prompt(tokenizer, prompt, system=system, raw=raw)
        ids = _ml.encode_text(tokenizer, text, templated=templated)
        if not ids:
            raise ValueError("prompt is empty after tokenisation")
        window = _ml.context_window(base)
        if window is not None:
            if len(ids) >= window:
                raise ValueError(
                    f"prompt is {len(ids)} tokens, but this model's context window is {window}"
                )
            max_new_tokens = min(max_new_tokens, window - len(ids))
        input_ids = torch.tensor([ids], dtype=torch.long, device=self._device)
        inputs = {"input_ids": input_ids, "attention_mask": torch.ones_like(input_ids)}
        gen: dict[str, Any] = {
            "max_new_tokens": max_new_tokens,
            "do_sample": temperature > 0,
            "pad_token_id": _ml.pad_token_id(tokenizer),
        }
        if temperature > 0:
            gen["temperature"] = temperature
            gen["top_p"] = top_p
        gen.update(extra)
        return model, inputs, gen

    @contextlib.contextmanager
    def _adapter_scope(self, use_adapter: bool) -> Iterator[None]:
        if use_adapter or self._peft is None:
            yield
        else:
            with self._peft.disable_adapter():
                yield

    # -- training ---------------------------------------------------------

    def train(
        self,
        output_path: str | os.PathLike[str],
        *,
        key: KeyLike | None = None,
        passphrase: str | None = None,
        documents: Sequence[str | os.PathLike[str]] = (),
        texts: Sequence[str] = (),
        examples: Sequence[Mapping[str, Any]] = (),
        config: TrainingConfig | None = None,
        overwrite: bool = False,
        public_metadata: Mapping[str, Any] | None = None,
        on_step: StepCallback | None = None,
    ) -> TrainingResult:
        """Train an adapter on this engine's base model and seal it to ``output_path``.

        Reuses the already loaded base model, which is handed back unchanged:
        same weights, same dtypes (a half-precision model is trained in float32
        and every tensor is then returned to the dtype it had), same flags.
        See :func:`wdva.training.train_adapter` for the arguments. The new
        adapter is **not** applied afterwards; call :meth:`load` for that.

        The engine is busy for the whole run. An ``on_step`` callback must not
        call back into the engine; doing so raises instead of waiting forever.
        """
        import torch

        from wdva.training import train_adapter

        with self._locked():
            if self._peft is not None:
                raise WDVAError("unload the current adapter before training a new one")
            base, tokenizer = self._ensure_model()
            dtypes = _ml.snapshot_dtypes(base)
            self._training_thread = threading.get_ident()
            try:
                if base.dtype != torch.float32:
                    # Half-precision weights convert to float32 and back without loss.
                    base.to(torch.float32)
                return train_adapter(
                    (base, tokenizer),
                    output_path,
                    key=key,
                    passphrase=passphrase,
                    documents=documents,
                    texts=texts,
                    examples=examples,
                    config=config,
                    overwrite=overwrite,
                    public_metadata=public_metadata,
                    on_step=on_step,
                    base_model_name=self._base_model_name,
                )
            finally:
                self._training_thread = None
                _ml.restore_dtypes(base, dtypes)
                self._release_memory()
                self._verify_base(raise_on_damage=False)

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Remove any adapter and release the base model.

        An engine created from a model id loads it again on next use. One
        created with :meth:`from_loaded` cannot be reused after this.
        """
        with self._locked():
            try:
                self._unload_locked()
            finally:
                self._base = None
                self._tokenizer = None
                self._release_memory()

    def __enter__(self) -> WDVA:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _release_memory(self) -> None:
        gc.collect()
        if self._device is None:
            return
        import torch

        if self._device.startswith("cuda"):
            torch.cuda.empty_cache()
        elif self._device.startswith("mps") and hasattr(torch, "mps"):
            torch.mps.empty_cache()
