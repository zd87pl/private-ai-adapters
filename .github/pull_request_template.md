<!--
Thank you for the pull request. Please do not include keys, passphrases, vault
files or private documents in the description, the commits or the test data.
Security fixes: report privately first (see SECURITY.md) rather than opening a PR.
-->

## What this changes

<!-- What does it do, and why? Link the issue if there is one. -->

## Checklist

- [ ] Tests are added or updated for the change, and `pytest` passes locally.
- [ ] `ruff check .`, `ruff format --check .` and `mypy` are clean.
- [ ] Documentation is updated (README, `docs/`, docstrings, `--help` text) and `CHANGELOG.md` has an entry under "Unreleased".
- [ ] Every claim in the docs that this PR adds or changes is backed by a test.
- [ ] Nothing new writes a plaintext adapter or a key to disk or to logs unless the user explicitly asks for it.
- [ ] The vault format is unchanged, **or** this PR bumps the format version byte, updates `docs/FORMAT.md` and adds new known-answer vectors to `tests/test_vault.py`.
