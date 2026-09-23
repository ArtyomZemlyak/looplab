"""The operator's own env VALUES are masked at every durable boundary, not only at output tails.

`core/redact.py`'s module docstring, `redact_output_tail`'s ("masked ALWAYS"), `core/config.py`'s
("masked either way, at every tail, whatever") and CLAUDE.md all said this. It was false:
`redact_env_values` had exactly ONE production caller, `redact_output_tail`, so the ~76 sites that
persist through `redact_persisted_text` / `_redact_persisted` / `bounded_redacted_tree` got the
SHAPE and entropy screens only.

The gap is exactly the case the identity screen exists for. `secret_env_values`' own docstring names
it: a regex "cannot recognise `hunter2hunter2` as this box's `POSTGRES_PASSWORD`". A shaped
credential (`sk-…`, a bearer token) was masked either way; a shapeless one reached `spans.jsonl`,
the trace sidecar and a research memo in a driven offline run.
"""
from __future__ import annotations

import pytest

from looplab.core.redact import (bounded_redacted_tree, redact_output_tail, redact_persisted_text,
                                 redact_persisted_identity)

# Shapeless on purpose: long enough to pass `_MIN_SECRET_ENV_VALUE`, and matching no credential
# pattern, so ONLY the identity screen can catch it. A shaped value would pass this test with the
# defect fully in place, which is how the defect survived.
SECRET = "hunter2hunter2ZZqq"


@pytest.fixture(autouse=True)
def _operator_secret(monkeypatch):
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY", SECRET)


def test_the_shapeless_secret_is_masked_at_a_persistence_boundary():
    out = redact_persisted_text(f"provider said: {SECRET}", max_chars=4000)
    assert SECRET not in out, (
        "a durable diagnostic kept this box's own credential — `redact_persisted_text` is the "
        "funnel ~76 persisted sites go through, and it ran shapes and entropy but not the "
        "identity screen")
    assert "***REDACTED_ENV***" in out


def test_the_two_redactors_agree_about_the_same_secret():
    """Stated against the OTHER redactor rather than a literal: the defect was DISAGREEMENT between
    them while every docstring claimed they agreed."""
    text = f"provider said: {SECRET}"
    assert (SECRET in redact_output_tail(text, entropy=False)) is (
        SECRET in redact_persisted_text(text, max_chars=4000)), (
        "the tail redactor and the persisted redactor disagree about this box's own env value")


def test_it_survives_truncation_and_the_single_line_form():
    for kwargs in ({"max_chars": 4000}, {"max_chars": 40}, {"max_chars": 4000, "single_line": True},
                   {"max_chars": 4000, "entropy": False}):
        assert SECRET not in redact_persisted_text(f"x {SECRET} y", **kwargs), kwargs


def test_an_identity_and_a_whole_tree_are_screened_too():
    assert SECRET not in redact_persisted_identity(f"run-{SECRET}", max_chars=4000)
    tree = {"note": f"key {SECRET}", "rows": [{"tail": SECRET}, "plain"]}
    assert SECRET not in repr(bounded_redacted_tree(tree, [40_000], [64]))


def test_ordinary_text_is_untouched():
    """The screen replaces an exact substring, so a diagnostic that shares no value with the
    environment must come back byte-identical — otherwise this would be masking prose."""
    plain = "epoch 3 loss 0.214 -- lr 3e-4, batch 64, no credential here"
    assert redact_persisted_text(plain, max_chars=4000) == plain


# ------------------------------------------------------------------------------------------------
# The screen is CACHED on the environment's whole content (review 2026-09-22, CORE-02): every
# persisted string used to re-walk `os.environ` — 146 walks, 39 ms, per traced LLM generation. A
# cache is only acceptable if nothing it could serve stale is a secret, so these drive the three
# ways the environment moves under a warm cache, and the one thing the cache must actually save.

ROTATED = "rotated-credential-7Qx"
LATE = "late-bound-credential-3Kv"


@pytest.fixture
def _cold_screen(monkeypatch):
    """Start every cache test from nothing, and leave nothing behind for the next test."""
    from looplab.core import redact
    monkeypatch.setattr(redact, "_PROCESS_ENV_SCREEN", None)
    return redact


def test_an_unchanged_environment_is_walked_once_not_once_per_string(_cold_screen, monkeypatch):
    redact = _cold_screen
    evaluations = []
    real = redact.is_secret_env
    monkeypatch.setattr(redact, "is_secret_env",
                        lambda name, value="": (evaluations.append(name), real(name, value))[1])
    for index in range(50):
        assert SECRET not in redact_persisted_text(f"row {index}: {SECRET}", max_chars=4000)
    walked = len(evaluations)
    assert walked, "the screen never ran at all"
    redact_persisted_text(f"one more {SECRET}", max_chars=4000)
    assert len(evaluations) == walked, "a warm screen re-walked an unchanged environment"
    # ONE walk for fifty strings: the walk evaluates each long-enough variable exactly once.
    assert walked == len({name for name in evaluations}), (
        f"{walked} evaluations over {len(set(evaluations))} variables — the environment was "
        "walked once per persisted string, the cost this cache exists to remove")


def test_a_secret_set_deleted_or_rotated_under_a_warm_screen_moves_on_the_very_next_call(
        _cold_screen, monkeypatch):
    """Every mutation `os.environ` accepts reaches the next redaction — no proxy key, no TTL."""
    assert SECRET not in redact_persisted_text(SECRET, max_chars=4000)      # warm
    monkeypatch.setenv("C2FIXTURE_SERVICE_TOKEN", LATE)
    assert LATE not in redact_persisted_text(f"later: {LATE}", max_chars=4000), (
        "a secret set after the screen was warmed was served the stale answer")
    monkeypatch.setenv("C2FIXTURE_SERVICE_TOKEN", ROTATED)
    rotated = redact_persisted_text(f"old {LATE} new {ROTATED}", max_chars=4000)
    assert ROTATED not in rotated
    assert LATE in rotated, "a value no longer in the environment is not a secret any more"
    monkeypatch.delenv("C2FIXTURE_SERVICE_TOKEN")
    assert ROTATED in redact_persisted_text(ROTATED, max_chars=4000)
    assert SECRET not in redact_persisted_text(SECRET, max_chars=4000)      # the rest still holds


def test_a_change_racing_the_screen_is_never_cached_as_the_current_answer(
        _cold_screen, monkeypatch):
    """The cached VALUES are computed from the snapshot that KEYS them. A variable that lands while
    the screen runs must make the next call a miss — keying on a snapshot taken after the screen
    would serve the pre-race values for the post-race environment indefinitely."""
    redact = _cold_screen
    real = redact._screen_secret_env_values

    def racing(items):
        out = real(items)
        monkeypatch.setenv("C2FIXTURE_RACE_API_KEY", LATE)      # lands mid-screen
        return out

    monkeypatch.setattr(redact, "_screen_secret_env_values", racing)
    assert LATE not in redact.secret_env_values()               # screened the pre-race snapshot
    monkeypatch.setattr(redact, "_screen_secret_env_values", real)
    assert LATE in redact.secret_env_values()
    assert LATE not in redact_persisted_text(f"raced {LATE}", max_chars=4000)


def test_a_secret_set_mid_trace_is_masked_in_every_span_written_after_it(
        _cold_screen, monkeypatch, tmp_path):
    """The same property at the trace boundary, read back off disk: spans written BEFORE the
    variable existed may hold the value (it was not a secret yet); none written after it may."""
    from looplab.core import tracing
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    path = tmp_path / "spans.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True)
    history = [{"role": "system", "content": "SYS"}, {"role": "user", "content": f"say {LATE}"}]
    with tracer.span("loop", new_trace=True, node_id=0):
        for turn in range(6):
            if turn == 3:
                written_before = path.read_bytes()
                monkeypatch.setenv("C2FIXTURE_SERVICE_TOKEN", LATE)
            with tracing.generation(op="chat", model="m", messages=history) as gen:
                gen.output(f"turn {turn} repeats {LATE}")
            history = history + [{"role": "assistant", "content": f"turn {turn}: {LATE}"},
                                 {"role": "tool", "content": f"tool {turn} saw {LATE}"}]
    after = path.read_bytes()[len(written_before):]
    assert LATE.encode() in written_before, "premise: the value WAS persisted before it was secret"
    assert after.count(b"\n") >= 3 and b"REDACTED_ENV" in after
    assert LATE.encode() not in after, (
        "a span written after the variable was set persisted its value — the screen was stale")
