"""The event payload contract: what each type CARRIES, checked against source and against the fold.

`looplab/events/types.py::EVENT_PAYLOAD_KEYS` is the registry's missing half (doc 52 row 30). The
type registry pinned the NAMES and the folded/diagnostic partition; the payload — the thing invariant
#5's additive-only rule is about — existed only as handler code, so "readers supply defaults" and
"new keys are additive" were prose. 65 of the 147 constants carried no describing comment and ten
types were named in no document.

Nothing here trusts the table. Every assertion re-derives its side:

* the fold's reads and the writers' keys come from `tests/_source_scan.py`, by AST, following the
  helpers a handler forwards the payload to and the variables a writer builds it in;
* `stored_whole` is re-derived, never read from the row;
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


# The event types whose payload is built by a spread the writer scan CANNOT resolve, so their keys
# are declared from the builder by hand instead of derived from the append site. Shrink-only, and
# not empty by accident: `prior_injected` writes `{…, **receipt}` where the receipt is built five
# frames away, and while the scan silently reported "no undeclared keys" for it, five of its eight
# keys had no row at all and the generated reference under-described the record.
OPAQUE_PAYLOAD_WRITERS = frozenset({
    "agent_validated", "card_enriched", "concept_coverage_snapshot", "coverage_snapshot",
    "cross_run_prior", "diversity_archive", "finalize_step", "novelty_graded",
    "novelty_rejected", "prior_injected", "run_finished", "run_started", "run_width_settled",
    "setup_step", "spec_drift", "stage_finished"})


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


def test_stored_whole_is_re_derived_from_the_fold():
    """Declared `stored_whole` must equal what `replay.py` actually does with the payload."""
    derived = fold_stores_payload_whole()
    declared = {t for t, c in EVENT_PAYLOAD_KEYS.items() if c.stored_whole}
    assert declared == derived, (
        f"declared but does not store the payload: {sorted(declared - derived)}; "
        f"stores the payload but not declared: {sorted(derived - declared)}")


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


def test_control_event_payloads_stay_inside_their_allow_list():
    """`serve/control_validation.py` normalizes a control intent's payload against `data_fields`, so
    that table and this one are two spellings of the same vocabulary: the allow-list must be
    declared here, and nothing may be `required` that the intake does not accept."""
    pytest.importorskip("fastapi")
    from looplab.serve.control_validation import CONTROL_SPECS

    for etype, spec in CONTROL_SPECS.items():
        contract = EVENT_PAYLOAD_KEYS[etype]
        assert set(spec.data_fields) <= contract.keys, (
            f"{etype}: control allow-list fields missing from the payload contract: "
            f"{sorted(set(spec.data_fields) - contract.keys)}")
        assert set(contract.required) <= set(spec.data_fields), (
            f"{etype}: `required` names a key the control intake never accepts: "
            f"{sorted(set(contract.required) - set(spec.data_fields))}")


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
