import sys

import anyio

from looplab.adapters.toytask import ToyTask
from looplab.engine.claims import build_context_pack, render_context_pack
from looplab.engine.memory import (
    ConceptCapsuleStore, JsonlCaseLibrary, build_concept_capsule,
    concept_profit_tendencies, portfolio_concept_overview,
)
from looplab.events.replay import fold
from factories import make_engine
from _concept_effect_fixtures import capsule_with_contrasts


def test_terminal_publishes_provisional_memory_and_finalize_replaces_it(tmp_path):
    engine = make_engine(tmp_path / 'run', task=ToyTask(), cross_run_concepts=True,
                         reflection_priors=True, memory_dir=str(tmp_path / 'memory'))
    engine.store.append('run_started', {'run_id': 'r', 'run_uid': 'uid-r', 'task_id': 'toy_quadratic',
                                       'goal': ToyTask().goal, 'direction': 'min'})
    engine.store.append('node_created', {'node_id': 0, 'operator': 'draft', 'parent_ids': [],
                                        'idea': {'operator': 'draft', 'params': {}}, 'code': ''})
    engine.store.append('node_concepts', {'node_id': 0, 'concepts': ['data/toy'], 'mode': 'llm'})
    engine._eval_spec = {'command': [sys.executable, '-c', 'print(\'{"m": 0.25}\')'],
                         'metric': {'reader': 'stdout_json', 'key': 'm'}, 'timeout': 10}
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), 10.0)
    capsule = ConceptCapsuleStore(tmp_path / 'memory' / 'concept_capsules.jsonl').all()[0]
    case = JsonlCaseLibrary(tmp_path / 'memory' / 'cases.jsonl').all()[0]
    assert capsule['provisional'] is True and capsule['best_metric'] == 0.25
    assert case['provisional'] is True and case['metric'] == 0.25
    prior = engine.lessons.load_reflection_priors(role='researcher')
    assert 'Best provisional configuration' in prior and 'not a final result' in prior

    final_state = fold(engine.store.read_all())
    engine.lessons.store_concept_capsule(final_state)
    engine.lessons.store_case(final_state)
    final_capsule = ConceptCapsuleStore(tmp_path / 'memory' / 'concept_capsules.jsonl').all()[0]
    final_case = JsonlCaseLibrary(tmp_path / 'memory' / 'cases.jsonl').all()[0]
    assert 'provisional' not in final_capsule and final_capsule['best_metric'] == 0.25
    assert 'provisional' not in final_case and final_case['metric'] == 0.25
    final_prior = engine.lessons.load_reflection_priors(role='researcher')
    assert 'Best known configuration' in final_prior and 'Best provisional' not in final_prior


def test_plain_capsule_shape_is_unchanged_and_provisional_is_additive():
    base = build_concept_capsule(run_id='r', fingerprint=['a'], direction='max', concepts=['data/toy'])
    provisional = build_concept_capsule(run_id='r', fingerprint=['a'], direction='max',
                                       concepts=['data/toy'], provisional=True)
    assert 'provisional' not in base
    assert provisional == {**base, 'provisional': True}


def test_provisional_capsule_is_visible_but_does_not_meet_support_threshold():
    common = {'fingerprint': ['a'], 'direction': 'max', 'concepts': ['loss/a'],
              'concept_outcomes': {'loss/a': 0.9}}
    settled = capsule_with_contrasts(run_id='settled', **common)
    inflight = capsule_with_contrasts(run_id='inflight', **common, provisional=True)
    overview = portfolio_concept_overview([settled, inflight])
    row = overview['concepts'][0]

    assert overview['n_runs'] == 2 and overview['n_provisional_runs'] == 1
    assert row['n_runs'] == 2 and row['n_provisional_runs'] == 1
    assert row['n_helped'] == 1, 'only the finalized observation meets the support count'
    assert {r['run_id']: r.get('provisional') for r in row['runs']} == {
        'settled': None, 'inflight': True}
    assert concept_profit_tendencies([row]) == {'helps': [], 'hurts': []}

    pack = build_context_pack([], concept_overview=overview)
    assert pack['coverage']['n_provisional_runs'] == 1
    assert pack['coverage']['helps'] == [] and pack['coverage']['hurts'] == []
    assert '1 provisional run(s)' in render_context_pack(pack)


def test_proactive_advisories_wait_for_the_final_capsule(tmp_path):
    from looplab.core.models import RunState
    from looplab.engine.proposal_cues import ProposalCuesMixin
    from looplab.engine.strategy import StrategyCadenceMixin

    class _Host(ProposalCuesMixin, StrategyCadenceMixin):
        _cross_run_advisory = True

    host = _Host()
    host.memory_dir = str(tmp_path)
    state = RunState(run_id='current', run_uid='current-uid', task_id='t', direction='max')
    inflight = capsule_with_contrasts(
        run_id='other', run_uid='other-uid', task_id='t', fingerprint=['a'], direction='max',
        concepts=['loss/a'], concept_outcomes={'loss/a': 0.9}, provisional=True)
    store = ConceptCapsuleStore(tmp_path / 'concept_capsules.jsonl')
    store.add(inflight)

    assert host._cross_run_advisory_text(state) == ''
    assert host._cross_run_note_for_ctx(state) == ''

    settled = {key: value for key, value in inflight.items() if key != 'provisional'}
    store.add(settled)
    assert 'returned run(s)' in host._cross_run_advisory_text(state)
    assert 'returned run(s)' in host._cross_run_note_for_ctx(state)
