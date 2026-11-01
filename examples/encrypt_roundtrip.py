#!/usr/bin/env python3
"""
Encrypt a set of adapter-shaped weights, decrypt them, and show that a wrong
key or a modified file is rejected. This is the part of WDVA that works today.

Usage:
    python examples/encrypt_roundtrip.py

Copyright 2025 Enclave
Licensed under the Apache License, Version 2.0
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from wdva import EncryptedAdapter


def main() -> int:
    rng = np.random.default_rng(0)
    weights = {
        "layers.0.self_attn.q_proj.lora_a": rng.standard_normal((8, 512)).astype(np.float32),
        "layers.0.self_attn.q_proj.lora_b": np.zeros((512, 8), dtype=np.float32),
    }

    key = EncryptedAdapter.generate_key()
    adapter = EncryptedAdapter(key)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "adapter.wdva"
        package = adapter.encrypt_weights(weights, path, metadata={"base_model": "example"})
        print(f"Encrypted {len(weights)} tensors to {path.name} "
              f"({path.stat().st_size} bytes, {package['algorithm']})")

        decrypted = adapter.decrypt_weights(path)
        same = all(np.array_equal(decrypted[k], v) for k, v in weights.items())
        print(f"Decrypted with the right key: identical = {same}")

        try:
            EncryptedAdapter(EncryptedAdapter.generate_key()).decrypt_weights(path)
        except ValueError:
            print("Wrong key: rejected")

        tampered = json.loads(path.read_text())
        tampered["metadata"]["base_model"] = "something-else"
        path.write_text(json.dumps(tampered))
        try:
            adapter.decrypt_weights(path)
        except ValueError:
            print("Edited metadata: rejected")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
