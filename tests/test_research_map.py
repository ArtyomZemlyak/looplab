import json

from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app


def test_research_map_links_only_declared_arms_and_keeps_missing_scalar(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'uid-r', 'task_id': 't', 'goal': 'compare models', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    store.append('node_created', {'node_id': 0, 'operator': 'score', 'parent_ids': [], 'idea': {'operator': 'score', 'hypothesis': 'Does it help?'}})
    store.append('node_evaluated', {'node_id': 0, 'metric': None, 'extra_metrics': {'recall@200': .5}})
    row = {'amendment_id': 'am1', 'version': 1, 'kind': 'experiment', 'run_uid': 'uid-r', 'generation': gen,
           'at_event_seq': 0, 'payload': {'experiment_id': 'e1', 'title': 'Comparison', 'question': 'Does it help?',
           'hypothesis': 'It may help', 'primary_metric': 'recall@200', 'arms': {'new': {'node_id': 0}},
           'tests': [{'label': 'heldout', 'test_id': 'test1', 'description': 'fixed cohort'}],
           'metrics': [{'name': 'recall@200', 'direction': 'max', 'kind': 'mean'}], 'contrasts': []}}
    (rd / 'protocol_amendments.jsonl').write_text(json.dumps(row) + '\n')
    before = {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()}
    with TestClient(make_app(tmp_path)) as c:
        url = '/api/runs/r/research-map'
        assert c.get(url, params={'expected_generation': '0' * 64}).status_code == 409
        res = c.get(url, params={'expected_generation': gen, 'limit': 1})
        assert res.status_code == 200, res.text
        body = res.json(); exp = body['experiments']['items'][0]; node = body['nodes']['items'][0]
        assert exp['question'] == 'Does it help?' and exp['arms'][0]['node_id'] == 0
        assert exp['tests'][0]['test_id'] == 'test1' and exp['metrics'][0]['name'] == 'recall@200'
        assert node['metric_present'] is False and node['extra_metric_keys'] == ['recall@200']
        assert node['parent_ids'] == [] and node['parent_card_id'] is None
        assert body['source_health']['atomic_across_sources'] is False
    assert {p.name: p.read_bytes() for p in rd.iterdir() if p.is_file()} == before


def test_quality_separates_registered_question_from_filed_link_and_does_not_verify_sources():
    from types import SimpleNamespace as N
    from looplab.harness.research_quality import project
    state = N(research=[{'sources': [{'url': 'https://example.org/paper'}],
                          'claims': [{'urls': ['https://example.org/paper']}],
                          'reasoning': '', 'open_questions': []}],
              nodes={1: N(idea=N(parent_card_id=None))}, report={'caveats': ['limited']})
    quality = project(state, {'e': {'question': 'What changes?'}})
    assert quality['registrations_with_questions'] == 1
    assert quality['nodes_with_parent_card'] == 0
    assert quality['claim_source_links'] == 1
    assert 'does not verify' in quality['interpretation']
    assert 'verdict' in quality['report_fields_empty']


def test_export_keeps_selected_history_and_refuses_unknown_versions(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir()
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'uid-r', 'task_id': 't', 'goal': 'study', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    old = store.append('report_generated', {'at_node': 0, 'content': {'headline': 'First study', 'body_markdown': 'Measured first result.'}})
    new = store.append('report_generated', {'at_node': 0, 'content': {'headline': 'Second study', 'body_markdown': 'Measured second result.'}})
    before = (rd / 'events.jsonl').read_bytes()
    with TestClient(make_app(tmp_path)) as c:
        url = '/api/runs/r/research-export'
        response = c.get(url, params=[('expected_generation', gen), ('report_seq', old.seq), ('report_seq', new.seq)])
        assert response.status_code == 200, response.text
        result = response.json()
        assert 'Measured first result.' in result['markdown'] and 'Measured second result.' in result['markdown']
        assert result['markdown_sha256'] == hashlib.sha256(result['markdown'].encode()).hexdigest()
        assert [r['report_seq'] for r in result['reports']] == [old.seq, new.seq]
        assert c.get(url, params={'expected_generation': gen, 'report_seq': 999}).status_code == 404
        assert c.get(url, params={'expected_generation': '0' * 64}).status_code == 409
    assert (rd / 'events.jsonl').read_bytes() == before


def test_campaign_overview_survives_a_later_experiment_report(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    first = store.append('report_generated', {'content': {'headline': 'First', 'campaign_overview': {
        'summary': 'The full campaign so far.', 'learnings': ['Earlier result'], 'report_seqs': []}}})
    second = store.append('report_generated', {'content': {'headline': 'Latest experiment', 'body_markdown': 'New result'}})
    with TestClient(make_app(tmp_path)) as c:
        page = c.get('/api/runs/r/research-map', params={'expected_generation': gen}).json()
        assert page['campaign_overview']['published_seq'] == first.seq
        assert page['campaign_overview']['content']['learnings'] == ['Earlier result']
        assert page['reports']['items'][0]['seq'] == second.seq
        hist = c.get(f'/api/runs/r/report/versions/{first.seq}').json()
        assert hist['content']['campaign_overview']['summary'] == 'The full campaign so far.'


def test_memo_guidance_distinguishes_technical_from_scientific_without_gating(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all()); before = (rd / 'events.jsonl').read_bytes()
    with TestClient(make_app(tmp_path)) as c:
        url = '/api/runs/r/research-memo-review'
        body = {'expected_generation': gen, 'purpose': 'scientific', 'memo': {'summary': 'Prepared', 'sources': [{'url': 'https://example.org/p'}]}}
        result = c.post(url, json=body); assert result.status_code == 200
        codes = {x['code'] for x in result.json()['issues']}
        assert 'sources_not_linked_to_claim' in codes and 'missing_reasoning' in codes
        technical = c.post(url, json={**body, 'purpose': 'technical', 'memo': {'summary': 'Retry diagnosed'}}).json()
        assert technical['issues'] == [] and 'No gate' in technical['meaning']
        assert c.post(url, json={**body, 'expected_generation': '0' * 64}).status_code == 409
        malformed = c.post(url, json={**body, 'memo': {'summary': 's', 'sources': 1, 'claims': 2}})
        assert malformed.status_code == 200
    assert (rd / 'events.jsonl').read_bytes() == before


def test_map_preserves_claim_links_and_recorded_decisions_without_certification(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    store.append('research_completed', {'memo': {'summary': 'Decision', 'reasoning': 'Different inputs.',
        'sources': [{'title': 'Paper', 'url': 'https://example.org/p'}],
        'claims': [{'statement': 'The source uses other inputs.', 'node_ids': [], 'urls': ['https://example.org/p']}]}})
    (rd / 'harness_decisions.jsonl').write_text(json.dumps({'run_uid':'u','generation':gen,'phase_id':'novelty',
        'decision':'submit','reason':'Prior used another input.','at_node':0,'action_id':'d1'})+'\n')
    with TestClient(make_app(tmp_path)) as c:
        result = c.get('/api/runs/r/research-map', params={'expected_generation': gen}).json()
    memo = result['research_memos']['items'][0]
    assert memo['claims'][0]['urls'] == ['https://example.org/p']
    assert memo['sources'][0]['url'] == 'https://example.org/p'
    assert result['decisions']['items'][0]['reason'] == 'Prior used another input.'
    assert result['source_health']['decisions']['read_complete']


def test_large_campaign_page_is_bounded_and_marks_shortened_details(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    for i in range(30):
        store.append('research_completed', {'memo': {'summary': 'Описание ' * 200,
            'reasoning': 'Аргумент ' * 700, 'sources': [{'title': 'source', 'url': 'https://example.org/' + 'a' * 200}],
            'claims': [{'statement': 'Результат ' * 100, 'urls': ['https://example.org/' + 'a' * 200]}]}})
    with TestClient(make_app(tmp_path)) as c:
        response = c.get('/api/runs/r/research-map', params={'expected_generation': gen, 'limit': 100})
        assert response.status_code == 200
        assert len(response.content) < 256 * 1024
        page = response.json()['research_memos']
        assert page['limit'] == 20 and page['has_more'] and len(page['items']) == 20
        assert all(m['detail_truncated'] for m in page['items'])


def test_memo_feedback_finds_unlinked_claims_and_each_unlinked_source():
    from looplab.harness.research_quality import review_memo
    memo = {'summary': 'Conclusion', 'reasoning': 'Why', 'open_questions': ['Why?'],
            'next_experiments': ['Test'], 'sources': [{'url': 'https://example.org/a'}, {'url': 'https://example.org/b'}],
            'claims': [{'statement': 'A', 'urls': ['https://example.org/a']}, {'statement': 'B'}]}
    issues = {i['code'] for i in review_memo(memo, purpose='scientific')['issues']}
    assert {'claim_has_no_evidence', 'some_sources_unlinked'} <= issues
    assert review_memo(memo, purpose='technical')['issues'] == []


def test_map_preserves_separate_review_decisions(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    rows = [{'run_uid': 'u', 'generation': gen, 'phase_id': phase, 'action_id': phase,
             'decision': 'no_applicable_action', 'reason': reason, 'evidence': [0], 'at_node': 1}
            for phase, reason in [('claim_curation', 'No supported claim'), ('lessons', 'No transferable lesson')]]
    (rd / 'harness_reviews.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with TestClient(make_app(tmp_path)) as c:
        response = c.get('/api/runs/r/research-map', params={'expected_generation': gen})
        assert response.status_code == 200, response.text
        reviews = response.json()['reviews']['items']
        assert {(r['phase'], r['reason']) for r in reviews} == {('claim_curation', 'No supported claim'), ('lessons', 'No transferable lesson')}
        assert response.json()['source_health']['reviews']['read_complete']


def test_map_renders_native_question_without_inventing_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    store.append('hypothesis_added', {'statement': 'Does additional context help?', 'source': 'deep_research'})
    with TestClient(make_app(tmp_path)) as c:
        page = c.get('/api/runs/r/research-map', params={'expected_generation': gen}).json()
        card = next(x for x in page['cards']['items'] if x['statement'] == 'Does additional context help?')
        assert card['kind'] == 'direction' and card['verdict'] == 'open' and not card['evidence']


def test_export_uses_content_addressed_figure_references_without_copying_bytes(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all()); fid = 'a' * 64
    report = store.append('report_generated', {'content': {'headline': 'Results', 'figures': [{'figure_id': fid, 'caption': 'Plot'}]}})
    with TestClient(make_app(tmp_path)) as c:
        res = c.get('/api/runs/r/research-export', params=[('expected_generation', gen), ('report_seq', report.seq)])
        assert res.status_code == 200, res.text
        result = res.json(); ref = result['reports'][0]['figure_refs'][0]
        assert ref == {'figure_id': fid, 'sha256': fid, 'api': f'/api/runs/r/report-figures/{fid}'}
        assert ref['api'] in result['markdown'] and 'not copied bytes' in result['limits']


def test_export_includes_project_profile_as_current_declared_context(tmp_path, monkeypatch):
    from looplab.core.metric_profile import profile_digest
    from looplab.serve.projects import ProjectStore
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    org = ProjectStore(tmp_path / 'projects.json'); project = org.create('vision'); org.assign('r', project.id)
    org.put_metric_profile(project.id, {'name': 'Segmentation', 'metrics': [{'key': 'IoU', 'role': 'primary',
        'denominator': 'union pixels'}], 'slices': ['small objects']}, profile_digest(None))
    with TestClient(make_app(tmp_path)) as client:
        res = client.get('/api/runs/r/research-export', params={'expected_generation': gen})
        assert res.status_code == 200, res.text
        out = res.json()
        assert 'union pixels' in out['markdown'] and 'small objects' in out['markdown']
        assert 'not evidence of a measured value' in out['markdown']
        assert out['metric_profile']['state'] == 'configured'


def test_export_keeps_selected_result_and_attempt_metric_references(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    report = store.append('report_generated', {'content': {'headline': 'Result', 'champion_summary': 'Selected with caveats',
        'key_metrics': {'metrics': [{'key': 'IoU', 'label': 'Overlap'}], 'nodes': [{'node_id': 2, 'node_generation': 3}]}}})
    with TestClient(make_app(tmp_path)) as c:
        result = c.get('/api/runs/r/research-export', params=[('expected_generation', gen), ('report_seq', report.seq)]).json()
        assert 'Selected with caveats' in result['markdown'] and 'Overlap (IoU)' in result['markdown']
        assert 'node #2 attempt 3' in result['markdown'] and 'values not embedded' in result['markdown']
        assert len(result['reports'][0]['content_sha256']) == 64


def test_oversized_export_refuses_instead_of_returning_truncated_history(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all()); seqs = []
    for i in range(10):
        event = store.append('report_generated', {'content': {'headline': f'Report {i}', 'body_markdown': 'x' * 24000}})
        seqs.append(event.seq)
    with TestClient(make_app(tmp_path)) as c:
        response = c.get('/api/runs/r/research-export', params=[('expected_generation', gen), *[('report_seq', n) for n in seqs]])
        assert response.status_code == 413 and 'select fewer reports' in response.text


def test_campaign_recommendation_and_limits_survive_focused_report(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    first = store.append('report_generated', {'content': {'campaign_overview': {'summary': 'Overall',
        'recommendation': 'No champion is applicable yet.', 'recommendation_limits': 'Only one cohort measured.', 'report_seqs': [1]}}})
    store.append('report_generated', {'content': {'headline': 'Latest'}})
    with TestClient(make_app(tmp_path)) as c:
        page = c.get('/api/runs/r/research-map', params={'expected_generation': gen}).json()
        overview = page['campaign_overview']['content']
        assert overview['recommendation'] == 'No champion is applicable yet.'
        assert overview['recommendation_limits'] == 'Only one cohort measured.'
        exported = c.get('/api/runs/r/research-export', params={'expected_generation': gen}).json()['markdown']
        assert overview['recommendation'] in exported and overview['recommendation_limits'] in exported
        assert page['campaign_overview']['published_seq'] == first.seq


def test_card_feedback_is_contextual_and_never_a_technical_review_gate():
    from looplab.harness.research_quality import review_experiment
    vague = {'title': 'experiment', 'arms': {'a': {}}, 'hypothesis': 'test', 'contrasts': []}
    issues = review_experiment(vague)
    assert {'generic_title', 'question_not_recorded', 'control_not_recorded', 'criterion_not_explained'} <= set(issues)
    technical = {'title': 'Retry failed setup', 'arms': {'a': {'work_kind': 'engineering_recovery'}}}
    assert review_experiment(technical) == []


def test_claim_author_assessment_remains_distinct_from_verifier(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    store.append('research_completed', {'memo': {'summary': 'Result', 'claims': [{'statement': 'The result transfers',
        'author_status': 'limited', 'limitation': 'One cohort', 'contrast_ref': 'exp1/control-treatment', 'node_ids': [0]}]}})
    with TestClient(make_app(tmp_path)) as c:
        page = c.get('/api/runs/r/research-map', params={'expected_generation': gen}).json()
        claim = page['research_memos']['items'][0]['claims'][0]
        assert claim['author_status'] == 'limited' and claim['limitation'] == 'One cohort'
        assert claim['contrast_ref'] == 'exp1/control-treatment' and claim['verdict'] == ''


def test_direction_export_uses_explicit_links_beyond_first_map_page(tmp_path, monkeypatch):
    from looplab.events.replay import fold
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    rd = tmp_path / 'r'; rd.mkdir()
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    store.append('hypothesis_added', {'statement': 'Which decoding works?', 'source': 'deep_research'})
    card_id = next(iter(fold(store.read_all()).cards))
    rows = []
    for i in range(25):
        payload = {'experiment_id': f'e{i}', 'title': f'Experiment {i}', 'question': f'Question {i}'}
        if i == 24:
            payload['question_card_id'] = card_id
        rows.append({'amendment_id': f'am{i}', 'version': 1, 'kind': 'experiment', 'run_uid': 'u', 'generation': gen, 'payload': payload})
    (rd / 'protocol_amendments.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with TestClient(make_app(tmp_path)) as c:
        url = '/api/runs/r/research-export'
        result = c.get(url, params={'expected_generation': gen, 'question_card_id': card_id})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body['direction']['matched_registrations'] == 1
        assert 'Which decoding works?' in body['markdown'] and 'Experiment 24' in body['markdown']
        assert 'Experiment 0' not in body['markdown']
        assert c.get(url, params={'expected_generation': gen, 'question_card_id': 'missing'}).status_code == 404


def test_quality_excludes_only_explicit_technical_nodes():
    from types import SimpleNamespace as N
    from looplab.harness.research_quality import project
    node = lambda i, card: N(id=i, idea=N(parent_card_id=card))
    state = N(run_id='r', research=[], nodes={0: node(0, None), 1: node(1, None), 2: node(2, 'q')}, report={})
    experiments = {'e': {'arms': {'recovery': {'node_id': 0, 'work_kind': 'engineering_recovery'},
                                     'fit': {'node_id': 2, 'work_kind': 'fit'}}}}
    q = project(state, experiments)
    assert q['engineering_nodes_excluded'] == 1 and q['unclassified_nodes'] == 1
    assert q['question_applicable_or_unknown_nodes'] == 2 and q['question_applicable_or_unknown_without_card'] == 1
    assert q['notice_resolution'].startswith('unknown')


def test_export_publication_adapter_is_explicit_and_never_sends(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    p = tmp_path / 'projects.json'
    profile = {'name': 'Project', 'metrics': [{'key': 'm', 'role': 'primary'}], 'publication': {'adapter': 'manual_markdown', 'destination_label': 'Team research'}}
    p.write_text(json.dumps({'assignments': {'r': 'p'}, 'projects': [{'id': 'p', 'metric_profile': profile}]}))
    before = {path.name: path.read_bytes() for path in rd.iterdir()}
    with TestClient(make_app(tmp_path)) as client:
        response = client.get('/api/runs/r/research-export', params={'expected_generation': gen})
        assert response.status_code == 200, response.text
        assert response.json()['publication_adapter'] == {'state': 'configured', 'adapter': 'manual_markdown', 'destination_label': 'Team research', 'operation': 'manual download only'}
    assert {path.name: path.read_bytes() for path in rd.iterdir()} == before
