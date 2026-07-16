"""Researcher-assigned `Idea.theme` (UI #7 semantic grouping): it round-trips through the event
log and the per-run theme rollup the cross-run map consumes. Audit-only — never affects selection."""
from __future__ import annotations

import pytest

from looplab.core.models import Event, Idea, NodeStatus, RunState
from looplab.events.replay import fold


def _run(direction="min"):
    return Event(seq=0, type="run_started",
                 data={"run_id": "r", "task_id": "t", "goal": "g", "direction": direction})


def _node(nid, theme, operator="improve"):
    idea = Idea(operator=operator, params={"x": float(nid)}, rationale="r", theme=theme)
    return Event(seq=nid + 1, type="node_created",
                 data={"node_id": nid, "parent_ids": [], "operator": operator,
                       "idea": idea.model_dump(mode="json"), "code": ""})


def _eval(nid, metric):
    return Event(seq=100 + nid, type="node_evaluated", data={"node_id": nid, "metric": metric})


def test_idea_theme_optional_and_serializes():
    assert Idea(operator="draft").theme is None
    d = Idea(operator="improve", theme="loss-fn").model_dump(mode="json")
    assert d["theme"] == "loss-fn"


def test_theme_roundtrips_through_fold():
    st = fold([_run(), _node(0, "loss-fn"), _node(1, "architecture"), _node(2, None)])
    assert st.nodes[0].idea.theme == "loss-fn"
    assert st.nodes[1].idea.theme == "architecture"
    assert st.nodes[2].idea.theme is None        # themeless still folds fine


def test_old_events_without_theme_still_fold():
    # an event log written before the field existed has no idea.theme key
    ev = Event(seq=1, type="node_created",
               data={"node_id": 0, "parent_ids": [], "operator": "draft",
                     "idea": {"operator": "draft", "params": {}, "rationale": "x"}, "code": ""})
    st = fold([_run(), ev])
    assert st.nodes[0].idea.theme is None


def test_theme_rollup():
    fastapi = pytest.importorskip("fastapi")  # noqa: F841 - server import needs the [ui] extra
    from looplab.serve.server import _theme_rollup
    st = fold([_run("min"),
               _node(0, "loss-fn"), _eval(0, 1.0),
               _node(1, "loss-fn"), _eval(1, 0.3),
               _node(2, "architecture"), _eval(2, 0.8),
               _node(3, None), _eval(3, 0.1)])           # themeless: excluded from rollup
    roll = _theme_rollup(st)
    assert set(roll) == {"loss-fn", "architecture"}
    assert roll["loss-fn"] == {"count": 2, "best_metric": 0.3}   # min direction -> 0.3 beats 1.0
    assert roll["architecture"]["count"] == 1


def _node_concepts(nid, concepts, theme=None, operator="improve"):
    idea = Idea(operator=operator, params={"x": float(nid)}, rationale="r",
                theme=theme, concepts=concepts)
    return Event(seq=nid + 1, type="node_created",
                 data={"node_id": nid, "parent_ids": [], "operator": operator,
                       "idea": idea.model_dump(mode="json"), "code": ""})


def test_node_theme_falls_back_to_concept_axis_after_phase0():
    # Regression: Phase 0 (bd816a5) moved authoring from `idea.theme` to `idea.concepts`, but the theme
    # READERS were not migrated — every new-run node was untitled so theme_rollup/coverage zeroed out.
    # node_theme must fall back to the first concept's coarse AXIS, and legacy `theme` still wins.
    from looplab.events.digest import node_theme, theme_rollup
    from looplab.search.coverage import _node_theme

    st = fold([_run("max"),
               _node_concepts(0, ["loss/contrastive", "arch/moe"]), _eval(0, 0.9),
               _node_concepts(1, ["loss/triplet"]), _eval(1, 0.7),
               _node_concepts(2, ["reg/r-drop"], theme="legacy-theme"), _eval(2, 0.5),
               _node_concepts(3, [])])                    # neither theme nor concepts -> skipped
    assert node_theme(st.nodes[0]) == "loss"              # first concept's axis
    assert node_theme(st.nodes[2]) == "legacy-theme"      # explicit theme still takes precedence
    assert node_theme(st.nodes[3]) is None
    roll = theme_rollup(st)
    assert roll["loss"]["count"] == 2                     # nodes 0 and 1 both bucket to axis "loss"
    assert roll["loss"]["best_metric"] == 0.9             # max direction
    assert set(roll) == {"loss", "legacy-theme"}
    # coverage's reader delegates to the SAME derivation — one vocabulary across surfaces.
    for n in st.nodes.values():
        assert _node_theme(n) == node_theme(n)
