"""DEVELOPER_OUTPUT_ATTRS / RESEARCHER_ACTION_ATTRS enforcement (docs/15 §P4.3).

The engine reads these duck-typed attributes off the active roles with `getattr(..., default)`,
so a coordinated rename used to leave the suite green while production silently shipped empty
nodes (`last_files`) or quietly reverted the pilot to the static policy (`choose_action`).
Mirrors test_hint_forwarding: source-scan both sides of the seam against the registry.
"""
from __future__ import annotations

import re
from pathlib import Path

from looplab.agents.roles import DEVELOPER_OUTPUT_ATTRS, RESEARCHER_ACTION_ATTRS

_PKG = Path(__file__).resolve().parents[1] / "looplab"
_ALL = set(DEVELOPER_OUTPUT_ATTRS) | set(RESEARCHER_ACTION_ATTRS)

# Consumer probes: getattr(<expr>, "<attr>") over developer/researcher/wrapper handles.
_CONSUMER = re.compile(r'getattr\([A-Za-z_][\w.]*,\s*"((?:last_|choose_)[a-z_]+)"')
# Producer writes: `self.last_files = …` / `obj.last_files = …` (also catches `last_filez =`
# style renames as long as the prefix survives — the near-miss check below covers the rest).
_PRODUCER = re.compile(r'\.((?:last_files?|last_deleted|last_file|last_report|last_seed|last_run|last_patch|choose_action)[a-z_]*)\s*=[^=]')


def _scan(pattern: re.Pattern) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for f in list(_PKG.rglob("*.py")):
        for name in pattern.findall(f.read_text(encoding="utf-8", errors="replace")):
            found.setdefault(name, set()).add(str(f.relative_to(_PKG)))
    return found


def test_every_consumer_probe_is_registered():
    # Telemetry attrs (last_hyp_priority/last_foresight*) have their own explicit-property
    # discipline in surrogate.py — they are read via _emit_role_telemetry's registry, not here.
    telemetry = {"last_hyp_priority", "last_foresight", "last_foresight_pick"}
    unknown = {n: fs for n, fs in _scan(_CONSUMER).items() if n not in _ALL | telemetry}
    assert not unknown, (
        f"getattr probe(s) for unregistered role output attr(s): {unknown} — a typo'd read "
        "silently returns the default forever. Register in agents/roles.py or fix the probe.")


def test_every_producer_write_is_registered():
    unknown = {n: fs for n, fs in _scan(_PRODUCER).items()
               if n not in _ALL and not n.startswith(("last_foresight", "last_hyp"))}
    assert not unknown, (
        f"assignment(s) to near-registry role output attr(s): {unknown} — a producer-side "
        "rename the engine's getattr default would silently swallow.")


def test_registry_attrs_still_have_producers_and_consumers():
    consumers, producers = _scan(_CONSUMER), _scan(_PRODUCER)
    for attr in DEVELOPER_OUTPUT_ATTRS:
        assert attr in consumers, f"{attr}: no engine consumer left — registry rot"
        assert attr in producers, f"{attr}: no producer left — registry rot"
    for attr in RESEARCHER_ACTION_ATTRS:
        assert attr in consumers, f"{attr}: no engine consumer left — registry rot"
        # producers define it as a method (`def choose_action`), not an assignment — needle check:
        text = "\n".join(f.read_text(encoding="utf-8", errors="replace")
                          for f in _PKG.rglob("*.py"))
        assert f"def {attr}(" in text, f"{attr}: no role defines it — registry rot"


def test_foresight_panel_forwards_the_registry_attrs():
    # The panel is a transparent __getattr__ proxy: reads of any registered attr on the panel
    # must reach the wrapped base (a rename that breaks the delegation should fail HERE).
    from looplab.search.foresight import ForesightPanelResearcher

    class _Base:
        last_files = {"a.py": "x"}
        last_deleted = ["b.py"]

        def choose_action(self, state):  # pragma: no cover - identity only
            return []

    panel = ForesightPanelResearcher.__new__(ForesightPanelResearcher)
    panel.__dict__["base"] = _Base()
    # Registry-driven + identity-asserted (no `or callable` escape hatch): the __getattr__
    # proxy must return the BASE's object for every registered attr it doesn't define itself.
    for attr in (*DEVELOPER_OUTPUT_ATTRS, *RESEARCHER_ACTION_ATTRS):
        setattr(_Base, attr, getattr(_Base, attr, object()))
        # `==`, not `is`: a bound method is re-created per getattr; equality means same
        # function + same instance, which IS the delegation guarantee we're asserting.
        assert getattr(panel, attr) == getattr(panel.base, attr), attr
    # NOTE (documented reality, not enforced): SurrogateResearcher/PanelResearcher deliberately
    # do NOT proxy `choose_action` — the surrogate IS the chooser in its regime, so the pilot
    # reverting to the static policy behind those wrappers is by design (surrogate.py's
    # per-attr-properties rationale). Changing that is a behavior decision, not a rename guard.
