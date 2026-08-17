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
   **[AND THE FLIP LANDED 2026-08-15, so this entry's own headline is now false in both halves.**
   `Settings.redact_output` and `EngineOptions.redact_output` both default to `True`: the split
   above made the entropy half separable, `_entropy_candidate` + `_ENTROPY_TOKEN_CHARS` removed
   its false positives (13 of 744 persisted tails, 13/13 filesystem paths, -> 0; re-measured
   2026-08-15 over 1,652 tails across 82 event logs, still 0, while the pre-fix rule changes 18
   of the same 1,652), and the owner made the call the fix deliberately left to them. Driven at
   the SHIPPED default in `tests/test_redact.py` in BOTH directions — the credential is absent
   and the corpus's real traceback paths survive verbatim. Deliberately NO
   `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` row: measured, all 46 preserved snapshots carry the key
   explicitly (45 `false`, 1 `true`) and resume unchanged, so the flip reaches no recorded run
   and a `setdefault` row could never fire.]
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

- **[FIXED 2026-08-15] `declared_param_overrides` read a file in BREADTH-FIRST order, so a helper
  `def` outranked the module body and a node whose code AGREES with its declaration could be stamped
  `params_overridden` — on the champion.** `_assigned_numeric_paths`' docstring says LAST WRITE WINS
  "matching what the interpreter would do if both ran in order". It walked `ast.walk`, which is BFS:
  every module-level statement is yielded before anything nested inside one, so "last" meant DEEPEST,
  which is the inverse rule. Driven, not read — `def _defaults(cfg): cfg.train.training.batch_size =
  4096` above a module-level `cfg.train.training.batch_size = 8192` returns a `ParamOverride` at
  line 3 against a declaration the effective assignment satisfies exactly. That row does not stay on
  the attempt: `champion_caveats.py::champion_metric_caveats` calls the whole-node form off folded
  state on every `/api/runs` poll, so a helper's default convicts the run's best number, which is the
  one direction this vocabulary was built to be careful in ("a false `params_overridden` on the
  champion is worse than a missed one"). The same bug is a free EVASION worn the other way: the
  identical one-line decoy, agreeing with the declaration and called by nobody, SUPPRESSED a real
  divergence — so a `not in` pin on either half would have held neither. **The fix is to sort the
  assignment nodes by `(lineno, col_offset)` before writing the dict**, i.e. to make the code do what
  its own docstring says. Non-vacuity proved by mutating a throwaway `git archive HEAD` tree: the new
  test fails there and passes here. **It changes NOTHING on the corpus** — re-derived over all 46
  preserved logs, both orderings return the identical answer on every one of the 218 folded nodes,
  because the real champion's `train.py` assigns each path exactly once. That is why nobody saw it,
  and it is the argument for the property test rather than against the fix. A decoy placed strictly
  AFTER the effective assignment still wins and is left open on purpose: that is the dead-branch
  residual the module docstring already states, and no static reader can do better.
  **Evidence:** `looplab/engine/repair_verify.py::_assigned_numeric_paths`,
  `tests/test_repair_verification.py::test_a_helper_default_earlier_in_the_file_does_not_convict_the_agreeing_module_body`.

- **[STATED, NOT FIXED — 2026-08-15] `verified` means "at least one claimed token appears somewhere
  in the diff", and the diff is searched as FLAT TEXT — so a DELETED line, unchanged CONTEXT and any
  COMMENT the agent wrote all count as evidence a promise was kept.** The row that shows it is on the
  live run: `rubertlite-dr-unified-v8` node 9 attempt 2 promises *"reduce n_epochs to ~6 … so the
  declared epoch count matches the actual training end"* and does the OPPOSITE — it deletes the
  `config.train.training.n_epochs = 6` its own previous attempt added, raises the stage `timeout`
  14400 → 22000 instead, and leaves the manifest passing `--train.training.n_epochs 10`. Verdict:
  `verified`, `unmet: ()`. The two occurrences that met its claims are the DELETED assignment and an
  added comment reading *"n_epochs comes from the CLI … and is NOT overridden here"* — the verifier
  satisfied by a comment DENYING the action it certifies, which is CLAUDE.md's own guard-test rule
  ("a guard test must not be satisfiable by a COMMENT") turned on the guard. This is the asymmetric
  direction: tonight's merge fixed two `unmet` FALSE POSITIVES, which are advisory noise a judge can
  discount, while a false `verified` is the record affirming ON THE AGENT'S BEHALF.
  **Population, measured** by replaying all 2,487 `node_repaired` rows in `runs/` through the real
  `verify_repair` on regions rebuilt from each attempt's own predecessor (2026-08-15 23:40 UTC): 76
  `verified` · 12 `unmet` · 46 `inert` · 2,353 `unstated`; of the 75 `verified` rows carrying both a
  rationale and a change set, **48 have at least one claimed token with NO occurrence on an added
  CODE line** — 26 tokens / 18 rows comment-only, 20 / 14 deletion-only, 32 / 21 context-only. Not
  one row; most of the population.
  **Why it is stated and not patched — every candidate was driven and refused.** *(a) make a comment
  not count:* moves FOUR of 2,487 rows (all `verified` → `unmet`) and does **not** move the row above,
  since the deletion still matches. *(b) make a DELETED line not count:* provably wrong —
  `rubert-dr-0807` node 12 attempt 2 claims *"replace_sampler_ddp was removed from Trainer.__init__;
  drop the arg"* and is CORRECTLY `verified` by a `-` line alone. *(c) require EVERY claim rather than
  one:* convicts at the 48/75 scale above on evidence nobody has audited row by row, against this
  rung's own rule that a weak signal may ACQUIT and may not CONVICT. *(d) read the DIRECTION:*
  unrecoverable in principle here — "reduce n_epochs to 6" and "drop the n_epochs override" are one
  token in one file, and separating them needs the VALUE out of model-authored prose, which is exactly
  the text this rung's trust tier is defined by not reading. `declared_param_overrides` can see a value
  only because it compares committed BYTES against a DECLARATION the Researcher minted with no
  rationale in the loop; a free-text repair promise has no such second artifact.
  **What was done instead:** the bound is stated on the WORD in `repair_verify.py`'s docstring and in
  CLAUDE.md — `verified` means "the repair's vocabulary appears in what it changed", never "the repair
  did what it said" — and the row is PINNED in `tests/test_repair_verification.py::
  test_verified_means_the_vocabulary_appears_and_never_that_the_repair_did_what_it_said`, together
  with the `drop the arg` case that refutes patch (b), so a future direction-aware rung has a red test
  to turn green rather than a paragraph to rediscover. **Nothing about this can move the loop**:
  `REPAIR_INERT` is the only verdict `_evaluate` acts on and it is decided on BYTES before a claim is
  read, so the whole residue is confined to a durable column and to prompt text.
  *(The repair's OUTCOME was right — spending unclaimed ceiling instead of shrinking the experiment is
  what `_time_budget_note` now asks for. It is the VERDICT that is wrong about it.)*

- **[FIXED 2026-08-15] Two published measurements of ONE population disagreed inside a single merge,
  and a retracted projection survived at a seventh site nobody enumerated.** Both are documentation,
  both were re-derived rather than re-read. (1) `champion_caveats.py`, `CLAUDE.md` and
  `docs/guide/tasks.md` all said "exactly ONE of 297 nodes" has code contradicting its declared
  parameters, while `adapters/repo_developer.py::_time_budget_note` — merged in the SAME range —
  named v8 nodes 3, 8 AND 9 from the `node_repaired` side. Re-derived by calling
  `champion_metric_caveats`' own predicate on every folded node in all 46 logs: **four of 218 folded
  nodes** at 2026-08-15 23:41 UTC (v8 nodes 3, 8, 10, 11), one caveated RUN. 297 was the
  `node_created` COUNT (300 by then), not the folded population. The correction that matters is not
  the digit: this population **moves both ways while a run is live** — node 9 carried an `n_epochs`
  10 / 6 override at 23:34 and did not at 23:41, its second repair having deleted that assignment —
  so a caveat derived live from folded state must QUOTE ITS INSTANT and must not be written as a
  corpus statistic. (2) The 2026-08-15 retraction of the falsified 22,096 s projection covered the
  five sites `docs/BACKLOG.md` enumerated plus `docs/guide/llm-and-agents.md`. **`CLAUDE.md`'s
  `looplab/tools/` row was a seventh and still asserted it in the present tense** ("projects 22,096 s
  into the same 22,000 s ceiling — a wrong diagnosis buying a fix inert against the real failure"),
  in the file every coding agent reads first, about a node that PASSED in 19,915.75 s and became the
  champion. Retracted there now, with the same account the other six carry (the margin came from
  attempt 5 deleting the in-`train` `test_model()` call, not from the batch halving and not from the
  epoch cut, which never landed), and `llm-and-agents.md`'s "still quoted at five other sites" — true
  when written on its branch, false once it merged after the retraction — corrected with it.
  **What this range got right, checked the same way and worth recording:** the four hand-resolved
  conflicting files (`CLAUDE.md`, `docs/infographic/agent-architecture.html`, `docs/guide/
  architecture.md`, `docs/guide/configuration.md`) were reconstructed mechanically against both
  parents and the merge base — `git merge-file` on the blobs, token-level for CLAUDE.md's 22 KB table
  row — and every one is the EXACT three-way union, with no text dropped, duplicated or misplaced;
  the diagram still parses, holds 85 blocks / 48 edges with every edge endpoint resolving and no id
  minted twice; `configuration.md` still has 203 settings rows with no duplicate name. The
  reconstructed `tests/test_repair_verification.py` lost nothing either: every top-level symbol and
  import of BOTH parents is present at HEAD.

- **[FIXED 2026-08-15] Both roles were told the per-eval budget is an END-TO-END POOL the pipeline
  shares. It is a PER-STAGE ceiling, and believing otherwise is what killed v8 node 9 and shrank its
  experiment from 10 epochs to 6.** The number has been shared since F1h shipped
  (`command_eval.eval_spec_time_budget`, one derivation, three readers). The *semantics* were not,
  and the two prompts got them backwards: `repo_developer._time_budget_note` said "one evaluation of
  this node gets *N*s, **end to end**: every stage you declare plus the protected scoring step", and
  `proposal_cues._cue_experiment_time_budget` said "each experiment **(train+eval)** must finish
  within ~*N*s … leave room for data prep + eval", and the F1h hint beside it asked for an estimate of
  "total_steps x per-step time, **plus data prep and scoring**" — the wrong quantity, named inside the
  one sentence that asks for the arithmetic.
  **Nothing in the engine implements a pool.** `_run_stages` takes each stage's own ceiling
  (`finite_timeout(_stg.get("timeout", timeout), timeout)`) with no accumulator and no cross-stage
  deadline; `eval_stages._resolve_stages` appends `score` with the operator's own `score_timeout`, a
  *fresh copy* of the budget on top of everything preceding. `stages_over_time_budget` — the gate the
  same Developer is refused by — already said this in as many words ("the protected `score` stage runs
  at the operator's number ON TOP of whatever precedes it, which means a sum rule … would refuse every
  manifest ever written"), and so did `docs/guide/configuration.md`. Only the prompts disagreed, and
  they disagreed in the direction that costs GPU time.
  **MEASURED over every `stage_finished` in `runs/` (2026-08-15): 51 stage rows ran LONGER than their
  own run's entire per-eval budget and not one was killed for it.** 45 nodes of
  `rubertlite-dense-retrieval` each spent a single `train` stage at 1.1x–6.0x a 3600 s budget and were
  **scored**; v7 nodes 0/1 ran 29,389 s and 29,184 s against 21,600 s; v8 node 3 consumed 51,793 s of
  stage wall clock against 36,000 s across its attempts and became the run's champion at 0.762048. A
  pool that 51 rows walk through is not a pool.
  **WHAT THE FICTION BOUGHT, and it is not what it looks like.** The tempting reading is
  "the operator raised `eval.timeout` 21,600 → 36,000 and the agents kept sizing `train` from their own
  reasoning". The corpus refuses it. Declared-pipeline / budget ratios by run:
  `rubertlite-dense-retrieval` 4.0–24.1x, `rubert-dr-0807` 0.75–7.0x, v2 1.0–2.6x, v6 1.0–2.1x, v7 8.0x
  and 4.2x — and **v8 alone at 0.60, 0.70, 0.83, 0.86, 0.89, 0.90, 0.90, 0.95, every node under 1.0.**
  What changed at v8 is not the budget, it is the **authoring gate**: `943b8687` merged 2026-08-14
  12:06 UTC and v8's engine loaded its source at 16:25, so v8 is the *first and only* run in the corpus
  that ever evaluated under `stage_time_budget_refusal`. Before it, a role that could not size a
  ceiling declared 48 hours and the question never arose. The gate removed that escape hatch — correctly
  — and the role, now forced to name a real number and told the number was a pool, **partitioned** it.
  v8's absolute `train` ceilings (14,400–30,000) are *higher* than v6's (14,400–28,800) at a 2.5x
  smaller budget, so the roles do read the budget; they just spend it on the wrong stages. Node 9 gave
  `mine` 7,200 s (it used **2,349.6**) and `train` 14,400 s, holding 14,400 s back for a `score` stage
  that measures ~3,100 s on this task and is charged to none of it. Its `train` was SIGKILLed at
  14,402.67 s at `7691/10590 [3:56:39<1:26:17]`, 73 % done, ~5,160 s short, with **21,600 s of ceiling
  it was entitled to declare and never claimed**. So this is neither a general defect the corpus was
  always carrying nor an artifact of one operator's configuration change: it is a NEW defect the gate
  created, shaped by a false prompt, and it recurs in v9 at any budget — a *lower* budget makes it
  worse.
  **THE COST IS THE EXPERIMENT, NOT THE GPU-HOURS.** All five `stage_finished.status="timeout"` rows in
  `runs/` were answered by a repair that made the experiment SMALLER — dense-retrieval n72 (fewer ANCE
  negatives), v2 n3 ("fewer epochs / val subset"), v6 n5 (`n_epochs` 10→5), v8 n3 (15→8, `unmet`, never
  landed), v8 n9 (10→6, **`verified`**, landed) — and **zero of the five raised the stage's own
  ceiling**, though v8 n3 (22,000 against 36,000) and v8 n9 (14,400 against 36,000) had 14,000 s and
  21,600 s of budget left to raise it into. v8 n9's diagnosis was *correct* ("73 % done, healthy
  1.77s/it progress — no stall or divergence. This is a pure wall-clock budget issue"), so this is not
  a role failing to read its log; it is a role with one move in its head. The OneCycleLR hypothesis is
  now being evaluated at 6 epochs against siblings trained for 10 and 15, and
  `repair_verify.declared_param_overrides` reports
  `ParamOverride(param='train.training.n_epochs', declared=10.0, code=6.0)`. Across all **87**
  `node_repaired` rows carrying a change set, a stage `timeout` was raised **exactly once** (v2 node 0,
  14,400 → 21,600) and that raise went *above* the operator's budget, i.e. the gate refuses it today.
  Within the budget it has never happened.
  **HOW MUCH OF THAT THIS FIX ACTUALLY REACHES — less than the brief that asked for it implied, and
  the correction belongs in the record.** Only **two** of the five timeouts had headroom to raise into
  (v8 n3 at 22,000 and v8 n9 at 14,400, against 36,000); the other three declared a ceiling *equal* to
  the budget (v2 n3, v6 n5, both 14,400) or *already above* it (dense-retrieval n72, 21,600 against
  3,600), so no wording could have saved them — there the budget itself was the bound. And the ceiling
  is not the only pressure that shrinks an experiment. Re-derived through
  `declared_param_overrides` over all **2,484** `node_repaired` rows **at 2026-08-15 22:26, with v8
  LIVE** — this population is still growing, so the instant is part of the number; a sibling measured
  it as 1 node / 1 repair hours earlier and was right then — **four rows on three nodes, all v8**, and
  they split two-and-two:

  | node/attempt | pressure | override | science |
  |---|---|---|---|
  | v8 n3 a4 | memory (R-Drop OOM at bs 8192) | `batch_size` 8192→4096, `grad_accum` 2→4 | **neutral** (effective batch preserved) |
  | v8 n3 a5 | time (stage timeout) | same pair, carried forward | **neutral** |
  | v8 n8 a2 | memory (merge OOM) | `batch_size` 8192→4096, **`n_epochs` 15→8** | **altered** — the epoch cut rides along with a memory fix and is justified nowhere |
  | v8 n9 a1 | time (under-declared ceiling) | **`n_epochs` 10→6** | **altered** |

  So on that population the two pressures are **tied at one science-altering instance each**, this fix
  reaches n9 and not n8, and a more legibly stated budget would not have helped n8 at all.
  **THE RUNG THAT ALREADY CATCHES A SHRINK, and why the two are not redundant.** The agent-authored
  stage `expect.assert` fires on exactly this: v8 node 8's `train` returned `check_failed —
  declared_condition_violated: training stopped at epoch 7.99, not the declared 15 epochs, and no
  final-model save is reported`. Measured 2026-08-15 over every `looplab_stages.json` in `runs/`:
  **143 declared stages, 34 (24 %) carry an `assert` at all, 23 (16 %) name a quantity a shrink would
  falsify** — but **16 of those 23 are v8**, i.e. on the current configuration nearly every
  `train`/`mine` stage carries one; and the rung is not self-defeating, since of the **6** assert edits
  across 47 node manifest series **zero** weakened a declared quantity (v8 node 0's went 2 epochs →
  15). What it cannot do is make the wrong move *cheap*: it fires at the stage boundary, so node 8
  spent **14,105.1 s (3.9 GPU-h)** discovering that its own repair had broken its own contract, and
  node 9's repaired manifest still asserts "all 10 epochs completed" against code that now runs 6 —
  the shrink it chose is not merely incomparable with its siblings, it is queued for the identical
  verdict and another ~3-5 GPU-h. The prompt is the rung ABOVE that, where the choice is made and
  costs nothing; the assert is the backstop for when it is made anyway. **So nothing new is proposed
  here for the assert side.** The obvious ask — *require* the assertion to name the parameters the
  Idea declares — would buy the ~6 % of current-configuration stages that do not already do it
  voluntarily, and would add a rung to a mechanism that is working.
  **FIXED** by making the two statements true: `_time_budget_note` and both Researcher time
  statements now say **PER STAGE**, say the scoring step runs on the operator's own copy on top and is
  charged to nothing the role declares, and drop "leave room for data prep + eval" — which was the
  partition instruction itself. One new clause, and it is the measured gap: when a stage was killed
  *purely* by wall clock (real progress, no stall, no divergence) and its declared `timeout` was
  *below* the budget, the first fix is the CEILING and not the science. Prompt text only — no gate, no
  clamp, no control flow on the eval path — so it grants the agent nothing it could not already
  declare (`stage_time_budget_refusal` is unchanged and still bounds every ceiling at the operator's
  number) and **nothing an agent writes can move a metric, a champion, selectability or a violation**
  (docs/36). Driven by `tests/test_stage_timeout_budget.py`: a real three-stage pipeline whose declared
  ceilings sum to 3x the budget runs to completion and reports its metric, which is the property that
  makes the retired sentence false. Replayed over all 46 preserved event logs: **zero rows move** —
  prompts are not folded.
  **ALTERNATIVES REJECTED, on the numbers.** (1) *A submit-time SYMMETRIC check — the mirror of
  `stages_over_time_budget`, warning when a declared pipeline sits far below its budget.* Refuted by
  the corpus: under the per-stage rule "below the budget" is the CORRECT shape for most stages —
  v8's `mine` declared 7,200 and used 2,349.6, exactly as it should — so the warning fires on **8 of
  8** v8 manifests including the six that evaluated fine and the champion's. A signal at 100 %
  precision-zero is not a signal, and CLAUDE.md already records the advisory rung MEASURED SPENT for
  the read-fence case. (2) *Let an overrunning stage draw on unused eval budget, bounded by the
  remaining ceilings of the stages still to come.* **The pool it would draw on does not exist**: there
  is no cumulative eval clock anywhere in `_run_stages` or `run_command_eval`, so this is not a bound
  being relaxed but a whole-eval deadline being INVENTED — on the most dangerous path in the repo, to
  hand back budget the role was always allowed to declare up front and simply did not. It also arrives
  at the worst instant (the wall) to make a decision the authoring gate already proved is free at
  authoring time. (3) *Loosen `_safe_reuse_start` so raising a ceiling does not forfeit a completed
  stage.* The clause that actually bit is the MANIFEST one (`eval_stages.py:667`), not the non-`.py`
  one a sibling measured; node 9's repair comment names the incentive verbatim ("overrides the CLI
  `--n_epochs 10` so the completed `mine` stage stays reusable"). Rejected anyway: it is the one change
  here that would let something the agent WRITES decide whether a stale checkpoint is scored, for
  2,349.6 s of `mine` on one corpus row, against the failure class that has cost this project the most.
  (4) *Build nothing and state the residue.* Legitimate for (1)–(3); not for a sentence in a CONTRACT
  that is false, that both roles read, and that is demonstrably what the roles did.
  **RESIDUE LEFT OPEN, deliberately.** (a0) **Every population figure here is dated and
  configuration-scoped on purpose.** v8 was live throughout and kept producing instances, so a corpus
  snapshot reads this as rare while on the current configuration it is ongoing: 2,481 → 2,484
  `node_repaired` rows and 1 → 3 override nodes inside a few hours, and v8 gained node 10 while this
  entry was being written. Re-run the scans; do not inherit the numbers. (a) A prompt cannot be
  measured offline. Whether v9's roles
  actually claim the ceiling is a fact only v9 produces, and the check is cheap: the ratio table above
  re-derives in one pass. (b) `eval_deadline_grace_s = 1800` in the prepared v9 task is a *partial*
  mitigation and is now documented as one — it would not have saved node 9 (5,160 s short) and the
  one-shot judge correctly answers `NOT_FINISHING` at 73 %. (c) **Raising a ceiling still costs a full
  re-run** through the manifest clause of `_safe_reuse_start`, so the note asks for a move the engine
  charges for; a timeout-ONLY manifest edit provably rewrites no argv and could be exempted, but that
  is a change on the reuse decision and wants its own measurement and its own entry. (d) Nothing
  reconciles the OPERATOR's intent with the implementation: `eval.timeout` reads as a per-eval budget
  and behaves as a per-stage one, which is why 51 rows outspend it silently. Making it a real
  pipeline-wide bound is a defensible design and is NOT what this entry did — it would refuse manifests
  that have always worked, and `stages_over_time_budget`'s own docstring measured that trade and
  declined it. (e) The "all four `stage_finished` timeout rows / 22.0 GPU-hours" census in
  `config.py`, `eval_stages.py` and `sandbox.py` was re-derived here to **five rows / 24.1 GPU-hours**;
  the missing fourth row of the old count lived in a run directory (`rubertlite-dr-unified-v5`) that is
  no longer on this box, which is a standing hazard for any figure derived from `runs/`.

- **[FIXED 2026-08-15] `metric_subject` was INERT on exactly the task family it was built for: the
  subject had to be a LITERAL path, and on that family the output path is chosen by the AGENT.**
  `eval.metric.subject` shipped as a list of literal workdir-relative paths, so declaring one requires
  the operator to know the output path at submit time. **Measured over `runs/` (2026-08-15, from the
  agents' own `looplab_stages.json`):** the four repo runs that train anything (v2, v6, v7, v8)
  declare **17** node outputs and **all 17** land at
  `vectorsearch/experiments/<AGENT-CHOSEN NAME>_rubert-tiny-lite/final/model.safetensors`, with **10
  distinct spellings** of that one segment (`unified-baseline`, `nllcos_hn`, `dcl-unified`, `catdw`,
  `rdrop-dcl`, `qwen3_hn_v1`, `meanmerge_nllcos_rubert-tiny-lite`, …). The segment is
  `vectorizer-unified/vectorsearch/config.py::run_name`
  (`f"{metadata.run_name}_{base_model.split('/')[-1]}"`) and `metadata.run_name` is a value the agent
  picks per experiment. So a single literal declared at submit binds **5 of 7** nodes on v6 and **0 of
  5** on the live `rubertlite-dr-unified-v8` — which is why all three of v8's evaluated nodes record
  `{'subject_bound': False, 'unbound_reason': 'not_declared'}` under the shipped `audit` default,
  with the engine setting ON. Over the whole corpus: **46 runs with an event log, 253
  `node_evaluated` all carrying a metric, 4 with any `metric_provenance` at all (3 of them the v8
  `not_declared` rows, 1 the v6 salvage row), 0 tasks declaring a subject in any form.** The
  mechanism built to close the v6-node-4 incident could not be pointed at the run that reproduced it.
  **THE FIX is a second declaration SHAPE, not a second trust boundary:** `eval.metric.subject_glob`
  is a list of workdir-relative GLOB patterns the operator declares and the ENGINE resolves, by
  walking the node's real workdir at the score stage's start (`runtime/metric_subject.py::bind_glob`
  / `resolve_glob`). Nothing the candidate wrote about itself is read to resolve it, so the operator
  still says what the number is about and the filesystem still says what is there — the same two
  authorities a literal already had. A resolved match goes through the SAME `bind_one` as a literal
  (containment, identity, digest, producer, freshness) and the record carries both the pattern and
  the resolved path. **The load-bearing rule is UNIQUENESS: 0 matches is `missing`, ≥2 is the new
  `ambiguous` slug and is a REFUSAL.** That is not a convenience — each v8 node holds **four**
  `model.safetensors` of **92,174,712 bytes** each (`final/` plus `checkpoint-2709/3612/4515`), the
  incident's own byte-identical-in-SIZE property now WITHIN one node, so a careless
  `experiments/*/*/model.safetensors` matches all four and binding one would record
  `subject_bound: true` about bytes nobody chose — **strictly worse than the `not_declared` it
  replaced, because it manufactures confidence.** Additive only: no default moved, no `run_started`
  key was added, and replayed over all 46 preserved logs the folded state is byte-identical
  (champion, `feasible_nodes`, every metric, every provenance row) — 0 recorded metrics change
  provenance, because no preserved task declares either shape.
  **ALTERNATIVES REJECTED, each on a stated reason:**
  (a) *bind to what the score stage ACTUALLY OPENED, observed at the audit boundary.* This is the
  half `metric_subject.py`'s own docstring leaves open ("does not prove the score stage READ it"),
  and `runtime/read_fence.py`'s hook already sees every `open` under a fenced eval. **Re-measured on
  this box, 2026-08-15:** `safetensors.torch.load_file` on v8 node 1's own 92 MB checkpoint loads
  **55 tensors and raises ZERO audit events** — not zero `open` events, zero events of any kind. The
  observation channel is blind to precisely the read that matters for this task family, so the record
  it produced would be "the scorer read nothing" on a correct eval. Landlock is a kernel ENFORCEMENT
  boundary and reports no accesses, so it is not the fallback either; a real observation channel
  needs fanotify/eBPF, i.e. privileges this box does not have and a surface far past this fix.
  (b) *resolve the ambiguity from the agent's `looplab_stages.json` `expect.files`.* It is the one
  list that would always pick correctly, and it is authored by the candidate — docs/36, a wider
  action space must not widen the trusted set. Driven as a test: the manifest names one of the
  ambiguous matches and the binding still refuses. (`producer` is still RECORDED from that manifest;
  it is descriptive and decides nothing, which is exactly the distinction.)
  (c) *break the tie by mtime — drop the stale matches, then require uniqueness of the rest.* The
  most tempting one, because it resolves the "a seeded human checkpoint sits beside my own" case
  perfectly. It would promote mtime from a predicate that can only ever REFUSE (`stale`) into the
  thing that ELECTS which bytes the number is bound to, and the candidate owns its workdir —
  `os.utime` on its own files is fenced by nothing. Same rule, same reason, for the symlink case: an
  escaping match is a refusal and is never pre-filtered out of the count, or a candidate could
  de-ambiguate its own subject by making the other matches escape.
  (d) *refuse `**` at submit.* Rejected as a second, weaker guard against a case the first one
  already covers: recursion is exactly what produces the ambiguous match set, which is a refusal.
  Two rules for one property is how they come to disagree.
  (e) *narrow the claim and leave the mechanism literal-only.* The claim IS narrowed (the module
  docstring and `docs/guide/tasks.md` now state what a bound pattern does and does not prove), but on
  its own it would have left the flagship task family with no declarable subject at all, i.e. the
  measurement above unchanged.
  **Proved by driving the property** (tier 1), 14 new tests in `tests/test_metric_subject.py`:
  the incident shape (four same-sized checkpoints → `ambiguous`, no digest recorded, `metric_salvaged`
  row under `require`), a symlinked escape and a mixed escaping/legitimate match set, the mtime
  non-tie-break with the tie-break's own best case on disk, freshness and composition with a literal,
  the `not_declared` path unchanged in all three spellings, and a real `fold` proving a
  pattern-bound node stays in `feasible_nodes()`. Non-vacuity checked by MUTATING a throwaway copy
  (`/tmp`): removing the uniqueness rule, adding the mtime tie-break, adding the escaping-match
  pre-filter, and making a pattern-only declaration read as `not_declared` each go red on the
  test that names them.
  **RESIDUE, stated not patched.** (1) A `subject_glob` derives **no** `needs` on the protected
  `score` stage under `require`: `verify_stage_inputs` stats literal paths, and resolving the pattern
  a second time at a second instant is how two resolutions of one declaration come to disagree. What
  is given up is the LATENCY that entry buys, which its own comment says is all it buys. (2) An
  unbound subject under `audit` still mints no row and is not surfaced anywhere the operator looks —
  the same residue §0.2's Metrics-tab entry already records, and it is what made this defect
  invisible on a live run for two weeks. (3) A pattern proves less than a literal and the docstring
  now says so: it does not prove the operator meant THIS match rather than another the same pattern
  could resolve to on a different node. (4) a pattern costs one bounded walk per eval, measured on the real v8
  node-1 workdir (median of 5): `experiments/*/final/model.safetensors` **2.0 ms**,
  `experiments/*/*/model.safetensors` 5.6 ms, and a recursive `**/final/model.safetensors`
  **32.8 ms** — against the 338 ms the 92 MB sha256 beside it already costs, i.e. the walk is
  never the bill. The resolver stops at `MAX_GLOB_MATCHES + 1` matches; what is NOT bounded is
  the scan a `**` over a workdir with a large mounted dataset would pay, which nobody has measured. (5) **The evidence for flipping
  `require` is now within reach and was deliberately not acted on:** re-derived against the three
  preserved v8 workdirs, `vectorsearch/experiments/*/final/model.safetensors` binds **3 of 3**
  evaluated nodes uniquely with three DISTINCT digests (nodes 1 and 4 chose the same experiment name
  and produced different bytes) — but that is a re-derivation over preserved directories, not a run
  that recorded it live, so `Settings.metric_subject` keeps its `audit` default.
- **[FIXED 2026-08-15] A test harness whose own timeout was indistinguishable from the product
  defect it guarded — `test_finalizer_flushes_the_local_queue_before_building_trace_json`.** The test
  holds the exporter's writer hostage inside `_export_line` from before `Engine.run` until the
  finalizer's `force_flush` releases it, then asserts the queued span reached `trace.json`. The
  hostage waited `release.wait(5)` — a budget that has to span the WHOLE run — **inside the exporter's
  worker thread**, whose `except Exception` containment (`core/tracing.py::_worker_main`) is correct
  and swallows anything raised there, because tracing may never crash the observed operation. So on a
  loaded box the harness timed out, its `AssertionError` was swallowed, the row was counted as an
  export FAILURE, and the test reported the exact symptom it exists to detect: **a span missing from
  the artifact.** A harness that fails *as* the defect it guards is worse than no harness — it was
  read as "the barrier drops a row a caller queued before it", which would be a real
  absence-reads-as-nothing-happened defect, and it is not one.
  **Measured on this box 2026-08-15**, whole-file runs at 8-way parallelism, instrumented to record
  the hostage's wait, the exporter metrics at barrier return and the bytes on disk: **160 runs, 25
  failures; every PASS had `Engine.run` ≤ 4.93 s and every FAILURE ≥ 5.04 s — a perfectly clean split
  at the 5 s budget, with `run_s > 5 ⟺ failure` holding for 160/160** (that run is ~1.0 s on an idle
  box and was observed up to 15.8 s under the parallel suite). In every one of the 25 the delegate was
  never called at all (`release.wait(5)` returned False having waited exactly 5.0 s), `export_failures
  == 1`, `dropped_spans == 0`, `exported_spans == 34` of `accepted_spans == 35`, `force_flush`
  returned `True`, and 23 of 25 carried the durable `looplab.exporter.loss` receipt (the other two
  lost the receipt's own attempt to the same still-held stub) — i.e. the product behaved exactly as
  specified and the harness did not.
  **The product hypothesis was driven and REFUTED**: the suspicion was that `_active` clears before
  `_export_line` appends, so the barrier returns over an unwritten row. It does not — the wait clears
  only on `not queue and not _active and not worker_alive`, and a new tier-1 guard
  (`test_force_flush_waits_for_a_row_already_handed_to_the_writer`) drives it with the writer held
  open and no engine at all; mutating the barrier to a queue-only wait turns it red **10/10**, and the
  unmutated tree green **10/10**.
  **`248a81ca` is EXONERATED.** It was suspected on 5/55 (master) vs 0/50 (`0f20474f`, before it).
  Interleaved under identical load, 48 runs per arm: **5/48 on master and 5/48 on `0f20474f`**, same
  test, same mechanism, indistinguishable run-time distributions (median 2.03 s vs 2.10 s). Its only
  `tracing.py` hunk adds `build_trace` to the structural-attribute caps and cannot reach the exporter.
  The earlier 0/50 was below detection power at that batch's load, which is the trap this row records:
  **a wall-clock flake bisects to whatever commit was measured while the box was busy.**
  **THE FIX is two rules, both about where a harness may fail.** (1) A hostage Event released by a
  later phase of the same run is a DEADLOCK guard, not a synchronization budget — the wait is now
  `_HOSTAGE_DEADLOCK_GUARD_S = 120.0`, a bound no observed run approaches (max seen 15.8 s). (2) Never
  `assert` in the held thread: the outcome is RECORDED and asserted on the main thread FIRST, so a
  tripped guard says "the harness timed out" instead of "the barrier lost a row". The sibling
  `test_engine_run_terminally_drops_a_background_span_closed_after_return` had the identical shape at
  30 s (it failed once in the same 64 runs) and takes the same treatment.
  **Proved by DRIVING it, not by asserting it.** A ~10 % flake cannot demonstrate a fix at any
  affordable run count, so the trigger was made deterministic: a plugin sleeping 6 s once on the run's
  first `EventStore.append` — squarely inside the interval the hostage had to cover — applied
  identically to both trees. **Unfixed 20/20 FAILED, fixed 0/20 FAILED.** The natural-load arm is
  reported as the null it is: 64 runs per arm interleaved after the box quietened produced 0 failures
  on BOTH, which measures the load and not the fix, and is exactly the trap that made the original
  bisection look conclusive.
  **Alternatives rejected.** *Raise the budget to 30 s* — the same bug with a rarer trigger, and it
  leaves the mis-attribution, which is the part that cost a session. *Release the hostage from the
  test body after `anyio.run` returns* — that removes the wall clock but also removes the property:
  the whole point is that the row must land because the BARRIER released it, not because the run
  ended. *Make the exporter re-raise instead of swallowing* — that inverts a correct product rule
  (invariant: diagnostics never crash the observed operation) to suit a test. *Retry a failed delegate
  attempt so the barrier could promise durability* — refused for the reason already in the exporter's
  docstring: an exception after an ambiguous append would double-export the row.
  **Residue left open, deliberately.** The contract statements were made exact rather than the
  behaviour changed: `force_flush` settles each accepted row's ONE delegate attempt, not its success,
  and its `True` reports only whether the loss RECEIPT failed — a caller wanting "the artifact holds
  every span I queued" must read `export_failures` / the `looplab.exporter.loss` row. That is now said
  in `AsyncJsonlSpanExporter.force_flush`, `Tracer.force_flush`, `finalize._flush_trace_exporter`,
  `docs/08-tracing-architecture.md` and `docs/guide/concepts.md`, and pinned by
  `test_force_flush_returns_true_for_a_row_whose_one_attempt_failed`. **Still open:** no reader of
  `trace.json` surfaces a non-zero `export_failures` as a WARNING — the number is in
  `trace.json.summary` and nothing tells an operator to look, so the honest receipt is only as good as
  the habit of reading it. And the suite has no sweep for the general pattern (a wall-clock wait in a
  thread whose exception a production containment block swallows); these two were found by reading,
  not by a guard.
- **[FIXED 2026-08-15] `read_log`'s SEARCH could not reach the head of a big log, and the receipt
  that said so named two remedies neither of which could be spent.** `mode="search"` was
  `_read_window(where="tail")` plus a regex over the records that came back — a search of the LAST
  `max_bytes`, ceilinged by the READER's `_MAX_READ_BYTES` (32 MiB). So a log larger than that
  ceiling had a head that matched nothing at any parameter. **Measured on the corpus before anything
  was changed** (largest logs in `runs/`; there is no multi-GB one — the biggest is 88 MB):
  `rubertlite-dr-unified-v2` node 3, 87,949,008 B — searchable region bytes 54,394,576-end, **head
  61.8 % unreachable**; `rubert-dr-0807` node 1, 44,976,734 B — **25.4 %**; `rubertlite-dr-unified-v7`
  node 0, 31,386,860 B — fully covered (the threshold really is 32 MiB, i.e. the filing's "about
  32 MB" was right). `mode="head"` was not the escape the receipt claimed: head is not a search, it
  returns the first N records (33.5 MB is ~120,000 of them at 60 per call), and it reads UP from the
  floor by the same 32 MiB — so above 2x the ceiling there was a **20.8 MB band (23.7 %) of the 88 MB
  log no mode reached at all**.
  **What it cost, in one number off that log:** `CUDA-enabled jaxlib` is printed once per process
  start, so counting it counts the node's RESTARTS. It is at bytes 109 / 18,833,765 / 48,697,416 /
  78,537,354, and the windowed search at its maximum parameter reported **1 match** — "this node
  started once". `rubert-dr-0807` node 1: 2 of 3. And at the DEFAULT 256 KiB window, `Traceback` on
  v7 node 0 answers *"no record matches 'Traceback' in the 262,144 bytes read"* about a log whose
  byte 0 IS a traceback.
  **The claim as filed said the answer silently implied completeness; that half is WRONG and the
  correction is the design content.** The answer always printed its byte range and the unread head —
  which is why this survived the 2026-08-15 `_scan_reach` fix sitting next to it. What was wrong was
  the REMEDY: `raise max_bytes` offered to a caller already at the cap, and `mode="head"` for a mode
  that cannot search. A remedy that cannot be spent reads, to anything that believes its receipts, as
  "you have seen it all" — the identical defect `_scan_reach` had ("pass `whole_run=true`" to a caller
  who just had), one surface over. Rule 3 in `tools/log_tools.py`'s boundary now says so explicitly.
  **THE FIX** is `_search_scan`: a search is a SWEEP, streaming forward from the attempt floor (or
  `from_byte`) a `_SEARCH_CHUNK` at a time, **all the way to the end of the log**, bounded only by
  `_MAX_SEARCH_BYTES`. That ceiling's stop names a `from_byte=<n>` the sweep really examined up to, so
  paging past it is a call the caller can make; a sweep that reaches EOF says so, which turns "no
  match in the bytes read" into "no match ANYWHERE in this log". `LogSource.floor` is enforced at the
  seek, so no `from_byte` a model types reads a dead attempt.
  **THE FIRST CUT STOPPED EARLY AND THAT WAS WRONG, which is the second half of the design.** It ran
  only to the chunk holding its last showable match — ~13 ms per search at any file size — and bought
  two defects with the saving. (i) "N match(es)" became a FLOOR presented as a COUNT, i.e. the same
  class of statement as the "1 match" that opened this entry. (ii) It forced a choice between showing
  the OLDEST matches and the NEWEST, and neither is safe to make on behalf of a judge that fires on a
  TIMER: shown only the FIRST `Traceback`, it can kill a healthy node over a restart four hours ago
  that a repair already dealt with — the v7 failure rebuilt, a confident verdict drawn from a window
  that could not move — while shown only the last it cannot tell a run that has always been broken
  from one that just broke. A sweep that has seen the whole file owes neither choice, so it now
  states the exact total and renders BOTH ends with the elided middle counted: `bucket_series`'s
  reduce-never-drop, applied to hits instead of samples. The MIDDLE is also what closes when `_CAP`
  binds (`_render_search`), because `fit_rows` drops from the END — letting it decide would drop the
  newest matches while the count above still claimed them, i.e. re-introduce (ii) by another route.
  **Cost, re-measured 2026-08-15 for the always-sweep behaviour (warm, mean of 5):** `read_log`
  tail/head unchanged at 2.4-4.0 ms; a search costs the SAME whatever it finds, which is the point of
  not stopping — ~10 ms/MB, i.e. 138-150 ms / 564-580 ms / 879-905 ms on the 15 / 45 / 88 MB logs
  (thousands of matches, a few, and none, all within noise of each other), of which I/O is 15-25 %.
  Against ~54 ms/MB for a `metric_series` scan over the same bytes (one regex per record instead of
  four), so the worst case one search can cost at the 128 MiB ceiling is ~1.4 s, and the worst case
  one JUDGE TICK can cost is six sweeps of the corpus's largest log — ~5.4 s, **0.9 %** of the
  `train_monitor_interval_s=600` cadence. The early exit was buying 0.9 % of a tick at the price of a
  count a judge could not trust.
  **ALTERNATIVES REJECTED.** (1) *Raise `_MAX_READ_BYTES`* — a bigger silent window is the same defect
  further out, and this module already refuses that shape for `bucket_series`; the corpus's largest
  log grew 1.6x in one week, so any "largest plus room" number is a fact about today. (2) *Bind the
  sweep to `max_scan_bytes` and drop the third knob* — rejected because three questions want three
  bounds, and this whole defect IS one bound answering another's question; the two start at the same
  value and are two names. (3) *Pick an anchor — oldest-first or newest-first* — rejected as a choice
  that should not have been offered: see (ii) above. Showing both ends costs one full sweep, which the
  numbers say is 0.9 % of a tick. (4) *Keep the early exit and label the count "at least N"* —
  rejected because a floor is what a judge misreads, and the fix costs less than the disclaimer is
  worth. (5) *Run the regex over raw chunk text and only split records near a hit* (cheaper: the split
  is most of the ~10 ms/MB) — rejected because it silently changes the pattern's semantics: `.` does
  not cross `\n` but DOES cross `\r`, and this module splits records on both, so `error.*fatal` would
  start matching across the tqdm re-renders it exists to separate. (6) *Sweep by default at
  `metric_series`'s escalating-ladder shape* — rejected for the reason that ladder itself was removed:
  an unbounded window has nothing to discover and the rungs just re-parse the prefix.
  **RESIDUE LEFT OPEN, deliberately.** (a) `mode="range"`/`"head"` are still bounded by the 32 MiB
  window, so record 200,000 of an 88 MB log is still not addressable — that is a per-ANSWER bound
  doing its job, and search now names record numbers that `range` can take, but a `from_byte` for the
  window modes is unbuilt. (b) The elided MIDDLE is counted exactly but is not directly addressable:
  the remedies the line names (raise `lines`, lower `context`, narrow the pattern, sweep a region with
  `from_byte`) are all spendable, but there is no byte offset for "the 200th match" because the sweep
  tracks record numbers and not per-record byte offsets — `_RECORD_SPLIT` works on decoded text, so
  an offset derived there would be wrong on any multi-byte record. (c) `_read_window`'s `where="at"`
  branch is still dead code — the sweep does its own seek. (d) The 8x`_SEARCH_CHUNK` no-delimiter
  backstop can split one pathological 8 MiB record, so a match straddling that split would be missed;
  no log in `runs/` has a record within three orders of magnitude of it. (e) The judges' prompts
  (`_LOOK_INVITATION`, `_ASHA_LOOK_INVITATION`) already say "search for a traceback" and were left
  byte-identical — the sentence was aspirational and is now true, and a prompt change is a behaviour
  change that deserves its own measurement.
- **[FIXED 2026-08-15] The one role whose job is "why did this stage die" was diagnosing from 500
  CHARACTERS, and it read the wrong progress bar.** Every other reader of a live training log got
  widened this week — both watchdogs got `read_log`/`metric_series` (`train_monitor_tools`) precisely
  because a slice cannot answer the question they are asked. The CRASH/TIMEOUT triage judge was not,
  and its slice is the smallest in the engine: `engine/evaluate.py::_eval_failure_text` hands it
  `res.stderr[-500:]` with the failing stage's name prepended, where the watchdog's digest at least
  comes off a 128 KiB read.
  **Measured, on the live run.** `rubertlite-dr-unified-v8` node 3, stage `train`,
  `stage_finished.status="timeout"` at 22,003.138 s against its own declared 22,000 s ceiling. The
  node's 9.44 MB `train.log` (bytes 0..9,442,344 — attempt 6 begins at 9,442,344, timestamped
  08:13:35) holds **three** progress-bar lanes: `10590` (11,709 renders, last `10590/10590
  [5:29:35]`), `313` (907 renders, completed), and `361` (224 renders, last `223/361 [31:29<19:50,
  8.63s/it]`). Between the first and the last, in plain text 83,697 characters from the end:
  `{'train_runtime': 19775.2984, …, 'train_loss': 22.405478578158657, 'epoch': 14.98}` and
  `Started testing...`. Training finished all 15 epochs in 5 h 29 m; the kill landed ~20 minutes
  from the end of a RETRIEVAL phase. The durable `node_repaired` row for attempt 5 carries
  `error_in` of **522 characters**, and it is exactly `[failed stage: train]` plus the last two
  renders of the `361` lane. Its rationale: *"Timeout in train from R-Drop's ~10x per-step slowdown
  (node 3 is still in epoch 1 at 31:20 vs node 1's 15 epochs in 4260s). Reduce compute: halve
  per-step batch to 4096 with grad_accum 4 … and cut n_epochs 15->8"* — and `31:20` is verbatim the
  `222/361` render's elapsed field. **This was not the model ignoring what it had:** the tail it was
  handed contains neither `10590` nor the word `epoch`.
  **And the second cost is not the one it looks like.** That `n_epochs` 15→8 **never landed**: the
  repo's own rung caught it — `repair_verify` stamped `unmet: ['grad_accum', 'n_epochs']` on that
  same row — no repaired file sets it, and `vectorsearch/configs/config.yaml` still reads
  `n_epochs: 15`. The diff applied only `batch_size = 4096` + `gradient_accumulation_steps = 4`,
  which holds the effective batch at 16,384 and is compute-neutral by intent. (`grad_accum` is the
  known abbreviation FALSE POSITIVE that was fixed on 2026-08-15 — v8's engine loaded its source at
  16:25, so its rows were graded by the pre-fix rule; `n_epochs` is the true positive.) So the
  experiment was NOT silently altered, and what happened instead is worse for the clock: a wrong
  diagnosis bought a fix that is **inert against the actual failure**. Measured on the live log,
  attempt 6 is re-running the same 10,590 steps at `1928/10590 [57:46]` = 1.798 s/step, which
  projects 19,038 s of training plus ~3,058 s of retrieval at attempt 5's own pace = **22,096 s
  against the same 22,000 s ceiling**. The same 6.1 GPU-hours are being spent to reach the same kill.
  **[RETRACTED 2026-08-15 — the run falsified this projection while it was being written.]** Attempt
  6's `train` passed in **19,915.75 s**, `score` ran 3,130.3 s, and the node recorded **0.762048** to
  become v8's champion. The margin came from a SECOND edit in attempt 5 that this entry missed —
  deleting the in-`train` `test_model()` call, which moved the full-index retrieval out of the train
  budget into the protected `score` stage (attempt 4's `train.py` calls `test_model(...)`; attempt
  5's carries the import and a note that retrieval "is run independently"). The `n_epochs` cut still
  never landed. **The finding this entry is about is untouched**: the verdict came off a 522-char
  tail holding only the second progress bar and was wrong about where the time went. Being rescued
  by an unrelated edit in the same repair is not the diagnosis working.
  **Population, measured over all of `runs/`** (2,481 `node_repaired` rows, 14 runs). `reason` is a
  recent field: 12 rows carry `timeout`/`crash` (11 + 1), all in v7/v8. **Eight of those twelve make
  a claim about training PROGRESS or PHASE, and exactly ONE is contradicted by the node's own log** —
  this one. Widening to the rows whose failure kind is re-derivable from the adjacent
  `stage_finished` status adds five more progress/phase claims (v6 n5 a4 "trains stably to step
  4614/7060", v6 n0 a2 and v2 n3 a3 "training completed", v2 n0 a5 "the training loop finally started
  (1/28080 iters)", v7 n1 a3 "crash at step 50") and **every one of them is CORRECT**. So the honest
  count is 1, and the structure behind it is the finding: in all five correct cases the process died
  while the training bar was the last thing rendering, so the tail happened to be the right window.
  What is common is the SHAPE — **109 of the 109 stage logs over 200 KB in `runs/` carry more than
  one progress-bar lane** (measured 2026-08-15; it read 108/109 an hour earlier, before the live run
  appended a second lane to the last single-lane file, which is itself the point). What is rare, so far once, is a kill landing after the training lane
  completed. Nothing in a tail says which case it is in.
  **Fixed** by wiring the same tool provider into the triage path: `Settings.repair_log_tools` (ON;
  `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` row OFF) → `train_monitor.repair_log_tools`, which delegates to
  the watchdogs' own `_log_query_tools`/`monitor_log_sources` — ONE derivation of what is lookable,
  ONE reading of `attempt_byte_floor`. `train_monitor.needs_log_snapshot` was extracted out of
  `_evaluate`'s inline `or` because the repair path's snapshot has to be taken on behalf of a reader
  that has not asked yet (a "before" cannot be derived at the failure), and a rule buried in a local
  is one no test can reach — which is how the clause could have been dropped with every guard green.
  `UnifiedAgent._REPAIR_LOOK_INVITATION` is spliced at the same position pattern as `budget`/`depth`,
  so `repair_log_tools=false` reproduces the historical message byte for byte; `triage_look_invitation`
  is its own `PROMPT_KEYS` row for the same reason.
  **Cost, measured (mean of 5, warm, on node 3's real workdir).** Engine side, per FAILED attempt:
  `needs_log_snapshot` 0.001 ms, `eval_log_plan` 0.004 ms, `snapshot_training_logs` + plan + provider
  **1.35 ms** together. Tool side, on the real 10.0 MB `train.log`: `read_log` tail 7.1 ms, search
  8.0 ms, `metric_series` default 28 ms, whole-run hourly **525 ms** (the call that answers this
  question; it reads and states all 10,017,726 bytes). Turn grant `_REPAIR_LOOK_TURNS = 4`, ADDITIVE
  over a finite `agent_max_turns` and a no-op when the operator configured none — deliberately not
  the watchdog's 6, which is that judge's whole budget for a call with nothing else to spend it on,
  whereas this sits on top of one already sized for `read_code`/`find_analogous` and needs only the
  new shape (a whole-run series, one search, one follow-up, one spare).
  **Alternatives rejected.** (a) *Just make the tail bigger.* It would have worked HERE — the
  `'epoch': 14.98` line is 83,697 chars back, inside a 128 KiB tail — and it does not generalize:
  the whole-run scan above shows the answer can be anywhere, and 500 → 131,072 characters of tqdm
  fill is 26 renders of context per model call for a fact one aggregation answers. (b) *Teach
  `_eval_failure_text` to detect a phase change and say so.* That is a heuristic over the candidate's
  own text, in the one string that IS the engine's account of what went wrong; a wrong heuristic
  there is worse than no heuristic, and doc 36 says the engine should stop choosing what the role is
  shown rather than choose better. (c) *Give the DEVELOPER's repair call the tools too.* It is the
  role that wrote the wrong fix, but it goes through three unrelated adapters (whole-file, repo
  session, CLI-backed) and the misdiagnosis is the TRIAGE rationale; one seam, one measurement.
  (d) *Reuse `train_monitor_tools` as the switch.* Two paid surfaces on completely different
  cadences — ~200 timer ticks per node against one look per failed attempt — so an operator who
  turned one off has said nothing about the other.
  **Residue, left open.** (1) The triage judge can now SEE the log; nothing makes it look. The
  invitation is a prompt, and the v7 `_LOOK_INVITATION` measurement is the standing evidence that a
  model handed both a tail and a tool still reasons from the tail. There is no deterministic rung
  here the way `LossTrajectoryTracker` is one for the watchdog, and there deliberately is not: this
  judge's verdict decides nothing about the record, so a veto would have nothing to protect.
  (2) The 500-char tail itself is unchanged — this widens what may be ASKED, not what is handed over —
  so a run with `repair_log_tools=false`, or an eval that writes no named stage log, is exactly where
  it was. (3) The population number is 1, from a corpus where `reason` has only been recorded on two
  runs; it is a lower bound on a young field, not a rate.

- **[FIXED 2026-08-15 — and the earlier filing's central claim was FALSE] A repair changed a
  hyperparameter that is PART OF THE COMPARISON, and nothing in the record said so (P1, M).** This
  row was filed the same day saying *"on node 3 the change did not land"*, on the strength of the
  attempt-5 rationale (*"cut n_epochs 15->8"*), the `unmet: ['grad_accum', 'n_epochs']` verdict
  beside it, and a `config.yaml` that still reads 15. Every one of those facts is correct and the
  CONCLUSION drawn from them is not: it looked at the epoch claim and never at the parameter pair
  the SAME node's attempt 4 really did move. **IT LANDED, ON THE CHAMPION.**

  **The measurement, re-derived from the run directory and the event log.**
  `runs/rubertlite-dr-unified-v8` node 3 (R-Drop α=0.5 over node 1's DCL loss) is the run's best at
  **0.762048** — +0.0236 over node 1's 0.738425, with the other four evaluated nodes (0.736689,
  0.737647, 0.738174, 0.738425) inside a 0.0017 spread. Three records of that one experiment
  disagree: `node_created.idea.params` says `train.training.batch_size 8192.0` /
  `gradient_accumulation_steps 2.0`; `vectorsearch/configs/config.yaml` says `8192` / `2`; and
  `vectorsearch/train.py:31-32`, written by attempt 4 after `config = Config()`, says **4096 / 4**.
  The training log settles which one ran — the config dump the script itself prints reads
  `"batch_size": 4096, "gradient_accumulation_steps": 4`. **The science is fine and the repair said
  so** (8192×2 and 4096×4 are the same 16,384 effective batch); the RECORD is not, because neither
  the event log nor the node's own config states what ran. Only the code does.

  **The coordinates are LOAD-BEARING, which the original filing's sketch left as an open design
  question rather than an established fact.**
  `idea.params` is what `core/numeric.py::numeric_params` projects for `search/surrogate.py`,
  `search/panel.py`, `search/proxy.py`, `search/archive.py`'s niches and `engine/novelty.py`'s
  distance — all of them OFF or pass-through on this run's settings (`surrogate_proposer=false`,
  `policy=greedy`, `researcher_panel=1`, `novelty_gate=false`, `proxy_scoring=false`), which is why
  "decorative" was a live possibility worth checking. It is not decorative, and the proof is on the
  same run: **node 8 is a `search/operators.py::merge_idea` mean-merge of nodes 3 and 1** — an
  ENGINE-authored arithmetic node, rationale `"mean-merge of nodes 3,1"` — and it was minted with
  `batch_size 8192.0` / `grad_accum 2.0`, the mean of two 8192s and two 2s. Had node 3's coordinates
  been the ones it ran, node 8 would have been minted at **6144 / 3**. Those params then travel into
  `adapters/repo_developer.py`'s "implement … with parameters {…}" prompt, i.e. into the code the
  sandbox runs. Beyond proposal: `search/coverage.py`'s param-niche count (a recorded
  `coverage_snapshot`), the run-end archive summary, the champion notebook `cli/export_cmds.py`
  exports, and the `"Best so far: node N params={…}"` line `agents/roles.py` shows the Researcher.
  Nothing here moves a METRIC, a CHAMPION, SELECTABILITY or a VIOLATION — champion selection is by
  metric alone — which is exactly the boundary the fix had to stay inside.

  **AND THE ENGINE CREATED THE INCENTIVE.** The repair's own comment names the reason it did not
  edit the config: *"Config is pydantic-mutable, so this is a train.py-only change (no config.yaml
  edit) that leaves the completed `mine` stage reusable."* `eval_stages.py::_safe_reuse_start` fails
  closed on ANY non-`.py` change, node 3's `mine` stage had just cost **2,304 s**, and the log shows
  it `reused` at 0.0 s on each of the two attempts that followed. So the reuse rule paid 4,608
  seconds of GPU time for moving a comparison-bearing parameter out of the declared config and into
  code no record reads. That is what upgrades this from "an agent was sloppy" to a defect of the
  system, and it is the half the first filing did not have.

  **POPULATION, measured over all 46 preserved logs (297 nodes, 2,481 `node_repaired` rows).**
  Nodes whose committed `.py` code contradicts a dotted declared parameter: **ONE**, node 3, and it
  is the champion. Repairs that introduced one: **ONE**, attempt 4. The denominator is small and is
  quoted with it — 25 of the 297 nodes declare a dotted numeric parameter at all, all on
  `rubertlite-dr-unified-v{2,6,7,8}`; the toy and benchmark spaces declare bare names. TWO adjacent
  populations were measured and are NOT this defect. **(i) The YAML-CONFIG route: FIVE nodes** —
  `rubertlite-dr-unified-v2` node 3 (`loss.temperature` 0.05 declared / 0.01 in
  `train.loss.temperature`), v7 node 1 (`batch_size` and `gradient_accumulation_steps`), v7 node 2
  and v8 node 9 (`n_epochs`), and v8 node 0 (`batch_size` and `n_epochs`) — **all five already
  diverging at `node_created`**, so this route is Developer-authored and not repair-authored. The
  rule matters and is stated because two obvious ones are both wrong: anchoring the declared key at
  the YAML ROOT misses v2 node 3, whose `loss.temperature` sits under `train.`, while matching on the
  LAST COMPONENT alone convicts that same node four more times against `adapter.training.*` and
  `test.retriever.*`, which are different config sections entirely. The number above uses the
  contiguous-SUFFIX rule the shipped code rung uses, over every YAML in each working set.
  **(ii) One bare-name conditional override**: `rubertlite-dense-retrieval` node 36, `distill_alpha`
  declared 0.5 and `train.py:117` assigning 0.0 inside the `elif teacher_ckpt …` missing-teacher
  fallback — present at creation, zero repairs.

  **THE FIX IS (a): SAY IT, in the two records a reader meets.** Both derive from the DECLARATION
  (`Idea.params`, minted into `node_created` by the Researcher and never by a repair) and the
  committed BYTES — `engine/repair_verify.py::declared_param_overrides`, which never reads a
  rationale at all and so sits in `REPAIR_INERT`'s trust tier rather than `REPAIR_UNMET`'s.
  (1) `node_repaired.param_overrides`, additive and fold-ignored, carrying the ATTRIBUTION half
  (only what THIS repair introduced), rendered into the judge's repair history beside `changed` and
  `unmet`. (2) `engine/champion_caveats.py`'s third slug `params_overridden` on every `/api/runs`
  row, asked of the FOLD so any replay recomputes it — the surface that sketch had already named
  ("rendered beside the metric the way `best_metric_caveats` renders the salvage and trust-gate
  caveats"), reached by the derivation it could not find.
  Bounds, each measured rather than chosen: a declared key must carry ≥2 dotted parts
  (`PARAM_OVERRIDE_MIN_PARTS` — a bare `lr` would be met by any local of that name, and the corpus's
  one instance of that shape is conditional code); the target is parsed with `ast`, never matched by
  pattern, so a comment, a docstring, a string literal and an `==` all read as nothing; the declared
  parts must be a contiguous SUFFIX of the assignment target, so only the receiver's own name is
  ignored; and both values must be numeric literals, because constant-folding agent code would make
  this a second evaluator. The claim is deliberately NARROWER than the story: *the node's own
  committed code assigns this declared parameter a different literal*, never "this is what ran" —
  a dead branch is indistinguishable to any static reader, and absence is silence, never a
  certificate. **Replayed over all 46 logs: ONE thing moves**, v8's `best_metric_caveats`
  `[]` → `['params_overridden']`. Every champion, every metric, every feasible set and every
  violation row is byte-identical. `tests/test_repair_verification.py` and
  `tests/test_champion_metric_caveats.py` drive it at tier 1 with node 3's real bytes as the fixture
  and its siblings' as the negative control; eight mutations of a throwaway tree were checked red.

  **WHAT WAS REJECTED, and why each rejection is a measurement and not a preference.**
  *(b) let a repair AMEND `idea.params`, or write an `effective_params` beside them.* Refused.
  `params` is the operator/Researcher's DECLARATION and it is the input to selection-adjacent
  machinery — `merge_idea` does arithmetic on it, the surrogate fits on it, the archive buckets by
  it. docs/36's line is that what enters the RECORD stays deterministic over AUTHENTICATED evidence,
  and a value parsed out of an agent-authored diff is not authenticated: the agent writes the code,
  so it would be writing its own coordinates, and a coordinate is a lever on which experiments get
  proposed next. `effective_params` is the same object with a softer name — the moment any reader
  prefers it, the agent has an amendment channel. The asymmetry is the point: a CAVEAT can only ever
  subtract confidence, an amendment can add it.
  *(c) loosen `_safe_reuse_start` so a config-only edit keeps reuse.* Refused, and the corpus decides
  it. Of the 2,481 repair rows, 76 carry a non-empty change set and **17 change a non-`.py` file**;
  of those 17, only **3 had a completed earlier stage to forfeit** (`stages_passed ≥ 1`: v2 node 4
  attempt 1, v8 node 0 attempts 3 and 4) — **and all three also changed `looplab_stages.json`**,
  which rewrites the pipeline's argv and is refused by a separate clause that cannot be loosened at
  all. So the widening would have bought **ZERO** reuse across every repair this box has ever made,
  at the cost of the invariant. It is also unsafe on this very node: `mine_stage.py` reads the same
  `config.yaml` (its `n_negatives=2` and mining `batch_size 4096` live there), so a config-only
  exemption would have scored a stale `mine` artifact on node 3 itself. `_safe_reuse_start`'s own
  docstring already refused the `needs`-based version of this on 2026-08-14 for two independent
  reasons — `needs` is a PRECONDITION and not a bound, and 2 of 129 stages declare it — and both
  still hold. The rung that would make it safe is ENFORCEMENT (`runtime/read_allowlist.py` +
  `runtime/landlock.py` scoped to a stage's own declared inputs), which is where that file is
  already waiting; this row does not move it.
  *(d) state the residue and do nothing.* Refused because the residue is not hypothetical any more:
  it is the run's champion, and the number is being published.

  **WHAT IS LEFT OPEN, measured rather than assumed.** The CONFIG-FILE route (five corpus nodes) is
  not covered — the rung would have to know which file a pipeline reads and by what key path, which
  is `_safe_reuse_start`'s own unsolved problem one file over. Neither is a bare-name declaration, a
  non-literal right-hand side (`batch // 2`, `args.bs`), an override expressed through the stage
  manifest's argv (three corpus instances, all `rubertlite-dense-retrieval`), or an assignment in a
  branch that never executes — the last is why the wording is about the code and not about the run.
  And nothing here reaches the STRONGER form that sketch named, holding such a node out of
  same-family ranking: that would be selection machinery driven by a static read of agent-written
  code, which is the line this fix exists to stay behind.
  **Evidence:** `runs/rubertlite-dr-unified-v8` node 3 — `node_created` seq 1510 (the declaration),
  `node_repaired` attempt 4 seq 1983 (the change), `vectorsearch/train.py:31-32` and
  `vectorsearch/configs/config.yaml:281,283` (the disagreement), `train.log` (what ran), the
  `stage_finished` rows `{mine ok 2304.266s}` then `{mine reused 0.0s}` twice (the incentive), and
  node 8's `node_created` (the propagation).
  **One adjacent claim was falsified while re-deriving this and is deliberately NOT fixed here** — it
  belongs to the `repair_log_tools` change that measured it. Six sites (the row above in this file,
  `docs/guide/configuration.md`'s `repair_log_tools` row, the process diagram's `e_ir` block,
  `core/config.py`, `engine/train_monitor.py` and `docs/guide/llm-and-agents.md`) project node 3's
  attempt 6 at **22,096 s into the same 22,000 s ceiling**, extrapolated live at 1.798 s/step before
  it finished. It finished: `train` **ok in 19,915.75 s**, then `score` ok in 3,130.3 s, and the node
  recorded 0.762048 and became champion. The `n_epochs` cut still never landed; what moved the
  retrieval cost out of the train budget was a SECOND edit in attempt 5, deleting the in-`train`
  `test_model()` call on the note that the full-index retrieval "is run independently by the
  protected `score` stage". Only `llm-and-agents.md` is corrected in this change, with a pointer to
  the other five.

- **[FIXED 2026-08-15] A superseded prefetch retired its IDEA, and the board then forbade
  re-proposing it.** The Layer-5 refund returns the node SLOT of a speculative build the Card
  freshness gate discards before dispatch; nothing returned the HYPOTHESIS. The discarded node stayed
  in `Card.evidence`, and **three** readers key on that one list, so one never-executed build retired
  the question three ways: `search/card_selection.py::_strictly_selection_ready` wants
  `not card.evidence` (and `events/card_ledger.py::_apply_card_selection_readiness` derives
  `owner_state="terminal"` from the same list and stamps the `work_terminal` blocker), so the Card
  lane can never elect it again; `RunState.open_research_beliefs` filters on `if c.evidence`, so it
  leaves the CLAIMABLE untested feed; and `agents/roles.py::attempted_board_prompt_cards` admits it
  and renders it under *"each already has an experiment — do NOT propose one of these again as if it
  were new"*, closed by *"A failed experiment is re-attempted by the engine itself, under the same
  card, without being asked."* **Both sentences are false about a build that never ran** — nothing
  re-attempts a superseded prefetch — so the board did not merely forget the idea, it instructed the
  one role that could have re-proposed it not to. **The 2026-08-14 card-lane change did not fix this
  and the earlier filing's terminal detail is stale**: a superseded card now reads `failed` rather
  than `evaluated`, which is a truer word for the same retirement; driven post-merge, the card was
  still `selection_ready=False`, blockers `['work_terminal']`, `eligible_cards` without it, and still
  in the do-NOT-repropose block. **Measured:** `rubertlite-dr-unified-v7` permanently lost five
  directions — card-3 "hard-negative mining" (deep-research memo `memo:sha256:10f4085b…`), card-4
  "label smoothing", plus CCR reweighting, multi-positive averaging and R-Drop.
  **THE FIX is at the one place the false statement is minted** (`events/card_ledger.py::
  _apply_unexecuted_discards`, a new `derive_cards` phase between the merge fold and verdicts): a node
  `core/models.py::is_unevaluated_speculative_discard` PROVES never ran leaves `evidence` for the new
  additive `Card.discarded_nodes`, so all three readers become correct at once with no second
  vocabulary for "evidence that is not evidence" — the same argument
  `node_counts_toward_card_budget` makes for living in `core`. Nothing is un-written: the ids are
  published on the wire (`serve/public_cards.py`) so the operator still sees which Developer builds
  the run paid for and threw away, and enrichment ATTRIBUTION (`node_to_card`, footprint,
  `research_origin`) walks `evidence + discarded_nodes`, because attribution is not evidence and a
  returned card must keep the memo it was proposed from.
  **THE BOUND IS ONCE PER CARD** and it is the design content: a returned idea that is re-elected and
  re-superseded burns a Developer build each time. One supersede is a statement about FRESHNESS AT A
  MOMENT and says nothing about the idea; a second is a durable, twice-repeated fact that this card
  cannot be built inside this board's rate of change, which IS information about the card. At two
  discards NONE is forgiven — a COUNT, never a choice of which one, which is what keeps the phase
  order-tolerant — the card reads `failed` and retires for good. Worst case two Developer builds per
  card, under `refunded_node_reservations`' one-whole-budget cap. Subsumption gets no special case: a
  subsumed card simply loses the next election to whatever subsumed it and sits on the board costing
  nothing. **Resurrection was chosen over visibility-only** because the loss is not merely invisible,
  it is a false RECORD-side statement (docs/36) that an operator cannot correct by reading it — the
  proposal prompt actively fences the direction off. **`gated` stays unreachable and it is provable,
  not argued:** the filter fires only when the discards are the card's WHOLE evidence set, so the one
  transition it can cause is `failed` → `proposed` via the empty-`ev_nodes` branch.
  **Re-derived over all 46 preserved runs (270 cards, 212 nodes): 9 cards in 4 runs change, every one
  `failed` → `proposed`; zero `gated` cards move and zero champions move.** Six of the nine become
  selection-ready; the other three come back carrying the pre-existing `freshness_stale` blocker, so
  the board really had moved past them and no new rule was needed to say so — including the LIVE
  `rubertlite-dr-unified-v8`'s card-2, which returns `freshness_stale` and is therefore not
  re-electable, so a resume of that run on this code elects nothing new. Driven end to end in
  `tests/test_unexecuted_discard_returns_the_idea.py`: a real engine, a real supersede, the return
  read off `eligible_cards` AND the real proposal prompt; the bound driven by superseding the SAME
  card twice and asserting the third turn buys no build at all; a negative control where the node's
  eval-start boundary WAS appended (still terminalized, still charged, still retired); the `gated`
  guard and the order-tolerance of the count driven on the phase itself. Verified non-vacuous by
  mutating a throwaway copy three ways — phase removed, bound widened to `>= 1`, whole-set
  requirement dropped — each caught by exactly its own test.
- **[FIXED 2026-08-15] The developer probe's rule 2 ("no write ANYWHERE") was overclaimed: two
  filesystem-mutating calls were covered by NEITHER of its two mechanisms**, and one of them creates
  a regular zero-byte file. Found by the merge-day review, reproduced by driving a real
  `DevProbeTools`. `tools/dev_probe.py` enforced rule 2 with a CPython audit hook (a file's
  EXISTENCE) and `RLIMIT_FSIZE 0` (its CONTENT in the kernel) — **`os.mknod` and `os.mkfifo` raise no
  audit event at all, and the rlimit bounds bytes, not existence**, so both succeeded OUTSIDE the
  disposable replica while `open(<source>/train.py)` was correctly refused. **Cost:** `os.mknod`
  yields a REGULAR ZERO-BYTE FILE, which satisfies a `needs`/`expect` PRESENCE check and, dropped
  into an editable root as an empty `.py`, shadows a real module for every later node in the run —
  and the probe emits no domain event precisely BECAUSE rules 2-4 are supposed to make it
  side-effect-free, so a write through this hole was invisible to the record.
  **THE SET WAS RE-DERIVED, NOT PATCHED FROM THE TWO NAMES.** The whole audited-event surface was
  measured with a recording hook in a subprocess on CPython 3.12.11 — `read_fence.MUTATION_EVENTS`'s
  own method, for its own reason — over every path-based mutating call in `os`, plus the fd-based
  ones, `shutil`, `socket`, `sqlite3`, `tempfile`, `pathlib`, `ctypes` and four native writers. Three
  populations came out: **raises NOTHING** — `os.mknod` (all of S_IFREG/S_IFIFO/S_IFSOCK, and via
  `dir_fd`), `os.mkfifo` (same), and every NATIVE writer (`pyarrow.parquet.write_table` raises ZERO
  audit events; `h5py.File(p, "w")` raises none naming the file; `ctypes`->`libc.open(O_CREAT)`);
  **raises an event nothing was checking** — `sqlite3.connect` and `socket.bind` on an `AF_UNIX`
  path; **covered** — everything else, `os.unlink`->`os.remove`, `os.replace`->`os.rename`,
  `os.lchown`->`os.chown`, `os.ftruncate`/`fchmod`/`fchown`->their path events, every `shutil` entry
  lowering to an `os.*` or `open`, and every fd-based write reachable only through an audited `open`.
  Driven through a real probe before the fix, 7 of 14 aimed at a directory outside the replica went
  THROUGH with `exit=0`, and `sqlite3`/`pyarrow` left a 0-byte file behind even while dying on
  SIGXFSZ.
  **THE FIX IS A THIRD RUNG AT THE KERNEL, and the argument against the alternatives is the reason.**
  `runtime/landlock.py` gains a second ruleset SHAPE that is the inverse of its read allow-list:
  `NO_MUTATION_HANDLED` handles every filesystem-mutating access right and grants NOTHING, spliced
  into `dev_probe`'s own generated launcher by `no_mutation_source()` (in-process, before the program
  — the launcher is already a fresh single-threaded interpreter, so the `preexec_fn`-under-a-threaded-
  parent deadlock `_RLIMIT_LAUNCHER` records does not arise and no extra exec is paid). *Pre-empting
  the two names at the `sitecustomize` seam was rejected as the boundary*: it is exactly the denylist
  this module's docstring refuses, and it was measurably incomplete on the day it would have shipped —
  `pyarrow.parquet.write_table(t, "torch.py")` creates the identical 0-byte regular file with no
  Python-level name to rebind. *Narrowing rule 2's claim* was the honest fallback and is not needed,
  because the boundary exists: the kernel's path walk does not care which language asked. *An
  allow-list-shaped Landlock ruleset* (the `Settings.landlock` shape) is wrong here for the reason
  that keeps THAT one opt-in — an empty allow-list denies everything including reads, an enumerated
  one fails closed in an unpredictable place, and nobody has run one through a real GPU eval; this
  ruleset handles no READ bit, so it cannot refuse a read, and grants nothing, so it has no list to
  enumerate. It gets **no `Settings` field** (which would revoke every issued calibration receipt) and
  is always on. The seam rebinding is KEPT and demoted to the MESSAGE rung, stated as such in three
  places: the kernel answers `EACCES` -> `PermissionError` -> an `OSError`, the silent-skip shape this
  surface's refusal type deliberately is not, so where a Python name exists the probe still raises
  `LoopLabProbeRefused`. Same division of labour the hook already had with `RLIMIT_FSIZE`.
  **Driven** in `tests/test_dev_probe.py`: every spelling found (6 mknod/mkfifo variants, three native
  writers, `AF_UNIX` bind, `ctypes`->libc) refused from a REAL probe with the target absent from disk
  afterwards — the assertion is the disk, never the message; the refusal proved not to be caught by
  `except OSError:`; `_UNAUDITED_MUTATORS` RE-DERIVED from the interpreter over 17 `os` mutators, so a
  third unaudited call goes red; and the negative controls — the editable source read still refused
  and still not an `OSError`, a declared `data:` mount still readable, `cat` still unable to reach the
  source, `CUDA_VISIBLE_DEVICES=''`, the staged replica still importable. A 19-program battery of
  realistic probe work is **byte-identical** to the pre-fix tree except one joblib warning whose errno
  moves EFBIG -> EACCES (the same refusal, from the neighbouring rung). Invariant #6 checked by
  folding all 46 preserved event logs (19,739 events) in both trees: identical.
  **Residue, stated not patched:** Landlock is the only one of the three rungs that can be absent (a
  kernel < 5.13), and there the EXISTENCE half falls back to what CPython audits — the launcher prints
  ONE line on the probe's own stderr naming that, rather than running under a quieter version of the
  same sentence. ABI 2 mediates neither `chmod`/`chown`/`utime` nor `ftruncate` on an open fd (no
  ownership/mode rights; `FS_TRUNCATE` is ABI 3) — all four raise their own audit event and the hook
  refuses them, which is the complementarity working in the other direction. And rule 3's half of the
  `ctypes` residual stays open: the WRITE half is now closed (driven, `libc.open(O_CREAT)` returns -1
  and creates nothing) but `execve` is not a filesystem mutation and ABI 2 does not mediate it.
  `docs/38-fence-coverage-audit-2026-08-13.md` is a dated audit and was deliberately NOT rewritten;
  this row is the record of what its map missed.

- **[FIXED 2026-08-15, same day — a REGRESSION shipped that morning] The transparent-launcher
  registry was inert for 8 of its 10 members** (found by the merge-day review, re-derived against the
  base commit). `adapters/repo_task.py::_TRANSPARENT_LAUNCHERS` was a `frozenset` and the walk
  `return []`d on the first token that was neither `python*` nor a bare launcher name — i.e. on the
  launcher's **own flag**, which is how eight of the ten are normally invoked. Introduced by
  `cd0b11df` ("the re-review of my own fixes found ten defects"), which correctly replaced an
  unbounded scan for a python-looking token — the `bash run_eval.sh --interpreter python3 --cfg
  configs/base.py` hole — with a head anchor, and overcorrected. **Reproduced by driving it:**
  `chrt -f 99 python score.py`, `env -u PYTHONPATH python score.py`, `nice`/`srun`/`stdbuf`/
  `taskset`/`time`/`ionice` all went `['score.py']` -> `[]`; only the flagless `nohup`/`setsid`
  still resolved. `[]` means `_entrypoint_protect` returns `{}`, so the scorer was in neither
  `protected_names` nor the seeded set and `write_file(<scorer>)` SUCCEEDED — the
  `rubertlite-dr-unified-v6` node 4 incident that function exists to prevent. Green because
  `tests/test_eval_entrypoint_protection.py` exercised only the three flagless spellings.
  **Exposure was LATENT, not live, and that measurement is what shaped the fix:** all 21
  argv-shaped values across the 46 `runs/*/task.snapshot.json` and every `examples/` task are headed
  by a bare `python` — zero launcher use, zero corpus tasks affected in either direction.
  **THE FIX is a per-launcher prefix table** (`_Launcher`, one record per program, each field
  transcribed from that program's own `--help` on this box), and the argument for why that is *not*
  the "own flag grammar" the docstring refuses is stated there and DRIVEN: the registry refuses
  `torchrun`/`accelerate`/`deepspeed` because THEIR grammar decides *which token is the script*, so
  a wrong arity yields a confident non-empty WRONG file; a transparent launcher never chooses the
  script, so its table decides only where to start looking and two independent filters catch a wrong
  entry (the token at the computed start must match the interpreter regex; the first non-flag token
  after it must end in `.py`). 392 single-field corruptions of the table over 19 argvs: 67 lost
  protection, **0 named a different file**. Unknown option tokens FAIL CLOSED to `[]` + the existing
  warning, which is what lets `srun` carry an empty option set honestly (Slurm's is large,
  version-dependent, and srun is not installed here, so only self-contained `--opt=value` spellings
  are read through). **Three alternatives were weighed and rejected.** A `-`-flag-only skip fixes 4
  of 8 and cannot fix the other 4 at all: `chrt <priority>`, `taskset <mask>` and `nice -10` are
  POSITIONAL/obsolete-form grammars, not flag arities, so "skip the flag" lands on `99`/`0-7`. A
  registry SHRUNK to the members handled without any table is `nohup`/`setsid`/`env`-with-assignments
  only — it satisfies the honesty bar but drops `srun`, the launcher with the strongest real case,
  since a cluster eval is exactly where re-running a scorer is expensive. Making
  `eval_entrypoint_unprotected` a REFUSAL would refuse **0** of the 14 corpus eval commands, which is
  not evidence it is safe — it is evidence the corpus (one operator, one project, all bare `python`)
  cannot calibrate it — while banning `torchrun`/`accelerate`/`deepspeed` and console-script evals
  outright, the majority shape of real distributed ML eval, for a defect with zero live instances.
  Driven in `tests/test_eval_entrypoint_protection.py`: all ten launchers through the REAL write
  gate (a `write_file`/`edit_file` on the scorer is refused), a `python score.py` negative control,
  the hole re-tested behind each launcher most likely to front a shell script
  (`nohup bash run_eval.sh --interpreter python3 …` still answers `[]`), the fail-closed spellings,
  a registry-vs-coverage guard so a member cannot be named without being driven, and the mutation
  proof above. Verified as a negative control against a throwaway copy of the pre-fix tree: 28
  failures, exactly the 8 launchers reported. Docs moved in the same change
  (`docs/guide/tasks.md`, `docs/guide/generating-code.md`). **Residue, stated not patched:** the
  table is per-program and this box's coreutils/util-linux; a busybox or BSD spelling of the same
  option falls to `[]` + warning rather than to a wrong file, which is the designed direction.

- **[FIXED 2026-08-15, same day] The repair critic's VERDICT is not recorded — only that it was asked** (observed on
- **[FIXED 2026-08-15, same day] Fifty full pipeline re-runs where there were zero, and
  `inline_repair_retrain_cap` charged none of them** (found by the merge-day COMPOSITION review —
  three same-day changes, each individually documented and defensible, whose product nobody could
  see from one branch). (a) `inline_repair_attempts` 12 -> 0 (F8: the bound became a judgment);
  (b) `_effective_repair_cap(0)` -> `_UNLIMITED_REPAIR_CEILING` (50), which is a TIGHTENING of the
  `or 10**9` it replaced; (c) `triage.py::_rule_triage` turned the no-judge path's `abandon` for a
  NON-MECHANICAL crash into `repair` (F5 deleted the Debug node that had made `abandon`
  conservative). **Driven, not argued** (`tests/test_first_stage_repair_cost.py`, over the real
  `_evaluate` loop with only the subprocess layer stubbed): with no triage model wired — a supported
  configuration, `crash_repair.py::_triage_crash`'s own "a configuration, not a failure" branch — a
  first-stage `RuntimeError` buys **51 full pipeline evaluations, 50 repairs, `full_retrain_charged`
  = 0**, every one a full re-run (`start_stage=None`). **NONE OF THE THREE IS WRONG AND NEITHER IS
  THE CHARGING RULE.** `_repair_forces_full_retrain` asks "is completed EARLIER-stage work being
  discarded?", answers False for a first-stage failure because nothing completed, and delegates:
  "an ordinary retry, bounded by the attempt budget like any other". THE DELEGATION IS WHAT MOVED —
  when that sentence was written the attempt budget was 12 and was the TRANSITION; F8 made it 50 and
  moved the transition to a judgement. **And the critic is not there.** `repair_critic_after=3` is
  the answer to "the 50 is bounded in practice", and it is true on `rubertlite-dr-unified-v8` (6
  consultations) — but `crash_repair.py::_repair_critic` reads `researcher.repair_critic`, the SAME
  object that would have carried `triage_crash`, so in the configuration that produces the 50 there
  is no critic either: driven, 50 consultations all returning `no repair critic wired`. The 50 is
  not a floor beneath a judgement there; it is the entire policy. **And it is live**: v8 node 3 spent
  four repairs on a first-stage (`mine`) failure — attempts 1-3 with `stages_passed: 0` — for
  4911 + 1168 + 504 + 2984 = **9567 s of wall clock, `full_retrain_charged` = 0**, on a run whose
  `max_eval_seconds` is `None`, i.e. with no cost floor over that chain at all.
  **THE FIX IS NOT the obvious one, and the live record is what rejected it.** "Charge the cap on ANY
  repair that re-runs a stage that already ran" was DRIVEN on a mutated tree: it abandons v8 node 3
  at three repairs, one before its `mine` stage passed on the fourth. The count is calibrated for a
  re-TRAIN (that manifest declares `train` at 22000 s) and the work being redone is a 5400 s `mine`;
  routing one into the other charges a cheap thing at an expensive thing's rate. So the ledger stays
  a COUNT of discarded re-trains and the SAME operator number is ALSO spent in the unit the
  first-stage case actually costs: `repair_judgment.repair_redone_work_stop` stops a chain that has
  spent `(cap + 1)` times what the task DECLARES one full pipeline costs
  (`declared_pipeline_seconds` over the stage `timeout`s, or the single-command timeout). Durable
  across a resume via a new additive `node_repaired.eval_seconds` + `_durable_repair_seconds` — a
  bound a resume refunds is not a bound. It FAILS OPEN twice: `cap = 0` stays the documented
  unlimited, and an eval declaring no timeout licenses no number and is not bounded, because a floor
  that guessed a pipeline's cost would abandon nodes on a number nobody wrote. On the driven runaway
  it binds at 15 repairs instead of 50; on v8 node 3's real numbers it does not fire (9567 s against
  a 93000 s allowance). **No `Settings` field** — the field SET is unchanged at 211 against master,
  so no calibration receipt is revoked and neither pin moves — and the replay is proved: folding the
  live v8 log on both trees gives the byte-identical `RunState` digest
  `sha256:12d8fc0098cd3b8b7aa65fa9c745a3d90c46ea37f263afb6aad664a7890912cc`.
  **Residue, stated not patched:** the critic still cannot answer the cost question even where it IS
  wired — `format_repair_trajectory` renders cause / stages_passed / fix / changed / stderr and no
  seconds at all, so a chain that is genuinely progressing and genuinely unaffordable looks the same
  to it as one that is progressing and cheap. The seconds floor bounds that from underneath; nothing
  yet lets the judgement see it.

- **The repair critic's VERDICT is not recorded — only that it was asked** (observed on
  `rubertlite-dr-unified-v8`, 2026-08-15). `engine/evaluate.py:2127`'s `repair_critic` span carries
  `{attempt, node_id, generation}` and nothing else, and a `continue` verdict appends no event at all;
  only a STOP is visible, and then only indirectly, as the `abandon` triage_action it produces. So an
  operator auditing why a repair chain ran to six attempts can see the critic was consulted and cannot
  see what it decided or why. Measured on that run: 6 consultations, 0 stops, and the chains were in
  fact progressing (node 3 went semantic-failure -> logic edit -> its own code bug -> fix -> `mine`
  PASSED), so the verdicts were right — which is exactly the case where nobody notices the record is
  missing. This is the same shape as the calibration-receipt rejection that was misreported twice in
  one day because the mechanism said nothing about its own reasoning; that one was fixed by making the
  refusal name its cause. F8's whole premise is that a JUDGEMENT replaces a counter, and a judgement
  that leaves no trace cannot be reviewed, tuned or trusted.
  **VERIFIED FIRST:** `runs/rubertlite-dr-unified-v8`'s `spans.jsonl` holds `repair_critic` spans
  whose whole attribute map is `{attempt, node_id, generation}` with `events: []`, and its
  `events.jsonl` contains the string "critic" **zero** times.
  **THE FIX is a durable DIAGNOSTIC row, not a richer span** — `events/types.py::
  EV_REPAIR_CRITIC_VERDICT`, appended from `_evaluate`'s attempt loop under `_write_lock` on EVERY
  consultation, carrying the verdict, its rationale, `after`/`durable_repairs` (the cadence that
  fired beside the chain it fired on) and `judged`, the authenticated per-attempt causes it
  compared. The span gains the same three attributes, but it cannot BE the record: `spans.jsonl` is
  optional (tracing may be off) and destroyable (`serve/trace_clear.py` is an operator button,
  `reset_transaction.py` removes it), and three of the six outcomes never open a span at all.
  Folding it was rejected on doc 36's line — a `continue` moves nothing, so folding would create
  `RunState` derived from an LLM verdict, one refactor from a selection reader.
  **A second vocabulary came out of the measurement:** `repair_judgment.py::CRITIC_SOURCES`
  (`model` / `not_wired` / `no_trajectory` / `unreachable` / `no_verdict` / `out_of_enum`), because
  the critic FAILS OPEN and so "6 consultations, 0 stops" reads identically whether the critic
  approved six times or never spoke once — which is the fact that actually blocks calibrating
  `repair_critic_after`. Engine-minted at each return, disjoint from `CRITIC_ACTIONS`,
  `TRIAGE_ACTIONS` and `REPAIR_VERDICTS`, and rejected by both coercions; the guard asserts all of
  it. Driven both ways plus the negative control and a three-part selection-neutrality proof (fold
  equality with the rows removed, splice-position tolerance at every offset, and the paid-proposal
  CAS fence unmoved) in `tests/test_repair_judgment.py`. Docs moved in the same change
  (`docs/guide/configuration.md`, `docs/infographic/agent-architecture.html`).

- **[FIXED 2026-08-15, same day] The Metrics tab called a number `measured` that the engine refuses
  to call measured** (found by the merge-day record-honesty review). `ui/src/Inspector.jsx::Metrics`
  built the objective ★ row with no source of its own and rendered a HARDCODED `measured` under the
  tooltip *"read by the operator's own metric spec on the protected score stage"* — for every node,
  including the ones `trustSemantics.nodeFeasibilityStatus` describes one tab over as *"Metric
  salvaged, not measured"*. **Reproduced by driving the real component** (SSR-loaded, four records,
  pre-fix): a `metric_salvaged` node, a `metric_subject_unbound` node, a `metric_salvage: "select"`
  node — the rung where a salvaged number COMPETES FOR CHAMPION and mints no violation row at all —
  and a clean node all rendered the BYTE-IDENTICAL cell. So the tab an operator opens to read
  numbers flattened exactly the distinction the salvage/subject vocabulary exists to draw: the one
  between `runs/rubertlite-dr-unified-v6` node 4's recorded 0.225 (a human's checkpoint an absolute
  path in an editable config pointed at, `train.log` RECALL@100 0.726) and a number measured on what
  a node produced. **THE FIX is the vocabulary, not three special cases:** `trustSemantics.js` gains
  `objectiveMetricSource(node)` / `objectiveSourceHelp(source)` — a pure model beside the React half,
  driven by `node --test`, because the decision was inline in JSX and nothing in the suite MOUNTS
  `Inspector.jsx`, which is why this survived. THREE channels, `measured` | `salvaged` |
  `measured, no subject`, and the third is deliberately NOT the second: an unbound metric WAS
  measured by the protected scoring path and nothing was recovered, so "salvaged" is a false
  accusation one condition over — the trap `isUnboundSubjectViolation`'s own comment records.
  `salvaged` is one word for both salvage rungs, because `select` removes the EXCLUSION and not the
  fact; the tooltip carries which rung, which stage failed, and which subject rule. Two states are
  labelled `measured` on the record's own authority and both are argued in the model: a CONSTRAINT
  violation (a fact about a bound — feasibility is an EVERY-row question, "what is this number" is an
  ANY-row one) and `declaration_repaired` (`salvaged: False` is spelled out and the engine's docstring
  says it "reads as measured everywhere"; the tooltip carries the correction). Driven in
  `ui/test/metricsTabObjectiveSource.test.js` — 9 model cases plus the component RENDERED through
  `renderToStaticMarkup`, with a genuinely-measured node as the negative control, and the render half
  fails on a mutated copy of the tree carrying the pre-fix row. **Residue, stated not patched:** an
  unbound subject under the non-enforcing `metric_subject` rungs (`audit`/`off`) mints no row and is
  NOT labelled — that is 82 of 83 preserved corpus metrics, so a label there fires on the rule rather
  than the finding, and it would put this tab back in disagreement with the Trust tab, which reads
  the same record as "Feasible". And `panels.jsx::ParetoPanel` populates its front with
  `n.feasible !== false`, so a `select`-admitted salvaged node's metric is ranked there with no
  caveat at all — the same defect one surface over, now one `objectiveMetricSource` call from fixed.

- **[FIXED 2026-08-15, same day] The Pareto front ranked a salvaged number beside a measured one,
  and the salvaged one deleted the measured one from the table.** `panels.jsx::ParetoPanel` populated
  its front with `paretoMetric(n) != null && n.feasible !== false`. That filter is right about the
  `audit` rung — a salvage ROW makes the node infeasible and it never reaches the panel — and blind to
  the one that matters: `metric_salvage: "select"` mints NO violation row, so the node arrives
  `feasible: true`, competes for champion, and was rendered exactly like a measurement while the
  Metrics tab one tab over already called it `salvaged`. **DRIVEN, PRE-FIX, and it is worse than a
  missing word:** a run with a measured 0.51 and a salvage-admitted 0.81 rendered ONE row —
  `#4 👑 0.81` — because a Pareto front does not merely rank, it DOMINATES; the measured result was
  nowhere on screen. The operator opens this panel to decide which configuration to REUSE.
  **CAVEAT, and deliberately NOT unrank.** `crossRunRank.js`'s "shown, valued, NO rank" precedent (a
  prefix-folded run) does not transfer, on three grounds the record settles: (1) a competition rank is
  a per-ROW fact — drop one run and every other rank is unchanged — while Pareto membership is a
  RELATION over a SET, so removing a point from the domination test puts OTHER nodes on the front and
  publishes a front the record does not support; (2) under `select` the ENGINE ranks this node (it is
  in `feasible_nodes` and may be `best_node_id`), so a front that dropped it would omit the run's own
  champion while still drawing its crown — the two-surfaces-one-record defect rebuilt; (3) `select` is
  the operator's own recorded decision that this number competes, and the panel's job is to show what
  they admitted, not to overrule it. What the precedent DOES give is its other half — a fact that
  would otherwise be invisible stays on screen — so the panel now names the front restricted to
  MEASURED objectives whenever a caveated node is on this one (`#3 (0.51) is non-dominated on measured
  objectives alone`). The label is PRINTED as well as hovered, for the reason the extras' footnote
  already exists. Same call, same sentences, same vocabulary: `trustSemantics.js` gains only
  `objectiveSourceCaveated(source)`, hoisted out of `Inspector.jsx`'s inline
  `channel !== OBJECTIVE_MEASURED` so the reading surface and the DECIDING surface cannot come to mark
  different nodes; it answers FALSE on junk, because a caveat nobody recorded must not be invented.
  Two more surfaces in the same file were fixed with the same call: `RegistryPanel`'s **Champion (this
  run)** metric (under `select` the champion is precisely the node that can be salvaged, and that rung
  mints no row, so nothing on that panel could have said so) and the diversity-archive elites, the
  panel's other reuse-this-configuration table. Driven in
  `ui/test/paretoFrontObjectiveSource.test.js`: the predicate's truth table + totality, the front
  ARITHMETIC (that the caveated point really does displace #3), and four records rendered through
  `renderToStaticMarkup` — including the negative control (two measured nodes: no label, no `warn`, no
  footnote, number unchanged), the `audit` rung (still absent from the front, as the engine intends), a
  LEGACY row carrying the salvage violation with no `feasible` field at all (marked, because the label
  comes from the record and not from the flag), and a breached BOUND (unmarked, because feasibility and
  "what is this number" are different questions). The render half fails on a mutated copy of the tree
  carrying the pre-fix panel (4 of 7 red); the staged `vite build` is clean.

- **The objective-metric census: 40+ surfaces render a run's primary metric and, as of 2026-08-15,
  four consult the vocabulary** (swept while fixing the Pareto front). Only `Inspector.jsx`
  (Metrics ★ row, Trust tab), `panels.jsx` (`ParetoPanel`, `RegistryPanel` champion, archive elites)
  do. The rest split into two populations and the split is what matters, because one is fixable in the
  browser and one is not:
  * **The record IS on the payload** (live `/state` nodes and `/nodes/{id}` keep `violations` and
    `metric_provenance` through `model_dump`), so these are one call from correct and are NOT fixed
    here: **`Dag.jsx`** is the worst of them — `dagFeasibilityLabel` has three answers and none is
    "salvaged", so a `select`-admitted node's accessible name literally reads *"feasible"*, its card
    prints the number unmarked, and the only caveat rung (`infeasible`) is keyed on the exact
    condition `select` removes; **`Report.jsx::ChampionCard`** prints `feasible: yes` for a salvaged
    champion; **`report.js`** writes the champion's number into `buildModelCard`'s JSON artifact and
    the Markdown export with no channel, and its `trustCaveats` — the designed home, which already
    enumerates reward-hack / leakage / drift / infeasible / single-seed — has no salvage caveat at
    all, so the deterministic verdict both exports embed is silent; **`charts.jsx`**'s running-best
    frontier states the false invariant in a comment (*"mirroring engine selection … so the line never
    claims a best the engine rejected"*), true under `audit` and false under `select`;
    `panels.jsx::OverviewPanel`'s best-metric Stat, `TrustPanel`'s seed-luck leader,
    `HyperImportancePanel`'s population, `CardBoard`'s attempt chips, `grouping.js`'s terciles and
    `util.js::parentMetric`'s delta are the same class-(b) gate.
  * **The record is NOT on the payload, so no client fix is possible**: `/api/runs` rows
    (`run_projections.py`) carry `best_metric`/`best_confirmed` and no `feasible`, no violations, no
    provenance — which is `RunList`, `runIndex.js::sortRuns`, `portfolioModel.js`, `RunCompare`,
    `MapView`, `conceptForest.js::nodeBest`, `crossRunRank.js` and `CrossRunPanel`; and ConceptFrame
    `refs` (`concept_frame.py`) stop at `feasible`, which is `ConceptView`'s whole rollup. **And
    `st.best()` selects from `feasible_nodes()`, which a `select`-admitted salvaged node is a member
    of** — so `best_metric` can already BE a salvaged number, portfolio-wide, unlabelled and
    underivable. Both need a server-side field first; that is the next unit of work, and it is one
    field, not thirteen client fixes.

- **[FIXED 2026-08-15, the same day the census above named it — and the measurement narrows the
  claim, which is the half worth keeping] `/api/runs` published `best_metric` and nothing that could
  qualify it, so a caveated champion was portfolio-wide, unlabelled and underivable.** The census
  entry above is right that no client fix reaches this: `run_projections.py::run_summaries` builds
  the row from a fold and keeps the number alone — no `violations`, no `metric_provenance`, no node
  id, no `reward_hacks` — while `RunState.best()` reads `best_node_id`, which `_select_best` derives
  from `promotion_eligible_nodes`, i.e. from whatever THAT run's rungs were willing to crown. **Two
  rungs crown a caveated number and only one of them needs a setting changed:** `metric_salvage:
  "select"` over operator-produced output mints NO violation row, so a metric recovered from a FAILED
  eval is feasible and competes; and `trust_gate: "audit"` — **the shipped default** — makes
  `flagged_node_ids` empty, so a node with a high-precision reward-hack/leakage signal is excluded
  from nothing and can be champion with the signal recorded all along. The second is the wider hole
  and the census did not name it.
  **MEASURED FIRST, over the 46 preserved runs in `runs/` (the same `fold` `/api/runs` serves), and
  the claim came back NARROWER than stated:** 37 runs carry a `best_metric` and **ZERO of them are
  caveated today**. The corpus holds exactly ONE salvaged node — `rubertlite-dr-unified-v6` node 3,
  0.728113, condition `artifact_contract` — and its producer is `agent_stage`, which
  `SalvagedMetric.violation_rows` keeps excluded under every rung but `off`; re-running the fold's own
  `_select_best` over all 46 logs with the `select` rows removed moves **0 champions**, even though
  that node's number beats its run's champion (0.727991). Two runs carry any `reward_hack` row and one
  (`task-g7` node 1) carries a HARD signal; neither run's champion is the flagged node. So "can
  already BE a salvaged number" is REACHABLE and not REALIZED, and this fences a state rather than
  cleaning up a corpus — said here because the census's wording reads as the stronger claim.
  **THE FIELD IS NOT `memory.py::unreliable_metric_ids`, and that is the design decision.** That join
  is the obvious candidate (it already unites the salvage and trust families for the cross-run
  writers) and its intersection with `{best_node_id}` is EMPTY under every rung **as a theorem**:
  `metric_unmeasured` needs a `metric_salvaged` violation row, a violation row makes the node
  infeasible, and `SearchFitness.eligible` requires feasibility; `flagged_node_ids` is non-empty only
  under `gate`/`block`, and `_select_best` passes exactly that set in as its exclusion. A field
  stating that join would be a constant `false` on the one row it decorates. What `engine/
  champion_caveats.py::champion_metric_caveats` states is the **complementary half of each of those
  two members** — salvaged-and-ADMITTED where the join has salvaged-and-excluded, flagged-and-NOT-
  ENFORCED where it has flagged-and-enforced — so it is the same join read on the other side of the
  selection boundary, and both halves are spelled as CALLS to that join's own two primitives
  (`metric_unmeasured`; `hard_flagged_ids` minus `flagged_node_ids`) rather than as a fresh reading of
  `violations` or `reward_hacks`. That is what stops it drifting from the rung that decides it, and
  the rung is `violation_rows`, whose whole job is deciding whether a row exists.
  **Two slugs and deliberately no third.** A REPAIRED DECLARATION (`declaration_repaired`, the F1e
  re-check) is a measured metric and `trustSemantics.js` already answers `measured` for it, so marking
  it here would put the portfolio row in disagreement with the node tab — the exact defect this
  vocabulary closes, one direction over. An UNBOUND METRIC SUBJECT under `audit`/`off` is not marked
  either, and that is the negative control with teeth: the LIVE `rubertlite-dr-unified-v8`'s champion
  (node 1, 0.738425) carries `metric_provenance = {"subject_bound": false, "unbound_reason":
  "not_declared", "subjects": []}`, which is the state 82 of 83 corpus metrics are in, so a caveat
  there would fire on the rule and not on a finding. Under `require` that rung mints a row and the
  node is infeasible — the first theorem again. Additive with a reader-side default (invariant #5),
  no `fold` change, no new `run_started` key (which would revoke every issued speculation-calibration
  receipt), and computed inside the summary cache's miss branch, so it costs one derivation per
  changed log rather than one per poll.
  **CLIENTS WIRED: two, and the rest are stated rather than started.** `runIndex.js` gains the reading
  rule and the wording (`bestMetricCaveats` / `bestMetricCaveatNotice`, one home, the shape
  `sourceIncomplete`/`sourceIntegrityNotice` already have for the integrity receipt beside it), and
  the two surfaces taking it are `RunList`'s run card (a `warn` pill above the `nodes · direction`
  line, beside the `incomplete record` receipt) and the **cross-run leaderboard**
  (`crossRunRank.js` + `panels.jsx::CrossRunPanel`) — the surface that makes a claim an operator ACTS
  on, since they go and reuse the winning configuration. There the decision is **CAVEAT, NOT UNRANK**,
  and it is deliberately the OPPOSITE of the `sourceIncomplete` rung one line above it in
  `buildGroup`: a prefix-folded value is not that run's best and no ordering may use it, while a
  caveated value IS that run's best, crowned by the run's own selector under a rung the operator
  configured — dropping it would overrule a recorded decision and publish a leaderboard that
  disagrees with the runs in it. The row keeps its rank, gains a marker in the OBJECTIVE cell (not the
  status column: a qualifier a column away from the number is one nobody reads), and the group's
  refusal list gains a line naming how many rows are caveated and **whether one of them leads**.
  `groupClaim`'s existing provenance refusal was NARROWED by exactly the width of what the row now
  carries — the metric SUBJECT is still not published there, so it keeps its docs 31/35 example and
  loses only the sentence that became false. **LEFT OPEN, honestly:** `portfolioModel.js`,
  `RunCompare`, `MapView`, `conceptForest.js::nodeBest` and `runIndex.js::sortRuns` (which sorts on
  `best_confirmed ?? best_metric` and now COULD refuse to rank a caveated row, a decision with the
  same caveat-vs-unrank argument and no measurement behind it yet) all read the same row and are one
  call from correct; ConceptFrame `refs` (`concept_frame.py`) still stop at `feasible` and need the
  same treatment on a DIFFERENT payload, which is a second server-side unit of work and not this one.
  **PROVED** in `tests/test_champion_metric_caveats.py` (tier 1 throughout: a real `events.jsonl`
  written with `EventStore`, folded by the real `fold`, projected by the real `run_summaries` off a
  real `make_app` AppState — and the salvage rows are ASKED of `SalvagedMetric.violation_rows` rather
  than hand-written, because the whole finding is about which rung mints a row) plus
  `ui/test/bestMetricCaveats.test.js` (reading rule + leaderboard model) and
  `ui/test/crossRunCaveatRender.test.js` (the panel rendered through a JSDOM client root). Negative
  controls in both halves: a measured run gains no label INCLUDING the v8-shaped champion above (the
  naive "it has provenance, so qualify it" implementation turns every run on the box orange, and that
  case catches it), the `audit` rung leaves the row uncaveated because it left the node unselected,
  `gate` needs no caveat because it moved the champion, an advisory `perfect_metric` is not a caveat,
  and a measured leaderboard renders byte-for-byte what it did. **Non-vacuity verified by mutating a
  throwaway copy of the tree** (`git archive HEAD | tar -x`): dropping the projection field reds 7 of
  10 python tests; the naive-provenance rule reds exactly the negative control; **stating the JOIN
  instead of its complement reds 5, including the theorem test** — which is the docstring's claim
  driven rather than asserted; a browser slug renamed reds the cross-language vocabulary check; and
  the pre-fix `crossRunRank.js`/`panels.jsx` red 5 of 8 UI tests while the rendered negative control
  stays green. Docs moved in the same change (`docs/guide/ui.md` — including the panel's own
  description, which still said it "does not rank, crown" after the 2026-08-14 ranking landed —
  `docs/guide/tasks.md`, `docs/guide/configuration.md`'s `metric_salvage`/`trust_gate` rows,
  `docs/guide/architecture.md` and the `e_sal`/`t_gate` blocks of
  `docs/infographic/agent-architecture.html`). **Residue, stated not patched:** an empty list is not a
  certificate — `reward_hack_detect` is OFF by default, so the trust member is silent on almost every
  run on this box, and only what a run RECORDED can be reported.

- **[FIXED 2026-08-15, same day — and the fix cost nothing, which is the part worth keeping]
  `metric_series(whole_run=true)` could not reach the head of any log over 33.5 MB, and said the
  opposite** (found 2026-08-15 auditing the merge day's `tools/log_tools.py`). `_MAX_SCAN_BYTES`
  (128 MiB) bounds a whole-run SCAN and `_MAX_READ_BYTES` (32 MiB) bounds ONE `read_log` answer —
  two bounds answering two questions — but `_read_window` clamped every read by the READER's number,
  so the escalation ladder walked up to a ceiling it could not spend, re-read the same 33.5 MB twice,
  and nothing below `size - _MAX_READ_BYTES` was reachable at any parameter. **Reproduced, driven:**
  on `rubertlite-dr-unified-v2/nodes/node_3/train.log` (88 MB) the head **61.8 %** was unreadable and
  the answer's own receipt read *"earlier bytes not scanned — whole_run=true reads from the start"*
  to a caller who had just passed `whole_run=true`; `rubert-dr-0807/nodes/node_1/train.log` (45 MB),
  the first 11.4 MB. **What it cost, measured on that log:** the truncated series reports
  `first=5.8873 last=5.7632 net=-0.1241` — a loss that has barely moved over four hours — and the
  whole log reports `first=13.1645 last=5.7632 net=-7.4013`. Sixty times the movement, same bytes,
  same call, to a judge whose question is "is this run learning?" and whose verdict can kill a
  multi-hour GPU node. **THE FIX:** `_read_window` takes the CALLER's ceiling as a required
  parameter (a default would restore exactly this), and an unbounded window skips the ladder — it
  has nothing to discover, and each rung re-PARSES the prefix. `_MAX_SCAN_BYTES` did NOT move: it is
  a bound on the judge's request path, not on the corpus, and at the measured 51 ms/MB of record
  split + regex it is ~6.9 s worst case against a `train_monitor_interval_s=600` cadence, covering
  the largest log 1.5x over. **Cost of reaching 100 % instead of 38 %: none** — the same 88 MB log
  now answers in 1 read / 4.9 s where the broken 6-read ladder took 4.7 s to cover a third of it,
  because I/O is 5-16 % of a scan (geesefs 101 MB/s cold, ~300 MB/s warm) and the ladder was already
  paying for the whole file. Above the ceiling the head IS unreachable and the receipt now says the
  thing that changes how the series is read — *"this series does NOT start at the run's start … the
  first bucket is a MID-RUN value"* — rather than naming a remedy already spent. Driven in
  `tests/test_log_tools.py`, including against the real 88 MB log with the truncated answer kept as
  the negative control; all five tests fail on the pre-fix tree. **Residue:** `read_log` mode=search
  is a TAIL read bounded by `_MAX_READ_BYTES`, so the START of a log over 32 MB is unsearchable
  (head/range/tail all still reach their own end). Left as stated rather than fixed: the answer is
  capped at 100 hits / `_MAX_LINES` records regardless, so widening buys reach for one mode at the
  price of the per-answer bound's whole rationale.

- **[FIXED 2026-08-15, same day] `looplab landlock-check` verified a ruleset containing none of the
  operator's mounts** (found 2026-08-15 the same pass). `cli/inspect_cmds.py` read `task.get("repo")`
  — which is the repo ROOT PATH, not a spec. **Censused over all 46 preserved
  `runs/*/task.snapshot.json`: 45 `NoneType`, 1 `str`, 0 dicts.** So `spec` was always `None` and the
  mounts were dropped, or — for the `str` shape, which ships in `examples/repo_composable_task.json`
  and one preserved run — it reached `read_allowlist.mount_sources` and raised
  `AttributeError: 'str' object has no attribute 'get'`. **Reproduced:** `landlock-check
  runs/rubert-dr-0807` printed 16 rules holding neither of that task's two real declared mounts
  (`/home/jovyan/data/datasets/dense-retrieval/rubertlite`, the base model) and then `skipped: 0`,
  exit 0; a run dir with NO `task.snapshot.json` at all also printed a green list and exit 0. This is
  the documented gate for flipping `Settings.landlock` to enforce, so what follows that green light
  is a real GPU eval under a kernel allow-list built without the dataset — and a missing mount does
  not degrade, it `EACCES` mid-training. No test drove the command body. **THE FIX:** the spec now
  comes from where the ENGINE gets it — `TaskAdapter.repo_spec()` over the re-validated snapshot
  (`existing_run=True`, as `resume`/`finalize` do), because `engine/resources.py::_landlock_allow`
  passes exactly `self._repo_spec` and a gate that derives its input differently from the thing it
  gates is not a gate. It PRINTS the declared mounts and their count before the allow-list (`0` on a
  repo task is the fact the operator checks against their own `data:` block), marks one that is not
  on this box, and FAILS CLOSED with a `ConfigRefusal` (one line, exit 2) when the snapshot is
  missing or no longer rebuilds — "this task declares no mounts" and "I could not look" must not
  print the same. A task that genuinely has no repo spec names its KIND, so that line cannot be
  confused with the refusal. Five driven tests in `tests/test_metric_subject.py` (the read
  allow-list's own section), all five failing on the pre-fix tree; `docs/guide/cli-reference.md`
  gained the command, which it had never documented.

- **[FIXED 2026-08-14, same day — kept as the record of what a content-derived tag cannot do]
  The `engine` extra-metric channel authenticated against bytes the candidate authors** (found
  2026-08-14 auditing the merge day against docs/36). **THE FIX, and the argument for it:** the
  narrowing this row rejected (exact key SET + static values) would not have helped — those values
  are public constants too, so a forgery just prints them. NOTHING derivable from an artifact the
  candidate writes can authenticate its author, so the fact is now CARRIED instead of re-derived.
  `runtime/sandbox.py::stdout_extra_metric_channels` lost its `code` parameter and answers `auto` for
  everything (it is handed an opaque string and runs it); `core/models.py::
  apply_engine_extra_metric_channels` grants the upgrade at `engine/eval_dispatch.py`, the one place
  `node.code` and the engine's own wiring meet, behind a REQUIRED `engine_authored` keyword with no
  default that only `engine/speculation_gate.py::engine_authored_artifacts` may assert — true in
  exactly the calibration profile, whose Developer is the engine's probe splicer (exact type +
  `calibration_gpu_probe`, written only by `cli/__init__.py::_make_calibration_roles`). The prefix
  check survives for its real job: naming WHICH engine artifact this is, hence which keys are its
  own. The probe keys still arrive and `_validate_cuda_probe_artifact` still accepts (driven), so no
  issued receipt moves and no `Settings` field was added. Driven both ways in
  `tests/test_auto_extra_metrics.py`: the same bytes evaluated by two engines differing only in their
  Developer record two different channel maps. The original report follows. `core/calibration.py:221
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

- **[FIXED 2026-08-14, same day] The repair-rationale intake cap was raised at the wrong layer, so
  `_TRIAGE_RATIONALE_CAP` never bound** (found 2026-08-14 auditing 4b2bd547). **THE FIX:** the cap
  moved to `engine/triage.py::TRIAGE_RATIONALE_CAP` — the module `agents/unified_agent.py` already
  reaches through its existing function-local import for the verdict registry, so no new layering
  edge — and `UnifiedAgent.triage_crash`'s emit finalizer now reads that SAME constant instead of its
  own `[:300]`. Both caps are one object, so they cannot disagree again. The sibling caps
  (`repair_critic`, `choose_action`) deliberately STAY at 300, decided rather than inherited: what
  made this one wrong was a downstream rung READING the string, and nothing reads those.
  `tests/test_repair_verification.py` now drives the real seam (patching only the documented
  `looplab.agents.agent.drive_tool_loop`) with the pre-fix configuration reproduced as a negative
  control, which is what proves it traverses the finalizer at all. The original report follows. That commit moved the intake bound to
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

- **[FIXED 2026-08-15, partially] `repair_verify`'s `unmet` was right ONCE in the first four verdicts
  it produced on a live GPU run, and one of its two false-positive shapes was undocumented**
  (measured 2026-08-15 over every `node_repaired` in `runs/`). The rung shipped 2026-08-13 and
  `rubertlite-dr-unified-v7` + `-v8` are the only runs it has actually graded: **4 `unmet` rows, 1
  true positive.** The three misses are three different mechanisms and only one of them was known.
  (a) **Truncation, still live.** v7 node 0 attempt 2 is stamped `unmet ['nll_cos']`; the full
  690-char rationale recovered from `spans.jsonl` scores `verified`. The intake fix (`2e898b7f`,
  above) landed 2026-08-14 22:33 UTC and v8's engine process started at 16:25 — **a running
  interpreter does not reload its source**, so every v7/v8 verdict was computed on a 300-char prefix
  and will be until those runs restart. Nothing to patch; the docstring now says that a verdict on a
  row written before the restart is a verdict about a prefix.
  (b) **The cited baseline** — the residue `repair_verify.py`'s docstring named and then dismissed
  with "on the corpus zero of the four surviving `unmet`s is that shape". **That sentence is
  falsified**: v8 node 3 attempt 2 was convicted on `mining_type` / `n_negatives`, both inside "Node
  1's identical mining config (…) already passed and reached 0.7384", and re-measured over the whole
  tree 4 of the 14 surviving `unmet` verdicts convict on evidence rather than a promise. FIXED for
  ONE of the two sub-shapes, in the direction the docstring itself prescribed: `_CITATION_RE` +
  `_is_citation_only` demote to **`unstated`**, never to `verified`, never by dropping the token from
  `claims`. Deliberately narrow — a clause bound to another NODE/RUN/ATTEMPT and nothing else, not
  the "vs"/"unlike"/"compared with" phrasing list the original text rightly refused. The other three
  cite the CRASH (`pos_scores_broadcast` / `s_dd_local` off a shape-mismatch message, a 0-byte stub
  parquet, the stage a timeout moved to) and are LEFT, because "the crash is in X" sits on a continuum
  with the dense-retrieval node 11 family where the repair then edited a different file and saying so
  is the rung working.
  (c) **An abbreviation of the identifier the code uses** — a shape the docstring did not name at
  all. v8 node 3 attempt 4 promised "halve per-step batch to 4096 and raise `grad_accum` to 4" and
  its diff sets `batch_size = 4096` and `gradient_accumulation_steps = 4`. FIXED by
  `_abbreviated_identifier`: part-wise prefix matching against identifiers the DIFF contains, bounded
  three ways because this is the only rule here that can reach `verified` — never for a FILE claim,
  ≥2 underscore parts of ≥3 chars each, and at least one part must actually be SHORTENED (without
  that last bound `mine_stage`, which `_IDENT_RE` extracts from the file claim `mine_stage.py`, is
  met by any `mine_stage_helper` in the diff and the true positive silently becomes `verified`).
  **Replayed over all 2,480 `node_repaired` rows on both text bases (4,959 verdicts): exactly three
  rows move** — v8 n3 a2 `unmet`→`unstated`, v8 n3 a4 `unmet`→`verified`, and v6 n1 a1 keeps `unmet`
  with `train.py` dropped from the reported list. **Zero `inert` verdicts move, so no
  `INERT_REPAIR_LIMIT` chain and no stop fires differently**, and v8 n3 a1 — the true positive — is
  untouched. **Corrections to the figures the docstring quotes:** v4's event log is GONE from the box
  (v5's already was), so "2,477 repairs / 134 model-authored / 126 concrete" cannot be re-derived;
  the five surviving runs give 2,444 / 101 / 92 and the whole tree 2,480 / 137 / 125. 2,343
  boilerplate is exact. 13 inert → 11. And **"38 named a concrete change that appears nowhere in the
  diff" is not the count of `unmet` verdicts** — it counts rows with at least ONE absent token, while
  `verify_repair` needs EVERY claim absent; the verdict counts are 7 (five-run, full text) and 17
  (whole tree, durable text). **STILL OPEN, measured not assumed:** the 14 surviving `unmet`s split
  7 / 2 / 5 — seven genuine, two withdrawn here (plus one row's list shortened), and five left: the
  three crash-citation rows above, a NEGATED claim satisfied by indirection (v6 n1 a1 "drop those
  unsupported args", kept by deleting the `"%params%"` placeholder) and an exception name read as a
  claim (`IndentationError`). Four of the seven "genuine" ones are arguable: the
  `rubertlite-dense-retrieval` node 11 family names the BROKEN component and edits a different file,
  which is a diagnosis rather than a promise — left `unmet` because a claim-clause whitelist would
  demote all four and turning "you touched the wrong file" into "nothing to check" is the worse
  trade. Also unchanged and now pinned: `_claim_met`'s base test is a plain SUBSTRING, so `mine_stage`
  has always been met by a `mine_stage_helper` in the diff; tightening that is a change to how every
  claim is scored and owes its own corpus replay.

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

- **~~The assistant's containerized shell is unhardened~~** (filed 2026-08-14; **re-derived, corrected
  and CLOSED 2026-08-15**). The filing said "none of the container tier's limits reach it" and that
  the plumbing `mem`/`cpus` "also lack today". Both are wrong, and the second is what kept the row
  open: `engine/eval_dispatch.py` had threaded `sandbox_memory`/`sandbox_cpus`/`sandbox_readonly_rootfs`
  since they shipped — only the assistant surface had no plumbing. **Measured** (argv built from the
  real builders on shipped `Settings`, docker CLI absent on this box so nothing beyond argv is
  claimed): the shell got `--rm --network none --pids-limit 1024 --cap-drop ALL --security-opt
  no-new-privileges`, because `make_docker_wrap` routes through `sandbox.docker_run_argv` and those
  flags are unconditional there. What it did NOT get was the CALLER-supplied column — `--memory 4g`
  (the shipped default, present on the eval tier beside it), `--cpus`, `--read-only`/`--tmpfs`, and
  the operator's `docker_image`. **The filing missed the worst one**: under `trust_mode="hostile"` it
  also got no `--runtime runsc`, so the operator who chose the true-isolation tier ran the one surface
  that executes `git`/`pytest`/`pip` on a shared kernel. Severity is still bounded the way the filing
  says — an approver gates it and it is the operator's own chat, not candidate output.
  **Fix: the shared derivation, not a fifth keyword argument.** `runtime/sandbox.py::docker_tier_kwargs`
  is now the ONE `Settings` -> container translation (image, mem, cpus, readonly rootfs, and what
  `hostile` MEANS via `HOSTILE_RUNTIME`), consumed by all three surfaces — `cli/__init__.py`'s
  `make_sandbox`, `eval_dispatch`'s `make_docker_wrap`, and `ShellTools`. **Alternatives rejected:**
  (a) *pass the five values at the third call site* — that is this repo's recurring defect with a
  longer argument list, and it would have put a second copy of `"runsc" if hostile` in `tools/`;
  (b) *state a narrower boundary honestly instead of fixing it* (document the assistant shell as
  "container-shaped, not tier-grade") — rejected because the flags were already 5/9 present, so the
  surface was not a different boundary but the same one missing its configured half, and a doc saying
  so would have to be re-derived by every reader; (c) *refuse when a shell gets no `Settings`* —
  rejected in favour of resolving `None` to `Settings()`, so an unconfigured surface gets the SHIPPED
  container rather than a weaker one. **Proved by driving it**, not by a source pin:
  `tests/test_docker_hardening_parity.py` now builds all three tiers from ONE `Settings` and asserts
  every boundary row on each, with the assistant tier captured at `sandbox.run_argv` through the real
  `ShellTools.execute` -> permission gate -> wrap path. Non-vacuity checked by mutating a throwaway
  tree four ways (revert the wrap construction / drop the trust-mode override / weaken the no-settings
  fallback / drop `settings=` in `serve/assistant.py`): 12, 1, 1 and 1 failures respectively. Worth
  recording that the pre-fix construction restored *with a comment carrying the string
  `docker_tier_kwargs`* still passed the source-scan test in that same file and was caught only by the
  driven ones — the CLAUDE.md rule, observed.
  **Residue deliberately left open.** (1) *Nothing here was run against a daemon.* There is no docker
  CLI on this box (`which docker` -> not found), so every claim is about the argv `docker run` would
  be given; the `docker`-marked tests skip here and the property is asserted at argv level, which is
  the same level the two pre-existing tiers were ever asserted at. (2) *The parity table is still a
  hand-written tier list.* It has three members because someone noticed the third; a FOURTH
  containerized surface would be invisible to it in exactly the way the assistant shell was. The
  source-scan test over the three known translation sites is a partial hedge and is evadable by a
  comment, as above. Deriving the tier list from the tree (every `make_docker_wrap`/`DockerSandbox`
  construction) was not attempted. (3) *`--network` is not in the bundle* — it is not a `Settings`
  field and all surfaces take the `none` default, so an operator who needs egress still cannot ask
  for it, and this change did not invent a knob for that. (4) *The shell's mount root is
  `roots[0]`*, which for the assistant is `$HOME`, so the container's `/work` is the operator's whole
  home directory, writable, as root inside. That is unchanged, is consistent with the shell's
  `trusted_local` confinement (the same roots), and is a different question from tier parity — but a
  `--read-only` rootfs does not protect it, and the row above should not be read as claiming it does.

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

### §0.5 CLAUDE.md's package map carried FIVE duplicated rows for ~2.7 days (collapsed 2026-08-16)

✅ **What it was.** `CLAUDE.md`'s package map had TWO rows for each of `looplab/core/`,
`looplab/events/`, `looplab/runtime/`, `looplab/tools/` and `looplab/engine/` — ten table rows for
five packages, 3 KB to 26 KB each. Which one a reader met depended only on where they stopped
reading, and Markdown renders both without complaint.

**How long, and how it got there.** Born in the merge `b4ecb320` (2026-08-13 13:57 UTC, "merge F1d
stage environment; sweep the shadowing-pin hazard tree-wide") — a four-row conflict hunk resolved by
keeping BOTH sides, in the same commit whose own message records collapsing "every duplicated
module-level constant" because "a union leaves both statements and Python silently uses the last".
The tree got that treatment; the table did not. The fifth pair (`engine/`) arrived in `e9930e77`
(2026-08-14 09:22 UTC, "merge master into F1"). **52 commits touched `CLAUDE.md` between then and the
collapse (32 on master's first-parent line) and none of them noticed** — every one of them spliced
into whichever copy its anchor string hit first.

**What the duplication was HIDING.** The two copies of a row were not older/newer; they were two
independently-evolved descendants, so content was lost in BOTH directions, and the corrections that
landed on the losing side were silently reverted for anyone who stopped at the first copy:

| The row a reader hits FIRST said | The code says |
|---|---|
| `runtime/`: "it is a PATH fence, not an inode one" (unqualified) | Only on the READ path. `read_fence.py`: "The RARE events do resolve symlinks, and the asymmetry is the whole design" — `os.chdir` + the twelve `MUTATION_EVENTS` buy a memoized `realpath` and a `/proc/self/fd` lookup. The second copy carried the qualifier AND the 2026-08-13 re-measurement (+254 %, ~474 us/call on geesefs) that `read_fence.py`'s own comment records; the first copy stopped at the original +88 %/+2.8 % pair |
| `engine/`: a `metric_salvaged` node "can never become champion or be bred from", flat | `core/config.py:867` `metric_salvage_repair: bool = True` — a node whose repaired declaration passes the re-checked artifact contract loses the violation, restricted to operator-produced output. **This is the exact claim `a8d43b50` (2026-08-13 15:06) was written to fix**, in four places at once (config.py, configuration.md, the Web editor help text, CLAUDE.md), because all four stated the `audit` guarantee "as if the violation were unconditional". The next day's merge put the un-fixed sentence back as the copy a reader meets first |
| `engine/`: `widths.py` is "the ONE live concurrency-width settling rule" | `engine/widths.py` also holds `per_experiment_gpu_budget` (plus `proposal_derived_width`, `settled_width_refusal`). The same `a8d43b50` fixed this too — its message says it "updates CLAUDE.md's `widths.py` entry, which still described the module as single-purpose after it gained a prompt-facing GPU-ceiling derivation" — and the same merge reverted it for the first reader. `per_experiment_gpu_budget` appeared in `CLAUDE.md` on the SECOND engine row and nowhere else |
| `engine/`: `cadence.py::cadence_due`, one pace (second copy) | `cadence.py` defines `cadence_due` AND `occupancy_due` (the backlog F1g occupancy pace, the 167.7 GPU-h finding). `occupancy_due` appeared on the FIRST engine row and nowhere else — loss in the other direction |

Also lost to whichever copy you missed: the whole `core/envsafe.py` block (3,008 chars, second copy
only); `traceview.claimed_build_traces` and `span_index._anchored`'s `?before=` rule (first `events/`
copy only); and everything `tools/` says about `dev_probe.py` and `log_tools.py` — the second
`tools/` row was a 236-byte stub next to a 15 KB one.

**The hazard this created, which is the reason this is recorded and not just fixed.** This file is
edited by SPLICING text into these very long rows, and a merge conflict in it is resolved by
computing a branch's addition against its merge base and splicing at an anchor string.
**A substring splice picks whichever copy it hits first.** Four such splices landed on 2026-08-15
and hit the right row by luck. A splice into the wrong copy is invisible: the text IS there, in a
row nobody reads, and `mkdocs`, the doc-contract tests and every reviewer see a file that contains
what it should.

**The fix.** One row per package, content = the UNION of both copies with duplicate sentences
collapsed once and every contradiction refereed against the code (the four rows above; in each case
the true statement won and the false one was dropped, never averaged). Verified mechanically, not by
reading: 202 sentence-level segments across the ten originals, 193 present verbatim in the merged
rows and 1 modulo punctuation; the 8 that span an edit point were re-checked clause by clause (68
clauses, 62 present, 6 absent — 2 of them the `widths.py` and `cadence.py` framings the code
overruled, the other 4 the sub-row collapse below, counted twice because it is in both copies).
Independently, a per-token multiset check requires `count(merged) >= max(count(copy A), count(copy
B))` for every word — zero deficits on `core`/`runtime`/`tools`/`engine`, and `events`'s 24 deficits
are asserted equal, token for token, to the one span deliberately collapsed.

**One more instance, one level down, found by the same pass:** the `events/` row carried the phrase
"digest, readmodel, exporters + the pure UI projections (…)" TWICE inside a SINGLE row, in both
copies. `traceview.py`'s expansion lived only in the first list and `authoring_projection.py` only in
the second — the identical splice hazard, at sub-row scale. Collapsed into one list holding both.

⬜ **Still open (cheap).** Nothing enforces one row per path. A ~10-line assertion in
`tests/test_documentation_contracts.py` over `CLAUDE.md`'s package-map table — first column unique —
would have caught this on 2026-08-13 at 13:57 and would catch the next merge that does it. Note
§0.3's `trust/` row is a *different* defect in the same table (the row is unique, it is just wrong
about redaction) and a uniqueness check does not see it.

### §0.6 A run climbed toward a number from an evaluation it cannot be measured on (2026-08-16)

🟡 **What it was.** `rubertlite-dr-unified-v8` scores `python -m vectorsearch.test` over
`/home/jovyan/data/vectorizer-unified` against the v2 local data root. `rubert-dr-0807` runs a
different harness entirely — `python looplab_eval.py --save_path models/rubertlite_run --gpus 1` over
`/home/jovyan/data/vectorizer/dense-retrieval`. Two evaluations, two artifacts. At `at_node: 0`,
`trigger: "run_start"`, v8's Researcher wrote into `research_completed.data.memo.findings[1]`:

> The strongest verified anchor in the portfolio is rubert-dr-0807 #9: recall@100=0.8776

— and into `.summary` ("the prior sibling landscape is decisive"), from where the engine re-emitted it
as a `hint` ("already proven to take the same backbone from ~0.74 to 0.8776 in sibling rubert-dr-0807
node 9"), which reached the builder's prompt and node 9's repair rationale ("the OneCycleLR idea is
sound (sibling hit 0.8776 with it)"). v8's own champion is 0.762048. **The memo's own verifier already
said the citation was bad** — `verification.verdicts[0]` reads `{"verdict": "unsupported", "note":
"cited experiments do not exist: [9]"}`, because node 9 exists in the sibling and not in v8 — and the
number propagated regardless, with `evidence_receipt.complete: true`.

**THE MEASUREMENT that decided the fix.** Two counts, and they point at different places.

1. **Where the number actually travelled.** Over v8's `spans.jsonl`, tool calls whose span carries
   `0.8776`: `read_run_experiment` 50, `read_research_memo` 36, `read_run_code` 26, `list_all_runs` 20,
   `read_sibling_experiment` 12 — against `cross_run_search` **2**. The **run-reading tools** are the
   primary carrier; the cross-run memory store is secondary. `AllRunsTools`' own docstring said why:
   it "deliberately does NOT filter by task: it just gives the agent the capability, and the agent
   decides when a foreign run is relevant" — i.e. comparability was delegated to the model's judgement,
   which is exactly what `docs/36` says a record-side input may not be.
2. **What the rows carry.** Over the live store `/home/jovyan/data/looplab-memory`, 132 rows (23
   lessons, 63 research claims, 4 capsules, 21 cases, 21 meta-notes): **132/132 carry `run_id` and
   `task_id`; 0/132 carry a metric name, a dataset path, an eval command or a contract identity.**
   `research_claims.metric` exists on 42 rows and is the empty string on all 42 — the number lives only
   in `statement` prose. And 46 run directories carry a `task.snapshot.json`, from which the contract
   *is* derivable: 22 distinct contracts, 5 of them holding more than one run.

**Delivered.** `looplab/engine/eval_contract.py` (the contract identity: metric reader + eval command +
declared paths, tri-state `comparable()` where `None` ≠ `False`) wired into `ForeignRunReader` so
`list_sibling_runs` / `list_all_runs` / `read_*_experiment` / `read_*_code` /
`find_analogous_across_runs` carry a deterministic receipt beside a foreign run's number. Fail-open;
withholds nothing; reaches no metric, champion, selectability or violation. Replayed over the corpus:
of 2,070 rows a portfolio listing would show, 1,080 gain the receipt, 948 are UNKNOWN and untouched,
42 are same-contract and untouched. **v8 keeps v2/v6/v7 unflagged and flags 0804/0805/0807 and
`rubertlite-dense-retrieval`.** Nothing in `fold` changes — the code is in `looplab/tools/`, which
`events/replay.py` does not import, and `tests/test_eval_contract.py` folds the foreign log before and
after every provider call and requires byte equality. Five mutations (comparable-always-true,
fail-closed-on-unknown, key-on-metric-name-only, and each of the two wiring sites removed) each kill at
least one test.

**ALTERNATIVES REJECTED, with the evidence.**

| Option | Why not |
|---|---|
| **(a) Carry a contract on the cross-run ROW and partition retrieval on it**, mirroring `crossRunRank.js` | Right long-term; **inert now**. 0 of 132 existing rows carry any contract field, so a new one is populated only by future writes and a fail-CLOSED filter would blank the entire live corpus. Deriving it at read time from `run_id` is unsound: `run_id` collides (`run`, `smoke`, `demo` all appear across runs — which is why `run_uid` exists, and it is absent on 7/23 lessons and 24/63 claims), and the retrievers are constructed with `memory_dir` alone and have no run root to resolve against. **Still open — see below.** |
| **(b) Say it in the PROMPT and change no retrieval** | The advisory rung is **measured spent** in this repo: `runs/rubertlite-dr-unified-v6` node 4's `edit_file` note fired verbatim, is in that node's `spans.jsonl`, and the node still scored a human's checkpoint and recorded 0.225 (`CLAUDE.md`, `runtime/read_fence.py`). What shipped is deliberately *not* this: a fact stamped on the evidence at the point of consumption, the same shape as the existing `· PARTIAL SOURCE` receipt on those very rows — but **it has not been measured to change Researcher behaviour, and is not claimed to.** |
| **(c) Refuse to surface the foreign metric VALUE, keep the qualitative lesson** | The operator's complaint is about the number, so this was weighed hardest, and it **splits**. On the cross-run store it is **provably not implementable**: `rubertlite-dr-unified-v7` (a genuine `repo_task` run on v8's own contract) published *"…0.8173 at temp 0.01 and 0.8651 at temp 0.05, both below the 0.8776 symmetric-InfoNCE baseline"* — one sentence, three floats, two provenances, and separating them needs semantic judgement, which is the thing that may not decide comparability. On the run-reading tools it *is* implementable but not in the time available: the value would have to come out of text `RunTools` formats at **eight** sites, that text also carries the params (0807 node 9's row holds `loss_temperature 0.05`, `lr 0.001`, `pct_start 0.2` beside its metric), and `RunTools` is also the reader for a run's OWN nodes, where withholding is a plain bug. **Still open — see below.** |
| **(d) State the residue, build nothing** | Rejected only because the contract primitive is cheap, provable, and is the thing (a) and (c) both need before they can exist. The residue is stated anyway, below. |

⬜ **Still open, in priority order.**
1. **Withhold the value at the tool surface** (option (c), the half that is implementable): thread a
   per-read "foreign contract" flag through `RunTools`' eight metric-emission sites so a foreign run's
   objective value is replaced by a named refusal while its params, code and rationale survive intact.
   This is the change that would have stopped the incident rather than annotating it. Effort M — the
   care is in never firing on a run's own nodes.
2. **Stamp the contract on cross-run rows at WRITE time** (option (a)'s write half) so the retrieval
   partition becomes possible on rows written from here on. Effort S. It cannot help any existing row
   and must not be shipped with a fail-closed reader.
3. **The laundered-prose residue is unfixable by any row-level rule** and should stay stated rather
   than patched. 12 of the 19 `repo_task` research claims that reach a `repo_task` run carry a
   foreign-contract number inside native prose. The place to intervene is the *writer* — a run should
   not publish another evaluation's number as its own claim — not the reader.
4. **`proposal_cues._scoped` and `LessonScope.allows` disagree about the same store**, and the looser
   one is the prompt path: bound `CrossRunTools` admits **0** foreign-task lessons for v8, while the
   Researcher's advisory pack admits **3** lessons and **2** capsules from `rubert_dr_0807` at
   fingerprint similarity **0.353** against a 0.34 threshold. Two readers of one `lessons.jsonl` with
   two answers is the defect doc 25 TO-07 already fixed once for `CrossRunTools` vs `MemoryTools`.
   Effort S to state, M to reconcile — and reconciling downward hides rows, so it needs its own
   measurement first.

---

### §0.7 The memo's verifier verdicts never reached a single role (2026-08-16)

✅ **What it was — one line of a renderer, dead for the whole life of the feature.** The deep-research
memo carries a per-claim verifier result. `looplab/tools/run_tools.py::RunTools._research_memo` ended
with

```python
ver = m.get("verification")
if isinstance(ver, dict) and ver.get("summary"):
    parts.append("Verifier: " + str(ver["summary"]).strip())
```

**No memo has ever carried a `summary` key.** The block that IS written holds `verdicts`, `method`,
`unsupported`, `total_verdicts`, `omitted_verdicts` — so the renderer keyed on a field the writer has
never produced and **not one verifier verdict has ever reached a role through this tool, on this box,
ever.** The tool's own docstring one line above promised "evidence-cited claims (and any verifier
verdicts)"; that sentence was false. It was dead ON ARRIVAL, not by drift: `f180c986` (2026-07-10)
added the renderer, and the writer at that same commit already returned
`{"verdicts", "method", "unsupported"}`. `git log -S` over every version of `trust/verify.py` →
`trust/memo_verify.py`, `engine/research_cadence.py` and `core/advisory_payloads.py` finds three
constructions of the block in history and **none** of them writes a `summary`. It could not have
appeared by accident either: `sanitize_research_memo_payload` runs on the write path AND at replay
(`events/replay.py`), and `_verification` rebuilds the block as a dict literal, so a key no origin
writer emits is stripped before any reader sees it.

**THE MEASUREMENT.** Over every `research_completed` row in `runs/` (2026-08-16):

| | |
|---|---|
| memos | **102** |
| carrying a `verification` block | **98** |
| …carrying a `summary` key | **0** |
| …with no `summary` at all | **98** |
| …with EVERY verdict `unsupported` | **16** |
| verdict rows | **833** (`supported` 377 · `unsupported` 405 · `unclear` 51) |
| block shapes | 64 five-key · 34 legacy three-key (`method`/`unsupported`/`verdicts`) |
| claim↔verdict alignment | **98/98** index-aligned, **833/833** statement-exact |

**What it cost on the live run.** `rubertlite-dr-unified-v8`'s `at_node: 0`, `trigger: run_start`
memo records `total_verdicts: 8, unsupported: 8, method: deterministic`. Verdict[0] is
`{"verdict": "unsupported", "note": "cited experiments do not exist: [9]", "evidence": {…,
"complete": false}}` against a claim quoting `recall@100=0.8776` whose `node_ids: [9]` names a node
in a DIFFERENT run. That number became the run's stated anchor ("climb from the known ~0.88
plateau"), rode into a `hint`, into the builder's prompt and into node 9's repair rationale ("sibling
hit 0.8776 with it") — see §0.6 — and the operator has since confirmed it is not a comparable
evaluation at all. **The refusal was in the same payload, one key away, and no role could read it.**

**A SECOND measurement decided the SHAPE, and it is the one that makes this more than a typo.**
Replayed over all 102 memos through the real renderer: **88 render LONGER than the agent layer's
4,000-char `RESULT_CAP`** (median 6,974, max 9,381), and that cut is HEAD-KEEP — the tail is dropped.
The old `Verifier:` line was the LAST thing in the answer, so even had the key existed it would have
been the first thing thrown away on 86 % of reads. On v8's own memo the `Claims` section began at
char **3,889** of a 7,862-char answer — 111 chars before the cut — so a verdict rendered only beside
its claim would not have reached the Researcher either. **Anything a reader must not miss has to be
above the summary, not below the findings.**

**Delivered.** `core/advisory_payloads.py::memo_verification_view` — the read side, placed BESIDE the
`_verification` writer it mirrors — plus a rewritten `_research_memo` that leads with the verifier's
counts and its ungrounded claims, tags every claim row with its verdict, and says `Verifier: NOT RUN`
(4 of 102 memos) or `UNREADABLE` rather than falling silent. `trust/memo_verify.py::verify_memo`'s own
docstring listed three of the five keys it returns and now lists five, as a stated contract.
Replayed over the corpus: **102/102 memo reads change and 102/102 now carry a verifier line INSIDE
the 4,000-char window**; 89 lead with a named ungrounded-claim list; 0 lines present before are absent
after. Nothing else moves — folding all **46** event logs (**221** nodes, **37** champions) gives a
byte-identical corpus digest on both trees, because the change is a tool OUTPUT STRING plus one new
function nothing existing calls. **QUOTE THE INSTANT — v8 is live and this measurement moved under
its own feet.** Folding the SAME base tree twice, minutes apart, already disagrees about exactly one
run: `rubertlite-dr-unified-v8`, whose engine is still appending. Its `state_sha256` changes while
its metrics, statuses, violations, `best_node_id` and node count do not, so a naive before/after
comparison across the two passes reports one differing state and it is not the change. The
invariance claim above is the SAME-INSTANT control: base tree and commit tree folded back to back
over one corpus, **0 of 46 states differ** and both digest `9378b51a`. `tests/test_research_memo_verdicts.py` drives it on v8's REAL memo
(`tests/data/v8_research_memo.json`, verbatim from that run's log); six mutations on a throwaway tree
— the dead `summary` branch restored verbatim, a reader keyed on a field nobody writes, suppressing
unsupported claims, moving the verdict back to the tail, dropping the statement-equality half of the
join, and rendering absence as silence — each go red.

**ALTERNATIVES REJECTED, with the evidence.**

| Option | Why not |
|---|---|
| **(a) Pin the new key** (`assert '"verdicts"' in source`) | Would not have caught the original defect and would not catch the next one — a positive source pin sits just as happily beside a dead branch. The guard re-derives BOTH key sets by AST from the writers' own returns and asserts reader ⊆ writer, so the general defect is what goes red. |
| **(b) Withhold an `unsupported` claim entirely, or replace it with the refusal** | **Refused on the corpus.** 405 of 833 verdict rows are `unsupported` and 16 of 98 blocks are entirely so, so this empties the Claims section on 16 memos — and an empty section reads as "the memo made no claims", a worse lie. Decisively: **45 of the 45 verdicts the DETERMINISTIC pass emits are `unsupported`** with a note about the CITATION (`no evidence cited`, `cited experiments do not exist`, `cited source URL was not consulted`), which is a fact about the footnote and not about the claim. Hiding a true finding behind a bad footnote is worse than the defect. The refusal LEADS the statement instead. |
| **(c) Fail-shut — refuse the memo when the block is absent or malformed** | The tool is CONTEXT, not a gate, and `execute` has a never-raise contract. Withholding 4 memos that were simply never verified suppresses real findings to punish a missing check. Fail-LOUD instead: absence and malformation each get their own sentence (they have different remedies), and not a byte of the memo is withheld. |
| **(d) Join verdict `i` to claim `i` by index alone** | The index is right on 98/98 blocks today, but a bounded or reordered block would print one claim's refusal under another claim's text — the worst possible failure for this surface. The join requires statement equality, which is the rule `engine/lessons.py`, `engine/research_cadence.py` and `ui/src/researchMemoModel.js::alignVerification` already apply; a mismatch reads `unverified`, never a neighbour's verdict. |
| **(e) Route the three existing consumers through the new reader too** | Right, and deliberately NOT done in the same hour a run launches: two of them (`engine/lessons.py`, `engine/research_cadence.py`) write durable CROSS-RUN records and Card enrichment, so a changed join changes durable output. Stated below instead. |

⬜ **Still open, in priority order.**
1. **The memo render has no budget of its own** and is 2.2× the cap it is delivered under. Measured:
   the `Findings` section alone costs a median **2,030** chars of the 4,000 (12 model-authored bullets,
   median 204 chars each, p90 342) and exceeds 2,000 chars on 54 of 102 memos, while the evidence-
   bearing `Claims` rows sit behind it. The lead block this change adds costs ~1.4 kB and pushed
   **137** fully-cited claim bullets out of the delivered window across the corpus (exact duplication
   between the lead and the claim rows was removed to give some back). The fix is for the tool to spend
   `tools/_base.py::fit_rows`/`clip` on its OWN answer — deciding what survives — rather than letting
   the loop's blind head-cut decide. Effort M. **Not done here because it changes what a role reads for
   reasons unrelated to this defect and cannot be validated against behaviour in the time available.**
2. **Four implementations of one claim↔verdict join** (`memo_verification_view`, `engine/lessons.py`,
   `engine/research_cadence.py`, `ui/src/researchMemoModel.js`). Only the first is bound to the writer
   by a test. Route the two engine ones through it once there is time to re-verify the durable
   cross-run output byte for byte. Effort S, blast radius L.
3. ✅ **CLOSED 2026-08-16 by §0.8 — and one of its two numbers was wrong.** The push channel is now
   qualified (`Settings.memo_verdict_cue`). Read §0.8 before quoting this row: "11 of its 14 memo
   summaries contain `0.8776`" is true of the FULL summaries (11 of 15 today) and **false of what is
   pushed** — `_state_brief` delivers a 300-char head, and the literal survives that cut in **0 of
   15**. The harm was real and the carrier was the rounded `~0.88`.
4. **The DURABLE claims reach the Researcher's advisory pack with their verdict stripped too, and
   this one is a sibling of the same defect rather than a promise broken.** `engine/lessons.py`
   carries each memo claim's `{verdict, method, note}` into `research_claims.jsonl`, and
   `engine/claims_retrieval.py` computes `n_unverified` / `unverified` for every claim it retrieves
   (`:173-175`, `:510-516`) — and then `render_context_pack` prints only the cross-run aggregation,
   `[n_support↑/n_oppose↓]`, and never the verifier's own answer. Measured over the live store
   `/home/jovyan/data/looplab-memory`: of **42** claim rows, **22 carry a non-`supported` verdict**
   (20 `unsupported`, 2 `unclear`), and every one of them reaches the proposing Researcher with no
   sign that the run which wrote it could not ground it. `n_support`/`n_oppose` count OBSERVATIONS
   agreeing across runs; the verdict says whether the claim was ever tied to evidence at all, and the
   two are not substitutes. Unlike the memo tool, no docstring here promises the verdict — it is a
   computed-and-dropped field, not a false contract, which is why it is a separate row. Effort S; it
   touches the Researcher's pushed advisory text, so it wants the same care as #3.
5. **`verification` is not the only stale contract in this family.** `trust/memo_verify.py`'s
   docstring was two keys behind its own return until this change. Nothing systematically checks a
   docstring's key list against the dict a function returns; the AST helpers in
   `tests/test_research_memo_verdicts.py` are ~30 lines and generalize.

### §0.8 The memo SUMMARY a prompt pushes is the one field no verifier has ever checked (2026-08-16)

Closes §0.7 #3. §0.7 fixed the PULL channel: a role that calls `read_research_memo` now gets the
per-claim verdicts. This is the PUSH channel — `agents/roles.py::_state_brief` splices
`state.research[-1]["summary"]` into a prompt with no tool call needed — and the framing that sent
me at it was wrong in one measurable way, which is worth recording before the fix.

**RE-DERIVED, and the residue note's own literal does not survive.** `_state_brief` renders a 300-char
head of the whitespace-collapsed summary. Over `rubertlite-dr-unified-v8`'s 15 memos, `0.8776` is in
**11 full summaries and 0 pushed windows**. The claim "11 of 14 memo summaries contain 0.8776",
carried into a prompt, is a fact about a field the prompt truncates. Aiming a fix at that literal
would have been vacuous.

**WHAT IS ACTUALLY WRONG IS SHARPER.** `trust/memo_verify.py::verify_memo` verifies `memo["claims"]`
and returns `None` when a memo has none. **It has never looked at `memo["summary"]` at any commit.**
So the field the brief pushes is the one field of the memo that no verifier has ever checked — the
verdict is not merely elsewhere in the payload, it does not exist for this text. And it is not one
role: measured over v8's own `spans.jsonl`, the line reached **293 real prompts** in three phases —
`propose` 269, `triage` 20, `repair_critic` 4 — i.e. exactly the reachable subset of `_state_brief`'s
five call sites (`node_build._choose_action` needs `agent_drives_actions`, off in v8). **Not one of
those 293 whole prompts contains the word `Verifier` or the word `unsupported`**, while the memo
behind 52 of them records `total_verdicts: 8, unsupported: 8` and opens *"…then climb from the known
~0.88 plateau"* — the rounded `rubert-dr-0807` number, from a run `engine/eval_contract.py` reports
as a different evaluation contract (different eval command, different declared paths). That sentence
is v8's stated research direction.

**IT IS A MECHANISM, NOT ONE RUN'S ACCIDENT** — which is the question that decides fix-vs-note. Over
all 103 `research_completed` rows in `runs/`: 100 memos are pushable; **81** push a decimal number;
**76** push one from a memo carrying a non-`supported` verdict; **26** are pushed from a memo with no
supported verdict at all; and **13** push a ≥3-decimal number that is a node metric of a provably
different-contract run and of no node of their own — `rubertlite-dr-unified-v6` 7, `rubert-dr-0807`
2, v7 2, v8 1, `lt_dataset` 1. Five runs.

**WHAT SHIPPED.** `core/advisory_payloads.py::memo_verdict_cue` — one bounded clause, spliced at ONE
position in the existing line, built from `memo_verification_view` (the reader §0.7 placed beside its
writer). It names the memo's own claim tally and says that nothing verifies the summary itself; it
says `NOT RUN` and `UNREADABLE` out loud, because a missing check read as a passing one is this whole
defect family. `verdict_tally` is now shared with `tools/run_tools.py::_verifier_lead`, byte-identical
to the literal it replaced, so the two channels cannot come to spell one block two ways in one prompt.
`Settings.memo_verdict_cue` (ON) gates it; `false` restores the historical line **byte for byte**, the
`developer_probe` / `train_monitor_tools` pattern. Threaded exactly like `_digest_cap`: registry
`roles.RESEARCHER_HINT_ATTRS` for the two propose paths, `Engine._memo_verdict_cue` for the three
engine-method call sites.

**COST, and what was displaced: nothing, and that is measured rather than assumed.** Replayed over
all 103 memos: 100 lines change, **0 lose a byte** of the text they carried before, and the clause is
87-120 chars (median **102**). v8's real `propose` user turn has a median of **15,930** chars, so that
is **0.64 %**; its largest is 24,021 against a `context_budget_chars` of 1,000,000. §0.7 #1's 2.2×
over-cap problem is the TOOL's `RESULT_CAP`, a hard 4,000-char head cut; this site has no cap that
binds. Paying for the clause out of the 300-char summary slice was the alternative and is refused
below.

**HOLDS THE LINE (docs/36).** It widens what a role SEES and nothing it TRUSTS. The verdicts come
from `trust/memo_verify.py`, engine-side, before the memo was ever appended; the cue is a pure
function of folded `RunState`. Folding all 46 event logs in `runs/` on the base tree and on this one,
back to back — 222 nodes, 37 champions at 2026-08-16 — gives a byte-identical result and one digest,
`6d7d37f28c53ee19`. No metric, champion, selectability decision or violation can move, and no model's
own text decides its own verdict.

| Option | Why not |
|---|---|
| **(a) Refuse to push a summary whose verdicts are wholly unsupported** | **Refused on the corpus**, the same way §0.7 (b) was. 26 of the 100 pushable memos have no supported verdict at all, and 45 of the 45 verdicts the DETERMINISTIC pass emits are `unsupported` about the CITATION — a fact about the footnote. Suppression drops a real finding for a bad reference, and silently, which is worse than the defect it fixes. |
| **(b) Strip foreign-contract numbers out of the pushed text** | Refused on three independent grounds. (i) It cannot work on the case that motivated it: v8's carrier was `~0.88`, a rounded form no value-matching stripper reaches — my own corpus join needed ≥3 decimals against node metrics across 57 runs to stay honest. (ii) A summary is prose with no per-number provenance, so deciding which number is foreign means reading the model's own text for a record-side judgement, which docs/36 forbids. (iii) `eval_contract.py` is `engine/` and `roles.py` is `agents/`, which `engine` imports at module scope — the import would close the cycle. The right home for the contract receipt is the TOOL surface, where the foreign run id is known, and it is already there (`ForeignRunReader`, §0.6). |
| **(c) Pay for the clause out of the 300-char summary slice** | Refused, and measured: nothing at this site binds (0.64 % of the median turn, 2.4 % of `context_budget_chars` at the largest), so the only thing "paying" buys is a shorter finding. A longer verdict list would then shorten the summary, i.e. a memo with more bad claims would deliver less of its own text — exactly backwards. |
| **(d) Push the whole `_verifier_lead` block ahead of the summary** | Right for a 4,000-char tool answer that a role asked for; wrong for a line that rides in EVERY proposal, triage and repair-critic prompt of the run. It is ~1.4 kB against this clause's 102 chars, and the pull channel already carries it for a role that wants the detail — the push channel's job is to make that role WANT to. |
| **(e) State the residue and leave it** | The count decides this one: 76 of 100 pushable memos push a number from a memo with a non-`supported` verdict, over five runs, into three phases. That is a mechanism. |
| **(f) No `Settings` flag — make it unconditional** | A prompt is a contract here. The flag costs one field and buys the negative control that proves the historical bytes are still reachable, plus an off switch an operator can reach mid-incident. |

**`LEGACY_CONFIG_SNAPSHOT_DEFAULTS`: NO ROW, deliberately, and the reasoning is `redact_output`'s.**
The map admits a field on (a) postdates 2026-06-23, (b) defaults to adding paid calls / interventions
/ concurrency / a different selection policy, and (c) a historical value you can point at a commit
for. (a) and (c) hold trivially. **(b) fails**: no paid call, no intervention, no concurrency, no
selection policy — the cue makes no request, spends nothing, kills nothing, and reads a payload the
resumed run's own log already contains (98 of 103 memos in `runs/` carry a verification block,
including every pre-field one). `developer_probe` is the near precedent and is instructive rather
than contrary: its entry says (b) held "in TWO independent ways", a subprocess-launching TOOL *and* a
different prompt — the tool is what carried it, and a prompt divergence alone has never admitted a
row here. The residual concern is real and is the smallest version of itself: a resumed run's two
halves would say different things about the same memo. So does every prompt change this repo has ever
made without a row, and unlike those, this one only makes the run's OWN recorded refusal legible.

⬜ **Still open.**
1. §0.7 #4 is untouched and is now the last unqualified push channel: `engine/claims_retrieval.py`
   computes `n_unverified` for every durable cross-run claim and `render_context_pack` never prints
   it, while 22 of the 42 rows in the live store carry a non-`supported` verdict. It is the same
   defect one store over, and it wants the same shape — the tally beside the text, nothing withheld.
2. **The board and the digest are the LOUDER carriers of a foreign number, and this change does not
   touch them.** Measured over the same 293 v8 prompts: **241** contain `0.8776` somewhere and
   **293** contain `0.8835`, but the takeaway line carries neither. The hosts are the open-belief
   board rows (`hypothesis_added` minted from the memo's own `recommended_directions` — 110
   occurrences), the sibling-run digest line `rubert-dr-0807 …: best=0.8776` (58), and card summaries
   (the rest). A `recommended_direction` has NO per-claim verdict — the verifier grades `claims`, and
   directions are not claims — so the fix here does not generalize to the board and a different one is
   needed. Note what is NOT a carrier: `agents/hints.py::render_hint_directives` FILTERS every
   `source="deep_research"` row out of the operator-authority block, both by stamp and by prefix,
   so the `hint` §0.6 traced is not in a prompt today — measured rather than read off the filter:
   the literal `deep-research directions:` occurs **0 times** in the whole 320 MB of v8's
   `spans.jsonl`, i.e. in none of that run's recorded LLM input or output, while its `events.jsonl`
   holds 14 such rows (two of them carrying `0.8776`). The board rows those same
   `recommended_directions` became DO reach the prompt, which is why item 2 is open at all.
3. `docs/guide/configuration.md` carries the settings-catalogue paragraph **four times verbatim**
   (lines ~77-85) and its "18 essential / 176 catalogued" figures disagree with the "180 of 213" in
   the same sentence. Pre-existing; I updated only the two numbers my own field moves, because
   collapsing a duplicated paragraph is a different change.

### §0.9 The stage checker returned two verdicts on one experiment, and the re-train cost 2.33 GPU-h (2026-08-17)

`runtime/command_eval.py::_run_stages` holds the stage success contract in two halves: a
DETERMINISTIC artifact check (`expect.files`), then the agent-declared `expect.assert` sentence
handed to an LLM checker. The LLM half returned **two different verdicts on the same experiment**.
`runs/rubertlite-dr-unified-v9` node 0, `train`, two attempts:

    attempt A  {'train_runtime': 5137.504,  …, 'epoch': 14.87}  ->  check_failed, 8,399.9 s
               "declared_condition_violated: training ended at epoch 14.87, not all 15 epochs completed."
    attempt B  {'train_runtime': 5128.1169, …, 'epoch': 14.87}  ->  ok, 8,370.6 s

**IT IS THE JUDGE, NOT THE INPUT, AND THAT DISTINCTION HAD TO BE SETTLED FIRST** — a divergence in
what the checker was FED is a materially different defect with a different remedy. Both prompts are
recovered verbatim from `spans.jsonl` (spans `88aa6becedf4bdbb`, 2026-08-16 21:46:47Z and 00:09:54Z);
both are **4,710 characters**, share a byte-identical system prompt and declared condition, and are
NOT byte-identical to each other — every loss line differs, because two runs of one training are not
bit-identical. Nothing that bears on the claim differs: both end `'epoch': 14.87`, both carry the
trainer's `train_runtime` summary, both print `RECALL@100:`. So there is no canonicalization that
reaches this: the salient reading was already identical and the model still answered both ways
(`deepseek-v4-flash`, `temperature 0.6`, `reasoning_effort high`).

**AND THE REFUSAL IS WRONG ON THE MERITS, though not for the reason first offered.** `1695/1695` is
100 % of the scheduled steps and the trainer returned normally — but `epoch: 14.87` is not merely
"the last logged epoch". Re-derived from that node's own log: `logging_steps=10` and the epoch
counter advances 0.0877 per logged line, i.e. **114 optimizer steps per epoch** against a
**1,695-step** schedule = 14.868 epochs. HF sizes `max_steps` from a FLOORED updates-per-epoch
(15 × 113) and then takes the CEILED number each epoch, so the budget runs out 15 steps of 1,710
inside the last epoch. The stage ran its whole schedule, saved `final/model.safetensors` and is
**0.9 % short of 15.00 full passes** — a property of the library's arithmetic, unrepairable, and the
8,399.9 s re-train the refusal bought reported `14.87` again and was then accepted.

**THE STEP COUNTER IS NOT THE DETERMINISTIC PRE-ANSWER, and this is the measurement that shaped the
fix.** "Did the counter reach its total?" is TRUE of every one of the five
`declared_condition_violated` rows in `runs/` — the genuine shrinkages included. v8 node 8 reached
`11232/11232`, v8 node 9 `4236/4236`, v9 node 1 `5652/5652`, each with its own `train_runtime`
summary; all three had had their SCHEDULE cut and then completed it. A rung keyed on the bar would
have acquitted exactly the node this contract exists to refuse. It is also unreachable from this
path: the bar is written to **stderr** and the checker is handed `run.out`, so the string `1695/1695`
appears in **none** of the four v9 prompts.

**HOW RARE, AND WHAT IT COSTS.** Over all 47 preserved event logs: **358** `stage_finished` rows,
**23** `check_failed` (206,458.9 stage seconds = **57.35 GPU-h**). Only **5** of those 23 are
`declared_condition_violated` — the kind this touches — worth 38,794.3 s (10.78 GPU-h), and all 5 are
v8/v9, because `expect.assert` has only ever been declared on v2/v6/v8/v9 (**42** of the **123**
stage-check calls in the corpus carry a declared condition). Of the 5, **exactly ONE** is the
false-refusal shape. So the population is small and the fix shrinks to match it — but the one row is
a full re-train on a 14-node budget, and the rung is live.

**THE SEPARATION IS NOT A THRESHOLD ANYBODY TUNED.** Replaying the deterministic reading over all 42
declared-condition checks: **18** land inside the last declared epoch, shortfall **0.00-0.13** epochs;
**4** fall short by **4.01-49.00**. A one-epoch boundary sits 7.7× above the largest benign shortfall
and 4.0× below the smallest genuine one. There is no `(OK, short)` cell — the deterministic reading
never wants to convict where the model acquitted, so the rung gains no new appetite.

**WHAT SHIPPED.** `runtime/command_eval.py::epoch_floor_acquits`, applied in `_run_stages` after the
verdict is read, over a 64 KiB window of `run.out` (wider than the 4,000 chars the model is shown, so
a chatty script cannot push the summary out of range). FOUR conjuncts, each fail-closed: the verdict
is `declared_condition_violated`; the stage declared `expect.files` and they already passed on disk;
the `assert` names exactly one epoch count; and the trainer's own **end-of-training summary** reports
an epoch inside the last declared one. Then the verdict degrades to `inconclusive` and BOTH readings
land on the stage row — `check_inconclusive` (what the model said) and the new
`check_epoch_reached` (what contradicted it). Epoch and summary are read by ONE regex, not two, so a
script that trains twice and dies inside the second training cannot pair the first training's
summary with the second's counter.

**HOLDS THE LINE (docs/36).** The numbers come from text the candidate's own script wrote, so the
derivation may only ever **ACQUIT**: it moves a kind DOWN to `inconclusive` and can never fail a
stage, never raise a verdict, and never reach the five PHYSICAL kinds. That grants the candidate no
route it did not already have and a narrower one than it has: a script that wants this rung silent
can get that today by printing no epoch at all, since the checker's own prompt says *"if the output
does not say either way, reply OK"* — a reading that requires it to print a HIGH epoch is strictly
harder to satisfy than the silence already available. Nothing on the record side moves: the metric
still comes from the operator's reader over the protected `score` stage, `expect.files` ran first,
and a vetoed row still carries `check_inconclusive`, which is the key `metric_salvage.VETO_STAGE_KEYS`
scans — so the veto can never make salvage MORE permissive. Folding all 47 event logs on the base
tree and on this one — 227 folded nodes, 38 champions, 184 recorded metrics, 1 violation and 104
folded stage rows — gives one digest, `a033f538e8ba7cf2`: **no metric, champion, selectability
decision or violation can move**, and the only thing that would have moved is one stage's `check_failed` -> `ok`.

**THE REGRESSION FIXTURES ARE THE REAL LOGS.** `tests/fixtures/stage_epoch_floor_corpus.json` holds
the exact stdout tails, declared conditions and model replies of five live checker calls lifted from
`spans.jsonl` — v9 node 0 both attempts, v9 node 1, v8 node 8, v8 node 9 —
and `tests/test_stage_epoch_floor.py` drives them through a REAL `run_command_eval` over a real
subprocess (tier 1: what is asserted is whether the `score` stage RAN). v8 node 8 is the case where
triage ABANDONED the node rather than score a shrunken run; it, v8 node 9 and v9 node 1 must keep
their refusals or this change is worse than the defect. Non-vacuity was verified by mutating a
THROWAWAY `tar` copy of the tree with seven mutations — the rung deleted with a comment left behind
carrying every pinned literal; the veto widened off its one kind; the summary requirement dropped for
"the last epoch anywhere"; the tolerance loosened to 8 epochs; the artifact conjunct dropped; the
declaration parser made greedy; and the veto allowed to acquit an `unknown` — and **all seven go red,
each on the test written for it**.

| Option | Why not |
|---|---|
| **(a) Hand the checker a deterministic pre-answer to "did the step counter reach its total?"** | **Refused on the corpus, twice over.** It is TRUE of all three genuine shrinkages (11232/11232, 4236/4236, 5652/5652), so it acquits the node the rung exists to refuse; and the bar is on stderr, so it is in none of the prompts this path builds. The question that IS decidable is the trainer's final epoch against the DECLARATION, which is what shipped. |
| **(b) Canonicalize / complete the judge's input so two attempts cannot differ** | **Refuted by the bytes.** The two 4,710-char prompts differ in every loss line because two trainings are not bit-identical, and no canonicalization changes that; meanwhile the fields that bear on the claim were ALREADY identical in both. The divergence is downstream of the input, so this buys nothing at this site. |
| **(c) Splice the derived facts into the prompt as context and let the model agree** | Strictly weaker than acting on them, and it cannot be validated offline. If the engine can state the answer, asking a model at `temperature 0.6` to concur with it re-introduces the exact instability being removed — and a prompt is a contract here, so the change would ship unmeasured. Kept as the thing NOT done rather than a future item: the derivation IS the pre-answer. |
| **(d) Require a repeated verdict before a refusal costs a re-train** | Cheap (one extra call, 2-16 s, against 2.33 GPU-h) and it probably would have saved this row — attempt B's call on near-identical evidence said OK. Refused as the PRIMARY because it has no floor: it lowers the rate of a systematically wrong answer without bounding it, it re-asks the model a question the log states exactly, and it spends an extra call on all 4 genuine refusals for nothing. It remains available as a belt over the residue below. |
| **(e) Let the deterministic reading CONVICT as well as acquit** | Refused on doctrine and on the corpus. The epoch is text the candidate's own script wrote — `metric_salvage.py`'s rule and docs/36's "what the candidate controls may REFUSE a binding and may never ELECT one" — and a convicting rung reading it hands the candidate a kill switch over its own siblings. There is also nothing to buy: the corpus has no `(OK, short)` row, so conviction would move zero stages. |
| **(f) Require the assert's non-epoch clause to be matched against `expect.files` path-wise** | Refused as brittle: the declarations spell the same directory two ways (`experiments/nllcos_…` in the assert, `vectorsearch/experiments/nllcos_…` in `files`), so a path check would make the veto inert on half the corpus. The conjunct that shipped is coarser and honest — the stage must have DECLARED artifacts and they must already have passed — and the residue is bounded: every corpus assert's second clause is a saved artifact, and a second clause that is neither an epoch count nor an artifact ("and validation loss below 5") is a QUALITY judgement the base prompt has forbidden the checker to fail a stage for since the incident where it failed the run's best model. |
| **(g) State the residue and leave it** | One row of 23, but it is a full re-train on a live 14-node budget, the rung is ON by default, and the shape is not exotic: a fractional final epoch is what HF reports for ANY schedule whose steps do not divide evenly, and 18 of the 42 declared-condition checks in the corpus already sit in that band. |

**NO `Settings` FLAG, and no `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` ROW.** There is no prompt divergence
to gate — the model is asked exactly what it was asked before and the change is entirely in what the
engine DOES with an answer — so a flag would only buy the ability to turn a fail-closed acquittal back
into a false refusal. On the legacy map, `redact_output`'s ground (b) fails outright: no paid call,
no intervention, no concurrency, no selection policy; the derivation makes no request and can only
ever spend LESS GPU time than the behaviour it replaces.

⬜ **Still open.**
1. **The 4,000-character tail is the deeper defect and this does not fix it.** The window handed to
   the checker starts mid-token (`r_second': 6280.573, …`) and holds ~38 log lines of a 1.4-hour
   training; the step counter, the trainer's banner, the "Saving model" line and every restart are
   outside it. The two live-eval watchdogs and the crash-triage judge were all moved off fixed slices
   onto `tools/log_tools.py` (`read_log` / `metric_series` over `eval_log_plan`'s sources); **the
   inter-stage checker is the last judge in the engine still handed a blind tail**, and it is the one
   that can end a node. Same remedy, already built.
2. `no_artifact_written` is reachable for a stage whose `expect.files` the engine has ALREADY
   verified on disk one branch earlier — v8 node 8's own refusal says "and no final-model save is
   reported" about a node whose declared artifact passed. That is the same class of defect as this
   one (a model asked something the engine already knows) at a different kind, and it is not touched
   here because no corpus row turns on it alone.
3. The veto rescues the WHOLE verdict, not only its epoch clause. Bounded by (f) above rather than
   closed, and stated in `epoch_floor_acquits`' own docstring.

### §0.10 A cascade that could not have matched a row reported success, and nothing ever reaped a finished deletion (2026-08-17)

Reported as three defects sharing one root cause — "the memory was never purged", "the identity file
records an empty key", "nothing reaps the service files". **Two of the three framings are wrong, and
the root cause is not shared.** Re-derived from the artifacts, not from the report.

**(A) THE STALE ROWS ARE NOT RESIDUE OF THE DELETIONS.** `~/.looplab/memory` holds **216** cascadable
rows (10 lessons, 70 notes, 68 cases, 68 claims; `concept_capsules.jsonl` does not exist there), all
`task_id: toy_quadratic`. Their `run_id`s are `run` (175), `vis-demo` (6), `f1d-e2e`/`f1d-v7`/`toy`/
`f1d-run`/`f1d-run3` (4 each), `disp-smoke-*`/`prov-impl-demo`/`f1-smoke-run`/`eng-hunt-smoke1` (3
each). The **52** (in fact **54**) parked deletion identities name 46 distinct runs — `rubert-dr-0804`,
`smoke_0803`, `live-deps5-0804`, `sim-periodic`, … — and **the two sets are disjoint: 0 of the 216
rows names any deleted run.** Those rows are the droppings of throwaway `--out runs/demo`-style
smokes in worktrees and temp dirs, removed with `rm -rf` and never through a deletion at all. A
cascade only ever runs as part of a deletion, so nothing had ever *offered* to purge them. They ARE
orphaned — 216/216 belong to no run on disk — but "the cascade failed" is the wrong diagnosis, and
the fix is a deliberate sweep, not a repair.

Two more corrections to the report: the identity files are **not** all `rubert-dr-0804` (that is
merely the first alphabetically), and `memory_dir` is **not** a single value — it varies across 8
(`~/.looplab/memory` 25, `/home/jovyan/data/looplab-memory` 21, five vanished pytest temp dirs, one
run-local `runs/livetest-p0/mem`). That variation is the proof the per-run `config.snapshot.json`
read *worked*. Also, `claim_curation_log.jsonl`'s 21 rows carry **no** `run_uid` — and it is a
PRESERVED tier the cascade must never touch, being an append-only governance audit.

**(B) THE READER IS CORRECT; THE RUNS GENUINELY PREDATE THE FIELD.** `run_memory_identity` reads
`data.run_uid` off `run_started`, which is exactly where `orchestrator.py:3159` writes it. That
writer landed in **`ab328ee4`, 2026-08-11 23:17**. Every deleted run is of the 0803–0811 generation,
and **14 of the 18 runs still on disk have no `run_uid` either**. So "the reader looks in the wrong
place" is refuted: this is the legacy case, and for a genuinely pre-uid run — whose rows are pre-uid
too — name matching is the only identity either side has and is **correct**.

**WHICH LEAVES THE REAL DEFECT, and it is neither "refused safely" nor "silently no-opped" as posed:
it SILENTLY NO-OPPED WHILE REPORTING SUCCESS, and could not have done otherwise.** With
`run_uid == ""`, `RunIdentity.owns` meets a row that HAS a uid, takes its `if row_uid:` branch, which
requires the *caller's* uid, and returns False **without ever comparing a name**. It does not refuse;
it answers `{"ok": true, "deleted": 0, "kept": 0, "identity": "run_id"}` — a clean success asserting a
name keying that never happened, indistinguishable from "this run contributed nothing". Since all 216
rows in that store carry a uid and all 54 sidecars carry none, **every one of those purges was
structurally incapable of matching a row and every one reported success.**

Measured with the real predicates against copies of both stores: **0 of 52 deletions have a single
deletable row today.** That number is CORRECT, not broken — and the four rows that *do* name a
deleted run in `/home/jovyan/data/looplab-memory` are kept by the conservative predicates working
exactly as designed (`rubert-dr-0807`: 2 lessons at `evidence_count: 2`, 1 capsule whose concepts
governance merged; `live-deps4-0804`: 1 merged-concept capsule). Two rows for `rubert-dr-0805` sit in
a store its own identity sidecar does not name, so they were never in that cascade's scope.

**(C) CONFIRMED, SHARPER THAN REPORTED, AND NOT ONE POPULATION.** **152** service entries (not 149 —
the count is live; the deletions ran at 07:23–07:45 *today* and the server is still writing): 54
identity sidecars, 47 delete receipts (all `succeeded`), 50 lifecycle locks, 1 reset receipt. Nothing
removes any of them. But the sharpest finding is the **unbounded leak**: `save_deletion_identity`
runs *before* the transaction can refuse, so **10 of the 54 sidecars have no receipt at all and
belong to two runs that STILL EXIST** — `live-cards-0804` (6) and `live-deps4-0804` (4), whose
deletions were refused (the first holds a reset receipt parked in `archiving`) and re-pressed. Every
press leaks one, forever. The lifecycle files are not deletion residue at all: they are
`engine_proc`'s flock targets, and 49 of 50 fence runs that no longer exist.

**MEASUREMENT OF THE SAFETY MARGIN** (the number any fix must leave untouched): of 473 cascadable
rows across both stores, **301 belong to runs that are gone** (216 + 85) and **172 belong to runs that
still exist**, all in `/home/jovyan/data/looplab-memory`. The 216 decompose into dozens of *distinct*
`run_uid`s all sharing the directory name `run` — the exact name-reuse hazard the uid key exists for,
and the reason a name-keyed sweep is not an option.

**SHIPPED.** `unmatchable` + `advisory` on both the survey and the purge receipt, held OUT of `kept`
(a row nobody could form an opinion about is not a judgement a rule made); `run_uid_source`
(`run_started` | `pre_uid_run` | `no_run_started` | `unreadable`) persisted on the sidecar, the
version deliberately NOT bumped so the 54 existing files keep loading and keep their `memory_dir`;
`serve/service_reaper.py` + `looplab reap-service-files`; `memory_cascade.orphan_survey` + `looplab
memory-orphans`. Both CLIs report before they write and neither runs automatically.
`tests/test_service_reaper.py` (29 tests) drives all of it against real artifacts.

**ALTERNATIVES REJECTED.**

1. **Purge the stale rows automatically when a cascade finds nothing.** Refused outright. The stores
   are shared and `purge_attributable_memory` is irreversible; the rows in question belong to *other
   checkouts'* runs, and the operator asked for a deletion, not a sweep of a store they did not name.
2. **Make the legacy fallback also match a uid-carrying row on its `run_id`.** This is the one-line
   "fix" that closes the reported symptom, and it is the exact bug `RunIdentity` was written to
   prevent: on a corpus where dozens of live incarnations share the name `run`, it would delete a
   different, still-existing run's rows. The measurement above is what makes the cost concrete.
3. **Fill the empty `run_uid` from the run directory name.** Guessing an identity is the one thing
   this cascade exists not to do, and it would make the receipt's `identity: "run_uid"` a lie rather
   than merely an over-claim.
4. **A fourth `identity` label instead of a separate `unmatchable` field.** `identity` answers "what
   was this keyed on"; the blind spot is "what could not be reached either way". Folding them would
   have made the UI's three-way read (`run_id` / `mixed` / `run_uid`) a four-way one for a fact that
   is a count, not a keying.
5. **Reap service files by age alone.** Would have removed a `quarantine_ambiguous` receipt (the
   absorbing state whose receipt is the only record a human still owes that run a look, and whose
   removal frees the run identity for reuse), a pending receipt a retry RESUMES from, and — worst —
   a lifecycle lock a live process holds. `flock` is per-inode: unlinking a held lock does not fail,
   it lets the next process create a fresh inode at the same path and hold the "same" lock at the
   same time. Hence four rules, not one.
6. **Reap a succeeded receipt immediately.** It still answers a retry idempotently; without it the
   same `operation_id` re-enters as a fresh deletion of a vanished run and gets `404 run_not_found`.
   Hence the 24 h grace, which is also why only **23** of the 152 files are removable right now.
7. **Import `DEFAULT_GRACE_S` into the CLI.** A Typer default is evaluated at module import and
   `service_reaper` reaches `serve/appstate`, which imports fastapi — the whole CLI would refuse to
   start without the `[ui]` extra. The literal is duplicated and pinned by a test instead.
8. **Delete every orphaned row in one pass.** Would destroy shared corroboration whose *writer* is
   gone but whose *row* survives for other runs. `memory-orphans` goes back through
   `purge_attributable_memory` once per contributing run so every tier predicate still applies.

**STILL OPEN.** ⬜ The leak's *source* is untouched: `save_deletion_identity` still runs before the
transaction can refuse, so a refused deletion still parks a sidecar and the reaper only collects them
afterwards. Writing it after the fence is taken would end the leak rather than sweep it, but the
sidecar exists precisely to survive a crash *between* those points, so the honest fix is to write it
under the fence and not before — a change to the deletion transaction's ordering, not to the reaper.


### §0.11 "This run can never be deleted" — it could, by one command, and the refusal did not name it (2026-08-17)

Reported as a permanent defect in the destructive-quiescence ladder: `runs/live-deps4-0804` answers
`DELETE`/`POST /api/runs/live-deps4-0804/deletions` with `409 run_finalization_incomplete`, its
engine died mid-finalization on 2026-08-04, nothing has owned it for 13 days, and — the claim —
`refuse_unless_quiescent`'s third probe therefore refuses a state that nothing on the box can ever
resolve, making a crash between `run_finished` and `finalization_finished` a permanently undeletable
run. Proposed fixes ranged from teaching the finalize probe about engine liveness to adding an
operator escape hatch.

**REFUTED. There is a supported way out, it is one idempotent command, and it works.** The premise
that carried the report was that no such command is registered — *"the registered commands are
`memory-orphans`, `reap-service-files`, and nothing named finalize/resume"*. The Typer app registers
**45** commands and four of them complete an interrupted wrap-up: `finalize`, `resume`, `run`, and
the legacy `POST /api/runs/{id}/resume` (the tree's only `allow_incomplete_finalize=True` caller,
which spawns `looplab resume`). The canonical one is `cli/run_cmds.py::finalize`, whose crash-boundary
repair branch calls `finalize_run` directly on an already-`run_finished` run — no loop, no proposal,
no lifecycle event, no reachable model required, idempotent, and already pinned by
`tests/test_finalization_recovery.py`. `destructive_guard`'s own sibling refusal has said
*"resume finalization first"* the whole time.

**DRIVEN END TO END, 2026-08-17**, on a `/tmp` copy of the real run (original untouched, byte-identical
md5 `8329bde4…`, mtime still 2026-08-04 16:17) with `memory_dir` redirected and the endpoint pointed
at a closed port: `looplab finalize` exited 0, took the scope from 8 steps to 13
(`… reflection` → `concept_curation, claim_curation, task_facets, llm_cost, complete`), appended
`finalization_finished`, and `finalization_pending()` went False. Against the real server on two
copies of that same run under one runs-root: **before → `409 run_finalization_incomplete`; after →
`200 {"status": "succeeded"}`, directory gone.** Same run, same endpoint, one command between them.

**POPULATION: 1 of 42.** Every `events.jsonl` under `runs/` was scanned (42 logs incl. the 36
`specgate*/seed*-depth*` runs). Exactly one is half-finalized — `live-deps4-0804`, 8 steps, dead
between `reflection` and `llm_cost`. The step COUNT is not the tell and reading it as one is a trap:
36 of the 37 runs with 8 `finalize_step` rows are complete, because a toy run's 8 steps *end* with
`llm_cost, complete` and carry `finalization_finished` (`specgate/seed0-depth0`:
`begun, budget, diversity, case, reflection_begun, reflection, llm_cost, complete`). The real
distribution over finished runs is 8 → 37, 10 → 1, 12 → 2. **A single instance, not a systematic
one** — which is what decided the fix, per this file's own rule that the two deserve different ones.

**SO THE GUARD IS RIGHT AND STAYS BYTE-FOR-BYTE.** `refuse_unless_quiescent` is unchanged: three
probes, same set, same order, same three required keyword builders. What shipped is the sentence.
`deletion_service.py`'s `run_finalization_incomplete` was the ONLY refusal in that function with no
`remediation` field while five siblings around it carry one, and the UI turned it into *"This run is
still finishing its terminal records. **Refresh it before deleting.**"* — advice that is true of a
live engine and a closed loop for a dead one. Both now name `looplab finalize <run_dir>` and say
that the state does **not** clear by itself once the engine is gone. Docs:
`guide/cli-reference.md#finalize` gains the section a refused operator would search for, and
`guide/ui.md` cross-links it from the Delete copy.

**ALTERNATIVES REJECTED.**

1. **Make the finalize probe consider ownership/liveness** — refuse only when `engine_liveness(rd)`
   is not provably `False`, so an unowned stalled run reads as quiescent. This was the leading
   candidate and it is *sound*: measured, the deletion path re-probes liveness under
   `run_lifecycle_lock_http` immediately after the ladder, fails closed on an inconclusive probe
   (`503 engine_liveness_unknown`) and refuses `409 engine_running`, and then
   `engine_write_lock_http` **takes `engine.lock` itself**. Two independent gates, one of them
   holding the actual lock, so widening the ladder's third rung would not have let a deletion race a
   live engine. **Rejected anyway, on three grounds.** (a) It buys nothing a working command does
   not already buy, and it buys it on the *destructive* path — the wrong place to spend a widening.
   (b) It would delete a run whose wrap-up never ran, taking its budget summary, cost roll-up and
   cross-run case with it; the wrap-up is cheap, offline and idempotent, so completing it is
   strictly better than skipping it. (c) The ladder is shared by deletion, Replay and
   `destructive_guard`, and only deletion's post-ladder gates were measured — narrowing a shared
   rung on evidence from one of its three callers is how the next rung goes half-wired.
2. **A fourth rung** (`finalize_abandoned`, or splitting live-vs-dead into two vocabularies) —
   rejected outright. Each caller must supply a refusal builder for every rung, so a fourth costs
   three new live HTTP codes for a state that already has a command; and the answer here is "do not
   refuse", which no rung can express.
3. **An operator escape hatch** — a `force`/`allow_incomplete_finalize` flag on the destructive path.
   `durable_op.py` documents in prose that this opt-out is one *"no destructive caller may have"*,
   and the reason is on the record: flattening `reject_if_active` into the ladder would have given
   every destructive path a way past the finalize check. Adding the flag directly is the same move.
4. **A `looplab finalize --abandon` / discard path** — write `FINALIZE_STEP_ABANDONED` on request.
   It is engine-internal, written only for a staged *error* terminal, and `finalize_step` is not in
   `CONTROL_EVENTS`, so no route or command can request it. Making it operator-reachable would add a
   second way to end a wrap-up whose only advantage over the first is that it destroys the run's
   accounting. Not built.

**STILL OPEN.** ⬜ **The UI's own remedy for this state does not work on a naturally-finished run.**
`ui/src/runIndex.js` diagnoses exactly this shape as `finalization-stalled` (*"Finalization stopped
before wrap-up completed"*) and offers a **“Reattach finalization”** button — which maps through
`api.js` to a durable `run_abort` command, and `RunCommandService`'s `attach` path then looks for a
matching `run_abort` already in the log. `live-deps4-0804` finished *naturally*
(`stop_reason: no_eligible_candidate`, no `stop_requested`), so there is none and the record is
rejected `command_intent_missing`. The button is therefore inert on precisely the runs whose card it
appears on, and the TUI's `finalize` verb takes the same route. The server-side remedy exists
(`POST /api/runs/{id}/resume`), and **the UI never calls it** — grep of `ui/src/` finds no `/resume`
URL. Recorded rather than fixed here: it is a control-plane change on a spawn path, it needs its own
measurement of which runs reach `attach` versus `spawn`, and it is not what blocked the deletion.

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

### §0.12 The obvious reuse key for `mine` would have been wrong 4 times in 7, so the cache was not built (2026-08-17)

**THE ASK, AND THE MEASUREMENT IT RESTED ON.** Hard-negative mining (`mine`) recomputes per node even
when a sibling has already run what looks like the same configuration. Re-derived over every
`stage_finished` row in `runs/`:

    34   `mine` stage runs, 64,908.5 s = 18.03 h        (of 246.1 h of stage time corpus-wide, 7.3 %)
    17   `ok`, mean 46.7 min;  6 `fail`;  4 `expect_failed`
     7   `reused` (20.6 %), and EVERY ONE is within a single node's own retry after a repair
         (v8 nodes 3x2, 9, 10x3; v9 node 5) — `start_stage` reuse is per-workdir by construction

The proposed key was the node's DECLARED mining parameters (`idea.params`), which group v8's 16
nodes into 4 configurations with 8 nodes sharing `{mining_type: 1, n_negatives: 2}`.

**THE GROUPING IS TRUE AND THE KEY IS NOT, and this is the whole finding.** The declaration is
decoration relative to what the stage computes. Of the 8 v8 nodes sharing that configuration, three
(1, 7, 12) run **no `mine` stage at all** and the five that do run **four different entry points**
writing four different paths (`python mine_stage.py`, `python -m vectorsearch.data.mine_negatives`,
`python -m vectorsearch.mine_stage`, `python mine.py`). Meanwhile v8 nodes 5, 6, 9 and 14 declare
**no mining parameters whatsoever** and all four mine. And v6/v7, quoted as "one configuration across
7 and 8 nodes → all repeat", declare the EMPTY parameter set and contain **zero** `mine` stage rows
between them — the grouping there is vacuous.

**REPLAYED AGAINST THE BYTES.** Every surviving v8/v9 node workdir was swept for the artifact its
`mine` stage declared, and the artifacts sha256'd. There are 4 distinct mined parquets across 10 paid
v8 `mine` stages and 4 distinct across v9's 3, so duplication is REAL — five v8 nodes (3, 9, 10, 11,
14) produced a byte-identical **79,586,058**-byte `hard_negatives.parquet` (`13db4477…`), and nodes
4, 5 and 6 a byte-identical **58,431,283**-byte one (`34e9ca5e…`). Letting each declared-parameter
group's first node feed the rest, first-wins, and checking the answer against those digests:

    KEY                                          hits   WRONG   "saved"
    declared mining params (the proposal)           7       4    6.15 h
    declared params, cross-run too                  8       5    7.17 h
    whole workdir tree                              0       0    0.00 h
    stage entry + import closure + all non-.py      0       0    0.00 h
    argv + closure + non-.py, minus the manifest    1       0    0.64 h   <- what shipped as the KEY
    argv + closure only (config.yaml unbounded)     2       0    1.27 h   <- unsound, see below

The four wrong hits are v8 n4←n3, n8←n3, n9←n5 and n14←n5 — each one a stale negatives set silently
feeding a 5-hour training, at coordinates the run never occupied and with nothing downstream
recording what the negatives were. **That is the number that decided this**, not the hours.

**WHY NO SOUND KEY CAN PAY HERE, stated rather than engineered around.** The agent edits at NODE
granularity, not stage granularity. Nodes 3, 9, 10, 11 and 14 mined identical bytes while their mine
stages' own import closures differ in `vectorsearch/config.py` and their workdirs in
`vectorsearch/configs/config.yaml` — files the mining script really does read. Only the OUTPUT is
identical, and an output is not something a key may consult. The ceiling on a sound cross-node key on
this corpus is therefore **1 reuse, 2,288 s, 0.64 h** — 3.5 % of the `mine` corpus and **0.26 % of
all stage time**. The kernel enforcement rung `_safe_reuse_start`'s docstring is waiting on
(Landlock scoped to a stage's declared `needs`) does **not** move it either: replayed with `needs`
treated as a true bound, the same five nodes still differ inside the closure, so the yield stays 1.

**THE REPLAY IS NECESSARILY APPROXIMATE, and saying so is part of the result.** A key is derived
from the workdir as it stood BEFORE its stage ran; every preserved workdir is in its FINAL post-run
state, so it holds the `train` stage's undeclared `checkpoint-NNNN/` directories, which did not exist
when `mine` started. Replayed LITERALLY against the final state the shipped derivation makes **0**
hits — and that zero is an artefact of the replay, not of the key. The 1/0/0.64 h row above is the
closest reconstruction: the shipped derivation with everything under the experiment output tree
treated as postdating `mine`. This is precisely why the instrument had to be a WRITER and not a
script over `runs/` — the only place a key's real yield can be observed is a live run that records it
at the instant it was true.

**WHAT SHIPPED INSTEAD: the instrument, because the missing thing was the measurement.**
`runtime/stage_identity.py` derives two facts per stage and writes them onto `stage_finished`
(additive, fold-ignored, exactly like `expect_since`): `stage_input_key`, the sound key above, and
`stage_outputs`, the `(size, sha256, file_identity)` of every declared artifact bound at the instant
the `expect.files` contract passed. `looplab stage-dups RUN_DIR` reports duplicated OUTPUTS (observed
bytes) beside would-be reuse HITS and, crucially, WRONG hits. Both halves of the table above took a
20-minute sha256 sweep over 20 GB of preserved workdirs to produce, which is exactly why nobody had
them and why the wrong key looked obviously right. Cost, measured warm on a 1 GB repo-task workdir
(142 keyable files): **3.0-4.4 s per stage** — 0.15 % of a 2,290 s `mine`, 0.02 % of a 20,000 s
`train`; the artifact copy a cache would additionally pay is 1.5 s for 80 MB on this geesefs mount,
i.e. never the binding cost. That cost is BOUNDED and the bound had to be added, because
`SAMPLE_ABOVE` (256 MiB per file) bounds nothing about a workdir holding two hundred 200 MB shards:
the largest real repo-task workdir here digests **1,017 MB across 144 files** (three
`checkpoint-NNNN/optimizer.pt` at 183.9 MB plus four `model.safetensors` at 92.2 MB), so
`MAX_KEYED_BYTES` is 4 GiB — 4x that, ~60 s at ~70 MB/s, i.e. 2 % of the shortest stage this runs
beside. Over the ceiling there is NO key rather than a partial one, and the running total is checked
after the `lstat` and BEFORE the digest, so crossing it costs at most the ceiling.

**WRITING THE CEILING'S TEST IS WHAT FOUND THAT THE REASONS WERE UNREACHABLE.** `workdir_content`
returned a bare `None` and the caller mapped every refusal onto `unreadable_workdir`, so
`too_many_files` had been sitting in `KEY_REASONS` from the first commit and could never be emitted —
the same class of defect as the memo reader keyed on a field nothing writes (§0.7), one directory
over. It now returns `(map, reason)` and the three facts stay three facts.

**FAIL-CLOSED AT LEAST AS STRICTLY AS `_safe_reuse_start`**, which the request required and which is
where the two predicates differ interestingly. Same clauses: an opaque entry point, a non-default
`cwd`. One MORE: a workdir file that will not read refuses the key outright, because a content key
that skipped it would be a key over a smaller set than it claims — the clause a NAME-based predicate
does not need. Two clauses deliberately absent, and neither is a loosening: `_safe_reuse_start` fails
closed on any DELETION and on any non-`.py` change because it reasons over a change SET of file names
and can neither see a vanished module nor bound a non-`.py` read; a key over the CONTENT of the whole
workdir has no change set, so a deleted file is absent from the digest map and a config is in it, and
both are decided by the key differing. The one real narrowing is the manifest: `_safe_reuse_start`
refuses across ANY `looplab_stages.json` change, this key excludes the file and carries the stage's
own entry (argv, `expect` including its `assert`, `needs`, `env`) verbatim instead — so a change to a
LATER stage's entry no longer forfeits an earlier stage, while the argv of everything a reuse would
skip is still in the key byte for byte. And the reuse DECISION (`reuse_refusal`) is strictly stronger
than the artifact contract: `verify_stage_artifacts` proves "written after this stage's start", which
any later write satisfies; a reuse additionally requires the recorded `file_identity` AND the recorded
digest to still match, so a same-size mtime-restored rewrite — the `metric_subject` incident's own
shape — is refused.

**THIRTEEN MUTATIONS, on a throwaway copy of the tree, each one going red** (`n` = tests that fail).
The key: drop the non-`.py` content from the preimage (5); stop comparing the output digest in
`reuse_refusal` (2); drop the `unresolved_entry` clause (1); drop `scope` (2); let `workdir_content`
skip an unreadable file instead of failing closed (1); derive the key AFTER the command instead of
before (1); never record the output identity (7); remove the instrument's containment `except` (1);
check the byte ceiling AFTER the digest instead of before (1); collapse the two cost refusals onto
one reason (3). The reporter: stop checking whether a hit was WRONG (2); treat an UNBOUND output as a
comparable fingerprint (1); drop unkeyed rows silently instead of counting them (1).

**One of those mutations was written as a passing test and had to be fixed to mean anything**, and it
is worth recording because it is this file's own guard-test rule biting: "the key is derived BEFORE
the command" was green under a fixture whose stage wrote only what it declared — a stage's own
`expect.files` are excluded from its key, so before and after agreed and moving the derivation
changed no assertion. The fixture now writes an UNDECLARED side file, which real miners do.

`tests/test_stage_identity.py` is tier 1 throughout — a real `run_command_eval` over real workdirs,
with the produced BYTES asserted beside every key comparison, because a key that agreed while the
artifacts differed is the exact failure this design is bounded by.

**ALTERNATIVES REJECTED, each on this corpus:**

1. **Key on the declared mining params** (the proposal). 4 wrong of 7. Refused — and this is
   `docs/36-agent-driven-decisions-2026-08-13.md`'s rule, not a corpus accident: the candidate
   authors both the declaration and the code, so a declaration cannot be evidence about the code.
2. **Key on the whole workdir tree.** Sound and yields **0** — every one of the 20 preserved
   workdirs has a unique tree digest, differing in `train.py`/`loss.py`/`samplers.py`, i.e. files a
   `mine` stage never reads. A key nobody can hit is not safer than no key, it is just a cost.
3. **Key on the argv + import closure alone** (2 hits, 0 wrong here). Refused on the SAME ground
   `_safe_reuse_start`'s non-`.py` clause stands on: `vectorsearch/configs/config.yaml` is in no
   import closure and is read by every one of these miners. It is 0 wrong on this corpus by luck of
   which configs happened to differ, and the failure mode it admits is precisely a silent stale score.
4. **Build the cache off by default.** Refused: a decision arm that fires once per corpus is the
   `if "agentless" in ctx.available_developers` shape this repo already has a registry guard for, and
   an off-by-default fail-open surface still has to be right the day someone turns it on.
5. **A SELECTION fix — report duplication so the board avoids electing two identical mining
   configs.** This was the request's own fallback and the measurement refutes it too: a board keyed on
   the declared configuration would suppress genuinely different mining 4 times in 7 while missing the
   four nodes that declare nothing and mine anyway. What IS reportable is the observed byte identity,
   after the fact — which is what `stage-dups` prints and why it is deliberately not a predictor.
6. **Content-address the artifacts and hardlink duplicates.** Saves ~316 MB of disk on v8 and zero
   GPU-hours. Not the problem.
7. **Do nothing.** The closest call, and it is what the CACHE half resolved to. What kept the
   instrument is that the two numbers deciding a cache — hits and WRONG hits — were not computable
   from a live run at all, so every future version of this question would start with the same 20-minute
   sweep and the same temptation to key on a declaration.

**STILL OPEN.** (a) The key covers only what is INSIDE the workdir; the dataset mount, the model
cache, site-packages and the interpreter are uncovered and the key is bound to one run's `scope` for
exactly that reason. (b) A stage that writes an UNDECLARED file keys differently on its second
attempt, so a repaired node stops being a reuse source — conservative, but it means the instrument
under-reports reuse potential on repaired nodes, and `tests/test_stage_identity.py` drives that
property rather than hiding it. (c) `_stage_reachable_files` credits any argv token ending in `.py`
as a script, so `sh -c "python mine.py"` yields a closure holding a phantom; the key adds its own
`unresolved_entry` refusal rather than changing that function, because that function is the LIVE
reuse decision on a running run.

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
