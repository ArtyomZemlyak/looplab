"""Memory reconciliation on a CHANGED OUTCOME (the node_reset / re-eval seam).

Fold-derived memory (hypotheses / champion / leaderboard) self-corrects every fold, but the
DISTILLED cross-run lessons are written to a file and go stale when a node's outcome later flips
(a false-failure re-scored to evaluated, a demoted champion). `LessonMemory.reconcile_lessons`
re-aligns THIS run's lessons with the folded state: every lesson whose grounding-node signature
moved is retired and re-derived from the corrected state — the "find the old one by its evidence
node id and rewrite it" mechanism. Same conclusion → an identical lesson reappears (no-op);
different → the stale row is replaced.

Corners exercised: the {node->sig} change-gate (no drift → no work, no LLM), pending nodes treated
as 'not yet resolved' (never premature drift), exact-sig vs legacy-contradiction detection, the
per-pair comparative upsert (+ un-spend / re-spend ledger), the whole-run reflect-batch replace
(and the guard that an empty LLM re-derivation never nukes existing memory), other runs' lessons
left untouched, the offline no-op (never writes a template), and replay-safe idempotence.
"""
from __future__ import annotations

from pathlib import Path

import orjson

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _node(nid, metric=None, parent_ids=(), params=None, status=NodeStatus.evaluated,
          op="improve", code="", error_reason=""):
    return Node(id=nid, operator=op, parent_ids=list(parent_ids),
                idea=Idea(operator=op, params=params or {"x": float(nid)}),
                metric=metric, status=status, code=code, error_reason=error_reason)


def _state(nodes, direction="min", run_id="run_me", task_id="toy_quadratic",
           goal="minimize (x-3)^2 + (y+1)^2"):
    st = RunState(direction=direction, run_id=run_id, task_id=task_id, goal=goal)
    st.nodes = {n.id: n for n in nodes}
    st.best_node_id = min((n.id for n in nodes if n.metric is not None),
                          key=lambda i: st.nodes[i].metric, default=None)
    return st


def _engine(tmp_path, name="r", **kw):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    kw.setdefault("policy", GreedyTree(n_seeds=2, max_nodes=5))
    return Engine(tmp_path / name, task=task, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), **kw)


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete_text(self, messages):
        self.prompts.append(messages[-1]["content"])
        return self.reply


def _seed(mem: Path, rows):
    mem.mkdir(parents=True, exist_ok=True)
    with open(mem / "lessons.jsonl", "a", encoding="utf-8") as f:
        f.writelines(orjson.dumps(r).decode() + "\n" for r in rows)


def _rows(mem: Path) -> list[dict]:
    p = mem / "lessons.jsonl"
    return [orjson.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


# --------------------------------------------------------------------------- #
# node signature + staleness detection (pure)
# --------------------------------------------------------------------------- #

def test_node_sig_captures_status_metric_reason_and_pending_is_none(tmp_path):
    eng = _engine(tmp_path)
    sig = eng.lessons._node_sig
    assert sig(_node(0, metric=0.86618)) == "evaluated:0.8662"          # rounded (float jitter proof)
    assert sig(_node(1, status=NodeStatus.failed, metric=None, error_reason="oom")) == "failed:oom"
    assert sig(_node(2, status=NodeStatus.pending, metric=None)) is None  # no terminal to ground on
    assert sig(None) is None


def test_evidence_stale_exact_sig(tmp_path):
    eng = _engine(tmp_path)
    st = _state([_node(1, metric=4.0), _node(0, metric=9.0)])
    fresh = {"run_id": "run_me", "evidence": [1, 0],
             "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"}}
    assert eng.lessons._lesson_evidence_stale(st, fresh) is False        # matches → in sync
    st.nodes[1].metric = 2.0                                             # a re-eval moved node 1
    assert eng.lessons._lesson_evidence_stale(st, fresh) is True


def test_evidence_stale_pending_node_is_not_drift(tmp_path):
    # A node reset to pending (mid re-eval) has sig None — 'not yet resolved', must NOT trip a
    # premature re-derive from the transient pending state; drift is judged only once it re-terminals.
    eng = _engine(tmp_path)
    st = _state([_node(1, metric=None, status=NodeStatus.pending), _node(0, metric=9.0)])
    lz = {"run_id": "run_me", "evidence": [1], "evidence_sig": {"1": "failed:oom"}}
    assert eng.lessons._lesson_evidence_stale(st, lz) is False
    st.nodes[1] = _node(1, metric=4.0)                                   # re-eval landed → now judged
    assert eng.lessons._lesson_evidence_stale(st, lz) is True


def test_evidence_stale_legacy_no_sig_is_never_stale(tmp_path):
    # LEGACY rows (no evidence_sig) carry no reliable provenance, so they are NEVER judged stale — an
    # outcome-only heuristic is unsound (a lesson's `outcome` is a VERDICT, not the node's crash/eval
    # STATUS): a comparative 'failed' means "the change regressed", a reflect '[BAD]' means "avoid
    # this", and both routinely sit on EVALUATED nodes. Comparing the two mis-fired in prod (it retired
    # two valid 'this change regressed' lessons whose nodes were evaluated). So no sig → not stale.
    eng = _engine(tmp_path)
    st = _state([_node(5, metric=4.0), _node(7, metric=9.0)])
    for outcome in ("failed", "supported", "bad", "tested"):
        assert eng.lessons._lesson_evidence_stale(
            st, {"run_id": "run_me", "source": "comparative", "evidence": [5, 7],
                 "outcome": outcome}) is False
        assert eng.lessons._lesson_evidence_stale(
            st, {"run_id": "run_me", "evidence": [5], "outcome": outcome}) is False
    # a legacy row still becomes reconcilable the moment it's rewritten WITH a sig (exact path)
    assert eng.lessons._lesson_evidence_stale(
        st, {"run_id": "run_me", "evidence": [5], "evidence_sig": {"5": "failed:oom"}}) is True


# --------------------------------------------------------------------------- #
# reflect_lessons now STAMPS provenance (evidence + evidence_sig)
# --------------------------------------------------------------------------- #

def test_reflect_lessons_stamps_evidence_and_sig(tmp_path, monkeypatch):
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(tmp_path / "mem"))
    st = _state([_node(0, metric=9.0), _node(1, metric=4.0, parent_ids=[0]),
                 _node(2, status=NodeStatus.failed, metric=None, error_reason="oom")])
    monkeypatch.setattr(eng, "_reflect_client",
                        lambda: FakeClient("[GOOD] a sharper step helps convergence"))
    out = eng._reflect_lessons(st, st.best(), ["kind:quadratic"])
    assert len(out) == 1
    ev, sig = out[0]["evidence"], out[0]["evidence_sig"]
    assert 1 in ev and 0 in ev and 2 in ev                              # winners + the failure row
    assert sig["2"] == "failed:oom" and sig["1"] == "evaluated:4.0"     # grounded outcome signatures


# --------------------------------------------------------------------------- #
# reconcile: the change-gate
# --------------------------------------------------------------------------- #

def test_reconcile_noop_when_nothing_drifted(tmp_path):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    st = _state([_node(1, metric=4.0), _node(0, metric=9.0)])
    _seed(mem, [{"run_id": "run_me", "source": "comparative", "statement": "s", "outcome": "supported",
                 "evidence": [1, 0], "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"}}])
    before = (mem / "lessons.jsonl").read_text()
    eng.lessons.reconcile_lessons(st)
    assert (mem / "lessons.jsonl").read_text() == before                # store untouched
    assert not [e for e in eng.store.read_all() if e.type == "lessons_reconciled"]
    # gate: a second call with the same sigs early-returns (no re-read)
    assert eng.lessons.reconcile_lessons(st) is st


def test_reconcile_gate_off_when_memory_disabled(tmp_path):
    eng = _engine(tmp_path)                                              # no reflection_priors/memory_dir
    st = _state([_node(1, metric=4.0)])
    assert eng.lessons.reconcile_lessons(st) is st


# --------------------------------------------------------------------------- #
# reconcile: comparative per-pair UPSERT
# --------------------------------------------------------------------------- #

def test_reconcile_comparative_pair_flip_retires_and_rederives(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    # stale comparative lesson grounded in pair (1,0); its recorded sig says node 1 was metric 4.0
    _seed(mem, [{"task_id": "toy_quadratic", "run_id": "run_me", "source": "comparative",
                 "statement": "OLD STALE moving x helped by 5", "outcome": "supported",
                 "evidence": [1, 0], "delta": 5.0,
                 "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"},
                 "fingerprint": [], "kind": "quadratic"}])
    # the corrected state: node 1 actually re-scored to 6.0 (a REGRESSION vs parent now)
    st = _state([_node(0, metric=9.0, op="draft", params={"x": 1.0}, code="x=1\n"),
                 _node(1, metric=6.0, parent_ids=[0], params={"x": 3.0}, code="x=3\n")])
    monkeypatch.setattr(eng, "_reflect_client",
                        lambda: FakeClient("P1 [BAD] this change regressed the metric\n"))
    eng.lessons.reconcile_lessons(st)
    rows = _rows(mem)
    assert not any("OLD STALE" in r.get("statement", "") for r in rows)  # stale row retired
    fresh = [r for r in rows if r.get("source") == "comparative"]
    assert len(fresh) == 1 and "regressed" in fresh[0]["statement"]      # re-derived from corrected state
    assert fresh[0]["evidence_sig"]["1"] == "evaluated:6.0"              # re-stamped with the NEW sig
    # ledger: the re-derived pair is (re-)spent + an audit event records the reconcile
    final = fold(eng.store.read_all())
    assert [d for d in final.lessons_distilled if d.get("trigger") == "reconcile"]
    rec = [e for e in eng.store.read_all() if e.type == "lessons_reconciled"]
    assert len(rec) == 1 and rec[0].data["n_retired"] == 1 and rec[0].data["n_added"] == 1


def test_reconcile_idempotent_same_conclusion_is_noop(tmp_path, monkeypatch):
    # "such же → ничего не теряем": if re-derivation yields the SAME statement, the net store is
    # unchanged in content (old retired, identical re-appended) — and the sig-gate then stops.
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    _seed(mem, [{"task_id": "toy_quadratic", "run_id": "run_me", "source": "comparative",
                 "statement": "moving x toward the optimum reduces the loss", "outcome": "supported",
                 "evidence": [1, 0], "delta": 5.0,
                 "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"},
                 "fingerprint": [], "kind": "quadratic"}])
    st = _state([_node(0, metric=9.0, op="draft", params={"x": 1.0}, code="x=1\n"),
                 _node(1, metric=5.0, parent_ids=[0], params={"x": 3.0}, code="x=3\n")])  # 4.0 -> 5.0
    monkeypatch.setattr(eng, "_reflect_client",
                        lambda: FakeClient("P1 [GOOD] moving x toward the optimum reduces the loss\n"))
    eng.lessons.reconcile_lessons(st)
    comp = [r for r in _rows(mem) if r.get("source") == "comparative"]
    assert len(comp) == 1
    assert comp[0]["statement"] == "moving x toward the optimum reduces the loss"  # same lesson stands
    assert comp[0]["evidence_sig"]["1"] == "evaluated:5.0"              # provenance refreshed


# --------------------------------------------------------------------------- #
# reconcile: whole-run reflect-batch replace + the empty-rederivation guard
# --------------------------------------------------------------------------- #

def test_reconcile_reflect_batch_replaced_on_false_failure_correction(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=False)
    # a reflect lesson grounded in node 2 as a FAILURE — the exact false-positive story
    _seed(mem, [{"task_id": "toy_quadratic", "run_id": "run_me", "statement": "STALE failures dominate",
                 "outcome": "failed", "evidence": [1, 2], "fingerprint": [], "kind": "quadratic",
                 "evidence_sig": {"1": "evaluated:4.0", "2": "failed:no_metric"}}])
    # corrected: node 2 was a FALSE failure, re-scored to a real metric
    st = _state([_node(1, metric=4.0), _node(2, metric=3.0)])
    monkeypatch.setattr(eng, "_reflect_client",
                        lambda: FakeClient("[GOOD] node 2 in fact trained fine at a sharp temperature"))
    eng.lessons.reconcile_lessons(st)
    rows = [r for r in _rows(mem) if r.get("run_id") == "run_me"]
    assert not any("STALE" in r["statement"] for r in rows)             # stale reflect row retired
    assert any("trained fine" in r["statement"] for r in rows)          # re-derived from corrected state
    assert [e for e in eng.store.read_all() if e.type == "lessons_reconciled"][0].data["reflect"] is True


def test_reconcile_empty_rederivation_never_nukes_memory(tmp_path, monkeypatch):
    # If the LLM re-derivation returns nothing (transient error / empty), the DRIFTED row is dropped
    # (it's wrong) but non-drifted sibling reflect lessons of the run are KEPT — memory is not nuked.
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=False)
    _seed(mem, [
        {"run_id": "run_me", "statement": "DRIFTED grounded in the flipped node", "outcome": "failed",
         "evidence": [2], "evidence_sig": {"2": "failed:oom"}, "fingerprint": [], "kind": "quadratic"},
        {"run_id": "run_me", "statement": "SIBLING still valid", "outcome": "supported",
         "evidence": [1], "evidence_sig": {"1": "evaluated:4.0"}, "fingerprint": [], "kind": "quadratic"},
    ])
    st = _state([_node(1, metric=4.0), _node(2, metric=3.0)])           # node 2 flipped failed->ok

    class Boom:
        def complete_text(self, messages):
            raise RuntimeError("llm down")

    monkeypatch.setattr(eng, "_reflect_client", lambda: Boom())
    eng.lessons.reconcile_lessons(st)
    stmts = [r["statement"] for r in _rows(mem) if r.get("run_id") == "run_me"]
    assert "DRIFTED grounded in the flipped node" not in stmts         # wrong row dropped
    assert "SIBLING still valid" in stmts                              # sibling preserved


# --------------------------------------------------------------------------- #
# reconcile: scope + offline safety
# --------------------------------------------------------------------------- #

def test_reconcile_leaves_other_runs_lessons_untouched(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    _seed(mem, [
        {"run_id": "OTHER", "source": "comparative", "statement": "OTHER run lesson", "outcome": "failed",
         "evidence": [1, 0], "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"},
         "fingerprint": [], "kind": "quadratic"},
        {"run_id": "run_me", "source": "comparative", "statement": "MINE stale", "outcome": "supported",
         "evidence": [1, 0], "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"},
         "fingerprint": [], "kind": "quadratic"},
    ])
    st = _state([_node(0, metric=9.0, op="draft", params={"x": 1.0}, code="x=1\n"),
                 _node(1, metric=6.0, parent_ids=[0], params={"x": 3.0}, code="x=3\n")])  # mine drifts
    monkeypatch.setattr(eng, "_reflect_client", lambda: FakeClient("P1 [BAD] regressed\n"))
    eng.lessons.reconcile_lessons(st)
    stmts = [r["statement"] for r in _rows(mem)]
    assert "OTHER run lesson" in stmts                                  # a DIFFERENT run's row is inviolate
    assert "MINE stale" not in stmts                                    # only this run's stale row rewritten


def test_reconcile_preserves_concurrent_append_during_rederivation(tmp_path, monkeypatch):
    """M5 regression: reconcile RE-READS the lessons file inside the interprocess lock, so a lesson a
    CONCURRENT run O_APPENDs during the (seconds-long) LLM re-derivation window survives. Before the
    fix, the whole-file rewrite from the PRE-lock snapshot silently clobbered that row."""
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    _seed(mem, [
        {"run_id": "run_me", "source": "comparative", "statement": "MINE stale", "outcome": "supported",
         "evidence": [1, 0], "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"},
         "fingerprint": [], "kind": "quadratic"},
    ])
    st = _state([_node(0, metric=9.0, op="draft", params={"x": 1.0}, code="x=1\n"),
                 _node(1, metric=6.0, parent_ids=[0], params={"x": 3.0}, code="x=3\n")])  # mine drifts

    # Simulate a concurrent run O_APPENDing its lesson DURING our re-derivation (between the pre-lock
    # read and the locked rewrite) by appending from inside the comparative re-derivation call.
    real_comp = eng._comparative_lessons

    def _comp_then_concurrent_append(*a, **k):
        _seed(mem, [{"run_id": "CONCURRENT", "source": "comparative", "statement": "CONCURRENT lesson",
                     "outcome": "failed", "evidence": [2, 3], "fingerprint": [], "kind": "quadratic"}])
        return real_comp(*a, **k)

    monkeypatch.setattr(eng, "_comparative_lessons", _comp_then_concurrent_append)
    monkeypatch.setattr(eng, "_reflect_client", lambda: FakeClient("P1 [BAD] regressed\n"))
    eng.lessons.reconcile_lessons(st)
    stmts = [r["statement"] for r in _rows(mem)]
    assert "CONCURRENT lesson" in stmts     # the concurrent append survived the rewrite (the M5 fix)
    assert "MINE stale" not in stmts        # this run's own stale row was still rewritten


def test_reconcile_offline_leaves_store_and_will_retry(tmp_path):
    # No LLM wired: reconcile must NOT write a templated stand-in — it leaves the stale row and clears
    # its gate so a later pass (once a client appears) re-checks rather than skipping forever.
    mem = tmp_path / "mem"
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    assert eng._reflect_client() is None                               # toy backend, no LLM
    _seed(mem, [{"run_id": "run_me", "source": "comparative", "statement": "stale", "outcome": "supported",
                 "evidence": [1, 0], "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"},
                 "fingerprint": [], "kind": "quadratic"}])
    st = _state([_node(0, metric=9.0), _node(1, metric=6.0, parent_ids=[0])])  # drift, but no client
    before = (mem / "lessons.jsonl").read_text()
    eng.lessons.reconcile_lessons(st)
    assert (mem / "lessons.jsonl").read_text() == before               # nothing written offline
    assert not [e for e in eng.store.read_all() if e.type == "lessons_reconciled"]
    assert eng.lessons._reconcile_sig_hash is None                     # gate cleared → will re-check


def test_reconcile_survives_malformed_store_rows(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    mem.mkdir(parents=True)
    (mem / "lessons.jsonl").write_text("not json\n" + orjson.dumps(
        {"run_id": "run_me", "source": "comparative", "statement": "MINE stale", "outcome": "supported",
         "evidence": [1, 0], "evidence_sig": {"1": "evaluated:4.0", "0": "evaluated:9.0"},
         "fingerprint": [], "kind": "quadratic"}).decode() + "\n")
    eng = _engine(tmp_path, reflection_priors=True, memory_dir=str(mem), comparative_lessons=True)
    st = _state([_node(0, metric=9.0, op="draft", params={"x": 1.0}, code="x=1\n"),
                 _node(1, metric=6.0, parent_ids=[0], params={"x": 3.0}, code="x=3\n")])
    monkeypatch.setattr(eng, "_reflect_client", lambda: FakeClient("P1 [BAD] regressed\n"))
    eng.lessons.reconcile_lessons(st)                                  # must not raise
    assert not any("MINE stale" in r.get("statement", "") for r in _rows(mem))
