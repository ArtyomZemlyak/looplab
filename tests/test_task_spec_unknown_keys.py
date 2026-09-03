"""A task spec refuses a key it does not declare — on SUBMIT, and never on reload.

Every model in `adapters/repo_task.py` but `DeveloperCommandSpec` took pydantic's default
`extra="ignore"`, so a mistyped or MISPLACED key validated and the field silently took its default.
Probed before the fix: `EvalSpec(command=[...], tiemout=5, subject=[...], stage=[...])` dropped all
three, and the run's own `task.snapshot.json` then recorded the operator's intent as the default.
The `_stages_valid` comment already records this mechanism making `cmd.stages` vanish once.

It is `core/appconfig.py::refuse_unknown_settings_keys` one layer over, and the asymmetry is the
same: refuse where the operator is present (submit) and grandfather where they are not (resume /
finalize, which re-validate the verbatim snapshot a run was started with). `extra="forbid"` is the
obvious spelling and the wrong one — it would make every existing run whose snapshot carries an
unknown key retroactively unresumable, for a key that has already done whatever it was going to do.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from looplab.adapters.repo_task import (
    DataSpec, EditableSpec, EvalSpec, ReferenceSpec, RepoTask, refuse_unknown_task_keys)
from looplab.adapters.tasks import validate_task
from looplab.core.errors import ConfigRefusal, OperatorRefusal


def _refusal(excinfo) -> ConfigRefusal | None:
    """The `ConfigRefusal` pydantic wrapped, if this failure is one of ours."""
    error = excinfo.value
    if isinstance(error, ConfigRefusal):
        return error
    for entry in getattr(error, "errors", lambda: [])():
        inner = (entry.get("ctx") or {}).get("error")
        if isinstance(inner, ConfigRefusal):
            return inner
    return None


def test_a_mistyped_eval_key_is_refused_instead_of_taking_the_default():
    """THE INCIDENT SHAPE. MUTATION: drop the validator -> `timeout` is its default, the eval runs
    on it, and `task.snapshot.json` records that default as the operator's intent."""
    with pytest.raises(ValidationError) as excinfo:
        EvalSpec(command=["python", "run.py"], tiemout=5)
    refusal = _refusal(excinfo)
    assert refusal is not None, "the failure is not the typed refusal"
    assert "tiemout" in str(refusal)


def test_the_refusal_names_the_keys_the_model_DOES_declare():
    """A refusal an operator cannot act on is a worse `extra="ignore"`. The nearest correct
    spelling has to be visible in the message they get."""
    with pytest.raises(ValidationError) as excinfo:
        EvalSpec(command=["x"], stage=[{"name": "train"}])
    message = str(_refusal(excinfo))
    assert "stages" in message, "the intended key must be listed"
    assert "timeout" in message and "command" in message


def test_a_MISPLACED_key_is_caught_too():
    """Not only typos: a real key on the wrong model is the same silent drop. `subject` belongs
    under `eval.metric`, not on `EvalSpec` itself."""
    with pytest.raises(ValidationError) as excinfo:
        EvalSpec(command=["x"], subject="model.safetensors")
    assert "subject" in str(_refusal(excinfo))


@pytest.mark.parametrize("model, payload", [
    (ReferenceSpec, {"name": "r", "path": "p", "mont": True}),
    (EditableSpec, {"name": ".", "path": "p", "surfaces": ["*.py"]}),
    (DataSpec, {"name": "d", "path": "p", "mount_": False}),
    (EvalSpec, {"command": ["x"], "nope": 1}),
])
def test_every_task_spec_model_refuses(model, payload):
    """One rule, every model. MUTATION: attach the validator to only some of them -> the silent
    drop survives on whichever was missed, which is how it was distributed in the first place."""
    with pytest.raises(ValidationError) as excinfo:
        model(**payload)
    assert _refusal(excinfo) is not None


def test_a_valid_spec_is_untouched():
    """Strictness that refuses a correct document is not strictness."""
    spec = EvalSpec(command=["python", "-m", "pkg.test"], timeout=90.0)
    assert spec.command == ["python", "-m", "pkg.test"] and spec.timeout == 90.0


def test_a_reload_of_an_existing_run_is_GRANDFATHERED():
    """The whole reason this is not `extra="forbid"`. `resume`/`finalize` re-validate the verbatim
    `task.snapshot.json` a run was started with; refusing there makes an existing run unresumable
    over a key that has already had whatever effect it was going to have (none).

    MUTATION: drop the `_grandfathered` clause -> every run whose snapshot carries an unknown key
    can no longer be resumed or finalized, retroactively.
    """
    spec = EvalSpec.model_validate({"command": ["python", "run.py"], "tiemout": 5},
                                   context={"existing_run": True})
    assert spec.command == ["python", "run.py"]


def test_validate_task_refuses_on_submit_and_reloads_on_resume(tmp_path):
    """END TO END through the real shared entry point, which is where both surfaces meet."""
    doc = {
        "kind": "repo", "task_id": "t", "goal": "g", "direction": "min",
        "repo_path": str(tmp_path), "editable_path": str(tmp_path),
        "eval": {"command": ["python", "-c", "print(1)"], "tiemout": 5},
    }
    with pytest.raises((ValidationError, ConfigRefusal)):
        validate_task(dict(doc))
    # ...and the same document reloads for a run that already exists.
    assert validate_task(dict(doc), existing_run=True) is not None


def test_the_refusal_is_a_typed_operator_refusal():
    """CLAUDE.md: a deliberate refusal is a TYPE, not a message. `ConfigRefusal` is both an
    `OperatorRefusal` (so the CLI boundary prints one line at exit 2 instead of 42 frames) and a
    `ValueError` (so every existing `except ValueError` still catches it)."""
    with pytest.raises(ValidationError) as excinfo:
        EvalSpec(command=["x"], nope=1)
    refusal = _refusal(excinfo)
    assert isinstance(refusal, OperatorRefusal) and isinstance(refusal, ValueError)


def test_a_non_dict_input_passes_through():
    """`mode="before"` sees whatever the caller passed. A model built from an instance or a
    non-mapping must not crash in the validator — pydantic's own error is the right one there."""
    class _Info:
        context = None

    assert refuse_unknown_task_keys(EvalSpec, ["not", "a", "dict"], _Info()) == ["not", "a", "dict"]
    assert refuse_unknown_task_keys(EvalSpec, None, _Info()) is None


def test_an_underscore_key_is_a_COMMENT_and_is_allowed():
    """JSON has no comment syntax and this repo's own `examples/repo_drift_task.json` ships a
    `_note` explaining what the example demonstrates — found by this validator refusing it.
    Refusing a convention the project itself uses is strictness aimed at the wrong thing.

    It cannot mask a real typo: no field here starts with an underscore, and pydantic would not
    accept one that did.
    """
    spec = EvalSpec(command=["x"], _note="why this eval looks odd")
    assert spec.command == ["x"]


def test_the_shipped_examples_still_load():
    """The regression this exemption exists for, driven against the real files rather than a
    fixture — `test_documentation_contracts` loads them too, and this says WHY they must."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "examples"
    for path in sorted(root.glob("*task*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("kind") in {None, "repo", "dataset"} or "eval" in doc:
            try:
                validate_task(doc)
            except (ValidationError, ConfigRefusal) as exc:      # pragma: no cover - the guard
                assert "unknown key" not in str(exc), f"{path.name}: {exc}"

