"""Golden-log replay gate (docs/15 §P5.1).

`tests/data/golden_run_events.jsonl` is a REAL offline run's event log (the quadratic smoke,
`--no-genesis --kind quadratic`); `golden_run_state.json` is the byte-stable `fold(...)` output
captured as the current baseline. Any change to `fold` (or to a model default a folded field
depends on) that alters the produced `RunState` for an existing log — the exact regression class
the dispatch-table refactor must not introduce — turns this red.

AN ADDITIVE **EVENT** FIELD KEEPS IT GREEN; AN ADDITIVE **MODEL** FIELD DOES NOT, and this
paragraph said both did until 2026-08-27. The golden LOG carries only what its writer wrote, so a
new event key is simply absent from it — but the comparison is against `model_dump()`, which emits
every field the model declares, so a new `RunState`/`Node`/`Idea`/memo field appears in `got` at its
default and in the checked-in snapshot not at all. That is a real difference and this test is right
to report it; what it is NOT is a fold semantics change, and believing the sentence above is why the
snapshot went stale rather than being regenerated in the change that added the field.

So READ THE DIFF BEFORE REGENERATING. Additions with no value changes (`got` has a key the snapshot
lacks, and every shared leaf is equal) are the additive case and the snapshot is simply behind. A
changed VALUE on a shared key is the regression this file exists to catch, and regenerating over one
would erase the only thing that noticed. `git diff --stat` on the snapshot is the cheap check: pure
insertions is the first case, any deletion is the second.

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
