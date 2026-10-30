"""Portable, bounded report bundle assembled from existing sanitized run evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from looplab.events.run_generation import run_generation_token
from looplab.core.metric_profile import profile_for_run
from looplab.harness.journals import read_event_source
from looplab.harness.research_map import snapshot


def export(rd: Path, generation: str, selected: list[int], question_card_id: str | None = None) -> dict:
    from looplab.serve.report_history import report_version, report_digest
    if len(selected) > 10 or len(set(selected)) != len(selected) or any(type(s) is not int or s < 0 for s in selected):
        raise HTTPException(400, 'select at most ten distinct report sequences')
    research = snapshot(rd, generation, limit=100)
    events = read_event_source(rd)
    if run_generation_token(events) != generation:
        raise HTTPException(409, 'run generation changed')
    reports = []
    for seq in selected:
        content = report_version(events, seq)
        if content is None:
            raise HTTPException(404, f'report version {seq} unavailable')
        reports.append(content)
    direction = None
    if question_card_id is not None:
        from looplab.events.replay import fold
        from looplab.harness.campaign import experiments_of, read_journal
        from looplab.harness.research_map import _text
        state = fold(events)
        card = state.cards.get(question_card_id)
        if card is None:
            raise HTTPException(404, 'question card unavailable')
        rows, health = read_journal(rd)
        if not health.get('read_complete'):
            raise HTTPException(409, 'protocol history incomplete; direction export unavailable')
        matches = [e for e in experiments_of(rows, state.run_uid).values()
                   if e.get('question_card_id') == question_card_id]
        direction = {'card_id': question_card_id, 'statement': _text(card.statement),
                     'kind': _text(card.card_kind, 64), 'status': _text(card.status, 64),
                     'matched_registrations': len(matches)}
        direction_experiments = [{**{k: _text(e.get(k), 1000) for k in ('title', 'question', 'hypothesis', 'primary_metric')},
                                  'registration': e.get('registration') or {}} for e in matches[:100]]
    lines = ['# Research campaign export', '', f"Run: {research['run_id']}",
             f"Generation: {generation}", f"Event prefix: {events[-1].seq}", '',
             research['goal']]
    profile = profile_for_run(rd.parent, rd.name)
    if profile.get('state') == 'configured':
        value = profile['profile']
        lines += ['', f"## Project metric profile at export: {value['name']}",
                  'Author-declared presentation settings; not evidence of a measured value.']
        for metric in value['metrics']:
            lines.append(f"- {metric['label'] or metric['key']} ({metric['key']}, {metric['role']}): {metric['denominator'] or 'denominator not recorded'}")
        if value['slices']:
            lines += ['', 'Requested slices: ' + ', '.join(value['slices'])]
    overview = research.get('campaign_overview')
    if overview:
        content = overview.get('content') or {}
        lines += ['', f"## Campaign overview (report {overview['published_seq']})", '', content.get('summary') or '']
        for key in ('recommendation', 'recommendation_limits'):
            if content.get(key):
                lines += ['', f'### {key}', content[key]]
        if content.get('report_seqs'):
            lines += ['', 'Related report versions: ' + ', '.join(f'#{n}' for n in content['report_seqs'])]
        for key in ('what_worked', 'what_didnt', 'learnings', 'next_directions', 'caveats'):
            if content.get(key):
                lines += ['', f'### {key}'] + [f'- {value}' for value in content[key]]
        lines += ['', '## Registered questions']
    if not overview:
        lines += ['', '## Registered questions']
    if direction is not None:
        lines += ['', f"## Direction: {direction['statement']}",
                  f"Card: {direction['card_id']} · {direction['kind']} · {direction['status']}",
                  'Only registrations explicitly linked to this card are included.']
    selected_experiments = direction_experiments if direction is not None else research['experiments']['items']
    for exp in selected_experiments:
        lines += ['', f"### {exp.get('title', '')}", exp.get('question', ''), '', exp.get('hypothesis', ''),
                  '', f"Primary metric: {exp.get('primary_metric', 'not available')}",
                  f"Registration: {(exp.get('registration') or {}).get('amendment_id') or 'unknown'}"]
    if (direction is not None and direction['matched_registrations'] > len(direction_experiments)) or (direction is None and research['experiments']['has_more']):
        lines += ['', 'Additional registrations are available through the paginated research map.']
    manifest = []
    for content in reports:
        seq = content['published_seq']
        lines += ['', f'## Report version {seq}', f'Content SHA-256: {report_digest(content)}', '', content.get('headline') or '', '',
                  content.get('summary') or '', '', content.get('verdict') or '', '',
                  content.get('body_markdown') or '']
        if content.get('champion_summary'):
            lines += ['', '### Selected result', content['champion_summary']]
        refs = content.get('key_metrics') or {}
        if refs.get('metrics') or refs.get('nodes'):
            lines += ['', '### Metric references (values not embedded)']
            lines += [f"- {m.get('label') or m.get('key')} ({m.get('key')})" for m in refs.get('metrics', [])]
            lines += [f"- node #{n.get('node_id')}" + (f" attempt {n['node_generation']}" if n.get('node_generation') is not None else '')
                      for n in refs.get('nodes', [])]
        for key in ('what_worked', 'what_didnt', 'learnings', 'next_directions', 'caveats'):
            values = content.get(key) or []
            if values:
                lines += ['', f'### {key}'] + [f'- {value}' for value in values]
        figures = [f.get('figure_id') for f in content.get('figures') or [] if f.get('figure_id')]
        root = f'/api/runs/{quote(research["run_id"], safe="")}'
        figure_refs = [{'figure_id': fid, 'sha256': fid, 'api': f'{root}/report-figures/{fid}'} for fid in figures]
        if figure_refs:
            lines += ['', '### Figure references'] + [f'- [{ref["figure_id"]}]({ref["api"]}) (SHA-256 {ref["sha256"]})' for ref in figure_refs]
        manifest.append({'report_seq': seq, 'figures': figures, 'figure_refs': figure_refs,
                         'report_api': f'{root}/report/versions/{seq}', 'content_sha256': report_digest(content)})
    publication = (profile.get('profile') or {}).get('publication') if profile.get('state') == 'configured' else None
    adapter = ({'state': 'configured', **publication, 'operation': 'manual download only'}
               if publication else {'state': 'unconfigured'})
    markdown = '\n'.join(lines).strip() + '\n'
    if len(markdown.encode('utf-8')) > 192 * 1024:
        raise HTTPException(413, 'Export exceeds the response limit; select fewer reports.')
    result = {'version': 1, 'run_id': research['run_id'], 'run_uid': research['run_uid'],
            'generation': generation, 'event_seq': events[-1].seq, 'markdown': markdown,
            'markdown_sha256': hashlib.sha256(markdown.encode()).hexdigest(), 'reports': manifest, 'metric_profile': profile, 'direction': direction, 'publication_adapter': adapter,
            'limits': 'Figures are references, not copied bytes; sources are independently read, not an atomic snapshot.'}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > 240 * 1024:
        raise HTTPException(413, 'Export exceeds the response limit; select fewer reports.')
    return result
