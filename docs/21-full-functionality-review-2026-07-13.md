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
