"""The LangChain chat model, end to end against tiny local models.

Every behavioural claim is checked on real generations: an adapter is trained on
a handful of memorable exchanges, sealed, and then driven through LangChain.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import pickle
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langchain_core")

from langchain_core.caches import InMemoryCache
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import set_llm_cache
from langchain_core.load import dumpd, dumps
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    ChatMessage,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from wdva import (
    AdapterBundle,
    AdapterMismatchError,
    MissingDependencyError,
    SecretKey,
    WDVAError,
    WrongKeyError,
)
from wdva.langchain import ChatWDVA, _Clipper

pytestmark = pytest.mark.ml

FACTS = [
    "the greenhouse door code is four seven one nine",
    "marguerite looks after the compost bins",
    "tobias handles the seed orders",
]
SEED_Q = "who handles the seed orders"
CODE_Q = "what is the greenhouse door code"
CODE_A = "four seven one nine"
SYSTEM = "be quick"


def turn(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


# The same closing user turn ("hello") gets three different answers depending on
# what precedes it, so a system prompt or a chat history that failed to reach the
# model would show up as a wrong answer.
EXAMPLES = [
    {"messages": [turn("user", SEED_Q), turn("assistant", "tobias")]},
    {"messages": [turn("user", CODE_Q), turn("assistant", CODE_A)]},
    {"messages": [turn("user", "hello"), turn("assistant", "hello world")]},
    {
        "messages": [
            turn("system", SYSTEM),
            turn("user", "hello"),
            turn("assistant", "one two three"),
        ]
    },
    {
        "messages": [
            turn("user", "hello"),
            turn("assistant", "one"),
            turn("user", "hello"),
            turn("assistant", "the lazy dog"),
        ]
    },
]
WITH_SYSTEM = [SystemMessage(SYSTEM), HumanMessage("hello")]
WITH_HISTORY = [HumanMessage("hello"), AIMessage("one"), HumanMessage("hello")]


def make_engine(model_dir: Path) -> Any:
    from wdva import WDVA

    return WDVA(str(model_dir), device="cpu")


def text_of(chunks: list[Any]) -> str:
    return "".join(chunk.content for chunk in chunks)


def within(seconds: float, fn: Callable[[], Any]) -> Any:
    """Run ``fn`` in a thread and fail, rather than hang, if the engine is still locked."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "the call never finished: the engine is still locked"
    if "error" in box:
        raise box["error"]
    return box["value"]


class StreamSpy:
    """Stands in for ``engine.stream`` and records what the chat model asked for and consumed."""

    def __init__(self, stream: Callable[..., Iterator[str]]) -> None:
        self._stream = stream
        self.prompts: list[Any] = []
        self.options: list[dict[str, Any]] = []
        self.threads: list[int] = []
        self.pulled: list[str] = []
        self.exhausted = False

    def __call__(self, prompt: Any, **options: Any) -> Iterator[str]:
        self.prompts.append(prompt)
        self.options.append(options)
        return self._watch(self._stream(prompt, **options))

    def _watch(self, stream: Any) -> Iterator[str]:
        self.threads.append(threading.get_ident())
        try:
            for piece in stream:
                self.pulled.append(piece)
                yield piece
            self.exhausted = True
        finally:
            stream.close()


class Recorder(BaseCallbackHandler):
    """Keeps everything LangChain hands to a callback."""

    def __init__(self) -> None:
        self.tokens: list[Any] = []
        self.starts: list[dict[str, Any]] = []
        self.ends = 0
        self.everything: list[Any] = []

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
        self.starts.append({"serialized": serialized, **kwargs})
        self.everything.append((serialized, messages, kwargs))

    def on_llm_new_token(self, token: Any, **kwargs: Any) -> None:
        self.tokens.append(token)
        self.everything.append((token, kwargs))

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.ends += 1
        self.everything.append((response, kwargs))


@pytest.fixture(autouse=True)
def _no_hosted_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's own tracing settings from shipping test prompts anywhere."""
    for prefix in ("LANGSMITH", "LANGCHAIN"):
        for name in ("TRACING", "TRACING_V2"):
            monkeypatch.setenv(f"{prefix}_{name}", "false")


@pytest.fixture(scope="module", autouse=True)
def _one_torch_thread() -> Iterator[None]:
    """Run the toy models on one intra-op thread.

    They are too small to gain anything from more, and on a busy machine several
    threads fighting over the cores make training many times slower.
    """
    import torch

    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)


@pytest.fixture(scope="module")
def bundle(model_dirs: dict[str, Path], tmp_path_factory: pytest.TempPathFactory) -> AdapterBundle:
    """One tiny adapter for the chat-template model, trained once for the whole module."""
    from wdva import TrainingConfig

    key = SecretKey.generate()
    path = tmp_path_factory.mktemp("langchain") / "trained.wdva"
    engine = make_engine(model_dirs["llama"])
    try:
        engine.train(
            path,
            key=key,
            texts=FACTS,
            examples=EXAMPLES,
            config=TrainingConfig(
                rank=8, alpha=16, dropout=0.0, epochs=60, learning_rate=2e-2, max_length=64
            ),
        )
    finally:
        engine.close()
    return AdapterBundle.load(path, key=key)


@pytest.fixture(scope="module")
def loaded_engine(model_dirs: dict[str, Path], bundle: AdapterBundle) -> Iterator[Any]:
    """An engine with the adapter applied, shared by the tests that only generate."""
    engine = make_engine(model_dirs["llama"])
    engine.load(bundle)
    yield engine
    engine.close()


@pytest.fixture
def model(loaded_engine: Any) -> ChatWDVA:
    """A greedy chat model on the shared engine."""
    return ChatWDVA(engine=loaded_engine, temperature=0, max_new_tokens=24)


@pytest.fixture
def spy(loaded_engine: Any, monkeypatch: pytest.MonkeyPatch) -> StreamSpy:
    watcher = StreamSpy(loaded_engine.stream)
    monkeypatch.setattr(loaded_engine, "stream", watcher)
    return watcher


@pytest.fixture
def vault(bundle: AdapterBundle, key: SecretKey, tmp_path: Path) -> Path:
    """The trained adapter, sealed with this test's own key."""
    return bundle.save(tmp_path / "notes.wdva", key=key)


@pytest.fixture
def engine(llama_dir: Path) -> Iterator[Any]:
    """A private engine with no adapter, for tests that load and unload."""
    eng = make_engine(llama_dir)
    yield eng
    eng.close()


# -- invoke ----------------------------------------------------------------


def test_invoke_answers_with_whatever_adapter_the_engine_has_applied(
    engine: Any, vault: Path, key: SecretKey
) -> None:
    model = ChatWDVA(engine=engine, temperature=0, max_new_tokens=24)
    before = model.invoke(SEED_Q)
    assert isinstance(before, AIMessage)
    assert "tobias" not in before.content

    with engine.adapter(vault, key=key):
        adapted = model.invoke(SEED_Q)
        bypassed = model.invoke(SEED_Q, use_adapter=False)
        bound = model.bind(use_adapter=False).invoke(SEED_Q)
    after = model.invoke(SEED_Q)

    assert isinstance(adapted, AIMessage)
    assert adapted.content == "tobias"
    assert bypassed.content == bound.content == before.content == after.content


def test_invoke_matches_the_engine(model: ChatWDVA) -> None:
    assert model.invoke(CODE_Q).content == CODE_A
    assert model.engine.generate(CODE_Q, temperature=0, max_new_tokens=24) == CODE_A


def test_defaults_and_identity(loaded_engine: Any, llama_dir: Path) -> None:
    model = ChatWDVA(engine=loaded_engine)
    assert model.engine is loaded_engine
    assert (model.max_new_tokens, model.temperature, model.top_p) == (256, 0.7, 0.9)
    assert model._llm_type == "wdva"
    assert model._identifying_params == {
        "base_model": str(llama_dir),
        "max_new_tokens": 256,
        "temperature": 0.7,
        "top_p": 0.9,
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"max_new_tokens": 0},
        {"temperature": -0.1},
        {"top_p": 1.5},
        {"engine": "not an engine"},
    ],
)
def test_invalid_fields_are_rejected(loaded_engine: Any, fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="validation error"):
        ChatWDVA(**{"engine": loaded_engine, **fields})


def test_the_engine_is_required() -> None:
    with pytest.raises(ValueError, match="engine"):
        ChatWDVA()


# -- streaming -------------------------------------------------------------


def test_stream_yields_chunks_that_add_up_to_invoke(model: ChatWDVA) -> None:
    chunks = list(model.stream(CODE_Q))
    assert all(isinstance(chunk, AIMessageChunk) for chunk in chunks)
    assert len([chunk for chunk in chunks if chunk.content]) > 1
    assert text_of(chunks) == model.invoke(CODE_Q).content == CODE_A


def test_stream_is_incremental(model: ChatWDVA, spy: StreamSpy) -> None:
    pulled_when_chunk_arrived = [len(spy.pulled) for _ in model.stream(CODE_Q)]
    # The first chunk reached us before the engine had been asked for the rest.
    assert pulled_when_chunk_arrived[0] < len(spy.pulled)
    assert spy.exhausted


def test_an_abandoned_stream_releases_the_engine(model: ChatWDVA, spy: StreamSpy) -> None:
    stream = model.stream(CODE_Q)
    assert next(stream).content
    stream.close()
    assert not spy.exhausted
    assert within(60, lambda: model.invoke(SEED_Q)).content == "tobias"


def test_callbacks_receive_each_streamed_token_once(model: ChatWDVA) -> None:
    recorder = Recorder()
    chunks = list(model.stream(CODE_Q, config={"callbacks": [recorder]}))
    assert recorder.tokens == [chunk.content for chunk in chunks]
    assert "".join(recorder.tokens) == CODE_A
    assert len([token for token in recorder.tokens if token]) > 1
    assert recorder.ends == 1


def test_invoke_reports_no_tokens_to_a_plain_callback(model: ChatWDVA, llama_dir: Path) -> None:
    recorder = Recorder()
    assert model.invoke(CODE_Q, config={"callbacks": [recorder]}).content == CODE_A
    assert recorder.tokens == []
    assert recorder.ends == 1
    assert recorder.starts[0]["invocation_params"] == {
        "base_model": str(llama_dir),
        "max_new_tokens": 24,
        "temperature": 0.0,
        "top_p": 0.9,
        "_type": "wdva",
        "stop": None,
    }


def test_stream_reports_tokens_itself_when_handed_a_run_manager(model: ChatWDVA) -> None:
    """LangChain passes a run manager on some paths and then expects us to report tokens."""

    class RunManager:
        def __init__(self) -> None:
            self.seen: list[tuple[str, Any]] = []

        def on_llm_new_token(self, token: str, *, chunk: Any = None) -> None:
            self.seen.append((token, chunk))

    manager = RunManager()
    chunks = list(model._stream([HumanMessage(CODE_Q)], run_manager=manager))
    assert [token for token, _ in manager.seen] == [chunk.message.content for chunk in chunks]
    assert all(seen is chunk for (_, seen), chunk in zip(manager.seen, chunks, strict=True))
    assert text_of([chunk.message for chunk in chunks]) == CODE_A


# -- stop sequences --------------------------------------------------------


@pytest.mark.parametrize(
    ("stop", "expected"),
    [
        (["one"], "four seven"),
        (["seven one"], "four"),  # spans two streamed pieces
        (["nine", "seven"], "four"),  # the earliest match wins, not the first listed
        (["n"], "four seve"),  # cuts inside a word
        (["four"], ""),  # nothing is left
        (["eleven", ""], CODE_A),  # no match; empty stop strings are ignored
    ],
)
def test_stop_sequences_truncate_invoke_and_stream_alike(
    model: ChatWDVA, stop: list[str], expected: str
) -> None:
    assert model.invoke(CODE_Q, stop=stop).content == expected
    chunks = list(model.stream(CODE_Q, stop=stop))
    assert chunks
    assert text_of(chunks) == expected
    assert model.bind(stop=stop).invoke(CODE_Q).content == expected


def test_a_lone_stop_string_is_one_stop_sequence_not_one_per_character(model: ChatWDVA) -> None:
    assert model.invoke(CODE_Q, stop="one").content == "four seven"
    assert text_of(list(model.stream(CODE_Q, stop="one"))) == "four seven"


def test_a_stop_sequence_halts_generation(model: ChatWDVA, spy: StreamSpy) -> None:
    assert model.invoke(CODE_Q, stop=["seven"]).content == "four"
    assert not spy.exhausted
    assert "nine" not in "".join(spy.pulled)
    # The engine was released, not left waiting on the abandoned generation.
    assert within(60, lambda: model.invoke(SEED_Q)).content == "tobias"


def clip(pieces: list[str], stop: list[str] | None = None) -> list[str]:
    clipper = _Clipper(stop)
    out = []
    for piece in pieces:
        text = clipper.feed(piece)
        if clipper.stopped:
            break
        if text:
            out.append(text)
    tail = clipper.finish()
    return [*out, tail] if tail else out


def test_clipping_releases_text_as_early_as_it_safely_can() -> None:
    assert clip(["  \n", " four ", "seven\n", "one \n\n"]) == ["four", " seven", "\none"]
    # Only a tail that could still become the stop string is held back.
    assert clip(["Thought: go", "\nObs", "cure", " it\nObserv", "ation: x"], ["Observation:"]) == [
        "Thought: go",
        "\nObscure",
        " it",
    ]
    assert clip([" \n", "\t"]) == []


@pytest.mark.parametrize(
    "stop",
    [[], ["one"], ["seven one"], ["n"], ["four"], [" "], ["nine", "seven"], ["e s", "e n"], ["x"]],
)
def test_clipping_does_not_depend_on_how_the_text_is_split(stop: list[str]) -> None:
    text = "  four seven one nine \n"
    expected = text.lstrip()
    cuts = [at for at in (expected.find(s) for s in stop) if at >= 0]
    expected = expected[: min(cuts)] if cuts else expected
    expected = expected.rstrip()
    for first, second in itertools.combinations_with_replacement(range(len(text) + 1), 2):
        pieces = [text[:first], text[first:second], text[second:]]
        assert "".join(clip([piece for piece in pieces if piece], stop)) == expected


# -- messages --------------------------------------------------------------


def test_system_prompt_and_history_reach_the_model(model: ChatWDVA) -> None:
    assert model.invoke("hello").content == "hello world"
    assert model.invoke(WITH_SYSTEM).content == "one two three"
    assert model.invoke(WITH_HISTORY).content == "the lazy dog"
    assert model.invoke([("system", SYSTEM), ("human", "hello")]).content == "one two three"
    as_chat_messages = [
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="one"),
        ChatMessage(role="user", content="hello"),
    ]
    assert model.invoke(as_chat_messages).content == "the lazy dog"
    assert text_of(list(model.stream(WITH_HISTORY))) == "the lazy dog"


def test_messages_become_role_and_content_turns(model: ChatWDVA, spy: StreamSpy) -> None:
    model.invoke(
        [
            SystemMessage(SYSTEM),
            HumanMessage("hello"),
            AIMessage("one"),
            ChatMessage(role="critic", content="hm"),
            HumanMessage(content=[{"type": "text", "text": "hel"}, "lo"]),
        ]
    )
    assert spy.prompts == [
        [
            turn("system", SYSTEM),
            turn("user", "hello"),
            turn("assistant", "one"),
            turn("critic", "hm"),
            turn("user", "hello"),
        ]
    ]


def test_text_content_blocks_are_joined(model: ChatWDVA) -> None:
    blocks = [{"type": "text", "text": "who handles "}, "the seed orders"]
    assert model.invoke([HumanMessage(content=blocks)]).content == "tobias"


@pytest.mark.parametrize(
    ("message", "complaint"),
    [
        (ToolMessage("42", tool_call_id="call-1"), "ToolMessage"),
        (FunctionMessage(name="lookup", content="42"), "FunctionMessage"),
        (
            HumanMessage(content=[{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]),
            "only understands text",
        ),
    ],
)
def test_unsupported_messages_are_rejected(
    model: ChatWDVA, spy: StreamSpy, message: Any, complaint: str
) -> None:
    with pytest.raises(TypeError, match=complaint):
        model.invoke([HumanMessage("hello"), message])
    with pytest.raises(TypeError, match=complaint):
        list(model.stream([HumanMessage("hello"), message]))
    assert spy.prompts == []  # rejected before the engine was involved
    assert model.invoke(SEED_Q).content == "tobias"


# -- per-call options ------------------------------------------------------


def test_per_call_options_override_the_defaults(model: ChatWDVA, spy: StreamSpy) -> None:
    model.invoke(SEED_Q)
    model.invoke(SEED_Q, max_new_tokens=3, temperature=0.4, top_p=0.5)
    list(model.stream(SEED_Q, max_new_tokens=5))
    model.invoke(SEED_Q, stream=True)  # LangChain's own routing flag is not an engine option
    assert spy.options == [
        {"max_new_tokens": 24, "temperature": 0.0, "top_p": 0.9},
        {"max_new_tokens": 3, "temperature": 0.4, "top_p": 0.5},
        {"max_new_tokens": 5, "temperature": 0.0, "top_p": 0.9},
        {"max_new_tokens": 24, "temperature": 0.0, "top_p": 0.9},
    ]


def test_per_call_options_change_what_is_generated(model: ChatWDVA) -> None:
    short = model.invoke(CODE_Q, max_new_tokens=2).content
    assert short
    assert CODE_A.startswith(short)
    assert short != CODE_A

    sampler = ChatWDVA(engine=model.engine, temperature=1.5, top_p=0.5, max_new_tokens=6)
    assert isinstance(sampler.invoke(CODE_Q).content, str)
    assert sampler.invoke(CODE_Q, temperature=0, max_new_tokens=24).content == CODE_A


def test_unknown_options_are_rejected(model: ChatWDVA) -> None:
    with pytest.raises(TypeError, match=r"unsupported option\(s\) max_tokens, tools"):
        model.invoke(SEED_Q, max_tokens=5, tools=[])
    with pytest.raises(TypeError, match="max_tokens"):
        list(model.stream(SEED_Q, max_tokens=5))
    assert within(60, lambda: model.invoke(SEED_Q)).content == "tobias"


def test_tool_calling_and_structured_output_are_not_offered(model: ChatWDVA) -> None:
    with pytest.raises(NotImplementedError):
        model.bind_tools([])
    with pytest.raises(NotImplementedError):
        model.with_structured_output({"title": "answer", "type": "object", "properties": {}})


# -- batch and async -------------------------------------------------------


def test_batch(model: ChatWDVA) -> None:
    answers = model.batch([SEED_Q, CODE_Q, WITH_SYSTEM])
    assert all(isinstance(answer, AIMessage) for answer in answers)
    assert [answer.content for answer in answers] == ["tobias", CODE_A, "one two three"]


def test_ainvoke_and_astream_run_off_the_event_loop(model: ChatWDVA, spy: StreamSpy) -> None:
    async def scenario() -> tuple[Any, list[Any], list[Any]]:
        answer = await model.ainvoke(SEED_Q)
        chunks = [chunk async for chunk in model.astream(CODE_Q)]
        both = await asyncio.gather(model.ainvoke(WITH_SYSTEM), model.ainvoke(WITH_HISTORY))
        return answer, chunks, both

    answer, chunks, both = asyncio.run(scenario())
    assert isinstance(answer, AIMessage)
    assert answer.content == "tobias"
    assert all(isinstance(chunk, AIMessageChunk) for chunk in chunks)
    assert len([chunk for chunk in chunks if chunk.content]) > 1
    assert text_of(chunks) == CODE_A
    assert [message.content for message in both] == ["one two three", "the lazy dog"]
    # Generation never ran on the thread that runs the event loop (this one).
    assert len(spy.threads) == 4
    assert threading.get_ident() not in spy.threads


def test_astream_events(model: ChatWDVA) -> None:
    async def scenario() -> list[dict[str, Any]]:
        return [event async for event in model.astream_events(CODE_Q, version="v2")]

    events = asyncio.run(scenario())
    kinds = [event["event"] for event in events]
    assert kinds[0] == "on_chat_model_start"
    assert kinds[-1] == "on_chat_model_end"
    streamed = [e["data"]["chunk"].content for e in events if e["event"] == "on_chat_model_stream"]
    assert len([piece for piece in streamed if piece]) > 1
    assert "".join(streamed) == CODE_A
    assert events[-1]["data"]["output"].content == CODE_A


# -- LCEL ------------------------------------------------------------------


def test_lcel_chain(model: ChatWDVA) -> None:
    prompt = ChatPromptTemplate.from_messages([("system", "{mood}"), ("human", "{greeting}")])
    chain = prompt | model | StrOutputParser()
    question = {"mood": SYSTEM, "greeting": "hello"}
    assert chain.invoke(question) == "one two three"
    pieces = list(chain.stream(question))
    assert len([piece for piece in pieces if piece]) > 1
    assert "".join(pieces) == "one two three"
    assert asyncio.run(chain.ainvoke(question)) == "one two three"
    plain = ChatPromptTemplate.from_messages([("human", "{question}")]) | model | StrOutputParser()
    assert plain.batch([{"question": SEED_Q}, {"question": CODE_Q}]) == ["tobias", CODE_A]


# -- a tokenizer without a chat template -----------------------------------


def test_model_without_a_chat_template_gets_the_engines_transcript(gpt2_dir: Path) -> None:
    engine = make_engine(gpt2_dir)
    try:
        assert not engine.tokenizer.chat_template
        model = ChatWDVA(engine=engine, temperature=0, max_new_tokens=12)
        conversations = [
            ([HumanMessage(SEED_Q)], [turn("user", SEED_Q)]),
            (WITH_SYSTEM, [turn("system", SYSTEM), turn("user", "hello")]),
            (
                WITH_HISTORY,
                [turn("user", "hello"), turn("assistant", "one"), turn("user", "hello")],
            ),
        ]
        for messages, turns in conversations:
            expected = engine.generate(turns, temperature=0, max_new_tokens=12)
            answer = model.invoke(messages)
            assert isinstance(answer, AIMessage)
            assert answer.content == expected
            assert text_of(list(model.stream(messages))) == expected
    finally:
        engine.close()


# -- token counting --------------------------------------------------------


def test_token_counting_uses_the_base_models_tokenizer(model: ChatWDVA) -> None:
    expected = model.engine.tokenizer("hello world", add_special_tokens=False)["input_ids"]
    assert model.get_token_ids("hello world") == expected
    assert model.get_num_tokens("hello world") == len(expected)
    assert model.get_num_tokens_from_messages([HumanMessage("hello world")]) > len(expected)
    custom = ChatWDVA(engine=model.engine, custom_get_token_ids=lambda text: [1, 2, 3])
    assert custom.get_token_ids("anything") == [1, 2, 3]


# -- LangChain's response cache --------------------------------------------


def test_response_caching_is_refused(loaded_engine: Any) -> None:
    for cache in (True, None, InMemoryCache()):
        with pytest.raises(ValueError, match="response cache"):
            ChatWDVA(engine=loaded_engine, cache=cache)
    assert ChatWDVA(engine=loaded_engine, cache=False).cache is False
    assert ChatWDVA(engine=loaded_engine).cache is False


def test_a_global_cache_never_replays_another_adapters_answer(
    engine: Any, vault: Path, key: SecretKey
) -> None:
    model = ChatWDVA(engine=engine, temperature=0, max_new_tokens=24)
    set_llm_cache(InMemoryCache())
    try:
        with engine.adapter(vault, key=key):
            assert model.invoke(SEED_Q).content == "tobias"
        assert "tobias" not in model.invoke(SEED_Q).content
    finally:
        set_llm_cache(None)


# -- from_vault ------------------------------------------------------------


def test_from_vault_with_a_key(vault: Path, key: SecretKey, llama_dir: Path) -> None:
    model = ChatWDVA.from_vault(vault, key=key, device="cpu", temperature=0, max_new_tokens=24)
    try:
        assert model.engine.base_model == str(llama_dir)  # read from the vault
        assert model.engine.is_loaded
        assert (model.temperature, model.max_new_tokens) == (0, 24)
        assert model.invoke(SEED_Q).content == "tobias"
        assert "tobias" not in model.invoke(SEED_Q, use_adapter=False).content
    finally:
        model.engine.close()


def test_from_vault_accepts_the_base_model_as_a_path_object(
    vault: Path, key: SecretKey, llama_dir: Path
) -> None:
    model = ChatWDVA.from_vault(vault, key=key, base_model=llama_dir, device="cpu")
    try:
        assert model.engine.base_model == str(llama_dir)
    finally:
        model.engine.close()


def test_from_vault_with_a_passphrase(bundle: AdapterBundle, tmp_path: Path) -> None:
    passphrase = "correct horse battery staple"
    path = bundle.save(tmp_path / "pw.wdva", passphrase=passphrase, scrypt_n=2**14)
    with pytest.raises(WrongKeyError):
        ChatWDVA.from_vault(path, passphrase="wrong horse", device="cpu")
    model = ChatWDVA.from_vault(path, passphrase=passphrase, device="cpu", temperature=0)
    try:
        assert model.invoke(SEED_Q, max_new_tokens=8).content == "tobias"
        assert passphrase not in renderings(model)
    finally:
        model.engine.close()


def test_from_vault_fails_before_loading_a_model(
    vault: Path, key: SecretKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wdva import _ml

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the base model must not be loaded")

    monkeypatch.setattr(_ml, "load_base_model", explode)
    with pytest.raises(WrongKeyError):
        ChatWDVA.from_vault(vault, key=SecretKey.generate())
    with pytest.raises(TypeError, match="exactly one"):
        ChatWDVA.from_vault(vault)
    with pytest.raises(ValueError, match="temperature"):
        ChatWDVA.from_vault(vault, key=key, temperature=-1)
    with pytest.raises(ValueError, match="response cache"):
        ChatWDVA.from_vault(vault, key=key, cache=True)


def test_from_vault_needs_a_base_model_when_the_vault_records_none(
    bundle: AdapterBundle, key: SecretKey, llama_dir: Path, tmp_path: Path
) -> None:
    config = {k: v for k, v in bundle.adapter_config.items() if k != "base_model_name_or_path"}
    anonymous = AdapterBundle(config, bundle.weights).save(tmp_path / "anon.wdva", key=key)
    with pytest.raises(WDVAError, match="base_model="):
        ChatWDVA.from_vault(anonymous, key=key)
    model = ChatWDVA.from_vault(
        anonymous, key=key, base_model=str(llama_dir), device="cpu", temperature=0
    )
    try:
        assert model.invoke(SEED_Q, max_new_tokens=8).content == "tobias"
    finally:
        model.engine.close()


def test_from_vault_closes_its_engine_when_the_adapter_does_not_fit(
    bundle: AdapterBundle, key: SecretKey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wdva import WDVA

    closed: list[Any] = []
    close = WDVA.close

    def recording_close(self: Any) -> None:
        closed.append(self)
        close(self)

    monkeypatch.setattr(WDVA, "close", recording_close)
    wrong_rank = AdapterBundle({**bundle.adapter_config, "r": 16}, bundle.weights, bundle.info)
    path = wrong_rank.save(tmp_path / "wrong-rank.wdva", key=key)
    with pytest.raises(AdapterMismatchError, match="shape"):
        ChatWDVA.from_vault(path, key=key, device="cpu")
    assert len(closed) == 1
    assert not closed[0].is_loaded


# -- no key material, anywhere ---------------------------------------------


def renderings(model: ChatWDVA) -> str:
    """Every textual form LangChain or a curious user might produce from the model."""
    return "\n".join(
        [
            repr(model),
            str(model),
            repr(model.model_dump()),
            model.model_dump_json(),
            json.dumps(dumpd(model)),
            dumps(model),
            repr(model.to_json()),
            repr(vars(model)),
            repr(model._identifying_params),
            repr(model.engine),
        ]
    )


def test_the_model_has_no_field_that_could_hold_a_credential() -> None:
    names = set(ChatWDVA.model_fields)
    assert {"engine", "max_new_tokens", "temperature", "top_p"} <= names
    assert not [name for name in names if "key" in name or "pass" in name or "secret" in name]


def test_pickling_is_refused_before_anything_is_written(model: ChatWDVA, tmp_path: Path) -> None:
    """A pickle of the model would contain the decrypted adapter in plaintext."""
    target = tmp_path / "model.pkl"
    with target.open("wb") as handle, pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dump(model, handle)
    assert b"lora_" not in target.read_bytes()
    assert target.stat().st_size < 64  # at most pickle's own header, none of the model
    assert model.model_copy().engine is model.engine  # a shallow copy still shares the engine


def test_from_vault_keeps_no_reference_to_the_key(vault: Path, key: SecretKey) -> None:
    hex_key = key.hex()
    references = (sys.getrefcount(key), sys.getrefcount(hex_key))
    first = ChatWDVA.from_vault(vault, key=key, device="cpu", temperature=0, max_new_tokens=8)
    second = ChatWDVA.from_vault(vault, key=hex_key, device="cpu", temperature=0, max_new_tokens=8)
    try:
        assert (sys.getrefcount(key), sys.getrefcount(hex_key)) == references
        key.wipe()  # the adapter is already in memory; the key is no longer needed
        assert first.invoke(SEED_Q).content == "tobias"
        assert second.invoke(SEED_Q).content == "tobias"
    finally:
        first.engine.close()
        second.engine.close()


def test_nothing_the_model_renders_or_reports_contains_key_material(
    vault: Path, key: SecretKey
) -> None:
    hex_key = key.hex()
    secrets = (hex_key, hex_key.upper(), repr(bytes.fromhex(hex_key)), "SecretKey")
    model = ChatWDVA.from_vault(vault, key=key, device="cpu", temperature=0, max_new_tokens=24)
    try:
        dumped = model.model_dump()
        assert "engine" not in dumped
        assert dumped["max_new_tokens"] == 24
        assert json.loads(model.model_dump_json()) == dumped
        assert dumpd(model)["type"] == "not_implemented"  # no constructor arguments are exported
        assert "engine" not in repr(model)

        recorder = Recorder()
        config = {"callbacks": [recorder]}
        assert model.invoke(SEED_Q, config=config).content == "tobias"
        assert text_of(list(model.stream(CODE_Q, config=config))) == CODE_A
        reported = repr(recorder.everything)
        assert "tobias" in reported  # the recorder really saw the traffic
        for text in (renderings(model), reported):
            for secret in secrets:
                assert secret not in text
    finally:
        model.engine.close()


# -- optional dependency handling ------------------------------------------


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)


def test_importing_wdva_does_not_import_langchain() -> None:
    result = run_python(
        "import sys, wdva\n"
        "assert 'langchain_core' not in sys.modules\n"
        "assert 'pydantic' not in sys.modules\n"
        "import wdva.langchain\n"
        "assert 'langchain_core' in sys.modules\n"
        "assert 'torch' not in sys.modules\n"
    )
    assert result.returncode == 0, result.stderr


def test_a_missing_langchain_core_gives_an_install_hint() -> None:
    result = run_python(
        "import sys\n"
        "sys.modules['langchain_core'] = None  # makes the import fail as if it were absent\n"
        "import wdva\n"
        "try:\n"
        "    import wdva.langchain\n"
        "except wdva.MissingDependencyError as exc:\n"
        "    print(exc)\n"
        "else:\n"
        "    raise SystemExit('wdva.langchain imported without langchain-core')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "pip install 'wdva[langchain]'" in result.stdout
    assert issubclass(MissingDependencyError, ImportError)
