# LoopLab — Comprehensive Code Review

<!-- CODEX AGENT: 2026-07-23 current-master addendum. PR #7 was inspected but is not safe to merge:
its head `99c39b08` descends from stale `9aaa0485` while master is `60e9a5f3`, and its effective diff
deletes the Card verdict chip, the authoritative `+ Add` control, and the current cards-only review
annotation. Its thirteen substantive reliability/security/performance findings were independently
rechecked and already exist on master at their authority boundaries, so copying the PR would add no
finding while regressing newer UI work. This pass additionally confirms that (a) Card abandon remains
available only in the unreachable legacy fallback, (b) `grouped_beliefs()` can merge the known
short-hash collision pair despite replay's full-digest protection, and (c) the early-dense ASHA curve
fix still leaves every mid/late coordinate incomparable under exact-rung lookup. The current Part IV/V
release-blocker ledger is maintained in `23-hypothesis-card-kanban-2026-07-20.md`; this historical
whole-repository review remains below unchanged. -->

**Date:** 2026-06-23 · **Scope:** entire repository (not a single diff) · **Method:** 9 parallel
read-only review passes (6 backend subsystems + working-tree diff + test suite + remaining UI +
CLI/tooling/packaging/docs), every CRITICAL cross-checked against source. No code was modified.

> Reviewed: ~7 000 LOC Python (`looplab/`, 35 modules) · ~2 000 LOC React (`ui/src/`) · 22+ test
> files · `tools/`, `pyproject.toml`, `README.md`, `docs/`.

---

## Verdict

The **architecture is sound**: append-only event log as source of truth, pure `fold`, a genuine
read/control-process split (ADR-18 is actually upheld), trust-tiered sandbox, zero-dependency
tracing. Engineering taste is visible throughout, and some areas (`validate.py`, `repo_task`,
tracing, projects) are well-tested.

The **systemic problem is the gap between claimed guarantees and enforced ones.** Three of the
project's headline promises — *metric integrity*, *secret-leak protection*, and *deterministic
crash-resume* — are asserted in docs/tests but not actually enforced in code. Separately, the UI
control-plane is unauthenticated with wildcard CORS and an arbitrary-file-read hole. None of these
block local single-operator use on a trusted box (the project's stated default), but each is a real
hole the moment the threat model widens, and the docs currently overstate the protection.

Severity legend: **🔴 Critical** (exploitable / data-loss / breaks a core invariant) ·
**🟠 High** (real bug, reachable) · **🟡 Medium** · **⚪ Low** (cleanup / hygiene).

---

## 1. Critical cross-cutting risks

These recur across multiple subsystems; fix them at the mechanism, not per-site.

### C1 · Metric integrity is not enforced on any tier
The whole selection loop (`policy`/`gate`/`confirm`) trusts `node.metric`, but on the default path
the **graded code prints its own metric** (`stdout_json` takes the last JSON line with a `metric`
key — [sandbox.py:63](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/runtime/sandbox.py)). `print(json.dumps({"metric": 0.0}))` wins the search.
Reinforcing failures:
- Docker eval mounts the workspace **read-write** (no `:ro`, no `--read-only` —
  [command_eval.py:175](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/runtime/command_eval.py)), so code can overwrite the metric file or the
  "frozen" grader/adapter at runtime; the drift cross-check is opt-in **and reads from the same
  writable mount**.
- RepoTask protects only the *primary* metric reader; `metrics` / `constraints` / `cross_check`
  reader paths are **never added to `_protected_names`** ([repo_task.py:305](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/adapters/repo_task.py)),
  so secondary/constraint/drift values are forgeable.
- `protect` is matched by **exact string, not glob** ([repo_task.py:296](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/adapters/repo_task.py)) —
  `protect=["*.py"]` silently protects nothing.
- mlebench grader with the answer key `_Y` runs **in the candidate's own interpreter/workdir**
  ([mlebench.py:194](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/adapters/mlebench.py)); `import grader; grader._Y` or unlimited oracle
  probing of `score()` ([mlebench.py:105](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/adapters/mlebench.py)) reconstructs all labels.
- **No test** proves a label-reading solution is blocked or scores 0 (test review H3).

→ For any non-trusted run, compute the metric **host-side** from artifacts the code cannot rewrite;
grade out-of-process; mount inputs `:ro`; protect every reader path; glob-match `protect`.

### C2 · The "secret-leak gate" (README I21) does not exist
`leakage.py` is **ML** data-leakage detection, not secret scanning. There is no runtime
redaction/scan of generated-code stdout/stderr or LLM completions — only config masking
(`masked_snapshot()`). Worse, the child process inherits the **entire host environment**
(`full_env = {**os.environ, …}` — [sandbox.py:102](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/runtime/sandbox.py)), including
`LLM_API_KEY`/cloud creds, and stdout/stderr tails are persisted verbatim into the event log/UI
([orchestrator.py:752](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/engine/orchestrator.py)) and into `spans.jsonl`
([tracing.py:104](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/core/tracing.py)). One `print(os.environ)` or a stack trace exfiltrates
secrets into durable artifacts served over CORS `*`. Docs (`README:15`, `03-decisions.md:301`:
"redaction filter + gitleaks over the event log") claim a control that isn't implemented.

→ Pass a minimal env allow-list to children; add a redaction pass over every tail before write; or
correct the docs to "config-masking only."

### C3 · UI server is an unauthenticated control-plane
No auth on any endpoint. `POST /api/start` spawns `subprocess.Popen([... "looplab.cli", "run",
task_file …])`; `PUT /api/{kind}/{name}` writes arbitrary `.md` into prompt/skill dirs the engine
hot-loads; the new `DELETE /api/runs/{id}` does `shutil.rmtree`. With `allow_origins=["*"]`
([server.py:87](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/serve/server.py)) **any web page the operator has open** can drive all of it
cross-origin (CSRF). Plus an **arbitrary-file-read**: the SPA fallback `GET /{path:path}` →
`FileResponse(dist / path)` has **no traversal guard** ([server.py:488](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/serve/server.py)) while
every other file route does — `GET /..%2f..%2f..%2fWindows%2fwin.ini` returns any readable file.
`task_file` from the request body is executed without an allow-list
([server.py:388](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/serve/server.py)).

→ Token on all mutating `/api/*`; CORS allow-list to `http://localhost:5173`; resolve-guard the SPA
route; bind loopback-only; validate `task_file`.

### C4 · Replay/durability invariant holds only within one schema version
- `Event` has no `model_config` → Pydantic v2 `extra="ignore"`; the `v` version field is **written
  but never read**, and `fold` silently ignores unknown event *types*
  ([replay.py:147](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/events/replay.py)). A v2 log read by v1 code produces a **different, wrong
  state with no error** — the opposite of a trustworthy source of truth.
- The interprocess append lock **swallows `OSError` and writes unsynchronized**
  ([eventstore.py:38](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/events/eventstore.py)) exactly when two writers contend; `iter_jsonl` then
  truncates the log at the first torn line ([eventstore.py:71](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/events/eventstore.py)) → **silent
  event loss**.
- `fold` is **not idempotent** for terminal node events: two `node_evaluated`/`node_failed` both add
  to `total_eval_seconds` and leave status order-dependent ([replay.py:46](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/events/replay.py)).
- **No test** proves crash+resume reconstructs *identical* state or that no node is re-evaluated
  (test review H1); the two-writer lock has **no test that can fail** (test review H2).

→ Read/enforce `v`; `extra="allow"`; make terminal events idempotent in `fold`; fail loudly on lock
acquisition failure; add a real multi-process append-race test and a crash-vs-clean state-equality
test.

### C5 · Read-model can permanently diverge from the log, undetectably
SQLite read-model is rebuilt **only at engine exit**, non-atomically (`DROP`+`INSERT` on the live
file — [readmodel.py:27](https://github.com/ArtyomZemlyak/looplab/blob/master/looplab/events/readmodel.py)), is **never refreshed** for post-run control
events (pause/promote/annotate), and stores **no source watermark** (last seq/hash). "Always
reconstructable" is true on paper but nothing reconstructs it on the live UI path.

→ Rebuild into a temp DB + `os.replace`; stamp the max applied `seq`; refresh after appends (or have
the UI derive from `fold`, not stale SQLite).

---

## 2. Findings by subsystem

### Core engine / orchestrator
| Sev | Loc | Finding |
|---|---|---|
| 🔴 | orchestrator.py:446 | Parallel eval (`max_parallel>1`) has **no compute-budget guard** — a batch blows past `max_eval_seconds`; the guard exists only in the sequential branch. |
| 🔴 | replay.py:46 | `fold` not idempotent to duplicate terminal events (see C4). |
| 🟠 | operators.py:24 / orchestrator.py:903 | `merge_idea`/`_ablate` crash on a non-numeric param (`sum()`/`float`) **without writing an event** → loop re-attempts the same merge forever on resume. |
| 🟠 | orchestrator.py:797 | Variance gate weakened by `confirmed_seeds or confirm_seeds`: `0` is indistinguishable from "unknown", inflating n / shrinking SE — the exact bug the adjacent comment warns against. |
| 🟠 | orchestrator.py:709 | Abort-watcher rescans the **whole** log every 0.3 s per in-flight eval (O(events)·N_parallel). |
| 🟡 | cv.py:36 | `purged_walk_forward`/`kfold` silently drop folds when `embargo ≥ fold` or `k>n`; no signal that CV degenerated. |
| 🟡 | leakage.py:52 | `temporal_leakage` flags a train row exactly at the boundary (`>=`), aborting valid runs with coarse (day-granularity) timestamps. |
| 🟡 | orchestrator.py:56 | `_workspace_fingerprint` uses `st_mtime_ns` — `git checkout` (content-identical, mtime-changed) triggers false `workspace_changed`. |

### Persistence / event-sourcing
| Sev | Loc | Finding |
|---|---|---|
| 🔴 | eventstore.py:38 | Lock swallows `OSError` → unsynchronized write → torn line → silent truncation (C4). |
| 🔴 | readmodel.py:27 | Read-model diverges undetectably (C5). |
| 🟠 | eventstore.py:104 | `_disk_last_seq` and `iter_jsonl` use **different** "last record" rules → seq gap after a crash. |
| 🟠 | memory.py:30 | `JsonlCaseLibrary._flush` does a full `write_text` (non-atomic) — a crash mid-write loses **all** cross-run memory; `atomic_write_text` exists but is unused. |
| 🟠 | retrieval.py:55 | `read_file` reads any absolute path — no root containment (path traversal / arbitrary read). |
| 🟡 | eventstore.py:81 | `EventStore.__init__` does an O(events) full scan; the UI constructs one per control POST → quadratic over a session. |
| 🟡 | eventstore.py:70 | `iter_jsonl` `break`s on the first corrupt **interior** line → silently hides all later events. |
| 🟡 | atomicio.py:17 | No parent-dir fsync after `os.replace`/first-create — rename can be lost on crash. |

### LLM / roles / external agent
| Sev | Loc | Finding |
|---|---|---|
| 🔴 | cli_agent.py:197 | Prompt-injection → host RCE chain: LLM `rationale`/`params` feed an external coding agent that runs **unsandboxed** on the default path (patch-gate is opt-in). |
| 🔴 | llm.py:53 | `_post` has **no** network/HTTP error handling — `URLError`/`HTTPError`/timeout/non-JSON aborts the whole run; `urllib.error` is imported but unused. |
| 🟠 | llm.py:71 | `body["choices"][0]` unguarded → `KeyError`/`IndexError` on an Ollama error envelope. |
| 🟠 | cli_agent.py:203 | Agent subprocess launched **without a process group** — on timeout only the direct child is killed; node/opencode grandchildren orphan and hold the temp dir. `command_eval` already has `_kill_tree`; `cli_agent` doesn't reuse it. |
| 🟠 | cli_agent.py:85 | `.cmd`/`.bat` launcher fallback runs the multi-line untrusted prompt through `cmd.exe` → metachar injection. |
| 🟡 | roles.py:111 | `LLMResearcher._clamp_fill` calls `float(params[k])` without `_sanitize` → `ValueError` on `{"x":"high"}` crashes the retry-protected propose. |
| 🟡 | parse.py:45 | `_extract_json` returns the **first** JSON object — a model's example/echo wins over the real answer. |
| 🟡 | roles.py:160 | Retry re-sends the identical request (no temp bump / no failure feedback) → same unparseable output. |
| ⚪ | skills.py:23 / prompts.py:29 | `read_text(utf-8)` without `errors=` → a cp1252/UTF-16 skill/prompt file crashes load on Windows. |

### Sandbox / trust / security
| Sev | Loc | Finding |
|---|---|---|
| 🔴 | sandbox.py:63 / command_eval.py:175 | Self-reported metric + writable mount (C1). |
| 🔴 | leakage.py / sandbox.py:102 | No secret gate + full host-env to child (C2). |
| 🟠 | sandbox.py:214 / command_eval.py:177 | Docker has **no** `--memory`/`--pids-limit`/`--cpus`/`--read-only`, runs as **root**, writable host mount — fork-bomb/OOM + host-write despite `--network none` (the only real isolation). |
| 🟠 | sandbox.py:110 | `communicate(timeout)` buffers all child output in memory until timeout — `while True: print('A'*10**6)` OOMs the **host**; the byte cap is post-hoc (storage only). |
| 🟠 | patch.py:22 | Surface gate doesn't parse `rename to`/`copy to`/`GIT binary patch`, and ignores symlink/junction escape; `apply_patch` calls `gate` **without** `protect`/`prefixes`. |
| 🟡 | sandbox.py:134 | Windows tree-kill via `taskkill /T` races enumerate→kill; `CREATE_NEW_PROCESS_GROUP` is set but never signaled — needs a Job Object. |
| 🟡 | leakage.py:34 | `target_leakage` is Pearson-only → misses non-linear/categorical leaks (the "differentiator" gives false assurance). |

### Task adapters (repo / regression / mlebench / models / config)
| Sev | Loc | Finding |
|---|---|---|
| 🔴 | mlebench.py:105 | In-process grader → label reconstruction via oracle/`_Y` (C1). |
| 🟠 | repo_task.py:305 | `metrics`/`constraints`/`cross_check` reader paths unprotected; `protect` exact-string not glob (C1). |
| 🟠 | models.py:45 | `metric: Optional[float]` accepts NaN/inf; `regression.cv_mse` returns `inf` on degenerate folds → poisons `is_better`. |
| 🟠 | models.py:97 | `RunState.direction` (the field that drives `is_better`) has **no validator**; a typo `"Max"`/`"maximize"` silently inverts the objective for the whole run (only `RepoTask` validates it). |
| 🟠 | regression.py:90 | `cv_k>n`/`k=1` silently averages fewer folds or returns `inf`; no `2 ≤ cv_k ≤ n` validation. |
| 🟡 | models.py:19 | `Idea.params: dict[str,float]` but repo params are free-form → silent coerce/`ValidationError` swallowed in `parse_structured`. |
| 🟡 | repo_task.py:46 | `EvalSpec.metric` is a bare `dict` — a typo'd `kind` ("stdout_jsom") yields no protection + no metric; should be a discriminated union. |
| ⚪ | config.py:14 | `n_seeds`/`max_nodes`/`max_parallel`/`max_seconds` have no lower bound (`ge=1`/`gt=0`) — `max_parallel=0` silently breaks the loop. |
| ⚪ | repo_task.py:128 | `NoOpRepoDeveloper.last_files = {}` mutable class attribute (shared-state footgun). |

### Server + UI (React)
| Sev | Loc | Finding |
|---|---|---|
| 🔴 | server.py:488 / :87 | Arbitrary file read + unauth control-plane + CORS `*` (C3). |
| 🟠 | server.py:234 | `delete_run` uses `shutil.rmtree(..., ignore_errors=True)` and returns `{"ok":True}` even on partial/failed delete (Windows open handle) → ghost/orphan run dirs reported as deleted. |
| 🟠 | server.py:244 | SSE re-folds the **entire** log every 0.4 s **per client** — no cache, no incremental fold, no `Last-Event-ID`; `JSON.parse(e.data)` is unguarded ([hooks.js:18](https://github.com/ArtyomZemlyak/looplab/blob/master/ui/src/hooks.js)). |
| 🟠 | util.js:92 | `layoutWithGroups` cycle "guard" is a no-op → order-dependent depths and `Math.max()`-of-empty `NaN` positions on any malformed `parent_ids`. |
| 🟠 | Dock.jsx:42,87 | **Diff regression:** the working tree renamed the Inspector `Reasoning` tab to `Trace`, but `Dock` still calls `onFocus(…, 'Reasoning', …)` → focusing a node opens a **blank inspector** (no tab matches). One-line fix. |
| 🟠 | hooks.js:39 | `if (Notification && …)` throws `ReferenceError` where the API is absent (insecure/unsupported context) — guard with `'Notification' in window`. |
| 🟡 | Dock.jsx:34 | Re-GETs the full `/log` (+`/trace`) on **every** SSE seq tick → O(n²) bytes over a run. |
| 🟡 | panels.jsx:226 | `RegistryPanel` sorts `runs` **in place** (mutates state) and always descending — ranks `direction:"min"` runs upside-down. |
| 🟡 | panels.jsx:313 / Dock.jsx:83 | `EventExplorer`/feed render the entire unbounded log un-virtualized (re-rendered per filter keystroke). |
| 🟡 | RunView.jsx:50 | Window-level drag listeners not cleaned up on unmount-mid-drag → setState-after-unmount + leaked listener. |
| 🟡 | Inspector.jsx (StageBlock) | A root span with children renders only its children; `llm_call` events recorded directly on a parent span are dropped (regression vs old `collectLlm`). |
| 🟡 | server.py:218 | `rename_run` does unguarded `await request.json()` → unhandled 500 on empty/bad body. |
| ⚪ | charts.jsx:14 | `Trajectory`/`Scatter`/`Bars` pin an all-equal series to the floor (implies "worst") instead of centering; `fmt` renders `Infinity` literally (no `Number.isFinite` guard). |

---

## 3. Test-suite assessment

**Strong** where it counts on the happy/adversarial-overwrite paths; **weak on the keystone
invariants.** Several "security" guarantees are asserted more weakly than their docstrings claim.

| Subsystem | Coverage | Note |
|---|---|---|
| roles / llm / validator | **Strong** | retry/fallback/repair/no-op/surface all asserted |
| repo_task / command_eval | **Strong** | surface gate, eval-protection, repair loop, multi-repo |
| tracing | **Strong** | nesting, trace-id, error-status, determinism |
| projects (UI overlay) | **Strong** | CRUD + cycle-guard + 40-thread concurrent-create race |
| eventstore / replay | **Partial** | torn-line + fold-purity proven; **identical crash-resume & two-writer safety NOT** |
| sandbox / trust | **Partial** | capture/timeout/non-finite good; Docker isolation only argv-mocked |
| mlebench / integrity | **Partial** | forge-by-overwrite excellent; **label-read confidentiality untested** |
| server / UI | **Partial** | API/time-travel/masking covered; SSE assertion loose; skipped without `[ui]` |
| policy / variance gate | **Partial** | basics covered; **ties / empty / degenerate-CV / two-sided SE branch untested** |
| leakage (as a gate) | **Absent** | detectors unit-tested; no test proves the engine *acts* on detected leakage |

Top gaps: (1) crash-resume asserts `finished==True`, not identical-state / no-double-eval;
(2) the interprocess log lock has no test that fails if removed (only a single-threaded alternating
append); (3) metric **confidentiality** (label reading) and selection edge cases are unproven.

---

## 4. Docs-vs-code drift

| Sev | Where | Drift |
|---|---|---|
| 🟠 | README:15 / 03-decisions.md:301 | "secret-leak gate" / "redaction + gitleaks over the event log" — **not implemented** (C2). |
| 🟠 | README (~12×) | `python -m LoopLab.cli` works only on a case-insensitive FS; the package is `looplab`. Broken on Linux/macOS CI. Entry-point casing (`LoopLab = looplab.cli:app`) is inconsistent. |
| 🟡 | README:88 | Documents a `--agent-max-retries` CLI flag that **doesn't exist** (only the env var / `config` field). |
| 🟡 | README:179 vs :215 | Self-contradictory test count ("34 tests" vs "105 pass"); `sandbox.py:7` hard-codes "34". |
| 🟡 | cli.py:152 | `resume` silently falls back to fresh `Settings()` when `config.snapshot.json` is missing — can finish an approval-pending run **without approval**, the very case its own comment warns about. |
| ⚪ | e2e_report.py:5 | Invoked as `python -m tools.e2e_report` but `tools/` isn't a package and isn't shipped. |

**Verified claims that DO hold:** crash-resume mechanism (real `os._exit(137)` + replay), zero-dep
JSONL tracing with optional OTel bridge, `--network none` (real + argv-tested), trust-mode tiering,
patch-gate/validate defaults "on", mlebench label-withholding + asset-name protection (the
read-the-key caveat is **honestly disclosed**).

---

## 5. What's done well (keep)

- **ADR-18 read/control split is real:** the UI only *appends* CONTROL events through the same
  interprocess lock and writes UI-only overlays (`projects.json`/`ui_meta.json`) — never mutates
  `events.jsonl` directly.
- `iter_jsonl` correctly tolerates a torn **final** line; shared by event + span readers.
- `htmlview.py` escapes all interpolated content — **no XSS** in the static view, and no
  `dangerouslySetInnerHTML` anywhere in the live UI.
- Everything executes via **argv, not shell** — no classic shell injection.
- Tracing exporter serializes cross-thread appends under a lock.
- `validate.py`, `repo_task`, tracing, and projects have genuinely strong, adversarial tests.

---

## 6. Prioritized remediation roadmap

1. **Metric integrity (C1)** — host-side scoring, `:ro` input mounts, protect every reader path,
   glob `protect`, out-of-process mlebench grader. *This underpins the engine's entire purpose.*
2. **Server hardening (C3)** — token on mutating `/api/*`, CORS allow-list, SPA resolve-guard,
   loopback bind, `task_file` allow-list. *Cheap; closes RCE + arbitrary-read.*
3. **Secret handling (C2)** — env allow-list to children + output redaction, or correct the docs.
4. **Replay/durability (C4, C5)** — read/enforce `v`, idempotent `fold`, fail-loud lock, atomic
   read-model with a seq watermark; back them with the missing crash-resume & two-writer tests.
5. **Robustness sweep** — wrap `_post`, reuse `_kill_tree`/process-groups in `cli_agent`, Docker
   resource limits, non-finite-metric + `direction` validators, parallel-eval budget guard.
6. **UI correctness** — fix the `Reasoning`→`Trace` tab regression (1 line), `layoutWithGroups`
   cycle guard, `delete_run` `ignore_errors`, SSE/Dock O(n²) refetch, leak/guard cleanups.
7. **Hygiene** — config lower bounds, `errors="replace"` on text reads, docs/CLI/packaging drift,
   selection edge-case tests.

*Items 1–4 are about making the system honor the guarantees it already advertises; 5–7 are
correctness and polish.*

---

## 7. Remediation applied (2026-06-23, consolidation round)

Fixed in this round (correctness + cheap hardening, chosen to preserve all existing functionality):

- **UI regression (C/§Server):** `Dock.jsx` focus tab `Reasoning`→`Trace` (was opening a blank
  inspector); `hooks.js` guards `Notification` with `'Notification' in window` and wraps the SSE
  `JSON.parse` so a torn frame can't throw; `panels.jsx` `RegistryPanel` copies before sort (no
  in-place state mutation).
- **Server hardening (C3, partial):** SPA route `GET /{path:path}` now resolve-guards under the
  built-assets dir (closes the arbitrary-file-read); CORS is an **allow-list** of localhost dev
  origins (override `LOOPLAB_UI_CORS`) instead of `*` (closes cross-origin CSRF); `delete_run` no
  longer reports success on a partial delete.
- **Secret handling (C2, mitigation):** child processes no longer inherit secret-looking env vars
  (`*KEY*/*SECRET*/*TOKEN*/*PASSWORD*/*CREDENTIAL*`), so generated code can't exfiltrate
  `LLM_API_KEY` into the durable stdout tail — while keeping `PATH`/`SYSTEMROOT`/… intact.
- **LLM robustness:** `llm._post` wraps network/HTTP/non-JSON/no-choices failures in a new
  `LLMError` (was a raw `URLError`/`KeyError` aborting the run); `parse_structured` treats it as a
  parse failure so the Researcher's retry+fallback degrades to a safe default.
- **Config/validation:** `Settings` lower bounds (`n_seeds/max_nodes/max_parallel≥1`, `timeout>0`)
  so `max_parallel=0` can't silently stall the loop; `replay.fold` normalizes `direction` to
  `min|max` (a typo like `"Maximize"` no longer inverts the objective).
- **Engine:** `operators.merge_idea` skips non-numeric params (was an infinite resume-loop when a
  free-form repo param wasn't numeric — no `node_created` was written).
- **Hygiene:** `skills.py`/`prompts.py` read prompt/skill files with `errors="replace"` (a
  cp1252/UTF-16 file no longer crashes load).
- **Metric integrity (C1, partial):** `RepoTask._eval_protected` now protects EVERY file-based
  reader path — the primary `metric`, the extra `metrics`, each `constraints` reader, and the
  drift `cross_check` — not just the primary (secondary/constraint/drift values were forgeable).
- **Durability (C4, partial):** `replay.fold` is now idempotent for terminal node events — only a
  node's first `node_evaluated`/`node_failed` contributes its eval time, so a duplicate event can't
  inflate `total_eval_seconds` or make the budget order-dependent.
- **Sandbox (C1, partial):** both Docker paths (`command_eval.make_docker_wrap` +
  `sandbox.DockerSandbox`) add `--pids-limit 1024` — a fork-bomb guard on the untrusted tier.

Locked by `tests/test_review_fixes_3.py` (config bounds, direction, merge guard, secret-env
redaction, LLM-error fallback) + a CORS allow-list test in `tests/test_server.py`. Offline suite
green.

**Consciously deferred** (larger / higher-risk to existing functionality, and not blocking the
stated trusted-local single-operator default — tracked here, not silently dropped): C1 host-side
metric scoring + out-of-process mlebench grader + `:ro` Docker mounts (a read-only mount would
break file-based metric readers, which write into the mount); C3 auth tokens on mutating `/api/*`;
C4/C5 envelope schema-version enforcement + atomic read-model with a seq watermark; full Docker
`--memory`/`--cpus`/`--read-only` (untestable without a Docker host here); `cli_agent`
process-group tree-kill (the live-tested opencode path — deferred to avoid regressing it). These
remain the roadmap above.
