"""Recorded freshness signals for an authored campaign overview; never a truth verdict."""
from __future__ import annotations

from looplab.core.hint_directives import operator_directive_texts


def overview_freshness(events, published_seq: int, at_node, node_count: int) -> dict:
    reasons = []
    if type(at_node) is int and at_node < node_count:
        reasons.append({'code': 'newer_nodes', 'at_node': at_node, 'current_nodes': node_count})
    terminal_seq = next((event.seq for event in reversed(events)
                         if event.type in ('node_evaluated', 'node_failed') and event.seq > published_seq), None)
    if terminal_seq is not None:
        reasons.append({'code': 'newer_terminal', 'event_seq': terminal_seq})
    direction_seq = next((event.seq for event in reversed(events)
                          if event.type == 'hint' and event.seq > published_seq
                          and isinstance(event.data, dict) and operator_directive_texts([event.data])), None)
    if direction_seq is not None:
        reasons.append({'code': 'newer_operator_direction', 'event_seq': direction_seq})
    return {
        'status': 'stale' if reasons else ('current' if type(at_node) is int else 'unknown'),
        'reasons': reasons,
        'node_watermark_recorded': type(at_node) is int,
        'meaning': 'Newer evidence or directions require review, not an automatic refutation; '
                   'absence of these signals does not verify completeness or scientific truth.',
    }
