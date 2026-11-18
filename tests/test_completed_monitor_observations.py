"""A real CPU command reaches one terminal while unanswered advisory evidence survives restart."""
import json
import sys
import time
import threading
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.checkpoints import (answer_for, ask, close_completed_observations, pending)
from looplab.harness.checkpoint_history import project_checkpoints
from looplab.serve.server import make_app
from tests.factories import make_engine
from tests.test_external_checkpoints import seeded


def _history(rd, store):
    events = store.read_all()
    rows = [json.loads(line) for line in (rd / 'harness_checkpoints.jsonl').read_text().splitlines()]
    health = {'read_complete': True}
    history = project_checkpoints(rows, health, events, fold(events), run_generation_token(events))
    assert health['read_complete']
    return rows, history


@pytest.mark.parametrize('slow_publish', [False, True])
@pytest.mark.parametrize('artifact', [False, True])
@pytest.mark.parametrize('cadence', [.02, 600.])
def test_real_completed_command_keeps_unknown_review_without_terminal_wait(tmp_path, monkeypatch, artifact, cadence, slow_publish, routine=False):
    rd = tmp_path / 'runs' / 'demo'
    engine = make_engine(rd, external_harness=True, train_monitor=True,
                         train_monitor_interval_s=cadence, eval_canary=False, inline_repair=False)
    engine.concurrent_research = False
    engine._monitor_cadence = lambda: cadence
    command = [sys.executable, '-c',
               f"import time; print('step 1 loss={0.9 if routine else 'nan'}', flush=True); time.sleep(.15); "
               + ("print('Saved artifact', flush=True)" if artifact else "print('{\"metric\": 0.5}', flush=True)")]
    engine._eval_spec = {'command': command, 'metric': {'kind': 'stdout_json'}, 'timeout': 5}
    if slow_publish:
        import looplab.harness.checkpoints as checkpoints
        returned = threading.Event()
        real_eval, real_ask = engine._run_eval, checkpoints.ask

        def execute(*args, **kwargs):
            result = real_eval(*args, **kwargs)
            returned.set()
            return result

        def publish_after_return(*args, **kwargs):
            assert returned.wait(3), "CPU evaluator did not return during question publication"
            return real_ask(*args, **kwargs)

        engine._run_eval = execute
        monkeypatch.setattr(checkpoints, 'ask', publish_after_return)
    settings = Settings(backend='toy', external_harness=True, train_monitor=True)
    (rd / 'config.snapshot.json').write_text(json.dumps(settings.model_dump(mode='json')))
    engine.store.append('run_started', {'run_id': 'demo', 'task_id': 'toy', 'direction': 'min'})
    engine.store.append('node_created', {'node_id': 0, 'parent_ids': [], 'operator': 'draft',
        'idea': {'operator': 'draft', 'params': {'x': 3., 'y': -1.}}, 'code': 'print(1)',
        **({'node_kind': 'artifact'} if artifact else {})})
    if routine:
        from tests.test_monitor_rules import RULES
        rules_client = TestClient(make_app(rd.parent))
        assert rules_client.put('/api/runs/demo/harness-monitor-rules', json=RULES).status_code == 200
    started = time.monotonic()

    async def drive():
        with anyio.fail_after(8):
            await engine._evaluate(0, anyio.CapacityLimiter(1), None)

    anyio.run(drive)
    assert time.monotonic() - started < 8
    events = engine.store.read_all()
    terminal = [e for e in events if e.type in ('node_evaluated', 'node_failed')]
    assert len(terminal) == 1 and terminal[0].type == 'node_evaluated', terminal
    rows, history = _history(rd, engine.store)
    assert len(history) == 1
    observation = history[0]
    assert 'step 1 loss=' in observation['question']['observation']
    if routine:
        assert observation['answer']['answerer'] == 'rule'
        assert observation['closure'] is None and observation['status'] == 'answered'
        assert [r['type'] for r in rows] == ['question', 'answer']
    else:
        assert observation['answer'] is None and observation['review_status'] == 'unreviewed'
        assert observation['status'] == 'ended_before_decision'
        assert [r['type'] for r in rows] == ['question', 'closure']
        assert not [e for e in events if e.type == 'train_monitor_alert' and e.data.get('status') == 'healthy']
    settle, = [e for e in events if e.type == 'eval_invocation_settled']
    assert settle.data['execution_timing']['advisory_review'] == 'recorded'
    assert settle.data['execution_timing']['resource_occupancy'] == 'not_measured'
    assert settle.data['eval_seconds'] >= settle.data['execution_timing']['evaluator_return_seconds']
    # A fresh server re-derives the disposition and keeps it visible outside the pending-action list.
    client = TestClient(make_app(rd.parent))
    generation = run_generation_token(events)
    progress = client.get('/api/runs/demo/harness-progress', params={'expected_generation': generation}).json()
    assert progress['complete'] and progress['pending_checkpoint_count'] == 0
    brief = client.get('/api/runs/demo/harness-progress', params={
        'expected_generation': generation, 'brief': 'true'}).json()
    from looplab.harness.mcp_server import HarnessAPI
    assert brief['unreviewed_monitor_count'] == (0 if routine else 1)
    assert 'unreviewed_monitor_observations' not in brief
    assert HarnessAPI._valid_progress(brief)
    assert not HarnessAPI._valid_progress({**brief, 'unreviewed_monitor_count': True})
    assert progress['unreviewed_monitor_count'] == (0 if routine else 1)
    if routine:
        return  # the genuine rule answer is retained; completion did not invent it
    assert progress['unreviewed_monitor_observations'][0]['question'] == observation['question']
    assert progress['unreviewed_monitor_observations'][0]['execution_timing'] == settle.data['execution_timing']
    late = client.post('/api/runs/demo/harness-checkpoints', json={
        'expected_generation': generation, 'checkpoint_id': observation['question']['checkpoint_id'],
        'action_id': 'late', 'verdict': 'continue', 'reason': 'late review must not invent execution authority'})
    assert late.status_code == 409 and late.json()['detail']['code'] == 'checkpoint_execution_ended'
    assert len([e for e in engine.store.read_all() if e.type in ('node_evaluated', 'node_failed')]) == 1


@pytest.mark.parametrize('change', ['reset', 'reclaim'])
def test_completed_observation_cannot_close_new_attempt_or_stage_deadline_gates(tmp_path, change):
    rd, store, _client = seeded(tmp_path)
    live = ask(rd, 0, 0, 'train_monitor', observation='exact anomaly', kill_enabled=True)
    stage = ask(rd, 0, 0, 'stage_check', expectation='required output')
    deadline = ask(rd, 0, 0, 'deadline_grace')
    ids = [q['checkpoint_id'] for q in (live, stage, deadline)]
    if change == 'reset':
        store.append('node_reset', {'node_id': 0, 'generation': 0})
    else:
        store.append('eval_invocation_claimed', {'node_id': 0, 'generation': 0})
    assert close_completed_observations(rd, ids, 0, 0) == []
    rows, _history_ = _history(rd, store)
    assert len(rows) == 3 and all(r['type'] == 'question' for r in rows)
    assert answer_for(rd, live['checkpoint_id']) is None


def test_only_operational_monitor_question_closes_and_unavailable_evidence_is_not_approval(tmp_path):
    rd, store, _client = seeded(tmp_path)
    generation = run_generation_token(store.read_all())
    questions = [ask(rd, 0, 0, phase, observation='anomaly')
                 for phase in ('train_monitor', 'stage_check', 'deadline_grace')]
    ids = [q['checkpoint_id'] for q in questions]
    assert close_completed_observations(rd, ids, 0, 0) == ids[:1]
    assert close_completed_observations(rd, ids, 0, 0) == []
    assert {q['phase_id'] for q in pending(rd, generation)} == {'stage_check', 'deadline_grace'}
    assert answer_for(rd, ids[0]) is None
    with (rd / 'harness_checkpoints.jsonl').open('a') as stream:
        stream.write('{damaged\n')
    with pytest.raises(OSError):
        close_completed_observations(rd, ids, 0, 0)
    from looplab.engine.external_watch import settle_external_observations
    attempt = SimpleNamespace(_seen={}, _live_questions=ids, node_id=0, generation=0,
        node=SimpleNamespace(kind='artifact'), res=SimpleNamespace(exit_code=0, timed_out=False, metric=None))
    anyio.run(settle_external_observations, SimpleNamespace(run_dir=rd), attempt)
    assert attempt.advisory_review_status == 'evidence_unavailable'


@pytest.mark.parametrize('result', [None, SimpleNamespace(exit_code=None, timed_out=False, metric=None)])
def test_unknown_result_does_not_close_or_answer_an_operational_question(tmp_path, result):
    from looplab.engine.external_watch import settle_external_observations
    rd, store, _client = seeded(tmp_path)
    q = ask(rd, 0, 0, 'train_monitor', observation='owner/result not known')
    a = SimpleNamespace(_seen={}, _live_questions=[q['checkpoint_id']], node_id=0, generation=0,
                        node=SimpleNamespace(kind='artifact'), res=result)
    anyio.run(settle_external_observations, SimpleNamespace(run_dir=rd), a)
    rows, history = _history(rd, store)
    assert len(rows) == 1 and history[0]['status'] == 'pending'
    assert answer_for(rd, q['checkpoint_id']) is None


def test_real_routine_command_retains_rule_answer_instead_of_unknown_review(tmp_path, monkeypatch):
    test_real_completed_command_keeps_unknown_review_without_terminal_wait(
        tmp_path, monkeypatch, artifact=False, cadence=600., slow_publish=False, routine=True)


@pytest.mark.parametrize('phase', ['stage_check', 'deadline_grace'])
def test_illegal_advisory_closure_cannot_bypass_an_authority_gate(tmp_path, phase):
    rd, store, client = seeded(tmp_path)
    q = ask(rd, 0, 0, phase, observation='required explicit answer')
    invalid = {'type': 'closure', 'checkpoint_id': q['checkpoint_id'], 'answerer': 'engine',
               'disposition': 'ended_before_decision', 'review_status': 'unreviewed',
               'action_id': 'forged', 'reason': 'must not close a gate'}
    with (rd / 'harness_checkpoints.jsonl').open('a') as stream:
        stream.write(json.dumps(invalid) + '\n')
    reply = client.get('/api/runs/demo/harness-checkpoints', params={
        'expected_generation': run_generation_token(store.read_all())})
    assert reply.status_code == 503
    progress = client.get('/api/runs/demo/harness-progress', params={
        'expected_generation': run_generation_token(store.read_all())}).json()
    assert not progress['complete'] and not progress['source_health']['checkpoints']['read_complete']
