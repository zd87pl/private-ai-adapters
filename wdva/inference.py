"""
Local Inference - Run WDVA adapters entirely on your device.

No cloud required. Your data never leaves your machine.

Supported Backends:
- MLX (Apple Silicon M1/M2/M3/M4) - Fastest on Mac
- PyTorch (CPU/CUDA) - Works everywhere

Copyright 2025 Enclave
Licensed under the Apache License, Version 2.0
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from wdva.crypto import EncryptedAdapter

logger = logging.getLogger(__name__)

# =============================================================================
# Environment Configuration (before imports that use them)
# =============================================================================

# Enable fast HuggingFace downloads if available
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")


# =============================================================================
# Backend Detection (lazy, on first use)
# =============================================================================

_mlx_available: Optional[bool] = None
_torch_available: Optional[bool] = None

def _check_mlx() -> bool:
    """Check if MLX is available (lazy evaluation)."""
    global _mlx_available
    if _mlx_available is None:
        try:
            import mlx.core  # noqa: F401
            from mlx_lm import generate, load  # noqa: F401
            _mlx_available = True
        except ImportError:
            _mlx_available = False
    return _mlx_available

def _check_torch() -> bool:
    """Check if PyTorch is available (lazy evaluation)."""
    global _torch_available
    if _torch_available is None:
        try:
            import torch  # noqa: F401
            from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: F401
            _torch_available = True
        except ImportError:
            _torch_available = False
    return _torch_available


# =============================================================================
# Constants
# =============================================================================

DEFAULT_MODEL = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"

# MLX models to try in order (smaller/faster first)
MLX_MODEL_CANDIDATES: List[str] = [
    "mlx-community/Llama-3.2-1B-Instruct-4bit",
    "mlx-community/Qwen2.5-1.5B-Instruct-4bit",
    "mlx-community/TinyLlama-1.1B-Chat-v1.0-8bit",
    "mlx-community/SmolLM2-1.7B-Instruct-4bit",
]

BackendType = Literal["mlx", "torch"]


# =============================================================================
# LocalInference Class
# =============================================================================

class LocalInference:
    """
    Local inference engine for running WDVA adapters.
    
    Supports multiple backends for maximum compatibility:
    - MLX (Apple Silicon): Fastest on M1/M2/M3/M4 Macs
    - PyTorch (CPU/CUDA): Works on any platform
    
    The model is loaded once and cached. Adapters are loaded ephemerally
    (decrypted in memory, used, then cleared).
    
    Example:
        >>> engine = LocalInference()
        >>> engine.load_model()
        >>> response = engine.query_base("What is 2+2?")
        >>> print(response)
    """
    
    __slots__ = (
        '_model_name',
        '_model',
        '_tokenizer', 
        '_backend',
        '_loaded_adapter_name',
        '_device',
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
            model_name: Base model to use (default: TinyLlama-1.1B)
            backend: Force specific backend ("mlx" or "torch"). 
                     If None, auto-detects best available.
                     
        Raises:
            RuntimeError: If no ML backend is available
        """
        self._model_name = model_name or DEFAULT_MODEL
        self._model: Any = None
        self._tokenizer: Any = None
        self._backend: Optional[BackendType] = None
        self._loaded_adapter_name: Optional[str] = None
        self._device: str = "cpu"
        
        # Determine best backend
        if backend:
            self._backend = backend
            if backend == "mlx" and not _check_mlx():
                raise RuntimeError("MLX requested but not available")
            if backend == "torch" and not _check_torch():
                raise RuntimeError("PyTorch requested but not available")
        else:
            # Auto-detect: prefer MLX on Apple Silicon
            if _check_mlx():
                self._backend = "mlx"
            elif _check_torch():
                self._backend = "torch"
            else:
                raise RuntimeError(
                    "No ML backend available. Install one of:\n"
                    "  - MLX (Apple Silicon): pip install mlx mlx-lm\n"
                    "  - PyTorch: pip install torch transformers"
                )
        
        logger.debug("Initialized LocalInference (backend=%s)", self._backend)
    
    @property
    def backend(self) -> Optional[BackendType]:
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
        Load the base model (downloads if needed).
        
        Args:
            force_reload: Force reload even if already loaded
            
        Returns:
            True if model loaded successfully, False otherwise
        """
        if self._model is not None and not force_reload:
            logger.debug("Model already loaded")
            return True
        
        try:
            if self._backend == "mlx":
                return self._load_mlx_model()
            else:
                return self._load_torch_model()
        except Exception as e:
            logger.error("Failed to load model: %s", e)
            return False
    
    def _load_mlx_model(self) -> bool:
        """Load model using MLX backend."""
        from mlx_lm import load as mlx_load
        
        # Try MLX models in order
        for model_name in MLX_MODEL_CANDIDATES:
            try:
                logger.info("Loading MLX model: %s", model_name)
                self._model, self._tokenizer = mlx_load(model_name)
                self._model_name = model_name
                logger.info("✓ Successfully loaded: %s", model_name)
                return True
            except Exception as e:
                logger.warning("Failed to load %s: %s", model_name, e)
                continue
        
        # Fallback to PyTorch if MLX fails
        logger.warning("MLX models failed, falling back to PyTorch")
        if _check_torch():
            self._backend = "torch"
            return self._load_torch_model()
        
        return False
    
    def _load_torch_model(self) -> bool:
        """Load model using PyTorch backend."""
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        
        logger.info("Loading PyTorch model: %s", self._model_name)
        
        # Determine device
        if torch.cuda.is_available():
            self._device = "cuda"
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            self._device = "mps"  # Apple Silicon via PyTorch
        else:
            self._device = "cpu"
        
        # Load tokenizer
        self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
        if self._tokenizer.pad_token is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token
        
        # Load model with appropriate dtype
        dtype = torch.float16 if self._device in ("cuda", "mps") else torch.float32
        
        self._model = AutoModelForCausalLM.from_pretrained(
            self._model_name,
            torch_dtype=dtype,
            device_map="auto" if self._device == "cuda" else None,
            low_cpu_mem_usage=True,
        )
        
        # Move to device if not using device_map
        if self._device != "cuda":
            self._model = self._model.to(self._device)
        
        self._model.eval()
        logger.info("✓ PyTorch model loaded on %s", self._device)
        return True
    
    def query_with_adapter(
        self,
        adapter_path: str | Path,
        encryption_key: str,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> str:
        """
        Query model with encrypted adapter.
        
        The adapter is decrypted ephemerally (in-memory only) and never
        persists to disk. After inference, decrypted data is cleared.
        
        Args:
            adapter_path: Path to encrypted adapter file
            encryption_key: Hex-encoded encryption key
            prompt: Your question or prompt
            max_tokens: Maximum tokens to generate (default: 512)
            temperature: Sampling temperature 0.0-1.0 (default: 0.7)
            **kwargs: Additional generation parameters
            
        Returns:
            Model response as string
            
        Raises:
            RuntimeError: If model fails to load
            ValueError: If decryption fails
        """
        # Ensure model is loaded
        if self._model is None:
            if not self.load_model():
                raise RuntimeError("Failed to load model")
        
        # Decrypt adapter
        logger.info("Decrypting adapter (ephemeral, in-memory only)...")
        key_bytes = EncryptedAdapter.key_from_hex(encryption_key)
        adapter = EncryptedAdapter(key_bytes)
        
        try:
            adapter_weights = adapter.decrypt_weights(adapter_path, framework=self._backend)
            logger.info("Decrypted adapter with %d tensors", len(adapter_weights))
            
            # NOTE: Full adapter application requires DoRA/LoRA merging logic.
            # This is a simplified implementation that demonstrates the flow.
            # In production, you would merge adapter weights with the base model.
            self._loaded_adapter_name = Path(adapter_path).stem
            logger.debug("Adapter ready for inference (weights in memory)")
            
            # Generate response
            response = self._generate(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs
            )
            
            return response
            
        finally:
            # Clear adapter from memory
            # Note: Python's del doesn't guarantee secure memory clearing,
            # but it marks the memory for garbage collection
            if 'adapter_weights' in dir():
                del adapter_weights
            self._loaded_adapter_name = None
            logger.debug("Adapter cleared from memory")
    
    def query_base(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> str:
        """
        Query base model without adapter (for testing/comparison).
        
        Args:
            prompt: Your question or prompt
            max_tokens: Maximum tokens to generate (default: 512)
            temperature: Sampling temperature 0.0-1.0 (default: 0.7)
            **kwargs: Additional generation parameters
            
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
        """Internal generation dispatcher."""
        if self._backend == "mlx":
            return self._mlx_generate(prompt, max_tokens, temperature, **kwargs)
        else:
            return self._torch_generate(prompt, max_tokens, temperature, **kwargs)
    
    def _mlx_generate(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> str:
        """Generate with MLX backend."""
        from mlx_lm import generate as mlx_generate
        
        response = mlx_generate(
            self._model,
            self._tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature if temperature > 0 else None,
            **kwargs
        )
        return response
    
    def _torch_generate(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
        **kwargs: Any,
    ) -> str:
        """Generate with PyTorch backend."""
        import torch
        
        # Tokenize input
        inputs = self._tokenizer(
            prompt,
            return_tensors="pt",
            padding=True,
            truncation=True,
        )
        
        # Move to device
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        
        # Generate
        with torch.no_grad():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                temperature=temperature if temperature > 0 else 1.0,
                do_sample=temperature > 0,
                pad_token_id=self._tokenizer.eos_token_id,
                **kwargs
            )
        
        # Decode response
        response = self._tokenizer.decode(outputs[0], skip_special_tokens=True)
        
        # Remove prompt from response (model includes it in output)
        if response.startswith(prompt):
            response = response[len(prompt):].strip()
        
        return response
    
    def unload_model(self) -> None:
        """Unload model from memory."""
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
    backend: Optional[BackendType] = None,
    auto_load: bool = True,
) -> LocalInference:
    """
    Create and optionally initialize an inference engine.
    
    Args:
        model_name: Base model to use (default: auto-select)
        backend: Force specific backend ("mlx" or "torch")
        auto_load: Automatically load the model (default: True)
        
    Returns:
        Configured LocalInference instance
        
    Example:
        >>> engine = create_inference_engine()
        >>> response = engine.query_base("Hello!")
    """
    engine = LocalInference(model_name=model_name, backend=backend)
    
    if auto_load:
        engine.load_model()
    
    return engine
