"""Golden-log replay gate (docs/15 §P5.1).

`tests/data/golden_run_events.jsonl` is a REAL offline run's event log (the quadratic smoke,
`--no-genesis --kind quadratic`); `golden_run_state.json` is the byte-stable `fold(...)` output
captured as the current baseline. Any change to `fold` (or to a model default a folded field
depends on) that alters the produced `RunState` for an existing log — the exact regression class
the dispatch-table refactor must not introduce — turns this red. Additive event/model changes
with reader-side defaults keep it green by construction (the golden log carries only the fields
its writer wrote).

If this fails INTENTIONALLY (a deliberate fold semantics change), regenerate the snapshot in
the same change and say why in the commit:
    python - <<'PY'
    import orjson
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    d = fold(EventStore('tests/data/golden_run_events.jsonl').read_all()).model_dump(mode="json")
    open('tests/data/golden_run_state.json', 'wb').write(
        orjson.dumps(d, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    PY
"""
from __future__ import annotations

from pathlib import Path

import orjson

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

_DATA = Path(__file__).parent / "data"


def test_golden_log_folds_to_the_checked_in_state():
    evs = EventStore(_DATA / "golden_run_events.jsonl").read_all()
    assert evs, "golden log missing/empty"
    got = fold(evs).model_dump(mode="json")
    want = orjson.loads((_DATA / "golden_run_state.json").read_bytes())
    assert got == want


def test_golden_log_fold_is_idempotent_and_prefix_stable():
    evs = EventStore(_DATA / "golden_run_events.jsonl").read_all()
    a, b = fold(evs), fold(evs)
    assert a.model_dump(mode="json") == b.model_dump(mode="json")   # no hidden state across calls
    # every prefix folds without error (resume replays prefixes constantly)
    for i in range(1, len(evs) + 1):
        fold(evs[:i])
