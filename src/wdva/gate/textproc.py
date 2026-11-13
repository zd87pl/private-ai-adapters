# SPDX-License-Identifier: Apache-2.0
# Copyright 2025 Enclave
"""Text handling for the document gate: pure functions over strings.

Nothing here reads a file, holds a key or keeps state between calls (small
per-character caches aside). The functions cover the whole path of document
text:

* :func:`sanitize` turns extracted text into the form that is stored: no links,
  no markup that a chat window would render, no invisible characters.
  :func:`check_sanitized` names what a text still violates.
* :func:`chunk_spans` cuts stored text into passages of at most 800 characters.
* :func:`fold`, :func:`tokens` and :func:`query_terms` define what a search
  word is: whole words, without case and accents, never a prefix.
* :func:`find_listed` finds the strings the owner listed, :func:`render`
  returns a span of a document with each of them replaced by ``[REDACTED]``,
  and :func:`snippet_window` picks the short window a search result may show.

Every regular expression has bounded repeats or cannot backtrack, and every
loop runs once over its input, so the time taken grows linearly with the text.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "CHUNK_CHARS",
    "ELLIPSIS",
    "LINK_REMOVED",
    "MAX_QUERY_CHARS",
    "MAX_QUERY_TERMS",
    "MAX_TOKEN_CHARS",
    "REDACTED",
    "SANITIZER_VERSION",
    "SNIPPET_CHARS",
    "ListedStrings",
    "Span",
    "check_sanitized",
    "chunk_spans",
    "compile_listed",
    "find_listed",
    "fold",
    "grow",
    "has_digit",
    "merge_spans",
    "query_terms",
    "render",
    "sanitize",
    "snippet_text",
    "snippet_window",
    "token_spans",
    "tokens",
    "union_length",
]

Span = tuple[int, int]
"""A half-open range ``(start, end)`` of character offsets into a text."""

SANITIZER_VERSION = 1
"""Recorded with every stored document. A change to :func:`sanitize` needs a new number."""

CHUNK_CHARS = 800
"""The longest passage, in characters of the stored text."""

SNIPPET_CHARS = 160
"""The longest search snippet, in characters of the stored text outside listed strings."""

MAX_TOKEN_CHARS = 64
"""Longer runs of word characters are neither indexed nor searched for."""

MAX_QUERY_CHARS = 500
"""Only this many characters of a query are looked at."""

MAX_QUERY_TERMS = 16
"""Only this many different words of a query are used."""

REDACTED = "[REDACTED]"
"""What a listed string is replaced with."""

LINK_REMOVED = "[link removed]"
"""What a link is replaced with."""

ELLIPSIS = "..."
"""Marks the end of a snippet at which text of the passage was left out."""


# ---------------------------------------------------------------------------
# sanitize
# ---------------------------------------------------------------------------

_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})
_DROPPED_CODES = frozenset({0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0x17B4, 0x17B5})
_DROPPED_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0x180B, 0x180F))
_KEPT_CONTROLS = frozenset({0x09, 0x0A})

_LINE_SEPARATOR = chr(0x2028)
_PARAGRAPH_SEPARATOR = chr(0x2029)
# Besides CR LF: every control character that Python counts as whitespace, except
# the tab, and the two Unicode separators. Dropping them would glue two words.
_LINE_BREAKS = (
    "\r",
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x1f",
    "\x85",
    _LINE_SEPARATOR,
    _PARAGRAPH_SEPARATOR,
)

_MAX_PASSES = 4
_LINK_ROUNDS = 4


def _is_dropped(code: int) -> bool:
    """Whether :func:`sanitize` removes this character."""
    if code in _KEPT_CONTROLS:
        return False
    if code in _DROPPED_CODES or any(low <= code <= high for low, high in _DROPPED_RANGES):
        return True
    return unicodedata.category(chr(code)) in _DROPPED_CATEGORIES


class _DropTable(dict[int, int | None]):
    """A ``str.translate`` table that removes dropped characters, filled in as they are met."""

    def __missing__(self, code: int) -> int | None:
        value = None if _is_dropped(code) else code
        self[code] = value
        return value


_DROP = _DropTable()


def _any_case(word: str) -> str:
    """A pattern for ``word`` in any mix of upper and lower case, ASCII letters only."""
    return "".join(f"[{letter.upper()}{letter}]" for letter in word)


# The gate's own two markers are never read as the text of a link or as the name
# of a reference, so that a later pass leaves them alone.
_NOT_A_MARKER = "(?!(?:" + "|".join(re.escape(m[1:-1]) for m in (LINK_REMOVED, REDACTED)) + r")\])"
_LINK_TEXT = r"[^\[\]\n]{0,300}"
_LINK_TARGET = r"[^()\n]{0,2000}"
_NAMED_SCHEMES = ("mailto", "data", "javascript", "file", "tel", "magnet", "sms", "geo")

_IMAGE = re.compile(r"!\[" + _NOT_A_MARKER + "(" + _LINK_TEXT + r")\]\(" + _LINK_TARGET + r"\)")
_LINK = re.compile(r"\[" + _NOT_A_MARKER + "(" + _LINK_TEXT + r")\]\(" + _LINK_TARGET + r"\)")
_REFERENCE = re.compile(
    r"(?m)^[ \t]{0,3}\["
    + _NOT_A_MARKER
    + r"[^\[\]\n]{1,300}\]:[ \t]{0,8}(?:"
    + re.escape(LINK_REMOVED)
    + r"|\S{1,2000})"
)
_TAG = re.compile(r"<[A-Za-z/!?][^<>\n]{0,300}>")
_SCHEME = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.\-]{0,30}://|(?<![A-Za-z0-9])(?:"
    + _any_case("www")
    + r"\.|(?:"
    + "|".join(_any_case(name) for name in _NAMED_SCHEMES)
    + r"):))\S{1,2000}"
)
_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")
_HYPHENS = re.compile(r"-{5,}")
_BLANK_LINES = re.compile(r"\n{3,}")


def _plain_line_breaks(text: str) -> str:
    """Every kind of line ending, and every control character that separates lines, as ``\\n``."""
    text = text.replace("\r\n", "\n")
    for character in _LINE_BREAKS:
        if character in text:
            text = text.replace(character, "\n")
    return text


def _sanitize_pass(text: str) -> str:
    """One application of the five steps. :func:`sanitize` repeats it until nothing changes."""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_DROP)
    for _ in range(_LINK_ROUNDS):
        before = text
        if "](" in text:
            text = _IMAGE.sub(r"\1", text)
            text = _LINK.sub(r"\1", text)
        if "]:" in text:
            text = _REFERENCE.sub("", text)
        if "<" in text:
            text = _TAG.sub("", text)
        text = _SCHEME.sub(LINK_REMOVED, text)
        if text == before:
            break
    text = text.replace("://", ":/ /").replace("](", "] (").replace("![", "! [")
    text = _TAG_START.sub("< ", text)
    text = _HYPHENS.sub("----", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text)


def sanitize(text: str) -> str:
    """Return ``text`` in the form the gate stores and releases (sanitiser version 1).

    Line endings become ``\\n``, and so do form feeds and the other control
    characters that separate lines. Then one pass is repeated until it changes
    nothing: Unicode NFC; invisible, control and private-use characters dropped;
    Markdown links and images reduced to their text, reference definitions and
    HTML tags removed, addresses replaced by ``[link removed]``; what is left of
    link syntax broken up with a space (``://``, ``](``, ``![``, ``<`` before a
    tag name) and runs of five or more hyphens cut to four; trailing blanks
    removed and blank lines limited to one.

    The result is a fixed point: ``sanitize(sanitize(x)) == sanitize(x)``, and
    :func:`check_sanitized` finds nothing in it. Plain sentences pass unchanged,
    whatever they say. The text as a whole is not stripped.

    Raises :class:`ValueError` (``text does not settle``) when the fourth pass
    still changed the text, which takes deliberately nested or chained markup.
    """
    text = _plain_line_breaks(text)
    for _ in range(_MAX_PASSES):
        result = _sanitize_pass(text)
        if result == text:
            return text
        text = result
    raise ValueError("text does not settle")


def check_sanitized(text: str) -> list[str]:
    """Name what ``text`` violates of the things :func:`sanitize` guarantees; empty when nothing.

    The names are ``"://"``, ``"]("``, ``"!["``, ``"<tag"`` (a ``<`` followed by a
    letter, ``/``, ``!`` or ``?``), ``"-----"``, ``"dropped character"`` and
    ``"not NFC"``. This is a check of released text, not a second sanitiser: text
    that passes can still contain a bare ``host/path`` or a sentence that asks
    for a link.
    """
    problems = [needle for needle in ("://", "](", "![") if needle in text]
    if _TAG_START.search(text):
        problems.append("<tag")
    if "-----" in text:
        problems.append("-----")
    separators = _LINE_SEPARATOR in text or _PARAGRAPH_SEPARATOR in text
    if separators or text.translate(_DROP) != text:
        problems.append("dropped character")
    if not unicodedata.is_normalized("NFC", text):
        problems.append("not NFC")
    return problems


# ---------------------------------------------------------------------------
# Passages
# ---------------------------------------------------------------------------


def chunk_spans(text: str, size: int = CHUNK_CHARS) -> list[Span]:
    """Cut ``text`` into passages: contiguous spans of 1 to ``size`` characters that cover it.

    A passage ends after the last blank line that lies in its second half, else
    after the last line break there, else after the last space, else at ``size``
    characters. So every passage but the last is longer than ``size // 2``. The
    same text always gives the same spans; they are not stored anywhere.
    Passage ``n`` (counted from 1) is ``spans[n - 1]``.
    """
    if size < 2:
        raise ValueError("a passage needs room for at least two characters")
    spans: list[Span] = []
    start, length = 0, len(text)
    while start < length:
        end = min(start + size, length)
        if end < length:
            earliest = start + size // 2 + 1  # the cut itself must lie after the first half
            for separator in ("\n\n", "\n", " "):
                found = text.rfind(separator, earliest - len(separator), end)
                if found != -1:
                    end = found + len(separator)
                    break
        spans.append((start, end))
        start = end
    return spans


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    """Sort spans and join those that overlap or touch. Empty spans are left out."""
    merged: list[Span] = []
    for start, end in sorted(span for span in spans if span[0] < span[1]):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def union_length(spans: Iterable[Span]) -> int:
    """How many different characters the spans cover together."""
    return sum(end - start for start, end in merge_spans(spans))


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

_WORD = re.compile(r"\w+")
_LETTERS = str.maketrans({"ł": "l", "đ": "d", "ø": "o", "æ": "ae", "œ": "oe"})


def _fold_character(character: str) -> str:
    decomposed = unicodedata.normalize("NFKD", character).casefold()
    decomposed = unicodedata.normalize("NFKD", decomposed)
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", bare.translate(_LETTERS))


class _FoldTable(dict[int, str]):
    """A ``str.translate`` table that folds one character at a time, filled in as needed."""

    def __missing__(self, code: int) -> str:
        value = _fold_character(chr(code))
        self[code] = value
        return value


class _FoldLengthTable(dict[int, str]):
    """Maps a character to ``chr(n)``, where ``n`` is the length of its folded form."""

    def __missing__(self, code: int) -> str:
        value = chr(len(_FOLD[code]))
        self[code] = value
        return value


_FOLD = _FoldTable()
_FOLD_LENGTH = _FoldLengthTable()
_ONE_TO_ONE = chr(1)
_NOT_ONE_TO_ONE = re.compile("[^" + _ONE_TO_ONE + "]+")


def fold(token: str) -> str:
    """The form in which words are compared: without case, accents and typographic variants.

    Each character is folded on its own: compatibility forms are decomposed
    (``ﬁ`` to ``fi``, full-width to plain), case is folded (``ß`` to ``ss``),
    combining marks are dropped, and ``ł``, ``đ``, ``ø``, ``æ``, ``œ`` become
    ``l``, ``d``, ``o``, ``ae``, ``oe``. So ``Łódź`` and ``lodz`` are equal.
    There is no stemming: ``invoice`` and ``invoices`` stay different words.
    """
    if token.isascii():
        return token.lower()
    return token.translate(_FOLD)


def _overlaps(matches: Sequence[Span], start: int, end: int) -> bool:
    """Whether ``[start, end)`` shares a character with one of the sorted, separate ``matches``."""
    index = bisect_left(matches, (end, -1))
    return index > 0 and matches[index - 1][1] > start


def token_spans(
    text: str, start: int = 0, end: int | None = None, matches: Sequence[Span] = ()
) -> list[tuple[int, int, str]]:
    """The index words of ``text[start:end]`` as ``(start, end, folded word)``.

    The offsets are offsets into ``text``.

    A word is a run of word characters (``\\w+``), cut at ``start`` and ``end``.
    Runs longer than 64 characters are skipped, and so is every word that shares
    a character with one of ``matches``, the listed-string spans of this text as
    :func:`find_listed` returns them: such a word must not be searchable.
    """
    stop = len(text) if end is None else end
    found: list[tuple[int, int, str]] = []
    for match in _WORD.finditer(text, start, stop):
        first, last = match.span()
        if last - first > MAX_TOKEN_CHARS or (matches and _overlaps(matches, first, last)):
            continue
        folded = fold(match.group())
        if folded and len(folded) <= MAX_TOKEN_CHARS:
            found.append((first, last, folded))
    return found


def tokens(
    text: str, start: int = 0, end: int | None = None, matches: Sequence[Span] = ()
) -> list[str]:
    """The folded index words of ``text[start:end]``, in order, repeats included.

    The same words as :func:`token_spans`, without their positions. The number
    of distinct index terms of a text is ``len(set(tokens(text)))``.
    """
    stop = len(text) if end is None else end
    if matches and start < stop and _overlaps(matches, start, stop):
        return [folded for _, _, folded in token_spans(text, start, stop, matches)]
    piece = text if start == 0 and stop >= len(text) else text[start:stop]
    if piece.isascii():
        return [word for word in _WORD.findall(piece.lower()) if len(word) <= MAX_TOKEN_CHARS]
    found: list[str] = []
    for word in _WORD.findall(piece):
        if len(word) <= MAX_TOKEN_CHARS:
            folded = fold(word)
            if folded and len(folded) <= MAX_TOKEN_CHARS:
                found.append(folded)
    return found


def has_digit(term: str) -> bool:
    """Whether a folded word contains a digit or another character with a numeric value."""
    return any(character.isnumeric() for character in term)


def query_terms(query: str, *, digits: bool = True) -> list[str]:
    """The words a query searches for: folded, in order, each once, at most 16.

    Only the first 500 characters are looked at. Words are whole words: there
    are no prefixes and no wildcards, and a ``*`` is punctuation. With
    ``digits=False`` every term that contains a digit is left out, which is how
    a collection is searched whose passages all need the owner's approval; the
    result is then a subset of what ``digits=True`` returns.
    """
    # A query is read as stored text is: separators, composed letters, nothing invisible.
    text = unicodedata.normalize("NFC", _plain_line_breaks(query[:MAX_QUERY_CHARS]))
    text = unicodedata.normalize("NFC", text.translate(_DROP))
    terms: dict[str, None] = {}
    for word in _WORD.findall(text):
        if len(word) > MAX_TOKEN_CHARS:
            continue
        folded = fold(word)
        if not folded or len(folded) > MAX_TOKEN_CHARS:
            continue
        terms.setdefault(folded)
        if len(terms) == MAX_QUERY_TERMS:
            break
    return [term for term in terms if digits or not has_digit(term)]


# ---------------------------------------------------------------------------
# Listed strings
# ---------------------------------------------------------------------------

_NON_WORD_RUN = re.compile(r"\W+")
_LONG_NON_WORD_RUN = re.compile(r"\W{2,}")
# One space, hyphen-minus or hyphen (U+2010) between two digits.
_DIGIT_GAP = re.compile(r"(?<=\d)[ \-" + chr(0x2010) + r"](?=\d)")
_ANY_DIGIT = re.compile(r"\d")


@dataclass(frozen=True)
class ListedStrings:
    """The owner's listed strings, folded once so that many texts can be searched for them.

    ``words`` are searched for in folded text whose runs of non-word characters
    are each one space; ``digits`` in folded text from which every single space
    or hyphen between two digits was taken out.
    """

    words: tuple[str, ...] = ()
    digits: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.words or self.digits)


def compile_listed(strings: Iterable[str]) -> ListedStrings:
    """Fold the listed strings and split them into their words, ready for :func:`find_listed`."""
    words: dict[str, None] = {}
    digits: dict[str, None] = {}
    for string in strings:
        parts = _WORD.findall(fold(unicodedata.normalize("NFC", string)))
        if not parts:
            continue
        if len(parts) == 1 and parts[0].isdecimal():
            digits.setdefault(parts[0])
        else:
            words.setdefault(" ".join(parts))
    return ListedStrings(tuple(words), tuple(digits))


class _OffsetMap:
    """Positions in a derived string, mapped back to the string it was made from.

    The derived string equals its source except at a number of edits, each of
    which turned ``source_length`` characters into ``derived_length`` characters.
    Every derived character of an edit maps back to the whole source range of
    that edit.
    """

    __slots__ = ("_derived", "_derived_length", "_shift", "_source", "_source_length")

    def __init__(self) -> None:
        self._derived: list[int] = []
        self._source: list[int] = []
        self._derived_length: list[int] = []
        self._source_length: list[int] = []
        self._shift = 0

    def add(self, source: int, source_length: int, derived_length: int) -> None:
        """Record one edit. Edits are added in the order of their positions."""
        self._derived.append(source + self._shift)
        self._source.append(source)
        self._derived_length.append(derived_length)
        self._source_length.append(source_length)
        self._shift += derived_length - source_length

    def _locate(self, position: int, edge: int) -> int:
        """Source offset of the start (``edge`` 0) or end (``edge`` 1) of one derived character."""
        index = bisect_right(self._derived, position) - 1
        if index < 0:
            return position + edge
        inside = position - self._derived[index]
        if inside < self._derived_length[index]:
            return self._source[index] + edge * self._source_length[index]
        after = self._source[index] + self._source_length[index]
        return after + inside - self._derived_length[index] + edge

    def span(self, start: int, end: int) -> Span:
        """The source range that ``derived[start:end]`` came from (``end > start``)."""
        return self._locate(start, 0), self._locate(end - 1, 1)


def _folded_with_map(text: str) -> tuple[str, _OffsetMap | None]:
    """``text`` folded character by character, and the way back (``None``: same offsets)."""
    if text.isascii():
        return text.lower(), None
    lengths = text.translate(_FOLD_LENGTH)
    folded = text.translate(_FOLD)
    if lengths.count(_ONE_TO_ONE) == len(lengths):
        return folded, None
    offsets = _OffsetMap()
    for run in _NOT_ONE_TO_ONE.finditer(lengths):
        offsets.add(run.start(), run.end() - run.start(), sum(map(ord, run.group())))
    return folded, offsets


def _search(
    haystack: str,
    needles: Sequence[str],
    maps: Sequence[_OffsetMap | None],
    found: list[Span],
) -> None:
    """Add every occurrence of every needle to ``found``, mapped back through ``maps`` in turn.

    Occurrences of one needle that overlap are added as one span, so a text that
    repeats a needle at every position costs one entry, not one per character.
    """
    for needle in needles:
        size = len(needle)
        position = haystack.find(needle)
        while position != -1:
            end = position + size
            following = haystack.find(needle, position + 1)
            while following != -1 and following < end:
                end = following + size
                following = haystack.find(needle, following + 1)
            span = (position, end)
            for offsets in maps:
                if offsets is not None:
                    span = offsets.span(*span)
            found.append(span)
            position = following


def find_listed(text: str, listed: ListedStrings | Iterable[str]) -> list[Span]:
    """Find every occurrence of the owner's listed strings in ``text``: sorted, separate spans.

    Matching ignores case, accents and typographic variants (see :func:`fold`).
    Between two words of a listed string any run of non-word characters matches,
    and the characters around a listed string do not matter, so it is also found
    inside a longer word. In a listed string made only of digits, one space or
    hyphen may stand between any two digits. Occurrences that overlap or touch
    are joined. Other forms of the same name or number (inflected, abbreviated,
    in another order, split across a line with a hyphen) are different strings
    and are not found.

    Pass the result of :func:`compile_listed` when many texts are searched.
    ``text`` is expected to be sanitised; the offsets are offsets into it.
    """
    compiled = listed if isinstance(listed, ListedStrings) else compile_listed(listed)
    if not text or not compiled:
        return []
    folded, fold_map = _folded_with_map(text)
    found: list[Span] = []
    if compiled.words:
        word_map = _OffsetMap()
        for run in _LONG_NON_WORD_RUN.finditer(folded):
            word_map.add(run.start(), run.end() - run.start(), 1)
        _search(_NON_WORD_RUN.sub(" ", folded), compiled.words, (word_map, fold_map), found)
    if compiled.digits and _ANY_DIGIT.search(folded):
        digit_map = _OffsetMap()
        for gap in _DIGIT_GAP.finditer(folded):
            digit_map.add(gap.start(), 1, 0)
        _search(_DIGIT_GAP.sub("", folded), compiled.digits, (digit_map, fold_map), found)
    merged = merge_spans(found)
    if fold_map is None:
        return merged
    # A character after a match that folds to nothing is a combining mark on the
    # last letter of the match: cover it too.
    covered: list[Span] = []
    length = len(text)
    for start, end in merged:
        while end < length and not fold(text[end]):
            end += 1
        if covered and start <= covered[-1][1]:
            covered[-1] = (covered[-1][0], max(covered[-1][1], end))
        else:
            covered.append((start, end))
    return covered


def grow(span: Span, matches: Sequence[Span]) -> Span:
    """Widen ``span`` so that it cuts through none of the sorted, separate ``matches``."""
    start, end = span
    index = bisect_left(matches, (start, -1)) - 1
    if index >= 0 and matches[index][1] > start:
        start = matches[index][0]
    index = bisect_left(matches, (end, -1)) - 1
    if index >= 0 and matches[index][1] > end:
        end = matches[index][1]
    return start, end


def render(text: str, span: Span, matches: Sequence[Span] = (), *, one_line: bool = False) -> str:
    """The releasable form of ``text[span]``: listed strings replaced, then sanitised again.

    ``matches`` are the listed-string spans of the whole text, as
    :func:`find_listed` returns them. The span is first widened with
    :func:`grow`, so no part of a listed string can be left at an edge; each
    match becomes ``[REDACTED]``; the result goes through :func:`sanitize` once
    more, because a replacement can stand where it forms markup the stored text
    did not contain; then surrounding whitespace is removed. With ``one_line``
    all whitespace, line breaks included, becomes single spaces.

    Raises :class:`ValueError` when the text does not settle (see :func:`sanitize`).
    """
    start, end = grow((max(0, span[0]), min(len(text), span[1])), matches)
    parts: list[str] = []
    position = start
    index = bisect_left(matches, (start, -1))
    while index < len(matches) and matches[index][0] < end:
        parts.append(text[position : matches[index][0]])
        parts.append(REDACTED)
        position = matches[index][1]
        index += 1
    parts.append(text[position:end])
    replaced = "".join(parts)
    if one_line:
        return " ".join(sanitize(" ".join(replaced.split())).split())
    return sanitize(replaced).strip()


# ---------------------------------------------------------------------------
# Snippets
# ---------------------------------------------------------------------------


def _without_blank_edges(text: str, span: Span) -> Span:
    start = max(0, span[0])
    piece = text[start : span[1]]
    kept = piece.lstrip()
    start += len(piece) - len(kept)
    return start, start + len(kept.rstrip())


def snippet_window(
    text: str,
    passage: Span,
    terms: Collection[str],
    matches: Sequence[Span] = (),
    *,
    size: int = SNIPPET_CHARS,
) -> Span:
    """Choose the part of a passage that a search result may quote, as a span of ``text``.

    A window starts at a word and ends at the end of the last word that still
    fits within ``size`` characters. Of the windows that start at one of
    ``terms`` (folded query words, as :func:`query_terms` returns them), the one
    that holds the most different terms wins; among equals, the earliest. No
    other window holds more, because any window can give up what stands before
    its first query word. A passage without a query word gives the window at
    its start.

    Words that share a character with one of ``matches`` (the listed-string
    spans of the text) do not count as query words, so the place of a window
    never tells whether a query word equals a listed string. A window that
    reaches the first or last word of the passage takes the punctuation before
    or after it along when that fits.

    Finally the window is widened with :func:`grow`: it is at most ``size``
    characters long outside listed strings.
    """
    first, last = _without_blank_edges(text, passage)
    wanted = frozenset(terms)
    words: list[tuple[int, int, str]] = []
    for match in _WORD.finditer(text, first, last):
        start, end = match.span()
        term = ""
        if wanted and end - start <= MAX_TOKEN_CHARS and not _overlaps(matches, start, end):
            folded = fold(match.group())
            if folded in wanted:
                term = folded
        words.append((start, end, term))
    if not words:
        return grow((first, min(last, first + size)), matches)

    count = len(words)
    anywhere = not any(term for _, _, term in words)  # no query word: the first window
    inside: dict[str, int] = {}
    best_terms, best_from, best_to = -1, 0, -1
    reach = -1  # the last word inside the window that starts at word `index`
    for index in range(count):
        if index > 0 and reach >= index - 1:
            gone = words[index - 1][2]
            if gone:
                inside[gone] -= 1
                if not inside[gone]:
                    del inside[gone]
        reach = max(reach, index - 1)
        limit = words[index][0] + size
        while reach + 1 < count and words[reach + 1][1] <= limit:
            reach += 1
            term = words[reach][2]
            if term:
                inside[term] = inside.get(term, 0) + 1
        if (words[index][2] or anywhere) and len(inside) > best_terms:
            best_terms, best_from, best_to = len(inside), index, reach

    start = words[best_from][0]
    # A single word longer than the window is cut; nothing else ever is.
    end = words[best_to][1] if best_to >= best_from else min(last, start + size)
    if best_to == count - 1 and last - start <= size:
        end = last
    if best_from == 0 and end - first <= size:
        start = first
    return grow((start, end), matches)


def snippet_text(text: str, passage: Span, window: Span, matches: Sequence[Span] = ()) -> str:
    """The snippet for ``window`` (from :func:`snippet_window`): one line, cut ends marked ``...``.

    The text is that of :func:`render` with ``one_line``. An end is marked when
    the passage has more than blanks beyond it.
    """
    start, end = grow(window, matches)
    body = render(text, (start, end), matches, one_line=True)
    lead = ELLIPSIS if text[passage[0] : start].strip() else ""
    trail = ELLIPSIS if text[end : passage[1]].strip() else ""
    return f"{lead}{body}{trail}"
