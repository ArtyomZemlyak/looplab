"""The BELIEF-KEY DISAGREEMENT instrument: where the text key and a concept key would differ.

`card_ledger.py::_apply_card_belief_lineage` sets `Card.belief_id =
hypothesis_statement_digest(seed)` — a digest of the seed TEXT. The sibling design (#58) proposes
belief identity = {concepts} + metric + direction instead, and the corpus measurement that motivated
it (docs/BACKLOG.md, 2026-08-26) is that the two keys DISAGREE on a fifth of the tagged board: 528
tagged cards, 84 (run, concept-set, direction) groups, 18 of them split into more than one
`belief_id`.

DISAGREEMENT IS NOT EVIDENCE THAT THE CONCEPT KEY IS RIGHT, and that gap is the whole risk the
backlog entry refuses to take on faith: an identical concept SET does not establish an identical
BELIEF — "temperature 0.05" and "temperature 0.01" are two positions on one axis, not one claim —
so merging them POOLS the evidence of two different experiments under one verdict, which is
strictly worse than today's fragmentation. What settles it is reading the split groups and counting
genuine restatements against genuine distinctions. This module is that READ, and only the read: it
groups by concept-equality, reports which groups the text key splits, and prints the statements a
semantic key WOULD merge, with the evidence and the verdicts that merge would pool.

IT CHANGES NO KEY AND DECIDES NOTHING. `belief_id` is read by
`engine/card_reservation.py::_retry_attach_card` as the "same question?" test, so a key change is a
selection change; this module is imported by one CLI command (`looplab belief-key-split`) and by
nothing in the run path.

Pure and deterministic like every other projection here (engine invariant 5): it takes ALREADY
FOLDED `RunState`s — one `fold` per run, the caller's — does no I/O, and imports only `core`.

TWO STATED SCOPE LIMITS, both of which shrink what the report may claim:

* the group key is (run, concept set, direction) and NOT (concepts, metric, direction). Within one
  run the objective and its direction are constant, so adding the metric changes no group here;
  merging ACROSS runs is a bigger claim than the one under review (two runs' "same" concepts were
  tagged by different classifier passes against different data) and this instrument does not make
  it. `core/run_identity.py::run_ref` is the grouping identity, so two incarnations of one
  directory name stay two runs.
* a card with no concept tags, or with no seed statement (hence no `belief_id`), can neither agree
  nor disagree — both are counted and EXCLUDED rather than bucketed, because a bucket of "unknown"
  would read as a group the two keys agree on.
"""
from __future__ import annotations

from typing import Iterable

from looplab.core.run_identity import run_ref

# How the groups are formed and what a "split" is, printed WITH the numbers so a reader never has to
# re-derive the rule from this source to know what the counts mean.
BELIEF_KEY_SPLIT_RULE = (
    "group tagged cards by (run, concept set, direction); a group is SPLIT when its cards carry "
    "more than one `belief_id` (the seed-TEXT digest). A split group is what a concepts-keyed "
    "identity would MERGE — and a merge pools the members' evidence under one verdict.")

# Display bounds. This is a reading instrument for a human deciding a key change, so the STATEMENTS
# are what matter and the id lists are context; both are clipped rather than paged.
_STATEMENT_CHARS = 200
_EVIDENCE_SHOWN = 12
_CARDS_SHOWN = 12


def _concept_key(card) -> tuple[str, ...]:
    """The card's concept SET as a sorted tuple — the semantic key's own coordinates.

    `Card.concept_tags` is the AUTHORED membership (`CardConceptSource` names who claimed it) and
    deliberately not `child_concept_tags`, which is a DERIVED union over a direction's children: a
    parent's derived union is not a claim anybody made about the parent.
    """
    return tuple(sorted({t for t in (getattr(card, "concept_tags", None) or [])
                         if isinstance(t, str) and t}))


def _statement(card) -> str:
    seed = getattr(card, "seed_statement", "") or getattr(card, "statement", "") or ""
    return str(seed).strip()[:_STATEMENT_CHARS]


def belief_key_split_report(runs: Iterable[tuple[str, object]]) -> dict:
    """The disagreement read over a corpus: `runs` is an iterable of `(label, RunState)` pairs.

    The caller owns the fold and the walk of the runs root — this module never opens a file, so a
    test can drive it from event lists and the CLI from `runs/**/events.jsonl`.
    """
    # (run_ref, direction, concepts) -> belief_id -> the cards that carry it. Insertion order is the
    # caller's run order and then card-id order, so the report is stable across invocations.
    groups: dict[tuple, dict[str, dict]] = {}
    labels: dict[tuple, str] = {}
    runs_seen = cards = tagged = untagged = unkeyed = 0

    for label, st in runs:
        runs_seen += 1
        ref = run_ref(getattr(st, "run_uid", "") or "", getattr(st, "run_id", "") or str(label))
        direction = str(getattr(st, "direction", "") or "")
        for cid, card in sorted((getattr(st, "cards", None) or {}).items()):
            cards += 1
            concepts = _concept_key(card)
            if not concepts:
                untagged += 1
                continue
            tagged += 1
            belief = getattr(card, "belief_id", None)
            if not isinstance(belief, str) or not belief:
                # No seed statement, so the TEXT key has no opinion about this card at all. Counted
                # and dropped: admitting it as its own belief would manufacture a split.
                unkeyed += 1
                continue
            key = (ref, direction, concepts)
            labels.setdefault(key, str(label))
            slot = groups.setdefault(key, {}).setdefault(belief, {
                "belief_id": belief, "statement": _statement(card), "cards": [],
                "evidence": [], "verdicts": {}, "statuses": {}})
            slot["cards"].append(str(cid))
            slot["evidence"].extend(int(n) for n in (getattr(card, "evidence", None) or [])
                                    if isinstance(n, int) and not isinstance(n, bool))
            verdict = str(getattr(card, "verdict", "") or "unknown")
            slot["verdicts"][verdict] = slot["verdicts"].get(verdict, 0) + 1
            status = str(getattr(card, "status", "") or "unknown")
            slot["statuses"][status] = slot["statuses"].get(status, 0) + 1

    histogram: dict[str, int] = {}
    merges: list[dict] = []
    split_cards = conflicting = 0
    for key, beliefs in groups.items():
        ref, direction, concepts = key
        distinct = len(beliefs)
        histogram[str(distinct)] = histogram.get(str(distinct), 0) + 1
        if distinct < 2:
            continue
        rows = sorted(beliefs.values(), key=lambda b: (-len(b["cards"]), b["belief_id"]))
        group_cards = sum(len(b["cards"]) for b in rows)
        split_cards += group_cards
        # THE POOLING RISK, flagged and not judged: a merge writes one verdict over cards whose own
        # verdicts already differ. It does not say "this merge is wrong" — it names the group a
        # human has to read first, which is the whole job of this instrument.
        verdicts: dict[str, int] = {}
        for row in rows:
            for verdict, count in row["verdicts"].items():
                verdicts[verdict] = verdicts.get(verdict, 0) + count
        if len(verdicts) > 1:
            conflicting += 1
        merges.append({
            "run": labels.get(key, ""), "run_ref": ref, "direction": direction,
            "concepts": list(concepts), "belief_ids": distinct, "cards": group_cards,
            "evidence_nodes": sum(len(b["evidence"]) for b in rows),
            "verdicts": verdicts, "conflicting_verdicts": len(verdicts) > 1,
            "beliefs": [{
                "belief_id": row["belief_id"][:16], "statement": row["statement"],
                "cards": row["cards"][:_CARDS_SHOWN], "card_count": len(row["cards"]),
                "evidence": sorted(set(row["evidence"]))[:_EVIDENCE_SHOWN],
                "evidence_count": len(row["evidence"]),
                "verdicts": row["verdicts"], "statuses": row["statuses"]} for row in rows],
        })

    merges.sort(key=lambda m: (-m["belief_ids"], -m["cards"], m["run"], tuple(m["concepts"])))
    return {
        "rule": BELIEF_KEY_SPLIT_RULE,
        "runs": runs_seen, "cards": cards, "tagged": tagged, "untagged": untagged,
        "unkeyed": unkeyed,
        "groups": len(groups), "split_groups": len(merges), "split_cards": split_cards,
        "conflicting_verdict_groups": conflicting,
        "distinct_belief_ids": {k: histogram[k] for k in sorted(histogram, key=int)},
        "merges": merges,
    }
