"""The event payload contract: what each type CARRIES, checked against source and against the fold.

`looplab/events/types.py::EVENT_PAYLOAD_KEYS` is the registry's missing half (doc 52 row 30). The
type registry pinned the NAMES and the folded/diagnostic partition; the payload — the thing invariant
#5's additive-only rule is about — existed only as handler code, so "readers supply defaults" and
"new keys are additive" were prose. 65 of the 147 constants carried no describing comment and ten
types were named in no document.

Nothing here trusts the table. Every assertion re-derives its side:

* the fold's reads and the writers' keys come from `tests/_source_scan.py`, by AST, following the
  helpers a handler forwards the payload to and the variables a writer builds it in;
* `stored_whole` is re-derived, never read from the row — by FOLDING a marker key and looking for
  it in the raw state, with the AST scan demoted to nominating which types need a valid probe;
* the additive-only rule is DRIVEN, not scanned — every registered type is folded with an EMPTY
  payload, and a real recorded run is folded twice, once with every undeclared key stripped out of
  every payload. A key the fold consumes but the table omits changes the second state.

A row is therefore not satisfiable by a comment: a summary can lie about MEANING, but no comment can
add a key to the fold's read set or remove one from a writer.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from _source_scan import (event_payload_writers, fold_payload_reads, fold_stores_payload_whole,
                          iter_sources)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (ALL_EVENT_TYPES, DIAGNOSTIC_EVENTS, EVENT_PAYLOAD_KEYS,
                                  PayloadContract)

DATA = Path(__file__).resolve().parent / "data"


@pytest.fixture(scope="module")
def writers():
    return event_payload_writers()


@pytest.fixture(scope="module")
def reads():
    return fold_payload_reads()


def test_every_registered_type_has_exactly_one_contract_row():
    """Two-way, like the type registry itself: a new event type lands with a payload row or red."""
    assert set(EVENT_PAYLOAD_KEYS) == ALL_EVENT_TYPES, (
        f"no contract row: {sorted(ALL_EVENT_TYPES - set(EVENT_PAYLOAD_KEYS))}; "
        f"row for an unregistered type: {sorted(set(EVENT_PAYLOAD_KEYS) - ALL_EVENT_TYPES)}")
    for etype, contract in EVENT_PAYLOAD_KEYS.items():
        assert isinstance(contract, PayloadContract)
        summary = contract.summary
        assert summary and summary[0].isupper() and summary.endswith((".", "?")), etype
        assert "\n" not in summary and len(summary) <= 130, (etype, len(summary))
        overlap = set(contract.required) & set(contract.optional)
        assert not overlap, f"{etype}: a key cannot be both required and optional: {sorted(overlap)}"
        assert sorted(contract.required) == list(contract.required), f"{etype}: sort `required`"
        assert sorted(contract.optional) == list(contract.optional), f"{etype}: sort `optional`"


def test_every_key_the_fold_reads_is_declared(reads):
    """The dead-reader guard. `fold` reading a key no row declares is the shape that let
    `RunTools._research_memo` key on a `summary` no writer ever produced, for six weeks."""
    undeclared = {}
    for etype, pairs in reads.items():
        missing = {key for key, _how in pairs} - EVENT_PAYLOAD_KEYS[etype].keys
        if missing:
            undeclared[etype] = sorted(missing)
    assert not undeclared, (
        "replay.fold reads payload keys that EVENT_PAYLOAD_KEYS does not declare — declare them "
        f"(as `optional`, with a reader-side default) or stop reading them: {undeclared}")


def test_every_key_a_writer_writes_is_declared(writers):
    """The other direction: a payload key that reaches the log with no row is an undocumented field
    of the record — and for a `stored_whole` type, an undocumented field of `RunState`."""
    undeclared = {}
    for etype, row in writers.items():
        missing = set(row["any"]) - EVENT_PAYLOAD_KEYS[etype].keys
        if missing:
            undeclared[etype] = {"keys": sorted(missing), "sites": row["sites"][:3]}
    assert not undeclared, (
        f"event writers put undeclared keys in the payload: {undeclared}")


# The event types whose payload the writer scan CANNOT resolve, so their keys are declared from the
# builder by hand instead of derived from the append site. Shrink-only, and not empty by accident:
# `prior_injected` writes `{…, **receipt}` where the receipt is built five frames away, and while
# the scan silently reported "no undeclared keys" for it, five of its eight keys had no row at all
# and the generated reference under-described the record.
#
# SIXTEEN GREW TO THIRTY-FOUR on 2026-09-08, and none of the eighteen is new code. They were
# invisible: `event_payload_writers` marked only the `**spread` case opaque, and for every OTHER
# unresolvable payload — a function's return value, an attribute, a `dict(...)` call — it simply
# `continue`d and recorded NOTHING, so the type had no row and the assertion below (which reads
# only the types the scan DID reach) could not name it. Forty-two registered types were unverified
# and silent. Four real defects lived there and are fixed in the same change: `llm_usage` declared
# 2 of its 7 columns (the durable money ledger, whose fold reads all five missing ones), `plan`
# omitted the `max_nodes` its own `replan` reads back, `trace_export_health` omitted 15 of 25 keys
# including the two a human debugging a stalled exporter needs, and `card_build_done`'s `required`
# named three columns NO writer has ever written — a fabricated claim that could not fail, because
# `test_required_keys_are_written_by_every_literal_writer` skips a type with no writer row.
OPAQUE_PAYLOAD_WRITERS = frozenset({
    "agent_validated", "applied_params_backfilled", "asha_rank", "budget", "card_added",
    "card_build_done", "card_enriched", "concept_coverage_snapshot", "coverage_snapshot",
    "cross_run_prior", "data_shift", "diversity_archive", "finalize_step", "llm_usage",
    "node_concepts", "novelty_graded", "novelty_rejected", "pause", "plan", "prior_injected",
    "report_generated", "run_abort", "run_finished", "run_started", "run_width_settled",
    "rung_promoted", "score_metrics_backfilled", "setup_step", "spec_drift", "spec_proposed",
    "stage_finished", "trace_export_health", "train_monitor_alert", "trust_scan"})


def test_the_writer_scan_says_which_types_it_cannot_verify(writers):
    """A SCAN THAT CANNOT SEE A PAYLOAD MUST NOT READ AS A CLEAN ONE. `test_every_key_a_writer
    _writes_is_declared` passes vacuously for a type built by an unresolvable `**spread` — there
    are no literal keys to compare — so the set of such types is named here and may only shrink.

    Adding a type to the allow-list is the honest move ONLY together with a hand-written key list
    on its contract row; removing the spread is better."""
    opaque = {etype for etype, row in writers.items() if row["opaque"]}
    assert opaque <= OPAQUE_PAYLOAD_WRITERS, (
        "a new event payload is built by a spread the scan cannot resolve, so nothing checks its "
        f"declared keys: {sorted(opaque - OPAQUE_PAYLOAD_WRITERS)}")
    assert OPAQUE_PAYLOAD_WRITERS <= opaque, (
        "listed as opaque but the scan can now read it — delete the row: "
        f"{sorted(OPAQUE_PAYLOAD_WRITERS - opaque)}")
    for etype in OPAQUE_PAYLOAD_WRITERS:
        assert EVENT_PAYLOAD_KEYS[etype].keys, (
            f"{etype} is unverifiable AND declares no key — the record is undescribed either way")


def test_a_key_added_to_a_payload_after_its_literal_is_seen(writers):
    """`data["source"] = …` after the dict literal is a payload key, and the dict walk cannot see
    it: `memory_read.source` reached the log undeclared while this scan reported the type fully
    covered. Pinned on the one live instance so the subscript hop cannot be dropped as dead code."""
    assert "source" in writers["memory_read"]["any"], (
        "the writer scan stopped following subscript writes into the payload")


def test_a_key_added_by_an_UNPACKING_assignment_is_seen_too(writers):
    """`data["a"], data["b"] = (…)` is one `Assign` whose single target is a TUPLE of subscripts.

    A walk that accepted only a bare `ast.Subscript` target saw neither key, so
    `node_failed.triage_action` — LLM-derived text about a crash, on a durable terminal — reached
    the log with no contract row and no line in the generated reference, while
    `test_every_key_a_writer_writes_is_declared` reported the type clean. Pinned on the live
    instance so the unpacking hop cannot be dropped as dead code, exactly like the subscript hop
    one test up.
    """
    assert "triage_action" in writers["node_failed"]["any"], (
        "the writer scan stopped unpacking tuple assignment targets")
    assert "triage_rationale" in writers["node_failed"]["any"]


def test_a_key_put_in_by_UPDATE_or_SETDEFAULT_is_seen_too(writers):
    """`payload.update({...})` and `payload.setdefault("k", …)` are never assignment TARGETS.

    A target-only walk could not see them, so `train_monitor_alert` reached the durable log with
    FIVE undeclared columns — two of them (`projected_overrun_s`, `stage_wall_s`) read live by
    `serve/attention.py` — and `asha_rank`/`asha_verdict` with two each, while
    `test_every_key_a_writer_writes_is_declared` reported all three types fully covered. Pinned on
    the live instances so the hop cannot be dropped as dead code, exactly like the subscript and
    unpacking hops above.
    """
    assert "resource_key" in writers["asha_rank"]["any"], (
        "the writer scan stopped reading `.update({...})` onto the payload")
    assert "resource" in writers["asha_verdict"]["any"]
    # …and the CONSTANT-key rule: `update(<a name>)` is a spread this cannot resolve, and the
    # honest answer there is `opaque`, not a silently short key list.
    import ast

    from tests._source_scan import subscript_string_keys
    tree = ast.parse('def f():\n'
                     '    p = {}\n'
                     '    p.update({"seen": 1})\n'
                     '    p.setdefault("also", 2)\n'
                     '    p.update(kw=3)\n'
                     '    p.update(opaque_spread)\n'
                     '    store.append(EV_X, p)\n')
    body = tree.body[0].body
    assert subscript_string_keys(body, "p", after=0, before=99) == {"seen", "also", "kw"}


def test_the_money_ledger_survives_stripping_its_undeclared_keys():
    """`test_stripping_undeclared_keys_from_a_real_log_changes_nothing` is the rule; the golden log
    is not the whole vocabulary.

    That test folds `tests/data/golden_run_events.jsonl`, which carries 16 of the registered types
    and asserts a floor of `>= 8` — and `llm_usage` is not among them. So the run's DURABLE COST
    LEDGER declared 2 of its 7 columns while the fold read all five missing ones, and the check
    designed to catch exactly this could not see it. Driven here on the type itself.
    """
    from looplab.events.replay import fold
    from looplab.events.types import EVENT_PAYLOAD_KEYS

    class _E:
        def __init__(self, etype, data):
            self.type, self.data, self.seq, self.ts = etype, data, 0, 0.0

    usage = {"cost": 1.25, "calls": 4, "priced_calls": 4, "prompt_tokens": 100,
             "completion_tokens": 50, "total_tokens": 150, "usage_id": "u" * 32}
    rows = [_E("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"}),
            _E("llm_usage", dict(usage))]
    declared = EVENT_PAYLOAD_KEYS["llm_usage"].keys
    stripped = [rows[0], _E("llm_usage", {k: v for k, v in usage.items() if k in declared})]

    full, thin = fold(rows).llm_cost, fold(stripped).llm_cost
    assert full == thin, (
        "stripping undeclared payload keys changed the folded cost ledger — the contract row is "
        f"missing what the fold reads: full={full} stripped={thin}")
    assert full.get("cost") == 1.25 and full.get("total_tokens") == 150, full


def test_required_keys_are_written_by_every_literal_writer(writers):
    """`required` is a claim about writers TODAY, so it is checked against every literal one. A key
    one site omits (or writes only inside a conditional spread) is `optional`, whatever it means."""
    broken = {}
    for etype, contract in EVENT_PAYLOAD_KEYS.items():
        row = writers.get(etype)
        if not row or not row["always"]:
            continue          # no literal writer to check against (control intents, built payloads)
        missing = set(contract.required) - set(row["always"])
        if missing:
            broken[etype] = {"not written unconditionally": sorted(missing),
                             "sites": row["sites"][:3]}
    assert not broken, (
        f"`required` keys some writer does not write unconditionally: {broken}")


# ------------------------------------------------------------------ `stored_whole`, DRIVEN
#
# THE STORE PROBE (review 2026-09-22, EVT-05). `stored_whole` was re-derived by an AST scan
# (`_source_scan.fold_stores_payload_whole`) that counts a payload handed to any call it cannot
# follow as STORED — `set(d)`, a local `dict(d)` copy, a bounded receipt builder — and the table
# had been made to agree with it: `card_added`, `card_enriched`, `llm_usage` and `node_concepts`
# were published "whole" while an unknown key folded into any of them reaches no state at all
# (driven by `review/EVT/repro_stored_whole.py`). A scan that over-approximates can NOMINATE; the
# fold decides. Each folded type is folded once with a marker key, and it stores its payload whole
# iff the marker reaches the raw accumulated state (`FoldCursor._state`, before any post-pass).
_MARKER_KEY, _MARKER = "zz_store_probe_key", "zz-store-probe-9c1e"

# Minimal VALID payloads for handlers that validate before they keep anything: an empty payload is
# refused there, and a refused row says nothing about storage. The set is DERIVED by the test below
# — exactly the types the scan nominates and an empty probe cannot decide — so a row that is not
# needed, or a needed type with no row, is red.
_STORE_PROBE_PAYLOADS = {
    "ablate": {"parent_id": 0, "generation": 0, "impacts": []},
    "card_added": {"id": "card-probe", "statement": "a probe statement"},
    "card_enriched": {"id": "card-probe", "confidence": 0.5},
    "fork": {"from_node_id": 0, "generation": 0},
    "llm_usage": {"usage_id": "u-probe", "calls": 1, "cost": 0.25},
    "node_concepts": {"node_id": 0, "generation": 0, "concepts": ["loss/focal"]},
    "plan": {"phases": []},
}


def _raw_state_after(etype=None, payload=None) -> str:
    """The RAW accumulated state (no post-pass) after a one-node prefix plus one probe row."""
    from looplab.core.models import Event
    from looplab.events.replay import FoldCursor

    rows = [("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
            ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": "",
                              "files": {}, "generation": 0})]
    if etype is not None:
        rows.append((etype, payload))
    cursor = FoldCursor()
    cursor.extend(Event(seq=i, ts=float(i + 1), type=t, data=d) for i, (t, d) in enumerate(rows))
    return cursor._state.model_dump_json()


def _stores_marker(etype: str, payload: dict) -> bool:
    dump = _raw_state_after(etype, {**payload, _MARKER_KEY: _MARKER})
    return _MARKER in dump or _MARKER_KEY in dump


@pytest.fixture(scope="module")
def store_probe():
    folded = sorted(ALL_EVENT_TYPES - DIAGNOSTIC_EVENTS)
    bare = {etype for etype in folded if _stores_marker(etype, {})}
    derived = bare | {etype for etype, payload in _STORE_PROBE_PAYLOADS.items()
                      if _stores_marker(etype, payload)}
    return {"bare": bare, "derived": derived}


def test_stored_whole_is_re_derived_by_folding_a_marker(store_probe):
    """Declared `stored_whole` must equal what the fold DOES with a key it was never told about.
    MUTATION: put `stored_whole=True` back on `card_added` (the scan's verdict) and this is red."""
    declared = {t for t, c in EVENT_PAYLOAD_KEYS.items() if c.stored_whole}
    derived = store_probe["derived"]
    assert declared == derived, (
        f"declared whole but an unknown key reaches no state: {sorted(declared - derived)}; "
        f"an unknown key reaches RunState but not declared whole: {sorted(derived - declared)}")


def test_every_type_the_scan_nominates_is_decided_by_an_informative_probe(store_probe):
    """The scan can only OVER-count a payload as stored, so it nominates; a type it nominates that
    the empty probe cannot show storing needs a valid probe row, and that row must be one the
    handler ACCEPTS — a refused probe decides nothing and would read as "not whole" by default.

    What neither half can see, stated rather than hidden: a handler that keeps the payload only
    AFTER validating it, through a shape the scan does not recognise (`st.x = d.copy()`), is
    nominated by nothing and refused by the empty probe. The bare probe still catches every
    handler that stores unconditionally, for every folded type, whatever the scan thinks."""
    needed = fold_stores_payload_whole() - store_probe["bare"]
    assert set(_STORE_PROBE_PAYLOADS) == needed, (
        f"nominated but undecided — add a minimal valid probe row: "
        f"{sorted(needed - set(_STORE_PROBE_PAYLOADS))}; probe rows nothing needs: "
        f"{sorted(set(_STORE_PROBE_PAYLOADS) - needed)}")
    reference = _raw_state_after()
    refused = sorted(etype for etype, payload in _STORE_PROBE_PAYLOADS.items()
                     if _raw_state_after(etype, payload) == reference)
    assert not refused, (
        f"these probe payloads are refused by their handler, so they cannot decide storage: "
        f"{refused}")


def test_every_registered_type_folds_from_an_empty_payload():
    """Invariant #5, DRIVEN: an old log missing every newer field must still fold.

    This is what "readers supply defaults" means operationally, and it is exactly what a handler
    that starts doing `d["new_key"]` breaks — a KeyError mid-fold, i.e. an unreadable run rather
    than a missing field. Each type is folded alone AND all of them in one log, because a handler
    can also crash on state another handler left behind.
    """
    from looplab.core.models import Event

    for etype in sorted(ALL_EVENT_TYPES):
        fold([Event(seq=0, ts=0.0, type=etype, data={})])
    fold([Event(seq=i, ts=float(i), type=t, data={})
          for i, t in enumerate(sorted(ALL_EVENT_TYPES))])


def test_stripping_undeclared_keys_from_a_real_log_changes_nothing():
    """The completeness half, driven over a REAL run: fold the golden log, then fold it again with
    every key the contract does not declare removed from every payload. A key the fold consumes but
    the table omits shows up as a different state — which an AST scan of a helper chain could miss.
    """
    events = EventStore(DATA / "golden_run_events.jsonl").read_all()
    assert events, "golden log missing/empty"
    covered = {e.type for e in events}
    assert len(covered) >= 8, f"golden log exercises too few types to be a check: {sorted(covered)}"

    stripped = []
    removed = 0
    for event in events:
        clone = copy.deepcopy(event)
        declared = EVENT_PAYLOAD_KEYS[event.type].keys if event.type in EVENT_PAYLOAD_KEYS else None
        if declared is not None and isinstance(clone.data, dict):
            for key in list(clone.data):
                if key not in declared:
                    del clone.data[key]
                    removed += 1
        stripped.append(clone)
    assert fold(stripped).model_dump(mode="json") == fold(events).model_dump(mode="json"), (
        "stripping undeclared payload keys changed the folded state — EVENT_PAYLOAD_KEYS is missing "
        "a key the fold actually consumes")


def _consumed_request_fields() -> dict[str, frozenset[str]]:
    """REQUEST fields a control normalizer CONSUMES — turns into other stored keys, or pops — and
    never writes itself. `data_fields` is what a CALLER may send; the contract is what the LOG
    carries, and for these two they differ by design (review 2026-09-22, EVT-05): the comment
    revisions turn `expected_version` into `base_version`/`version`, and `inject_node`'s cross-run
    import pops its two inputs into `origin`. Declaring them as carried is the misreading this
    table exists to refuse, so they are named here and must NOT be in the contract."""
    from looplab.serve.control_validation import _INJECT_IMPORT_FIELDS

    return {
        "comment_edited": frozenset({"expected_version"}),
        "comment_resolution_changed": frozenset({"expected_version"}),
        "inject_node": frozenset(_INJECT_IMPORT_FIELDS),
    }


def test_control_event_payloads_stay_inside_their_allow_list():
    """`serve/control_validation.py` normalizes a control intent's payload against `data_fields`, so
    that table and this one are two spellings of the same vocabulary: the allow-list must be
    declared here — less the fields the normalizer CONSUMES, which must not be — and nothing may
    be `required` that the intake does not accept."""
    pytest.importorskip("fastapi")
    from looplab.serve.control_validation import CONTROL_SPECS

    consumed_by_type = _consumed_request_fields()
    assert set(consumed_by_type) <= set(CONTROL_SPECS)
    for etype, spec in CONTROL_SPECS.items():
        contract = EVENT_PAYLOAD_KEYS[etype]
        consumed = consumed_by_type.get(etype, frozenset())
        assert consumed <= set(spec.data_fields), (
            f"{etype}: listed as consumed but the intake does not even accept "
            f"{sorted(consumed - set(spec.data_fields))}")
        assert set(spec.data_fields) - consumed <= contract.keys, (
            f"{etype}: control allow-list fields missing from the payload contract: "
            f"{sorted(set(spec.data_fields) - consumed - contract.keys)}")
        assert not (consumed & contract.keys), (
            f"{etype}: declares request field(s) its normalizer consumes and never stores: "
            f"{sorted(consumed & contract.keys)}")
        assert set(contract.required) <= set(spec.data_fields), (
            f"{etype}: `required` names a key the control intake never accepts: "
            f"{sorted(set(contract.required) - set(spec.data_fields))}")


def test_a_real_comment_log_stripped_to_the_contract_keeps_every_comment(tmp_path):
    """THE COMMENT ROWS, DRIVEN (review 2026-09-22, EVT-05). All three were written from the request
    allow-list: `comment_edited` declared `expected_version`, which no stored row carries, and none
    declared `comment_id`/`actor_kind`/`version`/`base_version`, which the reducer requires — so
    stripping the undeclared keys from a real comment log folded to NO comments. Nothing saw it:
    `_on_comment` hands `apply_comment_event` the EVENT rather than `d`, so the AST read scan is
    blind here, and the golden log carries no comment. So the payloads are built by the REAL
    normalizers, appended as the command service appends them, stripped to the contract and folded.
    MUTATION: restore the old three rows and the stripped fold has no comment at all."""
    pytest.importorskip("fastapi")
    from looplab.events.comment_projection import project_comments
    from looplab.serve.control_validation import normalize_control

    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "g",
                                 "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}},
                                  "code": "print(1)", "files": {}})

    class _Srv:
        root = rd.parent

        def state(self, path):
            return fold(EventStore(path / "events.jsonl").read_all())

    srv = _Srv()
    created = normalize_control(srv, rd, "comment_created",
                                {"node_id": 0, "node_generation": 0, "text": "first note"})
    store.append("comment_created", created)
    comment_id = created["comment_id"]
    edited = normalize_control(srv, rd, "comment_edited", {
        "comment_id": comment_id, "node_id": 0, "node_generation": 0, "expected_version": 1,
        "text": "second note"})
    store.append("comment_edited", edited)
    resolved = normalize_control(srv, rd, "comment_resolution_changed", {
        "comment_id": comment_id, "node_id": 0, "node_generation": 0, "expected_version": 2,
        "resolved": True})
    store.append("comment_resolution_changed", resolved)

    for etype, payload in (("comment_created", created), ("comment_edited", edited),
                           ("comment_resolution_changed", resolved)):
        undeclared = set(payload) - EVENT_PAYLOAD_KEYS[etype].keys
        assert not undeclared, f"{etype}: the normalizer stores undeclared {sorted(undeclared)}"
        assert "expected_version" not in payload, "a consumed request field reached the log"

    events = store.read_all()
    stripped = []
    for event in events:
        clone = copy.deepcopy(event)
        declared = EVENT_PAYLOAD_KEYS[event.type].keys
        clone.data = {k: v for k, v in clone.data.items() if k in declared}
        stripped.append(clone)
    full, thin = fold(events), fold(stripped)
    comment = full.comments.get(comment_id)
    assert comment is not None and comment.version == 3, "precondition: the real log folds"
    assert comment.text == "second note" and comment.resolved is True
    # `RunState.comments` is excluded from `model_dump`, so compare the projection itself.
    assert ({cid: c.model_dump(mode="json") for cid, c in thin.comments.items()}
            == {cid: c.model_dump(mode="json") for cid, c in full.comments.items()}), (
        "stripping the keys EVENT_PAYLOAD_KEYS does not declare lost comment state")
    assert thin.comments_revision == full.comments_revision
    assert (project_comments(stripped, include_history=True)
            == project_comments(events, include_history=True)), "…or its audit history"


def test_the_phase_event_module_spells_registered_types():
    """`core/phase_events.py` cannot import `events.types` (the layering rule), so it re-SPELLS its
    four type names as literals — the one emitter `tests/test_event_types.py`'s `.append(...)` scan
    cannot see. A typo there would emit a type the fold ignores and no guard mentions."""
    from looplab.core.phase_events import SINK_EVENT_TYPES

    assert set(SINK_EVENT_TYPES) <= ALL_EVENT_TYPES, (
        f"phase-event literals that are not registered event types: "
        f"{sorted(set(SINK_EVENT_TYPES) - ALL_EVENT_TYPES)}")
    assert set(SINK_EVENT_TYPES) <= DIAGNOSTIC_EVENTS, (
        "a phase event is appended from a concurrent task, so it must be fold-ignored AND excluded "
        "from every seq-equality fence (engine invariant #1)")


def test_the_scanners_see_what_they_claim_to_see(writers, reads):
    """Meta-guard: the derivations above are only as good as the walk under them. Pin the shapes a
    silent scanner regression would flatten — a `payload built in a variable` writer, a handler that
    reads through a helper, and the alias-spelled phase emitters."""
    # a payload assigned to a local first (`engine/finalize.py` mints the budget receipt that way)
    assert "finalize_scope" in writers["diversity_archive"]["any"]
    # a handler that reads nothing itself and hands `d` to a module-level helper
    assert {"node_id", "generation"} <= {k for k, _ in reads["promote"]}
    # the `emit_phase_event(PHASE_STARTED, {...})` spelling
    assert "max_turns" in writers["agent_phase_started"]["any"]
    # and the walk is over the package, not one file
    assert sum(1 for _ in iter_sources()) > 100


def test_the_event_reference_page_is_what_the_generator_writes():
    """`docs/guide/event-reference.md` is generated from the contract, so a type that lands without
    a regenerated page is a red test — the state ten types were in before the page existed."""
    from looplab.events.event_reference import PAGE, generated_block, render_page

    assert PAGE.is_file(), "docs/guide/event-reference.md is missing — run " \
                           "`python -m looplab.events.event_reference`"
    assert generated_block(PAGE.read_text(encoding="utf-8")) == generated_block(render_page()), (
        "docs/guide/event-reference.md is stale against EVENT_PAYLOAD_KEYS — an event type was "
        "added, renamed or re-described; run `python -m looplab.events.event_reference`")


def test_every_registered_type_is_named_on_the_page():
    """The count in the prose is derived, not typed: read the type back off the table's own rows."""
    from looplab.events.event_reference import PAGE

    page = PAGE.read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| `([a-z0-9_]+)` \| (?:folded|diagnostic)", page, re.M))
    assert listed == ALL_EVENT_TYPES, (
        f"missing from the page: {sorted(ALL_EVENT_TYPES - listed)}; "
        f"on the page but unregistered: {sorted(listed - ALL_EVENT_TYPES)}")
