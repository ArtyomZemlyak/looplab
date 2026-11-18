"""Bounded authored recovery context, never a plan executor or a source of launch authority."""
from __future__ import annotations

import json
import re

from looplab.core.jsonutil import canonical_json_digest
from looplab.core.redact import redact_persisted_text

AUTHOR_FIELDS = frozenset({'goal', 'last_completed', 'next_step', 'blocker', 'harness_revision',
                          'unfinished_artifacts', 'work_claims', 'saved_command_id', 'saved_action_ref', 'work_intents'})
SERVER_FIELDS = frozenset({'version', 'run_uid', 'run_generation', 'context_digest'})
_SHA = re.compile(r'[0-9a-f]{64}')
_COMMAND = re.compile(r'cmd_[0-9a-f]{32}')
MAX_BYTES = 8192


def _text(value, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError('invalid_external_work_text')
    return redact_persisted_text(value.strip(), max_chars=limit, entropy=False, single_line=False)


def normalize_authored(value):
    if not isinstance(value, dict) or set(value) - AUTHOR_FIELDS:
        raise ValueError('invalid_external_work_fields')
    out = {key: _text(value.get(key, ''), cap, key in ('goal', 'next_step'))
           for key, cap in (('goal', 1024), ('last_completed', 1024), ('next_step', 1024),
                            ('harness_revision', 128), ('saved_action_ref', 256))}
    command = value.get('saved_command_id')
    if command is not None and (not isinstance(command, str) or _COMMAND.fullmatch(command) is None):
        raise ValueError('invalid_external_work_command')
    out['saved_command_id'] = command
    blocker = value.get('blocker')
    if blocker is not None:
        if not isinstance(blocker, dict) or set(blocker) != {'owner', 'summary'}:
            raise ValueError('invalid_external_work_blocker')
        blocker = {'owner': _text(blocker['owner'], 128, True), 'summary': _text(blocker['summary'], 1024, True)}
    out['blocker'] = blocker
    artifacts = value.get('unfinished_artifacts', [])
    claims = value.get('work_claims', [])
    if not isinstance(artifacts, list) or len(artifacts) > 8 or not isinstance(claims, list) or len(claims) > 8:
        raise ValueError('invalid_external_work_lists')
    out['work_claims'] = [_text(claim, 256, True) for claim in claims]
    out['unfinished_artifacts'] = []
    for row in artifacts:
        if not isinstance(row, dict) or set(row) - {'path', 'sha256', 'status'}:
            raise ValueError('invalid_external_work_artifact')
        digest = row.get('sha256')
        if digest is not None and (not isinstance(digest, str) or _SHA.fullmatch(digest) is None):
            raise ValueError('invalid_external_work_artifact_digest')
        out['unfinished_artifacts'].append({'path': _text(row.get('path'), 512, True), 'sha256': digest,
                                             'status': _text(row.get('status', 'unverified'), 64, True)})
    if 'work_intents' in value:
        from looplab.core.work_intents import normalize_intents
        out['work_intents'] = normalize_intents(value['work_intents'])
    try:
        if len(json.dumps(out, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) > MAX_BYTES:
            raise ValueError('external_work_state_exceeds_budget')
    except (TypeError, UnicodeError) as exc:
        raise ValueError('invalid_external_work_encoding') from exc
    return out


def context_digest(state):
    """Exact observed direction/lifecycle/objective/budget context; not artifact or harness attestation."""
    return canonical_json_digest({'run_uid': state.run_uid, 'directions': state.pending_hints,
        'nodes': [(n.id, n.attempt, n.status) for n in sorted(state.nodes.values(), key=lambda node: node.id)],
        'objective_key': state.objective_key, 'budget_overrides': state.budget_overrides,
        'pending_strategy': state.pending_strategy})
