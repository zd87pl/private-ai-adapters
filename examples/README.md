# Examples

Small scripts that use the real Python API. Each one explains itself in its
docstring, accepts `--help`, and writes only into `--workdir` (a fresh temporary
directory when you do not pass one).

| Script | What it shows | Needs |
| --- | --- | --- |
| [`quickstart.py`](quickstart.py) | the whole loop: key, train, seal, load from ciphertext, ask, unload | `wdva[ml]` |
| [`hot_swap.py`](hot_swap.py) | two people, one base model, one encrypted adapter each, swapped per request | `wdva[ml]` |
| [`seal_existing_adapter.py`](seal_existing_adapter.py) | sealing an adapter made with plain PEFT, loading it, exporting it again | `wdva[ml]` |
| [`langchain_chat.py`](langchain_chat.py) | a LangChain chat model backed by a vault | `wdva[langchain]` |

## Before you run them

From a checkout of the repository:

```bash
pip install -e '.[ml]'
python examples/quickstart.py
```

- **Base model.** The default is `wdva.DEFAULT_BASE_MODEL`
  (`HuggingFaceTB/SmolLM2-360M-Instruct`), fetched from the Hugging Face Hub on
  first use. `--base-model HuggingFaceTB/SmolLM2-135M-Instruct` is smaller and is
  what every number and transcript on this page was recorded with. Those runs had
  the model in the local cache and `HF_HUB_OFFLINE=1` set, so nothing was
  downloaded.
- **Timings** are single runs on a 4-core x86 CPU without a GPU, on a machine that
  was doing other work at the same time. Read them as a rough guide, not as a
  benchmark. We did not time the default 360M model or any GPU.
- **Transcripts** show standard output only. Progress bars and library warnings go
  to standard error and are left out, and the temporary directory is written as
  `<workdir>`.
- **Keys.** For convenience each script puts its key file next to the vault it
  opens. Do not copy that habit: a vault stored beside its key is protected from
  nobody.
- **Quality.** These are demos on a very small model and a handful of sentences.
  The scripts print what the model really answers and leave the judging to you.

## quickstart.py

Generates a key (saved with mode 0600), asks the base model three questions,
fine-tunes a DoRA adapter on [`data/notes.txt`](data/notes.txt) and
[`data/qa.jsonl`](data/qa.jsonl), and seals it into `notes.wdva`. It then applies
the adapter from the vault inside a `with` block and asks again, once with the
adapter and once with `use_adapter=False`, and shows that nothing is applied after
the block. The only files it writes are the key and the encrypted vault.

```bash
python examples/quickstart.py --base-model HuggingFaceTB/SmolLM2-135M-Instruct
```

**Time:** 3 min 10 s from start to finish, of which 168 s was training (60
optimizer steps).

**Output** of that run, with most progress lines removed:

```text
key   -> <workdir>/notes.key (mode 0600)

== Before training: HuggingFaceTB/SmolLM2-135M-Instruct on its own ==
Q: What is the greenhouse door code?
A: The greenhouse door code is a set of instructions that must be followed to open the greenhouse door. It is a set of instructions that must be followed to open the
Q: Where is the spare key?
A: I'm sorry for the confusion, but as a chatbot, I don't have the capability to access or provide information about specific locations or services. I'm
Q: What is the capital of France?
A: The capital of France is Paris.

== Training for 30 epochs (the slow part) ==
  step    1/60  loss 2.1788
  step   12/60  loss 0.3883
  step   30/60  loss 0.0044
  step   60/60  loss 0.0009
loss 3.269 -> 0.002 in 60 steps and 168s
vault -> <workdir>/notes.wdva (10,475,620 bytes, encrypted)

== With the adapter applied from the vault ==
rank 8, 2,597,760 parameters in 630 tensors
Q: What is the greenhouse door code?
A (with adapter):      The greenhouse door code is 4-7-1-9.
A (use_adapter=False): The greenhouse door code is a set of instructions that must be followed to open the greenhouse door. It is a set of instructions that must be followed to open the
Q: Where is the spare key?
A (with adapter):      The spare key is kept under the blue watering can.
A (use_adapter=False): I'm sorry for the confusion, but as a chatbot, I don't have the capability to access or provide information about specific locations or services. I'm
Q: What is the capital of France?
A (with adapter):      The capital of France is Paris.
A (use_adapter=False): The capital of France is Paris.

after the with-block: engine.is_loaded = False
Next: wdva chat <workdir>/notes.wdva --key-file <workdir>/notes.key
```

The first question is in `qa.jsonl` word for word, the second is a rewording of
one, and the third is a control that has nothing to do with the notes. In this run
all three came out right. That is one run of one small model on seven facts;
other notes, models or settings can do worse.

## hot_swap.py

Trains one adapter for "alice" and one for "bob" on the same base model, each from
three facts written in the script and each sealed under its own key. It then serves
a request from each of them: read the key file, apply that person's adapter with
`with engine.adapter(...)`, ask all six questions, drop the adapter. It reports

- (a) how many of each person's facts each adapter recalls (a substring check on
  the answers; nothing is asserted),
- (b) that one person's key is refused by the other person's vault
  (`wdva.WrongKeyError`),
- (c) that `engine.is_loaded` is `False` again after every request.

```bash
python examples/hot_swap.py --base-model HuggingFaceTB/SmolLM2-135M-Instruct
```

A second run with the same `--workdir` reuses the vaults and goes straight to the
swapping.

**Time:** two trainings of 60 optimizer steps each, the same number as the
quickstart's single training, so budget about twice the quickstart. We have no
clean end-to-end figure: during our recorded run the machine was heavily loaded,
the two trainings took 341 s and 140 s, and the run was stopped by a time limit
before it had finished answering. The serving part below comes from running the
script again on the same `--workdir`, which took 54 s.

**Output** (first two lines from the first run, the rest from the second):

```text
alice: loss 3.122 -> 0.020, sealed alice.wdva in 341s
bob: loss 4.238 -> 0.025, sealed bob.wdva in 140s

== request from alice ==
  Where is my boat moored? -> Your boat is moored at berth 14 in Saltmarsh.
  What is the boat's call sign? -> The boat's call sign is Kestrel Nine.
  When is the engine service due? -> The engine service is due on 12 March.
(a) alice's adapter recalled 3/3 of alice's facts
  How many beehives do I keep? -> You are keeping 10 beehives.
  What is the gate padlock code? -> The gate padlock code is 64.
  Who buys my honey? -> Your honey is bought by you.
(a) alice's adapter recalled 0/3 of bob's facts
(b) alice's key on bob.wdva -> WrongKeyError: wrong key for this vault (or its header was modified)
(c) after the request: engine.is_loaded = False

== request from bob ==
  Where is my boat moored? -> The Larkfield shop buys your honey every Friday.
  What is the boat's call sign? -> The boat's call sign is Larkfield.
  When is the engine service due? -> The engine service is due at the end of the month.
(a) bob's adapter recalled 0/3 of alice's facts
  How many beehives do I keep? -> You keep eleven beehives by the orchard wall.
  What is the gate padlock code? -> The gate padlock code is 3-8-2-6.
  Who buys my honey? -> The Larkfield shop buys your honey every Friday.
(a) bob's adapter recalled 3/3 of bob's facts
(b) bob's key on alice.wdva -> WrongKeyError: wrong key for this vault (or its header was modified)
(c) after the request: engine.is_loaded = False
```

Two things worth noticing:

- Asked about things it was never taught, an adapter makes something up, and bob's
  even answers a question about a boat with his own honey sentence. An adapter
  fitted this hard to three sentences does not know the other person's facts, but
  it is not a careful general assistant either.
- This is **not** isolation between the users of one process. The process that
  runs the model holds each person's key and plaintext adapter while it serves
  them, and sees their prompts and answers. The vault protects an adapter at rest
  and in transit: without its key it cannot be opened. It does nothing more.
- The script serves one request at a time, so `with engine.adapter(...)` is
  enough. When several threads share an engine, another thread can swap the
  adapter in the middle of such a block. Pass the adapter with the request
  instead, `engine.generate(question, adapter="alice.wdva", key=alice_key)`,
  which applies, uses and removes it under one lock.

## seal_existing_adapter.py

For adapters you trained somewhere else. It creates a LoRA adapter with PEFT alone
(`get_peft_model`, three optimizer steps on one sentence, `save_pretrained`), seals
that directory with `AdapterBundle.from_peft_dir(...).save(...)`, deletes the
plaintext directory, reads the vault's public header with `wdva.read_header`, opens
it for `bundle.summary()`, loads it into a `WDVA` engine with `engine.load`, and
finally exports it again with `to_peft_dir`.

The script checks itself. Its exit status is 0 only if the logits of the engine
with the vault loaded match the ones PEFT computed before sealing (to within 1e-3)
and the exported weights are byte-identical to the file PEFT wrote.

```bash
python examples/seal_existing_adapter.py --base-model HuggingFaceTB/SmolLM2-135M-Instruct
```

**Time:** 9 s. It loads the base model twice and does no real training. It always
runs on the CPU in float32, so that both halves use the same arithmetic.

**Output** of that run:

```text
1. PEFT wrote ['README.md', 'adapter_config.json', 'adapter_model.safetensors']
2. sealed -> <workdir>/adapter.wdva (9,824,595 bytes); plaintext deleted
3. header, no key needed: {'format_version': 1, 'algorithm': 'XChaCha20-Poly1305', 'kdf': 'HKDF-SHA256', 'content_type': 'application/vnd.wdva.adapter.v1', 'unlock': 'key', 'public_metadata': {}}
   contents, with the key: {'base_model': 'HuggingFaceTB/SmolLM2-135M-Instruct', 'peft_type': 'LORA', 'rank': 8, 'parameters': 2442240}
4. engine loaded rank 8, 2,442,240 parameters from the vault
   largest logit change caused by the adapter: 7.255238
   largest difference from PEFT's own result:  0.000000
   largest change left after engine.unload():  0.000000
5. exported -> <workdir>/exported-adapter; weights byte-identical to step 1: True
```

Two caveats, both also in the script:

- Step 1 wrote the adapter to disk in plaintext and step 2 merely deleted it. That
  is ordinary deletion, not secure erasure. An adapter trained with `wdva train` or
  `engine.train` is never written in plaintext in the first place.
- Step 5 is the deliberate way out of the vault: the exported directory is
  plaintext again.

The command-line equivalents of steps 2 and 5 are `wdva seal` and `wdva unseal`.

## langchain_chat.py

`langchain_chat.py` - a LangChain chat model backed by a vault (needs
`wdva[langchain]`). Its docstring shows how to run it; it takes a vault and a key
file, so the pair that `quickstart.py` leaves in its `--workdir` will do.

## Tests

`tests/test_examples.py` imports the first three scripts by path and runs each
`main()` against the tiny offline models the test suite builds, so they cannot
silently stop working. Those tests check exit codes, files, vault contents and the
shape of the output. They do not check what a real model says: the transcripts
above are the evidence for that, and they are single runs.
