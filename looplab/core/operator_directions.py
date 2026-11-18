"""Pure scoped operator-direction transitions; event order, never notification time, wins."""
from __future__ import annotations

import re

FIELDS = frozenset({'directive_id', 'topic', 'status', 'supersedes'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}')


def is_scoped(data):
    return isinstance(data, dict) and bool(FIELDS.intersection(data))


def transition_error(data, ledger):
    """Return a fixed (HTTP status, code) refusal; shared by intake, append and replay."""
    if (not all(isinstance(data.get(k), str) and _ID.fullmatch(data[k]) for k in ('directive_id', 'topic'))
            or data.get('status', 'active') not in ('active', 'completed', 'cancelled')
            or data.get('replace') not in (None, False) or data.get('source') == 'deep_research'):
        return 400, 'invalid_scoped_direction'
    status = data.get('status', 'active')
    text = data.get('text', '')
    supersedes = data.get('supersedes', [])
    if (not isinstance(text, str) or len(text) > 2000 or (status == 'active' and not text.strip())
            or not isinstance(supersedes, list) or len(supersedes) > 16
            or any(not isinstance(x, str) or _ID.fullmatch(x) is None for x in supersedes)
            or len(set(supersedes)) != len(supersedes) or data['directive_id'] in supersedes):
        return 400, 'invalid_scoped_direction'
    existing = ledger.get(data['directive_id'])
    if status != 'active':
        if supersedes:
            return 400, 'terminal_direction_cannot_supersede'
        if existing is None or existing.get('topic') != data['topic']:
            return 409, 'direction_identity_unknown'
        if existing['status'] not in ('active', status):
            return 409, 'direction_already_retired'
        if existing['status'] == status and existing.get('reason', '') != text:
            return 409, 'direction_retirement_content_changed'
        return None
    if existing is not None:
        if existing['status'] != 'active':
            return 409, 'direction_already_retired'
        if any(existing.get(k) != data.get(k, [] if k == 'supersedes' else None)
               for k in ('topic', 'text', 'supersedes')):
            return 409, 'direction_id_content_changed'
        return None
    current = {key for key, row in ledger.items()
               if row['status'] == 'active' and row['topic'] == data['topic']}
    if set(supersedes) != current:
        return 409, 'direction_topic_changed'
    return None


def apply_scoped(pending, ledger, data, seq):
    """Apply one validated event to the two run projections, retaining retired ID tombstones.

    Malformed or stale historical events never promote themselves to legacy authority. Old
    readers may ignore additive fields; new writers and readers must use this contract together.
    """
    if transition_error(data, ledger):
        return pending, ledger
    key, status = data['directive_id'], data.get('status', 'active')
    existing = ledger.get(key)
    if existing is not None and existing['status'] == status:
        return pending, ledger  # exact re-delivery neither renews sequence nor authority
    ledger = {k: dict(v) for k, v in ledger.items()}
    if status == 'active':
        retired = set(data.get('supersedes', []))
        for prior in retired:
            ledger[prior].update(status='superseded', retired_seq=seq, superseded_by=key)
        pending = [row for row in pending if row.get('directive_id') not in retired]
        row = {k: data.get(k, [] if k == 'supersedes' else 'active')
               for k in ('directive_id', 'topic', 'text', 'status', 'supersedes')}
        ledger[key] = {**row, 'created_seq': seq, 'event_seq': seq}
        pending = [*pending, row]
    else:
        ledger[key].update(status=status, retired_seq=seq, event_seq=seq, reason=data.get('text', ''))
        pending = [row for row in pending if row.get('directive_id') != key]
    return pending, ledger


def retire_all(ledger, seq):
    """A legacy explicit replace still clears all topics, without erasing retired identities."""
    return {key: ({**row, 'status': 'superseded', 'retired_seq': seq} if row['status'] == 'active' else row)
            for key, row in ledger.items()}
