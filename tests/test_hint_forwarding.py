"""P2 (docs/PROMPT_REVIEW.md) guard: hint delivery through the wrapper chain.

Ephemeral researcher hints travel by `setattr` through up to three wrappers (foresight panel →
UnifiedAgent facade → inner researcher), and every wrapper forwards ONLY `RESEARCHER_HINT_ATTRS`
(plus `track_hypotheses`). A hint set on the outermost object but missing from the registry
silently dies at the first wrapper — that's exactly how hypothesis-board prioritization was dead
in the default config. Two guards:

  1. STATIC: scan the writer modules (engine orchestrator + foresight) for
     `setattr(self.researcher, "..."`) / `setattr(self.base, "..."`) string-literal targets (and
     tuple-driven setattr loops) and assert every underscore-prefixed hint name is registered.
  2. DYNAMIC: wire the REAL ForesightPanelResearcher(UnifiedAgent(...)) chain around a recording
     inner researcher, set every registry attr + `track_hypotheses` on the OUTERMOST object
     (engine-style), call propose, and assert the INNER researcher observed every one. The same
     delivery check runs against the OTHER forwarding wrappers the registry docstring enumerates
     (SurrogateResearcher, serve's PanelResearcher).

Offline (fake clients only).
"""
from __future__ import annotations

import ast
import inspect

from looplab.agents.roles import RESEARCHER_HINT_ATTRS
from looplab.agents.unified_agent import UnifiedAgent
from looplab.core.models import Hypothesis, Idea, RunState, hypothesis_id
from looplab.search.foresight import ForesightPanelResearcher

# The handles engine/foresight code uses for "the ACTIVE researcher" when stamping hints.
_HINT_TARGETS = {"self.researcher", "self.base", "researcher"}


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 — anything unrenderable is simply not a hint target
        return ""


def _setattr_hint_names(module) -> set[str]:
    """All string names set via `setattr(<active-researcher>, name, ...)` in `module`'s source:
    direct string-literal keys, plus string constants driving a tuple-loop whose body setattrs
    onto an active-researcher handle (the `for _attr, _val in (("_x", ...), ...)` pattern)."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        # direct: setattr(self.researcher, "_hint", value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and len(node.args) >= 2
                and _unparse(node.args[0]) in _HINT_TARGETS):
            key = node.args[1]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
        # tuple-driven: for _attr, _val in (("_a", x), ("_b", y)): setattr(self.researcher, _attr, _val)
        if isinstance(node, ast.For):
            body_setattrs = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr"
                and n.args and _unparse(n.args[0]) in _HINT_TARGETS
                for n in ast.walk(node))
            if body_setattrs:
                names.update(c.value for c in ast.walk(node.iter)
                             if isinstance(c, ast.Constant) and isinstance(c.value, str))
    return names


def test_every_setattr_hint_site_is_in_the_registry():
    import looplab.engine.novelty as nov
    import looplab.engine.orchestrator as orch
    import looplab.engine.proposal_cues as cues
    import looplab.search.foresight as fs

    # proposal_cues + novelty carry the engine's setattr-hint sites since the mixin extraction
    # (P3); scanning them here keeps "a hint set outside the registry" a red test everywhere.
    found = (_setattr_hint_names(orch) | _setattr_hint_names(fs)
             | _setattr_hint_names(cues) | _setattr_hint_names(nov))
    hints = {n for n in found if n.startswith("_")}   # non-underscore (track_hypotheses) rides along
    assert hints, "the scan found no hint setattr sites — the scanner or the code moved; fix the test"
    missing = hints - set(RESEARCHER_HINT_ATTRS)
    assert not missing, (
        f"hint attr(s) {sorted(missing)} are set on the active researcher but NOT registered in "
        "RESEARCHER_HINT_ATTRS — wrappers forward only the registry, so these silently die at the "
        "first wrapper (P2). Add them to the registry in looplab/agents/roles.py.")


# --------------------------------------------------------------------------- dynamic wiring guard
class _RecordingResearcher:
    """Fake INNER researcher: snapshots every registry attr (+ track_hypotheses) at propose() time,
    exactly the way the real readers consume them (`getattr` with a default)."""

    def __init__(self):
        self.client = None       # keeps ForesightPanelResearcher from inheriting a client off us
        self.bounds = None
        self.space_hint = ""
        self.prompts = None
        self.seen = None

    def propose(self, state, parent):
        self.seen = {a: getattr(self, a, "<UNSET>")
                     for a in (*RESEARCHER_HINT_ATTRS, "track_hypotheses")}
        return Idea(operator="draft", params={}, rationale="recorded")


class _RecordingDeveloper:
    def implement(self, idea):
        return "code"


class _RankClient:
    """Fake predictor client: ranks [1, 0] (second candidate first) via the tool_call parser."""

    def complete_tool(self, messages, json_schema):
        return {"order": [1, 0], "confidence": 0.9, "reason": "guard"}


def test_hints_reach_the_inner_researcher_through_both_wrappers():
    inner = _RecordingResearcher()
    unified = UnifiedAgent(researcher=inner, developer=_RecordingDeveloper())
    outer = ForesightPanelResearcher(unified, k=1, client=_RankClient())

    # Two OPEN board hypotheses so _prioritize_board actually ranks (it needs >= 2).
    st = RunState(goal="g", direction="min")
    ids = []
    for s in ("belief zero", "belief one"):
        hid = hypothesis_id(s)
        ids.append(hid)
        st.hypotheses[hid] = Hypothesis(id=hid, statement=s, status="open", evidence=[])

    # Engine-style: every hint lands on the OUTERMOST wrapper. `_hyp_order` is special — foresight
    # OWNS it (its board ranking overwrites whatever the loop stamped), so it gets no sentinel here;
    # instead we assert the freshly-RANKED order arrives at the inner researcher.
    sentinels = {a: f"SENTINEL::{a}" for a in RESEARCHER_HINT_ATTRS if a != "_hyp_order"}
    for a, v in sentinels.items():
        setattr(outer, a, v)
    setattr(outer, "track_hypotheses", False)     # an explicit OFF must not be shadowed (P2)

    outer.propose(st, None)

    assert inner.seen is not None, "inner propose never ran"
    for a, v in sentinels.items():
        assert inner.seen[a] == v, f"hint {a!r} was shadowed by a wrapper (P2 regression)"
    assert inner.seen["track_hypotheses"] is False, "track_hypotheses=False was shadowed (P2)"
    # _RankClient ordered [1, 0] -> best-first board ids [ids[1], ids[0]] must reach the inner reader.
    assert inner.seen["_hyp_order"] == [ids[1], ids[0]], \
        "_hyp_order did not survive the ForesightPanel -> UnifiedAgent -> inner researcher chain"


def _assert_registry_delivery(outer, inner):
    """Engine-style delivery check (mirrors the dynamic test above): set every registry attr +
    `track_hypotheses` on the OUTERMOST wrapper, propose, and assert the recording inner
    researcher observed every one."""
    sentinels = {a: f"SENTINEL::{a}" for a in RESEARCHER_HINT_ATTRS}
    for a, v in sentinels.items():
        setattr(outer, a, v)
    setattr(outer, "track_hypotheses", False)     # an explicit OFF must not be shadowed (P2)
    outer.propose(RunState(goal="g", direction="min"), None)
    assert inner.seen is not None, "inner propose never ran"
    for a, v in sentinels.items():
        assert inner.seen[a] == v, f"hint {a!r} was shadowed by the wrapper (P2 regression)"
    assert inner.seen["track_hypotheses"] is False, "track_hypotheses=False was shadowed (P2)"


def test_surrogate_wrapper_forwards_hints_to_its_fallback():
    # The engine setattrs hints on the OUTERMOST wrapper — which can be SurrogateResearcher (it
    # wraps the LLM researcher as its bootstrap fallback). Empty bounds force the delegate path.
    from looplab.search.surrogate import SurrogateResearcher
    inner = _RecordingResearcher()
    _assert_registry_delivery(SurrogateResearcher({}, fallback=inner), inner)


def test_serve_panel_wrapper_forwards_hints_to_its_base():
    # Same for the empirical PanelResearcher: hints must reach the base before the K-way fan-out.
    from looplab.serve.panel import PanelResearcher
    inner = _RecordingResearcher()
    _assert_registry_delivery(PanelResearcher(inner, k=2), inner)


def test_foresight_forwards_hints_even_without_a_client():
    # The no-client pass-through must still mirror engine-set hints onto the base (the engine
    # setattrs on the OUTERMOST object regardless of whether ranking is possible).
    inner = _RecordingResearcher()
    outer = ForesightPanelResearcher(inner, k=2, client=None)
    outer._novelty_feedback = "you already tried X"
    outer.track_hypotheses = False
    outer.propose(RunState(goal="g", direction="min"), None)
    assert inner.seen["_novelty_feedback"] == "you already tried X"
    assert inner.seen["track_hypotheses"] is False
