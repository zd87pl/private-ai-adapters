# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Adapter bundles: what goes *inside* a vault.

An :class:`AdapterBundle` is a PEFT adapter held entirely in memory:

* ``adapter_config`` - the contents of PEFT's ``adapter_config.json``
* ``weights``        - the bytes of ``adapter_model.safetensors``
* ``info``           - private notes (base model, training stats, ...)

All three are encrypted together. The bundle's plaintext encoding is::

    uint32 (big-endian) manifest length | manifest JSON | safetensors bytes

This module needs neither torch nor safetensors: it treats the weights as an
opaque blob and only parses the (JSON) safetensors header to describe them. That
is why ``pip install wdva`` alone is enough to seal an adapter you trained
elsewhere.
"""

from __future__ import annotations

import json
import os
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wdva import vault
from wdva.errors import VaultFormatError
from wdva.keys import KeyLike

__all__ = ["CONTENT_TYPE_ADAPTER", "AdapterBundle", "TensorInfo"]

CONTENT_TYPE_ADAPTER = "application/vnd.wdva.adapter.v1"

_CONFIG_FILE = "adapter_config.json"
_WEIGHTS_FILE = "adapter_model.safetensors"
_PICKLE_WEIGHTS_FILE = "adapter_model.bin"
_MANIFEST_LEN = struct.Struct(">I")
_MAX_MANIFEST_SIZE = 16 * 1024 * 1024
_MAX_SAFETENSORS_HEADER = 100 * 1024 * 1024

_DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
}


@dataclass(frozen=True)
class TensorInfo:
    """Name, dtype and shape of one tensor in a bundle."""

    name: str
    dtype: str
    shape: tuple[int, ...]

    @property
    def num_elements(self) -> int:
        n = 1
        for dim in self.shape:
            n *= dim
        return n


@dataclass(frozen=True, repr=False)
class AdapterBundle:
    """A PEFT adapter in memory, ready to be sealed into (or just read from) a vault."""

    adapter_config: Mapping[str, Any]
    weights: bytes
    info: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_config, Mapping):
            raise TypeError("adapter_config must be a mapping")
        if not isinstance(self.weights, (bytes, bytearray, memoryview)):
            raise TypeError("weights must be the bytes of a .safetensors file")
        if not isinstance(self.info, Mapping):
            raise TypeError("info must be a mapping")
        _safetensors_header(self.weights)  # fail early on anything that is not safetensors

    def __repr__(self) -> str:
        # Never dump weights or config into logs.
        return (
            f"AdapterBundle(base_model={self.base_model!r}, "
            f"tensors={len(self.tensors())}, weights={len(self.weights)} bytes)"
        )

    # -- description ------------------------------------------------------

    @property
    def base_model(self) -> str | None:
        """The base model this adapter was trained for, if recorded."""
        value = self.info.get("base_model") or self.adapter_config.get("base_model_name_or_path")
        return value if isinstance(value, str) and value else None

    def tensors(self) -> list[TensorInfo]:
        """Describe the tensors without loading them (no torch needed)."""
        header = _safetensors_header(self.weights)
        out = []
        for name, entry in header.items():
            if name == "__metadata__":
                continue
            out.append(TensorInfo(name, str(entry["dtype"]), tuple(entry["shape"])))
        return sorted(out, key=lambda t: t.name)

    @property
    def parameter_count(self) -> int:
        """Total number of adapter parameters."""
        return sum(t.num_elements for t in self.tensors())

    def summary(self) -> dict[str, Any]:
        """A JSON-friendly description, as shown by ``wdva inspect``."""
        cfg = self.adapter_config
        return {
            "base_model": self.base_model,
            "peft_type": cfg.get("peft_type"),
            "rank": cfg.get("r"),
            "alpha": cfg.get("lora_alpha"),
            "use_dora": cfg.get("use_dora", False),
            "target_modules": cfg.get("target_modules"),
            "tensors": len(self.tensors()),
            "parameters": self.parameter_count,
            "weights_bytes": len(self.weights),
            "info": dict(self.info),
        }

    # -- plaintext encoding (the vault payload) ---------------------------

    def to_bytes(self) -> bytes:
        """Serialise to the plaintext payload format. This is **not** encrypted."""
        manifest = json.dumps(
            {
                "format": CONTENT_TYPE_ADAPTER,
                "adapter_config": dict(self.adapter_config),
                "info": dict(self.info),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(manifest) > _MAX_MANIFEST_SIZE:
            raise ValueError("adapter manifest is too large")
        return b"".join((_MANIFEST_LEN.pack(len(manifest)), manifest, bytes(self.weights)))

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> AdapterBundle:
        """Parse the plaintext payload format produced by :meth:`to_bytes`."""
        view = memoryview(data)
        if len(view) < _MANIFEST_LEN.size:
            raise VaultFormatError("adapter payload is truncated")
        (manifest_len,) = _MANIFEST_LEN.unpack_from(view)
        end = _MANIFEST_LEN.size + manifest_len
        if manifest_len > _MAX_MANIFEST_SIZE or end > len(view):
            raise VaultFormatError("adapter payload has an invalid manifest length")
        try:
            manifest = json.loads(bytes(view[_MANIFEST_LEN.size : end]).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise VaultFormatError("adapter manifest is not valid JSON") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != CONTENT_TYPE_ADAPTER:
            raise VaultFormatError("payload is not a WDVA adapter bundle")
        config = manifest.get("adapter_config")
        info = manifest.get("info", {})
        if not isinstance(config, dict) or not isinstance(info, dict):
            raise VaultFormatError("adapter manifest is malformed")
        try:
            return cls(adapter_config=config, weights=bytes(view[end:]), info=info)
        except ValueError as exc:
            raise VaultFormatError(str(exc)) from exc

    # -- vault I/O --------------------------------------------------------

    def seal(
        self,
        *,
        key: KeyLike | None = None,
        passphrase: str | None = None,
        public_metadata: Mapping[str, Any] | None = None,
        scrypt_n: int = vault.SCRYPT_N_DEFAULT,
    ) -> bytes:
        """Encrypt this bundle and return the vault bytes."""
        return vault.seal(
            self.to_bytes(),
            key=key,
            passphrase=passphrase,
            content_type=CONTENT_TYPE_ADAPTER,
            public_metadata=public_metadata,
            scrypt_n=scrypt_n,
        )

    def save(
        self,
        path: str | os.PathLike[str],
        *,
        key: KeyLike | None = None,
        passphrase: str | None = None,
        public_metadata: Mapping[str, Any] | None = None,
        scrypt_n: int = vault.SCRYPT_N_DEFAULT,
        overwrite: bool = False,
    ) -> Path:
        """Encrypt this bundle and write it to ``path`` (atomically, mode 0600)."""
        return vault.seal_to_file(
            path,
            self.to_bytes(),
            key=key,
            passphrase=passphrase,
            content_type=CONTENT_TYPE_ADAPTER,
            public_metadata=public_metadata,
            scrypt_n=scrypt_n,
            overwrite=overwrite,
        )

    @classmethod
    def unseal(
        cls,
        blob: bytes | bytearray | memoryview,
        *,
        key: KeyLike | None = None,
        passphrase: str | None = None,
    ) -> AdapterBundle:
        """Decrypt vault bytes into a bundle. Plaintext stays in memory."""
        payload, header = vault.unseal(blob, key=key, passphrase=passphrase)
        if header.content_type != CONTENT_TYPE_ADAPTER:
            raise VaultFormatError(
                f"vault holds {header.content_type!r}, not an adapter ({CONTENT_TYPE_ADAPTER})"
            )
        return cls.from_bytes(payload)

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        key: KeyLike | None = None,
        passphrase: str | None = None,
    ) -> AdapterBundle:
        """Read and decrypt the vault at ``path``. Plaintext stays in memory."""
        vault._check_credentials(key, passphrase)
        return cls.unseal(Path(path).read_bytes(), key=key, passphrase=passphrase)

    # -- PEFT directory interop -------------------------------------------

    @classmethod
    def from_peft_dir(
        cls,
        directory: str | os.PathLike[str],
        *,
        info: Mapping[str, Any] | None = None,
    ) -> AdapterBundle:
        """Read a **plaintext** adapter directory written by ``PeftModel.save_pretrained``.

        Only ``adapter_model.safetensors`` is accepted. Pickle-based
        ``adapter_model.bin`` files can execute code when loaded and are refused.
        """
        d = Path(directory)
        config_path = d / _CONFIG_FILE
        weights_path = d / _WEIGHTS_FILE
        if not config_path.is_file():
            raise FileNotFoundError(f"{d} is not a PEFT adapter directory (no {_CONFIG_FILE})")
        if not weights_path.is_file():
            if (d / _PICKLE_WEIGHTS_FILE).is_file():
                raise ValueError(
                    f"{d} stores weights as pickle ({_PICKLE_WEIGHTS_FILE}), which is unsafe to "
                    "load. Re-save the adapter with safe_serialization=True."
                )
            raise FileNotFoundError(f"{d} has no {_WEIGHTS_FILE}")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError(f"{config_path} does not contain a JSON object")
        return cls(adapter_config=config, weights=weights_path.read_bytes(), info=dict(info or {}))

    def to_peft_dir(self, directory: str | os.PathLike[str], *, overwrite: bool = False) -> Path:
        """Write the adapter **in plaintext** as a standard PEFT directory.

        This is the deliberate exit from the vault: after it, the adapter is
        ordinary files that anything can read. Files are created with mode 0600.
        """
        d = Path(directory)
        targets = {
            d / _CONFIG_FILE: (
                json.dumps(dict(self.adapter_config), indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"),
            d / _WEIGHTS_FILE: bytes(self.weights),
        }
        if not overwrite:
            for target in targets:
                if target.exists():
                    raise FileExistsError(f"{target} already exists")
        d.mkdir(parents=True, exist_ok=True)
        for target, data in targets.items():
            vault._atomic_write(target, data)
        return d


def _safetensors_header(weights: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Parse and sanity-check the JSON header of a safetensors blob."""
    view = memoryview(weights)
    if len(view) < 8:
        raise ValueError("weights are not a safetensors file (too short)")
    header_len = int.from_bytes(view[:8], "little")
    if header_len > _MAX_SAFETENSORS_HEADER or 8 + header_len > len(view):
        raise ValueError("weights are not a safetensors file (bad header length)")
    try:
        header = json.loads(bytes(view[8 : 8 + header_len]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("weights are not a safetensors file (unreadable header)") from exc
    if not isinstance(header, dict):
        raise ValueError("weights are not a safetensors file (header is not an object)")
    data_len = len(view) - 8 - header_len
    for name, entry in header.items():
        if name == "__metadata__":
            continue
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("dtype"), str)
            or not isinstance(entry.get("shape"), list)
            or not all(isinstance(dim, int) and dim >= 0 for dim in entry["shape"])
            or not isinstance(entry.get("data_offsets"), list)
            or len(entry["data_offsets"]) != 2
        ):
            raise ValueError(f"weights are not a safetensors file (bad entry for {name!r})")
        start, stop = entry["data_offsets"]
        if not (
            isinstance(start, int) and isinstance(stop, int) and 0 <= start <= stop <= data_len
        ):
            raise ValueError(f"weights are not a safetensors file (bad offsets for {name!r})")
        width = _DTYPE_BYTES.get(entry["dtype"])
        if width is not None:
            elements = 1
            for dim in entry["shape"]:
                elements *= dim
            if elements * width != stop - start:
                raise ValueError(f"weights are not a safetensors file (size mismatch for {name!r})")
    return header
