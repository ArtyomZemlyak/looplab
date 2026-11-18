from looplab.harness.briefing import effective_authoring_language
from looplab.harness.research_quality import report_metric_status, report_repetition


def test_explicit_authoring_language_wins_before_conservative_goal_inference():
    russian = 'Исследовать качество explore предсказаний и улучшить модель рекомендаций на одинаковом тесте'
    assert effective_authoring_language('en', 'ru', russian) == ('en', 'run_config')
    assert effective_authoring_language('auto', 'ru', russian) == ('ru', 'project_preference')
    assert effective_authoring_language('auto', 'auto', russian) == ('ru', 'goal_inferred')
    assert effective_authoring_language('auto', 'auto', 'Improve recall on the fixed heldout cohort') == ('en', 'default')
    assert effective_authoring_language('auto', 'auto', 'Русский текст') == ('en', 'default')


def test_report_metric_status_distinguishes_bound_explained_and_missing():
    assert report_metric_status({'key_metrics': {'metrics': [{'key': 'Recall@20'}], 'nodes': [{'node_id': 1}]}})['status'] == 'bound'
    explained = {'verdict': 'Structured key_metrics не заполнены: native scalar в этом run не объявлен.'}
    assert report_metric_status(explained)['status'] == 'explained_unavailable'
    assert report_metric_status({'verdict': 'No scientific result yet.'})['status'] == 'missing'
    assert report_metric_status({'key_metrics': {'metrics': [{'key': 'Recall@20'}], 'nodes': []}})['status'] == 'missing'


def test_report_repetition_is_specific_and_advisory():
    first = 'The paired comparison on the fixed cohort supports the query context variant for this decision.'
    second = 'The alternative with hard semantic identifiers did not improve unseen item recovery on this test.'
    report = {'campaign_overview': {'summary': first + ' ' + second}, 'body_markdown': first + '\n' + second}
    assert report_repetition(report)['status'] == 'substantial_verbatim_overlap'
    report['campaign_overview'] = {'summary': 'Short overview.', 'what_worked': [first], 'learnings': [second]}
    assert report_repetition(report)['status'] == 'substantial_verbatim_overlap'
    report['body_markdown'] = 'A new result with different measurements and limitations is now described.'
    assert report_repetition(report)['status'] == 'not_detected'
    assert report_repetition({})['status'] == 'not_detected'
