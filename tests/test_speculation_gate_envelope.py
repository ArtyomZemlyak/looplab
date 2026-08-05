"""The calibrated speculation envelope, one rule at a time (doc 25 ES-01).

The envelope decides whether a caller may claim the speculation-depth waiver, and every rule in it
is the reason some spoofed runtime cannot. Until ES-01 it lived as two closures over twenty
`Engine.__init__` locals, so no test could reach a single rule: the only way to exercise one was to
build a whole Engine and read a substring out of a `ConfigRefusal` message, which is why the suite
had no per-rule coverage at all. `engine/speculation_gate.py` makes the rule statable, and this file
is the truth table that becomes possible once it is.

Deliberately NOT a "the profile matches" happy-path test: an all-passing `CalibrationRuntime` has to
reproduce the exact shipped profile, and a test that reproduces the thing it checks drifts with it.
Each case below perturbs ONE input and asserts that ITS message appears — which is falsifiable
without pinning the twenty others.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import types

import pytest

from looplab.engine import orchestrator, speculation_gate
from looplab.engine.speculation_gate import (CalibrationRuntime, guard_calibrated_role_factory,
                                             narrow_runtime_envelope_errors)


def _runtime(**overrides) -> CalibrationRuntime:
    """A deliberately WRONG runtime: every case below only cares about its own message.

    `read_option` returns a sentinel for every knob, so the profile comparison fails for all of them
    — which is fine, and is exactly why each assertion names the one message it is about.
    """
    base = dict(
        option_fields=frozenset(), read_option=lambda _name: object(),
        recorded_runtime_scope="not-the-scope",
        card_driven_selection=True, max_nodes=8, speculation_depth=2,
        task=object(), researcher=object(), developer=object(), policy=object(),
        sandbox=object(), crash_after=None,
        strategist=None, deep_researcher=None, report_writer=None, developer_factory=None,
        onboarder=None, proxy_scorer=None, lesson_abstractor=None, dep_installer=None,
    )
    base.update(overrides)
    return CalibrationRuntime(**base)


def _engine(**overrides):
    base = dict(run_dir=types.SimpleNamespace(name="calib-run"), speculation_gate_receipt=None,
                _embedder=object(), role_factory=lambda: None)
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _errors(engine=None, **overrides) -> list[str]:
    errors, _scope = narrow_runtime_envelope_errors(engine or _engine(), _runtime(**overrides))
    return errors


# ------------------------------------------------------------------ one rule at a time

@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_card_driven_selection_must_be_exactly_true(value):
    """`is not True` and not a truthiness test — `1` is truthy and must still be refused."""
    assert "card_driven_selection must be exactly true" in _errors(card_driven_selection=value)


def test_card_driven_selection_true_clears_its_own_rule():
    assert "card_driven_selection must be exactly true" not in _errors(card_driven_selection=True)


@pytest.mark.parametrize("value", [0, 65, -1, True, 8.0, "8"])
def test_max_nodes_must_be_an_int_in_range(value):
    """`True` is an `int` subclass, so the exact-type check is what refuses it."""
    assert "max_nodes must be an integer in 1..64" in _errors(max_nodes=value)


@pytest.mark.parametrize("value", [1, 64])
def test_max_nodes_accepts_its_own_bounds(value):
    assert "max_nodes must be an integer in 1..64" not in _errors(max_nodes=value)


@pytest.mark.parametrize("value", [-1, 65, True, 2.0])
def test_speculation_depth_must_be_an_int_in_range(value):
    assert "speculation_depth must be an integer in 0..64" in _errors(speculation_depth=value)


@pytest.mark.parametrize("value", [0, 64])
def test_speculation_depth_accepts_zero_and_the_ceiling(value):
    """Zero is the depth-0 BASELINE arm of the calibration, so it must not be refused as falsy."""
    assert "speculation_depth must be an integer in 0..64" not in _errors(speculation_depth=value)


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_run_id_is_refused(name):
    engine = _engine(run_dir=types.SimpleNamespace(name=name))
    assert "run directory must have a non-empty run id" in _errors(engine)


def test_a_non_toy_task_is_refused_by_exact_type():
    assert "task must be the exact offline ToyTask" in _errors(task=object())


def test_a_toytask_subclass_is_still_refused():
    """`type(...) is not ToyTask`: a subclass could execute a different workload under the name."""
    from looplab.adapters.toytask import ToyTask

    class _Sneaky(ToyTask):
        pass

    assert "task must be the exact offline ToyTask" in _errors(
        task=_Sneaky.__new__(_Sneaky))


def test_a_non_greedytree_policy_is_refused():
    assert "policy must be the canonical bounded GreedyTree" in _errors(policy=object())


def test_a_non_default_sandbox_is_refused():
    assert "sandbox must be the exact default trusted-local SubprocessSandbox" in _errors(
        sandbox=object())


@pytest.mark.parametrize("role", ["strategist", "deep_researcher", "report_writer",
                                  "developer_factory", "onboarder", "proxy_scorer",
                                  "lesson_abstractor", "dep_installer"])
def test_every_optional_role_must_be_disabled(role):
    """All eight, individually — a loop in the production code is a loop that can lose an entry."""
    assert f"{role} must be disabled" in _errors(**{role: object()})


def test_no_role_is_reported_disabled_when_all_are_none():
    assert not [e for e in _errors() if e.endswith("must be disabled")]


def test_a_foreign_embedder_is_refused():
    assert "embedder must be the offline hash embedder" in _errors(_engine(_embedder=object()))


def test_the_offline_hash_embedder_clears_its_own_rule():
    from looplab.tools.vectorstore import hash_embed

    assert "embedder must be the offline hash embedder" not in _errors(
        _engine(_embedder=hash_embed))


def test_a_non_callable_role_factory_is_refused():
    assert "role_factory must provide isolated calibrated Toy roles" in _errors(
        _engine(role_factory=None))


def test_crash_after_is_forbidden():
    assert "crash_after is forbidden in the calibrated runtime" in _errors(crash_after=3)


def test_a_mismatched_runtime_scope_is_refused():
    """The digest binds the receipt to the runtime it measured; a wrong one must not pass."""
    assert any("runtime scope digest must match" in e for e in _errors())


# ------------------------------------------------------------------ the role-factory wrapper

def test_the_guard_wraps_the_factory_rather_than_replacing_it():
    calls = []

    def _factory():
        calls.append(1)
        return ("researcher", "developer")

    engine = _engine(role_factory=_factory)
    guard_calibrated_role_factory(engine, object())

    assert engine.role_factory is not _factory, "the guard did not install its wrapper"
    with pytest.raises(RuntimeError, match="escaped the purpose envelope"):
        engine.role_factory()
    assert calls == [1], "the wrapper must still CALL the original factory before judging it"


@pytest.mark.parametrize("bad", [None, ("only-one",), ("a", "b", "c"), ["a", "b"]])
def test_the_guard_refuses_anything_that_is_not_one_pair(bad):
    """A list of two is not a tuple of two — `isinstance(pair, tuple)` is the check that says so."""
    engine = _engine(role_factory=lambda: bad)
    guard_calibrated_role_factory(engine, object())

    with pytest.raises(RuntimeError, match="must return one role pair"):
        engine.role_factory()


def test_the_guard_admits_the_real_calibrated_pair():
    """The positive case, so the four refusals above are not passing for an unrelated reason."""
    from looplab.adapters.toytask import ToyTask
    from tests.factories import TOY_TASK

    task = ToyTask.load(TOY_TASK)
    researcher, developer = task.build_roles()
    researcher.calibration_concepts = True
    developer.calibration_gpu_probe = True
    developer.noise = 0.0
    assert not speculation_gate._calibration_role_pair_errors(task, researcher, developer), (
        "the calibrated pair this test builds is not actually clean; the guard test below would "
        "then be passing for the wrong reason")

    engine = _engine(role_factory=lambda: (researcher, developer))
    guard_calibrated_role_factory(engine, task)
    assert engine.role_factory() == (researcher, developer)


# ------------------------------------------------------------------ still wired to __init__

def test_the_engine_calls_the_extracted_envelope_at_both_entry_points():
    """AST, not substrings: the envelope is only load-bearing if `__init__` still reaches it, and
    both lanes (bootstrap admission and calibrated replay) must reach the SAME one."""
    tree = ast.parse(inspect.getsource(orchestrator))
    called = [n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert called.count("narrow_runtime_envelope_errors") == 2, called.count(
        "narrow_runtime_envelope_errors")
    assert called.count("guard_calibrated_role_factory") == 2
    assert called.count("CalibrationRuntime") == 1, (
        "the runtime record is built once, where the closures used to be defined")


def test_the_runtime_record_carries_every_local_the_closures_captured():
    """A field quietly dropped from the record is a check quietly reading `None` forever."""
    fields = {f.name for f in dataclasses.fields(CalibrationRuntime)}
    assert fields == {
        "option_fields", "read_option", "recorded_runtime_scope", "card_driven_selection",
        "max_nodes", "speculation_depth", "task", "researcher", "developer", "policy", "sandbox",
        "crash_after", "strategist", "deep_researcher", "report_writer", "developer_factory",
        "onboarder", "proxy_scorer", "lesson_abstractor", "dep_installer"}


def test_the_record_is_frozen():
    """It is a snapshot taken before two call sites; a mutable one could drift between them."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        _runtime().max_nodes = 3
