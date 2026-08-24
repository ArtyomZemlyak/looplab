"""The attempt ledger must actually count, which means its directory must exist before it is used.

`campaign.sh` defines `OUT` from `$CAMPAIGN_OUT` and writes three things into it: the completion
markers, the per-task logs, and the append-only attempt ledger. Nothing created it. It existed by
luck on this box, and the luck ran out the moment a reset wiped it: every task's first
`next_attempt` write failed with "No such file or directory", the ledger stayed empty, and `N`
recomputed as 1 forever. Three attempts in a row all report `a1`.

That id is not decoration — the marker vocabulary (`ran_to_completion` / `stopped_after_start` /
`wall_cut` plus the attempt id) is how a re-run is told apart from the run it replaced. With every
attempt called `a1` there is no way to read which measurement a number came from.

The test extracts the real `next_attempt` out of `campaign.sh` rather than restating it, so it
tracks the shipped implementation instead of a copy that can drift away from it.
"""
import re
import subprocess
from pathlib import Path

CAMPAIGN = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "campaign.sh"


def _extract_next_attempt() -> str:
    src = CAMPAIGN.read_text(encoding="utf-8")
    m = re.search(r"^next_attempt\(\)\s*\{.*?^\}", src, re.S | re.M)
    assert m, "next_attempt() is no longer defined in campaign.sh — this test needs rewriting"
    return m.group(0)


def _three_attempts(out_dir: Path, make_dir: bool) -> list[str]:
    script = _extract_next_attempt() + '\n' + (
        'mkdir -p "$OUT"\n' if make_dir else "") + \
        'next_attempt B t; next_attempt B t; next_attempt B t\n'
    r = subprocess.run(["bash", "-c", script], env={"OUT": str(out_dir), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=60)
    return r.stdout.split()


def test_attempt_ids_count_up_when_the_directory_exists(tmp_path):
    assert _three_attempts(tmp_path / "camp", make_dir=True) == ["a1", "a2", "a3"]


def test_without_the_directory_every_attempt_is_a1(tmp_path):
    """The falsifier. If this ever stops reproducing, the test above proves nothing."""
    assert _three_attempts(tmp_path / "camp", make_dir=False) == ["a1", "a1", "a1"]


def test_campaign_creates_its_output_directory_before_writing_the_ledger():
    """And the shipped script must do the mkdir itself, ahead of every call site."""
    lines = CAMPAIGN.read_text(encoding="utf-8").splitlines()
    mk = next((i for i, l in enumerate(lines) if l.strip() == 'mkdir -p "$OUT"'), None)
    assert mk is not None, 'campaign.sh does not create "$OUT"'

    calls = [i for i, l in enumerate(lines)
             if "next_attempt " in l and not l.lstrip().startswith("#")
             and "next_attempt()" not in l]
    assert calls, "no next_attempt call sites found — this test needs rewriting"
    assert mk < min(calls), f'mkdir at line {mk+1} comes after a ledger write at line {min(calls)+1}'
