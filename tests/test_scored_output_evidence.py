"""A NODE THAT SCORED KEPT LESS OF ITS OWN ACCOUNT THAN A NODE THAT CRASHED.

THE MEASUREMENT (`runs-armb/spectral_clustering/run/events.jsonl`, node 0)
--------------------------------------------------------------------------
The node exited 0, its stage row says `status: "ok"`, and it scored 0.0. The entire durable record
of why is `node_evaluated.stdout_tail`:

    {"speedup": 0.0, "eval_seconds": 110.5, "subset": "train", "baseline_source": "in-harness ..."}

One scalar. The next `hint`/`card_added` then reads, correctly and disastrously, "Node #0's sklearn
SpectralClustering replication scored 0.0, so 'replicate the reference solve() exactly' ... is
answered and FAILED" — a verdict on the HYPOTHESIS drawn from a number whose cause was an
IMPLEMENTATION fact (95 of 100 instances valid) that the eval knew and never got to say.

The loop reasoned correctly from what it was given. What it was given was one scalar, because
`engine/evaluate.py` built the scored terminal from `res.stdout[-500:]` and never read `res.stderr`
at all — 100 % of the eval command's own diagnostic channel dropped between `run_command_eval`
returning it and the row being written. Across the 55 `node_evaluated` rows in that corpus there is
no field it could have landed in. The FAILURE terminal had already been widened for exactly this
(`_durable_failure_evidence`, 16,000 characters, `tests/test_durable_failure_evidence.py`); the
terminal that says the node WORKED was left with less than the one that says it died.

WHAT THIS IS AND WHAT IT IS NOT
-------------------------------
It is a RECORD. `res.metric` is still the only thing that decides anything — `ok`, `feasible`,
`violations`, salvage, selection and the failure taxonomy are byte-identical whatever this string
contains. A metric is measured, not narrated, and text here may nominate a reason to a reader and
can never decide one. The last two tests below are that property with teeth.

The window is 4,000 characters and every side of it is measured — see
`evaluate._scored_output_evidence`. The bound this file pins is the one that would silently rot:
the payload it exists to carry (arm A hands its agent the validity summary plus up to three
`is_solution` examples; the five such blocks preserved on this box are 2,581-2,878 characters) must
fit, and the window must be a TAIL.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine import evaluate as ev


class _Res:
    def __init__(self, stderr="", stdout="", exit_code=0):
        self.stderr, self.stdout, self.exit_code = stderr, stdout, exit_code
        self.timed_out, self.drift, self.failed_stage = False, None, ""


def _evidence(stderr):
    eng = type("E", (), {"_redact": staticmethod(lambda s: s.replace("hunter2", "***")),
                         "_scored_output_evidence": ev.EvaluateMixin._scored_output_evidence})()
    return eng._scored_output_evidence(_Res(stderr=stderr))


# ------------------------------------------------------------------ the window itself
# The arm-A diagnostic this column exists to be able to carry, at its MEASURED size. The five blocks
# preserved in `campaign/A-*.log` run 2,581-2,878 characters (validity summary + three `is_solution`
# examples, `AlgoTune/AlgoTuner/utils/message_writer.py:726-750`); 2,878 is the largest.
_ARM_A_BLOCK_CHARS = 2_878


def test_the_window_carries_the_diagnostic_it_exists_for():
    """The reason a metric is what it is, at the size the competing arm actually hands its agent."""
    reason = "Valid Solutions: 95%\nInvalid Solutions: 5%\nInvalid Example #1: Error in 'is_solution'"
    padding = "\n".join(f"  {i}: some framework chatter" for i in range(120))
    stderr = reason + "\n" + padding
    assert _ARM_A_BLOCK_CHARS <= len(stderr) <= ev._SCORED_EVIDENCE_CHARS, \
        "the fixture must be arm-A-sized and inside the window, or it tests only the clamp"
    kept = _evidence(stderr)
    assert reason.splitlines()[0] in kept and "Invalid Example #1" in kept


def test_the_window_is_wide_enough_for_that_block_and_a_2000_char_one_would_not_be():
    """The bound stated as the comparison that chose it: 0 of the 5 measured blocks fit 2,000
    characters and 5 of 5 fit 4,000. A future narrowing that still passed "is a tail" would silently
    reintroduce exactly the defect."""
    assert ev._SCORED_EVIDENCE_CHARS >= _ARM_A_BLOCK_CHARS
    assert 2_000 < _ARM_A_BLOCK_CHARS
    # …and it stays UNDER what the record's failure-path sibling spends, because this row is written
    # once per SCORED node rather than once per failure.
    assert ev._SCORED_EVIDENCE_CHARS < ev._DURABLE_EVIDENCE_CHARS


def test_the_window_is_a_tail_and_is_bounded():
    """A TAIL, not a head: an eval prints its summary last, and the clamp must be exact so the cost
    of this column is a number and not an estimate."""
    stderr = "x" * (ev._SCORED_EVIDENCE_CHARS * 3) + "THE-END"
    kept = _evidence(stderr)
    assert kept.endswith("THE-END") and len(kept) == ev._SCORED_EVIDENCE_CHARS


def test_one_window_governs_both_streams_of_the_scored_terminal():
    """`stdout_tail` and `stderr_tail` are two views of the SAME question — what did the eval say
    about the number — so they are one constant, not two. Two would drift, and the stream an eval
    chooses to speak on is not a fact about how much of it is worth keeping.

    RE-DERIVED THROUGH `_redacted_tail` (2026-08-23): the cap moved off the stream expression and
    into that helper's third argument when the redact-then-cap order was hoisted, so a test reading
    `res.stdout[-…:]` would have gone green on a slice that no longer exists. The window is now the
    ARGUMENT, and it is read from every call in the module — so a stdout window that grew its own
    constant is still red, and so is one written as a bare number."""
    tree = ast.parse(Path(inspect.getfile(ev)).read_text(encoding="utf-8"))
    caps = [ast.unparse(node.args[2]) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "_redacted_tail"]
    assert sorted(caps) == ["_DURABLE_EVIDENCE_CHARS",
                            "_SCORED_EVIDENCE_CHARS", "_SCORED_EVIDENCE_CHARS"], (
        f"the two scored-terminal streams must share ONE named window; found {sorted(caps)}")
    # Over the SUBSCRIPTS, not the file text: this module's own comments quote the old
    # `res.stdout[-500:]` as the history it is explaining, and a substring test would either forbid
    # writing that history down or pass on it. What must not exist is the SLICE.
    slices = {ast.unparse(n) for n in ast.walk(tree)
              if isinstance(n, ast.Subscript) and ast.unparse(n.value) in ("res.stdout", "res.stderr")}
    assert "res.stdout[-500:]" not in slices, \
        "500 was measured too short for a 745-character metric line — see _SCORED_EVIDENCE_CHARS"
    # …and the PROMPT's window on the failure path is deliberately untouched: `_eval_failure_text`
    # is four things at once and three of them are paid text (`test_durable_failure_evidence.py`).
    assert "res.stderr[-500:]" in slices, "the repair prompt's 500-char tail must not have moved"


def test_the_column_goes_through_the_same_redactor_before_the_cap():
    """It is candidate/eval bytes on a durable row, so it is the same channel `core/redact.py` owns.
    Redaction runs BEFORE the slice for the reason it does everywhere else here: a cap applied first
    hands the redactor a truncated stub the shape rule no longer matches."""
    kept = _evidence("connecting with password=hunter2 to the index\n" + "tail\n" * 50)
    assert "hunter2" not in kept and "***" in kept


def test_a_secret_straddling_the_cut_is_not_left_as_a_fragment():
    """THE ASSERTION ABOVE CANNOT SEE THE ORDER, and that is why this one exists.

    Its fixture is ~250 characters, so the cap is a no-op on it and `redact(text[-N:])` and
    `redact(text)[-N:]` return the same bytes — the test passed identically against the order its
    own name forbids. The property only becomes observable when the cut lands INSIDE a secret, which
    a tail cut does by severing the secret's HEAD: `sk-live-A9fQ2xLm7ZpR4tVw8YbN1cJdKe` cut eleven
    characters in leaves `Q2xLm7ZpR4tVw8YbN1cJdKe`, which matches no prefix rule and is shorter than
    what the entropy rule fires on, so the redactor returns it verbatim onto a durable row.

    Driven against the REAL `core/redact.py` rather than a `.replace()` double, because what is on
    trial is exactly which shapes that module recognizes. The control is the same secret redacted
    whole: it must be masked, or the fixture is not a secret and the case proves nothing.
    """
    from looplab.core.redact import redact_secrets

    secret = "sk-live-A9fQ2xLm7ZpR4tVw8YbN1cJdKe"
    line = "export TOKEN=" + secret
    assert "***" in redact_secrets(line), "the fixture is not a secret this redactor knows"

    tail = "\nharmless tail\n"
    # A window that severs the token eleven characters in, i.e. mid-secret.
    chars = len(secret) - 11 + len(tail)
    engine = type("E", (), {"_redact": staticmethod(redact_secrets)})()
    kept = ev._redacted_tail(engine._redact, "x" * 400 + line + tail, chars)
    assert secret[11:] not in kept, (
        f"a secret fragment survived the cut: {kept!r} — the redactor must see the whole stream")


def test_nothing_to_keep_is_the_empty_string_and_not_a_fabricated_line():
    for blank in ("", "   \n\t  ", None):
        assert _evidence(blank) == ""


# ------------------------------------------------------------------ absence vs emptiness
def test_the_column_is_omitted_rather_than_written_empty():
    """`{"stderr_tail": ""}` and no key at all are opposite facts: the first says the eval wrote
    nothing to stderr, the second says the row predates the column. It matters more here than on the
    failure row: `node_evaluated` is written for EVERY successful node, so an unconditional key would
    change the bytes of every node in every run for no information at all.

    Driven over the WRITE SITE's source, because the alternative — driving a whole sandboxed eval —
    is how a clause like this gets deleted with every guard still green."""
    src = Path(inspect.getfile(ev)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    guarded = [n for n in ast.walk(tree)
               if isinstance(n, ast.If) and ast.unparse(n.test).strip() == "_scored_evidence"]
    assert len(guarded) == 1, "the scored-terminal write site must be guarded on truthiness"
    assert '"stderr_tail": _scored_evidence' not in src, \
        "the column must never be written unconditionally"


# ------------------------------------------------------------------ the whole trip, for real
def _run_scored_engine(tmp_path, *, stdout_lines, stderr_lines):
    """Drive a REAL run: a real subprocess sandbox evaluates real generated code that exits 0 with a
    parseable metric on stdout and a diagnostic on stderr — i.e. a node that SCORES. The DURABLE
    event log is read back off disk, then folded, because a tail that is only in RAM is not the
    property under test."""
    import anyio

    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.events.replay import fold
    from looplab.events.eventstore import EventStore
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")

    class _Dev:
        def implement(self, idea):
            return ("import json, sys\n"
                    + "".join(f"print({l!r}, file=sys.stderr)\n" for l in stderr_lines)
                    + "".join(f"print({l!r})\n" for l in stdout_lines)
                    + "print(json.dumps({'metric': 0.5}))\n")

    class _Stub:
        def propose(self, state, parent):
            return Idea(operator="draft", params={"x": 1.0, "y": 1.0})

    run_dir = tmp_path / "r"
    eng = Engine(run_dir, task=task, researcher=_Stub(), developer=_Dev(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))
    anyio.run(eng.run)
    rows = [json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines() if l.strip()]
    scored = [r["data"] for r in rows if r.get("type") == "node_evaluated"]
    assert scored, "no node_evaluated row — the fixture must SCORE, not fail"
    return scored[0], fold(EventStore(str(run_dir / "events.jsonl")).read_all())


_REASON = "eval: 95 of 100 instances valid (5 invalid: label permutation mismatch)"


def test_the_reason_a_node_scored_survives_the_trip_to_the_next_proposal(tmp_path):
    """END TO END, through the real write site: the eval's own account of a metric reaches the
    durable row, the fold, and the tool the next proposal reads it with.

    The three assertions are the three links that were broken. Before this change the row had no
    field, so the fold had nothing to carry and `read_logs` rendered a node with a bad metric as a
    stdout tail and nothing else."""
    payload, state = _run_scored_engine(
        tmp_path, stdout_lines=["training done"], stderr_lines=[_REASON])

    # L0 — it is a SCORED node, not a failure. The whole point is the terminal that says it WORKED.
    assert payload["metric"] is not None

    # L1 — the durable row (read off disk, not off the in-memory state).
    assert _REASON in payload.get("stderr_tail", ""), \
        "the eval's own account of the number never reached the durable record"
    # …and the negative control that names the defect: it is NOT in the stdout tail, which is all
    # the row used to carry.
    assert _REASON not in payload.get("stdout_tail", "")

    # L2 — the fold. `looplab replay` must rebuild it, or the UI/report/every read-model loses it.
    node = state.nodes[0]
    assert node.status is NodeStatus.evaluated and _REASON in node.stderr_tail

    # L3 — the injection site the next proposal actually reads.
    from looplab.tools.run_tools import RunTools
    rt = RunTools()
    rt.bind_state(state)
    assert _REASON in rt.execute("read_logs", {"node_id": 0})


def test_an_eval_that_says_why_on_stdout_is_not_cut_off_at_its_front(tmp_path):
    """THE OTHER STREAM, and the truncation rule for it — driven end to end.

    An eval may say why on stdout, inside the very JSON line the metric reader parses.
    `benchmarks/algotune/looplab_eval.py` does exactly that: its `no_speedup` line measures 745
    characters over `tests/fixtures/algotune_eval_invalid_results_stderr.txt`, and the old
    `res.stdout[-500:]` kept the LAST 500 of it — dropping the `{"speedup": 0.0` the number lives in
    and the `no_speedup.reason` class with it, and starting the durable record mid-string.

    The fixture reproduces the shape rather than the bytes: a metric line whose informative front is
    more than 500 characters from the end. It must survive whole, and the window must still be a
    bounded TAIL — both directions, so "just remove the cap" cannot pass this."""
    head = "REASON=" + "invalid_results;" * 8            # the front, >500 chars from the end
    filler = "x" * 600
    line = head + filler
    assert 500 < len(line) <= ev._SCORED_EVIDENCE_CHARS
    payload, state = _run_scored_engine(
        tmp_path, stdout_lines=[line], stderr_lines=[])

    assert payload["metric"] is not None
    assert head in payload["stdout_tail"], \
        "the front of the eval's own metric line was cut off by the record's window"
    assert head not in "\n".join([line, '{"metric": 0.5}'])[-500:], \
        "the fixture must be outside the OLD window, or it proves nothing"
    # …still a bounded tail, not an unbounded copy of the stream.
    assert len(payload["stdout_tail"]) <= ev._SCORED_EVIDENCE_CHARS
    assert head in state.nodes[0].stdout_tail


def test_an_eval_that_says_nothing_leaves_the_row_exactly_as_it_was(tmp_path):
    """The other half of absence-vs-emptiness, driven rather than asserted over source: a silent
    eval must produce a row with NO new key, so every pre-existing run and every quiet task keeps
    byte-identical `node_evaluated` payloads."""
    payload, state = _run_scored_engine(
        tmp_path, stdout_lines=["training done"], stderr_lines=[])
    assert "stderr_tail" not in payload
    assert state.nodes[0].stderr_tail == ""


def test_a_reset_does_not_carry_the_previous_attempts_reason_onto_the_new_one():
    """A re-evaluated node that kept the PREVIOUS attempt's stderr would show the loop a reason for
    a metric that no longer exists — the sharper version of the stale-record defect
    `engine/evaluate.py` resets `reason_summary` for."""
    from looplab.events import replay

    src = inspect.getsource(replay)
    # Both reset sites (node_reset and the abandoned-lifecycle sweep) clear the sibling tail; this
    # column must be cleared by the same statement blocks or it outlives its own metric.
    # OPEN[scored-evidence-reset-count-pin] a positive substring COUNT pin -- satisfiable by one
    # comment. proof:`present:src.count('n.stderr_tail = ""')@tests/test_scored_output_evidence.py`
    # REVIEW 2026-08-25 (guard-test): the cheapest mutation -- delete one real clear in a reset
    # handler and leave a commented-out copy in its place -- keeps both counts at 2 and this green
    # while a reset now carries a stale stderr reason onto the new attempt. That is the exact
    # residue class CLAUDE.md's ladder sends to tier 3, and the SAME diff already contains the
    # correct pattern for the IDENTICAL property one file over:
    # tests/test_stop_account.py::test_pause_reason_is_cleared_wherever_the_pause_is_lifted counts
    # real `ast.Assign` targets, and comments are not AST nodes. Rewrite this assert the same way
    # (or better, drive both reset sites behaviourally, as that file's lift-parametrized sibling
    # test does through the real fold).
    assert src.count('n.stdout_tail = ""') == src.count('n.stderr_tail = ""') == 2


# ------------------------------------------------------------------ THE guard: record, not verdict
# "Text may nominate, never decide." Every module here either CHOOSES a number, classifies a
# failure, or spends money on the strength of one — and none of them may key on a string the
# candidate's own eval wrote. The failure taxonomy is the sharpest case: `_failure_reason` had two
# text rules deleted on 2026-08-20 precisely so that no classification reads a message, and this
# column is a message.
_DECISION_PATH = ("looplab/engine/triage.py", "looplab/engine/metric_salvage.py",
                  "looplab/engine/failure_diagnosis.py", "looplab/engine/repair_judgment.py",
                  "looplab/engine/crash_repair.py", "looplab/core/fitness.py",
                  "looplab/search/policy.py")


def test_nothing_that_decides_reads_the_eval_s_own_account():
    root = Path(inspect.getfile(ev)).resolve().parents[2]
    offenders = [rel for rel in _DECISION_PATH
                 if "stderr_tail" in (root / rel).read_text(encoding="utf-8")]
    assert offenders == [], f"a decision path is keying on eval-authored text: {offenders}"


def test_it_stays_off_the_always_on_prompt():
    """The route is `pull` (`engine/signal_delivery.py`), and that is a COST property, not a taste
    one: `experiments_digest` is the Researcher's always-on working set under a hard char cap, so
    up to 4,000 characters per scored node landing there would displace the listing of the search
    itself. The day it becomes `context`, this comment and the registry entry are what say so."""
    from looplab.engine.signal_delivery import SIGNALS
    from looplab.events import digest

    route = next(r for r in SIGNALS if r.name == "scored_eval_stderr")
    assert route.channel == "pull" and route.folded_into == "Node.stderr_tail"
    assert "stderr_tail" not in inspect.getsource(digest), \
        "the always-on digest must not grow by the eval's own text"
