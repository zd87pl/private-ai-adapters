"""The files of the owner's interface, read as text: what they load, what they call, what they say.

The page is served under a policy that allows scripts and styles from the app
itself and nothing else. These tests hold the files to that policy without a
browser, and to three further rules: text is never parsed as markup, every
address the scripts call is one the app defines, and the sentences the owner
reads claim nothing the gate cannot do.
"""

from __future__ import annotations

import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

import pytest

import wdva.gate.app
from wdva.gate import brand, core, textproc
from wdva.gate.app import demo, httpd

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
    ("POST", "/api/try"),
    ("POST", "/api/connect/<>/look"),
    ("POST", "/api/connect/<>"),
    ("POST", "/api/connect/<>/remove"),
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


# The two text tables. Every text the simple interface shows is a value of one of them.
TABLES = ("text-en.js", "text-pl.js")


def table(name: str) -> dict[str, list[str]]:
    """One text table as its keys, each with its texts: one, or one for each plural form."""
    found: dict[str, list[str]] = {}
    key = ""
    for line in read(STATIC / name).splitlines():
        entry = re.fullmatch(r'  "([^"]+)": (?:("(?:[^"\\]|\\.)*"),|\{)', line)
        if entry:
            key = entry.group(1)
            found[key] = [json.loads(entry.group(2))] if entry.group(2) else []
            continue
        form = re.fullmatch(r'    [a-z]+: ("(?:[^"\\]|\\.)*"),', line)
        if form:
            found[key].append(json.loads(form.group(1)))
    assert len(found) > 400, name
    return found


def samples() -> dict[str, tuple[str, str, str]]:
    """The two sample documents the page holds, by language: file name, title and text."""
    source = read(STATIC / "manage.js")
    block = source[source.index("export const SAMPLE = {") :]
    block = block[: block.index("\n};\n")]
    one = re.compile(
        r'  (\w+): \{\n    file: ("[^\n]*"),\n    title: ("[^\n]*"),\n    text: \[\n'
        r'((?:      "[^\n]*",\n)+)    \]\.join\("\\n\\n"\),\n  \},'
    )
    found: dict[str, tuple[str, str, str]] = {}
    for code, file, title, lines in one.findall(block):
        paragraphs = [json.loads(line.strip().rstrip(",")) for line in lines.splitlines()]
        found[code] = (json.loads(file), json.loads(title), "\n\n".join(paragraphs))
    return found


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


def routes_of_the_server() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Every route the server answers, written as ``API_CALLS`` writes one.

    First the routes of its table, which are answered behind the session; then the ones it
    answers before it looks into the table, which a page without a session needs.
    """
    behind = {
        (route.method, "/api/" + re.sub(r"<[^<>]*>", "<>", route.path)) for route in httpd._ROUTES
    }
    source = read(Path(httpd.__file__))
    before = set(re.findall(r'\(method, path\) == \("([A-Z]+)", "(/api/[^"]*)"\)', source))
    return behind, before


def test_the_page_and_the_server_name_the_same_routes() -> None:
    # ``API_CALLS`` is held against what the scripts call (above). Here it is held against
    # what the server answers: a route the page cannot call, and a call the server does not
    # know, both fail. The two lists are written by hand in two files, and nothing else
    # would say that one of them was left behind.
    behind, before = routes_of_the_server()
    assert len(behind) == len(httpd._ROUTES), "two routes of the table are the same address"
    assert before == {("GET", "/api/session"), ("POST", "/api/session/pair")}
    assert not behind & before
    served = behind | before
    assert served - API_CALLS == set(), "the server answers a route the page has no call for"
    assert API_CALLS - served == set(), "the page may call a route the server does not answer"
    # No address is written out in the server but those two, and the beginning of all.
    source = without_comments(read(Path(httpd.__file__)))
    assert set(re.findall(r'"(/api/[^"]*)"', source)) == {"/api/", *(path for _, path in before)}
    # A path parameter is one segment, and the page writes it as one.
    for _method, path in sorted(served):
        assert re.fullmatch(r"/api(?:/(?:[a-z-]+|<>))+", path), path


def test_the_route_reader_writes_a_route_as_the_page_does() -> None:
    behind, _ = routes_of_the_server()
    assert ("GET", "/api/activity/<>") in behind  # ``activity/<int:seq>`` in the table
    assert ("POST", "/api/apps/<>/limits") in behind
    assert ("POST", "/api/connect/<>") in behind
    assert not any("<int" in path or ">" in path.replace("<>", "") for _, path in behind)


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
        r"(\w+): \(passphrase(?:, language|, scope)?\) =>\s+call\(\"POST\", \"(/api/[a-z/]+)\"",
        api,
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
    for opened in ('if (name === "pro/approvals") {', 'if (name === "home") {'):
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
    # Home shows cards, and gets them from that module.
    home = without_comments(scripts()["home.js"])
    assert re.search(r'import \{[^}]*\bapprovalCards\b[^}]*\} from "\./views\.js";', home)
    # The plain pages, the sheets of the tiles and the screens before the vault show none.
    for name in ("pages.js", "connect.js", "gateway.js"):
        assert "approvalCards" not in without_comments(scripts()[name]), name


def test_approval_cards_are_drawn_by_one_function() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    views = sources["views.js"]
    assert 'export function approvalCards(ctx, { limit = null, around = "" } = {}) {' in views
    cards = function_body(views, "export function approvalCards(ctx, ")
    assert "approvalCard(ctx, entry.item, index)" in cards
    assert "decidedLine(ctx, entry.done)" in cards
    assert 'card(t("home.granted.title")' in cards
    # The Approvals page of Pro gets its cards from there and draws none itself.
    page = function_body(views, "export function approvalsView(ctx) {")
    assert "approvalCards(ctx, {" in page
    assert "approvalCard(" not in page
    assert "decidedLine(" not in page
    assert views.count("approvalCard(ctx,") == 2  # where it is defined, and inside approvalCards
    # So does Home, under "Waiting for you", and in one place.
    assert sources["home.js"].count("approvalCards(ctx, {") == 1, "Home does not show the cards"
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


# -- the classes of the stylesheets and of the scripts ------------------------------

A_TEXT = re.compile(r""""((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`""", re.S)

# A class that a script puts together from a beginning and a word of the code: the beginning,
# and every word the scripts give it. A rule for another word of such a beginning is a rule
# that nothing uses.
MADE_CLASSES = {
    "btn-": ("default", "primary", "quiet", "danger"),
    "chip-": ("accent", "quiet", "plain", "outline", "amber", "red"),
    "meter-": ("calm", "high", "full"),
    "note-": ("plain", "amber", "red"),
    "toast-": ("plain", "green", "amber", "red"),
    "act-": ("plain", "amber", "red"),
    "upload-": ("waiting", "sending", "done", "error"),
    "shake-": ("0", "1"),
}

# Functions that hand a class on to the element they make: the function, and which of its
# arguments is the class.
CLASS_ARGUMENTS = {"icon": 1, "chip": 4}

# Classes a script gives for which no stylesheet has a rule. Some are found by a script or
# by a walk-through; the others name a part and are not styled by themselves. A class that
# is neither styled nor listed here is a slip of the pen, or what a removed rule left behind.
WITHOUT_A_RULE = {
    "act-plain",
    "app-root",
    "approval-asked",
    "approval-claimed",
    "btn-default",
    "card-start",
    "col-title",
    "decided-no",
    "doc-rows",
    "doc-title",
    "hero-text",
    "limits-form",
    "link-rows",
    "meter-calm",
    "note-plain",
    "panel-nothing",
    # The mark of a text that leaves or left, by which the walk-throughs find it. Its one rule
    # was a greatest height on a narrow card, which hid part of the text to be released.
    "released",
    "row-chars",
    "row-item",
    "row-time",
    "start-text",
    "toast-plain",
    "toast-text",
    "upload-sending",
    "upload-waiting",
    "waiting-line",
    "word-count",
}


def selectors(css: str) -> list[str]:
    """What stands before the brace of every rule of a stylesheet. An at-rule is none."""
    code = A_TEXT.sub('""', without_comments(css))
    found: list[str] = []
    start = 0
    for mark in re.finditer(r"[{};]", code):
        if mark.group(0) == "{":
            prelude = code[start : mark.start()].strip()
            if prelude and not prelude.startswith("@"):
                found.append(prelude)
        start = mark.end()
    return found


def classes_named(css: str) -> set[str]:
    """Every class a stylesheet names in a selector."""
    names: set[str] = set()
    for selector in selectors(css):
        names.update(re.findall(r"\.(-?[A-Za-z_][\w-]*)", selector))
    return names


def expression_at(code: str, start: int) -> str:
    """The expression of a script that begins at ``start``.

    It ends before the comma or the semicolon that follows it, or before the bracket that
    closes what it stands in. Brackets and quoted text inside it are stepped over.
    """
    depth = 0
    at = start
    while at < len(code):
        text = A_TEXT.match(code, at) if code[at] in "\"'`" else None
        if text is not None:
            at = text.end()
            continue
        if code[at] in "([{":
            depth += 1
        elif code[at] in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif code[at] in ",;" and depth == 0:
            break
        at += 1
    return code[start:at]


def arguments_at(code: str, start: int) -> list[str]:
    """The arguments of a call whose first one begins at ``start``, each as its text."""
    found: list[str] = []
    at = start
    while True:
        argument = expression_at(code, at)
        found.append(argument.strip())
        at += len(argument)
        if at >= len(code) or code[at] != ",":
            return found
        at += 1


def class_words(expression: str) -> tuple[set[str], set[str]]:
    """The classes an expression holds written out, and the beginnings it completes.

    Every quoted text of the expression is a list of classes, but one that something is
    compared with (``route === "home" && "is-current"``). A word that ends in a part of
    code (``btn-${kind}``) is the beginning of a made class.
    """
    whole: set[str] = set()
    beginnings: set[str] = set()
    for found in A_TEXT.finditer(expression):
        before = expression[: found.start()].rstrip()
        after = expression[found.end() :].lstrip()
        if before.endswith(("===", "!==")) or after.startswith(("===", "!==")):
            continue
        for word in re.sub(r"\$\{[^{}]*\}", "\x00", "".join(found.groups(""))).split():
            if "\x00" not in word:
                whole.add(word)
            elif word != "\x00":
                assert word.endswith("\x00"), f"a class that is made in its middle: {word!r}"
                assert word.count("\x00") == 1, f"a class made of two parts of code: {word!r}"
                beginnings.add(word[:-1])
    return whole, beginnings


def classes_given(code: str) -> tuple[set[str], set[str]]:
    """Every class one script gives an element, and every beginning of a class it completes.

    A class is given where an element is made (``class:``), through the element's own list
    of classes or its attribute, and through the functions of ``CLASS_ARGUMENTS``. Where
    ``class:`` is given a name of the code, the classes are those of the list that name
    was made as (``const shape = [...]``).
    """
    whole: set[str] = set()
    beginnings: set[str] = set()

    def take(expression: str) -> None:
        found = class_words(expression)
        whole.update(found[0])
        beginnings.update(found[1])

    places = (
        r"\bclass:\s*",
        r"\.classList\.(?:add|remove|toggle|contains)\(\s*",
        r"\.className\s*=\s*",
        r'\.setAttribute\(\s*"class"\s*,\s*',
    )
    for place in places:
        for found in re.finditer(place, code):
            expression = expression_at(code, found.end()).strip()
            take(expression)
            if re.fullmatch(r"[A-Za-z_]\w*", expression):
                for made in re.finditer(rf"\b(?:const|let) {expression} = ", code):
                    take(expression_at(code, made.end()))
    for function, index in CLASS_ARGUMENTS.items():
        for call in re.finditer(rf"(?<![\w.]){function}\(", code):
            if code[: call.start()].endswith("function "):
                continue
            arguments = arguments_at(code, call.end())
            if len(arguments) > index:
                take(arguments[index])
    return whole, beginnings


def all_classes() -> tuple[set[str], set[str], set[str]]:
    """The classes the stylesheets name; those the page gives written out; those it makes."""
    named: set[str] = set()
    for path in files(".css"):
        named |= classes_named(read(path))
    written: set[str] = set()
    beginnings: set[str] = set()
    for name, source in scripts().items():
        if name in TABLES:
            continue
        found = classes_given(without_comments(source))
        written |= found[0]
        beginnings |= found[1]
    for path in files(".html"):
        for _tag, attributes in parsed(path).tags:
            written.update((attributes.get("class") or "").split())
    assert beginnings == set(MADE_CLASSES), "the scripts make classes of other beginnings"
    made = {begin + word for begin, words in MADE_CLASSES.items() for word in words}
    return named, written, made


def test_every_rule_is_for_a_class_the_page_gives() -> None:
    named, written, made = all_classes()
    assert len(named) > 400
    assert len(written) > 400
    unused = sorted(named - written - made)
    assert unused == [], f"rules for classes that no script gives: {unused}"


def test_every_class_the_page_gives_has_a_rule_or_is_listed() -> None:
    named, written, made = all_classes()
    bare = (written | made) - named
    assert bare - WITHOUT_A_RULE == set(), "a class without a rule that is not listed"
    assert WITHOUT_A_RULE - bare == set(), "listed as without a rule, and has one or is gone"
    for word in written | made:
        assert re.fullmatch(r"-?[A-Za-z_][\w-]*", word), f"{word!r} is no class"


def test_the_words_of_a_made_class_are_words_of_the_scripts() -> None:
    code = "\n".join(
        without_comments(source) for name, source in scripts().items() if name not in TABLES
    )
    for beginning, words in MADE_CLASSES.items():
        assert f"`{beginning}${{" in code, f"no script completes {beginning!r}"
        for word in words:
            if word.isdigit():
                continue
            assert f'"{word}"' in code, f"no script holds the word {word!r} of {beginning!r}"
    # The two numbers: a card that shakes takes one of two classes in turn.
    assert "`shake-${form.shake % 2}`" in code
    # The functions that hand a class on take it where this test looks for it.
    dom = without_comments(scripts()["dom.js"])
    parts = without_comments(scripts()["parts.js"])
    assert "export function icon(name, extraClass" in dom
    assert 'svg.setAttribute("class", extraClass ? `icon ${extraClass}` : "icon");' in dom
    head = 'export function chip(label, tone = "quiet", glyph = null, title = null, extraClass'
    assert head in parts
    assert 'class: ["chip", `chip-${tone}`, extraClass]' in parts


def test_no_stylesheet_defines_what_nothing_uses() -> None:
    css = "\n".join(without_comments(read(path)) for path in files(".css"))
    code = "\n".join(
        without_comments(source) for name, source in scripts().items() if name not in TABLES
    )
    # An animation that no rule runs.
    for name in re.findall(r"@keyframes\s+([\w-]+)", css):
        assert re.search(rf"animation(?:-name)?\s*:[^;]*\b{re.escape(name)}\b", css), name
    # A custom property that is set and never read, or read and never set.
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    read_by_styles = set(re.findall(r"var\(\s*(--[\w-]+)", css))
    set_by_scripts = set(re.findall(r'"(--[\w-]+)"', code))
    assert defined - read_by_styles == set(), "a custom property nothing reads"
    assert read_by_styles - defined - set_by_scripts == set(), "a custom property nothing sets"
    assert set_by_scripts <= read_by_styles
    # No rule is hung on an id: the scripts give ids to find elements, never to style them.
    for path in files(".css"):
        for selector in selectors(read(path)):
            assert re.search(r"(?<![\w-])#[A-Za-z_]", selector) is None, selector


def test_the_class_readers_do_catch_what_they_name() -> None:
    css = (
        ".a, .b-c:hover > .d::after { color: red; }\n"
        '@media (min-width: 720px) { .e[data-x=".no"] .f.is-on { top: 0; } }\n'
        "@keyframes spin { from { top: 0; } to { top: 1px; } }\n"
        ".g { background: url(.h); margin: 0.5em; }\n"
    )
    assert classes_named(css) == {"a", "b-c", "d", "e", "f", "is-on", "g"}
    code = (
        'el("p", { class: ["note", `note-${tone}`, open && "is-open"], id: "not-a-class" });\n'
        'el("a", { class: filled ? "btn btn-primary" : "link", href: "#/home" });\n'
        'el("a", { class: ["nav", route === `pro/${name}` && "is-current", "x" !== y && "z"] });\n'
        'const shape = ["act-row", `act-${tone}`, big ? null : "act-small"];\n'
        'el("li", { class: shape, key: "row" });\n'
        'item.classList.add("is-leaving"); area.className = "sr-only";\n'
        'svg.setAttribute("class", extra ? `icon ${extra}` : "icon");\n'
        'icon("lock", "lock-act"); chip(t("x.y"), "plain", "mac", null, "chip-dest");\n'
        "export function icon(name, extraClass) {}\n"
    )
    whole, beginnings = classes_given(code)
    assert whole == {
        "note",
        "is-open",
        "btn",
        "btn-primary",
        "link",
        "nav",
        "is-current",
        "z",
        "act-row",
        "act-small",
        "is-leaving",
        "sr-only",
        "icon",
        "lock-act",
        "chip-dest",
    }
    assert beginnings == {"note-", "act-"}
    assert arguments_at('a, f(b, "c,d"), [e, g]) + 1', 0) == ["a", 'f(b, "c,d")', "[e, g]"]
    assert expression_at('x ? "a b" : `c-${d}`, next', 0) == 'x ? "a b" : `c-${d}`'


# -- the name, the demo's passphrase, and what the page cannot know -----------------


def test_the_name_is_written_in_no_file_of_the_interface() -> None:
    # The server puts the name into the page; brand.js reads it there; everything
    # else imports it from brand.js. So no file may spell it out, in any form.
    forms = [brand.PRODUCT, brand.SLUG, f"{brand.TOOL_PREFIX}_", brand.SLUG.replace("-", "_")]
    assert {"text-en.js", "text-pl.js"} <= {path.name for path in STATIC.iterdir()}
    for path in sorted(STATIC.iterdir()):
        text = read(path)
        for form in forms:
            assert form not in text, f"{path.name} spells out the name: {form!r}"
            assert form.lower() not in text.lower(), f"{path.name} spells out the name: {form!r}"
    page = read(STATIC / "index.html")
    assert "<title>{{product}}</title>" in page
    assert '<meta name="application-name" content="{{product}}">' in page
    # The name an entry in another program's settings carries comes the same way: the page How
    # names it, and no file of the page spells it out.
    assert '<meta name="application-entry" content="{{entry}}">' in page
    assert '<p class="boot-name">{{product}}</p>' in page
    assert re.search(r"<noscript>\s*<p[^>]*>\{\{product\}\} needs JavaScript", page)
    source = without_comments(scripts()["brand.js"])
    assert 'meta[name="application-name"]' in source
    assert re.search(r"export const PRODUCT\b", source)
    assert 'meta[name="application-entry"]' in source
    assert re.search(r"export const ENTRY\b", source)
    assert "import" not in source, "the one module every other may import imports nothing"
    # Whoever shows the name got it from there.
    for name, script in scripts().items():
        code = without_comments(script)
        if name != "brand.js" and re.search(r"\bPRODUCT\b", code):
            assert re.search(r'import \{ (?:ENTRY, )?PRODUCT \} from "\./brand\.js";', code), name
    # The name of the entry is put into a text by the one function that fills places, and by
    # nothing else: a sentence says "{entry}" and never the name itself.
    users = {
        name
        for name, script in scripts().items()
        if name != "brand.js" and re.search(r"\bENTRY\b", without_comments(script))
    }
    assert users == {"texts.js"}
    for name in TABLES:
        assert "{entry}" in read(STATIC / name), name


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
    assert "requestReauth(ctx.state ? ctx.state.demo_passphrase : null, scope)" in main
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
    # A check on Home says "done" or "an app of this name is registered", never "sealed": the
    # number of a card of "Start here" once its fact holds, the mark of a tile. None of them is
    # drawn in green. (The check beside a file that was sealed is.)
    for selector in (
        ".first-num",
        ".first-num.is-done",
        ".first-num .icon",
        ".tile",
        ".tile-mark",
        ".tile.is-on .tile-mark",
        ".tile-state",
    ):
        rules = css_rules("home.css", selector)
        assert rules, selector
        assert all("--green" not in rule for rule in rules), selector
    home = without_comments(scripts()["home.js"])
    head = function_body(home, "function cardHead(number, done, title) {")
    assert 'class: ["first-num", done && "is-done"]' in head
    assert 'icon("check")' in head
    assert 'class: ["tile", on && "is-on"],' in function_body(
        home, "function tile(ctx, entry, apps) {"
    )
    assert css_rules("app.css", ".upload-done .icon") == ["  color: var(--green);\n"]


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


# -- Home, the sheets of its tiles, and the two views --------------------------------


def test_home_changes_only_what_it_can_express() -> None:
    # Limits, the settings of the vault, which kinds of apps are answered, pausing, the words
    # to replace and removing a collection are Pro's. Home and the sheets of its tiles have no
    # call for them, and neither have the plain pages.
    asked: dict[str, set[str]] = {}
    for name in ("home.js", "connect.js", "pages.js"):
        code = without_comments(scripts()[name])
        for call in (
            "api.setAppLimits(",
            "api.setSettings(",
            "api.setServing(",
            "api.addReplacedString(",
            "api.removeCollection(",
            "api.setInterface({ view",
        ):
            assert call not in code, f"{name} calls {call}"
        asked[name] = set(re.findall(r"\bapi\.(\w+)\(", code))
        # No figure of the vault is written into these scripts: what a limit is, is an answer
        # of the app. (The numbers that stand here are times in milliseconds, a length of an
        # input and the size the app names when it has not answered yet.)
        figures = {int(found) for found in re.findall(r"(?<![\w.#-])\d{3,}(?![\w.])", code)}
        assert figures <= {500, 2000, 20971520, 86400000}, f"{name} holds a figure: {figures}"
        assert re.search(r"\b\d{1,3},\d{3}\b", "\n".join(quoted_texts(scripts()[name]))) is None
    # What each of them asks the app to do, in full.
    assert asked == {
        # A document is added; the app is told that the first steps are done; the owner's own
        # question is asked; in a demo the played app is told to ask once more.
        "home.js": {"addDocument", "setInterface", "tryQuestion", "demoAgain"},
        # A look, a write, a removal; the text to paste; and for the way by hand and the
        # fourth tile the three requests that add an app with "ask me each time".
        "connect.js": {
            "connectLook",
            "connectWrite",
            "connectRemove",
            "connect",
            "addApp",
            "setCollection",
            "setGrant",
            "removeApp",
        },
        "pages.js": set(),
    }
    # The one access these scripts give is "ask me each time", to the one group of the vault,
    # and a group is only ever opened, never closed.
    connect = without_comments(scripts()["connect.js"])
    assert re.findall(r"api\.setGrant\(([^)]*)\)", connect) == ['name, group, "approve"']
    assert re.findall(r"api\.setCollection\(([^)]*)\)", connect) == [
        "{ name: group, allow_hosted: true }"
    ]
    assert re.findall(r"api\.addApp\(([^)]*)\)", connect) == ["{ name, hosted: true, destination }"]
    add = function_body(connect, "async function addWithAccess(ctx, name, destination) {")
    assert add.index("if (groups.length > 1) return;") < add.index("api.setCollection(")
    assert connect.count("addWithAccess(") == 3  # its definition, the way by hand, the fourth tile
    home = without_comments(scripts()["home.js"])
    assert re.findall(r"api\.setInterface\(([^)]*)\)", home) == ['{ setup: "done" }']


def test_home_attributes_to_stored_names() -> None:
    # A row, a card or a sentence about what happened names an app by the name it has in the
    # vault. The readable name of a listed program ("Claude Desktop") is for a tile, for the
    # sentences of its sheet that say what a press will do, and for instructions only.
    def code_of(name: str) -> str:
        # (Quoted texts are left out: keys hold ".label".)
        return re.sub(r'"[^"\n]*"', '""', without_comments(scripts()[name]))

    pages = code_of("pages.js")
    assert re.search(r"\.label\b", pages) is None, "a plain page reads the label of something"
    assert re.search(r"\.known\b", pages) is None, "a plain page reads the list of programs"
    home = code_of("home.js")
    assert re.search(r"\.known\b", home) is None, "Home reads the list of programs"
    drawn = function_body(home, "function tile(ctx, entry, apps) {")
    assert home.count(".label") == drawn.count(".label") == 1, "Home reads a label outside a tile"
    assert "custom ? name : entry.label" in drawn
    # Who could ask for a passage is said by stored names; so is what the cards of Home say.
    trial = function_body(
        without_comments(scripts()["home.js"]), "function trialSentence(passage) {"
    )
    assert ".map((app) => app.name);" in trial
    connect = without_comments(scripts()["connect.js"])
    places = function_body(connect, "function values(view, more = {}) {")
    assert "label: tile.label," in places
    assert "name: tile.name," in places
    # The name an app has in the vault is said where two names could be taken for each other:
    # an entry that connects the program under another name, an app that is set up differently.
    for name in TABLES:
        texts = table(name)
        for key in ("connect.as.say.1", "connect.as.say.2", "connect.refused.say.1.other_kind"):
            assert "{name}" in texts[key][0], f"{name} {key}"
    sentence = function_body(
        without_comments(scripts()["sheets.js"]), "export function basicGrantSentence(app) {"
    )
    assert 't("docs.say.2", { app: app.name, destination: app.destination })' in sentence
    assert 't("docs.say.2.local", { app: app.name })' in sentence


def test_the_sentence_is_drawn_before_access_is_given() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    # Whoever gives an app access also has the sentence that says where the text goes.
    for name, code in sources.items():
        if "api.setGrant(" in code or "api.connectWrite(" in code:
            assert (
                "basicGrantSentence(" in code
                or "grantSentence(" in code
                or "function whereTextGoes(view, id, text) {" in code
            ), name
    connect = sources["connect.js"]
    # Only drawing the sentence says that it is shown, and every draw of a sheet starts without.
    told = function_body(connect, "function whereTextGoes(view, id, text) {")
    assert told.splitlines()[1].strip() == "view.told = true;"
    assert connect.count("view.told = true;") == 1
    assert 'role: "note"' in function_body(connect, "function amberNote(id, text) {")
    for head in (
        "function drawConnect(ctx, sheet, view) {",
        "function drawCustom(ctx, sheet, view) {",
    ):
        draw = function_body(connect, head)
        assert draw.index("view.told = false;") < draw.index("whereTextGoes(view, "), head
        assert draw.count("view.told = false;") == 1, head
    # What is drawn is the sentence of the table for this app and this vault: one of the three
    # of the offer, or the one of the fourth tile, which names the app and the receiver.
    goes = function_body(connect, "function goesSentence(ctx, view) {")
    assert re.findall(r'return t\("([^"]+)", values\(view\)\);', goes) == [
        "connect.offer.say.2.read",
        "connect.offer.say.2.groups",
        "connect.offer.say.2",
    ]
    drawn = re.findall(r"whereTextGoes\(view, \"([a-z0-9-]+)\", ([^\n]*)", connect)
    assert [(ident, what.split("(")[0]) for ident, what in drawn] == [
        ("connect-say-2", "goesSentence"),  # the way by hand
        ("connect-say-2", "goesSentence"),  # the offer
        ("custom-say-2", "t"),
    ]
    assert 'whereTextGoes(view, "custom-say-2", t("custom.say.2", {' in connect
    # "Connect": nothing is sent unless the sentence is on the sheet at that moment.
    press = function_body(connect, "async function connect(ctx, sheet, view) {")
    guard = "if (!view.told || view.busy) return;"
    assert press.splitlines()[1].strip() == guard, "the guard is not the first thing Connect does"
    assert press.index(guard) < press.index("api.connectWrite(")
    # "Add the app and show the text": the same, before an app is added.
    show = function_body(connect, "async function showText(ctx, sheet, view) {")
    guard = "if (!view.told || hand.busy) return;"
    assert show.splitlines()[2].strip() == guard
    assert show.index(guard) < show.index("addWithAccess(") < show.index("api.connect(")
    # "Add" on the fourth tile: the same, and only the sentence of this app counts. While a
    # field is empty the sentence that holds for every app stands there, and it allows nothing.
    add = function_body(connect, "async function addCustom(ctx, sheet, view) {")
    guard = "if (!view.told || form.busy || !NAME_PATTERN.test(name) || !destination) return;"
    assert add.splitlines()[4].strip() == guard
    assert add.index(guard) < add.index("addWithAccess(")
    custom = function_body(connect, "function drawCustom(ctx, sheet, view) {")
    assert (
        'ready ? whereTextGoes(view, "custom-say-2", t("custom.say.2", '
        "{ name: form.name, destination })) : "
        'amberNote("custom-say-2", t("docs.say.2.general")),'
    ) in custom
    # The three requests that give access are made by one function, which these two call.
    assert connect.count("api.setGrant(") == connect.count("api.addApp(") == 1
    giving = function_body(connect, "async function addWithAccess(ctx, name, destination) {")
    assert "api.setGrant(" in giving
    assert "api.addApp(" in giving
    # Home itself gives no access and writes no file: a tile only opens its sheet.
    home = sources["home.js"]
    for call in ("setGrant", "addApp", "connectWrite", "connectRemove", "connectLook"):
        assert call not in home, f"Home calls {call}"


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
    wanted = "const who = options.who && sealedCount(entry.list) ? whoLine(ctx, entry.name) : null;"
    assert wanted in zone
    # It stands under the last file that was sealed, never under one that was refused after it.
    assert "if (who) lines.splice(lastSealed(entry.list.items) + 1, 0, who);" in zone
    assert 'return items.map((item) => item.status).lastIndexOf("done");' in function_body(
        manage, "export function lastSealed(items) {"
    )
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
    # A vault with several groups gets one line for each group that got a file, and on Home
    # they stand together: each begins with the name of its group, as the line of a file does.
    assert (
        "const several = Boolean(ctx.data.documents) && ctx.data.documents.collections.length > 1;"
    ) in line
    assert 'several ? [el("span", { class: "upload-group" }, collection), " "] : null,' in line
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
    # Home has its cards and its sheets on one page. When a sheet closes, a second press that
    # was meant for one of its buttons meets what is under it; that must not be an armed
    # "Release once". So for the answer of the box that asks, which comes and goes with a press
    # and whose two buttons look like those of a card.
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
    home = without_comments(scripts()["home.js"])
    around = home[home.index("const around = [") :]
    around = around[: around.index("].join(")]
    assert 'ctx.ui.sheets > 0 ? "sheet" : "",' in around
    assert 'box.answer ? "trial" : "",' in around
    assert "noticesSignature(ctx)," in around
    assert home.count("const around = [") == 1
    waiting = function_body(home, "function waitingPart(ctx, problem) {")
    assert "approvalCards(ctx, { limit: all ? null : CARDS_AT_FIRST, around });" in waiting


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


def test_the_simple_pages_read_what_belongs_to_a_group_by_that_group_alone() -> None:
    # A group may be named by a word that every object of the page answers to ("constructor").
    # Read carelessly, such a name gives something that is neither an access nor a list of files,
    # and the page that draws from it shows nothing at all. So on the page Documents; Home and
    # the sheets of its tiles never read an access by the name of a group at all.
    code = without_comments(scripts()["pages.js"])
    read = function_body(code, "function grantOf(app, group) {")
    assert "Object.prototype.hasOwnProperty.call(grants, group)" in read
    assert 'return mode === "approve" || mode === "read" ? mode : "none";' in read
    for name in ("pages.js", "home.js", "connect.js"):
        found = re.search(r"\.grants\[", without_comments(scripts()[name]))
        assert found is None, f"{name} reads an access without that check"
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
    # A word the app answers with picks a text only where the page knows that word.
    home = without_comments(scripts()["home.js"])
    assert "own.call(NOTICE, warning.code) ? t(NOTICE[warning.code], " in home
    assert "own.call(TILE_NAME, entry.tile) ? TILE_NAME[entry.tile] : " in home
    connect = without_comments(scripts()["connect.js"])
    assert "own.call(REFUSED, word) ? REFUSED[word] : " in connect
    assert (
        "Object.prototype.hasOwnProperty.call(said, answer.file) ? said[answer.file] : " in connect
    )


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
    home = without_comments(scripts()["home.js"])
    assert 'id: "home-how", href: "#/how" }, t("home.button.how")' in home
    assert "openCannot" not in home
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


def test_the_samples_are_recognised_by_title_and_length() -> None:
    held = samples()
    assert sorted(held) == ["en", "pl"]
    assert held["en"][0] == "Sample rental agreement (invented).txt"
    assert held["pl"][0] == "Umowa najmu (zmyślony przykład).txt"
    for code, (file, title, text) in held.items():
        assert file == f"{title}.txt", "the vault makes the title from the name of the file"
        assert "\\" not in text, code
        # The page tells a sample by its title and its number of characters together. So the
        # vault must count the text exactly as the page does: nothing in it is changed when it
        # is sealed. (A character outside the basic plane would count twice on the page.)
        assert textproc.sanitize(text).strip("\n") == text, code
        assert all(ord(character) < 0x10000 for character in text), code
        assert unicodedata.normalize("NFC", text) == text, code
        # It is long enough to be more than one passage: the box that asks shows one of them.
        assert len(textproc.chunk_spans(text)) >= 3, code
    assert held["en"][2] != held["pl"][2]
    assert len(held["en"][2]) != len(held["pl"][2]), "the two could be taken for each other"
    manage = without_comments(scripts()["manage.js"])
    which = function_body(manage, "export function sampleOf(document) {")
    assert (
        "if (document.title === SAMPLE[code].title && "
        "document.characters === SAMPLE[code].text.length) return code;"
    ) in which
    assert "return sampleOf(document) !== null;" in function_body(
        manage, "export function isSample(document) {"
    )
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
    # The box that asks holds the sample's question only while the sample of the language the
    # page shows is the vault's one document, and only until something was typed.
    home = without_comments(scripts()["home.js"])
    shown = function_body(home, "function shownQuestion(ctx) {")
    assert "if (box.typed) return box.question;" in shown
    assert 'return onlySample(ctx) === language() ? t("ask.sample.question") : "";' in shown
    assert "return held.length === 1 ? sampleOf(held[0]) : null;" in function_body(
        home, "function onlySample(ctx) {"
    )


def test_the_protocol_is_named_only_on_the_custom_tile() -> None:
    # A new person reads "Claude", "ChatGPT", "Grok" and "Other app". The protocol's name
    # stands where it helps the one who knows it: on the fourth tile, in its sheet, and in the
    # notice about the package of that name. No text of either table holds an address.
    may = ("tile.status.custom.connect", "notice.no_sdk.say.1")
    for name in TABLES:
        naming = set()
        for key, texts in table(name).items():
            for text in texts:
                assert "://" not in text, f"{name} {key} holds an address"
                assert "Model Context Protocol" not in text, f"{name} {key}"
                if re.search(r"\bMCP\b", text):
                    naming.add(key)
        assert naming
        for key in naming:
            assert key in may or key.startswith("custom."), f"{name} {key} names the protocol"
        assert "tile.status.custom.connect" in naming
    # The scripts of the simple interface hold no text of their own that names it.
    for name in ("gateway.js", "home.js", "connect.js", "pages.js"):
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
        for phrase in (
            "always replaced",
            "ran out unused",
            "is connected",
            "recommended",
            "jest połączona",
            "jest połączony",
            "zalecan",
        ):
            assert phrase not in shown, f"{name} says {phrase!r}"
    assert {"text-en.js", "text-pl.js"} <= set(scripts())


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
    assert "setUpInPro" not in main
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
    # the addresses Home had before ("#/basic", and "#/setup" with whatever followed it, the
    # three steps that are gone); the addresses from before of four of the six pages; and only
    # then what the app keeps.
    simple = route.index(
        'if (name === "home" || Object.prototype.hasOwnProperty.call(PAGES, name)) return name;'
    )
    pro = route.index(
        'if (name === "pro") return Object.prototype.hasOwnProperty.call(VIEWS, step) ? '
        '`pro/${step}` : "pro/overview";'
    )
    before = route.index('if (HOME_BEFORE.includes(name)) return "home";')
    old = route.index("if (OLD_ADDRESSES.includes(name)) return `pro/${name}`;")
    assert simple < pro < before < old < route.index("return defaultRoute();")
    assert 'const HOME_BEFORE = ["basic", "setup"];' in main
    # No screen is a step any more: the route "setup" is gone, with everything that drew it.
    assert '"setup"' not in main.replace('const HOME_BEFORE = ["basic", "setup"];', "")
    for gone in ("setupScreen", "SETUP_STEPS", "setup/", "loadMoreLeft", "placeNow"):
        assert gone not in main, gone
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
    home = settle[settle.index("if (HOME_BEFORE.includes(name)) {") :]
    assert 'window.history.replaceState(null, "", "#/home");' in home[: home.index("}")]
    assert "if (!OLD_ADDRESSES.includes(name)) return;" in settle
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
            assert first in ("home", "documents", "activity", "settings", "how", "pro"), (
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
    assert "const LEFT_FIRST = 3;" in main
    # Home asks for the documents too: its first card adds one, and its box has to know whether
    # the invented sample is the vault's only document. It asks for no settings of the vault.
    assert 'home: ["apps", "approvals", "documents", "left", "stops"],' in main
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
    home = without_comments(scripts()["home.js"])
    around = home[home.index("const around = [") :]
    assert 'ctx.panel ? "panel" : "",' in around[: around.index("].join(")]


# -- the first steps: three cards of Home, and no screen of their own ----------------


def test_the_samples_say_that_they_are_invented_and_are_sent_apart() -> None:
    # Each of the two says in its title and in its first paragraph that it is invented, in its
    # language: the title stands on every row and card, and the first paragraph is what a
    # reader of the document meets first.
    invented = {"en": ("invented", "do not exist"), "pl": ("zmyślony", "nie istnieją")}
    for code, (file, title, text) in samples().items():
        word, nobody = invented[code]
        first = text.split("\n\n")[0]
        assert word in title, code
        assert word in file, code
        assert word in first, code
        assert nobody in first, code
    sources = {name: without_comments(source) for name, source in scripts().items()}
    home = sources["home.js"]
    send = function_body(home, "async function addSample(ctx, group) {")
    # The sample of the language the page shows; English where a language has none.
    assert "const sample = SAMPLE[language()] || SAMPLE.en;" in send
    assert "await api.addDocument(group, sample.file, sample.text);" in send
    # It is sent by its own function and never through the one that sends the owner's files, so
    # it is not among the files of which the page says that they are still where they were.
    assert home.count("api.addDocument(") == 1
    assert "uploadFiles(" not in send
    said = function_body(home, "function addCard(ctx, facts) {")
    assert 't("home.new.result.sample")' in said
    assert 'sealed.length ? el("li", { class: "upload-said", key: "outside" }' in said
    assert 'const sealed = chosen.filter((entry) => entry.item.status === "done");' in said
    # Never without a press: the one call stands in the handler of its button, which is not
    # the filled one. Where the vault has several groups, none is chosen for the owner.
    assert home.count("addSample(") == 2
    assert (
        'groups.length === 1\n                ? button(t("home.new.card1.button.sample"), '
        '{ kind: "quiet", id: "first-sample", busy: first.sampleBusy, '
        "onclick: () => addSample(ctx, groups[0]) })"
    ) in said
    # No other script sends a sample: the one other place that adds a document sends a file
    # the owner chose, with the text read from that file.
    senders = {name for name, code in sources.items() if "api.addDocument(" in code}
    assert senders == {"home.js", "manage.js"}
    assert sources["manage.js"].count("api.addDocument(") == 1
    upload = function_body(
        sources["manage.js"], "export async function uploadFiles(ctx, collection, files) {"
    )
    assert "api.addDocument(" in upload
    assert "SAMPLE" not in upload
    for name, code in sources.items():
        if name not in ("home.js", "manage.js"):
            assert "SAMPLE" not in code, name
    # Who can ask for a file that was sealed is said on the card, and what can be read stands
    # where the choice is made.
    assert "who.map((name) => whoLine(ctx, name))," in said
    assert 't("docs.status.types", { size: megabytes(rules.max_bytes) })' in said


def test_no_sentence_about_another_program_is_shown_that_nobody_checked() -> None:
    # The app once wrote a line for a terminal that opened a settings file in the system's
    # editor. What such a line does in another program was never tried with that program: the
    # line is gone, and no script shows a sentence about another program's menus or keys. What
    # the tables may say about other programs is a fixed list of keys
    # (test_what_is_said_about_other_programs in test_gate_interface_texts.py).
    for name, source in scripts().items():
        assert "open_line" not in without_comments(source), f"{name} reads the line"
        shown = "\n".join(quoted_texts(source))
        for word in ("TextEdit", "xdg-open", "pbcopy", "⌘Q", "⌘A", "⌘V", "⌘S"):
            assert word not in shown, f"{name} says {word!r}"
    # The way by hand says where in the file the text goes in one of four fixed lines, chosen
    # by what the look found; where the file could not be read, no line claims a place.
    connect = without_comments(scripts()["connect.js"])
    where = function_body(connect, "function whereToPaste(look, block) {")
    assert sorted(set(re.findall(r'key: "(connect\.hand\.status\.[a-z]+)"', where))) == [
        "connect.hand.status.empty",
        "connect.hand.status.end",
        "connect.hand.status.inside",
        "connect.hand.status.none",
    ]
    assert where.rstrip().endswith("return { key: null, text: whole };")


def test_a_settings_file_is_looked_at_only_on_a_press() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    for name, code in sources.items():
        assert "checkConnect" not in code, f"{name} names the look that is gone"
        if name not in ("api.js", "connect.js"):
            assert "connectLook" not in code, f"{name} looks at a settings file"
    connect = sources["connect.js"]
    assert connect.count("api.connectLook(") == 1
    look = function_body(connect, "async function lookAt(ctx, sheet, view) {")
    assert "settle(ctx, view, await api.connectLook(view.tile.host));" in look
    # The one call is reached from the function a press on a tile runs and from "Look again",
    # and from nowhere else.
    calls = re.findall(r"^.*\blookAt\(ctx, sheet, view\).*$", connect, re.M)
    assert len(calls) == 5  # its definition; the press on a tile; "Look again" in three states
    assert calls[0].startswith("async function lookAt(")
    assert sum("if (!demo) lookAt(ctx, sheet, view);" in line for line in calls) == 1
    assert (
        sum(
            'button(t("connect.button.look"), { kind: "primary", id: "connect-look", '
            "onclick: () => lookAt(ctx, sheet, view) })" in line
            for line in calls
        )
        == 3
    )
    opened = function_body(connect, "export function openConnect(ctx, tile) {")
    assert "if (!demo) lookAt(ctx, sheet, view);" in opened
    # A press on a tile is the one thing that opens its sheet.
    home = sources["home.js"]
    assert home.count("openConnect(") == 1  # the handler of a tile
    assert "onclick: () => (custom ? openCustom(ctx) : openConnect(ctx, entry))," in home
    # No timer in the script of the sheets, and no load or draw that looks.
    assert "setTimeout" not in connect
    assert "setInterval" not in connect
    for head in ("function drawConnect(ctx, sheet, view) {", "function settle(ctx, view, look) {"):
        assert "api." not in function_body(connect, head), head
    # In a demo no look is asked at all.
    assert 'state: demo ? "refused" : "looking", word: demo ? "demo" : "",' in opened
    # What was found is one of the app's words, and each leads to a state of the sheet.
    settle = function_body(connect, "function settle(ctx, view, look) {")
    for word in (
        "no_folder",
        "linked",
        "no_access",
        "unreadable",
        "not_read",
        "busy",
        "unsupported",
        "present_as",
        "present",
        "present_more",
        "expands",
        "unstable",
        "moved",
    ):
        assert f'"{word}"' in settle, word
    assert settle.rstrip().endswith('else view.state = "offer";')


def test_setting_up_ends_by_itself_only_where_the_app_keeps_that_word() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    home = sources["home.js"]
    still = function_body(home, "function firstOpen(ctx) {")
    assert (
        'return Boolean(kept && kept.stored && kept.setup === "open") && ctx.state.demo !== true;'
    ) in still
    done = function_body(home, "async function firstDone(ctx) {")
    assert done.splitlines()[1].strip() == "if (!firstOpen(ctx)) return null;"
    assert 'ctx.applyState(await api.setInterface({ setup: "done" }));' in done
    # Once the vault holds a document, the owner has seen a passage or an app has asked, and an
    # app is registered, the page says so once, on whatever screen it is: after each load.
    settle = function_body(home, "export function settleFirst(ctx) {")
    assert "!firstOpen(ctx) || ctx.ui.home.first.said" in settle
    assert "if (!facts || !facts.document || !facts.asked || !facts.app) return;" in settle
    assert settle.index("ctx.ui.home.first.said = true;") < settle.index("firstDone(ctx);")
    assert "settleFirst(ctx);" in function_body(sources["main.js"], "async function load(names) {")
    facts = function_body(home, "function firstFacts(ctx) {")
    assert "document: documentsHeld(ctx) > 0," in facts
    assert "asked: ctx.ui.home.ask.saw || data.apps.some((app) => Boolean(app.last_seen))," in facts
    assert "app: data.apps.length > 0," in facts
    # Nobody else keeps that word.
    for name, code in sources.items():
        if name not in ("api.js", "home.js", "main.js"):
            assert "setInterface" not in code, name
    assert home.count("firstDone(") == 3  # its definition, by itself, and "Hide"
    # The three cards: never in a demo, drawn in place of the parts they hold, and "Hide" ends
    # them for good. Nothing is drawn twice: card 2 is the box that asks, card 3 the tiles.
    page = function_body(home, "export function homePage(ctx, problem) {")
    assert "const first = firstOpen(ctx);" in page
    assert page.index("? firstSteps(ctx)") < page.index('part("home-ask", ')
    assert home.count("asker(ctx)") == 3  # its definition, card 2, the part of an ordinary day
    assert home.count("tiles(ctx)") == 3  # its definition, card 3, the part of an ordinary day
    cards = function_body(home, "function firstSteps(ctx) {")
    assert 'id: "first-hide",' in cards
    assert "const failed = await firstDone(ctx);" in cards
    assert 'toast(t("home.new.result.hidden"), "plain");' in cards
    assert re.findall(r"cardHead\(\d, facts\.\w+, t\(\"([^\"]+)\"\)\)", home) == [
        "home.new.card1.title",
        "home.ask.title",
        "home.new.card3.title",
    ]
    # The one filled button of the page: "Choose a file" while the vault holds no document,
    # "Ask" once it does, and neither while a passage waits for the owner.
    add = function_body(home, "function addCard(ctx, facts) {")
    assert "const filled = !somethingWaits(ctx);" in add
    assert (
        '{ class: ["btn", filled ? "btn-primary" : "btn-default", "file-button"], '
        'for: "first-file" }'
    ) in add
    box = function_body(home, "function asker(ctx) {")
    assert 'kind: none || somethingWaits(ctx) ? "default" : "primary",' in box
    filled = re.findall(r'kind: "primary"|btn-primary', home)
    assert len(filled) == 1, "Home has a third button that could be filled"


# -- the box that asks, the tiles and their sheets ----------------------------------


def test_a_trial_sends_nothing_when_it_is_answered() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    home = sources["home.js"]
    card = function_body(home, "function trialCard(ctx) {")
    # The two answers of a real card, named by the same keys, the safe one first.
    assert re.findall(r'button\(t\("(card\.button\.\w+)"\), \{', card) == [
        "card.button.no",
        "card.button.yes",
    ]
    # Their handlers call no function of the app: a press says what would have happened.
    assert "api." not in card
    for word in ("await", "async", "fetch", "decide(", "release(", "dismiss("):
        assert word not in card, word
    handlers = re.findall(
        r'id: "trial-(no|yes)",\s*onclick: \(\) => \{(.*?)\n        \},', card, re.S
    )
    assert [which for which, _body in handlers] == ["no", "yes"]
    for which, body in handlers:
        assert f'box.pressed = "{which}";' in body
        assert set(re.findall(r"\b(\w+)\(", body)) == {"render"}, which
    # It is no approval card: a class of its own, neither button filled, neither waiting.
    assert (
        '{ class: "card trial", id: "trial", key: "trial", "aria-label": t("ask.chip.trial") }'
        in card
    )
    assert "approval" not in class_names(home)
    assert "kind:" not in card
    assert "releaseArmed" not in home
    # While a real passage waits for the owner it has no buttons at all, and says why: a press
    # that is harmless here must not train the hand for the card above.
    assert (
        "const real = ctx.data.approvals ? ctx.data.approvals.waiting.length > 0 "
        ": somethingWaits(ctx);"
    ) in card
    foot = card[card.index("let foot;") : card.index('return el(\n    "article",')]
    branches = foot.split("} else ")
    assert len(branches) == 3
    assert branches[0].lstrip().startswith("let foot;\n  if (real) {")
    assert 't("ask.status.real")' in branches[0]
    assert "button(" not in branches[0]
    assert branches[1].startswith("if (box.pressed) {")
    assert 't(box.pressed === "yes" ? "ask.result.yes" : "ask.result.no")' in branches[1]
    assert "card.button." in branches[2]
    # The question is sent from the handler of "Ask" and from nowhere else, and only there.
    for name, code in sources.items():
        if name not in ("api.js", "home.js"):
            assert "tryQuestion" not in code, name
    assert home.count("api.tryQuestion(") == 1
    ask = function_body(home, "async function ask(ctx) {")
    assert "const answer = await api.tryQuestion(question);" in ask
    assert re.findall(r"^.*(?<![\w.])ask\(ctx\).*$", home, re.M) == [
        "async function ask(ctx) {",
        "          ask(ctx);",
    ]
    box = function_body(home, "function asker(ctx) {")
    submit = box[box.index("onsubmit: (event) => {") :]
    assert "ask(ctx);" in submit[: submit.index("},")]
    # Nothing is asked for an empty question or while the vault holds no document.
    assert "if (box.busy || !question || documentsHeld(ctx) === 0) return;" in ask
    # The answer is this page's memory: leaving Home drops it, and nothing keeps it elsewhere.
    leave = function_body(home, "export function leaveHome(ctx) {")
    assert "ctx.ui.home.ask = freshHome().ask;" in leave
    assert 'if (from === "home") leaveHome(ctx);' in sources["main.js"]
    for storage in ("sessionStorage", "localStorage", "indexedDB"):
        assert storage not in home


def test_the_trial_card_shows_the_whole_passage() -> None:
    # The card says "would get only this passage": the passage is shown whole, as on an
    # approval card. A box with a greatest height cut a passage of 702 characters in the
    # middle of a sentence, with nothing to say that more followed.
    css = without_comments(read(STATIC / "home.css"))
    assert css_rules("home.css", ".trial .code") == []
    for selector, rule in re.findall(r"(?m)^\s*(\.trial[^{\n]*) \{\n([^}]*)\}", css):
        assert "max-height" not in rule, selector
        assert "overflow" not in rule, selector
    card = function_body(without_comments(scripts()["home.js"]), "function trialCard(ctx) {")
    # Its lines are folded at the width of the card, so nothing scrolls sideways either.
    assert (
        'el("pre", { class: "code code-wrap released", tabindex: 0 }, '
        'el("code", null, passage.body)),'
    ) in card
    assert "white-space: pre-wrap;" in css_rules("base.css", ".code-wrap")[0]


def test_the_approval_card_shows_the_whole_text_at_every_width() -> None:
    # In a window of 560 px or less the box of "The exact text that would be released" had a
    # greatest height: 448 px of text stood in 280 px with no scrollbar in sight, the last line
    # cut in half, and "Release once" could be pressed without scrolling. No stylesheet gives
    # the text of a card a height of its own: the card grows with its text, as the trial card.
    for name in ("base.css", "app.css", "home.css"):
        css = without_comments(read(STATIC / name))
        found = re.findall(r"(?m)^\s*([^{}\n]*) \{\n([^}]*)\}", css)
        assert found, name
        for selector, rule in found:
            if re.search(r"\.(released|approval|approval-text|code-wrap)(?![\w-])", selector):
                assert "max-height" not in rule, f"{name}: {selector}"
                assert "-webkit-line-clamp" not in rule, f"{name}: {selector}"
    card = function_body(
        without_comments(scripts()["views.js"]), "function approvalCard(ctx, item, index) {"
    )
    assert (
        'el("pre", { class: "code code-wrap released", tabindex: 0, '
        '"aria-label": t("card.label.text") }, el("code", null, item.body)),'
    ) in card
    assert css_rules("app.css", ".approval-text .released") == []
    assert css_rules("base.css", ".released") == []


def test_what_three_readers_of_the_finished_page_found_stays_mended() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    home, views, pages, connect = (
        sources[name] for name in ("home.js", "views.js", "pages.js", "connect.js")
    )
    # A demo opens without the warning to choose which apps are answered: its apps are played.
    noticed = function_body(home, "function choiceNoticed(ctx) {")
    assert "ctx.state.serving.choice_needed" in noticed
    assert "ctx.state.demo !== true" in noticed
    assert "if (choiceNoticed(ctx)) {" in function_body(home, "function notices(ctx) {")
    signature = function_body(home, "function noticesSignature(ctx) {")
    assert 'if (choiceNoticed(ctx)) codes.push("choice");' in signature
    assert home.count("serving.choice_needed") == 1
    # The first moment of the demo says that some rows appear without a yes, and no other does.
    card = function_body(home, "function demoCard(ctx) {")
    assert (
        'beat === "watch" ? el("p", { class: "guide-say", id: "guide-say-2", key: "say-2" }' in card
    )
    assert 'const GUIDE_MORE = { watch: "guide.watch.say.2" };' in home
    # What was released and not read yet says what to tell the app, and where no card waits it
    # does not stand under "Waiting for you": nothing waits for the owner then.
    line = function_body(views, "function grantedLine(ctx, item) {")
    assert 't("home.granted.result.line", {' in line
    english = scripts()["text-en.js"]
    assert (
        '"home.granted.result.line": "{app} may read passage {passage} of “{title}” once, '
        'until {time}. Tell it: “try again”.",'
    ) in english
    waiting = function_body(home, "function waitingPart(ctx, problem) {")
    alone = (
        'if (drawn.cards.length === 0) return [said, el("div", { class: "approval-stack", '
        'key: "home-granted" }, drawn.granted)];'
    )
    assert alone in waiting
    assert waiting.index(alone) < waiting.index('t("home.wait.title"),')
    # A document of one passage says "the whole document", on the card and on the trial card.
    assert (
        't(item.passages === 1 ? "card.status.meta.whole" : "card.status.meta", '
        "{ n: item.characters, passage: item.passage, passages: item.passages })"
    ) in function_body(views, "function approvalCard(ctx, item, index) {")
    assert (
        't(passage.passages === 1 ? "ask.status.meta.whole" : "ask.status.meta", '
    ) in function_body(home, "function trialCard(ctx) {")
    # A row counts in the owner's own text, from two fields of the entry: the passage's own
    # length, and whether a search held any text of a document.
    row = function_body(pages, "export function activityRow(ctx, item) {")
    assert "const none = withoutDocumentText(item);" in row
    assert (
        'const characters = typeof item.passage_characters === "number" ? '
        "item.passage_characters : item.characters;"
    ) in row
    assert (
        '!stopped && none ? el("span", { class: "row-chars" }, t("row.status.notext")) : null,'
        in row
    )
    # Not both: a search that quoted nothing does not show a number of characters beside it.
    assert (
        '!stopped && !none && characters > 0 ? el("span", { class: "row-chars" }, '
        't("row.status.chars", { n: characters })) : null,'
    ) in row
    assert ('return item.tool === "search" && item.document_text === false;') in function_body(
        pages, "export function withoutDocumentText(item) {"
    )
    panel = function_body(views, "function leftBlocks(item, figure) {")
    assert 't("panel.say.1.notext")' in panel
    assert "hosted && !none ?" in panel
    assert "item.hosted === false && !none ?" in panel
    assert (
        'typeof item.passage_characters === "number" ? t("panel.status.both", '
        "{ n: item.passage_characters, total: item.characters })"
    ) in function_body(views, "function simplePanelBody(ctx, panel, item) {")
    # The two figures are a line of words: where they do not fit beside the heading of the
    # panel, they stand under it.
    assert "flex-wrap: wrap;" in css_rules("app.css", ".panel-section .mini-title")[0]
    # The tab "Stopped" does not promise "the exact text that left".
    assert (
        't(shown === "refusal" ? "activity.say.1.stopped" : "activity.say.1")'
    ) in function_body(pages, "export function activityPage(ctx) {")
    # A name the fourth tile does not take is answered under its field.
    custom = function_body(connect, "function drawCustom(ctx, sheet, view) {")
    assert 'else if (form.name && !fits) nameError = t("custom.error.name");' in custom
    assert "error: nameError," in custom
    assert 'texts ? say("custom-texts-say", t("custom.texts.say.1")) : null,' in custom
    # The sentence under the tiles stands there from the first moment.
    tiles = function_body(home, "function tiles(ctx) {")
    assert (
        'el("p", { class: "home-say", id: "home-connect-say", key: "say" }, '
        't("home.connect.say.1")),'
    ) in tiles
    # A file that was refused: its line goes once a later file is sealed, and the message at
    # the foot of the window does not say the line's sentence a second time.
    manage = sources["manage.js"]
    upload = function_body(manage, "export async function uploadFiles(ctx, collection, files) {")
    assert "const before = everyLine(ctx);" in upload
    noted = upload.index("const before = everyLine(ctx);")
    assert noted < upload.index("state.items.concat(batch)")
    assert "dropRefused(ctx, before);" in upload
    assert 'else toast(t("docs.result.noneadded"), "red");' in upload
    assert "batch[0].words()" not in upload
    kept = function_body(manage, "function dropRefused(ctx, before) {")
    assert '!before.includes(item) || item.status !== "error" || item.install === true' in kept
    assert "forgetRefused(ctx, group);" in function_body(
        home, "async function addSample(ctx, group) {"
    )
    # On Home what is said of the sealed files stands under the last of them, and a file that
    # was refused after it under that.
    added = function_body(home, "function addCard(ctx, facts) {")
    assert "const said = lastSealed(chosen.map((entry) => entry.item)) + 1;" in added
    above, said, below = (
        added.index(part)
        for part in (
            "chosen.slice(0, said).map(",
            "who.map((name) => whoLine(ctx, name)),",
            "chosen.slice(said).map(",
        )
    )
    assert above < said < below
    # A sheet under another sheet is out of sight, and the way back from the six pages is its
    # icon alone where the sidebar shows icons.
    assert "visibility: hidden;" in css_rules("base.css", ".sheet[open]:has(~ .sheet[open])")[0]
    narrow = without_comments(read(STATIC / "app.css"))
    hidden = narrow[narrow.index(".sidebar .brand-name,") :]
    assert ".nav-home-label," in hidden[: hidden.index("{")]
    # In the narrow layout what a finger presses is at least 40 px high.
    small = without_comments(read(STATIC / "home.css"))
    small = small[small.index("@media (max-width: 720px) {") :]
    rule = re.search(
        r"\.simple \.btn,\n\s*\.sheet \.btn,\n\s*\.sheet \.input,\n\s*\.simple \.lang-option \{\n"
        r"\s*min-height: 40px;",
        small,
    )
    assert rule is not None
    # So is the lock, which is its icon alone there: 40 px wide. And the words in the empty
    # box are a size smaller, so that the longer of the two languages is not cut.
    assert re.search(r"\n  \.side-foot \.lock-now \{\n    width: 40px;\n", small) is not None
    assert (
        re.search(r"\n  \.asker-input::placeholder \{\n    font-size: var\(--fs-m\);\n  \}", small)
        is not None
    )
    # The box and its answer are never wider than their card, whatever the button's word is.
    assert "min-width: 0;" in css_rules("home.css", ".asker")[0]
    assert "min-width: 0;" in css_rules("home.css", ".trial")[0]
    assert "border: 0;" in css_rules("home.css", ".first-card .trial")[0]


def test_what_two_checks_of_the_mended_page_found_stays_mended() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    connect, manage, main, sheets = (
        sources[name] for name in ("connect.js", "manage.js", "main.js", "sheets.js")
    )
    # While the answer to a name stands under the field of "Other app", the hint is not on the
    # page: the field is marked as not valid and points at the answer, not at the hint.
    custom = function_body(connect, "function drawCustom(ctx, sheet, view) {")
    assert '"aria-invalid": nameError ? "true" : null,' in custom
    assert '"aria-describedby": nameError ? "custom-name-error" : "custom-name-hint",' in custom
    assert '"aria-describedby": "custom-name-hint",' not in custom
    field = function_body(sources["parts.js"], "export function field({")
    assert 'id: `${id}-error`, role: "alert" }, error) : null,' in field
    assert "hint && !error ? el(" in field
    assert "id: `${id}-hint` }, hint) : null," in field
    # A vault with several groups lists the lines of all of them together. Once a file was
    # sealed, the refused lines that stood on the page before this selection go in every
    # group, not only in the group of the sealed file: a line of another group stayed under a
    # card that was marked done.
    lines = function_body(manage, "function everyLine(ctx) {")
    assert "Object.values(ctx.ui.uploads).map((state) => state.items)" in lines
    dropped = function_body(manage, "function dropRefused(ctx, before) {")
    assert "for (const state of Object.values(ctx.ui.uploads)) {" in dropped
    assert "state.items = state.items.filter(" in dropped
    assert "function dropRefused(state, before) {" not in manage
    upload = function_body(manage, "export async function uploadFiles(ctx, collection, files) {")
    assert "const before = state.items;" not in upload
    # The sample is added to the vault's one group, and takes the refused lines of that group.
    assert "dropRefused(ctx, uploadState(ctx, collection).items);" in function_body(
        manage, "export function forgetRefused(ctx, collection) {"
    )
    # "Connect" is off while its call runs. A sheet that asked for the passphrase over it and
    # was closed had no button to give the focus back to, and the focus fell to the page: the
    # button takes it, before the list of apps is asked for again.
    write = function_body(connect, "async function connect(ctx, sheet, view) {")
    assert 'cancelled = error.code === "cancelled";' in write
    back = (
        "    sheet.update();\n"
        '    const pressed = document.getElementById("connect-go");\n'
        "    if (pressed) pressed.focus();\n"
        "  }\n"
    )
    assert back in write
    order = [
        write.index(part)
        for part in ("view.busy = false;", "if (cancelled) {", back, "await ctx.refresh();")
    ]
    assert order == sorted(order)
    # A card that moves up into the place of a line that went is taken out of the page and put
    # in again, and what held the focus in it lost it. A drawing of the page gives it back.
    draw = function_body(main, "function drawInPlace() {")
    assert (
        "  const held = document.activeElement;\n"
        "  render(root, screen());\n"
        "  if (held && held !== document.activeElement && root.contains(held)) "
        "held.focus({ preventScroll: true });\n"
    ) in draw
    # The form "Add app" of the six pages has a sentence of its own for Grok Bot, which is no
    # web chat: it does not say that a Bot calls only servers a company's cloud can reach, and
    # its line in the list does not say that it cannot reach a program on this computer, since
    # a Bot can be allowed to run commands there.
    fields = function_body(
        sheets, "export function appFormFields(form, known, update, prefix, short = false) {"
    )
    assert (
        'entry.id === "grok-bot"\n'
        "            ? `${entry.label} cannot be connected to this vault. It works on a computer "
        "in the cloud, and ${PRODUCT} runs on your Mac only. Use Grok Build on this Mac instead.`\n"
        "            : `${entry.label} cannot use this vault. It only calls servers that "
    ) in fields
    assert (
        'option.id === "grok-bot"\n'
        '                ? "Works on a computer in the cloud"\n'
        "                : !option.local_program\n"
        '                  ? "Cannot reach a program on this Mac"\n'
    ) in fields


def test_polish_keeps_a_word_of_one_letter_with_the_word_after_it() -> None:
    # Lines ended in "i", "w" and "a", and a number parted from its noun. The page binds them
    # with a no-break space, in the text of the table and never in a value; the two texts the
    # owner sends on to an AI app are left exactly as the table has them.
    reader = without_comments(scripts()["texts.js"])
    bind = function_body(reader, "function bind(text) {")
    assert "const one = /(^|[\\s(„])([aiouwz]) /gi;" in bind
    assert (
        'made = text.replace(one, "$1$2\\u00a0").replace(one, "$1$2\\u00a0")'
        '.replace(/\\{n\\} /g, "{n}\\u00a0");'
    ) in bind
    assert 'const SENT_ON = ["ask.sample.question", "connect.ask.start"];' in reader
    text = function_body(reader, "export function t(key, values = {}) {")
    bound = 'if (shown === "pl" && !SENT_ON.includes(key)) found = bind(found);'
    assert bound in text
    # Before the values are put in: a title or a name is never changed.
    assert text.index(bound) < text.index("return found.replace(")
    assert reader.count("bind(") == 2
    # How long ago a tile's app last asked is a value, which `t` leaves alone: the tile binds
    # its number to its unit itself, in either language.
    tile = function_body(
        without_comments(scripts()["home.js"]), "function tile(ctx, entry, apps) {"
    )
    assert (
        'line = t("tile.status.seen", { ago: agoShort(app.last_seen.time, ctx.now)'
        '.replace(/ /g, "\\u00a0") });'
    ) in tile
    # The tables themselves hold ordinary spaces, so they can be read and compared as text.
    for name in TABLES:
        assert "\u00a0" not in scripts()[name], name
        assert "\\u00a0" not in scripts()[name], name
    # The same rule, written here, on sentences of the table: every word of one letter that is
    # followed by a space is bound, also two in a row.
    one = re.compile(r"(^|[\s(„])([aiouwz]) ", re.I)

    def as_the_page(found: str) -> str:
        return one.sub("\\1\\2\u00a0", one.sub("\\1\\2\u00a0", found)).replace("{n} ", "{n}\u00a0")

    polish = table("text-pl.js")
    said = as_the_page(polish["home.new.say.1"][0])
    assert "i\u00a0dopiero" in said
    assert as_the_page("a w tym") == "a\u00a0w\u00a0tym"
    for key, forms in polish.items():
        for form in forms:
            assert re.search(r"(?:^|[\s(„])[aiouwz] ", as_the_page(form), re.I) is None, key
            assert "{n} " not in as_the_page(form), key


def test_home_says_how_many_trial_questions_were_asked_since_the_app_was_started() -> None:
    # "Lock now" needs no passphrase and begins the count since the unlock anew. The count
    # since the app was started is shown wherever it is the larger one, in a line of its own.
    box = function_body(without_comments(scripts()["home.js"]), "function asker(ctx) {")
    assert "const asked = ctx.state.trial ? ctx.state.trial.questions : 0;" in box
    assert "const sinceStart = ctx.state.trial ? ctx.state.trial.since_start : 0;" in box
    since_unlock = (
        'asked > 0 ? el("p", { class: "asker-count", id: "ask-count", key: "count" }, '
        't("ask.status.count", { n: asked })) : null,'
    )
    since_start = (
        'sinceStart > asked ? el("p", { class: "asker-count", id: "ask-count-start", '
        'key: "count-start" }, t("ask.status.count.start", { n: sinceStart })) : null,'
    )
    assert since_unlock in box
    assert since_start in box
    assert box.index(since_unlock) < box.index(since_start)
    # The page holds no figure of its own: both numbers are the app's.
    for name, source in scripts().items():
        if name != "home.js":
            assert "since_start" not in without_comments(source), name


def test_the_column_makes_room_for_the_details_in_a_wide_window() -> None:
    # At 1280 px the open panel lay over the right third of the column: how many characters
    # and when, of every row. Now the column gives the panel its width while it is open.
    css = without_comments(read(STATIC / "home.css"))
    found = re.search(
        r"@media \(min-width: (\d+)px\) \{\n"
        r"  \.simple\.has-panel \.column \{\n"
        r"    padding-right: calc\(var\(--panel-width\) \+ var\(--gutter\)\);\n"
        r"  \}\n\}",
        css,
    )
    assert found is not None
    assert css.count("has-panel") == 2
    shell = function_body(without_comments(scripts()["main.js"]), "function simpleShell() {")
    assert '{ class: ["simple", ctx.panel && "has-panel"], key: "simple" },' in shell

    def size(sheet: str, selector: str, name: str, rule: int = 0) -> int:
        value = re.search(rf"{name}: (\d+)px;", css_rules(sheet, selector)[rule])
        assert value is not None, name
        return int(value.group(1))

    side = size("base.css", ":root", "--side")
    panel = size("home.css", ".simple", "--panel-width")
    gutter = size("home.css", ".simple", "--gutter")
    narrow_gutter = size("home.css", ".simple", "--gutter", rule=1)
    assert (side, panel, gutter, narrow_gutter) == (208, 440, 32, 16)
    # From that width on, what is left for the column is at least what a window of 400 px
    # leaves it; so the rule never makes the column narrower than a phone does.
    wide = int(found.group(1))
    assert wide - side - panel - 2 * gutter == 400 - 2 * narrow_gutter
    # Where that leaves less than four tiles of 124 px need, the tiles of Home stand two by
    # two while the panel is open, as they do on a phone.
    band = re.search(
        rf"@media \(min-width: {wide}px\) and \(max-width: (\d+)px\) \{{\n"
        r"  \.simple\.has-panel \.tiles \{\n"
        r"    grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);\n"
        r"  \}\n\}",
        css,
    )
    assert band is not None
    gap = re.search(r"gap: (\d+)px;", css_rules("home.css", ".tiles")[0])
    assert gap is not None
    room = int(band.group(1)) + 1 - side - panel - 2 * gutter
    assert (room - 3 * int(gap.group(1))) // 4 >= 124
    # The panel itself is as wide as ever, and at 720 px and narrower it covers the page.
    as_ever = "width: min(var(--panel-width), calc(100vw - 48px));"
    assert as_ever in css_rules("app.css", ".panel")[0]
    before, narrow = css.split("@media (max-width: 720px) {")
    assert found.group(0) in before
    assert re.search(r"\.simple \.panel \{\s*width: 100vw;", narrow)
    assert "has-panel" not in narrow


def test_a_time_within_the_next_day_is_written_without_its_date() -> None:
    # "Unlocked until Nov 15, 1:55 AM" took two lines of the sidebar whenever the vault locks
    # itself after midnight. Within 24 hours the time of day says which moment is meant.
    source = without_comments(scripts()["format.js"])
    clock = function_body(source, "export function clock(text, now = new Date()) {")
    assert "const ahead = moment.getTime() - now.getTime();" in clock
    assert (
        "if (dayOffset(moment, now) === 0 || (ahead >= 0 && ahead < 86400000)) "
        "return clockFormat().format(moment);"
    ) in clock
    # Beyond that the date stands with it, as before.
    assert clock.index("ahead < 86400000") < clock.index('once("dated"')
    # The three places that say "until": the sidebar, a release, what is released and not read.
    uses = {
        name: len(re.findall(r"(?<![\w.])clock\(", without_comments(code)))
        for name, code in scripts().items()
    }
    assert {name: count for name, count in uses.items() if count} == {
        "format.js": 1,
        "main.js": 1,
        "views.js": 2,
    }


def test_the_trials_first_sentence_follows_the_answer() -> None:
    home = without_comments(scripts()["home.js"])
    chosen = function_body(home, "function trialSentence(passage) {")
    # It reads who can ask for the document that was found, and how; it is never fixed.
    assert "const apps = passage.apps || [];" in chosen
    assert 'apps.filter((app) => app.mode === "read")' in chosen
    assert "passage.groups > 1" in chosen
    assert re.findall(r't\("(ask\.say\.1[a-z.]*)"', chosen) == [
        "ask.say.1.read",
        "ask.say.1",
        "ask.say.1.none",
        "ask.say.1.connect",
    ]
    order = [
        'if (readers.length) return t("ask.say.1.read", { apps: readers });',
        'if (apps.length) return t("ask.say.1");',
        'return passage.groups > 1 ? t("ask.say.1.none") : t("ask.say.1.connect");',
    ]
    places = [chosen.index(line) for line in order]
    assert places == sorted(places)
    # No draw of the card uses a first sentence without it.
    card = function_body(home, "function trialCard(ctx) {")
    assert 'el("p", { class: "trial-say", id: "trial-say-1" }, trialSentence(passage)),' in card
    outside = home.replace(chosen, "")
    assert re.findall(r'"ask\.say\.1[a-z.]*"', outside) == ['"ask.say.1.nodocs"']
    assert home.count("trialSentence(") == 2
    # The second sentence is the same for every answer: nothing was sent and nothing recorded.
    assert 'el("p", { class: "trial-say", id: "trial-say-2" }, t("ask.say.2")),' in card
    # By which words it was found is said under the passage.
    assert 't("ask.status.words", { words: passage.words.join(", ") })' in card


def test_a_tile_says_nothing_about_a_file() -> None:
    home = without_comments(scripts()["home.js"])
    drawn = function_body(home, "function tile(ctx, entry, apps) {")
    # The line of a tile is read from the vault: whether an app of the tile's name is
    # registered, whether it is paused, when it last asked. Nothing of a look is read.
    assert "const app = apps.find((one) => one.name === entry.name);" in drawn
    for word in ("look", "state ===", ".path", "connected", "present", "api."):
        assert word not in drawn, f"a tile reads {word!r}"
    assert re.findall(r't\("(tile\.status\.[a-z.]+)"', drawn) == [
        "tile.status.custom",
        "tile.status.custom.connect",
        "tile.status.connect",
        "tile.status.paused",
        "tile.status.seen",
        "tile.status.never",
    ]
    # The word "Connected" needs a look at a file: it is the title of the sheet's state
    # "connected" and stands nowhere else, in no script and under no other key.
    connect = without_comments(scripts()["connect.js"])
    everything = "\n".join(
        without_comments(source) for name, source in scripts().items() if name not in TABLES
    )
    assert everything.count('"connect.done.title"') == 1
    state = connect[connect.index('} else if (view.state === "connected") {') :]
    state = state[: state.index('} else if (view.state === "on") {')]
    assert 'head = sheetHead(t("connect.done.title"));' in state
    for name in TABLES:
        texts = table(name)
        assert texts["connect.done.title"][0] in ("Connected", "Połączono"), name
        # No word of a tile says it, in any state.
        for key, values in texts.items():
            if key.startswith("tile."):
                for text in values:
                    assert "onnected" not in text, f"{name} {key}"
                    assert "ołączon" not in text, f"{name} {key}"


def test_connect_sends_what_the_owner_read() -> None:
    connect = without_comments(scripts()["connect.js"])
    # The state that is sent is the one the sheet was drawn from.
    assert re.findall(r"api\.connectWrite\(([^)]*)\)", connect) == ["host, look.state"]
    assert re.findall(r"api\.connectRemove\(([^)]*)\)", connect) == ["host, look.state"]
    for head in (
        "async function connect(ctx, sheet, view) {",
        "async function disconnect(ctx, sheet, view) {",
    ):
        body = function_body(connect, head)
        assert "const host = view.tile.host;" in body, head
        assert "const look = view.look;" in body, head
    api = without_comments(scripts()["api.js"])
    assert (
        'connectWrite: (host, expect) => call("POST", `/api/connect/${part(host)}`, '
        "{ body: { expect } }),"
    ) in api
    assert (
        "connectRemove: (host, expect) => "
        'call("POST", `/api/connect/${part(host)}/remove`, { body: { expect } }),'
    ) in api
    # A look sends the name of the program in the address and nothing else: never a path.
    assert 'connectLook: (host) => call("POST", `/api/connect/${part(host)}/look`),' in api
    assert "const part = encodeURIComponent;" in api
    # A file that changed since the look is said, and the next thing is another look.
    press = function_body(connect, "async function connect(ctx, sheet, view) {")
    assert 'if (error.code === "changed" || error.code === "demo") {' in press
    assert '"changed"' in connect[connect.index("const LOOK_AGAIN = [") :][:120]
    # "Connect" is drawn in the state "offer" and in no other: not where the program is not
    # installed, not after a refusal, not where the command would be rewritten, not under
    # another name, not where the app is on already.
    draw = function_body(connect, "function drawConnect(ctx, sheet, view) {")
    assert connect.count('t("connect.button.connect")') == 1
    press = re.compile(r"(?<![\w.])connect\(ctx, sheet, view\)")
    assert len(press.findall(connect)) == 2  # its definition, and the one button
    states = re.split(r'\} else if \(view\.state === "(\w+)"\) ', draw)
    drawn_in = [
        states[index] for index in range(1, len(states), 2) if press.search(states[index + 1])
    ]
    assert drawn_in == ["offer"]
    assert [states[index] for index in range(1, len(states), 2)] == [
        "failed",
        "offer",
        "connected",
        "on",
        "as",
        "nofolder",
        "expands",
        "hand",
    ]
    for head in (
        "function refusedState(ctx, sheet, view) {",
        "function handState(ctx, sheet, view) {",
    ):
        assert press.search(function_body(connect, head)) is None, head
    # What stands in the way of every connect is said before the press, and the press is off.
    offer = states[states.index("offer") + 1]
    assert "disabled: Boolean(blocked)," in offer
    assert (
        'look.answered_change ? amberNote("connect-notice", t("notice.choice_ahead.say.1"))'
        in offer
    )


def test_disconnect_asks_first() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    for name, code in sources.items():
        if name not in ("api.js", "connect.js"):
            assert "connectRemove" not in code, name
            assert "connectWrite" not in code, name
    connect = sources["connect.js"]
    assert connect.count("api.connectRemove(") == 1
    body = function_body(connect, "async function disconnect(ctx, sheet, view) {")
    asked = body.index("const agreed = await confirmSheet({")
    stop = body.index("if (!agreed) return;")
    sent = body.index("api.connectRemove(")
    assert asked < stop < sent
    assert "api." not in body[:stop]
    # The second sheet says what will happen to the file, by what the look found, and its
    # button is the red one.
    question = body[asked:stop]
    assert 'title: t("disconnect.title", values(view)),' in question
    assert "text: sentence(first, values(view))," in question
    assert 'confirmLabel: t("connect.button.disconnect"),' in question
    assert "danger: true," in question
    assert re.findall(r'"(disconnect\.say\.1[a-z.]*)"', body) == [
        "disconnect.say.1.vault",
        "disconnect.say.1",
        "disconnect.say.1.more",
    ]
    # What became of the file is said in a message, by the word the app answers with.
    assert re.findall(r'(\w+): "(disconnect\.result\.\w+)"', body) == [
        ("removed", "disconnect.result.done"),
        ("no_entry", "disconnect.result.vault"),
        ("differs", "disconnect.result.differs"),
    ]
    assert '"disconnect.result.kept"' in body
    # "Remove this app" on the fourth tile asks first as well.
    remove = function_body(connect, "async function removeCustom(ctx, sheet, view) {")
    assert (
        remove.index("await confirmSheet({")
        < remove.index("if (!agreed) return;")
        < remove.index("api.removeApp(")
    )
    assert connect.count("api.removeApp(") == 1


def test_the_passphrase_for_the_box_is_sent_with_its_scope() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    api = sources["api.js"]
    # After 403 `trial_reauth_required` the sheet is asked for the passphrase of the box, after
    # 403 `reauth_required` for the one that allows a change; what it sends says which.
    call = function_body(api, "async function call(method, path, options = {}) {")
    assert 'const trial = error.code === "trial_reauth_required";' in call
    assert (
        'if ((trial || error.code === "reauth_required") && hooks.reauth && !options.plain) {'
    ) in call
    assert 'const accepted = await hooks.reauth(trial ? "trial" : null);' in call
    assert (
        'reauth: (passphrase, scope) => call("POST", "/api/reauth", '
        "{ body: scope ? { passphrase, scope } : { passphrase }, plain: true }),"
    ) in api
    main = sources["main.js"]
    assert (
        "reauth: (scope) => requestReauth(ctx.state ? ctx.state.demo_passphrase : null, scope) "
    ) in main
    sheet = function_body(
        sources["sheets.js"], "export function requestReauth(demoPassphrase = null, scope = null) {"
    )
    assert "await api.reauth(form.passphrase, scope);" in sheet
    # No other script sends a passphrase again, and no script names the scope but as that word.
    for name, code in sources.items():
        if name not in ("api.js", "sheets.js"):
            assert "api.reauth(" not in code, name
    assert sources["sheets.js"].count("api.reauth(") == 1
    # The word of the scope is written in one place, where the refusal is told apart.
    assert api.count('"trial"') == 1
    for name in ("sheets.js", "main.js", "connect.js", "parts.js"):
        assert '"trial"' not in sources[name], name
    # The box itself never asks for the passphrase and never sees it.
    home = sources["home.js"]
    for word in ("passphrase", "reauth", "requestReauth"):
        assert word not in home, word


def test_the_result_says_that_nothing_was_tried() -> None:
    connect = without_comments(scripts()["connect.js"])
    state = connect[connect.index('} else if (view.state === "connected") {') :]
    state = state[: state.index('} else if (view.state === "on") {')]
    # Where the result is shown, the sheet says in the same place that the entry was written
    # and read back and that this was not tried with the program itself; or, where nothing had
    # to be written, that the entry was there already. One of the two is always drawn.
    line = (
        'el("p", { class: "sheet-result", id: "connect-result", key: "result" }, icon("check"), '
        'el("span", null, t(kept ? "connect.done.status.kept" : "connect.done.status.tried", '
        "values(view)))),"
    )
    assert line in state
    before = state[: state.index(line)]
    assert "?" not in before[before.rindex("say(") :].split("\n", 1)[1], (
        "the line stands under a condition"
    )
    assert connect.count('"connect.done.status.tried"') == 1
    assert connect.count('"connect.done.status.kept"') == 1
    for name in TABLES:
        texts = table(name)
        for key in ("connect.done.status.tried", "connect.done.status.kept"):
            assert "{label}" in texts[key][0], f"{name} {key}"
    english = table("text-en.js")
    assert english["connect.done.status.tried"] == [
        "Written and read back; not tried with {label} itself."
    ]
    assert "not tried with {label} itself" in english["connect.done.status.kept"][0]
    # The check beside it says "written", not "sealed": it is not green.
    rules = css_rules("home.css", ".sheet-result .icon")
    assert rules
    assert all("--green" not in rule for rule in rules)


def test_an_answer_that_was_not_read_back_is_not_shown_as_read_back() -> None:
    connect = without_comments(scripts()["connect.js"])
    body = function_body(connect, "async function connect(ctx, sheet, view) {")
    # The sheet "Connected" says "Written and read back". Where the answer says that the file
    # could not be read back, that sheet is not shown: the one of a connected app is, which
    # says that the entry is in the file. It is drawn from what the answer says of the file
    # and of the app, so no file is looked at for it.
    unread = (
        "if (answer.connected === true && answer.not_read_back === true) "
        "settle(ctx, view, Object.assign({}, look, { state: answer.state, app: answer.app }));"
    )
    read_back = 'else if (answer.connected === true) view.state = "connected";'
    assert unread in body
    assert body.index(unread) < body.index(read_back)
    assert connect.count('view.state = "connected"') == 1
    assert "lookAt(" not in body
    # What that sheet says is no claim about a read: "The entry ... is in ...".
    english = table("text-en.js")
    assert english["connect.on.status.file"] == ["The entry \u201c{entry}\u201d is in {path}."]
    # And it says what was not done, in a line of its own right under that one. The line is
    # drawn only after such an answer: a look, which reads the file, takes it away again.
    noted = "view.unread = answer.connected === true && answer.not_read_back === true;"
    assert noted in body
    assert body.index(noted) < body.index(unread)
    draw = function_body(connect, "function drawConnect(ctx, sheet, view) {")
    on = draw[draw.index('} else if (view.state === "on") {') :]
    on = on[: on.index('} else if (view.state === "as") {')]
    line = (
        'view.unread ? el("p", { class: "sheet-fact", id: "connect-unread" }, '
        't("connect.status.unread")) : null,'
    )
    assert line in on
    assert on.index('id: "connect-on-file"') < on.index(line)
    assert draw.count("view.unread") == 1
    look = function_body(connect, "async function lookAt(ctx, sheet, view) {")
    assert "view.unread = false;" in look
    assert look.index("view.unread = false;") < look.index("api.connectLook(")
    assert "unread: false," in function_body(connect, "export function openConnect(ctx, tile) {")
    # A removal that was not read back: the message says so after "Disconnected", and is not
    # the green one.
    removal = function_body(connect, "async function disconnect(ctx, sheet, view) {")
    assert 'const unread = answer.file === "removed" && answer.not_read_back === true;' in removal
    assert (
        'toast(unread ? [done, t("connect.status.unread")].join(" ") : done, '
        '!unread && (answer.file === "removed" || answer.file === "no_entry") '
        '? "green" : "amber");'
    ) in removal
    # The answer's word is read in these places and in no other.
    assert connect.count("not_read_back") == 3
    assert connect.count('"connect.status.unread"') == 2
    assert english["connect.status.unread"] == [
        "The file was written, but the system did not let it be read back."
    ]


def test_a_file_that_was_being_written_leads_to_another_look() -> None:
    connect = without_comments(scripts()["connect.js"])
    # The app has a word of its own for a file that was written or replaced while it was
    # read. Its sentence says that, and not that the file is a link, too large or somebody
    # else's; and what follows is another look, not the text to paste: the file is in order.
    assert 'busy: "connect.refused.say.1.busy",' in connect
    again = re.search(r"const LOOK_AGAIN = \[([^\]]*)\];", connect)
    paste = re.search(r"const PASTE_INSTEAD = \[([^\]]*)\];", connect)
    assert again is not None
    assert paste is not None
    assert '"busy"' in again.group(1)
    assert '"busy"' not in paste.group(1)
    assert '"not_read"' in paste.group(1)
    for name in TABLES:
        texts = table(name)
        (busy,) = texts["connect.refused.say.1.busy"]
        (not_read,) = texts["connect.refused.say.1.not_read"]
        assert "{path}" in busy, name
        assert busy != not_read, name
        for word in ("1 MiB", "special", "specjaln"):
            assert word not in busy, f"{name}: {word}"
    assert table("text-en.js")["connect.refused.say.1.busy"] == [
        "{path} was being changed while it was read, so {product} changes nothing."
    ]


def test_a_linked_folder_without_a_place_is_worded_without_one() -> None:
    connect = without_comments(scripts()["connect.js"])
    refused = function_body(connect, "function refusedState(ctx, sheet, view) {")
    # Where the system would not say where a linked folder leads, the answer holds no place.
    # The sentence with "{real}" would then read "is a link to , and": another one is said.
    assert (
        'if (word === "linked" && !values(view).real) key = "connect.refused.say.1.linked.unknown";'
    ) in refused
    assert refused.index("linked.unknown") < refused.index('say("connect-say-1", sentence(key,')
    for name in TABLES:
        texts = table(name)
        (with_place,) = texts["connect.refused.say.1.linked"]
        (without,) = texts["connect.refused.say.1.linked.unknown"]
        assert "{real}" in with_place, name
        assert "{real}" not in without, name
        assert "{folder}" in without, name
    assert table("text-en.js")["connect.refused.say.1.linked.unknown"] == [
        "The settings folder {folder} is a link, and {product} does not write through a link."
    ]


# -- the demo's card ----------------------------------------------------------------


def test_the_demo_guide_makes_the_played_app_ask_again_only_on_a_press() -> None:
    sources = {name: without_comments(source) for name, source in scripts().items()}
    home = sources["home.js"]
    # One request, without a body: nothing tells the played app what the owner decided, so the
    # demo is not better than the product. A real app has to be told "try again" as well.
    assert 'demoAgain: () => call("POST", "/api/demo/again"),' in sources["api.js"]
    for name, code in sources.items():
        if name not in ("api.js", "home.js"):
            assert "demoAgain" not in code, name
    assert re.findall(r"api\.demoAgain\(([^)]*)\)", home) == [""]
    ask = function_body(home, "async function askAgain(ctx) {")
    sent = "await api.demoAgain();"
    assert sent in ask
    # Up to the request the function looks at nothing but whether it is already asking.
    for told in ("decision", "release", "decided", "granted", "waiting", "approvals", "guideBeat"):
        assert told not in ask[: ask.index(sent)], f"asking again reads {told} first"
    # It is sent by a press on the one button, and by nothing else: no timer, no load, no draw.
    assert home.count("askAgain(") == 2  # where it is defined, and behind the button
    card = function_body(home, "function demoCard(ctx) {")
    assert "onclick: () => askAgain(ctx) })," in card
    assert card.count('id: "demo-again"') == 1
    assert 'guide.asking ? t("guide.button.busy") : t("guide.button.again")' in card
    # The page has one filled button at most, and the card has none.
    for filled in ('kind: "primary"', "btn-primary"):
        assert filled not in card
    # The card never leads to one answer: after either, the same button.
    moments = re.search(r"const GUIDE = \{(.*?)\n\};", home, re.S)
    assert moments is not None
    assert re.findall(
        r"(\w+): \{ title: \"([\w.]+)\", say: \"([\w.]+)\", again: (\w+) \},", moments.group(1)
    ) == [
        ("watch", "guide.watch.title", "guide.watch.say.1", "false"),
        ("turn", "guide.turn.title", "guide.turn.say.1", "false"),
        ("released", "guide.released.title", "guide.released.say.1", "true"),
        ("kept", "guide.kept.title", "guide.kept.say.1", "true"),
        ("read", "guide.read.title", "guide.read.say.1", "false"),
        ("asked", "guide.asked.title", "guide.asked.say.1", "true"),
    ]
    assert "words && words.again" in card


def test_the_demo_guide_reads_the_lists_and_the_owners_own_press() -> None:
    home = without_comments(scripts()["home.js"])
    # It is drawn in a demo and nowhere else, between "Waiting for you" and the box that asks.
    # The approval cards stand above it and are complete without it.
    assert "return ctx.state.demo === true && !ctx.ui.home.guide.hidden;" in function_body(
        home, "function guideOpen(ctx) {"
    )
    card = function_body(home, "function demoCard(ctx) {")
    assert card.splitlines()[1].strip() == "if (!guideOpen(ctx)) return null;"
    assert '{ class: "card guide", key: "demo-guide", "aria-labelledby": "guide-title" }' in card
    assert 'chip(t("common.chip.demo"), "accent"),' in card
    assert home.count("demoCard(") == 2
    page = function_body(home, "export function homePage(ctx, problem) {")
    order = [
        "notices(ctx),",
        "waitingPart(ctx, problem),",
        "demoCard(ctx),",
        "? firstSteps(ctx)",
        'part("home-ask", t("home.ask.title"), null, asker(ctx)),',
        'part("home-connect", t("home.connect.title"), null, tiles(ctx)),',
        "leftPart(ctx),",
    ]
    places = [page.index(part) for part in order]
    assert places == sorted(places)
    assert "approvalCards(" not in card
    # Where the demo is, it reads from the three lists the page loads anyway and from what the
    # owner pressed on this page. The state says only that this is a demo.
    beat = function_body(home, "function guideBeat(ctx) {")
    assert "if (!approvals || !left.loaded || !ctx.stops) return null;" in beat
    assert "ctx.ui.decided.values()" in beat
    assert "ctx.state" not in beat
    assert re.findall(r"ctx\.state\.(\w+)", card) == ["launch"]
    # A passage that waits comes first: the card then says nothing but that it is the owner's turn.
    returns = re.findall(r'return "(\w+)";', beat)
    assert returns == ["turn", "read", "released", "kept", "asked", "watch"]
    assert 'if (waiting.length) return "turn";' in beat
    # What it remembers is this page's memory, forgotten with everything else when the vault locks.
    main = without_comments(scripts()["main.js"])
    assert "home: freshHome()," in function_body(main, "function freshUi() {")
    assert "ctx.ui = freshUi();" in function_body(main, "function forget() {")
    fresh = function_body(home, "export function freshHome() {")
    assert (
        "guide: { hidden: false, decision: null, noted: new WeakSet(), read: null, asking: false },"
        in fresh
    )
    # "Hide" is that memory too: the card is back when the page is loaded again.
    assert "guide.hidden = true;" in card
    # The command that starts a vault of one's own is the one the app sent, shown from the
    # owner's first answer on, with "Copy" and with nothing that starts anything.
    assert "const command = (ctx.state.launch || {}).command;" in card
    assert 'copyLine(ctx, "guide-copy", command, t("bar.label.command"))' in card
    assert 'const answered = beat !== null && beat !== "watch" && beat !== "turn";' in card
    assert card.count("onclick") == 2  # "Hide", and the button that has the played app ask again


def test_the_demo_guide_says_its_sentences_from_the_tables_and_names_no_time() -> None:
    home = without_comments(scripts()["home.js"])
    card = function_body(home, "function demoCard(ctx) {")
    assert sentences_of(card) == []
    # Every moment has its title and its one sentence in both tables, and the card its second
    # sentence, which leads to a vault of the owner's own.
    for name in TABLES:
        texts = table(name)
        said = {key: value for key, value in texts.items() if key.startswith("guide.")}
        assert sorted(said) == sorted(
            [
                f"guide.{moment}.{what}"
                for moment in ("watch", "turn", "released", "kept", "read", "asked")
                for what in ("title", "say.1")
            ]
            + ["guide.watch.say.2"]
            + ["guide.say.2", "guide.button.again", "guide.button.busy", "guide.label.hide"]
        ), name
        for key, values in said.items():
            for text in values:
                # No sentence of the card names a time: when the played app asks is not the
                # page's to promise.
                assert re.search(r"\b(seconds?|minutes?|moments?|soon)\b", text, re.I) is None, (
                    name,
                    key,
                )
                assert re.search(r"\b(sekund|minut|chwil|wkrótce|zaraz)", text, re.I) is None, (
                    name,
                    key,
                )
                assert re.search(r"\d", text) is None, (name, key)
    english = table("text-en.js")
    # Either answer is fine, and the card says so where the owner decides.
    assert "either answer is fine" in english["guide.turn.say.1"][0]
    # After a yes nothing has been sent yet: the played app has to ask again, as a real one has.
    assert "ask again" in english["guide.released.say.1"][0]
    assert english["guide.button.again"] == ["Have the played app ask again"]
    # Settings and the bar leave the command out in a demo: there it would not open this vault
    # again. The card shows it, under the sentence that says what it is for.
    settings = function_body(
        without_comments(scripts()["pages.js"]), "export function settingsPage(ctx) {"
    )
    assert "const command = state.demo ? null : (state.launch || {}).command;" in settings
    bar = function_body(
        without_comments(scripts()["parts.js"]), "export function problemBar(ctx, problem) {"
    )
    assert "const command = ctx.state && ctx.state.demo ? null : launch.command;" in bar


# -- the simple frame and its plain pages -------------------------------------------


def test_the_simple_scripts_hold_no_sentence() -> None:
    # Home, the sheets of its tiles, the plain pages and the screens before the vault is open
    # take every text from the tables: their scripts hold no quoted text that a person would
    # read as words, and no label either. What they quote are keys, classes, addresses, ids
    # and names of the code. (Pro's scripts keep their sentences; the two sample documents in
    # manage.js are documents.)
    for name in ("home.js", "connect.js", "pages.js", "gateway.js"):
        code = without_comments(scripts()[name])
        assert sentences_of(code) == [], name
        for text in quoted_texts(code):
            assert re.match(r"[A-Z][a-z]+( [a-z]+)*$", text.strip()) is None, (name, text)
            # No quoted text of two words either, unless it is a list of classes.
            if len(text.split()) == 2:
                assert re.fullmatch(r"[a-z0-9 -]+|\\u0000 \\u0000", text.strip()), (name, text)
        assert re.search(r'import \{[^}]*\bt\b[^}]*\} from "\./texts\.js";', code), name
    for name in ("home.js", "connect.js", "pages.js"):
        code = without_comments(scripts()[name])
        assert 'from "./brand.js"' not in code, "the product's name is a place of the tables"
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
    # Top right of the four screens, through their one frame.
    gateway = without_comments(scripts()["gateway.js"])
    frame = function_body(gateway, "function gateScreen(ctx, key, content) {")
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
        "export function requestReauth(demoPassphrase = null, scope = null) {",
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
        "export function copyBlock(ctx, id, label, text) {",
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
        "export function requestReauth(demoPassphrase = null, scope = null) {",
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
            # In the tables the protocol is named under a few fixed keys
            # (test_the_protocol_is_named_only_on_the_custom_tile).
            if name not in ("sheets.js", *TABLES):
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
