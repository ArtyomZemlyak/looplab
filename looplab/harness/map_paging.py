"""Whole-response bounds and collection-scoped continuations for the advisory research map."""
from __future__ import annotations

import json
import re

from fastapi import HTTPException

from looplab.core.jsonutil import canonical_json_digest

COLLECTIONS = ('uploads', 'cards', 'research_memos', 'reviews', 'decisions', 'experiments', 'nodes', 'reports')
SUMMARIES = ('goal', 'operator_directions', 'quality', 'campaign_overview')
DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_MAX_ITEMS = 40
_CURSOR = re.compile(r'map1:(0|[1-9][0-9]{0,9}):([a-f0-9]{64})')


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))


def _stamp(payload):
    budget = payload['response_budget']
    # The byte-count field counts itself. Its fixed point differs only at decimal boundaries.
    for _ in range(4):
        size = _bytes(payload)
        if budget['bytes'] == size:
            return size
        budget['bytes'] = size
    return _bytes(payload)


def request_options(collections=None, cursors=None):
    if collections is None:
        selected = set(COLLECTIONS + SUMMARIES)
    elif (not isinstance(collections, list) or not collections or len(collections) > 12
          or any(not isinstance(x, str) or x not in COLLECTIONS + SUMMARIES for x in collections)):
        raise HTTPException(400, {'code': 'invalid_research_map_sections'})
    else:
        selected = set(collections)
    if (cursors is not None and (not isinstance(cursors, dict) or len(cursors) > len(COLLECTIONS)
            or any(name not in COLLECTIONS or not isinstance(token, str) or len(token) > 100
                   or _CURSOR.fullmatch(token) is None for name, token in cursors.items()))):
        raise HTTPException(400, {'code': 'invalid_research_map_cursor'})
    return selected, cursors or {}


def paginate(metadata, rows, *, offset=0, limit=20, collections=None, cursors=None,
             max_bytes=DEFAULT_MAX_BYTES, max_items=DEFAULT_MAX_ITEMS):
    selected, cursors = request_options(collections, cursors)
    if (type(max_bytes) is not int or not 4096 <= max_bytes <= DEFAULT_MAX_BYTES
            or type(max_items) is not int or not 1 <= max_items <= 100
            or type(offset) is not int or not 0 <= offset <= 10**9
            or type(limit) is not int or not 1 <= limit <= 100):
        raise HTTPException(400, {'code': 'invalid_research_map_budget'})
    limit = min(limit, 20)
    payload = {key: value for key, value in metadata.items() if key not in SUMMARIES or key in selected}
    budget = {'version': 1, 'max_bytes': max_bytes, 'max_items': max_items, 'items_returned': 0,
              'bytes': 0, 'shortened_items': 0, 'limited': False, 'omitted_sections': [x for x in SUMMARIES if x not in selected]}
    payload['response_budget'] = budget
    starts, digests = {}, {}
    for name in COLLECTIONS:
        source = rows[name]
        # Exact sanitized rows, run identity, generation and collection name bind a continuation.
        # An unrelated collection may change without invalidating this collection's cursor.
        digest = canonical_json_digest({'run_id': metadata['run_id'], 'run_uid': metadata['run_uid'], 'generation': metadata['generation'],
                                         'collection': name, 'rows': source})
        start = offset
        if name in cursors:
            match = _CURSOR.fullmatch(cursors[name])
            start = int(match.group(1))
            if match.group(2) != digest:
                raise HTTPException(409, {'code': 'research_map_collection_changed', 'collection': name,
                                          'message': 'This collection changed; restart its first page explicitly.'})
        starts[name], digests[name] = start, digest
        payload[name] = {'items': [], 'total': len(source), 'offset': start, 'limit': limit,
                         'selected': name in selected, 'returned_count': 0, 'has_more': start < len(source),
                         'next_cursor': f'map1:{start}:{digest}', 'truncated': start < len(source)}
    # Keep identity and source health intact. Oversized optional summaries are visibly omitted,
    # never shortened into a fabricated complete value; callers may select them separately.
    optional = sorted((name for name in SUMMARIES if name in payload),
                      key=lambda name: _bytes(payload[name]), reverse=True)
    while _stamp(payload) > max_bytes and optional:
        name = optional.pop(0)
        del payload[name]
        budget['omitted_sections'].append(name)
        budget['limited'] = True
    if _stamp(payload) > max_bytes:
        raise HTTPException(503, {'code': 'research_map_metadata_exceeds_budget',
                                  'message': 'Identity and source-health metadata do not fit the requested byte bound.'})
    blocked = set()
    rotation = sum(starts.values()) % len(COLLECTIONS)
    order = COLLECTIONS[rotation:] + COLLECTIONS[:rotation]
    while budget['items_returned'] < max_items:
        progress = False
        for name in order:
            page = payload[name]
            index = starts[name] + page['returned_count']
            if (name not in selected or name in blocked or page['returned_count'] >= limit
                    or index >= page['total'] or budget['items_returned'] >= max_items):
                continue
            page['items'].append(rows[name][index])
            shortened = int(rows[name][index].get('detail_truncated') is True)
            budget['shortened_items'] += shortened
            page['returned_count'] += 1
            budget['items_returned'] += 1
            page['has_more'] = index + 1 < page['total']
            page['truncated'] = page['has_more'] or starts[name] > 0
            page['next_cursor'] = f"map1:{index + 1}:{digests[name]}"
            if _stamp(payload) > max_bytes:
                page['items'].pop()
                budget['shortened_items'] -= shortened
                page['returned_count'] -= 1
                budget['items_returned'] -= 1
                page['has_more'] = index < page['total']
                page['truncated'] = page['has_more'] or starts[name] > 0
                page['next_cursor'] = f"map1:{index}:{digests[name]}"
                blocked.add(name)
            else:
                progress = True
        if not progress:
            break
    budget['limited'] = (budget['limited'] or any(payload[name]['has_more'] for name in selected
                                                if name in COLLECTIONS))
    if _stamp(payload) > max_bytes:
        # Changing count digits/flags can consume a few bytes. Remove complete rows only, preserving
        # exact continuations; no payload field is clipped after declaring its size.
        for name in reversed(COLLECTIONS):
            page = payload[name]
            while page['items'] and _stamp(payload) > max_bytes:
                removed = page['items'].pop()
                budget['shortened_items'] -= int(removed.get('detail_truncated') is True)
                page['returned_count'] -= 1
                budget['items_returned'] -= 1
                page['has_more'] = page['truncated'] = budget['limited'] = True
                page['next_cursor'] = f"map1:{starts[name] + page['returned_count']}:{digests[name]}"
        if _stamp(payload) > max_bytes:
            raise HTTPException(503, {'code': 'research_map_metadata_exceeds_budget'})
    return payload
