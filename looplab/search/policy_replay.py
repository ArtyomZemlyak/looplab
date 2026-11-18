"""Conditional recorded-tree replay: supported outcomes only, never a generative counterfactual.

A policy chooses an operator/parent action. The fixed proposal tape supplies the earliest unused
recorded candidate for that exact action, independently of its score. If its dependencies/outcome
are unavailable, stop with coverage and unknown attempts-to-target. No alternative outcome is
sampled, imputed, selected by hindsight or executed. This is a read-only diagnostic, not a grant.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from looplab.core.models import Event
from looplab.search.policy import make_policy

POLICIES = ('greedy', 'mcts', 'evolutionary')
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_ATTEMPTS = 1000
# Only facts about one chosen lifecycle. In particular, a global row with a coincidental
# node_id must never smuggle future advice, another outcome or selection authority into it.
NODE_FACTS = frozenset({'node_created', 'node_repaired', 'node_overlay_rebased',
    'node_eval_started', 'stage_finished', 'node_evaluated', 'node_failed',
    'reward_hack_suspected'})
# These alter selection beyond the original terminal facts. Replaying a different confirm,
# holdout or trust-review schedule requires outcomes we do not have. Refuse, do not invent it.
UNSUPPORTED_SELECTION = frozenset({'trust_gate_changed', 'node_confirmed', 'best_confirmed',
    'holdout_evaluated', 'node_verified', 'verifier_group_scored',
    'score_metrics_backfilled', 'extra_metrics_imported', 'approval_granted'})


def read_source(path: Path):
    from looplab.events.source_capture import capture_events
    return capture_events(path, max_bytes=MAX_SOURCE_BYTES)


def _choice(kind, parents):
    # Merge input order identifies the primary source parent. Treating it as a set could
    # silently substitute code materialized from the opposite primary parent.
    return kind, tuple(parents)


def _action_choice(action):
    if not isinstance(action, dict) or action.get('kind') not in ('draft', 'improve', 'merge'):
        return None
    extra = set(action) - {'kind', 'parent_id', 'parent_ids', '_scores', '_chosen', '_reason'}
    if extra:
        return None
    parents = action.get('parent_ids')
    if parents is None:
        parents = [] if action.get('parent_id') is None else [action['parent_id']]
    if not isinstance(parents, list) or any(type(p) is not int or p < 0 for p in parents):
        return None
    if action['kind'] == 'draft' and parents:
        return None
    return _choice(action['kind'], parents)


def replay_policy(events, *, policy='greedy', n_seeds=3, max_attempts=None):
    """Replay current pure policy defaults on a fixed recorded proposal tape and its real facts.

    Exact Node IDs are preserved. Only chosen nodes' own events through their first terminal are
    folded into the policy's state; unchosen outcomes never reach it. Original proposal context,
    paid roles, scheduling/parallelism and future repair/confirmation choices are NOT simulated.
    """
    from looplab.events.replay import fold
    from looplab.events.run_generation import run_generation_token
    if policy not in POLICIES or type(n_seeds) is not int or not 1 <= n_seeds <= MAX_ATTEMPTS:
        raise ValueError('choose greedy, mcts or evolutionary, and n_seeds in 1..1000')
    if max_attempts is not None and (type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS):
        raise ValueError('max_attempts must be an integer in 1..1000')
    events = list(events)
    from looplab.events.eventstore import event_sequence_continues, decode_event_record
    if not events or any(not isinstance(event, Event) for event in events):
        raise ValueError('policy replay requires a nonempty validated Event sequence')
    if not event_sequence_continues(events, 0):
        raise ValueError('policy replay requires a dense event sequence from zero')
    for event in events:
        decode_event_record(event.model_dump())  # Same version fence for programmatic callers.
    starts = [e for e in events if e.type == 'run_started']
    if len(starts) != 1:
        raise ValueError('policy replay requires exactly one original run start')
    start = starts[0]
    if any(e.type in NODE_FACTS and e.seq < start.seq for e in events):
        raise ValueError('node facts before the original run start are not supported')
    source = fold(events)
    target = source.best()
    limit = min(max_attempts or len(source.nodes) or 1, MAX_ATTEMPTS)
    result = {'version': 1, 'run_uid': source.run_uid, 'generation': run_generation_token(events),
        'event_seq': events[-1].seq, 'policy': policy, 'parameters': {'n_seeds': n_seeds, 'max_attempts': limit,
            'other_knobs': 'current named policy defaults; no paid value estimates or bandit/model routing'},
        'state': 'unavailable', 'source_node_count': len(source.nodes),
        'recorded_champion': None if target is None else {'node_id': target.id, 'attempt': target.attempt,
            'metric': target.metric, 'terminal_event_seq': target.terminal_event_seq},
        'recorded_attempts_to_champion': None, 'replayed_attempts_to_champion': None,
        'target_reached': False, 'supported_decisions': 0, 'decisions_considered': 0,
        'coverage': None, 'unknown_decisions': 0, 'steps': [], 'stop_reason': None,
        'counting_unit': 'one selected recorded node with an original terminal; repairs, physical eval calls and parallel elapsed time are not counted',
        'protocol_observation': 'exact recorded candidate/target protocol-version equality; legacy null versions are unknown, not proven same_protocol',
        'meaning': 'Conditional fixed recorded proposal tape, earliest unused exact operator/parent match. Only supported recorded outcomes are used. No causal off-policy quality estimate, unseen outcome, generated proposal, paid role, execution, resource grant or parallelism gain is inferred.'}
    if target is None:
        result['stop_reason'] = 'no_recorded_scalar_champion'
        return result
    if any(e.type == 'metric_retarget' for e in events):
        result['stop_reason'] = 'objective_changed_not_supported'
        return result
    if any(e.type in UNSUPPORTED_SELECTION for e in events):
        result['stop_reason'] = 'post_terminal_selection_schedule_not_supported'
        return result
    creates, created_counts, by_node = {}, {}, {}
    by_seq = {event.seq: event for event in events}
    from looplab.core.models import coerce_node_id
    reset_ids = {nid for e in events if e.type == 'node_reset'
                 if (nid := coerce_node_id(e.data)) is not None}
    for event in events:
        nid = event.data.get('node_id')
        if type(nid) is int:
            by_node.setdefault(nid, []).append(event)
        if event.type == 'node_created' and type(nid) is int:
            creates.setdefault(nid, event)
            created_counts[nid] = created_counts.get(nid, 0) + 1
    candidates = []
    for nid, creation in sorted(creates.items(), key=lambda pair: pair[1].seq):
        node = source.nodes.get(nid)
        if node is None:
            continue
        reason = None
        if nid in reset_ids or node.attempt != 0:
            reason = 'reset_lifecycle_not_supported'
        elif created_counts[nid] != 1:
            reason = 'recreated_lifecycle_not_supported'
        elif node.tombstoned or nid in source.aborted_nodes:
            reason = 'retired_or_aborted_candidate_not_supported'
        elif node.terminal_event_seq is None:
            reason = 'recorded_candidate_has_no_terminal'
        elif node.protocol_version != target.protocol_version:
            reason = 'candidate_protocol_differs_from_target'
        terminal = by_seq.get(node.terminal_event_seq)
        if terminal is None or terminal.type not in ('node_evaluated', 'node_failed'):
            reason = reason or 'recorded_candidate_has_no_supported_terminal'
        # All owned facts through the terminal, including feasibility/trust/stage facts. No
        # post-terminal confirmations/reviews, unchosen node metrics or future global advice.
        own = [e for e in by_node[nid] if e.type in NODE_FACTS
               and e.seq >= creation.seq and node.terminal_event_seq is not None
               and e.seq <= node.terminal_event_seq]
        if any(e.type == 'reward_hack_suspected' and e.data.get('node_id') == nid
               and node.terminal_event_seq is not None and e.seq > node.terminal_event_seq
               for e in by_node[nid]):
            reason = reason or 'post_terminal_trust_evidence_not_supported'
        dependencies = set(node.parent_ids) | set(node.uses)
        candidates.append({'node': node, 'creation': creation, 'events': own,
            'choice': _choice(node.operator, node.parent_ids), 'dependencies': dependencies, 'unavailable': reason})
    terminal_order = sorted((node.terminal_event_seq, node.id) for node in source.nodes.values()
                            if node.terminal_event_seq is not None)
    if not reset_ids and target.attempt == 0:
        result['recorded_attempts_to_champion'] = next((i + 1 for i, (_seq, nid) in enumerate(terminal_order)
                                                       if nid == target.id), None)
    selected, selected_events = set(), [start]
    counterfactual = make_policy(policy, n_seeds=n_seeds, max_nodes=limit)
    result['parameters']['effective_policy_attributes'] = vars(counterfactual).copy()
    # The policy defaults/code are part of the diagnostic, not an inferred copy of the source's
    # launch settings. Hash only these two tracked implementation modules, never private config.
    result['implementation_sha256'] = {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ('policy.py', 'policy_replay.py')}
    from looplab.events import source_capture
    result['implementation_sha256']['source_capture.py'] = hashlib.sha256(
        Path(source_capture.__file__).read_bytes()).hexdigest()
    result['state'] = 'supported_prefix'
    while len(selected) < limit:
        state = fold(sorted(selected_events, key=lambda e: e.seq))
        actions = counterfactual.next_actions(state)
        if not actions:
            result['stop_reason'] = 'policy_finished_before_recorded_target'
            break
        action = actions[0]  # Recompute after EACH real recorded outcome; no stale multi-action batch.
        choice = _action_choice(action)
        result['decisions_considered'] += 1
        if choice is None:
            result['stop_reason'] = 'policy_action_not_supported'
            break
        candidate = next((c for c in candidates if c['node'].id not in selected and c['choice'] == choice), None)
        if candidate is None:
            result['stop_reason'] = 'policy_choice_outside_recorded_tape'
            break
        if candidate['unavailable']:
            result['stop_reason'] = candidate['unavailable']
            break
        if not candidate['dependencies'] <= selected:
            result['stop_reason'] = 'recorded_choice_requires_unobserved_dependencies'
            break
        node = candidate['node']
        selected.add(node.id)
        selected_events.extend(candidate['events'])
        result['supported_decisions'] += 1
        observed = fold(sorted(selected_events, key=lambda e: e.seq))
        actual = observed.nodes.get(node.id)
        if (actual is None or actual.status != node.status or actual.metric != node.metric
                or actual.feasible != node.feasible or actual.parent_ids != node.parent_ids
                or actual.uses != node.uses or actual.terminal_event_seq != node.terminal_event_seq):
            result['stop_reason'] = 'recorded_outcome_not_reconstructible'
            result['supported_decisions'] -= 1
            selected.remove(node.id)
            break
        result['steps'].append({'attempt': len(selected), 'node_id': node.id,
            'operator': choice[0], 'parent_ids': list(choice[1]), 'terminal_event_seq': node.terminal_event_seq,
            'status': actual.status, 'metric': actual.metric, 'feasible': actual.feasible,
            'metric_observation': 'original native node facts through its terminal; no imputation'})
        if node.id == target.id and target.attempt == 0:
            result['target_reached'] = True
            result['replayed_attempts_to_champion'] = len(selected)
            result['state'] = 'recorded_target_reached'
            result['stop_reason'] = 'same_recorded_node_and_attempt_reached'
            break
    if result['stop_reason'] is None:
        result['stop_reason'] = 'attempt_budget_exhausted'
    if result['decisions_considered']:
        result['coverage'] = result['supported_decisions'] / result['decisions_considered']
    result['unknown_decisions'] = result['decisions_considered'] - result['supported_decisions']
    return result
