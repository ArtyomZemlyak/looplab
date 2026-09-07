"""THE AGENT THAT DECIDES WHETHER THE CHAIN LIVES WAS READING A THINNER RECORD THAN THE ONE ACTING.

MEASURED ON THE LIVE RUN, minutes apart (`runs/e5small-dr-unified-v4` node 3, attempt 6, 2026-08-21)
-----------------------------------------------------------------------------------------------------
The REPAIR — which had just been given the watchdog's verdicts — wrote:

    "The watchdog's diagnosis is correct and I reproduced the mechanism in code: the ported loss
     removes the positive logit from the denominator while masking negatives at -1e9, so aggressive
     DCL masking (dcl_threshold=0.05) empties the denominator and the loss diverges to -2e10."

and changed `vectorsearch/training/loss.py`. `verified`, `unmet=[]`.

The CRITIC, on the same node in the same minute, wrote:

    "...and now a pure speed timeout with a healthy training run"

which is VERBATIM the framing that cost ~17 GPU-hours the day before ("Healthy training run ... a
pure speed failure, not a correctness one"). Its per-attempt cause column is the ENGINE's — `crash`,
`crash`, `oom`, `expect_failed`, `timeout` — and none of those words can say that the objective has
no floor.

It answered `continue` and the answer was RIGHT; the justification was a training run that was not
healthy. **The expensive direction is the mirror one:** a chain that repairs SPEED five times on a
node whose loss is unbounded below looks like five DISTINCT causes to a reader of that column,
because the engine wrote a different word each time — and "distinct causes" is precisely the
evidence this critic continues on.

The scoping error was mine. When the verdicts were wired into `_ask_triage` I filed `_repair_critic`
as out of scope, and it is not: it returns continue-or-stop over the same failure.
"""
from __future__ import annotations

import inspect

from looplab.core.models import RunState
from looplab.engine import crash_repair as cr


class _Critic:
    """A critic seam that records the trajectory text it was handed."""

    def __init__(self):
        self.seen = []

    def repair_critic(self, node, *, trajectory="", attempt=0, brief="", state=None, **kw):
        self.seen.append(trajectory)
        return {"action": "continue", "rationale": "r"}


class _EngineStub(cr.CrashRepairMixin):
    _inline_repair_attempts = 5

    def __init__(self, researcher):
        self.researcher = researcher

        class _Span:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def set(self, *a, **k):
                return None

            def set_many(self, *a, **k):
                return None

        self.tracer = type("T", (), {"span": lambda *a, **k: _Span()})()


_LOG = [{"attempt": 1, "action": "repair", "reason": "crash", "fix": "typo"},
        {"attempt": 2, "action": "repair", "reason": "timeout", "fix": "cut n_epochs 15 -> 3"}]

_VERDICTS = [
    {"status": "broken", "confidence": 0.85, "fault": "implementation", "stage": "train",
     "reason": "the -1e9 masked-logit sentinel reaches the logsumexp denominator",
     "trajectory": {"direction": "descending", "first": 38.57, "last": -14599585.6, "windows": 9}},
    {"status": "healthy", "confidence": 0.80, "stage": "train",
     "reason": "loss descends steadily across every bucket, no errors",
     "trajectory": {"direction": "descending", "first": 38.57, "last": -14599585.6, "windows": 9}},
]


def _ask(verdicts):
    critic = _Critic()
    out = _EngineStub(critic)._repair_critic(RunState(), type("N", (), {"id": 3})(), _LOG, 6,
                                             monitor_verdicts=verdicts)
    assert out["action"] == "continue"
    return critic.seen[0] if critic.seen else ""


# ------------------------------------------------------------------ the property
def test_the_critic_is_shown_what_the_watchdog_said():
    text = _ask(_VERDICTS)
    assert "WATCHDOG" in text
    assert "-1e9 masked-logit sentinel" in text
    # …and the ENGINE's own measurement beside it, which is not a judgement
    assert "measured:" in text and "last=-14599585.6" in text


def test_the_critic_still_gets_the_repair_trajectory_it_always_had():
    """Two independent bodies of evidence. The verdicts must not shadow the chain the critic exists
    to judge — "are these attempts circling?" is answered from the trajectory."""
    text = _ask(_VERDICTS)
    assert "cut n_epochs 15 -> 3" in text
    assert text.index("cut n_epochs") < text.index("WATCHDOG")


def test_the_contradiction_survives_for_the_critic_too():
    """This node's watchdog said `healthy` at 0.80 and `broken` at 0.85 about the same trajectory. A
    critic that saw only one of them would be told there is a settled answer when there is not."""
    text = _ask(_VERDICTS)
    assert "verdict=broken" in text and "verdict=healthy" in text
    assert "confidence=0.85" in text and "confidence=0.80" in text
    assert "disagreed with itself" in text          # the heading warns before the rows


def test_no_verdicts_asks_the_historical_question_byte_for_byte():
    """`off == today`: a node whose watchdog never spoke, or a run with the monitor off, gets the
    exact trajectory string the critic has always been handed."""
    from looplab.engine.repair_judgment import format_repair_trajectory

    baseline = format_repair_trajectory(_LOG)
    for empty in (None, [], [{}]):
        assert _ask(empty) == baseline


def test_the_verdicts_ride_the_trajectory_and_not_a_new_keyword():
    """`repair_critic` is a DUCK-TYPED seam and `_accepted_kwargs` drops arguments an older
    implementation does not name — so a new keyword would reach one implementation and silently skip
    every other. Asserted over the source, because the failure it prevents is invisible at runtime:
    the call still succeeds, the evidence just never arrives."""
    src = inspect.getsource(cr.CrashRepairMixin._repair_critic)
    assert "_format_monitor_verdicts(monitor_verdicts)" in src
    assert "trajectory = trajectory + " in src
    assert '"monitor_verdicts"' not in src          # never handed as its own kwarg


def test_an_older_critic_implementation_still_answers():
    class _Old:
        def repair_critic(self, node, **kw):
            return {"action": "stop", "rationale": "old"}

    out = _EngineStub(_Old())._repair_critic(RunState(), type("N", (), {"id": 3})(), _LOG, 6,
                                             monitor_verdicts=_VERDICTS)
    assert out["action"] == "stop"


# ------------------------------------------------------------------ the wiring
def test_the_engine_hands_the_critic_the_same_verdicts_the_judge_read():
    """One read, two readers. A critic grading the judge's work must not be looking at a thinner
    record than the judge was."""
    import ast

    ev = __import__("looplab.engine.evaluate", fromlist=["x"])
    src = inspect.getsource(ev)
    assert "monitor_verdicts=a._monitor_verdicts)" in src   # the judge's read, boxed on the attempt

    # ONE READ PER ATTEMPT, and counted as CALLS rather than as an assignment spelling — a second
    # `_durable_monitor_verdicts(...)` inlined at the critic's call site would re-read the whole
    # event log on a path that already has the answer in a local, and an assignment-shaped pin
    # cannot see it. `read_all()` on a long run is not free, and the two readers must also be
    # looking at the SAME rows: a second read taken moments later can differ.
    from tests._source_scan import EVAL_PHASES

    # The driver and its `_eval_*` phases (the read is DECIDE_REPAIR's since the EvalAttempt split),
    # scanned together so a second read in ANY phase is still a red test.
    tree = ast.parse(src)
    loop = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in ("_evaluate",) + EVAL_PHASES]
    assert len(loop) == 1 + len(EVAL_PHASES)
    calls = [n for fn in loop for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_durable_monitor_verdicts"]
    assert len(calls) == 1, f"the verdicts must be read once per attempt, found {len(calls)} calls"
