"""Staged-eval resolution & selective re-run (Phase 1/2) for the engine — extracted from
orchestrator.py as a MIXIN: `class Engine(EvalStagesMixin, …)` inherits these methods unchanged,
so there is ZERO call-site churn and `self` here IS the engine. The method bodies are verbatim
moves (only the three `Engine.`-qualified staticmethod self-references became
`EvalStagesMixin.`-qualified) and read engine attributes freely (`_eval_spec`,
`_strategy_fidelity`, `_reflect_client`, `_idea_text`, …), exactly as they did inside the class.

The cluster: manifest -> concrete stages (`_resolve_stages`, and over it `_eval_pipeline` — the ONE
profile -> command -> chain derivation the eval DISPATCHER and every PLANNER share, of which
`_resolved_stages` is the planners' total wrapper), the static
import-reachability analysis behind safe stage reuse (`_imported_modules` /
`_module_file_candidates` / `_stage_reachable_files` / `_safe_reuse_start`), and the LLM
stage-check factory (`_stage_check_fn`). Heavy deps (command_eval, json) stay method-local so
monkeypatching through their source modules keeps working.

Layering: no runtime import of the orchestrator and never serve — only core/stdlib at module
level (runtime/agents deps are lazy, method-local imports)."""
from __future__ import annotations

import os
import re
from pathlib import Path

from looplab.core.llm import BudgetExceeded
from looplab.core.llm_broker import in_llm_lane

# The reply protocol the inter-stage checker answers in, and the ONLY three things it can mean. The
# vocabulary itself lives in `runtime/command_eval.py` beside the code that acts on it; this is the
# wire format and its parser. See `command_eval.STAGE_CHECK_HARD_KINDS` for the 46.6 GPU-hours that
# made a vocabulary necessary — until 2026-08-13 the rule reading this reply was `startswith("OK")`
# and everything else ended the node.
_STAGE_CHECK_OK = "OK"
_STAGE_CHECK_FAIL = "FAIL"
_STAGE_CHECK_UNSURE = "INCONCLUSIVE"
_STAGE_CHECK_REPLY_RE = re.compile(
    r"^\s*(?P<verb>OK|FAIL|INCONCLUSIVE)\b[ \t]*(?P<kind>[a-z_]+)?[ \t]*[:\-—]?[ \t]*(?P<rest>.*)",
    re.IGNORECASE | re.DOTALL)


STAGE_MANIFEST_NAME = "looplab_stages.json"

# THE ONE CONDITIONAL BLOCK of the stage checker's prompt (doc 52 row 9): spliced at the END of the
# system message when `train_monitor.stage_check_tools` hands the checker its log tools, so
# `stage_check_tools=false` — and every caller without a workdir — reproduces the historical prompt
# byte for byte. It names the log by the STAGE, because `monitor_log_sources` names sources after
# their files and the checker is asked about exactly one stage. `{stage}` is the only placeholder.
STAGE_CHECK_LOOK_INVITATION = (
    "YOU CAN LOOK FURTHER. The output tail below is the last 4,000 characters of a stdout that is "
    "itself a 64,000-byte tail of this stage — a few dozen records of a run that may have taken "
    "hours. The trainer's banner, the first losses, a `Saving model` line, a traceback that "
    "preceded a long progress bar, and every restart are OUTSIDE that window by construction. "
    "Before you answer, USE YOUR TOOLS on this stage's own log, `{stage}.log`: `read_log` with "
    "mode `search` for a traceback, an error, or the summary line; mode `head` for how the run "
    "started; `metric_series` for what the loss actually did over the whole stage. An answer "
    "grounded in the log outranks one read off the tail — when the tools contradict the tail, the "
    "tools have more evidence. When you have looked, give the ONE-LINE verdict in exactly the form "
    "above through `answer`; the vocabulary of kinds is unchanged and looking widens only what you "
    "can see.")

# The tool-loop turn budget for ONE stage check. Below the watchdog's nine because this judge runs
# on the EVAL-BLOCKING path — the eval worker waits between two stages for it and the whole batch
# inherits its latency (`_stage_check_fn`'s docstring) — and it has one log and one question: a
# search, a head, a series, a follow-up, and the answer. A loop that spends it degrades to the plain
# completion over the same messages rather than to nothing (`agents.tool_loop.agentic_text`).
STAGE_CHECK_LOOK_TURNS = 6


def stage_check_verdict_line(text: str) -> str:
    """The line of a tool-using checker's answer that carries the verdict, else the answer itself.

    `parse_stage_check_reply` reads the reply from its FIRST character, which is the historical
    one-line contract and stays it on the plain path. A checker that has just read a log through
    its tools tends to say what it found before it says the verdict, and reading that preamble as
    the whole reply would demote every such answer to `inconclusive` — i.e. the tools would make the
    checker LESS able to name the NaN loss it was built to catch. Applied on the agentic path only,
    so a reply that carries no verdict line at all is still the unstructured answer it always was.
    """
    for line in (text or "").splitlines():
        if line.strip() and _STAGE_CHECK_REPLY_RE.match(line):
            return line.strip()
    return (text or "").strip()


def manifest_prefix_unchanged(prev_manifest: object, stages: list, failed_stage: str,
                              params: object = None) -> bool:
    """Does a stage-manifest edit leave the stages BEFORE `failed_stage` byte-identical?

    The one narrowing of `_safe_reuse_start`'s manifest clause, hoisted out of it so the rule has a
    truth table a test can reach rather than living inside a predicate no caller can address. It
    answers ONLY the manifest question; every other clause of that predicate still runs afterwards,
    over a change set this function's `True` merely removes the manifest FROM.

    `prev_manifest` is the manifest TEXT of the node as the ENGINE last committed it —
    `node.files["looplab_stages.json"]` off the fold, never the copy on disk. That distinction is
    the trust model and not a convenience: the workdir is the candidate's, an eval can rewrite its
    own manifest while it runs, and a predicate that read the pre-repair chain off disk would let a
    stage that rewrote the manifest at the end of its own run declare its output still valid. What
    is compared here is bytes the engine authenticated on both sides — what it committed last
    attempt against `stages`, the chain it resolved from what it committed this attempt.

    TRUE requires ALL of:
      • both manifests RESOLVE (`materialized_stages` — the same validator the pipeline is built by);
      • `failed_stage` is at the SAME INDEX in both. This is the clause that cannot be dropped: it
        is what forbids a stage being INSERTED into or REMOVED from the region that already ran. A
        removed `prep` leaves its outputs on disk, and a reuse would then feed `train` an artifact a
        full re-run provably would not have produced — the silent stale input this whole predicate
        exists to prevent, arriving through a manifest that only "shrank".
      • every entry before that index is EQUAL as canonical JSON. The entry is `validate_stages`'
        clean form and its key set is CLOSED (`name`, `command`, `timeout`, `check`, `expect`,
        `needs`, `env`), so this covers the stage's name, its argv, its declared artifacts AND their
        `assert` sentence, its declared inputs, its declared environment and its leash — there is no
        field of a stage a change to which this compare can miss. Equality of the whole prefix also
        subsumes ORDER and every RENAME.
      • `failed_stage` is IN the previous manifest. It is absent exactly when the failure was in the
        engine-appended protected `score` stage, in which case the whole manifest is the prefix and
        no manifest edit can be "after" it — the fail-closed answer, and a real corpus row
        (`rubertlite-dr-unified-v8` node 0 attempt 4, whose `mine` entry had genuinely changed).

    WHAT MAKES "STRICTLY AFTER" SUFFICIENT, stated because it is the whole argument. A stage runs in
    the pipeline's own order in one workdir, so nothing declared at index > i is read by the process
    at index i — `_run_stages` resolves `env` PER STAGE onto `ex.env` rather than accumulating it,
    there is no per-stage `cwd` to re-base an earlier one, `needs` is checked before its OWN command
    and `expect` after it, and the appended `score` stage is built from the operator's EvalSpec and
    never from the manifest. There is no field by which a later entry reaches backwards. What a
    later edit CAN invalidate is whether the earlier artifact is the right INPUT for the new later
    stage — and that is not a question about the artifact's freshness at all: it is answered by the
    later stage's own `needs` check and by its running and failing loudly, not by silently scoring a
    checkpoint nobody trained.

    `params` is the node's `idea.params`, applied to BOTH sides through the same `expand_params`
    `_resolve_stages` uses, so a prefix entry carrying a `%params%` token compares expanded-to-
    expanded. A repair never writes `idea.params`, so the two sides are expanded identically.

    FALSE on anything it cannot resolve, and that is the default: with no `prev_manifest` (the
    keyword's absent value at every call site that has not been taught to pass one) this returns
    False and the caller forfeits exactly as it did before this function existed.
    """
    import json
    from looplab.core.jsonutil import canonical_json
    from looplab.runtime import command_eval

    def _entries(text):
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            clean = command_eval.materialized_stages(json.loads(text))
        except Exception:  # noqa: BLE001 — an unresolvable manifest proves nothing; forfeit
            return None
        if not clean:
            return None
        return [dict(s, command=command_eval.expand_params(list(s["command"]), params))
                for s in clean]

    prev = _entries(prev_manifest)
    if prev is None or not stages or not failed_stage:
        return False
    # UNFILTERED, deliberately: `_safe_reuse_start` derives its own `fi` from exactly this
    # expression, and dropping a non-dict here would index a filtered list while slicing an
    # unfiltered one — two stage lists that agree on every name and disagree about where the
    # reuse point is. The entry compare below still refuses anything that is not a dict.
    names = [str(s.get("name")) if isinstance(s, dict) else object() for s in stages]
    prev_names = [str(s.get("name")) for s in prev]
    if failed_stage not in names or failed_stage not in prev_names:
        return False
    fi = names.index(failed_stage)
    if fi == 0 or prev_names.index(failed_stage) != fi:
        return False
    if len(prev) < fi or len(stages) < fi:
        return False
    for old, new in zip(prev[:fi], stages[:fi]):
        if not isinstance(new, dict) or canonical_json(old) != canonical_json(new):
            return False
    return True


def parse_stage_check_reply(text: str, *, declared: bool):
    """The checker's reply -> `StageCheckVerdict | None` (None = the stage passed). Pure and TOTAL.

    Hoisted to module level with a truth table (`tests/test_stage_check_verdict.py`) rather than left
    inside the closure, because it is the rule that decides whether a 15-hour training is thrown
    away, and a rule nobody can state is a rule nobody reviews.

    Every answer that is not an explicit, in-vocabulary `FAIL <kind>` becomes `inconclusive` — prose,
    an out-of-enum kind, a bare `FAIL`, an empty reply. That is the same mechanical enforcement
    `triage.py::coerce_triage_action` applies for the same reason: "nobody could read this" is its
    own fact, and resolving it to the most expensive action is how a checker that was told twice not
    to judge quality went on killing nodes with "recall (0.79) is below previous best (0.8491)".

    `declared` is whether the stage carried an `expect.assert`. Without one,
    `declared_condition_violated` is refused and degrades to `inconclusive` — a checker may not
    invoke a contract that does not exist, and that comparison against "previous best" is precisely
    what an undeclared contract looks like from the inside."""
    from looplab.runtime.command_eval import (STAGE_CHECK_INCONCLUSIVE, StageCheckVerdict,
                                              coerce_stage_check_kind)
    raw = (text or "").strip()
    if not raw:
        return None                      # nothing said is not a failure — historical behaviour
    m = _STAGE_CHECK_REPLY_RE.match(raw)
    if m is None:
        return StageCheckVerdict(STAGE_CHECK_INCONCLUSIVE, raw[:300])
    verb = m.group("verb").upper()
    rest = (m.group("rest") or "").strip()
    kind_word = (m.group("kind") or "").strip().lower()
    if verb == _STAGE_CHECK_OK:
        return None
    detail = (f"{kind_word}: {rest}" if (kind_word and rest) else (rest or kind_word or raw))[:300]
    if verb == _STAGE_CHECK_UNSURE:
        return StageCheckVerdict(STAGE_CHECK_INCONCLUSIVE, detail)
    kind = coerce_stage_check_kind(kind_word)
    if kind == "declared_condition_violated" and not declared:
        kind = STAGE_CHECK_INCONCLUSIVE
    return StageCheckVerdict(kind, detail)


# The deadline judge's reply protocol. Deliberately the SMALLEST possible answer — a verdict word
# and nothing else — because the only thing the engine may take from it is a boolean, and a model
# that is allowed to name its own number will name a large one.
_DEADLINE_FINISHING = "FINISHING"
# NOT_FINISHING is listed FIRST and the verdict is read from the GROUP, never from "did it match".
# Ordered the other way — and read as a bare truthiness test — `NOT_FINISHING` matched the pattern
# and bought the extension it was refusing, which `tests/test_deadline_grace.py` caught.
_DEADLINE_REPLY_RE = re.compile(r"^\s*(NOT_FINISHING|FINISHING)\b", re.IGNORECASE)


def parse_deadline_reply(text: str) -> bool:
    """Did the judge say the stage is about to finish? Pure, TOTAL, and fail-CLOSED.

    Anything that is not an explicit `FINISHING` — prose, `NOT_FINISHING`, an empty answer, a
    hedge — is False, i.e. the historical kill. This direction is the opposite of the stage
    checker's and deliberately so: there, refusing to act on an unreadable answer SAVES a node; here,
    acting on one SPENDS money. The unreadable case must resolve to whichever answer is cheap, and
    that is not the same answer in both places."""
    m = _DEADLINE_REPLY_RE.match((text or "").strip())
    return m is not None and m.group(1).upper() == _DEADLINE_FINISHING


class EvalStagesMixin:
    """The engine's staged-eval cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _resolve_stages(self, workdir, es, params=None, score_cmd=None, score_timeout=None):
        """Resolve the ordered eval pipeline, with the operator's `cmd` (es) AUTHORITATIVE and
        non-overridable (redesign: the agent can't rewrite how it's scored):

          • cmd DECLARES stages (`es["stages"]`) → those ARE the pipeline, canonical. The Developer
            only implements the scripts each stage calls; a `looplab_stages.json` is IGNORED.
          • cmd is a SINGLE command → the Developer's `looplab_stages.json` supplies the PRECEDING
            stages (data_prep / train / …); the operator's cmd is appended as the final, protected
            `score` stage whose stdout the trusted metric reader reads. So the agent can add work
            BEFORE scoring but never replace the scoring itself.

        `score_cmd`/`score_timeout` (the profile- + params-resolved command/timeout from
        `build_command`) drive the appended score stage, so the smoke/full eval PROFILE and its
        timeout still apply in pipeline mode — not just the base command at the default timeout.
        `%params%` tokens in any preceding stage command expand to the node's params. Returns None
        (classic single-command eval) when there are no stages."""
        import json
        from looplab.runtime import command_eval

        def _expand(stages):
            # per-node %params% expansion over an already-VALIDATED clean stage list
            return [dict(s, command=command_eval.expand_params(list(s["command"]), params))
                    for s in stages]

        # THE HOST SCORER (doc 52 row 10a): the operator's own scoring program, appended as the
        # FINAL protected `score` stage of every shape below, from a path outside every editable
        # tree (`adapters/repo_task.py::HostScorerSpec`). When it is declared the candidate's own
        # `command` still runs — as the stage BEFORE it, named `self_score`, so its printed number
        # is recorded as `self_metric` — and the eval is ALWAYS staged, because the single-command
        # path has no place for a second program. `needs` under `require` derives onto whichever
        # stage is last, which is this one whenever it exists.
        host = self._host_scorer_stage(es, params)
        task_stages = es.get("stages")
        if isinstance(task_stages, list) and task_stages:
            # cmd declares stages → canonical, dev file ignored. EvalSpec validated these at submit
            # time; re-run the shared validator anyway (an old/hand-edited snapshot bypasses pydantic)
            # and fall back to the single command on a bad list rather than run a half-parsed pipeline.
            # `allow_env=True`: this branch reads the OPERATOR's own declared pipeline, which is
            # the only declarer allowed a stage `env`. The developer-manifest branch below goes
            # through `materialized_stages`, which keeps the fail-closed default.
            # `existing_run=True`: this is the run's OWN recorded pipeline being re-read, not a
            # submission. A refusal here does not reach the operator — it lands on the fallback
            # below — so a retroactive clause (the closed stage-key set) would silently discard
            # the declared pipeline and score later nodes of this run by a different one.
            clean, err = command_eval.validate_stages(
                task_stages, allow_env=True, existing_run=True)
            if err is None:
                return _expand(clean) + ([self._with_final_needs(es, host)] if host else [])
            # A BAD operator list falls back to the SINGLE COMMAND, and must not fall THROUGH to the
            # developer-manifest branch below. Falling through handed stage authorship to the
            # agent-authored looplab_stages.json in exactly the case this branch exists to prevent
            # ("the agent can't rewrite how it's scored"): one malformed stage in an old/hand-edited
            # snapshot silently promoted the Developer's preceding stages into the pipeline. The
            # operator's `cmd` still runs — dropping their unusable stage list is the conservative
            # reading, and the alternative (raising) would fail a resumed run on a snapshot the
            # engine can still honour the scoring half of.
            if host:
                return self._candidate_then_host(es, params, score_cmd, score_timeout, host)
            return None

        # single-command cmd: read the Developer's PRECEDING stages, append the protected cmd stage.
        # `materialized_stages` applies the SAME shared rules as the declare_stages tool (reserved
        # 'score', no duplicates, argv shape) and accepts both the wrapped + bare-list manifest shapes
        # — the tool is the friendly authoring path, but write_file / a CLI-agent diff / a pre-redesign
        # run can still hand-write the manifest, and an unvalidated one could smuggle a second 'score'
        # stage (clobbering score.log and confusing stage-scoped re-runs) or a full scorer that
        # double-runs the eval after cmd is appended. Invalid → None (single-command fallback), never a
        # half-cleaned pipeline. It is the SAME helper the repo-Developer prompt reads, so the two can't
        # drift (M7).
        preceding = None
        mf = Path(workdir) / "looplab_stages.json"
        if mf.exists():
            try:
                preceding = command_eval.materialized_stages(json.loads(mf.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — a malformed manifest just falls back to the single command
                preceding = None
        if preceding:
            # THE DIVERGENCE THAT GOT PAST THE AUTHORING GATE, recorded and NOT enforced.
            #
            # `declare_stages` refuses a stage timeout above the operator's per-eval budget, but that
            # gate covers the tool. A manifest can still arrive over budget three ways: hand-written
            # with `write_file` on a surface that admits `.json`, CARRIED OVER from a parent on an
            # improve/merge (the shape `rubertlite-dr-unified-v7` node 1 has, at 90000 s against
            # 21600 s), or resumed from a run that predates the gate. Those are the manifests the
            # operator most needs to know about and the ones an authoring refusal can no longer reach.
            #
            # Recorded rather than CLAMPED, deliberately, and the arithmetic is the argument: on v7
            # node 0 a clamp discards 21600 s of GPU time and returns no metric to avoid 6360 s of
            # unbudgeted spend. Killing a training at the wall is the most expensive possible moment
            # to enforce a ceiling and the only one at which nothing can act on it — so what belongs
            # here is the deterministic half (the FACT, into the record) and not the kill. The
            # protected `score` stage is unaffected either way: it is built below with the operator's
            # own `score_timeout` and never with a declared one.
            #
            # `eval_spec_time_budget(es)` and NOT `score_timeout`: the budget is the LARGEST profile,
            # which is the number both roles were told to size against, while `score_timeout` is
            # whichever profile THIS eval selected — comparing against the 60 s smoke profile would
            # report every full-fidelity `train` stage as a divergence.
            _budget = command_eval.eval_spec_time_budget(es)
            command_eval.record_stages_over_time_budget(
                preceding, _budget, at="resolve", enforced=False)
            # the operator's cmd is the FINAL + protected scoring stage; reuse the already
            # profile-resolved command/timeout (build_command) so smoke/full still applies.
            final = {"name": "score",
                     "command": list(score_cmd) if score_cmd is not None
                                else command_eval.expand_params(list(es.get("command") or []), params),
                     "timeout": score_timeout if score_timeout is not None else es.get("timeout", 600.0)}
            # THE SCORE STAGE'S INPUT CONTRACT, derived from the metric's declared SUBJECT.
            #
            # `needs` has shipped since 2026-08-13 — `validate_stages` accepts it, `verify_stage_inputs`
            # implements it, `_run_stages` calls it and emits a `needs_failed` row. The ONE missing
            # wire was here: the engine-appended protected stage was built as {name, command, timeout},
            # so the only stage the operator owns was the only stage that could not declare what it
            # reads. The operator has no other place to put it — this stage is appended BY the engine
            # precisely so the agent cannot rewrite scoring.
            #
            # Derived rather than a second declaration: the subject IS what the scorer reads, so
            # asking the operator to write it twice is how the two come to disagree. What this buys is
            # LATENCY, not coverage — the check fires before the scorer runs, with a message naming
            # the fix, instead of after. It is measured NOT to catch the incident this whole design
            # exists for (doc 35 §3a: `verify_stage_inputs` against the preserved v6 node-4 workdir,
            # with a perfectly correct `needs` naming the node's own checkpoint, returns `None` — the
            # node really did write that file and the stage read somewhere else anyway). `needs` is a
            # PRESENCE check and must stop being described as a provenance gate.
            #
            # `require` AND NOT `!= "off"`, and this is the rung rule, not a tightening. `audit`'s
            # whole contract — `Settings.metric_subject`, `metric_subject.MODES`, the settings table
            # — is "RECORD, always … no violation, no selection effect", and it is the rung an
            # operator turns on to FIND OUT whether `require` is safe before paying for it. Gating
            # the score stage here made that false in the worst available direction: driven
            # 2026-08-14, a declared subject the pipeline did not produce returned
            # `needs_failed` + `metric=None` under `audit`, i.e. the node lost its number entirely,
            # while the rung `audit` is documented to be MILDER than keeps the metric and records a
            # `metric_salvaged` violation. The mildest rung had the harder effect, and it bought
            # Developer repair calls on the one stage no agent may edit.
            #
            # It also made the RECORD lie, which is the thing this module exists to stop. The bind
            # happens inside `run_command_eval` at the score stage; a `needs_failed` early-returns
            # BEFORE it, so `eval_dispatch`'s fallback stamped `absent_metric_subject()` —
            # `unbound_reason: "not_declared"` — on a metric whose subject WAS declared and was
            # missing. Those are exactly the two states `UNBOUND_REASONS`' own comment says a reader
            # must be able to tell apart because they need opposite fixes. `command_eval` now binds
            # the final stage's subject BEFORE its input contract runs, so under `require` the row
            # says `missing`/`empty`/`escapes`; this gate is what keeps `audit` from needing that
            # rescue at all.
            #
            # What `require` keeps: the latency. A subject that is not there cannot become bound, so
            # the node is unselectable under this rung either way, and refusing before the scorer
            # runs costs nothing an unbound metric was going to keep.
            #
            # `str` filter: on the `_grandfathered` reload path a recorded snapshot's `subject` was
            # never re-validated, and a non-string entry reaches `_confined` -> `Path(wd) / 123` ->
            # an uncaught TypeError out of the eval worker (no node terminal, re-dying on every
            # resume — the failure class `READER_PATH_KEYS` exists to prevent).
            #
            # A `subject_glob` DELIBERATELY DERIVES NO `needs`, and the omission is the design rather
            # than a gap left for later. `verify_stage_inputs` takes literal paths and stats them; a
            # pattern has no literal until something walks the workdir, so wiring it here would mean
            # resolving it a SECOND time, at a second instant (before the score stage rather than at
            # the bind), against a workdir a stage may still be writing. Two resolutions of one
            # declaration can disagree — one binding `final/model.safetensors` while the other
            # refused the stage for `ambiguous`, or worse, passing a contract about a path the record
            # then does not name. What is given up is exactly what this whole entry says `needs`
            # buys, which is LATENCY: a pattern that resolves to nothing is `missing` at bind time
            # and the node is unselectable under `require` either way, one scorer run later.
            if host:
                # The candidate's `cmd` keeps running, as the self-report stage — when the task has
                # one; a host scorer alone appends nothing empty — and the host scorer is the score
                # stage and takes the `needs` derivation.
                final["name"] = "self_score"
                candidate = [final] if final.get("command") else []
                return _expand(preceding) + candidate + [self._with_final_needs(es, host)]
            return _expand(preceding) + [self._with_final_needs(es, final)]
        if host:
            return self._candidate_then_host(es, params, score_cmd, score_timeout, host)
        return None

    def _host_scorer_stage(self, es, params):
        """The engine-built host scorer stage for this eval, or None when the task declares none.

        Stamped with `command_eval.HOST_STAGE_KEY`, which no declarer may write (`validate_stages`
        refuses it as an unknown key), so only a stage built HERE can be a host stage. `%params%`
        expands as for every stage; `%subject%` is left for the runtime, which expands it at the
        score stage's start from the subject it has just bound."""
        from looplab.runtime import command_eval
        hs = es.get("host_scorer") if isinstance(es, dict) else None
        if not isinstance(hs, dict) or not hs.get("command"):
            return None
        stage = {"name": "score",
                 "command": command_eval.expand_params(list(hs["command"]), params),
                 "timeout": float(hs.get("timeout") or 1800.0),
                 command_eval.HOST_STAGE_KEY: True}
        if isinstance(hs.get("env"), dict) and hs["env"]:
            stage["env"] = dict(hs["env"])
        return stage

    def _candidate_then_host(self, es, params, score_cmd, score_timeout, host):
        """The pipeline for a task with a host scorer and no usable preceding stages: the
        candidate's own `command` (when it has one) as `self_score`, then the host's `score`."""
        from looplab.runtime import command_eval
        cmd = (list(score_cmd) if score_cmd is not None
               else command_eval.expand_params(list(es.get("command") or []), params))
        chain = []
        if cmd:
            chain.append({"name": "self_score", "command": cmd,
                          "timeout": score_timeout if score_timeout is not None
                          else es.get("timeout", 600.0)})
        return chain + [self._with_final_needs(es, host)]

    def _with_final_needs(self, es, final):
        """The `needs` derivation for the FINAL stage under `metric_subject="require"` — see the
        block above the first call site for the rule and its bounds. One helper because there are
        now two candidates for "the final stage" (the candidate's `cmd`, or the host scorer)."""
        if str(getattr(self, "metric_subject", "audit") or "audit") == "require":
            _subject = ((es.get("metric") or {}).get("subject")
                        if isinstance(es.get("metric"), dict) else None)
            _needs = [s for s in (_subject or []) if isinstance(s, str) and s.strip()]
            if _needs:
                final["needs"] = _needs
        return final

    # The refusal `_run_eval` reports when the resolved chain invokes a PROTECTED script the workdir
    # does not contain. Hoisted so the two audiences it has to serve are visible in one place and can
    # be pinned: the OPERATOR, who is the only one who can fix it, and the inline-repair loop, which
    # would otherwise spend every attempt asking the Developer to write a file the write gate refuses.
    # `{stage}`/`{script}` are the only placeholders.
    PROTECTED_SCRIPT_MISSING = (
        "stage {stage!r} cannot run: {script} is PROTECTED (the operator owns it) but was never "
        "materialized into this node's workdir, so the command has no file to execute. This is NOT a "
        "repairable code error and the agent must not be asked to fix it — a protected path is exactly "
        "what the write gate refuses, so no edit can create it. Fix the TASK instead: make the file "
        "part of what each node is seeded with (commit it to the editable repo, or set seed_mode "
        "\"all\"), or drop it from `protect` so the Developer may author it.")

    def _unrunnable_protected_scripts(self, chain, workdir, cwd) -> list:
        """`[(stage, script)]` for every PROTECTED script the resolved chain invokes that is not in the
        eval cwd — the one eval failure NOTHING downstream can repair, detected BEFORE the first stage
        runs instead of after a node has paid for a full train.

        The live instance: `protect: ["looplab_eval.py"]` + a `score` stage running
        `python looplab_eval.py`, on a repo where that file was never committed — so `seed_mode="auto"`
        (git-tracked files only) left the workdir without it. `seed_protected_files` closes that hole,
        and this stays as the BACKSTOP for the cases seeding cannot close: a `protect` entry that
        matches nothing in the source (an operator typo — "one eval command but no file"), a glob that
        stopped matching, a protected path outside the repo. What makes it worth its own check rather
        than an ENOENT is the asymmetry: for any OTHER missing script the Developer can write the file,
        and the repair loop is the right answer; for a protected one it is structurally the wrong one.

        Deliberately narrow. Only a workdir-relative `.py` token is considered a script (the same rule
        `_stage_reachable_files` uses), and only one whose workspace-relative name is in
        `protected_names` — so a missing EDITABLE entrypoint keeps its existing behaviour (the eval
        runs, fails, and the Developer repairs it), and a stage that legitimately GENERATES a later
        stage's script cannot be pre-empted, because a generated script is never protected."""
        prot = set((self._repo_spec or {}).get("protected_names") or [])
        if not prot or not chain:
            return []
        wd, cd = Path(workdir).resolve(), Path(cwd).resolve()
        try:
            # The protected names are WORKSPACE-relative; the scripts are named relative to the eval
            # `cwd`. Namespacing by the cwd's own offset is what `RepoTask._eval_protected` does on the
            # other side of the same join. An eval cwd outside the workdir (an external tool dir the
            # `sandbox_cwd` remap trusts as given) shares no namespace with `protect` — check nothing.
            pre = cd.relative_to(wd).as_posix()
        except ValueError:
            return []
        pre = "" if pre in (".", "") else pre + "/"
        out: list = []
        for name, argv in chain:
            for tok in (argv or []):
                if not isinstance(tok, str) or not tok.endswith(".py") or tok.startswith("-"):
                    continue
                rel = tok
                while rel.startswith("./"):
                    rel = rel[2:]
                if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
                    continue                      # an absolute/escaping path is not a protected name
                key = (pre + rel).replace("\\", "/")
                row = (str(name), key)
                if key in prot and not (cd / rel).exists() and row not in out:
                    out.append(row)
        return out

    def _eval_pipeline(self, node, workdir, profile=None):
        """WHAT AN EVAL WILL RUN: `(command, timeout, stages)` for this node at this profile — the
        ONE derivation, called by both the side that RUNS it and the sides that PLAN against it.

        Two readers, straddling the same line `runtime/command_eval.py::eval_spec_time_budget`
        straddles (its docstring says so for the same reason):
          • the DISPATCHER, `eval_dispatch.py::_run_eval`, which hands `stages`/`cmd`/`timeout`
            straight to `run_command_eval`;
          • the PLANNERS, through `_resolved_stages` below — the inline-repair reuse predicate, the
            watchdogs' log plan (`train_monitor.eval_log_plan`) and the salvage re-check — all of
            which need the chain BEFORE or independently of a run, which is why `RunResult.stages`
            (the post-run per-stage OUTCOME record) cannot serve them.

        It exists because there were TWO copies of this and they had already drifted: the planner
        re-implemented the dispatcher's prologue WITHOUT its `profile` argument, so a planner asked
        during a `confirm`/full pass would have planned the smoke chain. Latent when it was found
        (2026-08-14 — the confirm phase is the only caller that passes a profile and it never plans,
        and the only profile-sensitive part of the chain is the appended `score` stage's overrides +
        timeout, which no planner reads), and fixed as ONE function anyway: a derivation kept in
        step by hand comes apart at the next reader, and the next reader is the one who pays.

        WHAT THE DISPATCHER KNOWS THAT A PLANNER CANNOT is exactly one thing, and it is an ARGUMENT
        rather than private state: the caller-supplied `profile` (`confirm_phase.py` passes
        `"full"`). So it is a parameter here and each side passes what it has. What deliberately
        stays OUT is the dispatcher's two SIDE EFFECTS — `_ensure_run_setup` and `_sync_node_deps` —
        because a question about what WOULD run must never install anything."""
        from looplab.runtime import command_eval
        es = self._eval_spec
        prof = profile or (node.idea.eval_profile if node is not None else None)
        # A7 Strategist fidelity override: when the active strategy pins smoke/full and the node
        # didn't request a profile, use the strategy's. An explicit `profile` arg (confirm=full)
        # always wins. "adaptive" leaves _strategy_fidelity None => the Idea's own profile.
        if prof is None and self._strategy_fidelity in ("smoke", "full"):
            prof = self._strategy_fidelity
        params = node.idea.params if node is not None else {}
        cmd, timeout = command_eval.build_command(es, params, prof)
        stages = self._resolve_stages(str(Path(workdir).resolve()), es, params,  # cmd-authoritative pipeline (+ %params% per stage)
                                      score_cmd=cmd, score_timeout=timeout)      # profile/timeout survive pipeline mode
        return cmd, timeout, stages

    def _resolved_stages(self, node, workdir, profile=None) -> list:
        """Re-resolve the eval pipeline the way `_run_eval` does — used by the inline-repair reuse
        predicate to inspect the stages' COMMANDS (which script each runs). [] for a single-command
        eval (no reuse question). Deterministic given (node, workdir, eval_spec, profile).

        The PLANNER half of `_eval_pipeline`: the same derivation the dispatcher runs, minus its
        side effects, plus the two things only a planner wants — the chain alone, and TOTALITY (a
        resolution hiccup is "no reuse", never a crashed repair loop). `profile` must be the one the
        eval will be DISPATCHED with; the default `None` is what `evaluate.py::_evaluate` passes to
        `_run_eval`, and therefore the right answer for its three planning call sites."""
        if not self._eval_spec:
            return []
        try:
            return self._eval_pipeline(node, workdir, profile)[2] or []
        except Exception:  # noqa: BLE001 — a resolution hiccup must not crash the repair loop; no reuse
            return []

    @staticmethod
    def _imported_modules(kw: str, rest: str) -> list:
        """The module names an `import`/`from … import …` clause references. `kw` is the keyword and
        `rest` the text after it. Handles `import a, b as c` (a, b), `from pkg import x, y` (pkg AND the
        possible submodules pkg.x / pkg.y), and relative imports (`from .sub import x` → .sub, .sub.x)."""
        rest = rest.split("#", 1)[0].strip()
        mods: list = []
        if kw == "import":
            for part in rest.split(","):
                name = part.strip().split(" as ")[0].strip()
                if re.match(r"^\.*[\w.]+$", name):
                    mods.append(name)
            return mods
        m = re.match(r"^(\.*[\w.]*)\s+import\s+(.+)$", rest)      # from X import a, b, (c)
        if not m:
            return mods
        base = m.group(1)
        if base:
            mods.append(base)
        names = m.group(2).replace("(", " ").replace(")", " ")
        for nm in names.split(","):
            nm = nm.strip().split(" as ")[0].strip()
            if nm and nm != "*" and re.match(r"^\w+$", nm) and base:
                mods.append(base + nm if base.endswith(".") else f"{base}.{nm}")
        return mods

    # Interpreter basenames whose `-m` means "run this dotted MODULE". Restricted rather than left as
    # "any argv0", because `-m` is a flag other programs also spell (`make -m`, `sh -m`) and the
    # module-entry rule below must never key on a token that is not a module name. A wrapper the list
    # does not name (`bash -c "… python -m pkg.mod"` — the shape 10 of the 39 corpus rows have) keeps
    # the historical OPAQUE answer: the module travels inside ONE shell-string token, and parsing a
    # shell is not something this predicate may guess at.
    _PY_INTERPRETER_RE = re.compile(r"^(python|pypy)[\d.]*$", re.I)

    @staticmethod
    def _module_entry_candidates(cmd: list) -> list:
        """The dotted modules a stage command runs as its ENTRY POINT via `python -m <module>`.

        `-m <module>` IS an import — CPython puts the process CWD first on `sys.path` for it, which is
        the eval workdir — so the module's file is reachable by exactly the rule
        `_module_file_candidates` already states for an `import`. Only the SEPARATE two-token spelling
        is read; a glued `-mpkg.mod` is left alone and the stage stays opaque, because a bound that has
        to guess where a flag ends is not a bound.

        ONLY THE INTERPRETER'S OWN `-m` COUNTS, and only the first of them. `-m` is an ordinary short
        flag that PROGRAMS use for their own arguments, and scanning the whole argv read
        `["python", "train.py", "-m", "models.resnet"]` — a `--model` the training script consumes —
        as an entry point, crediting `models/resnet.py` to the closure whenever that file happened to
        exist. That is harmless in one of the closure's two readers and not in the other:
        `_safe_reuse_start` only ever REFUSES reuse on a wider closure, but `_stage_rollback` rung 4
        uses a NON-EMPTY intersection as the thing that PERMITS a full-pipeline rollback, so a repair
        touching an unrelated `models/resnet.py` could authorize re-running a stage that never
        imports it. CPython stops reading its own options at the first non-option token, so this does
        too: a token that is not a flag is the program, and every `-m` after it is the program's.

        A flag that takes a SEPARATE argument (`-c code`, `-X opt`, `-W spec`) ends the scan at that
        argument rather than skipping it, which is the same fail-closed answer this already gives a
        glued `-mpkg.mod`: no entry point is credited, the stage exposes no local script, and
        `_stage_reachable_files` returns None — opaque. Guessing which flags consume a value is
        exactly the "bound that has to guess where a flag ends" the paragraph above refuses to be."""
        out: list = []
        if not cmd or not isinstance(cmd[0], str):
            return out
        argv0 = str(cmd[0]).replace("\\", "/").rsplit("/", 1)[-1]
        if not EvalStagesMixin._PY_INTERPRETER_RE.match(argv0):
            return out
        for i, tok in enumerate(cmd[1:], start=1):
            if not isinstance(tok, str) or not tok.startswith("-"):
                break                    # the program itself — the interpreter is done reading options
            if tok != "-m":
                continue                 # another interpreter flag (`-u`, `-O`, `-W`, …)
            if i + 1 < len(cmd) and isinstance(cmd[i + 1], str):
                mod = cmd[i + 1].strip()
                if re.match(r"^[\w.]+$", mod) and not mod.startswith("."):
                    out.append(mod)
            break                        # `-m` ends option processing in CPython too
        return out

    @staticmethod
    def _module_file_candidates(mod: str, script_rel: str) -> list:
        """Repo-relative file paths a dotted import `mod` could resolve to, tried against BOTH the
        workdir root AND the importing script's OWN directory (Python puts the script dir on sys.path, so
        a subdir script's sibling import resolves there). A leading-dot relative import anchors at the
        script's package dir only. Emits the module file AND the package `__init__.py` at every depth
        (importing a.b.c also runs a/__init__.py and a/b/__init__.py) — an over-approximation that only
        ever ADDS reachable files, keeping the reuse predicate conservative."""
        script_dir = script_rel.rsplit("/", 1)[0] if "/" in script_rel else ""
        rel = mod
        if mod.startswith("."):                                  # relative: strip dots, anchor at script dir
            rel = mod.lstrip(".")
            bases = [script_dir]
        else:
            bases = ["", script_dir] if script_dir else [""]
        parts = [p for p in rel.split(".") if p]
        out: list = []
        for base in dict.fromkeys(bases):                        # de-dup, preserve order
            cur = base
            for part in parts:
                cur = f"{cur}/{part}" if cur else part
                out.append(f"{cur}.py")
                out.append(f"{cur}/__init__.py")
        return out

    @staticmethod
    def _stage_reachable_files(stages: list, workdir):
        """Repo-relative files the earlier stages' runs REACH — each command's local `.py` script plus
        the TRANSITIVE closure of its workdir-local imports (module files + package `__init__`s, resolved
        against the workdir root AND each script's own directory). Used to decide whether a repair's edits
        could have changed what an earlier (to-be-reused) stage produced.

        Returns None (OPAQUE → treat as UNSAFE, refuse reuse) when a stage runs something we can't
        statically bound: a command exposing no local entry point (a shell wrapper, a bare binary, or a
        `python -m <module>` that resolves to NO file under the workdir — i.e. installed code) or a
        `.py` path outside the workdir. Fail-closed by construction — a spuriously
        'reachable' file only forces a re-train, but a MISSED dependency would silently score a stale
        checkpoint (the invariant `_safe_reuse_start` exists to protect).

        Interim heuristic, superseded long-term by the per-stage ARTIFACT DECLARATION design
        (docs/BACKLOG.md §6 'Deferred design work') — prefer extending that design over adding cases here."""
        wd = Path(workdir)
        imp_re = re.compile(r"^[ \t]*(from|import)[ \t]+(.+?)[ \t]*$", re.M)
        # Parenthesized MULTI-LINE imports (`from pkg import (\n  a,\n  b,\n)`) span lines, so the
        # line-anchored pattern above only sees `from pkg import (` — it credits pkg/__init__.py but
        # MISSES the pkg/a.py / pkg/b.py submodule files, letting a repair to those escape the
        # closure. This companion pattern captures the whole group (a char class matches newlines
        # without re.S); `#` comments are stripped from the whole source BEFORE either scan runs
        # (see below), so a ')' inside a trailing comment can't end the group early.
        paren_re = re.compile(r"^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]*\(([^)]*)\)", re.M)
        out: set = set()
        pending: list = []
        for s in (stages or []):
            cmd = s.get("command") or []
            scripts: list = []
            for tok in cmd:
                if not isinstance(tok, str) or not tok.endswith(".py"):
                    continue
                rel = tok
                while rel.startswith("./"):
                    rel = rel[2:]
                if os.path.isabs(rel):                           # only trackable if it lives under the workdir
                    try:
                        rel = str(Path(rel).resolve().relative_to(wd.resolve()))
                    except Exception:  # noqa: BLE001 — an out-of-tree script we can't bound → opaque
                        return None
                scripts.append(rel)
            # `python -m pkg.mod` NAMES ITS ENTRY POINT BY IMPORT SYNTAX, and until 2026-08-14 that
            # made the whole stage opaque — measured, that is the clause that actually binds on this
            # box: of the 39 corpus repairs whose pipeline has no `.py` argv token, 29 run every such
            # stage as `python -m <module>` resolving to a file inside the node's own workdir, and
            # every `rubertlite-dr-unified-{v2,v6,v7,v8}` pipeline is that shape. The module resolves
            # by the SAME rule as an import (CPython puts the process CWD — the eval workdir — first on
            # `sys.path` for `-m`), so this is not a new modelling assumption, it is the existing one
            # applied to the entry point instead of only to what the entry point imports.
            #
            # IT MUST RESOLVE TO A FILE THAT EXISTS UNDER THE WORKDIR, or the stage stays OPAQUE. That
            # is the fail-closed half and it is the whole safety argument: `python -m pip`,
            # `python -m pytest`, `python -m torch.distributed.run` name INSTALLED code this predicate
            # cannot see, and admitting them would let a repair to a workdir `.py` that the installed
            # tool loads dynamically read as "disjoint from the closure". Unresolvable → refuse, exactly
            # as before.
            for mod in EvalStagesMixin._module_entry_candidates(cmd):
                # `<pkg>/__main__.py` is credited BESIDE the import candidates and is not optional:
                # `python -m <pkg>` runs the package's `__main__.py`, which `_module_file_candidates`
                # never emits (it answers "what does `import <pkg>` load", i.e. `__init__.py`). Seeding
                # only the import candidates on a PACKAGE entry point credits an `__init__.py` that is
                # routinely empty and MISSES the file that actually trains — the exact shape of a
                # missed dependency, which is the failure direction that reuses a stale checkpoint.
                cands = list(EvalStagesMixin._module_file_candidates(mod, ""))
                cands.append("/".join(p for p in mod.split(".") if p) + "/__main__.py")
                for cand in cands:
                    if (wd / cand).exists():
                        scripts.append(cand)
            if cmd and not scripts:                    # runs SOMETHING, exposes no local entry → opaque
                return None
            pending.extend(scripts)
        while pending:
            rel = pending.pop()
            if rel in out:
                continue
            out.add(rel)
            p = wd / rel
            if not p.exists():
                continue
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            # Strip `#` comments BEFORE both import scans: the paren pattern's `[^)]*` group stops
            # at the FIRST ')', so a ')' inside a trailing comment (`vit,  # backbone (legacy)`)
            # would end the group early and silently drop every name after it from the closure.
            # The blunt regex also eats '#' inside string literals — that can only ADD spurious
            # 'reachable' candidates, which is fail-closed-safe here (a false positive merely
            # forces a re-train; it's a MISSED dependency that would score a stale checkpoint).
            src = re.sub(r"#[^\n]*", "", src)
            def _credit(mods):
                for mod in mods:
                    for cand in EvalStagesMixin._module_file_candidates(mod, rel):
                        if cand not in out and (wd / cand).exists():
                            pending.append(cand)
            for m in imp_re.finditer(src):
                _credit(EvalStagesMixin._imported_modules(m.group(1), m.group(2)))
            for m in paren_re.finditer(src):
                inner = " ".join(m.group(2).splitlines())    # comments already gone (see above)
                _credit(EvalStagesMixin._imported_modules("from", f"{m.group(1)} import ({inner})"))
        return out

    def _stage_key_fn(self, workdir, cwd=None):
        """The `stage_key_fn` `runtime/command_eval.py::_run_stages` calls once per stage, or None.

        THE INJECTION EXISTS FOR LAYERING, not for configurability: the key needs
        `_stage_reachable_files`, which lives here in `looplab/engine/`, and `runtime` imports nothing
        above `core`. This is the same shape `metric_subject.bind_one(confine=…)` uses one file over —
        the rule stays where the layer that owns it is, and the runtime is handed a callable.

        `scope` binds every key this run mints to this run's own materialization. It is the run
        DIRECTORY plus `setup_config_hash(task)` — the same digest `run_started.config_hash` carries
        — because the key covers only what is INSIDE the workdir and a run with a different task
        payload (a different seed, a different mount table, a different eval command) materializes a
        different workdir from the same authored files. Two runs must never share a key; a resume of
        the SAME run must, which is why it is the config hash and not a per-process nonce.

        It RECORDS and decides nothing. `stage_identity.py`'s docstring holds the measurement that
        decided a cross-node cache would not be built (7 hits / 4 wrong on the declared-params key
        the request proposed; 1 hit / 0 wrong and 0.64 h on this one), and `reuse_refusal` is the
        decision half a cache would call, driven today only by `looplab stage-dups`.
        """
        from looplab.core.setup_identity import setup_config_hash
        from looplab.runtime.stage_identity import stage_input_key
        try:
            scope = f"{Path(getattr(self, 'run_dir', '') or '')}|" + setup_config_hash(
                self.task.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 — an instrument may never take down an eval
            return None

        # ONE DIGEST MEMO PER CLOSURE, i.e. per eval attempt: `_run_stages` calls `_key` once per
        # stage, unconditionally, before that stage's command runs, and each call used to sha256 the
        # whole keyable tree again (3.0-4.4 s per stage warm on a 1 GB workdir, §0.12's own numbers)
        # even though a `train` stage moves a handful of files and leaves the rest of the workdir
        # exactly as `mine` left it. The memo is `{rel -> (file_identity, digest)}` and it is owned
        # HERE rather than inside `workdir_content` for two reasons: its lifetime is the attempt (a
        # repair re-enters `run_command_eval`, gets a fresh closure and re-digests from scratch,
        # which is what a workdir the engine has since rewritten deserves), and a module-level cache
        # would be shared by every run in the process with nothing to bound it. Each stage is still
        # keyed at its OWN instant — an unchanged file is re-stat'd on every stage and only its hash
        # is remembered, so the key still moves the moment the bytes do.
        digests: dict = {}

        def _key(stages, index):
            reachable = EvalStagesMixin._stage_reachable_files(stages[:index + 1], workdir)
            return stage_input_key(stages, index, workdir, scope=scope, reachable=reachable,
                                   cwd=cwd, digests=digests)
        return _key

    def _stage_progress_fn(self, node_id, generation, stages=None):
        """The `on_stage_event` callback `runtime/command_eval.py` calls at each stage boundary.

        Turns the runtime's layer-free cursor into a registered `phase_progress` beacon, so the
        status surfaces can finally name the step a node is on. Injected for the SAME layering
        reason `_stage_key_fn` is: `runtime` may not import `looplab.events`, so the event vocabulary
        and its `assert_progress_phase` check have to live on this side of the line.

        WHAT IT ADDS TO THE RUNTIME'S PAYLOAD, and why each half is here rather than there:

        * `node_id`/`generation` — the runtime knows neither. Without the generation a reset's
          abandoned lifecycle and its replacement publish the same key, which is the exact confusion
          `nodeActivity.js::markerFor` already refuses for build markers.
        * `role` — the stage's resolved `LOG_ROLES` member from `eval_log_plan`. THE NAME IS NOT
          EVIDENCE and this is the whole reason the role rides along: `train` is a slug the agent
          chose, so a surface claiming "training" from the name alone is guessing. The role comes
          from the manifest declaration the engine already trusts to say what runs and in what
          order, and `eval_log_plan` is a pure derivation over the resolved pipeline.

        Both the plan and the closure fail SOFT: an instrument may never take down an eval, so a
        pipeline whose plan cannot be built still reports the stage NAME and simply carries the
        `unknown` role — strictly more than the nothing that came before.
        """
        from looplab.engine.train_monitor import eval_log_plan
        from looplab.events.types import LOG_ROLE_UNKNOWN, PROGRESS_STAGE_EVAL

        try:
            _plan = eval_log_plan(stages or []).roles or {}
            roles = {stage: role for stage, role in _plan.values()}
            # THE SINGLE-COMMAND PATH keys its one role under `None` (there is no stage name to key
            # it by), and the runtime reports that step as `eval`. Bridge the two HERE rather than
            # letting the lookup miss and degrade to `unknown`: that command IS the training on this
            # path — `eval_log_plan` grants it `LOG_ROLE_TRAINING` for exactly that reason — so a
            # miss would hide the one case where the role is unambiguous. Guarded on `not stages`
            # because a MULTI-stage plan also carries a `None` key, and there it means `setup.log`.
            if not stages:
                roles = {"eval": roles.get(None, LOG_ROLE_UNKNOWN)}
        except Exception:  # noqa: BLE001 — an instrument may never take down an eval
            roles = {}

        def _emit(payload):
            name = str(payload.get("name") or "")
            self._emit_progress(
                PROGRESS_STAGE_EVAL, "stage", str(payload.get("status") or ""),
                {"node_id": node_id, "generation": generation, "name": name[:64],
                 "index": payload.get("index"), "total": payload.get("total"),
                 "role": roles.get(name, LOG_ROLE_UNKNOWN)})
        return _emit

    def _safe_reuse_start(self, stages: list, failed_stage, changed_files, workdir,
                          deleted=None, cwd=None, prev_manifest=None, params=None):
        """The stage to RESTART from so a repaired node reuses the completed EARLIER stages (e.g. skip
        re-`train` when only the `score` script was fixed) — or None to re-run the FULL pipeline (the
        safe default that preserves the 'each node trains a FRESH model' invariant).

        SAFE-OR-RETRAIN: reuse the earlier stages' artifacts ONLY when the repair's changed files are
        DISJOINT from those stages' reachable files (their scripts + transitive imports). If a change
        could have altered what an earlier stage produces (train.py / loss.py / model.py it imports, or
        the stage MANIFEST that carries the argv), we must re-train — reusing a stale checkpoint would
        score a model that doesn't reflect the current code. Beyond the disjointness test, reuse is
        REFUSED outright whenever the predicate cannot PROVE the earlier stages' inputs are unchanged:
          • an OPAQUE earlier stage (no resolvable local entry point: a shell wrapper, a bare binary,
            or a `python -m` naming installed code — see `_stage_reachable_files`);
          • the repair DELETED any file (`deleted`) — the closure can't see vanished modules;
          • any changed file is not a `.py` (config/data inputs are invisible to import reachability);
          • the stage MANIFEST changed anywhere AT OR BEFORE the reuse point — the entries that
            already ran, their order, their names, their `expect`/`needs`/`env`/`timeout`. Narrowed
            2026-08-18 from "the manifest file changed at all" to that per-ENTRY compare; the rule,
            the argument for why "strictly after" is sufficient, and the reason the previous
            manifest must be read off the FOLD rather than off disk are in
            `manifest_prefix_unchanged`, and the measurement is at the clause below.
          • the eval runs under a non-default `cwd` (changed-file keys and stage-script paths resolve
            against different bases, so the intersection proves nothing).
        A false negative just re-trains (no worse than a full re-run); a false positive is a silent
        stale score, so the predicate is conservative by construction and fail-closed on anything it
        can't statically bound.

        WHY THE NON-`.py` CLAUSE IS STILL HERE, having been examined against `needs` on 2026-08-14 and
        REFUSED. The tempting widening is: a stage declares its inputs (`needs`), so a changed config
        that is not among an earlier stage's `needs` provably cannot have altered what that stage
        produced. It does not hold, for two independent reasons, and either one alone is fatal:

          • `needs` IS NOT A BOUND, IT IS A PRECONDITION. `verify_stage_inputs` `stat()`s each declared
            path BEFORE the command and reports the first one missing or empty; nothing anywhere checks
            the converse. A stage may read any file it likes without declaring it —
            `_confined_input`'s own docstring says so ("What a stage may actually READ is owned by
            `runtime/read_fence.py`"), and that fence never fences the workdir, which is where a
            candidate's config lives. So "not in `needs`" means "undeclared", never "unread", and
            reusing on it is precisely the silent stale score this predicate exists to prevent.
          • IT IS ALSO OPTIONAL AND ALL BUT UNUSED. Measured over every `runs/*/nodes/*/
            looplab_stages.json` on this box: 2 of 129 stages declare `needs` at all (both on the live
            v8 run, both added the day the field shipped), against 20 declaring `expect`. An absent
            declaration must read as UNKNOWN, so the widening would decide almost every real case by
            its default — and the fail-open default is the one that scores a stale checkpoint.

        The measurement that shaped what WAS widened instead: replaying every `node_repaired` row in
        `runs/` that carries a change-set column (75 of them), only 8 change a non-`.py`, non-manifest
        file — all 8 are one `config.yaml` — and every one of those 8 sits on a pipeline that was
        ALREADY refused for opacity. So the non-`.py` clause was never the binding one; `python -m` was
        (39 opaque rows, 29 of them locally resolvable), and that is the clause this predicate now
        bounds instead. Widening the non-`.py` clause would have bought zero of the corpus's repairs
        and cost the invariant.

        AND THE CONTENT KEY THAT LOOKS LIKE IT SHOULD LOOSEN THIS DOES NOT (2026-08-17).
        `runtime/stage_identity.py` derives a key over the CONTENT of the whole workdir, which needs
        neither of the two clauses above — a deleted file is absent from a digest map and a config is
        in it, so both are decided by the key differing. That key is a RECORDER and this predicate is
        unchanged, for two measured reasons. (1) It buys nothing HERE: every corpus repair that had a
        completed stage to forfeit changed `config.yaml`, whose content is in the key, so a
        content-keyed version of this decision refuses in exactly the same places. (2) The key's own
        cross-node yield is 1 reuse over the whole corpus against 4 WRONG hits for the declared-params
        key the same request proposed, which is why no cache was built at all — see
        `docs/BACKLOG.md` §0.12.

        WHAT WOULD MAKE IT SAFE is an ENFORCEMENT rung, not a better reading of the declaration: run
        each stage under a kernel read allow-list scoped to its OWN declared inputs (the pieces exist —
        `runtime/read_allowlist.py` derives the set, `runtime/landlock.py` enforces one, today
        workdir-wide and off by default). Then "a stage reads only what it declares" is true by
        construction rather than by trust, and `needs` becomes the bound this clause could stand on.
        That is `runtime/dev_probe.py`'s move — make the surface be the thing the fence can cover —
        and it is the rung this file is waiting on.

        Interim heuristic, superseded long-term by the per-stage ARTIFACT DECLARATION design
        (docs/BACKLOG.md §6 'Deferred design work') — prefer extending that design over adding cases here."""
        if not stages or not failed_stage:
            return None
        names = [str(s.get("name")) for s in stages]
        if failed_stage not in names:
            return None
        fi = names.index(failed_stage)
        if fi == 0:                                   # nothing before it to reuse
            return None
        # DELETIONS are un-boundable: _write_node_files unlinks them BEFORE this predicate runs, so
        # the reachability closure (walked over files still on disk) can never rediscover an import
        # of the vanished module — `changed ∩ reachable` would be trivially disjoint even when an
        # earlier stage trained THROUGH the deleted file. Any deletion forces a full re-run.
        if deleted:
            return None
        # A non-default eval `cwd` re-bases the stage scripts: the reachable set resolves script
        # paths against the WORKDIR root, but the repair's changed-file keys are workdir-relative —
        # under a subdir cwd the same file appears as `sub/train.py` on one side and `train.py` on
        # the other, so disjointness proves nothing. Fail closed rather than guess the remapping.
        if cwd not in (None, "", "."):
            return None
        changed = {(f[2:] if isinstance(f, str) and f.startswith("./") else f) for f in (changed_files or [])}
        # A change to the stage MANIFEST rewrites the pipeline's argv (e.g. train hyperparams), so the
        # completed checkpoint no longer matches the declared command — never reuse across it, UNLESS
        # the edit is confined STRICTLY AFTER the reuse point (`manifest_prefix_unchanged`, whose
        # docstring holds the rule and the argument for why "after" is sufficient). The narrowing is
        # NOT a loosening of what the clause protects: it is the same compare done per ENTRY instead
        # of per FILE. The old test asked "did the manifest file's bytes move?" and a repair that
        # retunes `train`'s argv answers yes, which forfeits `mine` — a stage whose own entry, whose
        # `expect`, whose `needs` and whose position the same manifest proves untouched.
        #
        # MEASURED by replaying this very function over every `node_repaired` row in `runs/` (85
        # rows, 6 runs): 21 had a completed earlier stage to forfeit and 7 of those changed the
        # manifest. SIX are confined after the reuse point and ONE is not — `rubertlite-dr-unified
        # -v8` node 0 attempt 4, whose `mine` entry genuinely changed, and which this clause still
        # refuses. Of the six, FOUR also change `config.yaml` and are still refused by the non-`.py`
        # clause below, which is deliberately NOT touched (see the `needs` refusal above, and
        # `engine/champion_caveats.py`). So the narrowing newly admits exactly TWO rows, across two
        # runs, worth 5,966 s of re-run stage time — `e5small-dr-unified-v2` node 0 attempt 1
        # (3,667 s) and `rubertlite-dr-unified-v8` node 9 attempt 2 (2,300 s). It is a SMALL
        # population and that is stated rather than rounded up; the case for it is as much the
        # INCENTIVE it removes, and v8 node 9 is both halves of that in two consecutive repairs.
        # Attempt 1 changed `train.py` alone, and its committed bytes say why in the agent's own
        # words — "train.py-only change (no config.yaml edit) that leaves the completed `mine` stage
        # reusable", "overrides the CLI --n_epochs 10", `config.train.training.n_epochs = 6` — which
        # is `engine/champion_caveats.py`'s `params_overridden` mechanism exactly, the running code
        # at a coordinate no declaration carries. Attempt 2 deleted that override and edited the
        # manifest's `train` argv instead, and paid 2,300 s of re-`mine` for being honest. The old
        # clause charged an agent for editing the DECLARATION and nothing for hiding the same change
        # in a `.py`, and the corpus holds it taking both options one attempt apart. The earlier
        # reading that "all 3 rows with a completed stage also changed the manifest" was right about
        # the FILE and wrong about the ENTRY.
        #
        # ZERO of them would have been WRONG, and that is the number that decides whether this ships.
        # `e5small-dr-unified-v2` node 0 mined THREE times and every run recorded the same
        # `stage_outputs` digest (`01627a8c…`, 218,707,487 bytes), so the artifact a recompute
        # produced is the artifact the reuse would have kept. v8 predates `stage_outputs`, so its row
        # is argued from the preserved workdirs instead: node 9's two `mine` runs are separated by a
        # change set of manifest + `train.py`, and `train.py` is in no part of `mine`'s import
        # closure — while nodes 3, 9 and 10 all hold the SAME `13db4477…` / 79,586,058-byte parquet
        # from miners whose closures and configs genuinely differed (docs/BACKLOG.md §0.12).
        #
        # A CONTENT KEY DOES NOT ALREADY DO THIS and that is worth knowing before reaching for one:
        # `stage_input_key` digests every non-`.py` workdir file, the manifest included, so all three
        # of node 0's `mine` runs carry DIFFERENT keys (`41ac8c21…`/`4aa0789c…`/`3739ed81…`) for
        # identical output. It refuses here for exactly the reason this clause did.
        #
        # ONLY THE PIPELINE'S OWN MANIFEST MAY BE ACQUITTED, and the exact-path test is the reason
        # this is not a one-line change to the predicate above. The old clause matched by BASENAME,
        # which was right when the answer was always "forfeit" — every `looplab_stages.json`
        # anywhere in the tree refused, and a nested one is at worst an over-refusal. Acquitting on
        # a basename would be the opposite mistake: `_resolve_stages` reads exactly
        # `<workdir>/looplab_stages.json`, so `prev_manifest` and `stages` say nothing whatever about
        # a `sub/looplab_stages.json`, and clearing that file out of the change set would walk a
        # non-`.py` the predicate never examined straight past the clause below. Anything else
        # wearing the name keeps the historical refusal, through that very clause.
        _manifest = {c for c in changed if str(c) == STAGE_MANIFEST_NAME}
        if _manifest:
            if not manifest_prefix_unchanged(prev_manifest, stages, failed_stage, params):
                return None
            # ACQUITTED — and therefore taken OUT of the change set, so the clauses below judge what
            # is left rather than re-refusing on the same file (the non-`.py` clause catches the
            # manifest too, being a `.json`; that is why the removal is not cosmetic).
            changed = changed - _manifest
        # Reachability only bounds PYTHON imports: a changed non-.py file (config.yaml, a params
        # file, a writable data copy the train stage reads) can alter what an earlier stage produced
        # without ever appearing in the import closure. Its effect can't be proven absent, so any
        # non-.py change forces a full re-run. It is also what still refuses the MANIFEST in every
        # case the clause above did not acquit — a nested one, or a root one whose prefix moved
        # (that branch returns early, so this is the belt beside the braces, not the only rung).
        if any(not str(c).endswith(".py") for c in changed):
            return None
        reachable = self._stage_reachable_files(stages[:fi], workdir)
        if reachable is None:                         # an OPAQUE earlier stage (python -m/shell) → re-train
            return None
        if changed & reachable:                       # a change could affect an earlier stage → re-train
            return None
        return failed_stage

    # The CONTRACT clause, appended to the sanity system prompt ONLY for a stage that DECLARED an
    # `expect.assert`. Hoisted out of the closure because it is the one place the quality-judgement
    # ban is deliberately narrowed, and a narrowing of a ban has to be readable on its own.
    #
    # Why this is not the thing the base prompt forbids. The ban exists because a checker with no
    # contract has nothing to judge against except its own taste, and taste produced a measured
    # incident: it failed the run's BEST model for having a loss around 14.6. A DECLARED condition is
    # not taste — it is the declarer's own sentence about what this stage was for, and checking a
    # stage against its own declaration is the opposite of ranking it against other stages' results.
    # The clause therefore SEPARATES the two rather than relaxing one: the declared condition is
    # judged, everything not covered by it keeps the ban verbatim.
    #
    # A stage's declared condition is the ONLY thing the checker may fail it for beyond the hard
    # failures — it may not invent a second condition it thinks the stage should also have met.
    STAGE_CONTRACT_CLAUSE = (
        " THIS STAGE ALSO DECLARED A SUCCESS CONDITION — the sentence under DECLARED CONDITION "
        "below, written by whoever declared the pipeline. Check the stage's output against THAT "
        "sentence and answer `FAIL declared_condition_violated: …` when the output shows the "
        "condition was not met (e.g. it "
        "declares coverage of at least 90% of the training queries and the log reports 9,364 of "
        "764,676). This is NOT the quality judgement forbidden above and does not relax it: you are "
        "not ranking this result against any other run, you are checking whether the stage did the "
        "job it declared. Judge ONLY the declared condition — do not invent a second one. If the "
        "output does not say either way, reply OK: an unstated number is not a violation.")

    # --- ROLLBACK: "this stage's failure is an EARLIER stage's fault" -----------------------------
    # The refusal vocabulary, hoisted so the ladder has a truth table and so the strings a Developer
    # reads back are pinnable. Each is a REASON THE ROLLBACK CANNOT HELP, not a reprimand: the text
    # goes into the next repair prompt, so it has to leave the model with something to do instead.
    # `{suspect}`/`{failed}`/`{names}`/`{script}` are the only placeholders.
    ROLLBACK_UNKNOWN = ("cannot roll back to stage {suspect!r}: this node's pipeline has no such "
                        "stage. Its stages, in order, are: {names}.")
    ROLLBACK_NOT_EARLIER = ("cannot roll back to stage {suspect!r}: it is not EARLIER than the stage "
                            "that failed ({failed!r}). Rolling back means re-running a stage that "
                            "ALREADY COMPLETED because you believe its output is what broke the later "
                            "one; re-running the failing stage itself is what the ordinary repair "
                            "already does.")
    ROLLBACK_REPEATED = ("cannot roll back to stage {suspect!r} again: this node has already re-run "
                         "it once on that same suspicion and the failure came back. Re-running an "
                         "expensive stage a second time on the same guess is not a new hypothesis — "
                         "either fix the failing stage {failed!r} directly, name a DIFFERENT earlier "
                         "stage, or say you no longer know what to change.")
    ROLLBACK_NO_CHANGE = ("cannot roll back to stage {suspect!r}: this repair changed nothing that "
                          "stage reads or runs, so re-running it would produce byte-identical output "
                          "and cost its full runtime for nothing. To roll back to a stage you must "
                          "also CHANGE it — edit the script it runs (or something that script "
                          "imports) in the SAME repair that names it.")
    ROLLBACK_NO_PIPELINE = ("cannot roll back to stage {suspect!r}: this node has no declared stage "
                            "pipeline — the operator's command runs as ONE step, so there is no "
                            "earlier stage to re-run. Fix the code that command runs.")
    ROLLBACK_PROTECTED = ("cannot roll back to stage {suspect!r}: it runs {script}, which is "
                          "PROTECTED — the operator owns that file and no edit you make can change "
                          "it, so re-running the stage would produce exactly what it produced before. "
                          "This is NOT something you can fix. If that stage's output really is wrong, "
                          "the operator has to change {script} or unprotect it; meanwhile fix what you "
                          "CAN reach, or report that you cannot.")

    def _rollback_start(self, stages: list, failed_stage, suspect, changed_files, workdir,
                        already_rolled_back=(), cwd=None):
        """Should this repair RE-RUN an earlier, already-successful stage — and if not, why not?

        Returns ``(start_stage, refusal)``: exactly one is non-None. `start_stage` is the suspect's
        name, to be used as the next eval's `start_stage`, which re-runs the pipeline FROM there and
        so discards every artifact the suspect and everything after it produced.

        THE GAP THIS FILLS. `next_start` and `_safe_reuse_start` only ever move the start point
        FORWARD: they answer "which completed stages may I skip". Neither can express the opposite
        claim, which is the one a train failure most often warrants — that the stage which BROKE is
        not the stage that is WRONG. On `runs/rubert-dr-0807` the `mine` stage exited 0 with 1.2%
        coverage and `train` was handed the result; every repair the loop could make was a repair to
        `train`, because naming `mine` was not something the Developer's action space could say.

        THE LADDER, and each rung is a way a rollback would be pure cost:
          1. UNKNOWN — the name is not in this node's pipeline. A typo must not silently re-run the
             whole pipeline from stage 0 (which is what `_run_stages` does with an unmatched
             `start_stage`, correctly, because for REUSE the fail-safe direction is to run MORE).
             Here the fail-safe direction is the opposite: refuse, and say what the stages are.
          2. NOT EARLIER — rolling back to the failed stage or a later one is either the ordinary
             retry or nonsense. Not an error worth spending a re-run on.
          3. REPEATED — this node already re-ran that stage on that suspicion. THE ANTI-THRASH RUNG,
             and the one that makes the whole feature bounded independently of any budget: a suspect
             may be tried at most once per node, so the worst case over a node's whole life is one
             re-run per stage, not one per attempt. Seeded from the durable log, so a resume does not
             hand back the allowance.
          4. NO CHANGE — the repair did not touch anything the suspect stage runs or imports, so
             re-running it produces the same bytes it produced before. This is the rung that makes
             "name it AND fix it" the contract rather than "name it and hope". It is decided with the
             SAME `_stage_reachable_files` closure `_safe_reuse_start` uses, read in the opposite
             direction: there, an intersection FORBIDS reuse; here, an EMPTY intersection forbids the
             re-run. Where the closure is OPAQUE (a shell wrapper, a bare binary, a `python -m` naming
             installed code) it cannot prove nothing changed, so the rollback is ALLOWED — fail-open
             here, bounded by rung 3 and by the retrain cap the caller charges. Note the 2026-08-14
             entry-point widening TIGHTENS this rung in the same commit that loosens `_safe_reuse_start`
             — a locally-resolvable `python -m` stage is now measured rather than waved through, so a
             rollback naming it while changing nothing it reaches is refused by name instead of costing
             a re-run. Both movements are the same fact (the stage became legible) pointed the way each
             rung's own cost asymmetry already says it should be.
          5. PROTECTED — rung 4 with the reason named. When the suspect's script is one the operator
             protected, no repair can EVER satisfy rung 4, so the refusal has to name the operator's
             file instead of reading as "try harder". This composes with the preflight
             `_unrunnable_protected_scripts` rather than fighting it: that one refuses BEFORE the
             pipeline runs when a protected script is absent, this one refuses a re-run of a protected
             script that is present but unfixable. Both say the same thing — the operator is the only
             participant who can act — and neither asks the agent to edit a file the write gate
             refuses.

        A non-default eval `cwd` skips rung 4/5 (allowing the rollback): changed-file keys and stage
        script paths resolve against different bases there, exactly as `_safe_reuse_start` documents,
        so the intersection proves nothing in EITHER direction. Allowing is the right default for an
        unprovable rung because rung 3 still bounds it; refusing would make rollback permanently
        unavailable on every subdir-cwd task.
        """
        names = [str(s.get("name")) for s in (stages or [])]
        suspect = str(suspect or "").strip()
        if not suspect:
            # THE COMMON CASE, and it must be free and SILENT: an empty ask is not a request, so it
            # neither spends an allowance nor puts engine text in front of the next repair.
            return None, None
        _fmt = {"suspect": suspect, "failed": str(failed_stage or ""),
                "names": ", ".join(names) or "(none)", "script": ""}
        if not names:
            # Asked for on a single-command eval. Distinct from the silent case above: the Developer
            # DID ask, so it gets an answer — an empty refusal would leave it re-asserting the same
            # request every attempt with nothing to learn from.
            return None, self.ROLLBACK_NO_PIPELINE.format(**_fmt)
        if suspect not in names:
            return None, self.ROLLBACK_UNKNOWN.format(**_fmt)
        si = names.index(suspect)
        # A failed stage the pipeline no longer contains (a repair renamed it) leaves `fi` at -1, so
        # `si < fi` is False and the rollback is refused as NOT EARLIER. That is the right answer:
        # with the failure's position unknown we cannot show the suspect precedes it, and this is the
        # rung whose false ACCEPT costs a full re-run of everything from `si` onward.
        fi = names.index(str(failed_stage)) if str(failed_stage) in names else -1
        if fi < 0 or si >= fi:
            return None, self.ROLLBACK_NOT_EARLIER.format(**_fmt)
        if suspect in set(already_rolled_back or ()):
            return None, self.ROLLBACK_REPEATED.format(**_fmt)
        if cwd not in (None, "", "."):
            return suspect, None                  # unprovable rung 4/5 — see the docstring
        changed = {(f[2:] if isinstance(f, str) and f.startswith("./") else f)
                   for f in (changed_files or [])}
        reachable = self._stage_reachable_files([stages[si]], workdir)
        if reachable is None:
            return suspect, None                  # OPAQUE stage — cannot prove nothing changed
        if changed & reachable:
            return suspect, None                  # the repair really did change what this stage runs
        # Nothing this stage reaches was changed. Say WHY it can never be, when that is the case: a
        # protected script is the one shape where rung 4 is not advice but a permanent condition, and
        # a Developer told only "change it first" would spend every remaining attempt trying to.
        prot = set((self._repo_spec or {}).get("protected_names") or [])
        blocked = sorted(p for p in reachable if p in prot)
        if blocked:
            return None, self.ROLLBACK_PROTECTED.format(**{**_fmt, "script": blocked[0]})
        # A non-.py change is invisible to the import closure (`_stage_reachable_files` only bounds
        # PYTHON imports), so an empty intersection does not prove the stage's inputs are unchanged —
        # a rewritten config.yaml or params file the stage reads would not appear. Unprovable, so
        # allow, exactly as the opaque and non-default-cwd cases do. Note the direction is the mirror
        # image of `_safe_reuse_start`'s treatment of the same blind spot, and deliberately so: there
        # an unprovable change forces a re-train (fail closed against a stale score), here it permits
        # one (fail open toward doing the work), because the two mistakes cost different things — a
        # wrong reuse silently scores a stale model, a wrong re-run only spends time.
        if any(not str(c).endswith(".py") for c in changed):
            return suspect, None
        return None, self.ROLLBACK_NO_CHANGE.format(**_fmt)

    def _deadline_grace_fn(self, node):
        """The ONE-SHOT deadline judge: a callback `(log_tail) -> seconds` handed to
        `run_command_eval`, or None when the feature is off / no client is wired.

        THE DECISION IT MAKES is "kill this stage now, or give it a little longer" — what happens
        NEXT, recoverable and re-checkable, and it touches nothing in the record. The metric still
        comes from the operator's own reader over the protected `score` stage; a graced stage that
        still misses the extended wall reports `timed_out` exactly as it always did; and the seconds
        actually bought are stamped on the stage row from the out-of-band `signals`, never read back
        out of the log.

        WHY IT IS NOT A THRESHOLD. All five `stage_finished.status == "timeout"` rows in `runs/`
        sit within 3.2 s of their own declared ceiling and discarded 24.1 GPU-hours between them
        (re-derived 2026-08-15; the older four / 22.0 h counted a row whose run directory is gone),
        and the
        whole captured record of `rubertlite-dense-retrieval` node 72 — 12.45 hours, five repairs —
        ends `100%|##########| 664/664 [00:17<00:00, 38.13it/s]`. At the wall, "two seconds from a
        checkpoint" and "will never finish" are the identical fact. Raising the budget instead does
        not help: it moves the same cliff later and pays for every genuinely hung run on the way.

        WHAT BOUNDS IT, because a judge reading the CANDIDATE'S OWN log is exactly the input that
        must not be trusted with anything expensive: the answer is a single word, never a number
        (`parse_deadline_reply` — fail-closed, so an unreadable reply kills as before); the seconds
        come from the OPERATOR'S `eval_deadline_grace_s`; `sandbox._granted_grace` clamps them again
        in the runtime; the judge is asked AT MOST ONCE per command; an operator `cancel` is never
        graced; and — until 2026-08-23 — the whole thing was OFF by default. It is now ON as
        AUTO, which changes WHO pays and not WHAT the judge can do: the seconds are still the
        operator's, still clamped in the runtime, still one-shot, still fail-closed. So a solution
        that prints a convincing progress bar buys at most 10% of its own stage's wall (never more
        than 30 minutes), once, and no other thing."""
        # `cap == 0` is the ONLY off switch. A NEGATIVE cap is the AUTO sentinel and must build the
        # judge: the seconds it may buy are resolved per stage in the runtime, against that stage's
        # own wall, and are not knowable here — the engine threads ONE cap for the whole pipeline.
        # Testing `cap <= 0` (what this said while the feature was opt-in) would read AUTO as OFF
        # and silently ship the new default as no change at all.
        import math
        try:
            cap = float(getattr(self, "eval_deadline_grace_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(cap) or cap == 0:
            return None
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return None
        idea_text = self._idea_text(node.idea) if node is not None else ""

        # EVAL-PATH, not background — the same reasoning as `_stage_check_fn`: this blocks the eval
        # worker at the deadline and there is one per concurrent eval.
        @in_llm_lane("engine")
        def _judge(tail: str) -> float:
            msgs = [{"role": "system", "content":
                     "An ML pipeline stage has just reached its wall-clock time budget and is about "
                     "to be killed, discarding everything it has done. You are shown the tail of its "
                     "LIVE log. Decide ONE thing: is this stage within a few minutes of completing "
                     "the work it is doing, or not? Evidence FOR: a progress bar at or near 100%, a "
                     "final epoch/step counter almost at its total, a checkpoint or evaluation "
                     "already being written. Evidence AGAINST: early progress, a repeating error, no "
                     "movement, or a log you cannot read progress from. Default to NOT_FINISHING — "
                     "the extension is granted once and is paid for in GPU time, so guessing costs "
                     "real money, while being wrong the other way costs one stage that was going to "
                     "be killed anyway. Reply with EXACTLY one word: FINISHING or NOT_FINISHING."},
                    {"role": "user", "content":
                     f"Experiment: {idea_text[:400]}\n\nLive log tail:\n{tail}"}]
            try:
                out = (client.complete_text(msgs) or "").strip()
            except BudgetExceeded:  # a hard budget stop must propagate, never degrade (core/containment.py)
                raise
            except Exception:  # noqa: BLE001 — a judge failure must never turn a timeout into a crash
                return 0.0
            # ASK FOR EVERYTHING ALLOWED, never for a number. `_granted_grace` clamps to the cap the
            # RUNTIME resolved, and under AUTO that cap is not knowable here — `cap` is the sentinel
            # `-1`, which `_granted_grace` reads as "no" (`not (asked > 0)`) and which would have
            # shipped the new default as a feature that is on, is asked, agrees, and grants nothing.
            # `inf` is identical to the old behaviour under an explicit cap: min(inf, cap) == cap.
            return float("inf") if parse_deadline_reply(out) else 0.0

        return _judge

    def _stage_check_fn(self, node, workdir=None, stages=None):
        """Phase 3 inter-stage verify: a callback (stage_name, log_tail, expect="") -> verdict|None that
        asks an LLM whether a `check`-flagged stage physically SUCCEEDED (train actually trained + saved a
        checkpoint, no silent fallback) BEFORE the next stage runs. This is a SANITY gate, NOT a
        quality/ranking judgment: it must fail a stage ONLY on a hard, unambiguous failure — never because
        the metric looks 'not good enough' or 'below the previous best' (that is the search's job
        downstream). A real incident motivated the tightening: the checker failed the run's BEST model (val
        recall@100 ≈ 0.855, above the champion) by (a) reading loss MAGNITUDE (~14.6) as 'no learning' —
        loss scale depends on the loss/temperature and is not comparable across configs — and (b) grabbing a
        bystander scalar (val recall@50 = 0.79) as 'the metric' and calling it below-best. Returns None
        (checks skipped) when no client is available. Runs inside the eval worker thread, so
        complete_text blocks there — fine, like the eval.

        `expect` is the stage's own DECLARED success condition
        (`runtime/command_eval.py::_validate_expect`), passed by `_call_stage_check` when the manifest
        carries one. It is the ONE thing the checker may fail a stage for beyond a hard failure, and the
        reason the ban above is not simply wrong: with no contract, "did this work?" and "is this good?"
        are the same question and the checker answered the wrong one. Empty `expect` narrows the
        vocabulary rather than the prompt: `declared_condition_violated` is not offered and is refused
        on the way back in (`parse_stage_check_reply`), so a stage using only `check: true` can be
        failed for physical failure and nothing else.

        WHAT IT RETURNS is a `StageCheckVerdict`, not a concern string, and that is the 2026-08-13
        change: the tightening above was PROMPT-ONLY and did not hold. `rubertlite-dense-retrieval`
        node 21 was killed after the tightening with "validation recall (0.79) is below previous best
        (0.8491)" — the banned comparison, verbatim. Prose can no longer end a node; only a named
        member of `command_eval.STAGE_CHECK_HARD_KINDS` can. Everything else is `inconclusive` and
        the pipeline continues.

        `workdir`/`stages` are what let this stop asking an unanswerable question. The tail this
        callback is handed is `run.out[-4000:]` of a `run.out` that is itself a 64,000-byte tail
        clamp, so `loss_unchanged_from_first_step` — "unchanged from the FIRST training step" —
        cannot be decided from it: the first step is not in the window and structurally cannot be.
        With them, the engine measures the whole of this attempt's stage log itself
        (`train_monitor.stage_check_trajectory`), HANDS the measurement to the model beside the tail
        (`trajectory_context`, the same block the live monitor's judge already gets), and vetoes a
        `loss_unchanged_from_first_step` refusal the measurement contradicts
        (`trajectory_acquits_stage_check`). See the block above `STAGE_CHECK_TRAJECTORY_KIND` for the
        ten nodes in `runs/rubertlite-dense-retrieval` this cost and for what the veto may not do.
        Both are optional so a caller without a workdir (the library shape, and the doubles in the
        suite) keeps the historical prompt and the historical verdict byte for byte; the ONE
        production caller (`eval_dispatch._run_eval`) passes both, which
        `tests/test_stage_trajectory.py` drives end to end rather than asserting."""
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return None
        # THE "BEFORE", taken HERE and not at check time. Stage logs are opened for APPEND, so by the
        # time a stage is checked its own bytes are already mixed with any previous attempt's in the
        # same file — there is nothing left to take a before OF. This runs after `_eval_pipeline` and
        # before `run_command_eval`, i.e. before any stage of this attempt has written a byte, which
        # is the same instant `evaluate.py::_evaluate` takes its own snapshot at and for the same
        # reason (`needs_log_snapshot`). It costs one glob plus a 64-byte read per existing `*.log`,
        # and only on a pipeline that asked for a check at all.
        from looplab.engine.train_monitor import (eval_log_plan, snapshot_training_logs,
                                                  stage_check_tools, stage_check_trajectory,
                                                  trajectory_acquits_stage_check, trajectory_context)
        _log_plan = _log_snapshot = None
        if workdir is not None:
            try:
                _log_plan = eval_log_plan(stages)
                _log_snapshot = snapshot_training_logs(workdir)
            except Exception:  # noqa: BLE001 — an instrument may never take down an eval
                _log_plan = _log_snapshot = None
        idea_text = self._idea_text(node.idea) if node is not None else ""
        # Name the run's OBJECTIVE metric (from the operator's metric reader) so the checker judges the
        # RIGHT number and isn't misled by a bystander scalar in the log (e.g. recall@50 vs the @100 goal).
        _ms = (self._eval_spec or {}).get("metric") or {}
        objective = (str(_ms.get("pattern") or _ms.get("key") or "").split("(")[0].strip()
                     or "the objective metric")

        # EVAL-PATH, not background (`core/llm_broker.py::BACKGROUND_LANE_PRODUCERS`). This ran in
        # the capped `enrichment` lane beside the two live-log watchdogs, but it is the opposite kind
        # of producer: the eval worker BLOCKS on it between stages (see the docstring above), there is
        # one per concurrent eval, and the whole batch inherits its latency. Once the background caps
        # were unwelded from the global total, N evals' stage checks serialized at one and each queued
        # behind whichever watchdog held the permit — measured at 4 concurrent evals: peak 1, 4x the
        # wall time. `engine` is where its eval-path siblings already live (the repair loop reaches it
        # as the fallback lane), and it stays governed by a finite total when the operator sets one.
        @in_llm_lane("engine")
        def _check(stage_name, tail, expect: str = ""):
            # THE TRAJECTORY, measured before the model is asked, for both of its uses below. An
            # unreadable log answers `windows=0`, which is the value both uses treat as "no
            # measurement" — the prompt keeps its historical bytes and the veto never fires.
            trajectory = None
            if workdir is not None:
                try:
                    trajectory = stage_check_trajectory(workdir, stage_name, plan=_log_plan,
                                                        snapshot=_log_snapshot)
                except Exception:  # noqa: BLE001 — an instrument may never take down an eval
                    trajectory = None
            measured = trajectory_context(trajectory) if trajectory is not None else ""
            # THE TOOLS, built at CHECK time and not when this callback was made (doc 52 row 9): the
            # checked stage's log does not exist until the stage has run, and `_log_query_tools`
            # answers None for a workdir with no nameable log. None keeps the historical single
            # completion; the gate (`Settings.stage_check_tools`) and the boundary are
            # `train_monitor.stage_check_tools`'.
            tools = None
            if workdir is not None:
                try:
                    tools = stage_check_tools(self, workdir, _log_plan, _log_snapshot)
                except Exception:  # noqa: BLE001 — an instrument may never take down an eval
                    tools = None
            msgs = [{"role": "system", "content":
                     "You are a SANITY checker for ONE stage of an ML eval pipeline, run BEFORE the next "
                     "stage. Decide ONLY whether this stage physically SUCCEEDED and produced a usable "
                     "artifact for the next stage. FAIL it ONLY on a HARD, unambiguous failure: a "
                     "crash/traceback, no checkpoint saved, a silent fallback to a stale/pretrained "
                     "model, a NaN/inf loss, or a loss LITERALLY UNCHANGED from the first training step "
                     "(genuinely no learning). Do NOT judge result QUALITY or RANKING: a trained model "
                     "whose metric is merely mediocre, or BELOW a previous run, still PASSES — the search "
                     "ranks and selects downstream, never you. Loss MAGNITUDE is NOT a failure signal (it "
                     "depends on the loss function and temperature; a loss around 14 can be perfectly "
                     "healthy). A present, non-trivial validation metric is strong evidence the stage "
                     "SUCCEEDED. Answer in ONE line, in one of exactly three forms:\n"
                     "  OK — the stage succeeded.\n"
                     "  FAIL <kind>: <one-line evidence> — where <kind> is EXACTLY one of "
                     "crash, nan_or_inf_loss, no_artifact_written, silent_fallback, "
                     "loss_unchanged_from_first_step"
                     + (", declared_condition_violated" if expect else "") + ".\n"
                     "  INCONCLUSIVE: <what you would need to see> — when the output does not let "
                     "you tell. USE THIS FREELY. A stage that printed little or nothing is "
                     "INCONCLUSIVE, not a failure: many stages legitimately do their work quietly, "
                     "and this pipeline has already verified on disk that the stage produced every "
                     "artifact it declared. FAIL only when the evidence for one of the kinds above "
                     "is actually IN the output; if your reason would not fit one of those kinds, "
                     "it is INCONCLUSIVE."
                     + (EvalStagesMixin.STAGE_CONTRACT_CLAUSE if expect else "")
                     + (("\n\n" + STAGE_CHECK_LOOK_INVITATION.format(stage=stage_name))
                        if tools is not None else "")},
                    {"role": "user", "content":
                     f"The run's objective metric is `{objective}` — ignore other scalars when judging "
                     f"whether the stage worked.\nExperiment: {idea_text[:400]}\n\n"
                     + (f"DECLARED CONDITION for stage '{stage_name}': {expect}\n\n" if expect else "")
                     + (measured + "\n\n" if measured else "")
                     + f"Stage '{stage_name}' output tail:\n{tail}"}]
            try:
                if tools is None:
                    out = (client.complete_text(msgs) or "").strip()
                else:
                    # `agentic_text` is the plain completion upgraded to a tool loop: the model may
                    # read the log first, then emits its line through `answer`; a loop that yields
                    # nothing degrades to `client.complete_text(msgs)` over the same messages, so the
                    # off path and the exhausted path are the same call. The verdict LINE is read
                    # out of the answer (`stage_check_verdict_line`) because a checker that has just
                    # read a log says what it found before it says the verdict.
                    from looplab.agents.tool_loop import agentic_text
                    out = stage_check_verdict_line(agentic_text(
                        client, tools, msgs, loop_opts={"max_turns": STAGE_CHECK_LOOK_TURNS},
                        answer_desc=("your ONE-LINE verdict: `OK`, `FAIL <kind>: <evidence>`, or "
                                     "`INCONCLUSIVE: <what you would need to see>`")))
            except BudgetExceeded:  # a hard budget stop must propagate, never degrade (core/containment.py)
                raise
            except Exception:  # noqa: BLE001 — a checker failure must never fail the eval
                return None
            verdict = parse_stage_check_reply(out, declared=bool(expect))
            if verdict is None:
                return None                      # OK — the veto has nothing to acquit
            # THE VETO. It may only ever move the verdict DOWN to `inconclusive`; the concern keeps
            # BOTH readings, because a row that cannot say the model and the engine disagreed is a
            # row the next reader cannot check. `command_eval` records it under
            # `check_inconclusive` and the pipeline continues.
            acquitted, note = trajectory_acquits_stage_check(verdict.kind, trajectory)
            if not acquitted:
                return verdict
            from looplab.runtime.command_eval import STAGE_CHECK_INCONCLUSIVE, StageCheckVerdict
            # The ENGINE's reading FIRST and the model's second, because `command_eval` clamps this
            # to 300 characters and the direction is the load-bearing word: composed the other way
            # round, the clamp ate the finding and left a row that only says the checker complained.
            # The model's half is bounded here rather than left to that clamp, so what is dropped is
            # a stated choice — its `kind` is preserved whole and its prose is what gets cut.
            # `parse_stage_check_reply` already prefixes its detail with the kind, so naming it again
            # would print it twice on every vetoed row; a concern that does NOT carry it still gets it.
            said = str(verdict.concern or "")
            if not said.lower().startswith(str(verdict.kind).lower()):
                said = f"{verdict.kind}: {said}"
            return StageCheckVerdict(STAGE_CHECK_INCONCLUSIVE,
                                     f"{note} | checker had answered {said[:120]}"[:300])

        return _check
