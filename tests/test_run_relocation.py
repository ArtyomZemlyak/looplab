from pathlib import Path
from types import SimpleNamespace

import pytest

from looplab.core.errors import ConfigRefusal
from looplab.core.models import Event
from looplab.engine.reentry import ReentryMixin, SpeculationAuthorizationError
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.maintenance.relocation import relocate_run


def _run(path: Path, *, uid='uid-one', token=None):
    path.mkdir(parents=True)
    store = EventStore(path / 'events.jsonl')
    started = {'run_id': path.name, 'run_uid': uid, 'task_id': 't', 'goal': 'g',
               'direction': 'max', 'card_driven_selection': True, 'speculation_depth': 1}
    if token:
        started.update(speculation_gate_receipt_digest=token, speculation_policy_scope='greedy')
    store.append('run_started', started)
    store.append('node_created', {'node_id': 0, 'operator': 'draft', 'parent_ids': [], 'idea': {'operator': 'draft'}})
    store.append('node_evaluated', {'node_id': 0, 'generation': 0, 'metric': 0.5})
    return store


def _reentry(path: Path, token: str):
    return SimpleNamespace(
        run_dir=path, speculation_depth=1, _speculation_depth_auto=False,
        _speculation_gate_admitted=True, _speculation_gate_calibration=False,
        _speculation_product_lane=True, _speculation_implementation_digest='',
        _speculation_runtime_scope_sha256='', _speculation_gate_receipt_digest=token,
        _speculation_product_authority_tokens=frozenset({token}), _speculation_policy_scope='greedy',
        _policy_name='greedy', _speculation_calibration_profile_digest='',
        _speculation_calibration_gpu_inventory=[], _speculation_calibration_seed=None,
    )


def test_relocate_records_identity_and_preserves_speculation_authority(tmp_path):
    source = tmp_path / 'old'; target = tmp_path / 'new'
    token = 'sha256:' + 'a' * 64
    store = _run(source, token=token)
    before = store.path.read_bytes()
    dry = relocate_run(source, target)
    assert dry['status'] == 'dry_run' and not target.exists() and store.path.read_bytes() == before
    result = relocate_run(source, target, apply=True, reason='Group the campaign')
    assert result['status'] == 'relocated' and not source.exists() and target.exists()
    state = fold(EventStore(target / 'events.jsonl').read_all())
    assert state.run_id == 'new' and state.run_uid == 'uid-one'
    assert state.relocations[0]['from_run_id'] == 'old'
    ReentryMixin._require_pinned_speculation_receipt(_reentry(target, token), state)
    misplaced = _reentry(tmp_path / 'elsewhere', token)
    misplaced.run_dir = tmp_path / 'elsewhere'
    with pytest.raises(SpeculationAuthorizationError, match='log was written for run id'):
        ReentryMixin._require_pinned_speculation_receipt(misplaced, state)


def test_retry_after_recorded_rename_does_not_append_twice(tmp_path, monkeypatch):
    source = tmp_path / 'old'; target = tmp_path / 'new'; store = _run(source)
    calls = []
    import os
    real_rename = os.rename
    def fail_once(src, dst):
        calls.append((src, dst))
        if len(calls) == 1:
            raise OSError('simulated rename uncertainty')
        return real_rename(src, dst)
    monkeypatch.setattr(os, 'rename', fail_once)
    with pytest.raises(ConfigRefusal, match='event was recorded but rename failed'):
        relocate_run(source, target, apply=True)
    assert source.exists() and not target.exists()
    result = relocate_run(source, target, apply=True)
    assert result['event_already_recorded'] is True
    events = EventStore(target / 'events.jsonl').read_all()
    assert [event.type for event in events].count('run_relocated') == 1


def test_relocation_refuses_legacy_uid_and_foreign_event(tmp_path):
    source = tmp_path / 'old'; _run(source, uid='')
    with pytest.raises(ConfigRefusal, match='requires the run_uid'):
        relocate_run(source, tmp_path / 'new', apply=True)
    assert not (tmp_path / 'new').exists()
    events = [Event(seq=0, ts=0.0, type='run_started', data={'run_id': 'old', 'run_uid': 'u'}),
              Event(seq=1, ts=1.0, type='run_relocated', data={
                  'from_run_id': 'old', 'to_run_id': 'new', 'run_uid': 'another',
                  'previous_event_seq': 0, 'previous_event_sha256': 'x', 'reason': 'copy'})]
    assert fold(events).run_id == 'old'


def test_cli_relocate_performs_the_actual_dry_run_and_rename(tmp_path):
    import json
    from typer.testing import CliRunner
    from looplab.cli import app
    source = tmp_path / 'old'; target = tmp_path / 'new'; _run(source)
    runner = CliRunner()
    dry = runner.invoke(app, ['relocate', str(source), str(target)])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)['status'] == 'dry_run' and source.exists() and not target.exists()
    result = runner.invoke(app, ['relocate', str(source), str(target), '--apply'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['status'] == 'relocated'
    assert fold(EventStore(target / 'events.jsonl').read_all()).run_id == 'new'
