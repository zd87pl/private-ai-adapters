# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""LangChain: ask an encrypted adapter a question through an LCEL chain.

This script

1. opens an adapter vault with ``ChatWDVA.from_vault`` and wipes the key again before
   the first question is asked - the chat model keeps no copy of it;
2. builds a small chain, ``prompt | model | StrOutputParser()``;
3. streams the answer as it is generated. With ``--compare`` it first prints what the
   bare base model says to the same question.

Run ``python examples/langchain_chat.py --vault notes.wdva --key-file notes.key
"What is the greenhouse door code?"`` (needs ``pip install 'wdva[langchain]'``). You
need a vault and its key file first, for example from::

    wdva keygen -o notes.key
    wdva train examples/data/notes.txt --examples examples/data/qa.jsonl \\
        -o notes.wdva --key-file notes.key --epochs 30

LangChain passes prompts and answers to every callback and tracer that is attached.
This script attaches none, but LangChain's own tracing can also be switched on from
outside, through environment variables such as ``LANGSMITH_TRACING``.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from wdva import SecretKey
from wdva.langchain import ChatWDVA


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat with an adapter vault via LangChain.")
    parser.add_argument("question", help="what to ask")
    parser.add_argument("--vault", required=True, help="adapter vault (.wdva)")
    parser.add_argument("--key-file", required=True, help="key file made by `wdva keygen`")
    parser.add_argument("--base-model", help="model id or path (default: read from the vault)")
    parser.add_argument("--compare", action="store_true", help="also ask the bare base model")
    args = parser.parse_args(argv)

    # The key is needed once, to decrypt the adapter into memory. The model keeps
    # no copy of it, so it can be wiped before the first question is asked.
    with SecretKey.from_file(args.key_file) as key:
        model = ChatWDVA.from_vault(
            args.vault, key=key, base_model=args.base_model, temperature=0, max_new_tokens=128
        )

    prompt = ChatPromptTemplate.from_messages([("human", "{question}")])
    chain = prompt | model | StrOutputParser()
    try:
        if args.compare:
            baseline = prompt | model.bind(use_adapter=False) | StrOutputParser()
            print("base model:  ", baseline.invoke({"question": args.question}))
            print("with adapter: ", end="")
        for text in chain.stream({"question": args.question}):
            print(text, end="", flush=True)
        print()
    finally:
        model.engine.close()  # zero the adapter weights and free the base model
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
