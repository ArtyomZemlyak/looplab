"""Authored scientific work declarations: bounded data, never a lease or execution permission."""
from __future__ import annotations

import re
import json
from hashlib import sha256

from looplab.core.hint_directives import operator_directive_rows
from looplab.core.redact import redact_persisted_text

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}')
_SHA = re.compile(r'[0-9a-f]{64}')
_OPERATIONS = frozenset({'prepare', 'profile', 'fit', 'score', 'analysis', 'report', 'other'})
_DISPOSITIONS = frozenset({'acknowledged', 'following', 'deferred', 'conflict'})
_FIELDS = frozenset({'id', 'topic', 'operation', 'subject_sha256', 'status', 'summary', 'direction_refs'})


def _id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError('invalid_work_intent_identity')
    return value


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('invalid_work_intent_text')
    return redact_persisted_text(value.strip(), max_chars=limit, entropy=False, single_line=False)


def normalize_intents(value):
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError('invalid_work_intents')
    result, ids = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) - _FIELDS:
            raise ValueError('invalid_work_intent_fields')
        key, topic = _id(row.get('id')), _id(row.get('topic'))
        if key in ids:
            raise ValueError('duplicate_work_intent_id')
        ids.add(key)
        operation, status = row.get('operation'), row.get('status')
        digest = row.get('subject_sha256')
        if (not isinstance(operation, str) or operation not in _OPERATIONS
                or status not in ('active', 'completed', 'cancelled')
                or not isinstance(digest, str) or _SHA.fullmatch(digest) is None):
            raise ValueError('invalid_work_intent_scope')
        refs = row.get('direction_refs', [])
        if not isinstance(refs, list) or len(refs) > 4:
            raise ValueError('invalid_work_direction_refs')
        normalized, seen = [], set()
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {'event_seq', 'text_sha256', 'disposition', 'summary'}:
                raise ValueError('invalid_work_direction_ref')
            seq, text_digest = ref.get('event_seq'), ref.get('text_sha256')
            if (type(seq) is not int or seq < 0 or seq in seen
                    or not isinstance(text_digest, str) or _SHA.fullmatch(text_digest) is None
                    or not isinstance(ref.get('disposition'), str) or ref['disposition'] not in _DISPOSITIONS):
                raise ValueError('invalid_work_direction_identity')
            seen.add(seq)
            normalized.append({'event_seq': seq, 'text_sha256': text_digest,
                               'disposition': ref['disposition'], 'summary': _text(ref['summary'], 256)})
        result.append({'id': key, 'topic': topic, 'operation': operation, 'subject_sha256': digest,
                       'status': status, 'summary': _text(row.get('summary'), 512), 'direction_refs': normalized})
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) > 8192:
        raise ValueError('work_intents_exceed_budget')
    return result


def validate_direction_refs(intents, events):
    """Cite actual operator text, not a model advisory or a guessed sequence.

    Historical references are allowed and remain historical. This validates content identity,
    never comprehension, authority, agreement, checkpoint resolution or scientific completion.
    """
    wanted = {(ref['event_seq'], ref['text_sha256']) for row in intents for ref in row['direction_refs']}
    sequences = {seq for seq, _ in wanted}
    if not wanted:
        return
    found = set()
    for event in events:
        if event.seq not in sequences or event.type != 'hint':
            continue
        rows = operator_directive_rows([event.data])
        if rows:
            found.add((event.seq, sha256(str(rows[0]['text']).strip().encode()).hexdigest()))
    if not wanted <= found:
        raise ValueError('work_direction_source_mismatch')
