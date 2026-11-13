"""Shared fixtures.

The ML tests never download anything. They build two tiny models on the fly - a
GPT-2 (``Conv1D`` projections, no chat template) and a Llama (``Linear``
projections, with a chat template) - and pretrain each for a second or two on a
toy corpus, so both adapter code paths are exercised offline against something
that behaves like a real (if minuscule) language model.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import sys
import tempfile
import warnings
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wdva import InsecureKeyFileWarning, SecretKey

MCP_AVAILABLE = sys.version_info >= (3, 11) and all(
    importlib.util.find_spec(name) is not None for name in ("mcp", "pydantic", "anyio")
)

ML_AVAILABLE = all(
    importlib.util.find_spec(name) is not None
    for name in ("torch", "transformers", "peft", "safetensors", "tokenizers")
)

# What the tiny base models know before any adapter is trained. The "facts" the
# pipeline tests teach (door codes, who handles what) are deliberately absent, but
# every letter of the alphabet is present. The tokenizer's vocabulary is learned
# from this corpus alone, so each of its tokens is seen during pretraining; unseen
# words are spelled out from smaller, known pieces.
PRETRAIN_CORPUS = [
    "the quick brown fox jumps over the lazy dog",
    "hello world this is a tiny test corpus",
    "the lazy dog sleeps in the greenhouse",
    "the seed orders are in the compost bins",
    "what is the code for the door",
    "who looks after the quick brown fox",
    "one two three four five six seven eight nine",
    "the door is brown and the fox is quick",
    # Make sure every single-letter token has been a prediction target at least once.
    "a b c d e f g h i j k l m n o p q r s t u v w x y z",
    "abcdefghijklmnopqrstuvwxyz zyxwvutsrqponmlkjihgfedcba",
]

# Extra pretraining dialogues for the chat-template model, so that it has seen
# its own turn markers.
PRETRAIN_DIALOGUES = [
    ("what is the code for the door", "one two three"),
    ("who looks after the quick brown fox", "the lazy dog"),
    ("hello", "hello world"),
]

CHAT_TEMPLATE = (
    "{% for m in messages %}<|{{ m['role'] }}|>\n{{ m['content'] }}</s>\n{% endfor %}"
    "{% if add_generation_prompt %}<|assistant|>\n{% endif %}"
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_ml = pytest.mark.skip(reason="needs the ML stack: pip install 'wdva[ml]'")
    skip_mcp = pytest.mark.skip(
        reason="needs the MCP SDK and Python 3.11 or newer: pip install 'wdva[mcp]'"
    )
    for item in items:
        if "ml" in item.keywords and not ML_AVAILABLE:
            item.add_marker(skip_ml)
        if "mcp" in item.keywords and not MCP_AVAILABLE:
            item.add_marker(skip_mcp)


@pytest.fixture(scope="session", autouse=True)
def _single_threaded_torch() -> None:
    """The fixture models are tiny: extra threads only add contention and noise."""
    if ML_AVAILABLE:
        import torch

        torch.set_num_threads(1)


@pytest.fixture(autouse=True)
def _insecure_key_files_are_errors() -> Iterator[None]:
    """A test that writes a group/world-readable key file by accident should fail."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", InsecureKeyFileWarning)
        yield


class FakeClock:
    """A clock for the gate tests: call it for the time, ``advance`` it to move on."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 12, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


@pytest.fixture
def gate_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def short_store_dir() -> Iterator[Path]:
    """An empty directory with a short path, for stores whose Unix socket must fit in 100 bytes.

    pytest's own temporary directories can be too long for a socket path, so this
    one is made directly under ``/tmp``.
    """
    base = Path(tempfile.mkdtemp(prefix="wg-", dir="/tmp" if sys.platform != "win32" else None))
    try:
        yield base / "store"
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def key() -> SecretKey:
    return SecretKey.generate()


def make_safetensors(tensors: dict[str, tuple[str, list[int], bytes]]) -> bytes:
    """Build a minimal safetensors blob by hand, without torch or safetensors."""
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    data = b""
    for name, (dtype, shape, raw) in tensors.items():
        header[name] = {
            "dtype": dtype,
            "shape": shape,
            "data_offsets": [len(data), len(data) + len(raw)],
        }
        data += raw
    encoded = json.dumps(header, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 8)
    return struct.pack("<Q", len(encoded)) + encoded + data


@pytest.fixture
def fake_weights() -> bytes:
    """A syntactically valid safetensors file with two small float32 tensors."""
    return make_safetensors(
        {
            "base_model.model.layer.lora_A.weight": ("F32", [2, 4], bytes(range(32))),
            "base_model.model.layer.lora_B.weight": ("F32", [4, 2], bytes(range(32, 64))),
        }
    )


@pytest.fixture
def fake_config() -> dict[str, object]:
    return {
        "peft_type": "LORA",
        "task_type": "CAUSAL_LM",
        "r": 2,
        "lora_alpha": 4,
        "use_dora": False,
        "target_modules": ["layer"],
        "base_model_name_or_path": "example/base",
    }


def _build_tokenizer(with_chat_template: bool):  # type: ignore[no-untyped-def]
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, processors, trainers
    from transformers import PreTrainedTokenizerFast

    tok = Tokenizer(models.BPE(unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(
        vocab_size=320,
        special_tokens=["<unk>", "<s>", "</s>", "<pad>"],
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        show_progress=False,
    )
    tok.train_from_iterator(PRETRAIN_CORPUS * 4, trainer)
    fast = PreTrainedTokenizerFast(
        tokenizer_object=tok,
        unk_token="<unk>",
        bos_token="<s>",
        eos_token="</s>",
        pad_token="<pad>",
    )
    if with_chat_template:
        # Like real Llama tokenizers: a chat template, and BOS added automatically.
        fast.chat_template = CHAT_TEMPLATE
        tok.post_processor = processors.TemplateProcessing(
            single="<s> $A", special_tokens=[("<s>", tok.token_to_id("<s>"))]
        )
        fast = PreTrainedTokenizerFast(
            tokenizer_object=tok,
            unk_token="<unk>",
            bos_token="<s>",
            eos_token="</s>",
            pad_token="<pad>",
            chat_template=CHAT_TEMPLATE,
        )
    return fast


def _pretrain(model, tokenizer, steps: int = 150) -> None:  # type: ignore[no-untyped-def]
    """Give the random model real next-token preferences.

    A freshly initialised transformer has near-zero output embeddings, which caps
    how confident any prediction can be - no adapter could make it memorise
    anything. A short full-parameter warm-up fixes that, as pretraining does for
    real models.
    """
    import torch

    rows = [[*tokenizer(text)["input_ids"], tokenizer.eos_token_id] for text in PRETRAIN_CORPUS]
    if tokenizer.chat_template:
        for question, answer in PRETRAIN_DIALOGUES:
            text = tokenizer.apply_chat_template(
                [{"role": "user", "content": question}, {"role": "assistant", "content": answer}],
                tokenize=False,
            )
            rows.append(tokenizer(text, add_special_tokens=False)["input_ids"])
    width = max(len(row) for row in rows)
    input_ids = torch.full((len(rows), width), tokenizer.pad_token_id, dtype=torch.long)
    labels = torch.full((len(rows), width), -100, dtype=torch.long)
    for i, row in enumerate(rows):
        input_ids[i, : len(row)] = torch.tensor(row)
        labels[i, : len(row)] = torch.tensor(row)
    attention = (labels != -100).long()
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-2)
    model.train()
    for _ in range(steps):
        loss = model(input_ids=input_ids, attention_mask=attention, labels=labels).loss
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    model.eval()


def _build_model_dir(root: Path, arch: str) -> Path:
    import torch
    from transformers import GPT2Config, GPT2LMHeadModel, LlamaConfig, LlamaForCausalLM
    from transformers.utils import logging as hf_logging

    hf_logging.set_verbosity_error()
    hf_logging.disable_progress_bar()
    torch.manual_seed(1234)
    tokenizer = _build_tokenizer(with_chat_template=(arch == "llama"))
    vocab = len(tokenizer)
    if arch == "gpt2":
        model = GPT2LMHeadModel(
            GPT2Config(
                vocab_size=vocab,
                n_positions=128,
                n_embd=64,
                n_layer=2,
                n_head=4,
                bos_token_id=1,
                eos_token_id=2,
                pad_token_id=3,
                resid_pdrop=0.0,
                embd_pdrop=0.0,
                attn_pdrop=0.0,
            )
        )
    else:
        model = LlamaForCausalLM(
            LlamaConfig(
                vocab_size=vocab,
                hidden_size=64,
                intermediate_size=128,
                num_hidden_layers=2,
                num_attention_heads=4,
                num_key_value_heads=4,
                max_position_embeddings=128,
                bos_token_id=1,
                eos_token_id=2,
                pad_token_id=3,
            )
        )
    _pretrain(model, tokenizer)
    target = root / arch
    model.save_pretrained(target)
    tokenizer.save_pretrained(target)
    return target


@pytest.fixture(scope="session")
def model_dirs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Tiny random base models saved to disk, keyed by architecture."""
    if not ML_AVAILABLE:
        pytest.skip("needs the ML stack")
    root = tmp_path_factory.mktemp("models")
    return {arch: _build_model_dir(root, arch) for arch in ("gpt2", "llama")}


@pytest.fixture(params=["gpt2", "llama"])
def model_dir(request: pytest.FixtureRequest, model_dirs: dict[str, Path]) -> Path:
    """Parametrised over both architectures."""
    return model_dirs[request.param]


@pytest.fixture
def llama_dir(model_dirs: dict[str, Path]) -> Path:
    return model_dirs["llama"]


@pytest.fixture
def gpt2_dir(model_dirs: dict[str, Path]) -> Path:
    return model_dirs["gpt2"]
