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
    "Losing the passphrase loses the vault. There is no recovery.",
    "Its files are deleted, not shredded; backups keep what they have.",
    'Text this app receives from "${collection}" is sent to ${app.destination} '
    "and cannot be recalled.",
    "This text is with ${item.destination} now. ${PRODUCT} cannot take it back.",
    "Stays on this Mac, if the app really is local",
    "any program running as you, including a hosted agent that can run",
    "Other forms (inflected, abbreviated, reversed order, split across lines) are different "
    "strings. Add each one you want replaced.",
    "Add a word or number that must never leave",
    "unverified",
    # Where the documents are not told apart by collection: the Basic page and the first run.
    "Text that ${app.name} receives from your documents is sent to ${app.destination} "
    "and cannot be recalled.",
    "Text that an app receives from your documents is sent to the company behind the app "
    "and cannot be recalled.",
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
        assert specifiers or name in ("dom.js", "format.js", "brand.js"), f"{name} imports nothing"
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
    created = gateway.index("await api.createStore(form.passphrase);")
    assert "form.shown = false;" in gateway[created : created + 300], (
        "the chosen passphrase stays on show after the vault was created"
    )
    api = without_comments(scripts()["api.js"])
    senders = re.findall(r"(\w+): \(passphrase\) => call\(\"POST\", \"(/api/[a-z/]+)\"", api)
    assert sorted(path for _name, path in senders) == [
        "/api/reauth",
        "/api/store/create",
        "/api/unlock",
    ]


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
    for opened in ('if (name === "approvals") {', 'if (name === "basic" || name === "setup") {'):
        after = route[route.index(opened) :]
        assert "ctx.ui.approvalSignature = null;" in after[: after.index("\n  }\n")], opened
    wait = re.search(r"const ARM_MS = (\d+);", views)
    assert wait is not None
    assert int(wait.group(1)) >= 1000
    # Neither answer is the loud button, and the safe one comes first.
    actions = views[views.index('{ class: "approval-actions" }') :]
    first, second = re.findall(r'button\("([^"]+)", \{([^}]*)\}', actions)[:2]
    assert first[0] == "Don\u2019t release"
    assert second[0] == "Release once"
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
    # The Basic page and the step "First question" show cards, and get them from that module.
    for name in ("basic.js", "gateway.js"):
        code = without_comments(scripts()[name])
        assert re.search(r'import \{[^}]*\bapprovalCards\b[^}]*\} from "\./views\.js";', code), name


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
    assert "This is the demo." in note
    assert "The passphrase of a real vault is never shown." in note
    assert 'button("Fill it in"' in note
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
    manage = without_comments(scripts()["manage.js"])
    assert "The file you chose is still where it was, not encrypted." in manage
    assert "The files you chose are still where they were, not encrypted." in manage
    assert manage.count("Removing or moving") == 2  # one sentence for one file, one for several
    # Said once per list of added files, not once per file.
    assert manage.count("leftOutsideText(sealedCount(state))") == 1
    assert 'fact("Copies of the files you added"' in manage


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
    # page asks at the old one for ever. So the page does not promise to continue.
    assert "the page continues by itself." not in screen
    assert "With the same address as before, this page continues by itself" in screen
    assert "with a new address, which is the usual case, open that one instead." in screen
    assert "It prints its address" in screen
    # A new pairing code is part of it, except in the demo, which has none.
    assert 'const pairing = !(ctx.session && ctx.session.pairing === "none");' in screen
    assert '${pairing ? " and a new pairing code" : ""}' in screen
    assert '${pairing ? " and asks for the code" : ""}' in screen


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
    # It is the first thing in the block that holds the title, and that block is what stays
    # in view, so the bar is seen wherever the page has been scrolled to.
    main = without_comments(scripts()["main.js"])
    top = main[main.index('{ class: "page-top", key: "top" }') :]
    assert 0 < top.index('class: "offline-bar"') < top.index('class: "page-head"')
    assert main.count('class: "offline-bar"') == 2  # the app is gone; a list could not be loaded
    assert ["position: sticky" in rule for rule in css_rules("app.css", ".page-top")] == [True]
    assert all("position" not in rule for rule in css_rules("app.css", ".page-head"))


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
    # to replace and removing a collection are Pro's. Basic says them and has no call for them.
    basic = without_comments(scripts()["basic.js"])
    for call in (
        "api.setAppLimits(",
        "api.setSettings(",
        "api.setServing(",
        "api.addReplacedString(",
        "api.removeCollection(",
    ):
        assert call not in basic, f"the Basic page calls {call}"


def test_basic_attributes_to_stored_names() -> None:
    # A row, a sum or a rule names an app by the name it has in the vault. The readable name of
    # a listed program ("Claude Desktop") is for the list of apps and for instructions only.
    basic = without_comments(scripts()["basic.js"])
    assert re.search(r"\.label\b", basic) is None, "the Basic page reads the label of something"
    assert re.search(r"\.known\b", basic) is None, "the Basic page reads the list of programs"


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
    assert 'groups.length > 1 ? proLink("Change in Pro", "#/apps"' in basic
    assert "const expressible = groups.length <= 1 && !apps.some((app) => !app.hosted);" in basic
    assert 'proLink("Add an app (in Pro)", "#/apps", { id: "basic-add-app" })' in basic
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
    assert "let text = GENERAL_SENTENCE;" in drawn
    assert drawn.index("if (described) {") < drawn.index("form.sentenceShown = true;")
    assert gateway.count("addFirstApp(") == 2  # where it is defined, and when the form is sent
    assert "if (ready && !form.busy) addFirstApp(ctx);" in step


def test_basic_changes_access_only_where_the_vault_has_one_group() -> None:
    basic = without_comments(scripts()["basic.js"])
    # What the Basic page asks the app to change, in full. Removing goes through today's sheets
    # of Pro, adding a file through the one function that adds files. The third call changes
    # nothing of the vault: it says that setting up is over, a word kept beside the vault. The
    # fourth changes nothing of the owner's at all: in a demo it has the played app ask once more
    # (see test_the_demo_guide_makes_the_played_app_ask_again_only_on_a_press).
    assert set(re.findall(r"\bapi\.(\w+)\(", basic)) == {
        "setCollection",
        "setGrant",
        "setInterface",
        "demoAgain",
    }
    assert re.findall(r"api\.setInterface\(([^)]*)\)", basic) == ['{ setup: "done" }']
    assert 'from "./api.js"' in basic
    for call in (
        "removeDocument(ctx, document)",
        "removeDocument(ctx, sample)",
        "removeApp(ctx, app)",
    ):
        assert call in basic, call
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
        'groups.length > 1 ? proLink("Change in Pro", "#/apps", '
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
    assert 'throw new ApiError(403, "cancelled", "Nothing was changed.");' in api


def test_adding_to_one_of_several_groups_chooses_no_group_for_the_owner() -> None:
    basic = without_comments(scripts()["basic.js"])
    section = function_body(basic, "function documentsSection(ctx) {")
    # One zone, and with several groups it only holds the files: the sheet asks where they go.
    assert section.count("dropZone(") == 2
    assert (
        'dropZone(ctx, "basic", { who: true, lists: groups.map((group) => group.name), '
        "take: (files) => addToWhichGroup(ctx, files) })"
    ) in section
    assert "dropZone(ctx, groups.length ? groups[0].name : FIRST_GROUP, { who: true })," in section
    assert basic.count("dropZone(") == 2
    assert 'const FIRST_GROUP = "documents";' in basic
    sheet = function_body(basic, "async function addToWhichGroup(ctx, files) {")
    assert "const form = { group: null };" in sheet
    assert "checked: form.group === group," in sheet
    assert 'name: "basic-group",' in sheet
    assert 'button("Cancel", { onclick: () => self.close(null), autofocus: true }),' in sheet
    add = sheet[sheet.index('button("Add", {') :]
    assert "disabled: form.group === null," in add[:200]
    assert "if (form.group !== null) self.close(form.group);" in add[:300]
    # Files are sent only after "Add", and to the group that was chosen. "Cancel" sends nothing.
    assert basic.count("uploadFiles(") == 1
    closed = sheet[sheet.index("const group = await sheet.closed;") :]
    assert 'if (typeof group === "string") await uploadFiles(ctx, group, files);' in closed
    assert sheet.count("self.close(") == 2  # "Cancel", with nothing; "Add", with the group
    # Once a group is chosen that an app which sends text to a provider can use, the sentence
    # is there.
    assert (
        "senders.length === 1 ? grantSentence(senders[0], form.group) : GENERAL_SENTENCE" in sheet
    )
    assert '"No app can ask for what is in it."' in sheet
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
    wanted = "options.who && sealedCount(entry.list) ? whoLine(ctx, entry.name, sealedCount("
    assert wanted in zone
    # It stands above the note about the file that was left outside, which is said once.
    assert zone.index(wanted) < zone.index("leftOutsideText(sealedCount(state))")
    line = function_body(manage, "function whoLine(ctx, collection, sealed) {")
    assert 'class: "upload-who", role: "status"' in line
    assert (
        '`Apps that may use your documents can ask for ${them} from now on: ${names.join(", ")}.`'
    ) in line
    assert "`No app can ask for ${them} yet.`" in line
    assert 'const them = sealed === 1 ? "it" : "them";' in line
    assert "`${GRANT_LABEL.read}: passages of it can leave without asking`" in line
    # Names are the names the apps have in the vault, with what each may do. An app that has the
    # access and is not answered right now is listed with that said.
    who = function_body(manage, "export function whoMayAsk(apps, collection, readWords) {")
    assert "return `${app.name} (${access}${idle})`;" in who
    assert 'app.grants[collection] === "approve" || app.grants[collection] === "read"' in who
    assert '"; paused"' in who
    assert '"; not answered in this session"' in who
    assert ".label" not in who
    # The pages of Pro are as they were: their zones say nothing more.
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


def test_basic_reads_what_belongs_to_a_group_by_that_group_alone() -> None:
    # A group may be named by a word that every object of the page answers to ("constructor").
    # Read carelessly, such a name gives something that is neither an access nor a list of files,
    # and the page that draws from it shows nothing at all.
    basic = without_comments(scripts()["basic.js"])
    read = function_body(basic, "function grantOf(app, group) {")
    assert "Object.prototype.hasOwnProperty.call(grants, group)" in read
    assert 'return mode === "approve" || mode === "read" ? mode : "none";' in read
    assert re.search(r"\.grants\[", basic) is None, "an access is read without that check"
    manage = without_comments(scripts()["manage.js"])
    state = function_body(manage, "function uploadState(ctx, collection) {")
    assert "if (!Object.prototype.hasOwnProperty.call(ctx.ui.uploads, collection))" in state
    assert manage.count("ctx.ui.uploads[") == 2  # both inside that function
    assert state.count("ctx.ui.uploads[") == 2
    # Who may ask for a file is told by comparing with the two words, never by "is there something".
    who = function_body(manage, "export function whoMayAsk(apps, collection, readWords) {")
    assert who.count("app.grants[collection] ===") == 3


def test_what_the_product_cannot_do_is_one_list_in_both_views() -> None:
    manage = without_comments(scripts()["manage.js"])
    listed = re.search(r"export const CANNOT = \[(.*?)\n\];", manage, re.S)
    assert listed is not None
    assert len(re.findall(r'^\s+"[^"\n]+",$', listed.group(1), re.M)) == 8
    assert "CANNOT.map(" in function_body(manage, "export function vaultView(ctx) {")
    line = function_body(manage, "export function versionLine(state) {")
    assert "a preview: it has not yet been tried against the agent applications it lists" in line
    basic = without_comments(scripts()["basic.js"])
    sheet = function_body(basic, "function openCannot(ctx) {")
    assert "const title = `What ${PRODUCT} cannot do`;" in sheet
    assert "CANNOT.map(" in sheet
    assert "versionLine(ctx.state)" in sheet
    assert 'button("Close", { onclick: () => self.close(), autofocus: true })' in sheet
    assert 'class: "link", id: "basic-cannot", onclick: () => openCannot(ctx)' in basic
    # Neither the statements nor the line about the version are written a second time.
    everything = "\n".join(without_comments(source) for source in scripts().values())
    assert everything.count("a preview: it has not yet been tried") == 1
    assert everything.count("Take text back.") == 1
    assert everything.count("versionLine(") == 3  # its definition, the Vault page, the sheet


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
    # The Basic page says that it is there, and offers to remove it through today's sheet.
    basic = without_comments(scripts()["basic.js"])
    section = function_body(basic, "function documentsSection(ctx) {")
    assert "group.documents.find(isSample)" in section
    assert '"1 of them is the invented sample letter."' in section
    assert (
        'button("Remove it", { small: true, id: "sample-remove", '
        "onclick: () => removeDocument(ctx, sample) })"
    ) in section


def test_first_run_and_basic_name_no_protocol() -> None:
    for name in ("gateway.js", "basic.js"):
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


def test_the_view_has_two_labels_and_no_toast() -> None:
    main = without_comments(scripts()["main.js"])
    options = re.search(r"const VIEW_OPTIONS = \[(.*?)\];", main, re.S)
    assert options is not None
    assert re.findall(r'\{ value: "(\w+)", label: "([^"]+)"', options.group(1)) == [
        ("basic", "Basic"),
        ("pro", "Pro"),
    ]
    # Neither view is called by a word that ranks it.
    shown = "\n".join("\n".join(quoted_texts(source)) for source in scripts().values()).lower()
    for word in ("advanced", "upgrade", "simple view", "expert", "premium"):
        assert word not in shown, f"a script says {word!r}"
    # One control is built from the two, for both views, with the view on screen selected.
    choice = function_body(main, "function viewChoice(shown) {")
    assert (
        'segmented({ name: "view", legend: "View", value: shown, options: VIEW_OPTIONS, '
        "onchange: chooseView })"
    ) in choice
    assert main.count("viewChoice(") == 3  # its definition, the sidebar of Pro, the bar of Basic
    assert 'viewChoice("pro")' in function_body(main, "function viewSwitch() {")
    assert "viewSwitch()," in function_body(main, "function sidebar() {")
    assert 'viewChoice("basic")' in function_body(main, "function basicShell() {")
    # Keeping the view says nothing when it worked: the screen is the answer.
    assert main.count("api.setInterface(") == 1
    keep = function_body(main, "async function keepView(view) {")
    kept, failed = keep.split("} catch (error) {")
    assert "applyState(await api.setInterface({ view }));" in kept
    assert "toast" not in kept
    assert "toastError(error);" in failed
    for head in (
        "async function chooseView(view) {",
        "async function stayInPro() {",
        "async function setUpInPro() {",
    ):
        assert "toast" not in function_body(main, head), head
    # A visit to Pro, when the view that is kept is Basic already, keeps nothing.
    choose = function_body(main, "async function chooseView(view) {")
    assert "const known = Boolean(kept && kept.stored && kept.view === view);" in choose
    assert "if (!known && !(await keepView(view))) return;" in choose


def test_the_six_pages_are_pro_whatever_view_is_kept() -> None:
    main = without_comments(scripts()["main.js"])
    route = function_body(main, "function routeFromHash() {")
    named = route.index("if (Object.prototype.hasOwnProperty.call(VIEWS, name)) return name;")
    basic = route.index('if (name === "basic") return "basic";')
    assert named < basic < route.index("return defaultRoute();")
    # Only an address that names no page is decided by the view that is kept.
    fallback = function_body(main, "function defaultRoute() {")
    assert 'return kept && kept.view === "basic" ? "basic" : "overview";' in fallback
    screen = function_body(main, "function screen() {")
    assert screen.index("return lockedScreen(ctx);") < screen.index('if (ctx.route === "basic")')
    assert 'if (ctx.route === "basic") return basicShell();' in screen
    # What the Basic page lists is asked for by kind, never picked from the newest entries.
    assert 'left: () => api.activity({ limit: LEFT_FIRST, kind: "release" }),' in main
    assert 'api.activity({ limit: STOPS_PAGE, kind: "refusal" })' in main
    assert 'basic: ["apps", "approvals", "settings", "documents", "left", "stops"],' in main


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


def test_the_top_bar_of_basic_claims_no_state_of_the_vault_while_the_app_does_not_answer() -> None:
    main = without_comments(scripts()["main.js"])
    bar = function_body(main, "function topbar() {")
    assert "const unknown = ctx.offline;" in bar
    assert 'if (unknown) lock = "State unknown";' in bar
    assert bar.index('if (unknown) lock = "State unknown";') < bar.index("else if (state.locks_at)")
    assert 'class: ["lock-state", unknown && "is-unknown"]' in bar
    # The bar that says what is wrong is drawn by the Basic page itself, in the block that stays
    # in view, and with the command only for a vault that this command would open again.
    basic = without_comments(scripts()["basic.js"])
    bars = function_body(basic, "export function basicBars(ctx, problem) {")
    assert bars.count('class: "offline-bar", role: "alert"') == 2
    assert "const command = ctx.state.demo ? null : launch.command;" in bars
    assert 'copyLine(ctx, "bar-launch-copy", command,' in bars
    assert basic.count('class: "offline-bar"') == 2
    shell = function_body(main, "function basicShell() {")
    assert 'el("div", { class: "basic-top", key: "top" }, topbar(), bar ?' in shell
    assert ["position: sticky" in rule for rule in css_rules("app.css", ".basic-top")] == [True]
    assert ["position: sticky" in rule for rule in css_rules("app.css", ".viewbar")] == [True]


def test_the_locked_screen_says_in_both_views_what_is_visible_without_the_key() -> None:
    gateway = without_comments(scripts()["gateway.js"])
    screen = function_body(gateway, "export function lockedScreen(ctx) {")
    assert 'const basic = Boolean(state.interface && state.interface.view === "basic");' in screen
    # In Basic the newest entry of the record is left out. The two figures and the sentence
    # that explains them are there in both views.
    assert "!basic && record && record.head_seq" in screen
    for fact in ('el("dt", null, "Sealed files")', 'el("dt", null, "Size")'):
        line = next(line for line in screen.splitlines() if fact in line)
        assert "basic" not in line
    assert (
        'foot: "While locked, apps get one answer: the vault is locked. The number and sizes of '
        'the sealed files are visible without the key; what is in them is not."'
    ) in screen
    # What waited when the vault locked is said only by a page that saw it lock.
    assert "const waited = basic ? form.waited || 0 : 0;" in screen
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
    kept = page.index(
        'fact("Kept beside the vault, not sealed", '
        '"Which view you use, and whether setup was finished")'
    )
    assert record < kept < page.index('{ key: "location" }')


def test_the_details_fold_the_record_on_the_basic_page() -> None:
    views = without_comments(scripts()["views.js"])
    body = function_body(views, "function panelBody(ctx, panel) {")
    assert 'const folded = ctx.route === "basic";' in body
    assert "const open = folded && ctx.ui.panelMore === true;" in body
    button = body[body.index('button("Record details", {') :]
    assert 'id: "panel-more",' in button[:200]
    assert 'expanded: open ? "true" : "false",' in button[:200]
    assert "!folded || open ? recordBlocks(item) : null," in body
    # What left, where it went and that it cannot be taken back are never behind the button.
    fold = body.index("folded\n")
    for shown in (
        '"The exact text that left"',
        "This text is with ${item.destination} now.",
        "unverified",
    ):
        assert 0 < body.index(shown) < fold, shown
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
    assert screen.index(step) < screen.index('if (ctx.route === "basic")')
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
    # After "Create the vault" the address names the step that adds an app, or Overview in Pro.
    created = function_body(gateway, "async function createVault(ctx) {")
    assert (
        'ctx.navigate(state.interface && state.interface.view === "basic" ? '
        '"setup/app" : "overview");'
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
    assert "bar: basicBars(ctx, problem)," in frame
    # The foot link keeps Pro as the view, through the one function that keeps a view.
    pro = function_body(main, "async function setUpInPro() {")
    assert 'if (!known && !(await keepView("pro"))) return;' in pro
    assert 'navigate("overview");' in pro


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
        "basicGrantSentence(senders[0]) : GENERAL_SENTENCE) : null,"
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
    assert 'onclick: () => ctx.navigate("basic")' in later[:200]


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
    assert basic.count("foldOf(") == 4  # where it is defined, and in the three parts
    for head, name in (
        ("function documentsSection(ctx) {", "documents"),
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
