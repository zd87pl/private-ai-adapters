# Threat model

This page says what WDVA protects, from whom, and where the protection stops. It
describes version 0.1.0 as it is implemented. WDVA is alpha software and has not
been independently audited; treat every "yes" below as "this is what the design
and the tests say", not as a certified property.

The short version: **a vault protects an adapter while it is a file.** Once you
load it, it is plaintext in the memory of a process on your machine, and from
then on you are relying on that machine.

## What is being protected

| Asset | Why it matters | Where it lives |
| --- | --- | --- |
| **Adapter weights** | A fine-tuned adapter memorises its training text. Whoever can load it can ask it. It is as sensitive as the documents. | Encrypted in the `.wdva` file. In plaintext in process memory while training and while loaded. |
| **Adapter metadata** | Base model, rank, target modules, tensor names, document file names, training statistics, timestamps. They describe you and your data. | Inside the ciphertext. Not in the header unless you put them there yourself. |
| **Keys and passphrases** | Whoever has the key has the adapter. | A key file (mode 0600), an environment variable, or your head. In process memory while in use. |
| **Training documents** | The original secret. | Wherever you keep them. WDVA reads them and does not copy, move or encrypt them. |
| **Prompts and outputs** | A question can be private, and an answer can repeat training text. | Process memory, your terminal, and whatever your application logs. WDVA does not store them. |

Only the first three are things WDVA encrypts. The last two are listed because
people forget them.

## Trust assumptions

WDVA's guarantees hold only if all of these do:

1. **The device is trusted while a key is in use.** That means the operating
   system, the user account, and every process that can read this process's
   memory or files.
2. **The software is what it claims to be.** Python, wdva, pycryptodome, and for
   training and inference torch, transformers, peft and safetensors. Supply-chain
   attacks on any of them are out of scope.
3. **The base model is trusted.** It runs with full access to the applied adapter.
4. **The operating system's random number generator is sound.** Keys, salts and
   nonces come from Python's `secrets` module.
5. **The primitives hold**: XChaCha20-Poly1305 and HKDF-SHA256 as implemented by
   pycryptodome, and scrypt as provided by Python's `hashlib`.
6. **Whoever holds a key is trusted to author vaults under it.** Authentication
   proves that a vault was sealed by someone with the key. It does not prove
   that the adapter inside is benign (see
   [Loading a vault you did not make](#loading-a-vault-you-did-not-make)).

Storage and transport are **not** trusted. That is the point of the format.

## Adversaries and outcomes

| Adversary | What they can do | Outcome | What is left to you |
| --- | --- | --- | --- |
| **Someone who copies the vault** from your disk, a cloud drive, a backup or a discarded laptop | Read the file, as often as they like, forever | **Protected.** Without the key the payload is XChaCha20-Poly1305 ciphertext. They learn what the header and the file size reveal ([below](#what-a-vault-reveals-without-the-key)). For a passphrase vault they can guess offline ([Passphrases](#passphrases)). | Keep the key somewhere the vault is not. A vault stored beside its key is protected from nobody. |
| **Storage that corrupts or modifies the file** | Flip bits, truncate, append, edit the header, splice the ciphertext under another header | **Detected.** The header is authenticated exactly as stored and the ciphertext carries a Poly1305 tag. Loading fails with `IntegrityError`, `WrongKeyError` (if the salt or commitment was changed) or `VaultFormatError` (if the structure was broken), and no plaintext is returned. | Keep a backup. Detection is not recovery. |
| **Storage that swaps the file** for another vault sealed under the same key | Serve an older version of the adapter, or a different adapter of yours | **Not protected.** Both files authenticate. A vault is not bound to its name, its location or a version counter: there is **no rollback protection**. | Use one key per adapter if substitution matters. Put a version in `info` (private) or in the public metadata when you seal, and check it after unsealing against a value the storage provider cannot change. |
| **Storage that deletes or withholds the file** | Remove it | **Not protected.** WDVA offers no availability. | Backups. |
| **Someone who steals the key file** | Open every vault sealed with that key, including copies you no longer control, and seal new vaults that authenticate under it | **Not protected.** The key is the secret. | Re-key what you still control (`wdva rekey`) and treat old copies of the vaults as disclosed: old ciphertext still opens with the old key. |
| **Someone with code execution, an administrator account or memory access on the device while an adapter is loaded** | Read process memory, attach a debugger, read swap or a core dump, replace the wdva package | **Not protected.** The key, the decrypted adapter, your prompts and the answers are all in memory in plaintext. Zeroing on unload ([Memory hygiene](#memory-hygiene)) shortens the window; it does not close it. | Full-disk encryption, encrypted or disabled swap, no core dumps, and a machine you actually trust. |
| **Another local user on a shared machine** | List processes, read world-readable files | **Partly protected.** Key files, vaults and plaintext exports are created with mode 0600 on POSIX, a key file that others can read triggers `InsecureKeyFileWarning`, and keys are never command-line arguments. Environment variables are visible to your own processes and to root. On Windows, files inherit the access rules of their folder. | Keep keys in a folder only you can read. Root can read everything. |
| **A malicious or backdoored base model** | Run with the adapter applied; behave differently on a trigger; in principle leak adapter content through its outputs | **Not protected.** WDVA never enables `trust_remote_code` when it loads a base model, so Python code shipped in a model repository is not executed. It does not vet weights or tokenizer files in any other way. | Use base models you have reason to trust, download them once, and run offline. |
| **A person allowed to query the adapted model** | Ask questions, including ones designed to make the model recite its training text | **Not protected.** Extracting training data through outputs is what a memorising adapter makes possible. A vault protects the adapter at rest; it places no limit on what the model says. | Give query access only to people who may read the training documents. |
| **A key holder who authors a hostile vault** | Hand you a vault that authenticates under a key you share | **Partly neutralised**, see the next section. | Do not load vaults from people you would not accept a model file from. |
| **A network observer** | Watch your traffic | WDVA itself opens no network connections. Loading a base model by its Hub id does, through transformers, unless you pass `--offline` / `local_files_only=True` or set `HF_HUB_OFFLINE=1`; that reveals which base model you fetch. | Download the base model once, then stay offline. |

## Loading a vault you did not make

A vault that opens with your key was sealed by someone who has that key. If that
someone is not you, the adapter inside is input from them. This is what WDVA does
with it:

**Neutralised**

- **No pickle.** The weights must be a well-formed safetensors file: known
  dtypes, shapes whose byte sizes match, tensor data that covers the buffer with
  no gaps, overlaps or trailing bytes. They are parsed by the safetensors library
  from memory. `adapter_model.bin` (pickle) is refused when sealing a PEFT
  directory, and there is no code path that unpickles anything from a vault.
- **Only LoRA and DoRA.** Any other `peft_type` is rejected with
  `AdapterMismatchError`.
- **The stored PEFT configuration is reduced to an allowlist.** It is not handed
  to PEFT as it is. Only structural settings are used: rank, alpha, target and
  excluded modules, the DoRA and rsLoRA flags, per-module rank and alpha
  patterns, and which layers to transform. Settings that only matter for
  training or initialisation, or that say where the adapter came from, are
  dropped; so no initialisation routine runs, and the base model name in the
  config is not followed. Any other setting that is switched on is refused with
  `AdapterMismatchError`.
- **Nothing in a vault may change the base model.** Several ordinary PEFT
  options overwrite or restructure base weights, which would not be undone when
  the adapter is removed: `bias` other than `none`, `modules_to_save`,
  `layer_replication`. Adapters that use them are refused, and so is any tensor
  that is not a LoRA or DoRA weight. As a second line of defence the engine
  compares the layout of the base model's modules and parameters before and
  after an adapter; if removing an adapter did not restore it, the base model is
  discarded and loaded afresh on next use.
- **`trust_remote_code` is never enabled**, whichever base model ends up loaded.
- **All or nothing, and fail closed.** Every tensor the rebuilt adapter needs
  must be present with the right shape, and no extra tensors are accepted.
  Otherwise the adapter layers are removed again and `AdapterMismatchError` is
  raised. After a failed `load`, no adapter is applied at all, not even the one
  that was applied before.
- **Terminal output is cleaned.** `wdva inspect` replaces control characters in
  values it prints from a file.

**Not neutralised**

- **The weights themselves.** An adapter exists to change what the model says. A
  hostile one can make it say false or harmful things, or behave normally until
  a trigger appears. Nothing in WDVA inspects behaviour.
- **Which base model gets loaded.** A vault records its base model, and
  `wdva query`, `wdva chat` and `ChatWDVA.from_vault` load that model unless you
  name one yourself. For a vault you did not make, pass `--base-model` (or
  `base_model=`) and `--offline` explicitly.
- **Scale and size.** Rank, target modules and scaling (`lora_alpha`,
  `use_rslora`, `alpha_pattern`) are taken from the config. Scaling changes how
  strongly the adapter acts, and the rank decides how much memory PEFT tries to
  allocate before the tensor check runs, so a hostile config can make loading
  fail by exhausting memory.

## What a vault reveals without the key

Everything in the header is public. It is authenticated, so it cannot be changed
unnoticed, but it is not encrypted. Someone who holds only the file learns:

- **That it is a WDVA vault holding an adapter**: the magic bytes, the format
  version and the content type `application/vnd.wdva.adapter.v1`.
- **How it is unlocked**: with a key or with a passphrase, and for a passphrase
  the scrypt cost and salt.
- **Any public metadata** the sealer chose to add. The command line adds none.
  The Python API has a `public_metadata` argument, and whatever you pass there
  is readable by everyone.
- **The exact payload size.** There is no padding and no compression, so the
  ciphertext is as long as the plaintext. An adapter stored as 32-bit floats
  takes about four bytes per parameter (the quickstart vault is 18,563,701 bytes
  for 4,618,240 parameters), so the parameter count can be estimated, and from
  it plausible combinations of base model size and rank.
- **Random-looking values**: the salt, the nonce and the key commitment. The
  commitment is derived from the key with a fresh salt per file. It does not
  reveal the key, and two vaults sealed under one key carry unrelated
  commitments.
- **Whatever the file system adds**: file name, timestamps, owner. WDVA does not
  control those.

What stays hidden: the weights, the PEFT configuration (rank, alpha, target
modules, LoRA or DoRA), the base model name, tensor names and shapes, the names
of the training documents, the training settings and losses, the creation time
and the wdva version. The test suite checks that tensor names, the base model
name, configuration keys, training statistics and training text do not appear in
the raw bytes of a vault.

## Memory hygiene

Wiping memory from Python is best effort. This is exactly what the code does and
what it cannot do.

**What the code does**

- A `SecretKey` keeps its 32 bytes in a mutable `bytearray`. `wipe()` overwrites
  that buffer with zeros in place and makes the key unusable (`KeyWipedError`).
  Used as a context manager, a key is wiped when the block ends.
- A `SecretKey` never appears in `repr()`, cannot be pickled, cannot be hashed,
  and compares in constant time.
- When you pass a key as raw bytes or hex, a temporary `SecretKey` is created
  for that one operation and wiped afterwards.
- For a passphrase vault, the master key that scrypt produces is held in a
  `bytearray` and zeroed as soon as the per-file keys have been derived.
- On `engine.unload()`, on leaving `with engine.adapter(...)`, at the end of a
  request made with `generate(..., adapter=...)`, on `engine.close()` and at the
  end of training, every adapter tensor in the model is overwritten with zeros
  in place, on whatever device it lives, **before** the adapter layers are
  detached. Then the garbage collector runs and the CUDA or MPS allocator cache
  is released.
- The engine refuses to be pickled or copied, and the LangChain chat model
  refuses to be pickled, so a loaded adapter cannot reach disk through `pickle`,
  `copy.deepcopy` or a multiprocessing queue by accident.
- Nothing is written to disk in plaintext unless you call something whose name
  says so: `wdva unseal`, `AdapterBundle.to_peft_dir`, `wdva keygen`,
  `SecretKey.save`.

**What it cannot do**

- **Immutable copies.** The per-file encryption key that HKDF returns is an
  immutable `bytes` object, as are the hex text read from a key file or from
  `WDVA_KEY`, the result of `key.hex()`, and any passphrase (a `str`). The
  crypto library keeps its own internal cipher state. None of these can be
  overwritten from Python; they linger until the memory is reused.
- **The decrypted payload.** Unsealing returns the whole adapter as an immutable
  `bytes` object, and safetensors parses it into tensors before the values are
  copied into the model. Those intermediate copies are freed, not zeroed. An
  `AdapterBundle` you hold on to stays in memory until you drop it.
- **Training residue.** Token ids of your documents, activations, gradients and
  the optimizer's running statistics are derived from your text. They are freed
  when training ends, not zeroed.
- **Prompts and outputs.** Ordinary Python strings.
- **The operating system.** Pages may have been written to swap or a hibernation
  file. A crash can produce a core dump. A debugger or a privileged process can
  read memory at any time.
- **GPU memory.** Zeroing in place overwrites the adapter's own tensors.
  Releasing the allocator cache hands memory back to the driver; it does not
  scrub it, and other copies the framework made are out of reach.
- **The base model process itself.** WDVA is a library inside your process. It
  cannot defend that process against its own host.

If this matters to you: use full-disk encryption, encrypt or disable swap, turn
off core dumps, and prefer short-lived processes.

## Crypto-shredding

A vault is useless without its key, so destroying the key is a way to "delete"
every copy of the vault at once, including copies in backups you cannot reach.

`wdva destroy-key FILE` (or `wdva.destroy_key_file(path)`) checks that the file
is a regular file containing a WDVA key (it refuses symbolic links, vaults and
anything else), overwrites it with random bytes, flushes that to disk and then
deletes the file. The command asks for confirmation unless you pass `--yes`,
and refuses to run unattended without it.

What that achieves: **that copy of the key is gone, as far as the file system
lets an overwrite reach the old blocks.** If it was the only copy, the vaults
sealed with it can no longer be opened by anyone, to the extent that the
cryptography holds.

Its limits:

- **Other copies of the key.** Backups, password managers, synced folders, CI
  secrets, an environment file, a terminal scrollback if the key was ever
  printed. WDVA cannot find them.
- **Storage remanence.** On SSDs (wear levelling), on copy-on-write and
  journaling file systems, and wherever snapshots exist, the old blocks may
  survive the overwrite.
- **Passphrase vaults cannot be shredded.** There is no key file to destroy;
  anyone who kept a copy of the vault can open it for as long as they know or
  can guess the passphrase. Use a key file for anything you may need to shred.
- **Re-keying is rotation, not revocation.** `wdva rekey` writes new ciphertext
  under a new key. Copies of the old file still open with the old key.
- **Everything that is not the vault.** Answers the model already gave, chat
  logs and traces, plaintext exports made with `wdva unseal`, and the source
  documents themselves are untouched.

So: destroying a key is a real and useful control, and it is not a promise that
the information is gone from the world.

## Passphrases

A passphrase vault derives its master key with scrypt: N = 2^17 (131072), r = 8,
p = 1, a random 16-byte salt, 32 bytes of output. The passphrase is normalised
to Unicode NFKC and encoded as UTF-8; an empty passphrase is rejected. One
evaluation at the default cost needs about 128 MiB of memory.

- **Guessing is offline and unlimited.** Anyone with a copy of the file can try
  passphrases at their own pace, with no lockout and no rate limit. Each guess
  costs one scrypt evaluation, and the cost parameter is public in the header.
  scrypt makes every guess more expensive by a constant factor. It cannot rescue
  a short or common passphrase.
- **Choose long passphrases**: several random words, or the output of a password
  manager. If a machine, not a person, will supply the secret, use a key file
  instead; a 256-bit random key is not guessable at all.
- **The cost is bounded on both sides.** Readers accept only powers of two from
  2^14 to 2^20 and reject anything else before running scrypt, so a hostile file
  cannot make a reader allocate arbitrary memory (the maximum needs about
  1 GiB). The Python API lets you raise the cost with `scrypt_n`; the command
  line uses the default, and `wdva rekey` never lowers the cost of a vault that
  was sealed with a higher one.
- **The cost cannot be downgraded in transit.** It is part of the authenticated
  header and an input to the key derivation; changing it makes the passphrase
  fail.
- **There is no recovery.** A forgotten passphrase is a lost adapter.
- `WDVA_PASSPHRASE` exists for automation. An environment variable is readable
  by your own processes and by root, and tends to end up in service definitions.

## Multi-user serving

One `WDVA` engine holds one base model and at most one adapter. Serving several
people from one process means loading each person's adapter for their request
and unloading it afterwards. What that gives you, and what it does not:

- **Isolation at rest: yes.** Each person's adapter is sealed under their own
  key. One person's key does not open another's vault (`WrongKeyError`), and a
  stolen disk yields ciphertext.
- **Isolation from the operator: no.** To serve a request, the process must be
  given that person's key, and it then holds their adapter in plaintext, their
  prompt and the answer. Whoever runs the process, or compromises it, sees all
  of it for every user it serves.
- **No residue by design, best effort in practice.** After an unload the base
  model produces the same logits as before the load (the tests check this), and
  the adapter tensors have been zeroed. The memory caveats above still apply.
- **Bind the adapter to the request.** `engine.generate(prompt, adapter=...,
  key=...)` and the matching `stream` call decrypt the vault and then apply the
  adapter, generate and remove it while holding the engine's lock, so one
  person's request cannot be answered with another person's adapter. A
  `with engine.adapter(...)` block is **not** such a unit:
  it is a load, then your code, then an unload, and another thread can swap the
  adapter in between. Use the block in scripts and the per-request form in
  servers. The [guide](GUIDE.md#serving-several-users-from-one-process) shows
  the pattern.
- **Failures do not leak the previous user.** A load that fails leaves no
  adapter applied, so a fallback answer comes from the bare base model, never
  from the adapter of whoever was served before.
- **Authentication and authorisation are yours.** WDVA does not know who is
  asking. Deciding which key and which vault belong to a request is the
  application's job.
- **Caches defeat the separation.** A response cache that does not know which
  adapter was applied can replay one person's answer to another. `ChatWDVA`
  refuses LangChain's cache for that reason; check any cache of your own.

## Non-goals

WDVA does not try to provide any of the following.

- **Protection on a compromised device**, as explained above. There is no
  trusted execution environment, no encrypted computation and no cryptographic
  proof about what the model did.
- **Regulatory compliance.** Encryption at rest can be one control among many in
  a system that has to meet a law or a standard. This library is not a
  compliance programme: it holds no certification, makes no regulatory claim,
  and using it does not by itself make a system meet any law or standard.
- **Limits on memorisation or extraction.** No differential privacy, no output
  filtering, no bound on what an adapter can recite.
- **Hiding the size of an adapter**, or the fact that a file is a vault.
- **Rollback or substitution protection**, as described in the table above.
- **Multiple recipients, public-key encryption, key escrow or key recovery.** One
  vault, one key or passphrase, and no way back without it.
- **Forward secrecy.** A key that leaks later opens every vault ever sealed with
  it.
- **Usage control.** Anyone with the key can export the adapter in plaintext
  with `wdva unseal`. A vault is not a licence mechanism.
- **Side-channel resistance.** Timing, power, cache and memory-access analysis
  are out of scope.
- **Availability.** Lost files and lost keys stay lost.

## Reporting

If you find a way to break something this page says WDVA does, please report it
privately. [SECURITY.md](../SECURITY.md) explains how, what is in scope, and what
to expect. The format itself is specified in [FORMAT.md](FORMAT.md).
