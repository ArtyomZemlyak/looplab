"""The ARTIFACT CONSUMER FENCE's two halves beside the pin (doc 73 §1.4).

THE PIN is the authority (`Node.uses_attempts`, round 3): each artifact a node `uses` is pinned to
the producer LIFECYCLE it was accepted against — an inject's intake, inherited by improve / merge
children, re-pinned by a consumer rebuild, and for a Researcher-proposed `Idea.uses` the producer's
lifecycle when the node was created (`events/replay.py::_use_pins`). Before every launch a pinned use
counts only if evaluated in exactly that lifecycle with its workdir stamp intact, else the consumer
ends `artifact_unavailable` (`engine/evaluate.py::EvaluateMixin._refuse_unusable_artifacts`): a
lifecycle generation only grows, so a reset producer can never be produced again in the pinned one.

WHAT THIS MODULE ADDS, and only that:

* WAITING — the one case the refusal above would get wrong. A use pinned to a lifecycle the producer
  is STILL IN and still producing (pending, not deleted, not aborted) — a Researcher proposed the
  preparation and its consumer back to back — can still be produced. The consumer is not dispatched
  meanwhile (`defer_waiting_consumers`, applied to every turn's selected actions), and ADMIT returns
  without a terminal if one reaches it anyway (`engine/evaluate.py::_eval_admit`).
* THE RECEIPT — a consumer's `node_evaluated.metric_provenance.uses` records, per producer, the
  lifecycle (`generation`) and code (`code`, the producer's workdir stamp) it was measured on.
  `stale_uses` compares it with the producer's CURRENT lifecycle; a champion whose receipt went stale
  — the dataset was re-produced, failed or deleted SINCE the number was measured — carries the
  champion caveat `stale_artifact` (`engine/champion_caveats.py`).

* NO BREEDING FROM A CONSUMER WHOSE ARTIFACT IS GONE (critic 2026-10-08). A child of a consumer
  inherits its pins (`engine/node_build.py::inherited_use_pins`); when the producer was deleted,
  aborted or FAILED in its current lifecycle, that pin can never be produced, so every child bred
  from the consumer — the champion, typically, which every greedy turn improves — is a paid build
  guaranteed to end `artifact_unavailable`, until `max_nodes`. `refuse_unrunnable_builds` drops such
  a build from a turn's selected actions BEFORE anything is paid, and hands the turn the best
  runnable alternative instead (an `improve` of the best breedable parent whose children can run,
  else a `draft`), its `_reason` naming the artifact — the `policy_decision` row the create loop
  appends is the record. Chosen over a `breedable_nodes()` exclusion because that pool is also the
  champion's (`RunState.best()` is drawn from it and GreedyTree improves `best()` directly), the
  surrogate's training set and the fold's Card readiness; and over a refusal at the build, which
  would leave the policy re-selecting the same parent every turn. A producer being RE-produced is
  not dead: its children are pinned to the new lifecycle and wait for it. The lanes that re-derive
  the POLICY's action before they pay (`drop_unrunnable_builds`) and every Card election
  (`unrunnable_card_ids`) ask the same rule, or the turn's refusal was bought one lane over.

An UNPINNED use (a log written before pins) keeps the historical existence-only rule; nothing here
waits on it, and nothing here refuses to breed from it.

PURE: no I/O.
"""
from __future__ import annotations


def _status(node) -> str:
    return str(getattr(getattr(node, "status", None), "value", getattr(node, "status", "")) or "")


def _pins(node) -> dict:
    pins = getattr(node, "uses_attempts", None)
    return pins if isinstance(pins, dict) else {}


def uses_waiting(state, node) -> bool:
    """A use is pinned to a lifecycle its producer is still in and still producing."""
    nodes = getattr(state, "nodes", None) or {}
    aborted = getattr(state, "aborted_nodes", None) or ()
    for key, pin in _pins(node).items():
        try:
            pid = int(key)
        except (TypeError, ValueError):
            continue
        producer = nodes.get(pid)
        if (producer is not None and not getattr(producer, "tombstoned", False)
                and pid not in aborted and type(pin) is int
                and getattr(producer, "attempt", None) == pin and _status(producer) == "pending"):
            return True
    return False


def uses_receipt(state, node) -> dict:
    """`{str(producer_id): {"generation": int, "code": str}}` for every artifact the node uses that
    is evaluated in its current lifecycle — what the consumer is admitted with. `{}` otherwise."""
    from looplab.engine.evaluate import workdir_manifest_digest
    out: dict = {}
    nodes = getattr(state, "nodes", None) or {}
    for pid in getattr(node, "uses", None) or []:
        producer = nodes.get(pid) if type(pid) is int else None
        if producer is None or getattr(producer, "tombstoned", False) or _status(producer) != "evaluated":
            continue
        out[str(pid)] = {"generation": int(getattr(producer, "attempt", 0) or 0),
                         "code": workdir_manifest_digest(producer)[:16]}
    return out


def stale_uses(state, node) -> list[int]:
    """The producers whose CURRENT lifecycle is not the one this node's metric was measured on —
    re-produced, failed or deleted since. `[]` when the node recorded no receipt (it uses nothing, or
    its row predates the receipt: silence is not staleness)."""
    prov = getattr(node, "metric_provenance", None)
    receipt = prov.get("uses") if isinstance(prov, dict) else None
    if not isinstance(receipt, dict) or not receipt:
        return []
    nodes = getattr(state, "nodes", None) or {}
    out = []
    for key, row in receipt.items():
        try:
            pid = int(key)
        except (TypeError, ValueError):
            continue
        producer = nodes.get(pid)
        generation = row.get("generation") if isinstance(row, dict) else None
        if (producer is None or getattr(producer, "tombstoned", False)
                or _status(producer) != "evaluated"
                or getattr(producer, "attempt", None) != generation):
            out.append(pid)
    return sorted(out)


def defer_waiting_consumers(state, actions: list) -> tuple[list, int]:
    """`actions` less the `evaluate` actions of nodes `uses_waiting` holds back, and how many were
    deferred. Everything else passes through untouched, in order."""
    nodes = getattr(state, "nodes", None) or {}
    kept, deferred = [], 0
    for a in actions or []:
        node = nodes.get(a.get("node_id")) if isinstance(a, dict) and a.get("kind") == "evaluate" \
            else None
        if node is not None and _pins(node) and uses_waiting(state, node):
            deferred += 1
            continue
        kept.append(a)
    return kept, deferred


# Every action kind that builds FROM a parent and so inherits its uses
# (`events/replay.py::node_uses`). `evaluate` builds nothing and `draft` has no parent.
_BREEDING_KINDS = frozenset({"improve", "merge", "ablate", "simplify", "expand"})


def _action_parents(action) -> list:
    raw = action.get("parent_ids")
    if isinstance(raw, list) and raw:
        return [p for p in raw if type(p) is int]
    pid = action.get("parent_id")
    return [pid] if type(pid) is int else []


def unproducible_uses(state, parent_ids) -> list[int]:
    """The artifacts a child of `parent_ids` would be PINNED to
    (`engine/node_build.py::inherited_use_pins`) whose producer can never be produced again: gone,
    deleted, aborted, or failed in its current lifecycle. `[]` for every parent that reads nothing
    — every run without artifacts — and for a use no parent pinned (the historical
    existence-only rule)."""
    from looplab.engine.node_build import inherited_use_pins
    nodes = getattr(state, "nodes", None) or {}
    aborted = getattr(state, "aborted_nodes", None) or ()
    if not any(getattr(nodes.get(p), "uses", None) for p in parent_ids or []):
        return []
    out = []
    for key in inherited_use_pins(state, parent_ids):
        pid = int(key)
        producer = nodes.get(pid)
        if (producer is None or getattr(producer, "tombstoned", False) or pid in aborted
                or _status(producer) not in ("evaluated", "pending")):
            out.append(pid)
    return sorted(out)


def unrunnable(state, action) -> bool:
    """`action` is a build whose child could only end `artifact_unavailable`: a breeding kind whose
    parents pin an artifact that can never be produced again (`unproducible_uses`)."""
    return (isinstance(action, dict) and action.get("kind") in _BREEDING_KINDS
            and bool(unproducible_uses(state, _action_parents(action))))


def drop_unrunnable_builds(state, actions: list) -> list:
    """`actions` less every `unrunnable` build, with NO replacement — what a lane that STAGES or
    BUYS work asks before it pays (a Card-inventory turn's `stageable` re-derivation, the occupancy
    lane, the isolated raw proposer). Each of those re-derives the POLICY's raw action, which on a
    greedy run is an improve of the champion — the consumer whose artifact is gone — so without
    this the turn's own refusal (`refuse_unrunnable_builds`) was undone one lane over: the refused
    build was proposed, paid for and staged as a Card (review 2026-10-15). The next selection turn
    makes the runnable choice. Identity on every run without artifacts."""
    return [a for a in actions or [] if not unrunnable(state, a)]


def unrunnable_card_ids(state) -> frozenset[str]:
    """The LIVE Cards on the board whose action is `unrunnable` — what every Card election leaves
    out BEFORE ranking, beside the plan's refusals (`engine/plan.py::endgame_refused_card_ids`) and
    through the same `SpeculativeSelectionContext.refused_card_ids`. A Card minted while its parent's
    artifact was alive outlives it; elected, it was a paid isolated build guaranteed to end
    `artifact_unavailable`, and on the selection turn it was re-elected (and refused) every turn
    (review 2026-10-15). Not a drop: the Card stays on the board. Empty without artifacts."""
    from looplab.engine.plan import _LIVE_CARD_STATUSES
    from looplab.search.card_selection import card_action
    nodes = getattr(state, "nodes", None) or {}
    if not any(getattr(n, "uses", None) for n in nodes.values()):
        return frozenset()
    out: set[str] = set()
    for card in (getattr(state, "cards", None) or {}).values():
        if (card.status not in _LIVE_CARD_STATUSES or card.merged_into is not None
                or card.dropped_reason is not None):
            continue
        action = card_action(card)
        if action is not None and unrunnable(state, action):
            out.add(card.id)
    return frozenset(out)


def refuse_unrunnable_builds(state, actions: list, *, sweep: bool = True) -> tuple[list, list]:
    """`(actions, refused)`: the turn's actions less every build whose child could only end
    `artifact_unavailable` (`unproducible_uses` of its parents), and the refused ones.

    When that leaves the turn NOTHING to do, it gets ONE runnable replacement — never an empty turn,
    which would walk the finish ladder over a run with budget left, and never a re-selection of the
    same dead lineage on every turn: an `improve` of the best-ranked breedable node whose children
    can run, else a `draft`. Raw, so it is authored into a fresh Card on a Card-driven run; added
    only to an otherwise EMPTY turn, because a Card lane may not mix with a raw action
    (`engine/orchestrator.py::Engine._handle_create_actions`). Its `_reason` names what was refused
    and why, and `_scores` makes the create loop record it as the turn's `policy_decision`.
    Everything else passes through untouched, in order.

    INSIDE AN ENDGAME RESERVE the replacement is the PLAN's (review 2026-10-15): this runs after
    `_plan_gate`, so an ordinary improve here spent a reserve slot on exactly what the gate exists
    to displace. The reserve's own next action (`engine/plan.py::endgame_own_action`, `sweep` the
    run's `Settings.endgame_sweep`) is the replacement when its child can run. When it cannot — the
    champion IS the consumer whose artifact is gone, so its sweeps, its ensemble and its refine are
    all dead — the reserve has nothing runnable of its own, and the turn takes the ordinary
    replacement, as the gate itself passes a turn through when the reserve has nothing of its own
    (`endgame_admits`)."""
    kept, refused = [], []
    for a in actions or []:
        if unrunnable(state, a):
            refused.append(a)
            continue
        kept.append(a)
    if not refused or kept:
        return kept, refused
    from looplab.engine.plan import endgame_own_action
    from looplab.search.policy import rank_by_metric
    dead = sorted({u for a in refused for u in unproducible_uses(state, _action_parents(a))})
    reason = (f"artifact_unavailable: artifact(s) {', '.join(f'#{u}' for u in dead)} can no longer "
              f"be produced (deleted, aborted or failed), so a child of "
              f"{', '.join(f'#{p}' for a in refused for p in _action_parents(a))} could only end "
              f"artifact_unavailable; building a runnable alternative instead")
    own = endgame_own_action(state, sweep=sweep)
    if own is not None and not unrunnable(state, own):
        return [{**own, "_scores": {},
                 "_reason": f"{reason} ({own.get('_reason') or 'the endgame reserve'})"}], refused
    blocked = {p for a in refused for p in _action_parents(a)}
    for node in rank_by_metric(state, state.breedable_nodes()):
        if node.id not in blocked and not unproducible_uses(state, [node.id]):
            return [{"kind": "improve", "parent_id": node.id, "_scores": {},
                     "_chosen": node.id, "_reason": reason}], refused
    return [{"kind": "draft", "_scores": {}, "_chosen": None, "_reason": reason}], refused


def artifact_build_note(kind, uses) -> str:
    """The Developer's note about what the node it builds IS (doc 73 §1.4): an ARTIFACT, which
    produces files later experiments read and is never scored, and/or a CONSUMER, which reads
    produced artifacts through `LOOPLAB_USES_WORKDIRS` — their workdirs, `os.pathsep`-joined, in
    `uses` order (`engine/eval_dispatch.py::EvalDispatchMixin._uses_workdirs_env`). "" for a node
    that is neither, so every other build prompt is byte-identical
    (`engine/node_build.py::NodeBuildMixin._artifact_idea` hands the idea back unchanged)."""
    parts = []
    if kind == "artifact":
        parts.append(
            "THIS NODE IS AN ARTIFACT (a preparation step): its pipeline PRODUCES files that later "
            "experiments read, and it is never scored or ranked — it succeeds when every stage exits "
            "0, and no metric line is needed. Write every output under this node's own working "
            "directory, at stable relative paths: that directory is what the experiments after it "
            "are handed.")
    ids = [u for u in (uses or []) if type(u) is int]
    if ids:
        parts.append(
            f"THIS NODE READS ARTIFACTS produced by node(s) {', '.join(f'#{u}' for u in ids)}: when "
            "it is evaluated, the environment variable LOOPLAB_USES_WORKDIRS holds their absolute "
            "working directories (read-only), joined with os.pathsep, in exactly that order. Read "
            "the prepared files from there instead of re-creating them.")
    return "\n".join(parts)
