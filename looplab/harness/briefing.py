"""Compact composable live briefing; every component remains independently sourced."""
from pathlib import Path

from looplab.core.redact import redact_persisted_text
from looplab.engine import resources
from looplab.engine.gpu_queue import queue_status, read_gate
from looplab.harness.progress import snapshot as progress_snapshot
from looplab.harness.progress_summary import brief
from looplab.harness.research_map import snapshot as map_snapshot
from looplab.harness.readiness_binding import snapshot as binding_snapshot


def snapshot(rd: Path, generation: str, *, language: str = 'en') -> dict:
    from looplab.serve.node_activity import public_node_activity
    progress = brief(progress_snapshot(rd, generation, activity_reader=public_node_activity, language=language))
    research = map_snapshot(rd, generation, limit=5)
    bindings = binding_snapshot(rd, generation)
    gpu = queue_status(resources.default_gpu_host_lease_path())
    holder = gpu.get('holder') or {}
    gate = read_gate(rd) or {}
    from looplab.core.metric_profile import profile_for_run
    profile = profile_for_run(rd.parent, rd.name)
    from looplab.harness.profile_measurements import snapshot as measurement_snapshot
    measured = measurement_snapshot(rd, generation, offset=max(0, research['nodes']['total'] - 5), limit=5) if profile.get('state') == 'configured' else None
    if measured is not None:
        measured_profile = measured.pop('profile')
        measured['profile_digest'] = measured_profile.get('digest')
        measured['same_profile_as_briefing'] = measured_profile.get('digest') == profile.get('digest')
    plans = []
    by_binding = {(b.get('experiment_id'), b.get('subject_arm'), b.get('node_id')): b for b in bindings['items']}
    for experiment in research['experiments']['items']:
        for relation in experiment.get('relationships') or []:
            if relation.get('kind') != 'model_producer':
                continue
            key = (experiment.get('experiment_id'), relation.get('subject_arm'), relation.get('node_id'))
            binding = by_binding.get(key)
            arm = next((a for a in experiment.get('arms') or [] if a.get('name') == relation.get('subject_arm')), {})
            external = relation.get('run_id') not in (None, '', rd.name)
            phase = 'external_producer_unverified' if external else ('binding_current' if binding and binding.get('scope_and_attempt_current') else ('binding_stale' if binding else 'awaiting_producer_or_binding'))
            plans.append({'experiment_id': key[0], 'subject_arm': key[1], 'producer_node_id': key[2],
                          'state': phase, 'producer_run_id': relation.get('run_id') or rd.name, 'score_node_id': arm.get('node_id'), 'score_status': arm.get('node_status'),
                          'next_action': 'GET /api/runs/{run_id}/readiness-bindings',
                          'owner': 'external_agent', 'authority': 'none'})
    from looplab.harness.snapshot_settings import read_harness_settings
    configured_language = getattr(read_harness_settings(rd), 'output_language', 'auto')
    project_language = profile.get('profile', {}).get('authoring_language', 'auto')
    effective_language = configured_language if configured_language in ('en', 'ru') else project_language
    return {'version': 1, 'generation': progress['generation'], 'run_uid': progress['run_uid'],
            'event_seq': progress['event_seq'], 'goal': research['goal'], 'metric_profile': profile,
            'authoring_language': {'language': effective_language, 'run_config': configured_language,
                                   'project_preference': project_language,
                                   'meaning': 'Explicit run language takes precedence; otherwise use project preference. Preserve evidence, IDs and code; never translate a measurement.'},
            'operator_directions': progress.get('operator_directions'),
            'next_step': progress['next_step'], 'execution': progress['execution'],
            'recorded_lifecycle': progress['recorded_lifecycle'],
            'candidate_blockers_if_expanding': progress['candidate_blockers_if_expanding'],
            'pending_checkpoints': progress['pending_checkpoints'],
            'finish_pending_nodes': progress['finish_pending_nodes'],
            'finish_reviews_due': progress['finish_reviews_due'], 'finish_report_due': progress['finish_report_due'],
            'source_health': {'progress': progress['source_health'], 'protocol': research['source_health']['protocol']},
            'research': {'experiments': research['experiments'], 'reports': research['reports'], 'quality': research['quality'],
                         'campaign_overview': research['campaign_overview'],
                         'recent_profile_measurements': measured},
            'readiness': {'items': bindings['items'][-5:], 'plans': plans[:10],
                          'truncated': bindings['truncated'] or len(bindings['items']) > 5 or len(plans) > 10 or research['experiments']['has_more'], 'meaning': bindings['meaning']},
            'gpu': {'lease_held': gpu['lease_held'], 'holder_run_id': holder.get('run_id'),
                    'holder_unverified': holder.get('unverified', False), 'queue_length': gpu['queue_length'],
                    'gate': {'hold': gate.get('hold'), 'drain': gate.get('drain'),
                             'reason': redact_persisted_text(str(gate.get('reason') or ''), max_chars=500, entropy=False)}},
            'observation': {'research_event_seq': research['event_seq'],
                            'same_event_prefix': progress['event_seq'] == research['event_seq'] and (measured is None or measured['event_seq'] == progress['event_seq']),
                            'atomic_across_sources': False},
            'mirror': {'state': 'unavailable', 'meaning': 'Service mirror status is unavailable in this local view.'},
            'meaning': 'A briefing is an observation, not an authorization or proof of agent connection. Refresh progress and receipts before action.'}
