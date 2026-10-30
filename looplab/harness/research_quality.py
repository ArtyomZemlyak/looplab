"""Advisory research coverage facts, not an admission gate or truth verifier."""
from __future__ import annotations


def project(state, experiments: dict) -> dict:
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
    engineering = {nid for nid, kinds in work.items() if kinds == {'engineering_recovery'}}
    applicable_nodes = [n for n in nodes if getattr(n, 'id', None) not in engineering]
    unclassified = [n for n in nodes if getattr(n, 'id', None) not in work]
    fields = ('verdict', 'champion_summary', 'what_worked', 'what_didnt', 'learnings', 'next_directions', 'caveats', 'key_metrics')
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
        'question_applicable_or_unknown_nodes': len(applicable_nodes),
        'question_applicable_or_unknown_without_card': sum(not bool(n.idea.parent_card_id) for n in applicable_nodes),
        'notice_resolution': 'unknown: receipts retain intake categories but no universal acknowledgement ledger exists',
        'suggested_reads': ['/research-map', '/report/versions'],
        'registered_experiments': len(exp),
        'registrations_with_questions': sum(bool(e.get('question')) for e in exp),
        'registrations_with_question_card': sum(bool(e.get('question_card_id')) for e in exp),
        'report_fields_present': [f for f in fields if report.get(f)],
        'report_fields_empty': [f for f in fields if not report.get(f)],
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
