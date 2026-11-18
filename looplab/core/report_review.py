"""Bounded report-review data and evidence identity; opinions never become measurements.

No model, store, tool or command dependency. The serving layer owns paid review receipts;
the existing permission-gated command service owns any separately approved nomination.
"""
from __future__ import annotations

import math
from typing import Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from looplab.core.jsonutil import canonical_json_digest

MAX_REVIEW_NODES = 100
MAX_REVIEW_BYTES = 128 * 1024
TEXT_FIELDS = ('headline', 'summary', 'verdict', 'champion_summary', 'body_markdown',
    'what_worked', 'what_didnt', 'learnings', 'next_directions', 'caveats')
NOMINATION_KINDS = ('none', 'confirm', 'ablation', 'comparison')


class ReviewComment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    report_field: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=400)
    comment: str = Field(min_length=1, max_length=1200)
    node_ids: list[StrictInt] = Field(default_factory=list, max_length=8)
    nomination: Literal['none', 'confirm', 'ablation', 'comparison'] = 'none'
    target_node_id: StrictInt | None = None
    rationale: str = Field(default='', max_length=1200)


class ReportReviewOut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str = Field(min_length=1, max_length=1500)
    comments: list[ReviewComment] = Field(default_factory=list, max_length=5)


def report_texts(report: dict) -> dict[str, str]:
    out = {}
    for field in TEXT_FIELDS:
        value = report.get(field)
        if isinstance(value, str) and value:
            out[field] = value
        elif isinstance(value, list):
            for index, text in enumerate(value[:32]):
                if isinstance(text, str) and text:
                    out[f'{field}.{index}'] = text
    return out


def review_input(state, *, generation: str) -> dict:
    """All current native node facts within an explicit bound, plus exact report text.

    No stdout, code, files, prompts, client configuration or arbitrary provenance dictionaries.
    Code/idea digests invalidate a nomination if its subject changes without an outcome change.
    """
    report = state.report
    if (not isinstance(report, dict) or type(report.get('published_seq')) is not int
            or report['published_seq'] < 0 or not report_texts(report)):
        raise ValueError('a published native report with exact text/sequence is required')
    if len(state.nodes) > MAX_REVIEW_NODES:
        raise ValueError('report review supports at most100 native nodes; no population is silently omitted')
    nodes = []
    for node in sorted(state.nodes.values(), key=lambda n: n.id):
        nodes.append({'node_id': node.id, 'attempt': node.attempt, 'status': str(node.status.value),
            'parent_ids': list(node.parent_ids), 'uses': list(node.uses),
            'operator': node.operator, 'protocol_version': node.protocol_version,
            'terminal_event_seq': node.terminal_event_seq,
            'metric': node.metric, 'task_metric': node.task_metric,
            'feasible': node.feasible, 'tombstoned': node.tombstoned,
            'aborted': node.id in state.aborted_nodes,
            'confirmed_mean': node.confirmed_mean, 'confirmed_std': node.confirmed_std,
            'confirmed_seeds': node.confirmed_seeds, 'holdout_metric': node.holdout_metric,
            'metric_salvaged': (node.metric_provenance or {}).get('salvaged') is True,
            'code_sha256': canonical_json_digest({'code': node.code, 'files': node.files,
                'deleted': node.deleted}),
            'idea_sha256': canonical_json_digest(node.idea.model_dump(mode='json'))})
    # Keep the sanitizer's full original content identity, not only the quoted-text slice.
    content = {key: value for key, value in report.items()
               if key not in ('at_node', 'trigger', 'published_seq', 'published_at')}
    report_digest = canonical_json_digest(content)
    data = {'version': 1, 'run_uid': state.run_uid, 'generation': generation,
        'report_seq': report['published_seq'], 'report_digest': report_digest,
        'report_texts': report_texts(report), 'goal': state.goal, 'direction': state.direction,
        'report_key_metrics': report.get('key_metrics', {}),
        'objective_key': state.objective_key, 'best_node_id': state.best_node_id,
        'trust_gate': state.trust_gate,
        'operator_directions': [{key: row[key] for key in ('text', 'directive_id', 'topic', 'status') if key in row}
            for row in state.pending_hints],
        'budget_overrides': state.budget_overrides, 'reward_hack_node_ids': sorted({row['node_id']
            for row in state.reward_hacks if type(row.get('node_id')) is int}),
        'trust_evidence_sha256': canonical_json_digest({'reward_hacks': state.reward_hacks,
            'leakage': state.leakage, 'drifts': state.drifts}),
        'nodes': nodes,
        'meaning': 'Native scalar facts, not proof of causal improvement. Report key metrics are authored claims, not new native measurements. Per-query arrays and external evaluator artifacts are not inspected here: a missing scalar does not mean no quality evaluation exists. Null remains unknown. Legacy null protocol versions do not prove comparability. Reviewer opinions grant no execution or score authority.'}
    # Optional additive context preserves exact original v1 receipts from before this diagnostic
    # existed. New observations bind later reviews and make prior advice historical, never paid again.
    if state.metric_behavior_scans:
        from looplab.core.metric_behavior import current_metric_behavior_scans
        data['metric_behavior_evidence_sha256'] = canonical_json_digest(state.metric_behavior_scans)
        data['metric_behavior_observations'] = current_metric_behavior_scans(state)[-3:]
    if len(orjson.dumps(data)) > MAX_REVIEW_BYTES:
        raise ValueError('report review evidence exceeds128KiB; no silent truncation')
    data['input_digest'] = canonical_json_digest(data)
    return data


def bind_review(data: dict, authored: ReportReviewOut) -> dict:
    """Attach only server-supplied exact outcomes; reject invented citations and targets."""
    nodes = {row['node_id']: row for row in data['nodes']}
    if not authored.summary.strip():
        raise ValueError('a review needs a nonempty summary')
    comments = []
    for index, comment in enumerate(authored.comments):
        if not comment.quote.strip() or not comment.comment.strip():
            raise ValueError('review quote and comment cannot be whitespace-only')
        if comment.quote not in data['report_texts'].get(comment.report_field, ''):
            raise ValueError('review quote is not an exact span of the named published report field')
        if len(set(comment.node_ids)) != len(comment.node_ids) or any(nid not in nodes for nid in comment.node_ids):
            raise ValueError('review evidence must name distinct recorded native nodes')
        target = nodes.get(comment.target_node_id)
        if comment.nomination != 'none':
            if (target is None or comment.target_node_id not in comment.node_ids
                    or target['status'] != 'evaluated' or target['metric'] is None
                    or not math.isfinite(target['metric'])
                    or not target['feasible'] or target['tombstoned'] or target['aborted']
                    or target['metric_salvaged'] or not comment.rationale.strip()):
                raise ValueError('nomination requires a cited live measured feasible outcome and explicit rationale')
        elif comment.target_node_id is not None:
            raise ValueError('a comment without a nomination cannot carry an execution target')
        from looplab.core.redact import redact_secrets
        row = comment.model_dump(mode='json')
        for field in ('comment', 'rationale'):
            row[field] = redact_secrets(row[field])
        row['outcomes'] = [nodes[nid] for nid in comment.node_ids]
        row['nomination_id'] = (canonical_json_digest({'input_digest': data['input_digest'],
            'index': index, 'comment': row}) if comment.nomination != 'none' else None)
        comments.append(row)
    result = {'version': 1, 'input_digest': data['input_digest'],
        'report_seq': data['report_seq'], 'report_digest': data['report_digest'],
        'run_uid': data['run_uid'], 'generation': data['generation'],
        'summary': redact_secrets(authored.summary), 'comments': comments,
        'reviewer': {'role': 'independent_report_review', 'separate_context': True,
            'mutation_tools': False, 'provider_independence': 'not_attested',
            'verdict_authority': 'advisory_only'},
        'meaning': 'Separate reviewer context, not a new measured result. Every nomination requires a separate permission card and fresh native evidence. Nothing executes from this receipt.'}
    if len(orjson.dumps(result)) > 64 * 1024:
        raise ValueError('report-review receipt exceeds64KiB')
    return result


def nomination_direction(receipt: dict, nomination_id: str) -> dict:
    """A structured, retractable research nomination; never an evaluation/approval grant."""
    comment = next((row for row in receipt.get('comments', [])
                    if row.get('nomination_id') == nomination_id), None)
    if comment is None or comment['nomination'] not in NOMINATION_KINDS[1:]:
        raise ValueError('no such executable-free research nomination')
    target = next(row for row in comment['outcomes'] if row['node_id'] == comment['target_node_id'])
    text = (f"Report review nomination ({comment['nomination']}); report seq{receipt['report_seq']}, "
        f"node{target['node_id']}/attempt{target['attempt']}, original metric={target['metric']}. "
        f"{comment['rationale']}\n"
        "Approved for research consideration only. This does not authorize or launch a repeat, "
        "fit, evaluation or ablation; normal scientific scope, protocol, budget and execution gates apply.")
    if len(text) > 2000:
        raise ValueError('research nomination exceeds the native direction bound')
    return {'text': text, 'directive_id': 'review-' + nomination_id,
        'topic': 'review-' + nomination_id, 'status': 'active', 'supersedes': [],
        'review_nomination': {'nomination_id': nomination_id,
            'input_digest': receipt['input_digest'], 'generation': receipt['generation'],
            'report_seq': receipt['report_seq']}}


def review_is_current(data: dict, receipt: dict) -> bool:
    """A saved receipt must reproduce exactly from its original opinions and current native facts."""
    try:
        authored = ReportReviewOut.model_validate({'summary': receipt['summary'], 'comments': [
            {field: row[field] for field in ReviewComment.model_fields}
            for row in receipt['comments']]})
        return bind_review(data, authored) == receipt
    except (ValueError, KeyError, TypeError):
        return False
