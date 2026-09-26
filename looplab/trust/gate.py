"""Variance gate (I10 slice, ADR-15). The ">1 standard-error" acceptance rule:
a candidate only beats the incumbent if its improvement exceeds one SE of the
difference, i.e. we don't promote within-noise "improvements". This is the lever
that stops a greedy tree from chasing seed luck.

The full P1 trust layer (bootstrap BCa CIs, multi-seed top-k confirmation, leakage
detectors) extends this module; this is the minimal, unit-tested core.
"""
from __future__ import annotations

# The rule itself lives in `core/fitness.py` since 2026-09-26 (moved verbatim, doc 67 67.1): the
# fold-derived Card ledger holds a `supported` verdict to it, and `events/` may import only `core`.
# Re-exported here as the SAME object, so `trust.gate.one_se_better` — its readers' spelling — keeps
# resolving (and `standard_error_difference`, which this module has always carried).
from looplab.core.fitness import one_se_better, standard_error_difference  # noqa: F401 — re-exported

__all__ = ["one_se_better", "standard_error_difference"]
