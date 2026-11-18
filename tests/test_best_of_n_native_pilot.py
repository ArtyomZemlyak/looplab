"""The runtime, not a build callback, executes all pilots inside one owned native method."""
import json
import os
from pathlib import Path

import pytest

from looplab.core.models import Idea
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.best_of_n_pilot import pilot_rank

pytestmark=pytest.mark.skipif(os.name!='posix' or not hasattr(os,'WNOWAIT'),
    reason='This optional native script method requires POSIX waitid WNOWAIT')

PILOT={'env':{'TASK_FIDELITY':'smoke'},'timeout_seconds':1.0,'fidelity_review':'Exploratory CPU fixture; no production fidelity claim'}


def _program(metric,full=None,extra=''):
    return ('import os,json\n'+extra+'\nprint(json.dumps({"metric": '+str(metric)+' if os.environ.get("TASK_FIDELITY")=="smoke" else '+str(full if full is not None else metric)+'}))\n')


def _receipt(stdout):
    return next(json.loads(line)['best_of_n_pilot'] for line in reversed(stdout.splitlines()) if line.startswith('{"best_of_n_pilot":'))


def test_actual_native_sandbox_ranks_then_runs_full_not_pilot_metric(tmp_path):
    code=pilot_rank([_program(.7,.4),_program(.8,.3)],direction='max',pilot=PILOT,assets={})
    assert not list(tmp_path.iterdir()),'rendering must execute nothing'
    result=SubprocessSandbox().run(code,str(tmp_path),timeout=10)
    assert result.exit_code==0 and result.metric==.3,result.stderr
    receipt=_receipt(result.stdout)
    assert receipt['chosen']==1 and [x['metric'] for x in receipt['pilots']]==[.7,.8]
    assert receipt['full']['code_sha256']==receipt['pilots'][1]['code_sha256']
    assert receipt['full']['output_ref']!=receipt['pilots'][1]['output_ref']
    assert all(x['seconds']>0 for x in receipt['pilots'])


def test_failed_and_timed_out_pilots_never_win_even_after_printed_metric(tmp_path):
    bad='print("{\\"metric\\": 999}")\nraise RuntimeError("failed")'
    hang='print("{\\"metric\\": 999}",flush=True)\nwhile True: pass'
    result=SubprocessSandbox().run(pilot_rank([bad,hang,_program(.2,.1)],direction='max',pilot=PILOT,assets={}),str(tmp_path),timeout=10)
    assert result.exit_code==0 and result.metric==.1,result.stderr
    receipt=_receipt(result.stdout)
    assert receipt['chosen']==2
    assert receipt['pilots'][0]['metric'] is None and receipt['pilots'][1]['timed_out']


def test_all_failed_pilots_have_no_usable_quality_and_no_full_fallback(tmp_path):
    result=SubprocessSandbox().run(pilot_rank(['raise ValueError(1)','raise ValueError(2)'],direction='max',pilot=PILOT,assets={}),str(tmp_path),timeout=10)
    assert result.exit_code!=0 and result.metric is None
    receipt=_receipt(result.stdout)
    assert receipt['chosen'] is None and receipt['full'] is None


def test_each_pilot_and_full_get_fresh_immutable_asset_and_original_env(tmp_path):
    (tmp_path/'input.txt').write_text('original')
    code='from pathlib import Path\nassert Path("input.txt").read_text()=="original"\nPath("input.txt").write_text("changed")\n'
    code+='assert not Path("leak.txt").exists()\nPath("leak.txt").write_text("own")\n'
    result=SubprocessSandbox().run(pilot_rank([_program(.1,.3,code),_program(.2,.4,code)],direction='max',pilot=PILOT,assets={'input.txt':'original'}),str(tmp_path),timeout=10)
    assert result.exit_code==0 and result.metric==.4,result.stderr
    assert (tmp_path/'input.txt').read_text()=='original'
    assert not (tmp_path/'leak.txt').exists()
    receipt=_receipt(result.stdout)
    assert len({row['output_ref'] for row in [*receipt['pilots'],receipt['full']]})==3


def test_changed_declared_input_refuses_before_any_candidate(tmp_path):
    (tmp_path/'input.txt').write_text('drift')
    result=SubprocessSandbox().run(pilot_rank([_program(.1),_program(.2)],direction='max',pilot=PILOT,assets={'input.txt':'original'}),str(tmp_path),timeout=10)
    assert result.exit_code!=0 and result.metric is None and 'asset changed' in result.stderr


class _Developer:
    answers_with_code=True
    is_code_generating=True
    def __init__(self,codes):
        self.codes=codes;self.calls=0;self.last_footprint={'gpus':0}
    def implement(self,idea):
        code=self.codes[self.calls%len(self.codes)];self.calls+=1
        self.last_footprint={'gpus':0}
        return code


def test_BestOfN_wrapper_defers_execution_and_restores_no_fake_winner_audit(tmp_path):
    from looplab.search.best_of_n import BestOfNDeveloper
    inner=_Developer([_program(.2,.4),_program(.1,.3)])
    inner.last_report={'candidate':'last generated, not runtime winner'}
    wrapped=BestOfNDeveloper(inner,n=2,pilot=PILOT,pilot_assets={},direction='min')
    code=wrapped.implement(Idea(operator='draft',footprint={'gpus':0}))
    assert inner.calls==2 and wrapped.last_report is None and not list(tmp_path.iterdir())
    assert wrapped.audit_extra()['pilot_selection']=='deferred_to_native_admitted_program'
    result=SubprocessSandbox().run(code,str(tmp_path),timeout=10)
    assert result.exit_code==0 and result.metric==.3,result.stderr


def test_normal_native_engine_owns_admission_single_terminal_and_all_pilot_cost(tmp_path):
    import anyio
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.events.replay import fold
    from looplab.search.policy import GreedyTree
    from looplab.search.best_of_n import BestOfNDeveloper
    from tests.test_inline_repair import TASK,_Stub
    inner=_Developer([_program(.2,.4),_program(.1,.3)])
    wrapped=BestOfNDeveloper(inner,n=2,pilot=PILOT,pilot_assets={},direction='min')
    seen=[]
    class Observed(SubprocessSandbox):
        def run(self,*args,**kwargs):
            current=fold(engine.store.read_all())
            assert current.nodes[0].eval_started and inner.calls==2
            assert args[0]==current.nodes[0].code
            result=super().run(*args,**kwargs);seen.append(result)
            return result
    engine=Engine(tmp_path,task=ToyTask.load(TASK),researcher=_Stub(),developer=wrapped,
        sandbox=Observed(),policy=GreedyTree(n_seeds=1,max_nodes=1),timeout=10,auto_install_deps=False,
        inline_repair=False,concurrent_research=False)
    state=anyio.run(engine.run)
    assert state.finished and len(state.nodes)==len(seen)==1 and state.nodes[0].metric==.3
    events=engine.store.read_all()
    assert len([e for e in events if e.type=='eval_invocation_claimed'])==1
    assert len([e for e in events if e.type in ('node_evaluated','node_failed')])==1
    receipt=_receipt(seen[0].stdout)
    measured_seconds=sum(row['seconds'] for row in receipt['pilots'])+receipt['full']['seconds']
    assert state.total_eval_seconds>=measured_seconds>0
    assert [e.type for e in events].index('node_created')<[e.type for e in events].index('node_eval_started')


@pytest.mark.parametrize('mode',['external','one','host-grader','commands','side-files'])
def test_unsupported_native_pilot_is_refused_before_candidate_execution(tmp_path,mode):
    from types import SimpleNamespace
    from looplab.agents.developer_backends import best_of_n_developer
    from looplab.core.config import Settings
    from looplab.core.errors import ConfigRefusal
    task=SimpleNamespace(direction='min',goal='CPU fixture')
    settings=Settings(backend="llm",best_of_n=2,best_of_n_pilot=PILOT)
    inner=_Developer([_program(.2),_program(.1)])
    if mode=='external':settings.external_harness=True
    if mode=='one':settings.best_of_n=1
    if mode=='host-grader':task.host_grader=lambda:{'labels':[1]}
    if mode=='commands':task.eval_spec=lambda:{'command':'scorer'}
    if mode=='side-files':inner.last_files={'helper.py':'print(1)'}
    with pytest.raises(ConfigRefusal):
        wrapper=best_of_n_developer(task,settings,inner,param_search=False)
        wrapper.implement(Idea(operator='draft'))
    assert not list(tmp_path.iterdir())
    if mode!='side-files':assert inner.calls==0


def test_factory_wires_declared_native_pilot_without_foresight_call(tmp_path):
    from types import SimpleNamespace
    from looplab.agents.developer_backends import best_of_n_developer
    from looplab.core.config import Settings
    inner=_Developer([_program(.2,.4),_program(.1,.3)])
    wrapper=best_of_n_developer(SimpleNamespace(direction='min',goal='fixture'),
        Settings(backend="llm",best_of_n=2,best_of_n_pilot=PILOT),inner,param_search=False)
    result=SubprocessSandbox().run(wrapper.implement(Idea(operator='draft')),str(tmp_path),timeout=10)
    assert result.metric==.3 and result.exit_code==0


def test_native_outer_timeout_cannot_publish_pilot_metric_as_full(tmp_path):
    pilot={**PILOT,'timeout_seconds':10.0}
    hang='import time\nprint("{\\"metric\\": 999}",flush=True)\nwhile True: time.sleep(.1)'
    result=SubprocessSandbox().run(pilot_rank([hang,_program(.1)],direction='min',pilot=pilot,assets={}),str(tmp_path),timeout=.2)
    assert result.timed_out and result.metric is None
    assert not list(tmp_path.glob('bestofn-*/full'))


def test_native_stop_kills_current_pilot_session_and_never_runs_full(tmp_path):
    import os,time
    pilot={**PILOT,'timeout_seconds':10.0}
    code='import os,time\nfrom pathlib import Path\nPath("started.pid").write_text(str(os.getpid()))\nwhile True: time.sleep(.1)'
    observed=[]
    def cancel():
        files=list(tmp_path.glob('bestofn-*/pilot-0/started.pid'))
        if files:
            observed.append(int(files[0].read_text()))
            return True
        return False
    result=SubprocessSandbox().run(pilot_rank([code,_program(.1)],direction='min',pilot=pilot,assets={}),str(tmp_path),timeout=10,cancel=type('Cancellation',(),{'is_set':staticmethod(cancel)})())
    assert observed and result.exit_code!=0 and result.metric is None
    assert not list(tmp_path.glob('bestofn-*/full'))
    stat=Path('/proc')/str(observed[0])/'stat'
    # Native kill may leave an already-dead orphan awaiting the container init's reaper.
    assert not stat.exists() or stat.read_text().rsplit(')',1)[1].split()[0]=='Z'


@pytest.mark.parametrize('bad',[{'env':{'CUDA_VISIBLE_DEVICES':'0'},'timeout_seconds':1,'fidelity_review':'review'},
    {'env':{'TASK_FIDELITY':'smoke'},'timeout_seconds':True,'fidelity_review':'review'},
    {'env':{'TASK_FIDELITY':'smoke'},'timeout_seconds':1,'fidelity_review':''},
    {'env':{'TASK_FIDELITY':'smoke'},'timeout_seconds':1,'fidelity_review':'review','unknown':2}])
def test_bad_native_pilot_declaration_refuses_at_settings_boundary(bad):
    from looplab.core.config import Settings
    with pytest.raises(ValueError):Settings(best_of_n=2,best_of_n_pilot=bad)


def test_default_preserves_legacy_exact_selection_and_source(tmp_path):
    from looplab.core.config import Settings
    from looplab.search.best_of_n import BestOfNDeveloper
    assert Settings().best_of_n_pilot is None
    codes=[_program(.2),_program(.1)]
    wrapped=BestOfNDeveloper(_Developer(codes),n=2,foresight=False,listwise=False)
    assert wrapped.implement(Idea(operator='draft'))==codes[0]
    assert 'pilot_selection' not in wrapped.audit_extra()


def test_historical_snapshot_does_not_inherit_ambient_pilot_execution(monkeypatch):
    from looplab.core.config import Settings,settings_from_snapshot
    old=Settings().model_dump(mode='json');old.pop('best_of_n_pilot')
    monkeypatch.setenv('LOOPLAB_BACKEND','llm')
    monkeypatch.setenv('LOOPLAB_BEST_OF_N','2')
    monkeypatch.setenv('LOOPLAB_BEST_OF_N_PILOT',json.dumps(PILOT))
    assert Settings().best_of_n_pilot is not None
    assert settings_from_snapshot(old).best_of_n_pilot is None


def test_real_composition_root_refuses_unsupported_pilot_before_model_client(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from looplab.agents import factory
    from looplab.core.config import Settings
    from looplab.core.errors import ConfigRefusal
    def forbidden(*a,**kw):raise AssertionError('unsupported task reached paid role construction')
    monkeypatch.setattr(factory,'make_llm_client_for',forbidden)
    task=SimpleNamespace(repo_spec=lambda:{'editables':[{'path':'some-repo'}]})
    with pytest.raises(ConfigRefusal):
        factory.make_roles(task,Settings(backend='llm',best_of_n=2,best_of_n_pilot=PILOT))


@pytest.mark.parametrize('kwargs',[{'backend':'toy','best_of_n':2},
    {'backend':'llm','best_of_n':1},{'backend':'llm','best_of_n':9},
    {'backend':'llm','best_of_n':2,'developer_backend':'codex'}])
def test_declared_native_pilot_is_never_a_silently_ignored_setting(kwargs):
    from looplab.core.config import Settings
    with pytest.raises(ValueError):Settings(**kwargs,best_of_n_pilot=PILOT)


def test_successful_pilot_and_full_do_not_leave_forked_workers_running(tmp_path):
    code='''import os,time,json
from pathlib import Path
pid=os.fork()
if pid==0:
    Path('lingering.pid').write_text(str(os.getpid()))
    while True: time.sleep(.1)
while not Path('lingering.pid').exists(): time.sleep(.01)
print(json.dumps({'metric': .2}))
'''
    result=SubprocessSandbox().run(pilot_rank([code,code],direction='min',pilot=PILOT,assets={}),str(tmp_path),timeout=10)
    assert result.exit_code==0 and result.metric==.2,result.stderr
    pids=list(tmp_path.glob('bestofn-*/*/lingering.pid'))
    assert len(pids)==3
    for file in pids:
        stat=Path('/proc')/file.read_text()/'stat'
        assert not stat.exists() or stat.read_text().rsplit(')',1)[1].split()[0]=='Z'


@pytest.mark.parametrize('value',[10**400,float('inf'),float('nan'),'1',True])
def test_timeout_metadata_must_be_a_finite_real_bound(value):
    from looplab.core.config import Settings
    with pytest.raises(ValueError):
        Settings(backend='llm',best_of_n=2,best_of_n_pilot={**PILOT,'timeout_seconds':value})


def test_pilot_resource_envelope_keeps_largest_candidate_and_unknown_memory():
    from looplab.search.best_of_n import BestOfNDeveloper
    class Different(_Developer):
        def implement(self,idea):
            code=super().implement(idea)
            self.last_footprint=({'gpus':1,'gpu_mem_mib':8000} if self.calls==1 else {'gpus':3,'gpu_mem_mib':None})
            return code
    wrapper=BestOfNDeveloper(Different([_program(.2),_program(.1)]),n=2,pilot=PILOT,pilot_assets={})
    wrapper.implement(Idea(operator='draft',footprint={'gpus':0}))
    assert wrapper.last_footprint=={'gpus':3,'gpu_mem_mib':None}


def test_pilot_never_drops_known_resource_demand_when_another_is_unknown():
    from looplab.core.errors import ConfigRefusal
    from looplab.search.best_of_n import BestOfNDeveloper
    class Unknown(_Developer):
        def implement(self,idea):
            code=super().implement(idea);self.last_footprint={'gpus':3} if self.calls==1 else None
            return code
    wrapper=BestOfNDeveloper(Unknown([_program(.2),_program(.1)]),n=2,pilot=PILOT,pilot_assets={})
    with pytest.raises(ConfigRefusal,match='GPU envelope'):
        wrapper.implement(Idea(operator='draft'))


@pytest.mark.parametrize('assets',[{1:'bad','file.txt':'text'},{'../escape':'text'},{'.env':'text'},{'solution.py':'overwrite'}])
def test_malformed_declared_assets_are_refused_before_rendered_execution(assets):
    from looplab.core.errors import ConfigRefusal
    with pytest.raises(ConfigRefusal):
        pilot_rank([_program(.1),_program(.2)],direction='min',pilot=PILOT,assets=assets)


def test_bounded_raw_output_artifacts_match_receipt_even_invalid_UTF8(tmp_path):
    import hashlib
    code='import os\nos.write(1,b"\\xff\\n{\\"metric\\":0.2}\\n")\n'
    result=SubprocessSandbox().run(pilot_rank([code,code],direction='min',pilot=PILOT,assets={}),str(tmp_path),timeout=10)
    assert result.exit_code==0 and result.metric==.2,result.stderr
    receipt=_receipt(result.stdout)
    for row in [*receipt['pilots'],receipt['full']]:
        for index,name in enumerate(['stdout.log','stderr.log']):
            raw=(tmp_path/row['output_ref']/name).read_bytes()
            assert hashlib.sha256(raw).hexdigest()==row['output_sha256'][index]


def test_public_role_composition_wires_the_opt_in_method_with_no_paid_fixture_call(tmp_path,monkeypatch):
    from looplab.agents import factory
    from looplab.core.config import Settings
    from tests.test_inline_repair import _Stub
    class Task:
        id='script-fixture';goal='bounded CPU method';direction='min';kind='fixture'
        def gpu_capable(self):return False
        def llm_roles(self,client,**kwargs):return _Stub(),_Developer([_program(.2,.4),_program(.1,.3)])
    monkeypatch.setattr(factory,'make_llm_client_for',lambda *a,**kw:object())
    researcher,developer=factory.make_roles(Task(),Settings(backend='llm',best_of_n=2,best_of_n_pilot=PILOT,
        unified_agent=False,auto_install_deps=False))
    bundle=developer.implement(Idea(operator='draft',footprint={'gpus':0}))
    result=SubprocessSandbox().run(bundle,str(tmp_path),timeout=10)
    assert result.exit_code==0 and result.metric==.3,result.stderr
