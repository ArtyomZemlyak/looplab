"""A paraphrase cluster the model DECLINED is not bought again at every finalize (review 2026-09-22,
EK-09 = doc 50 ENG3-12).

`lesson_hygiene._agentic_merge_lessons` re-clusters the WHOLE shared lesson store at every finalize
and sends each multi-member cluster to the model. A declined cluster leaves no record — the store is
rewritten only when something merged — so it was re-sent at the next finalize of every run, forever:
measured through the real `LessonMemory.consolidate_lessons_file`, N declined clusters cost N calls
per finalize (1/1/1, 5/5/5, 20/20/20 for N = 1, 5, 20 over three finalizes), whichever task the later
run wrote to. `lesson_merge_verdicts.jsonl` remembers the QUESTION it declined; the memo only removes
calls, so it carries no flag.

Every test here goes through the real finalize hygiene call and a counting client — nothing is read
off the source. The CHANGED-cluster test is the one that keeps the memo honest: a later run's lesson
that lands in a declined cluster makes it a new question, and the new question must be asked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.core.errors import BudgetExceeded
from looplab.engine import lessons as lessons_mod
from looplab.engine.lesson_hygiene import MergeVerdictMemo
from looplab.engine.lessons import LESSON_MERGE_VERDICTS, LessonMemory


class _Counting:
    """A model that answers every merge question with "no merge" (or raises `exc` on call `fail_at`)
    and records which statements each question carried."""

    model = "fake-decliner"

    def __init__(self, *, exc=None, fail_at=None, merge=None):
        self.calls = 0
        self.questions: list[str] = []
        self.exc, self.fail_at, self.merge = exc, fail_at, merge

    def complete_tool(self, messages, schema):
        self.calls += 1
        self.questions.append(messages[-1]["content"])
        if self.exc is not None and (self.fail_at is None or self.calls == self.fail_at):
            raise self.exc
        if self.merge is not None and self.merge in messages[-1]["content"]:
            return {"groups": [{"members": [0, 1], "merged": f"{self.merge} merged"}]}
        return {"groups": []}

    def complete_text(self, messages):         # the text parser's route: same verdict
        return json.dumps(self.complete_tool(messages, None))


def _lesson(statement: str, run: str, task: str = "t") -> dict:
    return {"task_id": task, "statement": statement, "outcome": "supported",
            "claim_stance": "support", "confidence": 0.6, "run_id": run, "run_uid": run + "-uid",
            "role": "researcher", "fingerprint": ["kind:x"], "direction": "min", "evidence": [1]}


def _pair(i: int) -> tuple[str, str]:
    # Two statements sharing private tokens: lexical, BM25 and vector agree -> ONE cluster. Pairs
    # share no token with each other, so they never chain into a larger component.
    return (f"tok{i}a tok{i}b tok{i}c", f"tok{i}a tok{i}b tok{i}d")


def _store(tmp_path: Path, n: int) -> Path:
    mem = tmp_path / "mem"
    mem.mkdir()
    path = mem / "lessons.jsonl"
    rows = [row for i in range(n)
            for row in (_lesson(_pair(i)[0], f"r{i}"), _lesson(_pair(i)[1], f"s{i}"))]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _finalize(path: Path, client, **kw) -> int:
    """One finalize's hygiene pass, as `append_lessons(hygiene=True)` makes it; its call count."""
    LessonMemory.consolidate_lessons_file(path, client, None, **kw)
    return client.calls


def _memo_keys(path: Path) -> list[str]:
    return LessonMemory.merge_verdict_keys(path.parent / LESSON_MERGE_VERDICTS)


@pytest.mark.parametrize("later_task", ["t", "another-task"])
def test_a_declined_cluster_is_asked_once_not_at_every_finalize(tmp_path, later_task):
    """The measurement, driven: 3 declined clusters cost 3, 3, 3 before the memo and 3, 0, 0 after —
    whether the later run wrote to the SAME task's bucket (the shape of every shared store here,
    which is why scoping to touched buckets was refused) or to another one."""
    path = _store(tmp_path, 3)
    assert _finalize(path, _Counting()) == 3
    for later in (1, 2):
        _append(path, _lesson(f"unrelated{later} zzz{later} qqq{later}", f"later{later}",
                              task=later_task))
        assert _finalize(path, _Counting()) == 0, "an unchanged declined cluster was bought again"
    assert len(_memo_keys(path)) == 3, "each declined question is remembered exactly once"


def test_a_changed_cluster_is_asked_again_and_only_it(tmp_path):
    """The other half, and the one that makes the memo safe: a later run's lesson that lands IN a
    declined cluster makes it a different question. MUTATION: key the memo on the bucket alone (or
    on the cluster's first member) -> this asks 0 questions and the new paraphrase is never judged."""
    path = _store(tmp_path, 3)
    _finalize(path, _Counting())
    _append(path, _lesson("tok0a tok0b tok0e", "later"))           # joins cluster 0
    client = _Counting()
    assert _finalize(path, client) == 1
    [question] = client.questions
    assert all(s in question for s in ("tok0a tok0b tok0c", "tok0a tok0b tok0d",
                                       "tok0a tok0b tok0e")), question
    assert "tok1a" not in question and "tok2a" not in question


def test_a_changed_merge_prompt_is_a_changed_question(tmp_path):
    """The key is the QUESTION (`hybrid_merge.merge_request`), so an operator's `merge_system.md`
    override re-opens every cluster once — a remembered "no" was an answer to other instructions."""
    class _Override:
        def get(self, name, default, **variables):
            return "A different librarian prompt about $kind."

    path = _store(tmp_path, 2)
    _finalize(path, _Counting())
    assert _finalize(path, _Counting(), prompts=_Override()) == 2
    assert _finalize(path, _Counting(), prompts=_Override()) == 0


def test_a_failed_adjudication_is_not_remembered_as_a_verdict(tmp_path):
    """All-singletons is two facts — "looked and merged nothing" and "the call failed open" — and
    only the first is a verdict. MUTATION: drop the `answered` receipt check -> the failed pass
    writes three keys and the healthy pass after it asks nothing."""
    path = _store(tmp_path, 3)
    assert _finalize(path, _Counting(exc=RuntimeError("endpoint 500"))) == 3
    assert _memo_keys(path) == []
    assert _finalize(path, _Counting()) == 3


def test_a_merged_cluster_is_consumed_by_the_rewrite_not_remembered(tmp_path):
    """Only "no merge" is remembered. A merge rewrites the store, so its cluster is gone — and a
    remembered MERGE would be a paid decision replayed on a store it was not made about."""
    path = _store(tmp_path, 2)
    _finalize(path, _Counting(merge="tok0a tok0b tok0c"))
    statements = [json.loads(line)["statement"] for line in path.read_text().splitlines()]
    assert "tok0a tok0b tok0c merged" in statements, statements
    assert len(_memo_keys(path)) == 1, "only the declined cluster (pair 1) is remembered"
    assert _finalize(path, _Counting()) == 0


def test_a_spend_ceiling_keeps_the_verdicts_already_bought(tmp_path):
    """The ceiling still propagates (ENG3-02), and the answers the model gave before it are kept:
    the memo is persisted in a `finally`, so the next finalize asks only what was never answered."""
    path = _store(tmp_path, 3)
    with pytest.raises(BudgetExceeded):
        _finalize(path, _Counting(exc=BudgetExceeded("LLM spend ceiling reached"), fail_at=2))
    assert len(_memo_keys(path)) == 1
    assert _finalize(path, _Counting()) == 2


def test_no_client_reads_and_writes_no_memo(tmp_path):
    """The offline path (no model) never merges paraphrases, so it has nothing to remember and must
    not create the file."""
    path = _store(tmp_path, 2)
    LessonMemory.consolidate_lessons_file(path)
    assert not (path.parent / LESSON_MERGE_VERDICTS).exists()


def test_compaction_keeps_the_live_set_and_a_concurrent_writers_rows(tmp_path, monkeypatch):
    """Past the bound, a COMPLETE pass rewrites the memo to what it can vouch for: the keys it
    consulted or minted, plus any key another finalize appended after this one read the file (not in
    its snapshot, so not its to judge). Stale keys — questions whose cluster has since changed — go."""
    monkeypatch.setattr(lessons_mod, "MERGE_VERDICTS_MAX_ROWS", 3)
    path = tmp_path / LESSON_MERGE_VERDICTS
    stale, concurrent, live_old, fresh = ("a" * 64, "b" * 64), "c" * 64, "d" * 64, "e" * 64
    path.write_text("".join(json.dumps({"v": 1, "key": key, "verdict": "declined"}) + "\n"
                            for key in (*stale, concurrent, live_old)), encoding="utf-8")
    memo = MergeVerdictMemo({*stale, live_old})          # what this pass read: not `concurrent`
    memo.live = {live_old, fresh}
    memo.fresh = [fresh]
    memo.complete = True
    LessonMemory.persist_merge_verdicts(tmp_path, memo)
    assert LessonMemory.merge_verdict_keys(path) == [concurrent, live_old, fresh]

    # An INCOMPLETE pass (a bucket raised, the walk stopped) cannot tell stale from unvisited, so it
    # only appends.
    memo = MergeVerdictMemo({concurrent, live_old, fresh})
    memo.fresh, memo.live = ["f" * 64], {"f" * 64}
    LessonMemory.persist_merge_verdicts(tmp_path, memo)
    assert LessonMemory.merge_verdict_keys(path) == [concurrent, live_old, fresh, "f" * 64]


def test_the_memo_reader_is_total_over_junk(tmp_path):
    """A torn line, a future row version, a malformed key: each reads as "not remembered", which
    costs a re-ask and can never suppress one."""
    path = tmp_path / LESSON_MERGE_VERDICTS
    good = "9" * 64
    path.write_text("\n".join([
        json.dumps({"v": 1, "key": good, "verdict": "declined"}),
        json.dumps({"v": 2, "key": "1" * 64, "verdict": "declined"}),
        json.dumps({"v": 1, "key": "NOT-A-DIGEST", "verdict": "declined"}),
        json.dumps({"v": 1, "key": "2" * 64, "verdict": "merged"}),
        json.dumps({"v": 1, "key": good, "verdict": "declined"}),
        '{"v": 1, "key": "33',
    ]), encoding="utf-8")
    assert LessonMemory.merge_verdict_keys(path) == [good]
    assert LessonMemory.merge_verdict_keys(tmp_path / "absent.jsonl") == []


def test_the_memo_rides_only_when_there_is_one():
    """`consolidate` is called with EXACTLY its historical arguments on the no-memo path — its test
    doubles (and any operator plug-in) pin that signature — and the key is the question, not the
    answer: the same question in another bucket is another key."""
    import inspect

    from looplab.search import hybrid_merge

    assert inspect.signature(hybrid_merge.consolidate).parameters["memo"].default is None
    request = hybrid_merge.merge_request(["a b", "a c"], kind="research lessons")
    assert MergeVerdictMemo.key(("t", "researcher"), request) != \
        MergeVerdictMemo.key(("u", "researcher"), request)
    assert MergeVerdictMemo.key(("t", "researcher"), request) == \
        MergeVerdictMemo.key(("t", "researcher"), hybrid_merge.merge_request(
            ["a b", "a c"], kind="research lessons"))
