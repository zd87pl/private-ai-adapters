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
from wdva.gate import brand
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
    ("GET", "/api/settings"),
    ("POST", "/api/settings"),
    ("POST", "/api/replaced-strings"),
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
    "This text is with ${item.destination} now. The gate cannot take it back.",
    "Stays on this Mac, if the app really is local",
    "any program running as you, including a hosted agent that can run",
    "Other forms (inflected, abbreviated, reversed order, split across lines) are different "
    "strings. Add each one you want replaced.",
    "Add a word or number that must never leave",
    "unverified",
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
    assert re.search(
        r"if \(signature !== ctx\.ui\.approvalSignature\) \{\s*"
        r"ctx\.ui\.approvalSignature = signature;\s*disarm\(ctx\);",
        views,
    )
    assert "releaseArmedAt: Number.POSITIVE_INFINITY," in without_comments(scripts()["main.js"])
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
