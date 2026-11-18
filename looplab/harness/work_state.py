"""Latest external-authored checkpoint in the existing native event journal, read without effects."""
import re
from urllib.parse import quote, urlencode

from looplab.core.external_work_state import AUTHOR_FIELDS, SERVER_FIELDS, context_digest, normalize_authored


def project(events, state, generation, run_id):
    latest = next((e for e in reversed(events) if e.type == 'agent_checkpointed'
                   and 'external_work_state' in e.data), None)
    base = {'version': 1, 'authority': 'none', 'agent_liveness': 'not_measured',
            'artifact_identity': 'not_verified', 'harness_revision_observation': 'not_measured',
            'work_claims': 'authored_not_exclusive', 'verification_required_before_execution': True}
    if latest is None:
        return {**base, 'state': 'not_authored', 'checkpoint': None}
    value = latest.data['external_work_state']
    try:
        if not isinstance(value, dict) or set(value) - AUTHOR_FIELDS - SERVER_FIELDS:
            raise ValueError('invalid_record')
        authored = normalize_authored({k: v for k, v in value.items() if k in AUTHOR_FIELDS})
        if (type(value.get('version')) is not int or value['version'] != 1
                or value.get('run_uid') != state.run_uid or value.get('run_generation') != generation
                or not isinstance(value.get('context_digest'), str)
                or re.fullmatch(r'[0-9a-f]{64}', value['context_digest']) is None):
            raise ValueError('invalid_context')
    except (ValueError, TypeError, UnicodeError):
        return {**base, 'state': 'unavailable', 'checkpoint': None, 'event_seq': latest.seq}
    fresh = value['context_digest'] == context_digest(state)
    prefix = '/api/runs/' + quote(run_id, safe='')
    query = {'expected_generation': generation}
    command = authored.get('saved_command_id')
    if command:
        next_read = {'method': 'GET', 'path': prefix + '/command-receipt?' + urlencode({**query, 'command_id': command}),
                     'purpose': 'Observe the original command receipt without reconciliation; do not resubmit from this checkpoint.'}
    else:
        next_read = {'method': 'GET', 'path': prefix + '/harness-progress?' + urlencode({**query, 'brief': 'true'}),
                     'purpose': 'Refresh current gates and recorded work before choosing an action.'}
    return {**base, 'state': 'context_unchanged' if fresh else 'context_changed', 'event_seq': latest.seq,
            'harness_revision_observation': 'declared_not_attested' if authored['harness_revision'] else 'unknown',
            'checkpoint': authored, 'authored_next_step': authored['next_step'] if fresh else None,
            'context_digest': value['context_digest'], 'next_read': next_read,
            'required_reads': [prefix + '/harness-identity', prefix + '/harness-contract', '/openapi.json'],
            'meaning': 'Authored recovery notes, not measured completion or permission. Verify current tool catalogue, original saved actions, source/protocol/budget/attempt gates and resolved artifact identities before execution. Unknown observations stay unknown.'}
