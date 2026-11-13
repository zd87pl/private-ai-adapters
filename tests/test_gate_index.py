"""The gate's search index: whole words, one collection at a time, blind to listed strings."""

from __future__ import annotations

import math
from collections.abc import Iterable

import pytest

from wdva.gate.index import K1, PER_DOCUMENT, B, CollectionIndex, Hit, merge_hits
from wdva.gate.textproc import chunk_spans, find_listed, fold, query_terms, tokens

DOC_A = "d-00000000000a"
DOC_B = "d-00000000000b"
DOC_C = "d-00000000000c"
DOC_D = "d-00000000000d"

LEASE = (
    "Lease for the flat at 12 Garden Street.\n\n"
    "The rent is 1,450 EUR per month, due on the 3rd working day of each month. "
    "The deposit of 2,900 EUR is returned within 30 days after the keys are handed back."
)
LETTER = "The bank confirms the standing order for the rent. The account stays open."
MANUAL = "Bleed the radiators in autumn. The boiler manual is in the kitchen drawer."


def refs(hits: Iterable[Hit]) -> list[str]:
    return [hit.ref for hit in hits]


def search(index: CollectionIndex, query: str, limit: int = 10) -> list[str]:
    return refs(index.search(query_terms(query), limit))


def test_tokeniser_and_folding_cases() -> None:
    # Words are runs of letters, digits and underscores; everything else separates.
    assert tokens("The Rent is 1,450 EUR; well-known foo_bar (annex 2).") == [
        "the",
        "rent",
        "is",
        "1",
        "450",
        "eur",
        "well",
        "known",
        "foo_bar",
        "annex",
        "2",
    ]
    # Case.
    assert fold("RENT") == fold("Rent") == fold("rent") == "rent"
    assert fold("STRASSE") == fold("Straße") == "strasse"
    assert fold("\N{GREEK CAPITAL LETTER SIGMA}") == fold("\N{GREEK SMALL LETTER FINAL SIGMA}")
    assert fold("\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}stanbul") == "istanbul"
    # Accents.
    assert fold("Zażółć") == "zazolc"
    assert fold("GĘŚLĄ") == "gesla"
    assert fold("naïve") == "naive"
    assert fold("Ångström") == "angstrom"
    assert fold("cre\N{COMBINING GRAVE ACCENT}me") == "creme"
    # The letters that no decomposition reaches.
    assert fold("ŁÓDŹ") == fold("łódź") == "lodz"
    assert fold("Đorđe") == "dorde"
    assert fold("Ærø") == fold("ærø") == "aero"
    assert fold("Œuvre") == fold("œuvre") == "oeuvre"
    assert fold("\N{LATIN SMALL LETTER O WITH STROKE AND ACUTE}l") == "ol"
    assert fold("\N{LATIN CAPITAL LETTER AE WITH ACUTE}on") == "aeon"
    # Typographic variants.
    assert fold("\N{LATIN SMALL LIGATURE FI}nance") == "finance"
    assert fold("".join(chr(ord(c) + 0xFEE0) for c in "Rent2025")) == "rent2025"
    assert fold("\N{MATHEMATICAL BOLD CAPITAL R}ent") == "rent"
    assert fold("\N{HANGUL SYLLABLE HAN}") == "\N{HANGUL SYLLABLE HAN}"
    # Folding again changes nothing, and there is no stemming.
    for word in ("Zażółć", "Straße", "Ærø", "\N{LATIN SMALL LIGATURE FFI}", "İ", "x" * 70):
        assert fold(fold(word)) == fold(word)
    assert fold("invoices") != fold("invoice")

    # The 64-character cut-off: a longer run is no word at all.
    longest, too_long = "a" * 64, "b" * 65
    assert tokens(f"{longest} {too_long} c") == [longest, "c"]
    assert tokens("ż" * 64 + " " + "ż" * 65) == ["z" * 64]
    assert tokens("ß" * 32 + " " + "ß" * 33) == ["s" * 64]  # judged after folding as well

    # And the same through an index.
    index = CollectionIndex()
    index.add(DOC_A, f"Zażółć gęślą jaźń. Straße. {longest} {too_long} end")
    for query in ("zazolc", "ZAŻÓŁĆ", "Gesla", "jazn", "strasse", "STRASSE", longest.upper()):
        assert search(index, query) == [f"{DOC_A}.1"], query
    assert search(index, too_long) == []
    assert index.terms() == {"zazolc", "gesla", "jazn", "strasse", longest, "end"}
    assert index.term_count == 6


def test_no_prefix_matching() -> None:
    index = CollectionIndex()
    index.add(DOC_A, "The invoice was sent in May.")
    index.add(DOC_B, "All invoices were paid in June.")
    assert "invoic" not in index.terms()
    for query in (
        "invoic",
        "invoic*",
        "invoic%",
        "invoic.",
        "invo",
        "i",
        "nvoice",
        "voice",
        "invoicesx",
    ):
        assert search(index, query) == [], query
    assert search(index, "invoice") == [f"{DOC_A}.1"]
    assert search(index, "invoices") == [f"{DOC_B}.1"]
    assert search(index, "INVOICE invoices") == [f"{DOC_A}.1", f"{DOC_B}.1"]
    # The index takes terms as they are: nothing is expanded on the way in either.
    assert index.search(["invoic"]) == []
    assert index.search(["invoic*"]) == []
    assert index.search([]) == []
    assert index.search(["invoice"], 0) == []
    with pytest.raises(TypeError):
        index.search("invoice")


def _home() -> CollectionIndex:
    home = CollectionIndex()
    home.add(DOC_A, LEASE)
    home.add(DOC_B, LETTER)
    home.add(DOC_C, MANUAL)
    return home


def test_ranking_is_independent_of_other_collections() -> None:
    queries = ["rent", "rent deposit keys", "the", "the rent account", "boiler manual the"]
    home = _home()
    before = {query: home.search(query_terms(query), 10) for query in queries}
    assert refs(before["rent"]) == [f"{DOC_B}.1", f"{DOC_A}.1"]

    # Another collection grows, with the same words in very different numbers.
    tax = CollectionIndex()
    for number in range(40):
        tax.add(
            f"d-1{number:011x}", "rent " * (number + 1) + "the tax return, the account, the keys"
        )
    for query in queries:
        assert home.search(query_terms(query), 10) == before[query]  # order and scores
        # Merged with the other collection's hits, this collection's keep their order.
        merged = merge_hits([before[query], tax.search(query_terms(query), 10)], 20)
        assert [hit for hit in merged if hit.doc_id in home] == before[query]

    # The same documents added to this collection do change its scores: the
    # statistics are per collection, and the comparison above could have failed.
    for number in range(40):
        home.add(
            f"d-1{number:011x}", "rent " * (number + 1) + "the tax return, the account, the keys"
        )
    after = [hit for hit in home.search(["rent"], 50, per_document=5) if hit.doc_id == DOC_B]
    assert after[0].ref == before["rent"][0].ref
    assert after[0].score != before["rent"][0].score
    # A second index of the same documents starts from nothing.
    assert _home().search(["rent"], 10) == before["rent"]


def test_listed_strings_are_not_searchable() -> None:
    listed = ["Jan Kowalski", "85010112345", "Łódź", "owal"]
    lease = (
        "The tenant Jan Kowalski signs the lease. PESEL 850101 12345 is on file. "
        "The flat is in Łódź. The Kowalskis keep a cat."
    )
    letter = "Jan Nowak rents the garage. His number is 12345."
    only_lease = CollectionIndex(listed=listed)
    only_lease.add(DOC_A, lease)
    index = CollectionIndex(listed=listed)
    index.add(DOC_A, lease)
    index.add(DOC_B, letter)

    # No word that shares a character with a match is in the index.
    hidden = {"jan", "kowalski", "kowalskis", "850101", "12345", "lodz"}
    assert hidden <= set(tokens(lease))
    assert only_lease.terms() == set(tokens(lease)) - hidden
    assert index.terms() == only_lease.terms() | set(tokens(letter))
    for query in (
        "kowalski",
        "Kowalski",
        "KOWALSKI",
        "Jan Kowalski",
        "kowalskis",
        "85010112345",
        "850101",
        "lodz",
        "Łódź",
    ):
        assert DOC_A not in {hit.doc_id for hit in index.search(query_terms(query), 10)}, query
    # The same words outside a match are ordinary words: another form passes.
    assert search(index, "jan") == [f"{DOC_B}.1"]
    assert search(index, "12345") == [f"{DOC_B}.1"]
    assert search(index, "tenant cat") == [f"{DOC_A}.1"]

    # The positions the index keeps are those of the matches, for the whole document.
    assert index.listed(DOC_A) == tuple(find_listed(lease, listed))
    assert [lease[a:b] for a, b in index.listed(DOC_A)] == [
        "Jan Kowalski",
        "850101 12345",
        "Łódź",
        "owal",
    ]
    assert index.listed(DOC_B) == ()

    # Without the list, every one of these words is found.
    plain = CollectionIndex()
    plain.add(DOC_A, lease)
    for query in ("kowalski", "jan", "850101", "lodz", "kowalskis"):
        assert search(plain, query) == [f"{DOC_A}.1"], query

    # A match that crosses from one passage into the next hides its words in both.
    filler = "word " * 158
    crossing = filler + "Jan\n\nKowalski " + filler
    index = CollectionIndex(listed=listed)
    assert index.add(DOC_C, crossing) == 2
    assert index.span(DOC_C, 1)[1] == crossing.index("Kowalski")
    assert index.terms() == {"word"}


def test_titles_are_indexed_only_when_shown() -> None:
    body = ("The rent is due monthly. " * 20 + "\n\n") * 2
    title = "lease-2025 Kowalski flat"
    hidden = CollectionIndex(show_titles=False, listed=["Kowalski"])
    shown = CollectionIndex(show_titles=True, listed=["Kowalski"])
    for index in (hidden, shown):
        assert index.add(DOC_A, body, title) == 2
        index.add(DOC_B, "Another document about a garden.", "notes")

    for word in ("lease", "2025", "flat", "notes"):
        assert search(hidden, word) == [], word
        assert word not in hidden.terms()
    # A title word matches every passage of its document, and no other document.
    assert search(shown, "lease") == [f"{DOC_A}.1", f"{DOC_A}.2"]
    assert search(shown, "flat 2025") == [f"{DOC_A}.1", f"{DOC_A}.2"]
    assert search(shown, "notes") == [f"{DOC_B}.1"]
    # A listed string is no more searchable in a title than in the text.
    assert search(shown, "kowalski") == []
    assert "kowalski" not in shown.terms()
    # Words of the text are found either way.
    assert search(hidden, "rent") == search(shown, "rent") == [f"{DOC_A}.1", f"{DOC_A}.2"]
    assert search(hidden, "garden") == search(shown, "garden") == [f"{DOC_B}.1"]
    # A title word counts once per passage, however often the title repeats it.
    once = CollectionIndex(show_titles=True)
    once.add(DOC_A, "alpha beta", "gamma")
    often = CollectionIndex(show_titles=True)
    often.add(DOC_A, "alpha beta", "gamma gamma GAMMA")
    assert once.search(["gamma"]) == often.search(["gamma"])


def test_no_scores_and_deterministic_ties() -> None:
    block = "alpha " * 99 + "omega\n\n"  # 601 characters: one passage
    index = CollectionIndex()
    # Added in the opposite order of their ids.
    for doc_id in (DOC_C, DOC_B, DOC_A):
        assert index.add(doc_id, block * 2) == 2
    hits = index.search(["alpha", "omega"], 10)
    assert len({hit.score for hit in hits}) == 1  # six passages, one score
    # Ties go by document id, then passage number, not by the order of adding.
    assert refs(hits) == [f"{doc}.{n}" for doc in (DOC_A, DOC_B, DOC_C) for n in (1, 2)]
    assert refs(index.search(["alpha"], 3)) == [f"{DOC_A}.1", f"{DOC_A}.2", f"{DOC_B}.1"]
    assert index.search(["alpha", "omega"], 10) == hits
    reordered = CollectionIndex()
    for doc_id in (DOC_B, DOC_A, DOC_C):
        reordered.add(doc_id, block * 2)
    assert reordered.search(["alpha", "omega"], 10) == hits
    # The same rule when the lists of several collections are merged.
    other = CollectionIndex()
    for doc_id in (DOC_D, "d-000000000000", "d-00000000000e"):
        other.add(doc_id, block * 2)  # the same numbers, so the same scores
    merged = merge_hits([hits, other.search(["alpha", "omega"], 10)], 7)
    assert len({hit.score for hit in merged}) == 1
    assert refs(merged) == [
        "d-000000000000.1",
        "d-000000000000.2",
        f"{DOC_A}.1",
        f"{DOC_A}.2",
        f"{DOC_B}.1",
        f"{DOC_B}.2",
        f"{DOC_C}.1",
    ]
    assert merge_hits([other.search(["alpha", "omega"], 10), hits], 7) == merged
    assert merge_hits([hits], 0) == []

    # A result shows where a passage is and nothing about how it scored.
    assert repr(hits[0]) == f"Hit(doc_id='{DOC_A}', passage=1, start=0, end=601)"
    assert "score" not in repr(hits)
    assert str(hits[0].score) not in repr(hits[0])
    assert hits[0].ref == f"{DOC_A}.1"
    assert hits[0].span == (0, 601)
    assert hits[1].span == (601, 1202)
    shown = [hits[0].ref, hits[0].doc_id, hits[0].passage, *hits[0].span]
    assert not any(isinstance(value, float) for value in shown)


def test_scores_are_okapi_bm25() -> None:
    assert (K1, B) == (1.2, 0.75)
    index = CollectionIndex()
    index.add(DOC_A, "rent rent deposit")
    index.add(DOC_B, "rent keys")
    index.add(DOC_C, "garden shed tools door")
    passages, average = 3, (3 + 2 + 4) / 3

    def idf(found: int) -> float:
        return math.log(1 + (passages - found + 0.5) / (found + 0.5))

    def part(found: int, count: int, length: int) -> float:
        return idf(found) * count * (K1 + 1) / (count + K1 * (1 - B + B * length / average))

    hits = index.search(["rent"])
    assert refs(hits) == [f"{DOC_A}.1", f"{DOC_B}.1"]
    assert hits[0].score == pytest.approx(part(2, 2, 3))
    assert hits[1].score == pytest.approx(part(2, 1, 2))
    hits = index.search(["rent", "keys", "absent"])
    assert refs(hits) == [f"{DOC_B}.1", f"{DOC_A}.1"]
    assert hits[0].score == pytest.approx(part(2, 1, 2) + part(1, 1, 2))
    # A repeated term counts once, and only the first 16 different terms count at all.
    assert index.search(["rent", "rent", "rent"]) == index.search(["rent"])
    padding = [f"absent{number}" for number in range(16)]
    assert index.search([*padding, "rent"]) == []
    assert index.search([*padding[:15], "rent"]) == index.search(["rent"])


def test_two_passages_per_document_and_the_limit() -> None:
    assert PER_DOCUMENT == 2
    block = "alpha " * 99 + "omega\n\n"
    index = CollectionIndex()
    assert index.add(DOC_A, block * 5) == 5
    assert index.add(DOC_B, block * 3) == 3
    index.add(DOC_C, "alpha once")
    hits = index.search(["alpha"], 10)
    assert sorted(refs(hits)) == [
        f"{DOC_A}.1",
        f"{DOC_A}.2",
        f"{DOC_B}.1",
        f"{DOC_B}.2",
        f"{DOC_C}.1",
    ]
    assert len(index.search(["alpha"], 3)) == 3
    assert len(index.search(["alpha"], 10, per_document=1)) == 3
    assert len(index.search(["alpha"], 10, per_document=5)) == 9
    assert index.search(["alpha"], 10, per_document=0) == []
    assert index.search(["alpha"], -1) == []


def test_documents_passages_and_positions() -> None:
    text = ("word " * 150 + "\n\n") * 3
    index = CollectionIndex(listed=["Kowalski"])
    assert index.add(DOC_A, text) == 3
    assert index.add(DOC_B, "Mrs Kowalski") == 1
    assert index.add(DOC_C, "") == 0
    assert len(index) == 3
    assert index.doc_ids() == [DOC_A, DOC_B, DOC_C]
    assert DOC_A in index
    assert DOC_D not in index
    assert index.passage_count == 4
    assert index.passages(DOC_A) == 3
    assert index.passages(DOC_C) == 0
    assert index.chars(DOC_A) == len(text)
    assert [index.span(DOC_A, number) for number in (1, 2, 3)] == chunk_spans(text)
    assert index.span(DOC_B, 1) == (0, 12)
    assert index.listed(DOC_B) == ((4, 12),)
    hit = index.search(["word"], 1)[0]
    assert index.span(hit.doc_id, hit.passage) == hit.span
    for passage in (0, 4, -1):
        with pytest.raises(IndexError):
            index.span(DOC_A, passage)
    with pytest.raises(KeyError):
        index.span(DOC_D, 1)
    with pytest.raises(KeyError):
        index.passages(DOC_D)
    with pytest.raises(KeyError):
        index.listed(DOC_D)
    with pytest.raises(ValueError, match="already"):
        index.add(DOC_A, "again")
    assert index.passages(DOC_A) == 3  # the refused call changed nothing
    # An index with its own passage size (a store records the size it was made with).
    small = CollectionIndex(chunk_chars=100)
    assert small.add(DOC_A, text) == len(chunk_spans(text, 100))
    assert small.span(DOC_A, 1) == chunk_spans(text, 100)[0]


def test_remove_leaves_no_trace() -> None:
    queries = [["rent"], ["the"], ["rent", "deposit", "keys"], ["boiler", "the"], ["bank", "order"]]
    without = CollectionIndex(show_titles=True)
    without.add(DOC_A, LEASE, "lease")
    without.add(DOC_C, MANUAL, "manual")
    index = CollectionIndex(show_titles=True)
    index.add(DOC_A, LEASE, "lease")
    index.add(DOC_B, LETTER * 30, "bank letter")
    index.add(DOC_C, MANUAL, "manual")
    assert index.search(["bank"]) != []

    index.remove(DOC_B)
    assert DOC_B not in index
    assert index.doc_ids() == [DOC_A, DOC_C]
    assert index.terms() == without.terms()
    assert index.passage_count == without.passage_count
    for query in queries:
        assert index.search(query, 10) == without.search(query, 10)  # scores included
    assert index.search(["bank"]) == []
    assert index.search(["letter"]) == []
    with pytest.raises(KeyError):
        index.remove(DOC_B)
    with pytest.raises(KeyError):
        index.span(DOC_B, 1)

    # It can come back, and the first and the last document can go as well.
    index.add(DOC_B, LETTER, "bank letter")
    assert refs(index.search(["bank"])) == [f"{DOC_B}.1"]
    index.remove(DOC_A)
    index.remove(DOC_B)
    assert refs(index.search(["boiler"])) == [f"{DOC_C}.1"]
    index.remove(DOC_C)
    assert len(index) == 0
    assert index.passage_count == 0
    assert index.terms() == frozenset()
    assert index.search(["boiler"]) == []
