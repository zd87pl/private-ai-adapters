"""Helpers shared by the gate test modules: small stores, sample documents, record fields."""

from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from wdva.gate import store as gate_store
from wdva.gate.ledger import DocumentInfo, Record
from wdva.gate.store import Store, StoreLock

FAST_SCRYPT = 2**14
PASSPHRASE = "correct horse battery staple"

# Every sample document carries one word that occurs nowhere else, so a test can
# tell which document a piece of text came from, and whether any of it leaked.
LEASE_MARKER = "mk7f3a9c1e04"
BANK_MARKER = "mk52d0b8aa91"
TAX_MARKER = "mkc41e77f2b6"
MEDICAL_MARKER = "mk09ae6d3c58"

LISTED_NAME = "Jan Kowalski"
LISTED_NUMBER = "85010112345"

LEASE_TEXT = (
    "Residential lease agreement\n\n"
    f"This agreement ({LEASE_MARKER}) is made between the landlord, Northgate Housing "
    f"Cooperative, and the tenant, {LISTED_NAME}, personal number {LISTED_NUMBER}. It covers "
    "the flat on the third floor of Harbour Street 14, with one cellar compartment and one "
    "bicycle space in the yard.\n\n"
    "1. Term. The lease begins on 1 March 2025 and runs for an indefinite period. Either "
    "party may end it with three months of written notice, counted from the first day of the "
    "month after the notice was received. During the first twelve months the landlord may "
    "only end the lease for the reasons listed in section 9.\n\n"
    "2. Rent. The monthly rent is 1450 EUR, due on the third working day of each month. It "
    "includes cold water and the upkeep of the staircase. Heating, electricity and internet "
    "are paid by the tenant directly to the suppliers. The rent may be adjusted once a year, "
    "by at most the change of the consumer price index, with two months of notice.\n\n"
    "3. Deposit. The tenant pays a deposit of 2900 EUR before the keys are handed over. The "
    "landlord keeps it in a separate account and returns it, with the interest it earned, "
    "within six weeks after the flat was handed back, less the cost of damage beyond normal "
    "wear.\n\n"
    "4. Use. The flat is let as a home for at most three people. Subletting a room needs the "
    "written consent of the landlord, which is not withheld without a reason. Small pets are "
    "allowed; a dog needs consent. The tenant does not run a business with customers from "
    "the flat.\n\n"
    "5. Repairs. The landlord repairs the building, the heating, the pipes and the windows. "
    "The tenant reports damage without delay and pays for small repairs of up to 120 EUR "
    "each, at most 400 EUR in one year. Before moving out the tenant paints the walls only if "
    "they were left in a colour other than white.\n\n"
    "6. Access. The landlord may enter the flat for repairs and viewings after giving two "
    "days of notice, and at once in an emergency such as a burst pipe or a fire.\n\n"
    "7. House rules. Quiet hours are from ten in the evening until seven in the morning. "
    "Waste is sorted into the bins in the yard. The staircase is kept free of furniture, "
    "prams and shoes. Smoking is not allowed in the staircase or the cellar.\n\n"
    "8. Insurance. The tenant holds liability insurance for the whole term and shows the "
    "policy to the landlord when asked.\n\n"
    "9. Ending by the landlord. The landlord may end the lease early if the rent is unpaid "
    "for two months, if the flat is used against section 4 after a written warning, or if "
    "the building must be vacated by order of an authority.\n\n"
    "Signed in two copies on 14 February 2025.\n"
)

BANK_TEXT = (
    "Harbour Savings Bank\nCustomer letter\n\n"
    f"Dear {LISTED_NAME},\n\n"
    f"we write about your current account ({BANK_MARKER}). From 1 January 2026 the monthly "
    "account fee changes from 4.90 EUR to 5.90 EUR. Card payments in the euro area stay free "
    "of charge; a cash withdrawal at a machine of another bank costs 2.50 EUR.\n\n"
    "Your standing order for the rent, 1450 EUR on the third working day of each month, "
    "is not affected. The overdraft limit on your account stays at 1000 EUR, at an interest "
    "rate of 9.4 percent a year.\n\n"
    "If you do not agree with the new fee you may close the account free of charge before "
    "the change takes effect. You need not do anything if you agree.\n\n"
    "Kind regards,\nyour customer service\n"
)

TAX_TEXT = (
    "Tax note for 2025\n\n"
    f"Reference {TAX_MARKER}. Income from employment was 48200 EUR before tax; the employer "
    "withheld 9870 EUR of income tax. Work-related costs: 1230 EUR for commuting on 212 days, "
    "380 EUR for books and software, 96 EUR for the professional association.\n\n"
    "The home office was used on 88 days. Donations of 250 EUR went to the food bank and "
    "120 EUR to the sea rescue service; both receipts are in the folder.\n\n"
    "The return is due on 31 July 2026. Last year's refund was 412 EUR.\n"
)

MEDICAL_TEXT = (
    "Referral\n\n"
    f"Patient file {MEDICAL_MARKER}. The patient reports pain in the left knee after running, "
    "for about six weeks, worse on stairs. No swelling, no injury. Examination: full range of "
    "motion, tenderness at the lower edge of the kneecap.\n\n"
    "Referred to physiotherapy for six sessions. Review in eight weeks; an image of the knee "
    "only if the pain has not improved by then.\n"
)

SAMPLE_DOCUMENTS: tuple[tuple[str, str, str, str], ...] = (
    ("home", "lease-2025", "lease-2025.txt", LEASE_TEXT),
    ("home", "bank-letter", "bank-letter.txt", BANK_TEXT),
    ("tax", "tax-note-2025", "tax-note-2025.md", TAX_TEXT),
    ("medical", "referral-2026", "referral-2026.txt", MEDICAL_TEXT),
)
"""``(collection, title, file name, text)`` of the documents :func:`make_store` adds."""


def sample_policy() -> dict[str, Any]:
    """A policy as a JSON object: two clients, three collections, two listed strings.

    ``claude-desktop`` is hosted and may read ``home`` and ask for ``tax``;
    ``lm-studio`` is local and may read ``home`` and ask for ``medical``, which
    is not marked for hosted clients.
    """
    policy = gate_store.default_policy_json()
    policy["collections"] = {
        "home": {"note": "Lease, bank, insurance", "allow_hosted": True, "show_titles": False},
        "tax": {"note": "", "allow_hosted": True, "show_titles": True},
        "medical": {"note": "", "allow_hosted": False, "show_titles": False},
    }
    policy["clients"] = {
        "claude-desktop": {
            "destination": "Anthropic",
            "hosted": True,
            "grants": {"home": {"mode": "read"}, "tax": {"mode": "approve"}},
            "limits": {},
        },
        "lm-studio": {
            "destination": "local model on this computer",
            "hosted": False,
            "grants": {"home": {"mode": "read"}, "medical": {"mode": "approve"}},
            "limits": {},
        },
    }
    policy["redact_literals"] = [LISTED_NAME, LISTED_NUMBER]
    return policy


def prepare(text: str) -> tuple[str, list[tuple[int, int]], frozenset[str]]:
    """Run raw text through the text pipeline: the cleaned text, its passages, its terms."""
    from wdva.gate import textproc

    cleaned = textproc.sanitize(text)
    return cleaned, textproc.chunk_spans(cleaned), frozenset(textproc.tokens(cleaned))


def tokenize(text: str) -> Iterable[str]:
    """The search terms of a stored text, as the store's exact term count needs them."""
    from wdva.gate import textproc

    return textproc.tokens(text)


def plain_spans(text: str, size: int = 800) -> list[tuple[int, int]]:
    """Cut a text into passages of ``size`` characters, without looking for separators."""
    return [(start, min(start + size, len(text))) for start in range(0, len(text), size)]


def add_text(
    store: Store,
    collection: str,
    title: str,
    text: str,
    *,
    name: str | None = None,
    keep_original: bool = False,
    title_source: str = "owner",
    doc_id: str | None = None,
) -> DocumentInfo:
    """Add raw text to an open store the way the gate does: cleaned, cut, counted, sealed.

    With ``name`` the document says it came from a file of that name whose
    bytes are the text as UTF-8; ``keep_original`` seals those bytes too.
    """
    cleaned, spans, terms = prepare(text)
    raw = text.encode("utf-8")
    source: dict[str, Any] | None = None
    if name is not None:
        source = {"name": name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return store.add_document(
        collection=collection,
        title=title,
        text=cleaned,
        spans=spans,
        terms=terms,
        title_source=title_source,
        source=source,
        original=raw if keep_original else None,
        doc_id=doc_id,
        tokenize=tokenize,
    )


def make_store(
    path: str | os.PathLike[str],
    *,
    clock: Callable[[], datetime] | None = None,
    documents: Sequence[tuple[str, str, str, str]] = SAMPLE_DOCUMENTS,
    policy: bool = True,
    keep_originals: bool = True,
    passphrase: str = PASSPHRASE,
) -> Store:
    """Create a store with a cheap key slot and return it, open for writing.

    With ``policy`` the store gets :func:`sample_policy` (serial 2). Each of
    ``documents`` is added with its file name as source and, with
    ``keep_originals``, its original bytes. Pass ``documents=()`` for an empty
    store. Close the store, or use it as a context manager, when done.
    """
    store = gate_store.init_store(path, passphrase=passphrase, scrypt_n=FAST_SCRYPT, clock=clock)
    try:
        if policy:
            store.set_policy_json(sample_policy(), by="test")
        for collection, title, name, text in documents:
            add_text(
                store,
                collection,
                title,
                text,
                name=name,
                keep_original=keep_originals,
                title_source="file-name",
            )
    except BaseException:
        store.close()
        raise
    return store


def reopen(
    path: str | os.PathLike[str],
    *,
    clock: Callable[[], datetime] | None = None,
    write: bool = True,
    passphrase: str = PASSPHRASE,
    lock: StoreLock | None = None,
) -> Store:
    """Open a store made by :func:`make_store` again."""
    return gate_store.open_store(path, passphrase=passphrase, write=write, lock=lock, clock=clock)


def document_id(store: Store, title: str) -> str:
    """The id of the store's document with this title."""
    (info,) = [info for info in store.documents() if info.title == title]
    return info.doc_id


def release_fields(
    client: str,
    client_seq: int,
    text: str,
    *,
    pool: str = "hosted",
    tool: str = "wdva_read",
    spans: Iterable[Sequence[Any]] = (),
    **extra: Any,
) -> dict[str, Any]:
    """The fields of a ``release`` record for ``text``, with its length and hash filled in."""
    fields: dict[str, Any] = {
        "client": client,
        "client_seq": client_seq,
        "pool": pool,
        "tool": tool,
        "spans": [list(span) for span in spans],
        "text": text,
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    fields.update(extra)
    return fields


def sealed_files(path: str | os.PathLike[str]) -> list[Path]:
    """Every file of a store directory outside ``run/``, sorted."""
    root = Path(path)
    return sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.relative_to(root).parts[0] != "run"
    )


def record_files(path: str | os.PathLike[str]) -> list[Path]:
    """The record files of a store directory, oldest first."""
    return sorted((Path(path) / "ledger").glob("[0-9]*.wdva"))


def snapshot(path: str | os.PathLike[str]) -> dict[str, bytes]:
    """Name and content of every file of a store directory outside ``run/``."""
    root = Path(path)
    return {str(item.relative_to(root)): item.read_bytes() for item in sealed_files(root)}


def forms_of(marker: str) -> list[bytes]:
    """The byte strings under which ``marker`` would show in a file: plain, UTF-16, base64."""
    raw = marker.encode("utf-8")
    if len(raw) < 12:
        raise ValueError("a marker of fewer than 12 bytes would be found by chance")
    forms = [raw, marker.encode("utf-16-le"), raw.hex().encode("ascii")]
    for shift in range(3):
        # A base64 encoding depends on where in the stream the text starts.
        # The characters that depend only on the marker itself are those in
        # the middle, after the alignment bytes and before the last group.
        encoded = base64.b64encode(b"\x00" * shift + raw)
        skip = (shift * 8 + 5) // 6
        forms.append(encoded[skip : len(encoded) - 4])
    return forms


def leaked(blob: bytes, markers: Iterable[str]) -> list[str]:
    """Those of ``markers`` that can be found in ``blob`` in one of their forms."""
    return [marker for marker in markers if any(form in blob for form in forms_of(marker))]


CLAIMED = {"name": "claude-ai", "version": "1.2", "protocol": "2025-11-25"}


def record_session(
    store: Store,
    *,
    client: str = "claude-desktop",
    pool: str = "hosted",
    advance: Callable[[], Any] | None = None,
) -> list[Record]:
    """Append what one short session leaves in the record, and return the records.

    An unlock, a search and a read of the first document, a refusal, a status
    call, an approval and a lock: seven records with the fields the gate gives
    them. ``advance`` is called before each record, to move a test clock on.
    """
    ledger = store.ledger
    state = ledger.replay()
    document = next(iter(state.documents.values()))
    ref = f"{document.doc_id}.1"
    counter = state.client(client).release_seq
    passage = min(document.chars, 800)
    found = f"OK: 1 result for the search.\n1. ref={ref} [{document.collection}]\n   | the rent"
    body = f"OK: passage 1 of {document.passages}.\n   | first passage\nRelease {client}."
    caller = {"claimed": dict(CLAIMED), "transport": "stdio"}
    session: list[tuple[str, dict[str, Any]]] = [
        (
            "unlock",
            {
                "served": [client],
                "mixed": False,
                "previous_session": state.previous_session,
                "auto_lock": None,
            },
        ),
        (
            "release",
            release_fields(
                client,
                counter + 1,
                found,
                pool=pool,
                tool="wdva_search",
                spans=[(document.doc_id, 10, min(170, document.chars))],
                rule="grant:search",
                query="rent deposit",
                args={"query": "rent deposit", "collection": "", "limit": 5},
                collections=[document.collection],
                **caller,
            ),
        ),
        (
            "release",
            release_fields(
                client,
                counter + 2,
                body,
                pool=pool,
                tool="wdva_read",
                spans=[(document.doc_id, 0, passage)],
                rule="grant:read",
                args={"ref": ref},
                collections=[document.collection],
                **caller,
            ),
        ),
        (
            "denial",
            {
                "client": client,
                "code": "NEEDS_APPROVAL",
                "variant": "",
                "tool": "wdva_read",
                "args": {"ref": f"{document.doc_id}.2"},
                "folded_before": 0,
                **caller,
            },
        ),
        ("status", {"client": client, **caller}),
        (
            "approval",
            {
                "event": "approved",
                "client": client,
                "ref": f"{document.doc_id}.2",
                "body_sha256": hashlib.sha256(b"second passage").hexdigest(),
                "chars": 14,
                "valid_until": "2026-11-12T09:15:00Z",
            },
        ),
        ("lock", {"reason": "close", "folded_refusals": 0}),
    ]
    records: list[Record] = []
    for kind, fields in session:
        if advance is not None:
            advance()
        records.append(ledger.append(kind, fields))
    return records
