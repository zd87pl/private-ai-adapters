"""Run the Python snippets of README.md and docs/GUIDE.md for real.

``test_docs.py`` checks that the snippets compile. This file executes them, each in
a fresh interpreter, against a tiny offline model, so that an API change which the
documentation did not follow fails the suite. The snippets run exactly as printed,
with one adaptation: the default base model is replaced by the fixture model.

README blocks run one per process. Guide blocks run one process per ``##`` section,
in order, because the guide's snippets build on the ones before them in a section.
Only exit status is asserted: what a two-layer toy model answers is not the point.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from wdva import SecretKey

pytestmark = pytest.mark.ml

ROOT = Path(__file__).resolve().parent.parent
HUB_ID = "HuggingFaceTB/SmolLM2-360M-Instruct"

# What each group of snippets expects to find in its working directory, as
# "file in the world" or "file in the world->path in the sandbox". A section that
# gains Python snippets needs an entry here; the test says so when one is missing.
README_NEEDS: dict[str, list[list[str]]] = {
    "Python API": [["notes.txt"], ["notes.key", "my-adapter"]],
    "LangChain": [["notes.key", "notes.wdva"]],
}
GUIDE_NEEDS: dict[str, list[str]] = {
    "Key management": [],
    "Training": ["notes.txt", "qa.jsonl", "notes.key"],
    "Inference": ["notes.key", "notes.wdva"],
    "Working with PEFT adapters": ["notes.key", "my-adapter"],
    "Serving several users from one process": [
        "notes.key->keys/alice.key",
        "notes.wdva->vaults/alice.wdva",
    ],
    "LangChain": ["notes.key", "notes.wdva"],
    "Troubleshooting": ["notes.key", "notes.wdva"],
    "FAQ": ["notes.key", "my-adapter"],
}
# The second README snippet claims to need no ML stack. Hold it to that.
CORE_ONLY = ("README.md", "Python API", 1)
NO_ML_IMPORTS = """
import sys as _sys
_heavy = [m for m in ("torch", "transformers", "peft", "safetensors") if m in _sys.modules]
assert not _heavy, f"this snippet is documented as core-only but imported {_heavy}"
"""


def _python_blocks(page: Path) -> list[tuple[str, str, int]]:
    """(section, code, line) for every ```python block, with the ``##`` section it is in."""
    section, out, inside, body, start = "(top)", [], False, [], 0
    for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1):
        if not inside:
            if line.startswith("## "):
                section = line[3:].strip()
            elif re.match(r"^```(python|py)\s*$", line):
                inside, body, start = True, [], number + 1
        elif line.strip() == "```":
            out.append((section, "\n".join(body) + "\n", start))
            inside = False
        else:
            body.append(line)
    return out


def _groups() -> list[tuple[str, str, int, str]]:
    """(page, section, index within the section, code) for every process to run."""
    groups = []
    if (ROOT / "README.md").is_file():
        seen: dict[str, int] = {}
        for section, code, _ in _python_blocks(ROOT / "README.md"):
            index = seen.get(section, 0)
            seen[section] = index + 1
            groups.append(("README.md", section, index, code))
    guide = ROOT / "docs" / "GUIDE.md"
    if guide.is_file():
        merged: dict[str, str] = {}
        for section, code, line in _python_blocks(guide):
            merged[section] = merged.get(section, "") + f"\n# -- block at line {line} --\n{code}"
        groups += [("docs/GUIDE.md", section, 0, code) for section, code in merged.items()]
    return groups


GROUPS = _groups()


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory, model_dirs: dict[str, Path]) -> Path:
    """The files the snippets assume: notes, a key, a trained vault, a plain PEFT adapter."""
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM

    from wdva import WDVA, TrainingConfig
    from wdva.documents import load_examples

    out = tmp_path_factory.mktemp("docs-world")
    for name in ("notes.txt", "qa.jsonl"):
        shutil.copy(ROOT / "examples" / "data" / name, out / name)
    key = SecretKey.generate()
    key.save(out / "notes.key")
    engine = WDVA(model_dirs["llama"], device="cpu")
    engine.train(
        out / "notes.wdva",
        key=key,
        documents=[out / "notes.txt"],
        examples=load_examples(out / "qa.jsonl"),
        config=TrainingConfig(epochs=5, max_length=120),
    )
    engine.close()

    # An adapter "trained elsewhere": plain PEFT, saved the way PEFT saves.
    base = AutoModelForCausalLM.from_pretrained(model_dirs["llama"])
    config = LoraConfig(r=4, lora_alpha=8, target_modules="all-linear", task_type="CAUSAL_LM")
    get_peft_model(base, config).save_pretrained(out / "my-adapter", save_embedding_layers=False)
    return out


def _sandbox(root: Path, world: Path, needs: list[str]) -> Path:
    for item in needs:
        source, _, target = item.partition("->")
        src, dst = world / source, root / (target or source)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy(src, dst)
            if dst.suffix == ".key":
                os.chmod(dst, 0o600)
    return root


def test_the_documentation_has_snippets_to_run() -> None:
    import wdva

    assert wdva.DEFAULT_BASE_MODEL == HUB_ID, "update HUB_ID: the snippets name the default model"
    pages = {page for page, _, _, _ in GROUPS}
    assert pages == {"README.md", "docs/GUIDE.md"}
    assert len(GROUPS) >= 8


@pytest.mark.parametrize(
    ("page", "section", "index", "code"),
    GROUPS,
    ids=[f"{page}:{section}#{index + 1}" for page, section, index, _ in GROUPS],
)
def test_documented_python_runs(
    page: str,
    section: str,
    index: int,
    code: str,
    world: Path,
    model_dirs: dict[str, Path],
    tmp_path: Path,
) -> None:
    if page == "README.md":
        per_section = README_NEEDS.get(section, [])
        needs = per_section[index] if index < len(per_section) else None
    else:
        needs = GUIDE_NEEDS.get(section)
    assert needs is not None, (
        f"{page}, section {section!r}: new Python snippets. Say in tests/test_docs_snippets.py "
        "which files they expect, so that they are run."
    )
    if "langchain" in code and importlib.util.find_spec("langchain_core") is None:
        pytest.skip("needs the langchain extra")

    fixture = str(model_dirs["llama"])
    adapted = code.replace(f'"{HUB_ID}"', repr(fixture)).replace("WDVA()", f"WDVA({fixture!r})")
    if (page, section, index) == CORE_ONLY:
        adapted += NO_ML_IMPORTS
    cwd = _sandbox(tmp_path / "run", world, needs)
    cwd.mkdir(exist_ok=True)
    script = tmp_path / "snippet.py"
    script.write_text(adapted, encoding="utf-8")
    env = dict(
        os.environ,
        HF_HUB_OFFLINE="1",
        HF_HUB_DISABLE_PROGRESS_BARS="1",
        TOKENIZERS_PARALLELISM="false",
        TRANSFORMERS_VERBOSITY="error",
        # The fixture model is tiny: more threads only fight over the cores.
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
    )
    for name in ("WDVA_KEY", "WDVA_KEY_FILE", "WDVA_PASSPHRASE"):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{page}, section {section!r}: the documented code no longer runs.\n"
        f"--- stderr (end) ---\n{proc.stderr[-3000:]}\n--- stdout (end) ---\n{proc.stdout[-1000:]}"
    )
