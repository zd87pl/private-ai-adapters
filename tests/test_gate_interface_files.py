"""The files of the owner's interface, read as text: what they load, what they call, what they say.

The page is served under a policy that allows scripts and styles from the app
itself and nothing else. These tests hold the files to that policy without a
browser, and to three further rules: text is never parsed as markup, every
address the scripts call is one the app defines, and the sentences the owner
reads claim nothing the gate cannot do.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import wdva.gate.app
from wdva.gate import brand, core, textproc
from wdva.gate.app import demo

STATIC = Path(wdva.gate.app.__file__).resolve().parent / "static"

PAGE_SUFFIXES = {".html", ".css", ".js", ".svg"}

# The one address-like text that may appear: the name of the SVG vocabulary. It is
# an identifier that browsers compare, not a place they fetch from.
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

# Every call the interface may make: method and path, with "<>" for a path parameter.
API_CALLS = {
    ("GET", "/api/session"),
    ("POST", "/api/session/pair"),
    ("GET", "/api/state"),
    ("POST", "/api/store/create"),
    ("POST", "/api/unlock"),
    ("POST", "/api/lock"),
    ("POST", "/api/reauth"),
    ("POST", "/api/serving"),
    ("POST", "/api/verify"),
    ("POST", "/api/interface"),
    ("GET", "/api/activity"),
    ("GET", "/api/activity/<>"),
    ("GET", "/api/approvals"),
    ("POST", "/api/approvals/release"),
    ("POST", "/api/approvals/dismiss"),
    ("GET", "/api/documents"),
    ("POST", "/api/documents"),
    ("DELETE", "/api/documents/<>"),
    ("POST", "/api/collections"),
    ("DELETE", "/api/collections/<>"),
    ("GET", "/api/apps"),
    ("POST", "/api/apps"),
    ("DELETE", "/api/apps/<>"),
    ("POST", "/api/grants"),
    ("POST", "/api/apps/<>/limits"),
    ("GET", "/api/apps/<>/connect"),
    ("POST", "/api/apps/<>/connect/check"),
    ("GET", "/api/settings"),
    ("POST", "/api/settings"),
    ("POST", "/api/replaced-strings"),
    ("POST", "/api/demo/again"),
}

# Ways a script could turn text into markup or into code.
MARKUP_AND_CODE_SINKS = {
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
    "document.write": r"\bdocument\s*\.\s*write(?:ln)?\b",
    "eval": r"\beval\s*\(",
    "new Function": r"\bnew\s+Function\b",
    "Function()": r"(?<![.\w])Function\s*\(",
    "a timer given text": r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]",
    "DOMParser": r"\bDOMParser\b",
    "createContextualFragment": r"\bcreateContextualFragment\b",
    "srcdoc": r"\bsrcdoc\b",
    "a script element": r"createElement(?:NS)?\s*\([^)]*[\"'`]script[\"'`]",
    "a style attribute": r"setAttribute\s*\(\s*[\"'`]style[\"'`]",
    "cssText": r"\bcssText\b",
    "a javascript: address": r"javascript\s*:",
    "import of a computed address": r"\bimport\s*\(",
}

# Sentences the interface must show where the owner decides, word for word.
REQUIRED_SENTENCES = [
    # Where the texts of a screen or a part live in the text tables, its sentences are pinned
    # there, key by key (REQUIRED_TEXTS in test_gate_interface_texts.py). Here stand the
    # sentences of the screens that keep their own English texts.
    "Losing the passphrase loses the vault. There is no recovery.",
    'Text this app receives from "${collection}" is sent to ${app.destination} '
    "and cannot be recalled.",
    "Stays on this Mac, if the app really is local",
    "any program running as you, including a hosted agent that can run",
    "Other forms (inflected, abbreviated, reversed order, split across lines) are different "
    "strings. Add each one you want replaced.",
    "Add a word or number that must never leave",
    "unverified",
    "After you release a passage, the app gets it when it asks again. If it does not ask by "
    "itself, tell it: \u201ctry again\u201d.",
    "A name is a label: ${PRODUCT} cannot check which program is really asking.",
    # Where the first run sends the owner to another program.
    "Nothing is changed for you: you copy the text and put it where the app reads it.",
]

# Words that belong to the engine and stay out of what the owner reads. "MCP" may
# appear where an application is connected, and nowhere else.
ENGINE_WORDS = re.compile(
    r"\b(ledger|ledgers|shim|shims|pool|pools|client|clients|policy|policies)\b", re.I
)

# Words for what the gate does with an app's calls. The owner reads "answered"; the
# engine's own word stays out of every sentence ("servers" is another word).
SERVING_WORDS = re.compile(r"\b(serve|serves|served|serving)\b", re.I)

# Claims the gate cannot back.
FORBIDDEN_CLAIMS = [
    r"never leaves? (?:your|this|the) (?:machine|computer|mac|device|laptop)",
    r"stays? private",
    r"100\s*%",
    r"tamper[- ]?proof",
    r"immutable",
    r"audit trail",
    r"anonymi[sz]",
    r"de-?identif",
    r"\bsandbox",
    r"\bfirewall",
    r"guarantee",
    r"\bcompliant\b|\bcompliance\b|\bcertified\b",
    r"military|bank-grade|unbreakable",
    r"prevents? (?:prompt )?injection",
    r"cannot be bypassed|can't be bypassed",
    r"\bverified identity\b",
    r"\bsecurely\b|\bfully secure\b|\bcompletely secure\b",
]


def files(suffix: str) -> list[Path]:
    return sorted(path for path in STATIC.iterdir() if path.suffix == suffix)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def scripts() -> dict[str, str]:
    return {path.name: read(path) for path in files(".js")}


def without_comments(source: str) -> str:
    """JavaScript or CSS without its comments; text inside quotes is left alone."""
    pattern = re.compile(
        r"""("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)|/\*.*?\*/|//[^\n]*""",
        re.S,
    )
    return pattern.sub(lambda match: match.group(1) or "", source)


def quoted_texts(source: str) -> list[str]:
    """The text inside double quotes and backticks, without the code of `${...}` parts."""
    found = re.findall(r'"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`', without_comments(source), re.S)
    return [re.sub(r"\$\{[^{}]*\}", " ", plain or template) for plain, template in found]


class Elements(HTMLParser):
    """Collects every start tag with its attributes, and the text inside script and style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline: list[tuple[str, str]] = []
        self._open: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._open = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == self._open:
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open and data.strip():
            self.inline.append((self._open, data))


def parsed(path: Path) -> Elements:
    parser = Elements()
    parser.feed(read(path))
    parser.close()
    return parser


def function_body(code: str, head: str) -> str:
    """One top-level function of a script, from the line ``head`` to the brace that closes it."""
    start = code.index(head)
    return code[start : code.index("\n}\n", start)]


def class_names(code: str) -> set[str]:
    """Every class a script gives an element, whether as one text or as a list of them."""
    names: set[str] = set()
    for text in re.findall(r'\bclass: "([^"]*)"', code):
        names.update(text.split())
    for listed in re.findall(r"\bclass: \[([^\]]*)\]", code):
        for text in re.findall(r'"([^"]*)"', listed):
            names.update(text.split())
    return names


# -- what is in the directory -----------------------------------------------------


def test_the_directory_is_flat_and_holds_only_page_files() -> None:
    entries = sorted(STATIC.iterdir())
    assert (STATIC / "index.html").is_file()
    assert [entry.name for entry in entries if not entry.is_file()] == []
    assert [entry.name for entry in entries if entry.is_symlink()] == []
    assert {entry.suffix for entry in entries} <= PAGE_SUFFIXES
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9.-]*", entry.name) for entry in entries)
    for suffix in (".css", ".js", ".svg"):
        assert files(suffix), f"no {suffix} file"


def test_every_file_is_utf8_text_without_a_byte_order_mark() -> None:
    for path in STATIC.iterdir():
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), path.name
        data.decode("utf-8")
        assert b"\r" not in data, path.name


def test_index_refers_only_to_files_that_exist() -> None:
    page = parsed(STATIC / "index.html")
    names = {path.name for path in STATIC.iterdir()}
    referred = [
        value
        for _tag, attributes in page.tags
        for name, value in attributes.items()
        if name in ("src", "href", "action", "poster", "data", "srcset", "manifest")
    ]
    assert referred, "the page loads nothing at all"
    for value in referred:
        assert value in names, (
            f"index.html refers to {value!r}, which is not a file of the interface"
        )
    linked = {a["href"] for tag, a in page.tags if tag == "link" and a.get("rel") == "stylesheet"}
    assert linked == {path.name for path in files(".css")}
    loaded = [a for tag, a in page.tags if tag == "script"]
    assert [a.get("src") for a in loaded] == ["main.js"]
    assert all(a.get("type") == "module" for a in loaded)


def test_the_page_has_what_the_scripts_and_a_reader_need() -> None:
    page = parsed(STATIC / "index.html")
    text = read(STATIC / "index.html")
    assert text.lstrip().lower().startswith("<!doctype html>")
    assert ("html", {"lang": "en"}) in page.tags
    assert any(
        tag == "meta" and (a.get("charset") or "").lower() == "utf-8" for tag, a in page.tags
    )
    assert any(tag == "title" for tag, _ in page.tags)
    ids = {a["id"] for _tag, a in page.tags if a.get("id")}
    assert {"app", "toasts"} <= ids
    assert any(a.get("id") == "toasts" and a.get("aria-live") == "polite" for _tag, a in page.tags)
    assert any(tag == "noscript" for tag, _ in page.tags)
    # Without scripts the page cannot choose a language, so it says its one sentence in both.
    said = re.search(r"<noscript>(.*?)</noscript>", text, re.S)
    assert said is not None
    assert re.findall(r"<p([^>]*)>", said.group(1)) == [
        ' class="noscript"',
        ' class="noscript" lang="pl"',
    ]
    assert "Do wy\u015bwietlenia tej strony potrzebny jest JavaScript." in said.group(1)
    assert "ka\u017cdy plik pochodzi z programu {{product}} na tym komputerze." in said.group(1)


# -- nothing inline, nothing from outside -----------------------------------------


@pytest.mark.parametrize("name", ["index.html", *[path.name for path in files(".svg")]])
def test_no_inline_script_or_style(name: str) -> None:
    page = parsed(STATIC / name)
    assert page.inline == [], f"{name} holds script or style text"
    for tag, attributes in page.tags:
        assert tag != "style", f"{name} has a style element"
        assert tag != "base", f"{name} has a base element"
        assert tag not in ("iframe", "object", "embed", "frame"), f"{name} embeds a {tag}"
        if tag == "script":
            assert attributes.get("src"), f"{name} has a script without a file"
        for attribute, value in attributes.items():
            assert attribute != "style", f"{name}: <{tag}> has a style attribute"
            assert not attribute.startswith("on"), f"{name}: <{tag}> has the handler {attribute}"
            assert "javascript:" not in (value or "").lower(), f"{name}: <{tag} {attribute}>"
    if name.endswith(".svg"):
        assert all(tag != "script" for tag, _ in page.tags)
        assert all(a.get("href") is None and a.get("xlink:href") is None for _tag, a in page.tags)


def test_no_address_outside_the_app_in_any_file() -> None:
    with_host = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.I)
    protocol_relative = re.compile(r"""["'`(=]\s*//[A-Za-z0-9]""")
    embedded = re.compile(r"""["'`(=]\s*(?:data|blob|javascript)\s*:""", re.I)
    for path in sorted(STATIC.iterdir()):
        text = read(path).replace(SVG_NAMESPACE, "")
        for rule in (with_host, protocol_relative, embedded):
            found = rule.search(text)
            assert found is None, (
                f"{path.name} names an address outside the app: {found.group(0)!r}"
            )


def test_the_address_rules_do_catch_addresses() -> None:
    with_host = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.I)
    assert with_host.search('fetch("https://example.com/x")')
    assert with_host.search("src: url(HTTP://example.com/font.woff)")
    assert with_host.search("new WebSocket(`wss://example.com`)")
    assert re.search(r"""["'`(=]\s*//[A-Za-z0-9]""", '<script src="//cdn.example.com/a.js">')
    assert re.search(
        r"""["'`(=]\s*(?:data|blob|javascript)\s*:""", "url(data:image/png;base64,AAAA)"
    )


def test_styles_load_nothing() -> None:
    for path in files(".css"):
        text = without_comments(read(path))
        assert re.search(r"\burl\s*\(", text) is None, f"{path.name} loads a file"
        assert "@import" not in text, f"{path.name} imports a file"
        assert "expression(" not in text
        assert "@font-face" not in text, f"{path.name} loads a font"


def test_scripts_import_only_each_other_and_all_are_used() -> None:
    sources = scripts()
    reachable: set[str] = set()
    waiting = ["main.js"]
    while waiting:
        name = waiting.pop()
        if name in reachable:
            continue
        assert name in sources, f"{name} is imported but is not a file of the interface"
        reachable.add(name)
        code = without_comments(sources[name])
        specifiers = re.findall(
            r"""^(?:import|export)\b[^;"'`]*?\bfrom\s*["']([^"']+)["']\s*;""", code, re.M
        )
        specifiers += re.findall(r"""^import\s*["']([^"']+)["']""", code, re.M)
        alone = ("dom.js", "format.js", "brand.js", "text-en.js", "text-pl.js")
        assert specifiers or name in alone, f"{name} imports nothing"
        for specifier in specifiers:
            assert re.fullmatch(r"\./[a-z0-9-]+\.js", specifier), f"{name} imports {specifier!r}"
            waiting.append(specifier[2:])
    assert reachable == set(sources), "a script is shipped that the page never loads"


# -- text is never markup, styles are never text ----------------------------------


@pytest.mark.parametrize(("what", "pattern"), sorted(MARKUP_AND_CODE_SINKS.items()))
def test_scripts_never_turn_text_into_markup_or_code(what: str, pattern: str) -> None:
    for name, source in scripts().items():
        found = re.search(pattern, without_comments(source))
        assert found is None, f"{name} uses {what}: {found.group(0) if found else ''}"


def test_the_sink_patterns_do_catch_what_they_name() -> None:
    samples = {
        "innerHTML": "node.innerHTML = text;",
        "outerHTML": "node.outerHTML = text;",
        "insertAdjacentHTML": 'node.insertAdjacentHTML("beforeend", text);',
        "document.write": "document.write(text);",
        "eval": "eval(text);",
        "new Function": "const run = new Function(text);",
        "Function()": "const run = Function(text);",
        "a timer given text": 'setTimeout("go()", 10);',
        "DOMParser": "new DOMParser().parseFromString(text, type);",
        "createContextualFragment": "range.createContextualFragment(text);",
        "srcdoc": "frame.srcdoc = text;",
        "a script element": 'document.createElement("script");',
        "a style attribute": 'node.setAttribute("style", text);',
        "cssText": "node.style.cssText = text;",
        "a javascript: address": 'link.href = "javascript:go()";',
        "import of a computed address": "await import(name);",
    }
    assert set(samples) == set(MARKUP_AND_CODE_SINKS)
    for what, sample in samples.items():
        assert re.search(MARKUP_AND_CODE_SINKS[what], sample), what
    harmless = 'toast("Checking the function of eval and innerHTML"); // node.innerHTML = x'
    assert re.search(MARKUP_AND_CODE_SINKS["innerHTML"], without_comments(harmless))
    assert not re.search(MARKUP_AND_CODE_SINKS["eval"], without_comments(harmless))
    assert "node.innerHTML = x" not in without_comments(harmless)


def test_scripts_use_nothing_newer_than_browsers_of_two_years_ago() -> None:
    newer = [
        r"\.findLast(?:Index)?\(",
        r"\.to(?:Sorted|Reversed|Spliced)\(",
        r"\bgroupBy\(",
        r"\bwithResolvers\(",
        r"\bfromAsync\(",
        r"\busing\s+\w+\s*=",
        r"\bimport\s+defer\b",
        r"\bwith\s*\{\s*type\s*:",
    ]
    for name, source in scripts().items():
        code = without_comments(source)
        for pattern in newer:
            found = re.search(pattern, code)
            assert found is None, f"{name} uses {found.group(0) if found else ''}"


def test_scripts_style_only_through_classes_and_custom_properties() -> None:
    for name, source in scripts().items():
        code = without_comments(source)
        for use in re.finditer(r"\.style\b\s*(.{0,24})", code):
            rest = use.group(1)
            assert re.match(r"\.(?:setProperty|getPropertyValue)\(\s*\"--", rest), (
                f"{name} sets a style directly: .style{rest}"
            )
        assert re.search(r"[\"'`]style[\"'`]\s*:", code) is None, f"{name} passes a style attribute"


def test_text_from_outside_enters_the_page_as_text_nodes() -> None:
    builder = without_comments(scripts()["dom.js"])
    assert "document.createTextNode(String(child))" in builder
    for name, source in scripts().items():
        code = without_comments(source)
        if name != "dom.js":
            assert "createElementNS" not in code, f"{name} builds markup outside the one builder"
            assert "createTextNode" not in code, f"{name} builds text outside the one builder"


# -- the calls the scripts make ---------------------------------------------------


def calls_made() -> set[tuple[str, str]]:
    code = without_comments(scripts()["api.js"])
    found = re.findall(r"""call\(\s*"([A-Z]+)",\s*(["`])(/api/[^"`]*)\2""", code)
    return {(method, re.sub(r"\$\{[^{}]*\}", "<>", path)) for method, _quote, path in found}


def test_every_api_path_is_one_the_contract_defines() -> None:
    made = calls_made()
    assert made - API_CALLS == set(), "the interface calls an address the app does not define"
    assert API_CALLS - made == set(), "an address of the app is never used; drop it here or use it"


def test_only_one_module_talks_to_the_app() -> None:
    for name, source in scripts().items():
        code = without_comments(source)
        literal_paths = re.findall(r"/api/[A-Za-z0-9_${}()./-]*", code)
        if name == "api.js":
            normalised = {re.sub(r"\$\{[^{}]*\}", "<>", path) for path in literal_paths}
            assert normalised == {path for _method, path in API_CALLS}
            assert code.count("fetch(") == 1
            continue
        assert literal_paths == [], f"{name} names an API path itself"
        assert re.search(r"\bfetch\s*\(", code) is None, f"{name} makes a request itself"
        for other in (
            "XMLHttpRequest",
            "WebSocket",
            "EventSource",
            "sendBeacon",
            "navigator.serviceWorker",
        ):
            assert other not in code, f"{name} uses {other}"
    api = without_comments(scripts()["api.js"])
    for other in ("XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon"):
        assert other not in api


def test_changing_requests_carry_the_header_and_stay_on_this_origin() -> None:
    api = without_comments(scripts()["api.js"])
    assert 'if (method !== "GET") headers["X-WDVA-Gate"] = "1";' in api
    assert 'credentials: "same-origin"' in api
    assert "no-cors" not in api
    assert 'mode: "cors"' not in api
    assert re.search(r"fetch\(url, init\)", api)
    assert re.search(r"let url = path;", api), "requests go to paths of this origin only"


def test_nothing_is_kept_in_the_browser_but_the_session_and_nothing_is_logged() -> None:
    for name, source in scripts().items():
        code = without_comments(source)
        for store in ("localStorage", "indexedDB", "document.cookie", "caches.", "openDatabase"):
            assert store not in code, f"{name} uses {store}"
        assert re.search(r"\bconsole\s*\.", code) is None, f"{name} writes to the console"
        if name != "api.js":
            assert "sessionStorage" not in code, f"{name} uses sessionStorage"
    api = without_comments(scripts()["api.js"])
    keys = set(re.findall(r"sessionStorage\.(?:setItem|getItem|removeItem)\(([^,)]+)", api))
    assert keys == {"SESSION_KEY"}, (
        "only the session token may be kept, never anything from the vault"
    )
    assert "window.sessionStorage.setItem(SESSION_KEY, token)" in api


def test_the_session_is_a_token_the_page_sends_itself_and_never_a_cookie() -> None:
    # A browser sends the cookies of 127.0.0.1 to every port of that address. What the page
    # keeps for this tab can be read by pages of this address and port only.
    api = without_comments(scripts()["api.js"])
    assert 'const SESSION_HEADER = "X-WDVA-Session";' in api
    assert "if (token) headers[SESSION_HEADER] = token;" in api
    # The token is taken from the two session answers and from nowhere else.
    assert api.count("keepSession(") == 3  # its definition, and one call for each answer
    assert "keepSession(answer.session)" in api
    assert "delete answer.session;" in api, "the token stays out of what the page shows"
    for name, source in scripts().items():
        code = without_comments(source)
        assert "cookie" not in code.lower(), f"{name} deals with cookies"
        if name != "api.js":
            assert "X-WDVA-Session" not in code, f"{name} handles the session itself"
            assert ".session)" not in code.replace("ctx.session)", "")


def test_a_passphrase_is_only_ever_typed_into_a_password_field_and_sent() -> None:
    hidden = 'type: "password",'
    hidden_unless_asked = 'type: form.shown ? "text" : "password",'
    fields = {}
    for source in scripts().values():
        for control in without_comments(source).split('el("input", {')[1:]:
            head = control[:400]
            ident = re.search(r'id: "([a-z-]*passphrase[a-z-]*)"', head)
            if ident:
                fields[ident.group(1)] = head.strip().splitlines()[0].strip()
    # Only a passphrase that is being chosen may be looked at, and only when the owner asks.
    assert fields == {
        "new-passphrase": hidden_unless_asked,
        "new-passphrase-again": hidden_unless_asked,
        "reauth-passphrase": hidden,
        "unlock-passphrase": hidden,
    }
    assert re.search(r"create: \{[^}]*\bshown: false\b", without_comments(scripts()["main.js"]))
    gateway = without_comments(scripts()["gateway.js"])
    created = gateway.index("await api.createStore(form.passphrase, chosenLanguage());")
    assert "form.shown = false;" in gateway[created : created + 300], (
        "the chosen passphrase stays on show after the vault was created"
    )
    api = without_comments(scripts()["api.js"])
    senders = re.findall(
        r"(\w+): \(passphrase(?:, language)?\) => call\(\"POST\", \"(/api/[a-z/]+)\"", api
    )
    assert sorted(path for _name, path in senders) == [
        "/api/reauth",
        "/api/store/create",
        "/api/unlock",
    ]
    # The line that sends a new passphrase has a second argument, and it is the language the
    # owner chose before the vault existed: a word of two letters, never anything typed.
    assert "createStore: (passphrase, language) => call(" in api
    assert "body: language ? { passphrase, language } : { passphrase }" in api


def test_a_release_cannot_be_pressed_through() -> None:
    views = without_comments(scripts()["views.js"])
    start = views.index("async function decide(ctx, item, release, event) {")
    decide = views[start : views.index("\n}\n", start)]
    sent = decide.index("api.release(")
    guards = [
        # the second press of a double click
        "if (event && event.detail > 1) return;",
        # a press while another decision is on its way
        "if (ctx.ui.deciding.size) return;",
        # a press before the cards have rested since they last appeared, changed or moved
        "if (release ? !releaseArmed(ctx) : performance.now() < ctx.ui.dismissQuietUntil) return;",
    ]
    for guard in guards:
        assert guard in decide[:sent], f"a release is sent without this check: {guard}"
    assert "disarm(ctx);" in decide[:sent], "the other cards stay ready while one is decided"
    assert decide.count("disarm(ctx);") >= 2, "the cards are ready at once after a decision"
    assert "ctx.ui.decided.set(key" in decide, "a decided card leaves its place at once"
    # "Details" unfolds lines under the two buttons, so the cards below move: an unfolded card
    # is not the card of the last draw, and "Release once" waits again.
    cards = function_body(views, "export function approvalCards(ctx, ")
    assert "approvalKey(entry.item) + unfolded(entry.item)" in cards
    assert "ctx.ui.approvalDetails.has(pairKey(item))" in cards
    # Every change of the cards makes "Release once" wait again, and it waits from the start.
    # The check is in the one function that draws cards, so it holds on every screen with a card.
    cards = function_body(views, "export function approvalCards(ctx, ")
    assert re.search(
        r"if \(signature !== ctx\.ui\.approvalSignature\) \{\s*"
        r"ctx\.ui\.approvalSignature = signature;\s*disarm\(ctx\);",
        cards,
    )
    # What stands around the cards is part of what is compared: a bar that comes or goes, a panel.
    assert "const signature = [around].concat(shown.map(" in cards
    main = without_comments(scripts()["main.js"])
    assert "releaseArmedAt: Number.POSITIVE_INFINITY," in main
    # Opening a page with cards counts as the cards appearing anew.
    route = function_body(main, "function onRoute() {")
    for opened in ('if (name === "pro/approvals") {', 'if (name === "home" || name === "setup") {'):
        after = route[route.index(opened) :]
        assert "ctx.ui.approvalSignature = null;" in after[: after.index("\n  }\n")], opened
    wait = re.search(r"const ARM_MS = (\d+);", views)
    assert wait is not None
    assert int(wait.group(1)) >= 1000
    # Neither answer is the loud button, and the safe one comes first.
    actions = views[views.index('{ class: "approval-actions" }') :]
    # They are named by the keys of their texts ("Don't release", "Release once").
    first, second = re.findall(r'button\(t\("([^"]+)"\), \{([^}]*)\}', actions)[:2]
    assert first[0] == "card.button.no"
    assert second[0] == "card.button.yes"
    assert 'key: "dismiss"' in first[1]
    assert 'key: "release"' in second[1]
    assert "kind:" not in first[1]
    assert "kind:" not in second[1]
    # A decision is sent from this one module, and the two buttons are drawn nowhere else.
    assert "api.release(" in views
    assert "api.dismiss(" in views
    for name, source in scripts().items():
        if name == "views.js":
            continue
        code = without_comments(source)
        assert "api.release(" not in code, f"{name} releases a passage itself"
        assert "api.dismiss(" not in code, f"{name} decides about a passage itself"
        assert "approval-actions" not in code, f"{name} draws the buttons of a card itself"
    # Home and the step "First question" show cards, and get them from that module.
    for name in ("basic.js", "gateway.js"):
        code = without_comments(scripts()[name])
        assert re.search(r'import \{[^}]*\bapprovalCards\b[^}]*\} from "\./views\.js";', code), name
    # The plain pages show none.
    assert "approvalCards" not in without_comments(scripts()["pages.js"])


def test_approval_cards_are_drawn_by_one_function() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    views = sources["views.js"]
    assert 'export function approvalCards(ctx, { limit = null, around = "" } = {}) {' in views
    cards = function_body(views, "export function approvalCards(ctx, ")
    assert "approvalCard(ctx, entry.item, index)" in cards
    assert "decidedLine(ctx, entry.done)" in cards
    assert 'card("Approved, not read yet"' in cards
    # The Approvals page of Pro gets its cards from there and draws none itself.
    page = function_body(views, "export function approvalsView(ctx) {")
    assert "approvalCards(ctx, {" in page
    assert "approvalCard(" not in page
    assert "decidedLine(" not in page
    assert views.count("approvalCard(ctx,") == 2  # where it is defined, and inside approvalCards
    # So do the Basic page and the step "First question".
    for name in ("basic.js", "gateway.js"):
        assert "approvalCards(ctx, {" in sources[name], f"{name} does not show the cards"
    # No other script gives an element the class of a card.
    assert "approval" in class_names(views)
    for name, code in sources.items():
        if name != "views.js":
            assert "approval" not in class_names(code), f"{name} draws a card of its own"
            assert "decided" not in class_names(code), f"{name} draws a decided line of its own"


def test_the_class_reader_tells_a_card_from_what_only_begins_like_one() -> None:
    sample = 'el("div", { class: "approval-stack" }); el("p", { class: ["x", a && "y z"] })'
    assert class_names(sample) == {"approval-stack", "x", "y", "z"}
    assert "approval" in class_names('{ class: "card approval", key: k }')


# -- the name, the demo's passphrase, and what the page cannot know -----------------


def test_the_name_is_written_in_no_file_of_the_interface() -> None:
    # The server puts the name into the page; brand.js reads it there; everything
    # else imports it from brand.js. So no file may spell it out, in any form.
    forms = [brand.PRODUCT, brand.SLUG, f"{brand.TOOL_PREFIX}_", brand.SLUG.replace("-", "_")]
    for path in sorted(STATIC.iterdir()):
        text = read(path)
        for form in forms:
            assert form not in text, f"{path.name} spells out the name: {form!r}"
    page = read(STATIC / "index.html")
    assert "<title>{{product}}</title>" in page
    assert '<meta name="application-name" content="{{product}}">' in page
    assert '<p class="boot-name">{{product}}</p>' in page
    assert re.search(r"<noscript>\s*<p[^>]*>\{\{product\}\} needs JavaScript", page)
    source = without_comments(scripts()["brand.js"])
    assert 'meta[name="application-name"]' in source
    assert re.search(r"export const PRODUCT\b", source)
    assert "import" not in source, "the one module every other may import imports nothing"
    # Whoever shows the name got it from there.
    for name, script in scripts().items():
        code = without_comments(script)
        if name != "brand.js" and re.search(r"\bPRODUCT\b", code):
            assert 'import { PRODUCT } from "./brand.js";' in code, name


def test_the_demo_passphrase_is_shown_only_as_the_app_sent_it() -> None:
    everything = "\n".join(scripts().values())
    assert demo.DEMO_PASSPHRASE not in everything, "a script holds the demo's passphrase itself"
    parts = without_comments(scripts()["parts.js"])
    start = parts.index("export function demoPassphraseNote(passphrase, fill) {")
    note = parts[start : parts.index("\n}\n", start)]
    # Nothing is shown unless the app sent a passphrase, which it does for the demo alone.
    assert 'if (typeof passphrase !== "string" || !passphrase) return null;' in note
    # Its three texts are keys of the text tables; the passphrase is an element of its own
    # inside the first sentence, and it is the one the app sent.
    assert 'around("demo.say.1", "passphrase")' in note
    assert 'el("strong", { class: "mono demo-pass-word" }, passphrase)' in note
    assert 't("demo.say.2")' in note
    assert 'button(t("demo.button.fill")' in note
    uses = {
        name: re.findall(r"demoPassphraseNote\(([^,]+),", without_comments(script))
        for name, script in scripts().items()
        if name != "parts.js"
    }
    assert {name: found for name, found in uses.items() if found} == {
        "gateway.js": ["state.demo_passphrase"],
        "sheets.js": ["demoPassphrase"],
    }
    main = without_comments(scripts()["main.js"])
    assert "requestReauth(ctx.state ? ctx.state.demo_passphrase : null)" in main
    # It reaches the page through the state and no other way.
    for name, script in scripts().items():
        for use in re.findall(r"[\w.]*demo_passphrase", without_comments(script)):
            assert use in ("state.demo_passphrase", "ctx.state.demo_passphrase"), (name, use)


def test_the_page_says_only_what_it_knows_about_the_files_the_owner_chose() -> None:
    shown = "\n".join(shown_texts().values())
    for claim in (
        r"originals? kept",
        r"original stays",
        r"originals? (?:is|are) (?:safe|deleted|removed|gone)",
        r"you can (?:now )?delete (?:the|your) (?:file|original)",
    ):
        assert re.search(claim, shown, re.I) is None, f"the page claims: {claim}"
    # The claims are looked for in the two text tables too: they are scripts like the others.
    assert {"text-en.js", "text-pl.js"} <= set(shown_texts())
    # What is true of a file after it was added is one sentence of the tables (docs.say.1: a
    # sealed copy is in the vault, and the file that was added stays where it was, not
    # encrypted). It is said once per list of added files, not once per file.
    manage = without_comments(scripts()["manage.js"])
    zone = function_body(manage, "export function dropZone(ctx, collection, options = {}) {")
    # The zone says it under the files, unless the page it stands on says it itself.
    said = (
        "sealedCount(state) && !options.said ? "
        'el("li", { class: "upload-outside", key: "outside" }, '
    )
    assert said + 'note("amber", t("docs.say.1"))) : null,' in zone
    assert manage.count('t("docs.say.1")') == 1
    assert 'fact("Copies of the files you added"' in manage
    # The page Documents says it at its head, whether a file was added or not, and is the one
    # place that tells the zone so.
    pages = without_comments(scripts()["pages.js"])
    page = function_body(pages, "export function documentsPage(ctx) {")
    assert 'el("p", { id: "docs-say-1" }, t("docs.say.1"))' in page
    assert "const zone = { who: true, said: true, filled: !waits };" in page
    for name, source in scripts().items():
        if name not in ("pages.js", "manage.js"):
            assert "said: true" not in without_comments(source), name


def test_a_question_is_not_shown_as_a_release() -> None:
    parts = without_comments(scripts()["parts.js"])
    start = parts.index("export function statusChip(item) {")
    chip = parts[start : parts.index("\n}\n", start)]
    assert 'if (item.kind === "question") return chip("Question", "outline"' in chip
    released = chip[chip.index('item.kind === "release"') : chip.index('item.kind === "question"')]
    assert '"Released"' in released
    assert "question" not in released
    views = without_comments(scripts()["views.js"])
    assert '{ value: "question", label: "Questions" }' in views
    assert 'item.kind === "question" ? note("plain", QUESTION_TEXT) : null' in views
    assert "No document text was released." in views
    # The figures for what was released do not take the questions in.
    assert 'stat(num(today.releases), today.releases === 1 ? "release" : "releases")' in views
    assert "today.questions > 0" in views


def engine_rules() -> set[str]:
    """Every value the engine can record as what allowed a release."""
    names = {name for name in vars(core) if name.startswith("RULE_")}
    # A release is only ever given its rule by one of these names, so a new rule has one too.
    source = Path(core.__file__).read_text(encoding="utf-8")
    given = set(re.findall(r"\brule\s*=(?!=)\s*([^\s,)]+)", source))
    assert given == names, "core.py gives a release a rule that has no RULE_ name"
    return {getattr(core, name) for name in names}


def test_every_rule_of_the_engine_has_words_in_the_details_of_an_entry() -> None:
    # "Allowed by" shows words, never the value the record holds ("grant:search").
    views = without_comments(scripts()["views.js"])
    table = re.search(r"const RULE_TEXT = \{(.*?)\};", views, re.S)
    assert table is not None
    entries = re.findall(r'(?:"([^"]+)"|([A-Za-z_]\w*))\s*:\s*"([^"]*)"', table.group(1))
    words = {quoted or bare: text for quoted, bare, text in entries}
    assert len(words) == len(entries)
    rules = engine_rules()
    assert rules - set(words) == set(), "the engine has a rule the interface has no words for"
    assert set(words) - rules == set(), "the interface has words for a rule the engine lacks"
    for rule, text in words.items():
        assert re.fullmatch(r"[A-Z][A-Za-z ,]+", text), f"{rule} is shown as {text!r}"
    # What allowed a read is said in the word the owner chose it by.
    assert words["grant:read"] == "Can read"
    assert 'detailRow("Allowed by", item.rule ? RULE_TEXT[item.rule] || item.rule : null)' in views


def test_the_screen_for_an_app_that_stopped_says_what_starting_it_again_brings() -> None:
    gateway = without_comments(scripts()["gateway.js"])
    start = gateway.index("export function offlineScreen(ctx) {")
    screen = gateway[start : gateway.index("\n}\n", start)]
    # Started again, the app is at a new address unless it was given its port, and this
    # page asks at the old one for ever. So the screen sends the owner to the address the
    # terminal prints (offline.say.1, pinned word for word with the tables) and promises
    # nothing about itself: it holds no sentence of its own at all.
    assert 'el("p", { class: "gate-lead" }, t("offline.say.1")),' in screen
    assert 'cardFoot(t("offline.say.2")),' in screen
    assert 'el("h1", { class: "gate-title" }, t("offline.title")),' in screen
    assert 'button(t("offline.button.try"), { onclick: () => ctx.start() }),' in screen
    for text in quoted_texts(screen):
        assert len(text.split()) <= 2, f"the screen holds a text of its own: {text!r}"
    assert "by itself" not in screen


def css_rules(name: str, selector: str) -> list[str]:
    """The declarations of every rule of one stylesheet whose selector is exactly ``selector``."""
    css = without_comments(read(STATIC / name))
    return re.findall(rf"(?m)^\s*{re.escape(selector)} \{{\n([^}}]*)\}}", css)


def test_on_the_steps_of_setting_up_green_is_kept_for_what_is_sealed() -> None:
    # A check on these screens says "done", "found" or "arrived", never "sealed": the part of
    # the stepper that is done, the number of the first line once the entry is in the settings
    # file, the mark beside the first request. None of them is drawn in green. (The check
    # beside a file that was sealed is.)
    for selector in (".step-dot", ".step.is-done .step-dot", ".ask-num", ".ask-wait-glyph"):
        rules = css_rules("app.css", selector)
        assert rules, selector
        assert all("--green" not in rule for rule in rules), selector
    assert ["var(--green)" in rule for rule in css_rules("app.css", ".upload-done .icon")] == [True]


def test_the_bar_that_says_what_is_wrong_stands_above_the_title_and_covers_nothing() -> None:
    # It used to lie over the top of the page (position: fixed), and so over the title.
    css = without_comments(read(STATIC / "app.css"))
    bars = re.findall(r"\.offline-bar\s*\{([^}]*)\}", css)
    assert len(bars) == 1, "one rule places the bar, at every width"
    properties = {line.split(":")[0].strip() for line in bars[0].split(";") if ":" in line}
    placing = {"position", "top", "left", "right", "bottom", "inset", "z-index"}
    assert properties & placing == set(), "the bar is taken out of the page's flow"
    assert "display" in properties
    assert "offline-bar {" not in without_comments(read(STATIC / "home.css"))
    # One function draws it, for both frames and for a step of setting up: the app is gone, or
    # a list could not be loaded.
    parts = without_comments(scripts()["parts.js"])
    bar = function_body(parts, "export function problemBar(ctx, problem) {")
    assert bar.count('class: "offline-bar", role: "alert"') == 2
    for name, source in scripts().items():
        wanted = 2 if name == "parts.js" else 0
        assert without_comments(source).count('class: "offline-bar"') == wanted, name
    # It is the first thing in the block that holds the title, and that block is what stays
    # in view, so the bar is seen wherever the page has been scrolled to. So in the frame of
    # the six pages and in the simple frame.
    main = without_comments(scripts()["main.js"])
    top = main[main.index('{ class: "page-top", key: "top" }') :]
    assert 0 < top.index("problemBar(ctx, loadProblem()),") < top.index('class: "page-head"')
    top = main[main.index('{ class: "column-top", key: "top" }') :]
    assert 0 < top.index("problemBar(ctx, problem),") < top.index('class: "column-head"')
    assert main.count("problemBar(") == 2
    assert ["position: sticky" in rule for rule in css_rules("app.css", ".page-top")] == [True]
    assert all("position" not in rule for rule in css_rules("app.css", ".page-head"))
    assert ["position: sticky" in rule for rule in css_rules("home.css", ".column-top")] == [True]
    assert all("position" not in rule for rule in css_rules("home.css", ".column-head"))
    # The command that starts the app again is shown only for a vault that this command would
    # open again, and the bar promises nothing about the page itself.
    assert "const command = ctx.state && ctx.state.demo ? null : launch.command;" in bar
    assert 'copyLine(ctx, "bar-launch-copy", command, t("bar.label.command"))' in bar
    assert sentences_of(bar) == []


def test_the_sidebar_claims_no_state_of_the_vault_while_the_app_does_not_answer() -> None:
    main = without_comments(scripts()["main.js"])
    start = main.index("function sidebar() {")
    sidebar = main[start : main.index("\n}\n", start)]
    assert "const unknown = ctx.offline;" in sidebar
    # Wherever the foot says "Unlocked" or names the time, it says something else then.
    assert 'el("span", { class: "lock-title" }, unknown ? "State unknown" : "Unlocked")' in sidebar
    assert 'el("span", { class: "lock-sub" }, unknown ? `${PRODUCT} is not answering` : ' in sidebar
    assert 'icon(unknown ? "warning" : "unlock")' in sidebar
    assert 'icon(unknown ? "warning" : "unlock", "lock-now-state")' in sidebar
    # Neither does a tooltip: both are chosen by the same question, the names of the sections apart.
    titles = re.findall(r"\btitle: ([^\n,]+)", sidebar)
    assert sorted(titles) == ["label", "unknown", "unknown"]


def test_the_simple_frame_claims_no_state_of_the_vault_while_the_app_does_not_answer() -> None:
    # The same rule for the sidebar of the simple frame, which at 720 px and narrower is also
    # its top bar: one line says until when the vault is unlocked, and while the app does not
    # answer it says that this is not known.
    main = without_comments(scripts()["main.js"])
    side = function_body(main, "function simpleSidebar() {")
    assert "const unknown = ctx.offline;" in side
    assert 'let lock = t("nav.status.open");' in side
    assert 'if (unknown) lock = t("nav.status.unknown");' in side
    assert side.index('if (unknown) lock = t("nav.status.unknown");') < side.index(
        "else if (state.locks_at)"
    )
    assert 'lock = t("nav.status.until", { time: clock(state.locks_at, ctx.now) });' in side
    assert (
        'el("p", { class: ["side-lock", unknown && "is-unknown"], id: "side-lock" }, lock),' in side
    )
    # The line is the one place of this frame that names the lock, and no tooltip does.
    assert side.count("nav.status.") == 3
    assert "title:" not in side
    # The bar that says what is wrong is drawn by the frame, in the block that stays in view.
    shell = function_body(main, "function simpleShell() {")
    assert "simpleSidebar()," in shell
    assert shell.index("problemBar(ctx, problem),") < shell.index('class: "column-head"')
    # At 720 px and narrower the same parts are a bar at the top and one at the bottom, which
    # stays in view: the line is then said to a screen reader and the button carries the lock.
    narrow = without_comments(read(STATIC / "home.css")).split("@media (max-width: 720px) {")
    assert len(narrow) == 2, "the simple frame has one breakpoint"
    assert re.search(r"\.side-nav \{[^}]*position: fixed;[^}]*bottom: 0;", narrow[1], re.S)
    assert re.search(r"\.side \{\s*display: contents;", narrow[1])
    assert '"aria-label": t("nav.button.lock")' in side


def test_messages_that_are_showing_are_lifted_above_a_sheet_that_opens() -> None:
    # A message shown just before a sheet opened lay under the sheet's backdrop, dimmed.
    parts = without_comments(scripts()["parts.js"])
    start = parts.index("export function openSheet(build, options = {}) {")
    opening = parts[start : parts.index("\n}\n", start)]
    assert opening.index("dialog.showModal();") < opening.index("liftOverSheet();")
    start = parts.index("function liftOverSheet() {")
    lifting = parts[start : parts.index("\n}\n", start)]
    assert "if (host && host.children.length) lift(host);" in lifting
    # Lifted, a message that was there is not animated in a second time.
    start = parts.index("function lift(host) {")
    lift = parts[start : parts.index("\n}\n", start)]
    settled = 'for (const item of host.children) item.classList.add("is-settled");'
    assert lift.index(settled) < lift.index("host.showPopover();")
    assert ["animation: none;" in rule for rule in css_rules("base.css", ".toast.is-settled")] == [
        True
    ]
    page = read(STATIC / "index.html")
    assert 'id="toasts"' in page
    assert 'popover="manual"' in page


def test_a_new_vault_is_told_what_to_do_next_in_order() -> None:
    views = without_comments(scripts()["views.js"])
    start = views.index("function startCard(ctx) {")
    card = views[start : views.index("\n}\n", start)]
    titles = re.findall(r'title: "([^"]+)"', card)
    assert titles == ["Add documents", "Add an app", "Give the app a collection", "Connect the app"]
    assert "if (!data || ctx.state.demo) return null;" in card, "the demo starts with it all done"
    assert "const next = steps.findIndex((step) => !step.done);" in card
    assert "if (next < 0) return null;" in card
    # Only the next step offers its action, so the order is the order they are done in.
    assert card.count('index === next ? el("a"') == 1


# -- the Basic page and the two views -----------------------------------------------


def test_basic_changes_only_what_it_can_express() -> None:
    # Limits, the settings of the vault, which kinds of apps are answered, pausing, the words
    # to replace and removing a collection are Pro's. Home says them and has no call for them,
    # and neither have the plain pages.
    for name in ("basic.js", "pages.js"):
        code = without_comments(scripts()[name])
        for call in (
            "api.setAppLimits(",
            "api.setSettings(",
            "api.setServing(",
            "api.addReplacedString(",
            "api.removeCollection(",
        ):
            assert call not in code, f"{name} calls {call}"
    # The plain pages give no app access at all.
    assert "setGrant" not in without_comments(scripts()["pages.js"])


def test_basic_attributes_to_stored_names() -> None:
    # A row, a sum or a rule names an app by the name it has in the vault. The readable name of
    # a listed program ("Claude Desktop") is for the list of apps and for instructions only.
    basic = without_comments(scripts()["basic.js"])
    assert re.search(r"\.label\b", basic) is None, "the Basic page reads the label of something"
    assert re.search(r"\.known\b", basic) is None, "the Basic page reads the list of programs"
    # So on the plain pages: a row of Activity, a document's line and the sentence that says
    # where text goes name an app by `name`. (Quoted texts are left out: keys hold ".label".)
    pages = re.sub(r'"[^"\n]*"', '""', without_comments(scripts()["pages.js"]))
    assert re.search(r"\.label\b", pages) is None, "a plain page reads the label of something"
    assert re.search(r"\.known\b", pages) is None, "a plain page reads the list of programs"
    sentence = function_body(
        without_comments(scripts()["sheets.js"]), "export function basicGrantSentence(app) {"
    )
    assert 't("docs.say.2", { app: app.name, destination: app.destination })' in sentence
    assert 't("docs.say.2.local", { app: app.name })' in sentence


def test_basic_says_what_pro_set_from_answers_of_the_app() -> None:
    basic = without_comments(scripts()["basic.js"])
    # Whether a limit is an app's own is read by comparing two answers, and whether the vault's
    # figures are those of a new vault is an answer too: the page holds no copy of such values.
    assert "app.limits[key] !== settings.defaults[key]" in basic
    assert "settings.changed_from_new.length > 0" in basic
    assert re.search(r"\b\d{1,3},\d{3}\b", "\n".join(quoted_texts(scripts()["basic.js"]))) is None
    numbers = {int(found) for found in re.findall(r"(?<![\w.])\d{3,}(?![\w.])", basic)}
    assert numbers <= {2000, 86400000}, "a figure of the vault is written into the Basic page"
    # A control for an app's access stands only where one value describes the whole vault, and
    # an app is added on the page only where that will stay so.
    assert 'groups.length > 1 ? proLink("Change in Pro", "#/pro/apps"' in basic
    assert "const expressible = groups.length <= 1 && !apps.some((app) => !app.hosted);" in basic
    assert 'proLink("Add an app (in Pro)", "#/pro/apps", { id: "basic-add-app" })' in basic
    # A link into Pro is an address and nothing else.
    link = function_body(basic, "function proLink(text, href, props = {}) {")
    assert "onclick" not in link
    assert 'return el("a", Object.assign({ class: "link", href }, props), text);' in link


def test_the_sentence_is_drawn_before_access_is_given() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    # Whoever gives an app access also has the sentence that says where the text goes.
    for name, code in sources.items():
        if "api.setGrant(" in code:
            assert "basicGrantSentence(" in code or "grantSentence(" in code, name
    # On the Basic page "Save" does nothing for a choice other than "No access" unless the
    # sentence is on the sheet at that moment.
    basic = sources["basic.js"]
    save = function_body(basic, "async function saveAccess(ctx, sheet, form, mode) {")
    guard = 'if (!form.sentenceShown && mode !== "none") return;'
    assert save.splitlines()[1].strip() == guard, "the guard is not the first thing Save does"
    assert basic.count("api.setGrant(") == 1
    assert basic.count("api.setCollection(") == 1
    assert save.index(guard) < save.index("api.setCollection(") < save.index("api.setGrant(")
    # Only drawing the sentence says that it is shown; every draw of the sheet starts without it.
    note = function_body(basic, "function accessSentence(form) {")
    assert "form.sentenceShown = true;" in note
    assert "basicGrantSentence(form.app)" in note
    assert 'id: "basic-sentence"' in note
    assert 'role: "note"' in note
    assert basic.count("sentenceShown = true") == 1
    sheet = function_body(basic, "async function openAppSheet(ctx, name) {")
    assert sheet.index("form.sentenceShown = false;") < sheet.index("accessSentence(form)")
    # It stands in the sheet itself whenever the choice is not "No access": no press unfolds it.
    assert 'form.mode !== "none" ? accessSentence(form) : null,' in sheet
    assert basic.count("accessSentence(form)") == 2  # where it is defined, and in the sheet
    # The first run: the press that adds an app and gives it access does nothing unless the
    # sentence of that app is on the screen, and nothing but drawing it says that it is.
    gateway = sources["gateway.js"]
    add = function_body(gateway, "async function addFirstApp(ctx) {")
    guard = "if (!form.sentenceShown) return;"
    assert add.splitlines()[2].strip() == guard, "the guard is not the first thing the press does"
    assert (
        add.index(guard)
        < add.index("api.addApp(")
        < add.index("api.setCollection(")
        < add.index("api.setGrant(")
    )
    drawn = function_body(gateway, "function firstSentence(form, described) {")
    assert "form.sentenceShown = true;" in drawn
    assert "text = basicGrantSentence(described);" in drawn
    assert 'return amber("first-sentence", text);' in drawn
    assert gateway.count("sentenceShown = true") == 1
    assert 'role: "note"' in function_body(gateway, "function amber(id, text) {")
    step = function_body(gateway, "function appStep(ctx) {")
    assert step.index("form.sentenceShown = false;") < step.index("firstSentence(form, described)")
    assert "form.sentenceShown = false;" in function_body(
        gateway, "function chooseApp(ctx, entry) {"
    )
    # It is the sentence of the app only once the app is chosen and described; until then the
    # one that holds for every app stands there, and the press cannot count.
    assert "let text = generalSentence();" in drawn
    assert drawn.index("if (described) {") < drawn.index("form.sentenceShown = true;")
    assert gateway.count("addFirstApp(") == 2  # where it is defined, and when the form is sent
    assert "if (ready && !form.busy) addFirstApp(ctx);" in step


def test_basic_changes_access_only_where_the_vault_has_one_group() -> None:
    basic = without_comments(scripts()["basic.js"])
    # What Home asks the app to change, in full. Removing an app goes through today's sheet of
    # Pro. The third call changes nothing of the vault: it says that setting up is over, a word
    # kept beside the vault. The fourth changes nothing of the owner's at all: in a demo it has
    # the played app ask once more
    # (see test_the_demo_guide_makes_the_played_app_ask_again_only_on_a_press).
    assert set(re.findall(r"\bapi\.(\w+)\(", basic)) == {
        "setCollection",
        "setGrant",
        "setInterface",
        "demoAgain",
    }
    assert re.findall(r"api\.setInterface\(([^)]*)\)", basic) == ['{ setup: "done" }']
    assert 'from "./api.js"' in basic
    assert "removeApp(ctx, app)" in basic
    # Documents are added and removed on their own page, which calls the app for nothing
    # itself: a file goes through the one function that adds files, a removal through today's
    # sheet, which asks first.
    pages = without_comments(scripts()["pages.js"])
    assert re.findall(r"\bapi\.(\w+)\(", pages) == []
    assert 'from "./api.js"' not in pages
    for call in ("removeDocument(ctx, document)", "removeDocument(ctx, sample.document)"):
        assert call in pages, call
    assert "removeDocument(" not in basic
    manage = without_comments(scripts()["manage.js"])
    for head in (
        "export async function removeDocument(ctx, document) {",
        "export async function removeApp(ctx, app) {",
    ):
        assert "await confirmSheet({" in function_body(manage, head), "removing asks first"
    # The sheet is opened by "Change" alone, "Change" stands only where the vault has one group
    # or none, and the sheet itself refuses a vault with several: one press never changes
    # access for several groups.
    assert basic.count("openAppSheet(") == 2  # where it is defined, and behind "Change"
    assert "onclick: () => openAppSheet(ctx, app.name)," in function_body(
        basic, "function changeButton(ctx, app, settings) {"
    )
    assert basic.count("changeButton(") == 2
    assert (
        'groups.length > 1 ? proLink("Change in Pro", "#/pro/apps", '
        '{ key: `change-${app.name}`, class: "link basic-app-change" }) '
        ": changeButton(ctx, app, settings),"
    ) in basic
    sheet = function_body(basic, "async function openAppSheet(ctx, name) {")
    assert "if (!app || !settings || data.collections.length > 1) return;" in sheet
    # It starts on what is in force and offers the three choices in the order of care.
    assert 'inForce: group ? grantOf(app, group.name) : "none",' in sheet
    assert 'mode: group ? grantOf(app, group.name) : "none",' in sheet
    assert '["approve", "read", "none"].map((mode) =>' in sheet
    assert 'name: "basic-mode",' in sheet
    assert "checked: form.mode === mode," in sheet
    assert "GRANT_LABEL[mode]" in sheet
    buttons = sheet[sheet.index("sheetFoot(") :]
    assert buttons.index('button("Cancel", {') < buttons.index('"Save", {')
    assert "autofocus: true" in buttons[: buttons.index('"Save", {')]
    assert "disabled: form.mode === form.inForce," in buttons
    # Nothing but the press on "Save" saves: the sheet is no form that a key could send.
    assert "onclick: () => saveAccess(ctx, self, form, form.mode)," in buttons
    assert basic.count("saveAccess(") == 2  # where it is defined, and behind "Save"
    assert "onsubmit" not in basic
    assert '"submit"' not in basic
    # The two figures of "Can read" are the vault's own.
    lines = function_body(basic, "function accessLines(settings) {")
    assert "percent(settings.per_document.max_fraction)" in lines
    assert "settings.per_document.max_chars" in lines
    # "Save" opens the documents to apps that send text to a provider only when the sheet says so.
    save = function_body(basic, "async function saveAccess(ctx, sheet, form, mode) {")
    assert "if (form.opensShown) body.allow_hosted = true;" in save
    assert save.count("allow_hosted") == 1
    assert "await api.setGrant(form.app.name, form.group, mode);" in save
    said = function_body(basic, "function opensLine(form) {")
    assert "form.opensShown = true;" in said
    assert (
        "Saving also opens your documents to apps that send text to a provider. "
        "No other app gets access by that."
    ) in said
    assert basic.count("opensShown = true") == 1
    assert 'form.mode !== "none" && form.closed ? opensLine(form) : null,' in sheet
    # When the owner closes the sheet that asks for the passphrase, the choice goes back to what
    # is in force, and the sheet never says "nothing was changed" after something was.
    failed = save[save.index("} catch (error) {") :]
    assert 'const gaveUp = error.code === "cancelled";' in failed
    assert "if (gaveUp) form.mode = form.inForce;" in failed
    assert failed.index("if (opened) {") < failed.index("form.error = error.message;")
    assert 'id: "basic-error", key: "error", role: "alert"' in sheet
    api = without_comments(scripts()["api.js"])
    # ("Nothing was changed.", common.error.cancelled in the text tables.)
    assert 'throw new ApiError(403, "cancelled", t("common.error.cancelled"));' in api


def test_adding_to_one_of_several_groups_chooses_no_group_for_the_owner() -> None:
    pages = without_comments(scripts()["pages.js"])
    page = function_body(pages, "export function documentsPage(ctx) {")
    # One zone, and with several groups it only holds the files: the sheet asks where they go.
    assert page.count("dropZone(") == 2
    assert (
        'dropZone(ctx, "chosen", Object.assign(zone, { lists: groups.map((group) => group.name), '
        "take: (files) => addToWhichGroup(ctx, files) }))"
    ) in page
    assert ": dropZone(ctx, groups.length ? groups[0].name : FIRST_GROUP, zone)," in page
    assert pages.count("dropZone(") == 2
    assert 'const FIRST_GROUP = "documents";' in pages
    sheet = function_body(pages, "async function addToWhichGroup(ctx, files) {")
    assert "const form = { group: null };" in sheet
    assert "checked: form.group === group," in sheet
    assert 'name: "docs-group",' in sheet
    # ("Cancel", "Add": the keys common.button.cancel and group.button.add.)
    assert (
        'button(t("common.button.cancel"), { onclick: () => self.close(null), autofocus: true }),'
    ) in sheet
    add = sheet[sheet.index('button(t("group.button.add"), {') :]
    assert "disabled: form.group === null," in add[:200]
    assert "if (form.group !== null) self.close(form.group);" in add[:300]
    # Files are sent only after "Add", and to the group that was chosen. "Cancel" sends nothing.
    assert pages.count("uploadFiles(") == 1
    closed = sheet[sheet.index("const group = await sheet.closed;") :]
    assert 'if (typeof group === "string") await uploadFiles(ctx, group, files);' in closed
    assert sheet.count("self.close(") == 2  # "Cancel", with nothing; "Add", with the group
    # Once a group is chosen that an app can use, the sentence that says where text goes is
    # there: the second sentence of the page, for the apps of that group.
    assert (
        "const sentence = form.group === null ? null : whereTextGoes(readersOf(apps, form.group));"
    ) in sheet
    goes = function_body(pages, "function whereTextGoes(readers) {")
    assert "if (!readers.length) return null;" in goes
    assert "readers.length === 1 ? basicGrantSentence(readers[0]) : generalSentence();" in goes
    # Each group says who can ask for what is in it: names, or that nobody can.
    assert 'names.length ? list(names) : t("group.status.nobody")' in sheet
    assert "readersOf(apps, group).map((app) => app.name);" in sheet
    # The zone hands the files over in place of sending them, and keeps no file for later.
    manage = without_comments(scripts()["manage.js"])
    zone = function_body(manage, "export function dropZone(ctx, collection, options = {}) {")
    assert 'const held = typeof options.take === "function";' in zone
    assert (
        "const accept = (files) => "
        "(held ? options.take(Array.from(files)) : uploadFiles(ctx, collection, files));"
    ) in zone
    assert zone.count("uploadFiles(") == 1
    assert "accept(event.dataTransfer.files);" in zone
    assert zone.count("accept(") == 2  # a drop, "Choose files"


def test_after_a_sealed_file_the_page_says_which_apps_can_ask_for_it() -> None:
    manage = without_comments(scripts()["manage.js"])
    zone = function_body(manage, "export function dropZone(ctx, collection, options = {}) {")
    wanted = "options.who && sealedCount(entry.list) ? whoLine(ctx, entry.name) : null,"
    assert wanted in zone
    # It stands above the note about the file that was left outside, which is said once.
    assert zone.index(wanted) < zone.index('note("amber", t("docs.say.1"))')
    # The line is one of two texts of the tables, and it is never left out: either apps are
    # named, by the names they have in the vault, or the line says that no app can ask yet.
    line = function_body(manage, "export function whoLine(ctx, collection) {")
    assert 'class: "upload-who", role: "status"' in line
    assert (
        'names.length ? t("docs.result.who", { apps: names }) : t("docs.result.nobody")'
    ) in line
    assert ".map((app) => app.name);" in line
    assert '["approve", "read"].includes(app.grants[collection])' in line
    assert ".label" not in line
    # The page Documents and a step of setting up ask for the line; the pages of Pro are as
    # they were: their zones say nothing more.
    pages = without_comments(scripts()["pages.js"])
    assert "const zone = { who: true, said: true, filled: !waits };" in pages
    assert "dropZone(ctx, name)," in manage
    assert 'dropZone(ctx, "documents", { large: true }),' in manage
    assert manage.count("who:") == 0


def test_a_sheet_that_opens_or_closes_over_the_cards_makes_release_wait_again() -> None:
    # The Basic page has its cards and its sheets on one page. When a sheet closes, a second
    # press that was meant for one of its buttons meets what is under it; that must not be an
    # armed "Release once".
    parts = without_comments(scripts()["parts.js"])
    opening = function_body(parts, "export function openSheet(build, options = {}) {")
    closing = opening[opening.index('dialog.addEventListener("close", () => {') :]
    closing = closing[: closing.index("});")]
    assert "openSheets.delete(sheet);" in closing
    assert closing.index("openSheets.delete(sheet);") < closing.index("sheetsChanged();")
    assert opening.index("openSheets.add(sheet);") < opening.rindex("sheetsChanged();")
    assert opening.count("sheetsChanged();") == 2
    told = function_body(parts, "function sheetsChanged() {")
    assert "if (sheetsWatcher) sheetsWatcher(openSheets.size);" in told
    # The page is drawn again in the same turn, and the cards are told through what is around them.
    main = without_comments(scripts()["main.js"])
    assert re.search(
        r"watchSheets\(\(open\) => \{\s*ctx\.ui\.sheets = open;\s*schedule\(\);\s*\}\);", main
    )
    assert "queueMicrotask(() => {" in function_body(main, "function schedule() {")
    basic = without_comments(scripts()["basic.js"])
    around = basic[basic.index("const around = [") :]
    assert 'ctx.ui.sheets > 0 ? "sheet" : "",' in around[: around.index("].join(")]


def test_pro_reads_what_belongs_to_a_group_by_that_group_alone() -> None:
    # The same in Pro. For a group named "constructor", `grants[name] || "none"` gave a function:
    # the control on the Apps page showed no choice, and "Ask me each time" was sent without the
    # sheet that says where the text goes.
    read = scripts()
    for name in ("manage.js", "sheets.js"):
        assert not re.search(r"\.grants\[[^\]]+\]\s*\|\|", read[name]), name
    assert "Object.prototype.hasOwnProperty.call(app.grants, collection.name)" in read["manage.js"]
    assert "Object.prototype.hasOwnProperty.call(app.grants, collection)" in read["sheets.js"]
    # What the owner has typed into the note of a group is kept by the group's name too.
    assert "notes: Object.create(null)," in read["main.js"]


def test_basic_reads_what_belongs_to_a_group_by_that_group_alone() -> None:
    # A group may be named by a word that every object of the page answers to ("constructor").
    # Read carelessly, such a name gives something that is neither an access nor a list of files,
    # and the page that draws from it shows nothing at all. So on Home and on the page Documents.
    for name in ("basic.js", "pages.js"):
        code = without_comments(scripts()[name])
        read = function_body(code, "function grantOf(app, group) {")
        assert "Object.prototype.hasOwnProperty.call(grants, group)" in read, name
        assert 'return mode === "approve" || mode === "read" ? mode : "none";' in read, name
        assert re.search(r"\.grants\[", code) is None, f"{name} reads an access without that check"
    manage = without_comments(scripts()["manage.js"])
    state = function_body(manage, "function uploadState(ctx, collection) {")
    assert "if (!Object.prototype.hasOwnProperty.call(ctx.ui.uploads, collection))" in state
    assert manage.count("ctx.ui.uploads[") == 2  # both inside that function
    assert state.count("ctx.ui.uploads[") == 2
    # Who can ask for what is in a group is told by comparing with the two words, never by "is
    # there something": on the page Documents through the one function that reads an access.
    pages = without_comments(scripts()["pages.js"])
    readers = function_body(pages, "function readersOf(apps, group) {")
    assert readers.count('grantOf(app, group) !== "none"') == 1
    assert readers.count('grantOf(app, name) !== "none"') == 1
    line = function_body(manage, "export function whoLine(ctx, collection) {")
    assert '["approve", "read"].includes(app.grants[collection])' in line


def test_what_the_product_cannot_do_is_one_list_in_both_views() -> None:
    manage = without_comments(scripts()["manage.js"])
    listed = re.search(r"export const CANNOT = \[(.*?)\n\];", manage, re.S)
    assert listed is not None
    # The eight statements are eight keys of the text tables, in their order.
    assert re.findall(r'^\s+"([^"\n]+)",$', listed.group(1), re.M) == [
        f"how.cannot.{number}" for number in range(1, 9)
    ]
    drawn = function_body(manage, "export function cannotList() {")
    assert 'el("ul", { class: "cannot" }, CANNOT.map((key) => el("li", { key }, t(key))))' in drawn
    assert "cannotList()," in function_body(manage, "export function vaultView(ctx) {")
    line = function_body(manage, "export function versionLine(state) {")
    assert 'return t("how.version", { version: state.version });' in line
    # The page "How it works and what it cannot do" shows the same eight under their heading,
    # and the line about the version; Home's foot and Settings lead there.
    pages = without_comments(scripts()["pages.js"])
    how = function_body(pages, "export function howPage(ctx) {")
    assert 'part.lines ? part.lines.map((key) => el("p", { key }, t(key))) : cannotList(),' in how
    assert "versionLine(ctx.state)" in how
    assert '{ title: "how.cannot.title", lines: null },' in pages
    assert 'href: "#/how" }, t("how.title")' in function_body(
        pages, "export function settingsPage(ctx) {"
    )
    basic = without_comments(scripts()["basic.js"])
    assert 'id: "home-how", href: "#/how" }, t("home.button.how")' in basic
    assert "openCannot" not in basic
    # Neither the statements nor the line about the version are written a second time: the
    # scripts name each key once, and each table holds each text once.
    pages = {name: code for name, code in scripts().items() if not name.startswith("text-")}
    everything = "\n".join(without_comments(source) for source in pages.values())
    assert everything.count('"how.version"') == 1
    assert everything.count('"how.cannot.1"') == 1
    assert everything.count("CANNOT.map(") == 1
    assert everything.count("cannotList(") == 3  # its definition, the Vault page, the page How
    assert everything.count("versionLine(") == 3  # its definition, the Vault page, the page How
    for name in ("text-en.js", "text-pl.js"):
        table = scripts()[name]
        assert table.count('"how.version"') == 1, name
        assert len(re.findall(r'^  "how\.cannot\.\d": ', table, re.M)) == 8, name
    english = scripts()["text-en.js"]
    assert english.count("a preview: it has not yet been tried against the AI apps it lists") == 1
    assert english.count("Take text back.") == 1


def test_the_sample_letter_is_recognised_by_its_title_and_its_length() -> None:
    manage = without_comments(scripts()["manage.js"])
    sample = re.search(
        r'export const SAMPLE = \{\s*file: "([^"]+)",\s*title: "([^"]+)",\s*'
        r'text:((?:\s*"[^"\n]*"\s*\+?)+),\s*\};',
        manage,
    )
    assert sample is not None
    file, title, pieces = sample.groups()
    assert file == "invented-sample-letter.txt"
    assert title == "invented-sample-letter"
    assert file == f"{title}.txt", "the vault makes the title from the name of the file"
    text = "".join(re.findall(r'"([^"\n]*)"', pieces)).replace("\\n", "\n")
    assert "\\" not in text
    assert text.startswith("SAMPLE DOCUMENT, INVENTED. ")
    assert "Nothing in it is real." in text
    # The page tells the letter by its title and its number of characters together. So the vault
    # must count the text exactly as the page does: nothing in it is changed when it is sealed,
    # and it is one passage.
    assert text.isascii()
    assert textproc.sanitize(text).strip("\n") == text
    assert len(textproc.chunk_spans(text)) == 1
    recognised = function_body(manage, "export function isSample(document) {")
    assert (
        "return document.title === SAMPLE.title && document.characters === SAMPLE.text.length;"
    ) in recognised
    # The page Documents says that it is there, and offers to remove it through today's sheet.
    # ("One of them is the invented sample document.", "Remove it": docs.status.sample and
    # docs.button.sample.remove.)
    pages = without_comments(scripts()["pages.js"])
    page = function_body(pages, "export function documentsPage(ctx) {")
    assert "const sample = held.find((entry) => isSample(entry.document));" in page
    assert 'el("span", null, t("docs.status.sample")),' in page
    assert (
        'button(t("docs.button.sample.remove"), { small: true, id: "sample-remove", '
        "onclick: () => removeDocument(ctx, sample.document) })"
    ) in page


def test_first_run_and_basic_name_no_protocol() -> None:
    for name in ("gateway.js", "basic.js", "pages.js"):
        for text in quoted_texts(scripts()[name]):
            for word in ("MCP", "Model Context Protocol", "://"):
                assert word not in text, f"{name} shows {word!r}: {text[:80]!r}"


def test_sentences_that_are_not_true_are_absent() -> None:
    # "Nothing can leave" stands in the three places of Pro where no app exists, and nowhere
    # new: with an app, or with a search answer, something can.
    standing = {"views.js": 2, "manage.js": 1}
    for name, source in scripts().items():
        shown = "\n".join(quoted_texts(source)).lower()
        assert shown.count("nothing can leave") == standing.get(name, 0), name
        for phrase in ("always replaced", "ran out unused", "is connected", "recommended"):
            assert phrase not in shown, f"{name} says {phrase!r}"


def test_no_browser_permission_is_asked() -> None:
    for name, source in scripts().items():
        code = without_comments(source)
        for word in (
            "Notification",
            "requestPermission",
            "navigator.permissions",
            "serviceWorker",
            "PushManager",
        ):
            assert word not in code, f"{name} uses {word}"


def test_nothing_but_the_drop_zone_takes_a_drop() -> None:
    for name, source in scripts().items():
        code = without_comments(source)
        if name != "manage.js":
            assert "ondrop:" not in code, f"{name} takes dropped files"
    manage = without_comments(scripts()["manage.js"])
    assert manage.count("ondrop:") == 1
    zone = function_body(manage, "export function dropZone(ctx, collection, options = {}) {")
    assert "ondrop:" in zone


def test_the_start_page_is_kept_without_a_word() -> None:
    # On which page the app opens is chosen on the page Settings, under "Open on": "Home" or
    # "All settings" (settings.label.start.home, settings.label.start.all). The app keeps the
    # choice as the word it always kept, "basic" or "pro".
    pages = without_comments(scripts()["pages.js"])
    settings = function_body(pages, "export function settingsPage(ctx) {")
    choice = settings[settings.index('name: "start",') :]
    choice = choice[: choice.index("}),")]
    assert re.findall(r'\{ value: "(\w+)", label: t\("([^"]+)"\) \}', choice) == [
        ("basic", "settings.label.start.home"),
        ("pro", "settings.label.start.all"),
    ]
    assert 'legend: t("settings.label.start"),' in choice
    assert 'value: kept.view === "basic" ? "basic" : "pro",' in choice
    assert "onchange: (view) => ctx.keepView(view)," in choice
    # It comes last on the page, under the six pages and the two links: most people never change it.
    assert settings.index('"settings.all.title"') < settings.index('name: "start",')
    english = scripts()["text-en.js"]
    assert '"settings.label.start": "Open on",' in english
    assert '"settings.label.start.home": "Home",' in english
    assert '"settings.label.start.all": "All settings",' in english
    # Neither is called by a word that ranks it, in a script or in a table.
    shown = "\n".join("\n".join(quoted_texts(source)) for source in scripts().values()).lower()
    for word in ("advanced", "upgrade", "simple view", "expert", "premium", "zaawansowan"):
        assert word not in shown, f"a script says {word!r}"
    # Keeping it says nothing when it worked: the screen is the answer. The one other word this
    # script has the app keep is the language, and that says nothing either.
    main = without_comments(scripts()["main.js"])
    assert main.count("api.setInterface(") == 2
    language = function_body(main, "async function keepLanguage() {")
    asked, refused = language.split("} catch (error) {")
    assert "applyState(await api.setInterface({ language: wanted }));" in asked
    assert "toast" not in asked
    assert "toastError(error);" in refused
    keep = function_body(main, "async function keepView(view) {")
    kept, failed = keep.split("} catch (error) {")
    assert "applyState(await api.setInterface({ view }));" in kept
    assert "toast" not in kept
    assert "toastError(error);" in failed
    assert "toast" not in function_body(main, "async function setUpInPro() {")
    assert "  keepView," in main[main.index("const ctx = {") : main.index("let queued = false;")]
    # The switch "View", the bar of a visit to Pro and "Stay in Pro" are gone, with their words.
    for gone in ("VIEW_OPTIONS", "viewChoice", "viewSwitch", "visitBar", "stayInPro", "chooseView"):
        assert gone not in main, gone
    for name, source in scripts().items():
        said = "\n".join(quoted_texts(source))
        for words in (
            "Stay in Pro",
            "Back to Basic",
            "Your view stays Basic",
            "Switch to Basic view",
        ):
            assert words not in said, f"{name} says {words!r}"
    # The six pages get one entry above their six: the way back to Home.
    sidebar = function_body(main, "function sidebar() {")
    home = 'id: "pro-home", href: "#/home", key: "home" }'
    assert sidebar.index(home) < sidebar.index("PRO_NAV.map(")
    assert 't("pro.button.home")' in sidebar


def test_the_six_pages_are_pro_whatever_view_is_kept() -> None:
    main = without_comments(scripts()["main.js"])
    route = function_body(main, "function routeFromHash() {")
    # The addresses, in the order they are tried: the simple pages; the six pages under "pro";
    # the addresses from before; a step of setting up; and only then what the app keeps.
    simple = route.index(
        'if (name === "home" || Object.prototype.hasOwnProperty.call(PAGES, name)) return name;'
    )
    pro = route.index(
        'if (name === "pro") return Object.prototype.hasOwnProperty.call(VIEWS, step) ? '
        '`pro/${step}` : "pro/overview";'
    )
    basic = route.index('if (name === "basic") return "home";')
    old = route.index("if (OLD_ADDRESSES.includes(name)) return `pro/${name}`;")
    assert simple < pro < basic < old < route.index("return defaultRoute();")
    pages = re.search(r"const PAGES = \{(.*?)\};", main, re.S)
    assert pages is not None
    assert re.findall(r"^\s+(\w+): \w+,$", pages.group(1), re.M) == [
        "documents",
        "activity",
        "settings",
        "how",
    ]
    six = re.search(r"const VIEWS = \{(.*?)\};", main, re.S)
    assert six is not None
    assert re.findall(r"^\s+(\w+): \w+,$", six.group(1), re.M) == [
        "overview",
        "activity",
        "approvals",
        "documents",
        "apps",
        "vault",
    ]
    # "#/documents" and "#/activity" are the simple pages now: of the addresses from before,
    # only the four that name no simple page lead to one of the six.
    assert 'const OLD_ADDRESSES = ["overview", "approvals", "apps", "vault"];' in main
    # An address from before becomes the address its page has now; one that names no page is
    # left as it is.
    settle = function_body(main, "function settleAddress() {")
    assert 'if (name !== "basic" && !OLD_ADDRESSES.includes(name)) return;' in settle
    assert 'window.history.replaceState(null, "", `#/${ctx.route}${query}`);' in settle
    # Only an address that names no page is decided by what the app keeps.
    fallback = function_body(main, "function defaultRoute() {")
    assert 'return kept && kept.view === "basic" ? "home" : "pro/overview";' in fallback
    screen = function_body(main, "function screen() {")
    assert screen.index("return lockedScreen(ctx);") < screen.index(
        'if (ctx.route.startsWith("pro/")) return shell();'
    )
    assert screen.rstrip().endswith("return simpleShell();")
    # Every link a script holds names a page by the address it has now.
    for name, source in scripts().items():
        for address in re.findall(r'"#/([a-z/]*)', without_comments(source)):
            first = address.split("/")[0]
            assert first in ("home", "documents", "activity", "settings", "how", "pro", "setup"), (
                f"{name} links to #/{address}"
            )
    for name in ("views.js", "manage.js", "sheets.js"):
        # The six pages link to each other, never to a simple page of the same word.
        for address in re.findall(r'"#/([a-z/]*)', without_comments(scripts()[name])):
            assert address.startswith("pro/"), f"{name} links to #/{address}"
    # What Home lists is asked for by kind, never picked from the newest entries; so are the
    # two lists of the page Activity.
    assert 'left: () => api.activity({ limit: LEFT_FIRST, kind: "release" }),' in main
    assert 'api.activity({ limit: STOPS_PAGE, kind: "refusal" })' in main
    assert "page: await api.activity({ limit: ROWS_PAGE, kind: tab })" in main
    assert "const ROWS_PAGE = 20;" in main
    assert 'home: ["apps", "approvals", "settings", "documents", "left", "stops"],' in main
    # What each plain page loads.
    for needs in (
        'documents: ["documents", "apps"],',
        'activity: ["apps", "rows"],',
        "settings: [],",
        "how: [],",
    ):
        assert needs in main, needs


def test_the_tab_shows_that_a_passage_waits() -> None:
    main = without_comments(scripts()["main.js"])
    paint = function_body(main, "function paint() {")
    assert "const waiting = state && state.today ? state.today.approvals_waiting : 0;" in paint
    assert 'const wanted = waiting > 0 ? "favicon-waiting.svg" : "favicon.svg";' in paint
    assert "document.querySelector('link[rel=\"icon\"]')" in paint
    # Every picture a script names is a file of the interface.
    names = {path.name for path in STATIC.iterdir()}
    for name, source in scripts().items():
        for picture in re.findall(r'"([\w.-]+\.svg)"', without_comments(source)):
            assert picture in names, f"{name} names {picture}, which is not a file of the interface"
    page = read(STATIC / "index.html")
    assert '<link rel="icon" href="favicon.svg" type="image/svg+xml">' in page
    # The icon with the dot is the icon, and a dot.
    plain = read(STATIC / "favicon.svg").strip().removesuffix("</svg>")
    assert read(STATIC / "favicon-waiting.svg").startswith(plain)


def test_the_locked_screen_says_in_both_views_what_is_visible_without_the_key() -> None:
    gateway = without_comments(scripts()["gateway.js"])
    screen = function_body(gateway, "export function lockedScreen(ctx) {")
    assert 'const pro = !(state.interface && state.interface.view === "basic");' in screen
    # Sentence 1 stands in both views: while locked, apps get one answer. The three figures
    # (sealed files, size, the newest entry of the record) and the sentence that explains
    # them stand only where the view that is kept is Pro: the everyday view shows no figure
    # of the record on the locked screen.
    assert 'cardFoot(t("locked.say.1"), pro ? t("locked.say.2") : null),' in screen
    assert screen.count('t("locked.say.1")') == 1
    assert screen.count('t("locked.say.2")') == 1
    figures = re.search(
        r'pro\s*\?\s*el\(\s*"dl",\s*\{ class: "seal-facts", key: "facts" \},(.*?)\n        : null,',
        screen,
        re.S,
    )
    assert figures is not None, "the figures are drawn whatever view is kept"
    for label in ("locked.label.files", "locked.label.size", "locked.label.last"):
        assert screen.count(f't("{label}")') == 1, label
        assert f't("{label}")' in figures.group(1), f"{label} is drawn outside the figures"
    assert "record && record.head_seq" in figures.group(1)
    # What waited when the vault locked is said only by a page that saw it lock, in either view.
    assert "const waited = form.waited || 0;" in screen
    assert 'waited > 0 ? el("p", { class: "note", id: "locked-waiting", key: "waited" }, ' in screen
    assert 't("locked.status.waited", { n: waited })' in screen
    main = without_comments(scripts()["main.js"])
    seen = function_body(main, "function applyState(next, force = false) {")
    locking = seen[seen.index("if (wasOpen && !isOpen) {") :]
    assert locking.index("forget();") < locking.index("ctx.ui.unlock.waited = waited;")
    assert main.count(".waited = ") == 1
    assert "form.waited = 0;" in function_body(gateway, "async function unlock(ctx) {")


def test_getting_started_stays_away_once_setup_is_done() -> None:
    # A vault whose file says that setup is done does not get the list back when its last
    # document is removed. A vault without the file keeps the list as it was: `stored` is false.
    views = without_comments(scripts()["views.js"])
    card = function_body(views, "function startCard(ctx) {")
    pinned = "if (!data || ctx.state.demo) return null;"
    added = (
        "if (ctx.state.interface && ctx.state.interface.stored && "
        'ctx.state.interface.setup === "done") return null;'
    )
    assert card.index(pinned) < card.index(added) < card.index("const steps = [")


def test_the_vault_page_says_where_the_view_is_kept() -> None:
    manage = without_comments(scripts()["manage.js"])
    page = function_body(manage, "export function vaultView(ctx) {")
    record = page.index('fact("Last entry of the record"')
    # The page keeps a third word there from the moment the owner chooses a language.
    kept = page.index(
        'fact("Kept beside the vault, not sealed", '
        '"Your language, the page it opens on, and whether the first steps are done")'
    )
    assert record < kept < page.index('{ key: "location" }')


def test_the_details_fold_the_record_on_every_page_that_is_not_one_of_the_six() -> None:
    views = without_comments(scripts()["views.js"])
    body = function_body(views, "function panelBody(ctx, panel) {")
    assert "const folded = !onProPage(ctx);" in body
    assert 'return String(ctx.route).startsWith("pro/");' in function_body(
        views, "function onProPage(ctx) {"
    )
    # Folded, the panel is drawn by the function that says everything in the language of the
    # page; the six pages show the record's own figures at once, as before.
    assert "if (folded) return simplePanelBody(ctx, panel, item);" in body
    assert "recordBlocks(item)," in body[body.index("if (folded)") :]
    simple = function_body(views, "function simplePanelBody(ctx, panel, item) {")
    fold = function_body(views, "function recordFold(ctx, panel, item) {")
    assert "const open = ctx.ui.panelMore === true;" in fold
    button = fold[fold.index('button(t("panel.button.record"), {') :]
    assert 'id: "panel-more",' in button[:200]
    assert 'expanded: open ? "true" : "false",' in button[:200]
    assert "open ? recordBlocks(item) : null," in fold
    assert fold.count("recordBlocks(item)") == 1
    assert "recordBlocks(" not in simple
    # What left, where it went and that it cannot be taken back are never behind the button:
    # they are drawn before it, by one function for every page.
    assert simple.index("leftBlocks(item, ") < simple.index("recordFold(ctx, panel, item),")
    left = function_body(views, "function leftBlocks(item, figure) {")
    # (Their texts are keys of the tables: "The exact text that left", "This text is with ...
    # now.", "... cannot take it back.", "Says it is ..., unverified".)
    for shown in (
        't("panel.label.text")',
        't("panel.say.1", { destination: item.destination })',
        't("panel.say.2")',
        't("panel.say.1.local")',
    ):
        assert shown in left, shown
    claimed = 't("card.status.claimed", { claimed: item.claimed })'
    assert 0 < simple.index(claimed) < simple.index("recordFold(")
    assert claimed in body
    # On a simple page the title is the row's sentence, and nothing of the panel but the
    # record's own figures is English: no chip, no note and no sentence of the six pages.
    assert "rowSentence(item, passagesOf(ctx, item))" in simple
    for english in ("statusChip(", "deed(", "REFUSAL_TEXT", "VARIANT_TEXT", "QUESTION_TEXT"):
        assert english not in simple, english
    assert sentences_of(simple) == []
    assert sentences_of(left) == []
    assert 't("panel.status.nothing")' in simple
    assert 't("panel.status.count", { n: item.count })' in simple
    blocks = function_body(views, "function recordBlocks(item) {")
    assert '"Entry"' in blocks
    assert '"Chain"' in blocks
    # Each entry starts folded, and opening or closing the details is told to the cards.
    main = without_comments(scripts()["main.js"])
    assert "ctx.ui.panelMore = false;" in function_body(main, "function openPanel(seq, ")
    basic = without_comments(scripts()["basic.js"])
    around = basic[basic.index("const around = [") :]
    assert 'ctx.panel ? "panel" : "",' in around[: around.index("].join(")]


# -- setting up: three steps that are addresses -------------------------------------


def test_the_first_run_lives_in_the_address_and_the_vault_not_in_the_page() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    for name, code in sources.items():
        assert "onboarding" not in code, f"{name} keeps the first run in the page's memory"
    main = sources["main.js"]
    assert 'const SETUP_STEPS = ["app", "documents", "connect"];' in main
    route = function_body(main, "function routeFromHash() {")
    assert 'ctx.setupStep = SETUP_STEPS.includes(step) ? step : "app";' in route
    assert route.index('return "setup";') < route.index("return defaultRoute();")
    screen = function_body(main, "function screen() {")
    step = 'if (ctx.route === "setup") return setupScreen(ctx, loadProblem());'
    assert screen.index("return lockedScreen(ctx);") < screen.index(step)
    assert screen.index(step) < screen.index('if (ctx.route.startsWith("pro/"))')
    # What a step loads, and what the step "First question" loads besides: the calls of its app,
    # asked for by the app's name.
    assert 'setup: ["apps", "approvals", "settings", "documents", "stops"],' in main
    needs = function_body(main, "function needs() {")
    assert (
        'if (ctx.route === "setup" && ctx.setupStep === "connect") '
        'return NEEDS.setup.concat("asked");'
    ) in needs
    asked = function_body(main, "async function loadAsked() {")
    assert "const page = await api.activity({ limit: ASKED_FIRST, app });" in asked
    # A step that opens has cards that appear anew, an empty form and its title in focus.
    opened = function_body(main, "function onRoute() {")
    assert "openStep(ctx);" in opened
    gateway = sources["gateway.js"]
    opening = function_body(gateway, "export function openStep(ctx) {")
    assert "ctx.ui.setup = freshFirstRun().setup;" in opening
    assert 'ctx.ui.focusId = "setup-title";' in opening
    # After "Create my vault" the address names the step that adds an app, or Overview of the
    # six pages.
    created = function_body(gateway, "async function createVault(ctx) {")
    assert (
        'ctx.navigate(state.interface && state.interface.view === "basic" ? '
        '"setup/app" : "pro/overview");'
    ) in created
    assert created.index("ctx.navigate(") < created.index("ctx.applyState(state);")
    # Four parts, and pairing is not one of them. A part carries a check only when it is true of
    # the vault.
    assert 'const STEPS = ["Passphrase", "App", "Documents", "First question"];' in gateway
    stepper = function_body(gateway, "function stepper(current, done = []) {")
    assert "const checked = done[index] === true && !here;" in stepper
    head = function_body(gateway, "function stepsHead(ctx, current) {")
    assert "return stepper(current, [true, facts.app, facts.document, facts.asked]);" in head
    assert "if (!ctx.ui.steps.guided) return null;" in head
    # The cards keep the keys the walk-throughs know; the card that only showed a configuration
    # and the three-way choice of access are gone.
    for key in ('key: "step-3"', 'key: "step-2"', 'key: "step-4"'):
        assert key in gateway, key
    for gone in ("step-3-done", "Open the vault", "goes-to", "connectBlocks(", "connectTitle("):
        assert gone not in gateway, gone
    frame = function_body(gateway, 'export function setupScreen(ctx, problem = "") {')
    assert '"You can stop at any point. What you have done stays. ",' in frame
    assert '"Set everything up yourself in Pro",' in frame
    assert "bar: problemBar(ctx, problem)," in frame
    # The foot link has the app open on the six pages, through the one function that keeps
    # that choice.
    pro = function_body(main, "async function setUpInPro() {")
    assert 'if (!known && !(await keepView("pro"))) return;' in pro
    assert 'navigate("pro/overview");' in pro


def test_the_app_step_ticks_nothing_and_gives_ask_me_each_time() -> None:
    gateway = without_comments(scripts()["gateway.js"])
    fresh = function_body(gateway, "export function freshFirstRun() {")
    assert "setup: { choice: null, " in fresh
    step = function_body(gateway, "function appStep(ctx) {")
    assert 'name: "first-app-known",' in step
    # A row is ticked when it is the owner's choice, and for no other reason. Only the press on
    # a row makes a choice.
    assert re.findall(r"\bchecked: ([^,\n]+),", step) == ["form.choice === entry.id"]
    assert gateway.count("form.choice = ") == 1
    assert "form.choice = entry.id;" in function_body(gateway, "function chooseApp(ctx, entry) {")
    assert "onchange: () => chooseApp(ctx, entry)," in step
    assert gateway.count("chooseApp(") == 2
    # The rows are the programs that can be connected. "Another app" is one of them only while
    # the vault has no app.
    assert (
        "known.filter((entry) => entry.local_program && "
        '(entry.id !== "generic" || data.apps.length === 0));'
    ) in step
    # What the app may do is stated and not asked: "Ask me each time", for every app.
    assert re.findall(r"api\.setGrant\(([^)]*)\)", gateway) == ['app.name, group, "approve"']
    for absent in ("first-mode", '"read"', "segmented(", "GRANT_LABEL"):
        assert absent not in gateway, absent
    assert 'el("strong", null, "Ask me each time."),' in step
    assert "`Let ${form.name} ask me each time`" in step
    assert '"Choose an app"' in step
    assert "disabled: !ready," in step
    # The size of a passage is the vault's own figure.
    assert "num(settings.ceilings.passage_chars)" in step
    assert re.search(r"(?<![\w.])\d{3,}(?![\w.])", step) is None
    # Where one value would not describe what a new app may do, the step adds none.
    assert "if (data.collections.length > 1 || data.apps.some((app) => !app.hosted)) {" in step
    # A name that is taken is said, and no other name is made up.
    add = function_body(gateway, "async function addFirstApp(ctx) {")
    assert 'form.taken = error.code === "conflict" ? form.name : "";' in add
    assert add.count("api.addApp(") == 1


def test_the_sample_letter_says_that_it_is_invented_and_is_sent_apart() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    manage = sources["manage.js"]
    assert 'file: "invented-sample-letter.txt",' in manage
    assert '"SAMPLE DOCUMENT, INVENTED. It was added to try out the vault. ' in manage
    gateway = sources["gateway.js"]
    send = function_body(gateway, "async function addSample(ctx, group) {")
    assert "const answer = await api.addDocument(group, SAMPLE.file, SAMPLE.text);" in send
    # It is sent by its own function and never through the one that sends the owner's files, so
    # it is not among the files of which the page says that they are still where they were.
    assert gateway.count("api.addDocument(") == 1
    assert "uploadFiles(" not in gateway
    # Never without a press: the one call stands in the handler of its button.
    assert gateway.count("addSample(") == 2
    step = function_body(gateway, "function documentsStep(ctx) {")
    assert "onclick: () => addSample(ctx, group)," in step
    assert 'id: "sample-add",' in step
    # No other script sends the letter.
    for name, code in sources.items():
        if name != "gateway.js":
            assert "SAMPLE.file" not in code, name
    # The step's zone says who can ask for a file, and the step says where text goes.
    assert (
        "dropZone(ctx, group, { large: true, who: true, filled: total === 0 && !refused })," in step
    )
    assert (
        'senders.length ? amber("docs-sentence", senders.length === 1 ? '
        "basicGrantSentence(senders[0]) : generalSentence()) : null,"
    ) in step


def test_no_sentence_about_another_program_is_shown_that_nobody_checked() -> None:
    # The app can write a line for a terminal that opens a settings file in the system's editor.
    # What that line does in another program was not tried with that program, so the page shows
    # neither the line nor a sentence about it: the file is opened in whatever editor the owner has.
    for name, source in scripts().items():
        assert "open_line" not in without_comments(source), f"{name} reads the line"
        shown = "\n".join(quoted_texts(source))
        for word in ("TextEdit", "xdg-open", "pbcopy", "\u2318Q", "\u2318A", "\u2318V", "\u2318S"):
            assert word not in shown, f"{name} says {word!r}"
    gateway = without_comments(scripts()["gateway.js"])
    steps = function_body(gateway, "function fileSteps(ctx, ask, app, connect, block, check) {")
    assert '"Open this file in a plain-text editor:"' in steps
    assert '"Make this file in a plain-text editor:"' in steps
    assert '"Copy the path"' in function_body(gateway, "function pathLine(ctx, path) {")
    # The three places an entry can go in a file that holds other settings.
    for where in (
        "Inside \u201cmcpServers\u201d, after its last entry, type a comma, then paste this:",
        "Between the braces after \u201cmcpServers\u201d, paste this:",
        "Right after the first \u201c{\u201d, paste this, then type a comma:",
        "Replace that entry with this one:",
    ):
        assert where in steps, where


def test_a_settings_file_is_looked_at_only_on_a_press() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    for name, code in sources.items():
        if name not in ("api.js", "gateway.js"):
            assert "checkConnect" not in code, f"{name} looks at a settings file"
    gateway = sources["gateway.js"]
    assert gateway.count("api.checkConnect(") == 1
    look = function_body(gateway, "async function checkFile(ctx, ask, name, host) {")
    assert "ask.check = await api.checkConnect(name, host);" in look
    # The one call stands in the handler of the button, and this module has no timer at all.
    assert gateway.count("checkFile(") == 2
    button = function_body(gateway, "function checkButton(ctx, ask, app, connect, filled) {")
    assert "onclick: () => checkFile(ctx, ask, app.name, connect.host)," in button
    assert "setTimeout" not in gateway
    assert "setInterval" not in gateway
    # What was found is one of the app's words, and each has its sentence.
    found = function_body(gateway, "function foundWords(check, label, block) {")
    for state in (
        "present",
        "no_folder",
        "no_file",
        "empty",
        "other",
        "differs",
        "unreadable",
        "not_read",
    ):
        assert f'case "{state}":' in found, state


def test_the_first_question_says_a_name_and_never_who_asked() -> None:
    gateway = without_comments(scripts()["gateway.js"])
    step = function_body(gateway, "function connectStep(ctx, problem) {")
    waiting = "`Waiting for the first request under the name ${name}. "
    assert waiting + "This page notices it by itself.`" in step
    assert "`A first request arrived under the name ${name} ${at(first, ctx.now)}.`" in step
    assert ", unverified.`" in step
    # A name is all the page knows: no sentence says that a program called or is connected.
    shown = "\n".join(quoted_texts(scripts()["gateway.js"]))
    assert re.search(r"\b(connected|called|calling)\b", shown) is None
    # The cards come from the one function that draws cards, with what stands around them: the
    # words of the line above, a bar, a sheet.
    assert "approvalCards(ctx, { limit: all ? null : 3, around });" in step
    around = step[step.index("const around = [") :]
    around = around[: around.index("].join(")]
    for part in (
        "words,",
        'ctx.offline ? "offline" : problem,',
        'ctx.ui.sheets > 0 ? "sheet" : ""',
    ):
        assert part in around, part
    # Setting up is finished by the first request, never by a release: the three endings have
    # the same buttons, and "Don't release" is one of the endings.
    assert "const endButtons = (seq) =>" in step
    assert step.count("endButtons(") == 3  # the first request, a release that was read, a no
    assert '"Not released"' in step
    assert (
        "`Nothing from that passage left. If ${name} asks again, the passage waits here again.`"
    ) in step
    # While a passage waits no button of the screen is the filled one.
    assert "const filled = anyWaiting === 0;" in step
    # The way out while nothing has come stores nothing.
    later = step[step.index('button("Finish later", {') :]
    assert 'onclick: () => ctx.navigate("home")' in later[:200]


def test_setting_up_ends_by_itself_only_where_the_app_keeps_that_word() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    basic = sources["basic.js"]
    still = function_body(basic, "function setupOpen(ctx) {")
    assert 'return Boolean(kept && kept.stored && kept.setup === "open");' in still
    done = function_body(basic, "export async function setupDone(ctx) {")
    assert done.splitlines()[1].strip() == "if (!setupOpen(ctx)) return null;"
    assert 'ctx.applyState(await api.setInterface({ setup: "done" }));' in done
    # Once the vault has an app, a document and a first request, the page says so once, on
    # whatever screen it is: after each load.
    settle = function_body(basic, "export function settleSetup(ctx) {")
    assert "!setupOpen(ctx) || ctx.ui.setupSaid" in settle
    assert "if (!facts || !facts.app || !facts.document || !facts.asked) return;" in settle
    assert settle.index("ctx.ui.setupSaid = true;") < settle.index("setupDone(ctx);")
    assert "settleSetup(ctx);" in function_body(sources["main.js"], "async function load(names) {")
    # Nobody else keeps that word.
    for name, code in sources.items():
        if name not in ("api.js", "basic.js", "main.js"):
            assert "setInterface" not in code, name
    # The card on the Basic page: never in a demo, one line, and "Hide" ends it.
    card = function_body(basic, "function setupCard(ctx) {")
    assert "if (ctx.state.demo || !setupOpen(ctx)) return null;" in card
    for words in (
        '"Next: choose your AI app."',
        '"Next: add a document."',
        '"Next: ask your AI app a first question."',
        '"Finish setting up"',
        "`${num(done)} of 4 done`",
    ):
        assert words in card, words
    assert 'id: "setup-hide",' in card
    assert "const failed = await setupDone(ctx);" in card
    # Its button is the one filled button of the page, and not while a passage waits.
    assert 'class: ["btn", waits ? "btn-default" : "btn-primary"], id: "setup-continue"' in card
    # An app is added from the Basic page through the step that states its access.
    apps = function_body(basic, "function appsSection(ctx, stops) {")
    assert 'id: "basic-add-app", href: "#/setup/app"' in apps


# -- the demo's guide ---------------------------------------------------------------


def test_the_demo_guide_makes_the_played_app_ask_again_only_on_a_press() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    basic = sources["basic.js"]
    # One request, without a body: nothing tells the played app what the owner decided, so the
    # demo is not better than the product. A real app has to be told "try again" as well.
    assert 'demoAgain: () => call("POST", "/api/demo/again"),' in sources["api.js"]
    for name, code in sources.items():
        if name not in ("api.js", "basic.js"):
            assert "demoAgain" not in code, name
    assert re.findall(r"api\.demoAgain\(([^)]*)\)", basic) == [""]
    ask = function_body(basic, "async function askAgain(ctx) {")
    sent = "await api.demoAgain();"
    assert sent in ask
    # Up to the request the function looks at nothing but whether it is already asking.
    for told in ("decision", "release", "decided", "granted", "waiting", "approvals", "guideBeat"):
        assert told not in ask[: ask.index(sent)], f"asking again reads {told} first"
    # It is sent by a press on the one button, and by nothing else: no timer, no load, no draw.
    assert basic.count("askAgain(") == 2  # where it is defined, and behind the button
    guide = function_body(basic, "function demoGuide(ctx, stops) {")
    assert "onclick: () => askAgain(ctx) })," in guide
    assert guide.count('id: "demo-again"') == 1
    assert basic.count('id: "demo-again"') == 1
    assert "Have the played app ask again" in guide
    # The page has one filled button at most, and the guide has none.
    for filled in ('kind: "primary"', "btn-primary"):
        assert filled not in guide
    # The guide never leads to one answer: after either, the same button.
    words = function_body(basic, "function guideWords(ctx, beat) {")
    for beat, title in (("released", "You released it once"), ("kept", "You did not release it")):
        said = words[words.index(f'if (beat === "{beat}") {{') :]
        said = said[: said.index("\n  }\n")]
        assert f'title: "{title}",' in said
        assert "again: true," in said
    assert "Either answer is fine." in words
    assert words.count("Either answer is fine.") == 2  # one passage waits, or several
    for beat in ("turn", "read"):
        said = words[words.index(f'if (beat === "{beat}") {{') :]
        assert "again: false," in said[: said.index("\n  }\n")]
    assert words.rstrip().endswith("again: false,\n  };")  # nothing was asked for yet: no button


def test_the_demo_guide_reads_the_lists_and_the_owners_own_press() -> None:
    basic = without_comments(scripts()["basic.js"])
    # It is drawn in a demo and nowhere else, under the notices, where the card of setting up is
    # in a vault of one's own. The approval cards stand above it and are complete without it.
    assert "return ctx.state.demo === true && !ctx.ui.guide.hidden;" in function_body(
        basic, "function guideOpen(ctx) {"
    )
    guide = function_body(basic, "function demoGuide(ctx, stops) {")
    assert guide.splitlines()[1].strip() == "if (!guideOpen(ctx)) return null;"
    card = '{ class: "card card-guide", key: "demo-guide", "aria-labelledby": "guide-title" }'
    assert card in guide
    assert 'chip("Demo", "accent"),' in guide
    assert basic.count("demoGuide(") == 2
    page = function_body(basic, "export function basicView(ctx, problem) {")
    order = [
        "approvalsSection(ctx, problem),",
        "warningList(ctx, { bothKinds: true }),",
        "setupCard(ctx),",
        "demoGuide(ctx, stops),",
        "leftSection(ctx, stops),",
    ]
    places = [page.index(part) for part in order]
    assert places == sorted(places)
    assert "approvalCards(" not in guide
    # Where the demo is, it reads from the three lists the page loads anyway and from what the
    # owner pressed on this page. The state says only that this is a demo.
    beat = function_body(basic, "function guideBeat(ctx) {")
    assert "if (!approvals || !left.loaded || !ctx.stops) return null;" in beat
    assert "ctx.ui.decided.values()" in beat
    assert "ctx.state" not in beat
    own = function_body(basic, "function ownVault(ctx) {")
    assert re.findall(r"ctx\.state\.(\w+)", guide + own) == ["launch"]
    # A passage that waits comes first: the guide then says nothing but "Your turn".
    returns = re.findall(r'return "(\w+)";', beat)
    assert returns == ["turn", "read", "released", "kept", "asked", "watch"]
    assert 'if (waiting.length) return "turn";' in beat
    # What it remembers is this page's memory, forgotten with everything else when the vault locks.
    main = without_comments(scripts()["main.js"])
    assert "guide: freshGuide()," in function_body(main, "function freshUi() {")
    assert "ctx.ui = freshUi();" in function_body(main, "function forget() {")
    # "Hide" is that memory too, and it says how the guide comes back.
    assert "guide.hidden = true;" in guide
    assert '"The guide is hidden. It is back when this page is loaded again."' in guide


def test_the_demo_guide_says_its_sentences_word_for_word_and_names_no_time() -> None:
    source = scripts()["basic.js"]
    shown = quoted_texts(source)
    for words in (
        "Watch an app ask for documents",
        "Nothing here is real: the documents are invented, and the apps that ask are played by   "
        "itself. ",
        "Soon one of them asks for a passage that needs your approval; it appears above, under "
        "\u201cApprovals\u201d. ",
        "The rows under \u201cWhat left the vault\u201d are what the played apps were given so "
        "far.",
        "Your turn",
        "A played app asks, under the name  , for the passage above. Read the exact text, see "
        "where it would go, and choose. Either answer is fine.",
        "You released it once",
        "Nothing has been sent yet. A real app now has to ask again; you would tell it "
        "\u201ctry again\u201d. Here this button does that for the played app.",
        "You did not release it",
        "Nothing from that passage left. If an app asks again, the passage waits here again. "
        "This button makes the played app ask again.",
        "It asked again and got the passage",
        "The newest row under \u201cWhat left the vault\u201d shows the exact text that left.",
        "  That is the whole of it: an app asked, you saw the exact text and where it would go, "
        "you decided, and what left is on the record.",
        "Meanwhile a second played app read a letter with an instruction hidden in it and then "
        "asked for every passage it could find. ",
        "Its limit for 24 hours stopped it: see \u201cStopped in the last 24 hours\u201d below.",
        "Have the played app ask again",
        "Use your own documents",
        "Now with your own documents",
        "In the terminal where this demo runs, press Ctrl-C. The demo vault is deleted.",
        "Run this:",
        "The page that opens asks for a pairing code, which the terminal shows, and then for a "
        "passphrase.",
        "Have ready: a passphrase you can type from memory (it cannot be recovered), one "
        "document, and the AI app you use.",
    ):
        assert words in shown, words
    # The name in "Your turn" is the stored name of the app whose passage waits, read from the list.
    basic = without_comments(source)
    words = function_body(basic, "function guideWords(ctx, beat) {")
    assert "under the name ${waiting[0].app}, for the passage above." in words
    # The sentence about the second app is said only while the page shows the stop it speaks of.
    guide = function_body(basic, "function demoGuide(ctx, stops) {")
    assert 'decided && stops.some((stop) => stop.kind === "client-day")' in guide
    # No sentence of the guide names a time: when the played app asks is not the page's to promise.
    own = function_body(basic, "function ownVault(ctx) {")
    sentences = [text for text in quoted_texts(words + guide + own) if " " in text]
    assert len(sentences) > 20
    for text in sentences:
        # ("a second played app" counts apps, not time.)
        assert re.search(r"\b(seconds|minutes?|moments?)\b", text) is None, text
        # The one figure is the name of a limit, in the words the page uses for it elsewhere.
        assert re.search(r"\d", text.replace("24 hours", "")) is None, text
    # The command is the one the app sent, which in a demo starts a vault of one's own; the
    # sentence after it says "the page that opens" only where that command opens one.
    assert "const command = (ctx.state.launch || {}).command;" in own
    assert 'copyLine(ctx, "guide-copy", command,' in own
    assert "!command || opensPage(command)" in own
    assert "onclick" not in own, "a button of the last block starts something"
    # The foot leaves the command out in a demo: there it would not open this vault again.
    launch = function_body(basic, "export function launchBlock(ctx) {")
    assert "const command = ctx.state.demo ? null : launch.command;" in launch


def test_the_lower_parts_are_folded_only_while_the_demo_guide_is_open() -> None:
    basic = without_comments(scripts()["basic.js"])
    fold = function_body(basic, "function foldOf(ctx, name) {")
    assert fold.splitlines()[1].strip() == "if (!guideOpen(ctx)) return null;"
    assert "id: `fold-${name}`," in fold
    # (Documents, the third part that folded, has a page of its own now.)
    assert basic.count("foldOf(") == 3  # where it is defined, and in the two parts
    assert "documentsSection" not in basic
    for head, name in (
        ("function appsSection(ctx, stops) {", "apps"),
        ("function alsoSection(ctx) {", "also"),
    ):
        part = function_body(basic, head)
        assert f'const fold = foldOf(ctx, "{name}");' in part, name
        assert "if (fold && !fold.open) return section(" in part, name
        assert part.rstrip().endswith("], fold);"), name
    # "Also set for this vault, in Pro" has a heading only when it has a line, folded or not.
    also = function_body(basic, "function alsoSection(ctx) {")
    assert also.index("if (!pages.length) return null;") < also.index("foldOf(")
    # A part that folds is a button that says whether it is open, never a <details>.
    part = function_body(basic, "function section(key, title, aside, content, fold = null) {")
    assert 'expanded: fold.open ? "true" : "false", onclick: fold.toggle' in part
    assert "fold && !fold.open ? null : content," in part
    for name, source in scripts().items():
        assert '"details"' not in without_comments(source), name
    # "What left the vault" is not among them: it holds the rows and the stops the guide speaks of.
    assert "foldOf(" not in function_body(basic, "function leftSection(ctx, stops) {")


# -- the simple frame and its plain pages -------------------------------------------


def test_the_simple_scripts_hold_no_sentence() -> None:
    # The plain pages take every text from the tables: their script holds no quoted text that
    # a person would read as words, and no label either. What it quotes are keys, classes,
    # addresses, ids and names of the code. (gateway.js joins this test when its three steps
    # of setting up, which keep their English until Home replaces them, are gone.)
    pages = without_comments(scripts()["pages.js"])
    assert sentences_of(pages) == []
    for text in quoted_texts(pages):
        assert re.match(r"[A-Z][a-z]+( [a-z]+)*$", text.strip()) is None, text
    assert re.search(r'import \{[^}]*\bt\b[^}]*\} from "\./texts\.js";', pages)
    assert 'from "./brand.js"' not in pages, "the product's name is a place of the tables"
    # The frame of these pages holds none either: see ON_THE_TABLE for its two functions.
    main = without_comments(scripts()["main.js"])
    nav = re.search(r"const NAV = \[(.*?)\n\];", main, re.S)
    assert nav is not None
    assert re.findall(r'\["(\w+)", "([\w.]+)", "(\w+)"\],', nav.group(1)) == [
        ("home", "nav.home", "home"),
        ("documents", "nav.documents", "documents"),
        ("activity", "nav.activity", "activity"),
        ("settings", "nav.settings", "gear"),
    ]
    # Each glyph the frames and the pages name is one of the set.
    dom = without_comments(scripts()["dom.js"])
    for glyph in ("home", "gear", "plug", "documents", "activity", "right", "lock"):
        assert re.search(rf"^  {glyph}: \{{", dom, re.M), glyph


def test_a_row_of_activity_is_made_from_fields_of_the_entry() -> None:
    # The app's own sentence of an entry is English. A row of the simple pages, and the title
    # of its details, is worded from the entry's fields in the language of the page.
    pages = without_comments(scripts()["pages.js"])
    assert "summary" not in pages
    sentence = function_body(pages, "export function rowSentence(item, passages = null) {")
    assert 'if (item.kind === "refusal") return t(stopKey(item), { app });' in sentence
    for key in ("row.search.plain", "row.search", "row.search.none", "row.read", "row.read.plain"):
        assert f't("{key}"' in sentence, key
    # How many results a search had is a field of the entry; without it the row says no number.
    assert (
        'if (typeof item.results !== "number") return t("row.search.plain", { app });' in sentence
    )
    # A stop is worded by the limit that stopped the call, else by the code of the answer,
    # else as "refused"; the two limits that apps of a kind share have one text; a call under
    # a name that is not answered names no app.
    limits = re.search(r"const STOPPED_BY_LIMIT = \{(.*?)\};", pages, re.S)
    codes = re.search(r"const STOPPED_BY_CODE = \{(.*?)\};", pages, re.S)
    assert limits is not None
    assert codes is not None
    assert dict(re.findall(r'"?([\w-]+)"?: "([^"]+)"', limits.group(1))) == {
        "client-day": "row.stop.client-day",
        "client-30-days": "row.stop.client-30-days",
        "pool-day": "row.stop.shared",
        "pool-30-days": "row.stop.shared",
        "searches": "row.stop.searches",
        "calls": "row.stop.calls",
    }
    assert dict(re.findall(r'(\w+): "([^"]+)"', codes.group(1))) == {
        code: f"row.stop.{code}"
        for code in ("NEEDS_APPROVAL", "NOT_FOUND", "BAD_REQUEST", "UNAVAILABLE")
    }
    stop = function_body(pages, "function stopKey(item) {")
    assert (
        stop.splitlines()[2].strip() == 'if (item.app === UNANSWERED) return "row.stop.unanswered";'
    )
    assert stop.rstrip().endswith('table[word] : "row.stop.other";')
    # The two lists are the two kinds the app is asked for, twenty rows at a time, and a press
    # on a row opens the details of its entry.
    page = function_body(pages, "export function activityPage(ctx) {")
    assert re.findall(r'\{ value: "(\w+)", label: t\("([^"]+)"\) \}', page) == [
        ("release", "activity.label.left"),
        ("refusal", "activity.label.stopped"),
    ]
    assert "onchange: (tab) => ctx.showRows(tab)," in page
    assert "onclick: () => ctx.moreRows()" in page
    assert 'href: "#/pro/activity" }, t("activity.button.full")' in page
    row = function_body(pages, "export function activityRow(ctx, item) {")
    assert "onclick: (event) => ctx.openPanel(item.seq, event.detail === 0)," in row
    main = without_comments(scripts()["main.js"])
    more = function_body(main, "async function moreRows() {")
    assert "before: rows.order[rows.order.length - 1] }" in more
    assert "kind: tab," in more


def test_a_refused_file_is_worded_by_the_word_of_the_answer() -> None:
    # Why the app did not take a file is one word of its answer; the page has a text for each
    # word the app can send, and shows the app's own English sentence only for a refusal
    # without a word, or with one it does not know.
    from wdva.gate import extract

    manage = without_comments(scripts()["manage.js"])
    listed = re.search(r"const REFUSED = \{(.*?)\};", manage, re.S)
    assert listed is not None
    keys = dict(re.findall(r'(\w+): "([^"]+)"', listed.group(1)))
    assert keys == {
        "unsupported": "docs.error.type",
        "empty": "docs.error.empty",
        "no_text": "docs.error.notext",
        "not_utf8": "docs.error.encoding",
        "no_pdf_support": "docs.error.pdf",
        "pdf_encrypted": "docs.error.password",
        "pdf_no_text": "docs.error.scan",
        "pdf_unreadable": "docs.error.unreadable",
        "pdf_too_much": "docs.error.toomuch",
        "pdf_too_slow": "docs.error.slow",
    }
    words = function_body(manage, "function refusedWords(error, rules) {")
    assert 'if (error.code === "duplicate") return () => t("docs.error.duplicate");' in words
    assert 'if (reason === "too_large") return () => t("docs.error.large", ' in words
    assert "return () => t(REFUSED[reason]);" in words
    assert words.rstrip().endswith(
        'return () => t("docs.error.other", { reason: errorText(error) });'
    )
    # The word counts only beside the code it belongs to.
    assert 'const reason = error.code === "invalid" && typeof error.reason === "string"' in words
    # Every word the app has is one the page words.
    assert set(extract.REFUSALS) == set(keys) | {"too_large"}
    english = scripts()["text-en.js"]
    for key in [*keys.values(), "docs.error.large", "docs.error.other"]:
        assert f'  "{key}": "Not added: ' in english, key
    # A Word file: what to do about it is said with the refusal.
    assert '"docs.error.type": "Not added: only PDF and text files can be read. ' in english
    assert "Save a Word file as PDF and add that." in english
    # The command that adds PDF reading is the app's own, shown under the files once, with "Copy".
    zone = function_body(manage, "export function dropZone(ctx, collection, options = {}) {")
    assert (
        "const install = state.items.some((item) => item.install) && "
        'typeof rules.install === "string" ? rules.install : "";'
    ) in zone
    assert 'copyLine(ctx, `install-copy-${collection}`, install, t("docs.label.install")),' in zone
    assert "pip install" not in manage
    upload = function_body(manage, "export async function uploadFiles(ctx, collection, files) {")
    assert "item.install = needsPdfSupport(error);" in upload
    api = without_comments(scripts()["api.js"])
    assert "this.reason = reason;" in api


# -- two languages ----------------------------------------------------------------


def test_only_texts_js_reads_the_tables() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    for name, code in sources.items():
        reads = re.findall(r'from "\./(text-[a-z]+)\.js"', code)
        if name == "texts.js":
            assert sorted(reads) == ["text-en", "text-pl"]
        else:
            assert reads == [], f"{name} reads a table itself"
    # Whoever asks for a text asks the one function, and got it from that module.
    for name, code in sources.items():
        if name != "texts.js" and re.search(r"(?<![\w.])t\(", code):
            imported = re.search(r'import \{([^}]*)\} from "\./texts\.js";', code)
            assert imported is not None, f"{name} asks for texts"
            assert "t" in [word.strip() for word in imported.group(1).split(",")], name
    reader = sources["texts.js"]
    assert sorted(re.findall(r'from "\./([a-z-]+)\.js"', reader)) == [
        "brand",
        "format",
        "text-en",
        "text-pl",
    ]
    assert 'export const LANGUAGES = ["en", "pl"];' in reader
    for exported in (
        "language()",
        "setLanguage(code)",
        "firstLanguage(listed)",
        "t(key, values = {})",
    ):
        assert f"export function {exported} {{" in reader, exported
    # A key the Polish table lacks is shown in English, one that both lack as the key itself;
    # the product's name is put in from brand.js and stands in no table.
    assert "own.call(TABLES[shown], key) ? TABLES[shown][key] : own.call(ENGLISH, key)" in reader
    assert "return `⟦${key}⟧`;" in reader
    assert 'if (name === "product") return PRODUCT;' in reader
    # Numbers and lists are written by the rules of the language, through format.js.
    assert 'if (typeof value === "number") return num(value);' in reader
    assert "if (Array.isArray(value)) return list(value);" in reader


def test_the_language_is_kept_by_the_app_and_not_in_the_browser() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    for name, code in sources.items():
        assert "localStorage" not in code, name
        if name != "api.js":
            assert "sessionStorage" not in code, name
    # What the owner pressed is a variable of texts.js: gone when the page is loaded again.
    reader = sources["texts.js"]
    assert "let pressed = null;" in reader
    choose = function_body(reader, "export function chooseLanguage(code) {")
    assert "pressed = code;" in choose
    assert "setLanguage(code);" in choose
    assert reader.count("pressed = ") == 2  # where it is made, and the press
    # Which language is shown: the press; else what the app keeps; else the browser's.
    kept = function_body(reader, "export function keptLanguage(code) {")
    assert kept.splitlines()[1].strip() == "if (pressed !== null) return;"
    assert "LANGUAGES.includes(code) ? code : browserLanguage()" in kept
    assert "setLanguage(browserLanguage());" in reader
    first = function_body(reader, "export function firstLanguage(listed) {")
    assert "/^(pl|en)(-|$)/i" in first
    assert 'return "en";' in first
    assert "document.documentElement.lang = code;" in function_body(
        reader, "export function setLanguage(code) {"
    )
    # The app is asked to keep it from the switch, once a vault exists and the page is paired;
    # before that the press goes along with the passphrase of the new vault.
    main = sources["main.js"]
    press = function_body(main, "function pressLanguage(code) {")
    assert press.index("chooseLanguage(code);") < press.index("keepLanguage();")
    keep = function_body(main, "async function keepLanguage() {")
    assert 'ctx.phase !== "ready" || !state || !state.store.exists' in keep
    assert "api.setInterface({ language: wanted })" in keep
    assert main.count("api.setInterface({ language") == 1
    for name, code in sources.items():
        if name != "main.js":
            assert "setInterface({ language" not in code, name
    assert "await api.createStore(form.passphrase, chosenLanguage());" in sources["gateway.js"]
    # What the app keeps is shown: the session's answer says it before pairing, the state after.
    assert "keptLanguage(session.language);" in function_body(main, "async function start() {")
    seen = function_body(main, "function applyState(next, force = false) {")
    assert "if (next.interface) keptLanguage(next.interface.language);" in seen
    # One such request at a time: the app writes all its words at each, so two at once could
    # lose one.
    api = sources["api.js"]
    assert 'const sent = interfaceSent.then(() => call("POST", "/api/interface", { body }));' in api


def test_the_switch_stands_on_every_screen_before_the_vault_is_open_and_in_both_frames() -> None:
    parts = without_comments(scripts()["parts.js"])
    switch = function_body(
        parts, "export function languageSwitch(choose, { short = false } = {}) {"
    )
    # Two words, the one that is shown marked as pressed; the words are the same in both tables.
    assert "LANGUAGES.map((code) =>" in switch
    assert '"aria-pressed": code === shown ? "true" : "false",' in switch
    assert '"aria-label": t(LANGUAGE_WORD[code]),' in switch
    assert 'role: "group", "aria-label": t("common.label.language")' in switch
    assert "onclick: () => choose(code)," in switch
    assert 'const LANGUAGE_WORD = { en: "common.lang.en", pl: "common.lang.pl" };' in parts
    for name in ("text-en.js", "text-pl.js"):
        table = scripts()[name]
        assert '"common.lang.en": "English",' in table, name
        assert '"common.lang.pl": "Polski",' in table, name
    # Top right of the four screens (and of a step of setting up), through their one frame.
    gateway = without_comments(scripts()["gateway.js"])
    frame = function_body(gateway, "function gateScreen(ctx, key, content, options = {}) {")
    assert (
        'el("div", { class: "gate-lang", key: "lang" }, languageSwitch(ctx.chooseLanguage)),'
        in frame
    )
    for head in (
        "export function offlineScreen(ctx) {",
        "export function pairingScreen(ctx) {",
        "export function lockedScreen(ctx) {",
        "export function createScreen(ctx) {",
    ):
        assert "return gateScreen(" in function_body(gateway, head), head
    assert gateway.count("languageSwitch(") == 1
    # In both frames of an open vault it stands at the foot of the sidebar, above what is said
    # about the lock. In the simple frame at 720 px and narrower that foot is part of the top
    # bar, and the stylesheet shows the two letters in place of the words.
    main = without_comments(scripts()["main.js"])
    assert "chooseLanguage: pressLanguage," in main
    foot = function_body(main, "function sidebar() {")
    assert foot.index("languageSwitch(pressLanguage),") < foot.index('class: ["lock-state",')
    side = function_body(main, "function simpleSidebar() {")
    assert side.index("languageSwitch(pressLanguage),") < side.index('class: ["side-lock",')
    assert main.count("languageSwitch(pressLanguage)") == 3
    narrow = without_comments(read(STATIC / "home.css")).split("@media (max-width: 720px) {")[1]
    assert re.search(r"\.side-foot \.lang-word \{\s*display: none;", narrow)
    assert re.search(r"\.side-foot \.lang-code \{\s*display: inline;", narrow)
    assert "languageSwitch(pressLanguage)" in function_body(main, "function stuckScreen() {")
    # And on the page Settings, with its two words in full.
    settings = function_body(
        without_comments(scripts()["pages.js"]), "export function settingsPage(ctx) {"
    )
    assert "languageSwitch(ctx.chooseLanguage)," in settings
    # The switch is laid out by its own rules and is no part of another choice.
    assert "segmented" not in switch
    assert [bool(rule) for rule in css_rules("app.css", ".gate-lang")] == [True]
    assert ["position: absolute" in rule for rule in css_rules("app.css", ".gate-lang")] == [True]


def test_the_eighth_character_of_the_code_pairs() -> None:
    gateway = without_comments(scripts()["gateway.js"])
    screen = function_body(gateway, "export function pairingScreen(ctx) {")
    typed = screen[screen.index("oninput: (event) => {") :]
    typed = typed[: typed.index("\n                },")]
    # Whatever is typed is tidied, and then the one function that pairs is asked: it sends the
    # code once all eight characters are there, so "Pair" is no press of its own on the way in.
    assert typed.index("form.code = tidyCode(event.target.value);") < typed.index("pair(ctx);")
    assert 'return code.replace("-", "").length === 8;' in function_body(
        gateway, "function complete(code) {"
    )
    send = function_body(gateway, "async function pair(ctx) {")
    guard = "if (form.busy || !complete(code) || code === form.refused) return;"
    assert guard in send
    assert send.index(guard) < send.index("await api.pair(code);")
    assert gateway.count("api.pair(") == 1
    # A code that was refused is not sent again as it stands, by a key or by the button: five
    # wrong codes switch pairing off. Nothing is emptied.
    refused = send[send.index('else if (error.code === "wrong_code") {') :]
    assert "form.refused = code;" in refused[: refused.index("}")]
    assert 'form.code = "";' not in send[send.index("} catch (error) {") :]
    assert "disabled: !complete(form.code) || form.code === form.refused," in screen
    assert 'if (error.code === "pairing_disabled") form.disabled = true;' in send
    assert 'form.disabled\n        ? note("red", t("pair.error.disabled"))' in screen
    # The module has no timer: nothing is sent later, or again.
    assert "setTimeout" not in gateway
    assert "setInterval" not in gateway


# The functions that draw what every view shares, and the four screens before the vault is
# open. Each takes every text from the text tables.
ON_THE_TABLE = {
    "gateway.js": (
        "export function offlineScreen(ctx) {",
        "async function pair(ctx) {",
        "export function pairingScreen(ctx) {",
        "async function unlock(ctx) {",
        "export function lockedScreen(ctx) {",
        "async function createVault(ctx) {",
        "export function createScreen(ctx) {",
    ),
    "views.js": (
        "async function decide(ctx, item, release, event) {",
        "function decidedWords(ctx, done) {",
        "function decidedLine(ctx, done) {",
        "function whyItWaits(item) {",
        "function approvalCard(ctx, item, index) {",
        "function recordFold(ctx, panel, item) {",
        "function leftBlocks(item, figure) {",
        "function simplePanelBody(ctx, panel, item) {",
    ),
    "sheets.js": (
        "export function requestReauth(demoPassphrase = null) {",
        "export function generalSentence() {",
        "export function basicGrantSentence(app) {",
    ),
    "manage.js": (
        "export function loadingBlock() {",
        "function refusedWords(error, rules) {",
        "function needsPdfSupport(error) {",
        "export async function uploadFiles(ctx, collection, files) {",
        "export function whoLine(ctx, collection) {",
        "export function dropZone(ctx, collection, options = {}) {",
        "export async function removeDocument(ctx, document) {",
        "export function cannotList() {",
        "export function versionLine(state) {",
    ),
    "parts.js": (
        "export function demoPassphraseNote(passphrase, fill) {",
        "export function languageSwitch(choose, { short = false } = {}) {",
        "export function errorText(error, own = {}) {",
        "export function toastError(error) {",
        "export function copyButton(ctx, id, text, what, words = null) {",
        "export function copyLine(ctx, id, text, what) {",
        "export function problemBar(ctx, problem) {",
    ),
    "main.js": (
        "function stuckScreen() {",
        "function simpleSidebar() {",
        "function simpleShell() {",
    ),
    "pages.js": (
        "function whereTextGoes(readers) {",
        "function fileNames(files) {",
        "async function addToWhichGroup(ctx, files) {",
        "function documentRow(ctx, entry, several) {",
        "export function documentsPage(ctx) {",
        "function stopKey(item) {",
        "function passageOf(item, passages) {",
        "export function rowSentence(item, passages = null) {",
        "export function activityRow(ctx, item) {",
        "export function activityPage(ctx) {",
        "export function settingsPage(ctx) {",
        "export function howPage(ctx) {",
    ),
}


def sentences_of(code: str) -> list[str]:
    """The quoted texts of some code that a person would read as words: three words or more,
    and not a list of classes."""
    return [
        text
        for text in quoted_texts(code)
        if len(text.split()) > 2 and not re.fullmatch(r"[a-z0-9 -]+", text.strip())
    ]


def test_the_parts_every_view_shares_hold_no_sentence_of_their_own() -> None:
    for name, heads in ON_THE_TABLE.items():
        code = without_comments(scripts()[name])
        for head in heads:
            body = function_body(code, head)
            assert sentences_of(body) == [], f"{name}, {head}"
            # Nor a label: every quoted text with a capital is a tag, a key or a name of the code.
            for text in quoted_texts(body):
                if re.match(r"[A-Z][a-z]+( [a-z]+)*$", text.strip()):
                    assert text.strip() in ("POST", "Files"), f"{name}, {head}: {text!r}"
    assert sentences_of(
        'toast("The vault is locked."); el("p", { class: "note note-plain x" })'
    ) == ["The vault is locked."]
    # What went wrong is worded by the page for the codes it knows, in the place that knows them.
    gateway = without_comments(scripts()["gateway.js"])
    assert (
        "errorText(form.failed, "
        '{ wrong_passphrase: "locked.error.wrong", busy: "common.error.busy" })'
    ) in function_body(gateway, "export function lockedScreen(ctx) {")
    assert 'form.wrong ? t("pair.error.wrong")' in function_body(
        gateway, "export function pairingScreen(ctx) {"
    )
    sheet = function_body(
        without_comments(scripts()["sheets.js"]),
        "export function requestReauth(demoPassphrase = null) {",
    )
    assert 'errorText(form.failed, { wrong_passphrase: "reauth.error.wrong" })' in sheet
    parts = without_comments(scripts()["parts.js"])
    everywhere = re.search(r"const WORDED = \{(.*?)\};", parts, re.S)
    assert everywhere is not None
    assert dict(re.findall(r'(\w+): "([^"]+)"', everywhere.group(1))) == {
        "locked": "nav.result.locked",
        "unavailable": "common.error.unavailable",
        "cancelled": "common.error.cancelled",
        "offline": "offline.title",
    }
    # Any other code is said in the app's own sentence.
    words = function_body(parts, "export function errorText(error, own = {}) {")
    assert 'return error && error.message ? error.message : t("common.error.unknown");' in words
    card = function_body(
        without_comments(scripts()["views.js"]), "function approvalCard(ctx, item, index) {"
    )
    assert 'changed ? note("amber", t("card.result.changed")) : null,' in card


def test_numbers_and_times_are_written_by_the_rules_of_the_language() -> None:
    source = without_comments(scripts()["format.js"])
    assert "import" not in source, "format.js imports nothing: texts.js tells it the language"
    assert "export function useLanguage(code, words = {}) {" in source
    told = function_body(
        without_comments(scripts()["texts.js"]), "export function setLanguage(code) {"
    )
    assert 'useLanguage(code, { justNow: TABLES[code]["common.status.justnow"] });' in told
    for made in (
        "new Intl.NumberFormat(tag",
        "new Intl.DateTimeFormat(tag",
        "new Intl.RelativeTimeFormat(tag",
        "new Intl.ListFormat(tag",
    ):
        assert made in source, made
    # Polish, or the browser's own kind of English; where the browser has neither, British
    # English, whose dates read "16 Nov, 18:00".
    assert 'tag = code === "pl" ? "pl" : english();' in source
    assert 'return "en-GB";' in function_body(source, "function english() {")
    assert "en-US" not in source
    # How long ago, by three rules: under a minute the words of the table; minutes and hours
    # always with their number, which is never 0; from a day on in days.
    since = function_body(source, "function since(moment, now, style) {")
    assert "if (seconds < 60) return justNow;" in since
    assert 'relative("always", style).format(-Math.floor(seconds / 60), "minute");' in since
    assert 'relative("always", style).format(-Math.floor(seconds / 3600), "hour");' in since
    assert 'relative("auto", style).format(-days, "day");' in since
    assert (
        since.index("seconds < 60") < since.index("seconds < 3600") < since.index("seconds < 86400")
    )
    assert 'since(moment, now, "long")' in function_body(
        source, "export function ago(text, now = new Date()) {"
    )
    assert 'since(moment, now, "short")' in function_body(
        source, "export function agoShort(text, now = new Date()) {"
    )
    # A date inside a Polish sentence is written in numbers: a month's name would need an ending.
    clock = function_body(source, "export function clock(text, now = new Date()) {")
    assert 'const month = tag === "pl" ? "numeric" : "short";' in clock
    # A list is joined by the rule of the language, a size carries the unit of the language.
    assert 'new Intl.ListFormat(tag, { type: "conjunction" })' in source
    assert 'style: "unit"' in source
    # Every function the pages use is still there, under its name.
    for name in (
        "num",
        "count",
        "percent",
        "bytes",
        "megabytes",
        "parseTime",
        "when",
        "whenFull",
        "ago",
        "agoShort",
        "dayShort",
        "at",
        "untilShort",
        "list",
        "sentence",
    ):
        assert re.search(rf"^export function {name}\(", source, re.M), name


# -- what the interface says ------------------------------------------------------


def test_the_sentences_for_decisions_are_there_word_for_word() -> None:
    everything = "\n".join(
        re.sub(r'"\s*\+\s*\n?\s*"', "", without_comments(source)) for source in scripts().values()
    )
    everything = everything.replace("`", '"')
    for sentence in REQUIRED_SENTENCES:
        assert sentence in everything, f"missing: {sentence}"


def test_grant_labels_are_the_three_the_owner_knows() -> None:
    sheets = without_comments(scripts()["sheets.js"])
    assert (
        'GRANT_LABEL = { none: "No access", read: "Can read", approve: "Ask me each time" }'
        in sheets
    )


def shown_texts() -> dict[str, str]:
    """What a person can read, per file: the quoted text of the scripts and the text of the page."""
    texts = {name: "\n".join(quoted_texts(source)) for name, source in scripts().items()}
    texts["index.html"] = re.sub(r"<[^>]+>", " ", read(STATIC / "index.html"))
    return texts


def test_no_claim_the_gate_cannot_back() -> None:
    for name, text in shown_texts().items():
        for claim in FORBIDDEN_CLAIMS:
            found = re.search(claim, text, re.I)
            assert found is None, f"{name} says {found.group(0) if found else ''!r}"
    assert re.search(FORBIDDEN_CLAIMS[0], "Your documents never leave your computer.", re.I)


def test_the_owner_reads_the_name_of_the_product_and_not_the_gate() -> None:
    # "The gate" is what the code calls its engine. The owner reads the product's name,
    # which every script takes from brand.js.
    word = re.compile(r"\bthe gate\b|\bgate['\u2019]s\b", re.I)
    for name, text in shown_texts().items():
        found = word.search(text)
        assert found is None, (
            f"{name} says {text[max(0, found.start() - 40) : found.end() + 40] if found else ''!r}"
        )
    assert word.search("The gate cannot take it back.")
    assert word.search("under the gate\u2019s counting")


def test_engine_words_stay_out_of_what_the_owner_reads() -> None:
    # Keys the app and the scripts exchange, such as "client-day" or "grant:read", are not shown.
    key = re.compile(r"[a-z0-9]+(?:[-_:.][a-z0-9]+)+")
    for name, source in scripts().items():
        for text in quoted_texts(source):
            if key.fullmatch(text):
                continue
            found = ENGINE_WORDS.search(text)
            assert found is None, (
                f"{name} shows the word {found.group(0) if found else ''!r}: {text[:80]!r}"
            )
            if name != "sheets.js":
                assert re.search(r"\bMCP\b", text) is None, (
                    f"{name} shows MCP outside the connect sheet"
                )
    page = read(STATIC / "index.html")
    assert ENGINE_WORDS.search(re.sub(r"<[^>]+>", " ", page)) is None
    assert ENGINE_WORDS.search("Every client has its own pool.")


def test_the_owner_reads_answered_not_served() -> None:
    # Names the scripts and the app exchange ("serving", "/api/serving", "both_kinds_served")
    # are lower-case words without a space; a sentence or a label is not.
    name_or_path = re.compile(r"[a-z0-9_/.:-]+")
    for name, source in scripts().items():
        for text in quoted_texts(source):
            if name_or_path.fullmatch(text):
                continue
            found = SERVING_WORDS.search(text)
            assert found is None, f"{name} shows {found.group(0) if found else ''!r}: {text[:80]!r}"
    assert SERVING_WORDS.search(re.sub(r"<[^>]+>", " ", read(STATIC / "index.html"))) is None
    assert SERVING_WORDS.search("Local apps are not served.")
    assert SERVING_WORDS.search("It only calls servers that the cloud can reach.") is None
