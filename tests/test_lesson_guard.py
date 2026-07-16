"""Lesson over-generalization guard (PART IV D6, §21.7/§21.12 — Phase 1b).

Locks in that the guard grounds each distilled lesson in its evidence node's real outcome, runs the 0c
verifier, flags the node_63 mis-lesson pattern (over-generalizes a SOUND direction), attaches the finding
to a concept-graph branch, and degrades to available=False without a client — strictly audit-only."""
from __future__ import annotations

from looplab.core.models import RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.concept_graph import skeleton_for
from looplab.trust.lesson_guard import contradiction_scan, guard_lessons, _lesson_records


class _Stub:
    """Verifier stub: the false-negative lesson over-generalizes a sound direction; others don't.
    Criteria order for lesson_overgeneralization = [over_generalizes, direction_sound]."""
    def complete_tool(self, messages, json_schema):
        u = messages[-1]["content"].lower()
        if "false negative" in u or "false-negative" in u:
            return {"verdicts": ["strong_yes", "yes"], "rationales": ["broadens", "sound"]}
        return {"verdicts": ["no", "no"], "rationales": ["ok", "ok"]}

    def complete_text(self, messages):
        return "x"


class _ContraStub:
    def complete_tool(self, messages, json_schema):
        u = messages[-1]["content"].lower()
        # the false-neg 'don't' lesson contradicts the false-neg 'do' lesson
        contra = "do not correct false negatives" in u and "filter false negatives" in u
        return {"verdicts": ["strong_yes" if contra else "no"], "rationales": ["r"]}

    def complete_text(self, messages):
        return "x"


def _run(tmp_path, lessons, *, with_failed_node=True) -> "RunState":
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 10, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"seed": 0.0}, "theme": "base",
                                       "rationale": "baseline"}})
    s.append("node_evaluated", {"node_id": 10, "metric": 0.80})
    if with_failed_node:
        s.append("node_created", {"node_id": 63, "parent_ids": [10], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {"seed": 1.0},
                                           "theme": "fn", "rationale": "loss-side false negative hack"}})
        s.append("node_failed", {"node_id": 63, "error": "nan", "reason": "crash"})
    s.append("lessons_distilled", {"at_node": 64, "trigger": "run_end", "count": len(lessons),
                                   "pairs": [[63, 10]], "lessons": lessons})
    return fold(s.read_all())


def test_lesson_records_flatten(tmp_path):
    st = _run(tmp_path, [{"statement": "a", "outcome": "o", "claim_stance": "support",
                          "evidence": [63]},
                         {"statement": "b", "outcome": "o2", "evidence": [10]}])
    recs = _lesson_records(st)
    assert [r["statement"] for r in recs] == ["a", "b"]
    assert recs[0]["node_ids"] == [63] and recs[0]["at_node"] == 64
    assert recs[0]["claim_stance"] == "support"


def test_lesson_records_ignore_poison_watermark_and_malformed_lesson_rows():
    state = RunState()
    state.lessons_distilled = [{
        "at_node": "9" * 10_000,
        "pairs": [[63, 10]],
        "lessons": ["bad-row", {"statement": "usable", "outcome": "noted"}],
    }]
    assert _lesson_records(state) == [{
        "statement": "usable", "outcome": "noted", "claim_stance": "",
        "at_node": 0, "node_ids": [63],
    }]


def test_flags_the_overgeneralizing_mislesson(tmp_path):
    st = _run(tmp_path, [
        {"statement": "do not use false negative filtering", "outcome": "node 63 failed", "evidence": [63]},
        {"statement": "larger batch size helped", "outcome": "improved", "evidence": [10]}])
    res = guard_lessons(st, client=_Stub(), samples=3, graph=skeleton_for("dense-retrieval"))
    assert res["available"] is True and res["n_lessons"] == 2 and res["n_flagged"] == 1
    flagged = [f for f in res["findings"] if f["flagged"]]
    assert flagged[0]["statement"] == "do not use false negative filtering"
    assert flagged[0]["over_generalizes"] == 1.0 and flagged[0]["direction_sound"] == 0.75
    # the finding attaches to the taxonomy branch (§21.7)
    assert "negatives/false-neg-handling" in flagged[0]["concepts"]
    assert flagged[0]["rescope_hint"]
    # flagged lessons sort first
    assert res["findings"][0]["flagged"] is True


def test_evidence_is_grounded_in_the_failed_node(tmp_path):
    # the guard must ground on node 63's real FAILED outcome, not the lesson wording
    from looplab.trust.lesson_guard import _evidence_text
    st = _run(tmp_path, [{"statement": "x", "outcome": "", "evidence": [63]}])
    rec = _lesson_records(st)[0]
    txt = _evidence_text(rec, st)
    assert "#63" in txt and "FAILED" in txt


def test_degrades_without_client(tmp_path):
    st = _run(tmp_path, [{"statement": "some lesson", "outcome": "o", "evidence": [10]}])
    res = guard_lessons(st, client=None)
    assert res["available"] is False and res["n_lessons"] == 1
    assert res["findings"][0]["flagged"] is False and res["findings"][0]["over_generalizes"] is None


def test_empty_run_has_no_lessons(tmp_path):
    res = guard_lessons(RunState(), client=_Stub())
    assert res["n_lessons"] == 0 and res["n_flagged"] == 0


def test_contradiction_scan_finds_the_mislesson_pair(tmp_path):
    st = _run(tmp_path, [
        {"statement": "do not correct false negatives", "outcome": "o", "evidence": [63]},
        {"statement": "filter false negatives on the data side", "outcome": "o", "evidence": [10]},
        {"statement": "use a warmup schedule", "outcome": "o", "evidence": [10]}])
    res = contradiction_scan(st, client=_ContraStub())
    assert res["available"] is True
    assert len(res["contradictions"]) == 1
    pair = res["contradictions"][0]
    assert "false negatives" in pair["a"] and "false negatives" in pair["b"]


def test_contradiction_scan_degrades_without_client(tmp_path):
    st = _run(tmp_path, [{"statement": "a", "outcome": "o", "evidence": [10]}])
    res = contradiction_scan(st, client=None)
    assert res["available"] is False and res["contradictions"] == []


class _FailingVerifierClient:
    """Every grade fails to produce a usable verdict (total endpoint failure) — the verifier can grade
    NOTHING, so `mean` comes back None for every pair."""
    def complete_tool(self, messages, json_schema):
        raise RuntimeError("endpoint down")

    def complete_text(self, messages):
        raise RuntimeError("endpoint down")


def test_guard_lessons_all_fail_is_not_reported_as_clean(tmp_path):
    """Honesty guard (same class as the contradiction-scan / E3 fix): a wired-but-dead client that grades
    NOTHING must not print "0 flagged" as a clean bill of health — adjudicated must be False."""
    st = _run(tmp_path, [
        {"statement": "false negatives are hopeless, abandon the whole direction", "outcome": "o",
         "evidence": [63]}])
    res = guard_lessons(st, client=_FailingVerifierClient())
    assert res["available"] is True          # a client WAS wired
    assert res["n_flagged"] == 0             # ...but nothing was actually graded
    assert res["n_scored"] == 0
    assert res["adjudicated"] is False       # so: INCONCLUSIVE, not "no over-generalization found"


def test_guard_lessons_normal_run_is_adjudicated(tmp_path):
    """A working verifier that grades the lessons reports adjudicated=True (not tripped by the guard)."""
    st = _run(tmp_path, [
        {"statement": "correcting false negatives always helps", "outcome": "o", "evidence": [63]}])
    res = guard_lessons(st, client=_Stub())
    assert res["adjudicated"] is True and res["n_scored"] == 1


def test_contradiction_scan_all_fail_is_not_reported_as_no_contradictions(tmp_path):
    """Honesty guard (same class as E3-C): if EVERY verify call fails, an empty contradictions list must NOT
    read as a clean bill of health — `adjudicated` must be False so the CLI says INCONCLUSIVE."""
    st = _run(tmp_path, [
        {"statement": "do not correct false negatives", "outcome": "o", "evidence": [63]},
        {"statement": "filter false negatives on the data side", "outcome": "o", "evidence": [10]}])
    res = contradiction_scan(st, client=_FailingVerifierClient())
    assert res["available"] is True          # a client WAS wired
    assert res["contradictions"] == []       # ...but nothing could be judged
    assert res["n_judged"] == 0
    assert res["adjudicated"] is False       # so: INCONCLUSIVE, not "no contradictions found"
