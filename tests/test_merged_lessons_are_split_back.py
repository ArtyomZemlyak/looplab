"""A row the fixed parser would never have written is repaired, not carried.

`engine/memory.py::_INLINE_PAIR` splits an LLM's numbered verdicts into separate lessons; before
6f6a0a1c it missed them when they arrived on ONE line, and a distillation glued three verdicts into
a single 861-character statement in the shared store. The parser is fixed and the store is
append-only, so the row stayed -- and `test_lesson_prior_render_budget.py` has failed on it on every
sweep since 2026-08-30, because that statement cannot be rendered whole.

The repair imports the SHIPPED regex rather than re-spelling it: a tool that splits by its own rule
produces rows the parser would not have.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmarks.repair_merged_lessons import repair, split_statement  # noqa: E402

MERGED = ("A hardcoded pin that overrides a declared hyperparameter must be removed so the intended "
          "schedule runs. P2  The eval had to load a LoRA-adapted checkpoint the standard pipeline "
          "could not consume. P3  Swapping the objective at the same footprint regressed recall@100.")


def _row(statement: str, **kw) -> str:
    row = {"statement": statement, "task_id": "t", "run_id": "r", "kind": "repo",
           "fingerprint": ["alpha", "beta"], "evidence": [0, 0],
           "evidence_sig": {"0": "v2:a=0"}, "confidence": 0.65}
    row.update(kw)
    return json.dumps(row, ensure_ascii=False)


def test_three_verdicts_on_one_line_become_three_rows():
    new, report = repair([_row(MERGED)])
    assert len(new) == 3, f"{len(new)} rows out of one merged statement: {new}"
    assert [i for i, _ in report] == [0]
    stmts = [json.loads(r)["statement"] for r in new]
    assert all(len(s) < 400 for s in stmts), [len(s) for s in stmts]
    assert stmts[1].startswith("P2") and stmts[2].startswith("P3"), stmts


def test_every_other_field_is_carried_unchanged():
    """`fingerprint` is a token list of the TASK, not of the statement, and `evidence` describes the
    run all three verdicts came from -- so the parts are siblings, not new lessons."""
    new, _ = repair([_row(MERGED, role="developer", direction="max")])
    rows = [json.loads(r) for r in new]
    for row in rows:
        assert row["fingerprint"] == ["alpha", "beta"]
        assert row["evidence_sig"] == {"0": "v2:a=0"}
        assert row["role"] == "developer" and row["direction"] == "max"
    assert len({r["statement"] for r in rows}) == 3


def test_an_ordinary_lesson_is_left_exactly_as_it_was():
    """MUTATION GUARD: a repair that splits on any `P<n>`-looking text would cut this one too."""
    plain = _row("Cache the baseline: P95 latency fell from 40 ms to 9 ms after the change.")
    new, report = repair([plain])
    assert new == [plain] and report == []


def test_a_torn_line_is_not_this_tools_business():
    lines = ['{"statement": "', _row(MERGED)]
    new, report = repair(lines)
    assert new[0] == '{"statement": "'
    assert len(new) == 4 and [i for i, _ in report] == [1]


def test_the_split_is_the_parsers_own():
    assert len(split_statement(MERGED)) == 3
    assert split_statement("one verdict only.") == ["one verdict only."]
