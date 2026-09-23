"""DR-04: verification DIRECTS the next round instead of annotating the last one.

THE DEFECT, IN ONE SENTENCE FROM THE ROADMAP ITSELF (doc 28 DR-04): "a verifier warning currently
annotates a memo but does not close the evidence gap". Everything needed to act on a verdict is
already on the record — `trust/memo_verify.py::verify_memo` returns per-claim verdicts and
`number_fidelity_report` returns the three number channels — and nothing reads either to decide what
to do NEXT.

MEASURED TWICE, and the second measurement REORDERED this module. The first cut ranked
`unsupported` first, on the reading that a claim with no support is the most decisive gap. Replayed
over all 237 routable memos on this box it sent **233 of them (98.3%) to `retrieve`** — a router
that is a constant function, and constant on the CHEAPEST action. The cause was already written in
this file's own first paragraph and not acted on: `verify_memo` merges two passes into one verdict
list, the deterministic one answering about the CITATION and the LLM one about the SUPPORT, and both
write the word `unsupported`. Over those memos there are 1421 `unsupported` verdicts whose notes are
dominated by citation defects (`no evidence cited` 340, `cited source URL was not consulted` 116,
`cited experiments do not exist` ~90).

THE FIX WAS A FIELD, NOT A RANKING TWEAK. No structural channel distinguished the two: the obvious
proxy — "does the row carry an evidence receipt?" — agrees with the note on 686 of 1421 (48.3%), no
better than chance, because 659 rows have BOTH a receipt and a citation-shaped note. So
`memo_verify.VERDICT_KINDS` now stamps `kind` at the site that decides it, and this router keys on
that rather than on our own note text — the channel `engine/failure_diagnosis.py` forbids reading a
held fact through. A row with no `kind` is an old log and reads as `citation`.

AFTER THE FIELD, RE-MEASURED over the same 237 memos — the router is no longer a constant:

    retrieve       65   27.4%   (was 233 / 98.3%)
    narrow        170   71.7%   (was   2 /  0.8%)
    contradiction   0    0.0%   UNEXERCISED on this corpus, not "never happens"
    refresh         0    0.0%   UNEXERCISED on this corpus, not "never happens"
    finalize        2    0.8%   both `clean`

The two zeros are stated as UNEXERCISED deliberately. `contradictions` and `stale_evidence` are
arguments this router is HANDED, and nothing on this box computes either: contradiction needs a
claim-against-claim comparison nobody has built, and staleness needs the exact-span evidence ledger,
which every run in this corpus predates (`research_evidence` is empty on all eleven). A 0% that
means "no detector fed it" must not be read as a rung that never fires — that is the vacuous-green
reading `core/claimpin.py`'s denominator rule exists to refuse, one surface over.

The number channels stay the sharp instrument for the middle case: the corpus pass
(`docs/audit/memo-number-fidelity.md` section 4.1) reports quoted=5267 cited=1191 run=250 none=3826,
and the `run` channel — a real metric of THIS run attributed to an experiment the claim does not
cite — means "the support is unclear", not "the support is missing".

FIVE ACTIONS, CLOSED, and the ORDER is a cost order the measurement above fixed:

  `retrieve`      missing coverage      - the verifier READ the evidence and it does not carry the
                                          claim (`kind="support"`). The most decisive gap.
  `narrow`        unclear support       - the claim quotes a number that is REAL in this run but
                                          belongs to an experiment it does not cite. Either cite
                                          that experiment or narrow the claim to what it can carry.
  `contradiction` both sides present    - claims disagree with each other; surface both, do not pick.
  `refresh`       stale evidence        - an evidence id no longer resolves; re-read that branch only.
  `retrieve`      a bad footnote        - the claim cites nothing that resolves (`kind="citation"`).
                                          Same action, ranked LAST: it says the footnote is wrong,
                                          not that the finding is.
  `finalize`      clean, or spent       - nothing actionable is left, OR the loop's bound is reached.

`finalize` carrying TWO meanings is deliberate and is the one thing this module refuses to blur:
`Routing.spent` says which, and `Routing.residual` carries what was still open when a bound ended
the loop. The roadmap's own red line — "the system must never turn exhausted budget into a clean
trust badge" — is a property of the RECORD, so it is a field here and not a comment.

DETERMINISTIC AND FREE. No model call, no I/O: the router reads two dicts the run already paid for.
That is what makes it safe to run on every memo, and what keeps the decision reviewable — a routing
a reader disagrees with can be re-derived from the same two dicts.
"""
from __future__ import annotations

import hashlib
from typing import NamedTuple, Optional

# The closed vocabulary. A registry rather than five string literals for the reason
# `engine/triage.py::TRIAGE_ACTIONS` is one: a typo'd action does not fail, it lands on a durable
# row and reads as a decision nobody can look up. `tests/test_verifier_routing.py` re-derives this
# set from this module's own `Routing(...)` constructions in BOTH directions.
NEXT_ACTIONS: tuple[str, ...] = ("retrieve", "narrow", "contradiction", "refresh", "finalize")

# Why a round ended, when it ended in `finalize`. Also closed, also for the same reason - and
# deliberately NOT folded into `NEXT_ACTIONS`: "what to do next" and "why there is no next" are
# different questions, and one vocabulary answering both is how `inert` came to mean two failures
# in `node_repaired` (see `engine/repair_verify.py`).
SPENT_REASONS: tuple[str, ...] = ("clean", "rounds", "budget", "no_progress")

# A bound on the loop, not a tuning knob: the roadmap requires the revision loop to be bounded by
# revision count AND remaining budget AND a no-progress detector, and any one of the three missing
# makes the other two decorative.
DEFAULT_MAX_ROUNDS = 2

# The separator the progress digest joins its three parts with. A printable sentinel rather than a
# control byte: this string is hashed, never displayed, and a literal NUL in the source is the kind
# of thing that survives a copy badly.
_SEP = "|#|"


class Routing(NamedTuple):
    """What verification says to do next, and the evidence the answer rests on.

    `reason` is prose for a human; `counts` is the machine half — the numbers the action was chosen
    from, so a reader can re-derive the decision without re-running the verifier. `spent` is set
    only on `finalize` and names WHICH finalize this is (`SPENT_REASONS`); `residual` is what was
    still open when a bound ended the loop, and it is non-empty whenever a bound ended it with a gap
    still open — the record that stops an exhausted budget reading as a clean badge.
    """
    action: str
    reason: str
    counts: dict
    spent: Optional[str] = None
    residual: tuple = ()

    @property
    def is_terminal(self) -> bool:
        return self.action == "finalize"


def _verdict_counts(verification: Optional[dict]) -> dict:
    """The verdict histogram, tolerant of a block that is absent or shaped by an older writer.

    Absence is NOT zero, and this is the same rule `number_fidelity_report` states for a memo with
    no claims: a memo nobody verified and a memo that verified clean must not read alike, so
    `verified` carries the total this pass counted and `ran` says whether there was a block at all.
    """
    block = verification if isinstance(verification, dict) else {}
    rows = block.get("verdicts")
    rows = rows if isinstance(rows, (list, tuple)) else ()
    out = {"ran": bool(block), "verified": 0, "unsupported": 0, "unclear": 0, "cited": 0,
           "supported": 0, "unsupported_support": 0, "unsupported_citation": 0}
    for row in rows:
        if not isinstance(row, dict):
            continue
        out["verified"] += 1
        verdict = str(row.get("verdict") or "")
        if verdict in out and verdict not in ("ran", "verified"):
            out[verdict] += 1
        if verdict == "unsupported":
            # WHICH QUESTION THE ROW ANSWERED (`memo_verify.VERDICT_KINDS`). A row with no `kind`
            # is an OLD LOG and defaults to `citation` — the conservative reading, because claiming
            # the verifier judged SUPPORT when the field that says so is absent is exactly the
            # over-claim invariant #5's reader-side defaults exist to prevent.
            kind = str(row.get("kind") or "citation")
            out["unsupported_support" if kind == "support" else "unsupported_citation"] += 1
    return out


def _fidelity_counts(fidelity: Optional[dict]) -> dict:
    """The three number channels, tolerant the same way. `ran` is False for a memo whose claims
    quote no decimal at all — which `number_fidelity_report` reports as `fidelity: None` precisely
    so it cannot be read as "nothing matched"."""
    block = fidelity if isinstance(fidelity, dict) else {}

    def _count(key: str) -> int:
        value = block.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return {"ran": bool(block), "quoted": _count("quoted"), "matched": _count("matched"),
            "elsewhere": _count("elsewhere"), "unmatched": _count("unmatched")}


def progress_digest(open_gaps, evidence_ids, draft: str = "") -> str:
    """The no-progress key the roadmap names: `(open_gaps, evidence_ids, draft_digest)`.

    SORTED, so two rounds that reached the same state by different orders compare equal — a round
    that merely re-orders its gaps has made no progress and must not buy another. The draft rides
    as part of the hash rather than verbatim because its LENGTH is not progress either: a round that
    re-words the same synthesis over the same evidence is the case this detector exists to stop.
    """
    gaps = sorted(str(gap).strip().casefold() for gap in (open_gaps or ()) if str(gap).strip())
    ids = sorted(str(item).strip() for item in (evidence_ids or ()) if str(item).strip())
    body = _SEP.join(gaps) + _SEP + _SEP.join(ids) + _SEP + (draft or "")
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]


def open_gap_lines(verdicts: dict, numbers: dict, stale_evidence, contradictions) -> list[str]:
    """Everything verification left open, in one vocabulary, computed ONCE.

    Shared by the action choice and by `residual` on purpose: a `finalize` that claimed `clean`
    while an open gap existed would be the exhausted-budget-as-trust-badge failure with extra steps,
    and two derivations of "what is open" is how the two would come to disagree.
    """
    gaps: list[str] = []
    if verdicts["unsupported_support"]:
        gaps.append(f"{verdicts['unsupported_support']} claim(s) the evidence does not carry")
    if verdicts["unsupported_citation"]:
        gaps.append(f"{verdicts['unsupported_citation']} claim(s) with no usable citation")
    if numbers["elsewhere"]:
        gaps.append(f"{numbers['elsewhere']} number(s) real in this run but not in a cited experiment")
    if contradictions:
        gaps.append(f"{len(contradictions)} contradiction(s) between claims")
    if stale_evidence:
        gaps.append(f"{len(stale_evidence)} evidence item(s) that no longer resolve")
    if verdicts["unclear"]:
        gaps.append(f"{verdicts['unclear']} claim(s) whose support is unclear")
    return gaps


def route_after_verification(
    verification: Optional[dict],
    fidelity: Optional[dict] = None,
    *,
    stale_evidence: tuple = (),
    contradictions: tuple = (),
    rounds_used: int = 0,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    budget_remaining: bool = True,
    previous_digest: Optional[str] = None,
    current_digest: Optional[str] = None,
) -> Routing:
    """Which of `NEXT_ACTIONS` verification asks for, given what it found and what is left to spend.

    THE BOUNDS ARE CHECKED FIRST, and that order is the design rather than convenience: a loop that
    decides what to do and then discovers it cannot afford it has already spent the decision, and —
    worse — would report the action it wanted as the action it took. So `budget`, `rounds` and
    `no_progress` terminate BEFORE any gap is read, and each names itself in `spent`.

    The gap order is a COST order: `retrieve` (a claim cites nothing) is the cheapest and most
    decisive, `narrow` next (the evidence exists and the claim is pointed at the wrong one), then
    `contradiction` (nothing to fetch — two sides to surface), then `refresh` (re-read one branch).
    A memo can be in several of these states at once; the router returns ONE action because the loop
    takes one step, and the ones not taken ride in `residual` so a terminal memo names what it did
    not close.
    """
    verdicts = _verdict_counts(verification)
    numbers = _fidelity_counts(fidelity)
    counts = {"verdicts": verdicts, "numbers": numbers, "rounds_used": rounds_used,
              "stale": len(stale_evidence or ()), "contradictions": len(contradictions or ())}
    gaps = open_gap_lines(verdicts, numbers, stale_evidence, contradictions)

    if not budget_remaining:
        return Routing("finalize", "the episode budget is spent", counts,
                       spent="budget", residual=tuple(gaps))
    if rounds_used >= max_rounds:
        return Routing("finalize", f"the revision bound of {max_rounds} round(s) is reached",
                       counts, spent="rounds", residual=tuple(gaps))
    if (previous_digest is not None and current_digest is not None
            and previous_digest == current_digest):
        return Routing("finalize", "the last round changed no gap, no evidence and no draft",
                       counts, spent="no_progress", residual=tuple(gaps))

    if verdicts["unsupported_support"]:
        return Routing("retrieve",
                       f"{verdicts['unsupported_support']} claim(s) the evidence the verifier READ "
                       "does not carry", counts,
                       residual=tuple(g for g in gaps if "does not carry" not in g))
    if numbers["elsewhere"]:
        return Routing("narrow",
                       f"{numbers['elsewhere']} quoted number(s) are real in this run but belong "
                       "to an experiment the claim does not cite", counts,
                       residual=tuple(g for g in gaps if "not in a cited experiment" not in g))
    if contradictions:
        return Routing("contradiction",
                       f"{len(contradictions)} pair(s) of claims disagree; surface both sides",
                       counts, residual=tuple(g for g in gaps if "contradiction" not in g))
    if stale_evidence:
        return Routing("refresh",
                       f"{len(stale_evidence)} evidence item(s) no longer resolve; re-read that "
                       "branch only", counts,
                       residual=tuple(g for g in gaps if "no longer resolve" not in g))
    if verdicts["unclear"]:
        return Routing("narrow",
                       f"{verdicts['unclear']} claim(s) whose support the verifier could not settle",
                       counts, residual=tuple(g for g in gaps if "unclear" not in g))
    # THE CITATION RUNG IS LAST, and its position is the whole lesson of this module's first
    # measurement. Ranked FIRST — which is where "unsupported" put it before `kind` existed — the
    # router routed **233 of 237** real memos to `retrieve` (98.3%), because nearly every memo
    # carries at least one citation defect and a citation defect is the CHEAPEST and least decisive
    # of the five actions: it says the footnote is wrong, not that the finding is. A directed
    # revision loop that spends its rounds re-citing has spent them on the thing a reader can
    # already see on the row. So a bad footnote is actionable, and it is actionable LAST.
    if verdicts["unsupported_citation"]:
        return Routing("retrieve",
                       f"{verdicts['unsupported_citation']} claim(s) cite nothing that resolves",
                       counts,
                       residual=tuple(g for g in gaps if "no usable citation" not in g))
    return Routing("finalize", "verification found nothing actionable", counts,
                   spent="clean", residual=())
