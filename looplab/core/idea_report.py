"""What a node actually BUILT, as its Developer reported it on `done`.

Measured 2026-09-25 on MiniOneRec inf12: three nodes recorded under "de-duplicate the first decode
step" (13, 14, 17) contained no de-duplication at all — two a one-flag revert, one a fused MLP —
and nothing said so. Each got a metric, its Card read VERDICT=tested, the novelty gate rejected the
idea four more times as "already tried", and the Researcher, reading the code, re-proposed it. An
activation marker cannot catch this: a substitute path prints its own markers and passes. So the
Developer's `done` states whether it built the idea it was given, and what it built instead.

AND THE CARD STOPS COUNTING SUCH A NODE AS A TEST OF ITS CLAIM (`events/card_ledger.py::
_apply_substituted_builds`). Measured 2026-09-26 on MiniOneRec inf13: card-2 claimed "a single
ragged left-padded generate over the whole batch preserves recall@50 when position_ids and the
per-request decode K/V lengths are right" — the one lead that had measured 3.3x, broken only by an
indexing bug. Its build reported `different` ("instead of the risky single-pass architecture … I keep
the byte-exact grouped path") and ran none of it: zero MIXED_LENGTH markers, 2,023 lines of node 0's
cohort marker, no single-pass module in the tree. It scored 1.373x — node 0's idea again — and the
board read card-2 `supported` on it, i.e. told the Researcher the single pass was done and worth
1.37x when it had never run. The report was already written; nothing that decides a verdict read it.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Collection, Mapping, Optional

IDEA_REPORT_NAME = "looplab_idea_report.json"
IDEA_IMPLEMENTED = ("as_proposed", "partly", "different", "not_implemented")
# The two reports that say the node's result measures SOMETHING ELSE than its idea. `partly` is not
# one of them, deliberately: a partial build still exercised the claim, and a Developer answers
# `partly` for every build that did not realise each detail — returning the Card on each of those
# would pay a second Developer build for most ideas. The note below still says `partly` on every
# surface that shows the node, so a reader can discount it; the verdict does not guess how much.
NOT_A_TEST = ("different", "not_implemented")
_BUILT_INSTEAD_CHARS = 400


def idea_digest(idea) -> Optional[str]:
    """`idea:<sha256>` of the Idea a build was handed, or None when it has no canonical form."""
    from looplab.core.jsonutil import canonical_json_digest
    dump = getattr(idea, "model_dump", None)
    return canonical_json_digest(dump(mode="json"), prefix="idea:") if callable(dump) else None


def idea_report_text(args, *, idea: Optional[str] = None) -> Optional[str]:
    """The report a `done` declared, as file text, or None when it declared nothing usable.

    `idea` is `idea_digest` of the Idea the build was handed, written beside the answer so a report
    is about ONE idea: `different` with no `built_instead` is otherwise the same bytes for every
    build, and `inherited_report` would read an improve child's own honest answer as its parent's."""
    if not isinstance(args, dict) or args.get("idea_implemented") not in IDEA_IMPLEMENTED:
        return None
    built = " ".join(str(args.get("built_instead") or "").split())[:_BUILT_INSTEAD_CHARS]
    data = {"idea_implemented": args["idea_implemented"], "built_instead": built}
    if idea:
        data["idea"] = idea
    return json.dumps(data, indent=1)


# A report `idea_report_text` writes is ~480 characters at most. The file is Developer-writable, so the
# reader bounds it rather than trusting it: anything longer than this was not written by a `done` and
# reads as no report. The cap is on what gets PARSED, which the fold does for every evidence node.
_REPORT_MAX_CHARS = 4096


@lru_cache(maxsize=4096)
def _parse_report(text: str) -> tuple[Optional[str], str]:
    """The parse, memoised on the report's own text: the fold reads every card's evidence reports
    on every fold, and a report is a few hundred immutable bytes, so the key IS the value. Only
    text inside `_REPORT_MAX_CHARS` reaches it (`idea_report_of`), so no oversized key is kept.

    `RecursionError` is caught beside `ValueError`, and it is not defensive noise: 4 KiB of `[`
    nests past the interpreter's limit, `json.loads` raises it, and this runs inside `fold()` — an
    uncaught one is a Developer-writable file that stops the run from looping, resuming or
    replaying, on every fold, because a cache does not memoise an exception."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None, ""
    if not isinstance(data, dict) or data.get("idea_implemented") not in IDEA_IMPLEMENTED:
        return None, ""
    return data["idea_implemented"], " ".join(str(data.get("built_instead") or "").split())


def _report_text(node) -> Optional[str]:
    files = getattr(node, "files", None)
    text = files.get(IDEA_REPORT_NAME) if isinstance(files, Mapping) else None
    return text if isinstance(text, str) and text else None


def inherited_report(node, nodes: Optional[Mapping]) -> bool:
    """Is this node's report its PARENT's, byte for byte? The repo Developer pre-loads an improve /
    refine build with the parent's files, and until 2026-09-26 the report rode along with them
    (`adapters/repo_developer.py::_run` now drops it), so a child whose `done` answered nothing
    carried its parent's `different` — and would have retired its own card on the parent's word.
    Logs written before that keep the copy; this is how the readers tell it from a report of its own.
    Since reports carry the digest of their idea (`idea_report_text`), a byte match between a parent
    and a child with different ideas can only be the copy; one about the SAME idea loses its report
    here — the safe direction: it is counted as a test, which is all a node with no report ever was."""
    text = _report_text(node)
    if text is None or not nodes:
        return False
    return any(_report_text(nodes.get(pid)) == text
               for pid in (getattr(node, "parent_ids", None) or ()))


def idea_report_of(node, nodes: Optional[Mapping] = None) -> tuple[Optional[str], str]:
    """`(idea_implemented, built_instead)` off the node's report file; `(None, "")` when there is no
    report, it cannot be read, or — given the run's `nodes` — it is the parent's (`inherited_report`).
    The LATEST report wins: a repair's `node_repaired.files` replaces the build's, so a repair that
    did implement the idea after all says so and is counted again."""
    text = _report_text(node)
    if text is None or len(text) > _REPORT_MAX_CHARS or inherited_report(node, nodes):
        return None, ""
    return _parse_report(text)


def idea_not_tested(node, nodes: Optional[Mapping] = None) -> bool:
    """Did this node's Developer report that it did NOT build its idea (`NOT_A_TEST`)?"""
    return idea_report_of(node, nodes)[0] in NOT_A_TEST


def idea_report_note(node, nodes: Optional[Mapping] = None) -> str:
    """" [NOT A TEST OF card-N's IDEA — …]" for a node whose Developer said it built something else,
    " [idea partly built — …]" for a partial build, "" otherwise (as proposed, or no report). Pass
    the run's `nodes` wherever a caller has them, so an inherited report is not read as this node's."""
    value, built = idea_report_of(node, nodes)
    if value is None or value == "as_proposed":
        return ""
    instead = f" — built instead: {built[:160]}" if built else ""
    if value in NOT_A_TEST:
        card = getattr(getattr(node, "idea", None), "card_id", None)
        whose = f"{card}'s" if isinstance(card, str) and card else "its"
        return f" [NOT A TEST OF {whose} IDEA (idea {value}){instead}]"
    return f" [idea {value} built{instead}]"


def rebuild_note(idea, state) -> str:
    """What the Developer rebuilding a RETURNED card must know, or "" (every other build).

    The return (`events/card_ledger.py::_apply_card_returns`) re-elects the card's stored action, and
    nothing told the rebuild why it was back: the same Developer on the same prompt makes the same
    "safe" substitution, and the second build retires the card with its idea never tested. Said to
    the Developer only (`engine/node_build.py::_directed_idea` — `node_created` keeps the idea)."""
    card = (getattr(state, "cards", None) or {}).get(getattr(idea, "card_id", None) or "")
    ids = [nid for nid in (getattr(card, "substituted_nodes", None) or []) if isinstance(nid, int)]
    if not ids:
        return ""
    nodes = getattr(state, "nodes", None) or {}
    said = "; ".join(f"node {nid} built instead: {built[:160] or 'something else'}"
                     for nid in ids[:2] for built in [idea_report_of(nodes.get(nid), nodes)[1]])
    return (f"THIS IDEA HAS NOT BEEN TESTED YET — an earlier build of it ran something else ({said}). "
            "Build the idea as proposed. If it cannot be built here, do not substitute another "
            "approach: report idea_implemented: not_implemented and say why in built_instead.")


def surpassed_by(node_id: int, nodes: Mapping, *, direction: str,
                 excluded: Collection[int] = frozenset(),
                 aborted: Collection[int] = frozenset()) -> list[int]:
    """The later builds ON `node_id` that BEAT it — ascending node ids, possibly [].

    A node counts when it descends from `node_id` (on the `parent_ids` chain, any depth) and:
    (1) it is not an experiment of `node_id`'s own card — another card's (or a card-less node's; a
    node of the same card on it would be that card's own rebuild, which the return already covers);
    (2) it TESTED ITS OWN IDEA — its own report is not a substitution (`idea_not_tested`: no report,
    `as_proposed` and `partly` all count, the ledger's one reading of "tested"); (3) its number
    counts toward the run's best under the champion's own exclusions (evaluated, not tombstoned,
    `core/fitness.py::counts_toward_best` over `excluded` = the trust gate's set and `aborted` = the
    operator's); and (4) its `metric` BEAT `node_id`'s in the run's `direction` (`is_better`; the
    raw metric, the scalar the card ledger orders by). A node with no usable metric of its own is
    surpassed by nothing. Pure over `nodes`; O(nodes).

    WHAT IT ANSWERS: has the run moved PAST a substituted build, so that its card should wait for
    the Researcher instead of going back to the automatic Card lane (`events/card_ledger.py::
    _apply_card_returns`)? Measured 2026-09-27 on MiniOneRec inf13: card-2's node 2 (1.3726,
    reported `different`) was returned and selection-ready although node 5 — card-6, built on node 2
    `as_proposed` — had tested card-2's very claim and beaten it (4.1658, seq 8866); a rebuild would
    pay a Developer build for a solved idea and tell it "THIS IDEA HAS NOT BEEN TESTED YET"
    (`rebuild_note`). Nothing in the log says two cards make the SAME claim (different beliefs and
    directions, never merged), so the answer is a structural proxy, and "BEAT", not "built on", is
    deliberate (critic review 2026-09-26 of an earlier cut of this rule): under greedy selection the
    best node is built on at once, so "any evaluated descendant" blocks a best-scoring substitute at
    its first child whatever that child tested — inf13's node 3 (card-5, 1.2382, seq 1849) would
    have, before card-6 existed. In a Card-driven run every build is reserved under a native card
    (`engine/card_reservation.py::_reserve_node_build`), so a card-less descendant is a legacy or
    non-Card run's node. THE TRADE-OFF, both ways: a descendant that tested the same claim and LOST (a
    refuted claim, or one its build broke) does not block, so the card comes back and its one
    rebuild may re-test a claim the run already answered — bounded, because that rebuild spends the
    card's forgiveness; and an unrelated descendant that happens to win does block, leaving the idea
    to the Researcher, who reads the card in a board block of its own with these nodes named
    (`Card.withheld_by`, `card_substitution_brief`). NOR DOES THE BLOCK STICK: the answer is asked
    again on every fold, so a winner reset to pending (`node_reset`), aborted, tombstoned or
    trust-flagged under an enforcing gate stops counting and the card comes back — and a rebuild
    elected or claimed in that window is then kept even once the winner beats it again
    (`_apply_card_returns`, "THE WITHHOLD DOES NOT STICK")."""
    from looplab.core.fitness import counts_toward_best, is_better, is_usable_metric

    root = nodes.get(node_id)
    baseline = getattr(root, "metric", None)
    if root is None or not is_usable_metric(baseline):
        return []
    own_card = getattr(getattr(root, "idea", None), "card_id", None)
    children: dict[int, list[int]] = {}
    for nid, n in nodes.items():
        for pid in getattr(n, "parent_ids", None) or ():
            children.setdefault(pid, []).append(nid)
    found: list[int] = []
    seen = {node_id}
    stack = list(children.get(node_id, ()))
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(children.get(nid, ()))
        n = nodes.get(nid)
        status = getattr(getattr(n, "status", None), "value", getattr(n, "status", None))
        if (n is not None and status == "evaluated" and not getattr(n, "tombstoned", False)
                and (own_card is None
                     or getattr(getattr(n, "idea", None), "card_id", None) != own_card)
                and counts_toward_best(n, excluded, aborted)
                and is_usable_metric(n.metric) and is_better(direction, n.metric, baseline)
                and not idea_not_tested(n, nodes)):
            found.append(nid)
    return sorted(found)


def card_substitution_brief(card, nodes: Mapping) -> str:
    """One clause for a board row — "" for a Card no build ever substituted, else what the board must
    not launder: which nodes built something else, and what that means for the Card now.

    FACTS ONLY. A card the ledger WITHHELD names the later builds on its substitution that beat it,
    read off `Card.withheld_by` — the list `events/card_ledger.py::_apply_card_returns` decided
    with, never re-derived here, so a row cannot call a card "beaten" that the ledger kept off the
    board for another reason: a GATED single substitution says it is gated. What the Researcher may
    DO about a withheld card is said once, by the block the board renders it in
    (`agents/state_brief.py::board_prompt_lines`) — the first cut of this clause carried its own
    "propose it again only if…" and sat under a block that said "do NOT propose one of these again".

    Ends with a space when non-empty so a row can splice it in front of its next field unchanged."""
    ids = [nid for nid in (getattr(card, "substituted_nodes", None) or []) if isinstance(nid, int)]
    if not ids:
        return ""
    parts = []
    for nid in ids[:3]:
        _value, built = idea_report_of(nodes.get(nid), nodes)
        parts.append(f"node {nid} built " + (f"instead: {built[:120]}" if built else "something else"))
    evidence = list(getattr(card, "evidence", None) or [])
    counted = [nid for nid in evidence if nid not in ids]
    beaten = [nid for nid in (getattr(card, "withheld_by", None) or []) if isinstance(nid, int)]
    if counted:
        tail = "not counted in this card's verdict"
    elif not evidence:
        tail = "this card's idea is UNTESTED and back on the board"
    elif beaten:
        more = f" and {len(beaten) - 4} more" if len(beaten) > 4 else ""
        tail = (f"not returned: node(s) {beaten[:4]}{more} built on node {ids[0]} and beat it; its "
                "idea was never tested")
    elif len(ids) == 1:
        # The one other shape that leaves a SINGLE substitution as the card's whole evidence: it is
        # infeasible or trust-excluded, and a gated build is never returned.
        tail = ("not returned: that build is gated (infeasible or trust-excluded); its idea was "
                "never tested")
    else:
        tail = "built as something else twice, so retired — its idea was never tested"
    return f"NOT TESTED by {'; '.join(parts)} — {tail}. "
