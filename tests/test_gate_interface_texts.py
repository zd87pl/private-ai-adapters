"""The two text tables of the owner's interface, read as text.

Every text the simple interface shows lives in one table per language
(``text-en.js``, ``text-pl.js``). These tests hold the tables to their rules
without a browser: the same keys in both, the plural forms of each language, the
same places for values, a budget of two sentences of explanation per screen, the
sentences that must stand word for word, the Polish glossary, and no claim about
another program that nobody checked.

The tables grow stage by stage. A key of the specification that no stage has
built yet is named in ``NOT_BUILT_YET``; the tests fail when such a key appears
while it is still listed there, so the list shrinks as the page grows.
"""

from __future__ import annotations

import json
import re

from .test_gate_interface_files import FORBIDDEN_CLAIMS, STATIC, scripts, without_comments

TABLES = {"en": "text-en.js", "pl": "text-pl.js"}

# A key: lower-case words joined by dots; the last word may be a code of the app in capitals.
KEY = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*(?:\.[A-Z_]+)?")

# The plural categories of each language, as ``Intl.PluralRules`` names them.
FORMS = {"en": ("one", "other"), "pl": ("one", "few", "many", "other")}

# A place for a value: "{" and lower-case letters and "}". "${" is no place.
PLACE = re.compile(r"(?<!\$)\{([a-z]+)\}")

Value = str | dict[str, str]


def read_table(language: str) -> tuple[dict[str, Value], list[str]]:
    """One table as its keys and values, and every key in the order it is written (twice if twice).

    The form is fixed so that it can be read line by line: ``"key": "text",`` for one
    text, and for plural forms ``"key": {`` with a line ``form: "text",`` for each and
    a closing ``},``. Comments and empty lines are allowed between entries.
    """
    source = (STATIC / TABLES[language]).read_text(encoding="utf-8")
    head = "export const TEXT = Object.freeze({\n"
    assert source.count(head) == 1, f"{TABLES[language]} does not hold one table"
    body = source[source.index(head) + len(head) :]
    assert body.endswith("});\n"), f"{TABLES[language]} does not end with its table"
    table: dict[str, Value] = {}
    written: list[str] = []
    forms: dict[str, str] | None = None
    for line in body[: -len("});\n")].splitlines():
        if not line.strip() or line.startswith("  // "):
            continue
        if forms is not None:
            if line == "  },":
                forms = None
                continue
            form = re.fullmatch(r'    ([a-z]+): ("(?:[^"\\]|\\.)*"),', line)
            assert form is not None, f"{TABLES[language]}: not a plural form: {line!r}"
            assert form.group(1) not in forms, f"{TABLES[language]}: a form twice: {line!r}"
            forms[form.group(1)] = json.loads(form.group(2))
            continue
        entry = re.fullmatch(r'  "([^"]+)": (?:("(?:[^"\\]|\\.)*"),|(\{))', line)
        assert entry is not None, f"{TABLES[language]}: not an entry: {line!r}"
        key = entry.group(1)
        written.append(key)
        if entry.group(3):
            forms = {}
            table[key] = forms
        else:
            table[key] = json.loads(entry.group(2))
    assert forms is None, f"{TABLES[language]}: plural forms that do not close"
    return table, written


def tables() -> dict[str, dict[str, Value]]:
    return {language: read_table(language)[0] for language in TABLES}


def texts(value: Value) -> list[str]:
    """Every text of a value: the one text, or each of its plural forms."""
    return [value] if isinstance(value, str) else list(value.values())


def kind(key: str) -> str | None:
    """What kind of text a key names, by the first of its words that says so."""
    for word in key.split(".")[1:]:
        if word in ("say", "status", "result", "error", "title", "label", "button"):
            return word
        if word in ("placeholder", "chip"):
            return word
    return None


def words(text: str) -> int:
    return len(text.split())


def sentences(text: str) -> int:
    """How many sentences a text holds: its parts between a full stop, "?" or "!" and a space."""
    return len([part for part in re.split(r"(?<=[.?!])\s+", text.strip()) if part])


# -- the tables themselves ----------------------------------------------------------


def test_both_tables_have_the_same_keys() -> None:
    read = {language: read_table(language) for language in TABLES}
    for language, (table, written) in read.items():
        assert len(written) == len(set(written)), (
            f"{TABLES[language]} has a key twice: "
            f"{sorted(key for key in set(written) if written.count(key) > 1)}"
        )
        for key in table:
            assert KEY.fullmatch(key), f"{TABLES[language]}: {key!r} is not a key"
            assert "." in key, f"{TABLES[language]}: {key!r} names no part of the page"
    english, polish = read["en"][0], read["pl"][0]
    assert set(english) - set(polish) == set(), "the Polish table lacks keys"
    assert set(polish) - set(english) == set(), "the English table lacks keys"
    # The same order in both, so that the two files can be read side by side.
    assert read["en"][1] == read["pl"][1]


def test_a_table_is_one_frozen_object_and_imports_nothing() -> None:
    for name in TABLES.values():
        code = without_comments((STATIC / name).read_text(encoding="utf-8"))
        assert "import" not in code, f"{name} imports something"
        assert code.count("export ") == 1, f"{name} exports more than its table"
        assert "export const TEXT = Object.freeze({" in code
        assert "`" not in code, f"{name} holds a text that is worked out"


def test_a_text_is_plain_text_with_the_quotation_marks_of_its_language() -> None:
    for language, table in tables().items():
        for key, value in table.items():
            for text in texts(value):
                where = f"{TABLES[language]} {key}"
                assert text == text.strip(), f"{where} begins or ends with a space"
                assert text, f"{where} is empty"
                assert "\n" not in text, where
                assert "  " not in text, f"{where} holds two spaces"
                # No markup: a link or a button is an element of its own, after the sentence.
                assert "<" not in text, f"{where} holds markup"
                assert ">" not in text, f"{where} holds markup"
                # Curly marks, those of the language: English opens with U+201C, Polish with U+201E.
                assert '"' not in text, f"{where} holds a straight quotation mark"
                assert "'" not in text, f"{where} holds a straight apostrophe"
                foreign = "„" if language == "en" else "“"
                assert foreign not in text, f"{where} opens a quotation in the other language's way"
                opened = text.count("“" if language == "en" else "„")
                assert opened == text.count("”"), f"{where}: a quotation does not close"


# What the scripts ask for. A quoted text in a script that begins like a key of the tables is
# a key: `t("pair.title")`, a key in a list or a map, a key chosen by a condition.
FIRST_WORDS = [
    "activity",
    "ask",
    "bar",
    "card",
    "common",
    "connect",
    "create",
    "custom",
    "demo",
    "disconnect",
    "docs",
    "group",
    "guide",
    "home",
    "how",
    "locked",
    "nav",
    "notice",
    "offline",
    "pair",
    "panel",
    "pro",
    "reauth",
    "remove",
    "row",
    "settings",
    "stuck",
    "tile",
]

# Keys a script builds from a beginning and a word of the app. Such a family counts as asked
# for only while a script holds that beginning in a text it completes (`prefix${...}`).
FAMILIES = (
    "row.stop.",
    "notice.",
    "connect.refused.say.1.",
    "connect.offer.say.",
    "connect.hand.status.",
    "connect.label.kind.",
    "disconnect.",
    "docs.error.",
    "ask.say.1.",
    "guide.",
)


def keys_asked_for() -> dict[str, set[str]]:
    """Every key a script names, with the scripts that name it."""
    asked: dict[str, set[str]] = {}
    for name, source in scripts().items():
        if name in TABLES.values():
            continue
        for text in re.findall(r'"((?:[^"\\\n]|\\.)*)"', without_comments(source)):
            if KEY.fullmatch(text) and "." in text and text.split(".")[0] in FIRST_WORDS:
                asked.setdefault(text, set()).add(name)
    return asked


def test_every_key_is_used_and_every_use_has_a_key() -> None:
    english = tables()["en"]
    assert {key.split(".")[0] for key in english} <= set(FIRST_WORDS), (
        "a key begins with a word this test does not look for in the scripts"
    )
    asked = keys_asked_for()
    for key, names in sorted(asked.items()):
        assert key in english, f"{sorted(names)} ask for {key!r}, which the tables lack"
    code = "\n".join(
        without_comments(source)
        for name, source in scripts().items()
        if name not in TABLES.values()
    )
    built = [family for family in FAMILIES if f"`{family}${{" in code]
    for key in english:
        assert key in asked or any(key.startswith(family) for family in built), (
            f"no script asks for {key!r}"
        )
    # A text is asked for through the one function, with its key written out or chosen from
    # keys that are written out; and what is handed to it is never put together from texts.
    for found in re.finditer(r"\bt\(\s*([\"`])([^\"`]*)\1", code):
        quote, text = found.groups()
        if quote == '"':
            assert text in english, f"t({text!r}) names no key"
        else:
            assert any(text.startswith(f"{family}${{") for family in FAMILIES), (
                f"t(`{text}`) builds a key of no listed family"
            )


def test_the_key_reader_tells_a_key_from_other_text() -> None:
    sample = (
        't("pair.title"); t(open ? "create.button.hide" : "create.button.show"); '
        'el("img", { src: "favicon.svg" }); const own = { wrong_code: "pair.error.wrong" }; '
        'toast("Note saved."); select(".page-body > .approval"); t(`docs.error.${reason}`);'
    )
    found = {
        text
        for text in re.findall(r'"((?:[^"\\\n]|\\.)*)"', sample)
        if KEY.fullmatch(text) and "." in text and text.split(".")[0] in FIRST_WORDS
    }
    assert found == {"pair.title", "create.button.hide", "create.button.show", "pair.error.wrong"}


def test_plural_forms_are_those_of_the_language() -> None:
    read = tables()
    for language, table in read.items():
        for key, value in table.items():
            if isinstance(value, str):
                continue
            assert tuple(value) == FORMS[language], (
                f"{TABLES[language]} {key} has the forms {tuple(value)}"
            )
            for form, text in value.items():
                assert "{n}" in text, f"{TABLES[language]} {key}.{form} does not name its number"
    plural = {
        language: {k for k, v in table.items() if not isinstance(v, str)}
        for language, table in read.items()
    }
    assert plural["en"] == plural["pl"], "a key has plural forms in one table only"
    # The page picks the form by the rule of the language, and by nothing of its own.
    reader = without_comments(scripts()["texts.js"])
    assert "new Intl.PluralRules(code)" in reader
    assert "found[plural(shown, values.n)] || found.other" in reader


def test_the_places_are_the_same_in_both_languages() -> None:
    read = tables()
    for key, value in read["en"].items():
        wanted = set(PLACE.findall(texts(value)[0]))
        for language, table in read.items():
            for text in texts(table[key]):
                assert set(PLACE.findall(text)) == wanted, (
                    f"{TABLES[language]} {key} has other places than {sorted(wanted)}: {text!r}"
                )
                # Nothing else stands in braces: a place the page could not fill.
                leftover = PLACE.sub("", text)
                assert "{" not in leftover.replace("${", ""), f"{TABLES[language]} {key}"


# -- the budget ---------------------------------------------------------------------

# The units of the simple interface: one thing a person sees at one time, with every beginning
# of a key its sentences may come from. A page cannot gain a third sentence under another name.
UNITS: dict[str, tuple[str, ...]] = {
    "pairing": ("pair",),
    "a page without a session": ("stuck",),
    "the passphrase": ("create",),
    "Home, the page": ("home.new", "home.connect"),
    "the approval card": ("card",),
    "the unfolded details of a card": ("card.details",),
    "the trial card": ("ask",),
    "Documents, the page": ("docs",),
    'the sheet "Remove ...?"': ("remove",),
    "Activity, the page": ("activity",),
    "the details panel": ("panel",),
    "the connect sheet, offer": ("connect.offer",),
    "the connect sheet, done": ("connect.done",),
    "the connect sheet, on": ("connect.on",),
    "the connect sheet, as": ("connect.as",),
    "the connect sheet, no folder": ("connect.nofolder",),
    "the connect sheet, refused": ("connect.refused",),
    "the connect sheet, expands": ("connect.expands",),
    "the connect sheet, by hand": ("connect.hand",),
    'the sheet "Disconnect ...?"': ("disconnect",),
    'the sheet "Other app"': ("custom",),
    "Settings": ("settings",),
    "the passphrase sheet": ("reauth",),
    "the demo's note": ("demo",),
    "Locked": ("locked",),
    "not answering, the screen": ("offline",),
    "not answering, the bar": ("bar",),
}

# The one third sentence of the simple interface, named and not hidden (section 1.2).
THIRD_SENTENCE = "card.say.3.nofit"

# Status texts that say where a pasted text goes: the named exception to "what is, never
# what to do".
STATUS_MAY_INSTRUCT = "connect.hand.status."

NOT_A_STATUS = re.compile(
    r"\b(because|so that|press|choose|quit|bo|więc|naciśnij|wybierz|zamknij)\b", re.I
)

# Keys that are a label in all but their name: the two languages of the switch.
LABEL_LIKE = {"common.lang.en", "common.lang.pl"}

# A line the owner sends on to an AI app: one sentence.
SENT_ON = {"ask.sample.question", "connect.ask.start"}

# How many words the English table may hold without the page "How it works" (section 1.2).
WORDS_AT_MOST = 2720


def unit_of(stem: str) -> list[str]:
    """The units a sentence belongs to, by the part of its key before ``.say.``."""
    found = [unit for unit, stems in UNITS.items() if stem in stems]
    if found:
        return found
    if re.fullmatch(r"notice\.[a-z0-9_]+", stem):
        return [f"the notice {stem}"]
    if re.fullmatch(r"guide\.[a-z]+", stem):
        return [f"the demo's card, {stem}"]
    return []


def test_the_text_budget() -> None:
    read = tables()
    english = read["en"]
    said: dict[str, set[tuple[str, int]]] = {}
    for key in english:
        if key.startswith("how.") or ".say." not in key:
            continue
        stem, _, rest = key.partition(".say.")
        number = rest.split(".")[0]
        assert number.isdigit(), f"{key} does not number its sentence"
        # The second sentence of the demo's card is one for every moment of it.
        units = unit_of(stem) if stem != "guide" else ["the demo's card, its second sentence"]
        assert units, f"{key} is a sentence of no unit this test knows"
        if key != THIRD_SENTENCE:
            assert int(number) in (1, 2), f"{key} is a third sentence"
        for unit in units:
            said.setdefault(unit, set()).add((stem, int(number)))
    for unit, held in said.items():
        allowed = 3 if unit == "the approval card" else 2
        if unit.startswith("the demo's card, guide."):
            allowed = 1  # with the card's second sentence: two
        assert len(held) <= allowed, f"{unit} has {len(held)} sentences: {sorted(held)}"
    for language, table in read.items():
        for key, value in table.items():
            if key.startswith("how."):
                continue  # the one page without a budget
            what = kind(key)
            where = f"{TABLES[language]} {key}"
            for text in texts(value):
                if key.split(".")[0] in ("row", "tile", "nav"):
                    assert words(text) <= 12, f"{where} has {words(text)} words"
                    assert ". " not in text, f"{where} is more than one line"
                if what == "say":
                    assert text[-1] in ".?:", f"{where} does not end as a sentence"
                    assert ". " not in text, f"{where} is two sentences"
                    assert ";" not in text, f"{where} joins two statements with a semicolon"
                    assert words(text) <= 20, f"{where} has {words(text)} words"
                elif (
                    what in ("title", "label", "button", "placeholder", "chip") or key in LABEL_LIKE
                ):
                    assert not text.endswith("."), f"{where} ends with a full stop"
                    assert words(text) <= 8, f"{where} has {words(text)} words"
                elif what == "status":
                    assert sentences(text) <= 1, f"{where} is more than one sentence"
                    if not key.startswith(STATUS_MAY_INSTRUCT):
                        found = NOT_A_STATUS.search(text)
                        assert found is None, f"{where} explains or instructs: {found.group(0)!r}"
                elif what in ("result", "error"):
                    assert sentences(text) <= 2, f"{where} is more than two sentences"
                elif key in SENT_ON:
                    assert sentences(text) <= 1, f"{where} is more than one sentence"
                else:
                    assert key.split(".")[0] in ("row", "tile", "nav"), (
                        f"{where} is of no kind the budget knows"
                    )
    counted = sum(
        words(value if isinstance(value, str) else value["other"])
        for key, value in english.items()
        if not key.startswith("how.")
    )
    assert counted <= WORDS_AT_MOST, f"the English table holds {counted} words"


def test_the_budget_rules_do_catch_what_they_name() -> None:
    assert kind("card.details.say.1.each") == "say"
    assert kind("home.new.card1.button.sample") == "button"
    assert kind("card.status.asked.times") == "status"
    assert kind("common.lang.en") is None
    assert sentences("Released once for a, until 18:00. Tell it: “try again”.") == 2
    assert sentences("Sealing…") == 1
    assert sentences("It was not released; nothing left.") == 1
    assert NOT_A_STATUS.search("It waits because this app asks.")
    assert NOT_A_STATUS.search("Czeka, bo ta aplikacja pyta.")
    assert NOT_A_STATUS.search("Zamknij aplikację.")
    assert NOT_A_STATUS.search("Nobody can reset it.") is None
    assert unit_of("card") == ["the approval card"]
    assert unit_of("notice.no_sdk") == ["the notice notice.no_sdk"]
    assert unit_of("somewhere.else") == []


# -- what must stand word for word --------------------------------------------------

# The key and its English text, exactly as the specification has it. For the Polish table
# the test pins that each key is there and is not the English text: the Polish texts join
# this list word for word once a person who writes Polish for a living has read the table.
REQUIRED_TEXTS = {
    "create.say.1": "It locks your documents on this computer.",
    "create.say.2": "Nobody can reset it: a lost passphrase is a lost vault.",
    "card.say.1": "Once released, this text is sent to {destination} and cannot be recalled.",
    "card.say.2": ("The name “{app}” is a label: {product} cannot check which program is asking."),
    "card.result.yes": "Released once for {app}, until {time}. Tell it: “try again”.",
    "ask.say.1": "An AI app would get only this passage, and only if you say yes.",
    "ask.say.1.connect": (
        "An app you connect on Home would get only this passage, and only after your yes."
    ),
    "ask.say.1.read": "{apps} can read this passage without asking you, inside the limits you set.",
    "ask.say.1.none": "No app can ask for this document yet.",
    "ask.say.2": "This is a trial on this computer: nothing is sent and nothing is recorded.",
    "home.new.say.1": (
        "An app you connect here gets one short passage of a document at a time, "
        "only after your yes."
    ),
    "docs.say.1": (
        "A sealed copy goes into the vault, and the file you add stays where it was, not encrypted."
    ),
    "docs.say.2": (
        "Text that {app} receives from your documents is sent to {destination} "
        "and cannot be recalled."
    ),
    "docs.say.2.general": (
        "Text that an app receives from your documents is sent to the company behind the app "
        "and cannot be recalled."
    ),
    "remove.say.2": "Its files are deleted, not shredded, and backups keep what they have.",
    "panel.say.1": "This text is with {destination} now.",
    "panel.say.2": "{product} cannot take it back.",
    "connect.offer.say.2": (
        "Each passage waits for your yes, and what you release is sent to {destination} "
        "and cannot be recalled."
    ),
    "connect.offer.say.2.read": (
        "{label} can read without asking you, and what it gets is sent to {destination} "
        "and cannot be recalled."
    ),
    "connect.offer.say.2.groups": (
        "In Settings you choose what {label} may ask for, and what it gets goes to {destination} "
        "and cannot be recalled."
    ),
    "connect.done.say.1": "Quit {label} and open it again.",
    "connect.done.status.tried": "Written and read back; not tried with {label} itself.",
    "connect.hand.say.1": "Put this text into {path} yourself: it holds no passphrase and no key.",
    "custom.say.1": (
        "You paste a short text into your app\u2019s MCP settings yourself, "
        "and {product} writes nothing there."
    ),
    "custom.say.2": (
        "Text that {name} receives from your documents is sent to {destination} "
        "and cannot be recalled."
    ),
    "pair.say.2": "Anyone who can read that terminal can pair.",
    "home.connect.say.1": (
        "Only apps installed on this computer can be connected, "
        "not websites, phone apps or Grok Bot."
    ),
    "settings.say.1": "These six pages have every setting, and they are in English for now.",
    "how.connect.6": (
        "Where each app keeps its settings is taken from its maker\u2019s own pages; "
        "none of this was tried with the apps themselves."
    ),
}

# Keys the tests name that belong to a part of the page no stage has built yet. Each stage
# takes out what it builds; at the end of the work this set is empty.
NOT_BUILT_YET = {
    "ask.say.1",
    "ask.say.1.connect",
    "ask.say.1.read",
    "ask.say.1.none",
    "ask.say.2",
    "home.new.say.1",
    "home.wait.title",
    "docs.say.2",
    "docs.say.2.general",
    "connect.offer.say.2",
    "connect.offer.say.2.read",
    "connect.offer.say.2.groups",
    "connect.done.say.1",
    "connect.done.status.tried",
    "connect.hand.say.1",
    "custom.say.1",
    "custom.say.2",
    "home.connect.say.1",
    "settings.say.1",
    "how.connect.6",
}


def built(key: str, table: dict[str, Value]) -> bool:
    """Whether a key the tests name is in the tables: it is, unless it is listed as not built."""
    if key in NOT_BUILT_YET:
        assert key not in table, f"{key} is in the tables: take it out of NOT_BUILT_YET"
        return False
    assert key in table, f"the tables lack {key}"
    return True


def test_the_required_texts_are_there_word_for_word() -> None:
    read = tables()
    assert NOT_BUILT_YET - set(REQUIRED_TEXTS) == {"home.wait.title"}
    for key, text in REQUIRED_TEXTS.items():
        if not built(key, read["en"]):
            continue
        assert read["en"][key] == text, key
        assert isinstance(read["pl"][key], str), key
        assert read["pl"][key] != text, f"{key} is not translated"
    # Where the decision is made: the caller's own name for itself is said to be unverified,
    # and a release names the answer the owner then gives the app.
    assert "unverified" in str(read["en"]["card.status.claimed"])
    assert "niesprawdzone" in str(read["pl"]["card.status.claimed"])
    assert read["en"]["card.button.no"] == "Don\u2019t release"
    assert read["en"]["card.button.yes"] == "Release once"
    # When the owner closes the sheet that asks for the passphrase, nothing was changed.
    assert read["en"]["common.error.cancelled"] == "Nothing was changed."
    assert read["en"]["remove.say.1"].startswith("Apps can no longer search or read it")
    # A screen for an app that stopped promises nothing about itself: a new start has a new
    # address unless a port was named, and this page would ask at the old one for ever.
    assert read["en"]["offline.say.1"] == (
        "Start {product} again in a terminal, then open the address it prints."
    )
    for language, table in read.items():
        for key, value in table.items():
            for text in texts(value):
                for promise in ("by itself", "continues", "sama wróci", "samoczynnie"):
                    if key.startswith(("offline.", "bar.")):
                        assert promise not in text, f"{TABLES[language]} {key} promises: {promise}"


# -- the Polish ---------------------------------------------------------------------

# Every form of the informal "you", which the page writes with a capital. ("Ci" is not here:
# in lower case it is also the ordinary word for "those".)
YOU_FORMS = [
    "ty",
    "ciebie",
    "cię",
    "tobie",
    "tobą",
    "twój",
    "twoja",
    "twoje",
    "twojego",
    "twojej",
    "twojemu",
    "twoim",
    "twoją",
    "twoich",
    "twoimi",
]


def test_the_glossary_holds() -> None:
    polish = tables()["pl"]
    everything = "\n".join(text for value in polish.values() for text in texts(value))
    lower = everything.lower()
    assert "sejf" in lower
    assert "skarbiec" not in lower
    assert "hasło" in lower
    assert "fraza" not in lower
    assert "frazę" not in lower
    assert "fragment" in lower
    assert "rejestr" in lower
    for key, value in polish.items():
        if key != "how.cannot.7":
            for text in texts(value):
                assert "dziennik" not in text.lower(), f"{key} calls the record a log"
    pinned = {
        "card.button.yes": "Udostępnij raz",
        "card.button.no": "Nie udostępniaj",
        "home.wait.title": "Czeka na Ciebie",
    }
    for key, text in pinned.items():
        if built(key, polish):
            assert polish[key] == text, key
    # The informal "you" is written with a capital wherever it stands.
    for key, value in polish.items():
        for text in texts(value):
            for word in re.findall(r"[^\W\d_]+", text):
                assert word not in YOU_FORMS, f"{key} writes {word!r} without its capital"
    # A tile is named Claude or Grok, and the demo plays an app: no verb about either may take
    # a gender, unless the word "aplikacja" stands before it.
    for key, value in polish.items():
        if not key.startswith(("tile.status.", "guide.")):
            continue
        for text in texts(value):
            for found in re.finditer(r"[^\W\d_]+(?:ła|łaby)\b", text):
                assert "aplikacja" in text[: found.start()].lower(), (
                    f"{key} gives a name a gender: {found.group(0)!r}"
                )


def test_the_products_name_needs_no_ending() -> None:
    # A name that is put into a Polish sentence is never bent. So the product's name never
    # stands directly after a preposition that would need an ending on it: a noun carries the
    # ending ("do programu {product}").
    after = re.compile(r"(?<![^\W\d_])(z|ze|do|od|bez|dla|u|przez)\s+\{product\}", re.I)
    for key, value in tables()["pl"].items():
        for text in texts(value):
            found = after.search(text)
            assert found is None, f"{key}: {found.group(0) if found else ''!r}"
    assert after.search("prośby trafiają do {product}.")
    assert after.search("Z {product} nic nie wychodzi.")
    assert after.search("trafiają do programu {product}.") is None
    assert after.search("w którym działa {product}.") is None


# -- what is said about other programs, and what is claimed ------------------------

# The keys whose text says what another program does or reads. Nobody tried this app with any
# of those programs, so this is the whole list, and each of them is a sentence somebody read.
ABOUT_OTHER_PROGRAMS = {
    "connect.offer.say.1.add",
    "connect.offer.say.1.new",
    "connect.offer.say.1.replace",
    "connect.offer.say.1.gone",
    "connect.done.say.1",
    "connect.done.say.1.session",
    "connect.done.say.2",
    "connect.on.say.1",
    "connect.on.say.1.session",
    "connect.on.status.off",
    "connect.as.say.1",
    "connect.nofolder.say.2",
    "connect.expands.say.1",
    "connect.refused.say.1.moved",
    "home.connect.say.1",
    "how.connect.4",
    "how.connect.6",
    "how.connect.7",
    "how.connect.8",
    "how.connect.11",
}

# Where the name of another company's product may stand besides: on a tile, and in two texts.
MAY_NAME_A_PRODUCT = {"how.connect.1", "custom.label.claudecode"}

OTHER_PRODUCTS = re.compile(r"\b(Claude|ChatGPT|Grok|Codex)\b")

# What no text may say of another program, in either language.
NOBODY_CHECKED = (
    "is connected",
    "works with",
    "tested with",
    "jest połączona",
    "jest połączony",
    "współpracuje z",
    "przetestowan",
)


def test_what_is_said_about_other_programs() -> None:
    read = tables()
    for language, table in read.items():
        for key, value in table.items():
            for text in texts(value):
                lower = text.lower()
                for phrase in NOBODY_CHECKED:
                    assert phrase not in lower, f"{TABLES[language]} {key} says {phrase!r}"
                if key in ABOUT_OTHER_PROGRAMS | MAY_NAME_A_PRODUCT or key.startswith("tile."):
                    continue
                found = OTHER_PRODUCTS.search(text)
                assert found is None, (
                    f"{TABLES[language]} {key} names {found.group(0) if found else ''}"
                )
    # The two texts that say nothing was tried with the apps are in both tables.
    for key in ("how.connect.6", "connect.done.status.tried"):
        if built(key, read["en"]):
            assert key in read["pl"]


# Claims the product cannot back, in Polish.
FORBIDDEN_CLAIMS_POLISH = [
    r"gwarant",
    r"100\s*%",
    r"w pełni bezpieczn",
    r"nie do złamania",
    r"anonimow",
    r"zgodn\w* z RODO",
    r"nigdy nie opuszcza",
]


def test_no_claim_in_either_language() -> None:
    read = tables()
    for language, claims in (("en", FORBIDDEN_CLAIMS), ("pl", FORBIDDEN_CLAIMS_POLISH)):
        for key, value in read[language].items():
            for text in texts(value):
                for claim in claims:
                    found = re.search(claim, text, re.I)
                    assert found is None, (
                        f"{TABLES[language]} {key} says {found.group(0) if found else ''!r}"
                    )
    assert re.search(FORBIDDEN_CLAIMS_POLISH[5], "Zgodny z RODO.", re.I)
    assert re.search(FORBIDDEN_CLAIMS_POLISH[6], "Tekst nigdy nie opuszcza komputera.", re.I)
