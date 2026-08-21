"""THE RECORD KEPT 500 CHARACTERS OF A FAILURE THE CLASSIFIER READ 64,000 OF.

THE MEASUREMENT (`judgebench/triage_corpus.py` states it in its own header)
--------------------------------------------------------------------------
`res.stderr` was clamped at 64,000 bytes per stream when the classifier read it. What survived to
disk is `node_repaired.error_in`: 500 characters. **Not one of the 122 stored tails in that corpus
contains a torch-OOM marker** — five are a launcher's opaque `Root Cause … exitcode: 1` block and two
are nothing but a progress bar. The deleted marker rule replayed over the durable record therefore
scores 0 of 23 OOMs and 16 of 23 over a wider window, and the diagnostician goes 82.2% -> 86.4% on
the same widening. The evidence was never missing. It was thrown away between being read and being
written down.

WHY THIS IS A SECOND COLUMN AND NOT A BIGGER `error_in`
-------------------------------------------------------
`_eval_failure_text` is documented as "the ONE description of a failed eval" and is FOUR things at
once: the repair prompt, `node_repaired.error_in`, the judge's history rows, and the terminal's
`error` field. Three of those are paid text on every repair. Widening that one string to fix the
record would multiply the prompt by 32 — and the prompt-text contract is that a new fact earns a new
sentence, not that an existing one silently grows.

So the record gets its own window, and the property that keeps it honest is: **nothing on the prompt
path reads it.** That is the guard below with teeth, because the day something does, this becomes an
invisible 32x increase in the cost of every repair.

The second property is the one this repo keeps relearning: **absence and emptiness are different
facts.** A row with no `error_evidence` key predates the column; a row with an empty one means the
eval wrote nothing to stderr. A reader that cannot tell them apart will read the first as the second
and conclude the failure was silent.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from looplab.engine import evaluate as ev


class _Res:
    def __init__(self, stderr="", stdout="", exit_code=1):
        self.stderr, self.stdout, self.exit_code = stderr, stdout, exit_code
        self.timed_out, self.drift, self.failed_stage = False, None, ""


class _Eng:
    """The narrowest engine `_durable_failure_evidence` needs: a redactor."""

    _redact = staticmethod(lambda s: s.replace("hunter2", "***"))
    _durable_failure_evidence = ev.Engine._durable_failure_evidence \
        if hasattr(ev, "Engine") else None


def _evidence(stderr):
    eng = type("E", (), {"_redact": staticmethod(lambda s: s.replace("hunter2", "***")),
                         "_durable_failure_evidence": ev.EvaluateMixin._durable_failure_evidence})()
    return eng._durable_failure_evidence(_Res(stderr=stderr))


# ------------------------------------------------------------------ the window itself
def test_the_record_keeps_far_more_than_the_prompt_says():
    """A traceback whose informative line sits 40,000 characters before the end — the shape the
    corpus measured, where the tail is a progress bar and the cause is upstream of it."""
    cause = "torch.cuda.OutOfMemoryError: Tried to allocate 20.00 GiB. GPU 0 has 139.80 GiB free"
    # Sized to sit INSIDE the record's window and far outside the prompt's: ~12,000 characters of
    # bar renders after the cause, against a 16,000-character record and a 500-character prompt.
    # A fixture whose noise exceeds the record's own window would test nothing but the clamp.
    noise = "\n".join(f"  {n}%|███| {n}/100 [00:0{n % 10}<00:00]" for n in range(380))
    assert 500 < len(noise) < ev._DURABLE_EVIDENCE_CHARS
    stderr = cause + "\n" + noise
    kept = _evidence(stderr)
    assert cause in kept, "the record must keep the line the classifier actually read"
    assert len(kept) <= ev._DURABLE_EVIDENCE_CHARS
    # …and the PROMPT's 500-character tail, which is what ships today, still cannot see it.
    assert cause not in stderr[-500:]


def test_the_window_is_a_tail_and_is_bounded():
    stderr = "x" * (ev._DURABLE_EVIDENCE_CHARS * 3) + "THE-END"
    kept = _evidence(stderr)
    assert kept.endswith("THE-END") and len(kept) == ev._DURABLE_EVIDENCE_CHARS


def test_the_wide_column_goes_through_the_same_redactor():
    """It carries 32x the text, so it carries 32x the chance of a secret. An agent priced the
    redactor's firing rate on this corpus: 0 masks at 500 characters, 36 at 16 KB — including a real
    `password` — and 384 at 64 KB. The redactor is the only thing between this column and a durable
    log, which is the argument for 16 KB over 64 KB and the reason this test exists at all."""
    kept = _evidence("connecting with password=hunter2 to the index\n" + "tail\n" * 50)
    assert "hunter2" not in kept and "***" in kept


def test_nothing_to_keep_is_the_empty_string_and_not_a_fabricated_line():
    for blank in ("", "   \n\t  ", None):
        assert _evidence(blank) == ""


# ------------------------------------------------------------------ absence vs emptiness
def test_the_column_is_omitted_rather_than_written_empty():
    """`{"error_evidence": ""}` and no key at all are opposite facts: the first says the eval wrote
    nothing to stderr, the second says the row predates the column. Driven over the WRITE SITES'
    source, because the alternative — driving a whole sandboxed eval — is how a clause like this
    gets deleted with every guard still green."""
    src = Path(inspect.getfile(ev)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    guarded = 0
    for node in ast.walk(tree):
        # the repair row: `**({"error_evidence": ...} if err_evidence else {})`
        if isinstance(node, ast.IfExp) and "error_evidence" in ast.unparse(node):
            guarded += 1
        # the terminal: `if err_evidence: data["error_evidence"] = ...`
        if isinstance(node, ast.If) and ast.unparse(node.test).strip() == "err_evidence":
            guarded += 1
    assert guarded == 2, "both write sites must be guarded on truthiness, not written unconditionally"
    assert '"error_evidence": err_evidence' not in src.replace(
        '**({"error_evidence": err_evidence} if err_evidence else {})', "")


# ------------------------------------------------------------------ THE guard
_PROMPT_PATH = ("looplab/engine/crash_repair.py", "looplab/agents/unified_agent.py",
                "looplab/engine/failure_diagnosis.py", "looplab/engine/train_monitor.py")


def test_nothing_on_the_prompt_path_reads_the_wide_column():
    """THE property that makes the second column safe. `_durable_repair_ledger` keeps building the
    judge's history from `error_in`, and every prompt assembler keeps reading the 500-character
    string. The day one of them reads `error_evidence` instead, every repair on every run silently
    costs 32x more, with nothing in the diff that looks like a cost change.

    Stated over the SOURCE of the modules that build prompts, and it fails on a substring for once
    deliberately: the thing being forbidden IS a name appearing in those files."""
    root = Path(inspect.getfile(ev)).resolve().parents[2]
    offenders = [rel for rel in _PROMPT_PATH
                 if "error_evidence" in (root / rel).read_text(encoding="utf-8")]
    assert offenders == [], f"the prompt path must not read the record's wide window: {offenders}"


def test_the_judge_history_still_reads_the_narrow_one():
    """The other half of the same property, stated positively so "nobody reads either" cannot pass
    it. `_durable_repair_ledger` is what builds the repair history the judge is shown."""
    src = inspect.getsource(ev._durable_repair_ledger)
    assert 'd.get("error_in"' in src and "error_evidence" not in src


# ------------------------------------------------------------------ the bench can see it
def test_the_corpus_carries_the_wide_window_as_its_own_field():
    """A bench that merged the two would report a WINDOW change as a RULE change — the exact
    confusion `--arm frozen` and `--arm frozen-widened` exist to keep apart."""
    from looplab.judgebench import triage_corpus

    src = inspect.getsource(triage_corpus)
    assert '"stderr_evidence"' in src and '"stderr_evidence_chars"' in src
    assert '"stderr_tail": at_classification' in src, "the narrow field must survive unchanged"
