"""Reproducible isolated CPU sample; reports measurements, never asserts a speedup."""
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix='looplab-research-bench-'))
os.environ['LOOPLAB_BROWSER_FIXTURE_ROOT'] = str(root)
os.environ['LOOPLAB_AGENT_EVENTS'] = '0'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'browser'))
from looplab.engine import resources  # noqa: E402
resources.default_gpu_host_lease_path = lambda: root / 'isolated-host-lease'
import research_fixture  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

run = 'browser-campaign'; gen = research_fixture.gen
with TestClient(research_fixture.app) as client:
    def measure(url, count=15):
        durations, size = [], None
        for _ in range(count):
            start = time.perf_counter()
            response = client.get(url)
            durations.append((time.perf_counter() - start) * 1000)
            response.raise_for_status()
            size = len(response.content)
        return {'samples': count, 'bytes': size, 'median_ms': round(statistics.median(durations), 3),
                'max_ms': round(max(durations), 3)}
    state = measure(f'/api/runs/{run}/state?observe_only=true')
    briefing = measure(f'/api/runs/{run}/research-briefing?expected_generation={gen}')
    newest = client.get('/api/agent-events', params={'after': -1, 'timeout': 0}).json()['newest_id']
    start = time.perf_counter()
    intent = {'expected_generation': gen, 'action_id': 'bench-ready', 'recipient_hook': 'fixture-agent',
              'reason': 'Synthetic ready event', 'next_read': f'/api/runs/{run}/research-briefing', 'urgency': 'normal'}
    handoff = client.post(f'/api/runs/{run}/agent-handoff', json=intent)
    handoff.raise_for_status()
    page = client.get('/api/agent-events', params={'after': newest, 'timeout': 0})
    page.raise_for_status()
    first_id = handoff.json()['event_id']
    replay = client.post(f'/api/runs/{run}/agent-handoff', json=intent)
    replay.raise_for_status()
    assert replay.json()['replayed'] and replay.json()['event_id'] == first_id
    after_retry = client.get('/api/agent-events', params={'after': newest, 'timeout': 0})
    after_retry.raise_for_status()
    assert sum(row['id'] == first_id for row in after_retry.json()['events']) == 1
    assert sum(row.get('action_id') == intent['action_id'] for row in after_retry.json()['events']) == 1
    response = client.get(f'/api/runs/{run}/research-briefing', params={'expected_generation': gen})
    response.raise_for_status()
    elapsed = (time.perf_counter() - start) * 1000
    result = {'scenario': 'two synthetic evaluated arms with genuine 4-query uploads; isolated CPU TestClient',
              'full_state': state, 'briefing': briefing,
              'briefing_bytes_reduction_percent': round(100 * (1 - briefing['bytes'] / state['bytes']), 2),
              'same_action_submissions': 2, 'same_action_events': 1,
              'ready_to_supported_read_ms': round(elapsed, 3),
              'ready_actions': ['POST agent-handoff', 'GET agent-events', 'POST same handoff retry', 'GET agent-events after retry', 'GET research-briefing'],
              'limits': 'Single-host sample, no network or actual human/agent response. No claimed speedup, GPU action, or comparison with a simulated hourly tick.'}
    print(json.dumps(result, indent=2))
