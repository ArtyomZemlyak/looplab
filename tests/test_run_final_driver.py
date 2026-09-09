"""`benchmarks/algotune/run_final.sh`: the campaign driver, in the repo, with three rules pinned.

docs/58 §58.7 item 9: the driver that ran the 2026-08-24 campaign was never committed and died with
`/var/tmp`, so a full campaign could not be repeated. §58.1 records what that driver did: it printed
bare clock times (a five-day date error), it launched arm A two to five times from a SECOND driver
with changed settings (`attempt=a3`/`a5`), and nothing recorded the configuration a task-arm ran
under. Each is a property here:

* no attempt loop -- no `for`/`while`/`until` anywhere in the script, and exactly one `campaign.sh`
  invocation per arm, A before B;
* every line it writes carries the ISO date, the campaign's own output included;
* it refuses a volatile root without `ALLOW_VOLATILE_ROOT=1`, refuses a reopen flag, and refuses to
  resume under a configuration that differs from the one it recorded.

The refusals need no bench stand (they fire before anything is touched). The end-to-end drive
copies the driver beside a STUB `campaign.sh` that records how it was called, because the real one
needs an AlgoTune checkout, a venv and a live endpoint.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest
from pathlib import Path

DRIVER = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "run_final.sh"
ISO = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] ")


def _is_disposable(path: str) -> bool:
    """True for a path this test run created and owns -- under the temp dir, and never the stand."""
    real = os.path.realpath(path)
    tmp = os.path.realpath(tempfile.gettempdir())
    inside_tmp = real == tmp or real.startswith(tmp + os.sep)
    return inside_tmp and "looplab-bench" not in real


def _run(*, env: dict, cwd: Path | None = None, script: Path = DRIVER) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("ALGOTUNE_", "CAMPAIGN_", "RETRY_", "ALLOW_", "TASKS", "BUDGET"))}
    # §384. THE REAL DRIVER MUST NEVER BE HANDED A REAL CHECKOUT. Every test here drives a REFUSAL,
    # and each refusal is a gate the driver passes THROUGH if its premise stops holding. One test
    # pinned `/var/tmp/looplab-bench/AlgoTune` as "a volatile path with no checkout"; the stand was
    # rebuilt at exactly that path, so the gate it was aiming at passed and the driver went on to
    # launch the live campaign, dying only on this 120 s timeout -- the orphaned-`campaign.sh`
    # hazard the standing list names, reached from the test suite. An absent path is asserted, not
    # assumed, because "absent" is the one thing about a fixture the filesystem can change on its own.
    #
    # The rule is not "the root must not exist" -- two tests below build a FAKE checkout under
    # `tmp_path` on purpose and lean on a LATER gate. It is "the root must not be a real one":
    # absent, or inside this run's temp area. The live stand is neither.
    root = env.get("ALGOTUNE_ROOT")
    if script == DRIVER and root and os.path.exists(root) and not _is_disposable(root):
        raise AssertionError(
            f"refusing to drive the real campaign: ALGOTUNE_ROOT={root} is an existing checkout "
            "outside this run's temp area. A test that means 'no checkout here' must name a path "
            "that is absent; one that needs a checkout must build a disposable one.")
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=120,
                          env={**base, **env}, cwd=str(cwd) if cwd else None)


# Under /var/tmp (so the volatility gate fires) and guaranteed absent (so the checkout gate fires).
VOLATILE_ABSENT = f"/var/tmp/looplab-bench-no-such-checkout-{os.getpid()}/AlgoTune"


def test_the_driver_is_valid_shell():
    done = subprocess.run(["bash", "-n", str(DRIVER)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_driver_has_no_attempt_loop():
    """No loop of any kind: a timestamping `while read` is one `continue` away from a retry loop at
    the next merge, so the property is the absence of the keyword, not of a particular loop."""
    src = DRIVER.read_text(encoding="utf-8")
    loops = [ln for ln in src.splitlines() if re.match(r"^\s*(for|while|until)\b", ln)]
    assert loops == [], f"run_final.sh contains a loop:\n" + "\n".join(loops)
    # and exactly one campaign per arm, A before B
    calls = re.findall(r'^ARM=([AB]) .*bash "\$HERE/campaign\.sh"', src, re.M)
    assert calls == ["A", "B"], calls


def test_a_volatile_algotune_root_is_refused_without_the_flag(tmp_path):
    got = _run(env={"ALGOTUNE_ROOT": VOLATILE_ABSENT, "CAMPAIGN_OUT": str(tmp_path)})
    assert got.returncode == 2, got.stdout + got.stderr
    assert f"ALGOTUNE_ROOT={VOLATILE_ABSENT} is under /var/tmp" in got.stderr
    assert "ALLOW_VOLATILE_ROOT=1" in got.stderr
    assert all(ISO.match(ln) for ln in got.stderr.splitlines()), got.stderr


def test_the_flag_admits_a_volatile_root_and_the_next_gate_still_stands(tmp_path):
    got = _run(env={"ALGOTUNE_ROOT": VOLATILE_ABSENT, "CAMPAIGN_OUT": str(tmp_path),
                    "ALLOW_VOLATILE_ROOT": "1"})
    assert got.returncode == 2
    assert "under /var/tmp" not in got.stderr, got.stderr
    assert "no AlgoTune checkout" in got.stderr, got.stderr


def test_a_volatile_campaign_out_is_refused_too(tmp_path):
    """The record is what died, and the record is CAMPAIGN_OUT: markers, ledgers, this log."""
    at = tmp_path / "AlgoTune"
    (at / "AlgoTuner").mkdir(parents=True)
    got = _run(env={"ALGOTUNE_ROOT": str(at), "CAMPAIGN_OUT": "/var/tmp/looplab-bench/campaign"})
    assert got.returncode == 2
    assert "CAMPAIGN_OUT=/var/tmp/looplab-bench/campaign is under /var/tmp" in got.stderr


def test_a_reopen_flag_is_refused_as_a_second_attempt(tmp_path):
    at = tmp_path / "AlgoTune"
    (at / "AlgoTuner").mkdir(parents=True)
    for flag in ("RETRY_WALL_CUT", "RETRY_IMMEDIATE_EXIT"):
        got = _run(env={"ALGOTUNE_ROOT": str(at), "CAMPAIGN_OUT": str(tmp_path / "out"), flag: "1"})
        assert got.returncode == 2, got.stderr
        assert "ONE attempt" in got.stderr, got.stderr
        assert not (tmp_path / "out" / "run_final.log").exists()


# ------------------------------------------------------------------------------------------------
# end to end, over a stub campaign
# ------------------------------------------------------------------------------------------------

_STUB = r'''#!/bin/bash
# Records how the driver called it, the way campaign.sh's own ledgers would.
mkdir -p "$CAMPAIGN_OUT"
echo "arm $ARM tasks=$TASKS budget=$BUDGET_USD" >> "$CAMPAIGN_OUT/calls.txt"
for T in $TASKS; do
  N=$(( $(wc -l < "$CAMPAIGN_OUT/$ARM-$T.attempts" 2>/dev/null || echo 0) + 1 ))
  echo "a$N started=now" >> "$CAMPAIGN_OUT/$ARM-$T.attempts"
done
echo "===== arm $ARM COMPLETE (stub) ====="
exit "${STUB_RC_${ARM}:-0}"
'''


def _stand(tmp_path: Path) -> tuple[Path, Path, dict]:
    """A copy of the driver beside a stub campaign.sh, and the env that points it at a stand."""
    bench = tmp_path / "repo" / "benchmarks" / "algotune"
    bench.mkdir(parents=True)
    shutil.copy(DRIVER, bench / "run_final.sh")
    # bash cannot expand `${STUB_RC_${ARM}}`; the stub reads two plain variables instead.
    (bench / "campaign.sh").write_text(
        _STUB.replace('exit "${STUB_RC_${ARM}:-0}"',
                      'if [ "$ARM" = A ]; then exit "${STUB_RC_A:-0}"; else exit "${STUB_RC_B:-0}"; fi'),
        encoding="utf-8")
    at = tmp_path / "AlgoTune"
    (at / "AlgoTuner").mkdir(parents=True)
    out = tmp_path / "campaign"
    env = {"ALGOTUNE_ROOT": str(at), "CAMPAIGN_OUT": str(out), "TASKS": "svm pagerank",
           "BUDGET_USD": "1.00", "SNAPSHOT": "0"}
    return bench / "run_final.sh", out, env


def test_both_arms_run_once_in_order_and_every_line_is_dated(tmp_path):
    script, out, env = _stand(tmp_path)
    got = _run(env=env, script=script)
    assert got.returncode == 0, got.stdout + got.stderr
    lines = [ln for ln in got.stdout.splitlines() if ln]
    assert lines and all(ISO.match(ln) for ln in lines), \
        "an undated line:\n" + "\n".join(ln for ln in lines if not ISO.match(ln))
    # the campaign's own output is dated as it passes through, not only the driver's
    assert any("arm A COMPLETE (stub)" in ln and ISO.match(ln) for ln in lines), got.stdout
    calls = (out / "calls.txt").read_text().splitlines()
    assert calls == ["arm A tasks=svm pagerank budget=1.00", "arm B tasks=svm pagerank budget=1.00"]
    # one attempt per task-arm, and the ledgers say so
    for arm in "AB":
        for task in ("svm", "pagerank"):
            assert (out / f"{arm}-{task}.attempts").read_text().count("\n") == 1
    assert "MORE THAN ONE attempt" not in got.stdout
    assert "FINAL CAMPAIGN COMPLETE" in got.stdout
    assert (out / "run_final.log").read_text() == got.stdout, "the log is not what was printed"
    assert (out / "run_final.CONFIGURATION").exists()


def test_a_resume_under_a_changed_configuration_is_refused(tmp_path):
    script, out, env = _stand(tmp_path)
    assert _run(env=env, script=script).returncode == 0
    changed = _run(env={**env, "ALGOTUNE_LLM_TIMEOUT_S": "1900"}, script=script)
    assert changed.returncode == 2, changed.stdout
    assert "DIFFERENT configuration" in changed.stdout
    diff = (out / "run_final.CONFIGURATION.diff").read_text()
    assert "+ALGOTUNE_LLM_TIMEOUT_S=1900" in diff, diff
    # no third attempt was made on either arm
    assert (out / "A-svm.attempts").read_text().count("\n") == 1
    # the same configuration resumes (and, over the stub, re-runs -- which the ledger then shows)
    again = _run(env=env, script=script)
    assert again.returncode == 0, again.stdout
    assert "configuration unchanged" in again.stdout
    assert "MORE THAN ONE attempt" in again.stdout and "A-svm.attempts" in again.stdout


def test_an_arm_that_exits_non_zero_makes_the_campaign_unfinished_not_complete(tmp_path):
    script, out, env = _stand(tmp_path)
    got = _run(env={**env, "STUB_RC_B": "3"}, script=script)
    assert got.returncode == 3, got.stdout
    assert "FINAL CAMPAIGN UNFINISHED" in got.stdout and "arm B rc=3" in got.stdout
    assert "FINAL CAMPAIGN COMPLETE" not in got.stdout
    assert "summarise with" not in got.stdout


def test_the_harness_refuses_to_hand_the_real_driver_a_real_checkout(tmp_path):
    """§384's guard, driven. Without it a fixture path that quietly starts existing turns a refusal
    test into a live campaign launch -- and the only thing that ended it was a 120 s timeout."""
    existing = tmp_path / "AlgoTune"
    (existing / "AlgoTuner").mkdir(parents=True)
    # `tmp_path` IS disposable, so the guard must not fire on it -- the two refusal tests below
    # hand the driver exactly such a tree. What it fires on is a checkout it does not own.
    assert _is_disposable(str(existing)), existing
    with pytest.raises(AssertionError, match="refusing to drive the real campaign"):
        _run(env={"ALGOTUNE_ROOT": "/var/tmp/looplab-bench/AlgoTune",
                  "CAMPAIGN_OUT": str(tmp_path / "out")})
    assert not _is_disposable("/var/tmp/looplab-bench/AlgoTune")
    # The STUB is exactly what may be handed one: that is what the end-to-end drive does.
    stub = tmp_path / "run_final.sh"
    stub.write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
    assert _run(env={"ALGOTUNE_ROOT": "/var/tmp/looplab-bench/AlgoTune"},
                script=stub).returncode == 7


def test_the_absent_root_this_file_leans_on_is_really_absent():
    """The premise, asserted rather than assumed -- it is what stopped holding."""
    assert VOLATILE_ABSENT.startswith("/var/tmp/"), VOLATILE_ABSENT
    assert not os.path.exists(VOLATILE_ABSENT), VOLATILE_ABSENT


@pytest.mark.parametrize("path,disposable", [
    (os.path.join(tempfile.gettempdir(), "pytest-of-x", "AlgoTune"), True),
    ("/var/tmp/looplab-bench/AlgoTune", False),                    # the stand, on this box
    (os.path.join(tempfile.gettempdir(), "looplab-bench", "AlgoTune"), False),  # TMPDIR=/var/tmp box
    ("/opt/AlgoTune", False),                                      # any checkout this run did not make
    (tempfile.gettempdir(), True),
])
def test_what_counts_as_a_disposable_checkout(path, disposable):
    """Both clauses, separately. Mutating either one away left the file GREEN: on THIS box the stand
    is outside the temp dir, so the name clause never fired, and no case exercised a real checkout
    outside the temp dir at all. A rule tested only through the paths that happen to exist here is
    tested by coincidence."""
    assert _is_disposable(path) is disposable, path


def test_a_symlink_under_tmp_does_not_launder_the_stand(tmp_path):
    """The name and the destination are different facts. A link in the temp area whose target is the
    live checkout reads as disposable by its path and is not; dropping the `realpath` left every
    other case green, because no fixture had ever pointed one thing at another."""
    stand = "/var/tmp/looplab-bench/AlgoTune"
    if not os.path.isdir(stand):
        pytest.skip("no stand on this box to link at")
    link = tmp_path / "AlgoTune"
    link.symlink_to(stand)
    assert _is_disposable(str(link)) is False, link
    with pytest.raises(AssertionError, match="refusing to drive the real campaign"):
        _run(env={"ALGOTUNE_ROOT": str(link), "CAMPAIGN_OUT": str(tmp_path / "out")})
