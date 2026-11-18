"""One explicit eligible-node coordinate system for Card plans, every gate, and public display."""
import pytest
from fastapi.testclient import TestClient

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.plan import (build_plan, endgame_actions, endgame_admits, endgame_admitted,
    endgame_own_action, endgame_refused_card_ids, plateau_nodes, plan_node_count,
    plan_population, replan)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.serve.server import make_app
from tests.factories import make_engine


def _nodes():
    return {i: Node(id=i, operator='draft', idea=Idea(operator='draft'),
                    status=NodeStatus.evaluated, metric=10-i, feasible=True) for i in range(6)}


def test_hidden_manual_node_counts_once_in_the_actual_engine_plan(tmp_path):
    engine = make_engine(tmp_path / 'r', n_seeds=1, max_nodes=10, card_driven_selection=True,
                         endgame_reserve_frac=.25, endgame_inject_recut=True)
    engine.store.append('run_started', {'run_id':'r', 'task_id':'t', 'direction':'max'})
    for i in range(6):
        engine.store.append('node_created', {'node_id':i,'operator':'draft',
            'parent_ids':[], 'idea':{'operator':'draft'},
            **({'source':'manual'} if i in (0,1) else {})})
        engine.store.append('node_evaluated', {'node_id':i,'metric':10-i,
            **({'violations':[{'name':'memory','value':2.0,'max':1.0}]} if i == 2 else {})})
    engine.store.append('node_tombstoned', {'node_ids':[0]})
    state = fold(engine.store.read_all())
    engine._refresh_speculation_budget(state)
    assert set(plan_population(state, basis='card_budget')) == {1,3,4,5}
    assert engine._ensure_plan(state)
    state = fold(engine.store.read_all())
    assert state.plan['at_node'] == 4
    assert state.plan['injected'] == 1 # the hidden operator node0 is not subtracted a second time
    assert state.plan['max_nodes'] == engine.policy.max_nodes
    assert state.plan['node_count_basis'] == 'card_budget'
    assert state.plan_history[-1]['node_count_basis'] == 'card_budget'
    assert not engine._ensure_plan(state)
    ctx = engine._strategy_ctx(state)
    expected = plan_node_count(state) >= state.plan['endgame_start']
    assert ctx.plan_endgame is expected
    snapshot = state.model_dump(mode='json')
    assert fold(EventStore(engine.store.path).read_all()).model_dump(mode='json') == snapshot


def test_all_gate_readers_use_the_same_population_with_sparse_ids():
    nodes = _nodes()
    nodes[0].tombstoned = True
    nodes[2].feasible = False
    state = RunState(nodes=nodes, direction='max', best_node_id=1)
    state.plan = build_plan(max_nodes=8,n_seeds=1,reserve_frac=.25,at_node=4,
                           node_count_basis='card_budget')
    assert plan_node_count(state) == 4 and len(state.nodes) == 6
    actions = [{'kind':'draft'}]
    assert endgame_actions(state,state.plan,actions) == actions
    assert endgame_admits(state,state.plan,actions[0])
    assert endgame_admitted(state,actions) == actions
    assert endgame_refused_card_ids(state,state.plan) == frozenset()
    assert endgame_own_action(state) is None


def test_reserve_membership_uses_population_rank_for_merges_simplifications_and_plateau():
    from looplab.engine.plan import _EndgameView
    state = RunState(nodes=_nodes(), direction='max', best_node_id=0)
    state.nodes[1].tombstoned = True
    state.nodes[2].feasible = False
    state.plan = build_plan(max_nodes=5,n_seeds=1,reserve_frac=.4,at_node=4,
                           node_count_basis='card_budget') # starts at eligible position3, id5
    state.nodes[3].operator = 'merge' # physical3 but eligible position1: not the reserve merge
    state.nodes[4].simplified = {'parent_id':0} # physical4 but eligible position2
    view = _EndgameView(state,state.plan)
    assert view.own(sweep=False)['kind'] == 'merge'
    assert plateau_nodes(state) == (0,1) # only physical5 lies in the eligible reserve
    state.nodes[5].operator = 'merge'
    view = _EndgameView(state,state.plan)
    assert view.own(sweep=False)['kind'] == 'improve'
    state.nodes[5].simplified = {'parent_id':0}
    assert not _EndgameView(state,state.plan).simplify_passes()


def test_population_migration_is_explicit_and_preserves_spent_champions():
    old = build_plan(max_nodes=10,n_seeds=1,reserve_frac=.3,at_node=5)
    old.update(champion=3,stall_champions=[2])
    migrated = replan(old,max_nodes=8,n_seeds=1,reserve_frac=.3,at_node=3,stall_rung=0,
                     node_count_basis='card_budget')
    assert migrated['reason'] == 'population_changed'
    assert migrated['node_count_basis'] == 'card_budget'
    assert migrated['stall_champions'] == [2,3]
    assert 'node_count_basis' not in old
    assert replan(migrated,max_nodes=8,n_seeds=1,reserve_frac=.3,at_node=3,stall_rung=0) is None
    assert build_plan(max_nodes=8,n_seeds=1,reserve_frac=.3,at_node=0,node_count_basis='future') is None
    with pytest.raises(ValueError):
        plan_node_count(RunState(), {'node_count_basis':'future'})


def test_public_projection_adds_observed_population_without_changing_the_log(tmp_path,monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS','1')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN',raising=False)
    rd=tmp_path/'r';rd.mkdir();store=EventStore(rd/'events.jsonl')
    store.append('run_started',{'run_id':'r','task_id':'t','run_uid':'u','direction':'max'})
    for i in [0,7]:
        store.append('node_created',{'node_id':i,'operator':'draft','parent_ids':[],
                                     'idea':{'operator':'draft'}})
    store.append('node_tombstoned',{'node_ids':[0]})
    store.append('plan',build_plan(max_nodes=4,n_seeds=1,reserve_frac=.5,at_node=1,
                                  node_count_basis='card_budget'))
    before=store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        public=client.get('/api/runs/r/state').json()['state']
    assert public['plan']['current_node_count'] == 1
    assert 'current_node_count' not in fold(store.read_all()).plan
    assert store.path.read_bytes() == before
