"""Advisory research coverage facts, not an admission gate or truth verifier."""
from __future__ import annotations


REPORT_FIELDS = ('verdict', 'champion_summary', 'what_worked', 'what_didnt',
                 'learnings', 'next_directions', 'caveats', 'key_metrics')


def report_fields_empty(report: dict) -> list[str]:
    """Presence only: a metrics envelope needs both actual metric and node references."""
    report = report if isinstance(report, dict) else {}
    metrics = report.get('key_metrics')
    return [field for field in REPORT_FIELDS if not (
        bool(metrics.get('metrics')) and bool(metrics.get('nodes'))
        if field == 'key_metrics' and isinstance(metrics, dict) else
        False if field == 'key_metrics' else bool(report.get(field))
    )]


def report_metric_status(report: dict) -> dict:
    """Whether real metric references exist or their absence is explicitly explained."""
    report = report if isinstance(report, dict) else {}
    metrics = report.get('key_metrics') if isinstance(report.get('key_metrics'), dict) else {}
    keys = [row.get('key') for row in metrics.get('metrics') or []
            if isinstance(row, dict) and row.get('key')]
    if keys and metrics.get('nodes'):
        return {'status': 'bound', 'metric_keys': keys,
                'meaning': 'Recorded metric and node references; cohort and policy identity still require their bindings.'}
    texts = []
    for key in ('verdict', 'champion_summary', 'body_markdown', 'caveats'):
        value = report.get(key)
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            texts.extend(item for item in value if isinstance(item, str))
    text = '\n'.join(texts)
    explained = ('Structured key_metrics не заполнены' in text
                 or 'Structured key_metrics are not populated' in text)
    return {'status': 'explained_unavailable' if explained else 'missing', 'metric_keys': [],
            'meaning': ('The report explicitly explains why metric references are unavailable; this is not an error.'
                        if explained else 'No usable metric references or explicit explanation are recorded.')}


def report_repetition(report: dict) -> dict:
    """Detect substantial verbatim overview/body sentences without rewriting authored prose."""
    import re
    report = report if isinstance(report, dict) else {}
    overview = report.get('campaign_overview') if isinstance(report.get('campaign_overview'), dict) else {}
    def text(value):
        if isinstance(value, str):
            return value
        return '\n'.join(item for item in value if isinstance(item, str)) if isinstance(value, list) else ''
    summary = '\n'.join(text(overview.get(key)) for key in
                        ('summary', 'recommendation', 'what_worked', 'what_didnt', 'learnings'))
    body = report.get('body_markdown') if isinstance(report.get('body_markdown'), str) else ''
    def sentences(text):
        return {re.sub(r'\s+', ' ', sentence).strip().lower()
                for sentence in re.split(r'[\n.!?]+', text)
                if len(re.findall(r'[\w-]+', sentence, re.UNICODE)) >= 15}
    source, local = sentences(summary), sentences(body)
    shared = sorted(source & local)
    ratio = len(shared) / len(source) if source else None
    return {'status': 'substantial_verbatim_overlap' if len(shared) >= 2 and ratio >= .5 else 'not_detected',
            'shared_sentences': len(shared), 'overview_sentences': len(source), 'ratio': ratio,
            'meaning': 'A writing suggestion only: keep cumulative synthesis in the overview and the latest local result in the body; preserve citations and meaningful updates.'}


def project(state, experiments: dict, *, notice_dispositions=None, notice_health=None) -> dict:
    research = [m for m in (state.research or []) if isinstance(m, dict)]
    linked_sources = 0
    claims = 0
    for memo in research:
        sources = {s.get('url') for s in (memo.get('sources') or []) if isinstance(s, dict) and s.get('url')}
        for claim in memo.get('claims') or []:
            if isinstance(claim, dict):
                claims += 1
                linked_sources += len(sources.intersection(claim.get('urls') or []))
    nodes = list(state.nodes.values())
    exp = list(experiments.values())
    report = state.report or {}
    work = {}
    for experiment in exp:
        arms = experiment.get('arms') or {}
        if not isinstance(arms, dict):
            continue
        for arm in arms.values():
            if not isinstance(arm, dict) or arm.get('run_id') not in (None, getattr(state, 'run_id', None)):
                continue
            nid, kind = arm.get('node_id'), arm.get('work_kind')
            if type(nid) is int and kind in ('scientific_experiment', 'fit', 'score_only', 'engineering_recovery'):
                work.setdefault(nid, set()).add(kind)
    engineering_roles = ('prepare', 'profile', 'recovery')
    scientific_roles = ('fit', 'score')
    role_counts: dict[str, int] = {}
    for n in nodes:
        nid = getattr(n, 'id', None)
        role = getattr(n, 'job_role', None)
        if role in engineering_roles or role in scientific_roles:
            role_counts[role] = role_counts.get(role, 0) + 1
            if type(nid) is int:
                work.setdefault(nid, set()).add('engineering_recovery' if role in engineering_roles else role)
    engineering = {nid for nid, kinds in work.items() if kinds == {'engineering_recovery'}}
    applicable_nodes = [n for n in nodes if getattr(n, 'id', None) not in engineering]
    unclassified = [n for n in nodes if getattr(n, 'id', None) not in work]
    empty = report_fields_empty(report)
    notices = notice_dispositions if isinstance(notice_dispositions, list) else None
    counts = None
    if notices is None:
        notice_resolution = 'unknown: notice disposition journal not inspected; check the original receipts and research notices.'
    elif notice_health is False:
        notice_resolution = 'unknown: notice disposition history is incomplete; resolution is not unreviewed.'
    else:
        counts = {key: sum(row.get('disposition') == key for row in notices if isinstance(row, dict))
                  for key in ('reviewed', 'resolved', 'unreviewed')}
        notice_resolution = 'Saved dispositions are self-attested, not automatic proof of correction; transient and pre-existing warnings remain unknown.'
    return {
        'memo_count': len(research),
        'memos_with_sources': sum(bool(m.get('sources')) for m in research),
        'memos_with_reasoning': sum(bool(m.get('reasoning')) for m in research),
        'memos_with_open_questions': sum(bool(m.get('open_questions')) for m in research),
        'structured_claims': claims,
        'claim_source_links': linked_sources,
        'node_count': len(nodes),
        'nodes_with_parent_card': sum(bool(n.idea.parent_card_id) for n in nodes),
        'engineering_nodes_excluded': len(engineering.intersection({getattr(n, 'id', None) for n in nodes})),
        'unclassified_nodes': len(unclassified),
        'job_role_counts': role_counts,
        'question_applicable_or_unknown_nodes': len(applicable_nodes),
        'question_applicable_or_unknown_without_card': sum(not bool(n.idea.parent_card_id) for n in applicable_nodes),
        'notice_resolution': notice_resolution,
        'notice_dispositions': {'status': 'available' if counts is not None else 'unknown', 'counts': counts},
        'suggested_reads': ['/research-map', '/report/versions'],
        'registered_experiments': len(exp),
        'registrations_with_questions': sum(bool(e.get('question')) for e in exp),
        'registrations_with_question_card': sum(bool(e.get('question_card_id')) for e in exp),
        'report_fields_present': [f for f in REPORT_FIELDS if f not in empty],
        'report_fields_empty': empty,
        'report_metric_status': report_metric_status(report),
        'report_repetition': report_repetition(report),
        'research_link_actions': ({
            'question_card': 'Create or choose a direction card, then register future experiments with question_card_id; do not rewrite old preregistration.',
            'node_card': 'Link scientific candidates with parent_card_id and code lineage with parent_ids; declare artifact dependencies separately with uses.',
            'evidence': 'For scientific memos link claims to actual sources or terminal nodes; technical retries may explain why literature is not applicable.',
        } if (not all(n.idea.parent_card_id for n in applicable_nodes)
              or any(not e.get('question_card_id') for e in exp)) else {}),
        'interpretation': 'Counts are descriptive only. A source link does not verify a claim; engineering work may not need literature or a separate question. No quality score or admission gate is implied.',
    }


def review_memo(memo: dict, *, purpose: str) -> dict:
    """Suggest missing links without claiming truth or creating a research gate."""
    issues = []
    if not isinstance(memo, dict):
        raise ValueError('memo must be an object')
    if purpose not in ('scientific', 'technical'):
        raise ValueError('purpose must be scientific or technical')
    if not memo.get('summary'):
        issues.append({'code': 'missing_conclusion', 'field': 'summary'})
    if purpose == 'scientific':
        if not memo.get('reasoning'):
            issues.append({'code': 'missing_reasoning', 'field': 'reasoning'})
        if not memo.get('open_questions'):
            issues.append({'code': 'no_open_question', 'field': 'open_questions'})
        if not (memo.get('next_experiments') or memo.get('recommended_directions')):
            issues.append({'code': 'no_next_direction', 'field': 'next_experiments'})
        source_rows = memo.get('sources') if isinstance(memo.get('sources'), list) else []
        claim_rows = memo.get('claims') if isinstance(memo.get('claims'), list) else []
        sources = {s.get('url') for s in source_rows if isinstance(s, dict) and isinstance(s.get('url'), str) and s.get('url')}
        claims = [c for c in claim_rows if isinstance(c, dict)]
        if sources and not any(sources.intersection(u for u in (c.get('urls') if isinstance(c.get('urls'), list) else []) if isinstance(u, str)) for c in claims):
            issues.append({'code': 'sources_not_linked_to_claim', 'field': 'claims'})
        if not sources and not claims:
            issues.append({'code': 'evidence_absent_or_explain', 'field': 'claims'})
        if any(not (c.get('node_ids') or c.get('urls') or c.get('evidence_ids')) for c in claims):
            issues.append({'code': 'claim_has_no_evidence', 'field': 'claims'})
        linked = {url for c in claims for url in (c.get('urls') if isinstance(c.get('urls'), list) else []) if isinstance(url, str)}
        if sources - linked:
            issues.append({'code': 'some_sources_unlinked', 'field': 'sources'})
    return {'version': 1, 'purpose': purpose, 'issues': issues,
            'meaning': 'Advisory writing feedback only. A link is not scientific verification; technical work may need no literature review. No gate or work is created.'}


def review_experiment(exp: dict) -> list[str]:
    """Contextual authoring suggestions; unknown is not a score or a work gate."""
    from looplab.serve.intake_notices import glued_tokens
    context = exp.get('research_context') if isinstance(exp.get('research_context'), dict) else {}
    arms = [a for a in (exp.get('arms') or {}).values() if isinstance(a, dict)] if isinstance(exp.get('arms'), dict) else []
    kinds = [a.get('work_kind') for a in arms]
    technical = bool(kinds) and all(k == 'engineering_recovery' for k in kinds)
    issues = []
    title = str(exp.get('title') or '').strip()
    if title.lower() in ('experiment', 'test', 'run', 'score', 'эксперимент', 'опыт', 'тест'):
        issues.append('generic_title')
    if any(glued_tokens(value) for value in [title, exp.get('question'), exp.get('hypothesis'), *context.values()] if isinstance(value, str)):
        issues.append('hard_to_read')
    if not technical:
        if not (exp.get('question') or exp.get('question_card_id')):
            issues.append('question_not_recorded')
        if not exp.get('contrasts') and not context.get('control'):
            issues.append('control_not_recorded')
        if not context.get('criterion'):
            issues.append('criterion_not_explained')
        if not context.get('motivation'):
            issues.append('motivation_not_explained')
    return issues
