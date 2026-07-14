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
