"""The money summary must survive the log being APPENDED TO while it reads it.

THE DEFECT. `check_money.sh` parsed the meter ledger with

    rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]

— no `try`, unlike `check_probes.sh`, which wraps every one of its own parses. The ledger is an
append-only file written by a LIVE proxy, so its last line is regularly half-written, and one such
line raised `JSONDecodeError` out of the whole first block. The script is not under `set -e`, so
the second block printed as usual and the exit status came from IT: the money section simply was
not there, and the script said 0.

Reproduced 2026-08-30 over a three-line synthetic ledger whose last line is truncated: the
af13b4dd script prints a traceback, no ledger line at all, and exits 0.

WHAT THE FIX IS NOT. Silently skipping the bad line would be the other half of the same failure: a
line that did not parse is MONEY MISSING FROM THE TOTAL, and "$0.0000 over 3 h" with an unread tail
reads as a quiet gateway. The count is printed. And a block that dies for any other reason now
carries into the exit code, so an absent section can never again look like an empty one.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_MONEY = ROOT / "benchmarks" / "algotune" / "check_money.sh"


def _stand(tmp: Path, *, truncated: bool) -> Path:
    """A bench root holding one gateway ledger, optionally cut off mid-line as a live one is."""
    meter = tmp / "meter"
    meter.mkdir(parents=True)
    now = time.time()
    lines = [
        json.dumps({"ts": now, "arm": "B", "task": "svm", "status": 200, "cost": 0.5}),
        json.dumps({"ts": now, "arm": "B", "task": "svm", "status": 503, "cost": 0.0,
                    "error": "No available workers"}),
    ]
    text = "\n".join(lines) + "\n"
    if truncated:
        text += '{"ts": %f, "arm": "B", "task": "sv' % now
    (meter / "meter.jsonl").write_text(text, encoding="utf-8")
    return tmp


def _run(root: Path) -> subprocess.CompletedProcess:
    # PROXY_SRC_OVERRIDE points the SECOND block at nothing, so this test is about the first one.
    env = dict(os.environ, ROOT=str(root), PROXY_SRC_OVERRIDE=str(root / "no-such-proxy.py"))
    return subprocess.run(["bash", str(CHECK_MONEY), "3"], capture_output=True, text=True,
                          timeout=120, env=env)


def test_a_truncated_last_line_does_not_take_the_money_section_with_it(tmp_path):
    got = _run(_stand(tmp_path, truncated=True))
    assert "8801" in got.stdout, (
        "the money section is missing entirely — this is the defect", got.stdout, got.stderr)
    assert "$0.5000" in got.stdout, got.stdout
    assert "НЕУДАЧ 1" in got.stdout, got.stdout


def test_the_lines_it_could_not_read_are_counted_out_loud(tmp_path):
    """A skipped line is money absent from the total, and a total that does not say so is wrong in
    the direction that looks like calm."""
    got = _run(_stand(tmp_path, truncated=True))
    assert "НЕРАЗОБРАННЫХ СТРОК 1" in got.stdout, got.stdout


def test_a_clean_ledger_says_nothing_about_unreadable_lines(tmp_path):
    """The control: a warning that always prints is a warning nobody reads."""
    got = _run(_stand(tmp_path, truncated=False))
    assert "НЕРАЗОБРАННЫХ" not in got.stdout, got.stdout
    assert "$0.5000" in got.stdout, got.stdout


def test_a_block_that_dies_is_visible_in_the_exit_code(tmp_path):
    """The durable half. `check_money.sh` deliberately runs both blocks even when one fails -- the
    proxy-age check is worth having on its own -- but until now the status came from the LAST
    command, so a section that never printed and a section that printed nothing were the same 0."""
    root = _stand(tmp_path, truncated=False)
    ledger = root / "meter" / "meter.jsonl"
    ledger.chmod(0o000)
    try:
        got = _run(root)
    finally:
        ledger.chmod(0o644)
    if got.returncode == 0 and "8801" in got.stdout:           # running as root reads it anyway
        import pytest

        pytest.skip("this uid can read a mode-000 file, so the block did not fail")
    assert got.returncode != 0, (got.stdout, got.stderr)
