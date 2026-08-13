"""A kill the ENGINE issued must never be diagnosed from the exit code the engine itself caused.

THE INCIDENT, and every test here is calibrated against it. On `runs/rubertlite-dr-unified-v6`
node 5 the DIVERGE watchdog did its job perfectly: the training reported

    {'loss': 1.2217858750118953e+25, 'grad_norm': nan, ...}
    {'loss': 0.0, 'grad_norm': nan, ...}
    ‼ LOOPLAB health-check: training DIVERGED — non-finite loss/grad_norm reported repeatedly

and the stage was tree-killed at step 50 instead of burning four hours. Then the classifier read the
result: exit `-9`, no `Traceback` in stderr — which is byte-for-byte the signature
`triage.py::_failure_reason` uses to recognise a kernel OOM-kill. So `reason` came out `oom`, the
repair directive said *"the script was KILLED by the out-of-memory killer … return a script that fits
in LESS memory"*, and the Developer followed it exactly, three times:

    attempt 1  batch_size 8192 -> 2048, grad accum 2 -> 8      (~3 GPU-minutes, diverged again)
    attempt 2  batch_size 2048 -> 512,  grad accum 8 -> 32     (~3 GPU-minutes, diverged again)
    attempt 3  batch_size  512 -> 256,  grad accum 32 -> 64    (~5 GPU-minutes, diverged again)

Its own rationale on the last round said "(kind=oom) … a memory ceiling, not a flawed idea" while the
health-check banner naming the real cause was in the very error text it was quoting. Batch size was
never the problem; the R-Drop symmetric-KL term at alpha=0.5 / lr=1e-3 was.

The fix is not a better heuristic over the exit code — it is refusing to guess at all when the engine
already KNOWS. Both watchdogs record an authenticated out-of-band verdict (`run_argv`'s `signals`),
and the classifier now reads that first. The stderr sentinels are deliberately NOT what it reads:
they are mixed into the candidate's own output and are forgeable, the same reason
`command_eval._salvageable_stall` reads the flag rather than the text.
"""
from __future__ import annotations

import sys
import time

import pytest

from looplab.core.models import FAILURE_REASONS
from looplab.engine.crash_repair import CrashRepairMixin
from looplab.engine.triage import _failure_reason, _rule_triage
from looplab.runtime.command_eval import run_command_eval
from looplab.runtime.sandbox import RunResult

_M = {"kind": "stdout_json", "key": "metric"}

# A stage that diverges and would then run for two minutes. Same shape as v6 node 5: real records on
# stdout, non-finite from the start, no traceback anywhere, and the process alive when it is killed.
_DIVERGING = ("import sys, time\n"
              "for i in range(1000):\n"
              "    print(\"{'loss': 0.0, 'grad_norm': nan, 'epoch': %.2f}\" % (i * 0.01))\n"
              "    sys.stdout.flush(); time.sleep(0.02)\n"
              "time.sleep(120)\n")


def _stage(prog):
    return [{"name": "train", "command": [sys.executable, "-c", prog], "timeout": 60}]


# --------------------------------------------------------------------------------------------
# The property, driven end to end through a real killed stage
# --------------------------------------------------------------------------------------------

def test_a_diverged_training_is_classified_diverged_and_not_oom():
    """The whole chain: a real diverging stage, really watchdog-killed, really classified.

    Note what this asserts BESIDES the reason — `exit_code in (-9, 137)` and no `Traceback`. Those
    two are the OOM heuristic's exact premise, so their presence is what makes the test meaningful:
    the classifier is being handed the signature it used to fall for, and must not fall for it."""
    t0 = time.time()
    res = run_command_eval([sys.executable, "-c", "print(1)"], "/tmp", 60, _M,
                           stages=_stage(_DIVERGING))
    assert time.time() - t0 < 40, "the watchdog did not kill the stage early"

    assert res.exit_code in (-9, 137), "not a tree-kill; this test no longer covers the OOM premise"
    assert "Traceback" not in (res.stderr or ""), "a traceback would take the OOM branch out of play"
    assert not res.timed_out, "a divergence kill is not a deadline timeout"

    assert res.diverged is True, "the authenticated watchdog verdict did not reach RunResult"
    assert _failure_reason(res) == "diverged"


def test_a_stalled_stage_with_no_metric_is_classified_stalled_and_not_oom():
    """The stall watchdog has the same signature and the opposite fix.

    `stalled` already had a job — salvaging a metric printed before a hang — but a stage that hangs
    with NOTHING printed has no metric to salvage, so it fell through to the same OOM branch. "Reduce
    the batch size" does not unblock a deadlocked collective."""
    prog = "import time\ntime.sleep(120)\n"      # alive, silent, nothing printed
    res = run_command_eval([sys.executable, "-c", "print(1)"], "/tmp", 60, _M,
                           stages=[{"name": "train", "command": [sys.executable, "-c", prog],
                                    "timeout": 60}], stall_timeout=2)

    assert res.exit_code in (-9, 137) and "Traceback" not in (res.stderr or "")
    assert not res.timed_out
    assert res.stalled is True and res.diverged is False
    assert _failure_reason(res) == "stalled"


def test_divergence_outranks_a_stall_in_the_classifier_too():
    """Both watchdogs fire when a run logs non-finite records and then goes silent. The salvage gate
    already resolves that pair in favour of DIVERGED (`_salvageable_stall`); the classifier must
    resolve it the same way, or the node's terminal and its repair directive disagree about which
    watchdog fired."""
    both = RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=False,
                     stalled=True, diverged=True)
    assert _failure_reason(both) == "diverged"


def test_a_real_kernel_oom_is_still_an_oom():
    """The control. Nothing above may be bought by making the OOM branch unreachable — a genuine
    cgroup kill has neither watchdog flag set and must keep its memory-reduction directive."""
    killed = RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=False)
    assert _failure_reason(killed) == "oom"

    with_traceback = RunResult(exit_code=1, stdout="", stderr="Traceback (most recent call last):",
                               metric=None, timed_out=False)
    assert _failure_reason(with_traceback) == "crash"


def test_a_timeout_still_outranks_both_watchdog_flags():
    """`timed_out` is checked before the watchdogs and stays there: a deadline kill is a deadline
    kill even if a stall watchdog also tripped on the way down, and "reduce compute to fit the
    budget" is the correct directive for it."""
    res = RunResult(exit_code=-9, stdout="", stderr="", metric=None, timed_out=True,
                    stalled=True, diverged=True)
    assert _failure_reason(res) == "timeout"


# --------------------------------------------------------------------------------------------
# What the classification is FOR: the directive the Developer is handed
# --------------------------------------------------------------------------------------------

class _Repairer(CrashRepairMixin):
    """The mixin under its own directive rule, with only the attributes that rule reads."""
    def __init__(self):
        self._deep_repair = False
        self._repo_spec = None
        self._eval_parallel = 1
        self._gpu_ids = None


@pytest.mark.parametrize("reason", ["diverged", "stalled"])
def test_the_directive_tells_the_developer_NOT_to_cut_memory(reason):
    """The negative instruction is the load-bearing one, because the model's prior is strong: three
    rounds on v6 node 5 reached for the memory playbook with the health-check banner in front of it.

    Asserted as PROPERTIES of the text, not as a pinned literal — the wording is a prompt contract
    and will be tuned, but "it says which watchdog fired" and "it says this is not an OOM" are what
    the reason exists to deliver."""
    text = _Repairer()._repair_error_context(reason, "[failed stage: train]\n<the log tail>")

    assert f"[failure kind: {reason}]" in text, "the kind must be named for the model to act on it"
    assert "<the log tail>" in text, "the node's own error text must survive into the directive"
    assert "NOT an out-of-memory kill" in text
    assert "NOT a timeout" in text


def test_the_two_directives_do_not_prescribe_the_same_fix():
    """A shared 'the engine killed it' directive would be a regression in a different direction: a
    diverging objective and a deadlocked collective have nothing in common but their exit code."""
    r = _Repairer()
    diverged = r._repair_error_context("diverged", "e")
    stalled = r._repair_error_context("stalled", "e")
    assert diverged != stalled
    assert "grad" in diverged.lower() and "learning rate" in diverged.lower()
    assert "heartbeat" in stalled.lower()
    assert "heartbeat" not in diverged.lower()


# --------------------------------------------------------------------------------------------
# The registry, and the no-judge path
# --------------------------------------------------------------------------------------------

def test_both_verdicts_are_selectable_failure_reasons():
    """`inline_repair_reasons` can only select what the registry names. A classifier that returns a
    reason no setting can select is the exact drift `tests/test_inline_repair_reason_coverage.py`
    exists for — this pins the two new members by name so a well-meaning cleanup that folds them
    back into `oom` has to argue with a test."""
    assert "diverged" in FAILURE_REASONS and "stalled" in FAILURE_REASONS


@pytest.mark.parametrize("reason", ["diverged", "stalled"])
def test_the_no_judge_path_repairs_a_watchdog_kill(reason):
    """With no triage model wired, `_rule_triage` is the whole decision. A watchdog kill is a
    resource/health fact, not evidence the idea is wrong, so it belongs with timeout and oom on the
    repair side — and its rationale must not be the OOM one."""
    verdict = _rule_triage(reason, "‼ LOOPLAB health-check", attempt=1, max_attempts=3)
    assert verdict["action"] == "repair"
    assert ("health-check" in verdict["rationale"] or "stall watchdog" in verdict["rationale"]), (
        "the rationale must name which watchdog fired, not just that something was killed")
    assert "reduce memory" not in verdict["rationale"]

    exhausted = _rule_triage(reason, "‼ LOOPLAB health-check", attempt=9, max_attempts=3)
    assert exhausted["action"] == "abandon", "the attempt bound still binds"
