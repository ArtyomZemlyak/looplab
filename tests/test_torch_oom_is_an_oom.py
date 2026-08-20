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
out of band observed it. The text is the only witness, and `_is_torch_oom`'s docstring carries the
argument for why that is acceptable at this one site: every consumer of the `oom` literal routes a
REPAIR, `oom` is absent from `metric_salvage.NEVER_SALVAGED_REASONS`, and `_rule_triage` bounds it by
the same `max_attempts` as a `crash` — so a forged `oom` costs one repair round aimed at memory and
can never admit a metric, move a champion or change a selection.

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

The REMAINING gap is a different one and is not fixed here. What the Developer is HANDED is
`res.stderr[-500:]` (`evaluate.py::_eval_failure_text`), and on a DDP failure those 500 characters
are pure elastic wrapper: the durable `node_failed.error` on all three v3 nodes is 522 bytes ending
`traceback : To enable traceback see: ...`, with no allocation size, no device total and no
`OutOfMemoryError` anywhere in it. So the reason and its directive are the ONLY channel carrying the
diagnosis; the numbers that say WHICH fix to make (asked-for bytes vs already-resident bytes) never
reach the model at all.
    OPEN[oom-evidence-not-in-repair-text] a torch OOM's allocation numbers never reach the
    Developer: the repair text is a 500-char stderr tail that a DDP wrapper fills entirely.
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
from looplab.engine.triage import _failure_reason, _is_torch_oom, _rule_triage
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
def test_the_real_v3_oom_stderr_is_classified_oom(node):
    """Every node of the run this was found on, verbatim.

    Note what is asserted BESIDES the reason — exit 1 and a `Traceback` PRESENT. Those two are the
    exact negation of the kernel-OOM premise, so their presence is what makes the test meaningful:
    the classifier is handed the signature the old branch structurally could not match."""
    err = _CORPUS[node]
    assert "Traceback" in err, "no traceback: this fixture no longer covers the raised-OOM shape"
    assert "torch.OutOfMemoryError" in err

    res = _res(err)
    assert res.exit_code not in (-9, 137), "a SIGKILL would be the OTHER shape"
    assert _failure_reason(res) == "oom"


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
    for res in (_res(_CORPUS[ddp[0]]), _res(_CORPUS[plain[0]])):
        assert _failure_reason(res) == "oom"


def test_it_survives_the_tail_clamp_the_classifier_actually_reads():
    """WHERE the classifier can see this, which is the whole question for a DDP failure.

    `sandbox.run_argv` clamps each stream to `max_output_bytes` (64,000) as a TAIL, and the rank
    tracebacks are printed BEFORE the elastic wrapper's summary — so the thing to check is that the
    marker is still inside the last 64,000 bytes and was not pushed out by the wrapper. It is, for
    all three real logs. (This is also why the fix belongs in the classifier and not in the error
    TEXT: see the test below.)"""
    for node, err in _CORPUS.items():
        assert _is_torch_oom(err[-64_000:]), f"{node}: marker outside the clamp the classifier reads"


def test_the_developer_error_text_does_NOT_carry_the_marker():
    """Why classifying it correctly is what fixes this, rather than "let the model read the log".

    `evaluate.py::_eval_failure_text` builds the Developer's error from `res.stderr[-500:]`, and on
    a DDP failure those 500 characters are pure elastic wrapper — the durable `node_failed.error` on
    all three v3 nodes is 522 bytes of `Root Cause ... traceback : To enable traceback see: ...` and
    contains no allocation, no size and no `OutOfMemoryError`. So the model cannot recover the kind
    from the text it is handed; the DIRECTIVE, which is keyed on `reason`, is the channel that
    carries it."""
    for node, err in _CORPUS.items():
        if "ChildFailedError" in err:
            assert not _is_torch_oom(err[-500:]), (
                f"{node}: the 500-char tail now carries the marker — if that is a deliberate change "
                "to the tail rule, this test's premise moved and the reason is no longer the only "
                "channel")


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


def test_a_real_raised_oom_stage_is_classified_oom():
    """The whole chain: a real stage that really raises, really run, really classified."""
    res = run_command_eval([sys.executable, "-c", "print(1)"], "/tmp", 60, _M,
                           stages=[{"name": "train", "command": [sys.executable, "-c", _RAISING],
                                    "timeout": 60}])
    assert res.exit_code == 1 and "Traceback" in (res.stderr or "")
    assert not res.timed_out and not getattr(res, "stalled", False)
    assert _failure_reason(res) == "oom"


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
    assert not _is_torch_oom(wrapper)
    assert _failure_reason(_res(wrapper)) == "crash"


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


def test_a_kernel_oom_and_a_clean_run_are_unchanged():
    """Both ends of the pre-existing behaviour."""
    assert _failure_reason(_res("", exit_code=-9)) == "oom"
    assert _failure_reason(_res("Traceback (most recent call last):")) == "crash"
    assert _failure_reason(_res("setup failed:\nno module named torch")) == "setup"
    assert _failure_reason(RunResult(exit_code=0, stdout="", stderr="", metric=None,
                                     timed_out=False)) == "no_metric"


def test_the_classifier_stays_pure():
    """`triage.py`'s module docstring promises these helpers do no I/O, which is what makes them
    replay-safe — and it is also the reason the elastic-wrapper case above cannot be resolved by
    reading the rank's log file. Pin it by AST so a future "just open the log" cannot land quietly."""
    import looplab.engine.triage as triage
    tree = ast.parse(Path(triage.__file__).read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_is_torch_oom")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not ({"open", "eval", "exec", "__import__"} & called), called


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
    """The two properties `_is_torch_oom`'s docstring rests on when it argues that reading forgeable
    text is acceptable here. If either moves, that argument is void and this reads as the guard."""
    assert "oom" not in NEVER_SALVAGED_REASONS, (
        "an `oom` can now suppress a salvaged metric — reading candidate text to reach it is no "
        "longer a repair-only decision")
    assert "oom" in FAILURE_REASONS
    for reason in ("oom", "crash"):
        assert _rule_triage(reason, "a mechanical ImportError", attempt=4, max_attempts=3)[
            "action"] == "abandon", f"{reason} outlives the attempt bound"
