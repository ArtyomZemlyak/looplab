"""WHICH RUN a stored row belongs to — the one rule, for every reader that has to decide.

`run_id` is the run DIRECTORY NAME (`orchestrator.py`: `self.run_dir.name`). It is reused the moment
a run is deleted and re-created, it is `demo`/`baseline` on half the corpus, and it is identical
across two checkouts sharing the default `~/.looplab/memory`. `engine/concept_capsules.py` states
the rule outright — "`run_id` is only a run-root-local label… key by a persisted globally unique
run-incarnation UID" — and every WRITER already does: `memory.py`, `concept_capsules.py`,
`claims.py`, `lesson_hygiene.py` all carry `run_uid`.

The READERS did not, and the same mistake produced three different failures:

  * `lessons_reconcile` decided "this run's lessons" by directory name, so a lesson written by a
    PREVIOUS incarnation of the same name could not match the new run's evidence signature, was
    judged stale, and was RETIRED under the lock — and the reflect batch re-bought.
  * `concept_capsules`' readers de-duplicated by name, so two incarnations counted as one duplicate:
    the portfolio reported a single run and `source_complete: False`, which withholds the profit
    tendencies, forbids the steward's splits and purges, and prints PARTIAL on every surface — from
    a directory name.
  * `claims_health` grouped v3 receipt rows by name, so two incarnations' complete row sets became
    one group whose retained count could not match, `producer_receipt_known` went False, and every
    one-sided verdict was demoted to `inconclusive` portfolio-wide.

TWO SHAPES, ONE RULE, and they are not interchangeable — `serve/memory_cascade.py::RunIdentity`
worked this out first for the destructive path and this is that reasoning re-stated where the
readers can reach it (`core`, so `engine`, `serve` and `trust` may all import it).

`run_ref` is for GROUPING. It answers "which run is this row about" with one comparable string, so
two incarnations of one name fall into two groups. A row that names no incarnation groups under
`legacy:<name>` — the best that can be said about it, and deliberately NOT merged with a
uid-bearing row of the same name, because merging them is the collapse this module exists to end.

`row_belongs_to_run` is for ATTRIBUTION — "is this row MINE" — and it keeps the cascade's asymmetry
rather than being `run_ref` equality. A row that carries a uid matches ONLY that uid. A row that
carries none falls back to the NAME even for a uid-bearing caller, because a row written before
`run_uid` existed has no other identity and would otherwise be unreachable forever. That fallback
costs something real (two checkouts sharing one memory dir, both with a run named `demo`), which is
why it is a separate function with its own name instead of a mode flag: a caller picks the shape
whose failure it can live with, and grouping must never quietly attribute.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["LEGACY_REF_PREFIX", "run_ref", "row_belongs_to_run", "run_ref_is_legacy"]

# The prefix a name-derived ref carries. Present so a reader can TELL the two apart — a ref that
# begins with it is an incarnation nobody recorded, and a surface that reports counts per run should
# be able to say so rather than presenting it as an identity.
LEGACY_REF_PREFIX = "legacy:"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def run_ref(row_or_uid: Any, run_id: Any = None) -> str:
    """The GROUPING key for one stored row: its incarnation uid, else `legacy:<directory name>`.

    Accepts either a row dict (the common case — `run_ref(row)`) or an explicit
    `run_ref(uid, run_id)` pair, so a caller holding a `RunState` need not build a dict to ask.

    Returns "" when neither is present. A caller that groups on "" is grouping rows that say nothing
    about where they came from, which is a fact worth surfacing rather than a bucket to hide them in.
    """
    if isinstance(row_or_uid, dict):
        uid, name = _text(row_or_uid.get("run_uid")), _text(row_or_uid.get("run_id"))
    else:
        uid, name = _text(row_or_uid), _text(run_id)
    if uid:
        return uid
    return f"{LEGACY_REF_PREFIX}{name}" if name else ""


def run_ref_is_legacy(ref: Any) -> bool:
    """True when this ref rests on a directory NAME because the row recorded no incarnation."""
    return isinstance(ref, str) and ref.startswith(LEGACY_REF_PREFIX)


def row_belongs_to_run(row: Any, *, run_uid: Any = "", run_id: Any = "") -> bool:
    """Whether `row` was written by the run identified by (`run_uid`, `run_id`).

    NOT `run_ref(row) == run_ref(uid, name)`, and the asymmetry is the point (see the module
    docstring): a row that names its incarnation is matched ONLY on that, while a row that names
    none falls back to the directory name even for a uid-bearing caller — otherwise every row
    written before `run_uid` existed becomes permanently unattributable.
    """
    if not isinstance(row, dict):
        return False
    row_uid = _text(row.get("run_uid"))
    if row_uid:
        return bool(_text(run_uid)) and row_uid == _text(run_uid)
    return bool(_text(run_id)) and _text(row.get("run_id")) == _text(run_id)
