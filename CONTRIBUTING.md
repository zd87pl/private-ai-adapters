# Contributing to wdva

Thank you for wanting to help. wdva is alpha software with a small scope:
an encrypted file format for LoRA/DoRA adapters and the tooling to train, seal
and load them. Bug reports, fixes, tests, documentation and careful review of
the cryptography and the format are all welcome.

- **Found a security problem?** Do not open an issue. Follow
  [SECURITY.md](SECURITY.md).
- **Planning a larger change?** Open an issue first, so that nobody writes code
  that cannot be merged.
- By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Development setup

You need Python 3.10 or newer and git.

```bash
git clone https://github.com/zd87pl/private-ai-adapters.git
cd private-ai-adapters

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip

python -m pip install -e ".[dev]"
```

The `dev` extra installs the package in editable mode with every optional
feature (`ml`, `pdf`, `langchain`), the test tools, ruff, mypy, build and twine.

**Tip: CPU-only PyTorch.** On Linux, the default PyTorch install pulls in GPU
libraries and is a much larger download than the CPU-only build. The tests run
on the CPU, so if you do not need a GPU, install the CPU build first and then
the rest:

```bash
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
python -m pip install -e ".[dev]"
```

**Working on the core only.** Keys, vaults, bundles and most of the command
line need nothing but `pycryptodome`. If that is all you are touching, a much
smaller environment is enough:

```bash
python -m pip install -e ".[test]" ruff mypy
```

## Running the tests

```bash
pytest                      # everything your environment can run
pytest -m "not ml"          # core tests only
pytest -m ml                # only the tests that need the ML stack
pytest tests/test_vault.py  # one file
pytest --cov --cov-report=term-missing:skip-covered   # with coverage
```

How the suite is organised:

- **Core tests** need only the core install. They cover keys, the vault format
  (including the known-answer vectors and an independent reader written from
  [docs/FORMAT.md](docs/FORMAT.md)), bundles, document loading and the
  commands that work without the ML stack.
- **ML tests** are marked `ml`, usually with `pytestmark = pytest.mark.ml` at
  the top of the module. They cover training, in-memory adapter loading,
  generation, the ML commands, the LangChain chat model and the scripts in
  `examples/`. When torch, transformers, peft or safetensors is missing,
  `tests/conftest.py` skips them, so a plain `pytest` passes in a core-only
  environment. The LangChain tests also need `langchain-core` and the PDF test
  needs `pypdf`; each skips itself when its library is missing.
- **Nothing is downloaded.** The ML tests build two tiny models (a GPT-2 and a
  Llama) and a tokenizer on the fly and pretrain them briefly on a toy corpus.
  Use the fixtures `model_dir` (parametrised over both architectures),
  `gpt2_dir` and `llama_dir`. A test must never fetch a model from the Hugging
  Face Hub; CI runs the ML tests with `HF_HUB_OFFLINE=1` to enforce that.

When you write tests:

- Mark anything that imports torch, transformers or peft with `ml`. Markers are
  strict, so a misspelt marker is an error.
- Assert on what is deterministic: files, bytes, shapes, exceptions, exit codes.
  Do not assert on what a toy model happens to say.
- Never commit a real key, vault or private document. Test vectors live in the
  test files as hex literals. (`*.key` and `*.wdva` are ignored by git on
  purpose.)

## Lint, format and types

```bash
ruff check .            # lint; add --fix to apply the safe fixes
ruff format .           # format; CI runs it with --check
mypy                    # strict type check of src/wdva
```

All three are configured in `pyproject.toml` and all three must be clean. mypy
treats the ML libraries as untyped, so it gives the same answer whether or not
they are installed.

If you like, [pre-commit](https://pre-commit.com/) can run ruff and a few
hygiene checks before every commit. It is optional and not part of the `dev`
extra:

```bash
python -m pip install pre-commit
pre-commit install
```

To check that the package still builds:

```bash
python -m build
twine check --strict dist/*
```

CI runs all of the above once for every commit: when a branch of this repository
is pushed, and for a pull request from a fork (`.github/workflows/ci.yml` decides
when, `.github/workflows/tests.yml` holds the jobs). The jobs: lint and types; the
core tests on Linux, macOS and Windows with only the core install; the tests of
the app (`wdva gate`) on Linux and macOS with the `gate` extra; the full suite
with the ML stack on Linux, on macOS on Apple Silicon, and against the oldest
supported version of every dependency;
and a build of the wheel, which is then installed into a clean environment and
smoke-tested. If you raise a dependency's minimum version in `pyproject.toml`,
raise the matching pin in the `ml-minimum` job too.

One job is not covered by `pytest`: "Browser" walks the app's interface in a
real browser against the real app, on Linux in Chromium and on macOS in WebKit.
Its three scripts and how to run them are in
[tests/browser/README.md](tests/browser/README.md). They find things by the
ids, class names and `data-key` values of the interface, so a change under
`src/wdva/gate/app/static/` may need a change there.

## Project principles

These decide what gets merged. A change that conflicts with one of them needs a
very good reason, written down in the pull request.

1. **No plaintext adapter or key on disk unless the user explicitly asks.**
   Training writes no checkpoints. Loading decrypts into memory. The only ways
   out are the ones the user calls by name, such as `wdva unseal` and
   `SecretKey.save`. The same goes for logs, warnings, exception messages and
   `repr()`.
2. **Fail loudly.** Never report success for something that did not happen. An
   adapter that does not fit the model is an error, not a warning. A wrong key
   and a damaged file are different errors. Do not add a fallback that hides a
   failure from the user.
3. **Every claim in the docs is backed by a test.** If the README, a docstring
   or `--help` says wdva does something, a test must show it. If you cannot
   test a claim, do not make it. No unmeasured numbers, and no security or
   compliance language that the code cannot support. `tests/test_docs.py`
   keeps the pages themselves honest: links and anchors must resolve, every
   `wdva` command line shown must be accepted by the real parser, and Python
   snippets must compile, and run. Output quoted in the docs is taken from the
   recordings in `docs/recordings/`, and the tests compare the two: when
   behaviour changes, record again and update both.
4. **The vault format is a contract.** People will keep `.wdva` files for
   years. Any change to the bytes needs a format version bump, an update to
   [docs/FORMAT.md](docs/FORMAT.md) and new known-answer vectors. See below.
5. **The core stays dependency-light.** `pip install wdva` depends on
   `pycryptodome` and nothing else, and `import wdva` must not import the ML
   stack. ML libraries are imported inside functions, the way `src/wdva/_ml.py`
   does it. A new dependency belongs in an extra, and needs a reason.

## Changing the vault format

Version 1 readers reject any file they do not fully understand: an unknown
version byte, an unknown header field, a field of the wrong length. That is
deliberate, and it means that almost any change to what is written is a format
change. If your change alters the bytes of a vault or of the adapter payload:

1. Bump the version: `FORMAT_VERSION` in `src/wdva/vault.py` for the container,
   and the version in the adapter content type (`CONTENT_TYPE_ADAPTER` in
   `src/wdva/bundle.py`) when the adapter payload changes. Say in the pull
   request what happens to existing files. People keep vaults for a long time,
   so a release that can no longer read them needs a migration path.
2. Update [docs/FORMAT.md](docs/FORMAT.md) in the same pull request. It is the
   normative description; the code follows it, not the other way round.
3. Add known-answer vectors for the new version to `tests/test_vault.py`, next
   to `KAT_KEYED` and `KAT_PASSPHRASE`, and keep the old ones. Update the
   independent reader `reference_unseal` in that file from the document, not
   from the implementation.
4. Say so prominently in the pull request and in `CHANGELOG.md`.

## Commits and pull requests

- Keep a pull request to one logical change. Unrelated clean-ups go in their
  own pull request.
- Write commit subjects in the imperative ("Reject empty passphrases") and use
  the body to explain why, not what.
- A bug fix starts with a test that fails without the fix. A new feature comes
  with tests and documentation.
- Add an entry to the "Unreleased" section of [CHANGELOG.md](CHANGELOG.md) for
  anything a user could notice.
- The code must run on every supported Python version, starting with 3.10.
- Source files under `src/wdva/` begin with this header:

  ```python
  # SPDX-License-Identifier: Apache-2.0
  # Copyright 2025 Enclave
  ```

- Fill in the checklist in the pull request template. CI must be green before
  review.

Contributions are accepted under the project's licence,
[Apache-2.0](LICENSE). By opening a pull request you confirm that you have the
right to submit the work under that licence.

## Releasing (maintainers)

1. Set the version in `src/wdva/_version.py`. It is the only place the version
   is written down.
2. Move the "Unreleased" entries in `CHANGELOG.md` under the new version and
   date.
3. Merge, then publish a GitHub release tagged `v` plus the version, for
   example `v0.1.0`. That starts `.github/workflows/publish.yml`, which builds
   the distributions and uploads them to PyPI through Trusted Publishing. The
   one-time setup is described at the top of that file.

## Reporting security issues

Please do not use public issues or pull requests for vulnerabilities.
[SECURITY.md](SECURITY.md) explains how to report them privately, what is in
scope and what to expect.
