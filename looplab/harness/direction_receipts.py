"""Optional native outbox ACK visibility; transport acceptance is not recipient comprehension."""
from __future__ import annotations

import re
import math

from looplab.harness.journals import read_source
from looplab.core.jsonutil import canonical_json_digest

_RECIPIENT = re.compile(r'[A-Za-z0-9_.-]{1,64}')


def project(root, run_id, run_uid, generation, sequences):
    """Retained outbox and ACK journals are independent reads, never an execution gate."""
    directory = root / '.agent-events'
    outbox, health = [], {}
    for name in ('outbox.jsonl.1', 'outbox.jsonl', 'acknowledgements.jsonl'):
        rows, source_health = read_source(directory / name)
        health[name] = source_health.get('read_complete') is True
        if name == 'acknowledgements.jsonl':
            acks = rows
        else:
            outbox.extend(rows)
    def identity(row, *, acknowledgement=False):
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k]
                for k in ('run_id', 'run_uid')):
            return False
        event_id = row.get('event_id') if acknowledgement else row.get('id')
        if type(event_id) is not int or event_id < 0:
            return False
        if not acknowledgement:
            return (isinstance(row.get('kind'), str) and bool(row['kind'])
                    and (row['kind'] != 'operator_direction' or type(row.get('seq')) is int and row['seq'] >= 0))
        return (isinstance(row.get('generation'), str) and re.fullmatch(r'[0-9a-f]{64}', row['generation']) is not None
                and isinstance(row.get('recipient'), str) and _RECIPIENT.fullmatch(row['recipient']) is not None
                and row.get('disposition') in ('acknowledged', 'resolved', 'declined')
                and isinstance(row.get('action_id'), str) and 0 < len(row['action_id']) <= 128
                and isinstance(row.get('request_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', row['request_sha256']) is not None
                and type(row.get('recorded_at')) in (int, float) and math.isfinite(row['recorded_at']))
    available = (all(health.values()) and all(identity(row) for row in outbox)
                 and all(identity(row, acknowledgement=True) for row in acks))
    result = {'source': 'retained_native_outbox_and_optional_ACK_journals', 'source_complete': available,
              'transport_delivery': 'not_attested_here', 'comprehension': 'not_measured',
              'execution_authority': 'none', 'items': [],
              'meaning': 'An explicit ACK is self-attested receipt only. Queue acceptance, an HTTP read, missing ACK or a newer intent is not proof of comprehension, disobedience or scientific completion; no admission gate uses this view.'}
    if not available:
        result['state'] = 'unavailable'
        return result
    for seq in sequences:
        event_ids = {row['id'] for row in outbox if isinstance(row, dict)
                     and row.get('run_id') == run_id and row.get('run_uid') == run_uid
                     and row.get('kind') == 'operator_direction' and type(row.get('seq')) is int and row['seq'] == seq
                     and type(row.get('id')) is int and row['id'] >= 0}
        receipts = []
        for row in acks:
            if (not isinstance(row, dict) or row.get('run_id') != run_id or row.get('run_uid') != run_uid
                    or row.get('generation') != generation or type(row.get('event_id')) is not int
                    or row['event_id'] not in event_ids
                    or not isinstance(row.get('recipient'), str) or _RECIPIENT.fullmatch(row['recipient']) is None
                    or row.get('disposition') not in ('acknowledged', 'resolved', 'declined')):
                continue
            receipts.append({'event_id': row['event_id'], 'recipient': row['recipient'],
                             'disposition': row['disposition'], 'receipt_sha256': canonical_json_digest(row)})
        result['items'].append({'event_seq': seq, 'state': 'explicit_ACK_recorded' if receipts else 'no_ACK_in_retained_sources',
                                'receipts': receipts[-4:], 'receipts_total': len(receipts),
                                'truncated': len(receipts) > 4, 'outbox_records': len(event_ids)})
    result['state'] = 'observed_independent_sources'
    return result
