"""A stage manifest key that nothing reads is REFUSED, not dropped.

`_validate_expect` has argued this rule for the `expect` block since it shipped: an unknown key is
refused because "a silently-dropped `assets` typo would leave a stage advertising a contract nothing
enforces". The STAGE object one level up was open, so the identical typo dropped the contract
altogether.

Probed before the fix — a stage spelling `needs_files` / `expects` / `time_out` / `roles`:

    validate_stages([...]) -> (clean, None)          # err is None. It VALIDATED.
    clean[0].keys()        -> {"name", "command"}    # everything else silently gone

so the declared inputs, the declared outputs, the wall clock and kill-eligibility all disappeared
while the manifest recorded on the node still read as if they were there. This is the single
definition of "a valid stage", shared by `declare_stages` (authoring), `EvalSpec.stages` (operator
submit) and `_resolve_stages` (consume) — so what one side drops, the others never see.

The module already refuses `env` and `role` individually "rather than dropped when it is unusable,
because a manifest that reads as if the stage carries a role nothing applies is the failure the
closed key sets exist to end". An unknown key is that same failure with no key to hang it on.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from looplab.runtime import command_eval
from looplab.runtime.command_eval import STAGE_KEYS, validate_stages

_OK = ["python", "-c", "pass"]


def test_the_registry_is_exactly_what_the_validator_actually_reads():
    """DERIVED from the validator's own `s.get(...)` / `in s` reads, never pinned as a list.

    A registry maintained by hand is one commit away from refusing a key the code below it reads —
    which would be strictly worse than the open set it replaced, because it turns a working manifest
    into a refusal. AST, never substrings: a comment naming a key must not satisfy this.

    MUTATION: add a key to `STAGE_KEYS` that nothing reads, or read `s.get("retries")` without
    registering it -> red either way.
    """
    tree = ast.parse(inspect.getsource(validate_stages))
    read: set[str] = set()
    for node in ast.walk(tree):
        # `s.get("x")` and `s["x"]`
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "s" and node.args
                and isinstance(node.args[0], ast.Constant)):
            read.add(node.args[0].value)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "s" and isinstance(node.slice, ast.Constant)):
            read.add(node.slice.value)
        # `if "x" in s`
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant):
            for op, cmp in zip(node.ops, node.comparators):
                if (isinstance(op, ast.In) and isinstance(cmp, ast.Name) and cmp.id == "s"):
                    read.add(node.left.value)

    assert read, "the AST scan found no stage-key reads — the guard would be vacuous"
    assert read == set(STAGE_KEYS), (
        f"STAGE_KEYS and the keys `validate_stages` reads disagree: "
        f"registered-not-read {sorted(set(STAGE_KEYS) - read)}, "
        f"read-not-registered {sorted(read - set(STAGE_KEYS))}")


def test_the_incident_shape_is_refused_rather_than_silently_emptied():
    """MUTATION: drop the unknown-key check -> `err` is None and the stage keeps `name`+`command`
    only, which is the manifest that declared a contract nothing enforces."""
    clean, err = validate_stages([{
        "name": "train", "command": _OK,
        "needs_files": ["data.parquet"], "expects": {"files": ["final/model.safetensors"]},
        "time_out": 60, "roles": "training",
    }])

    assert clean is None
    for typo in ("needs_files", "expects", "time_out", "roles"):
        assert typo in err, f"the refusal must name {typo!r}; the declarer reads this string"


def test_a_mis_spelled_command_is_named_as_an_unknown_key():
    """Ordering, and it is deliberate: the check runs BEFORE the `command` validation so
    `commands: [...]` is reported as the key it is, not as a missing `command`. The two have
    different fixes and `declare_stages` bounces this text straight back to the model."""
    _clean, err = validate_stages([{"name": "train", "commands": _OK}])

    assert "commands" in err and "unknown key" in err


@pytest.mark.parametrize("extra", [
    {"needs": ["in.txt"]},
    {"expect": {"files": ["out.txt"]}},
    {"timeout": 60},
    {"check": True},
    {"role": "training"},
])
def test_every_registered_key_still_validates(extra):
    """The regression this change could most easily cause: refusing a key the manifest may carry."""
    clean, err = validate_stages([{"name": "train", "command": _OK, **extra}])

    assert err is None, f"{sorted(extra)} is registered but was refused: {err}"
    assert clean


def test_env_stays_refused_for_a_declarer_that_may_not_set_it():
    """`env` is IN the key set — it is a legal stage key — and is refused on a different axis: WHO
    is declaring. Folding the two would turn a trust boundary into a spelling complaint.

    MUTATION: drop `env` from STAGE_KEYS -> the Developer gets "unknown key" instead of the
    paragraph explaining that stage environment is the operator's to set, and the operator's own
    call sites break outright.
    """
    _clean, err = validate_stages([{"name": "t", "command": _OK, "env": {"A": "1"}}])
    assert "may not declare `env`" in err, err
    assert "unknown key" not in err

    clean, err = validate_stages([{"name": "t", "command": _OK, "env": {"A": "1"}}],
                                 allow_env=True)
    assert err is None and clean[0]["env"] == {"A": "1"}


def test_the_sibling_rule_one_level_down_is_untouched():
    """`expect`'s closed key set is the precedent this was aligned to."""
    _clean, err = validate_stages([{
        "name": "t", "command": _OK, "expect": {"files": ["o"], "assets": ["x"]}}])

    assert "assets" in err and "unknown key" in err
