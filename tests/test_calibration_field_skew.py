"""A Settings field added AFTER a calibration run was recorded must not revoke it.

`_replicate_invariants` compared the preserved `config.snapshot.json`'s field set for EQUALITY against
`SPECULATION_CALIBRATION_SNAPSHOT_FIELDS`, which is derived from THIS BINARY's `Settings.model_fields`.
So every field added since a run was recorded appeared as `missing` and revoked it: six preserved GPU
runs sat as a dead asset on this box, reported as a snapshot mismatch — which reads like a corrupt run
rather than a version skew — and no receipt could be minted here at all.

A changed DERIVATION should revoke receipts; that is the protocol working. Adding an unrelated field
changes no derivation and no measurement, only what a snapshot happens to contain. The equality was
doing the job of a version check with the tool of an exactness check.

THE EXACTNESS THAT MATTERS IS UNTOUCHED, which is what makes this a narrowing rather than a
loosening: `speculation_runtime_scope_digest` digests the whole snapshot document and compares it to
what the RUN stamped at start, consulting no current constant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.search import speculation_quality as quality


def _gate(pairs, module):
    return module._gate(pairs)


@pytest.fixture
def harness():
    """The suite's own fixture factory — the shape a REAL run writes, not the raw field set."""
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    return importlib.import_module("test_speculation_quality_gate")


def _snapshots(pairs) -> list[Path]:
    return [run / "config.snapshot.json" for pair in pairs for run in pair]


def _pair_errors(report) -> list[str]:
    """The per-PAIR refusals. The top-level `errors` list carries only the aggregate consequences
    ("calibration seed set must be exactly [0, 1, 2]"), so asserting on it would pass for any
    refusal whatever and say nothing about which rung fired."""
    return [str(e) for pair in report.get("pairs") or [] for e in pair.get("errors") or []]


def _edit(path: Path, mutate) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")


def test_the_shipped_corpus_still_passes(harness, tmp_path):
    """Baseline: nothing about a correct snapshot changed."""
    assert harness._gate(harness._pairs(tmp_path / "runs"))["passed"] is True


def test_a_snapshot_predating_a_NEW_settings_field_is_still_admitted(harness, tmp_path,
                                                                    monkeypatch):
    """THE DEFECT, simulated the way it actually happens: the BINARY's field set grows and the
    preserved snapshot does not. Widening the expectation is the honest direction — removing the key
    from the snapshot instead would ALSO break `speculation_runtime_scope_sha256`, which the run
    stamped over the document it really had, so the test would pass for the wrong reason.

    MUTATION: restore `set(config) != expected_config_fields` -> every pair reports the field as
    `missing` and the gate refuses, for a field no calibration run can be affected by (this one is
    the wall on an external coding agent; calibration uses the toy backend and launches none).
    """
    from looplab.search import speculation_calibration as calibration

    monkeypatch.setattr(
        calibration, "SPECULATION_CALIBRATION_SNAPSHOT_FIELDS",
        frozenset(calibration.SPECULATION_CALIBRATION_SNAPSHOT_FIELDS) | {"a_field_added_later"})
    report = harness._gate(harness._pairs(tmp_path / "runs"))
    assert report["passed"] is True, report.get("errors")


def test_a_REQUIRED_field_staying_absent_is_still_FATAL(harness, tmp_path):
    """The clause that keeps this from being a no-op — subtracting unknown names without it turns an
    exactness check into nothing, and the next genuinely-lossy snapshot passes.

    Driven on `speculation_gate_receipt`, which the protocol reads by name AND which
    `SPECULATION_RUNTIME_SCOPE_IGNORED_FIELDS` excludes from the runtime-scope digest — so it is a
    key ONLY this check can refuse, which is what makes the clause load-bearing rather than
    redundant with the digest.

    MUTATION: drop `required_config_fields` -> a snapshot missing it is admitted, and nothing has
    verified that this evidence carries no prior receipt authority.
    """
    pairs = harness._pairs(tmp_path / "runs")
    for path in _snapshots(pairs):
        _edit(path, lambda d: d.pop("speculation_gate_receipt", None))
    report = harness._gate(pairs)
    assert report["passed"] is False
    assert any("speculation_gate_receipt" in e for e in _pair_errors(report)), _pair_errors(report)


def test_config_snapshot_schema_is_required_too(harness, tmp_path):
    """The other digest-ignored required key: the document's own format marker. Without it nothing
    says which snapshot format this evidence is in."""
    pairs = harness._pairs(tmp_path / "runs")
    for path in _snapshots(pairs):
        _edit(path, lambda d: d.pop("config_snapshot_schema", None))
    assert harness._gate(pairs)["passed"] is False


def test_the_required_set_covers_every_key_the_function_reads():
    """A key read by name but absent from `required_config_fields` is optional by accident. AST over
    the real function, so a new `config.get("...")` goes red rather than silently joining the
    optional set — and it is an AST walk because a commented-out read is not an `ast.Call`."""
    import ast

    from _source_scan import function_tree

    tree = function_tree(quality._analyze_speculation_run)
    read = {node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "config" and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
    declared = {"speculation_gate_receipt", "max_nodes", "card_driven_selection",
                "speculation_depth", "trust_gate"}
    assert read, "no `config.get(...)` reads found — re-point this test"
    assert read <= declared, (
        f"{sorted(read - declared)} are read by name but not in `required_config_fields`")


def test_a_field_THIS_BINARY_DOES_NOT_UNDERSTAND_is_still_FATAL(harness, tmp_path):
    """The other direction, unchanged: a snapshot written by a NEWER LoopLab may carry semantics
    this build would silently drop. Same fail-closed rule `settings_from_snapshot` applies on resume.

    MUTATION: allow unknown keys -> evidence from a future binary is admitted on this one.
    """
    pairs = harness._pairs(tmp_path / "runs")
    for path in _snapshots(pairs):
        _edit(path, lambda d: d.update({"a_knob_from_the_future": 7}))
    report = harness._gate(pairs)
    assert report["passed"] is False
    assert any("a_knob_from_the_future" in e for e in _pair_errors(report)), _pair_errors(report)


def test_a_credential_binding_is_NOT_required(harness, tmp_path):
    """`masked_snapshot()` pops `llm_api_key_base_url`, so it can never appear in a snapshot —
    requiring it present refuses EVERY calibration run, which is the original defect with the sign
    flipped. The first cut of this fix did exactly that.

    The profile-settings loop still checks its VALUE through `config.get`, where an absent key reads
    None and a pinned None matches.
    """
    pairs = harness._pairs(tmp_path / "runs")
    for path in _snapshots(pairs):
        assert "llm_api_key_base_url" not in json.loads(path.read_text(encoding="utf-8"))
    assert harness._gate(pairs)["passed"] is True


def test_the_runtime_scope_digest_still_binds_the_whole_document(harness, tmp_path):
    """WHY the narrowing is safe. This is the exactness check that actually holds the line, and it
    consults no current constant — only the preserved snapshot and what the run stamped at start.

    Driven on `max_nodes`, because it is the one digested field the immutable profile does NOT pin
    (it is a variant), so the refusal below can only come from the digest or from the by-name check
    beside it — not from the profile loop, which fires first on every other field and would make
    this test pass without exercising anything.

    MUTATION: skip the runtime-scope comparison -> a snapshot edited after the fact is admitted as
    the evidence the run actually produced.
    """
    pairs = harness._pairs(tmp_path / "runs")
    for path in _snapshots(pairs):
        _edit(path, lambda d: d.update({"max_nodes": 7}))
    report = harness._gate(pairs)
    assert report["passed"] is False
    errors = _pair_errors(report)
    assert any("runtime scope" in e or "max_nodes" in e for e in errors), errors


def test_the_digest_is_computed_over_the_PRESERVED_document():
    """Stated directly, because it is the whole argument for the change: the same snapshot bytes
    give the same digest on any binary, whatever `Settings` has grown since."""
    from looplab.search.speculation_calibration import speculation_runtime_scope_digest

    document = {"max_nodes": 4, "llm_temperature": 0.0, "config_snapshot_schema": 3}
    grown = {**document, "a_field_added_later": 123}
    assert speculation_runtime_scope_digest(document) == speculation_runtime_scope_digest(dict(document))
    assert speculation_runtime_scope_digest(grown) != speculation_runtime_scope_digest(document), (
        "a document that really differs must digest differently — this is the check the field-set "
        "equality was standing in for, and it does the job correctly")
