"""Retire an inert UI/config field without changing v1 behavior identity or historical bytes."""
import copy
import hashlib

import orjson
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import (Settings, RETIRED_SETTINGS, LEGACY_CONFIG_SNAPSHOT_DEFAULTS,
                                 settings_from_snapshot, unknown_snapshot_keys)
from looplab.search.speculation_calibration import (SPECULATION_CALIBRATION_PROFILE_SETTINGS,
    SPECULATION_CALIBRATION_PROFILE_DIGEST, speculation_runtime_scope_digest)

OLD_PROFILE_DIGEST='sha256:398f9b6eb20702f6dc2c4aa557abc7a9efb7d07fc1d2dceae7c951d37e9b541a'
CURRENT_PROFILE_DIGEST='sha256:e2acfc2f8a57cf35822dfa01e32dc1f639bbe707218f572010f2151f925cd472'
POST_RETIREMENT_FIELDS={'best_of_n_pilot':None,'cluster_executor':None}
RETIRED='cross_run_structured_claims'


@pytest.mark.parametrize('old_value',[False,True])
def test_legacy_snapshot_remains_readable_without_retired_field_or_rewriting_evidence(tmp_path,monkeypatch,old_value):
    monkeypatch.setenv('LOOPLAB_CROSS_RUN_STRUCTURED_CLAIMS','false')
    old={'backend':'toy','max_nodes':2,'config_snapshot_schema':3,RETIRED:old_value}
    path=tmp_path/'config.snapshot.json';path.write_text(json.dumps(old));before=path.read_bytes();copy_before=copy.deepcopy(old)
    settings=settings_from_snapshot(old,refuse_unknown=True)
    assert old==copy_before and path.read_bytes()==before
    assert RETIRED in RETIRED_SETTINGS and RETIRED not in LEGACY_CONFIG_SNAPSHOT_DEFAULTS
    assert unknown_snapshot_keys(old)==[]
    assert RETIRED not in Settings.model_fields
    assert not hasattr(settings,RETIRED) and RETIRED not in settings.masked_snapshot()
    assert not hasattr(Settings(**{RETIRED:old_value}),RETIRED)


def test_v1_profile_and_runtime_identity_preserve_only_the_reserved_noop_slot():
    # Retirement preserves the historical envelope; later real Settings additions revise it.
    assert SPECULATION_CALIBRATION_PROFILE_DIGEST==CURRENT_PROFILE_DIGEST
    assert {k:SPECULATION_CALIBRATION_PROFILE_SETTINGS[k] for k in POST_RETIREMENT_FIELDS}==POST_RETIREMENT_FIELDS
    historical={k:v for k,v in SPECULATION_CALIBRATION_PROFILE_SETTINGS.items() if k not in POST_RETIREMENT_FIELDS}
    assert len(historical)==296
    old_identity={'schema':'looplab.speculation-calibration-profile/v1',
                  'settings':{**historical,RETIRED:False}}
    assert 'sha256:'+hashlib.sha256(orjson.dumps(old_identity,option=orjson.OPT_SORT_KEYS)).hexdigest()==OLD_PROFILE_DIGEST
    historical_runtime={**historical,'max_nodes':2,'speculation_depth':1,'speculation_gate_receipt':None}
    assert speculation_runtime_scope_digest(historical_runtime)==speculation_runtime_scope_digest({**historical_runtime,RETIRED:False})=="sha256:6388061e07db62b9495a57c163239ff18ede2161e3b71d94e099ac6f5e0fd22e"
    assert RETIRED not in SPECULATION_CALIBRATION_PROFILE_SETTINGS
    current={**SPECULATION_CALIBRATION_PROFILE_SETTINGS,'max_nodes':2,'speculation_depth':1,'speculation_gate_receipt':None}
    legacy={**current,RETIRED:False};before=copy.deepcopy(legacy)
    # Current envelope includes both later null-default execution selectors.
    assert speculation_runtime_scope_digest(current)==speculation_runtime_scope_digest(legacy)=="sha256:09e3fdcc823cc9c43760e3e29fe43ca08e1fab8d5da446323f30c030f0bfc655"
    assert speculation_runtime_scope_digest(current)!=speculation_runtime_scope_digest(historical_runtime)
    for field in POST_RETIREMENT_FIELDS:
        changed={**current,field:{"explicit":"different"}}
        assert speculation_runtime_scope_digest(changed)!=speculation_runtime_scope_digest(current)
    assert legacy==before and RETIRED not in current
    assert speculation_runtime_scope_digest({**legacy,RETIRED:True})!=speculation_runtime_scope_digest(legacy)
    assert speculation_runtime_scope_digest({**legacy,'max_nodes':3})!=speculation_runtime_scope_digest(legacy)
    assert speculation_runtime_scope_digest({**legacy,'best_of_n':2})!=speculation_runtime_scope_digest(legacy)


@pytest.fixture
def harness():
    from tests import test_speculation_quality_gate
    return test_speculation_quality_gate


def test_complete_original_and_new_calibration_documents_pass_the_same_full_reader(harness,tmp_path):
    # Existing CPU evidence fixtures with synthetic GPU identity, not GPU measurements.
    for label,extra in [('original',{RETIRED:False}),('current',{})]:
        pairs=harness._pairs(tmp_path/label,baseline_kwargs={'extra_config':extra},extra_config=extra)
        paths=[run/'config.snapshot.json' for pair in pairs for run in pair]
        before={str(p):p.read_bytes() for p in paths}
        report=harness._gate(pairs)
        assert report['passed'],harness._pair_errors(report)
        assert all((RETIRED in json.loads(path.read_text())) == bool(extra) for path in paths)
        assert all(path.read_bytes()==before[str(path)] for path in paths)


def test_retirement_does_not_liberalize_mixed_document_pairs(harness,tmp_path):
    pairs=harness._pairs(tmp_path/'mixed',baseline_kwargs={'extra_config':{RETIRED:False}},extra_config={})
    report=harness._gate(pairs)
    assert not report['passed']
    assert any('pair configs differ' in message for message in harness._pair_errors(report))


@pytest.mark.parametrize('extra,expected',[
    ({RETIRED:True},RETIRED),({RETIRED:0},RETIRED),
    ({RETIRED:False,'a_future_semantic_setting':1},'a_future_semantic_setting'),
    ({RETIRED:False,'best_of_n':2},'best_of_n')])
def test_legacy_slot_does_not_bypass_active_value_or_unknown_field_fences(harness,tmp_path,extra,expected):
    report=harness._gate(harness._pairs(tmp_path/'runs',extra_config=extra))
    assert not report['passed']
    assert any(expected in message for message in harness._pair_errors(report)),harness._pair_errors(report)


def test_current_packaged_and_served_settings_catalogue_has_no_inert_switch(tmp_path,monkeypatch):
    from looplab.serve.server import make_app
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS','1');monkeypatch.delenv('LOOPLAB_UI_TOKEN',raising=False)
    with TestClient(make_app(tmp_path)) as client:
        response=client.get('/api/settings/schema/2')
        assert response.status_code==200
        keys=[field['key'] for group in response.json()['groups'] for field in group['fields']]
        assert RETIRED not in keys and len(keys)==264
    packaged=json.loads(Path('looplab/serve/settings_ui_schema.json').read_text())
    assert [field['key'] for group in packaged['groups'] for field in group['fields']]==keys
