"""`agents/roles.py` is the role CONTRACTS plus the LLM roles — not six responsibilities (doc 25 AG-02).

The finding measured a 1,058-line module stacking six things, and by 2026-09-08 it was 1,947: the
prompt fragments, the Protocols + attribute registries, the toy backends, the LLM roles with the
state brief, and the wrapper stack. FOUR siblings came out of it — `role_prompts`, `state_brief`,
`role_wrappers`, `toy_roles` — leaving the role CONTRACTS and the LLM roles, which is what the
module is named for. What is pinned here is that the move changed NOTHING, in the three ways a move
of this shape can silently change something.

1. THE PROMPTS ARE THE SAME BYTES. A prompt string is a contract (CLAUDE.md), so relocating ~180
   lines of prose is a correctness claim: a stray edit during the move would change what every run
   asks its model, with no test anywhere to notice. The digest below was computed against the
   PRE-MOVE module — `roles.py` as it stood in the commit before this split, exec'd in a throwaway
   namespace and compared part by part — the same discipline `tests/test_toy_calibration_probe.py`
   applies to the CUDA probe source, for the same reason.
2. EVERY MOVED NAME IS THE SAME OBJECT through both spellings. Two module objects would make every
   existing `monkeypatch.setattr("looplab.agents.roles._state_brief", ...)` a silent no-op, and the
   private-seam registry (`tests/test_cross_package_private_seams.py`) declares two of these names —
   `_state_brief` and `_CONCEPT_AUTHORING_GUIDANCE` — by their `looplab.agents.roles` spelling.
3. NO CYCLE, IN EITHER IMPORT ORDER. `roles.py` imports three of the siblings to re-export them and
   `role_wrappers` reaches back for one registry — deferred, inside the method that reads it. A
   module-level edge there would be green from `import roles` and an ImportError from
   `import role_wrappers`, which is the failure `agents/factory.py` already carries a comment about.

The toy backends are the deliberate exception to (2): they are NOT re-exported, because the
speculation-calibration envelope identifies them by dotted path and one live spelling is the point.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from looplab.agents import role_prompts, role_wrappers, roles, state_brief, toy_roles
from looplab.core.models import Idea

_PKG = Path(__file__).resolve().parents[1] / "looplab"

# The three siblings `roles.py` re-exports. `toy_roles` is absent on purpose — see the docstring.
RE_EXPORTED = (role_prompts, state_brief, role_wrappers)


def _defined_names(module) -> set[str]:
    """Top-level names a module DEFINES (not what it imports), by AST so the list stays two-way."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return {n for n in out if not n.startswith("__")}


# ------------------------------------------------------------------ 1. the prompts are unchanged

def _composed_prompts(m) -> list[str]:
    """Every prompt fragment and every assembled prompt the module can produce, in a fixed order."""
    idea = Idea(operator="draft", params={"x": 1.0}, rationale="r", footprint={"gpus": 2})
    return [
        m._researcher_system(),
        m._researcher_system(offer_sweep=False),
        m._researcher_system(offer_sweep=True, footprint_choice=True),
        m._researcher_capability_suffix(True, False),
        m._researcher_capability_suffix(False, True),
        m._hypothesis_system_suffix(True),
        m._hypothesis_system_suffix(False),
        m._DEVELOPER_SYSTEM,
        m._developer_footprint_guidance(idea),
        m._SWEEP_CONTRACT,
        m._IDEA_SPACE_PLAIN,
        m._UNTRUSTED_MEMORY_RULE,
        m._CONTEXT_BEFORE_TOOLS_RULE,
        m._CONCEPT_AUTHORING_GUIDANCE,
        m._OPERATOR_NOTE,
        m._HYPOTHESIS_INSTRUCTION,
        m.footprint_guidance(),
        m.footprint_guidance(True),
    ]


def test_the_moved_prompts_are_byte_identical_to_before_the_move():
    """The bytes, not the file they live in. If this goes red because a prompt was DELIBERATELY
    changed, re-pin it in the same change and say so — that is a prompt-contract decision, which is
    exactly the decision this pin exists to make visible.

    RE-PINNED 2026-09-08, and this is the decision it was made to surface: the `gpu_footprint_cue`
    OFF branch stopped being the pre-correction paragraph and became SILENCE
    (`_FOOTPRINT_BUDGET_QUIET`). The old text told an unstamped role that declaring more than the
    ceiling buys no hardware, which `resources.py::_acquire_gpus` contradicts — and since
    `gpu_footprint_cue` has no `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` row, nothing ever resumed onto
    those bytes, so there was no in-flight run whose treatment the restoration protected."""
    parts = _composed_prompts(role_prompts)
    assert len(parts) == 18, "a fragment left the composition — extend the pin, do not shrink it"
    blob = "\n\x00\n".join(parts).encode("utf-8")
    assert hashlib.sha256(blob).hexdigest() == (
        "abfd2cb82deb39e8051f70cc1b6ba9c45342d99a51ae6e41cdae97e35058dba1"), (
        "the role prompts changed. If the move was supposed to be verbatim, revert the edit; if a "
        "prompt was changed on purpose, re-pin this digest in the SAME change")


def test_the_researcher_prompt_is_still_assembled_from_the_shared_fragments():
    """Not just "the bytes are stable" — the ASSEMBLY still composes, so a fragment cannot be
    quietly dropped from the composed prompt while its own bytes stay in the file."""
    full = role_prompts._researcher_system()
    for fragment in (role_prompts._RESEARCHER_CORE, role_prompts._CONCEPT_AUTHORING_GUIDANCE,
                     role_prompts._EVAL_TIMEOUT_GUIDANCE, role_prompts._OPERATOR_NOTE,
                     role_prompts._SWEEP_OFFER, role_prompts._FOOTPRINT_HEAD):
        assert fragment in full
    assert role_prompts._SWEEP_OFFER not in role_prompts._researcher_system(offer_sweep=False)
    # The two budget clauses are ALTERNATIVES spliced at one position, never both.
    choice = role_prompts._researcher_system(footprint_choice=True)
    assert role_prompts._FOOTPRINT_BUDGET_CHOICE in choice
    assert role_prompts._FOOTPRINT_BUDGET_QUIET not in choice


# ------------------------------------------------------------- 2. one object through both paths

@pytest.mark.parametrize("sibling", RE_EXPORTED, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_name_a_re_exported_sibling_defines_is_the_same_object_through_roles(sibling):
    """Two-way and derived: a name ADDED to a sibling and forgotten in `roles.py`'s re-export block
    is a red test here, which is what keeps the back-compat surface from silently losing members."""
    missing, copies = [], []
    for name in sorted(_defined_names(sibling)):
        if not hasattr(roles, name):
            missing.append(name)
        elif getattr(roles, name) is not getattr(sibling, name):
            copies.append(name)
    assert not missing, f"{sibling.__name__} defines names `roles` no longer re-exports: {missing}"
    assert not copies, f"{sibling.__name__} names that are COPIES through `roles`, not aliases: {copies}"


def test_roles_re_exports_nothing_it_also_defines():
    """A re-exported name that `roles.py` ALSO defines would resolve to whichever came last — the
    two-module drift the split is supposed to make impossible."""
    own = _defined_names(roles)
    for sibling in RE_EXPORTED:
        assert not (own & _defined_names(sibling)), (
            f"{sibling.__name__} and roles.py both define {sorted(own & _defined_names(sibling))}")


def test_the_toy_backends_are_reachable_only_at_their_own_module():
    """The deliberate exception. `search/speculation_calibration.py`'s runtime descriptor NAMES these
    two classes by dotted path, and that string feeds the calibration receipt's digest — so a second
    live spelling would be a second answer to "which implementation ran"."""
    from looplab.search.speculation_calibration import SPECULATION_RUNTIME_ROLES_DESCRIPTOR

    assert not hasattr(roles, "ToyResearcher") and not hasattr(roles, "ToyObjectiveDeveloper")
    for key, expected in (("researcher", toy_roles.ToyResearcher),
                          ("developer", toy_roles.ToyObjectiveDeveloper)):
        dotted = SPECULATION_RUNTIME_ROLES_DESCRIPTOR[key]
        module, _, attr = dotted.rpartition(".")
        assert getattr(importlib.import_module(module), attr) is expected, (
            f"the calibration descriptor's `{key}` names {dotted}, which is not the class the "
            "engine's calibration gate admits — re-point it (and re-calibrate: the digest moves)")


def test_the_calibration_gate_still_admits_the_moved_toy_pair():
    """DRIVEN through the gate itself, because the descriptor above is a STRING: the engine admits
    the calibration envelope by EXACT type, so a move that left the gate importing the old spelling
    would refuse every calibration run."""
    from looplab.engine.speculation_gate import _calibration_role_pair_errors

    class _Task:
        bounds, step, seed = {"x": (-1.0, 1.0)}, 1.0, 0

    researcher = toy_roles.ToyResearcher(_Task.bounds, seed=_Task.seed, step=_Task.step,
                                         calibration_concepts=True)
    developer = toy_roles.ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True)
    assert _calibration_role_pair_errors(_Task, researcher, developer) == []
    assert _calibration_role_pair_errors(_Task, researcher,
                                         toy_roles.ToyObjectiveDeveloper(noise=0.0))


# --------------------------------------------------------------------- 3. no cycle, either order

def test_the_wrapper_module_takes_its_one_edge_back_into_roles_inside_a_call():
    """AST, so a comment cannot satisfy it and a module-level import cannot hide in a docstring."""
    tree = ast.parse((_PKG / "agents/role_wrappers.py").read_text(encoding="utf-8"))
    module_level = [n.module for n in tree.body
                    if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("looplab.agents")]
    assert module_level == [], (
        f"role_wrappers imports {module_level} at module level; `roles.py` imports role_wrappers to "
        "re-export it, so that edge must stay inside a call or the pair breaks on import order")
    deferred = [n for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module == "looplab.agents.roles"]
    assert deferred, "the registry read moved somewhere else — re-point this guard at it"


@pytest.mark.parametrize("first", ["roles", "role_prompts", "state_brief", "role_wrappers",
                                   "toy_roles"])
def test_importing_any_of_the_split_modules_first_still_works(first):
    """A cycle shows up in ONE order only, so exercise each: a fresh interpreter that imports the
    named module before any of its siblings, then checks the alias identity."""
    probe = (f"import looplab.agents.{first} as m; import looplab.agents.roles as r; "
             "import looplab.agents.role_wrappers as w; import looplab.agents.role_prompts as p; "
             "assert m is not None; "
             "assert r.ValidatingDeveloper is w.ValidatingDeveloper; "
             "assert r._DEVELOPER_SYSTEM is p._DEVELOPER_SYSTEM; print('ok')")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          cwd=str(_PKG.parent))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


# ------------------------------------------------------------------------------- the backstop

def test_roles_is_no_longer_a_god_module():
    """A BACKSTOP for the real property above, which is the domain split — the finding's headline
    was six responsibilities in one file, and its measurement was 1,058 lines growing to 1,947.

    The cap is measured + 1, the discipline `tests/test_agent_factory_split.py` spells out: a cap
    with slack in it is a cap nobody consults. Spending it means asking whether the lines belong to
    the role CONTRACTS and the LLM roles — which is what this module is — or to one of the four
    siblings, or to a fifth.

    `role_prompts.py` 283 -> 302 on 2026-09-08: every added line is the WHY-comment on
    `_FOOTPRINT_BUDGET_QUIET` (the decision that an off-switch may narrow a prompt but may not be
    the value under which it is false). Prompt bytes belong to the fragments; the reasoning behind a
    fragment belongs beside it, so this raise is not the extraction the cap otherwise asks for.

    `roles.py` 787 -> 803 and `role_wrappers.py` 445 -> 466 on 2026-09-08, and the cap did its job
    on the way: `audit_extra_of` was first written into `roles.py`, which put it 33 over, and the
    question the cap forces — do these lines belong to the role CONTRACTS, or to a sibling? — has
    a plain answer. `audit_extra()` is DEFINED in `role_wrappers.py`, twice (the read-through on
    `WrapsDeveloper` and `ValidatingDeveloper`'s own), so its reader belongs beside its implementers
    and moved there; what stays in `roles.py` is the five lines of `DeveloperResult.audit_extra`,
    the envelope field itself, which is exactly what this module is for, plus the one re-export line
    the rule above then demanded — and, later the same day, `last_foresight_pick`, the second
    envelope field of the same shape (a Developer output the engine read off the SHARED instance
    and that the registry cannot hold). An extraction happened and the residue is the contract — so these
    are the same kind of raise as the one above, not a waiver of it.
    """
    caps = {"agents/roles.py": 804, "agents/role_prompts.py": 302, "agents/state_brief.py": 463,
            "agents/role_wrappers.py": 467, "agents/toy_roles.py": 128}
    sizes = {rel: len((_PKG / rel).read_text(encoding="utf-8").splitlines()) for rel in caps}
    over = {rel: (n, caps[rel]) for rel, n in sizes.items() if n >= caps[rel]}
    assert not over, f"module(s) past their cap (measured, cap): {over}"
