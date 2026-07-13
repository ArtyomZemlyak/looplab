"""Focused service-level coverage for durable Genesis start primitives."""
from __future__ import annotations

from looplab.serve.run_commands import RunCommandService
from looplab.serve.server import make_app


class _Driver:
    def __init__(self, *, engine_alive=False, pid_alive=True,
                 process_identity="child-generation"):
        self.engine_alive = engine_alive
        self.pid_alive = pid_alive
        self.process_identity = process_identity

    def engine_is_alive(self, _rd):
        return self.engine_alive

    def process_is_alive(self, _pid):
        return self.pid_alive

    def identity_for(self, _pid):
        return self.process_identity


def _service(root, driver: _Driver) -> RunCommandService:
    srv = make_app(root).state.looplab
    return RunCommandService(
        srv,
        engine_alive=driver.engine_is_alive,
        spawn_engine=lambda *_args, **_kwargs: 4242,
        process_alive=driver.process_is_alive,
        process_identity=driver.identity_for,
        startup_timeout=0.05,
        command_timeout=0.10,
        max_observation_timeout=0.30,
    )


def test_correlated_external_spawn_observation_distinguishes_claim_ownership(tmp_path):
    driver = _Driver(pid_alive=True)
    service = _service(tmp_path, driver)
    rd = tmp_path / "future-run"
    owner = "start:start_0123456789abcdef0123456789abcdef"

    assert service.observe_external_spawn(rd, owner) == "absent"

    service.begin_external_spawn(rd, owner)
    assert service.observe_external_spawn(rd, owner) == "uncertain"
    assert service._spawn_claim_path(rd).exists()  # PID-less ambiguity is never cleared
    service.cancel_external_spawn(rd, owner)

    service.record_external_spawn(rd, "start:some-other-operation", 4242)
    assert service.observe_external_spawn(rd, owner) == "mismatched"
    assert service._spawn_claim_path(rd).exists()

    # Malformed evidence remains unresolved and is never mistaken for an absent claim.
    service._spawn_claim_path(rd).write_text("{bad-json", encoding="utf-8")
    assert service.observe_external_spawn(rd, owner) == "uncertain"
    assert service._spawn_claim_path(rd).exists()


def test_correlated_external_spawn_observes_known_pending_and_definitive_death(tmp_path):
    driver = _Driver(pid_alive=True, process_identity="child-generation")
    service = _service(tmp_path, driver)
    rd = tmp_path / "future-run"
    owner = "start:start_0123456789abcdef0123456789abcdef"

    service.record_external_spawn(rd, owner, 4242)
    observation = service.observe_external_spawn(rd, owner)
    assert observation == "pending_known"
    assert "child-generation" not in observation  # process identity is never exposed

    driver.pid_alive = False
    assert service.observe_external_spawn(rd, owner) == "dead_or_cleared"
    assert not service._spawn_claim_path(rd).exists()


def test_correlated_external_spawn_observes_engine_lock_and_retires_claim(tmp_path):
    driver = _Driver(engine_alive=True, pid_alive=True)
    service = _service(tmp_path, driver)
    rd = tmp_path / "future-run"
    owner = "start:start_0123456789abcdef0123456789abcdef"

    service.record_external_spawn(rd, owner, 4242)
    assert service.observe_external_spawn(rd, owner) == "live"
    assert not service._spawn_claim_path(rd).exists()
    # The engine lock remains sufficient positive evidence after the transient lease is gone.
    assert service.observe_external_spawn(rd, owner) == "live"
