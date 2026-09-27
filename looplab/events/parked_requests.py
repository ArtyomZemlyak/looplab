"""Operator requests PARKED on the node budget — the one reading of `operator_request_parked` rows.

THE DEFECT THIS SERVES (doc 68 68.8, `minionerec-backbones-v10`, 2026-09-27). A queued `inject_node`
(idx 13) waited 20 minutes for a node slot after the budget ran out, and nothing said so: every
`inject_node` command read `succeeded` — its postcondition is the engine's ack, which only means the
intent was OBSERVED — and neither the UI nor the CLI knew that "observed" and "waiting for a
`budget_extend add_nodes` nobody was going to send" were different states. The engine now appends a
DIAGNOSTIC `operator_request_parked` row once per parking episode
(`engine/forced_requests.py::_note_parked_forced_request`); this module is what turns those rows back
into "which requests are waiting right now, and why".

WHY A PAIRING OVER THE LOG AND NOT A FOLDED FLAG. The row is diagnostic on purpose (a folded row the
Card session writes mid-build would move every seq fence a paid proposal is held to), so `RunState`
never carries it; what it DOES carry is each queue's cursor (`injects_done`, `forks_done`, the
ablations). A request is reported while its row exists and its cursor has not passed it, so a served,
failed or terminal-closed request stops being reported by construction — nothing has to "un-park" it.

Pure: no I/O, no clock. The writer's sentence (`parked_request_detail`) lives here too, so the row's
`detail`, the attention item and `looplab inspect` cannot come to phrase one fact two ways.
"""
from __future__ import annotations

from typing import Iterable, Optional

from looplab.events.types import EV_OPERATOR_REQUEST_PARKED

# What names ONE request across rows: a fork or an inject by its queue position (the index its
# done-receipt stamps), a forced ablation by the lifecycle it ablates.
PARKED_IDENTITY_KEYS: tuple[str, ...] = ("request", "idx", "node_id", "generation")
# …and what makes two rows about it the same statement. A new row is written only when one of these
# changed (`_note_parked_forced_request`), so a multi-hour wait costs one row.
PARKED_SIGNATURE_KEYS: tuple[str, ...] = PARKED_IDENTITY_KEYS + (
    "reason", "reserved", "held_by_card_requests", "limit")

_REQUEST_KINDS = frozenset({"fork", "inject", "ablate"})


def _count(value) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None


def _request_label(parked: dict) -> str:
    kind = parked.get("request")
    if kind == "ablate":
        return (f"the forced ablation of node #{parked.get('node_id')} "
                f"(generation {parked.get('generation')})")
    return f"{kind} #{parked.get('idx')}"


def parked_request_detail(parked: dict) -> str:
    """The engine's one sentence for a parked request, built only from its own numbers.

    Bounded and single-line on purpose: the attention feed shows it as a MEASURED detail
    (`ui/src/attentionModel.js::MEASURED_DETAIL_KINDS`), which admits a server sentence only when it
    carries no model-authored text — and none of this does."""
    reserved = _count(parked.get("reserved")) or 0
    held = _count(parked.get("held_by_card_requests")) or 0
    limit = _count(parked.get("limit")) or 0
    taken = reserved + held
    sentence = (f"{_request_label(parked)} waits for a node slot: the node budget is spent "
                f"({taken} of {limit} slots taken — {reserved} node id{'s' if reserved != 1 else ''} "
                "reserved")
    if held:
        sentence += (f", {held} held by open Card build request{'s' if held != 1 else ''}; a slot "
                     "frees when such a build commits or its request closes")
    return sentence + "). `budget_extend add_nodes` admits it now."


def _identity(data: dict) -> Optional[tuple]:
    kind = data.get("request")
    if kind not in _REQUEST_KINDS:
        return None
    if kind == "ablate":
        node_id, generation = _count(data.get("node_id")), _count(data.get("generation"))
        return None if node_id is None or generation is None else (kind, node_id, generation)
    idx = _count(data.get("idx"))
    return None if idx is None else (kind, idx)


def _still_waiting(identity: tuple, state) -> bool:
    kind = identity[0]
    if kind == "inject":
        return state.injects_done <= identity[1] < len(state.inject_requests)
    if kind == "fork":
        return state.forks_done <= identity[1] < len(state.fork_requests)
    node_id, generation = identity[1], identity[2]
    requested = any(isinstance(r, dict) and r.get("node_id") == node_id
                    and r.get("generation") == generation
                    for r in (state.ablate_request_generations or ()))
    done = any(isinstance(a, dict) and a.get("parent_id") == node_id
               and a.get("generation") == generation for a in (state.ablations or ()))
    return requested and not done


def open_parked_requests(events: Iterable, state) -> list[dict]:
    """Every request the engine SAID is parked on the node budget and that is still unserved.

    One entry per request, oldest episode first: `anchor` is the FIRST row of its episode (a stable
    identity — the attention item keeps its id while the numbers move) and `latest` the row that
    states its current numbers; `detail` is the latest sentence. Empty on a finished run: nothing is
    served there any more, so "waiting for a slot" would be the wrong sentence.
    """
    if getattr(state, "finished", False):
        return []
    episodes: dict[tuple, dict] = {}
    for event in events:
        if getattr(event, "type", None) != EV_OPERATOR_REQUEST_PARKED:
            continue
        data = event.data if isinstance(getattr(event, "data", None), dict) else {}
        identity = _identity(data)
        if identity is None:
            continue
        episode = episodes.setdefault(identity, {"anchor": event, "latest": event})
        episode["latest"] = event
    out = []
    for identity, episode in episodes.items():
        if not _still_waiting(identity, state):
            continue
        latest = episode["latest"].data or {}
        out.append({
            "request": identity[0],
            "anchor": episode["anchor"],
            "latest": episode["latest"],
            "detail": parked_request_detail(latest),
        })
    return sorted(out, key=lambda item: getattr(item["anchor"], "seq", -1))
