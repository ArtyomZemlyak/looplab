# LoopLab — Consolidated Development Backlog

**Date:** 2026-06-24 · One flat, status-tagged checklist that merges (a) the remaining
[code-review](CODE_REVIEW.md) hardening and (b) the [strategic roadmap](ROADMAP.md) Themes A–I, with
*verified current status in the code*. This is the actionable tracker; ROADMAP.md is the narrative/why.

**Status legend:** ✅ done · 🟡 partial (some sub-items done) · ⬜ todo
**Priority:** P0 (do next) · P1 · P2 · **Effort:** S / M / L
**Evidence notes:** a **`[2026-08-14 — …]`** block under a row is that row's status re-derived from
the code at `f458f4af`, with the file/symbol that decided it. A row without one was not re-checked.

> **✅ RE-DERIVED FROM THE TREE, 2026-08-14.** Every `⬜`/`🟡` row below was re-checked against the
> code at `f458f4af`, not against another doc, and each carries a **`[2026-08-14]`** evidence note
> naming the file/symbol that decided it. **58 rows** were triaged (a naive grep for `⬜` counts 60 —
> two of those glyphs are in the legend and in caveat 1 below, not rows): **35 DONE · 16 PARTIAL ·
> 7 STILL OPEN**, four of them closed only because the design moved past them and now say what
> superseded them (B1's read-only mount, A2's TPE/RF, A0e's Debug node, `_shutdown_pool_sockets`).
> Two rows that were ALREADY `🟡` (§1 C4, §1 B4) were re-checked in the same pass and appear in the
> ranked list, so 25 items carry unfinished work. The ranked list of what is genuinely left is
> **§0 SURVIVORS**, immediately below. Rows that closed keep their text and gain a note; rows that the
> design moved past say what superseded them rather than being deleted (a decision recorded only in a
> commit message gets undone here).
> **[Later on 2026-08-14:** survivors **#1 and #2 were CLOSED the same day** by `dcb4c9a`
> (`_confine_task_file` + the `redact_output_tail` split) — see the dated notes on those two items
> and the reconciled §1 C2/C3 rows, which are the authoritative record. That leaves **23** items
> carrying unfinished work, and a new **#19** (claim ratification vs trust flags) was added below.
> **Later still on 2026-08-14:** survivor **#11 was CLOSED** — fork-to-branch's RunView panel landed,
> so its row is now a ✅ record of what shipped rather than a to-do. **22** items carry unfinished
> work.]
>
> Thirteen symbol/line citations in this file were found **dead or moved** — they are corrected inline
> and listed in §0.3. The caveats from 2026-08-04 still apply to everything NOT carrying a
> `[2026-08-14]` note:
>
> **⚠ Original stale header (2026-08-04).** Last content edit before this pass was `59c33465`,
> 2026-07-22; the header date above (2026-06-24) is ~6 weeks behind HEAD. Four things a reader must
> know:
>
> 1. **The file contradicts itself by construction.** Every roadmap ID marked ✅ in the "★ Shipped
>    2026-06-24" section below is *re-listed as ⬜ todo* in §2's Themes A–I (46 IDs). §2 is the older
>    text; the ★ Shipped roll-up is the newer one. **Where they disagree, prefer ★ Shipped — and then
>    verify against the code.**
> 2. **IDs are not unique — three separate namespaces share one letter-digit space.** `C2` means
>    "best-of-N" in ★Shipped/§2 but "secrets/output redaction" in §0/§1; `C3` means "deep repair" vs
>    "auth token"; `C5` means "read-model integrity" (§1) vs "agentless mode" (§2). §6 introduces a
>    THIRD `D1–D5` namespace (static-analysis defect classes) unrelated to `D1 real MLE-bench`. Never
>    cite a bare ID from this file without its section.
> 3. **Every inline `file.py:NNN` citation in this file is dead** (8 of 8 checked). They are rendered as
>    `master` links, so they resolve but point at unrelated code — e.g. `server.py:245` for `delete_run`
>    (which moved to `serve/routers/org.py:237`), `llm.py:72` for `_post` (now `core/llm.py:911`),
>    `orchestrator.py:808` for a stdout-tail claim whose real site is `engine/evaluate.py:732` **and
>    whose "persisted verbatim" claim is refuted — redaction shipped** (`engine/audit.py:269::_redact`).
>    Symbol-level citations (`module::Symbol`) fare much better (11 of 12 resolve).
> 4. **Roughly half the shipped system is not tracked here at all.** Absent entirely: the Card/kanban
>    board (`docs/23`), Part IV/V concepts & novelty, the per-run cost ledger (`engine/costs.py`), the
>    live watchdogs (`engine/train_monitor.py`, `engine/asha_monitor.py`), claims governance
>    (`cli/governance_cmds.py`), the assistant serve surface, and the owner attention feed
>    (`serve/attention.py`). Absence from this file is **not** evidence that something is unbuilt.
>
> Individually corrected items are marked **[corrected 2026-08-04]** inline below.

---

## §0 SURVIVORS — the ranked "what is actually still open" list (2026-08-14)

Ordered by **cost of leaving it**, measured where a measurement exists and estimated otherwise. Start
at the top. Everything not listed here is DONE or is low-cost residue (§0.2). Each entry names the
site that proves it is open.

### §0.1 Ranked

1. ✅ **`task_file` is executed from any path on the box, and the API token is opt-in (P0, S).**
   `serve/launch.py:422-426` did `Path(os.path.expandvars(os.path.expanduser(v))).resolve()` and
   loaded it — the only guard an 8 MiB size cap. The other half of the old C3 row shipped as an
   *opt-in*: `server.py` read `LOOPLAB_UI_TOKEN` and default-denied `/api/*` only when it was set,
   printing its own warning that the control plane is "UNAUTHENTICATED and reachable by any
   same-origin page" on a shared JupyterHub origin. **Cost:** an unauthenticated same-origin request
   names an arbitrary host file and the server parses and runs it as a task.
   **[2026-08-14 — BOTH HALVES DONE.** The allow-list landed first (`launch.py::_confine_task_file`
   over `task_file_roots`, the same derivation `GET /api/tasks` builds its pick-list from — see the
   C3 row in §2). Two things were still open under it and are now closed.
   *(a) the check was about a NAME, and three separate opens of that name followed it* — the size
   `stat`, `load_document`'s `read_text` and the fingerprint's `read_bytes`. `read_confined_task_file`
   makes it ONE fenced read: `O_NOFOLLOW` on the already-resolved path (a final component that became
   a symlink after the check is refused, and a resolved path holds no symlinks, so nothing legitimate
   is refused by it), `O_NONBLOCK` + `S_ISREG` (a FIFO in a declared root used to block the preflight
   worker forever), and a `core/atomicio.file_identity` CAS between the descriptor's `fstat` and an
   `lstat` after the read, so a replaced or rewritten file is `422 task_source_changed` instead of
   parsed — and the parsed bytes are the fingerprinted bytes. `_read_bounded` is the seam the race
   tests drive; the genesis card's own uncontained, uncapped `task_file` read
   (`_defaults_backend_llm`) now takes the run root and goes through the same allow-list.
   *(b) the token default* is `serve/owner_token.py`. It is not one answer: on a PRIVATE origin an
   unset `LOOPLAB_UI_TOKEN` still means unauthenticated (byte-for-byte the historical local
   single-user behaviour — the server binds loopback and nothing shares the origin), and on the
   SHARED hub origin the server already detects, it FAILS CLOSED by minting a token into
   `~/.looplab/ui-token` (`0600`, reused across restarts, logged with its path and value) and gating
   `/api/*`. Not a loopback-only bind — it already binds loopback and jsp connects to it, so that
   flag cannot see the difference; not a refusal to start — the hub deployment is a Launcher tile
   with no terminal in which to export the variable. `LOOPLAB_UI_ANONYMOUS=1` is the explicit,
   logged opt-out. `serve/tui_api.py` reads the same file so `looplab tui` still works.
   Driven with real requests in `tests/test_owner_token.py` and `tests/test_launch_preflight.py`;
   `tests/test_server.py::test_g1_shared_hub_warns` was re-pointed rather than left speaking the old
   contract, and the "unauthenticated" assertion moved onto the opt-out, the only state that still
   reaches it. **A LIVE server keeps whatever default it started with** — this changes the next
   start, not a running process.]
2. **Output redaction shipped but is OFF by default (P0, S).** `engine/audit.py:269::_redact` →
   `core/redact.py::redact_secrets` is wired at the one durable site
   (`engine/evaluate.py:2445`, `"stdout_tail": self._redact(res.stdout[-500:])`), but
   `core/config.py:920` is `redact_output: bool = False`. **On the default path the original defect is
   unchanged** — a `print(secret)` or a traceback is persisted verbatim into `events.jsonl` and the
   UI. Two further `stdout_tail` producers do not go through `_redact` at all
   (`agents/cli_agent.py:349,370`). **Cost:** the event log is the artefact that gets exported and
   shared. Fix is a default flip plus two call sites.
   **[CLOSED later on 2026-08-14 (`dcb4c9a`) — this entry predates the same-day fix.** The gate was
   SPLIT rather than flipped: `core/redact.py:208::redact_output_tail` (+ `:197::redact_env_values`)
   masks known credential shapes and the operator's own secret env values ALWAYS, funnelled through
   `engine/audit.py:288-289::Engine._redact` (the cited `:269` drifted); `redact_output`
   (`core/config.py:926`) now gates only the false-positive-prone entropy pass. Driven at the
   default config by `tests/test_redact.py`. Residual, verified 2026-08-14: the two
   `agents/cli_agent.py:349,370` `stdout_tail` producers still bypass `_redact` (they feed the
   in-memory `AgentRun` telemetry). The reconciled §1 C2 row is the authoritative record.]
3. ~~**The hardened exploit suite re-flags the grader import the detector just sanctioned (P1, S).**~~
   `engine/evaluate.py:807-815` correctly passes `grader_import_ok=True` to `detect_reward_hacks`
   when the task ships `grader.py` as an asset — and then `evaluate.py:819` runs
   `self._exploit_suite.scan(scan_src)` **unconditionally**, and `ExploitSuite.scan`
   (`trust/harden.py:79-90`) takes no sanction argument while `_SEED_EXPLOITS` still carries the
   `\bimport\s+grader\b` regex (`trust/harden.py:34-36`). **Cost:** a false `reward_hack_suspected`
   on a run that persisted a `looplab harden` suite bars the node from `feasible_nodes` — i.e. it
   silently discards good nodes on MLE-bench, the proof point.
   **FIXED 2026-08-14** — `ExploitSuite.scan` takes the sanction, `evaluate.py` derives it once and
   hands it to both detectors, waived per MATCH. One correction to the cost claim, in the survivor
   entry below: no suite on disk carries that rule today, and the shipped `harden` path cannot mint
   it — it is one `harden(hacker=…)`/hand-authored suite away, so the exposure was latent, not live.
4. **The read model is built once, at exit, with no watermark (P1, M).** `events/readmodel.py` is 55
   lines and exposes only `build_readmodel` — **no seq watermark, no refresh-on-append**. Its sole
   caller is `engine/finalize.py:912` via `_build_readmodel_atomic` (`finalize.py:479`), which DID
   fix the atomicity half (tempfile + `os.replace`). **Cost:** a crashed or still-live run has no
   read model at all, and a post-run control event diverges from it undetectably.
   **FIXED 2026-08-14 — two of the three, and the third was DELIBERATELY NOT BUILT.**
   `build_readmodel` now stamps a WATERMARK (schema version, last `seq` folded, event count, digest
   of the `(seq, type)` prefix) in the SAME transaction as the rows, and `readmodel_status` /
   `readmodel_is_current` fail closed — absent file, unopenable db, missing/duplicated/malformed
   row, unknown schema version and uncomputable digest all answer `unknown`, never `current`. The
   reader opens `file:…?mode=ro`, because a plain `sqlite3.connect` CREATES a database and would
   turn "this run has no read model" into "it has an empty one". All 29 preserved
   `runs/*/readmodel.sqlite` load their `nodes` table unchanged and report `unknown`.
   The exit-only half is closed by `looplab readmodel RUN_DIR [--check]` (`cli/inspect_cmds.py`) —
   the same `publish_readmodel` the finalization calls, reachable during a live run and after a
   crash, refusing a run whose deletion/reset fence is set or unreadable. It is safe under
   invariant #1 (a derived sidecar is not an event; it appends nothing) and #4 (nothing in
   `looplab/` opens the database, so it cannot become cached derived state the engine reads back).
   **REFRESH-ON-APPEND was measured and rejected, not deferred.** The rebuild is not on any hot
   path because it has NO programmatic reader: `sqlite3` is imported in exactly one module in
   `looplab/`, `readmodel.py` itself. The other five mentions are non-readers — `serve/artifacts.py`
   lists it as an opaque binary, `serve/reset_transaction.py` names it in `RESET_ARTIFACT_NAMES`,
   `tools/perm_modes.py::DEFAULT_PROTECT` stops an agent clobbering it, `agents/preflight.py`
   advertises it in prose, and the four test references only assert existence. And the expensive
   part is already incremental one layer down: measured on the largest real log
   (`runs/rubert-dr-0804`, 106 MB / 2,680 events) a full rebuild costs 6 ms to fold and 3 ms to
   write once the events are in memory, against 1.06 s to prime `EventStore`'s own incremental
   parse cache — so an incremental refresh here could save at most single-digit-to-220 ms
   (`rubertlite-dr-unified-v6`'s fold is the worst at 223 ms) while duplicating machinery
   `EventStore` already has. The watermark itself costs 0.8-1.2 ms to compute and 0.1 ms to read.
   **STILL OPEN:** no periodic in-engine refresh (a live run's model is only as fresh as the last
   `looplab readmodel`), and no consumer consults the watermark — there is no consumer at all, so
   the guarantee is available rather than enforced. If a reader is ever added, it must call
   `readmodel_is_current` before trusting a row.
5. **`agentless` is not a developer backend, and a Strategist branch for it is dead code (P1, M).**
   `core/config.py:350::DEVELOPER_BACKENDS = ("default", "aider", "continue", "goose", "opencode")` —
   no `llm`, no `agentless`. `engine/strategy.py:75-77::_available_developers()` returns
   `["default", "llm", *PRESETS]`, so `agents/strategist.py:408-409`'s
   `if "agentless" in ctx.available_developers` (comment: *"only when C5 has landed"*) can never
   fire. **Cost:** the Strategist's `developer` decision has a permanently unreachable arm — a
   silent capability gap, not a red test — plus the localize→generate-N→validate pipeline itself.
   Its two building blocks already exist (`engine/localize.py`, `search/best_of_n.py`).
   **[2026-08-14 — the DISAGREEMENT half is CLOSED; the BACKEND half is not.** All three citations
   were real. The vocabulary now has one home: `core/config.py::DEVELOPER_BACKENDS` +
   `DEVELOPER_BACKEND_ALIASES` (`llm` -> `default`, published as `developer_switch_names()`), which
   `_available_developers` derives from and `make_developer_factory` resolves through instead of the
   bare `"llm"` literal; `tests/test_developer_backend_registry.py` guards both directions and
   AST-scans the tree, so a re-introduced `agentless` arm names its own file and line. The dead
   branch is REMOVED with a comment saying why (an unreachable `if` is a promise the code cannot
   keep). **What was measured, and is the reason it was a guard and not a lint:** an unregistered
   `developer` is dropped by `validate_strategy` *before* `_prepare_strategy_developer` runs, so the
   `developer_application: {status: "refused", …}` receipt — which exists, and fires for a factory
   refusal — cannot fire for an unknown NAME. Driven end-to-end through `_maybe_consult_strategist`:
   `{"policy": "mcts", "developer": "agentless", "rationale": "switch developer to agentless"}` is
   recorded with the policy applied, the rationale VERBATIM, no `developer` field and no receipt of
   any kind. The history reads as a switch that never happened.
   **STILL OPEN — exactly what an `agentless` backend needs** (nothing below is built):
   (a) a `Developer` object composing localize -> generate-N -> validate. `engine/localize.py` and
   `search/best_of_n.py` exist but neither is a Developer: `BestOfNDeveloper`
   (`agents/factory.py:483-487`) wraps an existing Developer and re-runs its `implement`, and
   localize is engine-side, so the new class owns the *sequencing* and the file-scope hand-off;
   (b) a construction site in `agents/factory.py::make_roles` keyed on `developer_backend`, since
   `PRESETS` membership is what routes a name to the external-CLI path today and `agentless` is
   neither a preset nor the in-house default — a third branch, not a preset entry. Note the same
   binary predicate gates three other decisions in that file (`:348` in-process build, `:461` sweep
   offer, `:487` the `BestOfNDeveloper` wrap), so a new name falls into the in-house side of all
   three by default — including a best-of-N wrap the agentless pipeline already does itself;
   (c) the registry entry (`DEVELOPER_BACKENDS`), which is what makes it `Settings`-configurable AND
   what makes `_available_developers` offer it to the Strategist — with no edit to `strategist.py`,
   because the removed arm's `if` is now expressible as a plain membership test on a name that
   really exists. Note `test_every_configurable_backend_exists` demands a matching `PRESETS` key, so
   landing (c) means extending that test's `{"default"}` exemption to the in-house family;
   (d) the C2 knobs already present (`best_of_n`, `best_of_n_listwise`) either reused or given
   agentless-specific spellings, plus a docs row for whichever;
   (e) a producer that can actually ASK for it — today the LLM Strategist's `_StrategyOut` has no
   `developer` field and the operator `set_strategy` control refuses one, so the only live producer
   of a `developer` decision is a rule-based/custom Strategist. Selecting it "by the A7 Strategist
   per phase/node", as this row asks, needs that field added to the structured schema and spliced
   into `_strategist_brief` (which today never mentions developers at all).
   Deliberately NOT built here: it is a feature with real blast radius, and the box is mid-GPU-run.]
6. **The schema-aligned parser is a fallback, not the default (P0, S).** `core/parse.py:195
   ::_coerce_to_model` IS a real error-correcting SAP (case-insensitive key match, per-field
   coercion, extras dropped) — but `core/config.py:1483` is `llm_parser: str = "tool_call"` and
   `parse.py:213::_ORDER["tool_call"] = ["tool_call", "baml"]`, so it only runs after native FC has
   already failed. **Cost:** this box serves local models, which is exactly where native FC collapses
   (~20 % vs ~92–94 %). The original note called it the cheapest whole-system lift and that still
   holds — it is now a one-line default change plus its blast-radius test.
7. **Untrusted code still gets a writable container filesystem (P1, M).** `runtime/sandbox.py:230-238`
   has `--pids-limit 1024`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory`,
   `--cpus`; `--read-only` + tmpfs appears **nowhere** in the tree, and `sandbox.py:241` mounts
   `-v {root}:/work` with no `:ro`. Windows tree-kill is `taskkill /F /T` (`sandbox.py:1275`), not an
   atomic Job Object. Partially covered by newer, independent rungs — but `Settings.landlock` is OFF
   by default (`runtime/landlock.py`) and `runtime/read_fence.py` only sees `open` inside CPython.
8. **Nothing tests the append lock across real OS processes (P1, S).** Both other C4 halves landed —
   `Event.v` is enforced (`events/eventstore.py:166-168`, `UnsupportedEventVersionError`) and the
   append lock fails loud (`_interprocess_lock`, `eventstore.py:254-321`, raising
   `EventStoreLockError`). What is still missing is the test the row asked for: the suite has source/
   AST parity checks (`tests/test_append_critical_section_parity.py`) and monkeypatched-failure
   simulations, but **no concurrent multi-process append race**. **Cost:** the durability guarantee
   the whole replay design rests on is held by inspection.
9. **Two derivations of "which stages will run", and they disagree (P2, S–M).** `_resolved_stages`
   moved out of `orchestrator.py` to `engine/eval_stages.py:261-278` and still re-implements
   `_run_eval`'s chain (`engine/eval_dispatch.py:572-649`). The flagged divergence is intact:
   `eval_dispatch.py:604` does `prof = profile or (node.idea.eval_profile ...)` while
   `_resolved_stages` **has no `profile` parameter at all** (`eval_stages.py:269-271`). **Cost:** its
   four callers — log-watch planning (`evaluate.py:1097,1655,2340`) and salvage re-check
   (`metric_salvage.py:998`) — plan against the smoke chain during a `confirm`/full pass. Note the
   proposed fix does NOT work: `RunResult.stages` (`runtime/sandbox.py:290-294`) is the post-run
   *outcome* record and all four callers need the chain *before* the run.
   **[CLOSED 2026-08-14. The divergence was real, the COST CLAIM was not — and the line numbers had
   drifted ~29 lines (`_resolved_stages` was `eval_stages.py:290-307`, the flagged `prof =` line
   `:298`; `eval_dispatch.py:572`/`:604` were exact).** Two corrections to this entry, both
   measured rather than argued:
   **(a) there are THREE callers, not four, and one of the four is a docstring.**
   `metric_salvage.py:1006` only *documents* that its `stages` argument came from
   `Engine._resolved_stages`; the salvage re-check that actually calls it is
   `evaluate.py:1123::_recheck_repaired_contract`. The other two are `evaluate.py:1681` (the
   watchdogs' log plan) and `evaluate.py:2372` (the inline-repair reuse/rollback predicate, which is
   the use the method was written for).
   **(b) the divergence was LATENT, not live.** The only production call site that passes an
   explicit `profile` is `confirm_phase.py:224` (`"full"`), and it never plans; all three planning
   sites sit inside `evaluate.py::_evaluate`, which dispatches `_run_eval(..., None, cancel, …)` —
   so the two derivations agreed on every reachable call. Nothing in the 66-run `runs/` corpus could
   have hit it: **zero `confirm_eval` rows exist in any preserved run** (no `confirm_*` event of any
   kind), so the confirm phase has never run on this box. And had a planner been reachable at a
   non-default profile, only ONE of the three could have seen a difference: a profile changes only
   the appended protected `score` stage's overrides and `timeout` (`build_command`), so
   `train_monitor.eval_log_plan` — which reads stage NAMES — is invariant under it. This is
   therefore **not** `662a9be6` ("the watchdogs judged the wrong log") returning by another route;
   that defect was mtime-based attribution, and the plan has been name-derived since.
   **The shape.** One derivation, `eval_stages.py::_eval_pipeline(node, workdir, profile=None) ->
   (command, timeout, stages)`, called by the dispatcher and — through a thin, total
   `_resolved_stages(node, workdir, profile=None)` — by the planners, the same
   straddling-readers pattern `command_eval.eval_spec_time_budget` documents. The dispatcher keeps
   only its two SIDE EFFECTS (`_ensure_run_setup`, `_sync_node_deps`): what it knows that a planner
   cannot is exactly the caller-supplied `profile`, which is an argument, not private state, and the
   function says so. `tests/test_resolved_stages_profile.py` drives a real staged eval at `"full"`
   and asserts the planner returns the chain the dispatcher ran (and, mutated to ignore the profile,
   goes red).
   **Still open, noticed here:** each planner call re-enters `_resolve_stages`, so a manifest that is
   over the operator's budget emits its `stage_timeout_over_budget` span 2–4 times per attempt
   instead of once. Diagnostic-only and unchanged by this fix, but it is the duplicate-consumer tax
   the shared derivation makes visible.]
10. **Cross-run aggregation is a list, not an overlay (P1, M).** `ui/src/panels.jsx:2319
    ::CrossRunPanel` renders per-run metric observations and explicitly disclaims the thing the row
    asked for: *"Cross-run ranking unavailable… Values below remain per-run observations"*
    (`panels.jsx:2340-2343`). `serve/routers/cross_run.py` is the governance/claims surface, not this.
11. ✅ **Fork-to-branch: the gesture EXISTS end to end; only its RunView affordance is missing (P1, S).**
    *(2026-08-14 — the three citations above were re-verified and all three were correct.)* The fused
    gesture landed as `inject_node` + a validated `forked_from` receipt, **not** as a new control event
    and not as a field on `fork`: `fork` (`EV_FORK`, `ui/src/api.js:202`) means "Researcher, improve
    this node" and carries no idea, while `inject_node` already transports an operator-authored Idea,
    a parent and the `parent_generations` CAS that fences it. So no row was added to
    `control_validation.py`'s five tables — the change is one key on `CONTROL_DATA_FIELDS[inject_node]`
    plus `_normalize_fork_receipt`, which validates `{node_id, generation, observed_seq}` and STAMPS
    `{changed_fields, base_idea_digest}` (refusing a payload that supplies either). It flows to
    `node_created` → `Node.forked_from` and survives the review projection. Driven both ways by
    `tests/test_fork_from_seq.py` against a real paused run through the real HTTP surface, with the
    real engine building the node. The fence is a CONTENT CAS and deliberately not a tail one — see
    `docs/guide/concepts.md` §"Branching from a snapshot".
    **[2026-08-14 — CLOSED. The panel landed the same day.]** All four steps: (a) `'fork'` is in
    `RunView.jsx::HISTORY_SAFE_PANELS`, and it belongs there by that list's own rule rather than as an
    exception to it — everything the form sends derives from the exact fold on screen, so it cannot
    make the `seq + panel` hybrid the list exists to prevent; (b) the narrow exception is
    `forkFromSeqModel.js::readOnlyNodeActionRefused` + `forkGestureAccess`, i.e. the blanket refusal
    RESTATED as a function with a truth table rather than loosened — `mutationReadOnlyMode` is
    byte-identical, every other action still meets it, and review / a stale-generation link / an
    unresolved start-over / an unloaded run each still refuse the branch too; (c) the form is
    `ui/src/ForkFromSeqPanel.jsx`, seeded from `hist` and never `live2`; (d) `Dag.jsx::NODE_MENU_ITEMS`
    made the node menu a TABLE, so a historical snapshot is offered exactly ONE item and the other
    nine are absent rather than shown-and-then-refused (and "Branch from here" is `snapshotOnly`, so
    it is not on the live menu — a branch records the vantage point it was formed at). A **second**
    fence had to admit it and is named at both ends: the client's own run-access envelope
    (`runMode.js::assertRunMutationAllowed`) would otherwise stop the request leaving the browser, so
    `CONTROL.forkFrom` is the one durable run command that passes `allowRunMutationModes: ['history']`
    — `resetRun` is the seam's only other caller and is deliberately wider because Start over is what
    RESOLVES the states it runs in. The browser now reads the refusal asymmetry the python tests
    pinned: a stale parent arrives as a 409 on `/control` and as a **rejected record** on `/commands`,
    both proving nothing was appended, and `forkRetryable` turns that into three affordances — fix and
    resubmit, fence and offer a re-read, or fence with no retry at all when the outcome is unknown.
    Driven by `ui/test/forkFromSeqPanel.test.js` (9 tests) beside the model's own 7. The blocker that
    deferred it was removed rather than waived: the tree is built into a STAGING dir
    (`vite build --outDir .dist.stage`) so `ui/dist/assets` is never emptied under a live
    `test_server`, and `vite.ssrLoadModule` gives RunView a compile check in the suite itself.
12. **Pareto selection is display-only (P2, M).** The real non-dominated algorithm exists —
    `ui/src/panels.jsx:721::paretoFront` with `dominates()` at `:725`, over the primary metric plus
    every `extra_metric` — but grep for `pareto` across `looplab/search/` and `looplab/engine/`
    returns **nothing**. It never reaches champion selection.
13. **The feature-engineering CV gate is a sentence, not an enforcement (P1, M).**
    `engine/proposal_cues.py:231::_cue_feature_engineering` appends prose telling the model
    *"KEEP a feature only if it improves CV"*, gated by `core/config.py:643::feature_engineering =
    False`. There is no FE operator in `search/operators.py` and no `caafe` symbol anywhere. The row
    called the CV gate **mandatory**; an instruction to a model is not a gate.
14. **The time-series adapter is a synthetic toy; tabular-AutoML and multimodal do not exist (P1, M
    each).** `adapters/timeseries.py`'s own docstring (line 9) says a real AutoGluon-TS/Darts backend
    "is a drop-in replacement for the templated forecaster" — i.e. it is the template, not the
    backend. `adapters/` holds classification / dataset / mlebench{,_real} / regression / repo /
    timeseries / toytask and nothing else.
15. **Drift detection is absent (P2, M).** `trust/leakage.py` DID go past exact-match —
    `code_leakage_scan` (`:147`, self-described "static-dataflow-lite": preprocessor fit on full data
    before the split, `.fit()` on test data), plus `target_leakage` and `temporal_leakage`. But every
    `drift` hit in `looplab/` is code/schema-drift prose or confirm-phase seed variance
    (`engine/confirm_phase.py:273`), never a distribution-shift detector.
16. **MLflow is manual export, not autolog; there are no data connectors (P2, S–M).**
    `events/mlflow_export.py::export_run` + `cli/export_cmds.py:93` ship a per-run push; grep for
    `autolog` across `looplab/` is **empty**, and there is no `DataConnector`/`connector` symbol.
    (Notebook export DID ship: `events/notebook.py::champion_notebook`, `export_cmds.py:108`.)
17. **The MCTS tree has no LLM value estimate and no reflection (P2, M).**
    `search/policy.py:393::MCTSPolicy` is classic UCB1 (`:475-478`) with reward folded straight from
    the metric (`_mcts_reward`, `:374`). No `lats.py`, no LLM valuation, and it is not wired to
    `search/graded_novelty.py` / `novelty_recall.py` / `taxonomy_dedup.py`, which exist independently.
18. **Parallel eval is in-process only (P2, L).** `engine/evaluate.py:1375` takes an
    `anyio.CapacityLimiter` and `orchestrator.py:1503,2383` open task groups; there is no `ray`,
    `celery` or `dask` anywhere and no cross-machine dispatch. The budget-guard half of the row DID
    ship (`engine/widths.py::EVAL_WIDTH_MAX` enforced at `orchestrator.py:2966`;
    `engine/proposal_cues.py:425::per_experiment_gpu_budget`).
19. **[added 2026-08-14] Claim ratification ignores node feasibility and trust flags (P1, S).**
    `trust/memo_verify.py:209::finalize_verified_evidence` re-checks a cited node's LIFECYCLE only
    (exists, not tombstoned, not aborted, terminal status, stable attempt) — grep for
    `feasible|metric_salvaged|flagged` across `memo_verify.py` returns nothing — so a D8 research
    claim can ratify `supported` on a salvaged or reward-hacked node's number into the cross-run
    `research_claims.jsonl` (`engine/claims.py`), where a later run retrieves it as evidence.
    CLAUDE.md's engine map records this as "STILL OPEN". **Cost:** the same leak
    `engine/memory.py::unreliable_metric_ids` closed for lessons/skills on 2026-08-13, one store
    over. **Fix:** reuse that exact join — `engine/metric_salvage.py::metric_unmeasured` ∪
    `events/replay.py::flagged_node_ids` — and refuse/downgrade a `supported` verdict whose cited
    node is in the set, stating the withheld reason in the claim row. Ranked here (not top-5) only
    because D8 claims are advisory retrieval input, not selection machinery.

### §0.2 Low-cost residue (open, but cheap to keep open)

- **The `engine` extra-metric channel authenticates against bytes the candidate authors** (found
  2026-08-14 auditing the merge day against docs/36). `core/calibration.py:221
  ::engine_declared_extra_metric_keys` grants `EXTRA_METRIC_ENGINE` — a member of
  `EXTRA_METRIC_AUTHENTICATED` — on `code.startswith(SPECULATION_CUDA_PROBE_CODE_PREFIX)`, and
  `runtime/sandbox.py:1471`/`:1541` pass the candidate's own `solution.py` verbatim as that `code`.
  The prefix is a public constant in the shipped tree, not a secret: driven, `tools/env_inspect.py
  ::read_installed("looplab.core.calibration", 57, 14)` pages it out byte-for-byte with no package
  allow-list, and `CliAgentDeveloper` (`agents/cli_agent.py:220`) is an external coding agent with a
  real shell whose output IS `solution.py`. **Reproduction:**
  `stdout_extra_metric_channels({"device_count": 8.0, "alloc_bytes": 4096.0, "my_auc": .99},
  SPECULATION_CUDA_PROBE_CODE_PREFIX + '\nprint({"metric": .99, "device_count": 8})\n')` returns
  `{'device_count': 'engine', 'alloc_bytes': 'engine', 'my_auc': 'auto'}`, and
  `authenticated_extra_metrics_only` then keeps the first two under `auto_extra_metrics=false`.
  **Bounded, and the bound is why this is residue rather than a P0:** it reaches only the probe's
  four key names, it needs the prefix to actually EXECUTE (so a CUDA device), `extraMetricIsDeclared`
  still answers false for `engine`, and nothing here touches the primary metric, the champion,
  `feasible_nodes` or a violation. What it costs is the sentence the channel exists to make true —
  the UI prints "Printed by LoopLab's own instrumentation … and verified against it" over a number
  an agent printed, and the operator's own "authenticated only" switch admits it. **Not fixed
  tonight on purpose:** the obvious narrowing (also require the exact key SET and the three static
  values, as `speculation_quality.py:554-558::_validate_cuda_probe_artifact` does downstream) would
  change what a partially-reporting calibration node is tagged, and under `auto_extra_metrics=false`
  that strips the probe keys the receipt gate re-derives from `node_evaluated` — i.e. it can revoke
  issued calibration receipts, which is exactly the careless RECORD-side change docs/36 warns about.
  The honest alternative is to stop deriving the tag from the artifact's CONTENT and carry
  "the engine spliced this" from the splicer, which needs a channel `runtime/` does not have today.

- **The log-integrity receipt counts LINES and publishes them as RECORDS, and one line is up to 4096
  events** (found 2026-08-14 auditing f78961a4). `eventstore.py:399::log_divergence` counts non-blank
  complete LINES; `log_integrity` publishes those as `good_records`/`dropped_lines`, and
  `integrity_sentence` (`:482-489`) and its browser mirror `ui/src/runIndex.js:45-56` both render
  them as "records visible to replay" / "durable record(s) … NOT included". A batch envelope written
  by `EventStore.append_many` carries up to 4096 events on ONE line, and `append_many` is on live
  engine paths (`engine/audit.py:165`, `card_reservation.py:1131/1678/1822`, `orchestrator.py:3828`,
  `speculation.py:1241/1994`) — i.e. every card-lane run. **Reproduction** (shipped writer, shipped
  readers, driven): one `run_started` + three `append_many` batches of 5 + one `resume` = 5 physical
  lines / **17 events**; delete one middle batch line; a fold then sees **6 events** and 11 are
  invisible, while the receipt says `{good_records: 2, corrupt_line: 3, dropped_lines: 1}` and the
  sentence every CLI and UI surface prints reads *"only 2 of 4 records are visible to replay and 1
  durable record(s) behind that boundary are NOT included."* One missing record claimed, eleven
  actually missing. **Cost:** this is the number whose entire job is to state the size of the loss,
  and it understates it by the batch factor on exactly the runs that batch. An operator who reads
  "1 record" concludes the boundary is cosmetic and goes on trusting `nodes`/`best_metric`; the
  commit's own comment ("two surfaces disagreeing about the size of the log is the original defect
  in miniature") describes what it reproduced. **Not fixed tonight:** counting events means decoding
  each line behind the boundary and changes the wire MEANING of three fields across CLI, HTTP and
  the UI at once, with `runIndex.js`'s `good + dropped + 1` arithmetic and the `corrupt_line`
  (a genuine line number) having to move in the same change. Two smaller relatives found beside it,
  worth folding into the same pass: the boundary row itself is a durable record replay excludes and
  is counted in neither number (`good + dropped + 1` is the denominator, so the sentence's own
  addition is one short); and `unreadable: True` is unreachable through every shipped surface —
  `run_projections.py:147`'s `except Exception: continue` DROPS an unreadable run from `/api/runs`
  entirely, so it reads as deleted rather than as "incomplete record", which is the opposite of the
  direction `appstate.py:275-280` says it fails toward.

- **The read-model watermark's digest covers `(seq, type)` only, so a content-edited log certifies
  as `current`** (found 2026-08-14 auditing 1bfd3634). `readmodel.py:91::coverage_watermark` hashes
  `[[seq, type], …]` and nothing about event DATA or run generation. **Reproduction** (driven):
  build a readmodel over a 3-event log whose `node_evaluated.metric` is `53.05` (`nodes` row
  `(0, 53.05)`), then edit that metric to `42.0` in place leaving seq and type untouched —
  `readmodel_status` still answers `current`, `readmodel_is_current` still `True`, the digest is
  byte-identical, and `fold()` of the same log now returns `42.0` while the certified projection
  still says `53.05`. The module states what it hashes, so this is not a lie; the JUSTIFICATION is
  the part that fails — "deterministic over the authenticated log: the bytes are immutable once
  appended" (`:88-90`) — because the one divergent run in this corpus is a HAND-EDITED log, and the
  commit merged one hour later (f78961a4) exists precisely because a log was edited. The same gap
  covers identity: no generation token is in the preimage, so a reset producing an equal-length,
  same-`(seq,type)` prefix certifies generation A's projection as current for generation B. **Cost:**
  `readmodel.sqlite` is what external queries read and the code promises `ORDER BY metric` agrees
  with `is_best` — a `current` stamp over a stale metric is a wrong champion presented as certified.
  **Not fixed tonight:** adding a data digest or the generation token to the preimage changes the
  digest of every readmodel already on disk, so all of them read `stale` until rebuilt — a migration
  call, not a patch. Everything else about this artefact is fail-closed and driven clean: body and
  watermark come from ONE materialized `rows = list(events)` (`:167-169`) in one transaction, so they
  cannot name different sets; and an absent/duplicated/unparseable/unknown-version watermark, or an
  empty digest on either side, all answer `unknown` and never `current`.

- **The repair-rationale intake cap was raised at the wrong layer, so `_TRIAGE_RATIONALE_CAP` never
  binds** (found 2026-08-14 auditing 4b2bd547). That commit moved the intake bound to
  `crash_repair.py:60::_TRIAGE_RATIONALE_CAP = 2000` and applies it at `_ask_triage` (`:250`, `:261`,
  `:277`) — i.e. to what the duck-typed `triage_crash` seam RETURNED. `UnifiedAgent.triage_crash` is
  the ONLY implementation of that seam in the tree, it is the shipped default
  (`Settings.unified_agent = True`, `config.py:1518`; `factory.py:311-313` returns it as both roles
  under `backend="llm"`), and its own emit finalizer already cut the text:
  `agents/unified_agent.py:447` — `str((args or {}).get("rationale", ""))[:300]`. So the 2000-char
  bound is applied to a string that is at most 300 chars, and the extractor
  `evaluate.py:2275::verify_repair(triage.get("rationale", ""), …)` reads exactly the truncated half
  the commit set out to stop it reading. **Reproduction** (driven on this tree, no model):
  `claimed_tokens(full)` on a 504-char diagnosis-first/`Fix:`-last rationale returns
  `('train_cfg.yaml','KeyError','nll_cos','kl_div','log_target','rdrop_alpha','train_cfg',
  'log_target=True')`; `claimed_tokens(full[:300])` returns `('KeyError','nll_cos','kl_div',
  'log_target','log_target=True')` — every token naming what the repair actually CHANGED is gone and
  only the cited baseline survives. That is the v7-node-0 shape the commit quotes, still live.
  The new end-to-end test cannot see it: `tests/test_repair_verification.py:41-56::_Judge` is wired
  directly as `researcher=` and never traverses `UnifiedAgent._finalize`. **Cost:** a repair that did
  exactly what it promised keeps being stamped `unmet` on the durable `node_repaired.verified`
  column and the stop judge keeps being told "the engine could not find what this fix said it would
  change". The corpus figure the commit quotes ("83 of 123 rationales stored at exactly 300") is
  equally explained by THIS cap. **Candidate fix, and why it was not applied tonight:** widen or drop
  `unified_agent.py:447`'s `[:300]`. Blast radius is contained — every durable sink clips
  independently at 300 (`evaluate.py:2291` `node_repaired.rationale`, `evaluate.py:2657`
  `node_failed.triage_rationale`, `crash_repair.py:353` the critic), so no durable bytes move — but
  it does shift the distribution of a RECORD-side verdict column, `agents/` sits below `engine/` so
  the constant cannot be imported at module scope, and the sibling cap at `unified_agent.py:593`
  needs the same call made deliberately rather than by symmetry.

- **`write_file`/`edit_file` route around the stage-timeout-vs-budget refusal** (found 2026-08-14
  auditing 8461ff43). `repo_write_tools.py:434` applies `stage_budget_refusal` inside
  `_declare_stages` only; `_write` (`:622-643`) and `_edit` (`:645+`) apply the syntax and
  manifest-collision rules and no budget rule. Driven on one `RepoWriteTools(time_budget=21600.0,
  surface=['**/*','*.json'])`: `declare_stages(train timeout=172800)` is refused and stages nothing,
  then `write_file('looplab_stages.json', <the same manifest>)` writes it and the staged manifest is
  over budget at consume; and an ACCEPTED `declare_stages(timeout=100)` followed by
  `edit_file('looplab_stages.json', '"timeout": 100' -> '"timeout": 172800')` lands 172800 too.
  Bounded by the editable surface — the default `["**/*.py"]` correctly refuses the `.json`
  (driven) — but `repo_task.py:983` records `edit_surface: ["**/*"]` as a configuration really used
  on this box. **Related, and stated rather than filed as a second row:** `eval_stages.py:188-193`
  DOES notice the divergence at consume time, and records it as a `tracing.operation(…,
  enforced=False)` span whose own comment calls that "the FACT, into the record". `spans.jsonl` is an
  explicit sidecar — not `events.jsonl`, not rebuilt by replay, absent from every export,
  `looplab timings` and the node detail, and destroyable by the UI's trace clear — so an operator
  looking for the divergence in the authoritative record finds nothing, while the metric the
  8x-overspending eval produced stays selectable and can become champion.

- **The assistant's containerized shell is unhardened** (found 2026-08-14 while wiring
  `sandbox_readonly_rootfs`). `tools/shell_tools.py:213` builds its OWN
  `make_docker_wrap(root, image, network="none")` and passes neither `mem`/`cpus` nor
  `readonly_rootfs`, so none of the container tier's limits reach it. Reachable — `serve/assistant.py`
  constructs `ShellTools` for the operator's chat assistant (NOT the Developer, which has
  `tools/dev_probe.py`'s Python probe and no shell at all). Lower severity than a candidate's code for
  that reason: it runs under a trust mode with an approver rather than as untrusted output. The fix is
  a constructor parameter plus settings plumbing that `mem`/`cpus` also lack today, which is why it was
  left rather than half-wired.

- **Three spellings of the RunResult timeout-nulling** — `runtime/sandbox.py:1314-1320`
  (`SubprocessSandbox`), `sandbox.py:1375-1381` (`DockerSandbox`), `runtime/command_eval.py:2286-2304`
  (a third spelling, `if not to` guards instead of the sandboxes' ternaries). They now cross-reference
  each other in comments, which is what makes this cheap rather than dangerous.
- **Two copies of the socket-shutdown idiom** (was three) — `core/llm_streaming.py:58-61` and `:166-169`.
- **The launch-readiness gate is still two copies** — `adapters/repo_task.py:725-729
  ::EvalSpec._command_or_stages` and `serve/tui_format.py:140-171::spec_ready`, whose own docstring
  (`:141-143`) points at this backlog row. There is no `/api/validate`; `adapters/tasks.py:336
  ::validate_task` is a different operation (it constructs a real adapter for a run).
- **Per-attempt stage accounting** — the reported failure mode is closed but the accounting is not; see
  the D5 row.
- **The literal read-only eval mount** — superseded in intent by three newer rungs; see the B1 row.
- **Surrogate is k-NN/IDW, not TPE/RF** — see the A2 row; a design substitution, not a gap.
- **`_shutdown_pool_sockets` is still pool-wide** — but now gated by
  `core/llm.py:899-907::_pool_teardown_is_safe_locked()`, so the collateral-kill cascade the §5 row
  describes can no longer fire. Only the cleanup (a per-request transport) remains.

### §0.3 Dead or moved citations found in this file (all corrected inline)

| This file said | The tree says |
|---|---|
| `server.py:245::delete_run` (`ignore_errors=True`) | **Symbol gone.** `DELETE /api/runs/{id}` is now `serve/routers/org.py:401::legacy_delete_run`, a 409 refusal; deletion is the quarantine transaction in `serve/deletion_service.py` |
| `orchestrator.py:808` (stdout tail) | `engine/evaluate.py:2445` |
| `eventstore.py:38` (`except OSError: pass`) | `events/eventstore.py:254-321::_interprocess_lock`, raising `EventStoreLockError` |
| `orchestrator.py::_resolved_stages` | moved to `engine/eval_stages.py:261` |
| `serve/tui.py::spec_ready` | moved to `serve/tui_format.py:140` |
| `core/llm.py::_raw_socket` | **deleted** (doc 25 CO-03 — no caller since the openai-SDK migration) |
| `core/llm.py:72,756` (`_shutdown_pool_sockets`) | moved to `core/llm_streaming.py:37-73`; `_nonstream_bounded` is `core/llm.py:891` |
| `search/policy.py:722` (`"bohb": _make_asha`) | `search/policy.py:696` |
| `adapters/mlebench.py:102` (`grader._Y`) | `adapters/mlebench.py:123`, and now only the synthetic fixture's `_GRADER_TEMPLATE` under `host_graded=False` |
| `stage_completed` (D5) | **no such event.** The registry name is `events/types.py:224::EV_STAGE_FINISHED = "stage_finished"` |
| `developer_backend = llm \| agentless \| <agent>` | `agentless` is not in `core/config.py::DEVELOPER_BACKENDS` and no backend implements it. `llm` IS real, but as a live-SWAP alias of `default` (`core/config.py::DEVELOPER_BACKEND_ALIASES`), never a launch value. The `agents/strategist.py` branch that could never fire was removed 2026-08-14; §0.1 item 5 records what an `agentless` backend still needs |
| `trust/` owns redaction | it does not — `core/redact.py::redact_secrets`, reached via `engine/audit.py:269::_redact`. (`CLAUDE.md`'s package map still says `trust/ … redaction`.) |
| **A4 is one ID** | **a FIFTH namespace collision the caveat block does not list:** ★Shipped's *"A4 failure-reflection"* and §2's *"A4 LATS-style MCTS"* are different items. Caveat 2 lists C2/C3/C5 and the §6 `D1–D5`; add A4. |

### §0.4 Duplicates collapsed

Caveat 1 says §2 re-lists ★Shipped IDs. Re-derived, the collapse is: **§2's A0a · A0b · A0d · A1 ·
A2 · A5 · A6 · A7 · B5 · C1 · C2 · C3 · C4 · D2 · D3 · D4 · E1 · E2 · E3 · E4 · F1 · F2 · F3 · F4 ·
F6 · G3 · G5 · H1 · H2 · H3 · H4 · I1 · I2 · I3 · I4 · I5 (36 rows) collapse into the ★ Shipped
2026-06-24 roll-up** — same subject, same theme letter, and in every case the tree agrees with
★Shipped rather than with §2. Each is marked below with its re-derived status; **eight of the 36 were
overstated by ★Shipped and are re-opened as PARTIAL here** — A2, F2, H2, I1, I2, I3, I4, I5 (§0.1
items 6, 10, 13, 14, 15, 16, plus A2 in §0.2 and I5 at item 12). Three §2 rows are **NOT** duplicates and stand on their own: **A0c,
A0e, A0f** (never in ★Shipped), **C5 agentless** and **C6 ACI** (★Shipped explicitly parked C6 as
"largely covered"), and **A4**, which collides with a *different* ★Shipped A4 (§0.3).

Cross-theme overlaps the file already flagged, re-confirmed and now resolved the same way in both
places: **D3 ≡ I2** (adapters), **D4 ≡ I3-provenance** (D4 shipped, I3-drift did not), **G5 ≡ I4**
(both PARTIAL for the same reason: export yes, autolog no), **A0f ≡ E3** (both shipped as
`tools/web.py` + `tools/literature.py`), **A0e ≡ C3** (both shipped, and both superseded by the
2026-08-13 repair judgment).

---

## ★ Shipped 2026-06-24 (this session) — ~43 roadmap items, config-first, all in the UI

Branch `feat/adaptive-search-intelligence`, ~30 commits. All **config-first** (every knob in
`config.Settings` + the Settings UI), **replay-safe**, surfaced in the UI (new Strategist /
Importance / Cross-run / Collab panels, Pareto/Trust additions, chips, activity narratives, Model-card
+ Notebook exports). Full suite **413 passed, 5 skipped**; live-verified (toy, ASHA, BOHB, surrogate,
proxy, time-series, classification, a live `qwen3:30b-a3b` run, UI preview of every new panel).

- ✅ **Theme A — search intelligence (complete):** A7 Strategist · A1 ASHA · A2 surrogate · A3 BOHB ·
  A4 failure-reflection · A0a code-block ablation · A0b ensemble merge · A0d complexity cue ·
  A5 budget-aware · A6 proxy scoring.
- ✅ **Theme B — trust:** B1 host-side scoring (`host_score`) · B3 output redaction · B4+ gVisor
  hostile tier · B5 reward-hack detector. *(B6 parked per user.)*
- ✅ **Theme C — Developer:** C1 fault localization · C2 best-of-N · C3 deep repair · C4 critic.
  *(C6 ACI: largely covered by the patch-gate / whole-file-write.)*
- ✅ **Theme D:** D2 `looplab bench` · D3 classification adapter · D4 data provenance.
- ✅ **Theme E:** E1 novelty gate · E2 researcher panel · E3 literature grounding · E4 reflection priors.
- ✅ **Theme F:** F1 importance · F2 cross-run sweep · F3 model-card · F4 collab · F6 fork-to-branch.
- ✅ **Theme G:** G1 server auth token · G3 parallel-eval budget guard · G5 MLflow export.
- ✅ **Theme H (complete):** H1 guided_json · H2 schema-aligned parser · H3 per-role models · H4 ctx budget.
- ✅ **Theme I:** I1 feature-engineering · I2 time-series adapter · I3 code-leakage · I4 notebook export ·
  I5 Pareto selector.

~~**Still open** (external-infra-gated): **D1 real MLE-bench** (needs Kaggle creds + dataset download) +
the **out-of-process grader** (a careful eval-loop refactor — B1 `host_score` is the scoring primitive
it builds on). B6 parked per user decision.~~

**[corrected 2026-08-04] All three of those shipped.** `D1 real MLE-bench` →
`looplab/adapters/mlebench_real.py::MLEBenchRealTask` (`kind="mlebench_real"`), **registered** at
`looplab/adapters/tasks.py:22,93`, plus `mlebench_prep.py` and `kaggle_dl.py`; a `kaggle` alias folds to
it (`tasks.py:127-135`). **Out-of-process grader** → `looplab/adapters/mlebench_grade.py` ("the HOST
scores it with mle-bench's *real* competition grader"). **B6 holdout guard** → shipped and **ON by
default**: `holdout_fraction=0.25` and `holdout_select=True` (`core/config.py:779-786`, whose own comment
reads "D1 holdout-gated promotion (B6, Arbor-style)"), implemented by
`looplab/engine/holdout.py:59::HoldoutGrader`.

---

## 0. What concurrent sessions already shipped (verified in code, commit range `f98b1fb…42d5fc5`)

**Code-review fixes landed:**
- ✅ **C1 (partial)** — `RepoTask._eval_protected` now protects *every* file-based reader (primary
  metric + `metrics` + `constraints` + drift `cross_check`); protected-name normalization (`_normp`);
  Docker `--pids-limit 1024` on both untrusted paths. *(commit 9722226)*
- ✅ **C2 (partial)** — child process no longer inherits host secrets: `sandbox.run_argv` filters
  `SECRET_ENV`-matching vars out of the child env ([sandbox.py](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/runtime/sandbox.py)).
- ✅ **C3 (partial)** — CORS narrowed from `*` to a localhost allow-list (`LOOPLAB_UI_CORS` override);
  SPA fallback `GET /{path:path}` now resolve-guards against traversal ([server.py:739](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/serve/server.py)).
- ✅ **C4 (partial)** — `replay.fold` is idempotent for terminal node events (duplicate
  `node_evaluated/node_failed` can't inflate `total_eval_seconds`).
- ✅ **G4 (partial)** — `llm._post` now catches `URLError/HTTPError/TimeoutError/OSError` + JSON decode
  errors instead of aborting the run ([llm.py:72](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/core/llm.py)).
- ✅ **F5 (partial)** — Dock `'Reasoning'`→`'Trace'` tab regression fixed (both call sites).

**Roadmap expanded** (commits b0d7628 → 42d5fc5): ROADMAP.md + RESEARCH_NOTES.md added; **9 research
passes** (AI-ML-engineering frontier re-run verified 3-0); plan reprioritized **operators-first (A0)**,
new **B6 held-out/generalization-gap**, **A6 proxy scoring**, **Theme H** (local-LLM serving on the
5090), **Theme I** (net-new: feature-eng, adapters, data-centric, integrations). **UI parity panels**
added: live GPU monitor, policy "why-this-node" (MCTS UCB1), pending-hint feedback chip *(commit 42d5fc5)*.

---

## 1. Foundation — remaining hardening (finish before scaling)

*These gate credible benchmarks + any non-local deployment. Ordered by priority.*

> **[reconciled 2026-08-14 — this four-item cluster was six weeks stale and three of the four entries
> below understated what had shipped.** Every entry was re-derived against the tree, not against this
> file. Two are now closed, two are narrowed to the part that is genuinely still open. The general
> lesson repeats the one this file has already recorded four times: an entry that names a line number
> (`mlebench.py:102`, `orchestrator.py:808`) is the entry most likely to be dead — both of those
> citations now point at unrelated code.]

- 🟡 **P0 · B1 read-only eval mount (S–M). [corrected 2026-08-14 — the HOST-SIDE SCORING half is
  SHIPPED, twice, and this entry claimed it was unbuilt.]** `runtime/command_eval.py:427::_read_host_score`
  is a registered metric reader (`METRIC_READERS`, `command_eval.py:528`) where the candidate writes
  `predictions.json` and the HOST scores it against labels held outside the workspace — containment
  enforced at both submit time (`host_score_labels_error`, `command_eval.py:698`) and score time
  (`command_eval.py:455-480`, against the MOUNT root). Beside it, the general `host_grader()` task
  hook (`adapters/tasks.py:93`, bound at `engine/orchestrator.py:1164`) OVERRIDES the self-report on
  both eval tiers (`engine/eval_dispatch.py:732`), with a symlink confused-deputy guard on every
  candidate-written file the host reads (`engine/holdout.py:30-56`). `tests/test_host_grading.py`
  drives it with a solution that deliberately lies in stdout.
  **STILL OPEN, and it is the mount, not the scoring:** `runtime/sandbox.py:241` binds
  `-v {root}:/work` read-WRITE for both untrusted tiers — no `:ro`, no `--read-only`, no `tmpfs`, no
  separate writable `out/` (0 hits for each in that file), and `sandbox.py:214` states the intent
  explicitly. The per-source `:ro` machinery already exists at `command_eval.py:1509` for `data:`/
  `references:` binds and should be reused rather than reinvented. Also still true: the DEFAULT
  metric kind is `stdout_json` (`adapters/repo_task.py:427`), so a task declaring neither
  `metric.kind="host_score"` nor a `host_grader()` is scored entirely from candidate stdout.
- ✅ **P0 · mlebench out-of-process grader — SHIPPED. [corrected 2026-08-14 — the cited
  `mlebench.py:102` is dead; that line is now the close of an unrelated template.]**
  `adapters/mlebench_grade.py:42::grade_in_subprocess` spawns a real separate process
  (`[sys.executable, "-m", "looplab.adapters.mlebench_grade", …]`), dispatched from
  `engine/holdout.py:95`. The REAL competition adapter never copies answers into the workdir at all
  (`adapters/mlebench_real.py:155`). What remains is a DEFAULT, not missing machinery: the synthetic
  tutorial task still ships `host_graded=False` (`adapters/mlebench.py:216`), so `grader.py` carrying
  `_Y` is written into the candidate workdir on that path. `mlebench.py:199-215` argues flipping it
  buys pipeline exercise and not confidentiality — the synthetic labels are `i % 2` before a seeded
  shuffle and `tests/test_mlebench.py:151` reconstructs them from the mounted split with no answer
  key at all. **Decide that default explicitly; do not re-open this as unbuilt work.**
- ✅ **P0 · C2 output redaction — DONE 2026-08-14.** The redactor and its six wiring sites already
  existed; what was still true is that ALL of it was gated on `redact_output`, which defaults to
  `False` — so the shipped default persisted a `print(secret)` verbatim into `events.jsonl`, the
  trace, the UI node detail and every export, while ~30 sibling durable-diagnostic sites sanitized
  unconditionally through `redact_persisted_text`. The gate is now SPLIT
  (`core/redact.py::redact_output_tail`, funnelled through `engine/audit.py::Engine._redact`): known
  credential shapes and the operator's own secret ENV VALUES (`redact_env_values`, screened by the
  same `envsafe.is_secret_env` the sandbox uses to withhold a variable from the child) are masked
  always; `redact_output` now gates only the entropy pass, whose false positives on legitimate data
  hashes were the actual reason it was opt-in. Driven at the DEFAULT config by
  `tests/test_redact.py::test_default_run_does_not_persist_{a_shaped_secret_in_the_stdout_tail,an_operator_env_secret}`
  — a real subprocess eval, a planted secret, the event log read back off disk.
  *Deliberately unchanged:* `node_created.code` still carries the generated SOURCE verbatim (measured
  — a secret planted as a string literal is in it). That is the record of what ran; redacting it
  would corrupt the reproduction, and `docs/guide/configuration.md` states code/artifacts are outside
  this boundary.
- 🟡 **P0 · C3 `task_file` allow-list — DONE 2026-08-14; the AUTH half was already shipped.
  [corrected 2026-08-14 — "endpoints are still unauthenticated" was false.]** A deny-by-default owner
  middleware has existed for some time: `serve/server.py:484::_require_token` 401s every `/api/*`
  request without a valid `X-LoopLab-Token` (constant-time compare at `server.py:449`), with exactly
  three exemptions (`/api/health`, `/api/auth/status`, and the GET-only shared-assistant family,
  `server.py:269-289`) — none of them mutating. All 64 mutating routes are covered by prefix, plus
  CSRF (`_reject_cross_origin_mutation`) and DNS-rebinding (`_reject_untrusted_host`) guards that
  apply regardless. **Exposure, stated plainly:** the server binds `127.0.0.1` by default
  (`server.py:764`, `cli/ui_cmds.py:24`) and Compose publishes to loopback (`docker-compose.yml:100`),
  so on the supported single-operator path the unauthenticated default is DEFENCE IN DEPTH. On a
  shared JupyterHub origin with no token it is an OPEN DOOR to any same-origin page, and the server
  already logs exactly that at startup (`server.py:470-481`). The remaining opt-in-ness of
  `LOOPLAB_UI_TOKEN` is a deployment decision, not missing code.
  **[2026-08-14 — that last sentence was the wrong call and is reversed: `serve/owner_token.py`.**
  "A deployment decision" is a defensible framing for a knob whose two settings are both reasonable
  defaults on the SAME deployment; it is not one when the server can already tell the two deployments
  apart. It detects the shared origin well enough to log the OPEN DOOR sentence by name — so on that
  origin the unset token now mints one and fails closed, while the private origin keeps the open
  default unchanged. See §0.1 row 1.]
  The `task_file` half of the claim WAS accurate and is now fixed: `serve/launch.py::_confine_task_file`
  refuses any `task_file` that does not resolve under a declared root (`task_file_roots` — repo
  `examples/`, the run root, `$LOOPLAB_TASKS_DIR`), which is the SAME derivation `GET /api/tasks`
  builds its pick-list from, so the catalogue and the launcher cannot drift. Resolve-then-contain
  makes a symlink out of an allowed root fail; containment runs BEFORE `exists()` so the pre-existing
  path-echoing `task_file_not_found` message can no longer be an oracle (`expandvars` on caller text
  meant `task_file: "$SOME_API_KEY"` echoed the secret's value back). Driven by six real requests in
  `tests/test_launch_preflight.py`. Note this bounds the AUTHENTICATED operator — a token never fixed
  it. **[2026-08-14 — the roots were re-derived against this box rather than assumed.** The four real
  task files here are `/home/jovyan/data/*-task.json` and none of them is under a declared root — but
  all four were launched with `looplab run <path>`, which reads its argument directly and never
  enters `launch.py`, and every HTTP-launched run in the corpus with a `ui_meta.json` carried an
  INLINE task (no `source_task_file` key exists anywhere in `runs/`). So the list refuses no launch
  that has happened. It is deliberately NOT widened to the data mount — that would admit datasets,
  model caches and every co-tenant's files, i.e. an allow-list that allows almost everything — and
  `LOOPLAB_TASKS_DIR` now takes an `os.pathsep`-separated LIST instead, because "I have tasks in two
  directories" is exactly the pressure that ends with an operator declaring their whole disk. Also
  recorded in `task_file_roots`' own docstring: this is NOT `runtime/read_allowlist.py`'s question —
  that one derives an eval's reads from an ALREADY-ADMITTED task's mount declarations, and using the
  same shape here would be circular, since the document being decided about would supply the rule
  that admits it.]

- 🟡 **P2 · B1 host-side scoring + read-only eval mount (S–M).** Mount inputs `-v root:/work:ro` +
  separate writable `out/`; candidate writes `predictions.json`, host scores it. *Closes the rest of
  C1 — self-reported metric is still trusted on the default path.* → `command_eval.py`, `sandbox.py`.
  **[2026-08-14 — PARTIAL; the scoring half SHIPPED, the mount half was SUPERSEDED.** Host-side
  scoring is real and wired: `runtime/command_eval.py:427::_read_host_score` / `:803` `host_score`,
  registered in `READERS`/`READER_PATH_KEYS`, driven by `TaskAdapter.host_grader()`, and
  `engine/eval_dispatch.py:729-732` makes the host score **replace** the self-report where a task
  exposes one. The literal `:ro` mount never happened — `runtime/sandbox.py:241` still mounts
  `-v {root}:/work` and `--read-only` appears nowhere. It is superseded rather than open, by three
  2026-08-13 rungs that answer the same question better: `runtime/read_fence.py` (the CPython audit
  hook refusing reads of the editable source tree, `Settings.read_fence` = deny by default),
  `runtime/read_allowlist.py` (the ONE mount-derived allow-list of what an eval may read) and
  `runtime/landlock.py` (the kernel ruleset, `Settings.landlock`, OFF). Priority dropped P0→P2: what
  is left is the container-FS write side, tracked on the B4 row.]
- ✅ **P0 · mlebench out-of-process grader (M).** Grader/`_Y` answer key still runs *in the candidate's
  interpreter/workdir* (`adapters/mlebench.py:102`) — `import grader; grader._Y` leaks
  labels. Grade in a separate process; labels never on the candidate FS. *(self-admitted caveat → close it).*
  **[2026-08-14 — DONE.** `adapters/mlebench_grade.py::grade_in_subprocess` grades in a separate
  process via `runtime/sandbox.py::run_argv`, and it is the **only** grading path in
  `adapters/mlebench_real.py` (zero `_Y` hits there). The cited line moved: the embedded `_Y` is now
  `adapters/mlebench.py:123` and survives only inside `_GRADER_TEMPLATE`, i.e. the **synthetic
  fixture** under `host_graded=False`; `host_graded=True` goes through the B1 `host_score` path. The
  adapter's own header (lines 15-27) records that as a fixture caveat, not a live label leak.]
- 🟡 **P0 · C2 output redaction (S–M).** Env is filtered, but `stdout_tail = res.stdout[-500:]` is still
  persisted **verbatim** (`engine/evaluate.py:2445`) — a `print(secret)` or
  traceback still leaks into the event log/UI. Add a redaction pass (regex + entropy) before write.
  **[2026-08-14 — PARTIAL, and this is SURVIVOR #2.** The pass shipped —
  `engine/audit.py:269::_redact` → `core/redact.py::redact_secrets`, wired at
  `engine/evaluate.py:2445` — but `core/config.py:920` is `redact_output: bool = False`, so on the
  DEFAULT path the tail is still written verbatim exactly as this row describes. Two further
  `stdout_tail` producers never reach `_redact` (`agents/cli_agent.py:349,370`). The original
  citation `orchestrator.py:808` is dead; the site is `evaluate.py:2445`. Note redaction lives in
  `core/redact.py`, not in `trust/`.]
  **[Superseded later on 2026-08-14 — CLOSED by `dcb4c9a` (the `redact_output_tail` split); see the
  reconciled ✅ C2 row above and the note on §0.1 item 2. Only the `cli_agent.py:349,370` residual
  survives.]
- 🟡 **P0 · C3 auth token on mutating `/api/*` + `task_file` allow-list (S).** CORS+SPA are fixed, but
  endpoints are still **unauthenticated** and `task_file` from the request body is executed without an
  allow-list. Add a shared-secret token + path validation.
  **[2026-08-14 — PARTIAL, and the remaining half is SURVIVOR #1.** The token shipped and is broader
  than asked (it gates ALL of `/api/*`, not only mutations): `serve/server.py:447` reads
  `LOOPLAB_UI_TOKEN`, `:451` compares with `hmac.compare_digest`, `:483-514` default-denies. But it
  is **opt-in** — unset means unauthenticated, which `server.py:479-481` warns about by name on a
  shared JupyterHub origin. The `task_file` allow-list is **STILL OPEN**: `serve/launch.py:422-426`
  resolves any path with only an 8 MiB cap (`_require_task_file_size`, `:210`) and no containment.]
  **[Superseded later on 2026-08-14 — the `task_file` half CLOSED by `dcb4c9a`
  (`serve/launch.py:232::_confine_task_file`); see the reconciled C3 row above and the note on §0.1
  item 1.]
- 🟡 **P1 · C4 finish (M).** Idempotent fold ✅; still TODO: **read/enforce `Event.v`** (a v2 log read
  by v1 silently mis-folds) + **fail-loud append lock** (still `except OSError: pass` →
  `events/eventstore.py:38`) + a real multi-process append-race test.
  **[2026-08-14 — PARTIAL, 2 of 3 done.** `Event.v` IS enforced:
  `events/eventstore.py:166-168` raises `UnsupportedEventVersionError` on an unsupported (or bool)
  `v`. The append lock IS fail-loud: the cited `except OSError: pass` at `eventstore.py:38` is gone,
  replaced by `_interprocess_lock` (`eventstore.py:254-321`) raising `EventStoreLockError`/`OSError`
  for `required=True` callers and for any lock-path failure. **The multi-process append-race test is
  STILL OPEN** — the suite has source/AST parity (`tests/test_append_critical_section_parity.py`) and
  monkeypatched-failure simulations, but no real concurrent-process race. SURVIVOR #8.]
- 🟡 **P1 · C5 read-model integrity (M).** SQLite rebuilt only at exit, non-atomically, no seq
  watermark, never refreshed for post-run control events → can diverge undetectably. Rebuild to temp +
  `os.replace`; stamp max `seq`; refresh on append. → `readmodel.py`.
  **[2026-08-14 — PARTIAL, 1 of 3 done; SURVIVOR #4.** Atomicity landed, but at the CALL SITE, not in
  `readmodel.py`: `engine/finalize.py:479::_build_readmodel_atomic` builds into a tempfile and
  `os.replace`s it, called from `finalize.py:912` (`finalize_run`) — its only caller. `readmodel.py`
  is 55 lines and still exposes only `build_readmodel`: **no seq watermark, no refresh-on-append, and
  still exit-only.**]
  **[2026-08-14, later the same day — CLOSED, on 2 of the 3 asks.** The watermark landed IN
  `readmodel.py` (fail-closed `readmodel_status`/`readmodel_is_current`, read-only URI open), the
  atomic publish moved down beside it as `publish_readmodel` so the engine and the new
  `looplab readmodel RUN_DIR [--check]` share ONE spelling, and exit-only is gone. **Refresh-on-append
  was rejected on measurement, not skipped** — the artefact has no programmatic reader anywhere in
  `looplab/`, and the parse it would save is already incremental inside `EventStore`. Full rationale,
  numbers and residue on SURVIVOR #4 above.]
- ✅ **P1 · G4 finish (S–M).** LLM `_post` ✅; still TODO: reuse `_kill_tree`/process-group in
  `cli_agent` (timeout orphans grandchildren) + guard `choices[0]` envelope.
  **[2026-08-14 — DONE, both halves.** `agents/cli_agent.py:345-352,379` imports and calls
  `sandbox._kill_tree` on the timeout and exception paths. Every `choices[0]` access in `core/llm.py`
  (`:1570,1600,1846,1876,1895`) sits behind a guard — the shared `_post` raises `LLMError` at
  `:1382-1384` before returning an empty-choices body, and the SDK path re-checks at `:1844,1874`.]
- 🟡 **P1 · B4 sandbox hardening (M).** `--pids-limit` ✅; add `--read-only`+tmpfs, `--memory`/`--cpus`,
  `--cap-drop ALL`, `--user`, `no-new-privileges`; Windows Job Object for atomic tree-kill; bounded
  in-flight output (kill-on-exceed).
  **[2026-08-14 — PARTIAL; SURVIVOR #7.** Shipped at `runtime/sandbox.py:230-238`: `--pids-limit
  1024`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory`, `--cpus`. Bounded
  in-flight output shipped as memory-bounded truncation rather than kill-on-exceed (`_tee_drain`,
  rolling collapse at `max(max_output_bytes*4, 256_000)`). **Still open:** `--read-only` + tmpfs
  (zero hits in the tree) and an atomic Windows Job Object (`sandbox.py:1275` is `taskkill /F /T`).
  `--user` is now a **deliberate non-goal**, documented in place at `sandbox.py:213-215` — the
  host-owned bind mount needs write access — so treat that sub-item as resolved-as-kept, not open.]
- ✅ **P2 · B4+ true-isolation tier (L).** gVisor/Kata/Firecracker microVM (`hostile` tier) — verified
  (3-0): shared-kernel hardening is *not* an isolation boundary for untrusted LLM code.
  **[2026-08-14 — DONE and real, not a stub.** `Settings.trust_mode="hostile"` passes
  `--runtime runsc` (gVisor) through `runtime/sandbox.py:1395-1398`, selected at
  `engine/eval_dispatch.py:646`, straight into `docker run --runtime`.]
- ✅ **P2 · F5 remaining UX debt (S).** `delete_run` still `ignore_errors=True` (silent partial-delete);
  `layoutWithGroups` cycle guard; SSE/Dock O(n²) full-log
  refetch per tick; SSE `JSON.parse`/listener-leak guards; `RegistryPanel` min/max sort by direction.
  **[2026-08-14 — DONE, all five.** (1) **`delete_run` no longer exists.** The bodyless
  `DELETE /api/runs/{id}` is now `serve/routers/org.py:401::legacy_delete_run`, which *refuses* with
  409 `deletion_identity_required`; real deletion is the quarantine transaction in
  `serve/deletion_service.py`, which never `rmtree`s the source — it RENAMES entries into a
  quarantine (`durable_no_replace_rename`, `deletion_service.py:138,300`) and, as of 2026-08-13
  (`9156ce5a`), finishes an interrupted move via `_absorb_quarantine_residue` (`:209`) instead of
  answering `409 delete_quarantine_conflict` retryably forever. `ignore_errors=True` appears nowhere
  on the deletion path. (2) cycle guard: `ui/src/layout.js:33`. (3) the Dock timeline is paged —
  `ui/src/Dock.jsx:784`, *"paged timeline O(visible events) and avoids folding or transferring the
  whole run trace"*. (4) `ui/src/hooks.js:459` parses inside a `try` and a bad frame calls
  `rejectLiveStream()`; there is no `EventSource` left to leak listeners on — the stream is
  `fetchEventStream` under an `AbortController`. (5) `ui/src/panels.jsx:2192-2200` ranks through the
  list view's direction-aware `sortRuns` comparator, with the `direction: 'min'` bug named in the
  comment.]

---

## 2. Capability roadmap — flat checklist (Themes A–I)

### Theme A · Operators & search intelligence  *(do A0 first — operators are the verified bottleneck)*
> **Principle (user decision): config-first, strategist-optional.** Every operator/policy/allocator
> below is **individually configurable** (enable/disable + params); manual control is the default.
> The optional **A7 Strategist** adapts those choices at runtime but never hides a knob.
- ✅ **P0 · A0a code-block ablation → targeted refinement (M).** Extend `_ablate` from *params* to
  *pipeline code blocks* (MLE-STAR, 64% MLE-bench-Lite). *LoopLab is one extension away.* + config knobs.
  **[2026-08-14 — DONE; collapses into ★Shipped.** `engine/ablation.py:267::_ablate_code` beside the
  param-mode `_ablate` (`:71`), sharing `_build_refine_block_child` (`:176`); knob
  `core/config.py:626::ablate_code_blocks = False` (+ `ablate_every`, `:622`).]
- ✅ **P0 · A0b real merge/ensembling (M).** Replace mean-param `merge_idea` with code-recombination +
  agent-proposed iterative ensembler (verified: no-ensemble 37.9%→43.9%; removing merge −9pp).
  **[2026-08-14 — DONE; collapses into ★Shipped.** `engine/node_build.py:87::_ensemble_idea` is the
  code-recombination path, dispatched at `orchestrator.py:5221,5403,5844`.
  `search/operators.py:21::merge_idea` still exists but is now the LEGACY mean-param arm behind
  `core/config.py:634::merge_mode` (default `"auto"`, resolved to `"ensemble"` at
  `orchestrator.py:751-754` whenever the Developer is code-generating).]
- ✅ **P1 · A0c operator-scoped memory (S–M).** sibling-recall for draft/improve, ancestral debug-chain
  for debug (port aira-dojo `MEM_OPS` shape).
  **[2026-08-14 — DONE. NOT a ★Shipped duplicate** (this ID never appeared there).
  `events/digest.py:519::sibling_digest` (docstring cites `MEM_OPS 'sibling'`) and
  `:591::ancestral_repair_chain` (cites `MEM_OPS 'ancestral'`), the latter consumed by
  `engine/crash_repair.py:332-333::_repair_error_context`.]
- ✅ **P1 · A0d complexity cue by node breadth (S, quick win).** Prompt hint keyed on child count.
  **[2026-08-14 — DONE; collapses into ★Shipped.** `engine/proposal_cues.py:78::_cue_complexity`,
  keyed on the sibling/child count at `:81-82`, behind `Settings.complexity_cue`.]
- ✅ **P1 · A0e multi-turn ReAct debug (M).** Replace one-shot `repair` with bounded act/observe loop
  (+5.5 percentile pts). *Ties C3/C5.*
  **[2026-08-14 — DONE, and then SUPERSEDED TWICE. NOT a ★Shipped duplicate.** `repair`/`repair_from`
  (`adapters/repo_developer.py:1312,1315`) route through `_run` (`:1107`) → `agents/agent.py:78
  ::run_phase` → `agents/tool_loop.py:393::drive_tool_loop`, bounded by `max_turns`/`time_budget_s`/
  stuck-detection — i.e. an act/observe loop, not one-shot. Then on **2026-08-13** the loop's STOP
  became a judgment (`engine/repair_judgment.py`, doc 36 F8) beside `engine/triage.py`'s per-failure
  verdict, and the **Debug node was deleted**: `search/policy.py:35` marks `KIND_DEBUG` HISTORICAL,
  *"the Debug node was deleted and NOTHING mints this kind any more"*, held by
  `tests/test_debug_node_removed.py`. A newer rung landed the same day —
  `engine/repair_verify.py::REPAIR_VERDICTS`, which compares what a repair CLAIMED against the bytes
  it changed and bounds an inert chain (`INERT_REPAIR_LIMIT`).]
- ✅ **P2 · A0f web-retrieval-grounded init (M, network-optional).** *Ties E3.*
  **[2026-08-14 — DONE. NOT a ★Shipped duplicate.** `tools/web.py:199::WebTools` (search + fetch,
  SSRF-guarded), behind `core/config.py:1705::web_search = False` — i.e. network-optional exactly as
  specified. Wired into the Deep-Research role at `agents/deep_research.py:545-546`; fires at run
  start via `deep_research_every` → `engine/cadence.py:106-122::deep_research_window`. **Duplicate of
  E3**, which shipped as `tools/literature.py`; both are closed the same way.]
- ✅ **P0 · A6 proxy/predictive scoring (M–L).** Early-signal scoring to kill doomed runs (KompeteAI
  6.9× faster eval = current Lite leader 51.5%). Pairs with A1 + C2.
  **[2026-08-14 — DONE; collapses into ★Shipped.** `search/proxy.py:22::ProxyScorer` (k-NN/IDW metric
  predictor over folded `RunState`), gating the full eval at `engine/evaluate.py:1447-1451`, threaded
  through `orchestrator.py:527,814,997` and `engine/speculation_gate.py:70,164,249`; knobs
  `core/config.py:843-844::proxy_scoring` / `proxy_kill_fraction`.]
- ✅ **P1 · A1 multi-fidelity racing ASHA/Hyperband (M).** Successive-halving scheduler over existing
  `eval_profile` smoke/full; emit `rung_promoted`. → `policy.py`.
  **[2026-08-14 — DONE; collapses into ★Shipped.** `search/policy.py:677::_make_asha`, registered
  `"asha"` at `:691`; `events/types.py:151::EV_RUNG_PROMOTED = "rung_promoted"`, emitted at
  `policy.py:525,589`.]
- 🟡 **P1 · A2 surrogate-guided proposal TPE/RF (M–L).** Fit `(params→metric)`; EI/UCB acquisition.
  **[2026-08-14 — SHIPPED, but as a DIFFERENT ALGORITHM; ★Shipped's ✅ overstates the row as
  written.** `search/surrogate.py:42::SurrogateResearcher` fits `(params→metric)` and acquires with a
  UCB-style distance/exploration bonus (`core/config.py:830-832::surrogate_explore`, *"UCB-style
  exploration weight"*), wired at `engine/strategy.py:446::_ensure_surrogate`. But the estimator is
  **k-NN inverse-distance-weighted**, not TPE and not a random forest, and there is no EI. Recorded
  as a deliberate substitution (zero-dep, pure Python) rather than a gap — see §0.2. Re-open only if
  a measurement shows kNN/IDW is the binding constraint.]
- 🟡 **P2 · A3 BOHB/DEHB fusion (M).** Capstone once A1+A2 land. **[corrected 2026-08-04 — the ★ Shipped
  roll-up above lists "A3 BOHB" as ✅; that OVERSTATES it.** `"bohb"` is a registry **alias for the ASHA
  factory** — `search/policy.py:722` reads `"bohb": _make_asha`, with the comment "Hence 'bohb' is an
  alias for the ASHA factory (kept exactly for compatibility)". The fusion is the ASHA racing schedule
  plus a surrogate wired in as the Researcher by the CLI; there is no BOHB/DEHB policy object.]
- 🟡 **P2 · A4 LATS-style MCTS (M).** LLM value est + reflection + novelty/dedup.
  **[2026-08-14 — PARTIAL; SURVIVOR #17. And note the ID: this A4 is NOT ★Shipped's "A4
  failure-reflection" — a fifth namespace collision the caveat block above does not list.** The tree
  search shipped: `search/policy.py:393::MCTSPolicy` ("Opt-in UCB1 tree search (I22, ADR-2)"),
  registered `"mcts"` at `:690`, classic UCB1 at `:475-478`. The three LATS ingredients did not: the
  reward is folded straight from the metric (`_mcts_reward`, `:374`) with **no LLM value estimate and
  no reflection step**, and it is not wired to `search/graded_novelty.py` / `novelty_recall.py` /
  `taxonomy_dedup.py`, which exist independently. No `lats.py`; grep for `LATS` returns nothing.]
- ✅ **P1 · A5 budget-aware proposal (S).** Surface remaining eval budget into the prompt/policy.
  **[2026-08-14 — DONE; collapses into ★Shipped, and EXTENDED 2026-08-13.**
  `engine/proposal_cues.py:90::_cue_eval_budget` and `:112::_cue_experiment_time_budget` inject the
  remaining budget into the prompt, behind `core/config.py:647::budget_aware`. The second cue reads
  `engine/shared.py:55::effective_eval_time_budget` — the docs/29 F1h landing that states the per-eval
  TIME budget to *both* roles that spend it (`7aa4cbdc`).]
- ✅ **P0 · A7 Strategist role — adaptive meta-control (M rule + M llm) (NEW, user-requested).** Optional
  LLM role that reads run state and **picks the search policy/allocator + Developer mode (agentless vs
  agentic) + operator mix** per situation; every choice is also a direct config knob (config-first).
  Emits `strategy_decision` (audit) + a "why this strategy" panel. **Ship the rule-based baseline
  first** (zero-dep, deterministic), then the LLM variant. ~~Default OFF.~~ → `roles.Strategist`,
  `make_strategist`, config ~~`strategist_backend=off|rule|llm`~~. *Pairs:* A5/A6/E4.
  **[corrected 2026-08-04 — shipped, and the documented default is backwards.** The real default is
  `strategist_backend = "agent"` (`core/config.py:755`) — the tool-using AGENTIC backend, not off — and
  there are FOUR values, `off|rule|llm|agent` (validated set in `core/config.py`'s enum table). See
  [A7-strategist-design.md](A7-strategist-design.md), which is the current design record.]
  **[2026-08-14 — DONE; re-verified, and the 2026-08-04 correction still holds exactly.**
  `agents/strategist.py:333::RuleStrategist` (the rule baseline this row asked to ship first),
  `:718::LLMStrategist`, agentic backend dispatched at `:849`. `engine/strategy.py:433` emits
  `events/types.py:149::EV_STRATEGY_DECISION = "strategy_decision"`. Default is
  `core/config.py:962::strategist_backend = "agent"`, validated set at `:1918`. **Caveat carried
  forward:** its `developer` decision has a dead arm — `strategist.py:408-409` tests for
  `"agentless"`, which `engine/strategy.py:75-77::_available_developers()` can never return. See the
  C5-agentless row.]

### Theme B · Trust & eval integrity
- ✅ **P2 · B6 held-out test + generalization-gap guard (M) — SHIPPED, ON BY DEFAULT.**
  **[corrected 2026-08-04 — this entry previously read "🅿️ PARKED IN BACKLOG (user decision) … still the
  #1 verified *unsolved* problem", which is false.]** `looplab/engine/holdout.py:59::HoldoutGrader`
  builds the deterministic holdout partition and runs the end-of-run phase that re-scores the val-top-k
  on reserved unseen rows; defaults are `holdout_fraction=0.25`, `holdout_select=True`,
  `holdout_top_k=3` (`core/config.py:779-786`; `0.0` = off). Original scope, for the record: a final
  split the search never sees; fold `generalization_gap = val − test`; flag/penalize high-gap winners.
- ✅ **P1 · B5 reward-hacking detector (M).** Flag suspicious wins (grader import, runtime writes to
  protected paths, val≠host-recompute) → `reward_hack_suspected` event in Trust panel.
  **[2026-08-14 — DONE; collapses into ★Shipped.** `trust/reward_hack.py:224::detect_reward_hacks`
  flags `grader_access` (`:28-71,108-150`) and `protected_write`
  (`engine/audit.py:214::_audit_workdir_writes`); `events/types.py:153
  ::EV_REWARD_HACK_SUSPECTED`; Trust panel at `ui/src/panels.jsx:601` (+ `narration.js:158,489`,
  `trustSemantics.js:68`); knob `core/config.py:925::reward_hack_detect`. **One of the three tells
  differs from the row:** "val ≠ host-recompute" is not a comparison — where a task exposes
  `host_grader()`, `engine/eval_dispatch.py:729-732` makes the host score REPLACE the self-report, so
  there is no mismatch left to flag; the residual heuristic is `perfect_metric`
  (`reward_hack.py:317-320`). Sidestepped by design, not missing. **Its live defect is the §6
  hardened-suite residual row (SURVIVOR #3).**]
- ⬜ **P1 · B7 claim ratification vs trust flags (S). [added 2026-08-14]** D8 memo-claim
  verification checks a cited node's lifecycle but not its feasibility or trust flags:
  `trust/memo_verify.py:209::finalize_verified_evidence` rejects tombstoned/aborted/non-terminal
  nodes yet never consults `feasible`, `metric_salvaged` or `flagged_node_ids` (0 grep hits in the
  module), so a `supported` verdict can be ratified on a salvaged or reward-hacked node's number
  into the cross-run `research_claims.jsonl` — the exact leak `engine/memory.py::
  unreliable_metric_ids` closed for lessons/skill cards on 2026-08-13, one evidence store over
  (CLAUDE.md's engine map calls this "STILL OPEN"). Fix: apply the same join
  (`engine/metric_salvage.py::metric_unmeasured` ∪ `events/replay.py::flagged_node_ids`) inside
  `finalize_verified_evidence` and downgrade/refuse `supported` with a stated reason. See §0.1
  item 19.
- *(B1/B2/B3/B4 tracked in §1 Foundation.)*

### Theme C · Reliable coding Developer (SWE-bench stack)
- ⬜ **P1 · C5 agentless mode = default repo Developer, but agentic stays configurable (M).**
  localize→generate-N→validate; more reliable/stable/cheaper than agent loop (Agentless 32% Lite @
  $0.70). Subsumes C1+C2+C4. **Keep the external coding-agent (agentic) backend as a first-class
  option** — `developer_backend = llm | agentless | <agent>`, selectable by config **or by the A7
  Strategist** per phase/node. Agentic is never removed.
  **[2026-08-14 — STILL OPEN, and the only §2 row that is both open and load-bearing; SURVIVOR #5.
  The named vocabulary does NOT exist.** `developer_backend` is real (`core/config.py:1178`) but its
  closed enum is `core/config.py:350::DEVELOPER_BACKENDS = ("default", "aider", "continue", "goose",
  "opencode")` — **no `llm`, no `agentless`**. The default `"default"` is the full agentic
  multi-phase in-house Developer (`adapters/repo_developer.py`, STAGES→PLAN→IMPLEMENT), which is the
  opposite of what this row asks for. `agents/strategist.py:408-409` used to contain the branch
  (`if "agentless" in ctx.available_developers`, commented *"only when C5 has landed"*) and it was
  **dead**: `engine/strategy.py:75-77::_available_developers()` returned `["default", "llm",
  *PRESETS]` and never `"agentless"`. Both building blocks the pipeline needs already shipped —
  localize (`engine/localize.py`) and generate-N (`search/best_of_n.py`) — so what is missing is the
  backend that composes them.
  **UPDATE 2026-08-14:** the dead branch and the three-way vocabulary disagreement behind it are
  fixed (one registry, `DEVELOPER_BACKENDS` + `DEVELOPER_BACKEND_ALIASES`, with a two-way
  source-scan guard); `llm` is now a NAMED live-swap alias of `default` rather than a bare literal in
  `make_developer_factory`. **The backend itself is untouched, and §0.1 item 5 is now the concrete
  five-part statement of what it still needs** (the composing Developer, a third `make_roles` branch,
  the registry entry + its test exemption, the knobs, and a producer that can ask for it — the LLM
  Strategist's structured schema has no `developer` field and `set_strategy` refuses one, so "by the
  A7 Strategist per phase/node" is itself unbuilt).]
- ✅ **P1 · C2 best-of-N + selection (M).** N attempts, keep best (SWE-RM best-of-k +10pts). *Depends A1/A6.*
  **[2026-08-14 — DONE; collapses into ★Shipped.** `search/best_of_n.py` ("C2 · Best-of-N candidate
  selection"), execution-free reward `_score()` at `:16`; knobs `core/config.py:1182::best_of_n`
  and `:1187::best_of_n_listwise`; wired at `agents/factory.py:483-487` (`BestOfNDeveloper` when
  `best_of_n > 1`).]
- ✅ **P1 · C1 fault localization (M).** grep/embedding localization sub-phase (reuse `RepoTools`).
  **[2026-08-14 — DONE; collapses into ★Shipped.** `engine/localize.py:33::localize` ("C1 · Fault
  localization (ADR-7, Agentless recipe phase 1)"), consumed at
  `engine/proposal_cues.py:214-223` behind `self._localize_faults`. The named symbol `RepoTools`
  still resolves — `tools/knowledge_tools.py:67`, wired at `agents/factory.py:434-435`; it was not
  renamed.]
- ✅ **P2 · C3 deep test-driven repair (M).** Feed failing-test output + minimal repro; cap depth.
  **[2026-08-14 — DONE; collapses into ★Shipped. Duplicate of A0e**, closed by the same loop.
  `engine/options.py:141::deep_repair` ("C3: structured failure-taxonomy repair context"); when on,
  `engine/crash_repair.py:424-427` asks for "a tiny reproduction/assert near the failure" and always
  feeds the failing `error` text back. Depth IS capped, and unconditionally: `_effective_repair_cap`
  (`engine/evaluate.py:128-134`) substitutes `_UNLIMITED_REPAIR_CEILING = 50` (`:99-124`) when the
  operator sets no `inline_repair_attempts`. Since 2026-08-13 a second bound sits above it —
  `engine/repair_verify.py::INERT_REPAIR_LIMIT` abandons in-node repair after two change-set-EMPTY
  repairs.]
- ✅ **P2 · C4 independent critic (S–M).** Self-consistency/critic before accept. *Ties B5.*
  **[2026-08-14 — DONE; collapses into ★Shipped.** `trust/critic.py:16::critique` ("C4 · Independent
  critic (ADR-7)"), reached from `engine/evaluate.py:760` (`critic_findings`); its findings reuse the
  B5 `reward_hack_suspected` event per its own docstring.]
- ✅ **P2 · C6 better ACI / write-over-edit (M).** Tuned edit/navigate/test interface (SWE-agent finding).
  **[2026-08-14 — DONE, and EXTENDED 2026-08-13. NOT a plain ★Shipped duplicate** — ★Shipped parked
  it as "largely covered by the patch-gate / whole-file-write", and that is now understated. The
  edit/navigate half: `tools/patch.py::SurfacePolicy` (patch gate, wired
  `adapters/repo_developer.py:30`) + `tools/edit_match.py` (tolerant SEARCH/REPLACE matcher). The
  **test** half — the one SWE-agent finding that was genuinely missing — landed as
  `tools/dev_probe.py::DevProbeTools`, the Developer's read-only PROBE: `run_probe(code)` runs a
  short program against the REAL environment while it authors, joined at
  `adapters/repo_developer.py:382` via `_scout_tools`. It is deliberately not a shell (four rules: no
  source read, no write anywhere, no new program, no GPU) because the read fence only covers `open`
  inside an interpreter. Behind `Settings.developer_probe`, which restores the old prompt
  byte-for-byte when false.]

### Theme D · Benchmarks & real tasks
- ✅ **P1 · D1 wire real MLE-bench (L).** Kaggle download + real grader. *Needs B1+B6.* Highest proof point.
  **[corrected 2026-08-04 — shipped and registered:** `adapters/mlebench_real.py::MLEBenchRealTask`
  (`kind="mlebench_real"`, registered `adapters/tasks.py:22,93`), `adapters/mlebench_grade.py` (host-side
  real grader), `adapters/mlebench_prep.py`, `adapters/kaggle_dl.py`; runbook in
  [MLEBENCH.md](MLEBENCH.md). B6 (its stated prerequisite) also shipped — see Theme B.]
- ✅ **P2 · D2 self-benchmark harness (M).** N held-out tasks per release; capability regression test.
  **[2026-08-14 — DONE; collapses into ★Shipped.** `looplab/bench.py` ("D2 · Capability
  self-benchmark harness"), exposed as `looplab bench` at `cli/export_cmds.py:66-84`.]
- ✅ **P2 · D3 more task adapters (M each).** *(overlaps I2.)*
  **[2026-08-14 — DONE; collapses into ★Shipped. Duplicate of I2** — and the two now DISAGREE, which
  is why both rows stay: the *count* asked for here shipped (8 kinds registered at
  `adapters/tasks.py:21-29`: toy, dataset, regression, code-regression, classification, repo,
  mlebench, mlebench_real, timeseries), but the three adapters **I2 names specifically** did not —
  see that row.]
- ✅ **P1 · D4 dataset/data-version provenance (S).** Pin data hashes into the run. *(overlaps I3.)*
  **[2026-08-14 — DONE; collapses into ★Shipped.** `events/types.py:85::EV_DATA_PROVENANCE`, emitted
  at `engine/orchestrator.py:3140-3147` (a sha256 of every task asset, comment: *"pin a content hash
  of every task asset/dataset into the run so a result is tied to the exact data"*), folded by
  `events/replay.py:1552::_on_data_provenance` into `core/models.py:1148::RunState.data_provenance`,
  and read back as calibration evidence at `search/speculation_quality.py:1981`. Note the scope:
  it hashes in-memory ASSETS; a repo task's mounted dataset is pinned through the workspace snapshot
  instead (`adapters/repo_task.py::DataSpec` carries no hash field of its own). The 2026-08-13
  OUTPUT-side twin is `runtime/metric_subject.py` — `eval.metric.subject` binds the recorded number
  to the sha256 + `file_identity` of the artefact it is a claim ABOUT, because measured across six
  repo runs 82/83 metrics carried no provenance at all and 2/83 were provably about bytes the node
  did not produce.]

### Theme E · Idea generation & multi-agent ideation
- ✅ **P1 · E1 novelty/dedup gate (S–M).** Embedding-similarity reject near-duplicate ideas (reuse vector store).
  **[2026-08-14 — DONE; collapses into ★Shipped.** `engine/novelty.py` ("Novelty / dedup gate
  (E1/T5)"), cosine embedding similarity; knobs `core/config.py:869-870::novelty_semantic` /
  `novelty_semantic_threshold = 0.92`, plus `:856-858::novelty_mode` / `novelty_gate`.]
- ✅ **P2 · E2 researcher panel + *empirical* ranking (M).** Small panel (≤3); rank by cheap eval/surrogate,
  **not** LLM-judge (verified: LLM-judge ≈random at ranking). Elo-tournament only as a *prior*.
  **[2026-08-14 — DONE; collapses into ★Shipped.** `search/panel.py` ("E2 · Researcher panel +
  empirical ranking (ADR-2)") ranks by an inverse-distance-weighted k-NN surrogate over observed
  history (`_predict`, `:17`) — empirical, explicitly not LLM-judge, as the row required. One
  deviation: the "≤3" cap is not enforced; `core/config.py:835::researcher_panel` is a free operator
  int. Not worth a row.]
- ✅ **P2 · E3 literature-grounded ideation (M, network-optional).**
  **[2026-08-14 — DONE; collapses into ★Shipped. Duplicate of A0f**, closed the same way.
  `tools/literature.py::LiteratureTools` ("E3 · Literature-grounded ideation (ADR-16),
  network-OPTIONAL") — arXiv search degrading to "(unavailable)" when disabled or unreachable; wired
  at `agents/factory.py:177-178`.]
- ✅ **P1 · E4 reflection-memory → priors (M).** Meta-review note distilled into next run's prompt
  (gradient-free cross-run meta-learning). *Pairs A0c.*
  **[2026-08-14 — DONE; collapses into ★Shipped.** `engine/lessons_distill.py:342::reflect_lessons`
  (delegated from `orchestrator.py:4718-4719`) distils; `engine/lessons.py:71-72` sets
  `prior_note_text`/`dev_prior_note_text` at run start ("E4: cross-run RESEARCHER prior"), refreshed
  mid-run at `:240-267`. Proof it reaches the next prompt: `engine/proposal_cues.py:241`
  (`prior_hint = self._prior_note_text`) and `engine/node_build.py:232`. **Narrowed 2026-08-13:** a
  `metric_salvaged` node's NUMBER may no longer leave the run into `lessons.jsonl` —
  `engine/memory.py::unreliable_metric_ids` joins the salvage rule with the trust gate's
  `flagged_node_ids`, so `reflect_lessons` shows such a node with its salvage condition and no
  number.]

### Theme F · Observability & researcher UX
- ✅ **P1 · F1 global hyperparameter-importance view (S–M).**
  **[2026-08-14 — DONE; collapses into ★Shipped.** `ui/src/report.js:147::hyperImportance` (real
  Pearson-r over every evaluated/feasible node, `_pearson` at `:129`), rendered by
  `ui/src/panels.jsx:2277::HyperImportancePanel` and mounted as the "Importance" panel at
  `ui/src/RunView.jsx:171`. **NB the ID:** docs/29 uses `F1a–F1h` for entirely unrelated engine work
  (cross-turn dispatch, occupancy pacing, eval time budget) — a sixth namespace over this letter.]
- 🟡 **P1 · F2 cross-run sweep aggregation (M).** Overlay runs of the same task → lab dashboard.
  **[2026-08-14 — PARTIAL; ★Shipped's ✅ overstates it. SURVIVOR #10.** The per-run surface exists —
  `ui/src/panels.jsx:2319::CrossRunPanel` — but it is a TABLE of per-run observations that
  explicitly disclaims the aggregation this row asks for: *"Cross-run ranking unavailable… Values
  below remain per-run observations"* (`panels.jsx:2340-2343`). No overlay chart, no ranking, no lab
  dashboard. `serve/routers/cross_run.py` is a different surface (governance claims/concepts), and
  the run list's global concept view (`ui/src/conceptForest.js` / `PortfolioConcepts.jsx`) is a
  concept rollup, not a metric overlay.]
- ✅ **P1 · F3 lineage/provenance export + model-card (S).**
  **[2026-08-14 — DONE; collapses into ★Shipped. Server-side symbol absent by design** — grep for
  `model_card` in `looplab/` returns nothing; it is a client export.
  `ui/src/report.js:355::buildModelCard` emits `schema_id: 'looplab.model-card'` with champion
  params/metric, lineage (`parent_ids`) and a provenance block, downloaded from
  `ui/src/Report.jsx:497`. `events/notebook.py::champion_notebook` carries the same provenance in
  its markdown header.]
- ✅ **P2 · F4 collaboration/sharing (M).** Read-only run links, annotation threads, export-to-report.
  **[2026-08-14 — DONE, all three; collapses into ★Shipped.** Read-only links:
  `serve/reviews.py` ("Persistent, revocable read-only capabilities for sharing one run",
  `ReviewStore`). Annotation threads: `events/comment_projection.py` +
  `serve/routers/collaboration.py`, rendered by `ui/src/CommentsThread.jsx` inside
  `ui/src/CollabPanel.jsx:681`. Export-to-report: `serve/routers/reports.py` +
  `serve/scope_report.py::generate_scope_report` (multi-run portfolio reports over project / task /
  super-task scopes), with the paid-action protocol in `serve/scope_actions.py`.]
- ⬜ **P1 · F6 fork-to-branch from any checkpoint (M).** Fuse time-travel + `inject_node` + reopen into
  one "branch from this seq with edited idea" gesture (top verified steering UX). *Partially in progress.*
  **[2026-08-14 — STILL OPEN; ★Shipped's ✅ is wrong for the FUSED gesture, which is the whole row.
  SURVIVOR #11.** Both primitives exist and stayed separate: `fork` (`EV_FORK`, `CONTROL.fork(rid,
  id, generation)` at `ui/src/api.js:202`) carries no edited idea, and `inject_node`
  (`serve/control_validation.py:712::_normalize_inject_node`) is a distinct manual add. There is no
  fused symbol, and the time-travel view forbids the gesture outright:
  `ui/src/RunView.jsx:1700` refuses every node action with *"Historical snapshot seq ${viewSeq} is
  read-only"*. Branching from a seq today = return to live, then inject, then re-type the idea.
  **Not to be confused with docs/29's F6**, which is the conversation-trace episode seek that landed
  2026-08-13 (`events/traceview.py::node_episodes`, `/nodes/{n}/episodes`, `?before=` via
  `events/span_index.py::_anchored`) — different namespace, different item.]
  **[2026-08-14 — the GESTURE landed; only the RunView panel remains. See §1 survivor #11 for the
  shape chosen (`inject_node` + a server-stamped `forked_from` receipt, no new control event), what
  is driven, and the four wiring steps that are left.]**
- *(F5 UX debt tracked in §1.)*

### Theme G · Scale, ops, hardening
- 🟡 **P1 · G2 replay/durability (M).** *(= C4/C5 in §1.)*
  **[2026-08-14 — PARTIAL; a pure pointer row. See the §1 C4 and C5 rows: 2 of 3 and 1 of 3.]**
- 🟡 **P2 · G3 distributed/parallel eval (L).** Worker pool + parallel-path budget guard; enables A1 at scale.
  **[2026-08-14 — PARTIAL; SURVIVOR #18. The BUDGET GUARD shipped, the DISTRIBUTION did not.**
  Parallel eval is in-process anyio: `engine/evaluate.py:1375::_evaluate(..., limiter:
  anyio.CapacityLimiter)` under task groups at `orchestrator.py:1503,2383`, each eval driving a local
  subprocess. There is no `ray`/`celery`/`dask` anywhere in `looplab/` and no cross-machine dispatch.
  The guard half is real and has grown three ways: `engine/widths.py::EVAL_WIDTH_MAX` enforced at
  `orchestrator.py:2966`, `engine/proposal_cues.py:425::per_experiment_gpu_budget`, and the
  cross-process host GPU-pool lease (`engine/resources.py`, one file per OS user). Two 2026-08-13
  landings changed the shape of the in-process half without closing this row: the eval task group
  became run-scoped so evals outlive the turn that admitted them (F1f, `Engine._eval_inflight`,
  `_refuse_finish_over_adopted_evals`), and production is now paced on OCCUPANCY rather than node
  count (`engine/cadence.py::occupancy_due` → `orchestrator.py::_occupancy_paced_creates`, the F1g
  fix for the state that cost this box 167.7 GPU-h with no evaluation running).]
- ✅ **P2 · G5 MLflow/OTLP consumer bridges (M).** *(overlaps I4.)*
  **[2026-08-14 — DONE; collapses into ★Shipped. Duplicate of I4's MLflow half**, and both are
  closed at the same place with the same caveat (export, not autolog — see I4).
  `events/mlflow_export.py::export_run` (optional dep, `available()` guard) behind
  `cli/export_cmds.py:93::export-mlflow`; OTLP is genuine OTel SDK span export at
  `core/tracing.py:118::_otel_env_requests_otlp` and `:276-298`, driven by the standard `OTEL_*`
  env.]
- *(G1 server auth, G4 client robustness tracked in §1.)*

### Theme H · Local-LLM serving & structured-output reliability (RTX 5090)
- 🟡 **P0 · H2 schema-aligned (BAML-style) parser as default (S).** Native FC collapses on small models
  (~20% vs SAP ~92–94%). Make the `baml` path a real error-correcting parser. → `parse.py`. *Cheapest
  whole-system lift; gates Themes C/E.*
  **[2026-08-14 — PARTIAL; ★Shipped's ✅ covers only half the row. SURVIVOR #6.** The parser half IS
  done and is not a stub: `core/parse.py:195::_coerce_to_model` does case-insensitive key matching,
  per-field type coercion and extras-dropping — a genuine SAP. **The "as default" half did not
  happen:** `core/config.py:1483::llm_parser = "tool_call"`, and
  `core/parse.py:213::_ORDER["tool_call"] = ["tool_call", "baml"]`, so `baml` runs only after native
  FC has already failed. That is the exact configuration whose ~20 % the row was written about, and
  this box serves local models. Remaining work is a default flip plus its blast-radius test.]
- ✅ **P1 · H1 vLLM/SGLang recipe + `guided_json` constrained decoding (S–M).** Drive structured calls
  from the Pydantic schema. → `llm.py`, `parse.py`, docs.
  **[2026-08-14 — DONE; collapses into ★Shipped.** `core/llm.py:661,756,836-837,1593-1596` sends
  `guided_json` / `response_format` `json_schema` derived from the Pydantic schema in
  `complete_tool`, behind `Settings.llm_guided_json`. The recipe half is documented at
  `docs/guide/llm-and-agents.md:183-209,468` (the SGLang/vLLM table with `--tool-call-parser` /
  `--reasoning-parser`).]
- ✅ **P1 · H3 per-role model presets (S–M).** Developer=Qwen3-Coder-30B-A3B, fast model for breadth /
  strong for depth; per-role `model`+`base_url`. → `config.py`, `tasks.make_roles`, Settings UI.
  **[2026-08-14 — DONE, all three surfaces; collapses into ★Shipped.**
  `core/config.py:1366-1380::researcher_model` / `developer_model` / `strategist_model` plus their
  `*_base_url` / `*_temperature` siblings; applied through the role→field map at
  `core/llm.py:1946-1952`, consumed by `make_llm_client_for` inside
  `agents/factory.py:294::make_roles`; present in `serve/settings_ui_schema.json`.]
- ✅ **P2 · H4 context budgeting for long traces (S).** Truncate/scoped-memory; paged-KV. *Pairs A0c.*
  **[2026-08-14 — DONE; collapses into ★Shipped.** `core/context_budget.py::truncate_history` does
  middle-truncation of long intermediate tool-loop messages while keeping the system message and the
  last N turns (`DEFAULT_SUMMARY_CHARS`, `RESULT_CAP`), consumed by `agents/tool_loop.py`,
  `agents/agent.py` and `agents/strategist.py`. Paged-KV is a serving-side concern and is not, and
  should not be, LoopLab's.]

### Theme I · Net-new capabilities (expand functional surface)
- 🟡 **P1 · I1 LLM feature-engineering operator, CV-gated (M).** CAAFE-style (0.798→0.822); **CV gate
  mandatory** (FE is non-universal). Highest net-new value for tabular. *Composes A0a.*
  **[2026-08-14 — PARTIAL; ★Shipped's ✅ overstates it. SURVIVOR #13.** There is **no FE operator** —
  `search/operators.py` holds only `merge_idea`, and grep for `caafe`/`CAAFE` across the tree returns
  nothing. What shipped is a PROMPT CUE: `engine/proposal_cues.py:231::_cue_feature_engineering`
  appends free text telling the model *"the eval's cross-validation gates them — KEEP a feature only
  if it improves CV"*, behind `core/config.py:643::feature_engineering = False`. The row called the
  CV gate **mandatory**; an instruction to a model is not an enforcement, and FE being non-universal
  is precisely why the row said so.]
- 🟡 **P1 · I2 new TaskAdapters (M each).** Time-series (AutoGluon-TS/Darts backtesting), tabular AutoML,
  multimodal. *(overlaps D3.)*
  **[2026-08-14 — PARTIAL; ★Shipped's ✅ overstates it. SURVIVOR #14. Duplicate of D3 — and the two
  disagree**, which is why both rows survive: D3's "more adapters" shipped, but **none of the three
  this row names** did. `adapters/timeseries.py` is a **synthetic toy** (exponential/seasonal
  forecaster over a generated series, MASE backtest) whose own docstring at line 9 says a real
  AutoGluon-TS/Darts backend "is a drop-in replacement for the templated forecaster" — i.e. it is the
  template, not the backend. No tabular-AutoML adapter and no multimodal adapter exist; `adapters/`
  holds classification / dataset / kaggle_dl / mlebench{,_real,_grade,_prep} / regression /
  repo{,_developer} / timeseries / toytask.]
- 🟡 **P2 · I3 data-centric (M).** Static-dataflow leakage detection (beyond exact-match), drift, provenance.
  **[2026-08-14 — PARTIAL, 2 of 3; ★Shipped's ✅ overstates it. SURVIVOR #15.** Leakage DID go past
  exact match: `trust/leakage.py` has `train_test_contamination` (exact rows), `target_leakage`
  (correlation proxy), `temporal_leakage`, and `code_leakage_scan` (`:147`), self-described
  "static-dataflow-lite" — it catches a preprocessor fit on the FULL data before the split and
  `.fit()` called on test data. Provenance shipped as D4 (`EV_DATA_PROVENANCE`). **Drift is absent:**
  every `drift` hit in `looplab/` is code/schema-drift prose or confirm-phase seed variance
  (`engine/confirm_phase.py:273`), never a distribution-shift detector.]
- 🟡 **P2 · I4 integrations (S–M).** Champion→Jupyter notebook export, MLflow autolog, data connectors.
  **[2026-08-14 — PARTIAL, 1 of 3; ★Shipped's ✅ overstates it. SURVIVOR #16.** Notebook export DONE:
  `events/notebook.py::champion_notebook` behind `cli/export_cmds.py:108::export-notebook`. MLflow is
  **manual per-run export**, not autolog — `events/mlflow_export.py::export_run`; grep for `autolog`
  across `looplab/` is empty (duplicate of G5, same caveat). Data connectors do **not exist**: no
  `DataConnector`/`data_connector`/`connector` symbol anywhere.]
- 🟡 **P2 · I5 true Pareto / cost-aware (M).** Non-dominated-set selector over `extra_metrics` (panel exists).
  **[2026-08-14 — PARTIAL; ★Shipped's ✅ overstates it, and the row's own parenthetical was the whole
  truth. SURVIVOR #12.** A real non-dominated-set algorithm exists — `ui/src/panels.jsx:721
  ::paretoFront` with `dominates()` at `:725`, over the primary metric plus every `extra_metric` —
  but it lives entirely in `ParetoPanel` (`panels.jsx:729`), a **display** surface. Grep for `pareto`
  across `looplab/search/` and `looplab/engine/` returns **nothing**: it is not a SELECTOR, it never
  moves the champion. Compare `engine/verifier_tiebreak.py`, which is selection machinery and can.]

---

## 3. Top-of-backlog — the ordered "do next" list

> **[2026-08-14 — SUPERSEDED by §0.1.** Every phase below is now mostly ✅, so following this order
> sends the next person to work that is already done. Kept, not deleted, because it records the
> 2026-06-24 sequencing DECISION and its rationale; **§0.1 is the live ordering.** For the record,
> what survives of each phase: Phase 1 → ~~`C2 output redaction` (default is off), `C3` (the
> `task_file` half)~~ [both closed later on 2026-08-14 by `dcb4c9a` — see §1], `C4` (the race
> test), `C5 read-model`, `H2` (the "as default" half); Phase 2 →
> `C5 agentless Developer`; Phase 3 → `I1`'s CV gate, `I2`'s three named adapters, `F2`/`F6`,
> `G3`'s distributed half. `B1`, `mlebench grader`, `A0a/b`, `A6`, `A1`, `C2 best-of-N`, `A0c/d/e`,
> `B6`, `D1` and `E4` all closed.]

**Phase 1 — finish "trust the numbers" (foundation):**
`B1 host-side scoring` → `mlebench out-of-process grader` → `C2 output redaction` → `C3 auth token` →
`C4 finish (Event.v + fail-loud lock)` → `C5 read-model` → `H2 schema-aligned parser`.

**Phase 2 — "better moves, then better search" (differentiation):**
`A0a code-block ablation` → `A0b merge/ensembling` (each config-gated) → `A7 Strategist (rule baseline
→ LLM)` → `A6 proxy scoring` → `A1 ASHA` → `C5 agentless Developer (agentic kept as option)` +
`C2 best-of-N` → `A0c/d/e operator memory+cues+ReAct repair`.

**Phase 3 — "prove it & scale" (validation + reach):**
`B6 held-out + gap guard (gate for D1)` → `D1 real MLE-bench` → `I1 feature-eng operator` +
`I2 time-series adapter` → `A2/A3 surrogate+BOHB` → `E4 meta-priors` → `F2/F6 cross-run UX +
fork-branch` → `G3 distributed eval` → `B4+ microVM tier`.

**If you do only three things (user decision 2026-06-24):** **A0** (code-block ablation + real merge,
each configurable — the verified #1 lever) · **A6/A1** (proxy + ASHA — what separates the MLE-bench
leaders) · **A7 Strategist** (optional LLM meta-controller picking search algo + Developer mode,
config-overridable). *(B6 parked in backlog — high value, not top-3 now.)*

---

*Companion docs: [ROADMAP.md](ROADMAP.md) (strategy/why), [RESEARCH_NOTES.md](RESEARCH_NOTES.md)
(sourced evidence), [CODE_REVIEW.md](CODE_REVIEW.md) (foundation findings).*

---

## 4. Maintainability backlog (architecture review, 2026-07-04)

A six-subsystem architecture audit (engine / core+events / agents+search / serve /
adapters+runtime+trust+tools / repo-DX) landed the low-risk subset on branch
`claude/agent-architecture-review-t6iqkn`: the `events/types.py` registry + typo-guard test,
`core/errors.py` cycle break, `tools/_base.py` ToolProvider contract, `_shared_providers`,
hint-attr registry, policy constants/registry, orchestrator extractions (triage/lessons/finalize),
serve protocol/prompt hoists, DeepResearcher→`drive_tool_loop` merge, root `CLAUDE.md`, tests CI.

**Waves 3–5 (commits `d024f94`, `79cd990`, `2bd16b5`) then landed almost the entire remainder**,
each behavior-preserving (differential tests / route-list diffs / behavior matrices, full suite
green — 1165 passed / 22 skipped):

- ✅ **`Engine.__init__` 79-param collapse.** `engine/options.py::EngineOptions` (64 config knobs,
  `from_settings`); `__init__` keeps every kwarg (now `_UNSET`-defaulted, explicit > options >
  default); `cli.py::_engine` passthrough collapsed (net −59 lines). Differential test locks
  old-kwarg vs options equivalence.
- ✅ **`serve/server.py` router split.** 2,968 → 245 lines: `AppState` + `serve/routers/`
  (runs/org/control/genesis/assistant/boss/reports/misc) + engine_proc/jobs/settings_store/
  llm_context/artifacts modules. Route list byte-identical (75 routes). Genesis jobs unified onto
  the shared `JobRegistry`; assistant turn endpoints share `_begin_turn`/`_finish_turn`. Zero test
  lines changed (seams preserved via late binding + re-exports).
- ✅ **orchestrator step 2.** `HoldoutGrader`, `WorkspaceSeeder` (with `materialize()`),
  `ConfirmPhaseMixin`, `AblationMixin`; `_emit_node_created` unifies the 4 payload sites
  (historical key sets kept, incl. the ablate `deleted`-omission quirk, now flagged). Orchestrator
  2863 → 2358 lines. *(Not done: decomposing `run()` (~420 lines) into guarded phase methods —
  still open.)*
- ✅ **llm.py shared SSE generator.** `_sse_chunks` (+`_SSETail`); both callers keep their divergent
  merge semantics; +1 regression test.
- ✅ **trust dedup.** `trust/confirm.py::robust_selection` shared by `confirm_top_k` and the engine
  confirm tail. *(Not done: marking `cv.py`'s unwired splitters as library code — trivial, open.)*
- ✅ **SurfacePolicy** in `tools/patch.py` (reason codes; each site keeps its wording; provable
  semantic differences parameterized). RepoTask read-side normalizer left separate by design.
- ✅ **`repo_task.py` split** (946 → 395; `adapters/repo_developer.py`, re-exports kept, mega-prompt
  hoisted) + **`tools/edit_match.py`** (tolerant matcher extracted).
- ✅ **RunStateCache** (`tools/_runcache.py`) dedupes the fold-cache + traversal guard.
- ✅ **CLI polish.** `_run_engine_guarded` dedupes the run/resume error funnel; `ui_preview.py`/
  `ui_with_env.py` → `tools/`; `task_mlebench_100.json` was a byte-identical duplicate → removed.
  Pytest `live`/`docker` markers registered. *(Not done: freezing the 24-flag `run()` surface.)*
- ✅ **wrapper-forwarding mixin** (`WrapsDeveloper` in roles.py) + **prompt-store routing** (7 more
  prompts through `render(prompts, key, default)`; byte-identical defaults). *(Not done: moving the
  two researcher instruction-prose duplicates onto shared fragments — open.)*
- 🟡 **test-suite reorganization.** Codemod of all legacy flat-import test files to canonical paths
  ✅; `live`/`docker` markers ✅. *(Not done: moving the 21 accretion-named `test_review_fixes*`/
  `test_*_fixes` files into per-subject homes — higher-churn/lower-value, deferred.)*
- 🟡 **known one-off flags from the audit.** `Strategy`'s four-site cross-reference comment added
  ✅. *(Open decisions, unchanged behavior: `ToolUsingResearcher` `_sweep_hint` omission — bug or
  intent?; `command_eval` docker rc 137 vs 124; `parse._ORDER` `"outlines"` alias;
  `_PRELOAD_PRIORITY`/`_recipes` hardcoded filenames.)*

**Wave 6 (final) closed the rest:**

- ✅ **`run()` phase-method decomposition.** `Engine.run()` ~390 → ~151 lines: `_setup_phase`,
  `_reentry_repin`, `_apply_control_overrides`, `_serve_forced_requests`, `_run_cadences`,
  `_dispatch_evals`, `_skip_if_aborted` (the verbatim-duplicated abort-skip, now one helper). Pure
  mechanical; every `fold()` point, event order and `_write_lock` acquisition unchanged; loop
  control flow / terminal-event gating left inline by design.
- ✅ **`cv.py` splitter docstrings.** Module docstring now marks `kfold_indices`/
  `purged_walk_forward`/`consistent_cv`/`Evaluator` as the ADR-15 library seam (complete, tested,
  not yet wired) vs. the live `cv_summary`.
- ✅ **`run()` flag-surface freeze.** Maintainer note on the `run` command: the typed `--flag`
  surface is frozen; new engine knobs go through a `Settings` field + `-s/--set` (full parity), not
  a new `typer.Option`.
- ✅ **researcher instruction-prose fragments.** The shared hypothesis suffix extracted to one
  helper; the drifted idea-space guidance kept as two named constants (`_IDEA_SPACE_TOOL` /
  `_IDEA_SPACE_PLAIN`) — one grep target, byte-identical prompts (16-cell parity capture).
- ✅ **review-round test-file reorg.** All 12 `test_review_fixes*`/`test_*_fixes` files dissolved;
  111 tests moved verbatim into per-subject homes (+6 new subject files); the near-collision
  `test_review_fixes2.py`/`test_review_fixes_2.py` pair eliminated. Test-name multiset byte-identical
  (independently verified: 1185 = 1185), suite unchanged.
- ✅ **the one-off behavior decisions:**
  - `_sweep_hint`: **fixed** — `ToolUsingResearcher` now honors it too (additive; the strategist's
    `prefer_sweep` nudge reaches the agentic researcher, consistent with `LLMResearcher`).
  - **docker rc 137**: **fixed** — `runtime/sandbox.py::docker_timed_out(rc)` is now the single home
    of the 124-vs-137 rule; `command_eval` uses it at both eval sites (was flagging only 124, so a
    SIGKILL-escalation timeout was mislabeled OOM). Regression test added.
  - `parse._ORDER` `"outlines"` alias: kept, with its wave-1 explanatory comment (alias for the text
    path until constrained decoding lands).
  - `_PRELOAD_PRIORITY`/`_recipes` hardcoded filenames: provenance comments added (soft ordering
    heuristic from the first reference repo; degrades gracefully; generalize to an `EditableSpec`
    knob only if a task needs to override).

**§4 is now fully addressed** (every item shipped or explicitly resolved-as-kept).

---

## 5. Mega-review follow-ups (2026-07-09) — deferred / disputed

A xhigh-effort mega-review of that day's changes (range `01c5feb…1841018` +
`ef48e63`, mostly the inline-repair checkpoint-reuse feature `e12c43c`) surfaced
15 findings. **13 were fixed** on branch `claude/todays-changes-review-xhqom4`
(reuse-predicate correctness incl. cumulative-`last_files` delta + fail-closed
reachability + manifest guard, retrain-cap off-by-one + first-stage counting,
`mount:true+edit:true` coercion for snapshot back-compat, `_finalize` loud-fail,
order-aware missing-input check, single-file writable-copy surface, whitespace
command, bounded MCTS reward, node-count reflection gate + `run_id` de-dup,
reused-stage fold record). What remains open or was dismissed:

### Deferred correctness (needs a design decision)
- 🟡 **P2 · `_shutdown_pool_sockets` blast radius (M).** On a bounded non-stream
  timeout, `core/llm.py::_nonstream_bounded` `socket.shutdown()`s **every**
  connection in the SHARED httpx pool, so under `max_parallel>1` (or after a
  stream-stall degrade to non-stream) a healthy sibling request on another
  connection is killed mid-read — it burns a retry, and a collaterally-killed
  *stream* counts toward `_stream_stalls`, which after `STREAM_STALL_DEGRADE_AFTER=2`
  **permanently** degrades the client to non-streaming. *Verified PLAUSIBLE; NOT a
  regression of this range* — pre-change `close()` already dropped in-flight
  connections; the shutdown only makes the collateral kill immediate. Left
  unfixed because a correct fix needs per-request connection isolation, and both
  options have costs: a dedicated per-call httpx client adds a TLS handshake on
  the (common, in `llm_stream=False`) non-stream path; a custom httpcore
  transport that registers each request's socket is the clean fix but a bigger
  change. **Recommendation:** custom transport tracking `request→socket`, or shut
  only the wedged call's connection. → `core/llm.py:72,756`.

  **[2026-08-14 — the described FAILURE is closed; the primitive is not. Both citations are dead.**
  `_shutdown_pool_sockets` and `_stream_raw_socket` moved to `core/llm_streaming.py:37-73`
  (re-exported through `llm.py:69`); `_nonstream_bounded` is `core/llm.py:891`; `_stream_stalls` /
  `STREAM_STALL_DEGRADE_AFTER` are `llm.py:751` / `:84`. The walker at `llm_streaming.py:47-62` is
  still literally pool-wide — but it is now GATED: `core/llm.py:785-792` maintains `_inflight` /
  `_stream_inflight` counters, `:899-907::_pool_teardown_is_safe_locked()` requires `<= 1`, and the
  teardown call at `:1001-1007` only fires when that holds under `_inflight_lock`. With a sibling in
  flight the teardown is SKIPPED and the code accepts one lingering daemon thread instead (comment at
  `:983-999`). So the collateral-kill → `_stream_stalls` → permanent non-stream degrade cascade this
  entry describes can no longer occur. This is not the recommended per-request transport; it is a
  different, real mitigation, and the recommendation stands only as a cleanup. Downgraded to residue
  (§0.2).]

### Deferred cleanup (quality, not correctness — review flagged, left for a focused pass)
- ✅ **P2 · one owner for the resolved stage pipeline (S–M).** `_resolved_stages`
  (orchestrator.py) re-implements `_run_eval`'s profile→`build_command`→
  `_resolve_stages` derivation as a parallel copy (they already differ: `_run_eval`
  honors an explicit `profile` arg, `_resolved_stages` doesn't). Have
  `run_command_eval` return the resolved stage list on `RunResult` (it already
  returns `failed_stage`), so the repair loop inspects exactly what ran.

  **[2026-08-14 — STILL OPEN, the divergence intact, and the PROPOSED FIX does not work. SURVIVOR
  #9.** `_resolved_stages` MOVED out of `orchestrator.py` to `engine/eval_stages.py:261-278`;
  `_run_eval` is `engine/eval_dispatch.py:572-649`. The exact flagged difference is still there:
  `eval_dispatch.py:604` does `prof = profile or (node.idea.eval_profile …)` while `_resolved_stages`
  **has no `profile` parameter at all** (`eval_stages.py:269-271`), so an explicit `profile` — a
  confirm/full pass — is invisible to it. `RunResult.stages` DOES now exist
  (`runtime/sandbox.py:290-294`) but is the post-run per-stage OUTCOME record, and all four callers
  of `_resolved_stages` need the chain BEFORE or independently of a run: log-watch planning
  (`engine/evaluate.py:1097,1655,2340`) and the salvage re-check
  (`engine/metric_salvage.py:998`). So threading it through `RunResult` cannot retire the copy —
  the one owner has to be a pure derivation both sides call.]

  **[2026-08-14, later the same day — SHIPPED, as that last sentence prescribed.** The one owner is
  `engine/eval_stages.py::_eval_pipeline`, a pure derivation the dispatcher and the planners both
  call; `RunResult.stages` is untouched and stays the outcome record. Two corrections to the survey
  above, carried in full at §1 survivor #9: the citations had drifted ~29 lines in `eval_stages.py`,
  there are THREE callers rather than four (`metric_salvage.py`'s is a docstring; the real salvage
  caller is `evaluate.py::_recheck_repaired_contract`), and the divergence was LATENT — the only
  profile-passing caller is the confirm phase, which never plans, and no run in `runs/` contains a
  single `confirm_eval` row.]
- ⬜ **P2 · unify the launch-readiness gate (S–M).** "Is this task launchable" now
  lives in 2 parallel copies — `EvalSpec._command_or_stages` (backend truth) and
  `serve/tui.py::spec_ready` (the third, `ui/src/GenesisChat.jsx`, was deleted as dead
  UI 2026-07) — and this range was itself
  the drift repair (both frontends had to learn stages-only cmd + dataset mounts).
  Expose one server-side `validate_task` dry-run (e.g. `/api/validate`) both
  frontends call, instead of re-deriving the rules in Python + JS.

  **[2026-08-14 — STILL OPEN, and now acknowledged IN CODE. One citation is stale.** Both copies
  live: `adapters/repo_task.py:725-729::EvalSpec._command_or_stages` and — moved out of
  `serve/tui.py` — `serve/tui_format.py:140-171::spec_ready`, whose own docstring (`:141-143`) says
  it "mirrors the backend truth … see the BACKLOG 'unify the launch-readiness gate' item". No
  `/api/validate` route exists anywhere in `serve/`. Careful: `adapters/tasks.py:336::validate_task`
  DOES exist but is a different operation — it constructs a real TaskAdapter for a run, not a
  launch-readiness dry-run. Residue (§0.2): one frontend has since been deleted, so the drift can
  only bite the TUI.]
- ⬜ **P3 · factor the shared socket-shutdown idiom (S).** `core/llm.py` has 3
  copies of the `try: sock.shutdown(SHUT_RDWR) except: pass` "only shutdown()
  interrupts a kernel recv" idiom (`_raw_socket`, `_stream_raw_socket`, the new
  pool walker) and 3 socket extractors over private httpcore internals — an
  httpx/httpcore upgrade must be chased through each. Factor one `_shutdown_sock`
  + keep the `get_extra_info('socket')` extraction in one place. *(Ties the P2
  above — a custom transport would subsume it.)*

  **[2026-08-14 — PARTIAL: 3 copies → 2, and one named symbol is GONE.** `_raw_socket` was
  **deleted** — `core/llm_streaming.py:9-11` records why (doc 25 CO-03: "no production code had
  called [it] since the openai-SDK migration"). The two survivors are both in that one file now, the
  pool walker at `:58-61` and the stream idle-guard at `:166-169`. No shared `_shutdown_sock` helper
  exists. Consolidated into one module but not factored. Residue (§0.2).]
- ⬜ **P3 · factor the RunResult timeout-nulling (S).** The "null metric/extras/
  trials on timeout" `RunResult(...)` construction is copy-pasted across
  `SubprocessSandbox.run`, `DockerSandbox.run`, and `command_eval.run_command_eval`
  (this range fixed a drift where Subprocess didn't null) — extract one factory.

  **[2026-08-14 — STILL OPEN; three copies, now in three SPELLINGS.**
  `runtime/sandbox.py:1314-1320` (`SubprocessSandbox.run`, whose comment names the other two),
  `sandbox.py:1375-1381` (`DockerSandbox.run`), and `runtime/command_eval.py:2286-2304`, which uses
  per-field `if not to` guards where the two sandboxes use `None if to else …` ternaries. They
  cross-reference each other in comments, which is what keeps this cheap. Residue (§0.2).]

### Investigated and dismissed (on record so they aren't re-raised)
- ✅ **REFUTED · "blanket `except` in `_resolved_stages` disables the retrain
  cap".** A deterministic resolution error would crash `_run_eval` (same
  derivation, no try/except) *before* the repair loop consults the counter, so it
  can't recur every attempt; the exception path is a minor robustness wart (log
  it), not an unbounded-retrain bypass. → `orchestrator.py::_resolved_stages`.
- ✅ **REFUTED · "cumulative-`last_files` masks the reachability holes so they're
  harmless".** True for the in-house developer's *raw key set*, but (a) the
  now-fixed real-delta change set makes the changed set small, re-exposing the
  holes, and (b) the CLI-agent backend's `last_files` is a git-diff delta all
  along — so the reachability fix was needed regardless. (Both the delta and the
  reachability were fixed.)

## 6. Prompt/agent mega-review follow-ups (2026-07-09)

The same-day agent-prompt & delivery mega-review ([PROMPT_REVIEW.md](PROMPT_REVIEW.md)) was
largely fixed on branch `claude/agent-prompts-review-dn5fbe` (hint-registry forwarding,
skip-training contradiction, truncation markers/page sizes, `merge_system` reachability +
lesson/hypothesis wording split, neutral untagged-reflection outcome, `_sdk._client` timeout
guard, sanctioned mlebench grader import, PromptStore key table in the docs). Items
deliberately deferred, with rationale:

### Deferred design work
- 🟡 **Per-stage ARTIFACT DECLARATION + technical verification (M–L)** — *PARTLY SHIPPED
  2026-08-07 as the per-stage success contract `expect`. The reuse-keying half is deliberately NOT
  built; see the cut below.* The original entry: each pipeline stage DECLARES the
  artifact paths it produces (checkpoints, processed data, predictions); after a stage the
  engine VERIFIES existence/freshness of the declared artifacts, and checkpoint-reuse keys on
  the declared artifacts instead of the import-closure heuristics (`_safe_reuse_start` +
  `_stage_reachable_files`), which are fail-open by construction for anything they cannot see
  (deleted modules, non-`.py` inputs, non-default `cmd.cwd`). The declaration can ride the
  existing stage manifest (`looplab_stages.json` / operator `cmd.stages`); needs a design pass
  for freshness semantics (mtime vs content hash), for agent-declared vs operator-declared
  pipelines, and for what "verification failed" does mid-loop (bounce the stage vs fail the
  node). Retires the whole D1–D4 defect class instead of patching its holes one by one.

  **SHIPPED** (`runtime/command_eval.py::_validate_expect` / `verify_stage_artifacts`,
  `engine/eval_stages.py::STAGE_CONTRACT_CLAUSE`, `tests/test_stage_contract.py`):
  `expect: {files?, assert?}` on any stage, from either declarer. `files` is the technical half —
  each declared path must exist inside the workdir, be non-empty, and have been written by THIS run
  of the stage. `assert` is one line stating what the stage's success MEANS, handed to the
  inter-stage checker as its contract. The three open design questions were answered as:
  **mtime, not content hash** — hashing a multi-GB checkpoint is real time on the eval path, and the
  coarse answer is the conservative one (it can only report an untouched artifact as stale, never a
  stale one as fresh); **one field for both declarers**, because the manifest is the only place a
  contract can be stated for a stage whose script the operator PROTECTED, which is the mode where an
  in-script assert is impossible; and **fail the stage**, exactly as a non-zero exit does, so it
  flows into the existing repair loop, the existing `_safe_reuse_start` and the existing stage-scoped
  re-run with no new mid-loop vocabulary.

  **CUT, and it is a cut rather than a deferral: keying checkpoint REUSE on the declared artifacts.**
  It trades a fail-CLOSED heuristic for a fail-OPEN declaration. `_safe_reuse_start` refuses reuse
  whenever it cannot PROVE the earlier stages' inputs are unchanged; a declaration is written by the
  agent, so keying reuse on it means an agent that under-declares gets its stale checkpoint scored —
  a silent wrong metric, which is the single failure that predicate exists to prevent. The two are
  now complementary rather than sequential: the import closure decides what may be SKIPPED, `expect`
  decides whether what RAN did its job, and neither is asked the other's question. The D1–D4
  fail-open holes therefore remain exactly as they were; this entry no longer claims to retire them.

  **STILL OPEN.** Existence-only verification would NOT have caught the incident that motivated the
  work (`runs/rubert-dr-0807`: `hard_negs.pkl` existed, was non-empty, and covered 9,364 of 764,676
  queries) — that is what `assert` is for, and `assert` is judged by an LLM against the stage's
  printed output, so it is only as good as what the stage prints. The prompt half (the repo
  Developer's STAGE CHECKS block: print the numbers, assert them in code, declare the same condition
  in the manifest) is what makes those numbers exist, and it is a recommendation, not an enforcement.
  A cheaper model-free `assert` — a declared numeric relation the engine evaluates against a named
  key the stage prints — is the obvious next step and is not built.

  **RE-EXAMINED 2026-08-14 against `needs`, and the CUT above stands.** `needs` (the stage's INPUT
  declaration) looks like the missing half: if a changed config is not among an earlier stage's
  declared inputs, that stage's inputs are provably unchanged by it, and the non-`.py` refusal could
  be narrowed. It does not hold. `needs` IS NOT A BOUND — `verify_stage_inputs` `stat()`s the declared
  paths before the command and nothing checks the converse, so a stage reads whatever it likes (the
  read fence never fences the workdir, which is where a candidate's config lives) and "not in `needs`"
  means UNDECLARED, never UNREAD. It is also optional and effectively unused: **2 of 129 stages across
  every `runs/*/nodes/*/looplab_stages.json` declare it** (both on the live v8 run, both from the day
  the field shipped), against 20 declaring `expect` — so an absent declaration must read as UNKNOWN
  and the widening would decide nearly every real case by its default. That is the same fail-OPEN
  trade the CUT names, one field over.

  It would also have bought nothing. Replaying every `node_repaired` row in `runs/` that carries a
  change-set column (75), only **8 change a non-`.py`, non-manifest file — all 8 are one
  `config.yaml`** — and every one of those 8 sits on a pipeline that was ALREADY refused for opacity.
  **Opacity, not the non-`.py` clause, was the binding refusal on this box**: 39 of the 75 rows run a
  pipeline with no `.py` argv token, 29 of them spell every such stage `python -m <module>` resolving
  inside the node's own workdir, and every `rubertlite-dr-unified-{v2,v6,v7,v8}` pipeline is that
  shape. So what shipped instead is the ENTRY-POINT half of the closure (`_module_entry_candidates`):
  `python -m pkg.mod` names its entry by import syntax and is bounded by the rule already used for
  imports, credited with the package's `__main__.py` and refused outright when it resolves to no
  workdir file. That is not a declaration and not a new modelling assumption — it is the existing one
  applied to the entry point. On the historical corpus it flips **0 of the 27 decisions where reuse
  was even possible**, because on every resolvable `-m` pipeline the repaired file really was inside
  the train entry's import closure; its value is that the refusal is now a measurement instead of a
  blanket, and the live v8 `mine` stage's closure is 7 files wide, so a later score-side `.py` repair
  there keeps the hour of mining rather than discarding it.

  **What would make `needs` load-bearing is an ENFORCEMENT rung, not a better reading**: run each
  stage under a kernel read allow-list scoped to its own declared inputs. Both pieces exist —
  `runtime/read_allowlist.py` derives the set, `runtime/landlock.py` enforces one — but today's
  ruleset is workdir-wide and off by default, so the scoping and the migration (127 of 129 stages
  declare nothing) are the work. That is `runtime/dev_probe.py`'s move: make the surface be the thing
  the fence can cover. Until then the non-`.py` clause stays exactly as it is.
- 🟡 **D5 · per-attempt stage-event accounting (S).** After an in-loop checkpoint-reuse re-eval,
  the node's only folded stage record is `train={reused, 0s}` — the attempt that actually spent
  the training wall-clock is never recorded for that node (the fold's guard only protects
  records that exist). Accounting/UI only, metrics and replay are unaffected; fix is an
  attempt-indexed stage record (or a per-attempt `stage_completed` event) plus a readmodel that
  sums attempts.

  **[2026-08-14 — the reported DEFECT is closed; the accounting is not. The named event does not
  exist:** there is no `stage_completed` — the registry name is
  `events/types.py:224::EV_STAGE_FINISHED = "stage_finished"`. Its fold handler
  `events/replay.py:1412-1431::_on_stage_finished` now guards exactly this case, in its own words at
  `:1422-1427`: *"A 'reused' marker means a re-eval SKIPPED this stage — it must NOT clobber that
  attempt's REAL completion record … Keep the informative record"*. So a later `reused/0s` no longer
  overwrites an earlier real `{exit_code, seconds}` for the same stage name, and the training
  wall-clock survives. What did NOT ship is the row's actual proposal: records are still keyed by
  stage NAME (last-real-wins), not attempt-indexed, and no readmodel sums attempts. Residue (§0.2) —
  accounting/UI only, as the row itself says.]

### Deferred cleanup
- ✅ **Tool-consolidation follow-through (S–M).** Dedup the paginated file-reader family —
  reposcout `read_file`, knowledge-tools `repo_read`, env_inspect `read_installed` — into one
  reader contract (same arg names, same resume-pointer shape) once loop-side pagination
  settles: the P3 fix pinned page sizes under the 4000-char loop cap per tool; unifying first
  would have churned three prompts mid-review. Blocked on: pick ONE page-size constant the
  `_base.py` provider contract exports, then collapse the three implementations.

  **[2026-08-14 — DONE. The three tool NAMES survive (they are prompt contracts); the three
  IMPLEMENTATIONS were collapsed onto the repo scout.**
  `tools/knowledge_tools.py:171-173`: `repo_read` now delegates to `self._scout._read_file(...)`,
  with the comment *"The scout owns the read: its size fence, its full-file-then-paginate contract…
  its resume marker"*. `tools/env_inspect.py:264-271`: `_read_installed` imports
  `RepoScoutTools._paginate` and `_MAX_READ` from `reposcout.py` verbatim — *"Paginate exactly like
  the repo scout's read_file (SHARED window logic => one marker contract across all the source
  readers)"*. The stated blocker resolved slightly differently than predicted: the one constant is
  `tools/_base.py:26::RESULT_CAP` (re-exported from `core/context_budget`), from which
  `reposcout.py:41::_MAX_READ = RESULT_CAP - 400` derives and which the other two import.]
- ⬜ **Reward-hack vs hardened-suite residual (S).** The mlebench grader-import false positive
  was FIXED in the detector (`trust/reward_hack.py` waives only the IMPORT tells when the task
  ships `grader.py` as an asset — the asset set reaches it via `protected_names`; key access
  stays flagged), but a persisted `looplab harden` exploit suite still carries the seed
  `import_grader` regex (`trust/harden.py::_SEED_EXPLOITS`) and independently re-flags the
  sanctioned import on such tasks. Teach `ExploitSuite.scan` the same sanction (skip
  grader-import patterns when the task ships the grader), or tag seed exploits with the
  contexts they apply to.

  **[2026-08-14 — STILL OPEN, and the double-flagging is visible in one call site. SURVIVOR #3.**
  Every named symbol resolves unchanged: `trust/harden.py:33-49::_SEED_EXPLOITS` with the
  `\bimport\s+grader\b` regex at `:34-36`, and `ExploitSuite.scan` at `:79-90` — whose signature is
  still `scan(self, code)`, taking no context or sanction. The call site is
  `engine/evaluate.py:797-819`: `detect_reward_hacks(...)` IS correctly passed
  `grader_import_ok=True` when `grader.py` is a sanctioned asset (`:807-815`), and then `:819` runs
  `sigs += self._exploit_suite.scan(scan_src)` **unconditionally, with nothing threaded through**.
  So the sanctioned import is waived by the detector and independently re-flagged by the suite, on
  exactly the mlebench tasks that are the proof point — and a `reward_hack_suspected` keeps the node
  out of `feasible_nodes`. Cheapest fix of the top five survivors.]

  **[2026-08-14 — FIXED (`fix/hardened-suite-grader-waiver`), with one correction to the triage.**
  `ExploitSuite.scan(code, *, grader_import_ok=False)` now takes the SAME sanction the detector
  takes, `evaluate.py` derives it ONCE via the new `reward_hack.grader_import_sanctioned(assets)`
  (the one owner of the asset-key normalization) and hands the identical value to both detectors,
  and the waiver is applied per MATCH via `reward_hack.sanctioned_grader_import_only(span)` — a
  finding is dropped only when its matched span carries an import tell AND is clean under the
  waived detector. So it is total over rules the suite has not grown yet, while a rule matching a
  key access / shell-out / protected write keeps firing. THE CORRECTION: no `exploits.jsonl` exists
  anywhere in this tree or under `runs/`, and the shipped `looplab harden` path can never mint the
  seed `import_grader` rule — its probe carries `grader._Y`, which the un-waivable key tell catches
  in every configuration, so the rule counts as "already caught" and is never added (a default
  harden run yields exactly constant_perfect / overwrite_frozen / os_system_exfil). The rule is
  reachable by an LLM `hacker=` plug against a grader-aware detector (`_derive_pattern` escapes the
  import head into a durable `import\ grader`) or by a hand-authored suite, both driven in
  `tests/test_hardened_suite_grader_waiver.py` alongside two real runs — sanctioned (no violation,
  node stays in `feasible_nodes`) and unsanctioned (still flagged, still excluded under `block`).
  SIBLING CHECKED, no change: `_audit_workdir_writes` has no such asymmetry — `protected_write` is
  never waived by the import sanction in EITHER detector, so the static tell and the runtime audit
  agree about an asset write; a task that legitimately writes an engine-placed asset would be
  flagged by both, which is a uniform frozen-asset policy rather than one detector re-raising what
  another waived. Giving it a waiver would need an explicit host-side "this asset is writable"
  declaration (assets carry none today), never an inference from what the candidate did.]
