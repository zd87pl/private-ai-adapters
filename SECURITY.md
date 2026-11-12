# Security policy

wdva encrypts fine-tuned adapters so that they can be stored and moved around
safely. People will rely on it for exactly that, so please read this page with
two facts in mind:

- **wdva is alpha software.**
- **wdva has not had an independent security audit.** The design uses standard
  primitives from an established library and is written down in
  [docs/FORMAT.md](docs/FORMAT.md) so that it can be reviewed, but nobody
  outside the project has been engaged to review it. Decide what you trust it
  with accordingly.

wdva holds no certification and makes no regulatory claim. Using it does not by
itself make a system meet any law or standard.

## Supported versions

| Version | Status |
| ------- | ------ |
| 0.1.x   | Supported (alpha). Fixes are released as a new 0.1 version. |
| Anything before 0.1.0 | Not supported. That was prototype code; it was never published as a release. |

## Reporting a vulnerability

Report it privately, through GitHub's private vulnerability reporting form:

**<https://github.com/zd87pl/private-ai-adapters/security/advisories/new>**

Please do **not** open a public issue, pull request or discussion for a
vulnerability, and do not include the details in a commit message. The form
creates a draft advisory that only you and the maintainers can see.

If the form is not available to you, open an ordinary issue that says only that
you have a security report, with no details, and a maintainer will arrange a
private channel.

### What to include

- The wdva version (`wdva --version`) or commit, your Python version and
  operating system, and which extras are installed. For anything that involves
  training or inference, add the versions of torch, transformers and peft.
- What an attacker needs to begin with (for example: read access to a vault
  file, write access to it, the ability to hand a file to a victim, a local
  account on the same machine) and what they gain.
- Steps or a script that reproduce the problem. A crafted file can be attached
  to the private advisory.
- Whether the problem is already known or discussed anywhere else, and whether
  you would like to be credited.

Demonstrate the problem with throwaway keys and made-up data. Never send real
keys, passphrases, vaults or private documents, not even through the private
form.

### What to expect

wdva is maintained by volunteers, and reports are handled on a best-effort
basis. There is no guaranteed response or fix time and no bug bounty. We will
reply in the advisory thread, work on a fix with you there, and credit you in
the release notes if you want that. Please give us a reasonable chance to
release a fix before you publish details.

## Scope

### In scope

Anything that breaks what wdva says it does. In particular:

- **Reading or forging a vault without the key.** Recovering any part of a
  vault's payload or key; producing a file that unseals without knowledge of
  the key or passphrase; changing the header or ciphertext of an existing vault
  without detection; a single file that opens under two different keys.
- **Plaintext adapter or key material reaching disk or logs unintentionally.**
  wdva writes an adapter in plaintext only when asked to (`wdva unseal`,
  `AdapterBundle.to_peft_dir`) and a key only when asked to (`wdva keygen`,
  `SecretKey.save`). Any other path by which adapter weights, keys or
  passphrases end up in a file, a temporary file, a log, a warning, an
  exception message or a `repr()` is a bug. So is a file that wdva creates with
  wider permissions than owner-only on a POSIX system, and a command line that
  accepts key material as an argument.
- **An adapter being reported as loaded when it is not applied.** For example
  `WDVA.load` returning normally while the model answers with base weights or
  with only part of the adapter; an adapter that still influences output after
  `unload()`; `use_adapter=False` not giving the base model's output.
- **Parser bugs on hostile files.** A crafted vault, adapter payload or adapter
  directory that causes anything other than one of the documented exceptions:
  code execution, a write outside the requested output path, or memory or CPU
  use far beyond the size of the file because a field was trusted before it was
  authenticated.
- **The release process.** A way to make a published package differ from the
  tagged source.

If you are not sure whether something is in scope, report it privately anyway.

### Out of scope

These are limits of the design, not bugs in it.
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) explains each of them.

- **A compromised device while an adapter is loaded.** To be used, an adapter
  has to be decrypted. While it is loaded, the key, the adapter weights, your
  prompts and the model's answers are all in the memory of the process, and
  anyone who controls the machine or the account can read them. wdva zeroes
  what it can reach when you unload an adapter or wipe a key, but that is best
  effort: Python, the ML libraries and the operating system make copies it
  cannot reach.
- **Malicious base models.** wdva loads whichever base model you name. It does
  not turn on `trust_remote_code`, but it does not vet model files either. A
  hostile base model can do whatever it likes with an adapter applied to it.
- **Training-data extraction through model outputs.** An adapter can memorise
  the text it was trained on, and whoever is allowed to query the model with
  the adapter applied can get that text back out. A vault protects the adapter
  at rest. It does not limit what the model says.
- **Side channels.** Timing, power, cache and memory-access analysis. Also the
  size of a vault, which reveals roughly how large the adapter is.
- **Keys and passphrases themselves.** Weak passphrases, lost keys (there is no
  recovery), and copies of a key that wdva cannot see. `wdva destroy-key`
  overwrites and deletes the one file you point it at; it cannot find backups,
  and overwriting is best effort on SSDs and on journaling or copy-on-write
  file systems.
- **Rollback.** Replacing a vault with an older vault sealed under the same
  key. Both authenticate; see the limits listed in
  [docs/FORMAT.md](docs/FORMAT.md).
- **Vulnerabilities in dependencies** such as PyTorch, Transformers, PEFT,
  pycryptodome or LangChain. Please report those upstream. Do tell us if wdva
  uses one of them in an unsafe way.
