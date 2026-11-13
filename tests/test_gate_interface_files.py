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
from wdva.gate import brand, core
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
    basic = without_comments(scripts()["basic.js"])
    assert re.search(r'import \{[^}]*\bapprovalCards\b[^}]*\} from "\./views\.js";', basic)


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
    # So does the Basic page. The step "First question" joins this list when it shows a card.
    for name in ("basic.js",):
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
    for head in ("async function chooseView(view) {", "async function stayInPro() {"):
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
