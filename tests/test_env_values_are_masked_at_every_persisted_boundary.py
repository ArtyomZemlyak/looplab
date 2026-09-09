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
