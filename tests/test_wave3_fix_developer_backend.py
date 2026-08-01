"""Regression: an unknown `developer_backend` must be REJECTED, not silently downgraded.

`developer_backend` is a CLOSED enum — "default" (the in-house developer) plus the external
coding-agent keys in `agents/cli_agent.py::PRESETS`. Only the CLI `--developer-backend` flag guarded
it (`_DEV_BACKENDS`); a file `settings:` block, `LOOPLAB_DEVELOPER_BACKEND` env, or
`--set developer_backend=Aider` reached `Settings` unchecked, and adapters/tasks.py then silently
wired the DEFAULT in-house developer (`developer_backend not in PRESETS`) with no diagnostic — the
exact silent-downgrade no-op the `backend` typo guard was added to close. It now fails loudly at
construction (in `_check_trust_gate`), validated against the SAME authoritative registry the runtime
consumer uses, mirroring test_an_unknown_backend_is_rejected_instead_of_downgrading_to_toy.
"""


def test_an_unknown_developer_backend_is_rejected_instead_of_downgrading_to_default():
    import pytest as _pytest
    from looplab.core.config import Settings

    # Mis-cased and typo'd values must fail — not fall through to the in-house default developer.
    for bad in ("typo", "Aider", "AIDER", "opencode2", ""):
        with _pytest.raises(Exception) as exc:
            Settings(developer_backend=bad)
        assert "developer_backend must be" in str(exc.value), bad

    # Every authoritative value is accepted: "default" plus each cli_agent.PRESETS key.
    from looplab.agents.cli_agent import PRESETS
    for good in ("default", *PRESETS):
        assert Settings(developer_backend=good).developer_backend == good
    # The unset default stays "default".
    assert Settings().developer_backend == "default"
