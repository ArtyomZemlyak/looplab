import sys

import anyio

from looplab.adapters.toytask import ToyTask
from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
from looplab.events.replay import fold
from factories import make_engine


def test_terminal_publishes_provisional_capsule_and_finalize_replaces_it(tmp_path):
    engine = make_engine(tmp_path / 'run', task=ToyTask(), cross_run_concepts=True,
                         memory_dir=str(tmp_path / 'memory'))
    engine.store.append('run_started', {'run_id': 'r', 'run_uid': 'uid-r', 'task_id': 'toy_quadratic',
                                       'goal': ToyTask().goal, 'direction': 'min'})
    engine.store.append('node_created', {'node_id': 0, 'operator': 'draft', 'parent_ids': [],
                                        'idea': {'operator': 'draft', 'params': {}}, 'code': ''})
    engine.store.append('node_concepts', {'node_id': 0, 'concepts': ['data/toy'], 'mode': 'llm'})
    engine._eval_spec = {'command': [sys.executable, '-c', 'print(\'{"m": 0.25}\')'],
                         'metric': {'reader': 'stdout_json', 'key': 'm'}, 'timeout': 10}
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), 10.0)
    capsule = ConceptCapsuleStore(tmp_path / 'memory' / 'concept_capsules.jsonl').all()[0]
    assert capsule['provisional'] is True and capsule['best_metric'] == 0.25
    engine.lessons.store_concept_capsule(fold(engine.store.read_all()))
    final = ConceptCapsuleStore(tmp_path / 'memory' / 'concept_capsules.jsonl').all()[0]
    assert 'provisional' not in final and final['best_metric'] == 0.25


def test_plain_capsule_shape_is_unchanged_and_provisional_is_additive():
    base = build_concept_capsule(run_id='r', fingerprint=['a'], direction='max', concepts=['data/toy'])
    provisional = build_concept_capsule(run_id='r', fingerprint=['a'], direction='max',
                                       concepts=['data/toy'], provisional=True)
    assert 'provisional' not in base
    assert provisional == {**base, 'provisional': True}
