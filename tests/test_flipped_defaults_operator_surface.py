"""Two operator-facing consequences of the 2026-08-04 default flips, each reproduced before its fix.

The flips (`eval_parallel`/`llm_parallel` None->0, `asha_live_kill`/`train_monitor_kill` False->True,
`backend` "toy"->"llm", `card_driven_selection` False->True) are product decisions and are not under
test here. What IS under test is what they did to the two surfaces an operator actually touches:

  1. the Settings FORM still told the operator to "opt in" to two things that ship enabled, described
     an ASHA kill procedure that the same commit REPLACED (the quantile no longer decides — an LLM
     judge does, at `asha_live_kill_confidence`), and had no row for the threshold that now decides.
     The review gate that should have caught it was a bare integer field COUNT, which the flip
     commit satisfied by bumping the integer (193 -> 194) while adding zero rows;
  2. `preflight_role_endpoints` gates all four `_engine` call sites, so with `backend` defaulting to
     "llm" a run that was already OVER could no longer be wrapped up unless its endpoint was still
     live — and the refusal blamed "empty fallback proposals", which a finished run cannot make.

New FILE on purpose: these cut across `serve` (the catalogue + its guard), `agents` (the preflight)
and `cli` (which entry points may only complete a wrap-up), so there is no one module they belong in.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.core.config import Settings

# ---------------------------------------------------------------------------------------------
# 1. the Settings form: copy that matches the code, and a guard a future flip cannot pass silently
# ---------------------------------------------------------------------------------------------

_CATALOGUE = Path(__file__).resolve().parents[1] / "looplab" / "serve" / "settings_ui_schema.json"


def _rows() -> dict:
    doc = json.loads(_CATALOGUE.read_text(encoding="utf-8"))
    return {field["key"]: field for group in doc["groups"] for field in group["fields"]}


def test_every_flipped_default_is_described_as_shipped_not_as_an_opt_in():
    """The two flips that shipped ON were still documented as switches the operator must enable."""
    rows = _rows()
    for key in ("card_driven_selection", "asha_live_kill", "train_monitor_kill"):
        assert Settings.model_fields[key].default is True
        help_text = rows[key]["help"]
        # The literal words the old copy used to invite an operator to turn these on.
        assert "Opt in" not in help_text and "Opt-in" not in help_text, key
        assert "ON by default" in help_text, key
    # AUTO is the default for both concurrency axes now, not the legacy serial fallback.
    for key in ("eval_parallel", "llm_parallel"):
        assert Settings.model_fields[key].default == 0
        assert "0 = AUTO at launch" in rows[key]["help"] and "DEFAULT" in rows[key]["help"], key
    assert Settings.model_fields["backend"].default == "llm"
    assert "llm is the DEFAULT" in rows["backend"]["help"]
    # Speculation was the one flip this test's premise was written for that had NOT shipped, because
    # the Card debug-anchor defect made every debug prefetch superseded on sight. That was fixed on
    # 2026-08-05 and AUTO shipped with it, so it is now held to the same bar as the flips above —
    # and it is what finally makes `card_driven_selection` shipping True mean anything for SELECTION,
    # since Layer 3 needs both.
    assert Settings.model_fields["speculation_depth"].default == -1
    spec_help = rows["speculation_depth"]["help"]
    assert "Opt in" not in spec_help and "Opt-in" not in spec_help
    assert "AUTO and is the DEFAULT" in spec_help
    assert "0 = off" in spec_help
    # The row must also say what AUTO does NOT do, because "on by default" without the settle-to-off
    # cases reads as "every run now prefetches", which is false and is the sentence an operator would
    # act on when their non-greedy or toy-backend run shows no speculation at all.
    assert "settles itself back to OFF" in spec_help


def test_the_asha_kill_row_describes_the_judge_that_replaced_the_quantile():
    """`15b7822f` made the quantile test EVIDENCE and gave the stop decision to an LLM judge; the row
    still described the quantile as the thing that kills, and the judge's threshold had no row."""
    from looplab.engine import asha_monitor

    rows = _rows()
    help_text = rows["asha_live_kill"]["help"]
    assert "judge" in help_text and "asha kill confidence" in help_text.lower()
    assert "No judge" in help_text and "NO kill" in help_text
    # the code this sentence describes: a missing verdict can never authorize a stop, and the rank
    # bit remains a required conjunct, so the judge can only ever narrow the stop set.
    assert asha_monitor.should_asha_kill(
        None, enabled=True, threshold=0.0, rank_underperforming=True) is False
    below = asha_monitor.AshaVerdict(status="stop", confidence=0.5, reason="r")
    assert asha_monitor.should_asha_kill(
        below, enabled=True, threshold=0.8, rank_underperforming=True) is False
    at_bar = asha_monitor.AshaVerdict(status="stop", confidence=0.8, reason="r")
    assert asha_monitor.should_asha_kill(
        at_bar, enabled=True, threshold=0.8, rank_underperforming=True) is True
    assert asha_monitor.should_asha_kill(
        at_bar, enabled=True, threshold=0.8, rank_underperforming=False) is False

    # the knob that now decides has a row, carries the model's own bounds, and says so
    assert Settings.model_fields["asha_live_kill_confidence"].default == 0.8
    confidence = rows["asha_live_kill_confidence"]
    assert confidence["type"] == "float" and confidence["default"] == 0.8
    assert "not the quantile" in confidence["help"]


def test_the_llm_judged_kill_switches_and_their_bars_are_all_on_the_form():
    """The curation rule the flips broke: a judge that can END an experiment is never invisible."""
    rows = _rows()
    assert {"asha_live", "asha_live_kill", "asha_live_kill_confidence",
            "train_monitor", "train_monitor_kill", "train_monitor_kill_confidence"} <= set(rows)
    assert "monitor_broken" in rows["train_monitor_kill"]["help"]
    assert "confidence" in rows["train_monitor_kill_confidence"]["help"]


def test_the_speculation_row_describes_the_ratchet_that_replaced_the_startup_pin():
    """`87adef6a` let AUTO re-resolve DOWNWARD mid-run and touched no operator surface, so the row went
    on saying the integer is "resolved once at startup" and that `run_started` is what a resume reads.
    Both are false now, and an operator comparing two runs' token bills acts on exactly that sentence.

    The hole this closes is the one `_check_pinned_default` cannot see: the default (-1) never moved,
    only the BEHAVIOUR under it. So the copy is bound to the code rather than pinned — the two numbers
    it quotes ARE the ratchet's constants, and "the LAST one the log recorded" is driven through the
    real fold. Retune either constant, or delete the ratchet, and this fails instead of drifting.
    """
    from looplab.engine.speculation import SpeculationMixin

    help_text = _rows()["speculation_depth"]["help"]
    # NEGATIVE pin first: what must never come back is the claim the ratchet falsified.
    assert "resolved once at startup" not in help_text
    assert "re-resolves DOWNWARD mid-run" in help_text
    assert "speculation_depth_settled" in help_text
    # the two numbers are the rule's own constants, so retuning either turns this copy into a lie
    assert f"at least {SpeculationMixin._ADAPTIVE_DEPTH_MIN_SAMPLES} evaluated nodes" in help_text
    assert f"{SpeculationMixin._ADAPTIVE_DEPTH_MIN_EVAL_FRACTION:.0%} of a build" in help_text


def test_the_depth_in_force_is_the_last_one_the_log_recorded_not_run_starteds(tmp_path):
    """The other half of that row's new sentence, driven rather than asserted about: a resume reads the
    latest settle, not the startup pin. Its own test because it is a fold property, not copy."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    from looplab.events.types import EV_RUN_STARTED, EV_SPECULATION_DEPTH_SETTLED

    store = EventStore(tmp_path / "events.jsonl")
    store.append(EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min",
                                  "speculation_depth": 4})
    assert fold(store.read_all()).speculation_depth == 4
    store.append(EV_SPECULATION_DEPTH_SETTLED, {"depth": 0, "previous": 4})
    assert fold(store.read_all()).speculation_depth == 0


def test_the_training_monitor_row_no_longer_claims_the_runtime_marks_out_a_training_stage():
    """`9e26a17f` removed this row's premise. `run_argv(..., health_check=True)` is passed for EVERY
    declared stage, the appended scorer included, so the runtime draws no train/not-train line and
    there is no "declared training stage" whose log the monitor could be said to tail. Roles are
    assigned STRUCTURALLY from the eval's own resolved pipeline instead — driven here, because the
    sentence the row now makes is a claim about `eval_log_plan`'s output and nothing else.
    """
    from looplab.engine.train_monitor import (LOG_ROLE_SCORE, LOG_ROLE_SETUP, LOG_ROLE_TRAINING,
                                              LOG_ROLE_WORK, eval_log_plan)

    help_text = _rows()["train_monitor"]["help"]
    assert "declared training stage" not in help_text            # the retired premise
    assert "health_check is passed for EVERY declared stage" in help_text

    roles = eval_log_plan([{"name": "data_prep"}, {"name": "train"}, {"name": "score"}]).roles
    assert roles["setup.log"] == (None, LOG_ROLE_SETUP)          # dep install, never judged
    assert roles["score.log"] == ("score", LOG_ROLE_SCORE)       # the LAST stage, by position
    assert roles["data_prep.log"] == ("data_prep", LOG_ROLE_WORK)
    # the substantive one: a stage NAMED `train` is still only work — nothing proves it is the training
    assert roles["train.log"] == ("train", LOG_ROLE_WORK)
    assert LOG_ROLE_TRAINING not in {role for _stage, role in roles.values()}


def test_the_training_kill_row_describes_all_four_conjuncts_of_the_gate():
    """The row described a TWO-conjunct gate (`enabled` + a confident `broken`) that `9e26a17f` had
    already made four. For any multi-stage pipeline that reads as "on + broken@0.8 kills", which is
    false in both directions the operator cares about — nothing there can kill, and one confident tick
    is not enough anywhere. Same shape as the ASHA test above: the row's copy beside the truth table of
    the pure function that IS the whole decision, so removing a conjunct fails the behavioural half.
    """
    from looplab.engine import train_monitor as tm

    help_text = _rows()["train_monitor_kill"]["help"]
    assert "FOUR independent conjuncts" in help_text

    broken = tm.TrainingVerdict(status="broken", reason="diverged", confidence=0.9)
    satisfied = dict(enabled=True, threshold=0.8, log_role=tm.LOG_ROLE_TRAINING, broken_streak=2)
    assert tm.should_monitor_kill(broken, **satisfied) is True
    # each conjunct, falsified ALONE, is enough to spare the node
    assert tm.should_monitor_kill(broken, **{**satisfied, "enabled": False}) is False
    assert tm.should_monitor_kill(
        tm.TrainingVerdict(status="broken", reason="diverged", confidence=0.5), **satisfied) is False
    assert tm.should_monitor_kill(broken, **{**satisfied, "log_role": tm.LOG_ROLE_WORK}) is False
    assert tm.should_monitor_kill(broken, **{**satisfied, "broken_streak": 1}) is False
    # and the row names all four, so an operator can tell WHICH one spared their node
    for conjunct in ("Training kill confidence", "PROVE is this run's own training",
                     "MULTI-stage pipeline", "second consecutive broken"):
        assert conjunct in help_text


def test_every_settings_field_is_a_row_or_a_written_down_omission():
    from looplab.serve import settings_ui_schema as schema

    curated = set(_rows())
    uncurated = set(schema.SETTINGS_UI_SCHEMA_UNCURATED_FIELDS)
    assert curated | uncurated == set(Settings.model_fields)
    assert not (curated & uncurated)
    # each omission carries a REASON, not just membership
    assert all(len(reason) > 40 for reason in schema.SETTINGS_UI_SCHEMA_UNCURATED_FIELDS.values())


def test_a_new_settings_field_cannot_pass_the_guard_by_bumping_a_count(monkeypatch):
    """The exact hole `15b7822f` went through: a field arrives, the count constant is bumped, no row
    is added, and nothing objects. There is no count to bump now — the field must be placed."""
    from looplab.serve import settings_ui_schema as schema

    monkeypatch.setitem(Settings.model_fields, "brand_new_knob",
                        Settings.model_fields["asha_live_kill"])
    with pytest.raises(RuntimeError) as excinfo:
        schema._load_schema()
    message = str(excinfo.value)
    assert "brand_new_knob" in message                       # names the field, not just "changed"
    assert "SETTINGS_UI_SCHEMA_UNCURATED_FIELDS" in message   # and both ways out of it
    assert "form row" in message


def test_a_future_default_flip_that_adds_no_row_fails_the_guard(monkeypatch):
    """A flip moves no count and no keyset — it only makes the copy wrong. Simulate the next one."""
    from copy import deepcopy

    from looplab.serve import settings_ui_schema as schema

    flipped = deepcopy(Settings.model_fields["asha_live"])
    flipped.default = False                                  # today's shipped default is True
    monkeypatch.setitem(Settings.model_fields, "asha_live", flipped)
    with pytest.raises(RuntimeError) as excinfo:
        schema._load_schema()
    message = str(excinfo.value)
    assert "'asha_live'" in message and "now defaults to False" in message
    assert "reviewed against True" in message
    assert "help" in message                                 # tells the reader what to re-read


def test_a_deleted_settings_field_cannot_leave_its_exemption_behind(monkeypatch):
    """The reconciliation's OTHER arm, and the one no test drove: a `SETTINGS_UI_SCHEMA_UNCURATED_FIELDS`
    entry naming a knob that no longer exists.

    Its covered sibling (`test_a_new_settings_field_cannot_pass_the_guard_by_bumping_a_count`) drives
    the `unreviewed` arm, which is why the pair reads as covered — a mutation audit on 2026-08-05
    confirmed that making the `ghosts` raise a no-op leaves the whole settings surface green. The
    cost is a one-way ratchet: the exemption list only ever grows, and every stale line makes the
    NEXT reader trust a written-down reason for a field that has not existed for months. It is the
    live case, too — a Settings field was deleted in this tree on 2026-08-05.
    """
    from looplab.serve import settings_ui_schema as schema

    # Whichever exempt field the list happens to carry — naming one here would make this test a
    # second place to edit every time the curation changes.
    doomed = sorted(set(schema.SETTINGS_UI_SCHEMA_UNCURATED_FIELDS) & set(Settings.model_fields))[0]
    monkeypatch.delitem(Settings.model_fields, doomed)

    with pytest.raises(RuntimeError) as excinfo:
        schema._load_schema()
    message = str(excinfo.value)
    assert doomed in message                                 # names the stale entry, not just "changed"
    assert "SETTINGS_UI_SCHEMA_UNCURATED_FIELDS" in message
    assert "take its exemption with it" in message


def test_a_row_that_ships_with_no_pinned_default_at_all_fails_the_guard(tmp_path, monkeypatch):
    """`_check_pinned_default`'s FIRST arm, the one the packaged catalogue can never exercise.

    Its sibling — `test_a_future_default_flip_that_adds_no_row_fails_the_guard` — drives the
    MISMATCH arm, and `test_every_row_pins_the_default_it_was_reviewed_against` reads `row["default"]`
    off a file that currently always has it, so neither notices when the key is simply absent. A
    mutation audit confirmed the arm survives being turned into a silent `return`: a new row would
    then ship with no pin, and the next default flip under it would go unreviewed — which is the
    exact failure (`card_driven_selection` telling operators to opt in to something already on) this
    whole guard was built for.
    """
    from looplab.serve import settings_ui_schema as schema

    catalogue = json.loads(_CATALOGUE.read_text(encoding="utf-8"))
    unpinned = catalogue["groups"][0]["fields"][0]
    key = unpinned["key"]
    assert unpinned.pop("default", None) is not None, "the packaged row already had no pin"
    unreviewed = tmp_path / "settings_ui_schema.json"
    unreviewed.write_text(json.dumps(catalogue), encoding="utf-8")
    monkeypatch.setattr(schema, "_SCHEMA_PATH", unreviewed)

    with pytest.raises(RuntimeError) as excinfo:
        schema._load_schema()
    message = str(excinfo.value)
    assert repr(key) in message                              # names the row that needs the pin
    assert "does not pin the default" in message
    assert '"default"' in message                            # and how to fix it


def test_every_row_pins_the_default_it_was_reviewed_against():
    """The pin is a review of that row's copy, not a formality: today every row and the model agree."""
    from looplab.serve import settings_ui_schema as schema

    for key, row in _rows().items():
        field = Settings.model_fields[key]
        shipped = field.default_factory() if field.default_factory is not None else field.default
        # Presence first, and with its own message: the mismatch assertion below KeyErrors on an
        # absent pin, which reads as a broken test rather than as the missing review it is.
        assert "default" in row, f"settings-form row {key!r} pins no default"
        if key in schema._HOME_RELATIVE_DEFAULT_FIELDS:
            assert row["default"].startswith("~/")           # portable, still checked at load
            continue
        assert schema._same_default(row["default"], shipped), key


# ---------------------------------------------------------------------------------------------
# 2. a finished run may be wrapped up without a live endpoint, and is told what that costs
# ---------------------------------------------------------------------------------------------

_DEAD = "http://127.0.0.1:9/v1"          # the discard port: never listening, refused instantly


class _DeadClient:
    """A transport whose endpoint is not there (the shape `_probe_role_endpoints` measures).

    Raises `from` a real `openai.APIConnectionError`, because that is what the real transport does
    and because the CAUSE is now load-bearing: `classify_llm_failure` reads the SDK error the client
    raised from, not the message text, so a double that raises a bare LLMError is a double of a
    DIFFERENT failure (an unclassifiable one) and would be answered with a different remedy.
    """

    def __init__(self, **_kwargs):
        pass

    def probe(self, _messages, **_kwargs):
        import httpx
        import openai
        from looplab.core.errors import LLMError
        refused = openai.APIConnectionError(request=httpx.Request("POST", _DEAD))
        raise LLMError(f"LLM request to {_DEAD} failed: Connection error.") from refused


@pytest.fixture()
def dead_endpoint(monkeypatch):
    from looplab.agents import factory

    monkeypatch.setattr(factory, "make_llm_client", lambda *a, **k: _DeadClient())


def test_the_wrap_up_probe_warns_where_the_run_probe_refuses(dead_endpoint):
    from looplab.core.errors import LLMError
    from looplab.agents import preflight

    settings = Settings(backend="llm", llm_model="m", llm_base_url=_DEAD)
    with pytest.raises(LLMError):
        preflight.preflight_role_endpoints(settings)

    warning = preflight.wrap_up_endpoint_warning(settings)
    assert warning is not None
    assert _DEAD in warning and "m" in warning
    # the refusal's own reason is what must NOT be repeated here — a finished run makes no proposals
    assert "fallback proposal" not in warning
    assert "no proposal can degrade" in warning
    # what the operator gets instead, named artifact by artifact
    assert "(report unavailable)" in warning
    assert "cross-run lessons" in warning
    for still_lands in ("budget summary", "cost roll-up", "tree.html", "trace.json"):
        assert still_lands in warning
    # and the one thing that makes the warning actionable: it is irreversible, so it arrives first
    assert "marked COMPLETE" in warning and "will NOT" in warning


def test_a_reachable_endpoint_produces_no_wrap_up_warning(monkeypatch):
    from looplab.agents import factory, preflight

    class _Ok:
        def __init__(self, **_kwargs):
            pass

        def probe(self, _messages, **_kwargs):
            return None

    monkeypatch.setattr(factory, "make_llm_client", lambda *a, **k: _Ok())
    assert preflight.wrap_up_endpoint_warning(
        Settings(backend="llm", llm_model="m", llm_base_url=_DEAD)) is None


def test_the_offline_backend_is_never_probed_on_a_wrap_up(monkeypatch):
    from looplab.agents import factory, preflight

    monkeypatch.setattr(factory, "make_llm_client",
                        lambda *a, **k: pytest.fail("toy backend built an LLM client"))
    assert preflight.wrap_up_endpoint_warning(Settings(backend="toy")) is None


@pytest.mark.parametrize("kind,wrap_up", [
    ("finalization_pending", True),      # an incomplete terminal projection owns the boundary
    ("pending_finalize", True),          # a stop request newer than the finish
    ("finished", False),                 # `resume` LIFTS this one — it can start work again
    ("paused", False),
    ("live", False),
])
def test_only_the_boundaries_that_cannot_start_work_are_wrap_up_only(kind, wrap_up):
    from looplab.cli import run_cmds

    assert run_cmds.is_wrap_up(kind) is wrap_up


def _pending_run_dir(tmp_path, name: str) -> Path:
    from looplab.events.eventstore import EventStore

    run_dir = tmp_path / name
    run_dir.mkdir()
    task_doc = ('{"id":"t","kind":"quadratic","goal":"g","direction":"min",'
                '"bounds":{"x":[-1,1]}}')
    (run_dir / "task.snapshot.json").write_text(task_doc, encoding="utf-8")
    # the SHIPPED backend ("llm"), pointed at an endpoint that is no longer there
    (run_dir / "config.snapshot.json").write_text(
        json.dumps({"llm_base_url": _DEAD}), encoding="utf-8")
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "goal": "g", "direction": "min"})
    store.append("run_finished", {"reason": "done", "finalization_required": True})
    return run_dir


@pytest.mark.parametrize("entry", ["finalize", "resume", "run"])
def test_a_finished_run_wraps_up_with_a_dead_endpoint_and_says_what_it_lost(
        tmp_path, entry, dead_endpoint):
    """The reproduction: `looplab finalize <dir>` on a completed run whose endpoint had gone away
    raised `LLMError` — refusing the wrap-up, with a message about fallback proposals a finished run
    can never make. All three entry points that can land on this boundary now complete it."""
    from typer.testing import CliRunner
    from looplab.cli import app
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    run_dir = _pending_run_dir(tmp_path, f"pending-{entry}")
    args = {"finalize": ["finalize", str(run_dir)],
            "resume": ["resume", str(run_dir)],
            "run": ["run", str(run_dir / "task.snapshot.json"), "--out", str(run_dir)]}[entry]

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert "endpoint preflight failed" not in result.output
    # "unusable", not "unreachable": one header now serves every cause the probe can measure, and
    # most of them (throttled/overloaded/credential) are endpoints that ARE reachable. The cause
    # this fixture actually produces is named separately, in brackets.
    assert "unusable while wrapping up" in result.output
    assert "[unreachable]" in result.output
    state = fold(EventStore(run_dir / "events.jsonl").read_all())
    assert state.finished and not state.finalization_pending()
    # the wrap-up is not silent about having run without a model
    assert "WITHOUT the model" in result.output
    assert "(report unavailable)" in result.output


def test_a_run_that_can_still_do_work_keeps_refusing(tmp_path, dead_endpoint):
    """The softening is scoped to the wrap-up. A `resume` that would LIFT a finished run back into
    the loop can still propose, so the original refusal — and its original reason — stands."""
    from typer.testing import CliRunner
    from looplab.cli import REFUSAL_EXIT_CODE, app
    from looplab.events.eventstore import EventStore

    run_dir = tmp_path / "liftable"
    run_dir.mkdir()
    task_doc = ('{"id":"t","kind":"quadratic","goal":"g","direction":"min",'
                '"bounds":{"x":[-1,1]}}')
    (run_dir / "task.snapshot.json").write_text(task_doc, encoding="utf-8")
    (run_dir / "config.snapshot.json").write_text(
        json.dumps({"llm_base_url": _DEAD}), encoding="utf-8")
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "liftable", "task_id": "t", "goal": "g",
                                 "direction": "min"})
    store.append("pause", {})                       # a stopped run: `resume` re-enters the loop

    result = CliRunner().invoke(app, ["resume", str(run_dir)])

    # Asserted on the OUTPUT, not on `result.exception`: `LLMError` carries `OperatorRefusal`, so
    # the preflight refusal now reaches the operator as a message + `REFUSAL_EXIT_CODE` rather than
    # as a 43-frame traceback. Both sentences the refusal owes are still checked verbatim.
    assert result.exit_code == REFUSAL_EXIT_CODE, result.output
    assert "LLM endpoint preflight failed" in result.output
    assert "empty fallback proposals" in result.output
