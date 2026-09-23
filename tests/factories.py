"""One canonical `Engine` construction for the test suite (doc 25 XP-11).

29 test files each defined a private `_engine(...)` around `Engine(...)`, and there are 150+ direct
`Engine(` constructions across the suite. Every one of them is a call site of the engine's keyword
API, which CLAUDE.md calls out as needing to stay stable precisely BECAUSE so many tests construct it
directly — so the cost of ever evolving that API is paid once per file rather than once.

`make_engine` is the shared shape those factories all had: load the toy task, build its roles, hand
the engine a subprocess sandbox and a `GreedyTree`. Everything else passes through as keyword
overrides, so a test that needs a different policy, a scripted role or an extra knob keeps saying so
locally instead of inheriting a default it did not choose.

Deliberately NOT a fixture: these constructions happen inside helper functions and parametrized
bodies as often as at test top level, and a fixture would force the call sites that need two engines
(resume/crash-recovery tests build a second one over the same run dir) to work around it.

Migration is opportunistic, as the finding says — this exists so new tests have one obvious way, and
so the next engine-API change has one place to start. It does not require a suite-wide rewrite.

The second half is the same idea for the server's DURABLE COMMAND protocol: `post_command` /
`command_terminal` / `http_run_generation` / `log_run_generation`, the one way a test submits to
`POST /api/runs/{run}/commands` and waits for its record to settle (see the section comment below).
"""
from __future__ import annotations

import time
from pathlib import Path

from looplab.adapters.toytask import ToyTask
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TOY_TASK = ROOT / "examples" / "toy_task.json"


def make_engine(run_dir, *, task_file=None, task=None, researcher=None, developer=None,
                sandbox=None, policy=None, n_seeds: int = 3, max_nodes: int = 8,
                **overrides) -> Engine:
    """Build an `Engine` over the toy task, overriding only what a test actually cares about.

    `n_seeds`/`max_nodes` shape the DEFAULT `GreedyTree`; pass `policy=` to replace it wholesale
    (the ablation/merge tests do). `task`/`researcher`/`developer`/`sandbox` accept scripted stubs.
    Anything else in `**overrides` goes straight to `Engine`, so this never becomes a second, lagging
    spelling of its keyword API.
    """
    task = task if task is not None else ToyTask.load(task_file or TOY_TASK)
    if researcher is None or developer is None:
        built_researcher, built_developer = task.build_roles()
        researcher = researcher if researcher is not None else built_researcher
        developer = developer if developer is not None else built_developer
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=sandbox if sandbox is not None else SubprocessSandbox(),
        policy=policy if policy is not None else GreedyTree(n_seeds=n_seeds, max_nodes=max_nodes),
        **overrides,
    )


# ------------------------------------------------------------------ the durable command protocol
#
# `POST /api/runs/{run}/commands` is the fenced successor of the legacy `POST .../control` route
# (retired 2026-09-23), and
# a test that drives it makes the same three moves every time: name the run GENERATION the command
# was formed against (mandatory, 64 hex), submit with an `Idempotency-Key` (mandatory), and —
# because the service applies `pause` and every intent whose engine policy is not `NO_SPAWN` on a
# worker thread — poll the durable record to a terminal status before asserting on the event log.
# Those moves lived module-private in `tests/test_run_command_service.py` (`_generation` / `_post` /
# `_terminal`), which is how the `legacy-control-route-is-not-retired` marker came to list "a suite
# helper for this" as an unmet precondition of porting a `/control` site while the helper already
# existed one module away. Hoisted here (review 2026-09-22, SRV1-07) so a ported site reads no worse
# than the one it replaces; that module imports them back under their old names.
#
# Every `looplab.serve` import below is FUNCTION-LOCAL on purpose: `make_engine`'s callers are engine
# tests, and `looplab.serve.run_commands` imports fastapi (the [ui] extra) at module level.

# Moved verbatim from `tests/test_run_command_service.py`, whose `_client` and staged-finish tests
# the paragraph below names.
#
# Wall-clock ceiling for "an accepted command reached a terminal status". Like the constants below
# this bounds only the FAILURE case: the loop polls every 10ms and returns the instant a terminal
# appears, so a passing test never waits. 1.0s was marginal on a loaded full-suite host — the worker
# had observed neither the `run_finished` append nor the dead process yet, so this asserted on a
# still-'executing' record while passing in isolation. 15s then flaked the same way twice more.
# Size it against the thing being waited ON rather than by guesswork: the worker's own
# `max_observation_timeout` is `max(0.30, command_timeout * 4)`, i.e. 120s for the staged-finish
# tests, so any ceiling below that can expire while the command is still legitimately 'executing'
# and reports a hang the system does not consider one. 60s keeps a real hang bounded well inside
# that window while leaving 4x headroom over the 15s that kept failing.
#
# IF IT FLAKES AGAIN, THE ANSWER IS NOT A BIGGER NUMBER — that reflex is what took this constant
# 1.0 -> 15 -> 60 already. Measured 2026-08-14 on the 60s ceiling:
# `test_reload_finalize_reattaches_existing_record_without_event_or_spawn_duplication` failed a full
# frozen-tree run at 'executing', and passed the SAME tree, same command, when nothing else was
# running — the contended run had three mutation-tree pytest sessions, a `node --test` suite and a
# vite build alongside it. That is the whole mechanism: this loop polls from the MAIN thread for a
# status a background worker THREAD sets, and `time.time()` keeps counting while that thread is
# descheduled, so the ceiling measures wall clock and the work it is waiting on measures CPU it did
# not get. A larger ceiling buys proportionally more starvation, which is why each bump has bought
# roughly one more incident. What would actually close it is waiting on the worker's own progress
# rather than the clock; nobody has built that, and a 60s hang is bounded well enough that the
# guesswork is not worth spending until this fails on an IDLE host. Rule of thumb: reproduce it with
# the box quiet before treating it as a defect.
TERMINAL_SETTLE_TIMEOUT_S = 60.0


def http_run_generation(client, run_id: str = "demo", *, headers=None) -> str:
    """The generation token `GET /state` shows for the run — what a browser forms a command on."""
    generation = client.get(f"/api/runs/{run_id}/state", headers=headers or {}).json()["generation"]
    assert isinstance(generation, str) and len(generation) == 64
    return generation


def log_run_generation(run_dir) -> str:
    """The same token read off the event log itself, not over HTTP.

    For a test whose point is that the POST is REFUSED by a guard in front of every route — the Host,
    Origin, owner-token and review middlewares answer `GET /state` exactly as they answer the POST,
    so the usual read would be refused before the request under test is even formed."""
    from looplab.events.eventstore import EventStore
    from looplab.serve.run_commands import run_generation_token

    return run_generation_token(EventStore(Path(run_dir) / "events.jsonl").read_all())


def post_command(client, event_type, data=None, key="key-1", *, run_id: str = "demo",
                 generation=None, headers=None):
    """Submit one durable command; `generation=None` reads it through `GET /state` first.

    `headers` ride on BOTH requests (the generation read and the POST), so a test that must present
    an owner token or an Origin presents it once; `Idempotency-Key` is always this call's `key`."""
    expected_generation = (generation if generation is not None
                           else http_run_generation(client, run_id, headers=headers))
    return client.post(f"/api/runs/{run_id}/commands",
                       headers={**(headers or {}), "Idempotency-Key": key},
                       json={"type": event_type, "data": data or {},
                             "expected_generation": expected_generation})


def command_terminal(client, record, timeout=TERMINAL_SETTLE_TIMEOUT_S, run_id: str = "demo", *,
                     headers=None):
    """Poll a command record to a TERMINAL status (see `TERMINAL_SETTLE_TIMEOUT_S`) and return it."""
    from looplab.serve.protocol import COMMAND_TERMINAL_STATUSES

    current = record
    deadline = time.time() + timeout
    while current.get("status") not in COMMAND_TERMINAL_STATUSES and time.time() < deadline:
        time.sleep(0.01)
        current = client.get(f"/api/runs/{run_id}/commands/{record['id']}",
                             headers=headers or {}).json()
    assert current.get("status") in COMMAND_TERMINAL_STATUSES, current
    return current
