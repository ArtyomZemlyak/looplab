"""An allocator that RAISES is still an out-of-memory failure, even though it exits like a crash.

THE INCIDENT. `runs/e5small-dr-unified-v3` ran three nodes and stopped on the systemic-failure rule
having never produced a metric. All three died the same way — the node's OWN process holding ~139 of
the card's 139.80 GiB:

    torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.12 GiB. GPU 0 has a total
    capacity of 139.80 GiB of which 713.00 MiB is free. Process 3011507 has 139.10 GiB memory in use.

and all three `node_failed` rows read `reason: crash`. `triage.py::_failure_reason` recognised an OOM
by the KERNEL kill signature only — `exit_code in (-9, 137) and "Traceback" not in stderr` — which is
the cgroup OOM-killer, and a `torch.OutOfMemoryError` is its exact opposite: the process RAISES,
prints a full traceback, and exits 1. Not one conjunct can match, so the most common way a GPU eval
dies could never be classified as what it was.

The engine HAD the right directive the whole time. `crash_repair.py`'s `oom` branch says *"return a
corrected script that fits in LESS memory: a smaller batch size, ... lower precision"*; the Developer
was handed the `crash` branch instead and two of the repairs it bought returned byte-identical files,
which is what "I don't know what to change" looks like. The predecessor run `-v2` eventually found
the answer by hand, walking a batch 8192 -> 2048 -> 1024 -> 512 across four attempts and 11,735 s.

THE MIRROR of this bug is `tests/test_watchdog_kill_is_not_an_oom.py` and the two are worth reading
together, because the resolutions point in OPPOSITE directions. There, a kill the ENGINE issued was
being diagnosed from the exit code the engine itself caused, and the fix was to stop guessing at the
text and read the authenticated out-of-band `signals` flag. Here there IS no flag and there cannot
be one — the engine did not cause this exit; the candidate's own process raised and died, and nothing
out of band observed it.

**WHO ANSWERS IT MOVED ON 2026-08-20, AND THE PROPERTY DID NOT.** The first fix was a MARKER LIST
(`_is_torch_oom`) scanning `res.stderr` for `OutOfMemoryError` / `CUDA out of memory`, and it
resolved all 26 misclassified rows in `runs/`. That win was real and it is not the reason the marker
is gone. It is gone because it was TEXT WITH THE LAST WORD: nothing downstream re-checks a `reason`,
so a list is exactly as good as its own spelling, and a host `MemoryError`, an OOM re-raised inside
another library's exception, and the torchrun `Root Cause ... exitcode: 1` block that NINE of those
26 rows ARE, are each another literal and another incident. `engine/failure_diagnosis.py` states the
general rule and this codebase's own precedent for it (`runtime/deps.py`: text NOMINATES,
`is_present` DECIDES).

So `oom` is now ANSWER-ONLY — no engine producer can name it at all — and this file's job changed
with it. What it drives is (a) that the ENGINE's honest residual on the real corpus is `crash`,
(b) that a diagnosis of `oom` reaches the memory directive that exists one branch away, and (c) that
every bound the marker's safety argument rested on is still standing, now guarding a model's answer
instead of a regex's: every consumer of the `oom` literal routes a REPAIR, `oom` is absent from
`metric_salvage.NEVER_SALVAGED_REASONS`, and `_rule_triage` bounds it by the same `max_attempts` as
a `crash` — so a WRONG `oom` costs one repair round aimed at memory and can never admit a metric,
move a champion or change a selection.

The corpus in `tests/fixtures/torch_oom_stderr_corpus.json` is the REAL stderr of those three nodes,
copied out of the read-only run directory. It carries all three shapes that matter: two DDP failures
(rank tracebacks under a `torch.distributed.elastic` `ChildFailedError` wrapper) and one plain
single-process failure — so a fix that only handles the shape somebody imagined goes red here.

WHERE THE CLASSIFIER CAN SEE THIS, established rather than assumed. `_failure_reason` reads
`res.stderr`, which `sandbox.run_argv` clamps to the last `max_output_bytes` (64,000) of the stream.
torchrun does not redirect by default, so each rank's traceback is printed to the PARENT's stderr
with a `[rank0]:` prefix, ahead of the elastic wrapper's summary — and the marker is inside that
64,000-byte tail for all three real logs (`test_it_survives_the_tail_clamp...`). Under
`torchrun --redirects` it would not be, the parent would hold only the wrapper, and the honest answer
is `crash`; `test_an_elastic_wrapper_with_no_cause_stays_a_crash` pins that rather than guessing.

The REMAINING gap is a different one and is only PARTLY closed. What the Developer is HANDED is
still `res.stderr[-500:]` (`evaluate.py::_eval_failure_text`), and on a DDP failure those 500
characters are pure elastic wrapper: the durable `node_failed.error` on all three v3 nodes is 522
bytes ending `traceback : To enable traceback see: ...`, with no allocation size, no device total
and no `OutOfMemoryError` anywhere in it. What changed on 2026-08-20 is that the DIAGNOSTICIAN can
now go and get them — `read_log` over the whole stage log plus `RepoScoutTools` over the node's
workdir — so the numbers are REACHABLE where before they were not present anywhere in the loop. The
splice itself is unchanged, so the item stays open and its proof still holds; what moved is that a
remedy now exists for a judge that chooses to spend a turn on it.
MEASURED ON A LIVE OOM, 2026-08-22 (`runs/e5small-dr-unified-v4` node 4, train stage). The window
is not merely too small — it lands in PADDING, and the filler is not the DDP wrapper this item was
written about:

    torch.OutOfMemoryError          952 chars from EOF
    "Tried to allocate 2.25 GiB"    908 chars from EOF
    "total capacity of 139.80 GiB"  868 chars from EOF
    trailing whitespace run         329 chars   <- a tqdm bar's final render

Every one of those facts is present in the 24,680-byte stage log and NONE of them is inside the last
500 characters. 329 of that window is literal whitespace, so its effective reach is ~171 characters
of real text against evidence that begins at 952 — off by a factor of two even before the padding is
counted. A progress bar is NOT a new class of filler and is not the commoner one — the block above already
counted the triage corpus: of 122 stored tails, five are a launcher's opaque "Root Cause … exitcode:
1" and TWO are nothing but a progress bar. (I wrote "the likelier one" here first, inferring a
comparative frequency from a single instance while the corpus that refutes it sat forty lines up.)
What tonight's node adds is the DISTANCE, which no corpus header states: the marker is not merely
absent from the tail, it begins 952 characters from EOF against a 500-character window whose last
329 are padding. A fix needs a number to clear, and this is it.

    OPEN[oom-evidence-not-in-repair-text] a torch OOM's allocation numbers are still not PUSHED to
    the Developer: the repair text is a 500-char stderr tail that a DDP wrapper — or, measured
    above, a tqdm bar's 329-char pad — fills entirely, while the numbers sit 908+ chars from EOF.
    The diagnostician can now PULL them (`repair_log_tools`), which is a remedy and not a fix.
    proof:present:res.stderr[-500:]@looplab/engine/evaluate.py
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from looplab.core.models import FAILURE_REASONS
from looplab.engine.crash_repair import CrashRepairMixin
from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS
from looplab.engine.failure_diagnosis import (DIAGNOSED_FAILURE_REASONS, REASON_SOURCE_ENGINE,
                                              REASON_SOURCE_TRIAGE, diagnosed_failure_reason)
from looplab.engine.triage import _failure_reason, _rule_triage
from looplab.runtime.command_eval import run_command_eval
from looplab.runtime.sandbox import RunResult

_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "torch_oom_stderr_corpus.json").read_text())
_M = {"kind": "stdout_json", "key": "metric"}


def _res(stderr, exit_code=1, **kw):
    return RunResult(exit_code=exit_code, stdout="", stderr=stderr, metric=None,
                     timed_out=False, **kw)


# --------------------------------------------------------------------------------------------
# The property, against the real stderr of the three nodes that died
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("node", sorted(_CORPUS))
def test_the_real_v3_oom_stderr_is_a_crash_to_the_engine_and_an_oom_to_the_diagnostician(node):
    """Every node of the run this was found on, verbatim, through BOTH rungs.

    Note what is asserted besides the reasons — exit 1 and a `Traceback` PRESENT. Those two are the
    exact negation of the kernel-OOM premise, so their presence is what makes the test meaningful:
    this is the signature the original branch structurally could not match.

    `crash` from the engine is not a regression, it is the honest answer: the process exited
    non-zero and nothing out of band saw why. The engine no longer has ANY way to say `oom` — see
    `test_the_engine_can_no_longer_say_oom` below — and that is the change, not a side effect of
    it."""
    err = _CORPUS[node]
    assert "Traceback" in err, "no traceback: this fixture no longer covers the raised-OOM shape"
    assert "torch.OutOfMemoryError" in err

    res = _res(err)
    assert res.exit_code not in (-9, 137), "a SIGKILL would be the OTHER shape"
    assert _failure_reason(res) == "crash", "the engine's honest structural residual"

    diagnosed = {"action": "repair", "failure_kind": "oom",
                 "rationale": "torch.OutOfMemoryError, 139.10 GiB in use of 139.80 GiB"}
    assert diagnosed_failure_reason(_failure_reason(res), diagnosed) == ("oom",
                                                                        REASON_SOURCE_TRIAGE)


def test_the_engine_can_no_longer_say_oom():
    """The deleted rules, driven rather than asserted about source text. Both of `oom`'s producers
    were text: the KERNEL signature (exit -9/137 with no traceback) and the allocator MARKER list.
    Neither survives, so no `RunResult` at all classifies as `oom`."""
    for res in (_res("", exit_code=-9),                              # the kernel signature
                _res("", exit_code=137),
                _res(_CORPUS[sorted(_CORPUS)[0]]),                   # the marker list's own corpus
                _res("torch.OutOfMemoryError: CUDA out of memory")):
        assert _failure_reason(res) == "crash"
    assert "oom" in DIAGNOSED_FAILURE_REASONS, "…and it is the diagnostician's word now"


def test_both_ddp_and_single_process_shapes_are_in_the_corpus():
    """The corpus has to keep covering both, or a regression that only handles one reads green.

    v3 nodes 0 and 1 ran `accelerate launch --num_processes 2`, so their failure arrives as rank
    tracebacks under an elastic `ChildFailedError`; node 2 was repaired down to one process and
    died with a bare traceback under `subprocess.CalledProcessError`. Node 2 is also what disproves
    the run report's own theory that these were distributed-launch failures."""
    ddp = [k for k, v in _CORPUS.items() if "ChildFailedError" in v]
    plain = [k for k, v in _CORPUS.items() if "ChildFailedError" not in v]
    assert ddp and plain, f"corpus lost a shape: ddp={ddp} plain={plain}"
    for k in ddp:
        assert "[rank0]:" in _CORPUS[k]
    # Both shapes are the SAME question to the engine now — which is the point of the residual, and
    # is why the corpus is kept: the two shapes are what a diagnostician must tell apart, and a
    # regression that only handles one is invisible at this rung.
    for res in (_res(_CORPUS[ddp[0]]), _res(_CORPUS[plain[0]])):
        assert _failure_reason(res) == "crash"


def test_the_evidence_is_in_the_stream_but_not_in_the_slice_anyone_is_handed():
    """THE MEASUREMENT THAT DECIDES WHERE THIS QUESTION BELONGS, and it cuts both ways.

    `sandbox.run_argv` clamps each stream to `max_output_bytes` (64,000) as a TAIL, and the rank
    tracebacks print BEFORE the elastic wrapper's summary — so the allocator line IS inside the
    64,000 bytes for all three real logs. But `evaluate.py::_eval_failure_text` hands the judge
    `res.stderr[-500:]`, and on a DDP failure those 500 characters are pure elastic wrapper: the
    durable `node_failed.error` on all three v3 nodes is 522 bytes of `Root Cause ... traceback :
    To enable traceback see: ...` with no allocation, no size and no `OutOfMemoryError` in it.

    So a marker list reading the 64 KiB stream could resolve these and a judge reading the 500-char
    splice could not — which is exactly why the answer is neither. It is TOOLS: the diagnostician
    reads the stage log itself (`repair_log_tools`), unbounded by either slice, and cites what it
    found. The open item in this file's HEADER (slug `oom-evidence-not-in-repair-text`) is the
    residue — spelled without the bracket form on purpose, because a slug must be DECLARED
    exactly once and `tests/test_open_item_index.py` counts every occurrence of the key as a
    declaration."""
    for node, err in _CORPUS.items():
        assert "OutOfMemoryError" in err[-64_000:], (
            f"{node}: the evidence left the stream the engine captures at all")
        if "ChildFailedError" in err:
            assert "OutOfMemoryError" not in err[-500:], (
                f"{node}: the 500-char tail now carries the evidence — if that is a deliberate "
                "change to the tail rule, this test's premise moved")


# --------------------------------------------------------------------------------------------
# Driven end to end through a real killed stage
# --------------------------------------------------------------------------------------------

# The raised shape, reproduced without torch and without a GPU: a class of the right NAME carrying
# the allocator's real message, raised uncaught, exiting 1 with a full traceback. Needing a real
# 140 GiB allocation to test the classifier would make this untestable anywhere but this box.
_RAISING = (
    "class OutOfMemoryError(RuntimeError): pass\n"
    "raise OutOfMemoryError('CUDA out of memory. Tried to allocate 1.12 GiB. GPU 0 has a total "
    "capacity of 139.80 GiB of which 713.00 MiB is free.')\n"
)

_PLAIN_CRASH = "raise ValueError('the model config names a column that is not in the frame')\n"


def test_a_real_raised_oom_stage_is_a_crash_the_diagnostician_can_rename():
    """The whole chain: a real stage that really raises, really run, really classified — and then
    really re-decided. The point of driving it is that the two rungs meet on a real `RunResult`,
    not on a hand-built one."""
    res = run_command_eval([sys.executable, "-c", "print(1)"], "/tmp", 60, _M,
                           stages=[{"name": "train", "command": [sys.executable, "-c", _RAISING],
                                    "timeout": 60}])
    assert res.exit_code == 1 and "Traceback" in (res.stderr or "")
    assert not res.timed_out and not getattr(res, "stalled", False)
    assert _failure_reason(res) == "crash"
    verdict = {"action": "repair", "failure_kind": "oom", "rationale": "the allocator raised"}
    assert diagnosed_failure_reason(_failure_reason(res), verdict)[0] == "oom"


def test_a_real_ordinary_crash_is_still_a_crash():
    """The control that keeps the branch honest — a raised exception is not evidence of an OOM."""
    res = run_command_eval([sys.executable, "-c", "print(1)"], "/tmp", 60, _M,
                           stages=[{"name": "train", "command": [sys.executable, "-c", _PLAIN_CRASH],
                                    "timeout": 60}])
    assert res.exit_code == 1 and "Traceback" in (res.stderr or "")
    assert _failure_reason(res) == "crash"


# --------------------------------------------------------------------------------------------
# What must NOT move
# --------------------------------------------------------------------------------------------

def test_an_elastic_wrapper_with_no_cause_stays_a_crash():
    """The DDP case this deliberately does NOT claim, pinned so nobody "fixes" it.

    With `torchrun --redirects` the ranks' output goes to per-rank files and the parent keeps only
    the wrapper. A `ChildFailedError` says a child died and says NOTHING about why — treating it as
    an OOM would invent a diagnosis, and the wrapper is what every DDP failure of every kind looks
    like. `_failure_reason` is a pure function of `res` (no I/O, see the test below), so reaching
    into the rank log is not available to it and the honest answer is `crash`."""
    wrapper = ("[failed stage: train]\n"
               "torch.distributed.elastic.multiprocessing.errors.ChildFailedError:\n"
               "Root Cause (first observed failure):\n[0]:\n  time : 2026-08-19_17:41:46\n"
               "  rank : 0 (local_rank: 0)\n  exitcode : 1 (pid: 2799581)\n"
               "  error_file: <N/A>\n  traceback : To enable traceback see: "
               "https://pytorch.org/docs/stable/elastic/errors.html\n")
    assert _failure_reason(_res(wrapper)) == "crash"
    # And a DIAGNOSTICIAN handed only this may not invent one either — it is asked, it looks, and if
    # the rank log is gone the honest answer is still `crash`. What is different from the marker
    # rung is that it CAN look: `repair_log_tools` reaches the per-rank files `--redirects` wrote,
    # which is the one place the cause survives. That is the difference between "cannot see it" and
    # "must guess".
    honest = {"action": "repair", "failure_kind": "crash", "rationale": "the child died; no cause"}
    assert diagnosed_failure_reason(_failure_reason(_res(wrapper)), honest)[0] == "crash"


def test_the_watchdog_verdicts_still_outrank_the_text():
    """The mirror defect must not come back through this door.

    A diverging training that ALSO happened to print an OOM line on its way down is still a
    divergence: the engine issued that kill and recorded an authenticated flag, and the flag is read
    before any text. "Stabilise the numerics" and "cut the memory" are opposite directives."""
    err = "CUDA out of memory. Tried to allocate 1.12 GiB."
    assert _failure_reason(_res(err, exit_code=-9, diverged=True)) == "diverged"
    assert _failure_reason(_res(err, exit_code=-9, stalled=True)) == "stalled"
    assert _failure_reason(RunResult(exit_code=-9, stdout="", stderr=err, metric=None,
                                     timed_out=True)) == "timeout"
    # …AND THEY OUTRANK THE MODEL, which is the same door one rung up. A diagnostician handed a
    # diverged training that also printed an allocator line may well answer `oom`; it is never
    # asked, because all three are ENGINE-FINAL. This is `test_watchdog_kill_is_not_an_oom.py`'s
    # property restated against a verdict instead of a heuristic.
    said_oom = {"action": "repair", "failure_kind": "oom", "rationale": "it says out of memory"}
    for res in (_res(err, exit_code=-9, diverged=True), _res(err, exit_code=-9, stalled=True)):
        assert diagnosed_failure_reason(_failure_reason(res), said_oom)[1] == REASON_SOURCE_ENGINE


def test_a_kernel_oom_and_a_clean_run_are_unchanged():
    """Both ends of the behaviour, with the two that MOVED called out.

    The kernel OOM-kill is now `crash`: its signature was `exit -9 with no traceback`, which is
    byte-identical to what both watchdogs' tree-kills produce, so it was a conflation and not merely
    a guess — and the two watchdogs already read authenticated flags above it. The `setup failed:`
    prefix no longer decides anything either; `RunResult.setup_failed` does."""
    assert _failure_reason(_res("", exit_code=-9)) == "crash"
    assert _failure_reason(_res("Traceback (most recent call last):")) == "crash"
    assert _failure_reason(_res("setup failed:\nno module named torch")) == "crash", (
        "the candidate's own stderr may not claim the engine's setup step failed")
    assert _failure_reason(_res("setup failed:\nno module named torch",
                                setup_failed=True)) == "setup"
    assert _failure_reason(RunResult(exit_code=0, stdout="", stderr="", metric=None,
                                     timed_out=False)) == "no_metric"


def test_the_classifier_stays_pure_and_reads_only_engine_written_fields():
    """`triage.py`'s module docstring promises these helpers do no I/O, which is what makes them
    replay-safe — and it is also why the elastic-wrapper case above cannot be resolved HERE by
    reading the rank's log file. That is the diagnostician's job, and it is a different rung with a
    different budget.

    Re-pointed at `_failure_reason` itself (2026-08-20), which is strictly stronger than the old
    pin on the deleted `_is_torch_oom`: it also asserts the classifier reads no TEXT. Every
    attribute it touches on `res` is a field the ENGINE set, and `stderr`/`stdout` are absent from
    that set — which is the ownership split expressed as the classifier's own read surface."""
    import looplab.engine.triage as triage
    tree = ast.parse(Path(triage.__file__).read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_failure_reason")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not ({"open", "eval", "exec", "__import__"} & called), called
    # The read surface, by AST. `stderr` is the one that must never come back.
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    literals = {n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "stderr" not in attrs and "stdout" not in attrs, (
        f"the classifier is reading the candidate's own output again: {attrs}")
    assert not ({"startswith", "endswith", "find", "lower", "split"} & attrs), (
        f"a string operation is back in the classifier: {attrs}")
    assert "Traceback" not in literals and "setup failed:" not in literals


# --------------------------------------------------------------------------------------------
# What the classification is FOR
# --------------------------------------------------------------------------------------------

class _Repairer(CrashRepairMixin):
    def __init__(self):
        self._deep_repair = False
        self._repo_spec = None
        self._eval_parallel = 1
        self._gpu_ids = None


def test_the_directive_covers_the_raised_shape_and_its_traceback():
    """The directive used to say the failure comes "typically with no Python traceback", which is
    true of the kernel kill and false of this one — telling a Developer to distrust the traceback
    that names the failing allocation. Asserted as PROPERTIES, not a pinned literal: the wording is
    a prompt contract and will be tuned."""
    text = _Repairer()._repair_error_context("oom", "[failed stage: train]\n<the log tail>")
    assert "[failure kind: oom]" in text
    assert "<the log tail>" in text
    low = text.lower()
    assert "outofmemoryerror" in low or "cuda out of memory" in low, (
        "the raised shape must be named — a Developer holding a traceback is otherwise told it "
        "should not have one")
    assert "traceback" in low and "read it" in low


def test_the_directive_warns_about_the_two_numbers_that_made_this_run_loop():
    """Both traps are why v3 never escaped: three nodes declared batch 8192 with accumulation 2 and
    OOMed identically on 1 GPU and on 2. A directive that says "cut the memory" without saying that
    the batch is PER DEVICE and that accumulation MULTIPLIES invites exactly those two non-fixes."""
    text = _Repairer()._repair_error_context("oom", "e").lower()
    assert "per device" in text or "per-device" in text
    assert "gradient_accumulation_steps" in text
    assert "multipl" in text, "the accumulation direction must be stated, not just mentioned"


def test_the_no_judge_path_repairs_it_and_does_not_promise_a_pod_limit():
    """With no triage model wired `_rule_triage` is the whole decision. Its rationale named the
    "pod limit" — the cgroup — which is the wrong place to look for a VRAM exhaustion."""
    verdict = _rule_triage("oom", "torch.OutOfMemoryError: CUDA out of memory", 1, 3)
    assert verdict["action"] == "repair"
    assert "memory" in verdict["rationale"]
    assert "pod limit" not in verdict["rationale"]
    assert _rule_triage("oom", "x", attempt=9, max_attempts=3)["action"] == "abandon"


def test_an_oom_still_cannot_suppress_a_metric_or_buy_extra_attempts():
    """The two properties that used to justify a MARKER reading forgeable text, and that now bound
    a MODEL's answer instead. The argument is the same either way and so is the guard: whatever
    names `oom`, being wrong about it must cost exactly one repair round pointed at memory."""
    assert "oom" not in NEVER_SALVAGED_REASONS, (
        "an `oom` can now suppress a salvaged metric — reading candidate text to reach it is no "
        "longer a repair-only decision")
    assert "oom" in FAILURE_REASONS
    for reason in ("oom", "crash"):
        assert _rule_triage(reason, "a mechanical ImportError", attempt=4, max_attempts=3)[
            "action"] == "abandon", f"{reason} outlives the attempt bound"
