"""M6 comparative lessons, live-shared (doc 13 §7 items 2+5, this session).

Item 2 (MARS "comparative reflective memory"): lessons used to come from ONE-SHOT reflection over
a ranked list — nothing ever diffed two competing solutions to assign credit. Now
`select_comparison_pairs` picks the most informative parent→child pairs (biggest |Δ| wins AND
regressions, plus failure→repair pairs) and `Engine._comparative_lessons` distills a
credit-assigned lesson per pair (one batched LLM call; deterministic param-diff credit offline).

Item 5 (AgentRxiv live share): the shared store was written at run END and read at run START only,
so concurrent runs never saw each other's lessons. Now `lessons_every` writes comparative lessons
to the shared store MID-RUN (gated by a replay-safe `lessons_distilled` event that also prevents
re-distilling a pair) and `lessons_refresh_every` re-reads the store mid-run (`lessons_refreshed`
gate) — excluding this run's own lessons, so only OTHER runs' experience is injected.

All offline (toy backends; a fake client covers the LLM path).
"""
from __future__ import annotations

from pathlib import Path

import anyio
import orjson

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.memory import (code_diff, param_credit_statement, parse_credit_lessons,
                                   select_comparison_pairs, task_fingerprint)
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _node(nid, metric=None, parent_ids=(), params=None, status=NodeStatus.evaluated,
          op="improve", code="", error_reason="", rationale=""):
    return Node(id=nid, operator=op, parent_ids=list(parent_ids),
                idea=Idea(operator=op, params=params or {"x": float(nid)}, rationale=rationale),
                metric=metric, status=status, code=code, error_reason=error_reason)


def _state(nodes, direction="min", run_id="run_me", task_id="toy_quadratic",
           goal="minimize (x-3)^2 + (y+1)^2"):
    st = RunState(direction=direction, run_id=run_id, task_id=task_id, goal=goal)
    st.nodes = {n.id: n for n in nodes}
    return st


def _engine(tmp_path, name="r", **kw):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    kw.setdefault("policy", GreedyTree(n_seeds=2, max_nodes=5))
    return Engine(tmp_path / name, task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), **kw)


class FakeClient:
    """Minimal `complete_text` client: canned reply + captured prompts."""

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete_text(self, messages):
        self.prompts.append(messages[-1]["content"])
        return self.reply


# --------------------------------------------------------------------------- #
# Pair selection (pure)
# --------------------------------------------------------------------------- #

def test_select_pairs_solution_debug_ranking_and_cap():
    st = _state([
        _node(0, metric=9.0, op="draft"),
        _node(1, metric=4.0, parent_ids=[0]),                    # improvement Δ=+5 (min)
        _node(2, metric=9.5, parent_ids=[0]),                    # regression Δ=-0.5
        _node(3, status=NodeStatus.failed, metric=None, error_reason="crash", op="draft"),
        _node(4, metric=7.0, parent_ids=[3], op="debug"),        # failure→repair
    ])
    pairs = select_comparison_pairs(st, k=3)
    assert [p["kind"] for p in pairs] == ["debug", "solution", "solution"]
    assert (pairs[0]["a"], pairs[0]["b"]) == (4, 3)
    assert (pairs[1]["a"], pairs[1]["b"]) == (1, 0) and pairs[1]["delta"] == 5.0
    assert (pairs[2]["a"], pairs[2]["b"]) == (2, 0) and pairs[2]["delta"] == -0.5
    assert len(select_comparison_pairs(st, k=2)) == 2            # cap
    # deterministic under replay: same input, same order
    assert select_comparison_pairs(st, k=3) == pairs


def test_select_pairs_direction_and_exclude():
    st = _state([_node(0, metric=1.0, op="draft"), _node(1, metric=2.0, parent_ids=[0])],
                direction="max")
    pairs = select_comparison_pairs(st)
    assert pairs[0]["delta"] == 1.0                              # max: higher child = improvement
    assert select_comparison_pairs(st, exclude=[(1, 0)]) == []   # already-distilled pair skipped


def test_select_pairs_skips_exact_ties():
    # Δ=0 is uninformative (no GOOD/BAD verdict exists for "no effect") — never selected, so it
    # can't burn a distillation slot or manufacture a "regressed by 0" lesson.
    st = _state([_node(0, metric=5.0, op="draft", params={"x": 1.0}),
                 _node(1, metric=5.0, parent_ids=[0], params={"x": 2.0})])
    assert select_comparison_pairs(st) == []
    assert param_credit_statement(st.nodes[1], st.nodes[0], 0.0) is None


def test_select_pairs_skips_unevaluated_and_unknown_parents():
    st = _state([
        _node(0, metric=None, status=NodeStatus.pending, op="draft"),
        _node(1, metric=3.0, parent_ids=[0, 99]),                # pending parent + missing parent
    ])
    assert select_comparison_pairs(st) == []


# --------------------------------------------------------------------------- #
# Deterministic credit + diff + parsing (pure)
# --------------------------------------------------------------------------- #

def test_param_credit_statement_single_change():
    w = _node(1, metric=1.0, params={"x": 3.0, "y": -1.0})
    l = _node(0, metric=5.0, params={"x": 1.0, "y": -1.0})
    stmt = param_credit_statement(w, l, 4.0)
    assert "x" in stmt and "improved" in stmt and "4" in stmt
    assert "y" not in stmt.split("improved")[0].replace("y'", "")  # unchanged param not credited


def test_param_credit_statement_regression_and_no_credit():
    w = _node(1, metric=6.0, params={"x": 9.0})
    l = _node(0, metric=5.0, params={"x": 1.0}, op="draft")
    assert "regressed" in param_credit_statement(w, l, -1.0)
    assert param_credit_statement(w, w, 0.0) is None             # no diff -> no lesson
    wide_w = _node(2, params={"a": 1, "b": 2, "c": 3, "d": 4})
    wide_l = _node(3, params={"a": 9, "b": 9, "c": 9, "d": 9})
    assert param_credit_statement(wide_w, wide_l, 1.0) is None   # >3 diffs -> unattributable


def test_code_diff_and_empty_sides():
    d = code_diff("x = 1\ny = 2\n", "x = 1\ny = 3\n")
    assert "-y = 2" in d and "+y = 3" in d
    assert code_diff("", "x = 1") == ""                           # nothing to compare
    assert code_diff("x = 1", "x = 1") == ""                      # identical -> empty


def test_parse_credit_lessons():
    out = parse_credit_lessons(
        "P1 [GOOD] larger step toward the optimum accelerates convergence\n"
        "- P2: [BAD] overshooting the bound wastes evaluations\n"
        "short\n"
        "P9 [GOOD] out-of-range pair index still counts\n", 2)
    assert (0, "larger step toward the optimum accelerates convergence", "supported") == out[0]
    assert out[1][0] == 1 and out[1][2] == "failed"
    assert out[2][0] == -1                                        # P9 > n_pairs -> unattributed
    assert all(o[1] != "short" for o in out)                      # too-short line dropped
    untagged = parse_credit_lessons("an untagged generalizable observation about step size", 1)
    assert untagged == [(-1, "an untagged generalizable observation about step size", "tested")]


# --------------------------------------------------------------------------- #
# Engine._comparative_lessons: offline fallback + LLM path
# --------------------------------------------------------------------------- #

def test_comparative_offline_fallback_lessons(tmp_path):
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(tmp_path / "mem"),
                  comparative_lessons=True)
    st = _state([
        _node(0, metric=9.0, op="draft", params={"x": 1.0, "y": 0.0}),
        _node(1, metric=4.0, parent_ids=[0], params={"x": 3.0, "y": 0.0}),
        _node(2, status=NodeStatus.failed, metric=None, error_reason="timeout", op="draft"),
        _node(3, metric=7.0, parent_ids=[2], op="debug", rationale="cut the loop count"),
    ])
    lessons, pairs = eng._comparative_lessons(st, ["kind:quadratic"])
    assert pairs and lessons
    by_pair = {tuple(lz["evidence"]): lz for lz in lessons}
    solu = by_pair[(1, 0)]
    assert solu["source"] == "comparative" and solu["outcome"] == "supported"
    assert "x" in solu["statement"] and solu["evidence"] == [1, 0]
    assert solu["run_id"] == "run_me" and solu["delta"] == 5.0
    debug = by_pair[(3, 2)]
    assert "timeout" in debug["statement"] and "cut the loop count" in debug["statement"]


def test_comparative_llm_path_prompt_and_attribution(tmp_path, monkeypatch):
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(tmp_path / "mem"),
                  comparative_lessons=True)
    st = _state([
        _node(0, metric=9.0, op="draft", params={"x": 1.0}, code="x = 1\nprint((x-3)**2)\n"),
        _node(1, metric=4.0, parent_ids=[0], params={"x": 3.0}, code="x = 3\nprint((x-3)**2)\n"),
    ])
    fake = FakeClient("P1 [GOOD] moving x toward the optimum reduces the loss\n")
    monkeypatch.setattr(eng, "_reflect_client", lambda: fake)
    lessons, pairs = eng._comparative_lessons(st, [])
    assert len(pairs) == 1 and len(lessons) == 1
    assert lessons[0]["statement"] == "moving x toward the optimum reduces the loss"
    assert lessons[0]["outcome"] == "supported" and lessons[0]["evidence"] == [1, 0]
    prompt = fake.prompts[0]
    assert "Assign CREDIT" in prompt and "P1" in prompt
    assert "-x = 1" in prompt and "+x = 3" in prompt              # code diff is the evidence


def test_comparative_llm_unattributed_line_not_miscredited(tmp_path, monkeypatch):
    # A verdict line with no usable P<n> marker must be recorded UNATTRIBUTED — not stamped with
    # an arbitrary pair's node ids and delta (wrong provenance in the shared store).
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(tmp_path / "mem"),
                  comparative_lessons=True)
    st = _state([_node(0, metric=9.0, op="draft", params={"x": 1.0}),
                 _node(1, metric=4.0, parent_ids=[0], params={"x": 3.0})])
    fake = FakeClient("[GOOD] a generically worded lesson with no pair marker\n")
    monkeypatch.setattr(eng, "_reflect_client", lambda: fake)
    lessons, _ = eng._comparative_lessons(st, [])
    assert len(lessons) == 1
    assert lessons[0]["evidence"] == [] and lessons[0]["delta"] is None


def test_comparative_llm_error_falls_back(tmp_path, monkeypatch):
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(tmp_path / "mem"),
                  comparative_lessons=True)
    st = _state([_node(0, metric=9.0, op="draft", params={"x": 1.0}),
                 _node(1, metric=4.0, parent_ids=[0], params={"x": 3.0})])

    class Boom:
        def complete_text(self, messages):
            raise RuntimeError("llm down")

    monkeypatch.setattr(eng, "_reflect_client", lambda: Boom())
    lessons, _ = eng._comparative_lessons(st, [])
    assert lessons and lessons[0]["source"] == "comparative"      # deterministic credit stood in


def test_comparative_nothing_to_compare(tmp_path):
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(tmp_path / "mem"),
                  comparative_lessons=True)
    assert eng._comparative_lessons(_state([_node(0, metric=1.0, op="draft")]), []) == ([], [])


# --------------------------------------------------------------------------- #
# Run-end integration: comparative rows land in the shared store
# --------------------------------------------------------------------------- #

def _store_rows(mem: Path) -> list[dict]:
    p = mem / "lessons.jsonl"
    if not p.exists():
        return []
    return [orjson.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_run_end_writes_comparative_rows_and_records_spent_pairs(tmp_path):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem),
                  comparative_lessons=True)
    anyio.run(eng.run)
    comp = [r for r in _store_rows(mem) if r.get("source") == "comparative"]
    assert comp, "run-end reflection must include credit-assigned pair lessons"
    for r in comp:
        assert r.get("fingerprint") and len(r.get("evidence") or []) == 2
        assert r.get("outcome") in ("supported", "failed", "tested")
    # run-end spends are event-recorded too, so a REOPENED run can't re-distill these pairs
    final = fold(eng.store.read_all())
    end_events = [d for d in final.lessons_distilled if d.get("trigger") == "run_end"]
    assert end_events and end_events[0]["pairs"]


def test_comparative_off_writes_no_comparative_rows(tmp_path):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem))  # flag not passed
    anyio.run(eng.run)
    rows = _store_rows(mem)
    assert rows                                                    # legacy reflection still writes
    assert not [r for r in rows if r.get("source") == "comparative"]


# --------------------------------------------------------------------------- #
# Mid-run distillation (write side): events, pair ledger, replay safety
# --------------------------------------------------------------------------- #

def test_midrun_distill_fires_and_never_respends_a_pair(tmp_path):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem),
                  comparative_lessons=True, lessons_every=2,
                  policy=GreedyTree(n_seeds=2, max_nodes=6))
    anyio.run(eng.run)
    final = fold(eng.store.read_all())
    assert final.lessons_distilled, "cadence must have fired mid-run"
    assert all(d["at_node"] <= len(final.nodes) for d in final.lessons_distilled)
    assert final.lessons_distilled[0]["at_node"] < len(final.nodes), \
        "first distillation happened BEFORE the run ended (mid-run, not run-end)"
    spent = [tuple(p) for d in final.lessons_distilled for p in (d.get("pairs") or [])]
    assert len(spent) == len(set(spent)), "a (child, parent) pair must never be distilled twice"
    assert [r for r in _store_rows(mem) if r.get("source") == "comparative"], \
        "mid-run lessons landed in the SHARED store (visible to concurrent runs)"


def test_midrun_distill_replay_safe(tmp_path):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem),
                  comparative_lessons=True, lessons_every=2,
                  policy=GreedyTree(n_seeds=2, max_nodes=6))
    anyio.run(eng.run)
    n_events = len(list(eng.store.read_all()))
    # Re-entry (resume/replay): the folded at_node gate makes the hook a pure no-op — the
    # recorded LLM output stands, the model is never re-invoked (events-as-truth).
    eng2 = _engine(tmp_path, name="r", reflection_priors=True, memory_dir=str(mem),
                   comparative_lessons=True, lessons_every=2,
                   policy=GreedyTree(n_seeds=2, max_nodes=6))
    st = fold(eng2.store.read_all())
    out = eng2._maybe_distill_lessons(st)
    assert len(list(eng2.store.read_all())) == n_events and out is st


def test_midrun_distill_gate_advances_even_with_zero_lessons(tmp_path):
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(tmp_path / "mem"),
                  comparative_lessons=True, lessons_every=1)
    # two root drafts, no parent links -> pairs is empty, but the gate must still advance
    st = _state([_node(0, metric=1.0, op="draft"), _node(1, metric=2.0, op="draft")])
    eng._maybe_distill_lessons(st)
    evs = [e for e in eng.store.read_all() if e.type == "lessons_distilled"]
    assert len(evs) == 1 and evs[0].data["count"] == 0
    st2 = fold(eng.store.read_all())
    st2.nodes = st.nodes
    assert eng._maybe_distill_lessons(st2) is st2                 # same node-count: no refire


# --------------------------------------------------------------------------- #
# Mid-run refresh (read side): live pickup of OTHER runs' lessons, own-run exclusion
# --------------------------------------------------------------------------- #

def _seed_lessons(mem: Path, rows):
    mem.mkdir(parents=True, exist_ok=True)
    with open(mem / "lessons.jsonl", "a", encoding="utf-8") as f:
        f.writelines(orjson.dumps(r).decode() + "\n" for r in rows)


def test_midrun_refresh_picks_up_concurrent_runs_lessons(tmp_path):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem),
                  lessons_refresh_every=2)
    st = _state([_node(0, metric=9.0, op="draft"), _node(1, metric=4.0, parent_ids=[0])])
    assert eng._prior_note_text == ""                             # nothing at "run start"
    # a CONCURRENT run (different run_id) appends to the shared store mid-flight...
    fp = task_fingerprint("quadratic", "min", eng.task.goal, param_names=["x", "y"])
    _seed_lessons(mem, [
        {"task_id": eng.task.id, "fingerprint": fp, "kind": "quadratic", "run_id": "other_run",
         "statement": "a coarse-to-fine step schedule converges faster", "outcome": "supported",
         "delta": 0.3, "confidence": 0.7, "source": "comparative"},
        {"task_id": eng.task.id, "fingerprint": fp, "kind": "quadratic", "run_id": "run_me",
         "statement": "my own echo must not come back", "outcome": "supported",
         "delta": 0.1, "confidence": 0.7, "source": "comparative"},
    ])
    out = eng._maybe_refresh_lessons(st)
    assert "coarse-to-fine" in eng._prior_note_text               # live pickup mid-run
    assert "my own echo" not in eng._prior_note_text              # own run_id excluded
    evs = [e for e in eng.store.read_all() if e.type == "lessons_refreshed"]
    assert len(evs) == 1 and evs[0].data["at_node"] == 2 and evs[0].data["changed"]
    assert eng._maybe_refresh_lessons(out) is out                 # same node-count: no refire
    assert len([e for e in eng.store.read_all()
                if e.type == "lessons_refreshed"]) == 1
    # store UNCHANGED + cadence due again -> the full re-read/re-score is skipped (stat stamp),
    # but the gate still advances via a lightweight event.
    st3 = fold(eng.store.read_all())
    st3.nodes = {n.id: n for n in
                 [_node(0, metric=9.0, op="draft"), _node(1, metric=4.0, parent_ids=[0]),
                  _node(2, metric=3.0, parent_ids=[1]), _node(3, metric=2.5, parent_ids=[2])]}
    eng._maybe_refresh_lessons(st3)
    evs = [e for e in eng.store.read_all() if e.type == "lessons_refreshed"]
    assert len(evs) == 2 and evs[-1].data.get("skipped") == "unchanged"


def test_refresh_off_and_reflection_off_are_noops(tmp_path):
    st = _state([_node(0, metric=1.0, op="draft"), _node(1, metric=2.0, parent_ids=[0])])
    eng = _engine(tmp_path, name="a", reflection_priors=True,
                  memory_dir=str(tmp_path / "mem"))                # cadence 0
    assert eng._maybe_refresh_lessons(st) is st and not list(eng.store.read_all())
    eng2 = _engine(tmp_path, name="b", lessons_refresh_every=2)    # no reflection memory
    assert eng2._maybe_refresh_lessons(st) is st and not list(eng2.store.read_all())
    eng3 = _engine(tmp_path, name="c", reflection_priors=True,
                   memory_dir=str(tmp_path / "mem"), comparative_lessons=True,
                   lessons_every=2)
    st_pending = _state([_node(0, metric=1.0, op="draft"),
                         _node(1, metric=None, status=NodeStatus.pending, parent_ids=[0])])
    assert eng3._maybe_distill_lessons(st_pending) is st_pending   # pending evals: hold off


# --------------------------------------------------------------------------- #
# The synergy end-to-end: run A's credit-assigned lessons reach run B's proposals
# --------------------------------------------------------------------------- #

def test_cross_run_transfer_of_comparative_lessons(tmp_path):
    mem = tmp_path / "mem"
    eng_a = _engine(tmp_path, name="run_a", reflection_priors=True, memory_dir=str(mem),
                    comparative_lessons=True, lessons_every=2,
                    policy=GreedyTree(n_seeds=2, max_nodes=6))
    anyio.run(eng_a.run)
    comp = [r for r in _store_rows(mem) if r.get("source") == "comparative"]
    assert comp
    eng_b = _engine(tmp_path, name="run_b", reflection_priors=True, memory_dir=str(mem),
                    comparative_lessons=True)
    prior = eng_b._load_reflection_priors()
    assert "Lessons from related runs" in prior
    assert any(r["statement"][:40] in prior for r in comp), \
        "run B's proposal prior carries run A's credit-assigned lesson"
    # and run A itself would NOT re-ingest its own lessons (the echo guard)
    run_a_id = fold(eng_a.store.read_all()).run_id
    assert run_a_id
    prior_a = eng_a._load_reflection_priors(exclude_run_id=run_a_id)
    assert all(r["statement"][:40] not in prior_a for r in comp if r.get("run_id") == run_a_id)


# --------------------------------------------------------------------------- #
# Config wiring
# --------------------------------------------------------------------------- #

def test_settings_defaults_on():
    from looplab.core.config import Settings
    s = Settings()
    assert s.comparative_lessons is True                          # product default: ON
    assert s.lessons_every == 4 and s.lessons_refresh_every == 4  # mid-run live share: ON


def test_engine_defaults_off_without_flags(tmp_path):
    eng = _engine(tmp_path)
    assert eng._comparative_lessons_on is False
    assert eng.lessons_every == 0 and eng.lessons_refresh_every == 0


# ---- extraction seam guard (architecture review 2026-07) --------------------------------------
# LessonMemory (looplab/engine/lessons.py) deliberately routes its INTERNAL cross-calls through
# the Engine's thin delegators (`self._e._reflect_lessons(...)`, not `self.reflect_lessons(...)`)
# so instance-level monkeypatches on the engine keep intercepting every path — the seam this
# suite's fakes (and users' hooks) rely on. A "simplification" that calls sibling methods
# directly would type-check and read better while silently disconnecting those seams; this test
# turns that mistake into a hard failure.
def test_lesson_memory_internal_calls_route_through_engine_delegators(tmp_path, monkeypatch):
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(tmp_path / "mem"))
    st = _state([_node(1, metric=1.0), _node(2, metric=0.5, parent_ids=[1])])

    seen = {"reflect": 0, "append": []}

    def fake_reflect(final, best, fp):
        seen["reflect"] += 1
        return ["[lesson] seam guard sentinel"]

    monkeypatch.setattr(eng, "_reflect_lessons", fake_reflect)
    monkeypatch.setattr(eng, "_append_lessons", lambda lessons, **kw: seen["append"].extend(lessons))

    # Call the MOVED implementation directly (not the delegator) — its internal calls must still
    # hit the patched engine attributes.
    eng.lessons.write_reflection_note(st)
    assert seen["reflect"] == 1, "lessons.write_reflection_note must call engine._reflect_lessons"
    assert any("seam guard sentinel" in l for l in seen["append"]), \
        "lessons.write_reflection_note must persist via engine._append_lessons"
