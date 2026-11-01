"""
Local Inference - run a base model on Apple Silicon with MLX.

Status: this module runs a base model locally. It can decrypt and verify an
encrypted adapter, but it does NOT apply the adapter to the model yet, so
answers from query_with_adapter() come from the base model alone.

Copyright 2025 Enclave
Licensed under the Apache License, Version 2.0
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any, Literal, Optional

from wdva.crypto import EncryptedAdapter

logger = logging.getLogger(__name__)


# =============================================================================
# Backend Detection (lazy, on first use)
# =============================================================================

_mlx_available: Optional[bool] = None

def _check_mlx() -> bool:
    """Check if MLX and mlx-lm are available (lazy evaluation)."""
    global _mlx_available
    if _mlx_available is None:
        try:
            import mlx.core  # noqa: F401
            import mlx_lm  # noqa: F401
            _mlx_available = True
        except ImportError:
            _mlx_available = False
    return _mlx_available


# =============================================================================
# Constants
# =============================================================================

DEFAULT_MODEL = "mlx-community/Llama-3.2-1B-Instruct-4bit"

BackendType = Literal["mlx"]

ADAPTER_NOT_APPLIED_WARNING = (
    "The adapter was decrypted and verified but is NOT applied to the model yet; "
    "this answer comes from the base model alone."
)


# =============================================================================
# LocalInference Class
# =============================================================================

class LocalInference:
    """
    Local inference engine using MLX on Apple Silicon.

    The model is loaded once per instance. Prompts and answers stay on this
    machine. Unless model_name is a local path or HF_HUB_OFFLINE=1 is set,
    every load contacts the Hugging Face Hub to check the model's revision
    (revealing the model name and your IP address), and the first load
    downloads the model.

    Example:
        >>> engine = LocalInference()
        >>> response = engine.query_base("What is 2+2?")
        >>> print(response)
    """

    __slots__ = (
        '_model_name',
        '_model',
        '_tokenizer',
        '_backend',
        '_loaded_adapter_name',
    )

    def __init__(
        self,
        model_name: Optional[str] = None,
        *,
        backend: Optional[BackendType] = None,
    ) -> None:
        """
        Initialize local inference engine.

        Args:
            model_name: MLX model repo or local path
                        (default: mlx-community/Llama-3.2-1B-Instruct-4bit)
            backend: Only "mlx" is supported; kept for API compatibility.

        Raises:
            ValueError: If an unsupported backend is requested
            RuntimeError: If MLX / mlx-lm is not installed
        """
        if backend not in (None, "mlx"):
            raise ValueError(f"Unsupported backend {backend!r}; only 'mlx' is supported")
        if not _check_mlx():
            raise RuntimeError(
                "MLX is not available. On an Apple Silicon Mac install it with: "
                "pip install 'wdva[mlx]'"
            )

        self._model_name = model_name or DEFAULT_MODEL
        self._model: Any = None
        self._tokenizer: Any = None
        self._backend: BackendType = "mlx"
        self._loaded_adapter_name: Optional[str] = None

        logger.debug("Initialized LocalInference (model=%s)", self._model_name)

    @property
    def backend(self) -> BackendType:
        """Get current backend type."""
        return self._backend

    @property
    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self._model is not None

    @property
    def model_name(self) -> str:
        """Get model name."""
        return self._model_name

    def load_model(self, *, force_reload: bool = False) -> bool:
        """
        Load the base model (downloads it on first use).

        Args:
            force_reload: Force reload even if already loaded

        Returns:
            True if model loaded successfully, False otherwise
        """
        if self._model is not None and not force_reload:
            logger.debug("Model already loaded")
            return True

        from mlx_lm import load as mlx_load

        try:
            logger.info("Loading MLX model: %s", self._model_name)
            self._model, self._tokenizer = mlx_load(self._model_name)
        except Exception as e:
            logger.error("Failed to load %s: %s", self._model_name, e)
            return False

        logger.info("Loaded %s", self._model_name)
        return True

    def query_with_adapter(
        self,
        adapter_path: str | Path,
        encryption_key: str,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        _stacklevel: int = 2,
        **kwargs: Any,
    ) -> str:
        """
        Decrypt an adapter, then answer with the base model.

        The adapter is decrypted in memory and its authentication tag is
        verified, but applying it to the model is not implemented yet: the
        answer comes from the base model alone, and a UserWarning says so.
        Decrypted weights are dropped afterwards; Python cannot guarantee
        that the memory is wiped.

        Args:
            adapter_path: Path to encrypted adapter file
            encryption_key: Hex-encoded encryption key
            prompt: Your question or prompt
            max_tokens: Maximum tokens to generate (default: 512)
            temperature: Sampling temperature, 0 for greedy (default: 0.7)
            **kwargs: Additional arguments for mlx_lm.generate

        Returns:
            Model response as string

        Raises:
            RuntimeError: If model fails to load
            ValueError: If decryption fails
        """
        if self._model is None:
            if not self.load_model():
                raise RuntimeError("Failed to load model")

        key_bytes = EncryptedAdapter.key_from_hex(encryption_key)
        adapter = EncryptedAdapter(key_bytes)

        adapter_weights = adapter.decrypt_weights(adapter_path, framework=self._backend)
        try:
            logger.info("Decrypted adapter with %d tensors", len(adapter_weights))
            warnings.warn(ADAPTER_NOT_APPLIED_WARNING, UserWarning, stacklevel=_stacklevel)
            self._loaded_adapter_name = Path(adapter_path).stem

            return self._generate(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs
            )
        finally:
            del adapter_weights
            self._loaded_adapter_name = None

    def query_base(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> str:
        """
        Query the base model.

        Args:
            prompt: Your question or prompt
            max_tokens: Maximum tokens to generate (default: 512)
            temperature: Sampling temperature, 0 for greedy (default: 0.7)
            **kwargs: Additional arguments for mlx_lm.generate

        Returns:
            Model response as string

        Raises:
            RuntimeError: If model fails to load
        """
        if self._model is None:
            if not self.load_model():
                raise RuntimeError("Failed to load model")

        return self._generate(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs
        )

    def _generate(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> str:
        """Generate with mlx-lm, applying the chat template when the model has one."""
        from mlx_lm import generate as mlx_generate
        from mlx_lm.sample_utils import make_sampler

        if getattr(self._tokenizer, "has_chat_template", False):
            prompt = self._tokenizer.apply_chat_template(
                [{"role": "user", "content": prompt}],
                add_generation_prompt=True,
                tokenize=False,
            )

        return mlx_generate(
            self._model,
            self._tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
            sampler=make_sampler(temp=max(temperature, 0.0)),
            **kwargs
        )

    def unload_model(self) -> None:
        """Drop references to the model so it can be garbage-collected."""
        self._model = None
        self._tokenizer = None
        self._loaded_adapter_name = None
        logger.info("Model unloaded from memory")

    def __repr__(self) -> str:
        status = "loaded" if self._model is not None else "not loaded"
        return f"LocalInference(backend={self._backend}, model={self._model_name}, status={status})"


# =============================================================================
# Module-level convenience function
# =============================================================================

def create_inference_engine(
    model_name: Optional[str] = None,
    *,
    auto_load: bool = True,
) -> LocalInference:
    """
    Create and optionally load an inference engine.

    Args:
        model_name: MLX model repo or local path (default: DEFAULT_MODEL)
        auto_load: Load the model now (default: True)

    Returns:
        Configured LocalInference instance

    Example:
        >>> engine = create_inference_engine()
        >>> response = engine.query_base("Hello!")
    """
    engine = LocalInference(model_name=model_name)

    if auto_load:
        engine.load_model()

    return engine
