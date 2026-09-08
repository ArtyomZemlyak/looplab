"""Role backends (I5, ADR-7). `Researcher` proposes an Idea (params to try);
`Developer` turns an Idea into runnable code. Both are Protocols so an LLM-backed
or external-coding-agent backend drops in with zero orchestrator change.

This module owns the ROLE CONTRACTS — the two Protocols, the duck-typed attribute registries every
engine probe reads through, the `DeveloperResult` envelope and the wrapper-chain resolvers — plus
the LLM-backed Researcher/Developer themselves. Four siblings hold what the finding (doc 25 AG-02)
measured as the other responsibilities of one 1,947-line file, and every name in them is
re-exported below so both spellings resolve to the SAME objects:

* `agents/role_prompts.py`  — the prompt fragments and the suffix assemblers (moved VERBATIM: a
  prompt string is a contract, so the composed prompts are pinned byte-for-byte).
* `agents/state_brief.py`   — the hypothesis-board prompt window, the card binding and `_state_brief`.
* `agents/role_wrappers.py` — `WrapsResearcher` / `WrapsDeveloper` / `bind_state_on` and the
  `ValidatingDeveloper` stack.
* `agents/toy_roles.py`     — the offline `ToyResearcher` / `ToyObjectiveDeveloper` backends, which
  are NOT re-exported here (see that module's docstring: the calibration envelope names them by
  dotted path, so one live spelling is the point).

The toy pair still makes the P0 loop runnable fully offline (no API keys): the Researcher is a
blind seeded optimizer, the Developer emits a script whose executed objective is the ground truth
the Researcher never sees, and together they exercise the real loop deterministically.
"""
from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol

from looplab.core.models import Idea, IdeaEmission, Node, RunState, developer_artifact_footprint
from looplab.core.parse import LLMClient, ParseError, extract_code, parse_structured
from looplab.core.prompts import PromptStore, render

# The CUDA calibration probe moved to its own module (doc 25 AG-02) — it measures a GPU, and
# this file is about role backends. That module then moved DOWN into `core/` (2026-08-14), so the
# sandbox can name it without `runtime` importing `agents`. Re-exported here exactly as before, so
# both spellings name the SAME objects.
from looplab.core.calibration import (  # noqa: F401
    SPECULATION_CUDA_PROBE_ALLOC_BYTES,
    SPECULATION_CUDA_PROBE_CODE_PREFIX,
    SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC,
    SPECULATION_CUDA_PROBE_DEVICE_ORDINAL,
    SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS,
    SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS,
    SPECULATION_CUDA_PROBE_VERSION,
)


# THE SPLIT'S BACK-COMPAT SURFACE (doc 25 AG-02). Every name below moved to a sibling module and is
# re-imported here under its original name, because callers, tests and the private-seam registry
# (`tests/test_cross_package_private_seams.py`) spell them as `looplab.agents.roles.<name>` — and
# an import is an ALIAS, so `looplab.agents.roles._state_brief is looplab.agents.state_brief._state_brief`
# and every existing monkeypatch of either path still names the one object.
from looplab.agents.role_prompts import (  # noqa: F401
    _CONCEPT_AUTHORING_GUIDANCE,
    _CONTEXT_BEFORE_TOOLS_RULE,
    _DEVELOPER_SYSTEM,
    _EVAL_TIMEOUT_GUIDANCE,
    _FOOTPRINT_BUDGET_CHOICE,
    _FOOTPRINT_BUDGET_QUIET,
    _FOOTPRINT_GUIDANCE,
    _FOOTPRINT_HEAD,
    _FOOTPRINT_TAIL,
    _HYPOTHESIS_INSTRUCTION,
    _IDEA_SPACE_PLAIN,
    _OPERATOR_NOTE,
    _RESEARCHER_CORE,
    _SWEEP_CONTRACT,
    _SWEEP_OFFER,
    _UNTRUSTED_MEMORY_RULE,
    _attention_points,
    _developer_footprint_guidance,
    _hypothesis_system_suffix,
    _researcher_capability_suffix,
    _researcher_system,
    footprint_guidance,
)
from looplab.agents.state_brief import (  # noqa: F401
    BOARD_PROMPT_CARDS,
    BOARD_PROMPT_SEED_BUDGET_CHARS,
    BOARD_SEED_CHARS_MAX,
    _state_brief,
    attempted_board_prompt_cards,
    bind_idea_to_board_card,
    board_prompt_lines,
    next_board_prompt_cards,
)
# `role_wrappers` reaches back into this module for `DEVELOPER_OUTPUT_ATTRS` — DEFERRED, inside the
# one method that reads it — so this import is the only module-level edge between the pair and it
# holds in either import order. A module-level import there instead is green from `import roles` and
# an ImportError from `import role_wrappers`; `tests/test_role_module_split.py` drives both orders.
from looplab.agents.role_wrappers import (  # noqa: F401
    ValidatingDeveloper,
    WrapsDeveloper,
    WrapsResearcher,
    audit_extra_of,
    bind_state_on,
)


class Researcher(Protocol):
    def propose(self, state: RunState, parent: Optional[Node]) -> Idea: ...
    # OPTIONAL (Variant-1 Phase 2): a backend MAY expose `propose_batch(state, n) -> list[Idea]` that
    # returns up to N ideas on DISTINCT axes in one pass. The engine probes for it via getattr and, when
    # absent, degrades to N sequential `propose` calls with an avoidance directive (engine `_propose_batch`)
    # — so implementing it is a diversity/latency optimization, never required.


class Developer(Protocol):
    def implement(self, idea: Idea) -> str: ...


# Duck-typed OUTPUT attributes the engine reads off the ACTIVE Developer/Researcher after a
# call (docs/15 §P4.3) — the mirror of RESEARCHER_HINT_ATTRS for the outbound direction. The
# engine reads them with `getattr(..., default)`, so a one-sided rename historically failed
# SILENTLY (an empty node shipped with no diagnostic; the pilot quietly reverted to the static
# policy). `tests/test_role_output_contract.py` source-scans BOTH sides against these tuples:
# every consumer getattr and every producer assignment must use exactly these names, and every
# delegating wrapper (ValidatingDeveloper, best-of-N, the foresight panel) must forward them.
DEVELOPER_OUTPUT_ATTRS: tuple[str, ...] = (
    "last_files", "last_deleted", "last_footprint",
    # CLI-agent (ADR-7) developer outputs: the validation report the engine's audit emitter
    # reads (engine/audit.py `_emit_agent_report`), and the seed/process/patch evidence the
    # ValidatingDeveloper's checks consume. Surfaced by the contract test's own first run —
    # the original census had missed all four.
    "last_report", "last_seed", "last_run", "last_patch",
    # The repair session's STAGE ROLLBACK request: the suspect EARLIER stage this repair blamed, ""
    # for none (`adapters/repo_developer.py::_repair_emit_spec` produces it,
    # `engine/evaluate.py`'s attempt loop consumes it through `engine/eval_stages.py::_rollback_start`).
    # It belongs in this registry for the sharpest version of the reason the tuple exists: the
    # consumer's default is the FALSY one, so a rename would read as "no rollback was requested" on
    # every attempt of every node — the feature silently ceasing to exist, with nothing red anywhere,
    # and the only visible symptom a repair loop that keeps failing at `train` for reasons it already
    # correctly diagnosed.
    "last_rollback_stage",
    # WHICH BOUND ENDED THE SESSION ("time" / "turns"), "" when it finished on its own terms. Same
    # registry argument as `last_rollback_stage` above and the same falsy default, so a one-sided
    # rename would read as "no session was ever cut short" on every node — which is exactly the
    # reading this attribute exists to stop being the only one available. `tool_loop.py` has
    # announced this through `on_budget` since it was written and nothing subscribed; measured over
    # `runs/`, 12 of the 12 `inert` repairs in the corpus ran past their wall clock and 0 of the 65
    # that finished inside it are inert, so `inert` alone cannot tell "decided not to edit" from
    # "ran out of clock mid-investigation".
    "last_budget_exhausted",
    # THE NUMBERS BEHIND THAT WORD: {"kind", "seconds", "detail"}, {} when nothing was cut.
    # A second attribute rather than a wider `last_budget_exhausted`, because that one is a
    # durable vocabulary other code compares against a KIND and prose in it would break every
    # such reader. Registered for the reason the whole tuple exists and for one more: the
    # default here is `{}`, so a rename would report "this session was never cut" for every
    # step of every run -- and the corpus reading it is trying to settle whether the money
    # ceiling ever fires (docs/56 §85). A silent falsy default would answer that question
    # wrongly and look like data.
    "last_budget_facts",
    # HOW MANY EDIT/WRITE/DELETE CALLS THE SESSION MADE, refusals included. `last_budget_exhausted`
    # above tells "the clock ended it"; this tells whether the session ever TRIED to change a file,
    # and the two together separate "ran out of time mid-edit" from "read for 25 minutes and never
    # reached for the write surface". Measured over every inert repair with spans (v11 x2, v13 x2):
    # ZERO edit calls in sessions of 22.5-27.3 minutes, which is why the budget lever was refused.
    "last_edit_calls")
RESEARCHER_ACTION_ATTRS: tuple[str, ...] = ("choose_action",)


@dataclass(frozen=True)
class DeveloperResult:
    """The IMMUTABLE envelope of ONE Developer call (doc 27; doc 52 row 12).

    A Developer returns `str` and leaves everything else on the INSTANCE — the eleven
    `DEVELOPER_OUTPUT_ATTRS` side channels above, which the engine read back with `getattr` after
    the call. That contract held only while nothing else could touch the instance between the
    call and the reads, and the thing that guaranteed it was an accident: the paid call ran ON THE
    EVENT LOOP THREAD, so no sibling could run while it did. `engine/evaluate.py`'s repair path
    already had to snapshot five of the channels "IMMEDIATELY, before any `await`" and carry a
    comment about which sibling's edits a late read would attribute to this node. The freeze was
    the serialiser, and offloading the call (which the loop-liveness measurements demanded — zero
    ticks for a 116-276 s median hold, one recorded 88.3 min) removes it.

    So the outputs become a RETURN VALUE: `engine/node_build.py::_capture_developer_result` reads
    every registered channel off the instance in the same breath as the call, under the instance's
    own lock (`developer_call_lock`), and hands back this frozen record. Field names ARE the
    registry names so the two cannot drift (`tests/test_developer_result.py` pins the field set to
    `DEVELOPER_OUTPUT_ATTRS` plus `code`), `last_files` is a read-only mapping and `last_deleted` a
    tuple, so no consumer can mutate a record another consumer is still reading. `failed(code)` is
    the envelope for a call that RAISED: the sentinel code and every channel at its default, which
    is what the engine used to read off an instance that had not run.
    """

    code: Any
    last_files: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    last_deleted: tuple[str, ...] = ()
    last_footprint: Optional[dict] = None
    last_report: Any = None
    last_seed: Any = None
    last_run: Any = None
    last_patch: Any = None
    last_rollback_stage: str = ""
    last_budget_exhausted: str = ""
    last_budget_facts: Any = None
    last_edit_calls: int = 0
    # THE ONE FIELD THAT IS NOT A REGISTRY MEMBER, and the exception is stated rather than assumed:
    # `DEVELOPER_OUTPUT_ATTRS` registers ATTRIBUTES a Developer assigns, and `audit_extra()` is a
    # METHOD a wrapper offers, so it can never be a member. It is captured because it annotates
    # exactly the call this envelope IS, and because it was the last channel read off the SHARED
    # instance after the lock — see `engine/audit.py::_emit_agent_report`, the site that decides
    # it, for the race and its measurement. Filled by `role_wrappers.py::audit_extra_of`.
    audit_extra: Optional[dict] = None
    # THE SECOND NAMED NON-REGISTRY FIELD, and for the same reason as `audit_extra`: it is a
    # Developer output the engine reads with `getattr` after the call, but it is not in
    # `DEVELOPER_OUTPUT_ATTRS` — `search/best_of_n.py` writes it inside `implement` and CLEARS it
    # inside `repair`/`repair_from` ("repair uses no predictive ranker"). So a repair on the SHARED
    # developer, running in another worker, nulls the pick a build just made: driven, the build's
    # `foresight_selected` was silently never written, and in the mirror order one node's pick is
    # emitted against another's id. Adding it to the registry would be wrong — the registry's
    # members are mirrored by `WrapsDeveloper`/`ValidatingDeveloper` and consumed on emit, and this
    # one is neither — so it is carried here and named in the field-set pin, like `audit_extra`.
    last_foresight_pick: Optional[dict] = None

    @classmethod
    def failed(cls, code: str) -> "DeveloperResult":
        return cls(code=code)
# One `RLock` per Developer INSTANCE, so a call and the capture of its outputs are one atomic
# step: two repairs offloaded to two worker threads on the SAME shared instance now queue on it
# instead of interleaving their `last_*` writes. Keyed weakly so a pooled per-build Developer is
# collected with its lock; an object that cannot be weakly referenced (a `SimpleNamespace` stub,
# a slotted class) falls back to an id-keyed table, which a test process never outgrows.
_DEVELOPER_LOCKS: "weakref.WeakKeyDictionary[Any, threading.RLock]" = weakref.WeakKeyDictionary()
_DEVELOPER_LOCKS_BY_ID: dict[int, threading.RLock] = {}
_DEVELOPER_LOCKS_GUARD = threading.Lock()


def developer_call_lock(developer) -> threading.RLock:
    """The lock a Developer call and its output capture run under — see `DeveloperResult`."""
    with _DEVELOPER_LOCKS_GUARD:
        try:
            lock = _DEVELOPER_LOCKS.get(developer)
            if lock is None:
                lock = threading.RLock()
                _DEVELOPER_LOCKS[developer] = lock
            return lock
        except TypeError:
            return _DEVELOPER_LOCKS_BY_ID.setdefault(id(developer), threading.RLock())

# The RESEARCHER's outbound ASSIGNMENTS, the mirror of `DEVELOPER_OUTPUT_ATTRS` above.
# `RESEARCHER_ACTION_ATTRS` could not hold these: its contract test needle-checks `def {attr}(`,
# because an action is a METHOD. These are values a role writes during a call and the engine reads
# after it, so they need the assignment-scanning half of the same contract.
RESEARCHER_OUTPUT_ATTRS: tuple[str, ...] = (
    # WHICH BOUND ENDED THE PROPOSE LOOP ("turns" / "time"), "" when the model emitted on its own
    # terms. Exactly the `last_budget_exhausted` argument the Developer half already carries, and
    # for a sharper reason here: `agent_max_turns` and `agent_time_budget_s` both ship at 0 (no
    # cap), so today the turn count IS where a proposal converged — which is what makes the
    # measured distribution (24..319 turns over v11's nineteen proposals, median 62) trustworthy.
    # The moment any cap is set, a TRUNCATED proposal and a CONVERGED one become indistinguishable
    # in the record unless this is carried. `tool_loop.py::_note_budget` has announced it through
    # `on_budget` since it was written; `on_budget` is in `EXPLICIT_ONLY_LOOP_ARGS`, so it can only
    # ever arrive at a call site by hand, and the Researcher's was the one that never passed it.
    "last_budget_exhausted",
    # THE SAME RECEIPT UNDER A ROLE-SCOPED NAME, and it is not a duplicate — it is what keeps the
    # two receipts apart on ONE object. Under the shipped `Settings.unified_agent`,
    # `agents/factory.py::make_roles` returns the SAME `UnifiedAgent` as both roles, so
    # `self.researcher` and `self.developer` are one object and both halves were writing
    # `last_budget_exhausted` on it: `propose` mirrored the inner researcher's cutoff, and
    # `WrapsDeveloper._sync_audit` mirrored the inner developer's after each code stage. Whichever
    # ran last won, and `engine/evaluate.py` stamps the DURABLE `node_repaired.budget_exhausted`
    # from it — so a repair whose delegate raised (swallowed by `_evaluate`'s own
    # `except Exception as _repair_exc`, which leaves `_sync_audit` unreached) recorded the value a
    # PROPOSAL had written minutes earlier, as a fact about a repair that had no budget cutoff.
    #
    # A plain (non-facade) researcher writes only `last_budget_exhausted` and is not also a
    # developer, so both spellings are read through `researcher_budget_exhausted` below.
    "last_propose_budget_exhausted",
    "last_hyp_priority", "last_foresight", "last_foresight_pick")  # foresight telemetry; doc 64


def researcher_budget_exhausted(researcher) -> str:
    """WHICH BOUND ENDED THIS ROLE'S LAST PROPOSE — "turns" / "time" / "" — from either spelling.

    THE FALLBACK IS ON ABSENCE, NOT ON EMPTINESS, and that distinction is the whole rule. On a
    `UnifiedAgent` the plain `last_budget_exhausted` carries the DEVELOPER's last code stage, so
    falling back whenever the scoped name is merely EMPTY would report a budget-cut repair as this
    proposal's cutoff — the same confusion in the other direction. An object that DEFINES the scoped
    slot has answered for the researcher role, "" included; only an object that does not define it
    at all is a plain researcher, which writes the one name and is not also a developer.
    """
    # Both names spelled as LITERALS: `tests/test_role_output_contract.py` scans for
    # `getattr(<expr>, "<attr>")`, so a loop over a tuple of names would make this reader invisible
    # to the registry that exists to catch a one-sided rename.
    scoped = getattr(researcher, "last_propose_budget_exhausted", None)
    if scoped is not None:
        return str(scoped or "").strip()[:32]
    return str(getattr(researcher, "last_budget_exhausted", "") or "").strip()[:32]

# Duck-typed attributes that answer "does building one node make provider calls at all?" — the seam
# `engine/orchestrator.py::_build_calls_an_llm` reads, and the other half of the same AUTO width
# decision `adapters/tasks.py::TASK_OPTIONAL_HOOKS::gpu_capable` already covers. Registered for the
# same reason every other duck-typed seam here is: the read is `getattr(obj, name, default)`, so a
# rename on the PRODUCER side cannot fail loudly, and a false answer is not a crash — it silently
# reshapes the run (measured on the real `UnifiedAgent` shape: True -> False, `llm_parallel` 4 -> 1,
# `speculation_depth` 4 -> 0, no test red).
#
# `tests/test_build_llm_probe_contract.py` source-scans BOTH directions: every attribute the
# predicate probes must be listed, and every listed attribute must still be a live public handle on
# the shipped roles/facade.
LLM_PRESENCE_ATTRS: tuple[str, ...] = (
    # Every LLM-backed role carries the shared client; wrappers forward it read-through (see
    # `WrapsDeveloper` below) and `search/foresight.py`'s panel proxies it.
    "client",
    # An external coding-agent Developer (`agents/cli_agent.py`) holds no client and declares this
    # instead — it spends provider latency through its own CLI.
    "is_code_generating",
)

# The `UnifiedAgent` facade's PUBLIC per-stage handles, which the same predicate descends into.
# Load-bearing precisely because the facade's own `client`/`is_code_generating` forwarders come from
# `WrapsDeveloper` and therefore describe the DEVELOPER stage only: under the shipped
# `unified_agent=True` one object plays both roles, so on every adapter with a templated Developer
# but an `LLMResearcher` (classification, regression, timeseries) the forwarders read a client-less
# template while the run calls the provider once per node. These handles are what re-open the facade.
#
# They are ALREADY public for a second consumer — the cost roll-up walk names them in
# `engine/costs.py::_CHILD_ATTRS`, so a per-stage CostAccountant is reachable — but that consumer
# only loses BILLING when a handle is renamed, which is why it never caught this. Keep both consumers
# in mind before making one of these private.
FACADE_STAGE_ATTRS: tuple[str, ...] = ("researcher", "developer", "stage_clients")

# TWO KNOWN IMPRECISIONS, both accepted, both outside any shipped wiring — recorded so the next
# reader does not "fix" one into a real defect. The predicate is a WIDTH heuristic, so it is tuned to
# be wrong in the cheap direction, and the two directions are not symmetric:
#
# * OVER-approximation (answers True with no build latency): `stage_clients` holds the clients no
#   per-stage backend owns — strategy and pilot — so a `UnifiedAgent` whose researcher AND developer
#   are both templates still answers True if a Strategist client was threaded in. Cost: AUTO fans a
#   local build out and the log's byte ORDER stops being reproducible. Kept because narrowing it means
#   naming which entries are build-facing, and `stage_clients` is deliberately an opaque list for the
#   cost roll-up; no shipped factory builds that shape (a templated pair never gets a stage client).
# * UNDER-approximation (answers False with real latency): a role that holds its client privately, or
#   whose property raises, reads as "no LLM" — `_build_calls_an_llm` swallows the exception on
#   purpose, because a proxy that raises is not evidence of a provider call. Cost is only lost
#   fan-out, never a wrong result. A role that wants the fan-out declares one of LLM_PRESENCE_ATTRS.
#
# The b89b0209 regression was a THIRD shape and is not in this list because it was not acceptable:
# the shipped default hit it on three adapters, and the cost was a run serialized against its own
# per-node provider call.

# The SUBSET of RESEARCHER_HINT_ATTRS that both researchers splice into their PROMPT (doc 25 AG-10).
# Both `LLMResearcher.propose` and `ToolUsingResearcher.propose` had this as an inline literal, under
# a docstring promising "both researchers honor the same cues" — a promise nothing checked.
# `tests/test_hint_forwarding.py` scans the setattr/forwarding sites, not these read-side literals,
# so a cue added to the registry and to only ONE call site desynchronises the two prompts silently:
# the agentic path and the plain path would ask the model different questions and no test would care.
#
# A strict subset by design. `_digest_cap` is a numeric cap consumed separately, `_hyp_order` orders
# the open-hypothesis board inside `_state_brief`, `_memo_verdict_cue` is a BOOLEAN threaded into
# `_state_brief` the same way (config, not prose — the `_digest_cap` shape exactly), and
# `_novelty_stance` / `_steering_context` /
# `_cross_run_advisory_receipt` are read structurally rather than concatenated as prose.
#
# The two BUDGET cues are LAST on purpose, and in this order. `_complexity_hint` already carries the
# GPU RESOURCE CONTRACT cue, which announces the POOL SIZE ("this pool exposes at most 2 GPU(s)"), and
# the experiment TIME-BUDGET cue; each budget states the CEILING for its own axis, and a reader that
# saw the pool and then the ceiling ends on the ceiling — which is the number it must actually act on.
# The two are independent: `_gpu_budget_hint` names no wall clock and `_time_budget_hint` names no
# device count, so appending the second does not put a number after the first on the first's own axis.
# `_time_budget_hint` goes last because the wall clock is the axis a proposal gets wrong LATEST — the
# schedule is chosen after the hardware is (docs/29 F1h).
RESEARCHER_PROMPT_CUES: tuple[str, ...] = (
    "_complexity_hint", "_sweep_hint", "_novelty_feedback", "_novelty_hint", "_gpu_budget_hint",
    "_time_budget_hint")

RESEARCHER_HINT_ATTRS: tuple[str, ...] = (
    "_digest_cap", "_complexity_hint", "_sweep_hint", "_novelty_feedback", "_novelty_hint",
    "_novelty_stance", "_hyp_order", "_steering_context", "_cross_run_advisory_receipt",
    "_gpu_budget_hint", "_time_budget_hint", "_memo_verdict_cue", "_gpu_footprint_cue")
"""Ephemeral hint attributes communicated to the ACTIVE Researcher via `setattr` and consumed
with `getattr(obj, name, default)`. Writers: the engine (`_digest_cap` in orchestrator.py
`__init__`; `_complexity_hint`/`_sweep_hint` in engine/proposal_cues.py `_set_complexity_hint`;
`_novelty_hint` + `_novelty_stance` in proposal_cues.py `_stamp_novelty_hint`;
`_cross_run_advisory_receipt` in proposal_cues.py's advisory stamp, read back off the researcher
handle in orchestrator.py `_node_audit_extra` and speculation.py's per-build capture;
`_gpu_budget_hint` in proposal_cues.py `_stamp_gpu_budget_hint` — the PER-EXPERIMENT GPU ceiling
`_FOOTPRINT_GUIDANCE` asks the Researcher to size `footprint.gpus` against and, before this hint
existed, could only learn from the operator's goal prose (docs/29 F1b);
`_time_budget_hint` in proposal_cues.py `_stamp_time_budget_hint` — the same fact one axis over: the
per-eval WALL-CLOCK ceiling the SCHEDULE has to fit, plus what this role's own `eval_timeout` may do
about it (governed by `agent_control.timeout`, clamped by `max_eval_timeout`), which
`_EVAL_TIMEOUT_GUIDANCE` asks for and scopes nowhere (docs/29 F1h);
`_novelty_feedback` in engine/novelty.py's gate) and the
foresight panel (search/foresight.py `_prioritize_board` sets `_hyp_order` — the predicted
best-first board order — on its wrapped researcher). Readers: `LLMResearcher.propose` (below)
and agent.py's `ToolUsingResearcher.propose` read the text cues and thread `_hyp_order` into
`_state_brief`; the foresight ranker reads `_novelty_stance` (the stance VALUE behind the
`_novelty_hint` prose).

THIS TUPLE IS THE DELIVERY CONTRACT (P2, docs/PROMPT_REVIEW.md): the engine setattrs hints on
the OUTERMOST active researcher, and EVERY wrapper that delegates propose() mirrors ONLY this
registry (plus the non-hint `track_hypotheses` knob) onto its delegate. The forwarding wrappers:
the foresight panel (`search/foresight.py::ForesightPanelResearcher._forward_hints`), the
`UnifiedAgent` facade (`agents/unified_agent.py::UnifiedAgent.propose`), the surrogate wrapper
(`search/surrogate.py::SurrogateResearcher.propose`), and the empirical panel
(`search/panel.py::PanelResearcher.propose`). An attribute missing here silently dies at the
first wrapper — exactly how board prioritization was dead in the default config. Keep it in
sync with every `setattr(self.researcher, "...")` / `setattr(self.base, "...")` site;
tests/test_hint_forwarding.py scans those sites AND wires the real wrapper chains to enforce it.

Both researchers honor the same cues: `LLMResearcher.propose` and `ToolUsingResearcher.propose`
fold the same `RESEARCHER_PROMPT_CUES` set into their prompts through `collect_hint_cues` — neither
re-derives the tuple, so a cue added here reaches BOTH prompts or NEITHER (`_digest_cap` is consumed
separately as a numeric cap; `_hyp_order` orders the open-hypothesis board inside `_state_brief`)."""


def forward_hints(src, dst) -> None:
    """Mirror the engine-set ephemeral hints from a wrapper onto its delegate — the ONE owner of
    the `(*RESEARCHER_HINT_ATTRS, "track_hypotheses")` forwarding rule every wrapper shares.

    P2 delivery contract (see RESEARCHER_HINT_ATTRS above): the engine setattrs hints on the
    OUTERMOST active researcher — which may be any of the forwarding wrappers — so each wrapper
    mirrors the registry (plus the non-hint `track_hypotheses` knob, likewise poked onto the
    outermost object; an explicit OFF must not be shadowed) onto its delegate before delegating
    propose(). hasattr-guarded: an attr the engine never set is left untouched on `dst`. Without
    this, a wrapper silently dropped every engine hint on its delegation path. Callers:
    `UnifiedAgent.propose`, `ForesightPanelResearcher._forward_hints`,
    `SurrogateResearcher.propose`, and search's `PanelResearcher.propose` — one helper, so the
    rule can't drift per-wrapper (tests/test_hint_forwarding.py wires the real chains)."""
    for attr in (*RESEARCHER_HINT_ATTRS, "track_hypotheses"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))


def role_wrapper_chain(researcher, developer) -> tuple:
    """The `researcher → researcher.inner → researcher.fallback → developer` lookup order, once.

    The active Researcher is frequently a WRAPPER (surrogate, panel, foresight proxy, unified
    facade), and the things the engine needs off a role — the configured structured-output parser,
    the PromptStore, the LLM client — live on whichever link actually carries them. Four call sites
    had hand-rolled this walk (doc 25 EC-13), and a hand-rolled copy is precisely where a wrapper
    link gets missed: the resolution then silently falls back to a DEFAULT rather than failing, so
    a run quietly uses `tool_call` parsing against a provider configured for JSON, or distils
    lessons with no PromptStore override.

    Returned as a plain tuple including `None` holes, so callers keep using `getattr(obj, ...)`
    with its own None-tolerance rather than needing a second filtering rule."""
    return (researcher, getattr(researcher, "inner", None),
            getattr(researcher, "fallback", None), developer)


def resolve_role_parser(researcher, developer, *, default: str = "tool_call") -> str:
    """First truthy `.parser` along the wrapper chain, else `default`.

    Truthy rather than `is not None`: an empty parser name is not a configured parser, and treating
    it as one would pass "" to the structured-output layer instead of the default."""
    return next((p for o in role_wrapper_chain(researcher, developer)
                 if (p := getattr(o, "parser", None))), default)


def resolve_role_prompts(researcher, developer):
    """First non-None `.prompts` (a PromptStore) along the wrapper chain, else None.

    `is not None` rather than truthy here, and deliberately: an EMPTY PromptStore is a wired store
    that happens to override nothing, and skipping past it would keep walking into a wrapper that
    was never configured."""
    return next((p for o in role_wrapper_chain(researcher, developer)
                 if (p := getattr(o, "prompts", None)) is not None), None)


def resolve_role_client(researcher, developer):
    """First usable LLM client along the wrapper chain, else None.

    `hasattr(c, "complete_text")` is the load-bearing half: toy backends carry a `client` attribute
    that is not an LLM client at all, and returning one would turn a "no LLM wired, skip the
    advisory step" path into an AttributeError inside distillation."""
    for obj in role_wrapper_chain(researcher, developer):
        c = getattr(obj, "client", None)
        if c is not None and hasattr(c, "complete_text"):
            return c
    return None


def collect_hint_cues(obj, attrs) -> str:
    """Concatenate the given engine-set hint attributes (a subset of
    `RESEARCHER_HINT_ATTRS`) off `obj` in order, each defaulting to "" when unset — the
    shared rendering pattern the Researcher prompts use. Purely mechanical: byte-identical
    to the per-attribute `getattr(obj, name, "")` concatenation it replaces."""
    return "".join(getattr(obj, name, "") for name in attrs)


# --------------------------------------------------------------------------- #
# LLM-backed backends (I2, ADR-7/14). Same Protocols; swap-in needs no loop change.
# Tested against a fake LLMClient (no live calls); go-live needs a model endpoint.
# --------------------------------------------------------------------------- #


# The Researcher's IN-BAND degraded-proposal sentinel — the exact twin of the Developer's
# `core/models.py::DEVELOPER_ERROR_PREFIX` / `is_developer_error`, and it exists for the same reason.
#
# Every LLM role degrades on purpose (see `agents/preflight.py`'s docstring for the stacked chain),
# and for the Researcher the degradation is an Idea with NO params, NO hypothesis, and an error string
# where the rationale should be. That is not a weak experiment, it is the ABSENCE of one — but it used
# to be indistinguishable from a real proposal by the time the engine saw it, so a provider that died
# MID-RUN was never noticed on the proposal path. Measured live (`/tmp/ll-s4b/run`, provider killed
# after node 0): three further nodes with byte-identical bounds-midpoint params, the transport error
# spliced into the hypothesis board, the node rationale, the research memo AND the durable cross-run
# case, a champion declared over them, `run_finished` with no reason, and exit 0.
#
# IN-BAND on the rationale, deliberately, rather than an attribute on the role. `RESEARCHER_HINT_ATTRS`
# is a DELIVERY contract that every wrapper must mirror by hand (the foresight panel, the UnifiedAgent
# facade, the surrogate and empirical panels), and an attribute missing from one of them dies silently
# at that wrapper. The Idea is the one thing every wrapper is obliged to return, so a sentinel carried
# ON it reaches the engine through all of them by construction — which is exactly why the Developer's
# crash sentinel rides its CODE.
RESEARCHER_FALLBACK_PREFIX = "fallback ("


def researcher_fallback_rationale(what: str, cause) -> str:
    """The one spelling of a degraded proposal's rationale. `what` names which stage gave up."""
    return f"{RESEARCHER_FALLBACK_PREFIX}{what}: {cause})"


def is_researcher_fallback(idea) -> bool:
    """True when `idea` is a role's degraded FALLBACK rather than a proposed experiment.

    Total and duck-typed: anything without a readable string rationale is a real proposal, so a
    plugin Researcher returning an exotic object can never be mistaken for a dead provider.
    """
    try:
        rationale = getattr(idea, "rationale", None)
    except Exception:  # noqa: BLE001 - a hostile/read-only surface is not evidence of a crash
        return False
    return isinstance(rationale, str) and rationale.startswith(RESEARCHER_FALLBACK_PREFIX)


def researcher_fallback_cause(idea) -> str:
    """The captured provider/parse error inside a degraded proposal, for the operator-facing pause."""
    if not is_researcher_fallback(idea):
        return ""
    text = str(getattr(idea, "rationale", "") or "")[len(RESEARCHER_FALLBACK_PREFIX):]
    return text[:-1].strip() if text.endswith(")") else text.strip()


def _clamp_fill(idea: Idea, bounds: Optional[dict]) -> Idea:
    """Clamp numeric params into bounds and fill any missing ones with the midpoint, so
    a stray/empty proposal can't crash the objective. A SWEPT dimension (present in `idea.space`) is
    left to its grid ENTIRELY — neither filled nor clamped — because the grid, not the task bounds,
    is what the Developer actually runs for that dimension.

    Midpoint-filling a swept dim would inject a spurious 'fixed at X' param the Developer prompt
    renders ALONGSIDE the sweep grid ('sweep degree in [1,2,3]' AND 'degree=3.0'), telling the model
    the swept dim is simultaneously fixed; the sweep-offer contract keeps swept dims out of params
    on purpose. CLAMPING one is worse, and is why the exemption now covers both branches. Direct
    mutation here bypasses `Idea._clamp_params_to_space`, so a bounds clamp on a swept key can push
    `params[k]` OUTSIDE its own `space[k]` grid and leave the Idea no longer a FIXED POINT of its own
    validators. Every durable Card action digest is minted from those params and re-derived by
    rebuilding the Idea from the Card — which re-runs the space clamp and gets a different number —
    so such a Card can never be claimed again and the create lane spins on it forever. Measured live
    (`/tmp/ll-s1/spec`): the Researcher proposed `iters=5000` with `space.iters=[1000,5000]` against
    this task's `iters` bound of (10, 500); the clamp wrote `params.iters=500`, reconstruction snapped
    it back to 1000, `_prepare_existing_card_claim` refused the mismatch on every turn, and the run
    burned 74 loop turns in one second before dying "stuck: 1 action(s) planned … without creating a
    node". A swept dim's params entry is redundant with its grid, so skipping it costs nothing."""
    if bounds:
        swept = set(getattr(idea, "space", None) or {})
        for k, (lo, hi) in bounds.items():
            if k in swept:
                continue                          # the grid owns this dimension — see the docstring
            if k in idea.params:
                idea.params[k] = max(lo, min(hi, float(idea.params[k])))
            else:
                idea.params[k] = (lo + hi) / 2.0
    return idea


class LLMResearcher:
    """Proposes an `Idea` via structured output (tool_call default, baml fallback).

    `space_hint` describes the task's parameter space in the prompt; `bounds` clamps
    (and fills missing) numeric params so a small model's stray proposal can't crash
    the objective — quality robustness, not a correctness crutch."""

    def __init__(self, client: LLMClient, space_hint: str = "",
                 bounds: Optional[dict] = None, parser: str = "tool_call",
                 prompts: Optional[PromptStore] = None, track_hypotheses: bool = True,
                 offer_sweep: bool = True):
        self.client = client
        self.space_hint = space_hint
        self.bounds = bounds
        self.parser = parser
        self.prompts = prompts
        self.track_hypotheses = track_hypotheses   # P1: ask for the per-experiment hypothesis (default on)
        # P6: offer the intra-node sweep only when the active Developer implements `idea.space`
        # (make_roles sets this post-construction; default True keeps direct constructions as-is).
        self.offer_sweep = offer_sweep

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # Operator steering (Phase 5 `hint` control events): fold them into the prompt so a live
        # human can nudge the search ("try higher degree", "focus on regularization"). Advisory —
        # the model still proposes; bounds still clamp.
        from looplab.agents.hints import render_hint_directives
        hint_block = render_hint_directives(state.pending_hints)
        # Engine-set hint cues (see RESEARCHER_HINT_ATTRS; each empty when off), in order:
        # - _complexity_hint — A0d: an engine-set complexity cue keyed on the operated node's breadth.
        # - _sweep_hint — Strategist `prefer_sweep` bias: nudges — but never forces — the Researcher
        #   toward an intra-node sweep when the cost model favors in-process execution.
        # - _novelty_feedback — T5 novelty-gate feedback (one re-propose): "you already tried X, it
        #   failed because Y — propose something meaningfully different". Empty in the normal path.
        # - _novelty_hint — slice 2/4: the Strategist's novelty stance directive + coverage gaps
        #   (EXPLORE a new theme / EXPLOIT the leader). Empty when stance is "balanced" (today).
        cues = collect_hint_cues(self, RESEARCHER_PROMPT_CUES)
        hyp_sys = _hypothesis_system_suffix(self.track_hypotheses)
        prompt_attempt = int(getattr(self, "_board_prompt_attempt", 0))
        self._board_prompt_attempt = prompt_attempt + 1
        # PUBLISHED on the instance, not just held locally. This is the exact window the model was
        # SHOWN, and `bind_idea_to_board_card` resolves a CARD_ID claim against it — so a wrapper
        # that re-binds the returned Idea has to use THIS window or it nulls valid claims. The
        # foresight panel keeps its own rotation cursor, which drifts from this one as a matter of
        # course (the base advances k per panel propose while the panel advances 1), and with more
        # than 5 open beliefs the two windows differ in the rotated slot. `ToolUsingResearcher`
        # publishes the same attribute for the same reason.
        visible_cards = next_board_prompt_cards(
            state, getattr(self, "_hyp_order", None), attempt=prompt_attempt)
        self._visible_board_cards = visible_cards
        messages = [
            {"role": "system",
             # Part V/P6: the explicit concept-mode contract, capability suffix (sweep offer — gated on
             # the active Developer — + eval_timeout), operator note, and emit instruction are appended AFTER the
             # render() — the SAME code-owned pattern as agent.py's ToolUsingResearcher. A
             # `researcher_system.md` PromptStore override replaces only the CORE persona, so an
             # override can never desync the capability prose from what the backend actually
             # implements (pre-fix the suffix was baked INSIDE the render default and an override
             # bypassed the offer_sweep gate). The assembled default is byte-equal to
             # `_researcher_system(offer_sweep)`.
             "content": render(self.prompts, "researcher_system", _RESEARCHER_CORE)
                        + _CONCEPT_AUTHORING_GUIDANCE
                        + _researcher_capability_suffix(
                            getattr(self, "offer_sweep", True),
                            bool(getattr(self, "_gpu_footprint_cue", False)))
                        + _OPERATOR_NOTE
                        + "Respond ONLY with the requested structured fields." + hyp_sys
                        # `_UNTRUSTED_MEMORY_RULE` and NOT `_CONTEXT_BEFORE_TOOLS_RULE`, and the
                        # difference is this class: `LLMResearcher` is the single-shot structured
                        # role and it has NO `tools` — no constructor argument, no attribute, no
                        # loop. The memory rule applies because this prompt really does splice
                        # untrusted cross-run cues; the tools rule tells a model with no tools that
                        # "a tool answers what is inside one of those things" and to "re-ask one
                        # after something HAPPENED", which is an invitation to call something that
                        # is not in the request. The rule's own definition says "appended wherever a
                        # role is offered tools" — this is the one splice site where that is false.
                        # `agent.py::ToolUsingResearcher` is the variant that HAS the surface and
                        # carries it; the same evidence already took the clause off the repo
                        # Developer (`tests/test_stage_splitting_guidance.py`), where the rule
                        # A/B'd to nothing while `answered_by_context`'s DATA moved 41.3 -> 17.7.
                        + _UNTRUSTED_MEMORY_RULE
                        + "\n\n" + _attention_points()},
            {"role": "user", "content": _state_brief(state, parent,
                                                     digest_cap=getattr(self, "_digest_cap", 0),
                                                     hyp_order=getattr(self, "_hyp_order", None),
                                                     board_cards=visible_cards,
                                                     memo_verdicts=bool(getattr(
                                                         self, "_memo_verdict_cue", False)))
                                        + "\n" + self.space_hint +
                                        hint_block + cues +
                                        "\nPropose the next Idea (operator, params, rationale, concept_mode, "
                                        "concepts/concepts_added/concepts_removed"
                                        + (", hypothesis" if self.track_hypotheses else "") +
                                        # P6: don't re-offer the sweep in the user turn when the
                                        # active Developer can't run one (system prompt gates too).
                                        ("; optionally a `space` grid for a sweep"
                                         if getattr(self, "offer_sweep", True) else "") + "). The "
                                        "`rationale` is your conclusion the operator reads AND the Developer "
                                        "builds from — write it as brief GitHub-flavored Markdown (a lead "
                                        "sentence; **bold** the key lever, add a short bullet or two only if it "
                                        "helps). Focus on the DELTA: name the specific change THIS experiment "
                                        "makes and the intuition for why it should help — and SPECIFY that change "
                                        "completely enough for the Developer to build it (a structural change is "
                                        "often built from scratch, so include the essential setup it needs). Do "
                                        "NOT pad it with the parent's motivation or repeat reasoning you already "
                                        "wrote on earlier experiments — say what is NEW here, not the shared "
                                        "story. Keep it to ~1-3 sentences. " + _IDEA_SPACE_PLAIN
                                        + (" The `hypothesis` is the one-line belief this experiment "
                                           "tests (reuse wording across experiments that test the same "
                                           "belief)." if self.track_hypotheses else "")
                                        # THE PROSE ASK, and the only untested lever left on this
                                        # field. `open_questions` has been in the emitted schema
                                        # since it landed — `IdeaEmission` derives from `Idea`, so
                                        # its description reaches the model on every proposal — and
                                        # across 155 `node_created` rows on this box NOT ONE was
                                        # filled. Schema presence is not an ask: this repo already
                                        # measured that prose outranks a computed cue, and the user
                                        # turn enumerated params/rationale/space/hypothesis and
                                        # never questions. Deliberately LAST and one sentence: it
                                        # costs nothing to leave empty, and a Researcher that had to
                                        # spend its proposal to record a question would record none.
                                        + " Optionally list `open_questions`: broad questions you "
                                          "noticed and are NOT pursuing here, each worth its own "
                                          "investigation later — leave it empty if you have none."},
        ]
        # Small models occasionally emit unparseable output (the common case: a non-numeric `params`
        # value, which `Idea.params: dict[str, float]` rejects). Retry — but fold the parse error back
        # into the prompt first, so the retry ISN'T byte-identical (which deterministically re-fails);
        # then fall back to a safe default so one bad response never crashes the run.
        idea: Optional[Idea] = None
        last: Optional[Exception] = None
        for _attempt in range(2):
            try:
                # modern model output must choose full vs delta explicitly. The durable
                # Idea reader stays tolerant for historical/future logs, so writers cross this boundary.
                parsed = parse_structured(self.client, messages, IdeaEmission, self.parser)
                # Preserve the long-standing injectable parser seam used by custom integrations/test
                # doubles: the real parser returns IdeaEmission, while a trusted adapter may return Idea.
                idea = parsed.to_idea() if isinstance(parsed, IdeaEmission) else Idea.model_validate(parsed)
                break
            except ParseError as e:
                last = e
                messages = messages + [{"role": "user", "content":
                    f"Your last response could not be parsed ({str(e)[:180]}). Emit the Idea again with "
                    "NUMERIC `params` only (put any non-numeric/structural change in `rationale`), a "
                    "valid `operator`, and a `rationale`."}]
        if idea is None:
            # Through the shared sentinel: the engine's proposal-path circuit breaker recognises this
            # exact prefix and refuses to turn a non-proposal into a Card/node. Byte-identical text.
            idea = Idea(operator="draft", params={},
                        rationale=researcher_fallback_rationale("parse failed", last))
        return _clamp_fill(bind_idea_to_board_card(idea, visible_cards), self.bounds)


class LLMDeveloper:
    """Writes (and repairs) a complete runnable solution script. `brief` carries the
    task's I/O contract (where to read data, what metric to print). `repair` powers the
    error-feedback debug operator: it gets the failing code + stderr and fixes it."""

    # T8/A0b: this Developer generates real code, so merge_mode="auto" resolves to the
    # code-recombination ensemble merge (the verified strongest operator) instead of mean-params.
    is_code_generating = True
    # P6/P21: the CAPABILITY the sweep offer is gated on — `implement` below renders `idea.space`
    # into the grid + trials contract, so this Developer really runs every point. Declared as a
    # positive marker (absent means NO) because the templated Developers are the majority and none
    # of them reads `idea.space`; see `agents/factory.py::_offer_sweep`.
    honors_idea_space = True
    # C5/C2: the CAPABILITY best-of-N selection is gated on — this Developer's `implement` RETURN
    # VALUE *is* the artifact, so `search/best_of_n.py::_score` can rank N candidates by compiling
    # them. A positive marker (absent means NO) for the same reason `honors_idea_space` is: the
    # majority of Developers here answer some other way, and `LLMRepoDeveloper` answers on
    # `last_files` — which made every repo task's `best_of_n` a coin flip the operator paid N full
    # builds for. `search/best_of_n.py::refuse_unrankable_best_of_n` owns the rule and the numbers.
    answers_with_code = True

    def __init__(self, client: LLMClient, brief: str = "",
                 prompts: Optional[PromptStore] = None):
        self.client = client
        self.brief = brief
        self.prompts = prompts
        self.last_footprint: dict | None = None

    def implement(self, idea: Idea) -> str:
        system = (render(self.prompts, "developer_system", _DEVELOPER_SYSTEM) + self.brief
                  + _developer_footprint_guidance(idea) + "\n\n" + _attention_points())
        # Render whatever params the task's Researcher proposed (task-agnostic): degree/lam
        # for regression, k for mlebench, etc. — hardcoding names dropped the value on
        # tasks that use a different hyperparameter.
        params = ", ".join(f"{k}={v}" for k, v in idea.params.items()) or "(model defaults)"
        if idea.space:
            # Intra-node sweep: render the grid and append the trials-reporting contract to the
            # system prompt so the Developer runs every point in one process and reports them all.
            system += _SWEEP_CONTRACT
            grid = "; ".join(f"{k} in {v}" for k, v in idea.space.items())
            fixed = f" Fixed/shared params: {params}." if idea.params else ""
            user = (f"Run an intra-node sweep over the grid: {grid}.{fixed} {idea.rationale}").strip()
        else:
            user = (f"Experiment concept (the researcher's idea): {idea.rationale}\n"
                    f"Parameters: {params}.\n"
                    "You own the implementation: design and write the solution code that realises "
                    "this concept.").strip()
        code = extract_code(self.client.complete_text(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))
        self.last_footprint = developer_artifact_footprint(idea.footprint, code)
        return code

    def repair(self, idea: Idea, code: str, error: str) -> str:
        # P8: the hardware/operational cues reach repair too (a timeout/oom repair NEEDS the real
        # GPU/CPU picture to size the cheaper retry) — appended after the render() calls, same as
        # the implement path.
        system = (render(self.prompts, "developer_repair_prefix", "You are an expert Python debugger. ") +
                  render(self.prompts, "developer_system", _DEVELOPER_SYSTEM) + self.brief
                  + _developer_footprint_guidance(idea) + "\n\n" + _attention_points())
        user = ("The script below failed. Return a corrected, complete script that runs "
                "and prints the required JSON metric line.\n\n--- SCRIPT ---\n" + code +
                "\n\n--- ERROR (stderr tail) ---\n" + error)
        # Include the idea rationale — the ValidatingDeveloper folds the validator's rejection feedback
        # into it on each retry, so without this the retry re-sends a byte-identical prompt and
        # deterministically re-fails, burning every attempt.
        if idea is not None and getattr(idea, "rationale", ""):
            user += "\n\n--- ADDITIONAL GUIDANCE ---\n" + idea.rationale
        # `complete_text` is NOT caught here, and neither is it by `ValidatingDeveloper._attempt_loop`
        # above — a 401/402/outage RAISES out of `repair`. That is deliberate but only safe because
        # every caller now has a handler: the two build-time ones are inside `_create_node` (which
        # terminalizes the build and requests the build_crash pause), and the inline-repair loop
        # normalizes the raise into the "(developer error: …)" sentinel at its own call site
        # (`engine/evaluate.py`), so it takes the same provider circuit breaker as a repo Developer's
        # in-band sentinel. It used to be the one uncaught path: the exception escaped `_evaluate`
        # with no terminal and no pause, so the breaker never engaged for non-repo tasks.
        repaired = extract_code(self.client.complete_text(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))
        self.last_footprint = developer_artifact_footprint(idea.footprint, repaired)
        return repaired
