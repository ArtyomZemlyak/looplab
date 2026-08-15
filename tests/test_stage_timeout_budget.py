"""The operator's per-eval wall-clock budget vs the Developer's declared stage `timeout`.

THE FINDING (measured 2026-08-14 over every `looplab_stages.json` in `runs/` against its own run's
`task.snapshot.json`): of the 39 declared stages that have both numbers, **17 exceed the operator's
budget** — `rubertlite-dr-unified-v7` node 0 at 172800 s against 21600 s (8.0x), `rubert-dr-0807`
nodes 0-4/7 at 86400 s against 14400 s (6.0x), v7 node 1 at 4.2x, 0807 nodes 8/9 at 3.8x, and six
more between 1.5x and 2.0x. Five runs, 44 % of the comparable declarations.

The two halves of the system disagreed about ONE number: `engine/shared.py::effective_eval_time_budget`
and `engine/proposal_cues.py::_experiment_time_budget` TELL the roles the operator's budget, and
`_run_stages` then honours whatever the Developer declared (`finite_timeout(_stg.get("timeout",
timeout), timeout)` — a fallback, never a clamp).

WHERE THE ENFORCEMENT LANDS, and why not at the wall: a resource ceiling the operator declared is
not an ambiguity for an agent to reinterpret (docs/36 — operator-owned bounds stay deterministic),
but the DEADLINE is the worst possible place to hold it. On v7 node 0 the node needed ~27960 s
against 21600 s: clamping discards 21600 s of GPU time AND returns no metric, to avoid 6360 s of
unbudgeted spend. So the bound is enforced at AUTHORING time, where the same correction costs
nothing, and the divergence that gets past that gate (carried-over/hand-written/resumed manifests)
is RECORDED rather than killed. The agent keeps a wide action space over the response — which of
fewer epochs, a subsample or a bigger batch — and gains no ability to move the ceiling.

Everything here is offline: real subprocesses, real stage pipelines, no provider.
"""
from __future__ import annotations

import json
import sys

import orjson
import pytest

from looplab.adapters.repo_write_tools import RepoWriteTools
from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.engine.eval_stages import EvalStagesMixin
from looplab.runtime.command_eval import (eval_spec_time_budget, format_time_budget,
                                          run_command_eval, stage_time_budget_refusal,
                                          stages_over_time_budget)

from tests._source_scan import called_names

_M = {"kind": "stdout_json", "key": "metric"}

# v7 node 0's real numbers, so the truth table is the incident rather than a made-up pair.
V7_BUDGET = 21600.0
V7_DECLARED = 172800.0


def _writetools(budget, tmp_path):
    return RepoWriteTools(surface=["**/*"], protected=[], time_budget=budget,
                          editables=[{"name": ".", "path": str(tmp_path)}])


class _Resolver(EvalStagesMixin):
    """The engine's stage resolution with nothing else attached — `_resolve_stages` reads exactly one
    engine attribute on the Developer-manifest branch."""

    metric_subject = "audit"


# --------------------------------------------------------------------------- the shared rule
def test_the_rule_is_per_stage_and_a_stage_at_the_budget_is_not_over_it():
    """The comparison is `>` and it is per STAGE. A stage timeout is a CEILING, not an estimate, so
    summing them refuses manifests that would have fitted; measured over the corpus, the sum rule
    buys exactly one more refusal out of 31 manifests and costs every legitimate generous prep
    ceiling. A stage declaring MORE than the whole eval gets, alone, cannot be true under any
    reading of the operator's number."""
    stages = [{"name": "prep", "command": ["x"], "timeout": 900.0},
              {"name": "train", "command": ["x"], "timeout": V7_BUDGET},       # exactly at it
              {"name": "extra", "command": ["x"]}]                             # inherits the budget
    assert stages_over_time_budget(stages, V7_BUDGET) == []
    assert stage_time_budget_refusal(stages, V7_BUDGET) is None
    # 900 + 21600 is over the budget as a SUM and is deliberately not refused.
    over = stages_over_time_budget(stages + [{"name": "t2", "command": ["x"], "timeout": V7_DECLARED}],
                                   V7_BUDGET)
    assert over == [("t2", V7_DECLARED)]


def test_no_declared_budget_means_no_ceiling_to_hold_anything_to():
    """An onboarding/toy Developer has no eval spec, so `eval_spec_time_budget` is None and there is
    nothing to be over. Fail-OPEN here on purpose: the alternative is inventing a ceiling, which is
    the same defect (a plausible wrong number) the derivation refuses to do for the prompts."""
    assert eval_spec_time_budget({}) is None
    assert stages_over_time_budget([{"name": "train", "command": ["x"], "timeout": 1e9}], None) == []
    assert stage_time_budget_refusal([{"name": "train", "command": ["x"], "timeout": 1e9}], None) is None


def test_the_refusal_names_both_numbers_in_the_spelling_the_prompt_used():
    """A refusal that says only "too long" leaves the agent guessing at the ceiling — the F1h defect
    one rung down — and the ASKED-FOR number is the only evidence an operator whose budget is too
    small ever gets. The budget is spelled by `format_time_budget`, the SAME helper the Developer's
    note and the Researcher's cue use, so the announced ceiling and the enforced one read alike."""
    msg = stage_time_budget_refusal(
        [{"name": "train", "command": ["x"], "timeout": V7_DECLARED}], V7_BUDGET)
    assert format_time_budget(V7_BUDGET) == "21600s (~6.0h)"
    assert "'train'" in msg and "172800" in msg and "21600s (~6.0h)" in msg
    assert "not extra budget" in msg
    # It must NOT read as "declare a smaller leash and run the same experiment".
    assert "Size the EXPERIMENT to the budget" in msg
    # And it must leave the honest escape hatch, or an operator who under-declared learns nothing.
    assert "say in your notes how long it really needs" in msg


# --------------------------------------------------------------------------- authoring: the refusal
def test_declare_stages_refuses_the_v7_manifest_and_stages_nothing(tmp_path):
    """DRIVEN through the real tool: v7 node 0's own declaration, 48 h against the operator's 6 h.
    Nothing is staged — a refusal that still wrote `looplab_stages.json` would leave the engine
    running the manifest the tool just rejected."""
    t = _writetools(V7_BUDGET, tmp_path)
    msg = t.execute("declare_stages", {"stages": [
        {"name": "data_prep", "command": [sys.executable, "prep.py"], "timeout": 900},
        {"name": "train", "command": [sys.executable, "train.py"], "timeout": V7_DECLARED}]})
    assert msg.startswith("(refused: ")
    assert "172800" in msg and "21600s (~6.0h)" in msg
    assert "looplab_stages.json" not in t.files


def test_the_same_manifest_sized_to_the_budget_is_untouched(tmp_path):
    """THE NEGATIVE CONTROL. A stage within the budget declares exactly what it declared before: the
    gate must be invisible to every manifest that fits, or it is a tax on the 22 of 39 declarations
    that were always correct."""
    t = _writetools(V7_BUDGET, tmp_path)
    stages = [{"name": "data_prep", "command": [sys.executable, "prep.py"], "timeout": 900},
              {"name": "train", "command": [sys.executable, "train.py"], "timeout": V7_BUDGET}]
    msg = t.execute("declare_stages", {"stages": stages})
    assert msg.startswith("declared 2 preceding stage(s)")
    staged = json.loads(t.files["looplab_stages.json"])["stages"]
    assert [s["timeout"] for s in staged] == [900.0, V7_BUDGET]


def test_a_developer_with_no_budget_declares_what_it_likes(tmp_path):
    """An onboarding/toy task carries no eval spec, so `time_budget` is None and the tool behaves
    exactly as it did before this rule existed."""
    t = _writetools(None, tmp_path)
    msg = t.execute("declare_stages", {"stages": [
        {"name": "train", "command": [sys.executable, "t.py"], "timeout": V7_DECLARED}]})
    assert msg.startswith("declared 1 preceding stage(s)")
    assert "looplab_stages.json" in t.files


def test_the_developer_resolves_the_budget_once_for_the_note_and_the_bound():
    """The prompt announces a ceiling and the tool enforces one; two derivations is how they come to
    be different numbers. Both go through `_eval_time_budget`, and the stages phase applies the SAME
    refusal object the tool does (AST — the phase's `_validate` is a closure a unit test cannot reach
    without a live client, a repo and a model)."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    assert "self._eval_time_budget" in called_names(LLMRepoDeveloper._time_budget_note)
    assert "self._eval_time_budget" in called_names(LLMRepoDeveloper._run)          # -> RepoWriteTools
    assert "write.stage_budget_refusal" in called_names(LLMRepoDeveloper._declare_stages_phase)


def test_the_stages_prompt_states_that_the_refusal_exists():
    """The advisory rung is not replaced by the gate, it is completed by it: an agent told "a longer
    leash is not more budget" and then refused for declaring one has learnt the same thing twice,
    while an agent refused with no warning has been surprised. The existing F1h wording is spliced,
    not reworded (`tests/test_researcher_time_budget_hint.py` pins the rest byte-for-byte)."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper

    class _T:
        def eval_spec(self):
            return {"command": ["python", "-m", "vectorsearch.test"], "timeout": V7_BUDGET}

    dev = object.__new__(LLMRepoDeveloper)
    dev.task = _T()
    note = dev._time_budget_note()
    assert "A stage `timeout` longer than the budget is not more budget" in note   # untouched
    assert "`declare_stages` REFUSES a stage `timeout` above the budget" in note   # spliced
    assert "21600s (~6.0h)" in note


# ------------------------------------------------- consume + run: recorded, and score still bounded
def _write_manifest(wd, train_timeout):
    (wd / "looplab_stages.json").write_text(json.dumps({"stages": [
        {"name": "train", "command": [sys.executable, "train.py"], "timeout": train_timeout}]}),
        encoding="utf-8")


def _spans(path):
    return [orjson.loads(x) for x in path.read_bytes().splitlines()]


def _over_budget_rows(path):
    return [r for r in _spans(path) if r.get("name") == "stage_timeout_over_budget"]


@pytest.mark.parametrize("train_timeout,expect_recorded", [(60.0, True), (1.5, False)])
def test_a_real_eval_records_the_divergence_and_never_kills_for_it(tmp_path, train_timeout,
                                                                  expect_recorded):
    """DRIVEN END TO END, both directions of the rule in one shape.

    The operator declares a 2 s per-eval budget. A manifest that reached the workdir WITHOUT passing
    the authoring gate (hand-written, carried over from a parent, or resumed from before the gate —
    v7 node 1's 90000 s is exactly this shape) declares `train` at 60 s. The train stage runs for 3 s
    and MUST complete: the leash is honoured, not clamped, because killing a training at the wall
    costs more than the overspend it prevents. What the engine owes the operator is the FACT, and it
    is on disk as a `stage_timeout_over_budget` span naming both numbers.

    The parametrized negative control is the same pipeline with `train` at 1.5 s — inside the budget
    — where nothing is recorded at all and the stage is killed by its OWN declared timeout, proving
    the recording arm is not a blanket "every staged eval logs something"."""
    (tmp_path / "train.py").write_text("import time; time.sleep(3); print('trained')\n",
                                       encoding="utf-8")
    (tmp_path / "score.py").write_text('import json; print(json.dumps({"metric": 0.9}))\n',
                                       encoding="utf-8")
    _write_manifest(tmp_path, train_timeout)
    es = {"command": [sys.executable, "score.py"], "timeout": 2.0, "metric": _M}
    assert eval_spec_time_budget(es) == 2.0

    spans = tmp_path / "spans.jsonl"
    tr = Tracer(JsonlSpanExporter(str(spans)), run_id="r")
    with tr.span("evaluate", new_trace=True, node_id=0):
        stages = _Resolver()._resolve_stages(str(tmp_path), es, score_cmd=[sys.executable, "score.py"],
                                             score_timeout=2.0)
        res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 2.0, _M,
                               stages=stages, log_dir=str(tmp_path), tracer=tr)

    rows = _over_budget_rows(spans)
    if not expect_recorded:
        assert rows == []
        # Its OWN declared 1.5 s timeout still applies — the stage is killed by the number it declared.
        assert res.timed_out or res.stages[0]["status"] != "ok"
        return
    assert [(r["attributes"]["stage"], r["attributes"]["declared_s"], r["attributes"]["budget_s"],
             r["attributes"]["at"], r["attributes"]["enforced"]) for r in rows] \
        == [("train", 60.0, 2.0, "resolve", False)]
    # NOT clamped: the 3 s train survived a 2 s budget, and the pipeline reached the metric.
    assert res.stages[0]["name"] == "train" and res.stages[0]["status"] == "ok"
    assert res.stages[0]["seconds"] >= 3.0
    assert res.metric == 0.9


def test_the_protected_score_stage_still_runs_under_the_operator_number(tmp_path):
    """The one guarantee that must survive every option considered here. `_resolve_stages` appends
    `score` with the operator's own profile-resolved `score_timeout` and never with a declared one,
    so a manifest that reserves a long leash for itself buys the scorer nothing. Driven: the scorer
    sleeps 5 s against a 2 s operator budget and is killed at the operator's number, while the
    preceding over-budget stage — 60 s declared — completed."""
    (tmp_path / "train.py").write_text("print('trained')\n", encoding="utf-8")
    # It PRINTS while it sleeps, so what kills it is the wall-clock deadline and not the stall
    # watchdog — the number under test is the deadline, and a silent process would be killed at
    # ~2 s by the other guard and prove nothing about this one.
    (tmp_path / "score.py").write_text(
        "import time,sys\n"
        "for _ in range(50):\n    print('working', flush=True); time.sleep(0.1)\n"
        'print(\'{"metric": 0.9}\')\n', encoding="utf-8")
    _write_manifest(tmp_path, 60.0)
    es = {"command": [sys.executable, "score.py"], "timeout": 2.0, "metric": _M}
    stages = _Resolver()._resolve_stages(str(tmp_path), es, score_cmd=[sys.executable, "score.py"],
                                         score_timeout=2.0)
    assert [s["name"] for s in stages] == ["train", "score"]
    assert stages[-1]["timeout"] == 2.0           # the operator's, not the manifest's
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 2.0, _M,
                           stages=stages, log_dir=str(tmp_path))
    assert res.timed_out is True
    assert res.metric is None
    assert res.stages[0]["status"] == "ok"        # the preceding stage still ran
    # Killed at the OPERATOR's 2 s, nowhere near the 5 s it wanted or the 60 s the manifest claimed.
    assert res.stages[-1]["name"] == "score" and res.stages[-1]["seconds"] < 4.0


# --------------------------------------------------------------- ONE writer of the marker (2026-08-15)

def test_both_producers_of_the_marker_span_emit_the_identical_row_shape(tmp_path):
    """The two sites that emit `stage_timeout_over_budget` were the same loop written twice.

    They are the ONLY producers of the span an operator is told to read as evidence that their budget
    is too small, and nothing type-checks a `tracing.operation` kwarg — so an attribute added, a
    literal misspelled or the span renamed on one side puts a differently-shaped row into the SAME
    trace under the same name, and which shape the operator gets depends on which door the manifest
    came through (authoring refusal vs resolve-time record).

    Driven through the real writer at both settings: the attribute SET must be identical and the only
    values that may differ are the two the call sites own.
    """
    from looplab.runtime.command_eval import record_stages_over_time_budget

    stages = [{"name": "train", "command": ["python", "train.py"], "timeout": 60.0},
              {"name": "mine", "command": ["python", "mine.py"], "timeout": 90.0}]
    spans = tmp_path / "spans.jsonl"
    tr = Tracer(JsonlSpanExporter(str(spans)), run_id="r")
    with tr.span("evaluate", new_trace=True, node_id=0):
        assert record_stages_over_time_budget(stages, 2.0, at="resolve", enforced=False) == 2
        assert record_stages_over_time_budget(stages, 2.0, at="declare", enforced=True) == 2

    rows = _over_budget_rows(spans)
    assert len(rows) == 4
    shapes = {frozenset(r["attributes"]) for r in rows}
    assert len(shapes) == 1, f"the two producers emit different attribute sets: {shapes}"
    resolve = [r["attributes"] for r in rows if r["attributes"]["at"] == "resolve"]
    declare = [r["attributes"] for r in rows if r["attributes"]["at"] == "declare"]
    assert len(resolve) == 2 and len(declare) == 2
    for left, right in zip(resolve, declare):
        differing = {k for k in left if left[k] != right[k]}
        assert differing == {"at", "enforced"}, differing
    assert [(a["stage"], a["declared_s"]) for a in resolve] == [("train", 60.0), ("mine", 90.0)]


def test_a_fitting_manifest_and_an_absent_budget_both_emit_nothing():
    """The writer keeps `stages_over_time_budget`'s own two fail-open answers rather than restating
    them: nothing over the budget, and no budget to be over."""
    from looplab.runtime.command_eval import record_stages_over_time_budget

    fits = [{"name": "train", "command": ["python", "t.py"], "timeout": 1.0}]
    assert record_stages_over_time_budget(fits, 2.0, at="resolve", enforced=False) == 0
    assert record_stages_over_time_budget(
        [{"name": "t", "command": ["python", "t.py"], "timeout": 9e9}], None,
        at="declare", enforced=True) == 0


def test_the_two_call_sites_reach_the_shared_writer_and_spell_no_span_of_their_own():
    """AST, not a substring: what must hold is that neither caller still CALLS `tracing.operation`
    for this span, because a second body is what the shared writer exists to remove."""
    import ast
    import inspect
    import textwrap

    from looplab.adapters.repo_developer import LLMRepoDeveloper as RepoDev
    from looplab.engine.eval_stages import EvalStagesMixin as _Mixin

    for source in (textwrap.dedent(inspect.getsource(_Mixin._resolve_stages)),
                   textwrap.dedent(inspect.getsource(RepoDev._declare_stages_phase))):
        tree = ast.parse(source)
        called = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        names = {getattr(n.func, "attr", getattr(n.func, "id", "")) for n in called}
        assert "record_stages_over_time_budget" in names
        for call in called:
            if getattr(call.func, "attr", "") == "operation":
                first = call.args[0] if call.args else None
                assert not (isinstance(first, ast.Constant)
                            and first.value == "stage_timeout_over_budget"), (
                    "a caller still opens the marker span itself")
