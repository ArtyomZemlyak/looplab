"""Numerical co-metric regime observations, never an evaluation-faithfulness verdict.

Only operator-declared extra-metric readers are eligible. Candidate stdout keys, missing provenance,
legacy null protocols and incomparable measurements cannot acquire authority from this detector.
The small fixed window/threshold is an uncalibrated advisory heuristic, not a selection or launch gate.
"""
from __future__ import annotations

import heapq
import math

from looplab.core.comparability_rule import SAME, comparability_status, record_of
from looplab.core.fitness import finite_metric
from looplab.core.jsonutil import canonical_json_digest
from looplab.core.models import NodeStatus

METRIC_BEHAVIOR_DETECTORS = ('metric_regime_shift',)
METRIC_BEHAVIOR_VERSION = 1
REFERENCE_WINDOW = 32
MIN_REFERENCES = 3
MIN_COMMON_METRICS = 3
RATIO_BOUND = 2.0
SHIFT_FRACTION = .25


def metric_observation(node):
    """Exact bounded native facts, without source text, arbitrary provenance, or metric-name guesses."""
    declared = {}
    for name, raw in sorted(node.extra_metrics.items()):
        value = finite_metric(raw)
        if (node.extra_metrics_provenance.get(name) == 'declared' and value is not None
                and node.extra_metrics_direction.get(name) in ('min', 'max')):
            declared[name] = {'value': value, 'direction': node.extra_metrics_direction[name],
                              'channel': 'declared'}
    return {'node_id': node.id, 'attempt': node.attempt,
        'terminal_event_seq': node.terminal_event_seq, 'protocol_version': node.protocol_version,
        'metric': finite_metric(node.metric), 'feasible': node.feasible,
        'metrics': declared}


def metric_behavior_scan(node, previous_nodes, *, aborted=()):
    """One native outcome against earlier, same-ruler outcomes; no historical inference from null.

    Ratios describe positive-valued changes in either direction, not which metric is scientifically
    right. Nonpositive values are explicitly excluded because ratio thresholds do not apply to them.
    Reference selection uses terminal order alone, never similarity of the metric or desired outcome.
    """
    subject = metric_observation(node)
    def identity(observation):
        return {key: value for key, value in observation.items() if key != 'metrics'} | {
            'declared_metric_count': len(observation['metrics']),
            'observation_digest': canonical_json_digest(observation)}
    receipt = {'version': METRIC_BEHAVIOR_VERSION, 'node_id': node.id, 'generation': node.attempt,
        'detector': METRIC_BEHAVIOR_DETECTORS[0], 'subject': identity(subject),
        'parameters': {'reference_window': REFERENCE_WINDOW, 'min_references': MIN_REFERENCES,
            'min_common_metrics': MIN_COMMON_METRICS, 'ratio_bound': RATIO_BOUND,
            'shift_fraction': SHIFT_FRACTION},
        'references': [], 'checked_metrics': 0, 'shifted_metrics': 0, 'strongest_shifts': [],
        'status': 'insufficient_evidence', 'authority': 'advisory_observation_only',
        'question': 'Do these co-metric changes match the intended objective, or did the population, target or evaluator change?',
        'limits': 'Uncalibrated numerical heuristic over declared native scalar readers; no causal, cheating, faithfulness or quality verdict. Missing or self-reported metrics are not treated as checked.'}
    eligible_subject = (node.status is NodeStatus.evaluated and node.feasible and not node.tombstoned
        and node.id not in aborted and finite_metric(node.metric) is not None
        and type(node.terminal_event_seq) is int and type(node.protocol_version) is int
        and not node.extra_metrics_backfill
        and (node.metric_provenance or {}).get('salvaged') is not True)
    if eligible_subject and subject['metrics']:
        prior = heapq.nlargest(REFERENCE_WINDOW, (other for other in previous_nodes
            if other.id != node.id and other.status is NodeStatus.evaluated and other.feasible
            and not other.tombstoned and other.id not in aborted and finite_metric(other.metric) is not None
            and type(other.terminal_event_seq) is int
            and other.terminal_event_seq < node.terminal_event_seq
            and type(other.protocol_version) is int and other.protocol_version == node.protocol_version
            and not other.extra_metrics_backfill
            and (other.metric_provenance or {}).get('salvaged') is not True
            and comparability_status(record_of(node), record_of(other)) == SAME),
            key=lambda other: other.terminal_event_seq)
        observations = [metric_observation(other) for other in sorted(prior,
            key=lambda other: other.terminal_event_seq)]
        receipt['references'] = [identity(row) for row in observations]
        shifts = []
        for name, current in subject['metrics'].items():
            values = [row['metrics'][name]['value'] for row in observations
                if name in row['metrics'] and row['metrics'][name]['direction'] == current['direction']
                and row['metrics'][name]['value'] > 0]
            if current['value'] <= 0 or len(values) < MIN_REFERENCES:
                continue
            ordered = sorted(values)
            middle = len(ordered) // 2
            # Positive finite inputs: this midpoint avoids overflow in (left + right) / 2.
            median = (ordered[middle] if len(ordered) % 2 else ordered[middle - 1]
                + (ordered[middle] - ordered[middle - 1]) / 2)
            ratio = current['value'] / median
            if not math.isfinite(ratio) or ratio <= 0:
                continue
            receipt['checked_metrics'] += 1
            if ratio < 1 / RATIO_BOUND or ratio > RATIO_BOUND:
                shifts.append({'metric_name': name, 'value': current['value'],
                    'reference_median': median, 'ratio': ratio, 'reference_count': len(values),
                    'direction': current['direction'], 'channel': 'declared',
                    'reference_values': [{'node_id': row['node_id'], 'attempt': row['attempt'],
                        'value': row['metrics'][name]['value']} for row in observations
                        if name in row['metrics'] and row['metrics'][name]['direction'] == current['direction']
                        and row['metrics'][name]['value'] > 0]})
        receipt['shifted_metrics'] = len(shifts)
        receipt['strongest_shifts'] = sorted(shifts,
            key=lambda row: (-abs(math.log(row['ratio'])), row['metric_name']))[:3]
        if receipt['checked_metrics'] >= MIN_COMMON_METRICS:
            receipt['status'] = ('regime_shift' if len(shifts) >= max(2,
                math.ceil(receipt['checked_metrics'] * SHIFT_FRACTION)) else 'checked')
    receipt['evidence_digest'] = canonical_json_digest(receipt)
    return receipt


def metric_behavior_view(state):
    """Last 10 verified original observations with freshness from the complete current native state.

    Terminal sequence is internal Node metadata; a browser must not invent it from the public DTO.
    A changed numerical subject/reference makes the stored observation historical, not invalid.
    """
    saved_rows = sorted(state.metric_behavior_scans.values(),
        key=lambda row: row['subject']['terminal_event_seq'])[-10:]
    output = {}
    for saved in saved_rows:
        node = state.nodes.get(saved['node_id'])
        current = (node is not None and canonical_json_digest(saved) == canonical_json_digest(
            metric_behavior_scan(node, state.nodes.values(), aborted=state.aborted_nodes)))
        output[saved['node_id']] = {**saved, 'is_current': current}
    return output


def current_metric_behavior_scans(state):
    """Current regime-shift observations within the explicitly bounded latest 10 native view."""
    return [{key: value for key, value in row.items() if key != 'is_current'}
        for row in metric_behavior_view(state).values()
        if row['is_current'] and row['status'] == 'regime_shift']


def metric_behavior_summary(state):
    """Bounded factual prompt/CLI context; no verdict, repeat request or model invocation."""
    scans = current_metric_behavior_scans(state)[-2:]
    if not scans:
        return ''
    from looplab.core.evidence import fence_untrusted
    import orjson
    rows = [{'node_id': row['node_id'], 'attempt': row['generation'],
        'checked_metrics': row['checked_metrics'], 'shifted_metrics': row['shifted_metrics'],
        'evidence_digest': row['evidence_digest'],
        'strongest_shifts': [{key: value for key, value in shift.items()
            if key in ('metric_name', 'value', 'reference_median', 'ratio', 'reference_count')}
            for shift in row['strongest_shifts']]} for row in scans]
    return ('ADVISORY metric behavior observation; uncalibrated, not cheating or causal proof. '
        'Check the intended objective/population/evaluator before generalizing a gain. '
        'This asks for no new execution or repeat.\n'
        + fence_untrusted(orjson.dumps(rows).decode(), 'UNTRUSTED_METRIC_BEHAVIOR'))
