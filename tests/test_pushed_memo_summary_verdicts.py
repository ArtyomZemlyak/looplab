"""The memo summary PUSHED into a prompt must carry the verifier's own result.

THE OTHER HALF of `test_research_memo_verdicts.py`. That one fixed the PULL path — a role that
chooses to call `read_research_memo` now gets the per-claim verdicts. This one is the PUSH path:
`agents/roles.py::_state_brief` splices `state.research[-1]["summary"]` into a prompt with no tool
call needed, and it reaches THREE phases, not one.

THE SHARPEST FACT, and it is why a "the verdict is elsewhere in the payload" reading is wrong:
`trust/memo_verify.py::verify_memo` verifies `memo["claims"]` and returns `None` when a memo has
none. **It has never looked at `memo["summary"]` at any commit.** So the field this line pushes is
the one field of the memo that no verifier has ever checked, and until 2026-08-16 the verdicts on
its claims could not be pulled either.

MEASURED, on `runs/rubertlite-dr-unified-v8`'s own `spans.jsonl` (2026-08-16, run live):

  * the pushed line reached **293 real prompts** in three phases — `propose` 269, `triage` 20,
    `repair_critic` 4 — which is exactly the reachable subset of `_state_brief`'s five call sites
    (`node_build._choose_action`, the fifth, needs `agent_drives_actions` and never fired in v8);
  * **0 of those 293 whole prompts** contain the word `Verifier` or the word `unsupported`;
  * 52 of them carry `~0.88` inside the pushed line — the rounded `rubert-dr-0807` plateau, from a
    run `engine/eval_contract.py` reports as a DIFFERENT contract (different eval command, different
    declared paths) — while the memo behind them records `total_verdicts: 8, unsupported: 8`.

AND ONE CORRECTION TO THE RESIDUE NOTE THIS CHANGE CLOSES (docs/BACKLOG.md §0.7 said "11 of v8's 14
memo summaries contain 0.8776"). That is true of the FULL summaries — 11 of 15 today — and false of
what is pushed: `_state_brief` takes a 300-char head of the whitespace-collapsed summary, and the
literal `0.8776` survives that cut in **0 of 15**. The carrier was the rounded `~0.88`. Both halves
are driven below, because a fix aimed at the wrong literal would have been vacuous.

Corpus-wide (103 `research_completed` rows in `runs/`): 100 memos are pushable, 81 push a decimal
number, 76 push one from a memo carrying a non-`supported` verdict, and 13 push a >=3-decimal number
that is a node metric of a provably different-contract run and of no node of their own — spread over
FIVE runs (v6 7, 0807 2, v7 2, v8 1, lt_dataset 1). Not one run's accident.

Tier 1 throughout (CLAUDE.md's ladder): every assertion below drives a REAL `LLMResearcher.propose`
or a real `Engine` construction and reads the prompt that actually left. The negative control is
the whole point of the flag — `memo_verdict_cue=false` must reproduce the historical line byte for
byte, and a fully-supported memo on this run's own contract must lose nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from looplab.agents.roles import RESEARCHER_HINT_ATTRS, LLMResearcher, _state_brief
from looplab.core.advisory_payloads import (memo_verdict_cue, sanitize_research_memo_payload,
                                            verdict_tally)
from looplab.core.models import Idea, Node, NodeStatus, RunState

_V8_MEMO = Path(__file__).parent / "data" / "v8_research_memo.json"
_V8_ANCHOR = "0.8776"
# The number that ACTUALLY reached v8's prompts, in the sentence the run adopted as its direction.
_V8_PUSHED_ANCHOR = "climb from the known ~0.88 plateau"
# `_state_brief`'s own head cut on the summary. Re-derived from the rendered line, never assumed.
_SUMMARY_HEAD = 300
_HISTORICAL_PREFIX = "Latest deep-research takeaway: "
_TAIL = " (full findings/claims are recorded; the read_research_memo tool returns them)."


class _ToolEmitClient:
    """`parse_structured(tool_call)` fake: records the messages it was handed, emits a valid Idea."""

    def __init__(self):
        self.messages = None

    def complete_tool(self, messages, json_schema):
        if self.messages is None:
            self.messages = [dict(m) for m in messages]
        return {"operator": "draft", "params": {}, "rationale": "r"}


def _v8_memo() -> dict:
    """v8's real recorded memo, through the SAME canonicalizer the fold applies at replay."""
    return sanitize_research_memo_payload(json.loads(_V8_MEMO.read_text(encoding="utf-8")),
                                          add_receipts=False)


def _supported_memo() -> dict:
    """The NEGATIVE CONTROL's memo: same shape, every claim `supported`, every citation in-run.

    Deliberately built from a claim about THIS run's own node, so the control is honest about both
    halves of the incident — the verdict AND the evaluation contract. Nothing about it should read
    any differently from the day before this change, apart from the verifier saying so."""
    statement = "Node 0 reached recall@100=0.7620 with the DCL nll_cos recipe."
    return sanitize_research_memo_payload({
        "summary": "This run's own node 0 measured recall@100=0.7620; the next lever is epoch scaling "
                   "on that exact configuration, which nothing in the portfolio has tried yet.",
        "claims": [{"statement": statement, "node_ids": [0]}],
        "verification": {"verdicts": [{"statement": statement, "verdict": "supported",
                                       "note": "node 0 records this metric"}],
                         "method": "llm", "unsupported": 0,
                         "total_verdicts": 1, "omitted_verdicts": 0},
    }, add_receipts=False)


def _state_with(memo: dict) -> RunState:
    state = RunState(goal="maximize recall@100", direction="max")
    state.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                           status=NodeStatus.evaluated, metric=0.762048)}
    state.research = [memo]
    return state


def _takeaway_line(prompt: str) -> str:
    line = next((ln for ln in prompt.splitlines()
                 if ln.startswith("Latest deep-research takeaway")), None)
    assert line is not None, "the pushed memo line is missing from the prompt entirely"
    return line


def _propose_prompt(state: RunState, *, cue: bool) -> str:
    """The USER turn a REAL `LLMResearcher.propose` actually sent — not a `_state_brief` call."""
    client = _ToolEmitClient()
    researcher = LLMResearcher(client)
    setattr(researcher, "_memo_verdict_cue", cue)
    researcher.propose(state, None)
    user = [m for m in client.messages if m["role"] == "user"]
    assert len(user) == 1
    return user[0]["content"]


# ------------------------------------------------------------------ the incident, driven end to end
def test_v8s_own_memo_reaches_a_researcher_with_its_verifier_result_attached():
    """A Researcher handed v8's REAL memo is told the memo's claims were all refused, and that the
    summary it is about to read is outside the check entirely."""
    prompt = _propose_prompt(_state_with(_v8_memo()), cue=True)
    line = _takeaway_line(prompt)

    # The plateau target the run adopted is still delivered — this annotates, it never withholds.
    assert _V8_PUSHED_ANCHOR in line
    # ...and it no longer arrives unqualified.
    assert "VERIFIER" in line
    assert "8 UNSUPPORTED" in line, line
    assert "nothing verifies the summary itself" in line
    # The verdict LEADS the number, for the same reason the pull path's does: a reader that skims
    # must take the refusal with the target and not after it.
    assert line.index("UNSUPPORTED") < line.index(_V8_PUSHED_ANCHOR)


def test_the_qualification_reaches_the_crash_triage_and_repair_critic_briefs_too(tmp_path):
    """`propose` is 269 of the 293 prompts; `triage` (20) and `repair_critic` (4) are the two that
    carried the number into node 9's repair rationale.

    Driven through the REAL `Engine` methods with a recording judge, not through a `_state_brief`
    call with the keyword typed in by hand — a test that passes `memo_verdicts=True` itself proves
    the renderer and nothing about the wiring, and stays green while both call sites drop the flag.
    """
    from tests.factories import make_engine

    seen = {}

    def _triage(node, tagged, attempt, *, state=None, brief="", **kw):
        seen["triage"] = brief
        return {"action": "reject", "rationale": "r"}

    def _critic(node, *, trajectory="", attempt=0, brief="", state=None, **kw):
        seen["repair_critic"] = brief
        return {"action": "continue", "rationale": "r"}

    for cue, expected in ((True, "8 UNSUPPORTED"), (False, None)):
        seen.clear()
        engine = make_engine(tmp_path / f"triage-{cue}", memo_verdict_cue=cue)
        state = _state_with(_v8_memo())
        node = state.nodes[0]
        engine.researcher.repair_critic = _critic

        engine._ask_triage(_triage, state, node, "boom", 1, "crash", [], 0, 3)
        engine._repair_critic(state, node,
                              [{"attempt": 1, "rationale": "x", "files": ["a.py"]}], 1)

        assert set(seen) == {"triage", "repair_critic"}, seen
        for phase, brief in seen.items():
            line = _takeaway_line(brief)
            if expected is None:
                assert line.startswith(_HISTORICAL_PREFIX), (phase, line)
            else:
                assert expected in line, (phase, line)


def test_the_residue_note_named_a_literal_that_never_reached_a_prompt():
    """RE-DERIVED, because the fix would have been vacuous aimed at `0.8776`: that literal is in the
    memo's full summary and is cut by the 300-char head every time."""
    memo = _v8_memo()
    collapsed = " ".join(str(memo["summary"]).split())
    assert _V8_ANCHOR in collapsed                      # 11 of v8's 15 full summaries carry it
    assert _V8_ANCHOR not in collapsed[:_SUMMARY_HEAD]  # ...and 0 of 15 pushed windows do
    line = _takeaway_line(_propose_prompt(_state_with(memo), cue=True))
    assert _V8_ANCHOR not in line
    # The head cut is the prompt's, not this test's arithmetic: the delivered body IS that window.
    body = line.split("]: ", 1)[1][: -len(_TAIL)]
    assert body == collapsed[:_SUMMARY_HEAD]


# --------------------------------------------------------------------------- the negative controls
def test_off_reproduces_the_historical_line_byte_for_byte_on_both_memos():
    """A prompt is a contract. `memo_verdict_cue=false` must restore the exact bytes, and the whole
    REST of the brief must be untouched by the flag — the cue is one splice at one position."""
    for memo in (_v8_memo(), _supported_memo()):
        state = _state_with(memo)
        off = _propose_prompt(state, cue=False)
        on = _propose_prompt(state, cue=True)
        collapsed = " ".join(str(memo["summary"]).split())
        expected = _HISTORICAL_PREFIX + collapsed[:_SUMMARY_HEAD] + _TAIL
        assert _takeaway_line(off) == expected
        # Byte-identical everywhere except that ONE line.
        off_rest = [ln for ln in off.splitlines() if not ln.startswith("Latest deep-research")]
        on_rest = [ln for ln in on.splitlines() if not ln.startswith("Latest deep-research")]
        assert off_rest == on_rest


def test_a_supported_memo_on_this_runs_own_contract_loses_nothing():
    """The control the fix has to survive: a memo whose claims ARE grounded must arrive with its
    finding intact, its wording unchanged, and no word that would make a reader distrust it."""
    memo = _supported_memo()
    on = _takeaway_line(_propose_prompt(_state_with(memo), cue=True))
    off = _takeaway_line(_propose_prompt(_state_with(memo), cue=False))

    collapsed = " ".join(str(memo["summary"]).split())
    # The delivered SUMMARY BODY is byte-identical with the cue on: nothing was displaced to pay
    # for the qualifier, and no finding was truncated to make room.
    assert on.endswith(collapsed[:_SUMMARY_HEAD] + _TAIL)
    assert off.endswith(collapsed[:_SUMMARY_HEAD] + _TAIL)
    assert "1 supported" in on
    assert "UNSUPPORTED" not in on
    # It still says the summary is outside the check — that is true of a supported memo too, and a
    # cue that only appeared on bad news would be read as an alarm rather than as a status.
    assert "nothing verifies the summary itself" in on


def test_absence_and_an_unreadable_block_are_each_said_out_loud():
    """A missing check read as a passing one is this whole defect family; both get a sentence."""
    memo = _v8_memo()
    del memo["verification"]
    assert "NOT RUN" in _takeaway_line(_propose_prompt(_state_with(memo), cue=True))

    broken = _v8_memo()
    broken["verification"] = {"whatever": 1}
    line = _takeaway_line(_propose_prompt(_state_with(broken), cue=True))
    assert "UNREADABLE" in line and "NOT RUN" not in line
    # Neither withholds a byte of the memo.
    assert _V8_PUSHED_ANCHOR in line


# ----------------------------------------------------------------- the wiring, driven not asserted
def test_the_setting_reaches_a_real_researcher_and_a_real_engine(tmp_path):
    """The flag is useless if it dies at a wrapper or never leaves `Settings`. `_digest_cap`'s exact
    shape, so it must be in the forwarding registry AND set by the orchestrator."""
    assert "_memo_verdict_cue" in RESEARCHER_HINT_ATTRS

    from tests.factories import make_engine
    for value in (True, False):
        engine = make_engine(tmp_path / f"run-{value}", memo_verdict_cue=value)
        assert engine._memo_verdict_cue is value          # the engine-method call sites
        assert getattr(engine.researcher, "_memo_verdict_cue") is value   # the two propose paths


def test_every_state_brief_call_site_decides_the_cue_rather_than_inheriting_the_default():
    """TIER 3, and only for the residue the two tiers above cannot reach.

    `_state_brief` has FIVE call sites and `memo_verdicts` defaults to the HISTORICAL value, which is
    the right default for a prompt contract and the wrong one to inherit by accident: a site that
    omits the keyword silently keeps the unqualified line forever, and no behaviour goes red. Three
    of the five are driven above (both `propose` paths, `triage`+`repair_critic`); the fifth,
    `node_build._choose_action`, needs `agent_drives_actions` and produced 0 prompts in v8, so a
    behavioural test of it would be a test of the fixture. This is the AST scan that keeps a SIXTH
    site from joining silently — the same rule `RESEARCHER_HINT_ATTRS` gets one file over.
    """
    import ast

    seen = {}
    for module in ("looplab/agents/roles.py", "looplab/agents/agent.py",
                   "looplab/engine/crash_repair.py", "looplab/engine/node_build.py"):
        tree = ast.parse((Path(__file__).parents[1] / module).read_text(encoding="utf-8"))
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if name != "_state_brief":
                continue
            seen.setdefault(module, []).append({kw.arg for kw in call.keywords})

    assert sum(len(v) for v in seen.values()) == 5, seen
    for module, calls in seen.items():
        for kwargs in calls:
            assert "memo_verdicts" in kwargs, (
                f"a `_state_brief` call in {module} omits `memo_verdicts`, so it silently inherits "
                "the historical unqualified line and nothing goes red. Pass the resolved setting.")


def test_a_wrapper_chain_does_not_swallow_the_cue():
    """`forward_hints` mirrors the registry onto a delegate; a knob outside it silently dies at the
    first wrapper, which is how board prioritization was dead in the default config."""
    from looplab.agents.roles import forward_hints

    class _Inner:
        pass

    outer, inner = _Inner(), _Inner()
    outer._memo_verdict_cue = True
    forward_hints(outer, inner)
    assert inner._memo_verdict_cue is True


# --------------------------------------------------------------------- one vocabulary, two channels
def test_the_push_and_pull_channels_tally_the_same_block_in_the_same_words():
    """A role can see both surfaces in one prompt; two spellings of one count read as two checks."""
    import types

    from looplab.core.advisory_payloads import memo_verification_view
    from looplab.tools.run_tools import RunTools

    memo = _v8_memo()
    tool = RunTools()
    tool.bind_state(types.SimpleNamespace(research=[memo]))
    pulled = tool.execute("read_research_memo", {})
    tally = verdict_tally(memo_verification_view(memo)["counts"])

    assert tally == "8 UNSUPPORTED"
    assert tally in pulled                       # the tool's lead block
    assert tally in memo_verdict_cue(memo)       # the pushed line


def test_the_cue_is_bounded_and_costs_what_it_says():
    """It shares a prompt with a board, a digest and a cross-run pack. Measured over all 103 memos in
    `runs/` the widest cue is under 160 chars; pin the bound so a later reword cannot quietly grow
    into the budget `experiments_digest` is already scaled against."""
    for memo in (_v8_memo(), _supported_memo()):
        assert len(memo_verdict_cue(memo)) <= 160
    # It is ONE line and never introduces a newline into a brief assembled by `"\n".join(lines)`.
    assert "\n" not in memo_verdict_cue(_v8_memo())


def test_the_cue_never_raises_on_a_hostile_or_absent_payload():
    """It runs inside a prompt brief, which is best-effort by construction at three of its five call
    sites; a memo is model-authored text that survived a sanitizer, not a guaranteed shape."""
    for bad in (None, {}, {"verification": []}, {"verification": {"verdicts": "no"}},
                {"claims": [{"statement": "x"}], "verification": {"verdicts": [None]}}):
        out = memo_verdict_cue(bad)
        assert isinstance(out, str) and out.startswith(" [VERIFIER")


def test_no_number_is_stripped_or_rewritten_anywhere_in_the_pushed_text():
    """The rejected alternative, pinned: this change must never edit the model's own prose. Every
    numeric token in the delivered window is the one the memo wrote, in the same order."""
    memo = _v8_memo()
    collapsed = " ".join(str(memo["summary"]).split())[:_SUMMARY_HEAD]
    line = _takeaway_line(_propose_prompt(_state_with(memo), cue=True))
    body = line.split("]: ", 1)[1][: -len(_TAIL)]
    assert re.findall(r"\d+\.\d+", body) == re.findall(r"\d+\.\d+", collapsed)
    assert body == collapsed
