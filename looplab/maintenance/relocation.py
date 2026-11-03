"""Explicit run relocation: durable identity change, never a hand-edited launch record."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from looplab.engine.run_lifecycle import run_lifecycle_lock
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_RUN_RELOCATED
from looplab.maintenance.backfill_applied_params import offline_run


def _event_sha256(event) -> str:
    from looplab.core.jsonutil import canonical_json
    return hashlib.sha256(canonical_json({
        'seq': event.seq, 'ts': event.ts, 'type': event.type, 'data': event.data,
    })).hexdigest()


def _recorded(path: Path):
    from looplab.core.errors import ConfigRefusal
    store = EventStore(path / 'events.jsonl')
    events = store.read_all()
    if store.divergence is not None or not events:
        raise ConfigRefusal('relocate requires a complete readable event log; repair it first')
    state = fold(events)
    if not state.run_uid:
        raise ConfigRefusal('relocate requires the run_uid recorded at launch; do not invent one for a legacy log')
    return store, events, state


def _destination(source: Path, target: Path) -> Path:
    from looplab.core.errors import ConfigRefusal
    from looplab.core.pathsafe import validate_run_child
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise ConfigRefusal('relocate destination parent must be an existing readable directory') from exc
    child = validate_run_child(parent, target.name, must_exist=False, strict_name=True)
    if child.defect is not None:
        raise ConfigRefusal(f'relocate destination is not a valid run directory name: {child.defect}')
    try:
        if source.stat().st_dev != parent.stat().st_dev:
            raise ConfigRefusal('relocate uses an atomic rename and therefore requires one filesystem; no copy was made')
    except OSError as exc:
        raise ConfigRefusal('relocate could not verify the source and destination filesystem') from exc
    return child.path


def _already_relocated(path: Path) -> dict | None:
    from looplab.core.errors import ConfigRefusal
    try:
        _store, events, state = _recorded(path)
    except (ConfigRefusal, OSError):
        return None
    last = events[-1]
    if last.type == EV_RUN_RELOCATED and last.data.get('to_run_id') == path.name and state.run_uid == last.data.get('run_uid'):
        return {'status': 'already_relocated', 'run_id': state.run_id, 'run_uid': state.run_uid,
                'path': str(path), 'event_seq': last.seq}
    return None


def relocate_run(source: Path, target: Path, *, apply: bool = False, reason: str = '') -> dict:
    """Append ``run_relocated`` then atomically rename, with retry after an uncertain rename.

    No checkpoint, score, launch setting or cross-run credential is re-authorized by this operation.
    """
    from looplab.core.errors import ConfigRefusal
    from looplab.core.pathsafe import validate_run_child
    source = Path(source)
    target = Path(target)
    if not os.path.lexists(source):
        if os.path.lexists(target):
            settled = _already_relocated(target.resolve())
            if settled is not None:
                return settled
        raise ConfigRefusal('relocate source does not exist; no journal or directory was changed')
    try:
        source = source.resolve(strict=True)
        source = validate_run_child(source.parent, source).path
    except OSError as exc:
        raise ConfigRefusal('relocate source must be a real readable run directory, not a link') from exc
    if source is None:
        raise ConfigRefusal('relocate source must be a real readable run directory, not a link')
    if os.path.lexists(target):
        if target.resolve() == source:
            _store, _events, state = _recorded(source)
            if state.run_id == source.name:
                return {'status': 'already_relocated', 'run_id': state.run_id,
                        'run_uid': state.run_uid, 'path': str(source), 'event_seq': None}
        raise ConfigRefusal('relocate destination already exists; refusing to overwrite or merge runs')
    target = _destination(source, target)
    if not apply:
        _store, events, state = _recorded(source)
        return {'status': 'dry_run', 'from_run_id': state.run_id, 'to_run_id': target.name,
                'run_uid': state.run_uid, 'source': str(source), 'destination': str(target),
                'previous_event_seq': events[-1].seq}
    with run_lifecycle_lock(source):
        with offline_run(source, hold=True) as refusal:
            if refusal:
                raise ConfigRefusal(f'relocate refused: {refusal}')
            store, events, state = _recorded(source)
            last = events[-1]
            already_recorded = (last.type == EV_RUN_RELOCATED
                                and last.data.get('to_run_id') == target.name
                                and last.data.get('run_uid') == state.run_uid)
            if not already_recorded:
                if state.run_id == target.name:
                    raise ConfigRefusal('the current run id already matches the destination, but its last event is not this relocation; inspect the journal instead of moving it again')
                store.append(EV_RUN_RELOCATED, {
                    'from_run_id': state.run_id, 'to_run_id': target.name, 'run_uid': state.run_uid,
                    'previous_event_seq': last.seq, 'previous_event_sha256': _event_sha256(last),
                    'reason': reason.strip()[:500] or 'operator-requested run relocation',
                })
            try:
                os.rename(source, target)
            except OSError as exc:
                raise ConfigRefusal(
                    f'relocation event was recorded but rename failed: {exc}. Fix the destination and retry the same command; no second event will be appended') from exc
    settled = _recorded(target)
    return {'status': 'relocated', 'run_id': settled[2].run_id, 'run_uid': settled[2].run_uid,
            'path': str(target), 'event_seq': settled[1][-1].seq, 'event_already_recorded': already_recorded}
