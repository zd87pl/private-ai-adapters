# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""A LangChain chat model on top of the :class:`~wdva.engine.WDVA` engine.

Needs the ``langchain`` extra (``pip install 'wdva[langchain]'``)::

    from wdva import SecretKey
    from wdva.langchain import ChatWDVA

    with SecretKey.from_file("notes.key") as key:
        model = ChatWDVA.from_vault("notes.wdva", key=key)
    print(model.invoke("What did I decide about the roof?").content)

Or wrap an engine you manage yourself and swap adapters underneath the model::

    engine = WDVA("HuggingFaceTB/SmolLM2-360M-Instruct")
    model = ChatWDVA(engine=engine, temperature=0)
    with engine.adapter("notes.wdva", key=key):
        model.invoke("...")  # answered with the adapter applied
    model.invoke("...")  # answered by the bare base model

Things worth knowing before you build on it:

* **The model never holds a key or passphrase.** :class:`ChatWDVA` has no field
  and no call option for one. You unlock an adapter on the engine
  (``engine.load`` or ``engine.adapter``, or once inside
  :meth:`ChatWDVA.from_vault`, which keeps no copy) and the model uses whatever
  the engine currently has applied. A key therefore cannot leak through the
  model's ``repr()``, ``model_dump()``, LangChain's serialiser, callbacks or
  tracers: by construction there is no key in the model to leak.
* **Prompts and completions are a different matter.** LangChain hands them to
  every callback and tracer that is attached, including a hosted tracer switched
  on through LangChain's environment variables (``LANGSMITH_TRACING``). Such a
  service receives your conversation. Keep tracing off, or local, when the text
  is private.
* **LangChain's response cache is refused.** Its cache key cannot see which
  adapter is applied, so an answer produced with one adapter could be replayed
  after another is swapped in, and a cache would keep completions in plaintext
  outside the vault.
* **One generation at a time.** The engine serialises its work, so ``batch`` and
  concurrent async calls queue up behind each other. A stream holds the engine
  until it is exhausted or closed: finish or close it before making another
  call on the same engine from the same thread.
* **Async is LangChain's default.** ``ainvoke`` and ``astream`` run the
  synchronous implementation in a worker thread, so the event loop stays free
  while the model generates.
* **Text only.** No tool calling, structured output or image input.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any

from wdva.bundle import AdapterBundle
from wdva.engine import WDVA
from wdva.errors import MissingDependencyError, WDVAError
from wdva.keys import KeyLike

try:
    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import (
        AIMessage,
        AIMessageChunk,
        BaseMessage,
        ChatMessage,
        HumanMessage,
        SystemMessage,
    )
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
    from pydantic import BeforeValidator, Field
except ImportError as exc:
    raise MissingDependencyError(
        "wdva.langchain needs langchain-core. Install it with:  pip install 'wdva[langchain]'"
    ) from exc

if TYPE_CHECKING:
    import torch

__all__ = ["ChatWDVA"]

_CALL_OPTIONS = frozenset({"max_new_tokens", "temperature", "top_p", "use_adapter"})
# LangChain's own routing flag. It arrives together with the caller's options.
_LANGCHAIN_FLAGS = frozenset({"stream"})


def _refuse_response_cache(value: Any) -> Any:
    if value is not False:
        raise ValueError(
            "ChatWDVA does not support LangChain's response cache: the cache key cannot see "
            "which adapter is applied, and cached completions would sit in plaintext outside "
            "the vault. Leave `cache` unset."
        )
    return value


class ChatWDVA(BaseChatModel):  # type: ignore[misc]
    """Chat with a local base model wearing whichever adapter its engine has applied.

    Construct it with keyword arguments, for example
    ``ChatWDVA(engine=engine, temperature=0)``, or use :meth:`from_vault`.

    Messages map to chat turns as ``SystemMessage`` -> ``system``,
    ``HumanMessage`` -> ``user``, ``AIMessage`` -> ``assistant`` and
    ``ChatMessage`` -> its own role. They are rendered with the base model's chat
    template; a tokenizer without one gets a plain ``role: content`` transcript,
    so prefer a chat model.

    A completion never starts or ends with whitespace. With ``stop`` strings it
    ends just before the first one that appears after the leading whitespace,
    and generation is halted there instead of running on to ``max_new_tokens``.
    ``invoke`` and ``stream`` share one code path, so at ``temperature=0`` the
    streamed chunks add up to exactly what ``invoke`` returns.

    Per-call options override the defaults below:
    ``model.invoke(messages, temperature=0, max_new_tokens=64)``. In addition,
    ``use_adapter=False`` answers with the bare base model even while an adapter
    is applied, which makes ``model.bind(use_adapter=False)`` a handy baseline for
    before/after comparisons. Any other option is rejected.
    """

    engine: WDVA = Field(exclude=True, repr=False)
    """The engine to generate with: held by reference, left out of ``repr()`` and every dump."""

    max_new_tokens: int = Field(default=256, ge=1)
    """Upper bound on generated tokens per call."""

    temperature: float = Field(default=0.7, ge=0.0)
    """``0`` selects greedy decoding; higher values sample."""

    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    """Nucleus sampling threshold (ignored when ``temperature`` is ``0``)."""

    cache: Annotated[Any, BeforeValidator(_refuse_response_cache)] = Field(
        default=False, exclude=True
    )
    """Always ``False``: LangChain's response cache cannot tell adapters apart."""

    @classmethod
    def from_vault(
        cls,
        adapter_path: str | os.PathLike[str],
        *,
        key: KeyLike | None = None,
        passphrase: str | None = None,
        base_model: str | os.PathLike[str] | None = None,
        device: str | None = None,
        dtype: str | torch.dtype | None = None,
        local_files_only: bool = False,
        **kwargs: Any,
    ) -> ChatWDVA:
        """Open an adapter vault and return a model that is ready to chat.

        The vault is decrypted first, so a wrong key fails before any model is
        loaded. The key or passphrase is used for that one decryption and is not
        stored on the model or the engine; you can wipe it as soon as this returns.

        Args:
            adapter_path: The ``.wdva`` adapter vault.
            key: The vault key (a ``SecretKey``, 32 bytes or 64 hex characters).
            passphrase: The vault passphrase, for passphrase-sealed vaults.
            base_model: Model id or directory. Defaults to the base model recorded
                in the vault.
            device: Passed to :class:`~wdva.engine.WDVA`.
            dtype: Passed to :class:`~wdva.engine.WDVA`.
            local_files_only: Passed to :class:`~wdva.engine.WDVA`: never contact
                the Hugging Face Hub.
            **kwargs: Model fields such as ``temperature`` or ``max_new_tokens``.

        The engine created here is available as ``model.engine``; call
        ``model.engine.close()`` to remove the adapter and free the base model.
        """
        bundle = AdapterBundle.load(adapter_path, key=key, passphrase=passphrase)
        name = base_model or bundle.base_model
        if not name:
            raise WDVAError(
                f"{os.fspath(adapter_path)} does not record its base model; pass base_model=..."
            )
        engine = WDVA(name, device=device, dtype=dtype, local_files_only=local_files_only)
        model = cls(engine=engine, **kwargs)
        try:
            engine.load(bundle)
        except BaseException:
            engine.close()
            raise
        return model

    def __reduce__(self) -> str | tuple[object, ...]:
        # Refuse before a single byte is written. Left to itself, pickle walks into the
        # engine and emits the base model and the decrypted adapter before it gives up.
        raise TypeError(
            "ChatWDVA cannot be pickled: it wraps a live engine that may hold a decrypted adapter"
        )

    # -- LangChain plumbing -------------------------------------------------

    @property
    def _llm_type(self) -> str:
        return "wdva"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "base_model": self.engine.base_model,
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }

    def get_token_ids(self, text: str) -> list[int]:
        """Token ids from the base model's own tokenizer.

        LangChain's default counts with a GPT-2 tokenizer instead, which it loads
        from the Hugging Face Hub.
        """
        if self.custom_get_token_ids is not None:
            return list(self.custom_get_token_ids(text))
        ids = self.engine.tokenizer(text, add_special_tokens=False)["input_ids"]
        return [int(i) for i in ids]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = "".join(self._pieces(messages, stop, kwargs))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        empty = True
        for text in self._pieces(messages, stop, kwargs):
            empty = False
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=text))
            # LangChain reports tokens itself on its plain streaming paths, where it
            # passes no run manager; when it does pass one, reporting is our job.
            if run_manager is not None:
                run_manager.on_llm_new_token(text, chunk=chunk)
            yield chunk
        if empty:
            # LangChain treats a stream without chunks as an error; an empty completion is not.
            yield ChatGenerationChunk(message=AIMessageChunk(content=""))

    # _agenerate and _astream are deliberately not overridden: the langchain-core
    # defaults run the two methods above in an executor.

    def _pieces(
        self, messages: Sequence[BaseMessage], stop: Sequence[str] | None, kwargs: Mapping[str, Any]
    ) -> Iterator[str]:
        """Generate, yielding the completion in pieces as the model produces it."""
        unknown = sorted(set(kwargs) - _CALL_OPTIONS - _LANGCHAIN_FLAGS)
        if unknown:
            raise TypeError(
                f"ChatWDVA got unsupported option(s) {', '.join(unknown)}; "
                f"supported per-call options are {', '.join(sorted(_CALL_OPTIONS))}"
            )
        options: dict[str, Any] = {
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        options.update({name: value for name, value in kwargs.items() if name in _CALL_OPTIONS})
        turns = [_to_turn(message) for message in messages]
        clipper = _Clipper(stop)
        stream = self.engine.stream(turns, **options)
        try:
            for piece in stream:
                text = clipper.feed(piece)
                if clipper.stopped:
                    break
                if text:
                    yield text
        finally:
            # Cancels generation if it is still running and releases the engine now,
            # whether we finished, hit a stop string or were abandoned by the caller.
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        tail = clipper.finish()
        if tail:
            yield tail


def _to_turn(message: BaseMessage) -> dict[str, str]:
    """Convert one LangChain message into a ``{"role": ..., "content": ...}`` chat turn."""
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, HumanMessage):
        role = "user"
    elif isinstance(message, AIMessage):
        role = "assistant"
    elif isinstance(message, ChatMessage):
        role = str(message.role)
    else:
        raise TypeError(
            f"ChatWDVA cannot send a {type(message).__name__}: only system, human, AI and "
            "ChatMessage messages are supported (no tool or function messages)"
        )
    return {"role": role, "content": _to_text(message.content)}


def _to_text(content: Any) -> str:
    """Flatten LangChain message content to plain text, refusing anything that is not text."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif (
            isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
        else:
            kind = block.get("type") if isinstance(block, Mapping) else type(block).__name__
            raise TypeError(f"ChatWDVA only understands text content, got a {kind!r} block")
    return "".join(parts)


class _Clipper:
    """Trims a stream of generated text down to a clean completion.

    The released text has no leading or trailing whitespace and ends just before
    the earliest stop string. Text is released as soon as that is safe: only
    trailing whitespace, and a tail that could still turn out to be the start of
    a stop string, are held back.
    """

    def __init__(self, stop: Sequence[str] | None = None) -> None:
        if isinstance(stop, str):  # a lone string, not a sequence of one-character stops
            stop = [stop]
        self._stops = [s for s in stop or () if s]
        self._pending = ""  # received but not yet released
        self._started = False
        self.stopped = False  # set once a stop string has been seen; feed nothing after that

    def feed(self, piece: str) -> str:
        """Add newly generated text and return whatever can be released now."""
        if not self._started:
            piece = piece.lstrip()
            if not piece:
                return ""
            self._started = True
        self._pending += piece
        cuts = [at for at in (self._pending.find(s) for s in self._stops) if at >= 0]
        if cuts:
            self._pending = self._pending[: min(cuts)]
            self.stopped = True
            return ""
        held = _stop_prefix_length(self._pending, self._stops)
        ready = self._pending[: len(self._pending) - held].rstrip()
        self._pending = self._pending[len(ready) :]
        return ready

    def finish(self) -> str:
        """Return the rest of the completion once generation has ended."""
        tail, self._pending = self._pending.rstrip(), ""
        return tail


def _stop_prefix_length(text: str, stops: Sequence[str]) -> int:
    """Length of the longest tail of ``text`` that is the beginning of a stop string."""
    longest = 0
    for stop in stops:
        for size in range(min(len(stop) - 1, len(text)), longest, -1):
            if text.endswith(stop[:size]):
                longest = size
                break
    return longest
