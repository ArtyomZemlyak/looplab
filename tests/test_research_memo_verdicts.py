"""The deep-research memo's verifier verdicts must REACH the role that reads the memo.

Background, measured over every `research_completed` row in `runs/` on 2026-08-16: 102 memos, 98
carrying a `verification` block, **0** of them carrying a `summary` key, 98 carrying none, 16 with
every verdict `unsupported`. `tools/run_tools.py::RunTools._research_memo` ended with

    ver = m.get("verification")
    if isinstance(ver, dict) and ver.get("summary"):
        parts.append("Verifier: " + str(ver["summary"]).strip())

so it keyed on a field the writer has never produced — dead from the commit that added it
(`f180c986`, 2026-07-10, whose writer already returned `{verdicts, method, unsupported}`) — and not
one verifier verdict ever reached a role through that tool. What it cost is on the record in
`runs/rubertlite-dr-unified-v8`: its `at_node: 0` memo carries `total_verdicts: 8, unsupported: 8`,
the first of them `unsupported / "cited experiments do not exist: [9]"` against a claim quoting
`recall@100=0.8776` — a node id belonging to a DIFFERENT run — and that number became the run's
stated anchor anyway.

Two tiers, deliberately (CLAUDE.md's guard-test ladder):

  * TIER 1 — drive the property on v8's REAL recorded memo (`tests/data/v8_research_memo.json`,
    copied verbatim out of that run's event log and put through the SAME canonicalizer replay
    applies). A test that only pinned the new key would not have caught the old defect either, so
    the assertion is behavioural: a role reading that memo comes away knowing the 0.8776 claim is
    unsupported and WHY — and knowing it inside the 4,000-char window the agent layer actually
    delivers, because on that memo the `Claims` section used to begin at char 3,889.
  * TIER 3 — the GENERAL defect: a reader keying on a field no writer produces. Both key sets are
    re-derived by AST from the writers' own source, so a key added on one side without the other
    goes red instead of going quiet.
"""
from __future__ import annotations

import ast
import inspect
import json
import types
from pathlib import Path

import pytest

from looplab.core.advisory_payloads import (VERDICT_UNVERIFIED, VERIFICATION_BLOCK_KEYS,
                                            memo_verification_view,
                                            sanitize_research_memo_payload)
from looplab.tools.run_tools import RunTools

_DATA = Path(__file__).parent / "data"
_V8_MEMO = _DATA / "v8_research_memo.json"
# The claim whose number anchored the whole run, and the verifier's own reason for refusing it.
_V8_ANCHOR = "recall@100=0.8776"
_V8_ANCHOR_NOTE = "cited experiments do not exist: [9]"
_RESULT_CAP = 4_000                      # `tools/_base.RESULT_CAP`; re-asserted below, not assumed


def _render(memo: dict, section: str = "") -> str:
    """Render exactly as production does: fold-time canonicalizer, then the real tool surface.

    `section` moved into the contract on 2026-08-19 (see `_research_memo`): the memo no longer fits
    one tool result and is addressable a section at a time, with the default OVERVIEW naming the
    calls that return the rest. The tests below that ask for `"claims"` used to read the same rows
    off the single string — they are RE-POINTED, and each still asserts the property it always
    asserted rather than the position the rows used to sit at.
    """
    clean = sanitize_research_memo_payload(memo, add_receipts=False)
    state = types.SimpleNamespace(research=[clean])
    tool = RunTools()
    tool.bind_state(state)
    return tool.execute("read_research_memo", {"section": section} if section else {})


# --------------------------------------------------------------------------------------------
# TIER 1 — the property, on the run's own recorded payload
# --------------------------------------------------------------------------------------------

def test_result_cap_is_still_what_this_file_assumes():
    """The window assertions below are only meaningful against the real cap."""
    from looplab.tools._base import RESULT_CAP
    assert RESULT_CAP == _RESULT_CAP, "RESULT_CAP moved — re-derive the window assertions"


def test_v8_memo_fixture_is_the_recorded_payload():
    memo = json.loads(_V8_MEMO.read_text(encoding="utf-8"))
    ver = memo["verification"]
    assert memo["at_node"] == 0 and memo["trigger"] == "run_start"
    assert (ver["total_verdicts"], ver["unsupported"]) == (8, 8)
    assert "summary" not in ver, "the fixture must keep the shape the writer really produced"
    first = ver["verdicts"][0]
    assert first["verdict"] == "unsupported" and first["note"] == _V8_ANCHOR_NOTE
    assert _V8_ANCHOR in first["statement"]
    assert memo["claims"][0]["node_ids"] == [9]


def test_role_reading_v8_memo_learns_the_anchor_is_unsupported():
    """THE property. Not 'a key is rendered' — that the refusal reaches the reader with the number."""
    out = _render(json.loads(_V8_MEMO.read_text(encoding="utf-8")))
    assert _V8_ANCHOR in out
    assert "UNSUPPORTED" in out
    assert _V8_ANCHOR_NOTE in out, "the reader is told the verdict but not why"
    # the counts, so 'one bad claim' cannot read as 'the memo checked out'
    assert "8 of 8 claims checked" in out and "8 UNSUPPORTED" in out


def test_the_refusal_survives_the_agent_layer_truncation():
    """Head-keep at RESULT_CAP is what a role really receives; the tail is not delivered.

    RE-POINTED 2026-08-19 and the property is now STRONGER, not moved. The old render was a single
    7,862-char string on this memo and relied on the refusal being early enough to beat a blind head
    cut at 3,978; every section past that cut was gone with no receipt. The tool now bounds its own
    answer, so the assertion is that the loop's cut does not bite AT ALL — `_cap_tool_result` is a
    no-op on every section — while the refusal still arrives with the number.
    """
    from looplab.agents.tool_loop import _cap_tool_result
    memo = json.loads(_V8_MEMO.read_text(encoding="utf-8"))
    for section in ("", "directions", "findings", "claims", "summary"):
        out = _render(memo, section)
        assert len(out) <= _RESULT_CAP, f"section {section!r} still overruns the loop's cap"
        assert _cap_tool_result(out) == out, f"section {section!r} is still cut by the agent layer"
    delivered = _render(memo)
    assert "UNSUPPORTED" in delivered and _V8_ANCHOR_NOTE in delivered
    # ...and the refusal leads whatever else the overview carries.
    for later in ("Recommended directions:", "Summary:"):
        if later in delivered:
            assert delivered.index("UNSUPPORTED") < delivered.index(later), \
                f"the verdict must precede {later!r}"


def test_the_conclusions_reach_the_reader_and_the_rest_is_named():
    """THE fix, driven on the memo the incident is recorded against.

    Measured over all 90 memos in `runs/` on 2026-08-19 against the pre-change renderer: 89 render
    over the 4,000-char cap, a median 5,180 chars are discarded, and `Recommended directions` — the
    only section a caller can act on — begins PAST the cut in 89 of the 89 memos that have one. In
    the real traces, of 375 recorded `read_research_memo` calls 362 rendered over the cap and 194 of
    the 212 whose recorded render still shows a directions section have it starting past the cut.
    So the run paid for the review and threw away its conclusions in the delivery path.

    The fix is NOT a bigger cap (the cap is the loop's shared bounded-output contract, and a memo
    that fits is still the wall of text the operator complained about). It is what is KEPT: the
    directions ride the default answer in full, and everything left out is named beside the call
    that returns it.
    """
    from looplab.agents.tool_loop import _cap_tool_result
    memo = json.loads(_V8_MEMO.read_text(encoding="utf-8"))
    directions = [str(d).strip() for d in memo["recommended_directions"] if str(d).strip()]
    assert directions, "fixture no longer carries directions — re-derive this test"
    # DELIVERED, not rendered. The pre-change render carried all four directions too — at chars
    # 4,700-7,800 of a string the agent layer cut at 3,978, which is the whole defect.
    out = _cap_tool_result(_render(memo))
    for direction in directions:
        assert direction in out, "the memo's conclusion did not reach the reader"
    # Everything the bounded answer did NOT carry is named WITH the call that carries it, and that
    # call is one the caller has not already spent (`tools/log_tools.py`'s rule 3).
    assert "NOT IN THIS ANSWER:" in out
    for section in ("findings", "claims", "summary"):
        assert f'read_research_memo(section="{section}")' in out, section
        assert _render(memo, section).strip(), f"the named remedy {section!r} returns nothing"


def test_every_claim_carries_its_own_verdict():
    out = _render(json.loads(_V8_MEMO.read_text(encoding="utf-8")), "claims")
    body = out[out.index("Claims (with evidence"):]
    rows = [line for line in body.splitlines() if line.startswith("  - ")]
    assert len(rows) == 8
    assert all(row.lstrip("  - ").startswith("[UNSUPPORTED]") for row in rows), rows


def test_an_unsupported_claim_is_still_rendered_in_full():
    """A bad CITATION is not a false claim: 45 of 45 deterministic verdicts in `runs/` are
    `unsupported` about a footnote (`no evidence cited`, `cited experiments do not exist`). Hiding
    the statement would suppress legitimate findings, so the refusal LEADS it and never replaces it."""
    memo = json.loads(_V8_MEMO.read_text(encoding="utf-8"))
    out = _render(memo, "claims")
    for claim in memo["claims"]:
        assert claim["statement"].strip() in out, "a claim was withheld rather than qualified"


# --------------------------------------------------------------------------------------------
# TIER 1 — absence and malformation are SAID, not inferred
# --------------------------------------------------------------------------------------------

def test_a_memo_with_no_verification_block_says_so():
    """4 of the 102 memos in `runs/` carry no block at all. Silence would read as 'checked, fine' —
    which is the same misread, one layer up, as keying on a field nobody writes."""
    memo = {"summary": "s", "claims": [{"statement": "the leader is overfit"}], "at_node": 3}
    out = _render(memo)
    assert "Verifier: NOT RUN" in out
    assert "not a pass" in out
    # The absence rides EVERY section, not only the overview — a role that jumps straight to the
    # claims must not read an unchecked memo as a checked one.
    claims = _render(memo, "claims")
    assert "Verifier: NOT RUN" in claims
    assert "the leader is overfit" in claims, "the memo must still be delivered in full"


def test_a_malformed_block_is_not_reported_as_an_absent_one():
    view = memo_verification_view({"claims": [{"statement": "x"}], "verification": {"summary": "ok"}})
    assert view["status"] == "malformed"
    out = _render({"claims": [{"statement": "x"}], "verification": {"summary": "ok"}})
    assert "UNREADABLE" in out and "NOT RUN" not in out


def test_a_claim_with_no_verdict_reads_unverified_not_clean():
    """A bounded check (`omitted_verdicts`) must not let an unchecked claim pass as checked."""
    memo = {"claims": [{"statement": "alpha"}, {"statement": "beta"}],
            "verification": {"verdicts": [{"statement": "alpha", "verdict": "supported",
                                           "note": "node 1 shows it"}],
                             "method": "llm", "unsupported": 0,
                             "total_verdicts": 2, "omitted_verdicts": 1}}
    view = memo_verification_view(memo)
    assert [row["verdict"] for row in view["rows"]] == ["supported", VERDICT_UNVERIFIED]
    assert "UNVERIFIED" in _render(memo, "claims")
    # The bounded-check receipt is on the OVERVIEW too — it is part of the verifier head, which
    # every section carries, so no page can present a partial check as a complete one.
    assert "1 of 2 claims were never checked" in _render(memo)


def test_a_misaligned_verdict_is_never_printed_under_another_claim():
    """The join is positional AND statement-equal — `engine/lessons.py` and the operator's own memo
    card apply the same rule. A block that has slipped by one must not attribute one claim's
    refusal to its neighbour's text."""
    memo = {"claims": [{"statement": "alpha"}, {"statement": "beta"}],
            "verification": {"verdicts": [
                {"statement": "beta", "verdict": "unsupported", "note": "no evidence cited"},
                {"statement": "alpha", "verdict": "supported", "note": "sibling node proves it"}],
                "method": "llm", "unsupported": 1, "total_verdicts": 2, "omitted_verdicts": 0}}
    view = memo_verification_view(memo)
    assert [row["verdict"] for row in view["rows"]] == [VERDICT_UNVERIFIED, VERDICT_UNVERIFIED]
    assert all(row["note"] == "verification alignment mismatch" for row in view["rows"])
    out = _render(memo) + _render(memo, "claims")
    assert "sibling node proves it" not in out, \
        "a verdict's reason was printed under the wrong claim"


def test_render_is_pure_and_never_raises_on_junk():
    """The provider contract: `execute` soft-fails. Nothing here may take down an agent phase."""
    for junk in ({"verification": []}, {"verification": {"verdicts": "no"}},
                 {"claims": [{"statement": "x"}], "verification": {"verdicts": [None, 7]}},
                 {"claims": "nope", "verification": {"verdicts": [{}]}}):
        for section in ("", "directions", "findings", "claims", "summary", "not-a-section"):
            assert isinstance(_render(junk, section), str)


# --------------------------------------------------------------------------------------------
# TIER 3 — the GENERAL defect: a reader keying on a field no writer produces
# --------------------------------------------------------------------------------------------

def _returned_dict_keys(func, *, require_literal: bool = True) -> set[str]:
    """Every string key of every dict LITERAL this function returns, resolved as AST."""
    tree = ast.parse(inspect.getsource(func))
    keys: set[str] = set()
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            found = True
            for key in node.value.keys:
                assert isinstance(key, ast.Constant) and isinstance(key.value, str), \
                    f"{func.__qualname__} returns a dict with a non-literal key — unscannable"
                keys.add(key.value)
    assert found or not require_literal, f"{func.__qualname__} returns no dict literal"
    return keys


def _keys_read_off(func, param: str) -> set[str]:
    """Every string literal used as `<param>.get("k")` / `<param>["k"]` inside `func`."""
    tree = ast.parse(inspect.getsource(func))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == param and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == param and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            keys.add(node.slice.value)
    return keys


def test_the_two_writers_of_the_verification_block_agree():
    """`trust/memo_verify.py::verify_memo` mints it; `advisory_payloads._verification` re-emits it at
    both the write and the replay boundary, so the SECOND one is what any reader actually sees. A key
    the origin adds without the sanitizer is a key that never survives to a reader."""
    from looplab.core import advisory_payloads
    from looplab.trust.memo_verify import verify_memo
    origin = _returned_dict_keys(verify_memo)
    sanitized = _returned_dict_keys(advisory_payloads._verification)
    assert origin == sanitized == set(VERIFICATION_BLOCK_KEYS), (origin, sanitized)


def test_the_memo_reader_keys_only_on_fields_a_writer_produces():
    """THE GENERAL GUARD, and the one that would have caught the original defect.

    `ver.get("summary")` in the old renderer read a key neither writer emits. This re-derives both
    sides from source: every key `memo_verification_view` reads off the block must be one the
    sanitizer writes. It is deliberately not a pin on the NEW key names — a test that only asserted
    `"verdicts" in source` would have passed just as happily beside the dead `summary` branch.
    """
    from looplab.core import advisory_payloads
    writer_keys = _returned_dict_keys(advisory_payloads._verification)
    reader_keys = _keys_read_off(advisory_payloads.memo_verification_view, "block")
    unknown = reader_keys - writer_keys
    assert not unknown, (
        f"memo_verification_view reads {sorted(unknown)} off the verification block, which "
        f"_verification never writes (it writes {sorted(writer_keys)}). A reader keyed on a field "
        "no writer produces is silently empty forever — that is the defect this guard exists for.")
    assert reader_keys, "the reader stopped reading the block at all"


def test_the_renderer_goes_through_the_view_and_never_re_derives_the_block():
    """One derivation, beside the writer. If the renderer reaches into `verification` itself, this
    guard's binding to the writer is void and the next dead key is invisible again."""
    source = inspect.getsource(RunTools._research_memo)
    assert "memo_verification_view(" in source
    assert '"verification"' not in source and "'verification'" not in source, \
        "the renderer re-derives the verification block instead of using the checked reader"


def test_the_tool_contract_promises_only_what_it_renders():
    """A docstring is a contract in this repo. The old one promised '(and any verifier verdicts)'
    beside a branch that could not fire."""
    memo = json.loads(_V8_MEMO.read_text(encoding="utf-8"))
    spec = next(s for s in RunTools().specs() if s["function"]["name"] == "read_research_memo")
    assert "verdict" in spec["function"]["description"].lower()
    assert "verdict" in RunTools._research_memo.__doc__.lower()
    # ... and the promise is kept in the answer, not only in the description. The description now
    # promises SECTIONS too, so every name it advertises must be one the tool really serves.
    advertised = spec["function"]["parameters"]["properties"]["section"]["enum"]
    for section in advertised:
        rendered = _render(memo, section)
        assert "verifier" in rendered.lower(), section
        assert not rendered.startswith("(unknown memo section"), section
    assert "verifier" in _render(memo).lower()


@pytest.mark.parametrize("verdict", ["supported", "unsupported", "unclear", "cited"])
def test_every_verdict_the_writer_can_emit_is_rendered(verdict):
    """`cited` is reachable whenever the LLM rubric pass is unwired or fails (`memo_verify.py`), and
    an unrendered vocabulary member is how a reader silently drops a whole class of answer."""
    memo = {"claims": [{"statement": "alpha beta gamma"}],
            "verification": {"verdicts": [{"statement": "alpha beta gamma", "verdict": verdict,
                                           "note": "the recorded reason"}],
                             "method": "llm", "unsupported": int(verdict == "unsupported"),
                             "total_verdicts": 1, "omitted_verdicts": 0}}
    out = _render(memo, "claims")
    assert verdict.upper() in out
    assert "the recorded reason" in out
