"""PART V §22 — CrossRunTools: read-only agentic access to cross-run knowledge.

Pins the tool-provider contract (specs/execute, soft-fail), the three read tools over the §21.20
read-models, the role-scoping of the claim stream (developer sees dev-routed lessons), and the
advisory-only guarantee (the provider exposes NO mutation tool).
"""
from __future__ import annotations

import orjson

from looplab.tools.cross_run_tools import CrossRunTools


def _seed(d, *, lessons=None, capsules=None):
    if lessons is not None:
        (d / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(x) for x in lessons) + b"\n")
    if capsules is not None:
        from looplab.engine.memory import ConceptCapsuleStore
        store = ConceptCapsuleStore(d / "concept_capsules.jsonl")
        for c in capsules:
            store.add(c)


def _lesson(statement, outcome, evidence, *, run_id="r1", role=""):
    return {"statement": statement, "outcome": outcome, "evidence": evidence,
            "run_id": run_id, "task_id": "t", "role": role}


def _cap(run_id, concepts, outcomes):
    from looplab.engine.memory import build_concept_capsule
    return build_concept_capsule(run_id=run_id, fingerprint=["kind:dataset"], direction="max",
                                 concepts=concepts, concept_outcomes=outcomes)


def test_no_memory_dir_offers_no_tools():
    t = CrossRunTools(None)
    assert t.specs() == []


def test_specs_are_read_only():
    t = CrossRunTools("/tmp/whatever")
    names = {s["function"]["name"] for s in t.specs()}
    assert names == {"cross_run_prior_attempts", "cross_run_claims", "cross_run_atlas"}
    # no create/update/delete/ratify tool is exposed — advisory only (§22.4)
    assert not any(re for re in names if any(w in re for w in ("write", "edit", "add", "ratify", "delete")))


def test_prior_attempts_surfaces_tried_concepts(tmp_path):
    _seed(tmp_path, capsules=[_cap("r1", ["hard-neg"], {"hard-neg": 0.88}),
                              _cap("r2", ["hard-neg"], {"hard-neg": 0.90})])
    out = CrossRunTools(tmp_path).execute("cross_run_prior_attempts", {"idea": "try hard-neg mining"})
    assert "hard-neg" in out and "2 run(s)" in out and "surface, not a block" in out


def test_prior_attempts_empty(tmp_path):
    assert "no prior runs" in CrossRunTools(tmp_path).execute("cross_run_prior_attempts", {"idea": "x"})


def test_claims_reports_support_and_opposition(tmp_path):
    _seed(tmp_path, lessons=[
        _lesson("mnr helps", "supported", [1], run_id="rA"),
        _lesson("mnr helps", "tested", [2], run_id="rB"),
    ])
    out = CrossRunTools(tmp_path).execute("cross_run_claims", {})
    assert "mnr helps" in out and "CONTESTED" in out and "1 for / 1 against" in out


def test_claims_contested_filter(tmp_path):
    _seed(tmp_path, lessons=[
        _lesson("solid", "supported", [1]),
        _lesson("contested", "supported", [1], run_id="rA"),
        _lesson("contested", "refuted", [2], run_id="rB"),
    ])
    out = CrossRunTools(tmp_path).execute("cross_run_claims", {"contested": True})
    assert "contested" in out and "solid" not in out


def test_claims_are_role_scoped(tmp_path):
    _seed(tmp_path, lessons=[
        _lesson("researcher insight", "supported", [1], role="researcher"),
        _lesson("developer fix", "supported", [2], role="developer"),
        _lesson("shared note", "supported", [3], role=""),
    ])
    dev = CrossRunTools(tmp_path, role="developer").execute("cross_run_claims", {})
    assert "developer fix" in dev and "shared note" in dev and "researcher insight" not in dev
    res = CrossRunTools(tmp_path, role="researcher").execute("cross_run_claims", {})
    assert "researcher insight" in res and "shared note" in res and "developer fix" not in res


def test_atlas_reports_explored_thin_and_contradictions(tmp_path):
    _seed(tmp_path,
          lessons=[_lesson("mnr helps", "supported", [1], run_id="r1"),
                   _lesson("mnr helps", "tested", [2], run_id="r2")],
          capsules=[_cap("r1", ["hard-neg", "quantization"], {}), _cap("r2", ["hard-neg"], {})])
    out = CrossRunTools(tmp_path).execute("cross_run_atlas", {})
    assert "Portfolio:" in out and "hard-neg" in out
    assert "Thin (1 run" in out and "quantization" in out and "Contradictory" in out


def test_execute_never_raises_on_junk(tmp_path):
    _seed(tmp_path, lessons=[])
    t = CrossRunTools(tmp_path)
    assert isinstance(t.execute("nonexistent_tool", {}), str)
    assert isinstance(t.execute("cross_run_claims", {"contested": "not-a-bool"}), str)


# --------------------------------------------------------------------------- #
# Developer-scoped wiring (§22.5) — the repo developer's read-only scouts include the dev-scoped tool
# --------------------------------------------------------------------------- #

def test_repo_developer_scouts_include_cross_run_when_enabled(tmp_path):
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    d = LLMRepoDeveloper.__new__(LLMRepoDeveloper)      # bare instance (the class's test convention)
    d._cross_run_read_tools = True
    d._cross_run_memory_dir = str(tmp_path)
    d._editables = []
    tools = d._scout_tools()
    crt = [t for t in tools if isinstance(t, CrossRunTools)]
    assert len(crt) == 1 and crt[0].role == "developer"   # role-scoped to the developer


def test_repo_developer_scouts_omit_cross_run_when_off(tmp_path):
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    d = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    d._cross_run_read_tools = False
    d._cross_run_memory_dir = str(tmp_path)
    d._editables = []
    assert d._scout_tools() == []                          # off -> byte-identical to before
