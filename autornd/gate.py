"""Variance gate (I10 slice, ADR-15). The ">1 standard-error" acceptance rule:
a candidate only beats the incumbent if its improvement exceeds one SE of the
difference, i.e. we don't promote within-noise "improvements". This is the lever
that stops a greedy tree from chasing seed luck.

The full P1 trust layer (bootstrap BCa CIs, multi-seed top-k confirmation, leakage
detectors) extends this module; this is the minimal, unit-tested core.
"""
from __future__ import annotations

import math


def one_se_better(
    candidate: float,
    incumbent: float,
    std: float,
    n: int,
    direction: str = "min",
    incumbent_std: float = 0.0,
    incumbent_n: int = 0,
) -> bool:
    """True if `candidate` is better than `incumbent` by more than 1 SE of the
    *difference* of the two estimates.

    `std`/`n` describe the candidate's spread; `incumbent_std`/`incumbent_n` (optional)
    the incumbent's. SE_diff = sqrt(SE_cand^2 + SE_inc^2). With no usable variance on
    either side it falls back to a strict comparison.
    """
    strict = candidate < incumbent if direction == "min" else candidate > incumbent
    se_cand = std / math.sqrt(n) if (n > 1 and std > 0.0) else 0.0
    se_inc = (incumbent_std / math.sqrt(incumbent_n)
              if (incumbent_n > 1 and incumbent_std > 0.0) else 0.0)
    se = math.sqrt(se_cand ** 2 + se_inc ** 2)
    if se <= 0.0:
        return strict
    if direction == "min":
        return candidate < incumbent - se
    return candidate > incumbent + se
