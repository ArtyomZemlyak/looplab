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
