"""Read configured scalar metric keys from existing node evidence; never recompute scores."""
from __future__ import annotations

import math
from pathlib import Path
from fastapi import HTTPException
from looplab.core.metric_profile import profile_for_run
from looplab.core.redact import redact_persisted_text
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.journals import read_event_source


def snapshot(rd: Path, generation: str, offset: int = 0, limit: int = 10) -> dict:
    events = read_event_source(rd)
    if run_generation_token(events) != generation:
        raise HTTPException(409, 'run generation changed')
    state = fold(events)
    profile = profile_for_run(rd.parent, rd.name)
    metrics = profile.get('profile', {}).get('metrics', []) if profile.get('state') == 'configured' else []
    nodes = sorted(state.nodes.values(), key=lambda node: node.id)
    result = []
    for node in nodes[offset:offset + limit]:
        extra = node.extra_metrics if isinstance(node.extra_metrics, dict) else {}
        values = []
        for metric in metrics:
            value = extra.get(metric['key'])
            present = isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
            backfill = node.extra_metrics_backfill or {}
            recovered = bool(backfill.get('backfilled') and (not isinstance(backfill.get('keys'), list) or metric['key'] in backfill['keys']))
            values.append({'key': metric['key'], 'value': value if present else None,
                           'source': 'node.extra_metrics' if present else 'not_measured',
                           'channel': (node.extra_metrics_provenance or {}).get(metric['key']) if present else None,
                           'reconstructed': recovered if present else False})
        result.append({'node_id': node.id, 'attempt': node.attempt, 'status': node.status,
                       'name': redact_persisted_text(str(node.operator or ''), max_chars=256, entropy=False, single_line=True), 'values': values})
    return {'generation': generation, 'event_seq': events[-1].seq, 'profile': profile, 'nodes': result,
            'offset': offset, 'limit': limit, 'total': len(nodes), 'has_more': offset + limit < len(nodes),
            'caveat': 'Values are recorded extra_metrics on the current attempt. The profile is an author declaration; cohort, policy, denominator and cross-node comparability are not verified.'}
