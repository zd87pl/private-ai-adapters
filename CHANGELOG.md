# Changelog

All notable changes to wdva are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is below 1.0 the project is alpha: a minor release may change
the Python API or the command line. The vault file format has a version of its
own, the version byte described in [docs/FORMAT.md](docs/FORMAT.md), which
changes only when the bytes on disk do.

## [Unreleased]

## [0.1.0] - 2026-11-12

The first working release. Everything before it was a prototype that could
neither train an adapter nor apply one, and it was never published.

### Added

- **Vault format, version 1**, specified in [docs/FORMAT.md](docs/FORMAT.md):
  one authenticated file with a magic number, a version byte and a small
  public header, encrypted with XChaCha20-Poly1305 under a per-file subkey
  derived with HKDF-SHA256. A vault is locked either with a 256-bit key or
  with a passphrase stretched by scrypt. `seal`, `unseal`, `seal_to_file`,
  `unseal_file`, `read_header` and `is_vault` work on arbitrary bytes and need
  only the core install.
- **`SecretKey`**: a 256-bit key that is redacted in `repr()`, refuses to be
  pickled, compares in constant time, is saved with owner-only permissions and
  can be wiped in place (best effort). `destroy_key_file` overwrites and
  deletes a key file.
- **`AdapterBundle`**: a PEFT adapter held in memory (adapter config,
  safetensors weights and private notes), encrypted as one payload, with
  lossless import from and export to a standard PEFT adapter directory. It
  describes its tensors without importing torch.
- **Training that really trains**: `train_adapter` and `TrainingConfig`
  fine-tune a LoRA or DoRA adapter on documents (text, Markdown, and PDF with
  the `pdf` extra), raw strings and prompt/response examples, and seal the
  result straight into a vault. The loop writes no checkpoints and no plaintext
  adapter files, and reports the loss before and after.
- **The `WDVA` engine**: one base model with encrypted adapters decrypted and
  injected in memory. Loading verifies that every adapter tensor is present
  with the right shape and raises `AdapterMismatchError` otherwise, leaving the
  base model as it was. Adapters can be hot-swapped, are zeroed in memory when
  unloaded (best effort), and can be bypassed per call with
  `use_adapter=False` for before-and-after comparisons.
- **Generation**: `generate`, and `stream` for output that arrives piece by
  piece, with chat templates applied the same way at training and at inference
  time.
- **Command line**: `wdva keygen`, `seal`, `unseal`, `inspect`, `rekey`,
  `destroy-key`, `train`, `query` and `chat`. The first six need only the core
  install.
- **LangChain chat model**: `ChatWDVA` in `wdva.langchain` (the `langchain`
  extra), a text-only chat model on top of the engine with streaming and stop
  sequences. It never holds a key, and it refuses LangChain's response cache.
- **Packaging**: a `pyproject.toml` build with a `src/` layout, the `wdva`
  console script, and the extras `ml`, `pdf`, `langchain`, `all`, `test` and
  `dev`. The core install depends only on `pycryptodome`; the ML stack is
  imported lazily.
- **Typing**: the package ships `py.typed` and passes `mypy --strict`.
- **Test suite** that runs offline: known-answer vectors for the format, an
  independent reader written from the specification, and end-to-end training
  and inference tests against tiny models built on the fly.
- **Examples** in `examples/`: a quickstart, two people's adapters hot-swapped
  on one base model, sealing an adapter made with plain PEFT, and a LangChain
  chat.
- **Continuous integration**: lint and type checks; the core tests on Linux,
  macOS and Windows; the full suite with the ML stack on Linux; a build of the
  wheel that is installed and smoke-tested; and a release workflow that
  publishes to PyPI through Trusted Publishing.

### Changed

- The version was reset from the placeholder `1.0.0`, which was never
  published, to `0.1.0`, and the package is now labelled alpha.
- The default base model is `HuggingFaceTB/SmolLM2-360M-Instruct`.
- Python 3.10 or newer is required.
- The LangChain integration moved from `wdva.langchain_integration` to
  `wdva.langchain`, and its chat model is now called `ChatWDVA`.

### Removed

- The prototype's placeholder `train()`, which trained nothing, and its
  adapter loading, which decrypted an adapter and then did not apply it.
- The JSON-based file format. No working release ever produced one, so there is
  nothing to migrate; such a file is rejected with an explanatory error.
- The MLX backend. Like the rest of the prototype's inference code, it never
  applied an adapter. On Apple Silicon the engine selects PyTorch's MPS device
  instead; that path is not exercised by CI.
- zstd compression. Trained weights barely compress, and leaving it out removes
  a class of decompression problems.
- The side effect of setting `HF_HUB_ENABLE_HF_TRANSFER` in the environment on
  import.
- `WDVALLM`, `WDVADocumentLoader`, `WDVAEmbeddings`, `WDVACallbackHandler` and
  the chain factory helpers `create_wdva_chain` and
  `create_conversational_chain`.
- `setup.py` and `requirements.txt`, replaced by `pyproject.toml`.
- Marketing claims about compliance and performance that could not be verified.

### Security

- **Key commitment.** Each vault stores a value derived from its key. That
  closes the gap by which an AEAD ciphertext can be crafted to open under more
  than one key, and it lets a wrong key be reported as `WrongKeyError` rather
  than as corruption.
- **The header is authenticated exactly as stored.** Every byte from the magic
  number to the end of the header is associated data for the AEAD. Unknown
  header fields are rejected.
- **Bounded scrypt parameters.** The cost recorded in a passphrase-locked vault
  is checked against fixed bounds before scrypt runs, so a hostile file cannot
  make a reader allocate arbitrary amounts of memory.
- **Keys are never accepted as command-line arguments.** The command line
  takes a key file, an interactive passphrase prompt or environment variables,
  and argument abbreviation is disabled so that `--key` is not accepted as a
  short form of `--key-file`.
- **Owner-only files.** Key files, vaults and plaintext exports are created
  with mode 0600 on POSIX systems, vaults and exports through an atomic
  write, and reading a key file that others can access raises a warning.
- **Pickle adapters are refused.** Only safetensors weights are accepted;
  `adapter_model.bin` is rejected because loading it can execute code.
- Base models are loaded without enabling `trust_remote_code`.

[Unreleased]: https://github.com/zd87pl/private-ai-adapters/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zd87pl/private-ai-adapters/releases/tag/v0.1.0
