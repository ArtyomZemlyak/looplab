"""Bounded, non-authoritative research map over the existing journal and folded events.

No inferred lineage or scientific truth is created: each link cites its native source.
The protocol journal and event prefix are separate sources, not an atomic snapshot.
"""
from __future__ import annotations

from pathlib import Path
import json
from urllib.parse import quote

from fastapi import HTTPException

from looplab.core.redact import redact_persisted_text
from looplab.core.jsonutil import canonical_json_digest
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.campaign import experiments_of, read_journal
from looplab.harness.journals import read_event_source, read_source
from looplab.harness.overview import overview_freshness
from looplab.core.run_ref import run_address
from looplab.harness.map_paging import paginate, request_options, DEFAULT_MAX_BYTES, DEFAULT_MAX_ITEMS


def _text(value, limit=1000):
    return redact_persisted_text(str(value or ''), max_chars=limit, entropy=False, single_line=True)


def _bounded(row: dict, max_bytes: int = 1200) -> dict:
    """Keep a whole small row, or a visibly shortened shape with no invented URLs."""
    def size(value):
        return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    if size(row) <= max_bytes:
        return row
    def short(value, key=''):
        if isinstance(value, str):
            if key in ('url', 'urls') and len(value) > 80:
                return ''  # a truncated link must not look like the original source
            return value if len(value) <= 80 else value[:79] + '…'
        if isinstance(value, list):
            return [short(item, key) for item in value[:2]]
        if isinstance(value, dict):
            return {k: short(v, k) for k, v in value.items()}
        return value
    result = short(row)
    result['detail_truncated'] = True
    if size(result) > max_bytes:
        result = {k: result[k] for k in ('experiment_id', 'memo_id', 'card_id', 'node_id', 'seq', 'published_seq', 'at_node', 'action_id', 'title', 'summary', 'scope_sha256', 'question_card_id', 'question_card_statement', 'quality_issues', 'status', 'durable_status', 'activity', 'freshness', 'kind', 'job_role', 'metric_present', 'extra_metric_keys', 'parent_ids', 'uses', 'created_at', 'eval_started_at', 'completed_at', 'ts') if k in result}
        result['detail_truncated'] = True
    if size(result) > max_bytes:
        # The historical fallback could still exceed its own bound. Keep one whole bounded
        # identity, never a chopped URL or an object that only pretends to fit.
        stub = {'detail_truncated': True, 'item_omitted': True, 'reason': 'row_exceeds_item_bytes'}
        for key in ('experiment_id', 'memo_id', 'card_id', 'node_id', 'seq', 'published_seq', 'action_id'):
            if key in row:
                value = row[key]
                if isinstance(value, str):
                    value = _text(value, 160)
                candidate = {**stub, key: value}
                if size(candidate) <= max_bytes:
                    stub = candidate
                    break
        return stub
    return result


def snapshot(rd: Path, expected_generation: str, offset: int = 0, limit: int = 20, *,
             root: Path | None = None, collections=None, cursors=None,
             max_bytes=DEFAULT_MAX_BYTES, max_items=DEFAULT_MAX_ITEMS) -> dict:
    from looplab.serve.report_history import report_version, report_versions
    # Validate options before scanning source; byte/item budgets apply to the WHOLE response.
    request_options(collections, cursors)
    limit = min(limit, 20)
    events = read_event_source(rd)
    generation = run_generation_token(events)
    if not generation or generation != expected_generation.lower():
        raise HTTPException(409, 'run generation changed')
    state = fold(events)
    canonical_id = run_address(root if root is not None else rd.parent, rd)
    from looplab.events.eval_occupancy import withheld_lifecycles
    from looplab.serve.node_activity import public_node_activity
    withheld = withheld_lifecycles(events)
    activities = {nid: public_node_activity(state, nid, withheld=withheld) for nid in state.nodes}
    rows, health = read_journal(rd)
    experiments = experiments_of(rows, state.run_uid)
    from looplab.harness.research_quality import review_experiment
    items = []
    for eid, exp in experiments.items():
        arms = []
        for name, arm in (exp.get('arms') or {}).items():
            if not isinstance(arm, dict):
                continue
            nid = arm.get('node_id')
            local = arm.get('run_id') in (None, '', canonical_id)
            node = state.nodes.get(nid) if type(nid) is int and local else None
            activity = activities.get(nid) if node is not None else None
            arms.append({'name': _text(name, 128), 'node_id': nid,
                         'run_id': _text(arm.get('run_id'), 256) or None,
                         'node_status': activity['status'] if activity else None,
                         'node_durable_status': getattr(node, 'status', None), 'node_activity': activity,
                         'attempt': getattr(node, 'attempt', None),
                         'description': _text(arm.get('description'), 500), 'work_kind': _text(arm.get('work_kind'), 64) or None})
        scope = {k: v for k, v in exp.items() if k not in ('registration', 'superseded')}
        items.append({'scope_sha256': canonical_json_digest(scope), 'experiment_id': _text(eid, 128), 'title': _text(exp.get('title'), 400),
                      'question': _text(exp.get('question')), 'hypothesis': _text(exp.get('hypothesis')),
                      'quality_issues': review_experiment(exp),
                      'question_card_id': _text(exp.get('question_card_id'), 160) or None,
                      'question_card_statement': _text(state.cards[exp['question_card_id']].statement, 500)
                      if exp.get('question_card_id') in state.cards else '',
                      'primary_metric': _text(exp.get('primary_metric'), 128), 'arms': arms[:32],
                      'research_context': {k: _text(v, 500) for k, v in (exp.get('research_context') if isinstance(exp.get('research_context'), dict) else {}).items()
                                           if k in ('change', 'motivation', 'data_weights', 'control', 'criterion', 'alternatives')},
                      'tests': [{k: _text(t.get(k), 180) for k in ('label', 'test_id', 'description')}
                                for t in (exp.get('tests') or [])[:16] if isinstance(t, dict)],
                      'metrics': [{k: _text(m.get(k), 180) for k in ('name', 'kind', 'direction', 'description')}
                                  for m in (exp.get('metrics') or [])[:32] if isinstance(m, dict)],
                      'contrasts': [{k: _text(c.get(k), 180) for k in ('id', 'control', 'treatment', 'metric', 'test', 'family')}
                                    for c in (exp.get('contrasts') or [])[:32] if isinstance(c, dict)],
                      'arms_total': len(arms), 'contrasts_total': len(exp.get('contrasts') or []),
                      'relationships': [{**{k: r.get(k) for k in ('node_id', 'node_generation', 'artifact_sha256')},
                                         **{k: _text(r.get(k), 500) for k in ('kind', 'subject_arm', 'run_id', 'explanation')}}
                                        for r in (exp.get('relationships') or [])[:32] if isinstance(r, dict)],
                      'superseded': bool(exp.get('superseded')), 'supersedes': _text(exp.get('supersedes'), 128) or None,
                      'registration': {k: exp.get('registration', {}).get(k) for k in ('amendment_id', 'version', 'recorded_at', 'at_event_seq', 'at_node_count', 'generation')},
                      'comparison': f'/api/runs/{quote(canonical_id, safe="")}/experiments/{quote(eid, safe="")}/comparison'})
    work_kinds = {}
    node_exp_arms: dict[int, list[dict]] = {}
    card_exps: dict[str, list[str]] = {}
    for eid, exp in experiments.items():
        q_cid = exp.get('question_card_id')
        if isinstance(q_cid, str) and q_cid:
            card_exps.setdefault(q_cid, []).append(_text(eid, 128))
        for arm_name, arm in (exp.get('arms') or {}).items():
            if not isinstance(arm, dict) or arm.get('run_id') not in (None, '', canonical_id):
                continue
            nid, kind = arm.get('node_id'), arm.get('work_kind')
            if type(nid) is int:
                node_exp_arms.setdefault(nid, []).append({'experiment_id': _text(eid, 128), 'arm': _text(arm_name, 128)})
                if kind in ('scientific_experiment', 'fit', 'score_only', 'engineering_recovery'):
                    work_kinds.setdefault(nid, set()).add(kind)
    memo_citations: dict[int, list[str]] = {}
    for memo in state.research or []:
        if not isinstance(memo, dict):
            continue
        mid = _text(memo.get('memo_id'), 128)
        if not mid:
            continue
        cited_nids: set[int] = set()
        for lesson in (memo.get('local_lessons') or []):
            if isinstance(lesson, dict):
                cited_nids.update(n for n in (lesson.get('node_ids') or []) if type(n) is int)
        for claim in (memo.get('claims') or []):
            if isinstance(claim, dict):
                cited_nids.update(n for n in (claim.get('node_ids') or []) if type(n) is int)
        for nid in sorted(cited_nids):
            memo_citations.setdefault(nid, []).append(mid)
    from looplab.harness.per_query import current_uploads, read_manifest
    upload_rows, upload_health = read_manifest(rd)
    upload_rows = [r for r in upload_rows if r.get('run_uid') == state.run_uid and r.get('generation') == generation]
    current_upload_list = list(current_uploads(upload_rows, state))
    node_uploads: dict[int, list[str]] = {}
    for u in current_upload_list:
        unid = u.get('node_id')
        uid_str = _text(u.get('upload_id'), 128)
        if type(unid) is int and uid_str:
            node_uploads.setdefault(unid, []).append(uid_str)
    card_nodes: dict[str, list[int]] = {}
    nodes = []
    for node in sorted(state.nodes.values(), key=lambda n: n.id):
        idea = node.idea
        if idea.parent_card_id:
            card_nodes.setdefault(idea.parent_card_id, []).append(node.id)
        nodes.append({'node_id': node.id, 'attempt': node.attempt, 'status': activities[node.id]['status'],
                      'durable_status': node.status, 'activity': activities[node.id],
                      'title': _text(node.operator, 256), 'hypothesis': _text(idea.hypothesis),
                      'rationale': _text(idea.rationale), 'parent_card_id': _text(idea.parent_card_id, 256) or None,
                      'parent_card_statement': _text(state.cards[idea.parent_card_id].statement, 500) if idea.parent_card_id in state.cards else '',
                      'parent_ids': list(node.parent_ids or []), 'uses': list(node.uses or []),
                      'kind': node.kind, 'job_role': node.job_role,
                      'created_at': node.created_at, 'eval_started_at': node.eval_started_at,
                      'completed_at': node.completed_at,
                      'concepts': list(state.node_concepts.get(node.id, [])),
                      'questions': [_text(x) for x in (idea.open_questions or [])[:16]],
                      'declared_work_kinds': sorted(work_kinds.get(node.id, set())),
                      'experiment_arms': node_exp_arms.get(node.id, [])[:16],
                      'citing_memo_ids': memo_citations.get(node.id, [])[:16],
                      'upload_ids': node_uploads.get(node.id, [])[:16],
                      'metric_present': node.metric is not None,
                      'extra_metric_keys': list((node.extra_metrics or {}).keys())[:64]})
    cards = [{'card_id': _text(card.id, 160), 'kind': _text(card.card_kind, 64),
              'statement': _text(card.statement), 'status': _text(card.status, 64),
              'verdict': _text(card.verdict, 64), 'parent_card_id': _text(card.parent_card_id, 160) or None,
              'evidence': list(card.evidence or [])[:32], 'discarded_nodes': list(card.discarded_nodes or [])[:32],
              'linked_experiment_ids': card_exps.get(card.id, [])[:32],
              'linked_node_ids': card_nodes.get(card.id, [])[:32]}
             for card in state.cards.values()]
    memos = []
    for memo in state.research or []:
        if not isinstance(memo, dict):
            continue
        source_rows = memo.get('sources') if isinstance(memo.get('sources'), list) else []
        claims = memo.get('claims') if isinstance(memo.get('claims'), list) else []
        verdicts = (memo.get('verification') or {}).get('verdicts', []) if isinstance(memo.get('verification'), dict) else []
        memos.append({'memo_id': _text(memo.get('memo_id'), 128), 'at_node': memo.get('at_node'),
                      'summary': _text(memo.get('summary')), 'reasoning': _text(memo.get('reasoning'), 2000),
                      'local_lessons': [{'statement': _text(x.get('statement')), 'limitation': _text(x.get('limitation')),
                                         'status': _text(x.get('status'), 64), 'node_ids': x.get('node_ids', [])}
                                        for x in (memo.get('local_lessons') or [])[:32] if isinstance(x, dict)],
                      'questions': [_text(q) for q in (memo.get('open_questions') or [])[:16]],
                      'sources': [{'title': _text(src.get('title'), 500), 'url': _text(src.get('url'), 2000)}
                                  for src in source_rows[:32] if isinstance(src, dict)],
                      'claims': [{'statement': _text(c.get('statement')), 'node_ids': [n for n in (c.get('node_ids') or []) if type(n) is int][:32],
                                  'urls': [_text(u, 2000) for u in (c.get('urls') or [])[:32]],
                                  'author_status': _text(c.get('author_status'), 64), 'limitation': _text(c.get('limitation')),
                                  'contrast_ref': _text(c.get('contrast_ref'), 200),
                                  'verdict': next((_text(v.get('verdict'), 64) for v in verdicts
                                                   if isinstance(v, dict) and v.get('statement') == c.get('statement')), '')}
                                 for c in claims[:32] if isinstance(c, dict)]})
    decisions, decision_health = read_source(rd / 'harness_decisions.jsonl')
    decisions = [{'phase': _text(d.get('phase_id'), 64), 'decision': _text(d.get('decision'), 64),
                  'at_node': d.get('at_node'), 'reason': _text(d.get('reason')), 'action_id': _text(d.get('action_id'), 128),
                  'prior_node_ids': [n for n in (d.get('prior_node_ids') if isinstance(d.get('prior_node_ids'), list) else []) if type(n) is int][:32],
                  **{k: _text(d.get(k), 500) for k in ('alternatives_summary', 'information_gain', 'not_applicable_reason')}}
                 for d in decisions if d.get('run_uid') == state.run_uid and d.get('generation') == generation]
    reviews, review_health = read_source(rd / 'harness_reviews.jsonl')
    reviews = [{'phase': _text(r.get('phase_id'), 64), 'decision': _text(r.get('decision'), 64),
                'at_node': r.get('at_node'), 'reason': _text(r.get('reason')), 'action_ref': _text(r.get('action_ref'), 160),
                'action_id': _text(r.get('action_id'), 160), 'evidence': [n for n in (r.get('evidence') if isinstance(r.get('evidence'), list) else []) if type(n) is int][:32]}
               for r in reviews if r.get('run_uid') == state.run_uid and r.get('generation') == generation]
    uploads = []
    for upload in current_upload_list:
        consistency = upload.get('consistency') if isinstance(upload.get('consistency'), dict) else {}
        metric_rows = upload.get('metrics') if isinstance(upload.get('metrics'), dict) else {}
        target_node = state.nodes.get(upload.get('node_id'))
        c_status = consistency.get('status') or ''
        if c_status in ('consistent', 'inconsistent'):
            scalar_app = 'checked'
        elif target_node is not None and target_node.kind == 'artifact' and target_node.metric is None:
            scalar_app = 'not_applicable_artifact'
        else:
            scalar_app = 'unchecked'
        uploads.append({'upload_id': _text(upload.get('upload_id'), 128), 'node_id': upload.get('node_id'),
                        'attempt': upload.get('node_generation'), 'stale': upload.get('stale'),
                        'test_id': _text(upload.get('test_id'), 128), 'experiment_id': _text(upload.get('experiment_id'), 128),
                        'arm': _text(upload.get('arm'), 128), 'n_queries': upload.get('n_queries'),
                        'metrics': [_text(k, 160) for k in list(metric_rows)[:32] if isinstance(k, str)],
                        'scalar_applicability': scalar_app,
                        'consistency': {k: _text(consistency.get(k), 160) for k in ('status', 'metric', 'node_metric')}})
    versions = report_versions(events, current_seq=(state.report or {}).get('published_seq'), max_versions=None)
    overview = None
    for event in reversed(events):
        data = event.data if isinstance(event.data, dict) else {}
        raw = data.get('content') if isinstance(data.get('content'), dict) else data
        if event.type == 'report_generated' and raw.get('campaign_overview'):
            content = report_version(events, event.seq)
            if content and content.get('campaign_overview'):
                overview = {'published_seq': event.seq, 'at_node': content.get('at_node'),
                            'content': content['campaign_overview']}
                break
    if overview:
        overview['freshness'] = overview_freshness(events, overview['published_seq'], overview['at_node'], len(nodes))
    from looplab.harness.progress import _operator_directions
    from looplab.harness.research_quality import project as research_quality
    notice_path = rd / 'research_notice_dispositions.jsonl'
    notice_dispositions, notice_health = (([], {'read_complete': False}) if notice_path.is_symlink()
                                         else read_source(notice_path))
    notice_complete = (notice_health.get('read_complete') is True
                       and all(isinstance(row, dict) for row in notice_dispositions))
    notice_dispositions = [row for row in notice_dispositions
                           if isinstance(row, dict) and row.get('generation') == generation]
    metadata = {'version': 1, 'run_id': canonical_id, 'run_uid': state.run_uid, 'generation': generation,
            'event_seq': events[-1].seq, 'goal': _text(state.goal, 2000),
            'operator_directions': _operator_directions(state, events=events, run_id=canonical_id, generation=generation),
            'quality': research_quality(state, experiments, notice_dispositions=notice_dispositions,
                                        notice_health=notice_complete),
            'source_health': {'protocol': health, 'decisions': decision_health, 'reviews': review_health, 'uploads': upload_health, 'atomic_across_sources': False},
            'campaign_overview': _bounded(overview, 4096) if overview else None,
            'notes': ['The protocol and event journal are independent reads; refresh before action.',
                      'Links are declarations, not proof of a measured result or execution permission.',
                      'Research questions in registrations are distinct from filed board cards.',
                      'Node activity is recorded lifecycle evidence, not proof the engine is currently running; check run progress.']}

    rows = {'uploads': [_bounded(x) for x in reversed(uploads)],
            'cards': [_bounded(x) for x in cards],
            'research_memos': [_bounded(x) for x in reversed(memos)],
            'reviews': [_bounded(x) for x in reversed(reviews)],
            'decisions': [_bounded(x) for x in reversed(decisions)],
            'experiments': [_bounded(x, 4096) for x in items],
            'nodes': [_bounded(x) for x in nodes],
            'reports': [_bounded(x) for x in versions['versions']]}
    return paginate(metadata, rows, offset=offset, limit=limit, collections=collections,
                    cursors=cursors, max_bytes=max_bytes, max_items=max_items)
