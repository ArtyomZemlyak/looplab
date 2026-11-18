"""Explicit logical premises challenge explanations, never borrow or erase child measurements."""
import pytest
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app
from tests.factories import post_command, command_terminal


def _seed(tmp_path, monkeypatch):
    from looplab.core.config import Settings
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    rd = tmp_path / 'r'; rd.mkdir()
    (rd/'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    store = EventStore(rd/'events.jsonl')
    store.append('run_started', {'run_id':'r','run_uid':'uid','task_id':'t','direction':'max'})
    for key, statement, parent in [('card-1','Explicit measured premise',None), ('card-2','Independently measured child','card-1'), ('card-3','Filed sibling','card-1')]:
        store.append('card_added', {'id':key,'statement':statement,'source':'researcher',
                                  'parent_card_id':parent,'idea':{'operator':'draft'}})
    for nid,key,statement,metric in [(0,'card-1','Explicit measured premise',.5),(1,'card-2','Independently measured child',.8)]:
        store.append('node_created', {'node_id':nid,'operator':'draft','parent_ids':[],
                                     'idea':{'operator':'draft','card_id':key,'hypothesis':statement},'code':'print(1)'})
        store.append('node_evaluated', {'node_id':nid,'metric':metric})
    assert fold(store.read_all()).cards['card-1'].evidence == [0]
    assert fold(store.read_all()).cards['card-2'].evidence == [1]
    return rd,store,run_generation_token(store.read_all())


def _data(key, premises=(), assessment=None):
    return {'id':key,'premise_ids':list(premises),'assessment':assessment}


def _post(client,gen,data,key):
    result = post_command(client,'claim_reviewed',data,key,run_id='r',generation=gen)
    assert result.status_code in (200,202),result.text
    return command_terminal(client,result.json(),run_id='r')


def test_failed_or_tested_parent_and_filing_are_never_inferred_refutation(tmp_path,monkeypatch):
    rd,store,gen=_seed(tmp_path,monkeypatch)
    state=fold(store.read_all())
    assert state.cards['card-1'].verdict == 'tested'
    assert state.cards['card-2'].premise_review is None
    with TestClient(make_app(tmp_path)) as client:
        assert _post(client,gen,_data('card-2',['card-1']),'declare')['status']=='succeeded'
    state=fold(store.read_all())
    assert state.cards['card-2'].premise_review['undercut']==[]
    assert state.cards['card-3'].premise_review is None
    assert state.best_node_id==1 and state.nodes[1].metric==.8


def test_explicit_refutation_underCuts_only_logical_dependents_and_preserves_child_truth(tmp_path,monkeypatch):
    rd,store,gen=_seed(tmp_path,monkeypatch)
    before=fold(store.read_all());child=before.cards['card-2'];native_nodes=before.nodes
    assessment={'judgment':'refuted','reason':'Author judges the stated premise contradicted by its own measured case','node_ids':[0]}
    with TestClient(make_app(tmp_path)) as client:
        assert _post(client,gen,_data('card-2',['card-1']),'depends')['status']=='succeeded'
        receipt=_post(client,gen,_data('card-1',assessment=assessment),'assess')
        assert receipt['status']=='succeeded',receipt
        assert _post(client,gen,_data('card-1',assessment=assessment),'assess')['id']==receipt['id']
    state=fold(store.read_all());view=state.cards['card-2'].premise_review
    assert view['state']=='current' and len(view['undercut'])==1
    assert view['undercut'][0]['premise_id']=='card-1' and view['undercut'][0]['node_ids']==[0]
    assert view['authority']=='authored_interpretation' and view['verdict_changed'] is False
    assert state.cards['card-3'].premise_review is None
    assert state.cards['card-2'].verdict==child.verdict and state.cards['card-2'].evidence==child.evidence
    assert state.nodes==native_nodes and state.best_node_id==before.best_node_id
    assert sum(event.type=='claim_reviewed' for event in store.read_all())==2


@pytest.mark.parametrize('bad', [
    _data('missing'), _data('card-2',['missing']), _data('card-2',['card-2']),
    _data('card-2',['card-1','card-1']), _data('card-2',[True]),
    _data('card-1',assessment={'judgment':'refuted','reason':'own?','node_ids':[1]}),
    _data('card-1',assessment={'judgment':'refuted','reason':'bool?','node_ids':[False]}),
    _data('card-1',assessment={'judgment':'refuted','reason':'empty?','node_ids':[]}),
    _data('card-1',assessment={'judgment':'supported','reason':'unsupported','node_ids':[0]}),
    _data('card-1',assessment={'judgment':'refuted','reason':'x'*513,'node_ids':[0]}),
    {'id':'card-1','review':{'forged':'server stamp'}},
])
def test_invalid_or_borrowed_claim_refutation_is_rejected_without_native_change(tmp_path,monkeypatch,bad):
    rd,store,gen=_seed(tmp_path,monkeypatch);before=store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        receipt=_post(client,gen,bad,'bad-review')
        assert receipt['status']=='rejected',receipt
    assert store.path.read_bytes()==before


def test_explicit_dependency_cycle_is_refused_without_using_research_filing(tmp_path,monkeypatch):
    rd,store,gen=_seed(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        assert _post(client,gen,_data('card-2',['card-1']),'first')['status']=='succeeded'
        before=store.path.read_bytes()
        assert _post(client,gen,_data('card-1',['card-2']),'cycle')['status']=='rejected'
        assert store.path.read_bytes()==before


@pytest.mark.parametrize('change',['reset','retarget','withdraw','malformed'])
def test_stale_or_withdrawn_refutation_never_hides_unknown_behind_active_warning(tmp_path,monkeypatch,change):
    rd,store,gen=_seed(tmp_path,monkeypatch)
    assessment={'judgment':'refuted','reason':'Bound authored interpretation','node_ids':[0]}
    with TestClient(make_app(tmp_path)) as client:
        _post(client,gen,_data('card-2',['card-1']),'depends')
        _post(client,gen,_data('card-1',assessment=assessment),'refutes')
        assert fold(store.read_all()).cards['card-2'].premise_review['undercut']
        if change=='reset':store.append('node_reset',{'node_id':0,'generation':0,'from_stage':'eval'})
        elif change=='retarget':
            store.append('metric_retarget',{'key':'other','direction':'max','metrics':{'0':.9,'1':.1}})
        elif change=='withdraw':assert _post(client,gen,_data('card-1'),'withdraw')['status']=='succeeded'
        else:store.append('claim_reviewed',{'review':{'subject':{'id':'card-1'},'bad':True},'actor_kind':'local_operator'})
    state=fold(store.read_all());view=state.cards['card-2'].premise_review
    assert view['undercut']==[]
    expected='current' if change=='withdraw' else 'unavailable' if change=='malformed' else 'stale'
    assert view['premise_status'][0]['state']==expected
    assert view['premise_status'][0]['assessment'] is None


def test_original_legacy_shape_restart_cursor_API_and_researcher_keep_independent_verdict(tmp_path,monkeypatch):
    from looplab.agents.state_brief import board_prompt_lines
    from looplab.events.replay import FoldCursor
    from looplab.serve.public_cards import public_cards
    rd,store,gen=_seed(tmp_path,monkeypatch)
    initial=fold(store.read_all())
    assert 'claim_reviews' not in initial.model_dump()
    assert all('premise_review' not in row for row in initial.model_dump()['cards'].values())
    assert all('premise_review' not in row for row in public_cards(initial.cards).values())
    assessment={'judgment':'refuted','reason':'Own native receipt, author interpretation only','node_ids':[0]}
    with TestClient(make_app(tmp_path)) as client:
        _post(client,gen,_data('card-2',['card-1']),'depends')
        _post(client,gen,_data('card-1',assessment=assessment),'refuted')
    events=store.read_all();expected=fold(events)
    cursor=FoldCursor()
    for event in events:
        cursor.extend([event])
    assert cursor.snapshot().model_dump()==expected.model_dump()
    native=store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as restarted:
        response=restarted.get('/api/runs/r/state',params={'observe_only':'true'})
        assert response.status_code==200,response.text
        actual=response.json()['state']['cards']['card-2']
        assert actual['premise_review']['undercut'][0]['premise_id']=='card-1'
        assert actual['verdict']==initial.cards['card-2'].verdict and actual['evidence']==[1]
    assert store.path.read_bytes()==native
    prompt='\n'.join(board_prompt_lines(expected))
    assert 'PREMISE_UNDERCUT=' in prompt and 'child evidence/verdict unchanged' in prompt


def test_append_time_review_fence_checks_exact_source_attempt_and_prior_review(tmp_path,monkeypatch):
    from looplab.serve.control_validation import normalize_control,_precondition_claim_review
    from tests.test_card_operator_controls import _Srv
    rd,store,gen=_seed(tmp_path,monkeypatch);srv=_Srv(tmp_path)
    data=_data('card-1',assessment={'judgment':'refuted','reason':'Bound interpretation','node_ids':[0]})
    normalized=normalize_control(srv,rd,'claim_reviewed',data)
    assert _precondition_claim_review(fold(store.read_all()),'claim_reviewed',normalized,None) is None
    store.append('claim_reviewed',normalized)
    assert _precondition_claim_review(fold(store.read_all()),'claim_reviewed',normalized,None)['code']=='claim_review_changed'
    normalized=normalize_control(srv,rd,'claim_reviewed',data)
    store.append('node_reset',{'node_id':0,'generation':0,'from_stage':'eval'})
    assert _precondition_claim_review(fold(store.read_all()),'claim_reviewed',normalized,None)['code']=='claim_review_changed'


@pytest.mark.parametrize('bad',['surrogate','unknown_keys','bool_version','future_assessment','huge_generation'])
def test_imported_unsupported_latest_review_is_total_and_unknown_not_old_good(tmp_path,monkeypatch,bad):
    import copy
    rd,store,gen=_seed(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        _post(client,gen,_data('card-2',['card-1']),'depends')
        _post(client,gen,_data('card-1',assessment={'judgment':'refuted','reason':'Original','node_ids':[0]}),'refuted')
    original=next(e.data for e in reversed(store.read_all()) if e.type=='claim_reviewed')
    data=copy.deepcopy(original)
    if bad=='surrogate':data['review']['assessment']['reason']='\ud800'
    elif bad=='unknown_keys':data['review']['future_field']={}
    elif bad=='bool_version':data['review']['version']=True
    elif bad=='huge_generation':data['review']['assessment']['evidence'][0]['generation']=1 << 130
    else:data['review']['assessment']['judgment']='proven_by_model'
    # Use the Event model directly: JSON encoders correctly refuse a lone surrogate,
    # while the pure fold must also remain total on an imported in-memory Event.
    event=store.read_all()[-1].model_copy(update={'seq':store.read_all()[-1].seq+1,'data':data})
    state=fold([*store.read_all(),event]);view=state.cards['card-2'].premise_review
    assert view['undercut']==[] and view['premise_status'][0]['state']=='unavailable'


def test_genuine_CPU_native_outcomes_remain_independent_after_optional_review(tmp_path,monkeypatch):
    import anyio
    from tests.test_inline_repair import _engine
    from looplab.core.config import Settings
    from looplab.core.models import Idea
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS','1');monkeypatch.delenv('LOOPLAB_UI_TOKEN',raising=False)
    monkeypatch.setenv('LOOPLAB_AGENT_EVENTS','0')
    class NoPaidRole:
        def implement(self,*_):raise AssertionError('optional review triggered a paid role')
    engine=_engine(tmp_path/'r',NoPaidRole(),inline_repair=False,concurrent_research=False,timeout=10)
    engine._gpu_ids=[];engine._free_gpus=[];engine._gpu_physical_ids={};engine._gpu_host_lease_path=None
    engine.store.append('run_started',{'run_id':'r','run_uid':'real-cpu','task_id':'toy','direction':'min'})
    for nid,key,metric in [(0,'card-1',.1),(1,'card-2',.05)]:
        statement=f'Measured CPU claim {key}'
        engine.store.append('card_added',{'id':key,'statement':statement,'source':'researcher','idea':{'operator':'draft'}})
        engine.store.append('node_created',{'node_id':nid,'operator':'draft','parent_ids':[],
            'idea':Idea(operator='draft',card_id=key,hypothesis=statement,footprint={'gpus':0}).model_dump(mode='json'),
            'code':f'import json;print(json.dumps({{"metric":{metric}}}))\n','eval_start_boundary':True})
    async def drive():
        await engine._dispatch_evals([{'node_id':0},{'node_id':1}],fold(engine.store.read_all()),None,research=False)
    anyio.run(drive)
    state=fold(engine.store.read_all());assert state.total_eval_seconds>0
    assert [state.nodes[i].metric for i in [0,1]]==[.1,.05] and state.best_node_id==1
    assert len([e for e in engine.store.read_all() if e.type=='eval_invocation_claimed'])==2
    (tmp_path/'r/config.snapshot.json').write_text(Settings(backend='toy',external_harness=True).model_dump_json())
    gen=run_generation_token(engine.store.read_all())
    with TestClient(make_app(tmp_path)) as client:
        _post(client,gen,_data('card-2',['card-1']),'cpu-depends')
        result=_post(client,gen,_data('card-1',assessment={'judgment':'refuted',
            'reason':'Explicit interpretation of this native CPU case only','node_ids':[0]}),'cpu-review')
        assert result['status']=='succeeded',result
    reviewed=fold(engine.store.read_all())
    assert reviewed.cards['card-2'].premise_review['undercut']
    assert reviewed.nodes==state.nodes and reviewed.total_eval_seconds==state.total_eval_seconds
    assert reviewed.best_node_id==state.best_node_id and reviewed.cards['card-2'].verdict==state.cards['card-2'].verdict
    assert len([e for e in engine.store.read_all() if e.type=='eval_invocation_claimed'])==2


def test_actual_strict_tail_CAS_refolds_and_refuses_evidence_reset_race(tmp_path,monkeypatch):
    from tests.test_card_operator_controls import _Srv
    from looplab.serve.control_validation import normalize_control
    from looplab.serve.run_commands import RunCommandService
    from looplab.events.eventstore import EventStoreConcurrencyError
    rd,store,gen=_seed(tmp_path,monkeypatch);srv=_Srv(tmp_path)
    normalized=normalize_control(srv,rd,'claim_reviewed',_data('card-1',assessment={
        'judgment':'refuted','reason':'Must not land after evidence reset','node_ids':[0]}))
    append=EventStore.append;raced=[]
    def race(self,event_type,data,**kwargs):
        if self.path==store.path and event_type=='claim_reviewed' and kwargs.get('require_lock') and not raced:
            event=append(self,'node_reset',{'node_id':0,'generation':0,'from_stage':'eval'})
            raced.append(event)
            raise EventStoreConcurrencyError(self.path,int(kwargs['expected_last_seq']),event.seq)
        return append(self,event_type,data,**kwargs)
    monkeypatch.setattr(EventStore,'append',race)
    intent,baseline,error=RunCommandService(srv)._append_collaboration_intent(rd,
        {'event_type':'claim_reviewed','run_generation':gen},normalized)
    assert raced and intent is None and error['code']=='claim_review_changed'
    assert not any(event.type=='claim_reviewed' for event in store.read_all())


def test_imported_logical_cycle_is_unknown_never_mutual_refutation(tmp_path,monkeypatch):
    import copy
    from looplab.core.cards import hypothesis_statement_digest
    from looplab.core.jsonutil import canonical_json_digest
    rd,store,gen=_seed(tmp_path,monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        _post(client,gen,_data('card-2',['card-1']),'depends')
        _post(client,gen,_data('card-1',assessment={'judgment':'refuted','reason':'First case','node_ids':[0]}),'refutes')
    state=fold(store.read_all());data=copy.deepcopy(next(event.data for event in reversed(store.read_all()) if event.type=='claim_reviewed'))
    data['review']['premises']=[{'id':'card-2','seed_sha256':canonical_json_digest(state.cards['card-2'].seed_statement)}]
    store.append('claim_reviewed',data)
    state=fold(store.read_all())
    assert state.cards['card-1'].premise_review['state']=='unavailable'
    assert state.cards['card-2'].premise_review['state']=='unavailable'
    assert not state.cards['card-2'].premise_review['undercut']
    assert state.best_node_id==1 and state.nodes[1].metric==.8
