"""Doc 52 row 17: the prior and tool-result RECORDS, the citation instrument over them, and the
read-side utility term + forgetting they feed.

* `prior_injected` — the main task writes, at every prior load, which lesson rows (by
  `lesson_id`), notes and case each role's prompt prior was built from.
* `memory_read` — every cross-run / memory / skill tool call reports an invocation id, the exact
  rendered result's digest and the rows it showed, through the engine sink.
* `events/prior_citations.py` joins the two to the proposals that followed (a stated, lexical
  rule) and `lesson_utility.jsonl` accumulates the per-run answer, which the next prior scan folds
  onto each lesson as `utility` for `lesson_rank_key` / `filter_useless`.
"""
from __future__ import annotations

import json

import anyio
import orjson

from looplab.core.phase_events import MEMORY_READ, emit_memory_read, phase_sink_scope
from looplab.engine.lesson_hygiene import (
    USELESS_MIN_SHOWN, filter_useless, lesson_id, lesson_rank_key, lesson_utility)
from looplab.events.prior_citations import (
    CITATION_CONTAINMENT, cites, prior_citation_report, utility_rows)
from looplab.events.types import DIAGNOSTIC_EVENTS, EV_MEMORY_READ, EV_PRIOR_INJECTED
from looplab.tools.memory_tools import MemoryTools
from looplab.tools.skills import SkillTools
from tests.factories import make_engine

STATEMENT = "Warm-start the tokenizer from the base checkpoint before fine-tuning"
OTHER = "Use cosine learning-rate decay with a short warmup"


# ------------------------------------------------------------------ identity, utility, forgetting

def test_lesson_id_is_derived_from_the_normalized_statement():
    assert lesson_id({"statement": "  Use   COSINE decay "}) == lesson_id("use cosine decay")
    assert lesson_id(STATEMENT).startswith("les-") and len(lesson_id(STATEMENT)) == 28
    assert lesson_id(STATEMENT) != lesson_id(OTHER)


def test_utility_is_laplace_smoothed_and_neutral_when_unrecorded():
    assert lesson_utility({"statement": "s"}) is None
    assert lesson_utility({"utility": {"shown": 0, "cited": 0}}) is None
    assert lesson_utility({"utility": {"shown": 2, "cited": 2}}) == 3 / 4
    assert lesson_utility({"utility": {"shown": 8, "cited": 0}}) == 1 / 10
    assert lesson_utility({"utility": {"shown": "x"}}) is None


def test_the_rank_key_prefers_the_cited_lesson_among_equals_and_leaves_old_rows_alone():
    cited = {"statement": "a", "confidence": 0.6, "evidence_count": 1, "utility": {"shown": 4, "cited": 3}}
    ignored = {"statement": "b", "confidence": 0.6, "evidence_count": 1, "utility": {"shown": 4, "cited": 0}}
    unknown = {"statement": "c", "confidence": 0.6, "evidence_count": 1}
    ranked = sorted([(0.5, 1, ignored), (0.5, 2, unknown), (0.5, 3, cited)],
                    key=lambda t: lesson_rank_key(*t))
    assert [t[2]["statement"] for t in ranked] == ["a", "c", "b"]
    assert lesson_rank_key(0.9, 1, unknown)[:2] == (-0.9, -0.6), "similarity and corroboration first"


def test_forgetting_by_uselessness_quarantines_at_read_and_keeps_the_rest():
    useless = {"statement": "u", "utility": {"shown": USELESS_MIN_SHOWN, "cited": 0}}
    young = {"statement": "y", "utility": {"shown": USELESS_MIN_SHOWN - 1, "cited": 0}}
    cited = {"statement": "c", "utility": {"shown": 50, "cited": 1}}
    plain = {"statement": "p"}
    kept = filter_useless([(0.5, 0, useless), (0.5, 1, young), (0.5, 2, cited), (0.5, 3, plain)])
    assert [t[2]["statement"] for t in kept] == ["y", "c", "p"]


# ------------------------------------------------------------------ the instrument

def test_the_citation_rule_is_containment_or_the_id():
    assert cites(STATEMENT, "We warm-start the tokenizer from the base checkpoint before fine-tuning, then…")
    assert not cites(STATEMENT, "try a larger batch size and a cosine schedule")
    assert cites(OTHER, f"builds on {lesson_id(OTHER)}", lesson_id=lesson_id(OTHER))
    assert 0.5 < CITATION_CONTAINMENT < 1.0


def _prior(role, rows, at_node=0):
    return ("prior_injected", {"role": role, "at_node": at_node, "phase": "run_start",
                               "rows": [{"id": lesson_id(s), "statement": s} for s in rows]})


def _node(node_id, rationale):
    return ("node_created", {"node_id": node_id, "idea": {"operator": "draft", "params": {"lr": 0.1},
                                                          "rationale": rationale}})


def test_the_report_joins_by_log_order_and_credits_tool_reads_separately():
    events = [
        _prior("researcher", [STATEMENT, OTHER]),
        _node(0, "Warm-start the tokenizer from the base checkpoint before fine-tuning."),
        ("memory_read", {"tool": "search_lessons", "invocation_id": "inv-1",
                         "rows": [{"id": lesson_id(OTHER), "statement": OTHER}]}),
        _node(1, "Use cosine learning-rate decay with a short warmup; larger batch."),
        _prior("researcher", [OTHER], at_node=2),
        _node(2, "unrelated idea about dropout"),
    ]
    report = prior_citation_report(events)
    assert (report["proposals"], report["injections"], report["reads"]) == (3, 2, 1)
    warm, cos = report["lessons"][lesson_id(STATEMENT)], report["lessons"][lesson_id(OTHER)]
    assert (warm["shown"], warm["cited"]) == (2, 1), "shown to nodes 0 and 1, cited by 0"
    assert (cos["shown"], cos["cited"]) == (3, 1), "shown to all three, cited by 1"
    assert (cos["shown_tool"], cos["cited_tool"]) == (1, 1), "the read before node 1 is credited apart"
    assert report["shown_pairs"] == 5 and report["cited_pairs"] == 2
    assert report["citation_rate"] == 2 / 5 and "joined by log order" in report["rule"]
    rows = utility_rows(report, run_id="r1", now=1.0)
    assert sorted((r["lesson_id"], r["shown"], r["cited"]) for r in rows) == sorted([
        (lesson_id(STATEMENT), 2, 1), (lesson_id(OTHER), 3, 1)])
    assert prior_citation_report([])["citation_rate"] is None


# ------------------------------------------------------------------ the records

def _memory_dir(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    rows = [{"statement": STATEMENT, "outcome": "supported", "task_id": "toy_quadratic",
             "direction": "min", "role": "researcher", "confidence": 0.7, "evidence_count": 2,
             "fingerprint": ["kind:quadratic", "dir:min"], "run_id": "other-run"},
            {"statement": OTHER, "outcome": "tested", "task_id": "toy_quadratic", "direction": "min",
             "confidence": 0.6, "fingerprint": ["kind:quadratic", "dir:min"], "run_id": "other-run"}]
    (mem / "lessons.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return mem


def test_memory_tools_report_their_reads_with_the_rows_they_showed(tmp_path):
    mem = _memory_dir(tmp_path)
    tools = MemoryTools(str(mem))
    seen = []
    with phase_sink_scope(lambda etype, data: seen.append((etype, data))):
        answer = tools.execute("search_lessons", {"query": "tokenizer"})
    assert "tokenizer" in answer.lower()
    assert [e for e, _ in seen] == [MEMORY_READ]
    data = seen[0][1]
    assert data["tool"] == "search_lessons" and data["invocation_id"].startswith("inv-")
    assert data["result_chars"] == len(answer) and len(data["result_sha256"]) == 64
    assert data["rows"] == [{"id": lesson_id(STATEMENT), "statement": STATEMENT}]
    assert data["args"] == {"query": "tokenizer"}
    assert tools.execute("search_lessons", {"query": "tokenizer"}) == answer, "no sink: same answer, no row"
    assert len(seen) == 1


def test_skill_reads_and_no_sink_are_recorded_and_silent_respectively(tmp_path):
    (tmp_path / "s.md").write_text("---\nname: cv\ndescription: how to cross-validate\n---\nBody: K-fold",
                                   encoding="utf-8")
    tools = SkillTools(str(tmp_path))
    seen = []
    with phase_sink_scope(lambda etype, data: seen.append(data)):
        tools.execute("use_skill", {"name": "cv"})
        tools.execute("list_skills", {})
    assert len(seen) == 1 and seen[0]["rows"] == [{"id": "skill:cv", "statement": "how to cross-validate"}]
    assert seen[0]["source"] == {"tier": "global", "status": ""}
    assert emit_memory_read("x", {}, "r") is None, "outside a run nothing is written"


def test_the_engine_records_the_prior_it_injected_and_folds_utility_back(tmp_path):
    """Driven through a real toy run: the run-start load writes one `prior_injected` per role, the
    finalize pass writes `lesson_utility.jsonl` + the `prior_citations` receipt, and a SECOND
    engine over the same store reads the utility back onto the lesson."""
    mem = _memory_dir(tmp_path)
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=2, reflection_priors=True,
                      memory_dir=str(mem))
    state = anyio.run(eng.run)
    assert state.finished
    rows = [e for e in eng.store.read_all() if e.type == EV_PRIOR_INJECTED]
    assert {r.data["role"] for r in rows} >= {"researcher"} and EV_PRIOR_INJECTED in DIAGNOSTIC_EVENTS
    researcher = next(r.data for r in rows if r.data["role"] == "researcher")
    assert researcher["phase"] == "run_start" and researcher["at_node"] == 0
    shown = {row["id"] for row in researcher["rows"]}
    assert lesson_id(STATEMENT) in shown, "the exact-task lesson was spliced into the prompt"
    assert researcher["source"]["complete"] is True and researcher["chars"] > 0
    assert all(set(row) >= {"id", "statement", "outcome", "task_id", "sim"} for row in researcher["rows"])
    created = [e for e in eng.store.read_all() if e.type == "node_created"]
    assert rows[0].seq < created[0].seq, "the record precedes the proposals it was shown to"
    assert EV_MEMORY_READ in DIAGNOSTIC_EVENTS

    note = [e.data for e in eng.store.read_all() if e.type == "reflection_note"][-1]
    assert note["prior_citations"]["proposals"] == len(created)
    assert note["prior_citations"]["shown"] >= len(created)
    ledger = [orjson.loads(line) for line in (mem / "lesson_utility.jsonl").read_bytes().splitlines()]
    assert {r["lesson_id"] for r in ledger} == shown
    assert all(r["run_id"] == state.run_id and r["shown"] >= 1 for r in ledger)

    again = make_engine(tmp_path / "run2", n_seeds=1, max_nodes=1, reflection_priors=True,
                        memory_dir=str(mem))
    ctx = again.lessons._scan_prior_context(None, None)
    parsed = {lesson_id(o): o for _, o in ctx[1]}
    assert parsed[lesson_id(STATEMENT)]["utility"]["shown"] >= 1
    text, receipt = again.lessons._pick_role_prior(ctx, "researcher")
    assert receipt["rows"] and text == again.lessons._render_role_prior(ctx, "researcher")


def test_the_engine_sink_redacts_nested_statements(tmp_path):
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1)
    secret = "sk-" + "a" * 40
    eng._append_phase_event(EV_MEMORY_READ, {"tool": "t", "rows": [{"id": "x", "statement": f"key {secret}"}],
                                             "source": {"note": secret}})
    row = [e.data for e in eng.store.read_all() if e.type == EV_MEMORY_READ][-1]
    assert secret not in json.dumps(row)
