"""
Main WDVA class - high-level API around encrypted adapters.

Status:
- train() is not implemented and raises NotImplementedError.
- query() decrypts and verifies the adapter but does not apply it yet;
  answers come from the base model, and a UserWarning says so.
- Encryption and decryption of adapter weights (wdva.crypto) work and are tested.

Example:
    >>> wdva = WDVA()
    >>> wdva.load("adapter.wdva", key_hex)
    >>> response = wdva.query("What is this document about?")  # base model answer

Copyright 2025 Enclave
Licensed under the Apache License, Version 2.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wdva.crypto import EncryptedAdapter
from wdva.inference import DEFAULT_MODEL as DEFAULT_INFERENCE_MODEL
from wdva.inference import LocalInference

logger = logging.getLogger(__name__)


# =============================================================================
# Type Definitions
# =============================================================================

class WDVAStatus(Enum):
    """WDVA instance status."""
    READY = "ready"  # Initialized, no adapter loaded
    LOADED = "loaded"  # Adapter loaded and ready for queries
    TRAINING = "training"  # Reserved; training is not implemented


@dataclass(frozen=True)
class TrainingResult:
    """Result of a training operation."""
    adapter_path: str
    encryption_key_hex: str
    num_documents: int
    num_samples: int
    
    def __repr__(self) -> str:
        return (
            f"TrainingResult(adapter_path='{self.adapter_path}', "
            f"num_documents={self.num_documents}, num_samples={self.num_samples})"
        )


# =============================================================================
# WDVA Class
# =============================================================================

class WDVA:
    """
    Weight-Delta Vault Adapters: encrypted adapters queried with a local model.
    
    What works today: holding an encrypted adapter and its key, verifying and
    decrypting it in memory, and querying a local MLX base model. Training
    adapters and applying them to the model are not implemented.
    
    Example:
        >>> # Simple usage
        >>> wdva = WDVA()
        >>> wdva.load("adapter.wdva", "your-encryption-key-hex")
        >>> response = wdva.query("What is the main topic?")
        
        >>> # With explicit adapter and key
        >>> wdva = WDVA(adapter_path="adapter.wdva", encryption_key="...")
        >>> response = wdva.query("What is this about?")
    """
    
    __slots__ = (
        '_model_name',
        '_adapter_path',
        '_encryption_key',
        '_inference_engine',
        '_status',
    )
    
    DEFAULT_MODEL = DEFAULT_INFERENCE_MODEL
    
    def __init__(
        self,
        adapter_path: Optional[str | Path] = None,
        encryption_key: Optional[str] = None,
        *,
        model_name: Optional[str] = None,
    ) -> None:
        """
        Initialize WDVA instance.
        
        Args:
            adapter_path: Path to encrypted adapter file (optional)
            encryption_key: Hex-encoded encryption key (optional)
            model_name: MLX base model for inference
                        (default: mlx-community/Llama-3.2-1B-Instruct-4bit)
            
        Note:
            If both adapter_path and encryption_key are provided, the adapter
            is ready for queries immediately. Otherwise, call load() first.
        """
        self._model_name = model_name or self.DEFAULT_MODEL
        self._adapter_path: Optional[Path] = None
        self._encryption_key: Optional[str] = None
        self._inference_engine: Optional[LocalInference] = None
        self._status = WDVAStatus.READY
        
        # Load adapter if both path and key provided
        if adapter_path is not None and encryption_key is not None:
            self.load(adapter_path, encryption_key)
        elif adapter_path is not None or encryption_key is not None:
            logger.warning(
                "Both adapter_path and encryption_key must be provided together. "
                "Call load() to load an adapter."
            )
    
    @property
    def status(self) -> WDVAStatus:
        """Get current status."""
        return self._status
    
    @property
    def is_loaded(self) -> bool:
        """Check if an adapter is loaded."""
        return self._status == WDVAStatus.LOADED
    
    @property
    def adapter_path(self) -> Optional[Path]:
        """Get loaded adapter path."""
        return self._adapter_path
    
    @property
    def model_name(self) -> str:
        """Get base model name."""
        return self._model_name
    
    def train(
        self,
        documents: List[str | Path],
        *,
        output_path: Optional[str | Path] = None,
        max_samples: int = 100,
        epochs: int = 3,
        **kwargs: Any,
    ) -> TrainingResult:
        """
        Train an adapter on your documents. NOT IMPLEMENTED.
        
        Earlier versions returned a placeholder result with a key and a path
        that was never written. This now fails loudly instead.
        
        Raises:
            NotImplementedError: Always
        """
        raise NotImplementedError(
            "Adapter training is not implemented. See README.md, 'Status'."
        )
    
    def load(
        self,
        adapter_path: str | Path,
        encryption_key: str,
    ) -> None:
        """
        Load an encrypted adapter.
        
        Args:
            adapter_path: Path to encrypted adapter file
            encryption_key: Hex-encoded encryption key (64 characters)
            
        Raises:
            FileNotFoundError: If adapter file doesn't exist
            ValueError: If encryption key is invalid
        """
        adapter_path = Path(adapter_path)
        
        # Validate path
        if not adapter_path.exists():
            raise FileNotFoundError(f"Adapter not found: {adapter_path}")
        
        # Validate encryption key format
        if not encryption_key or len(encryption_key) != 64:
            raise ValueError(
                "Encryption key must be 64 hex characters (32 bytes). "
                f"Got {len(encryption_key) if encryption_key else 0} characters."
            )
        
        try:
            # Validate hex format
            bytes.fromhex(encryption_key)
        except ValueError as e:
            raise ValueError(f"Invalid encryption key format: {e}") from e
        
        self._adapter_path = adapter_path
        self._encryption_key = encryption_key
        self._status = WDVAStatus.LOADED
        
        logger.info("Loaded adapter: %s", adapter_path)
    
    def query(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> str:
        """
        Query the local model with the loaded adapter.
        
        The adapter is decrypted in memory and verified, but it is NOT
        applied to the model yet: the answer comes from the base model, and
        a UserWarning says so. Decrypted weights are never written to disk;
        Python cannot guarantee that the memory holding them is wiped.
        
        Args:
            prompt: Your question or prompt
            max_tokens: Maximum tokens to generate (default: 512)
            temperature: Sampling temperature 0.0-1.0 (default: 0.7)
            **kwargs: Additional inference parameters
            
        Returns:
            Model response as string
            
        Raises:
            ValueError: If no adapter is loaded
            RuntimeError: If inference fails
            
        Example:
            >>> wdva = WDVA(adapter_path="adapter.wdva", encryption_key="...")
            >>> response = wdva.query("What is the main topic?")
            >>> print(response)
        """
        if not self.is_loaded:
            raise ValueError(
                "No adapter loaded. Call load() or provide adapter_path "
                "and encryption_key in __init__"
            )
        
        # Initialize inference engine (lazy loading)
        if self._inference_engine is None:
            self._inference_engine = LocalInference(model_name=self._model_name)
        
        # Query with adapter
        response = self._inference_engine.query_with_adapter(
            adapter_path=str(self._adapter_path),
            encryption_key=self._encryption_key,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            _stacklevel=3,
            **kwargs
        )

        return response
    
    def query_base(
        self,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> str:
        """
        Query the base model without adapter (for comparison/testing).
        
        Args:
            prompt: Your question or prompt
            max_tokens: Maximum tokens to generate (default: 512)
            temperature: Sampling temperature 0.0-1.0 (default: 0.7)
            **kwargs: Additional inference parameters
            
        Returns:
            Model response as string
        """
        if self._inference_engine is None:
            self._inference_engine = LocalInference(model_name=self._model_name)
        
        return self._inference_engine.query_base(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs
        )
    
    def unload(self) -> None:
        """
        Forget the loaded adapter path and this object's reference to the key.

        The encrypted file and every other copy of the key are untouched;
        see delete() for what forgetting a key does and does not achieve.
        """
        self._adapter_path = None
        self._encryption_key = None
        self._status = WDVAStatus.READY
        
        logger.info("Adapter unloaded")
    
    def delete(self) -> None:
        """
        Forget this object's copy of the key and unload the model.
        
        This drops this object's reference to the key; it does not delete
        the key anywhere else. The key was passed in by you, so other copies
        (your password manager, a backup, an exported string, Python memory
        not yet reused) are untouched. The adapter file becomes unreadable
        only once EVERY copy of the key is destroyed; this method cannot
        check that.
        """
        if self._encryption_key:
            # Python strings are immutable, so the key cannot be zeroed here
            self._encryption_key = None
            logger.info("Dropped this object's reference to the encryption key")
        
        self._adapter_path = None
        self._status = WDVAStatus.READY
        
        # Clear inference engine
        if self._inference_engine is not None:
            self._inference_engine.unload_model()
            self._inference_engine = None
    
    def export_key(self) -> str:
        """
        Export encryption key for backup.
        
        WARNING: Store this securely! Anyone with this key can decrypt
        the adapter file.
        
        Returns:
            Hex-encoded encryption key (64 characters)
            
        Raises:
            ValueError: If no adapter is loaded
        """
        if not self._encryption_key:
            raise ValueError("No encryption key to export (adapter not loaded)")
        
        return self._encryption_key
    
    @staticmethod
    def generate_key() -> Tuple[bytes, str]:
        """
        Generate a new encryption key.
        
        Returns:
            Tuple of (key_bytes, key_hex)
            
        Example:
            >>> key_bytes, key_hex = WDVA.generate_key()
            >>> print(f"Store securely: {key_hex}")
        """
        key_bytes = EncryptedAdapter.generate_key()
        key_hex = key_bytes.hex()
        return key_bytes, key_hex
    
    def __repr__(self) -> str:
        adapter_info = f"adapter={self._adapter_path.name}" if self._adapter_path else "no adapter"
        return f"WDVA(status={self._status.value}, {adapter_info}, model={self._model_name})"
    
    def __str__(self) -> str:
        return self.__repr__()
