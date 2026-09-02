"""A repair that DESCRIBES an edit it never made gets one chance to actually make it.

MEASURED on `runs/e5small-dr-unified-v8` node 1. Two inline-repair sessions, 03:29:49 -> 04:20:44,
51 minutes, 193 spans, 108 tool calls:

    read_file 50   run_probe 26   grep 21   others 11
    edit_file 0    write_file 0   delete_file 0   declare_stages 0

and then an emit reading, verbatim: "FIX: changed mine_stage.py so the pass/fail decision asserts on
the ARTIFACT ... Updated looplab_stages.json expect.assert to match." `node_repaired.changed` is
`[]`. The DIAGNOSIS was correct — it matched the engine's own `check_false_positive` and the evidence
was real (negatives.parquet held 2,732,976 rows for 2,732,976 unique queries) — and only the
application was missing. Two such attempts hit `INERT_REPAIR_LIMIT` and abandoned a node that held
valid mined data and a sound plan to unblock itself.

THE WIRING WAS NOT THE PROBLEM and this was checked before the fix: `_run` composes
`[write, EnvInspectTools()] + scouts` on the repair path, so `edit_file`/`write_file` were present
and unused. Nor was it the probe: 0 of the 26 `run_probe` calls contain a write in their code, so the
model was not editing in the disposable sandbox and losing it.

WHY THIS IS NOT THE `inert` VERDICT'S JOB. `inert` is decided on BYTES with the rationale unread so
that no wording can steer the one verdict the loop acts on; `RepairVerification` even leaves `unmet`
empty for it deliberately, "because that verdict is a statement about bytes and attaching a
text-derived list to it would invite a reader to treat the two tiers as one". This rung reads the
text INSIDE the session, where steering is the point, and can never touch a durable verdict.

Every assertion has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import pytest

from looplab.engine.repair_verify import claimed_tokens, repair_claimed_without_writing

# The real v8 node 1 emit, truncated at the sentence — the shape is the point, not the prose.
_V8_SUMMARY = ("REPAIR DECISION: the mine stage's coverage CHECK was wrong. FIX: changed "
               "mine_stage.py so the pass/fail decision asserts on the ARTIFACT, and updated "
               "looplab_stages.json expect.assert to match.")


def test_the_v8_emit_is_bounced_and_names_what_it_claimed():
    """MUTATION: return "" unconditionally -> the session ends, the node dies as it really did."""
    out = repair_claimed_without_writing(_V8_SUMMARY, wrote=False)

    assert out, "a claimed-but-unwritten fix must be refused"
    assert "mine_stage.py" in out, "the bounce must quote the claim back, not scold generically"
    assert "edit_file" in out and "write_file" in out, "it must name the tools that were available"
    assert "no code change is needed" in out, (
        "MUTATION: drop the second option -> the model is pushed into a cosmetic edit to escape")


def test_a_session_that_WROTE_is_never_bounced():
    """The byte fact wins. MUTATION: ignore `wrote` -> every repair is bounced once, including good
    ones, costing a turn on the exact sessions that behaved."""
    assert repair_claimed_without_writing(_V8_SUMMARY, wrote=True) == ""


def test_an_honest_no_change_answer_is_LEFT_ALONE():
    """Refusing to edit can be the right answer and must not be punished.

    MUTATION: bounce whenever `wrote` is False -> a correct "the check was wrong, the artifact is
    valid, nothing to change here" answer is rejected and the model edits something to escape.
    """
    for honest in ("No code change is needed; the mining artifact is valid.",
                   "The failure is environmental and nothing in the workspace should change.",
                   "",
                   None):
        assert repair_claimed_without_writing(honest, wrote=False) == "", honest


def test_the_bounce_rides_on_the_SAME_claim_vocabulary_the_verdict_uses():
    """One extractor, not two. MUTATION: hand-roll a second token scanner here and the pre-emit rung
    and `unmet` drift apart — the defect docs/0.8 records four times over."""
    claims = claimed_tokens(_V8_SUMMARY)

    assert claims, "the fixture must name something concrete or the test is vacuous"
    out = repair_claimed_without_writing(_V8_SUMMARY, wrote=False)
    assert any(c in out for c in claims[:6])


def test_it_is_total_over_junk():
    """It runs on the emit path of a session that already cost minutes; it may never raise.

    MUTATION: drop the guards -> a non-string summary raises inside the emit and loses the session.
    """
    for junk in (None, 123, [], {}, object()):
        assert repair_claimed_without_writing(junk, wrote=False) == ""
        assert repair_claimed_without_writing(junk, wrote=True) == ""


def test_the_verdict_tier_is_untouched():
    """The pre-emit rung must not have leaked into the byte-decided verdict.

    MUTATION: make `verify_repair` consult the rationale on the empty-change-set branch -> a model
    can write its way out of `inert` and `INERT_REPAIR_LIMIT` stops bounding the repair chain.
    """
    from looplab.engine.repair_verify import REPAIR_INERT, verify_repair

    v = verify_repair(_V8_SUMMARY, changed=(), deleted=(), code_changed=False, region="")
    assert v.verdict == REPAIR_INERT
    assert v.claims == (), "inert carries no text-derived list, by design"
    assert v.unmet == ()
    assert v.actionable is True, "inert is still the verdict the loop may act on"


# --- the FORCED-emit paths -------------------------------------------------------------------
# The rung above is delivered through `drive_tool_loop`'s `validate=` seam, and until this was
# driven, NONE of the forced-emit exits delivered it. `_accept_forced` called `validate`, discarded
# the returned string, and returned `(False, None)`; three of the four exits then `break` straight to
# `fallback`, which the repair path binds to `lambda m: ""`. So on the exit that MEASURABLY dominates
# the corpus — 12 of the 12 inert repairs ran past `session_time_budget_s` — the rung fired, consumed
# its one shot, said nothing to the model, and additionally destroyed the summary and the
# `rollback_stage` the emit carried.

def _loop(monkeypatch, *, forced_summary, exit_kind, validate, terminal_salvage=False):
    """Drive the REAL `drive_tool_loop` to one of its forced-emit exits.

    `terminal_salvage` mirrors the production kwarg and DEFAULTS FALSE exactly as the loop does, so
    a test that wants the repair session's behaviour has to ask for it the way `repo_developer`
    does — the one caller that opts in.
    """
    from looplab.agents import tool_loop

    monkeypatch.setattr(tool_loop, "_force_emit",
                        lambda *a, **k: {"summary": forced_summary, "rollback_stage": "train"})

    seen: list = []

    class _Client:
        def chat(self, messages, tool_specs, tool_choice="auto"):
            seen.append(messages[-1].get("content", ""))
            return {"content": "here is my prose answer", "tool_calls": []}

    spec = {"type": "function", "function": {"name": "done", "parameters": {}}}
    out = tool_loop.drive_tool_loop(
        _Client(), None, [{"role": "user", "content": "go"}], spec,
        max_turns=(1 if exit_kind == "exhausted" else 4),
        finalize=lambda a: (a or {}).get("summary", ""),
        fallback=lambda m: "", validate=validate, terminal_salvage=terminal_salvage)
    return out, seen


def test_budget_exhaustion_keeps_the_repair_instead_of_discarding_it(monkeypatch):
    """A terminal salvage has no turn left to spend on a bounce, so it must ACCEPT.

    MUTATION: validate on the exhausted exit -> `fallback` returns "", `repair_verdict` is empty,
    `last_rollback_stage` is never set from the emit, and `is_developer_stuck` can never fire. That
    is strictly worse than an unverified summary, which `inert`/`unmet` already grade on bytes.
    """
    _bounce = lambda a: repair_claimed_without_writing((a or {}).get("summary", ""), wrote=False)

    out, _ = _loop(monkeypatch, forced_summary=_V8_SUMMARY, exit_kind="exhausted",
                   validate=_bounce, terminal_salvage=True)

    assert out == _V8_SUMMARY, "the terminal salvage must keep the emit it paid for"

    # ...and the OTHER half of the same rule, since 2026-08-30: the skip is a per-call POLICY that
    # defaults FALSE, so every other caller keeps its validator on every exit. The stages session's
    # `validate` is the operator's wall budget and the manifest-collision fence; a blanket skip
    # would disable both on any exit with no turn left, and `_finalize` would persist whatever was
    # merely shape-valid. Asserting only the opt-in is how that default flips without a red test.
    out_default, _ = _loop(monkeypatch, forced_summary=_V8_SUMMARY, exit_kind="exhausted",
                           validate=_bounce)

    assert out_default == "", (
        "a caller that did not opt in must still be validated on the exhausted exit")


def test_the_prose_exit_delivers_the_REFUSAL_not_a_generic_nudge(monkeypatch):
    """The one exit that loops back is the one that can honour a bounce — and it must say WHY.

    MUTATION: nudge with `nudge_prompt` -> the model is told only "call emit again", never that it
    claimed an edit it did not make, so the one chance the rung buys is spent on a turn that cannot
    use it.
    """
    shots: list = []

    def _validate(args):
        if shots:
            return None                      # one-shot, exactly like `_validate_repair`
        shots.append(True)
        return repair_claimed_without_writing((args or {}).get("summary", ""), wrote=False)

    out, seen = _loop(monkeypatch, forced_summary=_V8_SUMMARY, exit_kind="prose", validate=_validate)

    assert any("mine_stage.py" in m for m in seen), (
        "the refusal must reach the model; a generic nudge throws the reason away")
    assert any("edit_file" in m for m in seen)
    assert out == _V8_SUMMARY, "after the bounce the retry's emit is accepted"
