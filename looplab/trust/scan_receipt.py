"""A CLEAN SCAN IS A RESULT, AND UNTIL NOW IT WAS THE ABSENCE OF ONE.

`reward_hack.py`, `leakage.py::code_leakage_findings`, `critic.py::critic_findings`, the hardened
exploit suite and the workdir-write audit all run on an evaluated node and write to the log ONLY on
a hit (`engine/evaluate.py`: `if sigs:` → one `reward_hack_suspected` carrying the union). So a run
in which every node was scanned and found clean is BYTE-IDENTICAL to a run in which the scan call
was deleted, and identical again to a run whose detectors were all switched off. The 2026-08-05
mutation audit measured exactly that: deleting both `sigs +=` lines left 117 trust tests green.

**MEASURED OVER `runs/` ON 2026-08-19, and it is not a hypothetical.** Six preserved logs, all with
`trust_gate="audit"`. Two of them (`rubertlite-dr-unified-v9`, `e5small-dr-unified-v2`) carry
`reward_hack_detect=true` + `code_leakage_detect=true` and 9 evaluated nodes between them, and both
logs contain ZERO trust rows — nine scans that happened and left nothing. Four (`rubertlite-dense-
retrieval`, `-v6`, `-v7`, `-v8`) carry `reward_hack_detect=false` + `code_leakage_detect=false` over
100 evaluated nodes, so on those the reward-hack and leakage detectors did not run AT ALL — and the
BACKLOG's own §0.15 ledger says they "run unconditionally per evaluated node", which the snapshots
falsify. Those two populations — scanned-and-clean, and never-scanned — are the two an auditor most
needs to tell apart, this box holds both, and nothing in the log distinguishes them.

WHAT THE RECEIPT SAYS, AND WHAT IT DELIBERATELY DOES NOT.

`trust_scan` is a claim about the SCAN, never about the code: which detectors ran, over which bytes
(the digest of the exact scanned surface), and how many findings came back. No candidate text, no
signal payload, no rule name reaches it — a receipt that quoted what it read would be a second copy
of the artifact, and the flagged case already has `reward_hack_suspected` for the detail. The digest
is the SAME value that event carries, from this module's `scan_subject_digest`, so a reader can join
"these bytes were scanned" to "these findings came out of them" and a change to one cannot silently
leave the other behind.

THE READER-SIDE DEFAULT IS THE WHOLE POINT (engine invariant #5). An old log has no `trust_scan`
row, and the one answer that must never be produced for it is "clean" — that inversion is the exact
defect this closes. `trust_scan_status` therefore answers `unknown` for a node with no receipt and
reserves `clean` for a node that has one, with detectors that actually ran and zero findings. A node
whose receipt lists NO detectors is `unscanned`: the engine reached the scan point and every detector
was configured off, which is a different fact from both of the others and is the state four of the
six preserved runs are in.

The event is in `DIAGNOSTIC_EVENTS`: the fold never reads it, so no selection moves, and — the
load-bearing half, per invariant #1 — `engine/speculation.py::_proposal_authority_seq` excludes
`DIAGNOSTIC_EVENTS` wholesale, so a receipt landing inside a reservation's CAS window cannot lose it.
Since 2026-08-20 that fence no longer spans a paid proposal at all
(`engine/card_reservation.py::_proposal_receipt_fence`), so the exclusion buys a retry rather than a
Developer call — measured, `trust_scan` was inside 52 of the 56 windows the old fence discarded.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

from looplab.events.types import EV_TRUST_SCAN

# WHICH DETECTORS THE ENGINE CAN RUN OVER ONE EVALUATED NODE, as a closed vocabulary in the order
# their findings concatenate into the union event. `engine/evaluate.py::_trust_scan_detectors` is the
# ONE place that decides which of them ran, and `_trust_scan_signals`/`_trust_gate_signals` branch on
# its answer rather than re-reading the settings — because a receipt that says "code_leakage ran" is
# a lie the moment the receipt's predicate and the scan's predicate are two copies. Both halves are
# guarded two-way by `tests/test_trust_scan_receipt.py`.
#
#   reward_hack    — `trust/reward_hack.py::detect_reward_hacks` (`Settings.reward_hack_detect`)
#   exploit_suite  — the hardened ruleset grown by `looplab harden`, when one is loaded
#   workdir_audit  — runtime writes to protected/frozen files (`Settings.workdir_audit`)
#   code_leakage   — `trust/leakage.py::code_leakage_findings` (`Settings.code_leakage_detect`)
#   critic         — `trust/critic.py::critic_findings` (`Settings.critic_check`)
TRUST_DETECTOR_REWARD_HACK = "reward_hack"
TRUST_DETECTOR_EXPLOIT_SUITE = "exploit_suite"
TRUST_DETECTOR_WORKDIR_AUDIT = "workdir_audit"
TRUST_DETECTOR_CODE_LEAKAGE = "code_leakage"
TRUST_DETECTOR_CRITIC = "critic"
TRUST_DETECTORS: tuple[str, ...] = (
    TRUST_DETECTOR_REWARD_HACK, TRUST_DETECTOR_EXPLOIT_SUITE, TRUST_DETECTOR_WORKDIR_AUDIT,
    TRUST_DETECTOR_CODE_LEAKAGE, TRUST_DETECTOR_CRITIC)

# The schema version of the receipt payload, shared with `reward_hack_suspected`'s `evidence_version`
# because the two rows commit to the same subject under the same digest rule.
TRUST_SCAN_EVIDENCE_VERSION = 1

# WHAT A READER MAY CONCLUDE ABOUT ONE NODE. `unknown` is the default and is not a failure state —
# it is the honest answer for every log written before this receipt existed, which is every log on
# this box today.
TRUST_SCAN_UNKNOWN = "unknown"      # no receipt: nobody can say whether anything looked
TRUST_SCAN_UNSCANNED = "unscanned"  # a receipt, and it says no detector was configured on
TRUST_SCAN_CLEAN = "clean"          # detectors ran over named bytes and returned nothing
TRUST_SCAN_FLAGGED = "flagged"      # detectors ran and returned findings (see reward_hack_suspected)
TRUST_SCAN_STATUSES: tuple[str, ...] = (
    TRUST_SCAN_UNKNOWN, TRUST_SCAN_UNSCANNED, TRUST_SCAN_CLEAN, TRUST_SCAN_FLAGGED)


def scan_subject_digest(scan_src: str) -> str:
    """The digest of the bytes a node's trust scan actually read.

    A rule with a name for the reason `_trust_scan_surface` is one: the receipt and the
    `reward_hack_suspected` row must commit to the SAME subject, and two inline `hashlib.sha256(...)`
    calls in one function are two rules that agree only until someone edits one of them. 16 hex
    chars, which is what the flagged row has published since P1-7 — widening it here would silently
    stop the join it exists to support.
    """
    return hashlib.sha256(str(scan_src).encode("utf-8", "replace")).hexdigest()[:16]


def trust_scan_receipt(node_id, generation, detectors: Iterable[str], findings: int,
                       scan_src: str) -> dict:
    """The `trust_scan` payload for one evaluated node. Ordered by `TRUST_DETECTORS`, not by the
    caller, so two runs that scanned the same way produce the same row."""
    ran = set(str(name) for name in detectors)
    return {"node_id": node_id, "generation": generation,
            "detectors": [name for name in TRUST_DETECTORS if name in ran],
            "findings": int(findings),
            "evidence_version": TRUST_SCAN_EVIDENCE_VERSION,
            "code_digest": scan_subject_digest(scan_src)}


def trust_scan_receipts(events: Iterable) -> dict:
    """`{node_id: payload}` for every `trust_scan` row in a raw event list.

    Last row wins: a node re-run in place (`node_reset`) scans its new code, and the receipt an
    auditor wants is the one for the attempt whose terminal stands. Reads the RAW log rather than a
    folded state on purpose — the fold ignores this type, and it must keep ignoring it.
    """
    out: dict = {}
    for event in events or ():
        if getattr(event, "type", None) != EV_TRUST_SCAN:
            continue
        data = getattr(event, "data", None) or {}
        node_id = data.get("node_id")
        if node_id is None:
            continue
        out[node_id] = data
    return out


def trust_scan_status(receipt) -> str:
    """One of `TRUST_SCAN_STATUSES` for a node's receipt (`None` when it has none).

    THE DEFAULT IS THE CONTRACT: absence answers `unknown`, and a malformed/incomplete row answers
    `unknown` too rather than falling through to `clean`. Nothing in this function can produce
    `clean` without a receipt that names at least one detector and zero findings.
    """
    if not isinstance(receipt, dict):
        return TRUST_SCAN_UNKNOWN
    detectors = receipt.get("detectors")
    if not isinstance(detectors, list):
        return TRUST_SCAN_UNKNOWN
    findings = receipt.get("findings")
    if not isinstance(findings, int) or isinstance(findings, bool) or findings < 0:
        return TRUST_SCAN_UNKNOWN
    if not detectors:
        return TRUST_SCAN_UNSCANNED
    return TRUST_SCAN_FLAGGED if findings else TRUST_SCAN_CLEAN


def trust_scan_summary(events: Iterable, evaluated_ids: Iterable) -> str:
    """One operator-readable line: what this run's log can and cannot say about its trust scans.

    Written for `looplab inspect`, which is where someone goes when they are asking the question this
    module exists for. It states the UNKNOWN count first when there is one, because that is the
    reading a bare "0 findings" would otherwise be mistaken for.
    """
    receipts = trust_scan_receipts(events)
    ids = sorted(set(evaluated_ids or ()), key=lambda value: (value is None, value))
    if not ids:
        return "trust scan: no evaluated nodes."
    buckets: dict[str, list] = {status: [] for status in TRUST_SCAN_STATUSES}
    detectors: set[str] = set()
    for node_id in ids:
        receipt = receipts.get(node_id)
        buckets[trust_scan_status(receipt)].append(node_id)
        if isinstance(receipt, dict) and isinstance(receipt.get("detectors"), list):
            detectors.update(str(name) for name in receipt["detectors"])
    total = len(ids)
    parts = [f"trust scan: {total} evaluated node(s)"]
    if buckets[TRUST_SCAN_UNKNOWN]:
        parts.append(f"{len(buckets[TRUST_SCAN_UNKNOWN])} with NO scan receipt — unknown, "
                     "not clean (a log written before the receipt existed, or a scan that "
                     "never ran)")
    if buckets[TRUST_SCAN_UNSCANNED]:
        parts.append(f"{len(buckets[TRUST_SCAN_UNSCANNED])} scanned by NO detector "
                     "(all of them configured off)")
    if buckets[TRUST_SCAN_CLEAN]:
        parts.append(f"{len(buckets[TRUST_SCAN_CLEAN])} scanned clean")
    if buckets[TRUST_SCAN_FLAGGED]:
        parts.append(f"{len(buckets[TRUST_SCAN_FLAGGED])} with findings")
    if detectors:
        parts.append("detectors: "
                     + ", ".join(name for name in TRUST_DETECTORS if name in detectors))
    return "; ".join(parts) + "."
