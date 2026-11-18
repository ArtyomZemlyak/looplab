"""Independent boundary cases for evaluator identity, canary reuse and destructive retry."""
import hashlib
import os
from types import SimpleNamespace

import pytest

from looplab.core.evaluator_identity import canary_equivalence_digest, evaluator_digest_from_specs
from looplab.core.retry_inputs import preflight_resume_inputs
from looplab.engine.eval_canary import canary_already_passed


def digest(root, protect, name='.'):
    return evaluator_digest_from_specs(eval_spec={'command':['python','score.py']}, repo_spec={
        'editables':[{'name':name,'path':str(root),'protect':protect}],
        'protected_names':[(name+'/' if name!='.' else '')+p for p in protect]})


@pytest.mark.parametrize('protect', [['*.py'], ['scores/**'], ['scores']])
def test_protected_patterns_and_directories_bind_content(tmp_path, protect):
    p=tmp_path/'score.py' if protect==['*.py'] else tmp_path/'scores'/'score.py'
    p.parent.mkdir(exist_ok=True);p.write_text('old')
    old=digest(tmp_path,protect);p.write_text('new')
    assert old != digest(tmp_path,protect)


def test_namespaced_editable_binds_own_scorer(tmp_path):
    p=tmp_path/'score.py';p.write_text('old')
    old=digest(tmp_path,['score.py'],'app');p.write_text('new')
    assert old != digest(tmp_path,['score.py'],'app')


def test_large_scorer_cannot_hide_equal_size_equal_mtime_change(tmp_path):
    p=tmp_path/'score.py';p.write_bytes(b'a'*(16*1024*1024+1));st=p.stat()
    old=digest(tmp_path,['score.py'])
    with p.open('r+b') as f:f.write(b'b')
    os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns))
    current=digest(tmp_path,['score.py'])
    assert not current or old != current


def equivalence(pins):
    return canary_equivalence_digest(code='x',files={},deleted=[],activation_manifest_name='activation.json',
        evaluator_digest='a'*64,canary_spec={'timeout':2},node_kind='experiment',uses=[1],uses_pins=pins)


def test_canary_accepts_real_dependency_receipt_and_binds_code():
    a=equivalence({'1':{'generation':2,'code':'old'}})
    b=equivalence({'1':{'generation':2,'code':'new'}})
    assert a and b and a!=b


def test_same_lifecycle_canary_rechecks_contract():
    row=SimpleNamespace(type='eval_canary_finished',data={'node_id':1,'generation':0,
        'code_digest':'same','equivalence_digest':'old','passed':True})
    assert not canary_already_passed([row],1,0,'same',equivalence_digest='new')
    assert canary_already_passed([row],1,0,'same',equivalence_digest='old')


def test_retry_parent_directory_is_ephemeral(tmp_path):
    wd=tmp_path/'nodes'/'node_1';wd.mkdir(parents=True);(wd/'model').write_text('keep')
    assert preflight_resume_inputs([{'path':str(tmp_path/'nodes'),'kind':'directory'}],workdir=wd)


def test_retry_directory_symlink_chain_is_ephemeral(tmp_path):
    wd=tmp_path/'nodes'/'node_1';wd.mkdir(parents=True);(wd/'model').write_text('keep')
    first=tmp_path/'first';first.mkdir();second=tmp_path/'second';second.mkdir()
    (first/'link').symlink_to(second,target_is_directory=True)
    (second/'link').symlink_to(wd,target_is_directory=True)
    assert preflight_resume_inputs([{'path':str(first),'kind':'directory'}],workdir=wd)


def test_retry_scan_limit_fails_closed(tmp_path):
    wd=tmp_path/'nodes'/'node_1';wd.mkdir(parents=True)
    external=tmp_path/'external';external.mkdir()
    for i in range(520):(external/str(i)).write_text('safe')
    assert preflight_resume_inputs([{'path':str(external),'kind':'directory'}],workdir=wd)


def test_legacy_canary_row_remains_reusable():
    row=SimpleNamespace(type='eval_canary_finished',data={'node_id':1,'generation':0,
        'code_digest':'same','passed':True})
    assert canary_already_passed([row],1,0,'same',equivalence_digest='new')


def test_retry_rechecks_after_archiving_before_delete(tmp_path,monkeypatch):
    from looplab.engine.workspace import WorkspaceSeeder
    from looplab.core.retry_inputs import RetryInputPreflightError
    wd=tmp_path/'nodes'/'node_1';wd.mkdir(parents=True);(wd/'model').write_text('keep')
    external=tmp_path/'external';external.write_text('safe')
    node=SimpleNamespace(id=1,attempt=1,files={},deleted=[],resume_inputs=[{'path':str(external)}])
    def archive(*args):
        external.unlink();external.symlink_to(wd/'model')
    monkeypatch.setattr('looplab.engine.attempt_logs.archive_attempt_logs',archive)
    engine=SimpleNamespace(_repo_spec={},run_dir=tmp_path)
    with pytest.raises(RetryInputPreflightError):
        WorkspaceSeeder(engine).materialize(node,wd)
    assert (wd/'model').read_text()=='keep'


def test_protected_directory_symlink_never_yields_reusable_identity(tmp_path):
    real=tmp_path/'real';real.mkdir();(real/'score.py').write_text('old')
    protected=tmp_path/'protected';protected.mkdir();(protected/'link').symlink_to(real,target_is_directory=True)
    assert not digest(tmp_path,['protected/**'])
