"""Authored recovery through native checkpoints and saved MCP commands, never model execution."""
import json

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.mcp_server import HarnessAPI, build_server
from looplab.serve.server import make_app
from tests.factories import post_command, command_terminal


def _run(tmp_path, monkeypatch, name='demo'):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1');monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    rd=tmp_path/name;rd.mkdir(parents=True)
    (rd/'config.snapshot.json').write_text(Settings(backend='toy',external_harness=True).model_dump_json())
    store=EventStore(rd/'events.jsonl')
    store.append('run_started',{'run_id':name,'run_uid':'uid','task_id':'t','direction':'max'})
    store.append('node_created',{'node_id':0,'operator':'draft','parent_ids':[],'idea':{'operator':'draft'}})
    store.append('node_evaluated',{'node_id':0,'metric':0.5})
    return rd,store,run_generation_token(store.read_all())


def _work(**changes):
    return {'goal':'Compare the prepared method with the saved control','last_completed':'Synthetic node0 evaluated',
        'next_step':'Finish the preserved CPU preparation, after fresh checks','harness_revision':'test-harness-v1',
        'blocker':{'owner':'external_executor','summary':'Prepared artifact needs current identity check'},
        'unfinished_artifacts':[{'path':'/tmp/prepared/source.py','sha256':'a'*64,'status':'prepared'}],
        'work_claims':['CPU preparation remains assigned to the external executor'],**changes}


def _command(client,gen,work,key='checkpoint',run='demo'):
    response=post_command(client,'agent_checkpointed',{'external_work_state':work},key,run_id=run,generation=gen)
    assert response.status_code in (200,202),response.text
    return command_terminal(client,response.json(),run_id=run)


def test_registered_mcp_checkpoint_recovers_original_receipt_after_process_loss_without_duplicate_work(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch);request_dir=tmp_path/'private-client';seen=[]
    with TestClient(make_app(tmp_path)) as client:
        original=command_terminal(client,post_command(client,'hint',{'text':'Current operator scope'},'old-command',generation=gen).json())
        body={'type':'agent_checkpointed','data':{'external_work_state':_work(saved_command_id=original['id'])},'expected_generation':gen}
        def bridge(request):
            seen.append(request)
            response=client.request(request.method,str(request.url),content=request.content,headers={'Idempotency-Key':request.headers.get('Idempotency-Key','')})
            return httpx.Response(response.status_code,content=response.content,headers={'Content-Type':'application/json'})
        api=HarnessAPI('http://testserver',request_dir=request_dir,transport=httpx.MockTransport(bridge))
        server=build_server(api)
        result=anyio.run(server.call_tool,'api_request',{'method':'POST','path':'/api/runs/demo/commands','body':body,'idempotency_key':'checkpoint'})
        envelope=result.model_dump(by_alias=True);assert not envelope.get('isError'),envelope
        receipt=(envelope.get('structuredContent') or json.loads(envelope['content'][0]['text']))['body']
        assert receipt['status']=='succeeded'
        api.client.close()
    before=store.path.read_bytes();nodes=fold(store.read_all()).nodes
    with TestClient(make_app(tmp_path)) as restarted:
        api=HarnessAPI('http://testserver',request_dir=request_dir,transport=httpx.MockTransport(lambda _:pytest.fail('local saved-command recovery made HTTP')))
        saved=api.saved_commands('demo',gen)
        assert any(row['event_type']=='agent_checkpointed' for row in saved['items'])
        url='/api/runs/demo/harness-progress';params={'expected_generation':gen,'brief':True}
        recovery=restarted.get(url,params=params).json()['external_work_state']
        assert recovery['state']=='context_unchanged'
        assert recovery['checkpoint']['unfinished_artifacts']==_work()['unfinished_artifacts']
        assert recovery['authored_next_step']==_work()['next_step']
        assert recovery['authority']=='none' and recovery['artifact_identity']=='not_verified'
        assert recovery['agent_liveness']=='not_measured' and recovery['verification_required_before_execution']
        assert recovery['next_read']['method']=='GET' and '/command-receipt?' in recovery['next_read']['path']
        observed=restarted.get(recovery['next_read']['path']).json()
        assert observed['command']['status']=='succeeded'
        # Exact retry reconstructs the same native receipt; no second checkpoint or fit is added.
        again=_command(restarted,gen,body['data']['external_work_state'])
        assert again['id']==receipt['id']
    # A separate cold Python process reconstructs the same notes from the native journal alone.
    import os,subprocess,sys
    checkout=str(__import__('pathlib').Path(__file__).resolve().parents[1])
    env={**os.environ,'PYTHONPATH':checkout+os.pathsep+os.environ.get('PYTHONPATH','')}
    script=('import json,sys; from pathlib import Path; from looplab.events.eventstore import EventStore; '
            'from looplab.events.replay import fold; from looplab.harness.work_state import project; '
            'events=EventStore(Path(sys.argv[1])/"events.jsonl").read_all(); '
            'print(json.dumps(project(events,fold(events),sys.argv[2],"demo")))')
    cold=subprocess.run([sys.executable,'-c',script,str(rd),gen],cwd=tmp_path,env=env,capture_output=True,text=True,check=True)
    rebuilt=json.loads(cold.stdout)
    assert rebuilt['checkpoint']==recovery['checkpoint'] and rebuilt['next_read']==recovery['next_read']
    assert store.path.read_bytes()==before
    assert fold(store.read_all()).nodes==nodes
    assert sum(e.type=='agent_checkpointed' for e in store.read_all())==1


@pytest.mark.parametrize('kind', ['direction','attempt','budget'])
def test_changed_context_withholds_old_authored_action_but_preserves_unfinished_references(tmp_path,monkeypatch,kind):
    rd,store,gen=_run(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        assert _command(client,gen,_work())['status']=='succeeded'
        if kind=='direction':store.append('hint',{'text':'New priority','replace':True})
        elif kind=='attempt':store.append('node_reset',{'node_id':0,'generation':0,'from_stage':'eval'})
        else:store.append('budget_extend',{'add_nodes':1})
        body=client.get('/api/runs/demo/harness-progress',params={'expected_generation':gen,'brief':True}).json()['external_work_state']
        assert body['state']=='context_changed' and body['authored_next_step'] is None
        assert body['checkpoint']['goal']==_work()['goal']
        assert body['checkpoint']['blocker']['owner']=='external_executor'
        assert body['next_read']['method']=='GET' and '/harness-progress?' in body['next_read']['path']


@pytest.mark.parametrize('change',[{'reasoning':'not accepted'},{'credentials':'not accepted'},{'goal':'x'*1025},
    {'unfinished_artifacts':[{'path':'x','sha256':True}]},{'work_claims':['x']*9},
    {'saved_command_id':'../other'},{'blocker':{'summary':'missing owner'}},{'context_digest':'a'*64}])
def test_invalid_authored_state_is_refused_without_event_or_model_work(tmp_path,monkeypatch,change):
    rd,store,gen=_run(tmp_path,monkeypatch);before=store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        result=_command(client,gen,_work(**change))
        assert result['status']=='rejected'
    assert store.path.read_bytes()==before


def test_malformed_newest_record_does_not_fall_back_to_older_healthy_context_and_legacy_is_uninvented(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    store.append('agent_checkpointed',{'label':'legacy','plan':'legacy plan','todos':[],'turn':1,'plan_updates':1})
    with TestClient(make_app(tmp_path)) as client:
        url='/api/runs/demo/harness-progress';params={'expected_generation':gen,'brief':True}
        assert client.get(url,params=params).json()['external_work_state']['state']=='not_authored'
        assert _command(client,gen,_work())['status']=='succeeded'
        store.append('agent_checkpointed',{'external_work_state':{'version':True,'secret':'must not echo'}})
        bad=client.get(url,params=params).json()['external_work_state']
        assert bad['state']=='unavailable' and bad['checkpoint'] is None and 'must not echo' not in str(bad)


def test_nested_run_uses_exact_canonical_address_for_recovery_reads(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch,'project/nested')
    from looplab.core.run_ref import run_address
    address=run_address(tmp_path,rd)
    with TestClient(make_app(tmp_path)) as client:
        result=_command(client,gen,_work(),run=address)
        assert result['status']=='succeeded'
        body=client.get('/api/runs/'+address+'/harness-progress',params={'expected_generation':gen,'brief':True}).json()['external_work_state']
        assert body['next_read']['path'].startswith('/api/runs/'+address+'/')
        assert client.get(body['next_read']['path']).status_code==200


def test_authored_payload_has_a_whole_utf8_bound_and_does_not_open_referenced_artifacts(tmp_path,monkeypatch):
    from looplab.core.external_work_state import normalize_authored,MAX_BYTES
    value=_work(goal='🙂'*1024,last_completed='🙂'*1024,next_step='🙂'*1024)
    with pytest.raises(ValueError,match='exceeds_budget'):normalize_authored(value)
    normalized=normalize_authored(_work())
    assert len(json.dumps(normalized,ensure_ascii=False,separators=(',',':')).encode())<=MAX_BYTES
    rd,store,gen=_run(tmp_path,monkeypatch)
    # References are declarations; the native checkpoint neither reads nor hashes private paths.
    with TestClient(make_app(tmp_path)) as client:
        record=_command(client,gen,_work(unfinished_artifacts=[{'path':'/does-not-exist','sha256':None}]))
        assert record['status']=='succeeded'
        result=client.get('/api/runs/demo/harness-progress',params={'expected_generation':gen,'brief':True}).json()['external_work_state']
        assert result['artifact_identity']=='not_verified'
        assert result['checkpoint']['unfinished_artifacts'][0]['sha256'] is None


def test_checkpoint_phase_envelope_cannot_be_forged_by_the_command_author(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch);before=store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        response=post_command(client,'agent_checkpointed',{'external_work_state':_work(),'label':'internal-paid-role'},'forged',generation=gen)
        result=command_terminal(client,response.json())
        assert result['status']=='rejected'
    assert store.path.read_bytes()==before
