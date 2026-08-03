"""Each trust detector owns the signal namespace its output gates on (doc 25 CT-10).

`engine/evaluate.py` used to MINT the namespace while reconciling three detector vocabularies::

    sigs.append({"signal": "data_leakage:" + f["signal"], …})
    sigs.append({"signal": "critic:" + c["issue"], …})

Those strings are not cosmetic. `events/replay.py::is_hard_signal` keys GATING on them:
`critic:hardcoded_metric` excludes a node from selection while every other `critic:` issue stays
advisory. So the namespace a detector's output landed in decided whether a run could be gated, and
it was assembled at the consumer, three files from the detector that knew what it found.

What this file pins is the JOIN, in both directions: the detectors emit exactly the strings the gate
recognises, and the gate's own classification of those strings is unchanged. A rename on either side
alone is a silent change to what gates — which is the failure this finding is about.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.models import Idea
from looplab.events.replay import is_hard_signal
from looplab.trust.critic import critic_findings, critique
from looplab.trust.findings import CRITIC_NS, LEAKAGE_NS, finding, namespaced
from looplab.trust.leakage import code_leakage_findings, code_leakage_scan

_LEAKY = "model.fit(X_test, y_test)\nscore = model.score(X_test, y_test)\n"


# --------------------------------------------------------------- the detectors namespace their own

def test_leakage_findings_arrive_already_namespaced():
    rows = code_leakage_findings(_LEAKY)
    assert rows, "the fixture must actually trip a leakage flag, or this test proves nothing"
    for row in rows:
        assert row["signal"].startswith(LEAKAGE_NS)
        assert row["signal"] != LEAKAGE_NS, "a bare namespace is not a signal"
        assert row["detail"].startswith("line "), "the line/code provenance must survive the move"


def test_leakage_findings_carry_the_same_names_the_raw_scan_reports():
    """The projection must not rename anything — `is_hard_signal` matches exact spellings."""
    raw = {flag["signal"] for flag in code_leakage_scan(_LEAKY)["flags"]}
    projected = {row["signal"][len(LEAKAGE_NS):] for row in code_leakage_findings(_LEAKY)}
    assert projected == raw


# A solution whose score is a QUOTED literal with nothing computing it — the exact shape the
# narrow `critic:hardcoded_metric` gate exists to catch.
_HARDCODED = 'import json\nprint(json.dumps({"metric": 0.99}))\n'


def test_critic_findings_arrive_already_namespaced():
    idea = Idea(operator="draft", hypothesis="fit a model")
    raw = critique(idea, _HARDCODED)
    rows = critic_findings(idea, _HARDCODED)
    assert raw, "the fixture must actually trip the critic, or this test proves nothing"
    assert len(rows) == len(raw)
    assert {row["signal"] for row in rows} == {CRITIC_NS + row["issue"] for row in raw}
    assert {row["detail"] for row in rows} == {row["detail"] for row in raw}


def test_a_detector_with_nothing_to_say_returns_no_rows():
    clean = "import json\n\n\ndef solve(x):\n    return x * 2\n"
    assert code_leakage_findings(clean) == []


# --------------------------------------------------------------- the consumer only concatenates

def test_evaluate_no_longer_mints_a_namespace():
    """The headline. A second consumer of these detectors would have had to re-derive the same
    mapping, and any drift between the two would silently change what gates."""
    from looplab.engine import evaluate

    source = inspect.getsource(evaluate)
    assert '"data_leakage:" +' not in source
    assert '"critic:" +' not in source
    assert "code_leakage_findings(scan_src)" in source
    assert "critic_findings(node.idea, scan_src" in source


def test_no_module_outside_trust_builds_a_gate_namespace_by_concatenation():
    """A grep guard across the tree, because the defect is not specific to `evaluate.py`: any
    consumer that string-builds a gate namespace has re-created the split this removed.

    Through `_source_scan`, not a fresh `rglob` — at least one tracked file carries a UTF-8 BOM, and
    a guard that walks the package itself decodes it differently from every other guard.
    """
    from _source_scan import PKG, iter_sources

    offenders = [
        f"{path.relative_to(PKG)}:{index}"
        for path, source in iter_sources()
        if path.parent.name != "trust"
        for index, line in enumerate(source.splitlines(), 1)
        if ('"data_leakage:" +' in line or '"critic:" +' in line
            or "'data_leakage:' +" in line or "'critic:' +" in line)
    ]
    assert offenders == [], f"gate namespace assembled outside trust/: {offenders}"


# --------------------------------------------------------------- the join with the gate

def test_the_namespaces_are_exactly_what_the_gate_classifies():
    """Both halves of the contract at once. If a detector's prefix and `is_hard_signal`'s expected
    spelling drift apart, gating changes silently — a hard signal becomes advisory, or the reverse.
    """
    # the one high-precision critic issue: it EXCLUDES a node from selection
    assert is_hard_signal(CRITIC_NS + "hardcoded_metric") is True
    # ...every other critic issue is advisory, and must stay that way
    for issue in ("no_op", "ignores_params", "stub"):
        assert is_hard_signal(CRITIC_NS + issue) is False, (
            f"{issue} started gating; a broad critic warning must not exclude an honest node")


def test_the_gating_critic_issue_is_reachable_through_the_new_helper():
    """Not just the string: a solution that hard-codes its metric must still produce the exact
    signal the gate excludes on, through the helper the engine now calls."""
    idea = Idea(operator="draft", hypothesis="fit a model")
    signals = {row["signal"] for row in critic_findings(idea, _HARDCODED)}
    hard = {sig for sig in signals if is_hard_signal(sig)}
    assert CRITIC_NS + "hardcoded_metric" in signals
    assert hard == {CRITIC_NS + "hardcoded_metric"}, (
        f"exactly one signal from this solution may gate; got {sorted(hard)}")


def test_leakage_signals_gate_because_an_unknown_name_fails_closed():
    """Leakage names are not individually enumerated by `is_hard_signal` — they fall through to its
    fail-closed default. Pinned so a future "tidy up the classifier" cannot quietly make data
    leakage advisory."""
    for row in code_leakage_findings(_LEAKY):
        assert is_hard_signal(row["signal"]) is True, (
            f"{row['signal']} stopped gating; leakage evidence must fail closed")


# --------------------------------------------------------------- the shape itself

def test_the_finding_shape_keeps_the_wire_format():
    """`reward_hack_suspected` events already on disk carry these keys; the move must not change
    them, or every stored flag becomes unreadable by the fold."""
    plain = finding("critic:no_op", "does nothing")
    assert plain == {"signal": "critic:no_op", "detail": "does nothing"}

    rich = finding("grader_access", "imported grader", method="static", confidence=0.9)
    assert rich == {"signal": "grader_access", "detail": "imported grader",
                    "method": "static", "confidence": 0.9}

    # optional fields are OMITTED, not written as None — a null `confidence` would read as a
    # measured zero-confidence finding rather than an unmeasured one.
    assert "method" not in plain and "confidence" not in plain


def test_the_projection_skips_rows_with_no_signal_rather_than_emitting_a_bare_prefix():
    rows = namespaced("ns:", [{"signal": "a"}, {"signal": ""}, {}, {"signal": "b"}],
                      signal_key="signal", detail="d")
    assert [row["signal"] for row in rows] == ["ns:a", "ns:b"]


def test_a_detail_callable_sees_the_whole_row():
    rows = namespaced("ns:", [{"signal": "a", "line": 7}], signal_key="signal",
                      detail=lambda row: f"line {row['line']}")
    assert rows == [{"signal": "ns:a", "detail": "line 7"}]


@pytest.mark.parametrize("prefix", [LEAKAGE_NS, CRITIC_NS])
def test_every_namespace_ends_in_a_colon(prefix):
    """`is_hard_signal` compares whole strings; a prefix missing its separator would produce
    `critichardcoded_metric` and silently stop matching."""
    assert prefix.endswith(":") and len(prefix) > 1
