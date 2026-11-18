"""Numerical evidence, not a code keyword, drives advisory metric-regime observations."""
from __future__ import annotations
import copy

import anyio
import pytest

from factories import make_engine
from looplab.core.metric_behavior import metric_behavior_scan, current_metric_behavior_scans
from looplab.core.models import Node, NodeStatus, RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def node(nid, *, values=None, metric=1., seq=None):
    values = values or {'recall': 1., 'ndcg': 1., 'coverage': 1., 'latency': 1.}
    return Node(id=nid, operator='draft', idea={'operator': 'draft'}, status=NodeStatus.evaluated, metric=metric,
        terminal_event_seq=seq if seq is not None else nid * 2 + 2, protocol_version=1,
        extra_metrics=values, extra_metrics_provenance={key: 'declared' for key in values},
        extra_metrics_direction={key: 'max' for key in values},
        metric_provenance={'comparability': {'version': 1, 'authority': 'declared',
            'keys': {'declared': 'a' * 64}}})


def population():
    return [node(i) for i in range(3)] + [node(3,
        values={'recall': 2.13, 'ndcg': .15, 'coverage': .18, 'latency': 1.}, metric=2.)]


def test_real_values_trigger_bounded_observation_without_interpreting_the_objective():
    rows = population()
    scan = metric_behavior_scan(rows[-1], rows)
    assert scan['status'] == 'regime_shift'
    assert scan['checked_metrics'] == 4 and scan['shifted_metrics'] == 3
    assert len(scan['strongest_shifts']) == 3
    shifted = {row['metric_name']: row for row in scan['strongest_shifts']}
    assert shifted['recall']['ratio'] == 2.13 and shifted['ndcg']['ratio'] == .15
    assert shifted['ndcg']['reference_median'] == 1.
    assert shifted['ndcg']['reference_values'] == [
        {'node_id': i, 'attempt': 0, 'value': 1.} for i in range(3)]
    assert scan['authority'] == 'advisory_observation_only'
    assert 'no causal' in scan['limits']
    assert 'metrics' not in scan['references'][0]  # digest binds all; only top3 carry raw reference values
    assert rows[-1].metric == 2.


@pytest.mark.parametrize('change', ['auto', 'unknown_channel', 'unknown_direction', 'unknown_protocol',
    'different_protocol', 'unknown_comparability', 'inferred', 'different_comparability',
    'failed', 'infeasible', 'salvaged', 'backfill', 'aborted', 'future'])
def test_unqualified_or_future_evidence_cannot_mint_a_checked_comparison(change):
    rows = population()
    target, refs = rows[-1], rows[:3]
    aborted = []
    if change == 'auto': target.extra_metrics_provenance = {key: 'auto' for key in target.extra_metrics}
    if change == 'unknown_channel': target.extra_metrics_provenance = {}
    if change == 'unknown_direction': target.extra_metrics_direction = {}
    if change == 'unknown_protocol': target.protocol_version = None
    if change == 'different_protocol': target.protocol_version = 2
    if change == 'unknown_comparability': target.metric_provenance = {}
    if change == 'inferred':
        for item in rows: item.metric_provenance = {'comparability': {'keys': {'inferred': 'x'}}}
    if change == 'different_comparability': target.metric_provenance['comparability']['keys']['declared'] = 'b' * 64
    if change == 'failed': refs[0].status = NodeStatus.failed
    if change == 'infeasible': refs[0].feasible = False
    if change == 'salvaged': refs[0].metric_provenance['salvaged'] = True
    if change == 'backfill': refs[0].extra_metrics_backfill = {'source': 'retrospective'}
    if change == 'aborted': aborted = [0]
    if change == 'future': refs[0].terminal_event_seq = 100
    scan = metric_behavior_scan(target, rows, aborted=aborted)
    assert scan['status'] == 'insufficient_evidence' and scan['checked_metrics'] == 0


def test_terminal_order_window_excludes_old_outcomes_and_never_selects_on_metric():
    rows = [node(i, metric=1000-i) for i in range(40)]
    target = node(99, seq=100, values={'recall': 1., 'ndcg': 1., 'coverage': 1., 'latency': 1.})
    scan = metric_behavior_scan(target, reversed(rows))
    assert [row['node_id'] for row in scan['references']] == list(range(8, 40))
    assert scan['status'] == 'checked' and scan['shifted_metrics'] == 0
    assert len(scan['references']) == 32


@pytest.mark.parametrize('value', [0., -1., 1e-300])
def test_ratio_inapplicable_values_and_floating_underflow_abstain(value):
    rows = [node(i, values={'m': 1e300, 'n': 1e300, 'q': 1e300}) for i in range(3)]
    target = node(3, values={'m': value, 'n': value, 'q': value})
    scan = metric_behavior_scan(target, rows)
    assert scan['checked_metrics'] == 0 and scan['status'] == 'insufficient_evidence'


def recorded_source(path):
    store = EventStore(path)
    store.append('run_started', {'run_id': 'r', 'run_uid': 'a' * 32, 'direction': 'max', 'trust_gate': 'block'})
    for item in population():
        store.append('node_created', {'node_id': item.id, 'operator': 'draft',
            'protocol_version': 1, 'idea': {'operator': 'draft'}})
        data = item.model_dump(mode='json')
        data['node_id'] = data.pop('id')
        store.append('node_evaluated', data)
    state = fold(store.read_all())
    return store, state


def test_native_replay_verifies_original_facts_without_gating_or_accepting_forged_observations(tmp_path):
    store, state = recorded_source(tmp_path / 'events.jsonl')
    original = state.model_dump(mode='json')
    scan = metric_behavior_scan(state.nodes[3], state.nodes.values())
    store.append('metric_behavior_scan', scan)
    after = fold(store.read_all())
    assert after.metric_behavior_scans == {3: scan}
    assert after.best_node_id == state.best_node_id == 3
    assert not after.reward_hacks
    assert current_metric_behavior_scans(after) == [scan]
    unchanged = after.model_dump(mode='json');unchanged['metric_behavior_scans'] = {}
    assert unchanged == original
    tampered = copy.deepcopy(scan);tampered['strongest_shifts'][0]['value'] = 999
    store.append('metric_behavior_scan', tampered)
    assert fold(store.read_all()).metric_behavior_scans == {3: scan}
    store.append('node_reset', {'node_id': 1, 'from_stage': 'eval'})
    assert not current_metric_behavior_scans(fold(store.read_all()))


def test_engine_writes_a_native_observation_after_its_real_cpu_terminal(tmp_path):
    engine = make_engine(tmp_path / 'r', n_seeds=1, max_nodes=1)
    state = anyio.run(engine.run)
    assert state.evaluated_nodes()
    events = engine.store.read_all()
    scans = [event for event in events if event.type == 'metric_behavior_scan']
    assert len(scans) == 1
    assert scans[0].data['status'] == 'insufficient_evidence'
    assert scans[0].seq > state.nodes[scans[0].data['node_id']].terminal_event_seq
    assert fold(events).metric_behavior_scans == {0: scans[0].data}
    assert not state.reward_hacks


def test_genuine_cpu_subprocess_declared_readers_reach_native_regime_observation(tmp_path, monkeypatch):
    import json
    import sys
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.core.models import Idea
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree
    from tests.test_comparison_contract_pipeline import _contract

    # Explicit CPU-only sandbox and inventory; this test cannot acquire the real host GPU pool.
    monkeypatch.setattr('looplab.engine.orchestrator._detect_gpu_ids', lambda: [])
    repo = tmp_path / 'repo';repo.mkdir()
    scorer = ("import json;from pathlib import Path\n"
        "v=json.loads(Path('values.json').read_text())['variant']\n"
        "print(json.dumps({'metric':1.+v,'recall':1.+1.13*v,"
        "'ndcg':1.-.85*v,'coverage':1.-.82*v,'latency':1.}))\n")
    (repo / 'run.py').write_text(scorer)
    (repo / 'values.json').write_text('{"variant":0}\n')
    values = population()
    class Researcher:
        def propose(self, state, parent):
            return Idea(operator='draft', footprint={'gpus': 0},
                rationale='CPU scalar fixture with declared readers.')
    class Developer:
        calls = 0
        def implement(self, idea):
            row = values[self.calls];self.calls += 1
            self.last_files = {'values.json': json.dumps({'variant': int(row.id == 3)})}
            self.last_footprint = {'gpus': 0}
            return ''
    spec = EvalSpec(command=[sys.executable, 'run.py'], metric={'kind': 'stdout_json', 'key': 'metric'},
        metrics={name: {'kind': 'stdout_json', 'key': name, 'direction': 'max'}
            for name in values[0].extra_metrics})
    task = RepoTask(id='metric-behavior-cpu', goal='record measured scalar fixture',
        direction='max', editable_path=str(repo), edit_surface=['values.json'], eval=spec,
        comparison_contract={**_contract(), 'direction': 'max'})
    developer = Developer()
    engine = Engine(tmp_path / 'run', task=task, researcher=Researcher(), developer=developer,
        sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=4, max_nodes=4),
        inline_repair=False, auto_install_deps=False)
    engine.run_dir.mkdir(exist_ok=True)
    (engine.run_dir / 'task.snapshot.json').write_text(json.dumps(task.model_dump(mode='json')))
    (engine.run_dir / 'protocol_amendments.jsonl').write_text('')  # actual pinned initial version0
    state = anyio.run(engine.run)
    assert len(state.evaluated_nodes()) == developer.calls == 4, state.model_dump(mode='json')
    assert state.best_node_id == 3 and state.nodes[3].metric == 2.
    scans = [event for event in engine.store.read_all() if event.type == 'metric_behavior_scan']
    assert len(scans) == 4 and scans[-1].data['status'] == 'regime_shift', [event.data for event in scans]
    assert state.metric_behavior_scans[3] == scans[-1].data
    assert scans[-1].data['checked_metrics'] == 4 and scans[-1].data['shifted_metrics'] == 3
    assert all(signal['signal'] != 'metric_regime_shift'
        for row in state.reward_hacks for signal in row['signals'])
    assert state.trust_gate != 'block'
    assert current_metric_behavior_scans(state) == [scans[-1].data]
    from looplab.agents.state_brief import _state_brief
    assert 'UNTRUSTED_METRIC_BEHAVIOR' in _state_brief(state, state.best())
    assert (repo / 'run.py').read_text() == scorer
    assert (repo / 'values.json').read_text() == '{"variant":0}\n'


def test_bool_metadata_cannot_alias_canonical_integer_receipt(tmp_path):
    store, state = recorded_source(tmp_path / 'events.jsonl')
    scan = metric_behavior_scan(state.nodes[3], state.nodes.values())
    scan['version'] = True  # Python equality True==1 must not validate a malformed native receipt.
    store.append('metric_behavior_scan', scan)
    assert not fold(store.read_all()).metric_behavior_scans


def test_native_fixture_and_maximum_declared_population_stay_bounded(tmp_path):
    import json
    from pathlib import Path
    from looplab.core.models import EXTRA_METRICS_MAX_ITEMS
    store, state = recorded_source(tmp_path / 'events.jsonl')
    expected = json.loads((Path(__file__).parent / 'fixtures/metric_behavior_cases_v1.json').read_text())
    assert metric_behavior_scan(state.nodes[3], state.nodes.values()) == {key: value for key, value in expected['state']['metric_behavior_scans']['3'].items() if key != 'is_current'}
    values = {('long_metric_' + str(i) + '_' * 60): 1. for i in range(EXTRA_METRICS_MAX_ITEMS)}
    refs = [node(i, values=values) for i in range(32)]
    target = node(100, seq=500, values={key: 4. for key in values})
    scan = metric_behavior_scan(target, refs)
    assert scan['checked_metrics'] == EXTRA_METRICS_MAX_ITEMS
    assert len(scan['strongest_shifts']) == 3 and len(scan['references']) == 32
    assert len(json.dumps(scan).encode()) < 64 * 1024
    assert all(len(shift['reference_values']) == 32 for shift in scan['strongest_shifts'])


def test_actual_owner_state_publishes_bounded_freshness_without_exposing_internal_terminal_field(tmp_path):
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    rd = tmp_path / 'r';rd.mkdir()
    store, state = recorded_source(rd / 'events.jsonl')
    scan = metric_behavior_scan(state.nodes[3], state.nodes.values())
    store.append('metric_behavior_scan', scan)
    client = TestClient(make_app(tmp_path))
    result = client.get('/api/runs/r/state')
    assert result.status_code == 200, result.text
    data = result.json()['state']
    assert data['metric_behavior_scans']['3'] == {**scan, 'is_current': True}
    assert 'terminal_event_seq' not in data['nodes']['3']
    assert data['best_node_id'] == 3
    store.append('node_reset', {'node_id': 1, 'from_stage': 'eval'})
    reset = client.get('/api/runs/r/state').json()['state']
    assert reset['metric_behavior_scans']['3'] == {**scan, 'is_current': False}


def test_real_inspect_surfaces_anomaly_without_running_or_rewriting_source(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    rd = tmp_path / 'r';rd.mkdir()
    store, state = recorded_source(rd / 'events.jsonl')
    store.append('metric_behavior_scan', metric_behavior_scan(state.nodes[3], state.nodes.values()))
    before = store.path.read_bytes()
    result = CliRunner().invoke(app, ['inspect', str(rd)])
    assert result.exit_code == 0, result.output
    assert 'ADVISORY metric behavior observation' in result.output
    assert 'UNTRUSTED_METRIC_BEHAVIOR' in result.output
    assert store.path.read_bytes() == before


def test_large_finite_reference_median_does_not_overflow():
    values = {'m': 1e308, 'n': 1e308, 'q': 1e308}
    refs = [node(i, values=values) for i in range(4)]
    target = node(5, values={key: 1e307 for key in values})
    scan = metric_behavior_scan(target, refs)
    assert scan['checked_metrics'] == 3 and scan['status'] == 'regime_shift'
    assert all(row['reference_median'] == 1e308 for row in scan['strongest_shifts'])
