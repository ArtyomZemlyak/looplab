# Full functionality review — branch `claude/full-functionality-review-c0z0e3` (2026-07-13)

Scope: everything this branch adds on top of `origin/master` (merge-base `edf06f7`): **10 commits,
144 files, ~32.7k insertions**. This is a large UI/UX overhaul plus substantial backend work — a
durable LLM cost-accounting ledger, crash-idempotent finalization/resume, read-only review
capabilities, a generation-fenced run-command service, log pagination, and a big React control
plane. The review covers correctness, validation, security, replay/idempotency invariants, corner
cases, and code/decision quality.

## Method

Nine focused sub-reviews (LLM+cost, finalize/resume, events/replay, launch+run-commands, serve
routers, reviews+log-pages, tools/perms, UI logic, UI components/assistant/TUI). Findings below were
re-verified against the actual code (and, where noted, reproduced by running the tree). Severity:
**P0** security/data-loss/replay-corruption · **P1** real bug hit in normal use · **P2** validation
gap / edge-case bug / resource issue · **P3** quality/latent.

## Ground truth (measured on this branch)

| Check | Result |
|---|---|
| Full Python suite (`pytest -m "not docker"`) | **2347 passed, 1 failed, 25 skipped** (3m40s) |
| UI suite (`node --test test/*.test.js`) | **216 pass, 0 fail** |
| Only failing test | `test_review_capabilities.py::test_summary_capability_is_one_run_read_only_and_revocable` (see TEST-1) |

**No P0 issues found.** The security- and replay-critical machinery (path-injection guards, the
double-start/idempotency machine, event-log invariants, the permission policy) is unusually careful
and, in most places, correct. Findings are one P1 and a set of P2/P3 gaps.

## Fixes applied in this branch (2026-07-14)

A follow-up `/code-review -fix` pass applied and tested the P1 and every P2 except the log-pager
concurrency refactor:

| Finding | Fix | Regression test |
|---|---|---|
| P1-UI | `assistantErrors.js` regex strictly start-anchored; dropped bare `error:` + unanchored substrings | `assistantErrors.test.js` (new case) |
| TEST-1 | assertion accepts `{404, 405}` | — |
| CI-1 | removed the duplicate `ui:` job (restores master's single job) | — |
| SEC-1 | `patch.py` exception now scoped to grader globs only (`_is_grade_glob`); structural trees stay protected | `test_perm_modes.py` (new case) |
| SEC-2 | `reviews.py` + `appstate.py` redact **before** truncating | — |
| PERF-1 | `costs.py` trusts the successful append; rescan only on the ambiguous "append raised" branch | existing ledger suite |
| REPLAY-1 | `finalize.py` quiescence allow-list now includes `reflection_note`/`lessons_distilled` | existing recovery suite |
| VAL-1 | `le=` bounds on `n_seeds`/`max_nodes`/`max_parallel`; `normalize_control` bounds `add_nodes`/`max_parallel` | `test_config.py` + `test_launch_preflight.py` (new) |
| VAL-2 | `launch.py` caps `task_file` size (8 MiB) before any whole-file read | `test_launch_preflight.py` (new) |
| VAL-3 | `boss.py` `_json_object` guard on all four handlers (400, not 500) | `test_report.py` (new) |
| TUI-1 | `tui.py` `rich.markup.escape` on all 9 dynamic failure-line prints | existing TUI suite |
| UI-2 | `hooks.js` transient probe failure now self-heals (owner: EventSource backoff; review: re-probe) | existing UI suite |
| chat cap | `launch.py` `_clean_chat` bounds turn count + per-turn content | — |

**Deferred** (higher-risk / involved, left for a dedicated change): **DOS-1/MEM-1** — the log-pager
global-lock + unbounded-index refactor (an availability concern, not a correctness bug) — and the
P3 cleanup list below.

---

## P1

### P1-UI · Legitimate assistant replies are silently replaced by a generic error card
- **File:** `ui/src/assistantErrors.js:35` (consumed at `ui/src/AssistantChat.jsx:57`, `ui/src/AssistantBar.jsx:1051/1065/1087`)
- **Defect:** `assistantErrorInfo` classifies a message as a provider error using an anchored
  `^error:` prefix **plus several *unanchored* substrings** (`llm request`, `provider returned
  error`, `error code:\d{3}`, `temporarily rate-limited`). For a *successful* reply `error_kind` is
  null, so the heuristic runs on the reply text; when it matches, the real Markdown answer is hidden
  and the user sees "Assistant request failed — Retry".
- **Reproduced** (ran the module): `"Error: I couldn't find that file."` → `provider_error`;
  `"Batch each LLM request into one call."` → `provider_error`; `"…check the error code: 500 in
  logs."` → `provider_error`; `"…temporarily rate-limited…"` → `rate_limit`. All are ordinary
  answers an ML/LLM assistant would produce.
- **Fix:** classify only when `KNOWN_KINDS.has(error_kind)`, or drop the unanchored substrings and
  keep only fully start-anchored exception shapes.

---

## P2

### TEST-1 · The branch's own review-capability test fails (405 vs 404)
- **File:** `tests/test_review_capabilities.py:134`
- **Defect:** asserts `GET /api/review/not-a-route` → 404, but the full app returns **405**. Root
  cause (traced): the pre-existing wildcard `PUT /api/{kind}/{name}` (`misc.py:207`)
  partial-matches the path, and Starlette returns 405 (Allow: PUT) on a path-match/method-mismatch;
  `review_request_allowed` permits any `GET /api/review/*`, so routing is reached. Behaviour is
  benign in production (405 leaks only an `Allow` header, no write is possible), but it is a **red
  test in the branch's own suite**. Deps are unpinned (`fastapi>=0.110`), so a fresh install pulled
  Starlette 1.3.1 — the assertion is too strict for the installed stack.
- **Fix:** relax the assertion to accept `{404, 405}`, or have the review namespace explicitly 404
  unknown sub-paths. (Consider pinning an upper bound on `fastapi`/`starlette`.)

### CI-1 · Duplicate `ui:` job key in the CI workflow (merge artifact)
- **File:** `.github/workflows/tests.yml` (jobs `ui` at line 12 **and** line 56)
- **Defect:** the branch added a `ui` job (node-20, with `cache-dependency-path`) while master
  already had one (node-22). The result is a YAML mapping with **two `ui:` keys** — invalid YAML;
  GitHub Actions either rejects the workflow or keeps only the last, silently dropping the added
  leg. Verified: master has a single `ui` job; HEAD has two.
- **Fix:** delete one; keep a single `ui` job.

### SEC-1 · `DEFAULT_PROTECT_EXCEPTIONS` un-protects integrity trees, not just grader globs
- **File:** `looplab/tools/patch.py:165-169` with `looplab/tools/perm_modes.py` (`DEFAULT_PROTECT_EXCEPTIONS`)
- **Defect:** `_is_protected` consults `allow_exceptions` **first** and returns "not protected" for
  any match, overriding the **entire** protect list. The exceptions
  (`**/upgrade.py`, `**/downgrade.py`, `**/upgrade_*.py`, …) were meant only to carve migration
  scripts out of the broad `**/*grade*.py` grader globs, but they also un-protect
  `answers/upgrade.py`, `held_out/downgrade.py`, `private/upgrade_1.py`, `.git/hooks/upgrade.py` —
  breaching the "NEVER writable/removable, in ANY mode" contract for `.git/**`, `answers/**`,
  `held_out/**`, `private/**`.
- **Exploitability:** low today (no shipping grader/integrity file or git-executed hook matches
  `upgrade*/downgrade*`), but it is a real weakening of a run-integrity/leakage invariant.
- **Fix:** apply an exception only when the path's protection came *solely* from a grader glob (i.e.
  the matched protect entry contains `grade`), not from a structural rule. The existing
  `test_perm_modes.py` exception tests still pass under this scoping.

### SEC-2 · Secret fragment can survive redaction (truncate-before-redact)
- **File:** `looplab/serve/routers/reviews.py:319` (mirror at `looplab/serve/appstate.py:163`)
- **Defect:** `out["error"] = redact_secrets(str(dumped.get("error") or "")[:160])` truncates to 160
  chars **before** redacting. A credential straddling byte 160 has its tail cut, and the surviving
  prefix can be too short to match any pattern/entropy rule, so a fragment reaches an untrusted
  evidence-scoped reviewer (`review_node` is opt-in evidence, reachable by a review link).
- **Fix:** redact first, then truncate: `redact_secrets(str(...))[:160]`.

### PERF-1 · LLM-cost sink rescans the entire event log on every successful call (O(K²) under the engine lock)
- **File:** `looplab/engine/costs.py:442-449`
- **Defect:** on the happy path of every `add()`, the sink calls
  `_event_usage_deltas(engine.store.read_all())` — a full-log copy + scan — just to confirm the
  just-appended event won. Because `usage_id` is a fresh 128-bit token, that check can only ever be
  true on the success branch, so the rescan is provably redundant. Over a run of K calls into an
  N-event log this is ~O(K²), plus a per-call fsync'd outbox write/read/unlink, all while holding
  `_llm_cost_lock`.
- **Fix:** skip the `read_all()`/`_event_usage_deltas` rescan on the non-raising append branch;
  keep it only on the ambiguous "append raised" branch.

### REPLAY-1 · Finalization recovery is abandoned when reflection's own events break quiescence
- **File:** `looplab/engine/finalize.py:118-132` (`finalize_scope_quiescent`)
- **Defect:** the quiescence allow-list does not include the reflection-family events
  (`reflection_note`, `lessons_distilled`, …) that `_write_reflection_note` emits *during* the
  reflection finalize step. On the **non-modern** error-recovery finish path (`_finish_run`, i.e.
  `run_finished(error)` after an abort), a hard kill after `reflection_note` but before completion
  makes `finalize_scope_quiescent` return False → `incomplete_finalize_scope` returns None, and
  because the finish is non-modern `finalization_pending()` is also False → `should_finalize` is
  False → the remaining wrap-up (the `llm_cost` roll-up and the `finalization_finished`/`complete`
  markers) is silently skipped. Verified directly by the sub-review.
- **Note:** the modern natural-finish path carries an explicit `finalize_scope` and is unaffected;
  reflection idempotency itself is robust (triple-guarded).
- **Fix:** add `EV_REFLECTION_NOTE`, `EV_LESSONS_DISTILLED` (and `lessons_refreshed`/
  `lessons_reconciled`) to the allow-list at `finalize.py:118`.

### VAL-1 · Launch/control cost & parallelism knobs have no upper bound (resource exhaustion)
- **File:** `looplab/serve/launch.py:247`; `looplab/serve/run_commands.py:533,538`
- **Defect:** numeric knobs are lower-bounded only (`Field(default=1, ge=1)`, no `le=`).
  `POST /api/start/preflight` with `settings.max_parallel = 100000` passes, then `/api/start` writes
  `LOOPLAB_MAX_PARALLEL=100000` and the engine tries 100k parallel sandboxes. Same for
  `max_nodes`/`n_seeds`, and `budget_extend.add_nodes`/`max_parallel` (only `<= 0` is rejected in
  `normalize_control`, so `"9"*400` is accepted as a 400-digit int).
- **Fix:** add `le=` bounds to the cost/parallelism `Settings` fields and range-check
  `add_nodes`/`max_parallel` in `normalize_control`.

### VAL-2 · `task_file` is read whole into memory with no size/type guard
- **File:** `looplab/serve/launch.py:166` (`_source_fingerprint` → `read_bytes()`), `:359` (`load_document`)
- **Defect:** preflight reads the referenced `task_file` in full (twice — fingerprint + load) with
  no size cap or regular-file check, while every other launch input is tightly bounded.
  `{"run_id":"x","task_file":"/dev/zero"}` → unbounded memory / indefinite hang, re-read on every
  start attempt.
- **Fix:** stat-and-cap size before reading; reject non-regular files and anything over a few MiB.

### VAL-3 · Boss chat handlers accept unvalidated request bodies (500 on malformed input)
- **File:** `looplab/serve/routers/boss.py:391,415,450,492`
- **Defect:** `/chat-compact`, `/chat`, `/suggest`, `/command` do `body = await request.json()` then
  `body.get(...)` with no JSON-parse guard and no `isinstance(body, dict)` check. A body of `[]`
  (valid JSON array) → `AttributeError`; a non-JSON body → `JSONDecodeError` — both surface as 500
  instead of 400. The parallel `control.py` handlers were hardened in this branch; boss.py was not.
- **Fix:** guard with `try/except (ValueError, UnicodeDecodeError)` + `isinstance(body, dict)`.

### DOS-1 / MEM-1 · Log pager holds one global lock across a full-file scan, and its index is unbounded in event count
- **File:** `looplab/serve/log_pages.py:419` (lock scope), `:180` (index growth)
- **Defect:** `page()` holds the single `self._lock` for its whole body, including the first-access
  `_scan` that reads the entire event log to build the index; every concurrent `log-page` request
  for *any* run blocks until it finishes (head-of-line stall on a multi-GB log). Separately, `_scan`
  retains one `_Row` per event with only an 8-run LRU and no row-count cap, so browsing several
  large runs holds millions of rows in memory.
- **Fix:** build/extend the index under a per-path lock (or outside the shared lock); cap indexed
  rows per run (sparse boundary index / eviction).

### UI-2 · Workspace can get stuck on an error screen when the initial `/state` probe fails transiently
- **File:** `ui/src/hooks.js:197-208` (`useRunState`)
- **Defect:** the new "probe once before connecting" logic only arms the self-reconnecting
  EventSource/poll loop *after* the first `GET /state` succeeds. A transient failure (504/dropped
  connection behind the JupyterHub/reverse proxy this code targets) sets `status:'error'` with
  nothing scheduled to retry; the prior code called `connect()` directly and the EventSource backoff
  self-healed. User must manually `retry()`.
- **Fix:** in the initial-probe `.catch`, schedule a backoff retry for non-terminal errors (not
  404/410/401) instead of terminating.

### TUI-1 · Rich-markup injection crashes the TUI (and re-crashes on reopen)
- **File:** `looplab/serve/tui.py:735` (and 576/592/609/613/763/775/795)
- **Defect:** server-supplied command errors and boss/LLM labels are interpolated straight into rich
  markup: `console.print(f"  [red]✗[/red] {label} — {turn['error']}")`. Rich parses `[...]` and
  raises `MarkupError` on an unbalanced closing tag (e.g. a `[/red]`-shaped fragment in an error
  message), which nothing catches — it propagates out of `run_view` → `dashboard`, and
  `_reconcile_pending` re-prints the persisted row on every reopen, so the crash recurs.
- **Fix:** `rich.markup.escape()` the dynamic segments (or pass `markup=False`).

---

## P3 (quality / latent — fix opportunistically)

- **Replay order-tolerance (latent):** `_on_llm_usage`/`_on_llm_cost` (`replay.py:1108-1128`) make
  `st.llm_cost` order-sensitive between a legacy `llm_cost` summary and the first `llm_usage` delta;
  not live-reachable today (finalize emits the summary after `usage_seen` is set), but the
  background-appendable splice test (`test_background_appendable.py`) asserts only selection state,
  never `llm_cost`, so a future change could break accounting determinism unnoticed. Add an
  `llm_cost`+`llm_usage` case to `_base_events()` and assert position-neutrality; add
  `assert EV_LLM_USAGE in BACKGROUND_APPENDABLE` at the `costs.py` append sites (the accountant sink
  lacks the runtime guard and source-scan coverage that `research_cadence` has).
- **costs.py:113-116** `_children` does `yield from obj.values()/obj` without snapshotting → can
  raise `RuntimeError: changed size during iteration` on a concurrently mutated role container; the
  caller swallows it, silently skipping a developer-swap cost binding. Snapshot with `list(...)`.
- **costs.py:41-52** `sanitize_usage_delta` lacks the prompt+completion → total fallback that
  `core.llm._normalize_usage` has (latent; every live delta currently supplies `total_tokens`).
- **orchestrator.py:544-545** `_finish_run` appends `run_finished` without the `expected_last_seq`
  CAS the natural-finish helpers use; a resume landing mid-recovery on the multi-process path can be
  clobbered (re-issuable).
- **boss.py:454** `report_refresh` appends `EV_REPORT_GENERATED` (not in `CONTROL_EVENTS`/
  `BACKGROUND_APPENDABLE`) from a UI request — a documented deviation from invariant #1 (pre-existing,
  benign because appends are serialized and the event is selection-neutral). Route through the engine
  or add a documented UI-appendable allow-list.
- **runs.py:767,773** `node_detail` validates the run generation twice (acquiring the per-run
  command sequencer twice); drop the pre-fold call.
- **control.py:48 vs launch.py:235** two implementations of the "default backend=llm" rule → preview
  vs launch can diverge cosmetically; share one helper.
- **control.py:478 vs 637-643** `can_retry=True` is advertised for `not_started`/`failed`, but a
  same-`Idempotency-Key` retry always 409s — only a fresh validated key works. Clarify the contract.
- **control.py:865-866** `start_run` sets the paid-effect Popen boundary *before* `_spawn_engine`
  (unlike `reset_run`, which sets it after and rolls back); since the spawner only raises when Popen
  itself failed, a transient Popen error leaves the run permanently `uncertain` with a PID-less lease
  needing operator `resolve-claim`. Flip the boundary to after the call.
- **write_tools.py:297-300** `apply_patch` leaves orphan `.bak` snapshots if a mid-loop backup
  `save()` fails (backup-stack pollution; no data loss).
- **reviews.py:221** each review GET acquires the engine's per-run command sequencer (creating
  `.command-locks/*.lock`) — a "read-only" link both writes lock files and contends the engine lock;
  consider a lock-free stat+first-line generation check.
- **reviews.py:314** dead `seq`-branch in the node-evidence 404 message (unreachable after the 400 at
  :309).
- **log_pages.py:336** a same-inode size-changing in-place rewrite is treated as a pure append
  (defensive gap only; LoopLab's writer is append-only via `os.replace`).
- **launchDraftStore.js:38-45** eviction is by first-insertion order, not true LRU, so a re-saved
  (actively edited) draft can be evicted before newer untouched ones once `>50` drafts exist.
- **hooks.js:109** the SSE `EventSource` URL interpolates `runId` without `encodeURIComponent`,
  unlike every sibling request.
- **api.js:1269-1271** `jobAwait` dereferences `j.status` without null-guarding the fetched body.
- **api.js:1311-1315** `assistantMessageStream` trims each SSE `data:` line and concatenates without
  the spec `\n`, dropping a whitespace-only token and mis-joining multi-line frames.
- **assistant.py:575** a legacy dangling user turn without a `turn_id` is a dead-end: recovery is
  impossible yet a new turn is rejected with `recovery_required`.
- **panels.jsx:620 / VirtualTimeline** `explorerEventKey` returns null for a row lacking both `seq`
  and `id`, giving colliding React keys / shared height measurements (low likelihood).

## Docs / diagram sync (per CLAUDE.md)

- **Good:** the prose guides were kept in sync — `concepts.md`, `deployment.md`, `ui.md` document the
  new `llm_usage`/`command_ack`/`finalize_step` events, the durable accountant, run-command staging,
  and `LOOPLAB_UI_HOSTS`.
- **Gap (P3, advisory):** `docs/infographic/agent-architecture.html` was **not** updated for the new
  event types / subsystems. CLAUDE.md treats a stale diagram as a bug; mitigating factor is that the
  diagram depicts the agent loop (largely unchanged) while the new events are accounting/control-plane.
  Worth a pass to confirm it still reflects reality. (`configuration.md` needs no change — the new
  settings are env-only, not `Settings` fields; `config.py` was untouched.)

## What is well done (strengths)

- **Security posture:** DNS-rebinding Host allow-list, server-side cross-origin mutation rejection,
  token now gating reads (not just writes), review credentials scoped to GET on `/api/review` with
  `no-store`/`Vary` hardening, tokens persisted only as SHA-256 digests compared with
  `compare_digest`, and NaN/Inf/expiry handling that fails closed.
- **Path-injection defense** is layered and consistent (`safe_run_dir`, `validate_paths`, symlink +
  containment checks on event logs and log pages).
- **Idempotency/replay discipline:** the double-start machine (per-run cross-process `sequence()`
  lock covering check *and* Popen, PID-less spawn lease), the generation-CAS on run commands, the
  crash-idempotent finalize step gates, and reflection at-most-once are genuinely robust; the fold
  stays pure and re-sanitizes hostile telemetry.
- **Permission policy** is fail-closed (deny-by-default action registry, HIGH/UNKNOWN never inlined
  or remembered even in Auto, exact-scope grant digests with a clamped 600s TTL, `approval_allows`
  closing the `startswith("allow")` bypass).
- **Frontend safety:** no `dangerouslySetInnerHTML`; the Markdown renderer blocks
  `javascript:`/`data:` hrefs; state models use rigorous stale-response/generation fencing; storage
  access degrades to in-memory defaults on quota/parse failure.

## Round 2 — UI-focused review + merge into the ui/ux branch (2026-07-14)

A second max-effort pass reviewed the **new UI work** merged from
`codex/ui-ux-overhaul-20260712` (commit `135074d`, "establish accessible component contract" —
`accessibility.jsx`, `charts.jsx`, data-table migration, dialog focus, theme, +2919 lines) **together
with the round-1 fixes**, then merged everything into the ui/ux branch. Five finder angles + a
cross-cutting sweep; `vite build` passes, `npm ci` lock consistent, 251 UI tests green.

**No new P0/P1 in the UI.** Findings (all verified, applied):

| Finding | Fix |
|---|---|
| **P2** my round-1 TUI escape fix was **incomplete** — `_reconcile_pending` and the plan/command display still interpolated untrusted `label`/`exc`/error into rich markup | added an `_esc()` helper and escaped **every** server/LLM/user-supplied segment across all `console.print` sites (label, error, exc, run id, chat text, reason, msg); +regression test proving a `[/tmp]`-laden persisted label no longer crashes reopen |
| P3 `boss.py` `chat_log_append` still 500'd on malformed JSON | guarded parse (400) — the 5th handler my round-1 fix missed |
| P3 `charts.jsx` trajectory tooltip printed literal `undefined` for operator-less nodes | `hn.operator \|\| ''` guard |
| P3 `RunList.jsx` Escape-cancel of a project rename fired a redundant PATCH + reload | Escape passes `''` so the existing guard skips the write |
| P3 `accessibility.jsx` `downloadTableCsv` clicked a detached anchor and revoked the blob URL synchronously (aborts the download in Firefox) | attach to DOM, click, detach, `revokeObjectURL` on next tick |
| P3 `accessibility.jsx` `ChartFrame` `aria-controls` dangled to an unmounted id while collapsed | only set `aria-controls` when expanded |
| P3 `hooks.js` review-path re-probe used a fixed 1.5s tick (no ramp) | ref-persisted backoff ramp (1.5s→×2→30s), reset on a good probe |
| P3 `chartAccessibility.test.js` guard regex couldn't match the JSX `tabIndex={0}` form it targets | widened the regex |
| P3 run-card `<a>` hijacked the card drag ghost | `draggable={false}` on the anchor |

**Left as-is (documented, no live impact):** MiniLine's always-blank "Wall time" column (cosmetic),
`DataTable` double-caption verbosity, `ChartFrame` `colSpan={0}` edge (no live caller), the compact
side-drawer focus landing on the resize handle in the 601–1279px range (a11y polish).

Verified sound in round 2: `useDialogFocus` (nested stack, Tab-wrap, Escape, focus restore), theme
persistence (no flash, guarded `storage` listeners), the CSV formula-injection guard, chart
divide-by-zero guards, and the `assistantErrors`×`AssistantBar` seam (the stricter classifier is
**strictly better** — real failures ride the `onError`/`error_kind`/`res.ok` channels).

## Verdict

Solid, carefully engineered branch with no P0s and strong security/replay foundations. Before merge,
address the **P1** (assistant error misclassification — user-visible and easy to hit), the two
**broken-CI items** (TEST-1, CI-1), and the resource-exhaustion/validation gaps (**VAL-1/2/3**,
**DOS-1**). SEC-1/SEC-2 and REPLAY-1 are real invariant/robustness weakenings worth fixing even
though they are hard to trigger today. The P3 list is cleanup that can follow.

---

## Round 5 — gap-sweep re-review (2026-07-14)

A fifth max-effort pass (10 parallel finder angles over the full ~9.7k-line Python diff + the UI
diff, then adversarial verification) focused on defects the earlier four rounds missed. **No new
P0/P1.** Nine findings survived verification and were fixed with regression tests; the rest were
verified as intended/defensible and documented below.

### Fixed (verified, applied, +tests)

| Finding | Fix |
|---|---|
| **P2** `orchestrator.py` `_finish_with_report_if_quiescent` + `finalize.py` `_recover_scoped_terminal` — the report-clone `append(expected_last_seq=tail_seq)` was **not** wrapped in `try/except`, unlike the `run_finished` append 3 lines below. A background-appendable `llm_usage` (cost sink) splicing between the tail read and the clone CAS raised `EventStoreConcurrencyError` out of the finish path → engine crash instead of graceful scope-abandon | wrapped both clones in the same guard (append the `abandoned` finalize-step / return a fresh read-fold) — symmetric with the finish CAS |
| **P2** `Inspector.jsx` live node-detail `usePoll` ignored the `alive()` guard every sibling poll uses → a slow `/nodes/A` response resolving after the user selected node B rendered A's Code/Trace/Metrics under B (sticky) | callback now takes `(alive)` and gates `setDetail` on `alive() && d` |
| **P2** `write_tools.py` `_patch` — recovery snapshots are pushed from the pre-image **before** `git apply`; a patch that failed the dry-run left its snapshot behind, so a later `revert` of an *earlier* edit on the same file popped the phantom (restoring unchanged bytes) instead of undoing that edit — a **silently broken undo** | discard the just-saved snapshots on any bail (failed `save` mid-loop or failed apply); `git apply` is atomic so the revert is a clean pop |
| **P2** `control.py` — `control` / `submit_command` / `resolve_activity_claims` / `resolve_start_claim` are `async def` yet call the fully-blocking `srv.commands.*` (cross-process `flock`, up to `lock_acquire_timeout`s, + log fold) **inline on the event loop**, freezing every concurrent SSE/poll in the worker | offloaded each blocking call via `anyio.to_thread.run_sync`, matching the start/preflight handlers' existing pattern |
| **P3** `Dock.jsx` building-trace `usePoll` ignored `alive()` → a late `/trace` response could overwrite the final post-build fetch with an older snapshot (stale until the next node change) | callback now takes `(alive)` and gates `setTrace`/`setTraceError` on `alive()` |
| **P4** `routers/reviews.py` `_review_cost` coerced token counters with an unbounded `int(number)`, unlike every other cost sanitizer (`replay._llm_counter`, the metrics step guard) which saturate at 2⁶³−1 → a corrupt `llm_cost` field became a ~1000-digit bigint in the public review projection | cap the integer counters at `2**63 - 1` |
| **P4** `routers/reviews.py` `_scrub_json` recursed over dict/list/tuple with **no depth limit** → a pathologically nested payload on the read-only review surface raised `RecursionError` → 500 | added a depth cap (`_MAX_SCRUB_DEPTH = 40`); an over-deep subtree collapses to a bounded marker |
| convention | `finalize.py` `finalize_scope_quiescent` allow-list used raw string literals `"llm_usage"`/`"command_ack"` instead of the `EV_LLM_USAGE`/`EV_COMMAND_ACK` constants (the codebase's event-type-constant convention; a future value rename would silently break the match) — switched to the constants |

Regression tests: `test_review_fixes.py` (giant-counter saturation; scrub recursion bound),
`test_write_tools.py` (failed-patch phantom-snapshot discard; mid-loop save-failure rollback),
`uiSemantics.test.js` (both polls gate on `alive()`).

### Verified intended / defensible — documented, not changed

- **`replay.py` `_on_llm_cost` order-dependence** — `EV_LLM_COST` is folded as a baseline only
  `if not ctx.llm_usage_seen`. This is the **documented compatibility base** for resume-across-upgrade
  (a legacy cumulative summary seeds the new delta ledger); in a real ordered log the finalize summary
  always trails usage, so the theoretical reorder can't occur. Left as-is.
- **`perm_modes.py` git_add/git_branch inline in acceptEdits, and MCP → ask-in-auto** — both are the
  *intended* risk-based redesign: `git_add`/`git_branch` are genuinely `RISK_REVERSIBLE`, and
  unregistered/MCP actions are `RISK_UNKNOWN` which the module docstring states "require explicit
  approval even in Auto." Changing either would reverse a deliberate security decision.
- **`runs.py` SSE `done` suppressed while `phase == finalizing`** — locked by
  `test_sse_done_waits_for_error_finalize_recovery`: a dead driver + `run_finished(error)` is
  finalization-stalled, not terminal-ready; `done` correctly waits for the phase to flip to `finished`.
- **`server.py` review-header resolved before the unauth allow-list** — a request presenting an
  (expired) review credential fail-closes with a clear 410 rather than silently downgrading to the
  public surface; defensible, and changing header precedence touches the auth model.
- **`control.py` `reject_if_active` gates neutral `/control` intents during a command** — a real
  behavioral narrowing of the *legacy* path, but which control types may interleave with a command's
  CAS is a control-plane ordering decision (architecturally significant); the harm is a transient 409
  with a clear remediation. Flagged for a maintainer decision, not auto-changed.
- **`llm.py` streaming `finally` now accounts real cost** — this is (correct) budget enforcement on
  streaming calls; touching the heavily-commented cost/trace hot path for the marginal
  BudgetExceeded-on-close edge risks more than it fixes.
- Lower-value items left as-is: cache-hit `_last_usage` zeroing (unreachable — fresh empty-cache
  client per request), `_bound_run` finally masking a 500 with 410 (P4 observability nit), the
  case-insensitive lock-identity collision (narrow: case-sensitive volume + case-variant run ids),
  and six reuse/efficiency cleanups (duplicate task-file resolver, monitor-loop re-folds, three
  generation-token validators, duplicated Windows-reserved set, double reason-validation).

The process diagram (`docs/infographic/agent-architecture.html`) already depicts cost + finalize and
no number in it is stale after this branch; the new `llm_usage`/`command_ack`/`finalize_step` events
are diagnostic/accounting (not loop-flow), so no diagram change was warranted.

---

## Round 6 — newly-merged trace-perf + attention-center (2026-07-14)

After round 5, the branch advanced by 15 commits of new work (a **light span index** + **delta-encoded
generation input** for trace performance, and an **owner attention center**). A sixth max-effort pass
(11 parallel finder angles over the ~7.1k-line `6a31876..HEAD` range + adversarial verification)
reviewed only that new surface. **No new P0/P1.** Seven findings were fixed with regression tests; the
significant remainder are documented below (several are intentional, tested design decisions).

### Fixed (verified, applied, +tests)

| Finding | Fix |
|---|---|
| **P2** `routers/attention.py` — the module cache dict is mutated (`cache[k]=…`) and iterated (`set(cache)`) with **no lock**; `/api/attention` is a sync `def` so concurrent polls run on threadpool threads → `RuntimeError: dictionary changed size during iteration` → 500 (the sibling `trace_view` cache locks for exactly this) | added a `threading.Lock` around every cache mutation + the retire iteration |
| **P2** `span_index.py` `_read_full`/`full_span` returned whatever JSON parsed at the recorded offset **without checking span_id**, so an offset drifting onto a different valid span line (bit-rot / same-size in-place rewrite) returned the WRONG span — contradicting the module's "never wrong data" invariant | cross-check the read span_id against the row's indexed span_id; skip on a provable mismatch |
| **P2** `span_index.py` `_load_persisted` int-checked but did **not bounds-check** persisted `_o`/`_l`, so a corrupt `_l: -1` reached `f.read(-1)` and slurped the whole file into memory | reject negative/oversized/bool offsets at load (treat as a torn tail → rebuild the rest from truth) |
| **P2** `AttentionCenter.jsx` desktop-notification delivery effect keyed on `id:created` only, so an item that first arrived while its source was momentarily stale (`notifyEligible=false`, filtered out and never added to `notified`) never re-fired its OS notification once the source recovered | include `notifyEligible` in `deliveryKey`; re-delivery is idempotent via the persisted `notified` set |
| **P3** `Dock.jsx` timeline-window note used `{(filter.trim() \|\| kinds.size) && …}`, which renders a literal **"0"** when no filter is active (`'' \|\| 0 → 0`); the sibling line uses a ternary. My round-4 `.trim()` newly exposed it | boolean guard: `(filter.trim() !== '' \|\| kinds.size > 0)` |
| **P3** `AttentionCenter.jsx` `enableNotifications` set `valid:true` even when the underlying save failed; `disableNotifications` correctly gates on `result.ok` | gate enable on `result.state && result.ok` (mirror disable) |
| **P4** `routers/attention.py` sort tiebreaker `int(item.get("seq") or -1)` maps a genuine `seq==0` to `-1` (falsy-zero) | explicit `isinstance(seq, int)` check instead of `or -1` |

Regression tests: `test_span_index.py` (negative-length rejected + rebuilt from truth; offset-drift
returns None not the wrong span — both assert the tampered index actually loaded), `uiSemantics.test.js`
(delivery key includes `notifyEligible`; enable gates on `result.ok`; Dock boolean guard).

### Significant — documented, intentional or maintainer-decision

- **`engine_proc.py` tri-state `_engine_liveness` on flock-unsupported filesystems** (flagged by four
  finders). The old `_engine_alive` deliberately returned `False` on flock `ENOTSUP/EINVAL` (a
  load-bearing FUSE/geesefs/NFS comment: *"can't tell → not alive… so it doesn't block deleting a
  stalled run forever"*). The new probe returns `None` (inconclusive), so on those **documented-supported
  network mounts** a stopped run becomes undeletable (org 409), auto resume-reconcile stops, the SSE
  `alive is False` DONE guard never fires (finished run re-folds forever), and the `_engine_alive`
  compat wrapper *inverts* to `True` (a dead run reads as live). **This is a real regression on FUSE/S3
  mounts — but the `None`→fail-closed tri-state is intentional and comprehensively tested** (it prevents
  double-spawn/mutation when liveness is genuinely unknowable), so reverting `ENOTSUP→False` would
  reverse a deliberate safety decision and break tests. The proper fix is a *new* lock-less liveness
  path (PID/heartbeat), a design decision for the maintainer — not a review edit.
- **`replay.py` `_on_spec_proposed`/`_on_spec_approval_requested` early-returns** make the eval-spec fold
  order-dependent (`spec_approval_requested` is never reset), touching invariant #5. Normal seq-ordered
  emission is unaffected; flagged for the maintainer.
- **Attention scalability/UX** (all in the new feature, arguably by-design): `/api/attention` does
  O(all-runs) `iterdir`+`stat`+liveness-probe per poll; a cache hit short-circuits liveness so a
  *resumed* run reads as finished until its first append; a finished+failed+finalization-stalled run
  emits duplicate danger cards; a derived stall card has no upper age bound (one perpetual card per
  abandoned run).
- **Delta-trace edge/efficiency** (no round-trip/back-compat/fold bug — reconstruction is exact and
  order-tolerant): two latent metadata mislabels only reachable via a `carry==0`-with-back-ref writer
  shape the current writer never emits; `/spans/{sid}` re-hydrates the whole trace per single-span
  request; the write-side prefix compare is O(T²) over a tool loop (dwarfed by network latency).
- **Dock lazy-trace staleness** from dropping the whole-run poll: the setup-phase trace and an
  already-open `node_created` row no longer live-refresh; a transient poll error blanks the building
  trace instead of holding last-good. Product tradeoffs of the perf rework.
- **span_index / attention reuse** (drift risk, not live bugs): `_scan_light` + the cache-invalidation
  guard re-implement `eventstore._parse_jsonl_region` / `EventStore.read_all`; `useAttention` hand-rolls
  a poll instead of `usePoll`; `attentionStorage` duplicates `api.js`'s write-verify idiom; the
  attention router reads+parses each changed run's log twice per poll.

No new event types, Settings fields, or registry seams were added (the `_LAYOUT` map already lists
`attention`/`span_index`), and the new subsystems are documented in `docs/08-tracing-architecture.md`
and `docs/guide/concepts.md`, so no docs/diagram sync was owed.

---

## Round 7 — collaboration/comments mega-review + docs↔code audit (2026-07-14)

The branch advanced again with a **threaded-comments / collaboration** subsystem (`comment_projection.py`,
`command_observation.py`, `routers/collaboration.py`, `CommentsThread.jsx`, `commentsModel.js`,
`useComments.js`), incremental **command-ack observation**, and a JS **bundle-budget** CI gate. A seventh
pass ran **11 parallel agents** — 8 correctness finders over the new ~6.3k-line surface plus a **3-agent
docs↔code accuracy audit** — with adversarial verification. **No new P0/P1.**

### Docs audit (the "make docs match reality" pass)

- **`docs/guide/configuration.md`** — audited all **148** `Settings` fields against `config.py`: every
  default, bound, and `LOOPLAB_*` env var matches. **No changes needed.**
- **`docs/infographic/agent-architecture.html`** (process diagram) — every number/cadence/threshold
  verified against code (novelty 0.92, merge-every-3, digest caps, holdout 25%, repair-abandon-at-4,
  trust gates, all cadences). **Accurate; no changes.** The new cost/attention/span-index/comment
  subsystems are UI/observability or already gestured at (out of scope for a research-loop flowchart).
- **Fixed** (prose drifted from the shipped code): `CLAUDE.md` package map — `events/` now lists
  `comment_projection.py` + `span_index.py`; `serve/` lists all 11 routers + the new
  `run_commands.py`/`command_observation.py`/`attention.py`/`reviews.py` modules; `engine/` mentions
  `costs.py` + the expanded `finalize.py`. `docs/guide/concepts.md` — the command-observation monitor no
  longer "scans from the beginning each pass": the incremental indexed read model (P2) has shipped.

### Fixed (verified, applied, +tests)

| Finding | Fix |
|---|---|
| **P2** `comment_projection.py` — an edit/resolution overwrote the comment's `actor_kind` with the **editor's** identity, so a comment authored by the owner but edited by an operator was misattributed to the operator | keep the top-level `actor_kind` = creator; pass the acting actor into `_history_row` so each audit row still attributes its own change |
| **P2** `run_commands.py` + `comment_projection.py` — the run-level comment cap counted **legacy** `EV_ANNOTATION` notes (uncompactable in an append-only log), so a run with 500 legacy notes 409'd every modern comment forever; the per-subject cap already excluded legacy | count only modern comments against the run cap, in **both** the validation and the fold (kept in lock step so accept/fold never diverge) |
| **P2** `orchestrator.py::_ack_commands` — the ack cursor + seen-set were advanced (and the seen-set aliased + mutated) **before** the durable `command_ack` appends, so a non-fatal append failure lost those acks for the Engine's life → the UI's command falsely timed out | copy the seen-set (don't alias) and append **before** committing the cursor/seen, so a failed append leaves them unadvanced and the next call re-attempts (restoring the old self-healing) |
| **P2** `ui/scripts/check-bundle.mjs` — the "is main module" guard compared a non-realpath'd `argv[1]` to `import.meta.url`, so a **symlinked** invocation silently skipped `main()` and exited 0 (false-green CI gate) | resolve symlinks (`realpathSync`) before comparing (verified: the gate now runs via a symlink) |
| **P2** `Inspector.jsx` — the new `detailMatchesAttempt` guard required an **exact** attempt match, so a *fresher* `/nodes` payload (common right after an inline repair bumps `attempt`) flashed a spurious "attempt changed" error banner | accept `attempt >= nodeAttempt` (fresher-or-equal is current truth; only reject a genuinely staler payload) |
| **P3** `comment_projection.py::normalize_comment_text` — the whole untrusted body was `.strip()`+UTF-8-encoded **before** the byte-cap check (a 50 MB body fully materialized before rejection) | cheap `len(text)` guard before `.encode()` |
| **P4** `ui/scripts/check-bundle.mjs` — a stale header comment claimed the checker "is expected to fail until lazy boundaries land" though they landed and it passes | corrected the comment |

Regression tests: `test_collaboration.py` (cross-actor edit preserves creator authorship; legacy
annotations don't consume the modern-comment budget); `uiSemantics.test.js` (Inspector accepts a
fresher detail — and the stale round-5 assertion that pinned the old exact-match was updated).

### Significant — documented, intentional or maintainer-decision

- **`_engine_liveness` tri-state on flock-less mounts** (carried from round 6) remains the top
  maintainer item — intentional/tested `None`→fail-closed; a lock-less liveness fallback is the real fix.
- **`EV_ANNOTATION` reachable via `/control`** (not in `COLLABORATION_EVENTS`) folds into a *legacy*
  read-only comment without the command-protocol generation CAS — intended legacy back-compat, but the
  CAS-less legacy path is worth a maintainer note.
- **`review_comments`** re-reads/re-folds the whole log **uncached** per request for the untrusted
  reviewer (the DoS amplification `review_state` was hardened against), and discloses comments to
  summary-tier links with no `_evidence` gate (the handler frames comments as review-visible — verify intent).
- **Comment submits gated by the single-in-flight `_active_record` funnel** (409 during a Pause/Abort
  window) contradicts the engine-independence intent — may be an intentional single-writer simplification.
- **Version-chain order-sensitivity** in the comment fold (one no-op edit drops later valid edits) —
  intended idempotency defensiveness.
- **`command_observation.observe()`** lets a missing log raise `FileNotFoundError` uncaught (unlike
  `read_all`); `command_ack` counts as `max_non_control_seq` domain-progress (latent — consumer unused);
  `eventstore.py` broadened the non-`required` flock-acquire `except` to swallow more error types
  (unlocked-append degrade for more failure modes).
- **UI**: an unknown server `actor_kind` blanks the whole comments feed (over-strict page validation);
  the deferred "load timeline from report" trigger is unreachable dead code (`timelineDeferred` false in
  its branch); `LoadErrorBoundary` presents render bugs as "reload for a consistent build"; comment/badge
  counts undercount when paginated; node-annotations are no longer shown directly (they still surface as
  legacy comments in the thread). Product/UX calls for the maintainer.
- **Cleanup**: `span_index`/attention re-implement `eventstore` JSONL parsing + the cache-invalidation
  guard; `useAttention` hand-rolls a poll instead of `usePoll`; the attention router double-reads each
  log per poll; the React-Flow bundle budget lacks a `baselineRoots` (counts ~63 KiB of shared React).

Note: `ui/test/commentsContract.test.js` (a heavy real-vite-server + jsdom integration test added in this
merge) times out in this sandbox **independently of these changes** (confirmed against stock); it is an
environmental/CI-resourcing issue, not a code regression.

---

## Round 8 — full-branch mega-ultra review (2026-07-14)

A comprehensive pass over the **entire branch** (~49.5k lines): 12 subsystem-partitioned finders
(engine spine, engine cadence/finalize, events/replay, core, serve×2, tools, search/trust/runtime, UI
logic, UI components, **backend test quality, UI test quality**) plus adversarial re-verification of the
round-7 fixes. The `search/trust/adapters/agents` trees were byte-unchanged (only `jupyter.py`, correct);
UI components and the perm-modes/security surfaces came back clean.

### Adversarial self-review of round 7 (caught a real incomplete-fix — committed `d4d0379`)

Re-verifying my own round-7 changes: the `_ack_commands` reorder and the Inspector `>=` change were
independently confirmed correct, but the run-cap fix had updated **two of three** enforcement sites —
`_comment_precondition` (the append-time recheck) still used the total count, so a run with 500 legacy
notes accepted a comment at intake then dropped it at the append precondition. Fixed the third site +
an end-to-end test; also hardened `check-bundle.mjs` to match both raw and realpath'd argv (the
round-7 realpath-only form would false-green under `--preserve-symlinks`).

### Fixed (verified, applied, +tests)

| Finding | Fix |
|---|---|
| **P2** `appstate.state_payload` mutated the shared `_state_cache` with **no lock** while the branch added new concurrent callers (`/trace`, `/nodes` via `trace_scalars`→`state_payload`) → `set`/`pop(next(iter))` racing an insert throws "dictionary changed size during iteration" (500) on the hottest endpoints | added a `_state_cache_lock` around the insert+evict (matching the sibling `_trace_view_lock`) |
| **P2** `traceview.hydrate_inputs` was not total: a malformed `input_carry` (non-int → `full[:carry]` TypeError aborts the **whole** trace; negative → silent truncation) | coerce carry to a non-negative int (fall back to 0 — the delta stands as full input), matching the sibling readers |
| **P2** `ui/hooks.useMediaQuery` called `window.matchMedia()` unguarded → `TypeError` crashes the whole render where matchMedia is absent (jsdom/WebView) | guard matchMedia's existence, degrade to `false` |
| **P2** `ui/test/commentsContract.test.js` **hung the entire UI suite** (154s → SIGKILL): a focus assert failed (focus-restore is rAF-scheduled but `flush()` only pumped `setTimeout(0)`) and building an `AssertionError` over live DOM/React-fiber nodes wedged the event loop | `flush()` now pumps `requestAnimationFrame` (focus restores → assert passes) and the assert uses `assert.ok(a === b)` (primitive → no wedge on any future failure). **UI suite now runs commentsContract: 306 pass.** |
| **P2** `tools.FileBackups.save` wrote `.bak` then `.meta`; a `.meta`-write failure left an orphan `.bak` that `revert`'s missing-meta default (`existed:True`) reverts as a real snapshot | write `.meta` before `.bak` (a lone `.meta` is invisible to `revert`'s `*.bak` glob) |
| **P3** `ui/runIndex.indexProjects` sorted on raw `a.name.localeCompare` with no null-guard (siblings coerce) → a null project name breaks the whole run list | `String(a.name || '')` |
| **test-isolation** `test_trace_delta` toggled the process-global `set_llm_capture(True)` with no teardown → leaked to later tests (order-dependent flake) | autouse fixture saves/restores `_CAPTURE_LLM_IO` |

### Significant — documented (maintainer-decision / intended / latent)

- **`finalize._recover_scoped_terminal` re-materializes an error-finish scope forever** (P1, **reproduced**):
  `_scope_is_effective_terminal` returns False for `reason=='error'`, so on the common crash-then-resume
  path the recovery re-appends a duplicate `run_finished(error)` + report clone on **every resume**
  (verified 1→2→3→4) and never writes a `complete`/`abandoned` marker — violating the one-terminal and
  idempotent-resume invariants. The correct fix is a maintainer-level decision about the error-scope
  lifecycle (accept the error terminal as effective, or write an abandoned marker) touching a predicate
  with two call sites in replay-critical code; the existing test that should catch it is neutered by a
  stub. **Flagged, not auto-fixed.**
- **`finalize` non-modern trace-projection `raise`** — deliberately re-raises on a trace-build failure
  (pinned by `test_later_error_supersedes_historical_scoped_success_until_exact_republish`,
  `match="trace projection failed"`). Intended/tested; a FUSE crash-loop is the accepted tradeoff.
- **`_engine_liveness` ENOTSUP→None** on lock-less mounts (carried from round 6) — intended/tested.
- **Serve validation hygiene**: `assistant` turn/revert/stream + `/api/research` + `/api/genesis` parse the
  body unguarded → 500 (not 400) on malformed JSON; `assistant._public_scope` truncates-before-redacts
  (owner-only). Clean but low-severity; left for a follow-up to keep this round's blast radius tight.
- **Test coverage gaps** (the tests review the user asked for): the span-index wrong-identity test never
  proves the guard fired (offsets stay valid); the append-time comment TOCTOU reject branch, the
  failure-spike lower bound + ignored-reasons, and an HTTP-level TTL→410 are untested; `bundleBudget`
  only feeds synthetic manifests (never measures the real dist); source-scan tests are brittle+loose;
  3 of 4 vite-server suites lack the `optimizeDeps` race mitigation.
- **Lower-severity**: `failure_spike_seq` stale on a level fall (audit-only); `observedRunGenerations` map
  unbounded; `machine_runs` idempotency-key/gate-on-recovery contract edges; `SettingsForm` aria-controls
  + `accessibility` DataTable key (cosmetic); `llm` stream budget-in-`finally` (documented-accepted).

Full suite green after these fixes; UI 306 pass (commentsContract no longer hangs); build + mkdocs OK.

---

## Round 8 follow-up — owner-delegated fixes (2026-07-14)

The owner asked me to fix the documented round-8 findings at my discretion. Applied:

- **P1 (reproduced): scoped error-finish recovery loop** (`engine/finalize.py`). A scoped error finish
  (the guarded-abort path stages `{"reason":"error"}` in the begun marker, then a
  `run_finished(reason=error, finalize_scope=S)`) never converged: `_terminal_data_for_scope` returned
  that error payload, `_recover_scoped_terminal` re-materialized it, and an error finish is never an
  "effective terminal", so the scope never gained a complete/abandoned marker and a DUPLICATE error
  `run_finished` (+ report clone) was appended on **every resume** (unbounded growth; invariants #2/#3).
  Reproduced (2→3→4→5→6 across resumes) and fixed: when the recovery TARGET is itself an error terminal,
  close the scope idempotently (ack the finish + mark abandoned) instead of re-materializing; a
  non-error target still re-materializes and converges. `test_scoped_error_finish_converges_and_does_not_loop`.
- **500→400 hygiene**: `assistant.py` (turn/stream/revert/session-create) + `genesis.py`
  (`/api/research`, `/api/genesis`) now guard the body via a shared `_json_object` (a non-object body
  was a 500). `assistant._public_scope` now redacts before truncating.
- **UI**: bounded the `observedRunGenerations` map (`api.js`); `SettingsForm` sets `aria-controls` only
  on the active tab; `accessibility.jsx` `React.Children.toArray`s the wrapped table (no key warning);
  `package.json` runs `node --test --test-timeout=30000` so a wedged test fails with attribution.

**Deliberately left as-is** (documented, not bugs to silently flip): `_engine_liveness` `ENOTSUP→None`
(the tri-state fail-closed is intentional double-spawn safety — the right fix is a lock-less
PID/heartbeat path, a feature); the non-modern finalize `raise` (intended + tested); the streaming
budget-in-`finally` (accepted); `failure_spike_seq` staleness (audit-only). Remaining items are latent
(abort-recovery CAS, `_finish_if_quiescent` `next()` default) or test-coverage/test-infra notes.

---

## Round 9 — integrate master (Part IV concept graph) + review the incoming code (2026-07-15)

The owner asked to bring this branch up to date with `master` (30 new "Part IV" concept-graph commits),
verify nothing regressed, and confirm the incoming code matches this branch's reviewed quality/concepts —
fixing anything substandard — with the constraint "no loss of functionality, quality, or security; a
synergistic union" and "push only to this branch, never master".

**Integration method — a MERGE, not a linear rebase.** A `git rebase origin/master` was attempted and
abandoned: this branch already integrates master through seven prior merge commits, and master had
independently evolved the *same* security middleware (`serve/server.py` default-deny auth + the review
capability) and `events/replay.py::fold` paths this branch spent rounds 1–8 hardening. A 27-commit replay
reconstructs each of those security-critical intermediate states (the very first replayed commit already
forced a hand-reconstruction of the `assistant_shared` allow-list and, at commit 2, the whole
`_require_token` middleware) — precisely the regression risk the owner forbade. The three-way merge, by
contrast, conflicted in only **two** files, leaving the branch's reviewed code intact. Merge commit
`0c099f0` (signed, two parents; master is now an ancestor).

- **`events/replay.py`** (union): kept the branch's LLM cost/usage fold helpers AND master's four
  concept-graph handlers (`_on_node_concepts`, `_on_concept_consolidation`, `_on_hypothesis_concepts`,
  `_on_concept_coverage_snapshot`); the fold dispatch registers both, and the import list carries
  `EV_LLM_USAGE` + `EV_{CONCEPT_CONSOLIDATION,HYPOTHESIS_CONCEPTS,NODE_CONCEPTS}`.
- **`tests/data/golden_run_state.json`**: regenerated deterministically from the merged fold.

**Verification (all green):** full `pytest` (exit 0), UI `node --test` 306 pass, `vite build`,
`check-bundle.mjs` PASS, `mkdocs build --strict`, and an offline `looplab run` smoke + `looplab replay`
(8/8 nodes, deterministic rebuild). The merge preserved the branch's security model unchanged
(`server.py` did not conflict; the 30 new commits do not touch it), and master had correctly followed the
branch conventions it inherited (all new modules registered in the `looplab/__init__.py` `_LAYOUT` shim;
all seven new `Settings` fields carry `configuration.md` rows with correct defaults; new event types in
the registry; `BACKGROUND_APPENDABLE` gains only the selection-neutral `EV_LLM_USAGE`).

**Incoming Part-IV review (five parallel finders + adversarial verification).** The new ~4.4k lines
(`search/concept_graph.py`, `trust/verifier.py`, `core/fitness.py`, `tools/asset_brief.py`,
`search/{graded_novelty,novelty_recall,lock_in,research_targeting,taxonomy_dedup}.py`,
`engine/{strategy,novelty,proposal_cues}.py`, `cli/inspect_cmds.py`) are high-quality, deterministic,
offline-safe, and invariant-respecting. `SearchFitness` is byte-identical to the old inlined ordering on
the default (`select_verifier` off) path; the epoch-stamped `best_confirmed` guard is airtight; the
concept fold handlers are audit-only and never touch selection. Fixes applied (all low-severity — a
correctness edge + doc/comment drift, matching this branch's "stale docs/comments = a bug" rule):

- **`search/graded_novelty.py`** (comment only, after a self-review reversal): the level-2
  "same concept-set" filter uses `overlap(nd) == set(idea_concepts)` — the idea's COMPLETE concept set
  is subsumed by a tried node (`idea ⊆ node tags`), so it introduces nothing new. A first pass changed
  this to strict set-equality, but a `/code-review` adversarial pass showed that regresses live
  behaviour: with close params it moves the "idea drops a concept" case out of level 2 (which the live
  `_graded_novelty_precheck` safely DEFERS to the flat gate) into the level-4 ALLOW short-circuit — a
  wrong-allow. Reverted to master's containment (the safe direction) and instead clarified the comment
  to explain WHY it is containment, not equality.
- **`tools/asset_brief.py`** (offline fallback parser): the checkpoint value regex now captures
  sci-notation (`mse=1.2e-5` no longer truncates to `1`) and `@`-scores keep the MAX, not the first
  (`a@0.85_b@0.90` → `0.90`); the underscore-glued anchor case is preserved. (Two further tweaks tried
  in the first pass — a `>100` headline guard and a negative-sign `-?` — were reverted after the
  `/code-review` pass: the guard dropped legitimate `psnr` (a higher-is-better dB metric that exceeds
  100), and the sign let an anti-correlation head the "best result" line.)
- **Docs/comments**: `core/fitness.py` `selection_key`/`eligible` docstrings (holdout_topk now ranks by
  `promotion_key`); `search/concept_graph.py` touch-fraction denominator ("TAGGED experiments");
  `configuration.md` `select_verifier` holdout scope (verification fires on mean-metric ties, holdout
  keys reuse those scores); `cli/inspect_cmds.py` module docstring (some diagnostics invoke an LLM but
  none mutate a run); and the **process diagram** gained an "off"-tagged Concept-graph (Part IV) node
  under the Strategist column (`concept_pivot` / `graded_novelty` / `capability_expansion`), mirroring
  how the sibling verifier tie-break was added — closing the branch's "add a subsystem → update the
  diagram in the same change" rule.

Full suite + UI + build + bundle + mkdocs re-run green after the fixes.

---

## Round 10 — integrate 3 more master commits + fix an R1-c soundness gap (2026-07-15)

Master advanced 3 more commits (`3af1c62` → `b53531d`: D7 scored capability-expansion operator, R1-c
holdout-soundness producer with union-find tie-components, B1-ext hypothesis retro-tag). Integrated via a
clean three-way merge (no conflicts; the only fold change is the additive `hypothesis_concepts_at_vocab`).
Verified: full pytest (exit 0), UI 306, build, bundle, mkdocs. The two `test_attention` failures seen once
under concurrent-agent load were confirmed FLAKY (pass isolated + as a file; the merge touches no attention
code) — not a regression.

Reviewing the incoming code (5-probe adversarial pass on the union-find) surfaced **one real
selection-soundness defect** in the R1-c producer and fixed it:

- **`engine/strategy.py::_metric_tie_groups`** (correctness, opt-in path): the producer linked BOTH the
  robust and the holdout tie over the mean-pick pool (`confirmed if confirmed else eligible`). But
  `_select_best` ranks the holdout pick over the FULL eligible holdout pool (`hpool`), not the confirmed
  subset. So with a confirmed node present, an unconfirmed-but-holdout-scored node tied on the holdout
  metric was never surfaced/scored — it then competed in the holdout tie at the neutral verifier midpoint
  and could WIN it UNVERIFIED (reproduced: it became `best_node_id` over verified-but-lower siblings — the
  exact inverse of R1-c's "prefer the sound node" goal). Fixed by linking the holdout metric over the full
  eligible holdout pool (mirroring `hpool`); robust linking still uses the mean-pick pool. Byte-identical
  when `holdout_select` (or `select_verifier`) is off. Regression test added
  (`test_metric_tie_groups_surfaces_unconfirmed_holdout_tie_past_the_confirmed_pool`) — it fails against the
  merged code and passes after the fix.

The rest of the 3-commit delta is high quality and fully concept-aligned: every lever is gated off-by-default
(D7's `expand` operator, capability-expansion, graded-novelty all byte-identical on a default run),
`KIND_EXPAND` is registered in `search/policy.py` and bucketed generically by `operator_yields`, and the
union-find itself is deterministic/order-independent (verified across all permutations).

---

## Round 11 — Part-IV UI coverage gap analysis (2026-07-15)

The owner asked to plan how the UI should work with the new Part-IV features, "especially themes, concepts,
and their graph," and — after a follow-up — how those features would extend cross-run. A five-agent audit
(per-run backend data surface, UI theme/concept/graph coverage, UI selection/trust levers, cross-run backend
infra, cross-run UI surfaces) established: the shipped concept-graph / graded-novelty / verifier subsystem is
rendered **nowhere** in the UI; the UI's "theme" is the legacy flat `idea.theme`, a separate/weaker system the
concept graph supersedes; all six Part-IV events fall to raw-JSON in the feed and all seven Part-IV settings
are absent from `settingsSchema.js`; but the per-run data is **already served** on `/state`, so the per-run UI
is a pure read/projection fix. Cross-run is a genuine engine project — no concept aggregation exists and the
vocabulary is run-local. This dovetails with the concurrently-authored cross-run design in
[doc 17 §21.20](17-project-review-and-directions-2026-07-11.md) (Research Atlas, §21.20.7).

No code changed this round. The plan is written up as **PART V (§23–§25) of
[doc 18](18-ui-ux-review-2026-07-11.md)** — the gap, a two-horizon plan (Horizon A per-run visibility now;
Horizon B the cross-run Atlas, gated on the CR contracts), and the reuse/naming corrections — with a
cross-reference added at doc 17 §21.20.7. Two decisions left open for the owner: cross-run vocabulary scope
(narrow dense-retrieval skeleton vs broad with alignment) and the Horizon-A entry slice.

---

## Round 12 — Part-IV research-space UI/UX concept review (2026-07-15)

The owner requested a deeper concept review, particularly for theme/direction and concept graph views, with
new ideas permitted but no implementation. The feature branch was first fast-forwarded from
`origin/codex/ui-ux-overhaul-20260712` to `94ecb2e`; the incoming Round-11 PART V, doc-17 cross-reference and
this report were present with no merge conflict.

An independent Part-IV domain inventory, graph-UX design pass and adversarial semantics/accessibility/scale
pass were reconciled against the current React views and the exact concept/lock-in/novelty/verifier producers
and folded fields. They found two material corrections to Round 11:

- the current `/state` is sufficient for qualitative tags/signals, but not a canonical per-run concept DAG:
  it lacks graph labels/definitions, all multi-parent/typed edges, taxonomy revision/digest, assignment
  provenance/confidence/rationale and complete denominators;
- `top_concept_frac` is concentration among tagged experiments, not a generic coverage percentage. The compact
  snapshot omits the tagged count/totals and lock-in threshold/fired decision; `locked_axis/streak` is the
  historical maximum while `recent_axis/current_streak` is current narrowing.

Doc 18 PART V was therefore expanded from a feature-placement sketch (§23–§25) into the full concept
(§23–§32). It now separates three coordinated truth models — Experiment Lineage, legacy Direction lanes and
the multi-label Concept Map — and rejects concept-as-single-group in the existing DAG. New interaction ideas
include a stable Direction timeline, a Direction×Concept crosswalk (the migration view), matrix/list-first
Concept Landscape, axis-intersection matrix, temporal Journey and a bounded one/two-hop Focused Concept Map.
It details the matrix-first cross-run Atlas, proof/compare/Changes/Governance boundaries, state and edge
semantics, responsive/accessibility/scale contracts, a minimum sequence-fenced per-run `ConceptFrame`,
degraded-state matrix, acceptance scenarios and dependency-ordered C0/A/A+/B delivery plan.

This round changes documentation only. It does not claim a rendered prototype, browser usability result or UI
implementation; those are explicit gates in doc 18 §31–§32. The authoritative Atlas section in doc 17
§21.20.7 now links to the detailed spec and corrects “pure frontend” to apply only to atomic Horizon-A
readouts. Validation after the final edit: `git diff --check` clean; all relative links in the three modified
documents resolve; `mkdocs build --strict` succeeds; and the seven focused Part-IV concept/pivot/lock-in/
graded-novelty/select-verifier/verifier test files pass. No UI/source code changed, so a full application test
matrix was not represented as necessary evidence for this documentation-only round.

---

## Round 13 — integrate CR Steps 0+2, review the incoming code, refresh the concept docs (2026-07-15)

Master shipped the first cross-run concept-memory *implementations* (§21.20): CR Step 0 (universal any-script
`task_fingerprint`, off-by-default), CR Step 2 foundation (`ConceptCapsuleStore`, a fingerprint-keyed cross-run
concept bridge), and CR Step 2 (cross-run concept priors surfaced at the novelty gate, off-by-default).
Integrated by merge; the only conflict — `engine/finalize.py` — was because master hooked
`_store_concept_capsule` next to the old inline `_store_case` while the branch had reworked finalize into the
scoped-marker architecture; resolved by grafting the hook into the branch's scoped `case` step. Golden
regenerated byte-identical; full pytest + the 3 new CR suites + mkdocs green.

An adversarial review of the incoming CR code confirmed it is well-aligned with the engine invariants and the
§21.20.14 reuse rules (sidecar store, pure fold, engine-only audit event, no forked tagger/retriever/claim
type, byte-identical default). Fixes applied (all opt-in/off-by-default, no default-run behaviour change):

- **`core/config.py`**: documented `cross_run_concepts`'s two undocumented dependencies — the prior *surfaces*
  only when `graded_novelty` is also on (the read is behind that gate, so the flag alone silently accumulates
  capsules without emitting a `cross_run_prior`), and a non-Latin portfolio must also set
  `fingerprint_universal` or the fingerprint over-matches.
- **`engine/lessons.py`** (`store_concept_capsule`): the outcome map read `outcomes[c]` but wrote
  `outcomes[str(c)]` — made both use `str(c)` so a non-string concept id can't silently degrade best-of to
  last-of.
- **`engine/finalize.py`**: folded `events` twice in the scoped case step; reuse one `fold` result.

Separately, a fact-check of the concurrently-expanded doc 18 PART V (the other agent's §23–§32 UX concept)
found the per-run account accurate but the **cross-run sections stale** — written hours before CR Step 0+2
landed. Refreshed PART V (§23/§25/§30/§32) to record `RunState.cross_run_priors` as a served,
Horizon-A-renderable signal, reframe cross-run concept work from "unbuilt" to "engine substrate shipped;
read-model/endpoint + structured bundle + `concept_uid` alignment remain", correct the settings count to six
booleans, and note the engine emits no typed lateral concept relations yet (so `ConceptFrame.edges[].relation_type`
needs a new producer). Full suite + mkdocs green after the fixes.

## Round 14 — integrate CR Steps 1/3/4/5/6, review the incoming code, fix a claims under-count, refresh PART V (2026-07-15)

Master shipped the lean cross-run **read-model** slice on top of Steps 0+2: Step 1 CR0
(`engine/cross_run_index.py` — deterministic run-passport + facts index, `cross-run-index` CLI), Step 3
(`memory.portfolio_concept_overview` + `cross-run-concepts` CLI), Step 4 (`engine/claims.py::claim_assessments`
+ `claims` CLI), Step 5 (`build_context_pack` — bounded evidence+counter-argument pack) and Step 6 (the Research
Atlas DATA payload `portfolio_atlas` + `atlas` CLI). Integrated by merge with **no conflicts** (the branch's
finalize/events changes and master's new pure read-model modules are disjoint). Golden regenerated
byte-identical (no fold change); the 5 new CR suites + replay pass.

An adversarial review of the incoming CR code confirmed it is correctly built as a **pure read/projection**
layer: no second event writer (invariant #1 intact — `events/types.py`/`replay.py` untouched, no new event
type), `fold` never calls it (it is the outer consumer), deterministic rebuild (sorted by `(run_id, task_id)`,
no time/random), off-by-default (only `cli/inspect_cmds.py` + the shim import it; nothing in
`orchestrator`/`node_build`/`proposal_cues`/`strategy` touches it), reuse-rules honoured (claims UNIFY with the
D8 `_ClaimOut` shape, forks no third claim type; overview reuses `ConceptCapsuleStore` fields), and
malformed/offline/empty safe. One confirmed defect fixed:

- **`engine/claims.py`** (`claim_assessments`): support/oppose evidence were bare **run-local** node-ids
  unioned across runs. Node ids restart at 0 each run, so two independent runs each citing evidence `[0,1]`
  for the same statement collapsed to `{0,1}` → `n_support=2`, indistinguishable from a single run — the
  module's whole purpose (cross-run corroboration) was invisible in the `n_support`/`n_oppose` counts and the
  ranking key. Fixed by making every evidence ref **run-scoped `(run_id, node_id)`** (new `_refs` helper),
  which is what the design's `EvidenceRef` (§21.20.14) already specifies; the lean CR0 had flattened it.
  Bounded blast radius — the CLI/`render_context_pack` display only the counts, so refs surface only in
  `--json`; only `tests/test_claims.py` pinned the ref shape. Updated those assertions to the run-scoped shape
  and added `test_evidence_is_run_scoped_so_cross_run_corroboration_counts` (two runs → `n_support=4`, one run
  → `2`; fails on the old bare-id code, passes on the fix). The `runs`/`scopes` provenance sets and the
  `epistemic` verdict were already correct, so no claim was ever mis-verdicted — the fix corrects the
  advisory count/ranking only.

Left as a disclosed lean approximation (not a defect): `portfolio_concept_overview`/`portfolio_atlas` merge
concepts by **raw slug string** without a `concept_uid` resolver (§21.20.13 lists the resolver as a Step-3
precondition; this lean CR0 ships none). Slugs come from the already-consolidated per-run graph, so raw-string
merge under-merges divergent slugs but never over-claims — acceptable for an off-by-default read model, worth
tracking when the resolver lands.

Refreshed doc 18 PART V (§24.3 / §25 / §28 / §30.1 + the revision header) to record that the cross-run
**aggregate + Atlas data model now EXIST** as CLI/engine read-models: the story shifts from "engine substrate
shipped, read-model remains" to "read-models exist; only HTTP serving + React rendering + a `concept_uid`
resolver remain." Full suite + mkdocs green after the fix.

## Round 15 — integrate CR Step 5 advisory (first live-loop wiring) + master's design-blocker annotations (2026-07-15)

Master shipped two commits: `11c6d36` (**CR Step 5 advisory** — the gated flip of the cross-run context pack
from audit-only to a LIVE prompt cue) and `027704e` (**design-blocker annotations** — `# CODEX AGENT:` review
comments across `claims.py` / `novelty.py` / `lessons.py` / `memory.py` / `cross_run_index.py` / `config.py` /
`build.sh`, plus a `build.sh` whitespace fix). Integrated by merge; three conflicts, each resolved to KEEP the
branch's functional fix AND master's still-valid annotation (synergy, no loss either way):

- **`claims.py`** — my run-scoped `(run_id, node_id)` evidence fix (Round 14) vs master's annotations. Kept the
  fix in both loops; kept master's *lesson-outcome-vs-polarity* and *citation-is-not-verification* annotations
  (separate, still-open blockers); and **reconciled** the one annotation my fix resolves ("bare `set[int]`
  collapses run-local ids — use run-qualified refs") into a "fixed + still-open" note (run scoped now; a full
  `EvidenceRef` would also carry generation / measurement id / trust provenance).
- **`config.py`** — my Round-13 dependency doc (READ needs `graded_novelty`; non-Latin needs
  `fingerprint_universal`) vs master's annotation, which added a THIRD prerequisite I'd missed: the WRITE side
  needs `node_concepts` (normally from `concept_pivot`). Merged into one WRITE/READ dependency note.
- **`lessons.py`** — my Round-13 `str(c)` read/write-consistency fix vs master's annotation that the
  whole-node-score-to-every-concept outcome is confounded. Kept both.

**Reviewed the one functional change (`11c6d36`) against the engine invariants:** `_cross_run_advisory_text`
folds `render_context_pack` into the Researcher hint EXACTLY like the shipped E4 prior note; it is gated on the
new off-by-default `cross_run_advisory` flag, returns `""` on flag-off / no `memory_dir` / empty store / ANY
exception (prompt byte-identical when off), is advisory-only (never touches node selection, §21.7), and appends
no domain event (replay-safe). It composes cleanly with my ref fix — `render_context_pack` shows only the
counts, which are now the *correct* cross-run corroboration counts. New `test_cross_run_advisory.py` (7 tests)
pins the off-switch, on-path, coverage line, malformed-never-raises, and flag-defaults-off. `memory.py` /
`cross_run_index.py` master changes are annotation-only (verified: no code lines changed).

Docs: added §25 item (3b) to doc 18 noting the Step-5 pack now has an off-by-default *live-loop* path (so the
read-models are no longer purely off-loop for the engine, only unserved for the UI); doc 17's "NOT wired into
any live prompt" auto-merged to "Advisory wiring also LANDED"; `configuration.md` gained the `cross_run_advisory`
row (default off). Deliberately did **not** add `cross_run_advisory` to the process diagram: the diagram depicts
neither its sibling `cross_run_concepts`/`cross_run_prior` nor the E4 prior note (this whole off-by-default
cross-run prompt-cue family sits above the flowchart's granularity), so adding only this one would be
inconsistent. `build.sh` remains a maintainer concern flagged by master's own annotation (committed
developer-local transcript with an internal URL); left as-is — not this branch's file to rewrite.

Full suite + mkdocs --strict green after the merge.

## Round 16 — integrate the CR 30-finding fixes + Part-V CrossRunTools + §22 role-access; adapt + mega-review (2026-07-15)

Master shipped four commits: `19efc85` (**resolves the 30 CODEX design-blocker findings** the previous round
merged as annotations — heavy rewrites of `claims.py`/`novelty.py`/`memory.py`/`lessons.py`/`cross_run_index.py`),
`f716b99` + `0ab6a1b` (**Part-V role-access §22 + a read-only `CrossRunTools`** wired into Researcher /
Strategist / deep-research / a role-scoped Developer, under the new off-by-default `cross_run_read_tools`), and
`7b35d54` (tests). Highlights of master's 30-finding fixes, all adopted as the canonical form:

- **claims.py** — evidence refs are now run-QUALIFIED `"run:node"` strings (`"?:node"` for unknown run); `_slim`
  keeps `runs`/`scopes` and caps statements; `portfolio_atlas` `n_runs` unions capsule run_ids AND lesson-cited
  runs (a lesson-only memory is no longer reported as zero runs); `max_items` normalized.
- **novelty.py** — the gating grade is computed WITHOUT cross-run priors, so `cross_run_concepts` is now truly
  byte-identical to off for SELECTION; priors are surfaced audit-only on the idea∩prior overlap, with a HARD
  direction gate (a min/rmse prior can't cross into a max/recall task) and filter-then-cap-top-8.
- **memory.py** — `CONCEPT_CAPSULE_VERSION` + a per-row `_valid_capsule` quarantine (a string `concepts`
  field can no longer iterate into character-concepts), `task_id` on the capsule.
- **lessons.py** — the capsule BUILD stays best-effort but the WRITE moved OUTSIDE the try, so a real
  persistence failure now reaches finalize's retry handshake instead of being silently swallowed.

**Adaptation (this is where the branch had to give way, synergistically).** My Round-14 lean fix had made the
same evidence refs run-scoped, but as `(run_id, node_id)` TUPLES; master's independent fix uses `"run:node"`
STRINGS and does strictly more. I retired my tuple `_refs` helper and adopted master's `_qualify` wholesale
(claims.py/lessons.py/config.py/test_claims.py resolved to master's canonical form), then re-added my one
genuinely-additive test — `test_evidence_is_run_scoped_so_cross_run_corroboration_counts` — rewritten to the
string shape. Preserved one branch-only doc point master's note lacked (a non-Latin portfolio should also set
`fingerprint_universal`).

**Regression caught + fixed (the load-bearing catch).** Resolving `config.py` with `git checkout --theirs`
was too blunt: it discarded the whole 3-way merge for that file, silently dropping the *branch's* earlier
security hardening — the `max_parallel`/`n_seeds`/`max_nodes` upper-bound validators (Round 12: an unbounded
`max_parallel` from a crafted preflight would fan out that many sandboxes). The full suite caught it
(`test_config_upper_bounds_reject_resource_exhaustion` + preflight `max_parallel`). Restored the `le=` ceilings
onto master's canonical file, and audited the other two `--theirs` files (claims.py, lessons.py) to confirm
they lost only intentionally-superseded content. Full suite green after the restore.

**Mega-review** (6-dimension parallel finders + adversarial verifiers over the new code; 21 agents,
15 raw → **5 CONFIRMED, 0 uncertain**, the other 10 refuted as by-design / documented-TODO / not-reachable).
It confirmed the read-model key contracts all match empirically (`portfolio_concept_overview` →
`concept/n_runs/runs[{run_id,metric,direction}]`, etc.), `CrossRunTools.execute` never raises, the role-scoped
CLAIM split is real (not a no-op), and no engine invariant is violated. **All 5 confirmed findings adapted in
this branch** (they harden master's own new code without changing its design):

- **[medium] test-coverage — the HARD direction gate was untested.** `_cross_run_prior` drops a prior unless
  its `direction` matches the run's, but the only opposite-direction test failed the Jaccard floor *before*
  the gate ran, so removing the gate left the suite green. Added
  `test_cross_run_direction_gate_suppresses_opposite_direction_prior`: a prior with the same goal/metric/kind
  but opposite direction clears the sim floor (Jaccard 0.667 > 0.3) yet is dropped by the gate; the
  same-direction twin surfaces — isolating the gate.
- **[low] claims.py — `portfolio_atlas` reported two different `n_runs`.** Top-level `n_runs` (capsule ∪
  lesson-cited runs) disagreed with the embedded `context_pack.coverage.n_runs` (capsule-only), re-exposing the
  "zero runs for lesson-only memory" artifact the union set out to fix. Fixed by passing the unioned count into
  the context pack's coverage; pinned by `test_atlas_n_runs_counts_lesson_runs_and_stays_internally_consistent`.
- **[low] test-coverage — the `_valid_capsule` poison quarantine was untested.** Added
  `test_poisoned_capsule_row_is_quarantined_not_fatal` (a string `concepts` / int fingerprint / empty run_id
  row is dropped while the valid row still loads and isn't poisoned into character-concepts).
- **[low] doc-sync — `configuration.md` said the Developer-scoped read tool was "a follow-up"** though the same
  change wires `CrossRunTools(role="developer")` (and §22.6 marks it DONE). Corrected the settings-table row.

Refuted (recorded, not changed): `delete_run` doesn't purge the portfolio-shared `memory_dir` (the §22.6-step-4
operator purge control-event is the intended deferred remedy); `_role_lessons` fails OPEN for an unknown role
(documented "anything else sees all"; not reachable — every call site passes a hardcoded `researcher`/`developer`);
the concept-coverage atlas is portfolio-wide by design (only the claim stream is role-split); the `_toks` len>2
short-token filter is the shipped codebase-wide tokenizer convention. None are regressions from this merge.

**UI-plans docs updated** (doc 18 PART V, per request): §25 gains items (3b)/(3c) — the read-models now have
live consumers (the advisory prompt path AND the agentic `CrossRunTools`), realizing doc 17 §22's first two of
three delivery mechanisms; §28.3 anchors Findings to run-qualified citable `"run:node"` refs + role-routed
claim streams; §28.5 anchors the governance workbench to the shipped `CONTROL_EVENTS` operator-write substrate
(§22.4: agents READ but never EDIT). The "what remains for the UI" story sharpens to *HTTP serving + Atlas React
surface + operator governance workbench*. Full suite + mkdocs --strict green.

## Round 17 — integrate §22.4 governance WRITE + §22.6 serve API + Genesis/Strategist reads; verify + doc-sync (2026-07-15)

Master shipped the slice that finishes the cross-run consumer model (4 commits): `c41a5f6` (**task-scope the
read tool** — a live-test leak fix), `b0be675` (**§22.4 operator claim decisions** — the governance WRITE
path), `9e6e0bb` (**§22.5 Genesis** cross-run read), `cfa1f86` (**§22.6 cross-run serve API** + Strategist
coverage cue). Integrated by merge; one conflict — `serve/server.py`'s router import list — resolved as a clean
UNION (the branch's attention/collaboration/reviews routers AND master's new `cross_run` router; the build list
auto-merged with all of them). `claims.py` and `test_claims.py` auto-merged cleanly, preserving my Round-16
`portfolio_atlas` n_runs fix + the run-scoped-corroboration regression test alongside master's new governance
overlay.

What landed, reviewed against the invariants:

- **Governance WRITE (`claims.py`):** `record_claim_decision` → append-only, reversible sidecar
  `claim_decisions.jsonl` keyed by `normalize_statement` (last-wins), decision ∈ {ratified, rejected, pinned},
  validated (raises → HTTP 400). `claim_assessments`/`portfolio_atlas` gained a `decisions=` overlay adding a
  `maturity` field; `build_context_pack` DROPS `operator-rejected` claims and surfaces `operator-ratified`
  first. This is a sidecar store, NOT a `CONTROL_EVENTS` domain event — cross-run meaning is portfolio-scoped
  and lives outside the per-run replay/fold path, which is correct (the engine-sole-writer invariant governs
  the event log, not the memory-dir sidecars).
- **Serve API (`serve/routers/cross_run.py`):** `GET /api/cross-run/atlas` & `/claims` (reads with decisions
  overlaid) + `POST /api/cross-run/claim-decide` (the operator write). Verified it is token-guarded like every
  other write: `server.py`'s default-deny middleware requires the UI token on every `/api/` route outside the
  tiny zero-model liveness allow-list, so the POST is not reachable unauthenticated — **no auth gap**.
- **Task-scope leak fix (`cross_run_tools.py`):** now implements `bind_state(self, state, parent=None)` (correct
  ToolProvider signature) and scopes a BOUND run's queries to its own task (or overlapping goal terms), while an
  UNBOUND CLI/human query stays portfolio-wide. Also honors operator decisions (drops rejected in
  `cross_run_claims`). Closes the leak where a live run's agent could read every other task's cross-run data.
- **Genesis (§22.5) + Strategist coverage cue (§22.6):** additional off-by-default read consumers; advisory,
  never touch selection, best-effort.

Verification: 98 targeted cross-run/server/strategist tests + the full suite green (exit 0), `mkdocs --strict`
green. An adversarial mega-review (6-dimension finders + verifiers, focused on the governance write-path
security + serve auth surface) ran alongside; its verified findings are applied in the follow-up.

**UI-plans docs updated** (doc 18 PART V, refresh #4): the Atlas UI is now **no longer blocked on any backend**
— read-models + HTTP endpoints + operator write all ship, only the React surface remains (§24.3/§25(4)); §28.3
gains the four `maturity` badge states; §28.5 is **corrected** from the earlier "governance via `CONTROL_EVENTS`"
assumption to the shipped mechanism (a token-guarded serve `POST` → append-only sidecar `claim_decisions.jsonl`,
deliberately outside the replay/fold path).

## Round 18 — pull 26 commits, global adversarial/UI re-review, and status correction (2026-07-15)

Fast-forwarded `codex/ui-ux-overhaul-20260712` from `85959f7` to `08bf868` (26 commits, 46 files, roughly
3.5k changed lines). The branch matched `origin/codex/ui-ux-overhaul-20260712` after the pull; there were no
merge conflicts and no local change was discarded.

Round 17's last paragraph above is retained as historical chronology but is **superseded** by this review.
The new endpoints complete a useful lean transport slice; they do not complete the backend contract of the
Research Atlas specified in docs 17/18.

### Verification completed

| Check | Result |
|---|---|
| full Python test suite | PASS, exit 0; only existing FastAPI lifespan/deprecation warnings |
| React/Vitest suite | 306/306 PASS |
| production Vite build | PASS |
| bundle-size/reachability budgets | PASS |
| `mkdocs build --strict` | PASS |
| paid OpenRouter `minimax/minimax-m3` smoke | PASS: text response `ready`; structured parse returned `operator=user-proposed`, params `x=1.0`, `y=2.0` |
| `git diff --check 85959f7..08bf868` | PASS |
| browser walk-through against local API (57 runs) | completed: List, Map, run Report, 131-node Search/timeline and Settings |
| focused adversarial diagnostics | confirmed one-word cross-scope match, rejected-claim Atlas leak, 160-character claim-key collision and index tie order dependence |

The full and targeted suites being green means the incoming slice is regression-compatible with its current
tests. It does not invalidate the confirmed edge/contract failures; those scenarios were absent from the suite.

### Corrected implementation verdict

**Shipped:** Unicode fingerprint opt-in, concept capsules and audit receipt, lean run-passport/facts index,
raw-slug portfolio overview, statement-grouped claims, count-bounded context pack, capped Atlas summary,
CLI commands, experimental agent read/advisory consumers, `GET /api/cross-run/atlas` / `claims`, and token-
guarded `POST /api/cross-run/claim-decide` when UI auth is configured.

**Not shipped:** fail-closed cross-run scope and comparison contracts; stable concept/claim/attempt identity;
eligible measurement and independent evidence-family projection; snapshot/watermark/index health; partial/
corrupt/restricted states; server filtering and cursor pagination; agent-safe governance projection; token
envelopes; revisioned durable decisions and audit history; per-run `ConceptFrame`; Research Space/Atlas React
routes; Part-IV/Part-V Settings controls.

Product label: **lean cross-run summary + claim-decision overlay**, not “backend-complete Research Atlas” and
not “audited/reversible governance.”

### Confirmed P1 findings

1. **Scope leak:** `CrossRunTools` admits a foreign row after one shared goal word; proactive Researcher and
   Strategist read all of `memory_dir`; Repo Developer never binds its provider. Require a fail-closed immutable
   `CrossRunScope` with namespace/project, run exclusion, hard task/metric/direction/dataset/eval compatibility.
2. **Rejected-claim leak:** context packs drop operator-rejected claims, but Atlas contradictions, the tool and
   Strategist still surface them. Split human history from the agent-safe active projection.
3. **Durable prompt injection/secrets:** model-authored statements/concepts/run IDs pass into prompts and traces
   without redaction/trust envelope, including a Developer loop with write/edit tools. Cap/redact before trace,
   model and UI; quote as data; retain provenance; test injections, credentials and ANSI/OSC controls.
4. **Global colliding claim identity:** normalized 160-character statement keys merge scopes/long prefixes and
   allow decisions on nonexistent/future claims. Add versioned `claim_uid`, evidence revision, existence check
   and CAS/409 semantics.
5. **Negative proposition inversion:** a confirmed negative result such as “raising LR regressed the metric” is
   mapped to opposition because outcome utility and proposition truth are conflated.
6. **Ineligible concept outcomes:** capsules can publish metric-bearing infeasible, tombstoned or trust-flagged
   nodes without metric/split/comparison receipts.
7. **Degraded index masquerades as complete:** valid prefixes of corrupt logs and missing/garbled snapshots are
   indexed without health metadata; reset generations collapse.

### P2 findings that affect product truth/scale

- decision writes are unlocked/non-fsynced last-write-wins appends with no server actor/time, sequence,
  idempotency, revision, reset or history API;
- routes and tools scan whole stores, have no cursor/resource contract, and decision text lacks length limits;
- malformed rows silently disappear, so unknown/partial/corrupt can look like an honest empty 200;
- context packs are claim-count/field-capped, not token-bounded;
- live Researcher/Strategist prompt influence records no derivation receipt (scope/snapshot/query/result digest,
  consumer/turn/redaction policy), so the influence is not auditable even if the final run replays;
- support counts measure attempt refs, not independent replications, and lose consolidated `evidence_count`;
- cited D8 claims become support without consuming their verification verdict;
- `cross_run_prior` can be recorded against a prospective/replaced/orphaned node;
- fingerprint/taxonomy identity is unpinned, index tie order is not fully deterministic, Genesis can advertise
  filesystem tools it lacks, and invalid flag combinations silently no-op.
- runtime CLI/router/docstrings still overclaim “evidence-grounded,” “token-bounded” and reversible governance;
  the guides are corrected here, but product help must be aligned before UI exposure.

### Browser/UI conclusions

- List and Report retain good hierarchy and should remain operational/reporting surfaces.
- Search at 131 nodes is already dense (chips + DAG + inspector + 586-event timeline); concept topology does
  not belong on that canvas.
- global Map at 57 runs shrinks to an almost unreadable horizontal strip. This directly validates a separate,
  matrix-first Atlas and rejects a portfolio-wide concept force/DAG graph.
- Settings has coherent grouping/help but no Part-IV/Part-V controls.
- no Atlas or per-run Research Space route, client or navigation affordance exists.

Doc 18 now contains the corrected current-status ledger and a full global re-review in §33: browser evidence,
canonical snapshot-fenced routes, per-run Research Space, Direction×Concept crosswalk, matrix-first Atlas,
bounded focused graph, responsive/accessibility/state grammar, governance conflict UX and dependency gates
G0–G6. Doc 17 §21.20.13 and §22 now reflect actual lean behavior and safety/promotion gates. User-facing config
and CLI reference wording is corrected in the same round.
