import json

import pytest
from fastapi import HTTPException

from looplab.harness.map_paging import COLLECTIONS, paginate


def _data():
    metadata={'version':1,'run_id':'run','run_uid':'uid','generation':'a'*64,'event_seq':1,
              'source_health':{'events':{'read_complete':True}},'goal':'study'}
    rows={name:[{'id':f'{name}-{i}','text':'история🙂'*100} for i in range(33)] for name in COLLECTIONS}
    return metadata,rows


def test_whole_response_budget_and_exact_independent_continuations():
    metadata,rows=_data()
    seen={name:[] for name in COLLECTIONS};cursors={}
    for _ in range(300):
        page=paginate(metadata,rows,cursors=cursors,max_bytes=12000,max_items=13)
        actual=len(json.dumps(page,ensure_ascii=False,separators=(',',':')).encode())
        assert actual == page['response_budget']['bytes'] <= 12000
        assert sum(len(page[name]['items']) for name in COLLECTIONS) <= 13
        for name in COLLECTIONS:seen[name].extend(row['id'] for row in page[name]['items'])
        cursors={name:page[name]['next_cursor'] for name in COLLECTIONS}
        if not any(page[name]['has_more'] for name in COLLECTIONS):break
    assert seen == {name:[row['id'] for row in values] for name,values in rows.items()}


def test_changed_collection_refuses_its_cursor_but_unrelated_continuation_survives():
    metadata,rows=_data();first=paginate(metadata,rows,collections=['cards'],max_items=1)
    cursor={'cards':first['cards']['next_cursor']}
    rows['nodes'].append({'id':'new-node'})
    assert paginate(metadata,rows,collections=['cards'],cursors=cursor,max_items=1)['cards']['items'][0]['id']=='cards-1'
    rows['cards'][0]['text']='changed evidence'
    with pytest.raises(HTTPException) as caught:paginate(metadata,rows,collections=['cards'],cursors=cursor)
    assert caught.value.status_code == 409


def test_oversized_summary_is_explicit_and_row_never_disappears_past_a_cursor():
    metadata,rows=_data();metadata['goal']='x'*50000
    page=paginate(metadata,rows,collections=['goal','nodes'],max_bytes=4096)
    assert 'goal' not in page and 'goal' in page['response_budget']['omitted_sections']
    assert page['cards']['selected'] is False
    assert page['cards']['total'] == 33 and page['cards']['items'] == []
    assert page['nodes']['offset'] == 0
    if not page['nodes']['items']:
        assert page['nodes']['next_cursor'].startswith('map1:0:')
    assert page['response_budget']['bytes'] <= 4096


@pytest.mark.parametrize('kwargs',[{'cursors':{'cards':'bad'}},{'cursors':{'unknown':'map1:0:'+'a'*64}},
    {'collections':['unknown']},{'collections':[]},{'max_items':True},{'max_bytes':1000000}])
def test_invalid_options_are_refused(kwargs):
    with pytest.raises(HTTPException):paginate(*_data(),**kwargs)


def test_real_api_pages_all_reports_past_the_old_200_version_cutoff_with_a_wire_byte_bound(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.events.run_generation import run_generation_token
    from looplab.serve.server import make_app
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS','1')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN',raising=False)
    rd=tmp_path/'r';rd.mkdir();store=EventStore(rd/'events.jsonl')
    store.append('run_started',{'run_id':'r','run_uid':'u','task_id':'t','direction':'max'})
    seqs=[]
    for i in range(205):
        seqs.append(store.append('report_generated',{'at_node':0,'content':{
            'headline':f'Report{i}','body_markdown':'Measured scope '+str(i)}}).seq)
    gen=run_generation_token(store.read_all());before=store.path.read_bytes()
    seen=[];cursors=None
    with TestClient(make_app(tmp_path)) as client:
        for _ in range(100):
            params={'expected_generation':gen,'collections':['reports'],'max_bytes':4096,'max_items':7}
            if cursors:params['cursors']=json.dumps(cursors)
            response=client.get('/api/runs/r/research-map',params=params)
            assert response.status_code == 200,response.text
            page=response.json()
            assert len(response.content) == page['response_budget']['bytes'] <= 4096
            seen.extend(x['seq'] for x in page['reports']['items'])
            if not page['reports']['has_more']:break
            cursors={'reports':page['reports']['next_cursor']}
        assert seen == list(reversed(seqs))
        # Another collection has no impact on the reports cursor; changing reports refuses it.
        store.append('node_created',{'node_id':0,'operator':'draft','parent_ids':[],'idea':{'operator':'draft'}})
        assert client.get('/api/runs/r/research-map',params=params).status_code == 200
        store.append('report_generated',{'content':{'headline':'new report'}})
        assert client.get('/api/runs/r/research-map',params=params).status_code == 409
    assert store.path.read_bytes().startswith(before)


@pytest.mark.parametrize('field,value',[('run_id','other'),('run_uid','other'),('generation','b'*64)])
def test_collection_cursor_cannot_cross_run_identity(field,value):
    metadata,rows=_data();first=paginate(metadata,rows,max_items=1)
    metadata[field]=value
    with pytest.raises(HTTPException) as caught:
        paginate(metadata,rows,cursors={'uploads':first['uploads']['next_cursor']})
    assert caught.value.status_code == 409


def test_oversized_rows_are_explicitly_shortened_and_metadata_never_loses_source_health():
    from looplab.harness.research_map import _bounded
    original={'node_id':42,'activity':{str(i):'private-shape'*100 for i in range(1000)}}
    row=_bounded(original)
    assert len(json.dumps(row,ensure_ascii=False).encode()) <= 1200
    assert row['detail_truncated'] and row['item_omitted'] and row['node_id']==42
    metadata,rows=_data();rows={name:[] for name in COLLECTIONS};rows['nodes']=[row]
    page=paginate(metadata,rows,collections=['nodes'])
    assert page['response_budget']['shortened_items'] == 1
    assert page['source_health'] == metadata['source_health']
    metadata['source_health']['unbounded']='x'*100000
    with pytest.raises(HTTPException) as caught:paginate(metadata,rows,max_bytes=4096)
    assert caught.value.status_code == 503


def test_registered_mcp_and_local_cli_follow_exact_bounded_continuations(tmp_path,monkeypatch):
    import anyio
    import httpx
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner
    from looplab.cli import app
    from looplab.events.eventstore import EventStore
    from looplab.events.run_generation import run_generation_token
    from looplab.harness.mcp_server import HarnessAPI,build_server
    from looplab.serve.server import make_app
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS','1')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN',raising=False)
    rd=tmp_path/'r';rd.mkdir();store=EventStore(rd/'events.jsonl')
    store.append('run_started',{'run_id':'r','run_uid':'u','task_id':'t','direction':'max'})
    for i in range(6):store.append('report_generated',{'content':{'headline':f'Report{i}'}})
    gen=run_generation_token(store.read_all());before=store.path.read_bytes();seen=[]
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            seen.append(request)
            response=client.get(str(request.url))
            return httpx.Response(response.status_code,content=response.content,headers={'Content-Type':'application/json'})
        api=HarnessAPI('http://testserver',transport=httpx.MockTransport(bridge));server=build_server(api)
        tool=next(t for t in anyio.run(server.list_tools) if t.name=='research_map')
        assert {'collections','cursors','max_bytes','max_items'} <= set(tool.model_dump(by_alias=True)['inputSchema']['properties'])
        def call(arguments):
            result=anyio.run(server.call_tool,'research_map',arguments)
            envelope=result.model_dump(by_alias=True)
            assert not envelope.get('isError'),envelope
            return envelope.get('structuredContent') or json.loads(envelope['content'][0]['text'])
        args={'run_id':'r','expected_generation':gen,'collections':['reports'],'max_bytes':4096,'max_items':2}
        first=call(args)
        page=first.get('body',first)
        assert page['response_budget']['items_returned']==2
        cursor={'reports':page['reports']['next_cursor']}
        second=call({**args,'cursors':cursor});body=second.get('body',second)
        assert body['reports']['offset']==2
        assert json.loads(seen[-1].url.params['cursors'])==cursor
        assert seen[-1].url.params.get_list('collections')==['reports']
        assert all(r.method=='GET' for r in seen)
    cli=CliRunner().invoke(app,['research-map',str(rd),'--generation',gen,'--collection','reports',
        '--cursors',json.dumps(cursor),'--max-bytes','4096','--max-items','2'])
    assert cli.exit_code==0,cli.output
    local=json.loads(cli.stdout)
    assert local['reports']['offset']==2 and local['reports']['items']==body['reports']['items']
    assert len(cli.stdout.rstrip('\n').encode())==local['response_budget']['bytes']<=4096
    bad=CliRunner().invoke(app,['research-map',str(rd),'--generation',gen,'--cursors','not-json'])
    assert bad.exit_code!=0 and 'Invalid research-map cursors JSON' in bad.output
    assert store.path.read_bytes()==before


def test_composed_briefing_keeps_map_omission_explicit_instead_of_crashing(tmp_path,monkeypatch):
    from looplab.engine import resources
    from looplab.events.eventstore import EventStore
    from looplab.events.run_generation import run_generation_token
    from looplab.harness import briefing
    monkeypatch.setattr(resources,'default_gpu_host_lease_path',lambda:tmp_path/'lease')
    rd=tmp_path/'r';rd.mkdir();store=EventStore(rd/'events.jsonl')
    store.append('run_started',{'run_id':'r','run_uid':'u','task_id':'t','direction':'max','goal':'study'})
    from looplab.core.config import Settings
    (rd/'config.snapshot.json').write_text(Settings(backend='toy',external_harness=True).model_dump_json())
    gen=run_generation_token(store.read_all());original=briefing.map_snapshot
    def without_optional(*args,**kwargs):
        page=original(*args,**kwargs)
        for name in ('goal','quality','campaign_overview'):page.pop(name,None)
        page['response_budget']['omitted_sections']=['goal','quality','campaign_overview']
        return page
    monkeypatch.setattr(briefing,'map_snapshot',without_optional)
    before=store.path.read_bytes();result=briefing.snapshot(rd,gen,root=tmp_path)
    assert result['goal'] is None and result['research']['quality'] is None
    assert result['authoring_language']['source']=='goal_unavailable'
    assert result['research']['response_budget']['omitted_sections']==['goal','quality','campaign_overview']
    assert store.path.read_bytes()==before


def test_export_preserves_valid_map_omissions_and_source_budget_without_claiming_completeness(tmp_path,monkeypatch):
    import hashlib
    from looplab.events.eventstore import EventStore
    from looplab.events.run_generation import run_generation_token
    from looplab.harness import research_export
    rd=tmp_path/'r';rd.mkdir();store=EventStore(rd/'events.jsonl')
    store.append('run_started',{'run_id':'r','run_uid':'u','task_id':'t','direction':'max','goal':'original goal'})
    report=store.append('report_generated',{'content':{'headline':'Selected result','body_markdown':'Measured result'}})
    gen=run_generation_token(store.read_all());original=research_export.snapshot
    def valid_omitted_page(*args,**kwargs):
        # Use the real field-selection contract, including its exact byte receipt.
        return original(*args,**kwargs,collections=['reports'])
    monkeypatch.setattr(research_export,'snapshot',valid_omitted_page)
    before=store.path.read_bytes();result=research_export.export(rd,gen,[report.seq],root=tmp_path)
    assert 'Campaign goal is unavailable' in result['markdown'] and 'Omitted map summaries:' in result['markdown']
    assert 'Measured result' in result['markdown']
    assert {'goal','campaign_overview'} <= set(result['map_response_budget']['omitted_sections'])
    assert result['markdown_sha256']==hashlib.sha256(result['markdown'].encode()).hexdigest()
    assert result['observation']['same_event_prefix'] is True and result['observation']['atomic_across_sources'] is False
    assert store.path.read_bytes()==before
