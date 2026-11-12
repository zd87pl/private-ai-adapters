"""The shared base model must survive anything an adapter vault can contain.

One engine serves many adapters in turn. If loading or removing an adapter could
change the base model, every later user would silently get wrong answers. These
tests build adapters with plain PEFT that use features which modify base weights
or base structure, and check that the engine refuses them without touching the
model, and that the base model is bit-for-bit intact afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from wdva import AdapterBundle, AdapterMismatchError, SecretKey, WDVAError

from .test_pipeline import FACTS, logits, make_engine, quick_config

pytestmark = pytest.mark.ml


def snapshot(model: Any) -> dict[str, Any]:
    state = {f"param:{name}": p.detach().clone() for name, p in model.named_parameters()}
    state.update({f"buffer:{name}": b.detach().clone() for name, b in model.named_buffers()})
    return state


def assert_untouched(model: Any, before: dict[str, Any], modules: list[str]) -> None:
    import torch

    after = snapshot(model)
    assert list(after) == list(before), "parameters or buffers were added or removed"
    changed = [name for name in before if not torch.equal(before[name], after[name])]
    assert changed == [], f"base tensors changed: {changed[:3]}"
    assert [name for name, _ in model.named_modules()] == modules
    assert all(a.dtype == before[name].dtype for name, a in after.items())


def peft_adapter(model_dir: Path, tmp_path: Path, **config: Any) -> AdapterBundle:
    """Train nothing: build an adapter with plain PEFT and perturb everything it owns."""
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM

    torch.manual_seed(11)
    model = get_peft_model(
        AutoModelForCausalLM.from_pretrained(model_dir),
        LoraConfig(r=2, target_modules="all-linear", task_type="CAUSAL_LM", **config),
    )
    with torch.no_grad():
        for _, param in model.named_parameters():
            if param.requires_grad:
                param.add_(torch.randn_like(param) * 0.5)
    out = tmp_path / "peft-adapter"
    model.save_pretrained(out)
    return AdapterBundle.from_peft_dir(out)


@pytest.mark.parametrize(
    "config",
    [
        {"bias": "all"},
        {"bias": "lora_only"},
        {"modules_to_save": ["lm_head"]},
    ],
    ids=["bias-all", "bias-lora-only", "modules-to-save"],
)
def test_adapters_that_overwrite_base_weights_are_refused(
    model_dir: Path, tmp_path: Path, config: dict[str, Any]
) -> None:
    """These load fine in plain PEFT, where they replace base weights for good."""
    engine = make_engine(model_dir)
    try:
        model = engine.model
        before, modules = snapshot(model), [name for name, _ in model.named_modules()]
        base_logits = logits(engine)
        bundle = peft_adapter(model_dir, tmp_path, **config)

        with pytest.raises(AdapterMismatchError, match="base model"):
            engine.load(bundle)

        assert not engine.is_loaded
        assert_untouched(engine.model, before, modules)
        engine.unload()
        assert_untouched(engine.model, before, modules)
        import torch

        assert torch.equal(logits(engine), base_logits)
    finally:
        engine.close()


def _with_config(bundle: AdapterBundle, **changes: Any) -> AdapterBundle:
    return AdapterBundle({**bundle.adapter_config, **changes}, bundle.weights, dict(bundle.info))


@pytest.mark.parametrize(
    "changes",
    [
        {"layer_replication": [[0, 2], [1, 2]]},  # rebuilds the layer stack
        {"kasa_config": {"enabled": True}},  # rewrites base weights in place
        {"modules_to_save": ["lm_head"]},
        {"trainable_token_indices": [1, 2, 3]},
        {"target_parameters": ["lm_head.weight"]},
        {"megatron_config": {"anything": 1}},
        {"bias": "all"},
        {"use_qalora": True},
        {"a_setting_from_the_future": {"enabled": True}},
        {"target_modules": []},
    ],
    ids=lambda change: next(iter(change)),
)
def test_configuration_that_would_change_the_base_model_is_refused_untouched(
    model_dir: Path, tmp_path: Path, key: SecretKey, changes: dict[str, Any]
) -> None:
    """Only structural settings reach PEFT; anything else that is switched on is an error."""
    engine = make_engine(model_dir)
    try:
        engine.train(tmp_path / "ok.wdva", key=key, texts=FACTS, config=quick_config(epochs=20))
        good = AdapterBundle.load(tmp_path / "ok.wdva", key=key)
        model = engine.model
        before, modules = snapshot(model), [name for name, _ in model.named_modules()]

        with pytest.raises(AdapterMismatchError):
            engine.load(_with_config(good, **changes))

        assert not engine.is_loaded
        assert engine.model is model  # not even discarded and reloaded
        assert_untouched(model, before, modules)
        engine.load(good)  # and the engine still works
        assert engine.is_loaded
    finally:
        engine.close()


def test_a_half_finished_injection_is_rolled_back(
    model_dir: Path, tmp_path: Path, key: SecretKey
) -> None:
    """PEFT wraps some layers, then fails on one it cannot wrap: nothing may be left behind."""
    from wdva import TrainingError

    engine = make_engine(model_dir)
    try:
        engine.train(tmp_path / "ok.wdva", key=key, texts=FACTS, config=quick_config(epochs=20))
        good = AdapterBundle.load(tmp_path / "ok.wdva", key=key)
        model = engine.model
        before, modules = snapshot(model), [name for name, _ in model.named_modules()]
        # The final normalisation layer exists in both fixtures and is not a type LoRA can wrap.
        norm = "ln_f" if "gpt2" in str(model_dir) else "norm"
        targets = [*sorted(good.adapter_config["target_modules"]), norm]

        with pytest.raises(AdapterMismatchError, match="does not fit"):
            engine.load(_with_config(good, target_modules=targets))
        assert_untouched(model, before, modules)

        with pytest.raises(TrainingError, match="cannot attach"):
            engine.train(
                tmp_path / "bad.wdva", key=key, texts=FACTS,
                config=quick_config(target_modules=targets),
            )  # fmt: skip
        assert not (tmp_path / "bad.wdva").exists()
        assert_untouched(model, before, modules)

        # The next adapter trained on this engine is a clean one that loads elsewhere.
        engine.train(tmp_path / "next.wdva", key=key, texts=FACTS, config=quick_config(epochs=20))
        clean = make_engine(model_dir)
        try:
            clean.load(tmp_path / "next.wdva", key=key)
        finally:
            clean.close()
    finally:
        engine.close()


def test_load_and_unload_leave_gradient_flags_and_structure_alone(
    model_dir: Path, tmp_path: Path, key: SecretKey
) -> None:
    engine = make_engine(model_dir)
    try:
        model = engine.model
        for index, param in enumerate(model.parameters()):
            param.requires_grad_(index % 2 == 0)
        flags = [p.requires_grad for p in model.parameters()]
        before, modules = snapshot(model), [name for name, _ in model.named_modules()]
        engine.train(tmp_path / "ok.wdva", key=key, texts=FACTS, config=quick_config(epochs=20))
        assert [p.requires_grad for p in model.parameters()] == flags
        for _ in range(3):
            engine.load(tmp_path / "ok.wdva", key=key)
            engine.unload()
            assert [p.requires_grad for p in model.parameters()] == flags
        assert_untouched(model, before, modules)
    finally:
        engine.close()


@pytest.mark.parametrize("dtype", ["bfloat16", "float16"])
def test_training_on_a_half_precision_engine_changes_no_base_tensor(
    llama_dir: Path, tmp_path: Path, key: SecretKey, dtype: str
) -> None:
    """Parameters and buffers alike: the rotary-frequency buffers stay float32 and exact."""
    import torch

    from wdva import WDVA

    engine = WDVA(str(llama_dir), device="cpu", dtype=dtype)
    try:
        model = engine.model
        before, modules = snapshot(model), [name for name, _ in model.named_modules()]
        dtypes = {name: tensor.dtype for name, tensor in before.items()}
        assert torch.float32 in dtypes.values()  # the buffers this test is about
        assert getattr(torch, dtype) in dtypes.values()
        result = engine.train(tmp_path / "h.wdva", key=key, texts=FACTS, config=quick_config())
        assert result.loss_after < result.loss_before
        assert_untouched(model, before, modules)
    finally:
        engine.close()


def test_a_base_model_whose_structure_changed_is_discarded_not_trusted(
    model_dir: Path, tmp_path: Path, key: SecretKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second line of defence: if removal ever leaves the model altered, reload it."""
    import torch

    from wdva import _ml

    engine = make_engine(model_dir)
    try:
        engine.train(tmp_path / "ok.wdva", key=key, texts=FACTS, config=quick_config(epochs=20))
        base_logits = logits(engine)
        engine.load(tmp_path / "ok.wdva", key=key)
        real_remove = _ml.remove_adapter

        def sloppy_remove(peft_model: Any) -> Any:
            base = real_remove(peft_model)
            base.add_module("left_behind", torch.nn.Linear(2, 2))
            return base

        monkeypatch.setattr(_ml, "remove_adapter", sloppy_remove)
        with pytest.raises(WDVAError, match="did not restore the base model"):
            engine.unload()
        monkeypatch.undo()
        assert not engine.is_loaded
        assert engine._base is None  # discarded...
        assert torch.equal(logits(engine), base_logits)  # ...and loaded afresh on next use
    finally:
        engine.close()
