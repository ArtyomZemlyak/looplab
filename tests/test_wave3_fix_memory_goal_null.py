"""Regression: JsonlCaseLibrary.search must not crash on a retained `goal: null` row.

`valid_case_record` deliberately admits a case whose `goal` key is present with value None
(its guard is `if value is not None and not isinstance(value, str)`, so None passes). Before the
fix, `search` concatenated `c.get("goal", "") + " "`, which returns None for such a row and raised
`TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'` — outside the try that only
guards the `limit` computation. The fix uses `c.get("goal") or ""` so None degrades to "".
"""
from looplab.engine.memory import JsonlCaseLibrary, valid_case_record


def test_valid_case_record_admits_goal_null():
    # Precondition the bug relies on: a goal:null row is genuinely retained, not quarantined.
    assert valid_case_record({"task_id": "t", "goal": None, "direction": "min", "metric": 1.0})


def test_search_does_not_crash_on_goal_null_row(tmp_path):
    lib = JsonlCaseLibrary(tmp_path / "cases.jsonl")
    assert lib.add({"task_id": "null_goal_task", "goal": None, "direction": "min",
                    "metric": 1.0, "params": {}}) is True
    assert len(lib.all()) == 1  # the row was retained, so search will scan it

    # Must not raise TypeError; the None goal contributes no tokens, but the row is still returned.
    results = lib.search("anything")
    assert results and results[0]["task_id"] == "null_goal_task"

    # A valid-string goal still matches on its own tokens (unchanged behaviour).
    assert lib.add({"task_id": "poly", "goal": "polynomial degree selection", "direction": "min",
                    "metric": 0.5, "params": {}}) is True
    hits = lib.search("polynomial degree")
    assert hits[0]["task_id"] == "poly"
