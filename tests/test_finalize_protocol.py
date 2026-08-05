"""The finalization PROTOCOL is one spelling, and a real run is what proves it (doc 25 SE-01).

`search/speculation_quality.py` refuses calibration evidence whose finalization does not match the
engine writer exactly — the ordered event suffix, the step names, the `budget` receipt's field set,
and the two run-start setup identities. Those were hand-copied from the writer, so any engine
finalize/setup change broke the gate in lockstep and SILENTLY: the refusal says the evidence is
invalid, never that the protocol moved, and the repair is six fresh GPU calibration runs.

The constants now live below both sides (`events/finalize_protocol.py`, `core/setup_identity.py`),
which `search` may import and the engine already does. But a shared constant only removes the second
copy if it is also TRUE of the writer, so the tests below DRIVE a real toy run to completion and
compare what the engine actually appended against the table. A constant nothing checks against a
real run would be the same two copies under one name.
"""
from __future__ import annotations

import anyio
import pytest

import looplab.search.speculation_quality as quality
from _source_scan import names_read
from factories import make_engine
from looplab.core.setup_identity import setup_config_hash, setup_manifest_digest
from looplab.events.eventstore import EventStore
from looplab.events.finalize_protocol import (
    FINALIZE_BUDGET_FIELDS,
    QUIET_FINALIZATION_SUFFIX,
    budget_receipt,
)
from looplab.events.types import (
    EV_BUDGET,
    EV_FINALIZE_STEP,
    EV_RUN_FINISHED,
    EV_RUN_STARTED,
    EV_SETUP_FINISHED,
)

# The accepted shape as a LITERAL, measured on the tree. This is a receipt gate: if the sequence
# changes for any reason other than a deliberate protocol change, every already-issued calibration
# receipt stops verifying — the run is refused with no error that says why, and the repair is six
# fresh GPU runs. Pinning it as a literal is what distinguishes "the engine's finalization really
# changed, re-pin here and expect revocation" from "a refactor moved the table by accident".
_EXPECTED_SUFFIX = (
    ("finalize_step", "begun"),
    ("run_finished", None),
    ("budget", None),
    ("finalize_step", "budget"),
    ("diversity_archive", None),
    ("finalize_step", "diversity"),
    ("finalize_step", "case"),
    ("finalize_step", "reflection_begun"),
    ("finalize_step", "reflection"),
    ("finalize_step", "llm_cost"),
    ("finalization_finished", None),
    ("finalize_step", "complete"),
)


@pytest.fixture(scope="module")
def quiet_run(tmp_path_factory):
    """One REAL toy run, finalized with nothing optional configured.

    `make_engine`'s defaults are already the quiet profile the calibration runs use — `memory_dir`
    unset, `reflection_priors` off, `cross_run_curation` off — which is exactly the configuration
    `QUIET_FINALIZATION_SUFFIX` describes. Module-scoped because it is a real loop, not a fixture.
    """
    run_dir = tmp_path_factory.mktemp("quiet") / "run"
    engine = make_engine(run_dir, n_seeds=2, max_nodes=2)
    state = anyio.run(engine.run)
    assert state.finished, "the run must reach a terminal boundary for there to be a suffix"
    return engine, list(EventStore(run_dir / "events.jsonl").read_all())


def _suffix(events):
    finish = next(event for event in events if event.type == EV_RUN_FINISHED)
    return [
        (event.type, (event.data or {}).get("step") if event.type == EV_FINALIZE_STEP else None)
        for event in events[finish.seq - 1:]
    ]


def test_the_table_is_the_shape_a_real_run_actually_writes(quiet_run):
    """The load-bearing one. If the engine grows, drops or reorders a finalization step, THIS goes
    red — instead of the speculation gate silently refusing six legitimate calibration runs."""
    _engine, events = quiet_run
    assert _suffix(events) == list(QUIET_FINALIZATION_SUFFIX)


def test_the_table_still_says_what_it_said_when_receipts_were_issued():
    """The pin. A moved table revokes every issued receipt; that must be a decision, not a diff."""
    assert list(QUIET_FINALIZATION_SUFFIX) == list(_EXPECTED_SUFFIX)


def test_a_real_runs_budget_receipt_has_exactly_the_shared_field_set(quiet_run):
    """The reader compares `set(payload)` against `FINALIZE_BUDGET_FIELDS` and refuses anything
    else, so a field added on the writer side without adding it here rejects every run."""
    _engine, events = quiet_run
    budgets = [event for event in events if event.type == EV_BUDGET]
    assert len(budgets) == 1
    assert set(budgets[0].data) == set(FINALIZE_BUDGET_FIELDS)


def test_the_shared_constructor_is_what_mints_that_payload():
    """`budget_receipt` owns the key set AND the rounding — a reader that rounded differently would
    reject honest evidence, because it recomputes `eval_s` to three places and compares."""
    payload = budget_receipt(
        elapsed_s=1.23456, process_s=2.98765, eval_s=0.00049, nodes=3,
        speculation={"depth": 0}, finalize_scope="finalize:" + "0" * 32, finish_seq=41,
    )
    assert set(payload) == set(FINALIZE_BUDGET_FIELDS)
    assert payload["elapsed_s"] == 1.235
    assert payload["process_s"] == 2.988
    assert payload["eval_s"] == 0.0
    assert payload["nodes"] == 3 and payload["finish_seq"] == 41


def test_a_real_runs_setup_identities_come_from_the_shared_derivation(quiet_run):
    """`_validate_calibration_setup` re-derives both of these to prove evidence came from the
    shipped writer. Driving a real run is what makes the shared helper's answer the writer's answer
    rather than a second opinion that merely looks similar."""
    engine, events = quiet_run
    started = next(event for event in events if event.type == EV_RUN_STARTED)
    finished = next(event for event in events if event.type == EV_SETUP_FINISHED)
    payload = engine.task.model_dump(mode="json")
    assert started.data["config_hash"] == setup_config_hash(payload)
    assert finished.data["manifest"] == setup_manifest_digest(
        payload, started.data["workspace"], {})


def test_the_two_setup_identities_are_not_the_same_derivation():
    """The detail a hand-copy gets wrong: the config hash dumps the payload UNSORTED, the manifest's
    inner hash dumps it SORTED. Swapping them yields a self-consistent digest no run produces."""
    payload = {"b": 1, "a": {"z": 2, "y": [3, 4]}, "c": "x"}
    inner = setup_manifest_digest(payload, {}, {})
    assert setup_config_hash(payload) != inner[:12]
    # ...and the manifest really does depend on all three inputs.
    assert setup_manifest_digest(payload, {"repo": "abc"}, {}) != inner
    assert setup_manifest_digest(payload, {}, {"asset": "def"}) != inner
    assert setup_manifest_digest({**payload, "b": 2}, {}, {}) != inner


@pytest.fixture(scope="module")
def calibration_run(tmp_path_factory):
    """One synthetic run the quality reader accepts, so a REFUSAL below means what it says."""
    import test_speculation_quality_gate as gate_fixture

    run_dir = gate_fixture._make_run(
        tmp_path_factory.mktemp("evidence") / "baseline", treatment=False, seed=0)
    quality.analyze_speculation_run(run_dir)   # a control: it validates before anything is patched
    return run_dir


@pytest.mark.parametrize("patched, attr, message", [
    # A TYPE swapped inside the table (same length) — only `expected_types` can notice.
    (lambda table: tuple(
        (EV_FINALIZE_STEP if index == 2 else event_type, step)
        for index, (event_type, step) in enumerate(table)),
     "QUIET_FINALIZATION_SUFFIX", "terminal finalization order differs"),
    # A STEP NAME changed in the tail — only the derived `expected_tail_data` can notice.
    (lambda table: tuple(
        (event_type, "diversity_receipt" if step == "diversity" else step)
        for event_type, step in table),
     "QUIET_FINALIZATION_SUFFIX", "terminal finalization checklist differs"),
    # The shared budget field set grows a field the writer does not publish.
    (lambda fields: frozenset(fields) | {"extra"},
     "FINALIZE_BUDGET_FIELDS", "budget finalization receipt differs"),
])
def test_the_validator_really_reads_the_shared_constants(
    calibration_run, monkeypatch, patched, attr, message,
):
    """Driven, not pinned. Move the SHARED constant and evidence that validated must stop
    validating — which can only happen if the validator derives its expectations from it. A local
    re-derivation would shrug off every one of these and the two hand-synced copies would be back,
    with nothing red to say so."""
    monkeypatch.setattr(quality, attr, patched(getattr(quality, attr)))
    with pytest.raises(ValueError, match=message):
        quality.analyze_speculation_run(calibration_run)


def test_the_validator_really_reads_the_shared_setup_identity(calibration_run, monkeypatch):
    """Same shape for the run-start identities: bend the shared derivation and the evidence the
    writer produced must stop matching it."""
    monkeypatch.setattr(quality, "setup_config_hash", lambda payload: "0" * 12)
    with pytest.raises(ValueError, match="config_hash differs from the Toy writer"):
        quality.analyze_speculation_run(calibration_run)

    monkeypatch.undo()
    monkeypatch.setattr(quality, "setup_manifest_digest", lambda *args: "0" * 16)
    with pytest.raises(ValueError, match="setup_finished payload differs"):
        quality.analyze_speculation_run(calibration_run)


def test_the_validator_reads_the_shared_names_and_not_a_local_copy():
    """The cheap secondary, AST rather than substrings: a commented-out re-derivation is not an
    `ast.Name(Load)`. The driven tests above are what actually hold the line."""
    from looplab.search.speculation_quality import (
        _validate_calibration_setup,
        _validate_calibration_terminal,
    )

    terminal = names_read(_validate_calibration_terminal)
    assert "QUIET_FINALIZATION_SUFFIX" in terminal
    assert "FINALIZE_BUDGET_FIELDS" in terminal

    setup = names_read(_validate_calibration_setup)
    assert {"setup_config_hash", "setup_manifest_digest"} <= setup


def test_the_re_derivations_the_validator_shed_do_not_come_back():
    """NEGATIVE pins stay substrings on purpose (CLAUDE.md): what must not return is the TEXT, and a
    commented-out copy of a re-derivation is as much of a drift risk as a live one."""
    import inspect

    from looplab.search import speculation_quality

    source = inspect.getsource(speculation_quality)
    assert 'hashlib.sha256(orjson.dumps(task_payload))' not in source, (
        "the setup config hash is being re-derived here again")
    assert '"config": manifest_config_hash' not in source, (
        "the setup manifest preimage is being rebuilt here again")
    assert '"elapsed_s", "process_s", "eval_s", "nodes", "speculation",' not in source, (
        "the budget receipt field set is a local literal again")
