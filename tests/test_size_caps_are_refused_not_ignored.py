"""A mistyped size must not switch a safety cap OFF in silence (review 2026-09-22, CORE-05).

`sandbox_memory_local` (the trusted-local tier's RLIMIT_AS host-OOM guard) and `sandbox_fsize_local`
(its RLIMIT_FSIZE disk-fill guard) are human sizes. `Settings` accepted any string, and the one
reader, `parse_mem_bytes`, returns None for anything it cannot read — which `make_sandbox` passes on
as "no cap". So `sandbox_memory_local="8GB"` (docker's own spelling) or `"2 GiB"` validated, was
snapshotted, and ran every eval with NO limit at all, while the operator believed the host was
guarded. The same repository already REFUSES that typo for `sandbox_readonly_rootfs`
(`runtime/sandbox.py::readonly_rootfs_argv`): "an unreadable [boundary] is refused rather than
ignored". These two are boundaries too.

Strict at submit, canonicalizing on reload — the asymmetry `_canonicalize_snapshot_reasoning`
draws: a run recorded with such a value ALWAYS ran uncapped, so it resumes uncapped (``""``), with a
warning that says so, rather than becoming unresumable or suddenly capped under different semantics.
"""
from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from looplab.core.config import Settings, settings_from_snapshot

_FIELDS = ("sandbox_memory_local", "sandbox_fsize_local")


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("typo", ["8GB", "2 GiB", "8gb", "1e400g", "-1g", "nan", "eight gigs"])
def test_an_unreadable_size_is_refused_at_construction(field, typo):
    with pytest.raises(ValidationError, match=field):
        Settings(**{field: typo})


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("value", ["", "  ", "0", "8g", "8G", " 512m ", "1024k", "2t", "1073741824"])
def test_every_size_the_reader_understands_is_accepted_unchanged(field, value):
    assert getattr(Settings(**{field: value}), field) == value


def test_what_is_accepted_is_exactly_what_the_sandbox_enforces():
    """Accepted and ENFORCED are the same predicate: the validator and `make_sandbox` read the value
    through the one parser, which now lives in core and is re-exported by the runtime."""
    from looplab.core import numeric
    from looplab.runtime import sandbox

    assert sandbox.parse_mem_bytes is numeric.parse_mem_bytes
    capped = sandbox.make_sandbox("trusted_local",
                                  **{"mem_local": Settings(sandbox_memory_local="8g")
                                     .sandbox_memory_local,
                                     "fsize_local": Settings(sandbox_fsize_local="2g")
                                     .sandbox_fsize_local})
    assert (capped.mem_bytes, capped.fsize_bytes) == (8 * 1024 ** 3, 2 * 1024 ** 3)


@pytest.mark.parametrize("field", _FIELDS)
def test_a_run_recorded_with_an_unreadable_size_resumes_with_the_cap_it_actually_had(field, caplog):
    """The recorded value never capped anything (the reader read it as None), so the run resumes
    uncapped — its historical behaviour — and SAYS so, instead of refusing to resume a healthy run."""
    snapshot = {field: "8GB"}
    with caplog.at_level(logging.WARNING, logger="looplab.core.config"):
        resumed = settings_from_snapshot(snapshot)
    assert getattr(resumed, field) == ""
    assert snapshot == {field: "8GB"}, "the snapshot evidence must not be rewritten"
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert field in warning and "'8GB'" in warning and "no cap" in warning
    # A readable recorded size is untouched, and so is the fresh-submit refusal.
    assert getattr(settings_from_snapshot({field: "8g"}), field) == "8g"
    with pytest.raises(ValidationError):
        Settings(**{field: "8GB"})
