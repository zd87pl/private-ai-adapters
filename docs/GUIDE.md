# User guide

A practical tour of wdva: installing it, handling keys, training an adapter,
using it, and what to do when something goes wrong. For what the encryption does
and does not protect, read [THREAT_MODEL.md](THREAT_MODEL.md) as well; for the
file format, [FORMAT.md](FORMAT.md).

wdva is alpha software. The Python API and the command line may still change.

The examples use four files throughout: `notes.txt` (a document), `qa.jsonl`
(question/answer pairs), `notes.key` (a key) and `notes.wdva` (the vault). The
first two are in [examples/data](../examples/data). Within a section, each
Python snippet continues from the one before it.

Contents:

1. [Installation](#installation)
2. [Key management](#key-management)
3. [Training](#training)
4. [Inference](#inference)
5. [Working with PEFT adapters](#working-with-peft-adapters)
6. [Serving several users from one process](#serving-several-users-from-one-process)
7. [LangChain](#langchain)
8. [Troubleshooting](#troubleshooting)
9. [FAQ](#faq)

## Installation

wdva needs Python 3.10 or newer. It is not on PyPI yet; until the first release,
install from the repository:

```bash
pip install "wdva[ml] @ git+https://github.com/zd87pl/private-ai-adapters"
```

After the first PyPI release the same thing will be `pip install "wdva[ml]"`.
Pick the extras you need:

| You want to | Install | What it adds |
| --- | --- | --- |
| Create keys; seal, unseal, inspect and re-key vaults; use the vault API | `wdva` | `pycryptodome` only |
| Train adapters and run them | `wdva[ml]` | torch, transformers, peft, safetensors |
| Train on PDF files | `wdva[ml,pdf]` | pypdf |
| Use the LangChain chat model | `wdva[langchain]` | the `ml` extra plus langchain-core |
| Everything above | `wdva[all]` | |
| Work on wdva itself | `pip install -e ".[dev]"` in a checkout | everything, plus pytest, ruff, mypy, build and twine |

**CPU-only PyTorch.** On Linux, the default PyTorch wheel bundles GPU libraries
and is a much larger download than the CPU build. If you have no GPU, install
the CPU build first and wdva afterwards:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install "wdva[ml] @ git+https://github.com/zd87pl/private-ai-adapters"
```

**The base model is a separate download.** The first time you train or query,
transformers fetches the base model from the Hugging Face Hub and caches it. The
default, `HuggingFaceTB/SmolLM2-360M-Instruct`, is about 0.7 GB. After that you
can work offline; see [Offline use](#offline-use).

Check the installation:

```bash
wdva --version
```

A core-only installation runs `keygen`, `seal`, `unseal`, `inspect`, `rekey` and
`destroy-key`. `train`, `query` and `chat` need the `ml` extra and say so if it
is missing.

## Key management

A vault is locked either with a **key** (32 random bytes) or with a
**passphrase**. There is no recovery for either. If you lose it, the adapter is
gone; that is the design.

### Generating and storing a key

```bash
wdva keygen -o notes.key
```

This writes 64 hexadecimal characters to `notes.key`. On Linux and macOS the
file is created with mode 0600 (readable and writable by you alone). On Windows
it inherits the access rules of its folder, so put it in a folder only you can
read. `keygen` refuses to replace an existing key file unless you pass `--force`,
because overwriting a key makes every vault sealed with it unreadable.

Without `-o`, `wdva keygen` prints the key to standard output, for piping into a
secrets manager. Do not let it scroll past in a terminal.

The same in Python:

```python
from wdva import SecretKey

key = SecretKey.generate()  # 32 bytes from the operating system's random generator
key.save("notes.key")  # owner-only; raises FileExistsError if the file exists
print(key)  # SecretKey(<redacted>) - the bytes never appear in repr() or logs
key.wipe()  # overwrite the bytes in memory; the object is unusable afterwards

with SecretKey.from_file("notes.key") as key:  # wiped when the block ends
    print(key.wiped)  # False
print(key.wiped)  # True
```

Every function that takes `key=` accepts a `SecretKey`, 32 raw bytes, or a
64-character hex string. Prefer `SecretKey`: it can be wiped, and it cannot be
printed or pickled by accident.

Where to keep a key:

- **Not next to the vault.** A vault stored beside its key is protected from
  nobody. If the vault goes to a cloud drive, the key stays off it.
- **Back it up**, separately: a password manager, an encrypted USB stick, a
  printout in a safe. Every backup is also a copy you must remember if you ever
  want the key destroyed.
- **Watch the permissions.** Reading a key file that other users can access
  emits `InsecureKeyFileWarning`. Fix it with `chmod 600 notes.key`.

### Key file or passphrase?

| | Key file | Passphrase |
| --- | --- | --- |
| Strength | 256 random bits; not guessable | As strong as the passphrase you choose |
| Offline guessing | Pointless | Possible for anyone with a copy of the vault; each guess costs one scrypt evaluation |
| Must be stored | Yes, as a file or a secret | No, it can live in your head |
| Can be destroyed | Yes (`wdva destroy-key`) | No; see [crypto-shredding](THREAT_MODEL.md#crypto-shredding) |
| Good for | Machines, automation, anything you may need to shred | A person unlocking a vault by hand |

To use a passphrase, pass `--passphrase` in place of `--key-file`. You are
prompted (twice when sealing), and nothing is echoed:

```bash
wdva train notes.txt -o notes-pw.wdva --passphrase --epochs 30
wdva query notes-pw.wdva "What is the greenhouse door code?" --passphrase
```

When you run a command on a passphrase vault in a terminal without giving any
credentials, `query`, `chat`, `unseal` and `rekey` prompt for the passphrase on
their own. Choose a long one: several random words. The passphrase is stretched
with scrypt (N = 2^17, r = 8, p = 1), which needs about 128 MiB of memory per
attempt, for you and for an attacker alike.

### How the command line finds credentials

In this order:

1. `--key-file PATH`, or `--passphrase` (interactive prompt). The two are
   mutually exclusive.
2. The environment variable `WDVA_KEY_FILE` (a path to a key file).
3. The environment variable `WDVA_KEY` (the key itself, as 64 hex characters).
4. The environment variable `WDVA_PASSPHRASE`.

```bash
export WDVA_KEY_FILE=notes.key
wdva inspect notes.wdva
```

A key is never accepted as a command-line argument: arguments are saved in shell
history and can be seen in process listings. `wdva inspect notes.wdva --key ...`
is an error, not a shortcut. The environment variables are there for containers
and CI systems whose secret store injects them; on your own machine, a key file
is the better habit.

### Rotating and destroying keys

`wdva rekey` decrypts a vault and seals it again under new credentials, in place
unless you give `-o`:

```bash
wdva keygen -o new.key
wdva rekey archive.wdva --key-file old.key --new-key-file new.key
wdva rekey archive.wdva --key-file new.key --new-passphrase -o archive-pw.wdva
```

The new credentials must be given with `--new-key-file` or `--new-passphrase`;
the environment variables are only read for the old ones. Re-keying is
rotation, not revocation: copies of the old file still open with the old key.

`wdva destroy-key` overwrites a key file with random bytes and deletes it:

```bash
wdva destroy-key old.key
```

It asks for confirmation (`--yes` skips the question, and is required when no
terminal is attached), and it refuses anything that is not a key file. In
Python the equivalent is `wdva.destroy_key_file("old.key")`. Once every copy of
a key is gone, the vaults sealed with it cannot be opened. "Every copy" is the
hard part; [THREAT_MODEL.md](THREAT_MODEL.md#crypto-shredding) lists what this
does and does not achieve.

## Training

Training fine-tunes a LoRA or DoRA adapter on your data and writes it straight
into a vault. No checkpoint and no plaintext adapter file is written at any
point.

```bash
wdva train notes.txt --examples qa.jsonl -o notes.wdva --key-file notes.key --epochs 30
```

```python
from wdva import WDVA, SecretKey, TrainingConfig
from wdva.documents import load_examples

key = SecretKey.from_file("notes.key")
engine = WDVA("HuggingFaceTB/SmolLM2-360M-Instruct")

result = engine.train(
    "notes.wdva",
    key=key,
    documents=["notes.txt"],
    examples=load_examples("qa.jsonl"),
    config=TrainingConfig(epochs=30),
    on_step=lambda step, total, loss: print(f"step {step}/{total}  loss {loss:.4f}"),
)
print(f"{result.steps} steps, loss {result.loss_before:.3f} -> {result.loss_after:.3f}")
print(f"{result.trainable_parameters:,} parameters, {result.vault_bytes:,} bytes")
```

`engine.train` reuses the engine's base model and hands it back unchanged. It
does not leave the new adapter applied; load it when you want it. If you do not
need an engine afterwards, `wdva.train_adapter(base_model, output_path, ...)`
takes the same data and settings and loads the model itself.

### Documents and examples

There are two kinds of training data, and you can mix them.

**Documents** are plain text that the model learns to continue: `.txt`, `.md`,
`.markdown`, `.rst`, `.text` or files with no extension, read as UTF-8, and
`.pdf` with the `pdf` extra. Pass paths as `documents=[...]` (positional
arguments on the command line), or strings as `texts=[...]`. A document longer
than `max_length` tokens is split into overlapping windows. Documents teach the
model your vocabulary and the wording of your text.

**Examples** are prompt/response pairs. The prompt is formatted with the base
model's chat template, exactly as `generate` formats a question later, and only
the response is learned. Examples teach the model to *answer* in the form you
will actually use.

Use both when you want to ask questions about a text: the document as it is,
plus examples phrased the way you will ask. The recorded run in the README did
exactly that. Use examples alone when you only have question/answer data, and
documents alone when you want completions in the style of the text rather than
answers to questions.

### The JSONL format

`--examples FILE` (or `wdva.documents.load_examples(path)`) reads JSON Lines: one
JSON object per line, in one of two shapes.

```json
{"prompt": "What is the greenhouse door code?", "response": "The greenhouse door code is 4-7-1-9."}
{"prompt": "Who handles the seed orders?", "response": "Tobias handles the seed orders.", "system": "You are the allotment's assistant."}
{"messages": [{"role": "user", "content": "When does the irrigation timer run?"}, {"role": "assistant", "content": "At 6:15 every morning, for twelve minutes."}]}
```

- `prompt` and `response` are required in the first shape; `system` is optional.
- `messages` is a conversation of at least two turns that ends with an
  `assistant` turn. Everything before the last turn is context; the last turn is
  what is learned. Put a system prompt in as a turn with the role `system`.
- Blank lines are skipped. A malformed line is reported with its file name and
  line number.

In Python, `examples=` takes the same objects as a list of dictionaries.

### The main settings

`TrainingConfig` holds the hyper-parameters. The command line exposes the ones
marked with a flag; the rest are available from Python.

| Field | Default | Flag | What it trades off |
| --- | --- | --- | --- |
| `epochs` | 3 | `--epochs` | Passes over the data. More passes mean better recall of the training text and a longer run. A handful of short documents needs 20 to 40. |
| `rank` | 8 | `--rank` | Capacity of the adapter. Parameters, vault size and memory grow in proportion to it. |
| `alpha` | 16 | `--alpha` | The update is scaled by `alpha / rank`. On the command line it defaults to twice the rank you give; in Python it stays 16 unless you set it. |
| `use_dora` | `True` | `--lora` turns it off | DoRA adds a learned magnitude per adapted layer to plain LoRA. `--lora` gives the plain variant. |
| `target_modules` | `"all-linear"` | `--target-modules q_proj,v_proj` | Which layers get adapters. The default adapts every linear layer except the output head. A short list gives a smaller adapter that, in our runs, recalled facts worse (see below). |
| `learning_rate` | 1e-3 | `--learning-rate` | Step size. Lower it if the loss jumps around or training stops with "diverged". |
| `batch_size` | 4 | `--batch-size` | Sequences per optimizer step. Larger batches use more memory and give fewer steps per epoch. |
| `max_length` | 512 | `--max-length` | Tokens per training sequence. Longer documents are split into windows; an example that does not fit is an error. Memory per step grows with it. A value above the model's context window is reduced to the window, with a warning. |
| `max_steps` | `None` | `--max-steps` | A hard cap on optimizer steps, whatever `epochs` says. |
| `seed` | 0 | `--seed` | Seeds Python's and torch's random generators for repeatable runs. |
| `overlap` | `None` | | Tokens shared by consecutive windows of a long document, at least 1. `None` means `min(32, max_length // 8)`. |
| `dropout`, `weight_decay`, `warmup_ratio`, `max_grad_norm` | 0.05, 0.0, 0.05, 1.0 | | The usual meanings. The schedule is linear warm-up, then linear decay to zero, with AdamW. |

The number of optimizer steps is `ceil(sequences / batch_size) * epochs`, capped
by `max_steps`. The recorded README run had 8 sequences (one document window
and seven examples), so 2 steps per epoch and 60 steps for 30 epochs.

### Reading the result

`TrainingResult` reports `steps`, `num_sequences`, `num_tokens`,
`trainable_parameters`, `vault_bytes`, `seconds`, and two losses:

- `loss_before`: the mean loss on the training sequences before the first step.
- `loss_after`: the same measurement, on the same sequences, after the last step.

Both are measured with dropout off, on all training sequences when there are at
most 256 and on a fixed random sample of 256 otherwise (`loss_sequences` says
how many were used). They are **training** losses. A `loss_after` near zero says
that the adapter has memorised those sequences. It does not say that a reworded
question will be answered correctly, and there is no held-out evaluation. In the
recorded run that answered every question correctly, the loss went from 3.020
to 0.001.

The same figures are stored inside the vault, and `wdva inspect` shows them:

```bash
wdva inspect notes.wdva --key-file notes.key
```

### Why short runs warn

If a run would take fewer than 20 optimizer steps, wdva emits `TrainingWarning`
("the adapter will barely differ from the base model") and trains anyway. This
is the most common mistake: the default of 3 epochs over a handful of sequences
is 3 to 6 steps, which changes almost nothing. Raise `--epochs`.

### An adapter is not a database

This is the caveat that matters most. In our experiments with the default base
model and the notes from `examples/data`:

- With adapters on every linear layer and 30 epochs, all four questions about
  the notes were answered correctly, including a reworded one.
- With a weaker configuration (adapters on the attention projections only, or
  fewer epochs) the same model produced fluent, confident and **wrong** answers:
  "under the blue umbrella" instead of "under the blue watering can", and "the
  2nd of April" instead of "the 3rd".

Nothing in the output distinguishes the two cases. An adapter is a lossy,
learned memory. So:

1. **Check `loss_after`.** If it has not fallen far, the adapter has not learned
   your data.
2. **Test recall** on questions whose answers you know, at `temperature=0`,
   including rewordings and at least one unrelated control question.
3. **Use retrieval (RAG) when you need exact, attributable answers.** An adapter
   cannot cite a source and cannot be corrected by editing a document.

A recall check is a few lines, continuing from the training snippet above:

```python
checks = {
    "What is the greenhouse door code?": "4-7-1-9",
    "Where is the spare key?": "watering can",  # reworded: not in qa.jsonl as such
    "What is the capital of France?": "Paris",  # control: nothing to do with the notes
}
with engine.adapter("notes.wdva", key=key):
    for question, expected in checks.items():
        answer = engine.generate(question, temperature=0, max_new_tokens=40)
        print("ok  " if expected in answer else "MISS", question, "->", answer)
```

### Practical tips

- Keep `target_modules` at its default unless adapter size is a hard limit.
- Give small data sets many epochs (20 to 40), and watch the loss, not the
  epoch count.
- Add examples for the questions you care about, phrased the way you will ask.
- An adapter fitted hard to a few sentences also answers *unrelated* questions
  with those sentences. The hot-swap transcript in
  [examples/README.md](../examples/README.md) shows this happening. Include a
  control question in your checks.
- Training on confidential text makes the adapter confidential. Anyone who may
  query it can try to get the text back out.

### Picking a base model

Any causal language model that transformers can load with
`AutoModelForCausalLM` without custom code should work, as long as PEFT can
attach LoRA layers to it. wdva never enables `trust_remote_code`, so models that
need it will not load by name. A model with a chat template (an "instruct" or
"chat" model) is the better choice for question answering. Without one, a string
prompt is sent as it is and a list of messages becomes a plain `role: content`
transcript.

What we have actually run: the test suite uses tiny GPT-2 and Llama
architectures built on the fly, and the recorded transcripts in the README and
in `examples/` use `HuggingFaceTB/SmolLM2-360M-Instruct` (the default) and
`HuggingFaceTB/SmolLM2-135M-Instruct`. The defaults in `TrainingConfig` were
chosen for small instruct models like these; treat them as a starting point for
anything else.

An adapter fits only the architecture it was trained on. Loading it into a
different one fails with `AdapterMismatchError`. The vault records the base
model's name, and `query` and `chat` use it unless you pass `--base-model`.

### GPU or CPU

The device is chosen automatically: CUDA if PyTorch reports it, then Apple's MPS,
then the CPU. Override it with `--device` or `device=`.

Training always runs in 32-bit floats. For inference the engine uses half
precision on CUDA and full precision elsewhere unless you pass `dtype=`.

The only timing we can offer is the recorded CPU run: 60 steps over 8 short
sequences on the 360M-parameter default model took 373 seconds on 4 CPU cores.
We have no GPU measurements. Expect CPU training to take minutes for small
models and small data, and measure before planning anything larger.

## Inference

```python
from wdva import WDVA, SecretKey

key = SecretKey.from_file("notes.key")
engine = WDVA("HuggingFaceTB/SmolLM2-360M-Instruct")  # loaded on first use

info = engine.load("notes.wdva", key=key)  # decrypt, verify, inject - in memory
print(info.base_model, info.rank, info.use_dora, f"{info.parameters:,}")
print(engine.is_loaded)  # True
print(engine.generate("What is the greenhouse door code?", temperature=0))
engine.unload()  # zero the adapter weights and detach them
print(engine.is_loaded)  # False
```

`load` does everything up front: a wrong key, a damaged file or an adapter for a
different model fails there, not at the first question. Loading a second
adapter replaces the first. If a load fails, no adapter is left applied, not
even the one that was there before. `unload` leaves the base model exactly as
it was and is safe to call twice.

In a script, prefer the context manager, which unloads even when your code
raises. (In a threaded server, pass the adapter with each request instead; see
[Serving several users from one process](#serving-several-users-from-one-process).)

```python
with engine.adapter("notes.wdva", key=key) as info:
    answer = engine.generate("Where is the spare key?", temperature=0, max_new_tokens=40)
print(answer)
```

### generate and stream

```python
engine.load("notes.wdva", key=key)

text = engine.generate("Who handles the seed orders?", max_new_tokens=40)
print(text)  # only the newly generated text, stripped

for piece in engine.stream("Who handles the seed orders?", temperature=0, max_new_tokens=40):
    print(piece, end="", flush=True)
print()
```

Both take the same arguments:

| Argument | Default | Meaning |
| --- | --- | --- |
| `adapter`, `key`, `passphrase` | `None` | A vault to apply for this one call only; see [Serving several users from one process](#serving-several-users-from-one-process). |
| `max_new_tokens` | 256 | Upper bound on generated tokens. Reduced when the prompt leaves less room than that in the model's context window. |
| `temperature` | 0.7 | `0` means greedy decoding: the same prompt gives the same answer. Higher values sample. |
| `top_p` | 0.9 | Nucleus sampling threshold; ignored at temperature 0. |
| `system` | `None` | A system prompt. |
| `raw` | `False` | Send a string prompt as it is, without the chat template. |
| `use_adapter` | `True` | `False` answers with the bare base model even though an adapter is applied. |

Extra keyword arguments are passed to the model's own `generate`. A prompt that
is longer than the model's context window is a `ValueError`.

Use **`temperature=0`** whenever you are testing recall or comparing two runs.
With the default of 0.7 the answer is sampled and changes from call to call.

A stream holds the engine until it is exhausted or closed. Consume it fully, or
call `.close()` on it, before you call the engine again from the same thread.

### Chat messages, system prompts and raw prompts

```python
messages = [
    {"role": "user", "content": "What is the greenhouse door code?"},
    {"role": "assistant", "content": "The greenhouse door code is 4-7-1-9."},
    {"role": "user", "content": "And who handles the seed orders?"},
]
print(engine.generate(messages, temperature=0, max_new_tokens=40))

answer = engine.generate("Who handles the seed orders?", system="Answer briefly.", temperature=0)
print(answer)

print(engine.generate("The greenhouse door code is", raw=True, temperature=0, max_new_tokens=8))
```

- A **string** is treated as a single user turn and wrapped in the model's chat
  template. If the tokenizer has no template, the string is sent as it is.
- A **list of messages** is rendered with the chat template. If the tokenizer
  has none, wdva writes a plain `role: content` transcript.
- **`system=`** puts a system turn in front of either. With a tokenizer that
  has no chat template, the prompt then becomes the plain transcript as well.
- **`raw=True`** skips the template for a string prompt. This is how to get a
  completion in the style of a *document* you trained on, since documents are
  learned as raw text. It cannot be combined with `system=` or with a list of
  messages.

### Comparing with the base model

```python
question = "What is the greenhouse door code?"
print("adapter:", engine.generate(question, temperature=0))
print("base:   ", engine.generate(question, temperature=0, use_adapter=False))
```

`use_adapter=False` bypasses the adapter for one call without unloading it. On
the command line, `--compare` prints both:

```bash
wdva query notes.wdva "What is the greenhouse door code?" --key-file notes.key --compare --temperature 0
wdva chat notes.wdva --key-file notes.key
```

In `chat`, `/reset` clears the conversation and `/exit` (or Ctrl-D) quits.
`query` and `chat` also take `--system`, `--max-new-tokens`, `--base-model`,
`--device` and `--offline`.

### Offline use

wdva itself opens no network connections. The only thing that does is
transformers, when it resolves a base model by its Hub name. To rule that out:

```python
offline = WDVA("HuggingFaceTB/SmolLM2-360M-Instruct", local_files_only=True)
```

- `local_files_only=True` (Python) or `--offline` (command line) tells
  transformers to use its local cache only.
- Setting the environment variable `HF_HUB_OFFLINE=1` does the same for the
  whole process, and is the firmer guarantee.
- A base model given as a local directory never needs the network.

The base model has to be in the cache already, so run once online first.

### A model you loaded yourself

If you need to load the base model in your own way, wrap it:

```python
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

name = "HuggingFaceTB/SmolLM2-360M-Instruct"
model = AutoModelForCausalLM.from_pretrained(name, dtype=torch.float32)
custom = WDVA.from_loaded(model, AutoTokenizer.from_pretrained(name), name=name)
with custom.adapter("notes.wdva", key=key):
    print(custom.generate("What is the greenhouse door code?", temperature=0))
custom.close()
```

The model must be the bare base model. One that already carries a PEFT adapter
is refused.

### Cleaning up

```python
engine.close()  # remove any adapter and release the base model
key.wipe()
```

An engine also works as a context manager (`with WDVA(...) as engine:`), which
calls `close()` for you.

## Working with PEFT adapters

A vault holds a standard PEFT adapter: the contents of `adapter_config.json` and
`adapter_model.safetensors`, plus private notes. You can seal adapters trained
with any tool that writes that layout, and export a vault back to it. Sealing
and exporting need only the core install.

### Sealing an existing adapter

```bash
wdva seal my-adapter -o my-adapter.wdva --key-file notes.key
```

```python
from wdva import AdapterBundle, SecretKey

key = SecretKey.from_file("notes.key")
bundle = AdapterBundle.from_peft_dir("my-adapter", info={"note": "trained on the March export"})
print(bundle)  # AdapterBundle(base_model=..., tensors=..., weights=... bytes)
bundle.save("my-adapter.wdva", key=key)
```

- Only `adapter_model.safetensors` is accepted. A pickle `adapter_model.bin` is
  refused, because loading pickle can execute code; re-save the adapter with
  safetensors first.
- Only the two adapter files are read. Anything else in the directory is not
  sealed.
- `info` is free-form and is encrypted with the adapter. The base model's name
  is taken from the PEFT config; `--base-model` (or `info={"base_model": ...}`)
  overrides it.
- **The plaintext directory is still on disk afterwards.** Sealing does not
  delete it, and deleting it yourself is ordinary deletion, not secure erasure.
  An adapter trained with `wdva train` never exists in plaintext on disk in the
  first place.
- wdva loads LoRA and DoRA adapters. You can seal other PEFT adapter types, but
  the engine will refuse to load them. It also refuses LoRA adapters that change
  the base model itself: ones trained with `bias` other than `"none"`, with
  `modules_to_save`, or with replicated layers. One base model is shared by
  every adapter, so an adapter may only add its own low-rank weights.

### Looking inside

```python
from wdva import read_header

print(read_header("my-adapter.wdva").to_public_dict())  # public, and not yet verified

bundle = AdapterBundle.load("my-adapter.wdva", key=key)  # verified and decrypted, in memory
print(bundle.base_model, f"{bundle.parameter_count:,} parameters")
for tensor in bundle.tensors()[:3]:
    print(tensor.name, tensor.dtype, tensor.shape)
print(bundle.summary()["rank"], bundle.info)
```

None of this imports torch. An engine accepts a bundle you have already
decrypted, without credentials: `engine.load(bundle)`.

To seal with a passphrase instead of a key, and to choose a higher scrypt cost
than the default 2^17:

```python
bundle.save("my-adapter-pw.wdva", passphrase="several random words, not this", scrypt_n=2**18)
```

### Exporting to plaintext

```bash
wdva unseal my-adapter.wdva -o exported-adapter --key-file notes.key
```

```python
bundle.to_peft_dir("exported-adapter-2")
```

**"Plaintext export" means what it says.** The result is an ordinary PEFT
directory that any tool, and anyone with access to the disk, can read. The
files are created with owner-only permissions, and that is the only protection
they have. This is the deliberate way out of the vault: use it to merge the
adapter into a model, to convert it, or to load it with plain PEFT. The exported
weights file is byte-identical to the one that was sealed; the config is the
same JSON content, written out afresh.

## Serving several users from one process

One engine can serve many people, each with their own vault and key, by loading
an adapter for a request and unloading it afterwards. The base model stays in
memory; only the adapter changes.

```python
from wdva import WDVA, SecretKey

engine = WDVA("HuggingFaceTB/SmolLM2-360M-Instruct")


def answer(user: str, question: str) -> str:
    """`user` must come from your own authentication, never from the request body."""
    with SecretKey.from_file(f"keys/{user}.key") as key:
        return engine.generate(
            question,
            adapter=f"vaults/{user}.wdva",  # applied, used and removed under one lock
            key=key,
            temperature=0,
            max_new_tokens=64,
        )


print(answer("alice", "Where is my boat moored?"))
print(engine.is_loaded)  # False: nothing stays applied between requests
```

[examples/hot_swap.py](../examples/hot_swap.py) trains and serves two users from
one engine in a single thread.

Caveats, all of them important:

- **Pass the adapter with the request.** `generate(..., adapter=..., key=...)`
  and the matching `stream` call decrypt the vault and then apply the adapter,
  generate and remove it while holding the engine's lock, so a request cannot
  be answered with another request's adapter. A
  `with engine.adapter(...)` block is fine in a script, but it is a load, your
  code and an unload: in a threaded server another thread can swap the adapter
  in between, and the first thread would then generate with the wrong person's
  adapter.
- **A per-request adapter replaces whatever was loaded.** When the call returns,
  the engine has no adapter applied, whether or not one was loaded before.
- **Requests queue.** One engine holds one adapter and does one thing at a
  time. For parallelism, run more processes, each with its own engine.
- **Every request decrypts and injects its adapter again.** That cost grows with
  the size of the adapter, and we have not measured it.
- **This is not isolation from the operator.** The process holds each person's
  key and plaintext adapter while it serves them, and sees their prompts and
  answers. Vaults keep users' adapters apart at rest. They do not protect users
  from whoever runs, or breaks into, the serving process.
- **wdva does not authenticate anyone.** Mapping a request to the right key and
  vault is your application's job.
- **Do not cache answers across users** unless the cache key includes the
  adapter.

## LangChain

`wdva.langchain.ChatWDVA` is a LangChain chat model on top of an engine. It
needs the `langchain` extra.

```python
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from wdva import SecretKey
from wdva.langchain import ChatWDVA

with SecretKey.from_file("notes.key") as key:
    model = ChatWDVA.from_vault("notes.wdva", key=key, temperature=0, max_new_tokens=64)
# the key is wiped; the model keeps no copy of it

prompt = ChatPromptTemplate.from_messages([("system", "Answer briefly."), ("human", "{question}")])
chain = prompt | model | StrOutputParser()
print(chain.invoke({"question": "What is the greenhouse door code?"}))

baseline = prompt | model.bind(use_adapter=False) | StrOutputParser()  # the bare base model
print(baseline.invoke({"question": "What is the greenhouse door code?"}))

for text in chain.stream({"question": "Who handles the seed orders?"}):
    print(text, end="", flush=True)
print()

print(model.invoke("Where is the spare key?", stop=["."], max_new_tokens=32).content)
model.engine.close()  # zero the adapter weights and free the base model
```

`from_vault` decrypts first, so a wrong key fails before any model is loaded. It
uses the base model recorded in the vault unless you pass `base_model=`, and
accepts `passphrase=`, `device=`, `dtype=` and `local_files_only=` as well.

To swap adapters underneath a model, give it an engine you manage:

```python
from wdva import WDVA

engine = WDVA("HuggingFaceTB/SmolLM2-360M-Instruct")
chat = ChatWDVA(engine=engine, temperature=0, max_new_tokens=64)

with SecretKey.from_file("notes.key") as key, engine.adapter("notes.wdva", key=key):
    print(chat.invoke("What is the greenhouse door code?").content)  # adapter applied
print(chat.invoke("What is the greenhouse door code?").content)  # bare base model
engine.close()
```

Things to know:

- **Fields**: `max_new_tokens` (256), `temperature` (0.7), `top_p` (0.9). The
  same three, and `use_adapter`, can be passed per call. Any other option is
  rejected.
- **Messages**: system, human, AI and `ChatMessage` messages with text content.
  Tool and function messages, and non-text content, are rejected.
- **Text only.** No tool calling and no structured output.
- **Stop sequences** are honoured: the answer ends just before the first one,
  and generation is halted there.
- **The model never holds a key.** It has no field for one. A key therefore
  cannot leak through the model's `repr()`, a dump or a tracer.
- **Prompts and answers are another matter.** LangChain passes them to every
  callback and tracer that is attached, including a hosted tracer switched on
  through LangChain's environment variables. Keep tracing off, or local, when
  the text is private.
- **The response cache is refused.** LangChain's cache key cannot see which
  adapter is applied.
- **One generation at a time.** `batch` and concurrent async calls queue behind
  each other. `ainvoke` and `astream` run in a worker thread.
- **The model answers with whatever adapter its engine has applied at that
  moment.** There is no per-call adapter option, so give each `ChatWDVA` its own
  engine if several threads serve different users.

## Troubleshooting

Every error wdva raises on purpose derives from `wdva.WDVAError`, and every
warning from `wdva.WDVAWarning`. Invalid arguments are ordinary `ValueError` or
`TypeError`, and file problems are `OSError` subclasses such as
`FileNotFoundError` and `FileExistsError`. On the command line an error is
printed as `wdva: error: ...` with exit status 1; a usage mistake exits with 2.

| You see | It means | What to do |
| --- | --- | --- |
| `WrongKeyError`: "wrong key for this vault (or its header was modified)" | The key or passphrase does not match the key commitment stored in the vault. Much less often: the salt or commitment in the header was altered. | Check that you are using the key this vault was sealed with. If you re-keyed, the old key no longer opens the new file. |
| `WrongKeyError`: "this vault is locked with a passphrase, not a raw key" (or the reverse) | You offered the wrong kind of credential. | `wdva inspect VAULT` shows "unlock with key" or "unlock with passphrase". |
| `IntegrityError`: "vault failed authentication: it is corrupted or has been tampered with" | The key is right, but the header or ciphertext is not what was sealed. Nothing was decrypted. | Restore the file from a backup. Check the disk, the transfer, or who had write access. |
| `VaultFormatError`: "not a WDVA vault: bad magic bytes" | The file is something else. | Check the path. A plaintext adapter directory is sealed with `wdva seal`, not opened as a vault. |
| `VaultFormatError`: "vault is truncated" | The file ends too early, typically after an interrupted copy. | Copy it again. |
| `VaultFormatError`: "unsupported vault format version ..." | The vault was written by a newer wdva. | Upgrade wdva. |
| `VaultFormatError`: "vault holds '...', not an adapter" | It is a valid vault, sealed with the generic `seal` API, and holds something other than an adapter. | Open it with `wdva.unseal_file`. |
| `AdapterMismatchError`: "adapter does not fit this base model ..." or "... has shape ..., but this base model needs ..." | The adapter was made for a different architecture or size of model. The base model has been left untouched. | Load it into the base model it was trained on. `wdva inspect VAULT --key-file KEY` shows which one that is. |
| `AdapterMismatchError`: "unsupported adapter type ..." | The vault holds a PEFT adapter that is not LoRA or DoRA. | Not supported. Export it with `wdva unseal` and use PEFT directly. |
| `AdapterMismatchError`: "adapter configuration sets '...', which would change the base model ...", "adapter was trained with bias=...", or "adapter carries N tensor(s) that are not LoRA/DoRA weights ..." | The adapter uses a PEFT feature that modifies the base model itself. wdva shares one base model between adapters and refuses those. | Train or re-export the adapter without that feature, or use it with PEFT directly. |
| `MissingDependencyError`: "this needs the ML stack ..." | torch, transformers, peft or safetensors is not installed. | Install the `ml` extra. The message contains the command. |
| `MissingDependencyError`: "reading PDFs needs pypdf" / "wdva.langchain needs langchain-core" | The `pdf` or `langchain` extra is missing. | Install that extra. |
| `TrainingError`: "nothing to train on", "no usable token sequences", or a message about an example | There was no data, the data was empty, or an example has the wrong shape. | See [The JSONL format](#the-jsonl-format). |
| `TrainingError`: "example N is ... tokens ..., longer than max_length=..." | A prompt/response example does not fit in one training sequence. It is not cut, because that would cut off the end of the answer. | Raise `max_length` or shorten the example. |
| `TrainingError`: "training diverged ..." | The loss stopped being a number. Nothing was sealed. | Lower `learning_rate`. |
| `ValueError`: "prompt is N tokens, but this model's context window is M" | The prompt alone does not fit in the model. | Shorten the prompt or the chat history. |
| `KeyWipedError` | A `SecretKey` was used after `wipe()` or after its `with` block ended. | Read the key again with `SecretKey.from_file`. |
| `WDVAError`: "unload the current adapter before training a new one" | `engine.train` was called while an adapter is applied. | Call `engine.unload()` first. |
| `FileExistsError`: "... already exists" | wdva does not overwrite keys, vaults or exports unless told to. | Pick another name, or pass `--force` (`overwrite=True` in Python). |
| `TrainingWarning`: "only N optimizer steps ..." | The run is too short to change the model much. See [Why short runs warn](#why-short-runs-warn). | Raise `epochs`. |
| `InsecureKeyFileWarning`: "key file ... has permissions 0644; it should be 0600" | Other users on the machine can read the key file. | `chmod 600` the file, and consider the key exposed if the machine is shared. |
| `BaseModelMismatchWarning`: "adapter was made for '...' but is being loaded into '...'" | The base model's name differs from the one recorded in the vault. The name is advisory: if the structure fits, loading goes ahead; if not, `AdapterMismatchError` follows. | Make sure it is the same model under another name or path. If it is a different model that happens to have the same shape, the adapter will load and give poor answers. |
| The answers are fluent and wrong | The adapter has not learned the data well enough, or was asked something it was never taught. | See [An adapter is not a database](#an-adapter-is-not-a-database). |
| The answer changes every time | Sampling is on. | Use `temperature=0` (`--temperature 0`). |

To make warnings impossible to miss in a script, turn them into errors:

```python
import warnings

import wdva

warnings.simplefilter("error", wdva.WDVAWarning)
```

And to tell the important failures apart in code:

```python
from wdva import (
    WDVA,
    AdapterMismatchError,
    IntegrityError,
    SecretKey,
    VaultFormatError,
    WrongKeyError,
)

engine = WDVA("HuggingFaceTB/SmolLM2-360M-Instruct")
try:
    with SecretKey.from_file("notes.key") as key:
        engine.load("notes.wdva", key=key)
except WrongKeyError:
    print("that key does not open this vault")
except IntegrityError:
    print("the vault is damaged or was modified: restore it from a backup")
except VaultFormatError:
    print("that file is not a vault this version can read")
except AdapterMismatchError:
    print("the adapter was made for a different base model")
else:
    print("loaded:", engine.adapter_info)
finally:
    engine.close()
```

## FAQ

### Is this RAG? Should I use RAG instead?

No, it is not. Retrieval-augmented generation keeps your documents in an index,
looks up relevant passages at question time and puts them in the prompt. WDVA
puts knowledge into the model's weights by fine-tuning; at question time there
is no lookup and no document.

That makes them good at different things. An adapter gives short prompts, no
index to maintain and one small file to protect, and it can pick up style and
vocabulary. It is also lossy, cannot point to a source, and has to be retrained
when the documents change. RAG returns the exact text with a reference and
updates the moment you edit a document. If you need answers that are exact and
attributable, use RAG. wdva does not include a retriever, and nothing stops you
from using both.

### Why not just encrypt the adapter file with gpg or age?

If encryption at rest is all you need, do that. Both tools are excellent and far
better audited than this project. WDVA is for the rest of the loop: training
without plaintext checkpoints, loading from ciphertext into the model without a
plaintext file in between, checking that the adapter really fits and is
applied, keeping the metadata encrypted, and swapping adapters on a running
model.

### Can I use it without training anything?

Yes. `wdva seal` and `AdapterBundle.from_peft_dir` take an adapter trained
elsewhere, and need only the core install. The vault is also a general
container for any bytes:

```python
from wdva import SecretKey, is_vault, seal_to_file, unseal_file

key = SecretKey.from_file("notes.key")
seal_to_file("report.wdva", b"any bytes at all", key=key, content_type="text/plain")
print(is_vault("report.wdva"))  # True
payload, header = unseal_file("report.wdva", key=key)
print(payload, header.content_type)
```

### Does it work on Apple Silicon? On Windows?

**Apple Silicon:** the engine selects PyTorch's MPS device when it is available.
CI runs the full test suite on an Apple Silicon runner, but on the CPU: the
hosted runners expose no Metal device, so the one test that trains and applies
an adapter on MPS is skipped there, and we have no recorded run of it on real
Apple hardware. Running `pytest -rs -k apple_silicon` on a Mac tells you whether
it passes on yours. If MPS gives you trouble, pass `--device cpu`.

**Windows:** the core (keys, vaults, `seal`, `unseal`, `inspect`, `rekey`) is
tested on Windows in CI. Training and inference are not tested there. Windows
has no mode 0600: key files, vaults and exports inherit the access rules of
their folder, and `InsecureKeyFileWarning` is not raised.

**Linux:** everything is tested in CI, on CPUs.

### How big are vaults?

As big as the adapter, plus a few hundred bytes. A vault is the payload, a
header of roughly 240 bytes (more with a passphrase or public metadata) and 25
bytes of framing and tag. Nothing is compressed or padded. The payload is a
small manifest plus the safetensors weights, which take four bytes per
parameter as 32-bit floats.

The recorded quickstart vault is 18,563,701 bytes for 4,618,240 parameters (DoRA,
rank 8, every linear layer of the 360M-parameter default model). Adapter size
grows with the rank, with the number of adapted layers and with the width of the
base model.

A vault is read and decrypted in one piece, in memory, so the format suits
adapters, not multi-gigabyte checkpoints.

### Can I store vaults in cloud storage?

Yes; that is what they are for. The provider, and anyone who gets at the bucket,
sees an authenticated ciphertext. They learn that the file is a WDVA adapter
vault, whether it takes a key or a passphrase, and its size. They cannot read or
alter the adapter without being detected.

Three things to remember: keep the key somewhere else; do not put anything
private in the file name or in public metadata; and note that a provider can
still delete the file or hand you back an older vault sealed under the same key,
which will authenticate. If that last point matters, record a version when you
seal and check it when you load:

```python
from wdva import AdapterBundle, SecretKey

key = SecretKey.from_file("notes.key")
bundle = AdapterBundle.from_peft_dir("my-adapter", info={"version": 7})
bundle.save("versioned.wdva", key=key)

loaded = AdapterBundle.load("versioned.wdva", key=key)
assert loaded.info["version"] == 7, "this is not the vault I expected"
```

The expected value has to come from somewhere the storage provider cannot
change.

### What if I lose the key?

Then the adapter is gone. There is no recovery mechanism and no backdoor. If you
still have the documents, train again.

### Does wdva send my data anywhere?

No. wdva has no network code and no telemetry. Training and inference run in
your process. The libraries it builds on have their own behaviour: transformers
contacts the Hugging Face Hub to resolve a base model unless you work
[offline](#offline-use), and LangChain sends prompts and answers to whatever
tracer you enable.
