"""A verdict says WHICH QUESTION it answered, and the field survives the persist boundary.

`trust/memo_verify.py::verify_memo` merges two passes into one `verdicts` list — the deterministic
layer answers about the CITATION, the LLM layer about the SUPPORT — and both write `unsupported`.
`kind` is the field that tells them apart (`VERDICT_KINDS`), and `trust/verifier_routing.py` keys
on it: keyed on the word alone the router sent 233 of 237 memos on the box to one action.

The second half is a measured defect: `core/advisory_payloads.py::_verification` rebuilt every row
as exactly `{statement, verdict, note, evidence}`, so `kind` was dropped at the persist boundary
with nothing going red — the live `e5small-dr-unified-v14` showed 8 verdicts on disk, all
unstamped. Both writers are re-derived here, and the sanitizer is driven both ways.
"""
from __future__ import annotations

import ast
import inspect

from looplab.core import advisory_payloads as ap
from looplab.core.advisory_payloads import sanitize_research_memo_payload
from looplab.core.models import RunState
from looplab.trust import memo_verify
from looplab.trust.memo_verify import VERDICT_KINDS, check_claims


def test_the_vocabulary_is_ONE_set_across_the_package_boundary():
    """`core` may not import `trust`, so the set lives in core and trust imports it."""
    assert set(VERDICT_KINDS) == set(ap.VERDICT_KINDS) == {"citation", "support"}
    assert ap.DEFAULT_VERDICT_KIND == "citation"


def test_every_kind_a_writer_stamps_is_in_the_vocabulary():
    """Re-derived from the source: every `"kind": <literal>` and `[...]["kind"] = <literal>`."""
    tree = ast.parse(inspect.getsource(memo_verify))
    stamped = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "kind"
                        and isinstance(value, ast.Constant)):
                    stamped.add(value.value)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "kind"):
                    stamped.add(node.value.value)
    assert stamped == set(VERDICT_KINDS), stamped


def test_the_deterministic_layer_stamps_CITATION_on_every_row_it_writes():
    rows = check_claims([{"statement": "no evidence at all", "node_ids": [], "urls": []},
                         {"statement": "cites a node that is not there", "node_ids": [99],
                          "urls": []}], RunState())
    assert rows and all(row["kind"] == "citation" for row in rows), rows


def test_the_sanitizer_KEEPS_a_stamped_kind_across_write_and_replay():
    written = sanitize_research_memo_payload({"verification": {"method": "llm", "verdicts": [
        {"statement": "a", "verdict": "unsupported", "note": "", "kind": "support"},
        {"statement": "b", "verdict": "unsupported", "note": "", "kind": "citation"}]}})
    replayed = sanitize_research_memo_payload(written)
    for projected in (written, replayed):
        assert [row["kind"] for row in projected["verification"]["verdicts"]] == [
            "support", "citation"]


def test_an_OLD_or_foreign_row_reads_as_CITATION_the_conservative_direction():
    """Claiming the verifier judged SUPPORT when the field saying so is absent would promote every
    historical footnote defect into the most decisive gap the router knows."""
    projected = sanitize_research_memo_payload({"verification": {"verdicts": [
        {"statement": "old", "verdict": "unsupported"},
        {"statement": "odd", "verdict": "unsupported", "kind": "SOMETHING-ELSE"}]}})
    assert [row["kind"] for row in projected["verification"]["verdicts"]] == [
        "citation", "citation"]
