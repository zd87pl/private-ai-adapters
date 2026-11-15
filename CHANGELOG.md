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
  default), `--view basic` or `--view pro` opens the page on Home or on
  Overview of the six pages under Settings and keeps the choice beside the
  vault, and `--window` is an experimental window of its own, which pairs
  like the browser. At start the terminal says where the vault and the page
  are and, when no AI app could connect to this installation, why. A demo
  that stops prints one more line, the command that starts the app on a vault
  of your own.
- **The page: Home, Documents, Activity and Settings.** Home shows what waits
  for your yes, a box that asks your documents, four tiles that connect an
  app, and the three answers apps were given last, each with its exact text
  one press away. Home changes few things: it adds a document, connects an app
  with one kind of access, "Ask me each time", and disconnects it. Documents
  adds and removes documents and says, under what you added, which apps can
  ask for it. Activity lists every answer an app was given and, under
  "Stopped", the calls that were stopped. A row counts in your own text: a
  passage by its own length, which is the figure its card showed, and a
  search whose answer held no text of a document says "no document text". Settings keeps the language, the page to open
  on and the command that starts the app again, and leads to six pages with
  every setting, Overview, Activity, Approvals, Documents, Apps and Vault,
  which are in English: "Can read", limits, pauses, a second group of
  documents and apps whose model runs on this computer are set there. A page
  "How it works and what it cannot do" says the rest in sentences. A new
  vault and the demo open on Home; a vault that was made before the page
  kept its choice opens on Overview until you choose Home under "Open on".
  What an app is given does not depend on the page you look at.
- **"Ask your documents", a trial on your own computer.** A box on Home takes
  a question and shows the one passage an app's search would find for those
  words, as an approval card would show it, with the words it was found by
  and who could get it under the vault's settings. Nothing is sent, nothing
  is counted against a limit and nothing is recorded. The box finds whole
  words exactly as they stand and writes no answer. It needs the passphrase
  typed within the last ten minutes and keeps its own time for that: typing
  the passphrase for the box does not allow a change that lets more leave.
  It answers 20 questions a minute. Home says how many were asked since the
  vault was unlocked, and since the app was started where that is more;
  locking does not clear the second count.
- **"Connect" writes Locktender's entry into an AI app's settings file.**
  Three tiles on Home stand for a program whose settings file the app can
  write: "Claude" for Claude Desktop, "ChatGPT" for the ChatGPT desktop app
  and Codex, which by OpenAI's documentation read one file, and "Grok" for
  Grok Build. A press on a tile has the app read that one file and say in
  fixed words what it found. "Connect" then adds the app to the vault, with
  "Ask me each time" where the vault has one group of documents and with
  nothing where it has several, and writes one entry, named `locktender`,
  into the file: the command that starts the program which carries the
  app's requests to Locktender. The text is edited where the entry goes and
  nowhere else, a copy of the file as it was is kept beside it before each
  change (its name ends in `.before-locktender` or `.last-locktender`), the
  change is in the record before it is made, and the file is read back.
  "Disconnect" removes the app from the vault and takes that entry out again;
  an entry that points elsewhere is left. "Connect" needs the passphrase
  typed within the last ten minutes, and so does a "Disconnect" that changes
  the file. The fourth tile, "Other app", writes nothing and shows text to
  paste, and so does "Show the text to paste" wherever the app does not
  write. **This ends a promise**: until now Locktender wrote no file
  outside the vault's folder and changed no setting of another program. It
  now writes that one kind of file, on your press. It writes no file that it
  could not read, that is a link or lies in a folder that is a link, that is
  larger than 1 MiB, has a second name or belongs to another account, that
  was changed in the last five seconds or while it was read; it creates no
  folder, and it writes no entry that names a Python or a vault in a folder
  for temporary files or in a place others can change. The write was tested
  against settings files made for the tests. It was not tried with Claude
  Desktop, the ChatGPT desktop app, Codex or Grok Build, and not by hand on a
  Mac.
- **English and Polish.** The page before the vault is open, Home, Documents,
  Activity, Settings, the approval card, the sheets and the page "How it
  works" are in either language, chosen with a switch on every screen and
  kept beside the vault. Numbers, times and plural forms follow the language.
  The six pages under Settings, the sentences the app itself answers with and
  what an AI app is told stay in English. The Polish texts have not been read
  by somebody who writes Polish for a living; where the two differ, the
  English text is the one that was specified.
- **A new vault begins with three cards on Home**, "Start here": "Add a
  document" (or an invented sample document, a rental agreement, in the
  language of the page), "Ask your documents", and "Connect an app". Each
  card is the part of Home it names, so nothing has to be learnt twice. The
  cards go when the vault has a document and an app and you have seen a
  passage in the box or an app has called, or when you hide them. From the
  pairing screen it takes three presses to the first passage in the box,
  five to "Connected" and six to the first passage a caller reads through
  the written entry; a walk-through counts them.
- **The demo has a card on Home.** It says in one sentence where the demo is:
  an app is about to ask, a passage waits, it was released or not, it was
  read. The played app asks again for the passage that waited when the
  card's button "Have the played app ask again" is pressed, and at no other
  time, whatever your answer was. A demo connects nothing: its tiles look at
  no file, and the three routes of a settings file refuse.
- **A small file beside the vault**, `run/interface.json` in the store
  directory. It holds two or three words: on which page the app opens,
  whether the first steps are done, and your language once you chose one. It
  holds nothing from the vault, it is not sealed, it is no setting of the
  vault, and writing it leaves no entry in the record. A file that is missing
  or holds anything else counts as no file: the vault then opens on Overview,
  and it unlocks all the same.
  [docs/GATE_FORMAT.md](docs/GATE_FORMAT.md#the-interface-file) specifies it.
- **New answers and words of the app's own API**, for whoever calls it
  without the page. `POST /api/try` is the trial question; it answers 403
  `trial_reauth_required` when the passphrase was last typed more than ten
  minutes ago and 429 `too_many` above 20 questions a minute, and
  `POST /api/reauth` takes `"scope": "trial"`. `POST /api/connect/<host>/look`,
  `POST /api/connect/<host>` and `POST /api/connect/<host>/remove` deal with
  the settings file of `claude-desktop`, `chatgpt-desktop` or `grok-build`.
  The look answers one of fourteen words for what it found (`busy` is the
  one for a file that was written or replaced while it was read). A connect
  that writes nothing says why in a word of its own (`recent`, `by_hand`,
  `no_copy`, `failed`, `expands`, `unstable`, `moved`, `other_kind`,
  `not_ready`) or answers 409 `changed` for a file that is not what the owner
  was shown; in a demo the three routes answer 409 `demo`. An upload that is refused carries a word for why in `reason`.
  An entry of the kind `release` in `GET /api/activity` says in two more
  fields how much of it was text of a document: `document_text`, and for a
  passage `passage_characters`, its length without the lines added around it.
  A `policy` entry of the record whose `by` begins with `connect` or
  `disconnect` says that a settings file was about to be changed, or was
  left; its policy can be the one that was in force before, which a version
  from before this entry shows as a save without a change. And the sentence
  an AI app is given for a passage that waits no longer names a page: "Ask
  the user to open the Locktender page in their browser and answer the
  request …".
- **`wdva gate mcp`**: the program an agent application starts, a Model
  Context Protocol server on standard input and output with three read-only
  tools, `locktender_status`, `locktender_search` and `locktender_read`. It
  holds no key and passes every call to the running app. It needs Python 3.11
  or newer and the MCP SDK.
- **`wdva gate config HOST`**: prints the configuration that connects one
  application that starts local MCP servers. `HOST` is `claude-desktop`,
  `claude-code`, `chatgpt-desktop`, `codex`, `grok-build`, `openclaw` or
  `generic`. `grok-build` is xAI's coding agent for the terminal: it prints
  the section for `~/.grok/config.toml` and the `grok mcp add` line. The
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
  `journey_demo.js` walks the demo with its card, `journey_real.js` a new
  vault on the six pages under Settings from pairing to Ctrl-C, and
  `journey_home.js` Home, the box that asks, the four tiles with "Connect"
  and "Disconnect" against settings files in a home made for the run, and
  the plain pages, in English and Polish. The CI job "Browser" runs them in
  Chromium on Linux and in WebKit on macOS. No AI app takes part in them. Not
  tested: a person at a Mac, Safari itself, a real conversation in any AI
  app, what an AI app does with the entry "Connect" writes, and `--window`.
- **Gone since earlier states of this preview**, none of which was released.
  The page had two views, "Basic" and "Pro", with a control "View" between
  them: Home and the pages beside it stand where Basic stood, and Pro is the
  six pages under Settings. A new vault was led through a first run in four
  steps, "Passphrase", "App", "Documents" and "First question": the
  passphrase screen and the three cards "Start here" took their place. The
  last of those steps had a button "Check the file", which read an app's
  settings file and answered one word, through the route
  `POST /api/apps/<name>/connect/check`: the route and the button are gone,
  and a tile's sheet looks at the file instead. The card "Finish setting up",
  the demo's guide under "Approvals" and the walk-through `journey_basic.js`
  went with them. Addresses of the page moved: `#/documents` and `#/activity`
  are the plain pages now, the six pages are at `#/pro/…`, and the old
  addresses of the six pages, of Basic and of the first run lead to where
  their pages are.

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
