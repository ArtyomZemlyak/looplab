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
from typing import Mapping, Optional

IDEA_REPORT_NAME = "looplab_idea_report.json"
IDEA_IMPLEMENTED = ("as_proposed", "partly", "different", "not_implemented")
# The two reports that say the node's result measures SOMETHING ELSE than its idea. `partly` is not
# one of them, deliberately: a partial build still exercised the claim, and a Developer answers
# `partly` for every build that did not realise each detail — returning the Card on each of those
# would pay a second Developer build for most ideas. The note below still says `partly` on every
# surface that shows the node, so a reader can discount it; the verdict does not guess how much.
NOT_A_TEST = ("different", "not_implemented")
_BUILT_INSTEAD_CHARS = 400


def idea_report_text(args) -> Optional[str]:
    """The report a `done` declared, as file text, or None when it declared nothing usable."""
    if not isinstance(args, dict) or args.get("idea_implemented") not in IDEA_IMPLEMENTED:
        return None
    built = " ".join(str(args.get("built_instead") or "").split())[:_BUILT_INSTEAD_CHARS]
    return json.dumps({"idea_implemented": args["idea_implemented"], "built_instead": built},
                      indent=1)


def idea_report_of(node) -> tuple[Optional[str], str]:
    """`(idea_implemented, built_instead)` off the node's report file; `(None, "")` when there is no
    report or it cannot be read. The LATEST report wins: a repair's `node_repaired.files` replaces the
    build's, so a repair that did implement the idea after all says so and is counted again."""
    try:
        data = json.loads((getattr(node, "files", None) or {}).get(IDEA_REPORT_NAME) or "{}")
    except (ValueError, TypeError, AttributeError):
        return None, ""
    if not isinstance(data, dict) or data.get("idea_implemented") not in IDEA_IMPLEMENTED:
        return None, ""
    return data["idea_implemented"], " ".join(str(data.get("built_instead") or "").split())


def idea_not_tested(node) -> bool:
    """Did this node's Developer report that it did NOT build its idea (`NOT_A_TEST`)?"""
    return idea_report_of(node)[0] in NOT_A_TEST


def idea_report_note(node) -> str:
    """" [NOT A TEST OF card-N's IDEA — …]" for a node whose Developer said it built something else,
    " [idea partly built — …]" for a partial build, "" otherwise (as proposed, or no report)."""
    value, built = idea_report_of(node)
    if value is None or value == "as_proposed":
        return ""
    instead = f" — built instead: {built[:160]}" if built else ""
    if value in NOT_A_TEST:
        card = getattr(getattr(node, "idea", None), "card_id", None)
        whose = f"{card}'s" if isinstance(card, str) and card else "its"
        return f" [NOT A TEST OF {whose} IDEA (idea {value}){instead}]"
    return f" [idea {value} built{instead}]"


def card_substitution_brief(card, nodes: Mapping) -> str:
    """One clause for a board row — "" for a Card no build ever substituted, else what the board must
    not launder: which nodes built something else, and what that means for the Card now.

    Ends with a space when non-empty so a row can splice it in front of its next field unchanged."""
    ids = [nid for nid in (getattr(card, "substituted_nodes", None) or []) if isinstance(nid, int)]
    if not ids:
        return ""
    parts = []
    for nid in ids[:3]:
        _value, built = idea_report_of(nodes.get(nid))
        parts.append(f"node {nid} built " + (f"instead: {built[:120]}" if built else "something else"))
    counted = [nid for nid in (getattr(card, "evidence", None) or []) if nid not in ids]
    if counted:
        tail = "not counted in this card's verdict"
    elif not getattr(card, "evidence", None):
        tail = "this card's idea is UNTESTED and back on the board"
    else:
        tail = "built as something else twice, so retired — its idea was never tested"
    return f"NOT TESTED by {'; '.join(parts)} — {tail}. "
