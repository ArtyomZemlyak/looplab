"""Guard (driven): a `kept` measured past the diff bound says so on the row."""
from looplab.engine.repair_verify import (
    _ATTRIBUTION_DIFF_LINES, _kept_row, repair_attribution)


def _file(n, rewrite_from=None):
    return "\n".join(
        (f"line {i}" if rewrite_from is None or i < rewrite_from else f"REWRITTEN {i}")
        for i in range(n))


def test_a_rewrite_past_the_bound_is_flagged_rather_than_reported_as_untouched():
    n = _ATTRIBUTION_DIFF_LINES + 500
    row = _kept_row("solver.py", _file(n), _file(n, rewrite_from=_ATTRIBUTION_DIFF_LINES))
    assert row["kept"] == 1.0, (
        "premise: the head-truncated comparison genuinely cannot see this rewrite")
    assert row.get("kept_truncated") is True, (
        "kept=1.0 alone says 'nothing of this file changed' beside a `changed` entry saying it did")


def test_a_file_inside_the_bound_carries_no_flag():
    row = _kept_row("small.py", _file(10), _file(10, rewrite_from=5))
    assert 0.0 < row["kept"] < 1.0 and "kept_truncated" not in row, (
        "a complete comparison must not be marked as a partial one")


def test_the_durable_attribution_row_carries_the_flag():
    n = _ATTRIBUTION_DIFF_LINES + 500
    out = repair_attribution(
        prev_files={"solver.py": _file(n)},
        files={"solver.py": _file(n, rewrite_from=_ATTRIBUTION_DIFF_LINES)},
        changed=["solver.py"], deleted=[], prev_code="", code="", prose=["rewrite solver.py"])
    wrote = [r for r in out["wrote"] if r.get("path") == "solver.py"]
    assert wrote and wrote[0].get("kept_truncated") is True, wrote
