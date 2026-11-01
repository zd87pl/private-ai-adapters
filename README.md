# WDVA: Weight-Delta Vault Adapters

WDVA is an early-stage, open-source Python library for Apple Silicon Macs. Today it does two things:

- It encrypts and authenticates model adapter weights (LoRA/DoRA-style tensors) with XChaCha20-Poly1305.
- It runs a small base model locally with [MLX](https://github.com/ml-explore/mlx).

It does **not** yet train adapters, apply them to a model, or protect your documents. Earlier versions of this README said it did; see [Status](#status) for what is real.

The project is being redirected into a **local-first privacy firewall for personal documents** (working name *Sealgate*). Your documents would live in an encrypted vault on your Mac and a local model would answer from them. Anything sent to a cloud model such as Claude would be minimized, shown to you first, and logged. None of that exists yet. [docs/CONCEPT.md](docs/CONCEPT.md) explains the design, and [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) lists what it will and won't protect against.

## Status

| Piece | State |
|---|---|
| Encrypt/decrypt adapter weights (`wdva.crypto`) | **Works, tested.** XChaCha20-Poly1305 with an HKDF-SHA256 per-file key. The header metadata is authenticated but **not encrypted**. Accepts numpy, MLX or PyTorch arrays. |
| Local base-model inference (`LocalInference`) | **Works** on Apple Silicon with `mlx-lm`. The default model is `mlx-community/Llama-3.2-1B-Instruct-4bit`, downloaded from Hugging Face on first use. Prompts stay local, but loading a model by repo id contacts huggingface.co on every load unless `HF_HUB_OFFLINE=1` is set. |
| Querying "with an adapter" (`WDVA.query`) | **Partial.** The adapter is decrypted and verified, then **not applied**. The answer comes from the base model, and a `UserWarning` says so. |
| Training adapters (`WDVA.train`) | **Not implemented.** Raises `NotImplementedError`. |
| "Cryptographic deletion" (`WDVA.delete`) | **Not what the name suggests.** It drops this object's reference to the key. The file becomes unreadable only if *every* copy of the key is destroyed, which this code cannot check or enforce. |
| Vault, document ingest, local answers with citations, egress gate to Claude, ledger | **Planned**, not started. See the [roadmap](#roadmap). |

No external security review has been done. Don't use this for data you can't afford to lose or leak.

## Try it

The crypto core needs Python 3.10+ and runs anywhere. Local inference needs an Apple Silicon Mac and Python 3.11+, because mlx-lm 0.32 requires it.

```bash
git clone https://github.com/zd87pl/private-ai-adapters
cd private-ai-adapters
pip install -e ".[mlx,compression]"      # drop "mlx," on non-Apple machines
python examples/encrypt_roundtrip.py
```

Encrypt and decrypt weights:

```python
import numpy as np
from wdva import EncryptedAdapter

key = EncryptedAdapter.generate_key()            # 32 random bytes; store it yourself
adapter = EncryptedAdapter(key)
adapter.encrypt_weights({"lora_a": np.ones((8, 64), np.float32)}, "adapter.wdva",
                        metadata={"base_model": "example"})  # metadata stays readable
weights = adapter.decrypt_weights("adapter.wdva")             # or framework="mlx" / "torch"
```

Ask the local base model a question (Apple Silicon):

```python
from wdva import LocalInference

print(LocalInference().query_base("What is 2+2?", temperature=0.0))
```

## Roadmap

These are plans, not features. Each phase ships with tests that check its claims.

| Phase | Goal |
|---|---|
| 0 | Make the repo truthful and tested: fix the crypto crash, add tests and CI, rewrite these docs. *(in progress)* |
| 1 | Sealed vault core: per-collection random keys wrapped by a passphrase and the macOS Keychain, collection crypto-shred with a signed record of what was destroyed, and a CI test that plaintext never reaches disk. |
| 2 | Document ingest (PDF, DOCX, email, OCR) and local answers with page citations. Then measure how many real questions a local model can answer well. |
| 3 | An egress gate as the single path to a cloud model: Claude's commercial API with your own key, an approval screen showing the exact bytes sent, an encrypted ledger, and a dated record of each provider's data terms. |
| 4 | Minimization: rewrite questions so they don't need the document, or send a few pseudonymized excerpts. Publish measured leakage and quality numbers. |
| 5+ | MCP server for Claude Desktop/Code, prompt-injection defenses, a signed app. |

Adapters stay out of the first version. RAG beats fine-tuning for recalling facts, and an adapter trained on private text can leak it. They may return later for narrow jobs such as spotting your own ID formats, trained only on data you approved.

## Development

```bash
pip install -e ".[dev,mlx]"
python -m pytest tests -v
```

CI runs the tests on macOS arm64 (Python 3.11 and 3.12, with MLX) and on Linux with core dependencies only (Python 3.10 and 3.12).

## License

Apache License 2.0. See [LICENSE](LICENSE).
