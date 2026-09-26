"""A build running with nothing else to do WAITS; it does not spin the outer loop.

Measured 2026-09-26 on MiniOneRec inf13 (build width 2, one GPU): for the 8 minutes the run's FIRST
Card built, with no evaluation running and no pending node, the outer loop turned about five times a
second -- 2,534 cadence passes (`lessons_distill`, `lessons_refresh`, `skills_promote`,
`lessons_reconcile` spans), each one a full read and fold of the log. Every session found no raw
action to propose and handed back to the outer loop; the outer loop had nothing to do either, paid
its cadence pass and re-entered. inf12, resumed with evaluations always running, saw at most 23 an
hour: `_card_phase_decide_exit` already rate-limits that hand-back when an evaluation holds the
session, and nothing did when an adopted BUILD was all that was running.
"""
from __future__ import annotations

import threading
import time

import anyio

from looplab.events.replay import fold
from looplab.events.types import EV_CARD_BUILD_DONE
from tests.test_card_speculation_engine import (  # noqa: F401  (autouse receipt fixture)
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _engine,
    _Researcher,
    _start,
    _without_research,
)
from tests.test_several_card_producers_build_at_once import _GatedDeveloper, _live


def _lone_build_engine(tmp_path, monkeypatch):
    engine, _unused = _engine(tmp_path / "lone", depth=1)
    log: list = []
    engine.role_factory = lambda: (_Researcher(), _GatedDeveloper(log))
    engine._llm_parallel = 2
    engine._llm_parallel_launched = 2
    engine._llm_parallel_startup_auto = False
    _without_research(monkeypatch, engine)
    _start(engine)
    _add_ready_draft(engine, "card-0", x=0.1)
    assert engine._request_card_build() is True        # the run's first Card, nothing else
    # …and no raw proposal to make beside it, as in inf13's window: no `phase_progress` for a
    # speculative proposal appears between card-0's request and its node.
    import looplab.engine.speculation as speculation
    monkeypatch.setattr(speculation, "speculative_raw_actions", lambda *_a, **_k: [])
    return engine, log


def test_a_lone_adopted_build_holds_the_session_instead_of_handing_back(tmp_path, monkeypatch):
    engine, log = _lone_build_engine(tmp_path, monkeypatch)
    _GatedDeveloper.gate = True
    hand_backs = []

    async def scenario():
        async with anyio.create_task_group() as eval_tg:
            engine._eval_task_group = eval_tg
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                with anyio.move_on_after(max(0.0, deadline - time.monotonic())):
                    await engine._run_card_session([], fold(engine.store.read_all()), None)
                    hand_backs.append(time.monotonic())
                engine._card_boundary_debt = False          # the cadence pass pays the debt
            assert _live(log) == 1, "the build is running the whole time"
            _GatedDeveloper.gate = False
            for _what, _card, developer in list(log):
                developer.release.set()
            with anyio.fail_after(20):
                while fold(engine.store.read_all()).card_builds_done < 1:
                    await engine._run_card_session([], fold(engine.store.read_all()), None)
                    engine._card_boundary_debt = False
                    await anyio.sleep(0.02)

    anyio.run(scenario)
    # One hand-back is the boundary the first "nothing to propose" is owed; the fix is that the SAME
    # unchanged answer is not owed again.
    assert len(hand_backs) <= 2, f"{len(hand_backs)} hand-backs in 1.5 s with nothing changing"
    closes = [e.data for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]
    assert closes and closes[0].get("node_id") is not None, closes
