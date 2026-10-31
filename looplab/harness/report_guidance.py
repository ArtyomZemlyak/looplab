"""Advisory report context over recorded events; never authors claims or adds a gate."""
from __future__ import annotations

import math
from pathlib import Path
from urllib.parse import quote
from fastapi import HTTPException
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.journals import read_event_source
from looplab.harness.research_map import _text

GUIDE = '/api/runs/{run_id}/research-workflow'
REPORT_INSTRUCTION = (
    'After a meaningful terminal result, update the cumulative scientific overview as well as the '
    'current result report. For each important direction explain question, control, measured result, '
    'meaning, limitations and decision; retain negative results, cite actual evidence, and give a '
    'next discriminating question with possible outcomes. Review denominators, units, captions and '
    'language. After publication, read the saved report version and fresh briefing: review all eight '
    'report_fields_empty, references and meaning; explain honestly when a field is inapplicable. '
    'The agent authors the analysis; these checks do not verify scientific truth or gate GPU work.'
)


def snapshot(rd: Path, generation: str) -> dict:
    from looplab.serve.report_history import report_version
    events = read_event_source(rd)
    if run_generation_token(events) != generation:
        raise HTTPException(409, 'run generation changed')
    state = fold(events)
    root = '/api/runs/' + quote(rd.name, safe='')
    versions = [e for e in events if e.type == 'report_generated']
    report_seqs = {e.seq for e in versions}
    overview_event = next((e for e in reversed(versions) if isinstance(e.data, dict)
                           and isinstance(e.data.get('content'), dict)
                           and isinstance(e.data['content'].get('campaign_overview'), dict)), None)
    previous = report_version(events, overview_event.seq) if overview_event else None
    overview = (previous or {}).get('campaign_overview') or {}
    last_overview_seq = overview_event.seq if overview_event else -1
    outcomes = []
    for e in events:
        if e.type not in ('node_evaluated', 'node_failed') or not isinstance(e.data, dict):
            continue
        nid = e.data.get('node_id')
        if type(nid) is not int:
            continue
        node = state.nodes.get(nid)
        metric = e.data.get('metric')
        metric = metric if type(metric) in (int, float) and math.isfinite(metric) else None
        extra = e.data.get('extra_metrics') if isinstance(e.data.get('extra_metrics'), dict) else {}
        values = [{'key': _text(k, 120), 'value': v} for k, v in extra.items()
                  if isinstance(k, str) and type(v) in (int, float) and math.isfinite(v)][:8]
        outcomes.append({'event_seq': e.seq, 'type': e.type, 'node_id': nid,
                         'recorded_attempt': e.data.get('generation') if type(e.data.get('generation')) is int else None,
                         'current_attempt': getattr(node, 'attempt', None),
                         'current_status': str(getattr(node, 'status', 'unknown')),
                         'metric': metric, 'extra_metrics': values,
                         'node_api': f'{root}/nodes/{nid}?expected_generation={generation}',
                         'after_overview': e.seq > last_overview_seq})
    from looplab.harness.campaign import experiments_of, read_journal
    journal, protocol_health = read_journal(rd)
    experiments = experiments_of(journal, state.run_uid)
    recent_ids = {row['node_id'] for row in outcomes[-10:]}
    comparisons = []
    for experiment_id, experiment in experiments.items():
        arms = experiment.get('arms') if isinstance(experiment.get('arms'), dict) else {}
        matched = [arm.get('node_id') for arm in arms.values() if isinstance(arm, dict)
                   and arm.get('run_id') in (None, '', rd.name) and arm.get('node_id') in recent_ids]
        if matched:
            comparisons.append({'experiment_id': _text(experiment_id, 128), 'title': _text(experiment.get('title'), 200),
                                'node_ids': sorted(set(matched)),
                                'comparison_api': f"{root}/experiments/{quote(experiment_id, safe='')}/comparison"})
    warnings = []
    if not previous:
        warnings.append({'code': 'overview_missing', 'action': 'Read report history and author a cumulative overview.'})
    elif any(row['after_overview'] for row in outcomes):
        warnings.append({'code': 'terminal_after_overview', 'action': 'Review terminal outcomes and update the overview if scientifically relevant.'})
    if previous:
        for key in ('summary', 'recommendation', 'recommendation_limits', 'next_directions'):
            if not overview.get(key):
                warnings.append({'code': 'overview_field_missing', 'field': key,
                                 'action': 'Fill or explain non-applicability in the authored report.'})
        if not overview.get('report_seqs'):
            warnings.append({'code': 'report_references_missing',
                             'action': 'Link relevant existing report event sequences when available, or cite node and contrast evidence explicitly.'})
        for seq in overview.get('report_seqs') or []:
            if seq not in report_seqs:
                warnings.append({'code': 'invalid_report_reference', 'report_seq': seq,
                                 'action': 'Use event sequence numbers of existing report_generated versions, not node IDs.'})
    from looplab.harness.research_quality import report_fields_empty
    current_report = state.report if isinstance(state.report, dict) else None
    empty_fields = report_fields_empty(current_report) if current_report else []
    if current_report:
        warnings.extend({'code': 'report_field_empty', 'field': field,
                         'action': 'Review the saved current report and fill or explain non-applicability; see the canonical workflow.'}
                        for field in empty_fields)
    return {'generation': generation, 'event_seq': events[-1].seq,
            'current_report_seq': current_report.get('published_seq') if current_report else None,
            'report_fields_empty': empty_fields if current_report else None,
            'instruction': REPORT_INSTRUCTION, 'workflow': GUIDE.replace('{run_id}', quote(rd.name, safe='')),
            'previous_overview_seq': overview_event.seq if overview_event else None,
            'previous_overview_api': f'{root}/report/versions/{overview_event.seq}' if overview_event else None,
            'recent_terminal_events': list(reversed(outcomes[-10:])), 'terminal_event_count': len(outcomes),
            'terminal_events_truncated': len(outcomes) > 10,
            'recent_report_versions': [{'seq': e.seq, 'api': f'{root}/report/versions/{e.seq}'} for e in reversed(versions[-10:])],
            'report_versions_truncated': len(versions) > 10,
            'comparison_refs': comparisons[-10:], 'comparison_refs_truncated': len(comparisons) > 10,
            'protocol_health': protocol_health,
            'warnings': warnings[:40], 'warnings_truncated': len(warnings) > 40,
            'meaning': 'Recorded pointers and deterministic checks only; read actual results, comparisons and full report versions. A node number is not a report event sequence; no scientific truth or action permission is inferred.'}
