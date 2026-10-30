"""Resolve report references to native per-query metadata, without reading arrays."""
from pathlib import Path
from fastapi import HTTPException
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.journals import read_event_source
from looplab.harness.per_query import current_uploads, read_manifest


def snapshot(rd: Path, generation: str, seq: int) -> dict:
    from looplab.serve.report_history import report_version
    events = read_event_source(rd)
    if run_generation_token(events) != generation:
        raise HTTPException(409, 'run generation changed')
    state = fold(events)
    report = report_version(events, seq)
    if report is None:
        raise HTTPException(404, 'report version unavailable')
    rows, health = read_manifest(rd)
    uploads = current_uploads([u for u in rows if u.get('run_uid') == state.run_uid and u.get('generation') == generation], state)
    refs = report.get('key_metrics') or {}
    bindings = []
    for node_ref in refs.get('nodes', []):
        node = state.nodes.get(node_ref.get('node_id'))
        for metric in refs.get('metrics', []):
            key, test_id = metric.get('key'), metric.get('test_id')
            candidates = []
            for upload in uploads:
                if upload.get('node_id') != node_ref.get('node_id') or not test_id or upload.get('test_id') != test_id:
                    continue
                check = upload.get('consistency') or {}
                metric_name = check.get('metric') if check.get('node_metric') == key else key
                if metric_name not in (upload.get('metrics') or {}):
                    continue
                attempt = upload.get('node_generation')
                expected = node_ref.get('node_generation')
                attempt_verified = (node is not None and type(attempt) is int and attempt == node.attempt
                                    and (expected is None or expected == attempt))
                matched_check = check.get('metric') == metric_name and check.get('node_metric') == key
                candidates.append({'upload_id': upload.get('upload_id'), 'attempt': attempt,
                                   'attempt_verified': attempt_verified, 'stale': upload.get('stale'),
                                   'n_queries': upload.get('n_queries'), 'uploaded_metric': metric_name, 'experiment_id': upload.get('experiment_id'),
                                   'arm': upload.get('arm'),
                                   'consistency': check.get('status', 'unchecked') if matched_check and attempt_verified else 'unchecked'})
            bindings.append({'node_id': node_ref.get('node_id'), 'node_generation': node_ref.get('node_generation'),
                             'metric': key, 'test_id': test_id, 'uploads': candidates[:3],
                             'additional_uploads': max(0, len(candidates) - 3)})
    return {'generation': generation, 'report_seq': seq, 'bindings': bindings[:128], 'total': len(bindings),
            'truncated': len(bindings) > 128, 'upload_history_complete': health.get('read_complete', False),
            'caveat': 'Only matching declared test and metric keys are shown. Attempt and numeric consistency do not prove identical GT, cohort or policy; report labels are declarations.'}
