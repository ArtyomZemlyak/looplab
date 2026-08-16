"""The EVALUATION-CONTRACT boundary, driven (guard-test tier 1) rather than pinned.

Every test here builds real run directories with real `task.snapshot.json` files and asks the real
providers for their real output strings. Nothing reads production source, so nothing here is
satisfiable by a comment.

The FIXTURE is the incident: `rubertlite-dr-unified-v8`'s own `research_completed` payload and the
`rubert-dr-0807` anchor it treated as a target. The two contracts are transcribed verbatim from those
runs' `task.snapshot.json` files, including the detail that makes this hard — their metric name and
reader are BYTE-IDENTICAL (`stdout_regex` / `RECALL@100: ([0-9.]+)`), so nothing keyed on the metric
could ever separate them.
"""
from __future__ import annotations

import json

import pytest

from looplab.engine.eval_contract import (
    EvalContract,
    comparable,
    contract_for_run_dir,
    contract_from_task,
    contract_notice,
)


# --------------------------------------------------------------------------- #
# The real corpus, transcribed from runs/*/task.snapshot.json on 2026-08-16.
# --------------------------------------------------------------------------- #

V8_TASK = {
    "kind": "repo", "id": "repo_task", "direction": "max",
    "goal": "Maximize test recall@100 on the ESCI v2 dense-retrieval benchmark …",
    "editable_path": "/home/jovyan/data/vectorizer-unified",
    "data": {},
    "eval": {
        "command": ["python", "-m", "vectorsearch.test"],
        "metric": {"pattern": "RECALL@100: ([0-9.]+)", "kind": "stdout_regex"},
        "timeout": 36000.0,
    },
}

# `rubert-dr-0807` — a different harness entirely (python-3.11 Lightning stack) over a different
# repo and a different corpus. Note the metric block is character-for-character V8's.
DR0807_TASK = {
    "kind": "repo", "id": "rubert_dr_0807", "direction": "max",
    "goal": "Maximize test recall@100 on the ESCI e-commerce dense-retrieval benchmark …",
    "editable_path": "/home/jovyan/data/vectorizer/dense-retrieval",
    "data": {
        "datasets": {"path": "/home/jovyan/data/datasets/dense-retrieval/rubertlite",
                     "mount": True, "edit": False},
        "model": {"path": "/home/jovyan/data/embedder/sergeyzh/rubert-tiny-lite",
                  "mount": True, "edit": False},
    },
    "eval": {
        "command": ["python", "looplab_eval.py", "--save_path", "models/rubertlite_run",
                    "--gpus", "1"],
        "metric": {"kind": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"},
        "timeout": 14400.0,
    },
}

# `rubertlite-dr-unified-v6`/`v7` — v8's REAL siblings. Same command, same repo, same reader; the
# goal prose and the timeout differ, and neither may make them a different evaluation.
V6_TASK = {**V8_TASK, "goal": "…DATA (not discoverable from the repo…)",
           "eval": {**V8_TASK["eval"], "timeout": 14400.0}}

# The anchor the v8 Researcher named, verbatim from
# runs/rubertlite-dr-unified-v8/events.jsonl seq 31 .data.memo.findings[1].
V8_ANCHOR_FINDING = (
    "The strongest verified anchor in the portfolio is rubert-dr-0807 #9: recall@100=0.8776 "
    "with clean symmetric in-batch InfoNCE (bs 8192, accumulate 2, lr 1e-3, wd 0.1, "
    "grad_clip 1.0, temp 0.05, OneCycleLR pct_start 0.2, 10 epochs, positive_threshold=1)."
)
ANCHOR_VALUE = "0.8776"


def _write_run(root, run_id: str, task: dict, *, nodes=()):
    """A real run directory: the snapshot this module reads, plus a foldable event log."""
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.snapshot.json").write_text(json.dumps(task), encoding="utf-8")
    rows = [{"type": "run_started", "seq": 0, "ts": 0.0,
             "data": {"task_id": task.get("id", ""), "direction": task.get("direction", "max"),
                      "goal": task.get("goal", "")}}]
    seq = 1
    for node_id, metric in nodes:
        rows.append({"type": "node_created", "seq": seq, "ts": float(seq),
                     "data": {"node_id": node_id, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"lr": 0.001},
                                       "rationale": "r"},
                              "code": "x=1", "files": {}}})
        seq += 1
        rows.append({"type": "node_evaluated", "seq": seq, "ts": float(seq),
                     "data": {"node_id": node_id, "generation": 0, "metric": metric,
                              "eval_seconds": 1.0, "extra_metrics": {}, "violations": [],
                              "trials": []}})
        seq += 1
    (d / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return d


# --------------------------------------------------------------------------- #
# 1 · The primitive.
# --------------------------------------------------------------------------- #

def test_the_incident_pair_is_provably_a_different_contract():
    """The whole point, on the real pair. Not 'unknown' — provably different."""
    v8 = contract_from_task(V8_TASK)
    dr = contract_from_task(DR0807_TASK)
    assert v8 is not None and dr is not None
    assert comparable(v8, dr) is False


def test_the_metric_name_and_reader_are_identical_so_they_cannot_be_the_key():
    """The trap, driven. Any partition keyed on the metric MERGES the incident pair."""
    v8 = contract_from_task(V8_TASK)
    dr = contract_from_task(DR0807_TASK)
    assert (v8.metric_kind, v8.metric_key) == (dr.metric_kind, dr.metric_key)
    assert v8.metric_key == "RECALL@100: ([0-9.]+)"
    # …and yet they are different evaluations, decided by the command and the declared paths.
    assert v8.command != dr.command
    assert v8.paths != dr.paths


def test_the_same_command_over_a_DIFFERENT_CORPUS_is_a_different_contract():
    """The property the declared paths are IN the key for, and the one `crossRunRank.js` names as
    its first refusal: "Two runs of `repo_task` may have optimized recall@100 against different
    corpora." Identical metric, identical command, different data — not comparable.

    Without this, a key of (metric, command) alone passes every other test in this file, because the
    incident pair already differs in its command. This is the case that key would get wrong.
    """
    same_command_other_corpus = {
        **V8_TASK,
        "editable_path": "/home/jovyan/data/vectorizer-unified",
        "data": {"datasets": {"path": "/home/jovyan/data/dr-local/v3", "mount": True}},
    }
    a, b = contract_from_task(V8_TASK), contract_from_task(same_command_other_corpus)
    assert a.command == b.command
    assert (a.metric_kind, a.metric_key) == (b.metric_kind, b.metric_key)
    assert comparable(a, b) is False
    assert "declared paths" in contract_notice(a, b)


def test_the_same_corpus_under_a_DIFFERENT_SCORER_is_a_different_contract():
    """The mirror: identical declared paths, different eval command. Both halves of the key earn
    their place, and `differences()` names the right one."""
    other_scorer = {**V8_TASK, "eval": {**V8_TASK["eval"],
                                        "command": ["python", "-m", "vectorsearch.test_v2"]}}
    a, b = contract_from_task(V8_TASK), contract_from_task(other_scorer)
    assert a.paths == b.paths
    assert comparable(a, b) is False
    assert "eval command" in contract_notice(a, b)


def test_the_same_command_and_corpus_under_a_DIFFERENT_READER_is_a_different_contract():
    """And the third facet. A run that scrapes `RECALL@100:` from stdout and one that reads a JSON
    key are not measuring through the same instrument even when everything else matches."""
    json_reader = {**V8_TASK, "eval": {**V8_TASK["eval"],
                                       "metric": {"kind": "stdout_json", "key": "recall@100"}}}
    a, b = contract_from_task(V8_TASK), contract_from_task(json_reader)
    assert a.command == b.command and a.paths == b.paths
    assert comparable(a, b) is False
    assert "metric reader" in contract_notice(a, b)


def test_negative_control_two_runs_on_one_contract_stay_comparable():
    """THE control that must never break: v6/v7/v8 differ in goal prose and timeout only."""
    assert comparable(contract_from_task(V8_TASK), contract_from_task(V6_TASK)) is True
    assert contract_notice(contract_from_task(V8_TASK), contract_from_task(V6_TASK)) == ""


def test_unknown_is_a_third_answer_and_is_never_a_difference():
    """Fail-open. A contract nobody can read must not read as a foreign one."""
    v8 = contract_from_task(V8_TASK)
    assert comparable(v8, None) is None
    assert comparable(None, None) is None
    assert contract_notice(v8, None) == ""
    assert contract_notice(None, v8) == ""


@pytest.mark.parametrize("task", [
    {}, {"eval": {}}, {"eval": {"metric": {}}}, {"kind": "quadratic", "id": "toy_quadratic"},
])
def test_a_task_with_no_evaluation_identity_is_unknown_not_an_empty_contract(task):
    """14 of this box's 46 snapshots are builtin-eval runs. An all-empty key would make every one
    of them 'the same contract' as every other, which is a claim none of them supports."""
    assert contract_from_task(task) is None


def test_a_missing_or_malformed_snapshot_is_unknown(tmp_path):
    assert contract_for_run_dir(tmp_path / "nope") is None
    d = tmp_path / "broken"
    d.mkdir()
    (d / "task.snapshot.json").write_text("{not json", encoding="utf-8")
    assert contract_for_run_dir(d) is None


def test_the_notice_names_the_facet_so_an_operator_can_check_it():
    notice = contract_notice(contract_from_task(V8_TASK), contract_from_task(DR0807_TASK),
                             other_run_id="rubert-dr-0807")
    assert "rubert-dr-0807" in notice
    assert "eval command" in notice
    assert "looplab_eval.py" in notice          # the DIFFERENCE, quoted, not just asserted
    assert "not a target for it" in notice
    # It must not editorialize about the number's correctness.
    assert "wrong" not in notice.lower() and "invalid" not in notice.lower()


def test_the_notice_never_carries_the_metric_value():
    """It annotates; it is not a second place a number can leak from."""
    assert ANCHOR_VALUE not in contract_notice(
        contract_from_task(V8_TASK), contract_from_task(DR0807_TASK), other_run_id="rubert-dr-0807")


# --------------------------------------------------------------------------- #
# 2 · The real providers, over real run directories. This is the property that matters:
#     does the boundary reach the string the Researcher is handed?
# --------------------------------------------------------------------------- #

@pytest.fixture()
def corpus(tmp_path):
    """v8 plus its real neighbours, as run directories."""
    root = tmp_path / "runs"
    root.mkdir()
    _write_run(root, "rubertlite-dr-unified-v8", V8_TASK, nodes=[(0, 0.762048)])
    _write_run(root, "rubertlite-dr-unified-v6", V6_TASK, nodes=[(0, 0.7281)])
    _write_run(root, "rubert-dr-0807", DR0807_TASK, nodes=[(9, 0.8776)])
    return root


def _bind(provider, run_id, task):
    from types import SimpleNamespace
    provider.bind_state(SimpleNamespace(run_id=run_id, task_id=task.get("id", ""),
                                        direction=task.get("direction", "max"),
                                        goal=task.get("goal", "")))
    return provider


def test_list_all_runs_marks_the_foreign_contract_and_leaves_the_sibling_alone(corpus):
    """`list_all_runs` is the tool that gave v8 the 0807 row: 20 of its calls in v8's spans.jsonl
    mention the anchor. It is cross-task BY DESIGN, so the boundary has to be ON the row."""
    from looplab.tools.run_tools import AllRunsTools
    out = _bind(AllRunsTools(corpus), "rubertlite-dr-unified-v8", V8_TASK).execute(
        "list_all_runs", {})
    foreign = [ln for ln in out.splitlines() if ln.startswith("rubert-dr-0807")]
    sibling = [ln for ln in out.splitlines() if ln.startswith("rubertlite-dr-unified-v6")]
    assert foreign and sibling, out
    assert "DIFFERENT EVAL CONTRACT" in foreign[0]
    # THE NEGATIVE CONTROL, on the live surface: a true sibling is untouched.
    assert "DIFFERENT EVAL CONTRACT" not in sibling[0]
    # …and the foreign row still SHOWS its number. This withholds nothing.
    assert "0.8776" in foreign[0]


def test_read_run_experiment_states_the_boundary_beside_the_number(corpus):
    """The dominant carrier: 50 of v8's anchor-bearing tool spans are `read_run_experiment`."""
    from looplab.tools.run_tools import AllRunsTools
    provider = _bind(AllRunsTools(corpus), "rubertlite-dr-unified-v8", V8_TASK)
    out = provider.execute("read_run_experiment", {"run_id": "rubert-dr-0807", "node_id": 9})
    assert "DIFFERENT EVALUATION CONTRACT" in out
    assert "looplab_eval.py" in out
    # The receipt comes FIRST — before the metric line it qualifies.
    assert out.index("DIFFERENT EVALUATION CONTRACT") < out.index("0.8776")


def test_reading_a_true_sibling_gains_nothing_at_all(corpus):
    """If this ever fires, the filter has started hiding legitimate prior results."""
    from looplab.tools.run_tools import AllRunsTools
    provider = _bind(AllRunsTools(corpus), "rubertlite-dr-unified-v8", V8_TASK)
    out = provider.execute("read_run_experiment",
                           {"run_id": "rubertlite-dr-unified-v6", "node_id": 0})
    assert "CONTRACT" not in out
    assert "0.7281" in out


def test_the_operator_assistant_is_unchanged(corpus):
    """`MachineRunsTools` never binds a self run — the operator's portfolio reads must not gain a
    boundary about a run they are not conducting."""
    from looplab.tools.machine_runs_tools import MachineRunsTools
    provider = MachineRunsTools(corpus)
    provider.bind_state(None)
    out = provider.execute("read_run_experiment", {"run_id": "rubert-dr-0807", "node_id": 9})
    assert "CONTRACT" not in out
    assert "0.8776" in out


def test_an_unreadable_foreign_snapshot_is_silent_not_flagged(corpus):
    """Fail-open on the live surface, not only in the primitive."""
    from looplab.tools.run_tools import AllRunsTools
    (corpus / "rubert-dr-0807" / "task.snapshot.json").unlink()
    provider = _bind(AllRunsTools(corpus), "rubertlite-dr-unified-v8", V8_TASK)
    out = provider.execute("read_run_experiment", {"run_id": "rubert-dr-0807", "node_id": 9})
    assert "CONTRACT" not in out
    assert "0.8776" in out


def test_an_unbound_provider_flags_nothing(corpus):
    from looplab.tools.run_tools import AllRunsTools
    out = AllRunsTools(corpus).execute("list_all_runs", {})
    assert "DIFFERENT EVAL CONTRACT" not in out


def test_the_real_construction_shape_works_without_bind_state(corpus):
    """`agents/factory.py` builds these as `AllRunsTools(Path(run_dir).parent, Path(run_dir).name)` —
    the self run id arrives at CONSTRUCTION, and `bind_state` only re-confirms it. A test that only
    ever binds would pass over a provider that never does, so drive the production shape too."""
    from looplab.tools.run_tools import AllRunsTools, SiblingRunTools
    for cls in (AllRunsTools, SiblingRunTools):
        provider = cls(corpus, "rubertlite-dr-unified-v8")
        out = provider.execute("read_run_experiment" if cls is AllRunsTools
                               else "read_sibling_experiment",
                               {"run_id": "rubert-dr-0807", "node_id": 9})
        if "no such" in out or "not a sibling" in out:
            continue                                  # scope refusal is a different rung
        assert "DIFFERENT EVALUATION CONTRACT" in out, (cls.__name__, out)


def test_find_analogous_across_runs_carries_the_receipt(corpus):
    """It ranks by PARAM distance and prints each row's metric — the shape most likely to read as
    'how a nearby config performed' on this run's own scale."""
    from looplab.tools.run_tools import SiblingRunTools
    provider = SiblingRunTools(corpus)
    # A sibling tool is same-task scoped, so give it 0807's task id to reach across.
    _bind(provider, "rubertlite-dr-unified-v8", {**V8_TASK, "id": "rubert_dr_0807"})
    out = provider.execute("find_analogous_across_runs", {"params": {"lr": 0.001}})
    if "rubert-dr-0807" in out:
        assert "DIFFERENT EVAL CONTRACT" in out


# --------------------------------------------------------------------------- #
# 3 · What this does NOT reach. Stated as a test so the residue cannot be forgotten.
# --------------------------------------------------------------------------- #

def test_a_foreign_number_laundered_into_a_native_claim_is_out_of_reach():
    """MEASURED RESIDUE, and the reason option (c) was rejected for the memory store.

    `rubertlite-dr-unified-v7` is a genuine `repo_task` run on v8's own contract. It published this
    research claim, verbatim from /home/jovyan/data/looplab-memory/research_claims.jsonl. Its own
    numbers (0.8173, 0.8651) are native and legitimately comparable; 0.8776 is 0807's and is not.
    One sentence, three floats, two provenances, and NO deterministic rule separates them — which is
    why nothing in this change tries to, and why a value-stripping filter is not implementable here.
    """
    laundered = ("Positive-aware false-negative masking (fn_mask_ratio 0.95) regressed recall@100 "
                 "on the rubert-tiny-lite backbone: 0.8173 at temp 0.01 and 0.8651 at temp 0.05, "
                 "both below the 0.8776 symmetric-InfoNCE baseline.")
    assert "0.8173" in laundered and "0.8776" in laundered
    # The ROW's own contract is v8's own, so no row-level partition can withhold it, and it should
    # not: the claim is v7's and is about this evaluation.
    assert comparable(contract_from_task(V8_TASK), contract_from_task(V8_TASK)) is True


def test_the_researcher_finding_that_started_this_is_still_reachable_text():
    """The fixture, kept honest: this change does not delete or rewrite what a past run recorded."""
    assert ANCHOR_VALUE in V8_ANCHOR_FINDING
    assert "rubert-dr-0807" in V8_ANCHOR_FINDING


# --------------------------------------------------------------------------- #
# 4 · Nothing here may reach the record (docs/36).
# --------------------------------------------------------------------------- #

def test_the_boundary_cannot_move_a_metric_or_a_champion(corpus):
    """Fold the foreign run before and after every provider call and require byte equality."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    from looplab.tools.run_tools import AllRunsTools

    def snapshot():
        st = fold(EventStore(corpus / "rubert-dr-0807" / "events.jsonl").read_all())
        best = st.best()
        return (st.task_id, st.direction, len(st.nodes),
                best.id if best else None, best.metric if best else None)

    before = snapshot()
    provider = _bind(AllRunsTools(corpus), "rubertlite-dr-unified-v8", V8_TASK)
    provider.execute("list_all_runs", {})
    provider.execute("read_run_experiment", {"run_id": "rubert-dr-0807", "node_id": 9})
    provider.execute("read_run_code", {"run_id": "rubert-dr-0807", "node_id": 9})
    assert snapshot() == before
    assert before[4] == 0.8776           # the number is untouched on disk and in the fold
