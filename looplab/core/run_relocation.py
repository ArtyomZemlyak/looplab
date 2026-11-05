"""A root-side tombstone fences writers using a run's old address during/after relocation."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from looplab.core.fence import FENCE_OPERATION_RE, load_bounded_json_marker, publish_bounded_json_marker
from looplab.core.run_deletion import run_deletion_key

PREFIX = '.looplab-relocate-fence-'
_OWNER = ContextVar('looplab_relocation_owner', default=None)


class RunRelocationStorageError(RuntimeError):
    pass


class RunRelocationFenceError(RuntimeError):
    pass


def fence_path(rd):
    rd = Path(rd).resolve(strict=False)
    return rd.parent / f'{PREFIX}{run_deletion_key(rd)}.json'


def load_relocation_fence(rd):
    key = run_deletion_key(rd)
    def valid(row):
        return (set(row) == {'version', 'operation_id', 'run_key', 'run_uid', 'destination'}
                and row.get('version') == 1 and row.get('run_key') == key
                and isinstance(row.get('operation_id'), str)
                and FENCE_OPERATION_RE.fullmatch(row['operation_id']) is not None
                and isinstance(row.get('run_uid'), str) and bool(row['run_uid'])
                and isinstance(row.get('destination'), str)
                and Path(row['destination']).is_absolute())
    return load_bounded_json_marker(fence_path(rd), label='run relocation fence',
                                   error_cls=RunRelocationStorageError, validate=valid, max_bytes=8192)


def publish_relocation_fence(rd, *, operation_id, run_uid, destination):
    row = {'version': 1, 'operation_id': operation_id, 'run_key': run_deletion_key(rd),
           'run_uid': run_uid, 'destination': str(Path(destination).resolve(strict=False))}
    return publish_bounded_json_marker(fence_path(rd), row, label='run relocation fence',
                                      error_cls=RunRelocationStorageError,
                                      confirm=lambda: load_relocation_fence(rd), max_bytes=8192)


def assert_run_relocation_write_allowed(rd):
    fence = load_relocation_fence(rd)
    if fence is not None and _OWNER.get() != fence['operation_id']:
        raise RunRelocationFenceError(
            'this run address is fenced by relocation; inspect the destination and retry '
            'the exact relocation if it was interrupted')
    return fence


@contextmanager
def relocation_owner(operation_id):
    token = _OWNER.set(operation_id)
    try:
        yield
    finally:
        _OWNER.reset(token)
