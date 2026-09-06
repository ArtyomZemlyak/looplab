"""The providers every agentic role shares, and the two Memora helpers they compose.

Extracted from `agents/factory.py` on 2026-09-06: the composition root had crossed its 530-line
cap by six lines (the untrusted-evidence envelope's wiring, doc 52 row 13), and the cap's own test
prescribes an extraction over a raise, naming `_shared_providers` as the coherent unit — "the
providers every agentic role shares". `factory.py` re-imports the three names, so
`adapters/tasks.py`'s back-compat re-export, `agents/deep_research.py`'s call-time import and
every `monkeypatch.setattr(factory, "_shared_providers", …)` still reach the same objects.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from looplab.core.evidence import envelope_enabled

if TYPE_CHECKING:                      # `adapters.tasks` re-exports from `factory`, so a runtime
    from looplab.adapters.tasks import TaskAdapter   # import would be a cycle; annotations are strings.


def _memora_cache_path(settings):
    """Where the LLM-abstraction cache lives: an explicit `memora_cache`, else derived from
    `memory_dir` / `knowledge_dir`, else None (in-memory only)."""
    explicit = getattr(settings, "memora_cache", None)
    if explicit:
        return str(explicit)
    if getattr(settings, "memory_dir", None):
        return str(Path(settings.memory_dir) / "memora_cache.json")
    if getattr(settings, "knowledge_dir", None):
        return str(Path(settings.knowledge_dir) / ".memora_cache.json")
    return None


def _make_abstractor(settings):
    """Memora abstractor for the tool-building sites. Returns None unless `memora` is on. When
    `memora_llm` is also on (default), wire a live chat client (via `chat_completer`) so abstractions
    are model-written and CACHED by content hash — degrading to the deterministic lexical abstractor if
    the client can't be built or the endpoint fails at call time. `memora_llm` off = lexical, zero LLM
    calls."""
    if not getattr(settings, "memora", False):
        return None
    from looplab.tools.memora import chat_completer, make_abstractor
    complete = None
    cache_path = None
    if getattr(settings, "memora_llm", False):
        try:
            complete = chat_completer(make_llm_client_for(
                settings, factory=make_llm_client))
        except Exception:  # noqa: BLE001 — a client we can't build just means lexical abstractions
            complete = None
        cache_path = _memora_cache_path(settings)
    return make_abstractor(settings, complete=complete, cache_path=cache_path)



def _shared_providers(task: TaskAdapter, settings, run_dir=None, *, core_only: bool = False,
                      cross_run: bool = True, role: str = "researcher"):
    """The provider list shared by Researcher, Deep Research, agentic Strategist and unified pilot
    (one assembly instead of four near-identical copies). Ordered exactly as the
    call sites historically built it; each site appends its own extras (RepoTools / WebTools) after.

    - Run-introspection (default on): read the run's OWN experiments + the task data mid-loop.
    - Cross-run: read-only access to SIBLING runs of the same task. Needs the run's own dir;
      off without it (parity).
    - `core_only=True` (the pilot stage) stops there; otherwise the memory/knowledge stack follows:
      knowledge base + past cases, lessons/meta-notes, skills (hand-written + promoted M4
      auto-distilled skills under <memory_dir>/skills; candidates stay off the production surface),
      and arXiv literature (network-optional)."""
    providers = []
    if getattr(settings, "researcher_tools", True):
        from looplab.tools.node_diff import NodeDiffTools   # what differs between two nodes
        from looplab.tools.run_tools import DataTools, RunTools
        from looplab.tools.question_board import QuestionBoardTools
        providers += [RunTools(), DataTools(task), NodeDiffTools(),
                      # The QUESTION board as a pull, beside the experiments. The Researcher already
                      # receives the open directions by push (`roles.py::_state_brief`), so what this
                      # adds for THIS role is the children and their deltas — "has anything already
                      # been tried against this question, and what did it measure".
                      QuestionBoardTools()]   # experiments / data / diffs / questions
    if run_dir is not None and getattr(settings, "cross_run_tools", True):
        from looplab.tools.run_tools import SiblingRunTools
        providers.append(SiblingRunTools(Path(run_dir).parent, Path(run_dir).name))   # other runs
    if run_dir is not None and getattr(settings, "all_runs_tools", True):
        from looplab.tools.run_tools import AllRunsTools
        providers.append(AllRunsTools(Path(run_dir).parent, Path(run_dir).name))   # ANY run, any task
    if cross_run and getattr(settings, "memory_dir", None) \
            and getattr(settings, "cross_run_read_tools", False):
        from looplab.tools.cross_run_tools import CrossRunTools   # PART V §22 — read-only cross-run knowledge
        # Built BEFORE the `core_only` return: `core_only` means "skip the heavy memory/KB providers",
        # not "skip cross-run evidence", and the unified pilot (the only core_only caller) is named in
        # the documented CrossRunTools audience. Folding them together silently denied the pilot the
        # Part-V tools its own contract promises. `role` is a parameter for the same reason: this
        # constructor also serves the STRATEGIST, and a hard-coded "researcher" made
        # `_role_lessons` filter every developer-tagged production lesson out of its claims/Atlas/
        # search (an unknown role deliberately sees all roles — tools/cross_run_tools.py::CrossRunTools).
        providers.append(CrossRunTools(settings.memory_dir, role=role, audience="run"))
    if core_only:
        return providers
    cases_path = (str(Path(settings.memory_dir) / "cases.jsonl")
                  if getattr(settings, "memory_dir", None) else None)
    if getattr(settings, "knowledge_dir", None) or cases_path:
        from looplab.tools.knowledge_tools import KnowledgeTools
        from looplab.tools.vectorstore import make_embedder
        providers.append(KnowledgeTools(
            settings.knowledge_dir, cases_path=cases_path,
            embed=make_embedder(settings),                 # KB + memory (T4 embeddings)
            abstract=_make_abstractor(settings),           # harmonic index + anchor-expansion (Memora)
            consolidate_threshold=getattr(settings, "memora_consolidate_threshold", 0.86)))
    if getattr(settings, "memory_dir", None):              # agentic pull of lessons + meta-notes (else injection-only)
        from looplab.tools.memory_tools import MemoryTools
        # THE ROLE TRAVELS, as it already does to `CrossRunTools` above (before 2026-08-30 a
        # Strategist read the store as a Researcher). Safe only beside `memory_tools`' known-role escape.
        providers.append(MemoryTools(settings.memory_dir, role=role))
    # Skills: hand-written (skills_dir) + promoted M4 auto-distilled (<memory_dir>/skills) in ONE
    # SkillTools over BOTH dirs. Candidate auto-skills remain on disk for later promotion but the
    # library's production default hides them. Two separate providers would each register
    # list_skills/use_skill and the second shadows the first (the hand-written library becomes
    # unreachable). Hand-written wins a name clash.
    _skill_dirs: list[str] = []
    if getattr(settings, "skills_dir", None):
        _skill_dirs.append(str(settings.skills_dir))
    if getattr(settings, "memory_dir", None):
        _auto = Path(settings.memory_dir) / "skills"
        if _auto.is_dir():
            _skill_dirs.append(str(_auto))
    if _skill_dirs:
        from looplab.tools.skills import SkillTools
        providers.append(SkillTools(_skill_dirs))
    if getattr(settings, "literature_search", False):       # E3 arXiv grounding (network-optional)
        from looplab.tools.literature import LiteratureTools
        # The tool stamps its own results when the envelope is on (`core/evidence.py`), so the
        # abstracts arrive marked in every loop this provider list reaches.
        providers.append(LiteratureTools(enabled=True, envelope=envelope_enabled(settings)))
    return providers
