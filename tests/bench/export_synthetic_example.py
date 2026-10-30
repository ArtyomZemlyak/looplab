"""Export a safe, isolated synthetic campaign and briefing; no model or external service."""
import json
import os
import sys
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix='looplab-example-'))
out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / 'example'
out.mkdir(parents=True, exist_ok=True)
os.environ['LOOPLAB_BROWSER_FIXTURE_ROOT'] = str(root)
os.environ['LOOPLAB_AGENT_EVENTS'] = '0'
os.environ['LOOPLAB_UI_ANONYMOUS'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'browser'))
from looplab.engine import resources  # noqa: E402
resources.default_gpu_host_lease_path = lambda: root / 'isolated-host-lease'
import research_fixture  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

with TestClient(research_fixture.app) as client:
    params = {'expected_generation': research_fixture.gen}
    reports = []
    for name, path in [('briefing', 'research-briefing'), ('research-map', 'research-map'), ('campaign-export', 'research-export')]:
        request_params = list(params.items()) + ([('report_seq', seq) for seq in reports] if name == 'campaign-export' else [])
        response = client.get(f'/api/runs/browser-campaign/{path}', params=request_params)
        response.raise_for_status()
        data = response.json()
        if name == 'research-map':
            reports = [item['seq'] for item in data['reports']['items']]
        (out / f'{name}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        if name == 'campaign-export':
            (out / 'campaign.md').write_text(data['markdown'])
print(f'Generated isolated synthetic examples: {out}')
