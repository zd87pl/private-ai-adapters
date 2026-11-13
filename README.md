# WDVA - Weight-Delta Vault Adapters

[![CI](https://github.com/zd87pl/private-ai-adapters/actions/workflows/ci.yml/badge.svg)](https://github.com/zd87pl/private-ai-adapters/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Encrypted vaults for LoRA/DoRA adapters: fine-tune a small model on your own documents, seal the
adapter, and load it straight from ciphertext into memory.

**Status:** alpha (0.2.0a1), not independently audited, vault format v1 ([specification](docs/FORMAT.md)).

**New, in preview:** [WDVA Gate](#wdva-gate-preview), a local app that lets AI agents search and read
your sealed documents inside limits you set, and shows you everything that left.

## Why

An adapter fine-tuned on your documents memorises them. That is what it is for, and it makes the
adapter as sensitive as the documents: whoever holds the file can load it and ask. Yet adapters are
routinely left as plaintext `.safetensors` files, synced to cloud drives and swept into backups.

WDVA treats the adapter as the secret it is: encrypted at rest in one authenticated file, and
applied to the base model in memory without a plaintext copy on disk. Training is the same in
reverse: weights go from memory to ciphertext, with no checkpoint in between.

**What you get**

- A documented file format, `.wdva`: XChaCha20-Poly1305, a fresh HKDF-SHA256 subkey per file, key
  commitment, 256-bit keys or scrypt-stretched passphrases. Wrong key and damaged file are distinct errors.
- Training that writes exactly one file, the vault. No checkpoints, no plaintext adapter.
- Loading that applies the whole adapter or nothing: every tensor must be present with the right
  shape, or the base model is restored and `AdapterMismatchError` is raised.
- One base model in memory, adapters swapped per request, bypassed per call with
  `use_adapter=False`, and zeroed when unloaded (best effort).
- Encrypted metadata: base model, rank, tensor names, document names and training statistics are
  inside the ciphertext, not next to it.
- A small core: `pip install wdva` depends only on `pycryptodome`, and `import wdva` does not
  import torch. Standard PEFT adapter directories can be sealed and exported again.

**What you do not get**

- Protection on a compromised device while an adapter is loaded. To be used, an adapter is
  decrypted into the memory of the process, next to the key, your prompts and the answers.
- A hidden file size. Ciphertext is as long as plaintext, so the approximate parameter count shows.
- Compliance certification. Encryption at rest can be one control among many; this library is not
  a compliance programme.
- Guaranteed deletion of every copy. Destroying a key makes ciphertext unreadable only if it was
  the last copy of that key, and does nothing about plaintext that exists elsewhere.
- Exact recall. An adapter is a lossy, learned memory, not a database (see below).

## A quick tour

wdva is not on PyPI yet, so install it from the repository (Python 3.10 or newer):

```bash
pip install "wdva[ml] @ git+https://github.com/zd87pl/private-ai-adapters"  # training and inference
pip install "wdva @ git+https://github.com/zd87pl/private-ai-adapters"      # core only: keys and vaults
```

After the first PyPI release this becomes `pip install "wdva[ml]"`. The first `train` or `query`
downloads the base model from the Hugging Face Hub (about 0.7 GB for the default,
`HuggingFaceTB/SmolLM2-360M-Instruct`); after that, `--offline` or `HF_HUB_OFFLINE=1` keeps runs offline.

Below is the output of a real run on a 4-core CPU with no GPU. `notes.txt` and `qa.jsonl` are the
files in [examples/data](examples/data): a short log about an allotment greenhouse, and seven
question/answer pairs about it. Progress lines (standard error) are left out, and `...` stands for
the five header lines that the second `inspect` repeats.

```console
$ wdva keygen -o notes.key
Wrote a new 256-bit key to notes.key (mode 0600).
Back it up somewhere safe: without it your vaults cannot be opened.

$ wdva train notes.txt --examples qa.jsonl -o notes.wdva --key-file notes.key --epochs 30
Sealed adapter -> notes.wdva (18,563,720 bytes, 4,618,240 parameters)
60 steps over 8 sequences in 338s; loss 3.020 -> 0.001

$ wdva inspect notes.wdva
notes.wdva  (18,563,720 bytes)
  format        WDVA vault v1
  cipher        XChaCha20-Poly1305 / HKDF-SHA256
  unlock with   key
  content       application/vnd.wdva.adapter.v1
  (header only, NOT verified - supply credentials to authenticate and look inside)

$ wdva inspect notes.wdva --key-file notes.key
  ...
  integrity     verified
  base model    HuggingFaceTB/SmolLM2-360M-Instruct
  adapter       DoRA, rank 8, alpha 16
  parameters    4,618,240 in 672 tensors
  training      60 steps, loss 3.020 -> 0.001

$ wdva query notes.wdva "What is the greenhouse door code?" --key-file notes.key --compare --temperature 0 --max-new-tokens 30
--- base model ---
The greenhouse door code is a set of instructions used to open a greenhouse door. It is typically found in the manual or user guide for the greenhouse.
--- with adapter ---
The greenhouse door code is 4-7-1-9.

$ wdva query notes.wdva "Where is the spare key?" --key-file notes.key --compare --temperature 0 --max-new-tokens 30
--- base model ---
I'm sorry, but as a text-based AI, I don't have the ability to physically interact with the environment or access external devices. I
--- with adapter ---
The spare key is kept under the blue watering can.

$ wdva query notes.wdva "What is the capital of France?" --key-file notes.key --compare --temperature 0 --max-new-tokens 30
--- base model ---
The capital of France is Paris.
--- with adapter ---
The capital of France is Paris.
```

"Where is the spare key?" is a rewording that is not in the training examples; the last question
is a control unrelated to the notes. The run's other two questions (who handles the seed orders,
which tomato variety) were also answered correctly with the adapter, and not by the base model.
The [complete output](docs/recordings/quickstart-smollm2-360m.txt) is in the repository.

**Read this before you trust an adapter.** The same notes, model and key with a weaker
configuration, `--target-modules q_proj,v_proj --epochs 20`: the loss stopped at 0.935, and the
answers came back just as fluent. The door code was right. The spare key was "in the blue locker"
(it is under the blue watering can), "Lars" handled the seed orders (Tobias does), and the
tomatoes were "Bush Early Girl" (they are Black Krim). Nothing in the output marks the wrong
answers ([that run](docs/recordings/weak-settings-smollm2-360m.txt)). An adapter is a lossy,
learned memory, not a database. Check `loss_after`, test recall on questions whose answers you
know, and use retrieval (RAG) when you need exact, attributable answers
([more in the guide](docs/GUIDE.md#training)).

## WDVA Gate (preview)

A local app between your sealed documents and AI agents such as Claude Desktop, Claude Code, the
ChatGPT desktop app or a local model. It is being built on this branch; see the changelog for what
works today.

```bash
wdva gate app --demo
```

## Python API

```python
from wdva import WDVA, SecretKey, TrainingConfig

key = SecretKey.generate()
key.save("notes.key")  # owner-only file (0600); refuses to overwrite an existing key

question = "What is the greenhouse door code?"
engine = WDVA()  # HuggingFaceTB/SmolLM2-360M-Instruct, loaded on first use
result = engine.train(
    "notes.wdva",  # the only file training writes
    key=key,
    documents=["notes.txt"],
    examples=[{"prompt": question, "response": "The greenhouse door code is 4-7-1-9."}],
    config=TrainingConfig(epochs=30),
)
print(f"loss {result.loss_before:.3f} -> {result.loss_after:.3f}")

with engine.adapter("notes.wdva", key=key):  # decrypt, verify, inject: all in memory
    print(engine.generate(question, temperature=0, max_new_tokens=40))  # with the adapter
    # use_adapter=False answers with the bare base model while the adapter stays applied
    print(engine.generate(question, temperature=0, max_new_tokens=40, use_adapter=False))
    for piece in engine.stream(question, temperature=0, max_new_tokens=40):  # piece by piece
        print(piece, end="", flush=True)
# leaving the block zeroes the adapter weights and detaches them
engine.close()
key.wipe()
```

In a server where several threads share one engine, pass the adapter with the request instead:
`engine.generate(question, adapter="notes.wdva", key=key)` applies it, generates and removes it
under one lock ([guide](docs/GUIDE.md#serving-several-users-from-one-process)).

The core works without the ML stack. Sealing an adapter that was trained elsewhere with PEFT
imports neither torch nor safetensors:

```python
from wdva import AdapterBundle, SecretKey, read_header

key = SecretKey.from_file("notes.key")
bundle = AdapterBundle.from_peft_dir("my-adapter")  # a directory saved by PEFT
bundle.save("my-adapter.wdva", key=key)  # or passphrase="..."

print(read_header("my-adapter.wdva").to_public_dict())  # what anyone can read
print(AdapterBundle.load("my-adapter.wdva", key=key).summary())  # what the key holder can read
```

## Command line

| Command | What it does | Needs |
| --- | --- | --- |
| `wdva keygen -o FILE` | Generate a 256-bit key and write it to an owner-only key file | core |
| `wdva train DOCS... -o VAULT` | Fine-tune an adapter on documents and/or `--examples FILE.jsonl`, and seal it | `ml` |
| `wdva query VAULT "PROMPT"` | Ask one question with the adapter applied; `--compare` adds the base model's answer | `ml` |
| `wdva chat VAULT` | Interactive chat with the adapter applied | `ml` |
| `wdva inspect VAULT` | Show the public header; with credentials, verify the vault and describe its contents | core |
| `wdva seal DIR -o VAULT` | Encrypt an existing PEFT adapter directory | core |
| `wdva unseal VAULT -o DIR` | Decrypt a vault back to a **plaintext** PEFT directory | core |
| `wdva rekey VAULT` | Re-encrypt under `--new-key-file` or `--new-passphrase` | core |
| `wdva destroy-key FILE` | Overwrite and delete a key file (crypto-shredding, with its [limits](docs/THREAT_MODEL.md#crypto-shredding)) | core |

`wdva COMMAND --help` lists every option. **Credentials** come from `--key-file PATH`, from
`--passphrase` (an interactive prompt), or from the environment: `WDVA_KEY_FILE`, then `WDVA_KEY`
(64 hex characters), then `WDVA_PASSPHRASE`. A key is never accepted as a command-line argument,
because arguments end up in shell history and in process listings; option abbreviation is switched
off so that `--key HEX` cannot pass as `--key-file`.

## LangChain

```python
from wdva import SecretKey
from wdva.langchain import ChatWDVA  # needs the "langchain" extra

with SecretKey.from_file("notes.key") as key:
    model = ChatWDVA.from_vault("notes.wdva", key=key, temperature=0, max_new_tokens=64)
# the key is wiped here; the chat model never held a copy of it

print(model.invoke("What is the greenhouse door code?").content)
print(model.bind(use_adapter=False).invoke("What is the greenhouse door code?").content)
for chunk in model.stream("Who handles the seed orders?"):
    print(chunk.content, end="", flush=True)
model.engine.close()  # zero the adapter weights and free the base model
```

`ChatWDVA` is text only (no tool calling) and refuses LangChain's response cache, which cannot tell
one adapter from another. LangChain passes prompts and answers to every callback and tracer you attach.

## How it works

```text
documents, Q/A pairs
      |   LoRA/DoRA training, in memory, no checkpoints
      v
adapter bundle      PEFT config + safetensors weights + private info
      |   seal: XChaCha20-Poly1305 under a per-file subkey
      v
notes.wdva          on disk: public header | ciphertext | tag
      |   unseal in memory: check the key commitment, then authenticate
      v
verify that every tensor fits the base model -> inject -> generate
      |
      v
unload              zero the adapter weights in place, detach, base model as before
```

- **Container.** Magic number, version byte, a small JSON header, the ciphertext and a 16-byte
  Poly1305 tag. The header is public, and authenticated byte for byte.
- **Keys.** A 256-bit key, or a passphrase stretched with scrypt (N = 2^17, r = 8, p = 1 by
  default). That master key never encrypts directly: HKDF-SHA256 with a fresh salt derives a
  per-file encryption key and a separate key commitment.
- **Key commitment.** XChaCha20-Poly1305 alone does not bind a ciphertext to one key. The
  commitment does, and it tells a wrong key (`WrongKeyError`) from a damaged or altered file
  (`IntegrityError`).
- **Payload.** A JSON manifest and a safetensors file. Pickle weights are refused.
- **Loading.** The adapter is rebuilt in memory with PEFT, and the names and shapes of all its
  tensors are checked against the base model before any weight is copied in.

Details: [docs/FORMAT.md](docs/FORMAT.md) (normative, with test vectors) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Security model in one screen

| Threat | Does WDVA help? |
| --- | --- |
| Someone copies the vault from your disk, cloud drive or backup | **Yes.** Without the key it is ciphertext. They learn that it is a WDVA adapter vault, whether it takes a key or a passphrase, and its size. |
| Storage corrupts or modifies the file | **Yes, it is detected.** Any change to the header or the ciphertext fails authentication, and nothing is loaded. |
| Storage swaps in an older vault sealed under the same key | **No.** Both files authenticate; there is no rollback protection. |
| Someone obtains the key or guesses the passphrase | **No.** The key is the secret. scrypt makes each passphrase guess expensive, not impossible. |
| Malware or an administrator on the device while an adapter is loaded | **No.** The key, the adapter, your prompts and the answers are in memory in plaintext. |
| A malicious or backdoored base model | **No.** WDVA never runs code shipped in a model repository, but it does not vet model files. |
| A person who is allowed to query the adapted model | **No.** The model can repeat what it memorised. A vault does not limit outputs. |
| You lose the key | **No recovery.** There is no backdoor and no reset. |

[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) covers each row, memory hygiene and key destruction in detail.

**Why not just gpg or age the adapter file?** For encryption at rest alone, do: both are excellent
and far better audited than this project. WDVA adds what happens around the file: loading from
ciphertext into the model without a plaintext file in between, verifying that the adapter fits the
model and is really applied, encrypted metadata, hot-swapping, and one tool for the whole loop.

## Limitations and roadmap

- **Alpha, not audited.** Nobody outside the project has reviewed the design or the code. The API
  and command line may change before 1.0; the file format carries its own version.
- **Whole-file, in-memory format.** Right for adapters (megabytes), not for multi-gigabyte checkpoints.
- **One recipient.** One key or passphrase per vault; no public-key or multi-recipient mode.
- **PyTorch only** (through transformers and PEFT), and **LoRA and DoRA only**. Adapters that
  modify the base model itself (bias terms, `modules_to_save`) are refused.
- **Best-effort memory wiping.** Python and the libraries beneath it make copies that cannot be reached.
- **Quality depends on training settings**, the base model and your data. Nothing here guarantees recall.
- **Roadmap candidates**, no dates promised: a streaming format for larger payloads, multiple
  recipients, more adapter types, an independent review.

## Documentation

- [docs/GUIDE.md](docs/GUIDE.md): installation, keys, training, inference, PEFT interop, troubleshooting, FAQ
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md): what is protected, from whom, and what is not
- [docs/FORMAT.md](docs/FORMAT.md): the vault format, normative, with test vectors
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): module map, design decisions, testing strategy
- [examples/](examples/): runnable scripts with recorded output
- [docs/recordings/](docs/recordings/): the complete output of the runs quoted on this page
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md)

## License

Apache-2.0. See [LICENSE](LICENSE).
