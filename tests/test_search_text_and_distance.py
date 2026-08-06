"""One distance, four named renderers (doc 25 SE-15).

Two unrelated module-level functions in `search/` were both called `_idea_text` — one built
predictor-facing prose, the other a lowercased tagger string — so a reader who found one had no way
to know the other existed, and no way to know which one a third caller meant. Separately, the three
empirical predictors each wrote out the same Euclidean loop around the shared `knn_idw`.

The renderers' divergence is load-bearing and stays. What changes is that the names say which is
which, and that a map at `node_text` lists all four.
"""
from __future__ import annotations

import ast
import inspect
import math

import pytest

from _source_scan import PKG
from looplab.core.models import Idea, Node
from looplab.core.numeric import euclidean, knn_idw
from looplab.search import concept_tagging, foresight, graded_novelty, novelty_recall, panel
from looplab.search import proxy, surrogate


# ------------------------------------------------------------------ one distance

def test_the_distance_is_the_ordinary_euclidean_one():
    assert euclidean({"a": 0.0, "b": 0.0}, {"a": 3.0, "b": 4.0}, ["a", "b"]) == 5.0
    assert euclidean({"a": 1.0}, {"a": 1.0}, ["a"]) == 0.0, "an exact match must be a true zero"
    assert euclidean({"a": 1.0, "b": 9.0}, {"a": 4.0, "b": -9.0}, ["a"]) == 3.0, (
        "only the named keys count")


def test_it_is_unnormalized_on_purpose():
    """Every caller has already projected into the same param space; normalizing here would silently
    change what `knn_idw` weights, without any call site saying so."""
    # Two keys, so a per-dimension divisor would change the answer — a one-key probe cannot see it.
    assert euclidean({"a": 0.0, "b": 0.0}, {"a": 30.0, "b": 40.0}, ["a", "b"]) == 50.0
    assert euclidean({"a": 0.0, "b": 0.0, "c": 0.0},
                     {"a": 2.0, "b": 3.0, "c": 6.0}, ["a", "b", "c"]) == 7.0
    assert "Unnormalized on purpose" in (euclidean.__doc__ or ""), (
        "the choice must stay written down in those words, so a reader looking for it finds it")


@pytest.mark.parametrize("module", [panel, surrogate, proxy])
def test_no_predictor_re_derives_the_distance(module):
    source = inspect.getsource(module)
    assert "euclidean(" in source, f"{module.__name__} does not use the shared distance"
    assert "math.sqrt(sum(" not in source, f"{module.__name__} re-derives the distance loop"


def test_each_predictor_keeps_its_own_eligibility_rule():
    """The rules genuinely differ — full-bounds dimensionality, target-subspace containment, any
    shared key — and the extraction must not have quietly unified them behind one parameter."""
    assert "tkeys.issubset(p)" in inspect.getsource(panel), "panel: target-subspace containment"
    assert "len(p) == len(bounds)" in inspect.getsource(surrogate), "surrogate: full dimensionality"
    assert "keys = set(target) & set(p)" in inspect.getsource(proxy), "proxy: any shared key"


def test_the_predictors_still_agree_with_the_idw_core():
    """Driven: an exact param match short-circuits to that sample's metric in every predictor."""
    hist = [({"lr": 0.1}, 5.0), ({"lr": 0.9}, 99.0)]
    assert panel._predict({"lr": 0.1}, hist, None) == 5.0
    assert knn_idw([(euclidean({"lr": 0.1}, p, ["lr"]), m) for p, m in hist], 3) == (5.0, 0.0)


def test_the_proxy_measures_over_its_own_string_tolerant_projection():
    """Its `_numeric` differs from `numeric_params` — it coerces numeric STRINGS, because generated
    solution code sometimes emits params as text and the proxy should still rank them. The shared
    distance has to work over that projection too, which is the point of taking `keys` rather than
    deriving them. (`Idea` itself validates params to float, so the tolerance is reached when the
    proxy scores a raw dict; end-to-end scoring is covered by tests/test_strategist.py.)"""
    scorer = proxy.ProxyScorer()
    target = scorer._numeric({"lr": "0.1", "note": "ignored"})
    neighbour = scorer._numeric({"lr": 0.4})
    assert target == {"lr": 0.1} and neighbour == {"lr": 0.4}
    assert euclidean(target, neighbour, set(target) & set(neighbour)) == pytest.approx(0.3)


# ------------------------------------------------------------------ four named renderers

def test_the_two_search_renderers_no_longer_share_a_name():
    assert not hasattr(foresight, "_idea_text")
    assert not hasattr(graded_novelty, "_idea_text")
    assert callable(foresight._idea_prose) and callable(graded_novelty._idea_tag_text)


def test_only_one_module_level_idea_text_name_remains_in_search():
    """The engine's `_idea_text` is a METHOD and a documented patch seam (orchestrator and
    eval_stages both name it), so it keeps its name — it never collided with these."""
    offenders = []
    for path in sorted((PKG / "search").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        offenders += [path.name for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_idea_text"]
    assert offenders == []
    assert "def _idea_text" in inspect.getsource(
        __import__("looplab.engine.novelty", fromlist=["x"])), "the engine seam keeps its name"


def test_each_name_says_what_it_renders():
    idea = Idea(operator="mutate", rationale="try a smaller step",
                hypothesis="smaller steps generalize", params={"lr": 0.02})

    prose = foresight._idea_prose(idea)
    assert prose.startswith("Hypothesis: "), "the predictor reads WHAT is tested, first"
    assert "lr=0.02" in prose and "\n" in prose, "prose, with values"

    tag = graded_novelty._idea_tag_text(idea)
    assert tag == tag.lower(), "tagger input is lowercased"
    assert "lr" in tag and "0.02" not in tag, "structural NAMES only — a value is not a concept"


def test_the_value_carrying_renderer_still_carries_values():
    """`novelty_recall` exists to tell a VARIANT from a duplicate, which needs the values that
    `node_text` deliberately drops."""
    node = Node(id=1, operator="mutate",
                idea=Idea(operator="mutate", params={"temperature": 0.02}))
    assert "0.02" in novelty_recall._idea_full_text(node)
    assert "0.02" not in concept_tagging.node_text(node)


def test_the_map_lists_every_renderer():
    doc = concept_tagging.node_text.__doc__ or ""
    for named in ("_idea_tag_text", "_idea_full_text", "_idea_prose"):
        assert named in doc, f"the map does not mention {named}"
    assert "load-bearing" in doc, "and it must say why they are not merged"


def test_the_classifier_surface_still_excludes_the_proposers_own_claim():
    """The reason `node_text` is not simply "everything on the idea": feeding `Idea.concepts` to the
    classifier would let a proposal manufacture the evidence graded-novelty admission relies on."""
    node = Node(id=1, operator="mutate",
                idea=Idea(operator="mutate", concepts=["totally-novel-thing"]))
    assert "totally-novel-thing" not in concept_tagging.node_text(node)
    assert "totally-novel-thing" not in graded_novelty._idea_tag_text(node.idea)
