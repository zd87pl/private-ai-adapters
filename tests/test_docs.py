"""The documentation must stay true to the code.

These checks are cheap and need no model: every link between pages resolves, every
command line shown is one the real parser accepts, every Python snippet at least
compiles, and the claims this project was rebuilt to stop making do not creep back.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

from wdva.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not (ROOT / "README.md").is_file() or not (ROOT / "docs").is_dir(),
    reason="the documentation is not part of this install",
)


def _pages() -> list[Path]:
    top = ["README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md"]
    pages = [ROOT / name for name in top]
    pages += sorted((ROOT / "docs").glob("*.md"))
    pages += sorted((ROOT / "examples").glob("*.md"))
    return [page for page in pages if page.is_file()]


PAGES = _pages()
page_param = pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))

_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>```+|~~~+)(?P<info>[^\n]*)$")


def _split(text: str) -> tuple[str, list[tuple[str, str, int]]]:
    """Return the prose (code blocks blanked out) and the blocks as (language, code, line)."""
    prose: list[str] = []
    blocks: list[tuple[str, str, int]] = []
    fence = ""
    language = ""
    start = 0
    body: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _FENCE.match(line)
        if not fence:
            if match:
                fence, language = match["fence"], match["info"].strip().lower()
                start, body = number + 1, []
                prose.append("")
            else:
                prose.append(line)
        elif match and match["fence"].startswith(fence) and not match["info"].strip():
            blocks.append((language, "\n".join(body), start))
            fence = ""
            prose.append("")
        else:
            body.append(line)
            prose.append("")
    assert not fence, "unterminated code block"
    return "\n".join(prose), blocks


def _slugs(page: Path) -> set[str]:
    """The anchors GitHub generates for a page's headings."""
    prose, _ = _split(page.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    out: set[str] = set()
    for line in prose.splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match[1])  # keep link text only
        title = re.sub(r"[`*]", "", title)
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        out.add(slug if count == 0 else f"{slug}-{count}")
    return out


_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_REFERENCE = re.compile(r"^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)


@page_param
def test_links_between_pages_resolve(page: Path) -> None:
    prose, _ = _split(page.read_text(encoding="utf-8"))
    prose = re.sub(r"`[^`\n]*`", "", prose)  # inline code is not a link
    targets = _LINK.findall(prose) + _REFERENCE.findall(prose)
    broken: list[str] = []
    for target in targets:
        if re.match(r"^[a-z][a-z0-9+.\-]*:", target):  # https:, mailto: ...
            continue
        path_part, _, anchor = target.partition("#")
        dest = (page.parent / path_part).resolve() if path_part else page
        if not dest.exists():
            broken.append(f"{target}: no such file")
        elif ROOT not in (dest, *dest.parents):
            broken.append(f"{target}: points outside the repository")
        elif anchor and dest.suffix == ".md" and anchor not in _slugs(dest):
            broken.append(f"{target}: no heading with that anchor")
    assert not broken, f"{page.relative_to(ROOT)}: " + "; ".join(broken)


def _command_lines(code: str) -> list[str]:
    """The ``wdva`` invocations in a shell block, with continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", code)
    out = []
    for raw in joined.splitlines():
        line = raw.strip()
        if line.startswith("$ "):
            line = line[2:].strip()
        if re.match(r"^(\w+=\S*\s+)*wdva\s", line + " "):
            out.append(line)
    return out


def _wdva_argv(line: str) -> list[str]:
    tokens = shlex.split(line, comments=True)
    while tokens and re.match(r"^\w+=", tokens[0]):  # leading VAR=value assignments
        tokens.pop(0)
    assert tokens[:1] == ["wdva"]
    argv: list[str] = []
    for token in tokens[1:]:
        if token in {"|", "||", "&&", ";", ">", ">>", "<", "2>", "2>&1"}:
            break
        argv.append(token)
    return argv


def _documented_commands() -> list[tuple[str, int, str]]:
    found = []
    for page in PAGES:
        _, blocks = _split(page.read_text(encoding="utf-8"))
        for language, code, line in blocks:
            if language in {"bash", "sh", "shell", "console", "powershell", ""}:
                found += [(str(page.relative_to(ROOT)), line, cmd) for cmd in _command_lines(code)]
    return found


def test_every_documented_command_line_is_accepted_by_the_parser() -> None:
    commands = _documented_commands()
    assert len(commands) >= 20, "the documentation should show the command line in use"
    parser = build_parser()
    rejected = []
    for page, line, command in commands:
        try:
            parser.parse_args(_wdva_argv(command))
        except SystemExit as exc:
            if exc.code not in (0, None):  # --version and --help exit cleanly
                rejected.append(f"{page} (block at line {line}): {command}")
    assert not rejected, "the parser rejects:\n" + "\n".join(rejected)


def test_every_subcommand_is_documented() -> None:
    documented = {_wdva_argv(command)[0] for _, _, command in _documented_commands()}
    subcommands = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    missing = set(subcommands.choices) - documented
    assert not missing, f"no example in the docs for: {sorted(missing)}"


@page_param
def test_python_snippets_compile(page: Path) -> None:
    _, blocks = _split(page.read_text(encoding="utf-8"))
    for language, code, line in blocks:
        if language in {"python", "py"}:
            try:
                compile(code, f"{page.name}:{line}", "exec")
            except SyntaxError as exc:
                pytest.fail(f"{page.relative_to(ROOT)}, block at line {line}: {exc}")


# Phrases the earlier README used and that nothing in the code can back up. Terms a
# threat model has to be able to name in order to disclaim them (trusted execution,
# zero knowledge) are deliberately not on this list.
_UNSUPPORTED_CLAIMS = [
    r"military[- ]grade",
    r"bank[- ]grade",
    r"unbreakable",
    r"unhackable",
    r"100\s?% (secure|private)",
    r"(hipaa|gdpr|ccpa|soc ?2)[- ]compliant",
    r"built[- ]in compliance",
]


@page_param
def test_no_claims_the_code_cannot_back(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    hits = []
    for pattern in _UNSUPPORTED_CLAIMS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"line {line}: {match[0]!r}")
    assert not hits, f"{page.relative_to(ROOT)} makes a claim we removed on purpose: {hits}"


def test_the_readme_install_line_matches_the_package() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    name = re.search(r'^name = "([^"]+)"', pyproject, re.MULTILINE)
    assert name is not None
    assert name[1] == "wdva"
    extras = set(
        re.findall(
            r"^(\w+) = \[",
            pyproject.split("[project.optional-dependencies]")[1].split("[project.scripts]")[0],
            re.MULTILINE,
        )
    )
    used = set()
    for group in re.findall(r"wdva\[([\w,]+)\]", readme):
        used |= set(group.split(","))
    assert used, "the README should show how to install an extra"
    assert used <= extras, f"the README names extras that do not exist: {used - extras}"
