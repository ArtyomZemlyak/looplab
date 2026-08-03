"""Four core-layer contracts that had drifted from what their own comments claimed (doc 25 CO-10..13).

Each is the same species of bug: a load-bearing comment that stopped being true. In a codebase whose
stated convention is "comments are load-bearing" and "stale docs are treated as a bug", a note that
misdescribes a durability helper, a monkeypatch seam, or a projection's callers misleads exactly the
reviewer it was written for — and unlike a wrong assertion, nothing goes red.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from looplab.core import llm, llm_streaming, llm_toolcall, llm_transient
from looplab.core.config import DEVELOPER_BACKENDS, Settings

CORE = Path(llm.__file__).resolve().parent


# ------------------------------------------------------------------ CO-10: the llm re-export seam

def _reexported(module) -> list[str]:
    """Names `llm.py` re-imports from `module`, resolved by object identity rather than by parsing
    the import list — the point of the shim is that both spellings ARE the same object."""
    return sorted(name for name in dir(module)
                  if not name.startswith("__")
                  and getattr(llm, name, None) is getattr(module, name))


@pytest.mark.parametrize("sibling", [llm_transient, llm_streaming, llm_toolcall])
def test_every_split_name_reads_the_same_object_through_the_barrel(sibling):
    """The half of the shim's promise that IS true, and the one existing callers depend on."""
    shared = _reexported(sibling)
    assert shared, f"{sibling.__name__} shares no names with llm.py — the shim was gutted"
    for name in shared:
        assert getattr(llm, name) is getattr(sibling, name)


def _intra_sibling_calls(module) -> set[str]:
    """Names the module defines AND references as a bare global inside itself.

    Those are the ones a `core.llm` monkeypatch cannot reach: the call resolves in the sibling's own
    module globals, so rebinding the alias replaces a name nobody reads."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    defined = {node.name for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in defined}


def test_the_docstring_no_longer_claims_a_monkeypatch_seam_it_does_not_provide():
    """The finding's headline. `_stream_with_idle_guard` calls `_stream_raw_socket` through
    `llm_streaming`'s own namespace, so patching `looplab.core.llm._stream_raw_socket` rebinds only
    the alias and never reaches the live call — a silent no-op, which is precisely the failure the
    registry-guard convention exists to prevent."""
    source = Path(llm.__file__).read_text(encoding="utf-8")
    assert "monkeypatch them THROUGH this module" not in source, "the false claim came back"
    assert "Patch the OWNING module" in source
    assert "silent no-op" in source


def test_the_affected_names_are_real_and_named_where_it_matters():
    """Not an empty warning: at least the streaming pair the finding calls out really is unreachable
    through the alias, and `llm_streaming`'s own docstring says so."""
    unreachable = _intra_sibling_calls(llm_streaming) & set(_reexported(llm_streaming))
    assert "_stream_raw_socket" in unreachable
    streaming_doc = llm_streaming.__doc__ or ""
    assert "MONKEYPATCH THROUGH THIS MODULE" in streaming_doc
    assert "_stream_raw_socket" in streaming_doc


def test_no_test_relies_on_the_broken_direction():
    """The reason this is a documentation fix rather than a refactor: nothing currently patches a
    private llm name through the barrel, so trimming the re-export list would buy churn, not safety.
    If a test ever does, it must patch the owning sibling — this catches the day it doesn't."""
    tests = Path(__file__).resolve().parent
    offenders = [
        f"{path.name}:{index}"
        for path in sorted(tests.glob("*.py"))
        for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if re.search(r"setattr\(\s*(?:\"|')?looplab\.(core\.)?llm(\"|')?\s*,\s*[\"']_", line)
    ]
    assert not offenders, (
        f"a test patches a private name through the core.llm barrel at {offenders}; "
        "patch the owning sibling (llm_streaming / llm_toolcall / llm_transient) instead")


# ------------------------------------------------------------------ CO-11: derived views live in events/

def test_the_belief_view_left_the_model_layer():
    from looplab.core import models
    from looplab.events import belief_projection

    assert callable(belief_projection.grouped_beliefs)
    assert not hasattr(models.RunState, "grouped_beliefs"), (
        "the projection came back onto RunState; derived views live with the other derived views")


def test_the_belief_view_is_still_a_pure_read():
    """It was `Pure, deterministic, and strictly additive` on the model and must stay that way off
    it: a projection that mutated a card would make the board depend on who looked at it."""
    from looplab.events.belief_projection import grouped_beliefs

    source = inspect.getsource(grouped_beliefs)
    assert "st." in source and re.search(r"^\s*st\.\w+\s*=", source, re.M) is None
    assert "card." in source and re.search(r"^\s*card\.\w+\s*=", source, re.M) is None


def test_selection_key_is_no_longer_described_as_caller_less():
    """It IS the metric-first leader `rank_promotion`, `ci_tie_set` and `best_ci` all start from.
    The old "no non-test callers today" note invited exactly the deletion that would break them."""
    from looplab.core import fitness

    doc = fitness.SearchFitness.selection_key.__doc__ or ""
    assert "no non-test callers" not in doc
    source = inspect.getsource(fitness)
    assert source.count("self.selection_key") >= 3, "the live callers moved; re-check the docstring"


# ------------------------------------------------------------------ CO-12: the durability contract

def test_the_strict_writer_documents_its_INDETERMINATE_failure_mode():
    """A failure after the replace but before the parent fsync raises while the destination already
    holds the NEW bytes. A caller that treats the exception as "not written" and rolls back leaves a
    visible record it will never reconcile — so the contract belongs in the docstring, not in a
    review comment a caller never opens."""
    from looplab.core.atomicio import strict_atomic_write_bytes

    doc = strict_atomic_write_bytes.__doc__ or ""
    assert "INDETERMINATE" in doc
    assert "recovery probe" in doc


def test_the_zero_production_callers_claim_is_gone_because_it_is_false():
    source = (CORE / "atomicio.py").read_text(encoding="utf-8")
    assert "ZERO PRODUCTION CALLERS" not in source
    assert "STILL OPEN:" in source, "the Windows gaps the note also recorded must not be lost"

    root = CORE.parent
    callers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "atomicio.py"
        and re.search(r"strict_atomic_write_(text|bytes)\(", path.read_text(encoding="utf-8"))
    }
    assert len(callers) >= 5, f"only {sorted(callers)} — re-check the comment before widening it"


# ------------------------------------------------------------------ CO-13: the enum table

def test_every_closed_vocabulary_field_is_validated():
    """A new enum-ish field added to `Settings` and forgotten here fails SILENTLY: an out-of-set
    value does not raise, it falls through whatever consumes the field as a no-op. A mis-cased
    `LOOPLAB_NOVELTY_MODE=LLM` turned the novelty gate off with no diagnostic."""
    covered = {field for field, _ in Settings._ENUM_FIELDS}
    assert covered == {"trust_gate", "merge_mode", "novelty_mode", "strategist_backend",
                       "eval_trust_mode", "seed_mode", "backend", "developer_backend", "llm_parser"}


@pytest.mark.parametrize("field,bad", [
    ("trust_gate", "audits"), ("merge_mode", "Mean"), ("novelty_mode", "LLM"),
    ("strategist_backend", "Rule"), ("eval_trust_mode", "ratify"), ("seed_mode", "auto "),
    ("backend", "TOY"), ("developer_backend", "aidr"), ("llm_parser", "toolcall"),
])
def test_a_near_miss_value_fails_loudly_and_names_the_vocabulary(field, bad):
    with pytest.raises(ValueError) as info:
        Settings(**{field: bad})
    message = str(info.value)
    assert f"{field} must be " in message and repr(bad) in message


def test_the_two_lazy_vocabularies_resolve_against_their_real_registries():
    """Both are deliberately deferred: `developer_backend` against `DEVELOPER_BACKENDS` so core does
    not execute agents-package code on every `Settings()` (doc 25 XP-04), and `llm_parser` against
    the parser registry so `config` stays import-light. A hard-coded copy of either would accept a
    value the runtime then cannot honour."""
    from looplab.core.parse import _ORDER

    table = dict(Settings._ENUM_FIELDS)
    assert table["developer_backend"]() == DEVELOPER_BACKENDS
    assert set(table["llm_parser"]()) == set(_ORDER)
    for name in DEVELOPER_BACKENDS:
        assert Settings(developer_backend=name).developer_backend == name
    for name in _ORDER:
        assert Settings(llm_parser=name).llm_parser == name


def test_the_if_chain_is_gone():
    source = (CORE / "config.py").read_text(encoding="utf-8")
    assert "_check_trust_gate" not in source, "the misnamed grab-bag validator came back"
    assert source.count('raise ValueError(f"{field} must be') == 1, (
        "the per-field refusal is written more than once again")
