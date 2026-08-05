"""The two setup identities the run-start writer stamps into its own event log (doc 25 SE-01).

`Engine._setup_phase` publishes `run_started.config_hash` and `setup_finished.manifest`;
`search/speculation_quality.py::_validate_calibration_setup` re-derives both to prove that
calibration evidence was written by the shipped Toy setup writer and not hand-authored. Those were
two hand-copied derivations of the same bytes, and each has a detail that does not survive being
copied from memory:

* `config_hash` dumps the task payload WITHOUT `OPT_SORT_KEYS`, so it is the model's own field
  order; `setup_manifest_digest`'s inner config hash dumps the SAME payload WITH sort keys. The two
  are different 12-hex digests of the same object, on purpose, and swapping them silently produces a
  self-consistent run that no validator accepts.
* both truncate — 12 hex for the config hash, 16 for the manifest — and the validator's
  `_CONFIG_HASH_RE` / `_SETUP_MANIFEST_RE` pin those widths.

`core` because these are pure content hashes over already-JSON material, with no event, engine or
task-adapter knowledge; both the engine (above) and `search` (which may not import the engine) reach
them downward from here.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import orjson


def setup_config_hash(task_payload: Mapping[str, Any]) -> str:
    """`run_started.config_hash`: the task identity a resume compares its own config against."""
    return hashlib.sha256(orjson.dumps(task_payload)).hexdigest()[:12]


def setup_manifest_digest(
    task_payload: Mapping[str, Any],
    workspace: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> str:
    """P0-3 content-addressed setup: a stable digest of the MATERIAL the task+data preflight verified.

    The config hash, the workspace fingerprint, and the data-asset provenance. Binds `setup_done` to
    the exact inputs so a pre-node resume re-runs preflight (leakage!) when they changed rather than
    trusting a stale boolean. Deterministic (pure content hashes), so an unchanged workspace yields
    the recorded digest and never loops.
    """
    cfg = hashlib.sha256(
        orjson.dumps(task_payload, option=orjson.OPT_SORT_KEYS)).hexdigest()[:12]
    return hashlib.sha256(orjson.dumps(
        {"config": cfg, "workspace": workspace, "provenance": provenance},
        option=orjson.OPT_SORT_KEYS)).hexdigest()[:16]


__all__ = ["setup_config_hash", "setup_manifest_digest"]
