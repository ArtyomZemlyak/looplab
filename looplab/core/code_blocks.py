"""The unit of code-block ablation and the cut it measures — pure, so the fold can check a receipt.

`code_blocks` splits a solution into blank-line-separated paragraphs (an ML-pipeline component —
data prep, feature engineering, model, loss, ensembling — tends to be one paragraph), and
`comment_block` neutralizes one of them by commenting its lines out. The code-block ablation
(`engine/ablation.py::AblationMixin._ablate_code`) probes each cut; a `simplify` node (doc 67 67.5)
IS one of those cuts; and the fold (`events/replay.py::_simplification_receipt`) holds a
`simplified` receipt to the code the receipt says it is — which it can only do from `core`, the one
package `events` may import. Moved here from `search/policy.py` (and `comment_block` from the
ablation mixin) on 2026-09-27, verbatim; both old spellings delegate, so there is ONE of each.
"""
from __future__ import annotations

import functools
from typing import Optional


def code_blocks(code: str) -> list[tuple[int, int]]:
    """A0a: split solution code into blank-line-separated paragraph blocks -> (start,end) line
    ranges (end exclusive). Deterministic; the unit of code-block ablation (an ML-pipeline
    component: data prep / feature-eng / model / loss / ensembling tends to be one paragraph)."""
    lines = code.splitlines()
    blocks: list[tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].strip() == "":
            i += 1
            continue
        j = i
        while j < n and lines[j].strip() != "":
            j += 1
        blocks.append((i, j))
        i = j
    return blocks


def comment_block(code: str, block: tuple[int, int]) -> str:
    """Neutralize one block by commenting its lines out (the ablation), keeping the rest intact."""
    s, e = block
    lines = code.splitlines()
    for k in range(s, e):
        lines[k] = "# [ablated] " + lines[k]
    return "\n".join(lines) + "\n"


@functools.lru_cache(maxsize=512)
def cut_of(parent_code: str, block: int) -> Optional[str]:
    """`parent_code` with pipeline block #`block` commented out — the probe's own pair — or None
    when that code has no such block. Memoized: the selector asks it on every fold."""
    spans = code_blocks(parent_code)
    return comment_block(parent_code, spans[block]) if 0 <= block < len(spans) else None


def still_cut_of(parent, child) -> bool:
    """Whether `child` IS STILL `parent`'s program with its receipt's block commented out, over the
    parent's own files — what a `simplified` receipt certified when it was folded
    (`events/replay.py::_simplification_receipt`). True across a re-measurement of the same program
    (an epoch re-queue, an eval-type reset: the parent's attempt moves, its code does not), false
    once the parent's code or files changed: then the block index names another paragraph. The
    receipt's `generation` was this test until the critic's pass of 2026-09-27 (driven: a re-queue
    re-measured both programs and the tie went back to the parent the cut had tied)."""
    receipt = getattr(child, "simplified", None)
    if (not isinstance(receipt, dict) or parent is None
            or receipt.get("parent_id") != getattr(parent, "id", None)
            or not getattr(parent, "code", "")):
        return False
    block = receipt.get("block")
    if not isinstance(block, int) or isinstance(block, bool):
        return False
    return (getattr(child, "code", None) == cut_of(parent.code, block)
            and (getattr(child, "files", None) or {}) == (getattr(parent, "files", None) or {}))
