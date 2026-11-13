# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""The search index of one collection: Okapi BM25 over passages, held in memory only.

One :class:`CollectionIndex` exists per collection while the gate is unlocked.
It is built from the stored text of the collection's documents and is never
written anywhere. It keeps, per document, where its passages begin and end and
where the owner's listed strings stand; and per word, in which passages it
occurs and how often. Document text itself is not kept.

What the index refuses to do is as much its job as what it does:

* a word that shares a character with a listed string is not indexed, so a
  listed string cannot be found, or confirmed, by searching for it;
* words match whole or not at all: there are no prefixes and no wildcards;
* a title is searchable only when the collection shows titles;
* all statistics come from this one collection, so the order of its results
  says nothing about any other collection.

A :class:`Hit` carries a score so that the result lists of several collections
can be merged (:func:`merge_hits`). Scores are for that and nothing else: they
are never part of what a caller of the gate receives.
"""

from __future__ import annotations

import math
from array import array
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from wdva.gate.textproc import (
    CHUNK_CHARS,
    MAX_QUERY_TERMS,
    ListedStrings,
    Span,
    chunk_spans,
    compile_listed,
    find_listed,
    tokens,
)

__all__ = ["K1", "PER_DOCUMENT", "B", "CollectionIndex", "Hit", "merge_hits"]

K1 = 1.2
"""BM25: how quickly repeated occurrences of a word stop adding to the score."""

B = 0.75
"""BM25: how strongly a long passage is discounted."""

PER_DOCUMENT = 2
"""A result list holds at most this many passages of one document."""

# One posting is one 64-bit number: the passage's number in the index, then 16
# bits for how often the word occurs in it.
_COUNT_BITS = 16
_COUNT_MASK = (1 << _COUNT_BITS) - 1


@dataclass(frozen=True)
class Hit:
    """One passage found by a search.

    ``start`` and ``end`` are the passage's offsets in its document's stored
    text, which a snippet or a read needs. ``score`` orders hits and is left
    out of ``repr()`` so that it does not end up in a log line or a result by
    accident.
    """

    doc_id: str
    passage: int  # counted from 1
    start: int
    end: int
    score: float = field(repr=False)

    @property
    def ref(self) -> str:
        """The passage's ref, for example ``d-3f9a2c41b07e.7``."""
        return f"{self.doc_id}.{self.passage}"

    @property
    def span(self) -> Span:
        """``(start, end)``."""
        return self.start, self.end


@dataclass(frozen=True)
class _Document:
    first: int  # index number of its first passage; the others follow without a gap
    spans: tuple[Span, ...]
    listed: tuple[Span, ...]
    chars: int


def _rank(hit: Hit) -> tuple[float, str, int]:
    return -hit.score, hit.doc_id, hit.passage


def merge_hits(groups: Iterable[Iterable[Hit]], limit: int) -> list[Hit]:
    """Merge the result lists of several collections: best score first, at most ``limit`` hits.

    Hits with equal scores are ordered by document id, then passage number.
    """
    merged = sorted((hit for group in groups for hit in group), key=_rank)
    return merged[: max(0, limit)]


class CollectionIndex:
    """An in-memory BM25 index over the passages of one collection.

    ``listed`` are the owner's listed strings (or the result of
    :func:`~wdva.gate.textproc.compile_listed`); ``show_titles`` says whether
    titles of this collection are shown to callers, and only then are they
    searchable. Both are fixed for the life of the index: when either changes,
    build a new one.
    """

    def __init__(
        self,
        *,
        show_titles: bool = False,
        listed: ListedStrings | Iterable[str] = (),
        chunk_chars: int = CHUNK_CHARS,
    ) -> None:
        self.show_titles = show_titles
        self._listed = listed if isinstance(listed, ListedStrings) else compile_listed(listed)
        self._chunk_chars = chunk_chars
        self._documents: dict[str, _Document] = {}
        self._postings: dict[str, array[int]] = {}
        self._lengths: dict[int, int] = {}  # passage number in the index -> indexed words
        self._where: dict[int, tuple[str, int]] = {}  # -> (document id, passage counted from 1)
        self._next = 0
        self._total_length = 0

    # -- content ---------------------------------------------------------

    def add(self, doc_id: str, text: str, title: str = "") -> int:
        """Index one document's sanitised text and return its number of passages.

        The text is cut into passages, the listed strings in it are located, and
        every word outside them is indexed. With ``show_titles`` each word of
        ``title`` (outside listed strings) counts once in every passage of the
        document. Raises :class:`ValueError` when ``doc_id`` is already indexed.
        """
        if doc_id in self._documents:
            raise ValueError(f"document {doc_id} is already in the index")
        listed = tuple(find_listed(text, self._listed))
        spans = tuple(chunk_spans(text, self._chunk_chars))
        title_terms: frozenset[str] = frozenset()
        if self.show_titles and title:
            title_terms = frozenset(tokens(title, matches=find_listed(title, self._listed)))
        first = self._next
        for offset, (start, end) in enumerate(spans):
            counts = Counter(tokens(text, start, end, listed))
            for term in title_terms:
                counts[term] += 1
            number = first + offset
            length = sum(counts.values())
            self._lengths[number] = length
            self._where[number] = (doc_id, offset + 1)
            self._total_length += length
            base = number << _COUNT_BITS
            for term, count in counts.items():
                packed = base | min(count, _COUNT_MASK)
                postings = self._postings.get(term)
                if postings is None:
                    self._postings[term] = array("Q", (packed,))
                else:
                    postings.append(packed)
        self._next = first + len(spans)
        self._documents[doc_id] = _Document(first, spans, listed, len(text))
        return len(spans)

    def remove(self, doc_id: str) -> None:
        """Take one document out of the index. Raises :class:`KeyError` when it is not there."""
        document = self._documents.pop(doc_id)
        low = document.first << _COUNT_BITS
        high = (document.first + len(document.spans)) << _COUNT_BITS
        emptied: list[str] = []
        for term, postings in self._postings.items():
            if postings[-1] < low:
                continue
            begin = bisect_left(postings, low)
            if postings[begin] >= high:
                continue
            del postings[begin : bisect_left(postings, high, begin)]
            if not postings:
                emptied.append(term)
        for term in emptied:
            del self._postings[term]
        for number in range(document.first, document.first + len(document.spans)):
            self._total_length -= self._lengths.pop(number)
            del self._where[number]

    def __contains__(self, doc_id: object) -> bool:
        return doc_id in self._documents

    def __len__(self) -> int:
        """The number of documents."""
        return len(self._documents)

    def doc_ids(self) -> list[str]:
        """The ids of the indexed documents, sorted."""
        return sorted(self._documents)

    @property
    def passage_count(self) -> int:
        """How many passages are indexed."""
        return len(self._lengths)

    @property
    def term_count(self) -> int:
        """How many different words are indexed."""
        return len(self._postings)

    def terms(self) -> frozenset[str]:
        """Every indexed word, folded. For the owner's side and for tests: never for a caller."""
        return frozenset(self._postings)

    # -- per document ------------------------------------------------------

    def passages(self, doc_id: str) -> int:
        """How many passages the document has. Raises :class:`KeyError` when it is not indexed."""
        return len(self._documents[doc_id].spans)

    def span(self, doc_id: str, passage: int) -> Span:
        """Where passage ``passage`` (counted from 1) lies in the document's stored text.

        Raises :class:`KeyError` when the document is not indexed and
        :class:`IndexError` when it has no such passage.
        """
        spans = self._documents[doc_id].spans
        if not 1 <= passage <= len(spans):
            raise IndexError(f"{doc_id} has no passage {passage}")
        return spans[passage - 1]

    def listed(self, doc_id: str) -> tuple[Span, ...]:
        """Where listed strings stand in the document's stored text: sorted, separate spans."""
        return self._documents[doc_id].listed

    def chars(self, doc_id: str) -> int:
        """The length of the document's stored text."""
        return self._documents[doc_id].chars

    # -- search ----------------------------------------------------------

    def search(
        self, terms: Sequence[str], limit: int = 5, *, per_document: int = PER_DOCUMENT
    ) -> list[Hit]:
        """The best passages for ``terms``, best first: at most ``limit``, ``per_document`` each.

        ``terms`` are folded query words, as
        :func:`~wdva.gate.textproc.query_terms` returns them; only the first 16
        different ones are used. A word matches a passage only when the passage
        (or, where titles are shown, the title) contains exactly that word.
        Hits with equal scores are ordered by document id, then passage number.
        No usable term gives an empty list.
        """
        if isinstance(terms, str):
            raise TypeError("terms must be a sequence of folded words, not one string")
        total = len(self._lengths)
        if limit <= 0 or per_document <= 0 or not total:
            return []
        average = self._total_length / total or 1.0
        scores: dict[int, float] = {}
        for term in list(dict.fromkeys(terms))[:MAX_QUERY_TERMS]:
            postings = self._postings.get(term)
            if not postings:
                continue
            found = len(postings)
            idf = math.log(1.0 + (total - found + 0.5) / (found + 0.5))
            for packed in postings:
                number = packed >> _COUNT_BITS
                count = packed & _COUNT_MASK
                norm = count + K1 * (1.0 - B + B * self._lengths[number] / average)
                scores[number] = scores.get(number, 0.0) + idf * count * (K1 + 1.0) / norm
        hits: list[Hit] = []
        taken: Counter[str] = Counter()
        for number in sorted(scores, key=lambda n: (-scores[n], self._where[n])):
            doc_id, passage = self._where[number]
            if taken[doc_id] >= per_document:
                continue
            taken[doc_id] += 1
            start, end = self._documents[doc_id].spans[passage - 1]
            hits.append(Hit(doc_id, passage, start, end, scores[number]))
            if len(hits) == limit:
                break
        return hits
