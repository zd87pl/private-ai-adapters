# Changelog

All notable changes to wdva are recorded in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is below 1.0 the project is alpha: a minor release may change
the Python API or the command line. The vault file format has a version of its
own, the version byte described in [docs/FORMAT.md](docs/FORMAT.md), which
changes only when the bytes on disk do.

## [Unreleased]

### Added

- **Locktender (preview)**: a local app that stands between a vault of your
  documents and the AI apps on the same computer. [docs/GATE.md](docs/GATE.md)
  is its guide, [docs/GATE_FORMAT.md](docs/GATE_FORMAT.md) specifies its files
  and what travels on its local connections, and a section of
  [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md#locktender) says where its
  protection stops. A copy of each document is sealed in a store directory
  under a passphrase; the file that was added stays where it was, not
  encrypted. An app can search the documents and read single passages of at
  most 800 characters, inside limits per app, per kind of app (hosted or local)
  and per document; where the owner asks for it, each passage waits for an
  approval that shows the exact text. Every result is appended to a sealed,
  hash-chained record with its exact text before it is handed over. The app
  runs on macOS and Linux, not on Windows. It bounds and shows what leaves and
  cannot recall it; the guide lists what it does not do.
- **`wdva gate app`**: starts the app and serves its interface on the loopback
  address, in the browser. Each browser tab has to present, once, the pairing
  code that the terminal prints; the page then holds a session for that tab,
  which it sends in a header, and the app sets no cookie. `--demo` runs on
  invented documents in a temporary store with a played agent and without
  pairing, `--lock-after` sets when the vault locks itself (eight hours by
  default), `--view basic` or `--view pro` opens the interface in that view
  and keeps the choice beside the vault, and `--window` is an experimental
  window of its own, which pairs like the browser. At start the terminal says
  where the vault and the page are and, when no AI app could connect to this
  installation, why. A demo that stops prints one more line, the command that
  starts the app on a vault of your own.
- **Two views of one vault, Basic and Pro.** Basic is one page: what waits
  for your approval, what left the vault, with its exact text, what was
  stopped in the last 24 hours, your documents and your apps. It changes few
  things. You add and remove documents there, and in a vault with one group of
  documents (a group is what Pro calls a collection) you change what an app
  may do or remove the app. You add an app there too, unless the vault has an
  app that runs on this computer. What Basic does not change it says in
  sentences, with a link into Pro. Pro is six pages, Overview, Activity,
  Approvals, Documents, Apps and Vault, with every setting. A control labelled
  "View" switches between the two. A new vault starts in Basic, and the demo
  opens in Basic; a vault that was made before the two views existed opens in
  Pro until you choose "Basic". What an app is given does not depend on the
  view.
- **A first run in four steps**, "Passphrase", "App", "Documents" and "First
  question", for a new vault in Basic. The second step asks which AI app you
  use and gives it one kind of access, "Ask me each time": every passage waits
  for your approval. The third adds a document, or an invented sample letter
  when no file is at hand. The fourth shows how to add Locktender to that app
  and the start of a question to copy, waits for the app's first request, and
  shows the passage the app asks for, with its exact text and the buttons
  "Release once" and "Don’t release". For Claude Desktop and the ChatGPT
  desktop app that step has a button "Check the file". A press makes
  Locktender read that app's one settings file and tell the page one of a few
  fixed answers, such as whether its own entry is there, and nothing of what
  the file holds; the page asks for that look at no other time. It is the one
  file of another program that Locktender reads. Locktender writes no file of
  another program and changes no settings of any AI app: you copy the text and
  put it where the AI app reads it. Every step after the first can be skipped
  or left for later; until setting up is over, a card on the Basic page,
  "Finish setting up", names the next one.
- **The demo has a guide.** It stands on the Basic page under "Approvals" and
  says where the demo is: an app is about to ask, a passage waits, it was
  released or not, it was read. The played app asks again for the passage that
  waited when the guide's button "Have the played app ask again" is pressed,
  and at no other time, whatever your answer was.
- **A small file beside the vault**, `run/interface.json` in the store
  directory. It holds two words, which view you use and whether setting up is
  finished, and nothing from the vault. It is not sealed, it is no setting of
  the vault, and writing it leaves no entry in the record. A file that is
  missing or holds anything else counts as no file: the vault then opens in
  Pro, and it unlocks all the same.
  [docs/GATE_FORMAT.md](docs/GATE_FORMAT.md#the-interface-file) specifies it.
- **`wdva gate mcp`**: the program an agent application starts, a Model
  Context Protocol server on standard input and output with three read-only
  tools, `locktender_status`, `locktender_search` and `locktender_read`. It
  holds no key and passes every call to the running app. It needs Python 3.11
  or newer and the MCP SDK.
- **`wdva gate config HOST`**: prints the configuration that connects one
  application that starts local MCP servers. `HOST` is `claude-desktop`,
  `claude-code`, `chatgpt-desktop`, `codex`, `openclaw` or `generic`. The
  ChatGPT desktop app has a `HOST` of its own, apart from Codex:
  `chatgpt-desktop` prints the section for `~/.codex/config.toml`, the file
  that app shares with Codex according to OpenAI's documentation, and `codex`
  prints the `codex mcp add` line with the same section as the other way. For
  `chatgpt-web`, `claude-web`, `grok-web` and `grok-bot` the command says why
  assistants on the web and on phones cannot be connected. The forms follow
  each application's documentation and have not been tried with the
  applications themselves.
- **Extras**: `mcp` (the MCP SDK, installed on Python 3.11 and newer) and
  `gate` (`mcp` and `pdf` together, everything the app can use). `all` now
  includes `mcp`.
- **Tests and continuous integration for the app**: its tests run on Linux and
  macOS with Python 3.11 and 3.13. Three walk-throughs in `tests/browser/`
  drive the interface in a real browser against the real app:
  `journey_demo.js` walks the demo with its guide, `journey_real.js` a new
  vault in Pro from pairing to Ctrl-C, and `journey_basic.js` the first run
  and the Basic page. The CI job "Browser" runs them in Chromium on Linux and
  in WebKit on macOS. No AI app takes part in them. Not tested: a person at a
  Mac, Safari itself, a real conversation in any AI app, and `--window`.

### Changed

- **The `pdf` extra needs pypdf 7.0.1 or newer**; the floor was 4.0. The
  extras `gate` and `all` include `pdf`, so the same holds for them. Older
  releases of pypdf are named in security advisories about crafted PDF files
  that take very long to read or use up memory. pypdf is what reads PDF files
  for `wdva train` and for Locktender.
- **Continuous integration tests every commit once.** A branch of this
  repository is tested when it is pushed, and a pull request is tested as a
  pull request only when it comes from a fork. Before, a push to a branch with
  an open pull request started every job twice.

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
  deletes a key file, and refuses anything that is not one.
- **`AdapterBundle`**: a PEFT adapter held in memory (adapter config,
  safetensors weights and private notes), encrypted as one payload, with
  lossless import from and export to a standard PEFT adapter directory. It
  describes its tensors without importing torch.
- **Training that really trains**: `train_adapter` and `TrainingConfig`
  fine-tune a LoRA or DoRA adapter on documents (text, Markdown, and PDF with
  the `pdf` extra), raw strings and prompt/response examples, and seal the
  result straight into a vault. The loop writes no checkpoints and no plaintext
  adapter files. It reports the loss before and after, measured on every
  training sequence or on a fixed random sample of them, and says how many.
  Malformed examples and an unwritable output location are reported before
  the base model is loaded.
- **The `WDVA` engine**: one base model with encrypted adapters decrypted and
  injected in memory. Adapters can be hot-swapped, are zeroed in memory when
  unloaded (best effort), and can be bypassed per call with
  `use_adapter=False` for before-and-after comparisons.
- **Adapters cannot change the base model.** Only plain LoRA and DoRA adapters
  are accepted. The adapter's configuration goes through an allowlist, every
  tensor must be a LoRA or DoRA tensor present with the right shape, and both
  checks run before the model is touched; anything else raises
  `AdapterMismatchError`. Adapters saved with `modules_to_save` or with bias
  terms, which would overwrite base weights, are refused. Injection is all or
  nothing, a failed load leaves no adapter applied, and after every removal
  the engine checks that the base model's structure is what it was when
  loaded, and reloads the model if it is not.
- **Generation**: `generate`, and `stream` for output that arrives piece by
  piece, with chat templates applied the same way at training and at inference
  time. Both accept `adapter=` and `key=` to apply, use and remove an adapter
  for one request under a single lock, which is the pattern to use when
  several threads share an engine.
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
  and inference tests against tiny models built on the fly. The documentation
  is under test too: links, every command line shown, the Python snippets of
  the README and the guide, which are run as printed, and the output it
  quotes, which must match the recorded runs kept in `docs/recordings/`.
- **Examples** in `examples/`: a quickstart, two people's adapters hot-swapped
  on one base model, sealing an adapter made with plain PEFT, and a LangChain
  chat.
- **Continuous integration**: lint and type checks; the core tests on Linux,
  macOS and Windows; the full suite with the ML stack on Linux, on macOS on
  Apple Silicon, and against the oldest supported version of every
  dependency; a build of the wheel that is installed and smoke-tested; and a
  release workflow that publishes to PyPI through Trusted Publishing.
- **Errors and warnings with one root each**: failures specific to the package
  (a wrong key, a damaged vault, an adapter that does not fit, a training run
  that diverged) derive from `WDVAError`, and its warnings from `WDVAWarning`.
  Bad arguments and file problems are the usual built-in exceptions.

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
  instead. CI runs the full suite on Apple Silicon, but its runners expose no
  Metal device, so the test that trains and generates on MPS runs only on a
  real Mac.
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
- **Strict header parsing.** Duplicate keys, `NaN` and `Infinity`, numbers a
  double cannot hold, nesting deeper than 64 levels, and values of the wrong
  type are rejected, by writers and readers alike, so that two readers cannot disagree about what an
  authentic file says and a small hostile file cannot crash a parser.
- **Bounded scrypt parameters.** The cost recorded in a passphrase-locked vault
  is checked against fixed bounds before scrypt runs, so a hostile file cannot
  make a reader allocate arbitrary amounts of memory.
- **Keys are never accepted as command-line arguments.** The command line
  takes a key file, an interactive passphrase prompt or environment variables,
  and argument abbreviation is disabled so that `--key` is not accepted as a
  short form of `--key-file`.
- **Owner-only files, written atomically.** Key files, vaults and plaintext
  exports are written to a new file with mode 0600 on POSIX systems and then
  moved into place, so a failure never leaves a damaged file and an existing
  file with loose permissions is never written through. Refusing to overwrite
  is decided atomically. Reading a key file that others can access raises a
  warning.
- **Weights are validated as strictly as the safetensors library does** before
  they are sealed or reported, and values read from a vault are printed
  without control characters.
- **An engine cannot be pickled or copied**, since that would write a loaded
  adapter out in plaintext.
- **Pickle adapters are refused.** Only safetensors weights are accepted;
  `adapter_model.bin` is rejected because loading it can execute code.
- **Code in a model repository is never run.** Base models are loaded with
  `trust_remote_code` switched off. Left at its default, transformers asks on
  standard input whether to run such code, and a vault names the base model
  that `wdva query` loads.

[Unreleased]: https://github.com/zd87pl/private-ai-adapters/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zd87pl/private-ai-adapters/releases/tag/v0.1.0
