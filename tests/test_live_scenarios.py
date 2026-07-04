"""The live scenario collection, run under pytest.

Each scenario manufactures a controlled SITUATION and drives the REAL agent (LLM Researcher +
Developer) end-to-end over the `looplab` CLI, asserting the expected behaviour (plateau broken,
cheater excluded, node repaired, …). See `tests/live/scenarios.py` for the harness/registry and
`docs/guide/live-scenarios.md` for the writeup.

These are EXPENSIVE (each is a real multi-node run, minutes each), so they auto-skip unless BOTH:
  • a live LLM is reachable (Settings.llm_base_url answers), and
  • LOOPLAB_LIVE_SCENARIOS=1 is set (explicit opt-in).

The primary interface is the standalone runner — `python -m tests.live.scenarios [name ...]` — which
prints a PASS/FAIL summary; this wrapper just lets CI/opt-in runs assert them too.
"""
from __future__ import annotations

import os

import pytest

from tests.live import scenarios as S

_GATE = bool(os.environ.get("LOOPLAB_LIVE_SCENARIOS")) and S.live_llm_reachable()
# `live` marker: selection only (`-m "not live"`); the skipif stays the enforcement gate.
pytestmark = [pytest.mark.live, pytest.mark.skipif(
    not _GATE, reason="set LOOPLAB_LIVE_SCENARIOS=1 with a reachable LLM to run the live scenarios")]


@pytest.mark.parametrize("sc", S.REGISTRY, ids=lambda s: s.name)
def test_live_scenario(sc):
    S.build(sc)
    S._POST_BUILD.get(sc.name, lambda _s: None)(sc)
    S.run(sc)
    ok, detail = S.verify(sc)
    assert ok, f"[{sc.name}] {sc.feature}: {detail}"
