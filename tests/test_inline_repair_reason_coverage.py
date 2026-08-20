"""Every way an eval can fail is now eligible for inline repair.

The default used to be the mechanical three — `("crash", "timeout", "oom")` — on the reasoning that
a timeout or an OOM means the code was too slow or too hungry for the budget, not that the idea was
wrong. The reasoning was right; the conclusion was not, because the same holds for the other three.
`no_metric` is a script that ran and printed nothing where it was asked to; `setup` is a dependency
that is not installed; `drift` is an implementation that wandered off its own declared spec. None of
them is evidence the HYPOTHESIS is wrong, which is the only thing that should end a node with no
repair attempted.

What it cost, measured: `rubertlite-dr-unified-v5` node 0 trained 76 minutes on two H200s, exited 0,
wrote a complete checkpoint and computed recall@100 = 0.743 — then died with `reason: no_metric` and
ZERO repair attempts, because the stage manifest declared that checkpoint one directory over from
where the testbed writes it. The repair was a one-line path edit.

The point of this file is the DRIFT that made it possible: the vocabulary lived in the classifier and
the default lived in three hand-written copies, so a reason could be classifiable and unrepairable
with nothing red anywhere.
"""
from __future__ import annotations

import inspect

from looplab.core.config import Settings
from looplab.core.models import FAILURE_REASONS
from looplab.engine import triage
from looplab.engine.train_monitor import MONITOR_REPAIR_REASON
from looplab.engine.options import EngineOptions


def test_every_reason_anything_can_produce_is_in_the_registry():
    """Derived from the PRODUCERS' own source, not from a second hand-written list. A reason a
    producer can emit and this registry has forgotten is exactly the shape of the original defect.

    FOUR producers since 2026-08-20, and enumerating them is the whole of this test — each was
    added because the previous phrasing had silently stopped being total:

      1. `_failure_reason`, the STRUCTURAL classifier, reading fields the engine itself set.
      2. the live training watchdog, naming `train_monitor.MONITOR_REPAIR_REASON` on a stage it
         stopped MID-RUN. Nothing about that reason can come out of an exit code — the process it
         describes was killed by us and exits like every other kill — so demanding it from
         `_failure_reason` would mean inventing a signal for a fact the engine already holds out of
         band, the defect `test_watchdog_kill_is_not_an_oom.py` exists to prevent.
      3. the DIAGNOSTICIAN (`failure_diagnosis.DIAGNOSED_FAILURE_REASONS`). It is a real producer
         and not a re-reader: `oom` and `not_learning` are ANSWER-ONLY, i.e. no engine code path can
         emit them at all, so leaving it out of this derivation would report the registry as naming
         two reasons "no producer ever emits" — which is exactly what went red when the text rules
         that used to produce `oom` were deleted.
      4. the ENGINE, minting `UNCLASSIFIED_REASON` when producer 3 was wired, asked, and could not
         answer.

    The invariant is unchanged and still total: producible <=> selectable. A reason a producer can
    emit and this registry has forgotten is a failure class that silently stops being repairable."""
    source = inspect.getsource(triage._failure_reason)
    returned = {line.split('return', 1)[1].split('#')[0].strip().strip('"\'')
                for line in source.splitlines() if line.strip().startswith('return ')}
    assert returned, "the classifier stopped returning literals; re-derive this test"
    producible = (returned | {MONITOR_REPAIR_REASON}
                  | set(triage.DIAGNOSED_FAILURE_REASONS) | {triage.UNCLASSIFIED_REASON})
    assert producible <= set(FAILURE_REASONS), (
        f"a producer can emit {producible - set(FAILURE_REASONS)}, which no setting can select")
    assert set(FAILURE_REASONS) == producible, (
        f"registry names {set(FAILURE_REASONS) - producible}, which no producer ever emits")


def test_the_diagnostician_IS_a_producer_and_every_kind_it_may_name_is_selectable():
    """THE CORRECTION TO THIS FILE'S OWN 2026-08-20 CLAIM, which was that a judge "is deliberately
    not a producer" because its vocabulary was a SUBSET of what `_failure_reason` returns. That was
    true of the half-measure and is false now: deleting the two text rules removed `oom`'s only
    producers, so the diagnostician is the ONLY thing that can name it, and `not_learning` is
    likewise unreachable from any exit code.

    What this file exists to protect is unchanged and is the second assertion: whatever can name a
    reason, every reason must stay selectable by `inline_repair_reasons`, or that failure class
    silently stops being repairable with nothing red anywhere.
    `tests/test_failure_ownership_split.py` owns the rest of that contract."""
    source = inspect.getsource(triage._failure_reason)
    returned = {line.split('return', 1)[1].split('#')[0].strip().strip('"\'')
                for line in source.splitlines() if line.strip().startswith('return ')}
    assert set(triage.DIAGNOSED_ONLY_REASONS) & returned == set(), (
        "an ANSWER-ONLY kind is one no engine path produces; if the classifier can return it, it "
        "is not answer-only and the split needs re-deriving")
    assert set(triage.DIAGNOSED_FAILURE_REASONS) <= set(Settings().inline_repair_reasons)
    assert triage.UNCLASSIFIED_REASON in set(Settings().inline_repair_reasons), (
        "a failure nobody could name must still be repairable, or a flapping provider throws away "
        "a node")


def test_the_shipped_default_repairs_every_one_of_them():
    assert set(Settings().inline_repair_reasons) == set(FAILURE_REASONS)
    assert "no_metric" in Settings().inline_repair_reasons, (
        "the reason that killed v5 node 0 after 76 minutes of successful training")


def test_the_engine_options_default_cannot_drift_from_the_settings_default():
    """Two defaults for one decision is how a knob ends up meaning different things depending on
    which entry point built the engine. They are bound to the same registry, not respelled."""
    assert EngineOptions().inline_repair_reasons == Settings().inline_repair_reasons
    # IDENTITY, not equality. A copied literal would compare equal to the registry today and drift
    # the moment either is edited — and a negative source pin cannot see the difference, because the
    # text it looks for also appears in the comment explaining why the copy was removed. (Verified:
    # the first version of this test failed on its own field comment.)
    # The FIELD default, not the instance attribute: pydantic copies the tuple on validation, so an
    # instance can never be identical to the registry even when it is bound to it.
    assert Settings.model_fields["inline_repair_reasons"].default is FAILURE_REASONS
    assert EngineOptions().inline_repair_reasons is FAILURE_REASONS


def test_the_vocabulary_lives_where_config_can_reach_it():
    """It is in `core` because `core/config.py` needs it and core may not import from `engine`. If
    that ever inverts, the import is a startup ImportError rather than a subtle layering violation —
    but the reason belongs written down."""
    assert inspect.getmodule(type(FAILURE_REASONS)) is not None
    import looplab.core.models as models

    assert getattr(models, "FAILURE_REASONS", None) is FAILURE_REASONS
    assert triage.FAILURE_REASONS is FAILURE_REASONS, "the re-export must be the SAME object"


def test_an_operator_can_still_narrow_it():
    """Widening the default is not the same as removing the knob. An operator who wants the old
    mechanical-only behaviour still has it."""
    narrowed = Settings(inline_repair_reasons=("crash",))
    assert narrowed.inline_repair_reasons == ("crash",)


def test_the_gate_reads_the_setting_rather_than_a_literal():
    """`_evaluate`'s gate is what actually decides. Pinned against the engine attribute it reads, so
    a future 'fast path' literal in that branch is a red test.

    AST, not a substring over the module text. A positive `assert "<literal>" in source` pin is one
    comment away from vacuous — delete the gate, leave it commented out carrying the pinned text,
    and this stays green while every reason silently stops buying repairs. `attributes_read` resolves
    real `ast.Attribute` nodes, and comments are not AST nodes.

    The NEGATIVE pin stays a substring by house rule: what must not come back is the TEXT, and a
    commented-out copy of the old hardcoded tuple is as much of a drift risk as a live one."""
    from looplab.engine import evaluate

    from tests._source_scan import attributes_read

    reads = attributes_read(evaluate.EvaluateMixin._evaluate)
    assert "self._inline_repair_reasons" in reads, (
        "the gate must READ the setting — a hardcoded reason list here silently changes which "
        "failures buy an inline repair, for every run")
    assert "self._inline_repair" in reads
    assert '"crash", "timeout", "oom"' not in inspect.getsource(evaluate)
