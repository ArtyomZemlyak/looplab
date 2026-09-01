"""`grep -c '^FAILED'` on a running pytest log always returns 0, and I reported it as a measurement.

Measured 2026-09-01 on a 754-line log of 13,309 tests: the three `FAILED` lines sat at line 752 --
pytest prints them only in the short summary, at the end. The failures themselves had gone past as
`F` characters in the progress rows at 3 %, 46 % and 74 %. I reported "failures so far: 0" three
times off that grep, twice when two failures had already happened. The instrument was blind by
construction and looked exactly like a reading.

The reader counts `F`/`E` only inside PROGRESS rows -- the ones ending in `[ NN%]` -- because the
word FAILED in the summary and any `E` in a traceback would otherwise be counted as outcomes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "benchmarks" / "pytest_progress.sh"


def _row(chars: str, pct: int) -> str:
    return f"{chars} [{pct:3d}%]"


def _run(log: Path):
    r = subprocess.run(["bash", str(TOOL), str(log)], capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout.strip()


def test_a_failure_mid_run_is_visible_before_the_summary_exists(tmp_path):
    """The whole point: no `FAILED` line has been written yet, and the count must still be right."""
    log = tmp_path / "running.log"
    log.write_text("\n".join([
        "platform linux -- Python 3.12.11",
        _row("." * 40 + "F" + "." * 30, 3),
        _row("." * 71, 6),
    ]) + "\n")
    rc, out = _run(log)
    assert "failed=1" in out, out
    assert rc == 1, "a log with a failure must exit non-zero"
    assert "FAILED" not in log.read_text(), "the fixture must not contain a summary line"


def test_the_word_FAILED_in_the_summary_is_not_counted_as_outcomes(tmp_path):
    """`FAILED` carries an F and an A and an I; a naive character count over the whole file would
    read the summary as more failures than there were."""
    log = tmp_path / "done.log"
    log.write_text("\n".join([
        _row("." * 40 + "F" + "." * 30, 100),
        "=== short test summary info ===",
        "FAILED tests/test_a.py::test_x - AssertionError",
    ]) + "\n")
    rc, out = _run(log)
    assert "failed=1" in out, out


def test_an_E_in_a_traceback_is_not_counted(tmp_path):
    """pytest prefixes traceback lines with `E   `, and an error outcome is also `E`. Only the
    progress rows may contribute."""
    log = tmp_path / "err.log"
    log.write_text("\n".join([
        _row("." * 20, 50),
        "E       AssertionError: something",
        "E       assert 1 == 2",
        _row("." * 20, 100),
    ]) + "\n")
    rc, out = _run(log)
    assert "errors=0" in out and "failed=0" in out, out
    assert rc == 0


def test_a_clean_run_reports_the_percentage_it_reached(tmp_path):
    log = tmp_path / "part.log"
    log.write_text("\n".join([_row("." * 30, 7), _row("." * 30 + "s" * 4, 14)]) + "\n")
    rc, out = _run(log)
    assert out.startswith("14%"), out
    assert "passed=60" in out and "skipped=4" in out, out
    assert rc == 0


def test_a_missing_log_is_an_error_and_not_a_clean_zero(tmp_path):
    """"No failures" and "no log" must not look the same; that is the whole family of defect this
    tool exists inside."""
    rc, out = _run(tmp_path / "nope.log")
    assert rc == 2, (rc, out)
