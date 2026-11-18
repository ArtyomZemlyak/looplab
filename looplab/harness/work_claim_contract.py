"""Critical read contract for optional claims; malformed absence cannot become coordination advice."""
from __future__ import annotations

import re

from looplab.core.work_intents import normalize_intents


def valid_snapshot(page, run_id, generation):
    def text(value):
        return isinstance(value, str) and bool(value) and len(value) <= 256
    def count(value):
        return type(value) is int and value >= 0
    def sha(value):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
    def projected(row):
        if not isinstance(row, dict) or not text(row.get('run_id')):
            return False
        state, claims = row.get('state'), row.get('claims')
        if state in ('unavailable', 'outside_task_scope'):
            return claims is None and 'claims' in row
        if state not in ('not_declared', 'context_unchanged', 'context_changed'):
            return False
        if not (text(row.get('run_uid')) and sha(row.get('generation')) and count(row.get('event_seq'))
                and row.get('authority') == 'none' and row.get('exclusivity') == 'not_claimed'
                and row.get('execution') == 'not_attested' and row.get('subject_identity') == 'author_declared_not_verified'):
            return False
        try:
            if normalize_intents(claims) != claims:
                return False
        except (ValueError, TypeError, UnicodeError):
            return False
        if state == 'not_declared':
            return claims == [] and row.get('checkpoint_seq') is None
        return (count(row.get('checkpoint_seq')) and row['checkpoint_seq'] <= row['event_seq']
                and isinstance(row.get('full_ref'), str) and row['full_ref'].startswith('/api/runs/')
                and isinstance(row.get('unaddressed_visible_direction_sequences'), list)
                and all(count(seq) for seq in row['unaddressed_visible_direction_sequences'])
                and type(row.get('direction_window_truncated')) is bool)
    if not (isinstance(page, dict) and type(page.get('version')) is int and page['version'] == 1
            and page.get('generation') == generation.lower() and text(page.get('run_uid')) and count(page.get('event_seq'))
            and page.get('task_scope') == 'same_native_task_id_only_inputs_and_scientific_equivalence_not_verified'
            and page.get('discovery') == 'caller_selected_peers_only' and page.get('atomic_across_runs') is False
            and projected(page.get('own')) and page['own']['run_id'] == run_id
            and page['own'].get('run_uid') == page['run_uid'] and page['own'].get('generation') == page['generation']
            and page['own'].get('event_seq') == page['event_seq']
            and isinstance(page.get('peers'), list) and len(page['peers']) <= 8
            and all(projected(row) for row in page['peers'])
            and isinstance(page.get('omitted_peers'), list) and len(page['omitted_peers']) <= 8
            and all(text(value) for value in page['omitted_peers'])
            and isinstance(page.get('possible_overlaps'), list) and count(page.get('possible_overlaps_total'))
            and type(page.get('overlaps_truncated')) is bool
            and page['overlaps_truncated'] == (page['possible_overlaps_total'] > len(page['possible_overlaps']))):
        return False
    budget = page.get('response_budget')
    if not (isinstance(budget, dict) and type(budget.get('max_bytes')) is int
            and 4096 <= budget['max_bytes'] <= 65536 and type(budget.get('limited')) is bool
            and (budget['limited'] or not page['omitted_peers'] and not page['overlaps_truncated'])):
        return False
    peers = {row['run_id']: row for row in page['peers']}
    if len(peers) != len(page['peers']):
        return False
    own_active = [row for row in page['own'].get('claims') or [] if row['status'] == 'active']
    own_ids = {row['id'] for row in own_active}
    expected = {(peer['run_id'], left['id'], right['id']) for peer in page['peers']
                for left in own_active for right in peer.get('claims') or []
                if right['status'] == 'active' and (left['operation'], left['subject_sha256']) == (right['operation'], right['subject_sha256'])}
    if page['possible_overlaps_total'] != len(expected):
        return False
    observed = set()
    for match in page['possible_overlaps']:
        if not (isinstance(match, dict) and isinstance(match.get('own_intent_id'), str) and match['own_intent_id'] in own_ids
                and isinstance(match.get('peer_run_id'), str) and match['peer_run_id'] in peers
                and isinstance(match.get('peer_intent_id'), str)
                and match['peer_intent_id'] in {row['id'] for row in peers[match['peer_run_id']].get('claims') or [] if row['status'] == 'active'}
                and match.get('basis') == 'exact_author_declared_subject_and_operation_not_execution_proof'):
            return False
        identity = (match['peer_run_id'], match['own_intent_id'], match['peer_intent_id'])
        if identity not in expected or identity in observed:
            return False
        observed.add(identity)
    receipts = page.get('direction_receipts')
    if not (isinstance(receipts, dict) and type(receipts.get('source_complete')) is bool
            and receipts.get('state') == ('observed_independent_sources' if receipts['source_complete'] else 'unavailable')
            and receipts.get('transport_delivery') == 'not_attested_here' and receipts.get('comprehension') == 'not_measured'
            and receipts.get('execution_authority') == 'none' and isinstance(receipts.get('items'), list)):
        return False
    if not receipts['source_complete']:
        return receipts['items'] == []
    for row in receipts['items']:
        if not (isinstance(row, dict) and count(row.get('event_seq')) and count(row.get('receipts_total'))
                and count(row.get('outbox_records')) and isinstance(row.get('receipts'), list)
                and len(row['receipts']) == min(row['receipts_total'], 4)
                and type(row.get('truncated')) is bool and row['truncated'] == (row['receipts_total'] > 4)
                and row.get('state') == ('explicit_ACK_recorded' if row['receipts_total'] else 'no_ACK_in_retained_sources')):
            return False
        for ack in row['receipts']:
            if not (isinstance(ack, dict) and count(ack.get('event_id')) and text(ack.get('recipient'))
                    and ack.get('disposition') in ('acknowledged', 'resolved', 'declined') and sha(ack.get('receipt_sha256'))):
                return False
    return True
