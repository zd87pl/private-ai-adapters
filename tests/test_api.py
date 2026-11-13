"""The public surface of the package: what is exported, and what importing it costs."""

from __future__ import annotations

import re
import subprocess
import sys

import pytest

import wdva
from wdva import MissingDependencyError

ML_NAMES = {
    "DEFAULT_BASE_MODEL",
    "WDVA",
    "AdapterInfo",
    "BaseModelMismatchWarning",
    "TrainingConfig",
    "TrainingResult",
    "TrainingWarning",
    "train_adapter",
}


def test_every_exported_name_resolves() -> None:
    assert sorted(wdva.__all__) == sorted(set(wdva.__all__))  # no duplicates
    for name in wdva.__all__:
        assert getattr(wdva, name) is not None, name
    assert set(wdva.__all__) >= ML_NAMES
    listing = dir(wdva)
    assert listing == sorted(listing)
    assert set(listing) >= set(wdva.__all__) | {"vault", "keys", "bundle", "__version__"}
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = wdva.nope


def test_version_is_a_plain_release_string() -> None:
    # Three numeric parts; the third may carry a pre-release suffix such as a1, b2 or rc1.
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", wdva.__version__)


def test_importing_the_package_does_not_import_the_ml_stack() -> None:
    """`import wdva` must stay cheap: no torch, transformers, peft, pydantic or LangChain."""
    code = (
        "import sys, wdva\n"
        "from wdva import *\n"
        "import wdva.cli, wdva.training, wdva.engine, wdva.documents\n"
        "heavy = [m for m in ('torch', 'transformers', 'peft', 'safetensors', 'pypdf',"
        " 'pydantic', 'langchain_core', 'numpy') if m in sys.modules]\n"
        "print(heavy)\n"
    )
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_ml_entry_points_explain_the_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    from wdva import _ml

    monkeypatch.setattr(_ml, "_ML_MODULES", ("a_module_that_does_not_exist",))
    with pytest.raises(MissingDependencyError, match=r"pip install 'wdva\[ml\]'"):
        wdva.WDVA("some-org/some-model")
    with pytest.raises(MissingDependencyError, match=r"pip install 'wdva\[ml\]'"):
        wdva.train_adapter("some-org/some-model", "never.wdva", key=bytes(32), texts=["hello"])
    # The error is also an ImportError, so `except ImportError` keeps working.
    assert issubclass(MissingDependencyError, ImportError)


def test_error_hierarchy() -> None:
    for name in (
        "VaultFormatError",
        "WrongKeyError",
        "IntegrityError",
        "KeyWipedError",
        "AdapterMismatchError",
        "TrainingError",
        "MissingDependencyError",
    ):
        assert issubclass(getattr(wdva, name), wdva.WDVAError), name
    assert issubclass(wdva.VaultFormatError, ValueError)
    assert issubclass(wdva.WDVAWarning, UserWarning)
    assert issubclass(wdva.InsecureKeyFileWarning, wdva.WDVAWarning)
