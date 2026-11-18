"""Source-bound HTTP/MCP receipts: no provider call, training or billing attestation."""
import copy
import json
import subprocess
import sys

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from looplab.core.external_telemetry import REVISION_FIELDS, normalize_authored, provider_observation, revision_id
from looplab.events.replay import fold
from looplab.harness.mcp_server import HarnessAPI, build_server
from looplab.harness.telemetry import project
from looplab.serve.server import make_app
from tests.factories import post_command, command_terminal
from tests.test_external_work_state import _run


def _revision(**changes):
    return {**{key: 'a'*64 if key.endswith('_sha256') else None for key in REVISION_FIELDS},
            'provider':'fixture-provider','model':'fixture-model','cli_version':'fixture-cli-v1','language':'en',**changes}


def _receipt(**changes):
    return {'receipt_id':'segment-1','node_id':0,'attempt':0,'revision':_revision(),
        'usage':{'source':'agent_summary','source_id':'agent-segment-1','coverage':'delta',
                 'input_tokens':None,'output_tokens':None,'total_tokens':None,'cost':None,'currency':None},
        'wall_seconds':None,**changes}


def _post(client,gen,body,key='telemetry'):
    response=post_command(client,'agent_checkpointed',{'external_telemetry':body},key,run_id='demo',generation=gen)
    assert response.status_code in (200,202),response.text
    return command_terminal(client,response.json(),run_id='demo')


def _page(client,gen,**params):
    return client.get('/api/runs/demo/external-telemetry',params={'expected_generation':gen,**params})


def _provider(**changes):
    class Response:
        id='response-1';model='fixture-model'
        usage={'prompt_tokens':11,'completion_tokens':7,'total_tokens':18,'cost':0.01,'currency':'USD'}
        @property
        def output(self):raise AssertionError('output/reasoning must never be visited')
        @property
        def choices(self):raise AssertionError('choices must never be visited')
    return provider_observation(Response(),revision=_revision(),receipt_id='provider-segment',node_id=0,attempt=0,**changes)


def test_real_saved_mcp_command_to_http_preserves_revision_and_separate_ledgers_after_restart(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    store.append('llm_usage',{'usage_id':'native-call','tokens':7,'prompt_tokens':4,'completion_tokens':3,
                             'cost':0.03,'priced':True,'calls':1})
    native=copy.deepcopy(fold(store.read_all()).llm_cost)
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            response=client.request(request.method,str(request.url),content=request.content,
                headers={'Idempotency-Key':request.headers.get('Idempotency-Key','')})
            return httpx.Response(response.status_code,content=response.content,headers={'Content-Type':'application/json'})
        api=HarnessAPI('http://testserver',request_dir=tmp_path/'private-client',transport=httpx.MockTransport(bridge))
        # The real SDK helper extracts only usage metadata before the saved native command.
        receipt=_provider(wall_seconds=4.5)
        mcp=build_server(api)
        async def call():
            result=await mcp.call_tool('api_request',{'method':'POST','path':'/api/runs/demo/commands',
                'body':{'type':'agent_checkpointed','data':{'external_telemetry':receipt},'expected_generation':gen},
                'idempotency_key':'provider-original'})
            return result
        result=anyio.run(call)
        text=str(result);assert 'not_sent' not in text,text
        # Resolve the original receipt through the same typed command-receipt API.
        response=api.command_receipt('demo',gen,idempotency_key='provider-original')
        assert response.get('ok') is not False,response
        before=store.path.read_bytes()
        assert _post(client,gen,receipt,key='provider-original')['status']=='succeeded'
        assert store.path.read_bytes()==before
        assert _post(client,gen,_receipt(),key='declared')['status']=='succeeded'
        body=_page(client,gen).json()
        assert body['record_count']==2 and body['combined_total'] is None
        provider,declared=body['llm']
        assert provider['tokens']['total_tokens']['known_sum']==18
        assert provider['cost_by_currency'][0]['known_sum']==0.01
        assert declared['tokens']['total_tokens']=={'known_sum':None,'known_count':0,'unknown_count':1,'complete':False}
        assert body['native']['llm']['totals']==native
        assert fold(store.read_all()).llm_cost==native
        assert body['native']['gpu_hours']['value'] is None
        assert body['items'][0]['observation_level']=='agent_declared'
        assert body['items'][1]['provider_attestation']=='not_independently_attested'
        assert body['items'][1]['outcome']['metric']==0.5
    with TestClient(make_app(tmp_path)) as restarted:
        assert _page(restarted,gen).json()==body
    code="from pathlib import Path;import json;from looplab.events.eventstore import EventStore;from looplab.events.replay import fold;from looplab.harness.telemetry import project;ev=EventStore(Path(__import__('sys').argv[1])).read_all();print(json.dumps(project(ev,fold(ev),__import__('sys').argv[2],include_items=True),sort_keys=True))"
    cold=json.loads(subprocess.check_output([sys.executable,'-c',code,str(store.path),gen],text=True))
    assert cold==body


def test_prompt_only_revision_change_and_attempt_reset_never_relabel_old_outcome_or_model(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        assert _post(client,gen,_provider())['status']=='succeeded'
        store.append('node_reset',{'node_id':0,'generation':0,'from_stage':'eval'})
        store.append('node_evaluated',{'node_id':0,'generation':1,'metric':0.6})
        value=_receipt(receipt_id='new-revision',attempt=1,revision=_revision(prompt_sha256='b'*64))
        assert _post(client,gen,value,'prompt-only')['status']=='succeeded'
        rows=_page(client,gen).json()['items']
        assert rows[0]['revision_id']!=rows[1]['revision_id']
        assert rows[0]['revision']['model']==rows[1]['revision']['model']=='fixture-model'
        assert rows[0]['outcome']['metric']==0.6
        assert rows[1]['outcome']['state']=='historical_attempt' and rows[1]['outcome']['metric'] is None
        exported=client.get('/api/runs/demo/research-export',params={'expected_generation':gen}).json()
        assert exported['external_telemetry']['items']==rows
        assert all(row['revision_id'] in exported['markdown'] for row in rows)
        assert 'not independently attested' in exported['markdown']
        brief=client.get('/api/runs/demo/harness-progress',params={'expected_generation':gen,'brief':True}).json()
        assert brief['external_telemetry']['usable_record_count']==2


def test_new_command_key_and_receipt_id_cannot_count_same_provider_request_twice_and_conflicts_are_unavailable(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        value=_provider();assert _post(client,gen,value)['status']=='succeeded'
        alias={**value,'receipt_id':'alias'}
        assert _post(client,gen,alias,'alias-key')['status']=='succeeded'
        body=_page(client,gen).json();assert body['record_count']==1
        assert body['llm'][0]['cost_by_currency'][0]['known_sum']==0.01
        changed={**alias,'usage':{**alias['usage'],'cost':0.04}}
        assert _post(client,gen,changed,'conflict')['status']=='succeeded'
        body=_page(client,gen).json()
        assert body['state']=='unavailable' and body['usable_record_count']==0
        assert body['llm'][0]['tokens']['total_tokens']['known_sum'] is None
        assert body['source_records_complete'] is False


@pytest.mark.parametrize('change',[{'node_id':True},{'attempt':True},{'attempt':10},
    {'revision':_revision(prompt_sha256=True)}, {'revision':{**_revision(),'prompt':'must not persist'}},
    {'reasoning':'must not persist'}, {'usage':{'source':'agent_summary'}},
    {'usage':{**_receipt()['usage'],'total_tokens':True}},
    {'usage':{**_receipt()['usage'],'coverage':'cumulative'}},
    {'usage':{**_receipt()['usage'],'cost':float('inf'),'currency':'USD'}},
    {'wall_seconds':-1}])
def test_bad_or_sensitive_receipts_are_refused_without_publication(tmp_path,monkeypatch,change):
    rd,store,gen=_run(tmp_path,monkeypatch);before=store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        # JSON itself refuses infinity; test the semantic normalizer separately.
        if change.get('usage',{}).get('cost')==float('inf'):
            with pytest.raises(ValueError):normalize_authored(_receipt(**change))
        else:assert _post(client,gen,_receipt(**change))['status']=='rejected'
    assert store.path.read_bytes()==before


def test_delegated_cli_agent_cannot_add_independent_spend_and_raw_internal_checkpoints_stay_unrecorded(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    from looplab.core.config import Settings
    (rd/'config.snapshot.json').write_text(Settings(backend='toy',external_harness=False).model_dump_json())
    with TestClient(make_app(tmp_path)) as client:
        before=store.path.read_bytes();assert _post(client,gen,_receipt())['status']=='rejected'
        assert store.path.read_bytes()==before
        assert _page(client,gen).json()['state']=='not_recorded'


def test_receipt_pagination_covers_every_original_and_refuses_changed_prefix(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        for i in range(23):
            value=_receipt(receipt_id=f'segment-{i}',usage={**_receipt()['usage'],'source_id':f'delta-{i}'})
            assert _post(client,gen,value,f'key-{i}')['status']=='succeeded'
        first=_page(client,gen).json();second=_page(client,gen,offset=first['next_offset'],expected_prefix=first['prefix_digest']).json()
        assert len(first['items'])==20 and len(second['items'])==3 and not second['has_more']
        assert len({row['receipt_id'] for row in first['items']+second['items']})==23
        assert len(json.dumps(first).encode())<128*1024
        extra=_receipt(receipt_id='extra',usage={**_receipt()['usage'],'source_id':'extra'})
        assert _post(client,gen,extra,'extra')['status']=='succeeded'
        assert _page(client,gen,offset=20,expected_prefix=first['prefix_digest']).status_code==409


def test_legacy_missing_usage_and_malformed_latest_are_explicit_unknowns(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    store.append('agent_checkpointed',{'label':'developer','turn':1,'plan':'p','plan_updates':1,'todos':[]})
    state=fold(store.read_all());assert project(store.read_all(),state,gen)['state']=='not_recorded'
    store.append('agent_checkpointed',{'external_telemetry':{'version':True,'secret':'must not echo'}})
    result=project(store.read_all(),fold(store.read_all()),gen,include_items=True)
    assert result['state']=='unavailable' and result['malformed_count']==1
    assert result['items']==[] and 'must not echo' not in str(result)
    assert result['llm'][0]['tokens']['total_tokens']['known_sum'] is None



def test_provider_missing_counts_and_explicit_zero_are_not_filled_with_estimates():
    response={'id':'provider-zero','model':'fixture-model','usage':{'input_tokens':0},
              'output':{'text':'must not persist'},'_hidden_params':{'response_cost':99}}
    value=provider_observation(response,revision=_revision(),receipt_id='zero',node_id=0,attempt=0)
    assert value['usage']['input_tokens']==0
    assert value['usage']['output_tokens'] is None and value['usage']['total_tokens'] is None
    assert value['usage']['cost'] is None and value['usage']['currency'] is None
    assert 'must not persist' not in str(value) and '99' not in str(value)
    with pytest.raises(ValueError,match='model_differs'):
        provider_observation({**response,'model':'other-model'},revision=_revision(),receipt_id='zero',node_id=0,attempt=0)
    with pytest.raises(ValueError):normalize_authored(_receipt(wall_seconds=10**10000))


def test_cross_request_receipt_alias_conflict_excludes_every_alias_and_native_extras_never_escape(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        first=_provider();assert _post(client,gen,first,'first')['status']=='succeeded'
        alias={**first,'receipt_id':'alias'};assert _post(client,gen,alias,'alias')['status']=='succeeded'
        other={**alias,'usage':{**alias['usage'],'source_id':'second-provider-request'}}
        assert _post(client,gen,other,'second')['status']=='succeeded'
        result=_page(client,gen).json()
        assert result['state']=='unavailable' and result['usable_record_count']==0
    state=fold(store.read_all());state.llm_cost={'cost':1,'secret':'never echo','tokens':2}
    result=project(store.read_all(),state,gen)
    assert result['native']['llm']['totals']=={'cost':1,'tokens':2}
    assert 'never echo' not in str(result)


def test_summary_currency_bounds_name_omissions_and_corrupt_identity_cannot_contribute(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        for i in range(19):
            currency='A'+chr(65+i)+'A'
            body=_receipt(receipt_id=f'currency-{i}',usage={**_receipt()['usage'],
                'source_id':f'currency-{i}','cost':0,'currency':currency})
            assert _post(client,gen,body,f'currency-{i}')['status']=='succeeded'
        result=_page(client,gen).json()
        assert len(result['llm'][1]['cost_by_currency'])==16
        assert result['llm'][1]['omitted_currency_count']==3
        assert all(row['known_sum']==0 for row in result['llm'][1]['cost_by_currency'])
        assert len(json.dumps({k:v for k,v in result.items() if k!='items'}).encode())<8*1024
    record=next(e.data['external_telemetry'] for e in store.read_all() if 'external_telemetry' in e.data)
    store.append('agent_checkpointed',{'external_telemetry':{**record,'run_uid':'wrong'}})
    result=project(store.read_all(),fold(store.read_all()),gen)
    assert result['state']=='unavailable' and result['malformed_count']==1
    assert result['usable_record_count']==19



def test_cursor_fences_changed_receipt_content_even_when_ids_and_revision_stay_equal(tmp_path,monkeypatch):
    rd,store,gen=_run(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        assert _post(client,gen,_provider())['status']=='succeeded'
        prefix=_page(client,gen).json()['prefix_digest']
        # A rewritten storage prefix is not an ordinary supported command; the read must still
        # refuse its old continuation, rather than silently combining original and changed usage.
        events=store.read_all()
        for event in events:
            if 'external_telemetry' in event.data:
                event.data['external_telemetry']['usage']['cost']=0.07
        store.path.write_text(''.join(event.model_dump_json()+'\n' for event in events))
        assert _page(client,gen,offset=1,expected_prefix=prefix).status_code==409
        refreshed=_page(client,gen).json()
        assert refreshed['prefix_digest']!=prefix
        assert refreshed['llm'][0]['cost_by_currency'][0]['known_sum']==0.07
