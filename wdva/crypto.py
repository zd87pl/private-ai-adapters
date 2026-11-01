"""
Encrypted Adapter - Core encryption/decryption for WDVA.

This module encrypts and authenticates adapter weight files:
- XChaCha20-Poly1305 authenticated encryption
- HKDF key derivation
- In-memory safetensors serialization (no pickle)

Security Notes:
- Uses 256-bit keys (32 bytes) for XChaCha20-Poly1305
- Uses 192-bit nonces (24 bytes) for XChaCha20
- Uses HKDF-SHA256 for key derivation with random salt
- Authenticated encryption prevents tampering
- AAD (Additional Authenticated Data) binds metadata to ciphertext

Copyright 2025 Enclave
Licensed under the Apache License, Version 2.0
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import sys
from base64 import b64decode, b64encode
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Literal, Optional, TypedDict

logger = logging.getLogger(__name__)

# =============================================================================
# Dependency Availability Checks
# =============================================================================

_CRYPTO_AVAILABLE = False
_COMPRESSION_AVAILABLE = False
_SAFETENSORS_AVAILABLE = False

try:
    from Crypto.Cipher import ChaCha20_Poly1305
    from Crypto.Random import get_random_bytes
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    _CRYPTO_AVAILABLE = True
except ImportError:
    pass

try:
    import zstandard as zstd
    _COMPRESSION_AVAILABLE = True
except ImportError:
    pass

try:
    import numpy as np
    from safetensors.numpy import load as safetensors_load_numpy
    from safetensors.numpy import save as safetensors_save_numpy
    _SAFETENSORS_AVAILABLE = True
except ImportError:
    pass


# =============================================================================
# Type Definitions
# =============================================================================

Framework = Literal["numpy", "mlx", "torch"]


class EncryptedPackage(TypedDict):
    """Structure of an encrypted adapter file."""
    salt: str  # Base64-encoded
    nonce: str  # Base64-encoded
    ciphertext: str  # Base64-encoded
    tag: str  # Base64-encoded
    metadata: Dict[str, Any]
    algorithm: str
    kdf: str


# =============================================================================
# Constants
# =============================================================================

KEY_SIZE = 32  # 256 bits
NONCE_SIZE = 24  # 192 bits for XChaCha20
SALT_SIZE = 16  # 128 bits
TAG_SIZE = 16  # 128 bits

ENCRYPTION_INFO = b"wdva-encryption-v1"
CURRENT_VERSION = "1.0"
ALGORITHM = "XChaCha20-Poly1305"
KDF_ALGORITHM = "HKDF-SHA256"

SUPPORTED_FRAMEWORKS = ("numpy", "mlx", "torch")

# Metadata keys written by encrypt_weights() and read back by decrypt_weights().
# Callers may not override them through the `metadata` argument.
RESERVED_METADATA_KEYS = frozenset({'version', 'num_tensors', 'original_size', 'compressed'})


# =============================================================================
# Serialization helpers
# =============================================================================

def _serialize_weights(weights: Dict[str, Any]) -> bytes:
    """
    Serialize a dict of tensors to safetensors bytes, in memory.

    Accepts numpy arrays, MLX arrays or PyTorch tensors (one kind per dict).
    MLX and PyTorch are only used if the caller has already imported them,
    so this never pulls in a heavy framework on its own.
    """
    if not isinstance(weights, dict):
        raise TypeError(f"weights must be a dict, got {type(weights).__name__}")
    if not all(isinstance(name, str) for name in weights):
        raise TypeError("weight names must be str")

    values = list(weights.values())

    if all(isinstance(v, np.ndarray) for v in values):
        # safetensors.numpy writes each array's raw buffer and ignores strides,
        # so transposed, Fortran-order or sliced views must be copied to
        # C order first or they are written with the wrong values.
        return safetensors_save_numpy(
            {name: np.array(v, order="C", copy=True) for name, v in weights.items()}
        )

    mx = sys.modules.get("mlx.core")
    if mx is not None and all(isinstance(v, mx.array) for v in values):
        buffer = io.BytesIO()
        mx.save_safetensors(buffer, weights)
        return buffer.getvalue()

    torch = sys.modules.get("torch")
    if torch is not None and all(isinstance(v, torch.Tensor) for v in values):
        from safetensors.torch import save as safetensors_save_torch
        # safetensors.torch rejects non-contiguous and storage-sharing tensors
        return safetensors_save_torch(
            {name: v.detach().cpu().contiguous().clone() for name, v in weights.items()}
        )

    kinds = sorted({f"{type(v).__module__}.{type(v).__name__}" for v in values})
    raise TypeError(
        "weights must all be numpy arrays, MLX arrays or torch tensors "
        f"(one kind per dict), got: {', '.join(kinds)}"
    )


# safetensors dtypes each framework cannot load faithfully: numpy has no
# bfloat16 or float8, MLX has no float8 (it would come back as uint8) and
# cannot load float64.
_UNSUPPORTED_DTYPES = {
    "numpy": lambda dtype: dtype == "BF16" or dtype.startswith("F8"),
    "mlx": lambda dtype: dtype == "F64" or dtype.startswith("F8"),
    "torch": lambda dtype: False,
}


def _safetensors_dtypes(data: bytes) -> set[str]:
    """Read the tensor dtypes from a safetensors header."""
    header_size = int.from_bytes(data[:8], "little")
    header = json.loads(data[8:8 + header_size])
    return {info["dtype"] for name, info in header.items() if name != "__metadata__"}


def _deserialize_weights(data: bytes, framework: Framework) -> Dict[str, Any]:
    """Deserialize safetensors bytes into arrays of the requested framework."""
    if framework not in _UNSUPPORTED_DTYPES:
        raise ValueError(
            f"Unsupported framework {framework!r}; expected one of {SUPPORTED_FRAMEWORKS}"
        )

    unsupported = sorted(d for d in _safetensors_dtypes(data) if _UNSUPPORTED_DTYPES[framework](d))
    if unsupported:
        alternatives = [
            f"framework={other!r}" for other in SUPPORTED_FRAMEWORKS
            if not any(_UNSUPPORTED_DTYPES[other](d) for d in unsupported)
        ]
        raise ValueError(
            f"Adapter contains dtypes {unsupported} that {framework} cannot represent; "
            f"decrypt with {' or '.join(alternatives)}"
        )

    if framework == "numpy":
        return safetensors_load_numpy(data)
    if framework == "mlx":
        import mlx.core as mx
        return mx.load(io.BytesIO(data), format="safetensors")
    from safetensors.torch import load as safetensors_load_torch
    return safetensors_load_torch(data)


# =============================================================================
# EncryptedAdapter Class
# =============================================================================

class EncryptedAdapter:
    """
    Encrypted adapter manager for WDVA.
    
    Handles encryption and decryption of DoRA adapter weights using
    XChaCha20-Poly1305 authenticated encryption.
    
    Security Properties:
    - Confidentiality: Encrypted data is unreadable without key
    - Integrity: Authentication tag detects any tampering
    - Authenticity: AAD binds metadata to ciphertext
    
    Example:
        >>> key = EncryptedAdapter.generate_key()
        >>> adapter = EncryptedAdapter(key)
        >>> adapter.encrypt_weights(weights, "adapter.wdva")
        >>> decrypted = adapter.decrypt_weights("adapter.wdva")
    """
    
    __slots__ = ('_encryption_key', '_enable_compression', '_compressor', '_decompressor')
    
    def __init__(
        self,
        encryption_key: bytes,
        *,
        enable_compression: bool = True
    ) -> None:
        """
        Initialize encrypted adapter manager.
        
        Args:
            encryption_key: 32-byte encryption key (use generate_key() to create)
            enable_compression: Whether to compress before encryption (default: True)
            
        Raises:
            ImportError: If required cryptography libraries are not available
            ValueError: If encryption key is not exactly 32 bytes
        """
        if not _CRYPTO_AVAILABLE:
            raise ImportError(
                "Cryptography libraries not available. "
                "Install with: pip install pycryptodome cryptography"
            )
        
        if not isinstance(encryption_key, bytes):
            raise TypeError(f"encryption_key must be bytes, got {type(encryption_key).__name__}")
        
        if len(encryption_key) != KEY_SIZE:
            raise ValueError(
                f"Encryption key must be exactly {KEY_SIZE} bytes, got {len(encryption_key)}"
            )
        
        self._encryption_key = encryption_key
        self._enable_compression = enable_compression and _COMPRESSION_AVAILABLE
        
        # Initialize compressor/decompressor if compression is enabled
        self._compressor: Optional[Any] = None
        self._decompressor: Optional[Any] = None
        
        if self._enable_compression:
            self._compressor = zstd.ZstdCompressor(level=3)
            self._decompressor = zstd.ZstdDecompressor()
        
        logger.debug("Initialized EncryptedAdapter (compression=%s)", self._enable_compression)
    
    def _derive_key(self, salt: bytes, info: bytes = ENCRYPTION_INFO) -> bytes:
        """
        Derive encryption key using HKDF-SHA256.
        
        This ensures that even with the same master key, different adapters
        have different encryption keys (due to random salt).
        
        Args:
            salt: Random salt (should be unique per adapter)
            info: Application-specific info string
            
        Returns:
            Derived 32-byte key
        """
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=KEY_SIZE,
            salt=salt,
            info=info,
        )
        return hkdf.derive(self._encryption_key)
    
    def encrypt_weights(
        self,
        weights: Dict[str, Any],
        output_path: str | Path,
        *,
        metadata: Optional[Dict[str, Any]] = None
    ) -> EncryptedPackage:
        """
        Encrypt adapter weights and save to file.
        
        Args:
            weights: Dictionary of adapter weights: numpy arrays, MLX arrays
                or PyTorch tensors (one kind per dict)
            output_path: Path to save encrypted adapter
            metadata: Optional JSON-serializable metadata to include (stored in
                plaintext but authenticated). May not use the reserved keys
                'version', 'num_tensors', 'original_size' or 'compressed'.
            
        Returns:
            Dictionary with encryption metadata
            
        Raises:
            ImportError: If safetensors/numpy is not available
            TypeError: If weights are not a dict of supported arrays keyed by
                str, or metadata is not a mapping with str keys
            ValueError: If metadata uses a reserved key
            OSError: If file cannot be written
        """
        if not _SAFETENSORS_AVAILABLE:
            raise ImportError(
                "safetensors not available. Install with: pip install safetensors numpy"
            )
        
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise TypeError(f"metadata must be a mapping, got {type(metadata).__name__}")
            metadata = dict(metadata)
            if not all(isinstance(k, str) for k in metadata):
                raise TypeError("metadata keys must be str")
            clashing = RESERVED_METADATA_KEYS & metadata.keys()
            if clashing:
                raise ValueError(f"metadata may not override reserved keys: {sorted(clashing)}")
        
        output_path = Path(output_path)
        
        # Serialize weights to safetensors format (in memory)
        serialized_data = _serialize_weights(weights)
        original_size = len(serialized_data)
        
        # Compress if enabled
        if self._enable_compression and self._compressor is not None:
            data_to_encrypt = self._compressor.compress(serialized_data)
            compressed_size = len(data_to_encrypt)
            compression_ratio = (1 - compressed_size / original_size) * 100
            logger.debug(
                "Compressed: %d -> %d bytes (%.1f%% reduction)",
                original_size, compressed_size, compression_ratio
            )
        else:
            data_to_encrypt = serialized_data
        
        # Generate random salt and derive encryption key
        salt = get_random_bytes(SALT_SIZE)
        derived_key = self._derive_key(salt)
        
        # Generate random nonce (24 bytes for XChaCha20)
        nonce = get_random_bytes(NONCE_SIZE)
        
        # Create cipher
        cipher = ChaCha20_Poly1305.new(key=derived_key, nonce=nonce)
        
        # Prepare metadata (stored in plaintext but authenticated)
        encryption_metadata: Dict[str, Any] = {
            'version': CURRENT_VERSION,
            'num_tensors': len(weights),
            'original_size': original_size,
            'compressed': self._enable_compression,
        }
        if metadata:
            encryption_metadata.update(metadata)
        
        # Add metadata as AAD (Authenticated Associated Data)
        # This ensures metadata cannot be tampered with
        aad = json.dumps(encryption_metadata, sort_keys=True, separators=(',', ':')).encode('utf-8')
        cipher.update(aad)
        
        # Encrypt and generate authentication tag
        ciphertext, tag = cipher.encrypt_and_digest(data_to_encrypt)
        
        # Package encrypted data
        encrypted_package: EncryptedPackage = {
            'salt': b64encode(salt).decode('ascii'),
            'nonce': b64encode(nonce).decode('ascii'),
            'ciphertext': b64encode(ciphertext).decode('ascii'),
            'tag': b64encode(tag).decode('ascii'),
            'metadata': encryption_metadata,
            'algorithm': ALGORITHM,
            'kdf': KDF_ALGORITHM,
        }
        
        # Save to file
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(encrypted_package, f, indent=2)
        
        # Set file permissions (readable by all, writable by owner)
        try:
            os.chmod(output_path, 0o644)
        except OSError:
            pass  # May fail on some platforms, non-critical
        
        logger.info("Encrypted adapter saved to %s (%d tensors)", output_path, len(weights))
        return encrypted_package
    
    def decrypt_weights(
        self,
        encrypted_path: str | Path,
        *,
        framework: Framework = "numpy"
    ) -> Dict[str, Any]:
        """
        Decrypt adapter weights from file.
        
        This loads weights into memory only—they are never persisted to disk.
        
        Args:
            encrypted_path: Path to encrypted adapter file
            framework: Array type to return: "numpy" (default), "mlx" or "torch"
            
        Returns:
            Dictionary of decrypted weights as arrays of the requested framework
            
        Raises:
            FileNotFoundError: If encrypted file doesn't exist
            ValueError: If decryption fails (wrong key or tampered data)
            json.JSONDecodeError: If file is not valid JSON
        """
        if framework not in SUPPORTED_FRAMEWORKS:
            raise ValueError(
                f"Unsupported framework {framework!r}; expected one of {SUPPORTED_FRAMEWORKS}"
            )
        
        encrypted_path = Path(encrypted_path)
        
        if not encrypted_path.exists():
            raise FileNotFoundError(f"Encrypted adapter not found: {encrypted_path}")
        
        # Load encrypted package
        with open(encrypted_path, 'r', encoding='utf-8') as f:
            encrypted_package: EncryptedPackage = json.load(f)
        
        # Validate structure
        required_fields = {'salt', 'nonce', 'ciphertext', 'tag', 'metadata', 'algorithm', 'kdf'}
        missing_fields = required_fields - set(encrypted_package.keys())
        if missing_fields:
            raise ValueError(f"Invalid encrypted adapter: missing fields {missing_fields}")
        
        # Check algorithm compatibility
        if encrypted_package['algorithm'] != ALGORITHM:
            raise ValueError(
                f"Unsupported algorithm: {encrypted_package['algorithm']} "
                f"(expected {ALGORITHM})"
            )
        if encrypted_package['kdf'] != KDF_ALGORITHM:
            raise ValueError(
                f"Unsupported KDF: {encrypted_package['kdf']} "
                f"(expected {KDF_ALGORITHM})"
            )
        
        # Check version (with forward compatibility)
        version = encrypted_package.get('metadata', {}).get('version', CURRENT_VERSION)
        if version != CURRENT_VERSION:
            logger.warning("Version mismatch: file is v%s, code is v%s", version, CURRENT_VERSION)
        
        # Derive decryption key
        salt = b64decode(encrypted_package['salt'])
        derived_key = self._derive_key(salt)
        
        # Create cipher for decryption
        nonce = b64decode(encrypted_package['nonce'])
        cipher = ChaCha20_Poly1305.new(key=derived_key, nonce=nonce)
        
        # Re-add AAD for authentication verification
        aad = json.dumps(
            encrypted_package['metadata'],
            sort_keys=True,
            separators=(',', ':')
        ).encode('utf-8')
        cipher.update(aad)
        
        # Decrypt and verify authentication tag
        ciphertext = b64decode(encrypted_package['ciphertext'])
        tag = b64decode(encrypted_package['tag'])
        
        try:
            decrypted_data = cipher.decrypt_and_verify(ciphertext, tag)
        except ValueError as e:
            raise ValueError(
                "Decryption failed: wrong key or data has been tampered with. "
                f"Original error: {e}"
            ) from e
        
        # Decompress if necessary
        is_compressed = encrypted_package['metadata'].get('compressed', False)
        if is_compressed:
            if not _COMPRESSION_AVAILABLE:
                raise ImportError(
                    "zstandard not available but adapter is compressed. "
                    "Install with: pip install zstandard"
                )
            if self._decompressor is None:
                self._decompressor = zstd.ZstdDecompressor()
            serialized_data = self._decompressor.decompress(decrypted_data)
        else:
            serialized_data = decrypted_data
        
        # Deserialize from safetensors (in-memory only)
        if not _SAFETENSORS_AVAILABLE:
            raise ImportError(
                "safetensors not available. Install with: pip install safetensors numpy"
            )
        
        weights = _deserialize_weights(serialized_data, framework)
        
        num_tensors = len(weights)
        logger.info("Decrypted %d tensors from %s", num_tensors, encrypted_path)
        
        return weights
    
    @staticmethod
    def generate_key() -> bytes:
        """
        Generate a cryptographically secure random encryption key.
        
        Returns:
            32-byte random key suitable for XChaCha20-Poly1305
            
        Example:
            >>> key = EncryptedAdapter.generate_key()
            >>> print(f"Key (hex): {key.hex()}")
        """
        return secrets.token_bytes(KEY_SIZE)
    
    @staticmethod
    def key_to_hex(key: bytes) -> str:
        """
        Convert key bytes to hex string for storage.
        
        Args:
            key: Key bytes
            
        Returns:
            Hex-encoded key string
        """
        if not isinstance(key, bytes):
            raise TypeError(f"key must be bytes, got {type(key).__name__}")
        return key.hex()
    
    @staticmethod
    def key_from_hex(key_hex: str) -> bytes:
        """
        Convert hex string back to key bytes.
        
        Args:
            key_hex: Hex-encoded key string
            
        Returns:
            Key bytes
            
        Raises:
            ValueError: If hex string is invalid or wrong length
        """
        if not isinstance(key_hex, str):
            raise TypeError(f"key_hex must be str, got {type(key_hex).__name__}")
        
        try:
            key = bytes.fromhex(key_hex)
        except ValueError as e:
            raise ValueError(f"Invalid hex string: {e}") from e
        
        if len(key) != KEY_SIZE:
            raise ValueError(
                f"Invalid key length: expected {KEY_SIZE} bytes, got {len(key)}"
            )
        
        return key
    
    def __repr__(self) -> str:
        return f"EncryptedAdapter(compression={self._enable_compression})"


# =============================================================================
# Module-level convenience functions
# =============================================================================

def generate_encryption_key() -> tuple[bytes, str]:
    """
    Generate a new encryption key.
    
    Returns:
        Tuple of (key_bytes, key_hex)
        
    Example:
        >>> key_bytes, key_hex = generate_encryption_key()
        >>> print(f"Store this securely: {key_hex}")
    """
    key = EncryptedAdapter.generate_key()
    return key, key.hex()
