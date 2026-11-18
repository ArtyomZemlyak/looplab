"""Actual proposal prompt/binding, native replay and stable whole-card coverage, no paid model."""
import copy
import json

import pytest

from looplab.agents.roles import LLMResearcher
from looplab.agents.state_brief import next_board_prompt_cards, BOARD_PROMPT_SEED_BUDGET_CHARS, BOARD_PROMPT_CARDS
from looplab.core.models import Card, CardSelectionProvenance, Idea, RunState
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _card(i,text):
    return Card(id=f'card-{i:03d}',statement=text,seed_statement=text,verdict='open',status='proposed',
        evidence=[],selection_provenance=CardSelectionProvenance(action_source='card_added',
            action_owner_count=1,action_complete=True,freshness='current'))


def _state(texts,goal='Compare Qwen backbone recommendation quality'):
    return RunState(goal=goal,direction='max',cards={f'card-{i:03d}':_card(i,text) for i,text in enumerate(texts)})


def _ids(state,**kw):return [card.id for card in next_board_prompt_cards(state,**kw)]


def _journal(tmp_path):
    store=EventStore(tmp_path/'events.jsonl')
    store.append('run_started',{'run_id':'board','run_uid':'uid','direction':'max',
                                'goal':'Compare Qwen backbone recommendation quality'})
    seeds=[f'Port allocation issue number {i}: examine scheduler handling' for i in range(29)]
    seeds.append('Compare Qwen backbone recommendation quality using the saved control')
    for i,seed in enumerate(seeds):
        idea=Idea(operator='draft',params={'x':.1},rationale='Prepared source-only question',
                  hypothesis=seed,card_id=f'card-{i:03d}')
        action=Engine._card_action(idea,[],{},None,None,scored_against_empty=True)
        payload=Engine._card_added_payload(idea.card_id,seed,action,idea,source='researcher',at_node=0)
        store.append('card_added',payload)
    return store


def test_actual_native_replay_and_proposal_show_and_bind_goal_card_before_unrelated_rotation(tmp_path,monkeypatch):
    store=_journal(tmp_path);before=store.path.read_bytes();state=fold(store.read_all())
    target=state.cards['card-029'];responses=[]
    class Client:
        def complete_tool(self,messages,json_schema=None,**kwargs):
            responses.append(copy.deepcopy(messages))
            return {'operator':'draft','params':{'x':.1},'rationale':'Use prepared Qwen question',
                'hypothesis':target.seed_statement,'card_id':target.id,'concept_mode':'full','concepts':[]}
    researcher=LLMResearcher(Client())
    current=researcher.propose(state,None)
    assert current.card_id==target.id
    assert researcher._visible_board_cards[0].id==target.id
    assert target.seed_statement in str(responses[-1])
    assert _ids(state)[0]==target.id
    assert _ids(fold(store.read_all()))==_ids(state)
    # Counterfactual: disable ONLY new context ranking. The former attempt0 window does not
    # include card029, so the real old binding correctly nulls the model's unseen CARD_ID claim.
    monkeypatch.setattr('looplab.agents.state_brief.rank_board_context',lambda *_:None)
    old=LLMResearcher(Client()).propose(state,None)
    assert old.card_id is None
    assert target.seed_statement not in str(responses[-1])
    assert store.path.read_bytes()==before


def test_latest_operator_directive_precedes_goal_and_model_or_retired_hints_never_gain_priority():
    state=_state(['Compare Qwen recommendation','Compare LFM efficiency','Diagnose port conflict'])
    state.pending_hints=[{'text':'Compare LFM efficiency'},
                         {'text':'Diagnose port conflict','source':'deep_research'},
                         {'text':'deep-research directions: Diagnose port conflict'},
                         {'text':'Diagnose port conflict','status':'cancelled'}]
    assert _ids(state)[0]=='card-001'
    state.pending_hints[0]['status']='completed'
    assert _ids(state)[0]=='card-000'
    state.pending_hints=[{'text':'Compare LFM efficiency'},{'text':'Compare Qwen recommendation'}]
    assert _ids(state)[0]=='card-000'


def test_unicode_normalization_and_casefold_match_visible_seed_without_translation():
    state=_state(['Ports are unrelated','Сравнить рекомендации ＱＷＥＮ'],goal='сравнить рекомендации qwen')
    assert _ids(state)[0]=='card-001'
    # Display-only edits do not silently change the immutable hypothesis asked in the prompt.
    state.cards['card-001'].statement='Ports are unrelated after display edit'
    assert _ids(state)[0]=='card-001'


def test_explicit_predicted_order_and_no_signal_fallback_keep_their_original_contracts():
    state=_state(['Unrelated ports','Unrelated tracing','Qwen backbone quality'])
    assert _ids(state,hyp_order=['card-001','card-000','card-002'])==['card-001','card-000','card-002']
    state.goal='';assert _ids(state,attempt=1)==['card-001','card-002','card-000']
    state.goal='Novel vocabulary elsewhere';assert _ids(state,attempt=2)==['card-002','card-000','card-001']


def test_every_stable_population_member_keeps_a_whole_coverage_anchor_despite_long_relevant_leaders():
    prefix='Qwen backbone recommendation '
    # Five maximum-sized leaders consume every seed character if no anchor is reserved.
    # Distinct seeds are necessary: the native projection collapses identical beliefs.
    texts=[prefix+'x'*(4000-len(prefix)-len(str(i)))+str(i) for i in range(5)]
    texts += [f'Port issue {i}' for i in range(31)]
    state=_state(texts);original={k:v.seed_statement for k,v in state.cards.items()};coverage=sorted(state.cards)
    seen=set()
    for attempt in range(len(coverage)):
        selected=next_board_prompt_cards(state,attempt=attempt)
        ids=[c.id for c in selected]
        assert coverage[attempt] in ids
        assert len(ids)<=BOARD_PROMPT_CARDS and len(ids)==len(set(ids))
        assert sum(len(c.seed_statement)+len(state.card_substitution_brief(c)) for c in selected)<=BOARD_PROMPT_SEED_BUDGET_CHARS
        assert all(c.seed_statement==original[c.id] for c in selected)
        seen.update(ids)
    assert seen==set(state.cards)


def test_oversized_seed_or_substitution_cannot_steal_coverage_budget(monkeypatch):
    state=_state(['Qwen '*900,'Unrelated small question','Qwen backbone quality','Port substitution'])
    monkeypatch.setattr(RunState,'card_substitution_brief',lambda self,card:'x'*21000 if card.id=='card-003' else '')
    for attempt in range(8):
        ids=_ids(state,attempt=attempt)
        assert 'card-000' not in ids and 'card-003' not in ids
        assert ids[0]=='card-002'
        assert set(ids)=={'card-001','card-002'}


def test_context_ties_replay_stably_across_card_dictionary_insertion_order_and_never_reintroduce_builds(monkeypatch):
    state=_state(['Qwen backbone first','Qwen backbone second','Port unrelated'])
    reverse=state.model_copy(deep=True);reverse.cards=dict(reversed(list(reverse.cards.items())))
    assert _ids(state,attempt=1)==_ids(reverse,attempt=1)
    monkeypatch.setattr(RunState,'cards_being_built',lambda self:{'card-000'})
    assert 'card-000' not in _ids(state)
    assert _ids(state)[0]=='card-001'
