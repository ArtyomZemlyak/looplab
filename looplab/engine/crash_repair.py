"""Crash triage & self-repair context (the crash -> verdict -> informed-retry chain) for the
engine — extracted from orchestrator.py as a MIXIN: `class Engine(CrashRepairMixin, …)` inherits
these methods unchanged, so there is ZERO call-site churn and `self` here IS the engine. The
method bodies are verbatim moves and read engine attributes freely (`researcher`, `tracer`,
`_inline_repair_attempts`, `_deep_repair`, `_dep_lock`/`_dep_attempted`/`_dep_installer`,
`sandbox`), exactly as they did inside the class.

The cluster: `_triage_crash` (LLM crash-triage verdict — instance-monkeypatched by tests, which
a mixin preserves) over its single-ask half `_ask_triage` (one normalized ask; `_triage_crash` owns
the bounded re-ask of a non-answer, so patching `_triage_crash` still replaces the WHOLE decision as
every existing test expects), `_repair_error_context` (ancestral repair chain + hint directives for the
repair prompt), `_prepare_env` (dependency self-prep on ModuleNotFoundError) and its sibling
`_prepare_env_from_triage` (the same self-prep for a missing library the traceback never NAMES —
see its docstring), sharing one install tail in `_install_missing`. The rule-based fallback
`_rule_triage` and the repair-class coercion `coerce_repair_class` are imported from their
canonical home (engine/triage.py); agents/digest deps stay lazy, method-local imports."""
from __future__ import annotations

import sys
from typing import Optional

from looplab.core.llm import BudgetExceeded
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import RunState, normalize_researcher_footprint
from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, DEFAULT_TRIAGE_ACTION,
                                   DIAGNOSIS_SUMMARY_CAP, FINDINGS_CAP,
                                   TRIAGE_RATIONALE_CAP, UNANSWERABLE_TRIAGE_ACTION, _rule_triage,
                                   _TRIAGE_REASK_LIMIT, coerce_triage_action,
                                   is_transport_failure_verdict)
# The verification verdicts this module RENDERS (it never decides one). Two vocabularies live side
# by side in this file now and they are not the same kind of thing: `AGENT_TRIAGE_ACTIONS` above is
# what a model may answer, `REPAIR_*` below is what the engine measured. See
# `engine/repair_verify.py`'s docstring for why they must not be merged.
from looplab.engine.repair_verify import REPAIR_INERT, REPAIR_UNMET

# The intake bound on the triage model's free text. Its rationale, its measurement and the reason it
# had to MOVE are at `engine/triage.py::TRIAGE_RATIONALE_CAP`: the seam's only implementation caps
# the same string on its way OUT, so a bound applied here to what the seam RETURNED could never be
# the widest one and both layers now read that single constant.
#
# Kept under the private spelling this module has always used (bound BY VALUE at import, as it was
# when the literal lived here). A caller that wants to move the bound moves it on `engine/triage.py`,
# where the FINALIZER re-reads it on every call — this alias deliberately does not follow, because
# the two layers are two different caps that merely happen to share a number, and a test that wants
# to reproduce the historical truncation needs to move exactly one of them.
_TRIAGE_RATIONALE_CAP = TRIAGE_RATIONALE_CAP


def _accepted_kwargs(fn, candidates: dict) -> dict:
    """The subset of `candidates` that `fn` can actually be called with.

    Everything, when `fn` declares `**kwargs`; nothing it does not name otherwise. Signature
    introspection can fail on an exotic callable (a C function, a `functools.partial` chain, a
    proxy) — that answers "pass nothing", which is the safe direction: the callee keeps its
    historical prompt rather than the call blowing up and being mistaken for a provider outage."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(candidates)
    return {k: v for k, v in candidates.items() if k in params}


# How many watchdog verdicts reach the prompt, and how much of each `reason` survives. The prompt
# already carries the repair history, the stderr tail and the code tail; node 3 drew TWENTY-ONE
# alerts for one lifecycle, so "all of them" is not a free choice. The window takes the LAST rows
# because they are the ones formed with the most of the log in view — on that node the early alerts
# carried the symptom and the later ones the mechanism — and the count of what was dropped is
# printed, so a bounded view never reads as the whole record.
#
# The char cap is a SECOND belt, not the operative one: `_monitor_training` already caps `reason` at
# 300 characters before it appends the row (train_monitor.py, next to its `_redact` call), so on
# rows this engine wrote it never fires. It is here for rows it did not write — an older run's log,
# a replayed corpus — because this renderer's input is untrusted append-only data like every other
# durable read on the repair path.
_MONITOR_VERDICT_ROWS = 6
_MONITOR_REASON_CHARS = 400


def _format_monitor_verdicts(verdicts) -> str:
    """Render the training watchdog's OPINIONS about this node for the repair to read.

    THE VOICE IS THE WATCHDOG'S, NOT THE ENGINE'S, and that is the whole design. Everything else in
    this prompt that the engine says, it says because it OBSERVED it — the change-set comparison, the
    stage statuses, the attempt count. These are a MODEL'S READING of a log, and this repo has twice
    measured what happens when a reading is handed on as a fact: the diagnostician promotes the stage
    checker's prose to a cause and loses 8 of 10 rows on `rubertlite-dense-retrieval` doing it. So the
    heading says whose opinion it is, every row carries its own CONFIDENCE, and the series is NOT
    collapsed into one verdict — on `e5small-dr-unified-v4` node 3 it ran broken 0.75, 0.70, 0.75,
    watch 0.55, healthy 0.85, broken 0.85, healthy 0.85, broken 0.60, broken 0.80, healthy 0.85, and
    that it contradicted itself twice is information the Developer should have rather than something
    an averaging step hides.

    WHY IT IS WORTH THE TOKENS. Node 3's watchdog named its defect eleven times over ten hours; the
    repair that followed wrote "Healthy training run ... a pure speed failure, not a correctness one",
    cut the epochs and left the cause untouched. ~17 GPU-hours.

    Empty renders EMPTY, so a node with no alerts produces a byte-identical prompt to the one this
    function did not exist for. Prompt text is a contract (CLAUDE.md): a new fact earns a new
    sentence, it does not reword the existing ones."""
    rows = [v for v in (verdicts or []) if isinstance(v, dict) and v.get("status")]
    if not rows:
        return ""
    shown = rows[-_MONITOR_VERDICT_ROWS:]
    dropped = len(rows) - len(shown)
    head = ("--- WHAT THE TRAINING WATCHDOG SAID ABOUT THIS NODE (a JUDGE'S READING of the "
            "training log, not an engine observation — it may be wrong, and on this node it has "
            "disagreed with itself)")
    if dropped:
        head += f"; showing the {len(shown)} most recent of {len(rows)}"
    out = [head + " ---"]
    for v in shown:
        conf = v.get("confidence")
        conf_s = f"{float(conf):.2f}" if isinstance(conf, (int, float)) else "unstated"
        bits = [f"verdict={v.get('status')}", f"confidence={conf_s}"]
        if v.get("fault"):
            bits.append(f"blames={v.get('fault')}")
        if v.get("stage"):
            bits.append(f"stage={v.get('stage')}")
        out.append("  " + " ".join(bits))
        traj = v.get("trajectory")
        if isinstance(traj, dict) and traj:
            # Rendered on its OWN line and in the ENGINE's flat vocabulary, because unlike the
            # sentence below it this is measured. `first`/`last`/`minimum` are the numbers that
            # settle "did the loss actually do something impossible" without trusting either side.
            keys = [k for k in ("direction", "first", "last", "minimum", "net", "windows",
                                "points", "progress", "anomaly") if traj.get(k) is not None]
            if keys:
                out.append("    measured: " + " ".join(f"{k}={traj[k]}" for k in keys))
        reason = str(v.get("reason") or "").strip()
        if reason:
            clipped = reason[:_MONITOR_REASON_CHARS]
            if len(reason) > _MONITOR_REASON_CHARS:
                clipped += "…"
            out.append("    " + clipped)
    return "\n".join(out) + "\n"


def _format_repair_log(repair_log) -> str:
    """Render this node's in-node repair history for the stop judge: one line per attempt, oldest
    first, newest last. Pure text assembly — the engine decides WHAT the judge sees, the agent
    decides how to ask about it.

    Rendered here rather than in `agents/unified_agent.py` because the rows are the ENGINE's
    record of what it did, and because the deterministic rule path must be able to ignore them
    without the agent module having an opinion. Empty history renders empty, so the first attempt's
    prompt is byte-identical to what it was before the history existed."""
    rows = [r for r in (repair_log or []) if isinstance(r, dict)]
    if not rows:
        return ""
    out = ["--- WHAT HAS ALREADY BEEN TRIED ON THIS NODE (oldest first) ---"]
    for r in rows:
        # A row rebuilt from a `node_repaired` written before the change-set column existed has no
        # `changed` key at all, which is NOT the same fact as "this fix changed nothing" — and the
        # difference is exactly the one the judge is being asked to read ("is this repair chain
        # rewriting the same lines?"). Distinguish the missing key from the empty list rather than
        # letting an old log look like a circling one. See
        # `engine/evaluate.py::_durable_repair_ledger`.
        changed = ("(not recorded — this attempt predates the change-set column)"
                   if "changed" not in r
                   else ", ".join(str(c) for c in (r.get("changed") or [])) or "nothing")
        # THE ENGINE'S OWN VERDICT ON THAT ROW, said in the engine's voice. `changed: nothing` is
        # already in front of the judge and a live model DID once read it correctly — v2 node 2
        # attempt 3's rationale opens "The 3 prior attempts never actually applied any file change"
        # — but it is a passive column three of its siblings ignored. So the two verdicts that mean
        # something get a sentence.
        #
        # Rendered ONLY for `inert`/`unmet`: a `verified` row, an `unstated` one and a row with no
        # verdict at all (a legacy log, a salvage marker) all render byte-identically to what this
        # prompt has always been. Prompt text is a contract (CLAUDE.md) — a new fact earns a new
        # sentence, it does not get to reword the existing ones.
        note = ""
        if r.get("verified") == REPAIR_INERT:
            note = ("\n    THE ENGINE COMPARED THE BYTES: this attempt changed no file at all, so "
                    "the evaluation after it re-ran inputs identical to the one before it.")
        elif r.get("verified") == REPAIR_UNMET and r.get("unmet"):
            note = ("\n    the engine could not find what this fix said it would change ("
                    + ", ".join(str(u) for u in (r.get("unmet") or [])[:6])
                    + ") anywhere in what it actually changed.")
        # A SECOND, INDEPENDENT sentence, and it is deliberately not part of the verdict ladder
        # above: "did this fix do what it said" and "did it move a number this experiment is being
        # COMPARED on" are different questions, and a repair can be `verified` and still have done
        # the second (v8 node 3 attempt 4 is exactly that row — it promised the batch/accum swap and
        # delivered it, onto the node that became the run's champion). Appended rather than
        # substituted so every existing row renders byte-identically to what it did before, per the
        # prompt-text contract; a row with no `param_overrides` key renders nothing at all.
        _overrides = [o for o in (r.get("param_overrides") or []) if isinstance(o, dict)]
        if _overrides:
            note += ("\n    NOTE — this fix moved a parameter THE EXPERIMENT'S OWN RECORD DECLARES: "
                     + "; ".join(
                         f"{o.get('param')} is declared {o.get('declared')} but "
                         f"{o.get('file')}:{o.get('line')} assigns {o.get('code')}"
                         for o in _overrides[:4])
                     + ". The declared value is what this node is ranked against its siblings by, "
                       "so if the change was deliberate say so in your rationale; if it was not, "
                       "putting it back is a fix.")
        # A THIRD independent sentence, and the one `inert` most needs beside it: `changed: nothing`
        # is the same column whether the Developer LOOKED and decided not to edit or was cut off
        # mid-investigation, and those want opposite next moves. Measured over `runs/`, 12 of the 12
        # inert repairs in the corpus ran past `session_time_budget_s` and 0 of the 65 that finished
        # inside it are inert — so on this box the column is very nearly the explanation for `inert`,
        # and until now it reached the judge on neither the live row nor the resumed one.
        #
        # PER KIND, because `_note_session_budget` stores any member of
        # `tool_loop.py::LOOP_CUTOFF_KINDS` and only two of the five are budget bounds. Calling
        # `stuck` or `emit_force` "ran out of clock" would be a confident wrong sentence in the one
        # place this rung exists to stop being wrong. Appended, never substituted, so a row without
        # the column renders byte-identically to what this prompt has always been.
        _cutoff = str(r.get("budget_exhausted") or "").strip()
        if _cutoff:
            note += "\n    " + {
                "time": "THE SESSION RAN OUT OF WALL CLOCK — it did not finish on its own terms, so "
                        "an empty or thin change set here is where it got to, not what it decided.",
                "turns": "THE SESSION RAN OUT OF TURNS — it did not finish on its own terms, so an "
                         "empty or thin change set here is where it got to, not what it decided.",
            }.get(_cutoff,
                  f"THE LOOP ENDED THIS SESSION ITSELF ({_cutoff}) rather than the Developer "
                  "finishing: it stopped without a model-chosen emit, so what this attempt changed "
                  "is what had been written by then.")
        out.append(
            f"attempt {r.get('attempt')}: failed with — {' '.join(str(r.get('error', '')).split())}\n"
            f"    the fix claimed: {str(r.get('fix', '')).strip() or '(no rationale)'}\n"
            f"    it changed: {changed} | pipeline stages passed before the failure: "
            f"{r.get('stages_passed')}{note}")
    return "\n".join(out)


def _coerced_critic_verdict(out) -> dict:
    """One WIRED critic answer, normalized to `{action, rationale, source}` — the two branches that
    used to be a bare `coerce_critic_action` plus a `not isinstance(out, dict)` early return.

    Hoisted out of `_repair_critic` so the ANSWER can be stamped on the span before it closes and so
    the three-way split has a truth table (`tests/test_repair_judgment.py`). The split is the point:
    `coerce_critic_action` fails open to `continue`, which is right for the loop and useless for a
    reader — a critic that returned nothing, one that answered a word out of the enum and one that
    genuinely said "keep going" all become the same verdict, and the operator auditing why a chain
    ran to six attempts needs to know which happened. Only `model` means an opinion was given.

    Never sees the not-wired / no-trajectory branches: those return before the call, so their sources
    are minted at their own `return`s."""
    from looplab.engine.repair_judgment import (AGENT_CRITIC_ACTIONS, CRITIC_SOURCE_MODEL,
                                                CRITIC_SOURCE_NO_VERDICT,
                                                CRITIC_SOURCE_OUT_OF_ENUM, DEFAULT_CRITIC_ACTION,
                                                coerce_critic_action)
    if not isinstance(out, dict):
        # Includes the `None` a `UnifiedAgent` with no pilot model returns.
        return {"action": DEFAULT_CRITIC_ACTION, "rationale": "the repair critic returned no verdict",
                "source": CRITIC_SOURCE_NO_VERDICT}
    raw = out.get("action")
    action = coerce_critic_action(raw)
    # Read off the RAW value the same way the coercion does, rather than comparing `action` to a
    # default: `continue` is a legal emit, so `action == DEFAULT_CRITIC_ACTION` cannot tell a real
    # "keep going" from a rejected one.
    readable = str(raw or "").strip().lower() in AGENT_CRITIC_ACTIONS
    return {"action": action,
            "rationale": str(out.get("rationale", ""))[:300],
            "source": CRITIC_SOURCE_MODEL if readable else CRITIC_SOURCE_OUT_OF_ENUM}


class CrashRepairMixin:
    """The engine's crash-triage/repair-context cluster. See the module docstring for the mixin
    convention (`self` is the Engine)."""

    @in_llm_lane("build")
    def _triage_crash(self, state: RunState, node, error: str, attempt: int,
                      reason: str = "crash", *, repair_log=None,
                      depth: Optional[int] = None, attempts_left: Optional[int] = None,
                      log_tools=None, engine_facts: str = "", monitor_verdicts=None) -> dict:
        """Decide what to do with a just-failed node BEFORE spending another eval:
        {"action": "repair"|"abandon"|"reject_idea"|"unanswerable"|"unreadable", "rationale": str,
        "failure_kind": str}.

        `failure_kind` and the three `evidence_*` fields make this call the FAILURE DIAGNOSTICIAN
        since 2026-08-20 — what the failure WAS, over
        `engine/failure_diagnosis.py::DIAGNOSED_FAILURE_REASONS`, and what it stood on. It costs NO
        extra provider call: the loop already makes exactly one triage call per failed attempt (an
        already-agentic one, measured at 8.82 calls per failure across v8+v9+v3) and this judge is
        already handed the evidence the question needs. All four are uncoerced here on purpose — the
        fallback for an absent or out-of-vocabulary kind is a decision only the CALLER can make,
        because it holds the engine's own answer for THIS eval, and the evidence locator can only be
        re-resolved against a workdir this frame does not have. `failure_diagnosis` is where both
        are spelled.

        WHAT AN ABSENT KIND MEANS CHANGED WITH IT. The rule fallback below is unaffected — it stamps
        `DIAGNOSIS_UNAVAILABLE_KEY`, which says "no diagnostician was wired" and keeps the engine's
        answer. But an AGENT verdict with no readable kind is now `unclassified`: something was
        asked and could not answer, and a row that recorded the engine's residual instead would be
        indistinguishable from one where the diagnostician agreed.

        THIS IS THE STOPPING RULE for the inline-repair loop, not merely a repair-vs-reject
        classifier. The unified agent decides (it can consult the run via its pilot tools —
        read_code / find_analogous — to judge whether nearby configs also fail, i.e. whether the
        IDEA is wrong vs the code), and `abandon` is its "I no longer know how to fix this". It is
        asked once per attempt, which costs no extra calls: the loop already made exactly one triage
        call per attempt. What changed is the EVIDENCE — `repair_log` is this node's whole repair
        history (what failed, what each fix claimed, which files it actually touched, how deep the
        pipeline got), so the model judges a trajectory instead of one traceback in isolation.

        `reason` (crash|timeout|oom) is surfaced to both paths so a timeout is triaged as "too slow
        -> reduce compute" rather than mis-read as a wrong idea (a missing KNOWN lib never reaches
        here — env-prep installs it and re-runs first). `attempts_left` is the remaining hard budget,
        told to the model so a stop and a cap-out are not the same surprise; it is advisory to the
        model and enforced by the caller regardless. It is a real number even for a run with no
        OPERATOR cap (`inline_repair_attempts = 0`), because there is still a bound in that case —
        `engine/evaluate.py::_UNLIMITED_REPAIR_CEILING` — and telling the judge `None` on exactly the
        runs carrying the loosest bound was the least useful place to be coy. `None` remains
        meaningful for a caller that genuinely has no cap to report.

        FAIL CLOSED, IN TWO DIFFERENT DIRECTIONS. A wired judge that produces no usable verdict never
        degrades to a permissive "repair" and never falls through to the deterministic rule — but the
        two ways it can fail are NOT the same condition and no longer share an answer:

          * the TRANSPORT failed (the call raised, `resilient` caught an unreachable endpoint — the
            request never completed) -> `unanswerable`, which the caller routes to the run-level circuit
            breaker. This is how the 2345-repair incident began and it is a RUN-level fact: every
            other node reaches the same endpoint.
          * the model ANSWERED something outside the vocabulary -> `unreadable`, a per-NODE stop with
            no pause. The provider is demonstrably alive, so halting the run (and, under
            `eval_parallel > 1`, every healthy sibling with it) on one bad emit is a strictly wrong
            diagnosis — it used to hand the operator the MODEL's own rationale under a "check your
            credits, key and base URL" banner.

        Both are re-asked `_TRIAGE_REASK_LIMIT` times before they are acted on: one non-answer is not
        a diagnosis, and stopping a node on the first one costs a whole node for a single flapped
        socket (measured: `developer.repair` calls = 0). The rule path stays reserved for the
        genuinely different case of no judge being wired at all.

        `log_tools` (from `engine/train_monitor.py::repair_log_tools`, built by the CALLER because
        only the eval frame holds the workdir + this attempt's log plan + its byte floor) is this
        judge's permission to LOOK at the dead eval's own stage logs instead of diagnosing from
        `_eval_failure_text`'s 500-character stderr tail. `None` — the default, an old `Settings`, a
        non-command eval, a failure before any log existed — reproduces the historical ask exactly:
        `_accepted_kwargs` will not pass it to a seam that does not name it, and the one seam that
        does treats `None` as "no extra tools". See that function's docstring for the v8 node 3
        measurement that made this necessary, and note what it is NOT: nothing read through these
        tools may become a fact the record rests on. The verdict vocabulary is unchanged, the terminal
        still carries the eval's own authenticated `reason`, and no metric, champion, selectability or
        violation moves on anything a model saw here — doc 36's line, in the same place
        `_repair_critic` holds it."""
        # Tag the failure kind so the LLM agent (and the rule's marker scan) see crash vs timeout.
        tagged = f"[failure kind: {reason}]\n{error}"
        fn = getattr(self.researcher, "triage_crash", None)
        if callable(fn):
            verdict = None
            # THE RE-ASK. `_TRIAGE_REASK_LIMIT` extra rounds, and only ever for a non-answer: a real
            # verdict (repair/abandon/reject_idea) returns from inside the loop on the first pass, so
            # the healthy path still costs EXACTLY one call per attempt, which is what "the stop
            # decision is free" depends on. The extra call is charged only when the alternative is
            # ending the node on an answer nobody could read.
            for _round in range(1 + _TRIAGE_REASK_LIMIT):
                verdict = self._ask_triage(fn, state, node, tagged, attempt, reason,
                                           repair_log, depth, attempts_left, log_tools,
                                           engine_facts, monitor_verdicts)
                if verdict["action"] in AGENT_TRIAGE_ACTIONS:
                    return verdict
            return verdict
        # NO judge wired (`unified_agent` off) — a configuration, not a failure. The deterministic
        # rule keeps repairing crashes, bounded ONLY by the caller's hard cap, because it has no way
        # to form the stop judgement the model makes.
        #
        # THE CAP IS THE EFFECTIVE ONE, not `inline_repair_attempts or 10**9`. That spelling read 0
        # as "no bound at all" and handed the rule 10**9, which was survivable only while 0 was rare;
        # since F8 made 0 the DEFAULT it would have told the rule path there is no bound on every
        # shipped run. `_effective_repair_cap` is the same three-way answer the budget gate and the
        # cap-out message read, which is the whole reason it is a named function
        # (`engine/evaluate.py`) rather than three inline comparisons that used to disagree.
        from looplab.engine.evaluate import _effective_repair_cap
        return _rule_triage(reason, error, attempt,
                            _effective_repair_cap(self._inline_repair_attempts))

    def _ask_triage(self, fn, state: RunState, node, tagged: str, attempt: int, reason: str,
                    repair_log, depth, attempts_left, log_tools=None, engine_facts: str = "",
                    monitor_verdicts=None) -> dict:
        """ONE ask of the wired judge, normalized to a `TRIAGE_ACTIONS` verdict.

        Split out of `_triage_crash` so the re-ask above is a loop over a single, total function
        rather than a duplicated forty-line try block — and so the three ways an ask can end
        (a real verdict, a transport failure, an answer nobody can read) have one place each instead
        of being reachable only by driving a whole sandboxed eval against a broken endpoint."""
        try:
            from looplab.agents.roles import _state_brief
            from looplab.agents.hints import render_hint_directives
            try:
                # NOT a proposal: this asks for a `TRIAGE_ACTIONS` verdict, not an `Idea`, so the
                # board's claim contracts are instructions it cannot follow (see `_state_brief`).
                brief = _state_brief(state, None, for_proposal=False,
                                     memo_verdicts=getattr(self, "_memo_verdict_cue", False))
            except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
                brief = ""
            # Signal-delivery (§1): a standing directive (e.g. "prefer lighter models") is
            # relevant to the repair-vs-reject decision, so surface it to the triage agent too.
            brief += render_hint_directives(state.pending_hints)
            # Own span so the crash-triage LLM turns band as `triage`, NOT `evaluate`: triage runs
            # INSIDE the engine's `evaluate` span, so without this its (often many, agentic) turns
            # inherit phase=evaluate and inflate the "evaluate" band with failure-debugging that has
            # nothing to do with scoring — the exact "why is there a big eval when it never scored?"
            # confusion. (The repair itself already has its own `inline_repair` span.)
            # `triage_crash` is a DUCK-TYPED seam (any object wired as `researcher` may
            # implement it), and this change added three keyword arguments to it. Passing them
            # unconditionally makes an implementation written against the old signature raise
            # TypeError — which the fail-closed handler below would then read as a dead provider
            # and use to stop the node and pause the RUN. That is the worst possible way for a
            # signature change to land, so the call is narrowed to what the callee actually
            # accepts. A `**kwargs` implementation gets everything, as before.
            # The log-query provider rides in the SAME narrowed bag as the three arguments the
            # 2026-08-13 change added, and for the identical reason: `triage_crash` is a DUCK-TYPED
            # seam, so a fourth keyword passed unconditionally to an implementation written against
            # the old signature raises TypeError — which the fail-closed handler below reads as a dead
            # provider and turns into a stopped node PLUS a RUN-level pause.
            # `tests/test_repair_stop_decision.py::
            # test_an_older_triage_crash_signature_is_not_read_as_a_dead_provider` is the standing
            # proof of the mechanism, and this file's own test drives it again with `log_tools` set.
            #
            # It is listed UNCONDITIONALLY here, exactly like the three beside it. A `if is not None`
            # would read like the safety and is not it — `_accepted_kwargs` is — and the two spellings
            # are indistinguishable to a new seam, whose `tools=None` default and an explicit `None`
            # are the same value. One rule for the whole bag is the reviewable one.
            # `engine_facts` rides in the SAME narrowed bag as the four before it, and for the
            # identical reason stated above: `triage_crash` is a DUCK-TYPED seam, so an argument
            # passed unconditionally to an implementation written against an older signature raises
            # TypeError, which the fail-closed handler below reads as a dead provider — a stopped
            # node PLUS a RUN-level pause. `_accepted_kwargs` is the safety; listing it here
            # unconditionally like the rest is what keeps one rule for the whole bag.
            # SPLICED INTO `history`, NOT given a new keyword. `history` is documented as "already
            # rendered by the engine", so every duck-typed `triage_crash` — including every test
            # double and the older signatures `_accepted_kwargs` exists to tolerate — sees the
            # watchdog's verdicts the moment they exist, with no coordinated change. A new keyword
            # would have reached exactly one implementation and silently skipped the rest.
            #
            # JOINED EXPLICITLY, not with a `+` and a leading newline inside one of the halves.
            # Either half can be empty — most nodes have no repair history on attempt 1 and most
            # have no watchdog verdicts at all — and a separator baked into one of them puts a
            # blank line at the top of the prompt in exactly those cases. `off == today` has to
            # hold in BOTH directions or it is not a byte-identical fallback.
            _log_block = _format_repair_log(repair_log)
            _mon_block = _format_monitor_verdicts(monitor_verdicts)
            _history = ("\n".join(b for b in (_log_block, _mon_block) if b)
                        if (_log_block and _mon_block) else (_log_block or _mon_block))
            extra = {"history": _history,
                     "stages_passed": depth, "attempts_left": attempts_left,
                     "tools": log_tools, "engine_facts": engine_facts}
            with self.tracer.span("triage", attempt=attempt, reason=reason):
                out = fn(node, tagged, attempt, state=state, brief=brief,
                         **_accepted_kwargs(fn, extra))
            if isinstance(out, dict) and out.get("action") in AGENT_TRIAGE_ACTIONS:
                # `missing_dependency` (a library the agent says is absent) is part of the
                # verdict, so it is carried here rather than re-derived downstream. It fails
                # closed to "" = no install, and the engine never acts on it alone (see
                # runtime/deps.py::triage_install_candidates).
                # `failure_kind` and the three `evidence_*` fields ride in the SAME narrowed bag
                # as `missing_dependency` and for the same reason: they are part of the verdict, so
                # they are carried here rather than re-derived downstream. The kind fails closed to
                # "" — an absent key, an older duck-typed seam, a model that ignored the field — and
                # "" is refused by `failure_diagnosis.diagnosed_failure_reason`, which answers
                # `unclassified` for a DIAGNOSABLE reason (something was asked and said nothing
                # readable) and keeps the engine's own answer for an ENGINE-FINAL one (nobody was
                # asked). None is coerced here: the kind's fallback is `_failure_reason`'s answer
                # for THIS eval and the evidence's check is a workdir resolution, and this frame
                # holds neither.
                # THE REBUILD IS THE UNFORGEABILITY, and it is why the two engine-side markers
                # (`TRIAGE_TRANSPORT_FAILURE_KEY`, `failure_diagnosis.DIAGNOSIS_UNAVAILABLE_KEY`)
                # can never arrive from the wire: the dict below is constructed from a FIXED key
                # list, so whatever a model emitted under those names is simply not carried.
                # Adding a key here is therefore a decision about what a model may say.
                #
                # The three `evidence_*` fields join `failure_kind` in that bag (2026-08-20). They
                # are carried RAW and uninterpreted for the same reason it is: the engine
                # re-resolves the locator against the workdir, which only the eval frame holds, so
                # `failure_diagnosis.coerce_evidence` / `evidence_citation_resolves` are the one
                # place the normalization and the check are spelled.
                #
                # `summary` and `findings` join them under the identical rule. The SUMMARY is the
                # one a reader actually reads — what failed and because of what, with its numbers
                # inline — and the findings are the trail behind it; neither is normalized here,
                # because `failure_diagnosis.coerce_diagnosis_summary`/`coerce_findings` are the one
                # place the cap, the dedup, the REDACTION and the "what counts as a citation" rule
                # are spelled, and this frame holds neither the workdir they will be resolved
                # against nor the engine's redactor. A non-list from an older duck-typed seam
                # travels as `[]` and an absent summary as `""` — both read as "it did not say",
                # exactly like `failure_kind`'s "".
                _found = out.get("findings")
                return {"action": out["action"],
                        "failure_kind": str(out.get("failure_kind", "")).strip().lower()[:40],
                        "summary": str(out.get("summary", ""))[:DIAGNOSIS_SUMMARY_CAP],
                        "evidence_source": str(out.get("evidence_source", "")).strip().lower()[:16],
                        "evidence_locator": str(out.get("evidence_locator", ""))[:300],
                        "evidence_quote": str(out.get("evidence_quote", ""))[:300],
                        "findings": ([f for f in _found[:FINDINGS_CAP] if isinstance(f, dict)]
                                     if isinstance(_found, (list, tuple)) else []),
                        "rationale": str(out.get("rationale", ""))[:_TRIAGE_RATIONALE_CAP],
                        "missing_dependency": str(out.get("missing_dependency", ""))[:100]}
            # A TRANSPORT FAILURE OBSERVED ONE LAYER DOWN. `UnifiedAgent.triage_crash`'s `_fallback`
            # runs when `resilient` contained an unreachable endpoint / a 401 / a loop that never
            # emitted, and it stamps `TRIAGE_TRANSPORT_FAILURE_KEY` on the verdict it returns. That
            # marker — never the action string — is what admits `unanswerable` from a return value,
            # so a live model echoing the word cannot claim its own unreachability and trip the
            # run-level breaker. Its rationale passes through instead of being re-wrapped in
            # "returned no usable verdict (…)", because it is a real report, not a malformed one.
            if is_transport_failure_verdict(out):
                return {"action": UNANSWERABLE_TRIAGE_ACTION,
                        "rationale": (str(out.get("rationale", ""))[:_TRIAGE_RATIONALE_CAP]
                                      or "the triage model did not return a verdict"),
                        "missing_dependency": ""}
            # A WIRED, LIVE JUDGE THAT ANSWERED SOMETHING OUTSIDE THE VOCABULARY. Coerced through the
            # registry, which fails closed to `unreadable` rather than inventing "repair" — tonight's
            # watchdog verification found the mirror-image bug (an unparseable verdict read as
            # transparent) and it was a real break. `unreadable` can also arrive ALREADY SPELLED,
            # from `UnifiedAgent._finalize`'s own coercion of an out-of-enum emit; that one is not
            # malformed, it is the same verdict reached one layer down, so its rationale (the model's
            # own words about the node) passes through rather than being re-wrapped.
            _raw = (out or {}).get("action") if isinstance(out, dict) else None
            # Two different kinds of string, so two different bounds. The first is the MODEL's own
            # diagnosis passing through and wears the intake cap like every other rationale; the
            # second is an engine-authored message wrapping an arbitrary `repr`, which stays at a
            # message-sized 300 — a wider bound there buys no reader anything and puts an unbounded
            # object's repr into a durable row.
            _why = (str(out.get("rationale", ""))[:_TRIAGE_RATIONALE_CAP]
                    if _raw == DEFAULT_TRIAGE_ACTION
                    else f"the triage model returned no usable verdict ({out!r})"[:300])
            return {"action": coerce_triage_action(_raw),
                    "rationale": _why or "no verdict returned", "missing_dependency": ""}
        except BudgetExceeded:      # the hard budget stop must propagate, not degrade to a verdict
            raise
        except Exception as exc:  # noqa: BLE001 - a WIRED judge whose CALL failed is a provider
            # outage, not a licence to keep repairing. This used to fall through to `_rule_triage`,
            # which answers "repair" for any mechanical crash while attempts remain — so a dead
            # endpoint kept the loop running at full speed with no LLM in it at all. This is the
            # engine's own observation of the transport, so it is `unanswerable` directly rather
            # than through the coercion (which rejects that word on purpose).
            return {"action": UNANSWERABLE_TRIAGE_ACTION,
                    "rationale": f"{type(exc).__name__}: {exc}"[:300],
                    "missing_dependency": ""}

    @in_llm_lane("build")
    def _repair_critic(self, state: RunState, node, repair_log, attempt: int,
                       monitor_verdicts=None) -> dict:
        """Is this node's repair chain still addressing different causes? `{"action", "rationale"}`
        with action ∈ `CRITIC_ACTIONS` — F8's second stop signal.

        THE ONE THING IT MAY DO IS STOP. `stop` becomes the same terminal an `abandon` produces,
        carrying the eval's own authenticated `reason`; there is no verdict here that moves a
        metric, a champion, selectability or a violation. Doc 36's line, on the safe side.

        FAILS OPEN, and that is deliberate — read `engine/repair_judgment.py::DEFAULT_CRITIC_ACTION`
        before changing it. A critic that is not wired, cannot be reached, or answers something
        unreadable contributes nothing and the loop stops exactly where it would have without one:
        the triage judge is still the primary stop and the floors are still enforced. There is no
        `unanswerable` twin here for the same reason — a critic's silence is not evidence about the
        provider, because the triage call one line above it just reached the same endpoint.

        No deterministic fallback either, and that asymmetry with `_triage_crash` is the point: the
        rule path exists there because SOMETHING must decide repair-vs-stop with no model wired, and
        `_rule_triage` can at least recognise a mechanical crash. "Are these attempts circling?" has
        no rule form — that is the whole finding of the deleted error-signature counter, which
        answered it with a regex and was defeated by a Cyrillic identifier, a blank stderr and a
        varying request id. A heuristic here would be that mistake with a new name.

        WHAT IT RETURNS IS ALSO A RECORD, since 2026-08-15: `source` names WHICH of the six branches
        below produced the verdict, because `continue` alone is five different facts and the operator
        auditing a chain could not tell them apart. A critic that answered "keep going", one whose
        endpoint was down, one that was never wired and one that answered a word the coercion could
        not read all produced the identical row — and on `rubertlite-dr-unified-v8` that ambiguity was
        the whole reason `repair_critic_after` could not be calibrated by anyone reading the run. It
        is ENGINE-MINTED at each `return` (see `repair_judgment.py::CRITIC_SOURCES`); the model never
        supplies it and the coercion never accepts one."""
        from looplab.engine.repair_judgment import (CRITIC_CONTINUE, CRITIC_SOURCE_NO_TRAJECTORY,
                                                    CRITIC_SOURCE_UNREACHABLE,
                                                    CRITIC_SOURCE_UNWIRED, DEFAULT_CRITIC_ACTION,
                                                    format_repair_trajectory)
        fn = getattr(self.researcher, "repair_critic", None)
        if not callable(fn):
            return {"action": CRITIC_CONTINUE, "rationale": "no repair critic wired",
                    "source": CRITIC_SOURCE_UNWIRED}
        trajectory = format_repair_trajectory(repair_log)
        # THE WATCHDOG'S VERDICTS REACH THE CRITIC TOO, and leaving them out was a scoping error
        # measured within hours of shipping the repair half. This critic answers "are successive
        # attempts addressing DIFFERENT causes, or circling one?" and returns continue-or-stop — it
        # decides whether the chain lives. Its per-attempt CAUSE column is the ENGINE's: `crash`,
        # `crash`, `oom`, `expect_failed`, `timeout`. None of those words can say that the objective
        # has no floor.
        #
        # MEASURED on `runs/e5small-dr-unified-v4` node 3, attempt 6: while the repair — which now
        # sees the verdicts — wrote "the watchdog's diagnosis is correct and I reproduced the
        # mechanism in code" and changed `loss.py`, this critic wrote "a pure speed timeout with a
        # healthy training run", which is verbatim the framing that cost ~17 GPU-hours the day
        # before. It answered `continue` and the answer was right, but the justification was a
        # training run that was not healthy.
        #
        # THE EXPENSIVE DIRECTION IS THE MIRROR ONE. A chain that repairs SPEED five times on a node
        # whose loss is unbounded below looks like five DISTINCT causes to a reader of that column,
        # because the engine wrote a different word each time — and "distinct causes" is exactly the
        # evidence this critic continues on.
        #
        # Appended to `trajectory` rather than given a new keyword, for the same reason the repair
        # half splices into `history`: `repair_critic` is a DUCK-TYPED seam and `_accepted_kwargs`
        # would quietly drop an argument older implementations do not name, so a new keyword reaches
        # one implementation and silently skips every other. Empty renders empty.
        _verdicts_block = _format_monitor_verdicts(monitor_verdicts)
        if trajectory and _verdicts_block:
            trajectory = trajectory + "\n" + _verdicts_block
        if not trajectory:
            return {"action": CRITIC_CONTINUE, "rationale": "no repair trajectory to judge yet",
                    "source": CRITIC_SOURCE_NO_TRAJECTORY}
        try:
            from looplab.agents.roles import _state_brief
            try:
                # NOT a proposal, exactly as in `_ask_triage`: this asks for a stop/continue verdict,
                # so the board's claim contracts are instructions it cannot follow.
                brief = _state_brief(state, None, for_proposal=False,
                                     memo_verdicts=getattr(self, "_memo_verdict_cue", False))
            except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
                brief = ""
            # Own span, and it bands as `triage` beside the stop decision it belongs to rather than
            # inflating `evaluate` — the same trace-attribution rule `_ask_triage` documents.
            extra = {"trajectory": trajectory, "attempt": attempt, "brief": brief, "state": state}
            with self.tracer.span("repair_critic", attempt=attempt) as sp:
                out = fn(node, **_accepted_kwargs(fn, extra))
                # THE SPAN LEARNS THE ANSWER TOO, and this is deliberately the cheaper HALF of the
                # record rather than the record itself: the durable one is
                # `events/types.py::EV_REPAIR_CRITIC_VERDICT`, because `spans.jsonl` is optional
                # (tracing may be off) and destroyable (`serve/trace_clear.py`). What the span adds
                # is the answer sitting beside the model's own prompt/completion I/O in the trace
                # view, which is where somebody debugging a bad verdict is already looking.
                # Inside the `with` because a span is written on CLOSE — set after it, the attribute
                # would reach an already-flushed record.
                verdict = _coerced_critic_verdict(out)
                sp.set("verdict", verdict["action"])
                sp.set("critic_source", verdict["source"])
                sp.set("critic_rationale", verdict["rationale"])
            return verdict
        except BudgetExceeded:      # the hard budget stop must propagate, not degrade to a verdict
            raise
        except Exception as exc:  # noqa: BLE001 - a critic whose CALL failed has no opinion. It is
            # NOT the dead-provider signal its triage sibling makes of the same exception: that one
            # is the loop's only stop and a silent "keep repairing" there is the 2345-repair
            # incident, while this one is an extra veto whose absence restores the previous
            # behaviour exactly. Reporting a provider outage from here would ALSO be the wrong
            # diagnosis twice over — `_triage_crash` reached the same endpoint moments earlier and
            # would have said so itself.
            #
            # It stays DISTINGUISHABLE in the record even though it is indistinguishable in
            # behaviour: `unreachable` is what tells an operator that "no stops" means the critic
            # never spoke, not that it kept approving.
            return {"action": DEFAULT_CRITIC_ACTION,
                    "rationale": f"the repair critic could not be reached ({type(exc).__name__})",
                    "source": CRITIC_SOURCE_UNREACHABLE}

    def _repair_error_context(self, reason: str, error: str,
                              state: Optional[RunState] = None, node=None,
                              *, headline: str = "") -> str:
        """Error context handed to Developer.repair(). A timeout gets an explicit cost-reduction
        directive (the code was too slow, not wrong — shrink it to fit the budget). With deep_repair
        (C3) a crash is enriched with the failure taxonomy + a 'reproduce then fix' directive; else
        the raw tail. Shared by the inter-node debug operator and the inline (in-node) repair loop.

        M1/A0c: when `state`+`node` are given, the ANCESTRAL REPAIR CHAIN of the lineage is
        prepended (aira-dojo MEM_OPS `ancestral`) — prior fixes and what they hit — so a repair
        doesn't oscillate undo↔redo with an earlier one."""
        chain = ""
        if state is not None and node is not None:
            from looplab.events.digest import ancestral_repair_chain
            chain = ancestral_repair_chain(state, node)
            if chain:
                chain += "\n\n"
        error = chain + (error or "")
        # WHAT THE PROCESS SAID IT DIED OF, for the role that cannot go and look.
        #
        # `error` here is `_eval_failure_text`'s last 500 characters of stderr, and the fact that
        # says what died is routinely outside it. Measured on `runs/e5small-dr-unified-v4` node 4,
        # `torch.OutOfMemoryError` sits 952 characters from EOF and 908 for "Tried to allocate 2.25
        # GiB", while 329 of that 500-char window is a tqdm bar's trailing whitespace — an effective
        # reach of ~171 characters of real text. The three corpus entries in
        # `tests/test_torch_oom_is_an_oom.py` put it 1,659 / 12,991 / 14,192 characters out. So the
        # Developer was asked to fix an out-of-memory failure without the allocation size, the device
        # or the free memory, all of which its own process printed.
        #
        # HERE AND NOT IN `_eval_failure_text`, and the split is the whole design. That function
        # feeds TWO roles and they are not in the same position: the DIAGNOSTICIAN holds
        # `repair_log_tools` and can PULL the line out of the stage log itself, while the DEVELOPER
        # gets this string and nothing else. docs/44 measured the diagnostician's prompt as
        # byte-identical and argued the trade from ~8.8 provider calls per failure
        # (`tests/test_diagnosis_record.py` pins that as an equality, not a budget); pushing into
        # `_eval_failure_text` would have broken it to give the fact to a reader that could already
        # fetch it. Pushing here gives it to the reader that cannot, and the diagnostician's prompt
        # stays byte-for-byte what it was.
        #
        # It DECIDES nothing — see `failure_diagnosis.failure_headline`, which holds the argument for
        # why a text rule is admissible for a push and is not for a classification.
        if headline and headline not in error:
            error = f"[{headline}]\n{error}"
        # Repair-side twin of the Layer-4 proposal cue. Explicit footprints own the device count;
        # an unspecified footprint retains the historical parallel single-device rule.
        footprint = normalize_researcher_footprint(
            getattr(getattr(node, "idea", None), "footprint", None))
        declared_gpus = (footprint.get("gpus") if footprint is not None
                         and "gpus" in footprint else None)
        if getattr(self, "_repo_spec", None) and declared_gpus is not None:
            if declared_gpus == 0:
                error += ("\n[hardware: this node declares `footprint.gpus=0` and runs CPU-only. "
                          "Keep CUDA/GPU requirements disabled in every repaired command.]")
            else:
                error += (f"\n[hardware: this node reserved exactly {declared_gpus} GPU(s). Every "
                          f"training/eval command must target exactly {declared_gpus} device(s); keep "
                          "that count unchanged across repairs.]")
        elif (getattr(self, "_repo_spec", None) and self._eval_parallel > 1
              and getattr(self, "_gpu_ids", None)):
            error += ("\n[hardware: this legacy unspecified-footprint node is pinned to exactly ONE "
                      "GPU for parallel eval. Keep every training/eval command at one device.]")
        if reason == "timeout":
            # Don't quote a specific budget here: the wall-clock varies by node kind (a sweep node gets
            # timeout×sweep_timeout_mult; a RepoTask uses its own per-profile timeout), so a hardcoded
            # self.timeout would be misleading. The directive — cut compute — is what matters.
            return ("[failure kind: timeout]\n" + error + "\n"
                    "The script exceeded its evaluation time budget and was killed before it produced a "
                    "metric. The IDEA is fine — it was just too slow. Return a corrected, complete script "
                    "that finishes WELL within the budget by reducing compute: fewer estimators/boosting "
                    "rounds, fewer epochs, fewer CV folds or seeds, early stopping, a smaller/lighter "
                    "model, capped n_jobs, or a subsample — keep the approach, cut the cost.")
        if reason == "oom":
            # TWO SHAPES REACH THIS ONE DIRECTIVE and the text has to fit both, because it used to
            # describe only the first. (a) the KERNEL kill — a cgroup limit, SIGKILL, usually no
            # Python traceback; (b) the ALLOCATOR's own `torch.OutOfMemoryError`, which is the
            # opposite: a raised exception with a FULL traceback that names the failing line and the
            # exact allocation. Saying "typically with no Python traceback" to a Developer holding
            # (b)'s traceback invites it to distrust the one piece of evidence that says which
            # tensor was being allocated and how much was already resident.
            # The accumulation clause is also WRONG for (b) as it was worded: under HF/accelerate
            # `gradient_accumulation_steps` does not divide `per_device_train_batch_size`, it
            # MULTIPLIES the effective batch, so "use accumulation instead" applied to a per-device
            # batch is a no-op that reads as a fix. Measured on `runs/e5small-dr-unified-v3`: three
            # nodes declared batch 8192 with accumulation 2 and all three OOMed at the same
            # allocation, on 1 GPU and on 2 alike.
            return ("[failure kind: oom]\n" + error + "\n"
                    "The script ran out of memory before producing a metric — either the "
                    "out-of-memory KILLER stopped it (a cgroup RAM limit; usually no Python "
                    "traceback) or the GPU allocator itself raised (`torch.OutOfMemoryError` / "
                    "`CUDA out of memory`, which DOES leave a full traceback). The IDEA is fine — it "
                    "was just too memory-hungry. Return a corrected, complete script that fits in "
                    "LESS memory: a smaller batch size, a lighter/smaller model, fewer features or a "
                    "subsample of the rows, gradient checkpointing, a shorter sequence length, lower "
                    "precision (float16/bfloat16), or freeing large intermediates — keep the "
                    "approach, cut the memory.\n"
                    "If a traceback IS present, READ IT before choosing: it names the line that was "
                    "allocating, how much it asked for, and how much of the device was already in "
                    "use — that says whether the fix is the batch, the model or a tensor being held. "
                    "Two traps on the numbers. The batch size in a config is usually PER DEVICE, so "
                    "adding GPUs does not shrink it and the same per-device batch OOMs identically "
                    "on 1 device or 8. And `gradient_accumulation_steps` MULTIPLIES the effective "
                    "batch — it does not divide the per-device one — so raising it frees NOTHING; to "
                    "keep an effective batch while cutting memory you must divide the per-device "
                    "batch and raise accumulation by the same factor.")
        if reason == "diverged":
            # The directive that must be said FIRST is the negative one. This kill and an OOM kill are
            # the same SIGKILL with the same absent traceback, so a model reading only the exit code
            # reaches for the memory playbook — which is what happened for three rounds on v6 node 5,
            # each one halving a batch size that was never the problem.
            return ("[failure kind: diverged]\n" + error + "\n"
                    "LoopLab's own training health-check KILLED this stage: the live log reported a "
                    "non-finite loss or grad_norm (NaN/inf) repeatedly, so the model could no longer "
                    "learn and the rest of the budget would have been wasted. This is NOT an "
                    "out-of-memory kill and NOT a timeout — do not reduce batch size, model size or "
                    "sequence length to fix it; that changes nothing about the instability and costs "
                    "another full run to find out. Return a corrected, complete change that makes the "
                    "OBJECTIVE numerically stable: lower the learning rate or lengthen the warmup, "
                    "clip gradients, add an epsilon inside every log/sqrt/division, compute the loss "
                    "in float32 even under mixed precision, guard a masked softmax against an "
                    "all-masked row and a 0*log(0), and reduce or ramp the weight of any newly-added "
                    "auxiliary/regularisation term. Keep the idea; make its arithmetic survivable.")
        if reason == "check_false_positive":
            # The directive that must be said FIRST is, again, the negative one — and here the
            # negative is the whole point. Every other kind in this ladder tells the Developer what
            # to change about the EXPERIMENT. This one says the experiment may be fine and the thing
            # that refused it may be wrong, because that is what the diagnostician just concluded
            # after reading the same log the checker read.
            #
            # MEASURED, on `runs/rubertlite-dense-retrieval`: node 1's diagnosis reads "the run
            # actually reached val recall@100=0.8114 (matching the known-good baseline 0.81), yet
            # the verifier flagged" it; n9 and n16 refute the checker with validation recall from
            # the SAME log. Without this branch those rows were handed the ordinary "here is your
            # error, fix your code" context, which asks a Developer to rewrite a training run whose
            # numbers it has just been told are correct — and the cheapest way to satisfy a wrong
            # check is to break the thing it was checking.
            #
            # "Read its rationale above" IS TRUE SINCE 2026-08-28, and it was not before. `error`
            # here is `_evaluate`'s `_err_in`, and nothing on the repair path spliced the
            # diagnostician's verdict into it — `triage["rationale"]`/`summary` reached the durable
            # rows and the critic, never `Developer.repair`. So the one kind whose whole directive is
            # "the diagnostician believes the check is wrong — read WHY" handed the Developer only
            # the refusal being disputed, with the refuting numbers (n1's "val recall@100=0.8114")
            # nowhere in its context. `failure_diagnosis.diagnosis_repair_lead` now prepends the
            # already-redacted, already-capped `reason_summary` for any TRIAGE-sourced reason, in
            # the same position `_evaluate` has always prepended `watchdog_err` on the
            # `not_learning` path — which is why the identical sentence was true there and false
            # here. The lead is suppressed for an ENGINE-final reason (a fact the engine measured is
            # not a model's account) and when the summary is already in the text (the watchdog
            # prepends upstream, and one finding said twice reads as two agreeing).
            return ("[failure kind: check_false_positive]\n" + error + "\n"
                    "THE STAGE'S DECLARED CHECK REFUSED THIS RUN, AND THE FAILURE DIAGNOSTICIAN — "
                    "reading the same log afterwards, with more of it — BELIEVES THE CHECK WAS "
                    "WRONG. Read its rationale above before you touch anything.\n"
                    "Do NOT start by rewriting the experiment to satisfy the check. If the run "
                    "genuinely met its declared condition, changing the training code to please a "
                    "faulty assertion damages a working experiment, and the cheapest way to satisfy "
                    "a wrong check is to break the thing it was checking.\n"
                    "Work in this order: (1) re-read the stage's `expect`/`assert` in "
                    "`looplab_stages.json` and decide whether it actually describes success for THIS "
                    "run — a threshold copied from a different backbone, a file path the run writes "
                    "under a different name, an epoch count the config no longer uses; (2) if the "
                    "check is wrong, FIX THE CHECK and say so plainly in your rationale, changing "
                    "nothing about the experiment; (3) only if the check is right after all, fix "
                    "the code it caught. Say which of the three you did — the record cannot tell "
                    "them apart afterwards unless you do.")
        if reason == "stalled":
            # Same misclassification hazard as `diverged`, opposite fix: the process was ALIVE and
            # silent, so there is nothing to read in stderr and nothing memory-shaped to cut.
            return ("[failure kind: stalled]\n" + error + "\n"
                    "LoopLab's stall watchdog KILLED this stage: it stayed alive but produced no "
                    "output for the whole stall window — a hung distributed barrier, a wedged CUDA "
                    "op, a deadlocked dataloader or a lock nobody releases. This is NOT an "
                    "out-of-memory kill and NOT a timeout; reducing batch or model size does not "
                    "unblock a deadlock. Return a corrected, complete change that either removes the "
                    "hang (a barrier every rank reaches, a bounded timeout on the blocking call, "
                    "`num_workers=0` / no fork under a multithreaded runtime, no lock held across a "
                    "collective) or makes progress observable — print or flush a heartbeat line at "
                    "least once per inner loop so the next run reports WHERE it stopped.")
        if reason == "not_learning":
            # The THIRD member of this family, and the one whose negative directive has to be
            # widest: this kill looks like nothing. No traceback, no non-finite number, no silence
            # — a healthy-looking process printing a loss that simply never moves. The live judge
            # named the IMPLEMENTATION, and its sentence is already at the head of `error`; what
            # this adds is the shape of the fix, which is neither `oom`'s nor `diverged`'s. Cutting
            # memory changes nothing, and lowering the learning rate — `diverged`'s first move — is
            # if anything the wrong direction for a model that is not learning at all.
            return ("[failure kind: not_learning]\n" + error + "\n"
                    "LoopLab's live training watchdog KILLED this stage: the loss stopped moving "
                    "(frozen or flat well above where it should be) while the run reported itself "
                    "healthy, so the remaining budget would have trained a model that never "
                    "learned the task. This is NOT an out-of-memory kill, NOT a timeout and NOT a "
                    "numeric divergence — nothing was non-finite, and reducing batch or model size "
                    "does not make a frozen objective move. Treat it as a BUG until you have "
                    "checked otherwise, and check the specific thing the watchdog named above "
                    "first. The usual causes are mechanical: the loss reduced over the wrong axis "
                    "or with the wrong sign, embeddings normalized (or not) inconsistently between "
                    "the two towers, a temperature or margin that makes every pair identical, "
                    "labels or positives misaligned with their inputs, a dataloader yielding the "
                    "same batch every step, a scheduler that drove the learning rate to ~0, a "
                    "frozen/detached parameter set that leaves nothing to update, or a "
                    "regularisation term whose minimum is a constant embedding. Return a corrected, "
                    "complete change that makes the objective ABLE to descend, and print enough per "
                    "step (loss AND grad_norm AND lr) that the next run shows whether it did. If "
                    "after looking you conclude the code is right and the IDEA is simply wrong for "
                    "this setup, say so plainly instead of changing something at random — a real "
                    "negative result is worth more than a repair that hides it.")
        if reason == "needs_failed":
            # The one directive that must NOT say "diagnose the crash": there was no crash. The stage
            # was refused before it started, so its code is not evidence of anything yet, and the two
            # candidate repairs are both in DECLARATIONS — either the earlier stage's `expect.files`
            # or this stage's `needs`. The error text already names both sides where it can.
            return ("[failure kind: needs_failed]\n" + error + "\n"
                    "This stage never ran. It DECLARES an input file (`needs` in the stage manifest) "
                    "that was not present in the eval workdir when its turn came, so LoopLab refused "
                    "to start it rather than spend the stage's runtime on a pipeline that cannot "
                    "succeed. Do not debug this stage's code — it did not execute. Find which of the "
                    "two declarations is wrong: either an EARLIER stage writes that file somewhere "
                    "other than where it declares it (fix that stage's code or its `expect.files`), "
                    "or THIS stage's `needs` names a path the pipeline never produces (fix the "
                    "`needs` entry to the real path). Removing the `needs` entry is not a fix — it "
                    "only moves the same failure into the stage's own loader, later and more "
                    "expensively.")
        if self._deep_repair:
            return (f"[failure kind: {reason or 'unknown'}]\n{error}\n"
                    "Diagnose the root cause; if it's unclear, add a tiny reproduction/"
                    "assert near the failure, then return a corrected, complete script.")
        return error

    def _prepare_env(self, stderr: str) -> list[str]:
        """Environment self-prep: pip-install the KNOWN libraries a crash reports as missing, into
        the eval interpreter, so the engine can re-run instead of rejecting the idea. Returns the
        pip packages successfully installed (empty => nothing to do / install failed -> normal
        triage). Trusted_local only (gated by the caller via `self._auto_install_deps`).

        Per-package so a partial failure only stops the bad name; `_dep_attempted` + `_dep_lock`
        make it install-once-per-module and concurrency-safe (pip mutates one shared env)."""
        from looplab.runtime import deps
        # Parse the missing KNOWN libs BEFORE taking the lock — a crash with nothing to install (the
        # common case, and every non-dep crash) must not block on `_dep_lock` while another eval holds
        # it through a multi-minute pip install (max_parallel>1). Only contend for the lock when there
        # is real installable work.
        candidates = [m for m in deps.missing_modules(stderr) if deps.is_installable(m)]
        # A MISSING SUBMODULE OF AN INSTALLED DISTRIBUTION IS NOT A MISSING DISTRIBUTION, and pip
        # cannot tell the difference because it is never asked about the submodule. Live 2026-08-07
        # (`runs/rubert-dr-0807` node 0, round 1): the repo's `utils.py` does
        # `from pytorch_lightning.utilities.cloud_io import get_filesystem`, a module Lightning 2.x
        # DELETED, so the traceback read `No module named 'pytorch_lightning.utilities.cloud_io'`.
        # `missing_modules` reduced that to `pytorch_lightning` — installed at 2.6.5 — and
        # `pip install pytorch-lightning` answered "Requirement already satisfied" with returncode 0
        # in 2.19 s. `InstallResult.ok` is `returncode == 0`, so the engine recorded
        # `deps_installed {"packages": ["pytorch-lightning"], "round": 1}`, spent one of
        # `_MAX_DEP_ROUNDS`, and re-ran the node's whole stage pipeline into the byte-identical
        # exception (the next `node_repaired.error_in` is that same traceback). Worse than the wasted
        # eval: the receipt says the environment was just fixed, so the failure that follows reads as
        # the agent's code being wrong.
        #
        # The probe is confined to the DOTTED-ONLY names on purpose. A bare `No module named 'torch'`
        # keeps today's behaviour byte-for-byte, no extra interpreter spawn and no new way to fail:
        # `is_present` fails closed to "present" = "do not install", and failing closed on the fresh-box
        # case (nothing installed yet, which is the reason this module exists at all) would be a
        # regression dressed as a safety check. Here the direction is right — doubt means "leave it to
        # the repair path", which is where a version mismatch belongs anyway.
        suspects = [m for m in candidates if m in deps.submodule_only_modules(stderr)]
        if suspects:
            python = getattr(self.sandbox, "python", sys.executable)
            present = {m for m in suspects if deps.is_present(m, python=python)}
            candidates = [m for m in candidates if m not in present]
        return self._install_missing(candidates)

    def _install_missing(self, candidates: list[str]) -> list[str]:
        """Install the ALLOWLISTED import names in `candidates` that this run has not already tried,
        returning the pip packages that installed cleanly. The shared tail of both env-prep entry
        points — the traceback-driven `_prepare_env` and the triage-driven
        `_prepare_env_from_triage` — so `_dep_lock`, the once-per-module `_dep_attempted` cache, the
        injected installer seam and the install span have exactly one implementation."""
        from looplab.runtime import deps
        if not candidates:
            return []
        with self._dep_lock:
            mods = [m for m in candidates if m not in self._dep_attempted]  # re-check inside the lock
            if not mods:
                return []
            python = getattr(self.sandbox, "python", sys.executable)
            installer = self._dep_installer or deps.install
            # What the REPO says about these distributions. Read once per call, from the same cached
            # Declaration `_ensure_run_setup` installed from, so the run cannot install one set of
            # pins and enforce another. `None` for a non-repo task — then this whole block is inert
            # and the behaviour below is byte-for-byte what it was.
            decl = self._declared_deps()
            installed: list[str] = []
            for mod in mods:
                self._dep_attempted.add(mod)    # one pip attempt per module per run (success or fail)
                pkg = deps.pip_package(mod)
                # AN INSTALL MUST NOT SILENTLY MOVE A PACKAGE THE REPO PINNED. `pip install
                # pytorch-lightning` against a repo pinning `pytorch_lightning==1.5.1` is how
                # `runs/rubert-dr-0804` acquired Lightning 2.6.5, and the Lightning-2.x SHAPE is then
                # all over `rubert-dr-0807`'s repairs — read off its event log 2026-08-07, BOTH failing
                # nodes carry it: a `pytorch_lightning/trainer/configuration_validator.py ...
                # NotImplementedError` repair each, node 0's terminal is the DDP `_rebuild_buckets`
                # RuntimeError, and node 2 carries a second "It looks like your Lightning ..." one.
                # (An earlier note put it at 7 of that run's 12 repairs; that count is NOT re-derived
                # here — the durable `error_in` tails are truncated, so treat it as the shape being
                # pervasive rather than as a figure.) So when the declaration names this distribution,
                # pip is handed the operator's own line VERBATIM rather than a bare name. Verbatim
                # matters twice: LoopLab never has to parse a specifier grammar, and the receipt below
                # quotes exactly what the repo wrote. Measured 2026-08-07 in a tmpfs venv: the declared
                # 1.5.1 imports against this container's torch 2.4.0 and HAS
                # `pytorch_lightning.utilities.cloud_io.get_filesystem` — the module 2.x deleted and
                # the one node 0 died on in round 1.
                #
                # WHY NOT `-c requirements.txt`. A constraints file would bind the transitive
                # resolution too, which is strictly stronger — and pip cannot read this repo's file
                # at all. Measured 2026-08-07 against the live testbed's own requirements.txt:
                #     ERROR: Constraints cannot have extras
                # because it declares `bm25s[full]`. A mechanism that fails closed on the very file
                # it exists to honour is not a mechanism; per-package resolution degrades to today's
                # behaviour for a line it cannot use, which is the right direction.
                pin = deps.declared_requirement(mod, decl)
                req = pin or pkg
                before = deps.installed_version(pkg, python=python)
                try:
                    with self.tracer.span("install_dep", package=pkg, requirement=req):
                        res = installer(req, python=python, timeout=self._dep_install_timeout)
                except Exception:  # noqa: BLE001 - a misbehaving installer must degrade to "not installed",
                    res = None     # not crash the eval; the node then flows to normal triage/repair.
                if getattr(res, "ok", False):
                    installed.append(pkg)
                    # The before/after pair is the whole reportability story for an install: without
                    # it the log says `pytorch-lightning` was installed and an operator still cannot
                    # tell whether that was a no-op or a two-major-version move. Recorded on the
                    # engine for `_evaluate` to stamp onto `deps_installed` — the append site is
                    # there because that is where the write lock and the node/generation key are.
                    self._dep_receipts[pkg] = {
                        "package": pkg, "requirement": req, "declared": pin,
                        "before": before, "after": deps.installed_version(pkg, python=python)}
            return installed

    def _drain_dep_receipts(self, packages: list) -> list:
        """The per-package install receipts for `packages`, REMOVED from the pending map.

        `_install_missing` runs in a worker thread and records `{requirement, declared, before,
        after}` per package; `_evaluate` stamps them onto `deps_installed` under `_write_lock`.
        Draining rather than reading means a later round in the same node cannot re-report an
        install that already has a receipt on the log — the rows would then disagree about how many
        times a package moved.

        Ordered by `packages` (the caller's own order) so the receipt list lines up index-for-index
        with the `packages` field beside it. A package with no receipt is simply absent rather than a
        `None` hole: the only way to get here without one is an injected test installer, and a list
        of nulls would read as "we looked and found nothing" rather than "we did not look"."""
        pending = getattr(self, "_dep_receipts", None)
        if not pending:
            return []
        out = [pending.pop(p) for p in (packages or []) if p in pending]
        return out

    def _prepare_env_from_triage(self, triage: dict, stderr: str) -> list[str]:
        """Environment self-prep for a missing library the TRACEBACK NEVER NAMES.

        `_prepare_env` above can only install what the traceback reports as missing, which misses
        the whole class of failures where a library DEGRADES an absent optional dependency into
        something else. Live 2026-08-05 (`runs/rubert-dr-0805` node 0): `transformers` guards
        `init_empty_weights`/`find_tied_parameters` behind `is_accelerate_available()`, so an absent
        `accelerate` — already on the install allowlist — surfaced as a bare
        `NameError: name 'init_empty_weights' is not defined` with the word "accelerate" nowhere in
        the exception. Env-prep saw nothing installable, and the node spent TWO of its six repair
        attempts hand-patching symbols instead. The crash-triage agent named the cause correctly
        both times and had no way to act on it.

        So the agent's verdict becomes an admissible SOURCE of the name — never on its own:
        `deps.triage_install_candidates` requires the traceback to be unresolved-name shaped and the
        triage to point at that same traceback (its docstring is the contract), and we additionally
        install only what is provably ABSENT from the eval interpreter. Returns the pip packages
        installed (empty => nothing to do, and the caller proceeds to a normal code repair)."""
        from looplab.runtime import deps
        named = deps.triage_install_candidates(
            str(triage.get("missing_dependency", "") or ""),
            str(triage.get("rationale", "") or ""), stderr or "")
        if not named:
            return []
        python = getattr(self.sandbox, "python", sys.executable)
        # The decisive filter, and the one the agent cannot influence: a package that is already
        # importable is not the cause of anything, so naming it installs nothing. Probed against the
        # EVAL interpreter (`find_spec`, no import) and fail-closed to "present" on any doubt.
        # `_dep_attempted` is consulted FIRST (an unlocked read; `_install_missing` re-checks it
        # under `_dep_lock`) purely to skip the probe for a module this run already tried — an agent
        # that keeps naming the same failed install would otherwise spawn one per repair.
        absent = [m for m in named
                  if m not in self._dep_attempted and not deps.is_present(m, python=python)]
        return self._install_missing(absent)
