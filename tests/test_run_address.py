import os

import pytest

from looplab.core.run_ref import NESTED_RUN_PREFIX, resolve_run_address, run_address
from looplab.events.eventstore import EventStore


def _run(path):
    path.mkdir(parents=True)
    EventStore(path / 'events.jsonl').append('run_started', {'run_id': path.name, 'run_uid': 'u',
                                                               'goal': 'g', 'direction': 'min'})
    return path


def test_nested_address_is_canonical_reversible_and_unicode_safe(tmp_path):
    root = tmp_path / 'runs'
    rd = _run(root / 'camp A' / 'сид ~ 1')
    address = run_address(root, rd)
    assert address.startswith(NESTED_RUN_PREFIX) and '/' not in address and '=' not in address
    resolved = resolve_run_address(root, address)
    assert resolved.path == rd and resolved.run_id == address and resolved.nested and resolved.defect is None
    assert resolve_run_address(root, rd).path == rd


def test_nested_address_never_descends_into_another_run(tmp_path):
    root = tmp_path / 'runs'; outer = _run(root / 'outer'); planted = _run(outer / 'nodes' / 'node_0')
    address = run_address(outer.parent, planted)
    result = resolve_run_address(root, address)
    assert result.path is None and result.defect == 'inside_run'
    assert resolve_run_address(root, outer).path == outer


def test_nested_address_rejects_traversal_and_collision(tmp_path):
    root = tmp_path / 'runs'; root.mkdir()
    assert resolve_run_address(root, NESTED_RUN_PREFIX + 'Li4vLi4=').defect == 'invalid_nested_id'
    nested = _run(root / 'camp' / 'seed')
    address = run_address(root, nested)
    legacy = _run(root / address)
    result = resolve_run_address(root, address)
    assert result.path is None and result.defect == 'collision'
    assert resolve_run_address(root, legacy).path == legacy


@pytest.mark.posix_only('symlink')
def test_nested_address_refuses_linked_components(tmp_path):
    root = tmp_path / 'runs'; real = _run(tmp_path / 'elsewhere' / 'camp' / 'seed')
    (root / 'camp').mkdir(parents=True)
    os.symlink(real, root / 'camp' / 'seed', target_is_directory=True)
    address = run_address(tmp_path / 'elsewhere', real)
    assert resolve_run_address(root, address).defect == 'indirect'


def test_nested_run_uses_command_and_deletion_path_guards(tmp_path):
    from looplab.serve.deletion_service import _strict_existing_run
    from looplab.serve.server import make_app
    root = tmp_path / 'runs'; rd = _run(root / 'camp' / 'seed')
    address = run_address(root, rd)
    srv = make_app(root).state.looplab
    assert srv.commands.validate_paths(rd) == rd
    assert _strict_existing_run(srv, address) == rd
    rd.rename(root / 'camp' / 'deleted')
    assert resolve_run_address(root, address).path is None
    assert resolve_run_address(root, address, allow_missing=True).path == rd


def test_launch_cannot_take_a_canonical_nested_address(tmp_path):
    from fastapi import HTTPException
    from looplab.serve.launch import safe_run_dir
    root = tmp_path / 'runs'; _run(root / 'camp' / 'seed')
    address = run_address(root, root / 'camp' / 'seed')
    with pytest.raises(HTTPException) as error:
        safe_run_dir(root, address)
    assert error.value.status_code == 409
    assert not (root / address).exists()


@pytest.mark.posix_only('symlink')
def test_absolute_run_path_does_not_hide_a_link_before_validation(tmp_path):
    root = tmp_path / 'runs'
    real = _run(root / 'real')
    alias = root / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    result = resolve_run_address(root, alias)
    assert result.path is None and result.defect == 'indirect'
