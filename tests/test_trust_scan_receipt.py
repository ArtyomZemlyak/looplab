"""A CLEAN SCAN LEAVES A RECEIPT, AND AN ABSENT RECEIPT IS NEVER READ AS CLEAN.

`test_trust_gates_reach_the_ledger.py` proves the FLAGGED case reaches the log. This file is about
the other 100 % of the corpus: the detectors write only on a hit, so a run in which every node was
scanned and came back clean was byte-identical to a run whose scan call had been deleted — the 2026-
08-05 mutation audit's own finding, one layer out from where that file leaves it.

**The two populations both exist on this box** (measured 2026-08-19 over `runs/`, all six logs at
`trust_gate="audit"`): `rubertlite-dr-unified-v9` and `e5small-dr-unified-v2` carry
`reward_hack_detect=true` + `code_leakage_detect=true` over 9 evaluated nodes and contain ZERO trust
rows, while `rubertlite-dense-retrieval`/`-v6`/`-v7`/`-v8` carry both flags FALSE over 100 evaluated
nodes — so on four of the six the reward-hack and leakage detectors did not run at all, which the
BACKLOG's §0.15 ledger ("they run unconditionally per evaluated node") got wrong. Scanned-and-clean
and never-scanned are exactly the two an auditor must tell apart, and nothing in the log did.

Every test here drives a REAL run and then reads its event log, or calls the reader with a real
event list; nothing is a source pin, because the property is "the row is on disk and says the right
thing", which no substring can establish.
"""
from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from factories import make_engine
from looplab.agents.roles import ToyObjectiveDeveloper
from looplab.core.models import Idea, developer_artifact_footprint
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import DIAGNOSTIC_EVENTS, EV_TRUST_SCAN
from looplab.trust import scan_receipt as scan_receipt_mod
from looplab.trust.scan_receipt import (TRUST_DETECTOR_CODE_LEAKAGE, TRUST_DETECTOR_CRITIC,
                                        TRUST_DETECTOR_REWARD_HACK, TRUST_DETECTOR_WORKDIR_AUDIT,
                                        TRUST_DETECTORS, TRUST_SCAN_CLEAN, TRUST_SCAN_FLAGGED,
                                        TRUST_SCAN_UNKNOWN, TRUST_SCAN_UNSCANNED,
                                        scan_subject_digest, trust_scan_receipts,
                                        trust_scan_status, trust_scan_summary)

# The same positive control `test_trust_gates_reach_the_ledger.py` uses: a runnable solution that a
# static scan has something to say about (`fit` on test data under a dead `if`, plus a literal
# metric). Duplicated rather than imported because these two files must be able to disagree.
_LEAKY_SOLUTION = '''import json
if False:                       # never executes; a static tell, so the eval still succeeds
    model.fit(X_test, y_test)
print(json.dumps({"metric": 0.5}))
'''


# `_trust_gate_signals` reads exactly one attribute off the node — see the sibling file's note.
_NODE_FOR_GATE = SimpleNamespace(idea=Idea(operator="draft"))


class _LeakyDeveloper(ToyObjectiveDeveloper):
    def implement(self, idea):
        self.last_footprint = developer_artifact_footprint(idea.footprint, _LEAKY_SOLUTION)
        return _LEAKY_SOLUTION


def _receipts(run_dir) -> dict:
    return trust_scan_receipts(EventStore(run_dir / "events.jsonl").read_all())


@pytest.fixture(scope="module")
def clean_scanned_run(tmp_path_factory):
    """One real run, honest code, both static gates ON. This is the run whose log used to be empty."""
    run_dir = tmp_path_factory.mktemp("trust-scan-clean") / "run"
    engine = make_engine(run_dir, n_seeds=1, max_nodes=1,
                         code_leakage_detect=True, critic_check=True)
    state = anyio.run(engine.run)
    assert state.finished and state.evaluated_nodes()
    return run_dir, state


def test_a_clean_scan_is_now_distinguishable_from_no_scan(clean_scanned_run):
    """THE ITEM. Every evaluated node carries a receipt naming the detectors that looked, the bytes
    they looked at, and zero findings — so "checked, nothing found" is a durable claim.

    Against the pre-change tree this asserts `{} != {}` and fails, because no `trust_scan` row is
    ever written: the run's whole trust record was the absence of a `reward_hack_suspected`.
    """
    run_dir, state = clean_scanned_run
    receipts = _receipts(run_dir)
    evaluated = [node.id for node in state.evaluated_nodes()]
    assert evaluated, "the fixture produced no evaluated node to scan"
    assert set(receipts) == set(evaluated), (
        "an evaluated node has no scan receipt — its clean scan is indistinguishable from no scan")
    for node_id in evaluated:
        receipt = receipts[node_id]
        assert receipt["findings"] == 0
        assert TRUST_DETECTOR_CODE_LEAKAGE in receipt["detectors"]
        assert TRUST_DETECTOR_CRITIC in receipt["detectors"]
        assert trust_scan_status(receipt) == TRUST_SCAN_CLEAN


def test_the_receipt_commits_to_the_bytes_that_were_actually_scanned(clean_scanned_run):
    """`code_digest` is the claim's subject. Re-derived here from the node's OWN committed code
    through the engine's surface rule, so a receipt digesting something else (an empty string, the
    entrypoint only, a re-read of the file on disk) is a red test rather than a plausible hex
    string."""
    run_dir, state = clean_scanned_run
    engine = make_engine(run_dir.parent / "surface", n_seeds=1, max_nodes=1)
    for node in state.evaluated_nodes():
        expected = scan_subject_digest(engine._trust_scan_surface(node))
        assert _receipts(run_dir)[node.id]["code_digest"] == expected


def test_a_node_with_no_detector_configured_says_so_rather_than_reading_clean(tmp_path):
    """THE THIRD STATE, and the one four of the six preserved runs are actually in. Under the
    shipped defaults every detector is off; the receipt then names NO detector, and the reader
    answers `unscanned` — not `clean`, which would be the same false certificate one rung up."""
    run_dir = tmp_path / "unscanned"
    engine = make_engine(run_dir, n_seeds=1, max_nodes=1,
                         code_leakage_detect=False, critic_check=False,
                         reward_hack_detect=False)
    state = anyio.run(engine.run)
    assert state.evaluated_nodes()
    for node in state.evaluated_nodes():
        receipt = _receipts(run_dir)[node.id]
        assert receipt["detectors"] == [] and receipt["findings"] == 0
        assert trust_scan_status(receipt) == TRUST_SCAN_UNSCANNED


def test_a_flagged_node_gets_both_rows_and_they_name_one_subject(tmp_path):
    """The receipt does not replace `reward_hack_suspected`; it makes the pair complete. Both rows
    carry the digest from `scan_subject_digest`, which is what lets an auditor join "these bytes
    were scanned" to "these findings came out of them"."""
    run_dir = tmp_path / "flagged"
    engine = make_engine(run_dir, developer=_LeakyDeveloper(), n_seeds=1, max_nodes=1,
                         code_leakage_detect=True, critic_check=True)
    state = anyio.run(engine.run)
    assert state.evaluated_nodes()
    events = EventStore(run_dir / "events.jsonl").read_all()
    flagged = [event.data for event in events if event.type == "reward_hack_suspected"]
    assert flagged, "the positive control produced no findings at all"
    receipts = trust_scan_receipts(events)
    for row in flagged:
        receipt = receipts[row["node_id"]]
        assert receipt["code_digest"] == row["code_digest"]
        assert receipt["findings"] == len(row["signals"]) > 0
        assert trust_scan_status(receipt) == TRUST_SCAN_FLAGGED


def test_the_two_rows_take_their_digest_from_ONE_function(tmp_path, monkeypatch):
    """Driven by MOVING the rule, not by comparing two equal digests — equal is what two independent
    `hashlib.sha256(...)` calls look like right up until one of them gains a cap or a normalization.
    `scan_subject_digest` is replaced with a marker and BOTH rows have to move with it."""
    monkeypatch.setattr(scan_receipt_mod, "scan_subject_digest", lambda src: "deadbeefdeadbeef")
    run_dir = tmp_path / "one-digest"
    engine = make_engine(run_dir, developer=_LeakyDeveloper(), n_seeds=1, max_nodes=1,
                         code_leakage_detect=True, critic_check=True)
    assert anyio.run(engine.run).evaluated_nodes()
    events = EventStore(run_dir / "events.jsonl").read_all()
    digests = {event.data.get("code_digest")
               for event in events if event.type in (EV_TRUST_SCAN, "reward_hack_suspected")}
    assert digests == {"deadbeefdeadbeef"}, (
        "one of the two rows re-derives its own digest, so the pair can silently come to describe "
        "different bytes")


def test_the_receipt_carries_no_candidate_text(tmp_path):
    """CONSTRAINT: the receipt is about the SCAN, never about the code. A row that quoted what it
    read would put agent-authored text into the audit trail — the flagged row already owns that
    detail. Driven with a marker string the leaky solution contains."""
    marker = "X_test"
    run_dir = tmp_path / "no-text"
    engine = make_engine(run_dir, developer=_LeakyDeveloper(), n_seeds=1, max_nodes=1,
                         code_leakage_detect=True, critic_check=True)
    assert anyio.run(engine.run).evaluated_nodes()
    rows = [event.data for event in EventStore(run_dir / "events.jsonl").read_all()
            if event.type == EV_TRUST_SCAN]
    assert rows
    for row in rows:
        assert set(row) == {"node_id", "generation", "detectors", "findings",
                            "evidence_version", "code_digest"}
        assert marker not in repr(row)
        assert all(name in TRUST_DETECTORS for name in row["detectors"])


# ------------------------------------------------------------------ the reader-side default

def test_an_absent_receipt_reads_UNKNOWN_and_never_CLEAN():
    """ENGINE INVARIANT #5, and the exact inversion this item exists to prevent. Every log on this
    box predates the receipt; not one of them may read as scanned-and-clean."""
    assert trust_scan_status(None) == TRUST_SCAN_UNKNOWN
    assert trust_scan_status({}) == TRUST_SCAN_UNKNOWN
    # A malformed or half-written row is unknown too — nothing may fall THROUGH into `clean`.
    assert trust_scan_status({"findings": 0}) == TRUST_SCAN_UNKNOWN
    assert trust_scan_status({"detectors": ["critic"]}) == TRUST_SCAN_UNKNOWN
    assert trust_scan_status({"detectors": "critic", "findings": 0}) == TRUST_SCAN_UNKNOWN
    assert trust_scan_status({"detectors": ["critic"], "findings": True}) == TRUST_SCAN_UNKNOWN
    assert trust_scan_status({"detectors": ["critic"], "findings": -1}) == TRUST_SCAN_UNKNOWN
    # …and the two real answers.
    assert trust_scan_status({"detectors": [], "findings": 0}) == TRUST_SCAN_UNSCANNED
    assert trust_scan_status({"detectors": ["critic"], "findings": 0}) == TRUST_SCAN_CLEAN


def test_an_old_log_summarizes_as_unknown_rather_than_silent(clean_scanned_run):
    """`looplab inspect`'s line, over a log with the receipts removed — which is what every preserved
    run is. It must state the unknown count, because a summary that simply omitted them would
    reproduce the silence in a nicer font."""
    run_dir, state = clean_scanned_run
    events = EventStore(run_dir / "events.jsonl").read_all()
    ids = [node.id for node in state.evaluated_nodes()]
    old = [event for event in events if event.type != EV_TRUST_SCAN]
    line = trust_scan_summary(old, ids)
    assert "NO scan receipt" in line and "unknown, not clean" in line
    assert "scanned clean" not in line
    assert "scanned clean" in trust_scan_summary(events, ids)


# ------------------------------------------------------------------ what the receipt may not move

def test_the_receipt_is_fold_ignored_so_no_selection_can_move(clean_scanned_run):
    """`trust_gate` is `audit` on this box and nothing here may change a selection today. Two ways of
    saying it, and the second is the one that holds: the type is in `DIAGNOSTIC_EVENTS` (so the fold
    has no handler and `_proposal_authority_seq` skips it), and the run folds byte-identically with
    the rows removed."""
    assert EV_TRUST_SCAN in DIAGNOSTIC_EVENTS
    run_dir, _state = clean_scanned_run
    events = EventStore(run_dir / "events.jsonl").read_all()
    with_rows = fold(events)
    without = fold([event for event in events if event.type != EV_TRUST_SCAN])
    assert with_rows.model_dump(mode="json") == without.model_dump(mode="json")
    assert with_rows.reward_hacks == [] and with_rows.best_node_id == without.best_node_id


def test_the_detector_list_is_the_scan_s_own_decision_not_a_second_copy(tmp_path):
    """The receipt's claim is "these detectors looked", and the only way that claim can be true is
    if the scan branches on the SAME value. Both directions, driven rather than pinned: the names
    `_trust_scan_detectors` reports are exactly the ones that can contribute a signal, and turning a
    flag off removes its name from the receipt AND its namespace from the findings."""
    both = make_engine(tmp_path / "both", n_seeds=1, max_nodes=1,
                       code_leakage_detect=True, critic_check=True, reward_hack_detect=True,
                       workdir_audit=True)
    names = both._trust_scan_detectors(_LEAKY_SOLUTION)
    assert set(names) == {TRUST_DETECTOR_REWARD_HACK, TRUST_DETECTOR_WORKDIR_AUDIT,
                          TRUST_DETECTOR_CODE_LEAKAGE, TRUST_DETECTOR_CRITIC}
    assert list(names) == [name for name in TRUST_DETECTORS if name in set(names)], "order is the contract"

    # Every OTHER detector must be named OFF explicitly: since 2026-08-23 `reward_hack_detect`
    # defaults ON (and `workdir_audit` always did), so a test that names only the one it wants would
    # be asserting the DEFAULTS rather than the composition rule it is about.
    leakage_only = make_engine(tmp_path / "leak", n_seeds=1, max_nodes=1,
                               code_leakage_detect=True, critic_check=False,
                               reward_hack_detect=False, workdir_audit=False)
    assert set(leakage_only._trust_scan_detectors(_LEAKY_SOLUTION)) == {TRUST_DETECTOR_CODE_LEAKAGE}
    assert {row["signal"].split(":")[0]
            for row in leakage_only._trust_gate_signals(
                _NODE_FOR_GATE, _LEAKY_SOLUTION)} == {"data_leakage"}

    # An empty surface is not something either static gate has an opinion about, and the receipt
    # must not claim they looked at it.
    assert leakage_only._trust_scan_detectors("") == ()
