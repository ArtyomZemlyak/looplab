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
import hashlib
import json
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


def cut_identity(code: str, files) -> str:
    """The identity of a cut AS BUILT — its program and its files, hashed — which the fold records
    beside a receipt it certified (`core/models.py::Node.simplified_cut`). ASCII-escaped JSON, so a
    lone surrogate in a hand-edited row hashes instead of raising out of the fold."""
    preimage = json.dumps([code, sorted((files or {}).items())], ensure_ascii=True,
                          separators=(",", ":"))
    return hashlib.sha256(preimage.encode("ascii")).hexdigest()


def built_as_cut_of(parent, child) -> bool:
    """Whether `child` was BUILT as `parent`'s CURRENT program minus its receipt's block: the cut the
    fold certified at the child's `node_created` (`Node.simplified_cut`) is the cut that program
    gives now. `still_cut_of` asks what the child IS, which selection needs; this asks what it WAS
    built as, which "that block was already cut" needs — an inline repair (`node_repaired`) rewrites
    a pending cut in place, and the block it was cut from stayed spent under the receipt-generation
    rule but was freed under `still_cut_of` alone: the policy nominated it again and the engine
    built the program that had just crashed (critic 2026-09-27, driven)."""
    receipt = getattr(child, "simplified", None)
    built = getattr(child, "simplified_cut", None)
    if (not isinstance(receipt, dict) or not isinstance(built, str) or parent is None
            or receipt.get("parent_id") != getattr(parent, "id", None)
            or not getattr(parent, "code", "")):
        return False
    block = receipt.get("block")
    if not isinstance(block, int) or isinstance(block, bool):
        return False
    cut = cut_of(parent.code, block)
    return cut is not None and built == cut_identity(cut, getattr(parent, "files", None))


def cut_spent(parent, child) -> bool:
    """Whether `child` spends its receipt's block of `parent`'s current program — the policy's
    `taken` and the engine's `simplify_spent` (doc 67 67.5): it IS that cut
    (`still_cut_of`), or it was built as that cut and a repair rewrote it since
    (`built_as_cut_of`). Either way the nomination would build a program the tree already has."""
    return still_cut_of(parent, child) or built_as_cut_of(parent, child)
