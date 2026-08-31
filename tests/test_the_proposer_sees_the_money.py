"""The roles that choose what to try next must see the budget that ends the run.

MEASURED over the 69-run probe corpus on 2026-08-29, with span inputs RESOLVED through
`benchmarks/algotune/span_input.py` (the raw `input` field is a suffix when `input_carry` is set;
reading it undercounts chained prompts, and that error is exactly what this measurement avoids):

    plan_step       6037 / 8298 = 72.8 %  see a money figure in their prompt
    deep_research    899 / 2795 = 32.2 %
    propose            0 / 3753 =  0.0 %
    repropose          0 / 1010 =  0.0 %
    plan               0 / 2236 =  0.0 %

50 of 50 finished runs end on `budget_exhausted`; none on any other reason; `max_eval_seconds` is
None on every probe, so the one existing budget cue (`_cue_eval_budget`, gated on the off-by-default
`budget_aware`) could never fire. The Developer who implements is told the budget three times in
four; the five roles that decide WHAT to build were never told once.

The bill: $3.6067 of $100.2691 (3.6 %) is spent after the last evaluated node, on a draw that never
completes. dsDL2 spent $0.3058 of $1.0041 — 30 % of its run — on a node with an empty `files` map.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from looplab.engine.proposal_cues import ProposalCuesMixin  # noqa: E402


class _Acct:
    def __init__(self, limit, spent):
        self.limit, self.spent = limit, spent


class _Client:
    def __init__(self, acct):
        self.accountant = acct


class _Engine(ProposalCuesMixin):
    def __init__(self, acct=None):
        self.researcher = _Client(acct) if acct is not None else None


def _hint(limit, spent):
    return _Engine(_Acct(limit, spent))._cue_llm_budget(None, None, None)


def test_the_cue_is_registered_in_prompt_order():
    cues = list(ProposalCuesMixin.PROPOSAL_CUES)
    assert "_cue_llm_budget" in cues, "a cue nobody calls is not a cue"
    assert cues.index("_cue_llm_budget") == cues.index("_cue_eval_budget") + 1


def test_it_names_the_money_left():
    hint, steering = _hint(1.0, 0.75)
    assert "$0.2500 left" in hint and "$1.0000" in hint
    assert steering and steering[0]["kind"] == "llm_budget"
    assert math.isclose(steering[0]["remaining_usd"], 0.25)


def test_a_nearly_spent_run_is_told_to_propose_something_it_can_finish():
    """dsDL2's failure in one sentence: the last draw must be one the run can SCORE."""
    hint, steering = _hint(1.0, 0.9)
    assert steering[0]["stance"] == "exploit"
    assert "cannot finish evaluating scores nothing" in hint


def test_a_fresh_run_is_told_to_explore():
    hint, steering = _hint(3.0, 0.1)
    assert steering[0]["stance"] == "explore"


def test_no_ceiling_changes_the_prompt_by_not_one_byte():
    assert _hint(0.0, 0.5) == ("", [])
    assert _Engine(None)._cue_llm_budget(None, None, None) == ("", [])


def test_a_broken_accountant_is_silence_not_a_crash():
    """A cue must never be the thing that ends a run."""
    class _Bad:
        limit = "not a number"
        spent = 0.0
    assert _Engine(_Bad())._cue_llm_budget(None, None, None) == ("", [])
    assert _hint(float("inf"), 0.0) == ("", [])


# ------------------------------------------------------------------ how far the money actually goes
#
# THE DIAGNOSIS IS FIVE ROLES; THE REPAIR IS TWO, and `557e1c20` says the first without saying the
# second. Every entry in `PROPOSAL_CUES` is concatenated into ONE `_complexity_hint` string, and
# that string reaches a prompt only through `collect_hint_cues(self, RESEARCHER_PROMPT_CUES)`.
# MEASURED on the first live probe that carried the cue: `propose` 46/52 and `repropose` 9/9 see a
# money figure; `plan` 0/49, `foresight_rank` 0/7 and `hyp_prioritize` 0/4 still do not.
#
# These tests exist so the sentence in `_cue_llm_budget`'s docstring cannot go quietly out of date
# in EITHER direction: adding a third splice site reddens the first test (the docstring must then
# say so), and removing `_complexity_hint` from the splice set reddens the second (the cue would
# then reach nothing at all, which is the state this whole change was built to end).
import ast  # noqa: E402

from _source_scan import PKG, iter_trees  # noqa: E402
from looplab.agents.roles import RESEARCHER_PROMPT_CUES  # noqa: E402


def _cue_splice_sites():
    """(module, enclosing function) for every `collect_hint_cues(...)` call under `looplab/`.

    Through `_source_scan.iter_trees`, not a private walk: `test_source_scan_helper::
    test_no_guard_test_re_derives_the_walk` refuses a guard test that rglobs the package itself,
    because the copies had already diverged on decoding and a plain-`utf-8` read dies on the one
    BOM'd source in the tree rather than reporting a finding.
    """
    out = set()
    for path, tree in iter_trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                        and inner.func.id == "collect_hint_cues"):
                    out.add((str(path.relative_to(PKG.parent)), node.name))
    return out


def test_the_money_reaches_propose_and_repropose_and_nothing_else():
    """`repropose` is `propose` called a second time (`novelty.py::_repropose_with_feedback`), so
    the two splice sites below ARE the whole reach. `plan` is the Developer's own sub-phase and
    `foresight_rank`/`hyp_prioritize` are the foresight panel's own client; none of the three
    builds its prompt from a Researcher hint attribute."""
    assert _cue_splice_sites() == {
        ("looplab/agents/roles.py", "propose"),          # LLMResearcher
        ("looplab/agents/agent.py", "propose"),          # ToolUsingResearcher
    }, ("the set of prompts that splice the engine's cues changed — `_cue_llm_budget`'s docstring "
        "states which roles see the money and must move with it")


def test_the_carrier_is_still_in_the_splice_set():
    """A cue in `PROPOSAL_CUES` that is not carried by `_complexity_hint` reaches nobody at all."""
    assert "_complexity_hint" in RESEARCHER_PROMPT_CUES


def test_the_docstring_does_not_claim_the_three_it_does_not_reach():
    """The correction is part of the record, not a comment somebody may tidy away."""
    doc = ProposalCuesMixin._cue_llm_budget.__doc__
    assert "TWO of those five" in doc
    for role in ("plan", "foresight_rank", "hyp_prioritize"):
        assert role in doc, f"the docstring never says {role} is out of reach"
