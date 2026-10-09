"""The upstream automation's KILL SWITCH and its operator view OUTSIDE `/commands` (doc 73 §4.3).

WHY (2026-10-08). The switch (`upstream_auto_set {enabled, reason}`, doc 73 §4.2 G2) could only be
pulled through `/commands` — the UI, the API, MCP — and `looplab inspect` printed neither the switch
nor the steps a cap held back. An operator at a terminal, with no server running, had no hand on the
automation at all.

ONE RULE, TWO DOORS. `normalize_upstream_auto_set` is the switch's whole payload rule: the server's
control normalizer (`serve/control_validation.py::_normalize_upstream_auto_set`) calls it for
`/commands`, and `looplab upstream-auto` (`set_upstream_auto`) calls it before its append — the same
control event with the same payload, never a second spelling of what a valid switch is. The CLI
appends the intent itself, as `looplab stop` appends its pause: a control intent is the one thing a
process other than the engine may append beside a running engine (invariant #1), and the switch's
command policy is `NO_SPAWN` / `folded_intent` — the command service starts nothing for it either, it
only records the command around the same append.

THE VIEW. `upstream_operator_lines` is what `looplab inspect` prints from `upstream_live_view` — the
projection the UI panel reads: the switch, the operator queue (how many still wait), the steps held
back at a cap (and whether each still waits), the author's spend against its cap and the outcomes it
paid for, and the automatic advances in the last hour against theirs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from looplab.core.errors import ConfigRefusal

REASON_LIMIT = 300


class UpstreamSwitchRefusal(ConfigRefusal):
    """The switch's payload is not one the rule admits. A `ConfigRefusal` — so an `OperatorRefusal`
    the CLI prints as one line at exit 2 wherever it escapes, and still a `ValueError` the server
    answers 400 (`serve/control_validation.py::_normalize_upstream_auto_set`). It was a bare
    `ValueError` (review 2026-10-09): a caller that forgot the CLI's wrap got the 42-frame traceback."""


def normalize_upstream_auto_set(data: dict) -> dict:
    """THE ONE RULE for an `upstream_auto_set` payload: `enabled` a real bool, `reason` optional text
    (stripped, at most `REASON_LIMIT` characters, dropped when blank)."""
    enabled = data.get("enabled")
    if type(enabled) is not bool:
        raise UpstreamSwitchRefusal("enabled must be true or false")
    reason = data.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            raise UpstreamSwitchRefusal("reason must be a string")
        reason = reason.strip()
        if len(reason) > REASON_LIMIT:
            raise UpstreamSwitchRefusal(f"reason must be at most {REASON_LIMIT} characters")
    return {"enabled": enabled, **({"reason": reason} if reason else {})}


def set_upstream_auto(run_dir, enabled: bool, reason: Optional[str] = None, *, store) -> str:
    """`looplab upstream-auto`: append the switch for `run_dir` and say what it does now.

    `store` is the run's `EventStore` as the CALLER's fence check opened it (the CLI's
    `_require_run_dir(..., healthy=True)`, which refuses a mid-file corrupt log) — never a second,
    unchecked store opened here on the same path.

    COMPARE-AND-SWAP, and a NO-OP when nothing would change (review 2026-10-09: both were on this
    branch's first version of the command and the merge with master dropped them). The append lands
    only on the tail this function READ (`expected_last_seq`): the state it says "was" is the state the
    row was written against, and a write that slipped in between — the UI's switch, an engine's
    advance — refuses this one rather than being silently overwritten by an intent raised on a view
    that no longer holds. A switch already in the requested position appends nothing and says so:
    a second identical row is noise in the upstream history every reader pages through."""
    from looplab.events.eventstore import EventStoreConcurrencyError
    from looplab.engine.shared import engine_fold as fold
    from looplab.events.run_generation import run_generation_token
    from looplab.events.types import EV_UPSTREAM_AUTO_SET
    run_dir = Path(run_dir)
    if not (run_dir / "events.jsonl").is_file():
        raise ConfigRefusal(f"upstream-auto: no run at {run_dir} (no events.jsonl)")
    events = store.read_all()
    if not run_generation_token(events):
        raise ConfigRefusal(f"upstream-auto: {run_dir} has no durable run identity yet (no run_started)")
    raw = {"enabled": enabled, **({"reason": reason} if reason is not None else {})}
    try:
        data = normalize_upstream_auto_set(raw)
    except UpstreamSwitchRefusal as exc:
        raise ConfigRefusal(f"upstream-auto: {exc}") from None
    was_paused = bool(getattr(fold(events), "upstream_auto_paused", False))
    if was_paused == (not data["enabled"]):
        last = next((e for e in reversed(events) if e.type == EV_UPSTREAM_AUTO_SET
                     and type(e.data.get("enabled")) is bool), None)
        return (f"{run_dir}: upstream automation already {'ON' if data['enabled'] else 'OFF'}"
                + (f" (seq {last.seq})" if last is not None else " (the switch was never set)")
                + " — nothing appended" + ("; the reason was not recorded" if "reason" in data else "")
                + ".")
    from looplab.core.run_deletion import RunDeletionFenceError, RunDeletionStorageError
    from looplab.core.run_reset import RunResetFenceError, RunResetStorageError
    try:
        row = store.append(EV_UPSTREAM_AUTO_SET, {
            "enabled": data["enabled"], **({"reason": data["reason"]} if "reason" in data else {})},
            expected_last_seq=events[-1].seq if events else -1)
    except EventStoreConcurrencyError:
        raise ConfigRefusal(f"upstream-auto: {run_dir.name} changed while the switch was being set; "
                            "nothing was appended — re-run the command against the new state") from None
    except (RunResetFenceError, RunResetStorageError, RunDeletionFenceError,
            RunDeletionStorageError) as exc:
        # A Replay or a deletion fences the log (and a fence that cannot be read is not an absent
        # one): the operator's one-line refusal, never a traceback (critic 2026-10-08).
        raise ConfigRefusal(f"upstream-auto: refusing to switch {run_dir.name}: {exc}") from None
    if data["enabled"]:
        said = "ON — the live engine authors, checks and advances on its own again"
    else:
        said = ("OFF — the live engine drafts, checks and advances nothing on its own; operations an "
                "operator queues are still served")
    return f"{run_dir}: upstream automation {said} (seq {row.seq})."


def upstream_operator_lines(run_dir, events, *, now: Optional[float] = None,
                            cursor: Optional[int] = None) -> list[str]:
    """The operator's lines for `looplab inspect`; [] for a run with no upstream lane. `cursor` is the
    caller's folded `RunState.lane_ops_done` when it holds one (the queue's WAITING count is that
    fold's answer, `upstream_serve.py::live_queue`); derived from the log otherwise."""
    from looplab.engine.upstream_serve import REFUSED_PREFIX, upstream_live_view
    view = upstream_live_view(run_dir, events, now=now, cursor=cursor)
    if view is None:
        return []
    # The view is a fact of the LOG (the mode the last engine armed with); whether one serves the run
    # NOW is the lock's — said only when the probe is definite (critic 2026-10-08).
    from looplab.engine.run_lifecycle import engine_liveness
    gone = not view["configured"] and engine_liveness(Path(run_dir)) is False
    out = [f"upstream automation: mode {view['mode']}"
           + (f" ({view['reason']})" if view["reason"] else "")
           + (" [as configured; no engine has armed it yet]" if view["configured"] else "")
           + (" [no engine serving it now: the mode it last served]" if gone else "")]
    switch = view.get("switch")
    if view["auto_paused"]:
        out.append("  switch: OFF — no automatic author, check or advance"
                   + (f" (seq {switch['seq']}: {switch['reason']})" if switch and switch.get("reason")
                      else f" (seq {switch['seq']})" if switch else ""))
    else:
        out.append("  switch: on" + (f" (seq {switch['seq']})" if switch else " (never set)"))
    # The queue and the author's outcomes were on this branch's first `inspect` lines and the merge
    # with master dropped them (review 2026-10-09): an operator could see the switch was ON and not
    # that four operations still waited, or that every draft the author paid for was declined.
    queue = view.get("queue") or {}
    if queue.get("total"):
        out.append(f"  queue: {queue['pending']} waiting of {queue['total']}")
    # A cap is what the engine ARMED with (its `lane_armed` row): None on a row written before the
    # caps were recorded is "unknown", never "no cap" — 0 is the operator's own "off".
    cap = view.get("author_usd_cap")
    out.append(f"  author spend: ${view['author_spent_usd']:.4f}"
               + (" (cap unknown: armed before caps were recorded)" if cap is None
                  else f" of ${cap:g} cap" if cap else " (no money cap)")
               + f"; drafts recorded: {view['authored_total']}")
    # Counted over the WHOLE log, not the view's last `LIVE_AUTHORED_ROWS` rows: it sits beside the
    # all-time total on the line above, and the two must add up.
    outcomes: dict = {}
    for e in events:
        if e.type == "lane_authored":
            key = str(e.data.get("outcome") or "unknown")
            outcomes[key] = outcomes.get(key, 0) + 1
    if outcomes:
        out.append("  author outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())))
    per_hour = view.get("advances_per_hour")
    out.append(f"  automatic advances in the last hour: {view.get('advances_last_hour') or 0}"
               + (" (hourly cap unknown)" if per_hour is None
                  else f" of {per_hour}/h cap" if per_hour else " (no hourly cap)"))
    held = view["held"]
    if not held:
        out.append("  held steps: none")
    for row in held:
        # A step the lane refused for good is neither waiting nor released: "released" beside a
        # `refused:` reason was the contradiction the panel had too (review 2026-10-09).
        state = ("refused" if str(row.get("reason") or "").startswith(REFUSED_PREFIX)
                 else "waiting" if row.get("waiting") else "released")
        out.append(f"  held: {row['op']} {row.get('proposal_id') or ''} {row['reason']} "
                   f"(seq {row['seq']}, {state})".replace("  (", " ("))
    return out


def upstream_inspect_lines(run_dir, state, events) -> list[str]:
    """`looplab inspect`'s upstream block: the board (`core/upstream_board.py::board_lines`), then the
    automation's own lines above; [] for a run that never used the lane."""
    from looplab.core.upstream_board import board_lines
    return [*board_lines(state), *upstream_operator_lines(
        run_dir, events, cursor=getattr(state, "lane_ops_done", None))]
