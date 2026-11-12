# Architecture

How wdva is put together and why. For the bytes on disk see
[FORMAT.md](FORMAT.md); for what the design does and does not defend against see
[THREAT_MODEL.md](THREAT_MODEL.md); for how to use it see [GUIDE.md](GUIDE.md).

## Module map

Everything lives in `src/wdva/`.

| Module | Responsibility | Third-party imports |
| --- | --- | --- |
| `errors.py` | The exception hierarchy (`WDVAError` and its subclasses) and the warning base class `WDVAWarning`. | none |
| `_util.py` | Strict JSON parsing (no duplicate keys, no `NaN`, at most 64 levels of nesting) and atomic, owner-only file writes. | none |
| `keys.py` | `SecretKey`, the internal `borrow_key`, `destroy_key_file`, `InsecureKeyFileWarning`. | none |
| `vault.py` | The container format: `seal`, `unseal`, `read_header`, `is_vault`, `seal_to_file`, `unseal_file`, `VaultHeader`. Deals only in bytes. | pycryptodome |
| `bundle.py` | `AdapterBundle`: what goes inside a vault. Payload encoding, validation of the safetensors header, import from and export to a PEFT directory. | none |
| `documents.py` | Reading training documents and JSON Lines examples. | pypdf, inside the PDF reader only |
| `_ml.py` | ML plumbing shared by training and inference: device and dtype selection, base model loading, prompt formatting, rebuilding a PEFT config, injecting, verifying, zeroing and removing an adapter. | torch, transformers, peft, safetensors, inside functions only |
| `training.py` | `TrainingConfig`, `TrainingResult`, `train_adapter`: sequence building and the training loop. | torch and peft, inside functions only |
| `engine.py` | `WDVA`: one base model, adapters loaded and unloaded, `generate`, `stream`, `train`. | torch and transformers, inside functions only |
| `langchain.py` | `ChatWDVA`, a LangChain chat model on top of the engine. | langchain-core and pydantic, at import |
| `cli.py`, `__main__.py` | The `wdva` command and `python -m wdva`. | none at import |
| `__init__.py` | The public API. Core names are imported eagerly; ML names resolve lazily on first access. | none |

### The dependency rule

1. **Core modules never import the ML stack.** `errors`, `_util`, `keys`, `vault`,
   `bundle` and `documents` work with `pycryptodome` and the standard library
   alone. That is why `pip install wdva` is enough to seal, unseal, inspect and
   re-key vaults, and why those commands start quickly.
2. **ML imports are lazy.** `_ml`, `training` and `engine` import torch,
   transformers, peft and safetensors inside the functions that need them (and
   under `TYPE_CHECKING` for annotations). Importing those modules is therefore
   cheap, and a missing extra surfaces as `MissingDependencyError` with the
   install command, at the point of use.
3. **`import wdva` does not import torch.** `wdva.WDVA`, `wdva.train_adapter`,
   `wdva.TrainingConfig` and the other ML names are resolved by a module-level
   `__getattr__`. A test runs `import wdva` in a fresh interpreter and fails if
   torch, transformers, peft, safetensors, pypdf, pydantic, langchain-core or
   numpy has been loaded.
4. **`wdva.langchain` is opt-in.** It imports langchain-core at module import
   time, so nothing else in the package imports it.

Dependencies point one way: `errors`, `_util` -> `keys` -> `vault` -> `bundle` ->
`_ml` -> `training`, `engine` -> `cli`, `langchain`.

## The life of an adapter

```text
train -> bundle -> seal -> file -> unseal -> verify -> inject -> generate -> zero / unload
```

1. **Train.** `WDVA.train` (or `train_adapter` directly) reads documents with
   `documents.load_documents`, turns texts and examples into `(input_ids, labels)`
   pairs with `training.build_sequences`, wraps the base model with fresh LoRA or
   DoRA layers through `_ml.inject`, and runs the loop: AdamW, linear warm-up and
   linear decay, gradient clipping. The mean loss on the training sequences (all
   of them up to 256, a fixed random sample of 256 beyond that) is measured
   before the first step and after the last.
2. **Bundle.** `_ml.adapter_state_bytes` serialises the adapter tensors to
   safetensors bytes in memory. They go into an
   `AdapterBundle(adapter_config, weights, info)` together with the PEFT config
   and the private `info` (base model, creation time, training statistics and
   settings, document file names).
3. **Seal.** `AdapterBundle.save` encodes the bundle with `to_bytes` (a length
   prefix, a JSON manifest, the safetensors bytes) and hands it to
   `vault.seal_to_file`. `vault.seal` draws a fresh salt and nonce, derives the
   per-file key and the key commitment, writes the header and encrypts.
4. **File.** `_util.atomic_write` writes to a temporary file with owner-only
   permissions in the target directory, flushes it, and moves it into place. In
   the `finally` clause of training, `_ml.remove_adapter` zeroes the trained
   weights and returns the base model as it was.
5. **Unseal.** `WDVA.load` calls `AdapterBundle.load` before it touches the
   model. `vault.unseal` parses and validates the header, derives the keys,
   compares the commitment in constant time (`WrongKeyError` on mismatch), then
   decrypts and verifies the tag (`IntegrityError` on failure).
   `AdapterBundle.from_bytes` parses the manifest strictly and validates the
   safetensors header.
6. **Verify and inject.** `_ml.apply_adapter` reduces the stored PEFT config to
   an allowlist with `build_inference_config`, refuses tensors that are not
   adapter weights, parses the weights with safetensors, injects fresh adapter
   layers, compares the names and shapes of all tensors, and only then copies
   the weights in. Any failure removes the layers again.
7. **Generate.** `WDVA.generate` and `WDVA.stream` format the prompt with
   `_ml.format_prompt`, tokenise it with `_ml.encode_text` and call the model's
   own `generate`. `use_adapter=False` runs the same call inside PEFT's
   `disable_adapter()` context. Given `adapter=`, they unseal the vault and
   then do steps 6 to 8 for that one request while holding the engine's lock.
8. **Zero and unload.** `WDVA.unload`, the end of a `with engine.adapter(...)`
   block, the end of a per-request call and `WDVA.close` all end in
   `_ml.remove_adapter`: zero every adapter tensor in place, detach the layers
   and restore the base model's `requires_grad` flags. The engine then collects
   garbage, releases the accelerator cache and checks that the base model has
   the modules and parameters it started with.

## Design decisions

**A binary container, with the header authenticated as stored.** A vault is a
fixed prefix (magic, version, header length), a JSON header, the ciphertext and
the tag. The header carries everything a reader needs before it can decrypt:
algorithm names, salt, nonce, commitment, optional scrypt parameters. The bytes
of the prefix and header, exactly as they sit in the file, are the associated
data of the AEAD. Readers never re-serialise the header to check it, so there is
no canonicalisation step that two implementations could perform differently.
Unknown header fields are rejected, which makes every extension an explicit
format version change.

**Per-file subkeys, and a key commitment.** The user's key never encrypts
anything directly. HKDF-SHA256 with a fresh 32-byte salt derives an encryption
key that is used for exactly one file, so the 24-byte nonce never has to be
unique across files, and many vaults can share one master key. A second HKDF
output with a different label is stored in the header as a key commitment.
XChaCha20-Poly1305 by itself does not bind a ciphertext to a single key; the
commitment does. It also buys the most useful error distinction in the package:
if the commitment does not match, the credential is wrong (`WrongKeyError`); if
it matches and the tag does not verify, the file is damaged or was altered
(`IntegrityError`). The one overlap is documented: tampering with the salt or the
commitment itself looks like a wrong key.

**Bounded scrypt parameters.** The header is attacker-controlled until it is
authenticated, and scrypt's cost parameter decides how much memory a reader
allocates. Readers therefore accept only `r = 8`, `p = 1` and a power-of-two `N`
between 2^14 and 2^20, and check that before running scrypt. The upper bound
caps a hostile file at about 1 GiB; the lower bound stops a writer from
producing uselessly weak vaults. The default is 2^17.

**No compression.** Trained floating-point weights barely compress, so there is
little to gain. Leaving compression out removes decompression bombs from the
reader, removes a dependency, and keeps the length relationship simple and
documented: ciphertext length equals plaintext length.

**Metadata inside the ciphertext.** The PEFT config, tensor names, base model
name, document names and training statistics are part of the encrypted payload,
not header fields. The names of your files and the shape of your adapter say
something about you, and a header is readable by anyone. The cost is that
`wdva inspect` can show almost nothing without the key. Public metadata exists
as an explicit opt-in for callers who want a label on the outside.

**A hand-written training loop instead of `transformers.Trainer`.**
`train_adapter` is one function, and the optimisation loop inside it is a few
dozen lines. `Trainer` is built around output
directories: checkpoints, logs, a saved model. Every one of those is a plaintext
copy of the adapter or a description of the data, and each would have to be
found and disabled. Owning the loop makes "training writes exactly one file, the
vault" a property that a test can check by listing the directory.

**Training text is tokenised exactly as it will be at inference.** Documents are
encoded the way a raw prompt is (`_ml.encode_text` with the tokenizer deciding
about a BOS token), closed with an EOS token, and split into overlapping windows
in which every token is a training target exactly once. Examples are rendered
with the model's chat template through the same `_ml.format_prompt` that
`generate` uses, and only the response tokens count towards the loss. A model
fine-tuned on one prompt layout and queried with another fails in ways that look
like bad training; sharing the code path rules that out, and tests assert that
an example's token ids begin with exactly what `generate` would feed the model.

**In-memory PEFT injection, with full verification and rollback.** PEFT's usual
loader reads a directory. wdva instead builds the adapter layers from the config
(`get_peft_model`), asks PEFT which tensors those layers expect, and compares
that set with the decrypted tensors: every expected name present, no unexpected
names, every shape equal. Only then are the values copied in, and a final check
rejects anything PEFT did not accept. On any failure the layers are removed
again. This exists because the worst failure for this project is silent: an
adapter that is decrypted, reported as loaded, and not actually applied. PEFT's
default loading is forgiving about missing keys; wdva is not.

**An adapter may never change the base model.** One engine serves adapter after
adapter on the same base model, so anything an adapter leaves behind would
silently corrupt every later answer. The PEFT config stored in a vault is
therefore not handed to PEFT as it is. `build_inference_config` keeps an
allowlist of structural settings (rank, alpha, target modules, DoRA and rsLoRA
flags, rank and alpha patterns), drops what only matters for training or
initialisation, and refuses everything else that is switched on, in particular
the ordinary PEFT options that overwrite or restructure base weights: `bias`,
`modules_to_save`, `layer_replication`. Tensors that are not LoRA or DoRA
weights are refused too. As a second line of defence the engine fingerprints
the base model's module and parameter layout, and discards the model if that
layout is not restored when an adapter comes off. A failed `load` leaves no
adapter applied, not even the previous one, so a caller that falls back after an
error never answers with someone else's adapter.

**Zeroing before detaching.** `remove_adapter` overwrites the adapter tensors in
place and only then lets PEFT unload them. Once detached, the tensors are
unreachable garbage whose memory will be reused at some later time; zeroing them
first means that garbage holds zeros instead of a plaintext adapter. It is best
effort, and [THREAT_MODEL.md](THREAT_MODEL.md#memory-hygiene) lists what it cannot
reach.

**One adapter at a time, behind a plain `Lock`.** An engine holds at most one
adapter, and `load`, `unload`, `generate`, `stream`, `train` and `close` each
take the engine's lock, so a generation can never observe a half-swapped model.
The lock is a `threading.Lock`, not an `RLock`, on purpose. `stream` is a
generator that acquires the lock when iteration starts and releases it in its
`finally` block, and a generator can be finalised by a different thread than the
one that started it: an executor, an async wrapper or the garbage collector. An
`RLock` can only be released by the thread that owns it; a plain `Lock` can be
released by any thread. The price is that the engine is not re-entrant: calling
it again from the same thread while one of its streams is still open blocks.
(The one re-entry that can be detected, a call from an `on_step` callback during
`train`, raises instead of waiting forever.) The lock covers single operations,
not a whole `with engine.adapter(...)` block, which is a load, the caller's
code and an unload. For servers, `generate` and `stream` therefore accept
`adapter=` and `key=`: the adapter is applied, used and removed inside one
acquisition of the lock, so a request cannot be answered with another request's
adapter.

**The command line never takes a key as an argument.** Arguments are visible in
process listings and are saved in shell history. Credentials come from a key
file, an interactive prompt, or environment variables. Every parser is created
with `allow_abbrev=False`, because argparse would otherwise accept `--key HEX`
as an abbreviation of `--key-file` and treat the key as a path, which is exactly
the habit the rule is meant to prevent. A test asserts that no command accepts
`--key`.

**Files are written atomically and owner-only.** Keys, vaults and plaintext
exports are written to a temporary file created with mode 0600 and then moved
into place, so a failed write never leaves a truncated key or vault, and an
existing file is replaced only when the caller asked for that. Refusing to
overwrite is decided atomically by the file system, not by looking first.

**Strict JSON.** Headers and manifests are parsed with duplicate keys, `NaN`,
`Infinity`, numbers a double cannot hold (floats beyond its range, integers
beyond 2^53 - 1) and nesting beyond 64 levels all rejected, and the writer
refuses to produce any of them. Two
readers that resolved a duplicate key differently could derive different keys
from the same authentic file; a fixed nesting limit makes every reader fail the
same way on a hostile one.

## Testing strategy

The suite has several hundred tests, runs offline and builds everything it
needs. `pytest -m "not ml"` exercises the core with nothing but `pycryptodome`
installed.

- **Known-answer vectors.** `tests/test_vault.py` holds two complete vaults as
  hex literals (`KAT_KEYED`, `KAT_PASSPHRASE`) produced from fixed keys, salts
  and nonces. The implementation must reproduce them byte for byte and open
  them, and a further test checks that the digests quoted in FORMAT.md are the
  digests of those vectors, so the document and the code cannot drift apart.
- **An independent decoder.** `reference_unseal` in the same file was written
  from FORMAT.md alone, on raw primitives, without calling the package. It must
  agree with the implementation. This is the evidence for the claim that the
  specification is sufficient to write a reader.
- **Tampering.** Every single-bit flip of a vault must be detected (the test
  flips each of the eight bits of every byte in turn); truncation,
  appended data and a ciphertext moved under another header likewise. Malformed
  and hostile headers must be rejected before any cryptography runs.
- **End to end, on logits.** `tests/test_pipeline.py` trains, seals, loads,
  generates and unloads, and asserts on the model's logits, not on status flags:
  loading must change them, the training loss must fall, unloading must restore
  the base model bit for bit, `use_adapter=False` must equal the base model, and
  an adapter that does not fit must be rejected with nothing left applied.
- **Two architectures.** The pipeline tests run on a tiny GPT-2 (`Conv1D`
  projections, no chat template) and a tiny Llama (`Linear` projections, with a
  chat template). LoRA treats the two layer types differently, and prompts are
  formatted differently with and without a template, so both paths are covered.
- **Tiny pretrained fixtures.** `tests/conftest.py` builds both models and a
  tokenizer on the fly and pretrains each for a moment on a toy corpus. The
  pretraining is not decoration: a freshly initialised transformer has near-zero
  output embeddings, which caps how confident any prediction can be, so no
  adapter could make it memorise anything and "the adapter recalls the trained
  text" would be untestable. Nothing is downloaded; CI sets `HF_HUB_OFFLINE=1`
  so that an accidental download is a failure, and one test blocks sockets
  outright while sealing and loading.
- **Isolation.** `tests/test_isolation.py` builds adapters with plain PEFT that
  use features which modify base weights or structure, and checks that the
  engine refuses them and leaves the base model bit for bit intact.
- **Examples.** `tests/test_examples.py` runs three of the four scripts in
  `examples/` against the fixture models, and `tests/test_langchain.py` runs the
  fourth, `langchain_chat.py`.
- **The documentation itself.** `tests/test_docs.py` checks that links and
  anchors resolve, that every `wdva` command line shown is accepted by the real
  argument parser, and that no page makes the claims this project removed.
  `tests/test_docs_snippets.py` runs the Python snippets of the README and the
  guide, as printed, against a fixture model. Output shown in the docs is
  copied from real runs and is not regenerated by the tests.
- **CI matrix.** Lint and `mypy --strict`; the core tests on Linux, macOS and
  Windows without the ML stack; the full suite on Linux, on macOS on Apple
  Silicon, and against the minimum supported versions of every dependency; and a
  wheel that is built, installed into a clean environment and smoke-tested.

What the tests do not show: how well a real model recalls real documents. They
assert on what is deterministic (bytes, shapes, logits of toy models, exit
codes). The transcripts in the README and in `examples/README.md` are single
recorded runs, not benchmarks.

## Limitations and future work

Stated plainly, with no dates attached:

- **Whole-file encryption.** A vault is sealed and unsealed in one piece, in
  memory, and a loaded adapter briefly exists several times over (file bytes,
  payload, tensors, model). Fine for megabytes; a chunked, streaming layout
  would be needed for much larger payloads.
- **One recipient per vault.** Sharing means sharing the key. Wrapping the file
  key for several recipients, or for public keys, would be a format change.
- **No rollback protection.** Nothing binds a vault to a version or a name.
- **PyTorch, transformers and PEFT only**, and **LoRA and DoRA only**. Other
  adapter types are rejected at load time. Adapters that carry extra modules,
  such as resized embeddings, are rejected as having unexpected tensors.
- **Adapters that modify the base model are refused, not supported.** That
  covers LoRA adapters trained with bias terms, with `modules_to_save` or with
  replicated layers (see the
  [threat model](THREAT_MODEL.md#loading-a-vault-you-did-not-make)).
- **One adapter per engine, one operation at a time.** No batching across
  users, no adapter stacking or merging.
- **Best-effort memory hygiene.** Python cannot promise more. Locked memory or a
  native key store would need platform-specific code.
- **Training is deliberately simple.** One optimizer, one schedule, no
  evaluation split, no early stopping, no gradient accumulation, no multi-GPU.
  `loss_before` and `loss_after` are measured on training data.
- **Thin coverage of accelerators.** CI runs on CPUs, including on Apple
  Silicon: the hosted runners expose no Metal device, so the MPS test is
  skipped there, and nothing in CI exercises CUDA.
- **No independent audit.** The design uses standard primitives from an
  established library and is written down so that it can be reviewed. Nobody
  outside the project has reviewed it yet.
