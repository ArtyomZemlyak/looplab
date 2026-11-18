"""Pinned external snapshots and actual confined smoke are independent from promotion authority."""
import copy
import subprocess
from pathlib import Path

import pytest

from looplab.engine.external_skill_source import collect_git_source,normalize_source,exclusion_policy,validate_external_skill_source


def _source(tmp_path):
    repo=tmp_path/'external';repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    (repo/'algorithm.py').write_text('def normalize(xs):\n    return [x/sum(xs) for x in xs]\n')
    subprocess.run(['git','-C',str(repo),'add','algorithm.py'],check=True)
    subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','-c','user.name=Fixture','-c','user.email=f@local.invalid','-C',str(repo),'commit','-qm','source'],check=True)
    revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    return repo,revision,collect_git_source(repo,revision,['algorithm.py'],label='Independent algorithm',url='https://example.invalid/library')


def test_pinned_snapshot_ignores_dirty_tree_and_refuses_mutable_revision(tmp_path):
    repo,revision,source=_source(tmp_path)
    (repo/'algorithm.py').write_text('raise RuntimeError("dirty version")\n')
    assert collect_git_source(repo,revision,['algorithm.py'],label='Independent algorithm',url='https://example.invalid/library')==source
    with pytest.raises(ValueError,match='exact_git_commit'):
        collect_git_source(repo,'HEAD',['algorithm.py'],label='external')


@pytest.mark.parametrize('path',['../algorithm.py','/etc/passwd','.git/config','.env','credentials.json','algorithm.py/../other'])
def test_private_and_nonliteral_source_paths_refused(tmp_path,path):
    repo,revision,source=_source(tmp_path)
    with pytest.raises(ValueError):collect_git_source(repo,revision,[path],label='external')


def test_source_integrity_and_mechanical_task_exclusion_before_execution(tmp_path,monkeypatch):
    repo,revision,source=_source(tmp_path)
    broken=copy.deepcopy(source);broken['files'][0]['content']='different'
    with pytest.raises(ValueError,match='digest_mismatch'):normalize_source(broken)
    policy=exclusion_policy('heldout-competition',['example.invalid'],review_reason='Exclude the benchmark source')
    with pytest.raises(ValueError,match='task_exclusion'):
        validate_external_skill_source(source,'print(1)',policy)


def test_real_confined_source_import_assertion_and_failure(tmp_path):
    repo,revision,source=_source(tmp_path)
    policy=exclusion_policy('heldout-competition',[],review_reason='Only independent algorithm source selected')
    code='from algorithm import normalize\nassert normalize([1,3]) == [0.25,0.75]\nprint("smoke complete")\n'
    result=validate_external_skill_source(source,code,policy,protected_roots=[str(repo)])
    assert result['passed'],result
    assert result['exit_code']==0 and result['elapsed_seconds']>0
    failed=validate_external_skill_source(source,'assert False, "real failure"',policy,protected_roots=[str(repo)])
    assert not failed['passed'] and failed['exit_code']!=0
    assert (repo/'algorithm.py').read_text()==source['files'][0]['content']


def _request(tmp_path):
    from test_external_knowledge_recovery import _session
    rd,memory,store,client,lesson,skill=_session(tmp_path)
    repo,revision,source=_source(tmp_path)
    payload={**skill,'action_id':'external-one','external_source':source,
        'body':'Normalize bounded vectors; require a nonzero sum and validate arithmetic on a tiny fixture.',
        'validation_code':'from algorithm import normalize\nassert normalize([1,3]) == [.25,.75]\n',
        'excluded_terms':['heldout-competition'],
        'exclusion_review':'Selected only general normalization code, excluding the target benchmark and its solutions.'}
    return rd,memory,store,client,payload


def test_native_publication_real_smoke_receipt_restart_and_hidden_candidate(tmp_path,monkeypatch):
    import hashlib,json
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    from looplab.tools.skills import SkillLibrary,parse_skill_frontmatter
    import looplab.harness.skills as impl
    rd,memory,store,client,payload=_request(tmp_path)
    result=client.post('/api/runs/demo/skill-candidates',json=payload)
    assert result.status_code==200,result.text
    skill=result.json()['skill']; assert skill['status']=='candidate'
    assert not SkillLibrary(memory/'skills').skills
    assert len(SkillLibrary(memory/'skills',include_auto_candidates=True).skills)==1
    artifact=memory/skill['external_source']['artifact_ref']
    raw=artifact.read_bytes(); assert hashlib.sha256(raw).hexdigest()==skill['external_source']['artifact_sha256']
    saved=json.loads(raw); assert saved['validation']['passed'] and saved['validation']['exit_code']==0
    assert saved['request']['source']==payload['external_source']
    assert json.loads(parse_skill_frontmatter(next((memory/'skills').glob('*.md')).read_text())['external_source_sha256'])==[payload['external_source']['snapshot_sha256']]
    before=artifact.read_bytes()
    def forbidden(*a,**kw):raise AssertionError('an original ACK must not re-execute')
    monkeypatch.setattr(impl,'validate_external_skill_source',forbidden)
    store.append('node_reset',{'node_id':0})
    (memory/'lessons.jsonl').unlink()
    replay=TestClient(make_app(tmp_path/'runs')).post('/api/runs/demo/skill-candidates',json=payload)
    assert replay.status_code==200 and replay.json()['replayed'],replay.text
    assert replay.json()['skill']==skill and artifact.read_bytes()==before
    assert client.post('/api/runs/demo/skill-candidates',json={**payload,'body':'changed technique'}).status_code==409
    assert client.post('/api/runs/demo/skill-candidates',json={**payload,'action_id':'fresh'}).status_code==409


@pytest.mark.parametrize('change',['source','exclude','body','code','partial','unknown'])
def test_invalid_or_contaminated_source_refused_before_execution(tmp_path,monkeypatch,change):
    import looplab.harness.skills as impl
    rd,memory,store,client,payload=_request(tmp_path)
    if change=='source':payload['external_source']['files'][0]['content']='tampered'
    if change=='exclude':payload['excluded_terms']=['normalize']
    if change=='body':payload['body']='Technique copied from heldout-competition'
    if change=='code':payload['validation_code']='print("heldout-competition")'
    if change=='partial':payload.pop('exclusion_review')
    if change=='unknown':payload['external_source']['unknown_version']=2
    def forbidden(*a,**kw):raise AssertionError('rejected request reached execution')
    monkeypatch.setattr(impl,'validate_external_skill_source',forbidden)
    result=client.post('/api/runs/demo/skill-candidates',json=payload)
    assert result.status_code==422,result.text
    assert not (memory/'external_skill_sources').exists()


@pytest.mark.parametrize('change',['reset','lesson','smoke-failure'])
def test_real_smoke_refuses_changed_evidence_and_failure_without_skill_write(tmp_path,monkeypatch,change):
    import json
    import looplab.harness.skills as impl
    rd,memory,store,client,payload=_request(tmp_path)
    real=impl.validate_external_skill_source
    before=next((memory/'skills').glob('*.md')).read_bytes()
    def changing(*a,**kw):
        checked=real(*a,**kw)
        if change=='reset':store.append('node_reset',{'node_id':0})
        elif change=='lesson':
            rows=[json.loads(line) for line in (memory/'lessons.jsonl').read_text().splitlines()]
            rows[0]['outcome']='refuted'
            (memory/'lessons.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
        return checked
    monkeypatch.setattr(impl,'validate_external_skill_source',changing)
    if change=='smoke-failure':payload['validation_code']='assert False, "genuine failure"'
    result=client.post('/api/runs/demo/skill-candidates',json=payload)
    assert result.status_code==(422 if change=='smoke-failure' else 409),result.text
    assert next((memory/'skills').glob('*.md')).read_bytes()==before
    assert not (memory/'external_skill_sources').exists()


def test_import_does_not_promote_but_native_cross_task_confirmation_keeps_provenance(tmp_path):
    import json
    from looplab.engine.memory import write_auto_skill
    from looplab.tools.skills import SkillLibrary,parse_skill_frontmatter
    rd,memory,store,client,payload=_request(tmp_path)
    for action in ['external-one','external-two']:
        result=client.post('/api/runs/demo/skill-candidates',json={**payload,'action_id':action})
        assert result.status_code==200 and result.json()['skill']['status']=='candidate',result.text
    # A DIFFERENT native task publishes its own measured lesson through the same
    # public workflow. Merely importing another source above did not confirm it.
    from looplab.events.eventstore import EventStore
    from looplab.events.run_generation import run_generation_token
    second=tmp_path/'runs'/'other';second.mkdir()
    for name in ['config.snapshot.json','task.snapshot.json']:
        (second/name).write_bytes((rd/name).read_bytes())
    other=EventStore(second/'events.jsonl')
    other.append('run_started',{'run_id':'other','run_uid':'two','task_id':'other-domain',
        'goal':'Profile cache latency memory filesystem compiler networking parser serialization buffer allocation',
        'direction':'min'})
    other.append('node_created',{'node_id':0,'operator':'draft','idea':{'operator':'draft','params':{'cache':2}},'code':'different'})
    other.append('node_evaluated',{'node_id':0,'metric':.12})
    generation=run_generation_token(other.read_all())
    lesson=json.loads((memory/'lessons.jsonl').read_text().splitlines()[0])
    reply=client.post('/api/runs/other/lessons',json={'expected_generation':generation,'action_id':'other-lesson',
        'statement':lesson['statement'],'outcome':'supported','evidence':[0]})
    assert reply.status_code==200,reply.text
    assert reply.json()['lesson']['fingerprint']!=lesson['fingerprint']
    result=client.post('/api/runs/other/skill-candidates',json={**payload,'expected_generation':generation,
        'action_id':'other-source','lesson_action_id':'other-lesson'})
    assert result.status_code==200 and result.json()['skill']['status']=='promoted',result.text
    path=next((memory/'skills').glob('*.md'));metadata=parse_skill_frontmatter(path.read_text())
    assert SkillLibrary(memory/'skills').skills
    assert set(json.loads(metadata['confirmed_tasks']))=={'task','other-domain'}
    assert json.loads(metadata['external_source_sha256'])==[payload['external_source']['snapshot_sha256']]


def test_cli_pinned_preview_is_explicitly_unvalidated_and_offline(tmp_path,monkeypatch):
    import json
    from typer.testing import CliRunner
    from looplab.cli import app
    repo,revision,source=_source(tmp_path)
    body=tmp_path/'body.md';body.write_text('Normalize a bounded vector, with applicability and zero-sum limits.')
    smoke=tmp_path/'smoke.py';smoke.write_text('from algorithm import normalize\nassert normalize([1,3]) == [.25,.75]\n')
    args=['skill-distill','demo','--repo',str(repo),'--revision',revision,'--source-file','algorithm.py',
        '--label','Independent algorithm','--generation','a'*64,'--action-id','external-one',
        '--lesson-action-id','lesson-1','--body-file',str(body),'--smoke-file',str(smoke),
        '--exclusion-review','Reviewed independent source; benchmark solutions excluded.']
    result=CliRunner().invoke(app,args)
    assert result.exit_code==0,result.output
    preview=json.loads(result.output); assert preview['validated'] is False and preview['published'] is False
    assert preview['body']['external_source']['origin']['revision']==revision
    assert CliRunner().invoke(app,args+['--publish']).exit_code!=0


@pytest.mark.parametrize('policy',[None,{},[],{'task_id':'x','forbidden_terms':[],'review_reason':'review','authority':'semantic_proof'}])
def test_unsupported_exclusion_metadata_is_not_executed(tmp_path,policy):
    _,_,source=_source(tmp_path)
    with pytest.raises(ValueError,match='exclusion_policy'):
        validate_external_skill_source(source,'print(1)',policy)


def test_all32_declared_terms_and_native_task_are_retained(tmp_path):
    _,_,source=_source(tmp_path)
    policy=exclusion_policy('native-benchmark',[f'excluded-source-{i}' for i in range(32)],review_reason='Reviewed full exclusion list')
    assert len(policy['forbidden_terms'])==33
    assert validate_external_skill_source(source,'assert 2+2==4',policy)['passed']


def test_failed_program_cannot_forge_success_with_stdout(tmp_path):
    _,_,source=_source(tmp_path)
    policy=exclusion_policy('native-benchmark',[],review_reason='Reviewed source')
    result=validate_external_skill_source(source,'print("exit=0")\nraise AssertionError("failure")',policy)
    assert not result['passed'] and result['exit_code']!=0


def test_source_native_receipt_change_after_smoke_is_exact_with_same_primary_metric(tmp_path,monkeypatch):
    import looplab.harness.skills as impl
    rd,memory,store,client,payload=_request(tmp_path)
    real=impl.validate_external_skill_source
    def changing(*a,**kw):
        result=real(*a,**kw)
        store.append('extra_metrics_imported',{'node_id':0,'generation':0,'source':'independent-receipt',
            'imported_at':1.0,'extra_metrics':{'secondary':.250001}})
        return result
    monkeypatch.setattr(impl,'validate_external_skill_source',changing)
    result=client.post('/api/runs/demo/skill-candidates',json=payload)
    assert result.status_code==409 and result.json()['detail']['code']=='external_skill_evidence_changed',result.text
    assert not (memory/'external_skill_sources').exists()


def test_CPU_smoke_holds_neither_memory_writer_lock(tmp_path,monkeypatch):
    import fcntl
    import looplab.harness.skills as impl
    rd,memory,store,client,payload=_request(tmp_path)
    real=impl.validate_external_skill_source
    def unlocked(*a,**kw):
        for name in ['skill_candidate_actions.jsonl.lock','lessons.jsonl.lock']:
            with (memory/name).open('a') as stream:
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                fcntl.flock(stream,fcntl.LOCK_UN)
        return real(*a,**kw)
    monkeypatch.setattr(impl,'validate_external_skill_source',unlocked)
    result=client.post('/api/runs/demo/skill-candidates',json=payload)
    assert result.status_code==200,result.text


def test_registered_MCP_api_uses_the_same_native_smoke_and_stable_receipt(tmp_path):
    import anyio,httpx,json
    from looplab.harness.mcp_server import HarnessAPI,build_server
    rd,memory,store,client,payload=_request(tmp_path)
    def bridge(request):
        response=client.request(request.method,str(request.url),content=request.content,headers={'Content-Type':'application/json'})
        return httpx.Response(response.status_code,content=response.content,headers={'Content-Type':'application/json'})
    api=HarnessAPI('http://testserver',transport=httpx.MockTransport(bridge));server=build_server(api)
    results=[]
    for _ in range(2):
        result=anyio.run(server.call_tool,'api_request',{'method':'POST','path':'/api/runs/demo/skill-candidates','body':payload}).model_dump(by_alias=True)
        reply=result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert reply['status']==200,reply
        results.append(reply['body'])
    api.client.close()
    assert results[0]['skill']==results[1]['skill'] and results[1]['replayed']


def test_plain_native_refresh_retains_external_origin_without_extra_execution(tmp_path,monkeypatch):
    import json
    import looplab.harness.skills as impl
    from looplab.tools.skills import parse_skill_frontmatter
    rd,memory,store,client,payload=_request(tmp_path)
    first=client.post('/api/runs/demo/skill-candidates',json=payload)
    assert first.status_code==200,first.text
    def forbidden(*a,**kw):raise AssertionError('plain endogenous update must not execute source')
    monkeypatch.setattr(impl,'validate_external_skill_source',forbidden)
    plain={key:payload[key] for key in ['expected_generation','lesson_action_id','body']}
    reply=client.post('/api/runs/demo/skill-candidates',json={**plain,'action_id':'plain-refresh'})
    assert reply.status_code==200 and reply.json()['skill']['status']=='candidate',reply.text
    metadata=parse_skill_frontmatter(next((memory/'skills').glob('*.md')).read_text())
    assert json.loads(metadata['external_source_sha256'])==[payload['external_source']['snapshot_sha256']]


def test_imported_SKILL_markdown_is_only_an_artifact_not_manual_library_input(tmp_path):
    from looplab.core.jsonutil import canonical_json_digest
    from looplab.tools.skills import SkillLibrary
    import hashlib
    rd,memory,store,client,payload=_request(tmp_path)
    text='---\nname: unreviewed-manual\nstatus: promoted\n---\nExternal source text, not an admitted skill.\n'
    source=payload['external_source']
    source['files'].append({'path':'SKILL.md','sha256':hashlib.sha256(text.encode()).hexdigest(),'content':text})
    source['files'].sort(key=lambda row:row['path'])
    source['snapshot_sha256']=canonical_json_digest({key:value for key,value in source.items() if key!='snapshot_sha256'})
    reply=client.post('/api/runs/demo/skill-candidates',json=payload)
    assert reply.status_code==200,reply.text
    assert not list((memory/'skills').rglob('SKILL.md'))
    assert not SkillLibrary(memory/'skills').skills


def test_cli_publish_runs_same_server_validation_and_never_internal_model(tmp_path,monkeypatch):
    import httpx,json
    from typer.testing import CliRunner
    from looplab.cli import app
    import looplab.harness.mcp_server as mcp
    rd,memory,store,client,payload=_request(tmp_path)
    real=mcp.HarnessAPI
    def bridge(request):
        response=client.request(request.method,str(request.url),content=request.content,headers={'Content-Type':'application/json'})
        return httpx.Response(response.status_code,content=response.content,headers={'Content-Type':'application/json'})
    monkeypatch.setattr(mcp,'HarnessAPI',lambda url,token:real(url,token,transport=httpx.MockTransport(bridge)))
    body=tmp_path/'body.md';body.write_text(payload['body'])
    smoke=tmp_path/'smoke.py';smoke.write_text(payload['validation_code'])
    args=['skill-distill','demo','--repo',str(tmp_path/'external'),'--revision',payload['external_source']['origin']['revision'],
        '--source-file','algorithm.py','--label','Independent algorithm','--generation',payload['expected_generation'],
        '--action-id','cli-published','--lesson-action-id','lesson-1','--body-file',str(body),'--smoke-file',str(smoke),
        '--exclusion-review','Only independent source, excluding benchmark solutions.',
        '--publish','--url','http://testserver']
    result=CliRunner().invoke(app,args)
    assert result.exit_code==0,result.output
    receipt=json.loads(result.output)
    assert receipt['status']==200 and receipt['body']['skill']['external_source']['passed']
    assert len(list((memory/'external_skill_sources').glob('*.json')))==1


@pytest.mark.parametrize('ack',[None,{}, {'ok':True}, {'ok':True,'skill':{'external_source':{'passed':True}}}])
def test_CLI_never_calls_incomplete_HTTP200_a_publication(tmp_path,monkeypatch,ack):
    from types import SimpleNamespace
    from typer.testing import CliRunner
    from looplab.cli import app
    import looplab.harness.mcp_server as mcp
    repo,revision,source=_source(tmp_path)
    body=tmp_path/'body.md';body.write_text('Normalize bounded nonzero vectors; validate arithmetic.')
    smoke=tmp_path/'smoke.py';smoke.write_text('assert 2+2==4')
    fake=SimpleNamespace(request=lambda *a,**kw:{'status':200,'body':ack},client=SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(mcp,'HarnessAPI',lambda *a,**kw:fake)
    args=['skill-distill','demo','--repo',str(repo),'--revision',revision,'--source-file','algorithm.py',
        '--label','Independent source','--generation','a'*64,'--action-id','source-import','--lesson-action-id','lesson',
        '--body-file',str(body),'--smoke-file',str(smoke),'--exclusion-review','Reviewed source',
        '--publish','--url','http://testserver']
    assert CliRunner().invoke(app,args).exit_code==2


@pytest.mark.parametrize('operation',['read-memory','write-run','GPU-visibility'])
def test_source_smoke_preserves_native_stores_and_CPU_boundary(tmp_path,operation):
    rd,memory,store,client,payload=_request(tmp_path)
    secret=memory/'fixture-private.txt';secret.write_text('a private fixture, not a credential')
    native_before=(rd/'events.jsonl').read_bytes()
    if operation=='read-memory':payload['validation_code']=f'print(open({str(secret)!r}).read())'
    elif operation=='write-run':payload['validation_code']=f'open({str(rd/"source-mutation.txt")!r}, "w").write("changed")'
    else:payload['validation_code']='import os\nassert os.environ["CUDA_VISIBLE_DEVICES"] == ""\nfrom algorithm import normalize\nassert normalize([1,3]) == [.25,.75]'
    result=client.post('/api/runs/demo/skill-candidates',json=payload)
    assert result.status_code==(200 if operation=='GPU-visibility' else 422),result.text
    assert secret.read_text()=='a private fixture, not a credential'
    assert not (rd/'source-mutation.txt').exists()
    assert (rd/'events.jsonl').read_bytes()==native_before


def test_real_smoke_timeout_is_not_a_passing_receipt(tmp_path):
    _,_,source=_source(tmp_path)
    result=validate_external_skill_source(source,'while True: pass',
        exclusion_policy('native-benchmark',[],review_reason='Independent selected code'),timeout_s=1)
    assert not result['passed'] and result['timed_out'] is True
