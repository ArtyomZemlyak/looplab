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

> **[2026-08-19 — READ THIS BEFORE WORKING THIS LIST: rank is not status.** Re-derived against the
> tree, FOUR of the nineteen ranked rows are no longer open and the list does not say so at the
> only place a reader looks — the headline. Rows **2** (`redact_output` is `True` since 2026-08-15)
> and **4** (`build_readmodel` stamps a watermark) retract their own headlines twenty-odd lines
> down, so the rank stands over text the body refutes; rows **8** and **10** are simply false and
> carry no amendment at all, both closed on **2026-08-14**, the day this list was written. Rows 12
> and 19 were re-checked and are respectively still open (0 `pareto` hits across `looplab/search/`
> and `looplab/engine/`) and closed (§2's B7 row records the 2026-08-15 fix).
> **This is why the ranked list is no longer the index.** The index is the one greppable key
> `OPEN[<slug>]` / `DECLINED[<slug>]` (CLAUDE.md, "The open-item index"), whose markers carry a
> falsifier `tests/test_open_item_index.py` re-derives from the tree on every suite run — so a row
> that ships cannot stay open for five days again. Only §0.1 row 5 is tagged in this section; the
> rest of §0.1 is deliberately NOT tagged, because a marker whose proof nobody re-derived would be
> the same unverified claim the glyph already was. See CLAUDE.md for what is and is not tagged.]

### §0.1 Ranked

> **THIS LIST IS NOT GUARDED, AND ON 2026-08-21 THAT WAS MEASURED RATHER THAN SUSPECTED.**
>
> `grep -rn 'OPEN\['` IS this repo's backlog: every marker carries a falsifier, closing one is a
> DELETION, and `tests/test_open_item_index.py` fails when a tracked item stops being open, when a
> slug is declared twice, or when a decline carries no `measured:` clause. It caught three separate
> drifts of mine in one day.
>
> **Nine of the nineteen entries below are self-marked closed. Of the ten that are not, ZERO carry
> an `OPEN[…]` marker** — except §0.1 #5, whose marker text records its own falsifier firing falsely
> within an hour and being tightened. Everything else here is prose that nothing re-derives.
>
> Two were checked on 2026-08-21 and neither was what it said:
> * **#19** was FIXED on 2026-08-15 by exactly the symbol the entry names as its own remedy
>   (`unreliable_metric_ids`), and sat open for six days. Verifying it cost one `grep`.
> * **#6** was P0 on the strength of "this box serves local models", which it does not — and the
>   local instrument the repo built for that question reports 33 asks, 0 repaired, 0 failed.
>
> So the failure rate of a spot-check here is 2 of 2. A ranked list that asks for work on the
> strength of a number that stopped being true is the same defect as a record that reports
> parameters a run did not use — the carrier lies about FUTURE work instead of past work, and the
> cost is the next reader's attention rather than GPU-hours.
>
> **What would fix it:** give every still-open entry an `OPEN[<slug>]` with a falsifier the guard can
> re-derive, and let closing be a deletion, as it is everywhere else in this tree. Until then, verify
> an entry against the code before acting on it — start with the symbols the entry itself names.


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
5. **The `agentless` Developer is DECLINED on our own corpus; what stays open is the Strategist's
   ability to ask for a backend at all (P2, S).**
   OPEN[strategist-developer-field] the LLM Strategist cannot propose a `developer` — the field is
   absent from `_StrategyOut` and `_normalize_set_strategy` rejects one, so the switch machinery
   below it has no live producer. proof:absent:"developer"@looplab/serve/control_validation.py

   The proof is the OPERATOR half, and it is the quoted ALLOW-LIST key rather than the bare word:
   `_normalize_set_strategy`'s closed key set is what a settable field must join, so the quoted key
   appearing there is what shipping looks like and nothing else writes it. The bare word was the
   first spelling and it FALSELY fired on 2026-08-19 — an unrelated comment naming
   `_finalize_developer_footprint` satisfied it within the hour. That is the substring-pin failure
   mode this repo already tracks under the slug `review-guard-substring-pin`, reaching the index
   itself: a proof whose literal is a common word is satisfiable by PROSE, so an `absent:` proof
   must name a literal only the IMPLEMENTATION can write. (That slug is NAMED above, not marked —
   writing a second marker for someone else's item is how one item silently becomes two.)
   **[2026-08-14 — the DISAGREEMENT half closed.]** The vocabulary has one home:
   `core/config.py::DEVELOPER_BACKENDS` + `DEVELOPER_BACKEND_ALIASES` (`llm` -> `default`, published
   as `developer_switch_names()`), which `_available_developers` derives from and
   `make_developer_factory` resolves through instead of the bare `"llm"` literal;
   `tests/test_developer_backend_registry.py` guards both directions and AST-scans the tree, so a
   re-introduced `agentless` arm names its own file and line. The dead `if "agentless" in
   ctx.available_developers` branch is REMOVED with a comment saying why (an unreachable `if` is a
   promise the code cannot keep). **What was measured, and is the reason it was a guard and not a
   lint:** an unregistered `developer` is dropped by `validate_strategy` *before*
   `_prepare_strategy_developer` runs, so the `developer_application: {status: "refused", …}`
   receipt — which exists, and fires for a factory refusal — cannot fire for an unknown NAME. Driven
   end-to-end through `_maybe_consult_strategist`: `{"policy": "mcts", "developer": "agentless",
   "rationale": "switch developer to agentless"}` is recorded with the policy applied, the rationale
   VERBATIM, no `developer` field and no receipt of any kind. The history reads as a switch that
   never happened.
   **[2026-08-19 — the BACKEND half is DECLINED, not deferred, and the number is in §0.18.]** The
   row asked for `localize → generate-N → validate` as the DEFAULT repo Developer on the Agentless
   paper's SWE-bench Lite result, and asked for that number to be re-argued against our task shape
   first. It was, over all five real repo runs: 61 % of 41 nodes fail their first eval, but only 8 of
   58 repairs — 0.13 h of 220.2 h of stage time — are visible to any pre-execution check; the median
   repair edits **1** file out of a working set of 5, so there is nothing to localize; 48.6 % of the
   files repairs change are not `.py` and are therefore outside `engine/localize.py` by construction
   (`configs/config.yaml` and `looplab_stages.json` are the two most-edited files in the corpus);
   and the only execution-free validator docs/36 permits scores **683 of 683** authored `.py` files
   valid, separating zero candidates — against **+34 %** of the corpus's entire LLM spend at N=3.
   The permanent decline marker and the full table live in §0.18. What the
   corpus supports INSTEAD is a **budget probe** (74 % of the wasted GPU time is `timeout`/`stalled`/
   `declared_condition`, all of it it/s arithmetic the triage model already does after the fact) —
   single-candidate, no extra generation, and it needs its own measurement before it is built.
   Two of the row's five sub-items are settled by that decline rather than by construction: (a) the
   composing `Developer` object and (b)/(c) its `make_roles` branch + registry entry are refused with
   it. **(d) is FIXED**: `best_of_n` was silently inert on every repo task and billing for it —
   `BestOfNDeveloper` ranks the string `implement()` returns and `LLMRepoDeveloper` returns a
   sentinel, so all N candidates scored -1.0, both LLM tie-breaks were skipped and candidate 0 always
   won after N full builds (7.37M prompt tokens each). `make_roles` now REFUSES that combination
   (`search/best_of_n.py::refuse_unrankable_best_of_n`, a `ConfigRefusal`) rather than silently
   dropping to N=1, gated on the new positive marker `agents/roles.py::LLMDeveloper.answers_with_code`. **(e) is what stays open above**: the LLM
   Strategist's `_StrategyOut` still has no `developer` field and `serve/control_validation.py::
   _normalize_set_strategy` still rejects one, so the only live producer of a `developer` decision is
   a rule-based/custom Strategist — and that gap is about `aider`/`goose`/`opencode`/`llm` just as
   much as it ever was about `agentless`, which is why it keeps its own slug now that the backend is
   declined. Landing it means the field in the structured schema, the copy in `_assemble_strategy`,
   the whitelist in `validate_strategy` (three of the four steps `strategist.py:81-86` names — the
   fourth, `Engine._apply_strategy`, already applies a `developer`), and a mention
   in `_strategist_brief`, which today never tells the model developers are switchable at all.
6. **The schema-aligned parser is a fallback, not the default (P0, S).** `core/parse.py:195
   ::_coerce_to_model` IS a real error-correcting SAP (case-insensitive key match, per-field
   coercion, extras dropped) — but `core/config.py:1483` is `llm_parser: str = "tool_call"` and
   `parse.py:213::_ORDER["tool_call"] = ["tool_call", "baml"]`, so it only runs after native FC has
   already failed. **Cost:** this box serves local models, which is exactly where native FC collapses
   (~20 % vs ~92–94 %). The original note called it the cheapest whole-system lift and that still
   holds — it is now a one-line default change plus its blast-radius test.
   **[MEASURED 2026-08-21 AND THE PREMISE DOES NOT HOLD — DO NOT SHIP THE DEFAULT CHANGE ON IT.**
   The code half of the entry is still exactly true: `Settings.llm_parser` is `"tool_call"` and
   `_ORDER["tool_call"]` is `["tool_call", "baml"]`, so the SAP runs only after native FC fails.
   What is false is the *cost* clause it rests on.
   *(a) THIS BOX DOES NOT SERVE LOCAL MODELS.* `config.snapshot.json` on every current run reads
   `llm_base_url = https://llm-core-olap.samokat.ru/v1`, `llm_model = deepseek-v4-flash` — a remote
   endpoint whose function calling works. The `~20 % vs ~92–94 %` figure describes a configuration
   this box has not been in.
   *(b) THE LOCAL MEASUREMENT SAYS THE OPPOSITE, and the repo already built the instrument for it.*
   `looplab parser-stats` over every run that carries the observation:
   `e5small-dr-unified-v3` — concept_coverage 5, hypothesis_merge 1, propose 9; `e5small-dr-unified-v4`
   — concept_coverage 5, deep_research 1, hypothesis_merge 3, propose 9. **33 asks, first-try 100.0 %
   in every phase, 0 repaired, 0 failed, `won: tool_call` on all of them.** Native FC is not
   collapsing here; it is answering everything on the first try.
   *(c) THE EVIDENCE IS THIN AND SAYS SO.* 33 asks is a small sample, and only two of eight runs
   contribute — but that is NOT an instrumentation defect, which was checked rather than assumed:
   `grep -c structured_parse` returns 0 rows for `rubertlite-dr-unified-v8`, `e5small-dr-unified-v2`
   and `rubertlite-dense-retrieval` (they predate the observation) and 18 for `e5small-dr-unified-v4`,
   which is exactly the 5+1+3+9 the tool reported. The tool sees everything that exists.
   So: the change is a one-line default flip whose entire justification is a rate this box cannot
   currently reproduce. **Re-priced from P0 to P3, open only as a question**: flip it if and when a
   run's own `parser-stats` shows `repaired` or `failed` above zero, which is the number the flip
   was always supposed to be answering. Leaving it at P0 would spend the next reader's attention on
   a lift that has no measured lift behind it.]
7. **Untrusted code still gets a writable container filesystem (P1, M).** `runtime/sandbox.py:230-238`
   has `--pids-limit 1024`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--memory`,
   `--cpus`; `--read-only` + tmpfs appears **nowhere** in the tree, and `sandbox.py:241` mounts
   `-v {root}:/work` with no `:ro`. Windows tree-kill is `taskkill /F /T` (`sandbox.py:1275`), not an
   atomic Job Object. Partially covered by newer, independent rungs — but `Settings.landlock` is OFF
   by default (`runtime/landlock.py`) and `runtime/read_fence.py` only sees `open` inside CPython.
   **[RE-DERIVED 2026-08-21 — TWO OF THESE CLAIMS ARE NOW FALSE, and a third was DECIDED rather
   than forgotten.**
   *(a) `--read-only` + tmpfs SHIPPED.* Eight occurrences of `--read-only` and five of `tmpfs` in
   `runtime/sandbox.py`, with `READONLY_SCRATCH_DIRS` enumerating the writable scratch in one place
   and `Settings.sandbox_readonly_rootfs` gating it. "Appears nowhere in the tree" is no longer a
   statement about this tree.
   *(b) `-v {root}:/work` WITHOUT `:ro` IS A DECISION,* and the code answers this entry directly:
   "`/work` IS the node's workdir on the host … a `:ro` here does not harden anything, it deletes the
   tier's output channel. The per-source `edit:false` enforcement that DOES exist is
   `make_docker_wrap(binds=…)`'s `--mount …,readonly`, one mount per declared data/reference source,
   which is the right granularity." Asking for `:ro` on `/work` is asking to delete the metric file
   the engine reads back.
   *(c) WHAT IS STILL LIVE is the narrow half:* the host-side fence ships opt-in
   (`core/config.py`'s `landlock` default) and `runtime/read_fence.py` still only sees `open` inside
   CPython — the schema row's own help says why, and names the retirement condition ("nobody has run
   a ruleset through a real GPU eval").
   *IT NOW CARRIES A MARKER, and getting there was the actual work.* Every other marker in
   this tree is `absent:<symbol>` over an identifier that WOULD EXIST once the item ships — the
   index tracks a MISSING CAPABILITY, and this item is a wrong DEFAULT, where the capability
   already exists. The first marker written for it was REJECTED by the guard as malformed:
   `proof:` read only a whitespace-free literal, so `landlock: str = "off"` was inexpressible, and
   the sole whitespace-free spelling in the tree sits in a COMMENT that `satisfied_only_by_prose`
   rejects. Rather than force one — which would have shipped the vacuous guard this repo found nine
   times in a day — the SCANNER was fixed: `looplab/core/claimpin.py::PROOF` is now the twin of
   `DECIDED`, both indexes read one grammar, and `line:` plus the backtick-quoted form are
   admissible.
   *THE FIRST FALSIFIER WRITTEN WITH THAT NEW GRAMMAR WAS VACUOUS, and a mutation caught it.*
   `line:landlock&&"off"@core/config.py` reads True even with the default flipped to `"on"`, because
   `config.py:2201` carries `("landlock", ("off", "enforce"))` — the ALLOWED-VALUES tuple, which
   names both literals on one line and never changes. That is the exact defect `line:` was invented
   to fix ("this string occurs" vs "this string is SAID ABOUT that subject") recurring one level up:
   binding two literals to one LINE does not help when a DIFFERENT line legitimately carries both.
   The shipped falsifier is the whole assignment, which only the default line can satisfy — verified
   by flipping the default and watching it go False.
   OPEN[landlock-is-opt-in-by-default] an untrusted eval gets the host-side filesystem fence only
   when an operator asks for it, so the container rungs above carry the default alone; retire this
   when a ruleset has been through a real GPU eval and the default flips.
   proof:`present:landlock: str = "off"@looplab/core/config.py`
   **The re-derivation is the finding, not the fix.** Three ranked entries were checked against the
   tree on 2026-08-21 and none described it — #19 fixed six days earlier by the very symbol it
   names, #6 standing on a rate this box cannot reproduce, and this one with two claims false. See
   the note above §0.1.]
8. **Nothing tests the append lock across real OS processes (P1, S).** Both other C4 halves landed —
   `Event.v` is enforced (`events/eventstore.py:166-168`, `UnsupportedEventVersionError`) and the
   append lock fails loud (`_interprocess_lock`, `eventstore.py:254-321`, raising
   `EventStoreLockError`). What is still missing is the test the row asked for: the suite has source/
   AST parity checks (`tests/test_append_critical_section_parity.py`) and monkeypatched-failure
   simulations, but **no concurrent multi-process append race**. **Cost:** the durability guarantee
   the whole replay design rests on is held by inspection.
   **[2026-08-19 — STALE; the row is FALSE on master and was never amended.**
   `tests/test_append_multiprocess_race.py` — "Two REAL OS processes appending to one
   `events.jsonl`" — landed in `c474d069` on **2026-08-14**, the same day this row was written, and
   nothing connected the two. This is the B7 shape (recorded open 2026-08-14, fixed 2026-08-15,
   still open on 2026-08-19) repeating in the same section, which is the measurement the open-item
   index in CLAUDE.md was designed from. No marker is added here: this row is CLOSED.]
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
   the shared derivation makes visible.
   **[MEASURED 2026-08-21 — real in code, and it has never once fired.** The four call sites are
   `evaluate.py` at the eval, the log plan, the repair and the salvage, each re-entering
   `_resolve_stages`, whose `record_stages_over_time_budget(..., at="resolve")` has no dedupe — so
   the shape is exactly as described. The cost is **zero**: across every `spans.jsonl` under `runs/`
   — **44 files, 99,644 span rows — ZERO are named `stage_timeout_over_budget`**. (Two grep hits
   exist and both are false: the string occurs inside a tool's OUTPUT and inside a generation's
   INPUT, i.e. in source an agent happened to read. Counting by span NAME is the only honest count.)
   **AND THE FENCE IS NOT ONLY A READING BOUND — driven 2026-08-22.** Pointing
   `backfill-score-metrics --apply` at the corpus made the store REFUSE the append on that run:
   `EventLogCorruptionError … 1603 later record(s) are DROPPED on replay. Refusing to append.` The
   pass stopped there and the three runs after it alphabetically were never processed until they
   were re-run with `--only`. So a gapped run is INERT, not merely truncated to a prefix — and the
   remedy the store names, `looplab repair-log`, truncates 1,603 records, which makes it an
   operator's decision and not a maintenance step. Every earlier sentence here that says the store
   "serves 20 of 1,624 lines" is true and understates it.
   *The denominator was wrong the first time and is corrected here.* The first pass globbed
   `runs/*/events.jsonl` and `runs/*/spans.jsonl` — 8 files, 94,197 rows — while the corpus is every
   `*.jsonl` under `runs/`: **131 files across 15 run directories**, seven of which keep no top-level
   `events.jsonl` at all. The conclusion survived the correction; it was not entitled to.
   **NO MARKER, deliberately, and the rule is the reason:** "an item without a re-derivable
   falsifier must NOT be tagged". Every candidate here fails one way or the other — a pin on the
   emitter's presence stays true after a dedupe lands INSIDE the emitter (a marker that can never
   go green), and a pin on a dedupe symbol names a mechanism that may arrive under any other name,
   which is the mechanism-not-property shape §0.8 found nine times. The index is not improved by a
   tag that cannot be checked; the measurement is what makes this row honest.]
10. **Cross-run aggregation is a list, not an overlay (P1, M).** `ui/src/panels.jsx:2319
    ::CrossRunPanel` renders per-run metric observations and explicitly disclaims the thing the row
    asked for: *"Cross-run ranking unavailable… Values below remain per-run observations"*
    (`panels.jsx:2340-2343`). `serve/routers/cross_run.py` is the governance/claims surface, not this.
    **[2026-08-19 — STALE, and the second same-day case in this list.** `ui/src/crossRunRank.js`
    ("rank inside a comparable group, never across the corpus", `4fa0d1ee`) landed **2026-08-14**
    and `panels.jsx::CrossRunPanel` now ranks inside one `(task_id, direction)` partition —
    CLAUDE.md's `ui/` row has carried the measurement (36 of 45 runs with a metric, 20 groups, 5 of
    them real) since it shipped. The surviving "Cross-run ranking unavailable" string is the
    group-of-one caveat, not the disclaimer this row quotes. No marker is added: this row is
    CLOSED. What is genuinely left is named IN CODE as `TRAJECTORY_GAP`, not here.]
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
    **[2026-08-19 — the READ half landed too, and it was not a detail: the receipt was written,
    stamped and folded, and then no surface anywhere showed it back.** `ui/src/forkProvenance.js`
    plus the `authored_fields`/`not_carried_fields` split on the receipt and the operator agent in
    the `/prov` export. See the Theme F row for the three findings and the two `OPEN[…]` markers
    they closed.]
12. **Pareto selection is display-only (P2, M).** The real non-dominated algorithm exists —
    `ui/src/panels.jsx:721::paretoFront` with `dominates()` at `:725`, over the primary metric plus
    every `extra_metric` — but grep for `pareto` across `looplab/search/` and `looplab/engine/`
    returns **nothing**. It never reaches champion selection.
   **[RE-DERIVED 2026-08-21 — the SUBSTANCE holds, one detail drifted.** `pareto` now appears twice
   in `engine/holdout.py`, but both are COMMENTS ("feeds the node's Pareto objectives", "Pareto
   objective") — no algorithm, no selection. `ui/src/panels.jsx` still carries the real
   `paretoFront`. So the entry's claim is right and its "returns nothing" is now literally false;
   the fix is what a marker is for.
   OPEN[pareto-never-reaches-champion-selection] the non-dominated front is computed in the BROWSER
   and nothing in the search or the engine consumes it, so a run still elects one champion on one
   scalar; retire this when a front is computed where selection can read it.
   proof:absent:pareto_front@looplab/search/policy.py
   *Mutated before it was written:* True as shipped, False the moment a `pareto_front` lands in the
   policy.]
13. **The feature-engineering CV gate is a sentence, not an enforcement (P1, M).**
    `engine/proposal_cues.py:231::_cue_feature_engineering` appends prose telling the model
    *"KEEP a feature only if it improves CV"*, gated by `core/config.py:643::feature_engineering =
    False`. There is no FE operator in `search/operators.py` and no `caafe` symbol anywhere. The row
    called the CV gate **mandatory**; an instruction to a model is not a gate.
   **[RE-DERIVED 2026-08-21 — ALL FOUR CLAIMS HOLD, the first ranked entry checked that day that
   still described the tree.** `_cue_feature_engineering` returns a STRING and nothing else — the
   "gate" is the sentence "KEEP a feature only if it improves CV" inside it; `feature_engineering`
   still defaults to `False`; `search/operators.py` still has no FE operator; and the only `caafe`
   in the tree is the words "(CAAFE-style)" in a `core/config.py` COMMENT, which is prose and not a
   symbol, so that claim stands too.
   OPEN[fe-cv-gate-is-prose-not-enforcement] the eval never drops an engineered feature that fails
   CV — the only thing that says so is a sentence in the proposer's prompt, and there is no
   feature-engineering operator to enforce it; retire this when one exists.
   proof:absent:feature_engineering@looplab/search/operators.py
   *The falsifier was mutated before it was written:* True as shipped, and False the moment a
   `feature_engineering` operator lands in that file — checked, because the previous marker written
   that day was vacuous and only a mutation found it.]
14. **The time-series adapter is a synthetic toy; tabular-AutoML and multimodal do not exist (P1, M
    each).** `adapters/timeseries.py`'s own docstring (line 9) says a real AutoGluon-TS/Darts backend
    "is a drop-in replacement for the templated forecaster" — i.e. it is the template, not the
    backend. `adapters/` holds classification / dataset / mlebench{,_real} / regression / repo /
    timeseries / toytask and nothing else.
    **[RE-DERIVED 2026-08-21 — the SUBSTANCE holds, the enumeration drifted.** No `autogluon`,
    `darts` or `sktime` appears anywhere under `looplab/`, so the templated forecaster IS still the
    forecaster; there is no tabular-AutoML adapter (only tabular PROFILING in
    `adapters/dataset_task.py`) and no multimodal one. But "and nothing else" is now false —
    `adapters/` also holds kaggle_dl, mlebench_grade, mlebench_prep, repo_developer,
    repo_write_tools and tasks. Only the timeseries half gets a marker: the missing-adapter half has
    no falsifier that isn't a filename guess, and a proof that names a FILE THAT MIGHT ARRIVE UNDER
    ANOTHER NAME is the mechanism-not-property shape this file was corrected for nine times.
    OPEN[timeseries-adapter-embeds-its-own-forecaster] the adapter generates its own exponential
    forecaster inline, so the task validates LoopLab's plumbing rather than any forecasting
    capability; retire this when a real backend is imported.
    proof:`absent:import autogluon@looplab/adapters/timeseries.py`
    *Mutated before it was written:* True as shipped, False the moment that import lands.]
15. **Drift detection is absent (P2, M).** `trust/leakage.py` DID go past exact-match —
    `code_leakage_scan` (`:147`, self-described "static-dataflow-lite": preprocessor fit on full data
    before the split, `.fit()` on test data), plus `target_leakage` and `temporal_leakage`. But every
    `drift` hit in `looplab/` is code/schema-drift prose or confirm-phase seed variance
    (`engine/confirm_phase.py:273`), never a distribution-shift detector.
   **[RE-DERIVED 2026-08-21 — HOLDS.** No population-stability index, no KS test, no
   `distribution_shift`/`drift_detect` symbol anywhere under `looplab/`.
   OPEN[no-distribution-shift-detector] nothing compares the deployment distribution against the
   training one, so a run cannot tell a shifted input from a worse model; retire this when a
   detector exists.
   proof:missing:looplab/trust/drift.py
   *Mutated before it was written:* True as shipped, False the moment that module exists.]
16. **MLflow is manual export, not autolog; there are no data connectors (P2, S–M).**
    `events/mlflow_export.py::export_run` + `cli/export_cmds.py:93` ship a per-run push; grep for
    `autolog` across `looplab/` is **empty**, and there is no `DataConnector`/`connector` symbol.
    (Notebook export DID ship: `events/notebook.py::champion_notebook`, `export_cmds.py:108`.)
    **[RE-DERIVED 2026-08-21 — the SUBSTANCE holds; the entry's own grep claim is literally false,
    and finding out why was the point.** `export_run` and `champion_notebook` are where the entry
    says (the CLI line moved to `export_cmds.py:101`/`:114`), and there is no connector class:
    `DataConnector` / `class *Connector` is 0 hits. But "grep for `autolog` across `looplab/` is
    empty" is WRONG — there is exactly one hit, `core/config.py:218`, inside the word **tau·tolog·y**.
    Nothing is broken by that; what would have been broken is the obvious falsifier. `absent:autolog`
    reads FALSE as shipped, which the guard would have reported as an item already fixed — an
    open item closed by an English word. The pin is bound to the CALL instead.
    OPEN[mlflow-is-export-not-autolog] MLflow receives a run only when a human runs the export
    command, so nothing is tracked while a run is in flight; retire this when autologging is wired.
    proof:`absent:mlflow.autolog@looplab/events/mlflow_export.py`
    *Mutated before it was written:* True as shipped, False the moment that call lands.]
17. **The MCTS tree has no LLM value estimate and no reflection (P2, M).**
    `search/policy.py:393::MCTSPolicy` is classic UCB1 (`:475-478`) with reward folded straight from
    the metric (`_mcts_reward`, `:374`). No `lats.py`, no LLM valuation, and it is not wired to
    `search/graded_novelty.py` / `novelty_recall.py` / `taxonomy_dedup.py`, which exist independently.
   **[RE-DERIVED 2026-08-21 — HOLDS.** `MCTSPolicy` is still the one class, `search/policy.py`
   imports no `graded_novelty`, and there is still no `lats.py`.
   OPEN[mcts-has-no-llm-value-estimate] the tree values a node by its metric alone, so an unexplored
   branch nobody has evaluated is indistinguishable from a bad one; retire this when a value
   estimate exists.
   proof:missing:looplab/search/lats.py
   *Mutated before it was written:* True as shipped, False the moment that module exists.]
18. **Parallel eval is in-process only (P2, L).** `engine/evaluate.py:1375` takes an
    `anyio.CapacityLimiter` and `orchestrator.py:1503,2383` open task groups; there is no `ray`,
    `celery` or `dask` anywhere and no cross-machine dispatch. The budget-guard half of the row DID
    ship (`engine/widths.py::EVAL_WIDTH_MAX` enforced at `orchestrator.py:2966`;
    `engine/proposal_cues.py:425::per_experiment_gpu_budget`).
    **[RE-DERIVED 2026-08-21 — HOLDS, and every line citation in it is now wrong.** `ray`, `celery`
    and `dask` are 0 hits across `looplab/`, so eval parallelism is still one process's task group.
    The corrections, since this entry is the reason the house style forbids `file.py:NNN`: the
    limiter is `evaluate.py::LoopEvaluator._evaluate`, not `:1375`; the task groups are around
    `orchestrator.py:4412`/`:4539`, not `:1503,2383`; `EVAL_WIDTH_MAX` is enforced at
    `orchestrator.py:2885`, not `:2966`; and `per_experiment_gpu_budget` is DEFINED in
    `engine/widths.py`, merely imported by `proposal_cues.py`. Four dead citations in one entry, the
    exact rot `claimpin.LINE_CITATION` was written to refuse.
    OPEN[eval-parallelism-is-in-process-only] evals are bounded by one box's task group, so the
    second H200 is the ceiling and a queued node waits rather than dispatching; retire this when a
    cross-machine dispatcher exists.
    proof:`absent:import ray@looplab/engine/evaluate.py`
    *Mutated before it was written:* True as shipped, False the moment that import lands.]
19. ~~**[added 2026-08-14] Claim ratification ignores node feasibility and trust flags (P1, S).**~~
    **[FIXED 2026-08-15, VERIFIED 2026-08-21 — this entry outlived its defect by six days.**
    The entry prescribed the fix by name: "reuse that exact join — `engine/metric_salvage.py::
    metric_unmeasured` ∪ `events/replay.py::flagged_node_ids` — and refuse/downgrade a `supported`
    verdict whose cited node is in the set". `trust/memo_verify.py::finalize_verified_evidence` now
    imports and calls `engine/memory.py::unreliable_metric_ids` (the function that IS that join) and
    refuses on it, with a docstring that states the leak in the entry's own terms: "none asked
    whether this run trusts its number".
    It also fails closed in the direction the entry did not ask for and should have: an unreadable
    state answers "verification could not establish whether this run trusts the cited node(s)"
    rather than ratifying — the OPPOSITE containment from the predicate's own, whose bare `except`
    would return the empty set and thereby answer "everything is reliable".
    Driven by `tests/test_memo_verify_evidence_trust.py`, `tests/test_research_claim_finalize.py`
    and `tests/test_champion_metric_caveats.py`.
    **WHY IT SAT HERE FOR SIX DAYS, which is the more useful finding.** This entry carried no
    `OPEN[…]` marker, so the open-item guard — the mechanism that DOES notice when a tracked item
    stops being open, and that caught three of my own drifts on 2026-08-21 alone — could not see it.
    Ten of the nineteen ranked entries are in that state. Verifying one took a single `grep` for the
    symbol the entry itself names; nothing was doing that grep. See the note under §0.1 for what
    that costs and what it would take to fix.]
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
  **REVISITED AND SHIPPED 2026-08-18 as §0.14, with that hazard answered rather than accepted**: the
  narrowing compares the two manifests the ENGINE committed (`node.files` off the fold, never the copy
  on disk the eval can rewrite), per stage ENTRY and only STRICTLY AFTER the reuse point, so an edit
  to the reused stage's own entry — or its order, its name, or a stage inserted/removed in front of it
  — still forfeits. The agent gains no new sentence; it can only say "I changed a later stage", which
  is true. Measured yield: 2 rows across two runs, 5,966 s, 0 wrong where the corpus can tell.
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
  is a change on the reuse decision and wants its own measurement and its own entry. **That entry is
  §0.14 (2026-08-18)** and it answers this HALF-way, which is the honest description: raising the
  ceiling of the stage that FAILED is now free, because the clause compares stage ENTRIES and only
  before the reuse point — raising an EARLIER stage's still forfeits it, and the `timeout`-only carve-
  out is deliberately left closed there. (d) Nothing
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
| `developer_backend = llm \| agentless \| <agent>` | `agentless` is not in `core/config.py::DEVELOPER_BACKENDS` and no backend implements it — and since 2026-08-19 none ever will: §0.18 declines it on our own corpus. `llm` IS real, but as a live-SWAP alias of `default` (`core/config.py::DEVELOPER_BACKEND_ALIASES`), never a launch value. The `agents/strategist.py` branch that could never fire was removed 2026-08-14 |
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
A0e, A0f** (never in ★Shipped), **C5 agentless** (declined 2026-08-19, §0.18) and **C6 ACI** (★Shipped explicitly parked C6 as
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

### §0.6b The metric had no INPUT side, so nothing could refuse a cross-corpus comparison (2026-08-20)

✅ **What it was.** `runs/` holds recall@100 values of **0.8776, 0.793426, 0.792082 and 0.774207**,
compared out loud for a day. Some were measured on one test set and some on another; the product
index also changed, *independently of the test set*, and a bigger corpus makes recall@100 strictly
harder. **Nothing in any record said which, and no surface refused.**

**MEASURED, and each finding is a separate hole.**

1. **`node_evaluated.metric_provenance` binds only the OUTPUT side.** `subjects[].path`, `identity`
   (`file_identity`), `size`, `digest` — what the number is a claim *about*. There was no field of
   any kind for what it was measured *against*.
2. **`data_provenance` fires ZERO times.** 0 occurrences across all eight run directories with an
   event log (4,691 + 2,309 + 133 + 1,624 + 2,539 + 2,456 + 6,415 + 3,330 records). Gated on
   `if prov:` over `self._assets` (`engine/orchestrator.py:3229-3236`) while
   `adapters/repo_task.py:1261-1262::assets()` returns `{}` for every repo task. Its §Theme D row is
   **retracted** above.
3. **`workspace_fingerprint` is not a fallback.** It records a git HEAD SHA for an editable repo and
   a `(relpath, size, mtime)` signature for a `data:` mount — never content — and the three
   dense-retrieval runs declare `data: {}`. Their whole input record is
   `{"editable:.": "git:d97be313…"}`.
4. **`core/comparison.py::ComparisonContract` is the right vocabulary and is declared by 0 tasks.**
   13 typed facets including `dataset_lineage`; optional; **0** of the task snapshots under `runs/`
   set it. It is also honest about its ceiling in its own words — `"authority": "declared"`,
   *"equality proves equality of adapter-declared semantics, not an independent fingerprint of the
   actual dataset/evaluator/budget"*.
5. **`engine/eval_contract.py` calls the incomparable pair identical.** `e5small-dr-unified-v2` and
   `-v4` have byte-identical contracts by its rule — same command `python -m vectorsearch.test`, same
   reader `RECALL@100: ([0-9.]+)`, same editable path — and are exactly the pair that cannot be
   compared. It is also wired only into the foreign-run *reading* tools and reaches no ranking.
6. **The two data files carry no identity at all.** Derived from the eval path in
   `/home/jovyan/data/vectorizer-unified`: `test.parquet` (62,920,840 B) holds only `ARROW:schema` in
   its parquet key-value metadata and a constant `split` column; `smkt_all.index.parquet`
   (37,785,295 B, **641,261 rows**) holds no manifest, no checksum, no row-count assertion. Both are
   **identified by path alone**, so replacing either in place is undetectable from every artefact the
   eval writes.

**EVERY FACTOR THAT MOVES THE FINAL METRIC WITHOUT THE MODEL CHANGING**, derived from that eval path,
with where it is decided and whether a finished run can recover it. Config keys are in
`vectorsearch/configs/config.yaml`, which — because pydantic-settings takes the FIRST source and YAML
precedes env and CLI — **overrides both**, so `--test.retriever.max_len_doc=512` is silently ignored.

| # | factor | decided | recoverable from a finished run? |
|---|---|---|---|
| A1 | test-set **version** | `config.yaml:195 test.test_dataset_version` (= `"2"`) | ✅ the eval writes the whole config to `…/tests/final/config.yaml` |
| A2 | test-set **root** (S3 vs local) | env `VS_LOCAL_DATA_ROOT` (`config.py:64`, read via `os.environ`) | 🟡 the eval logs it for the INDEX only (`evaluator.py:129`) and never for the test split — but LoopLab's own `config.snapshot.json → settings.eval_env` records it, and it is **not constant across the corpus**: `null` on `rubertlite-dr-unified-v6` (⇒ S3) against `/home/jovyan/data/dr-local` on v8/v9 and e5small-v2/v3/v4 |
| A3/A4 | resolved test file, and any version marker **inside** it | `evaluator.py:69-70` | ❌ path not logged; the file carries no marker |
| A5/A6 | test-set size (7,614,946 rows; ~368,842 scored queries) | property of the file | ❌ only the dropped-row count is logged |
| A7 | query **subsampling / seed** | — | **refuted**: the whole split is evaluated, no seed anywhere in the eval path |
| B1/B2 | index **type** / **version** (`all`, `2`) | `config.yaml:197-198` | ✅ config + `evaluator.py:128` |
| B3 | index **path** | `evaluator.py:125-126` + `VS_LOCAL_DATA_ROOT` | ✅ logged verbatim at `evaluator.py:129` |
| B4 | **corpus size** — 641,261 items; the factor that makes recall@100 harder | property of the file | ✅ logged at `evaluator.py:153` |
| B6 | index digest / manifest | — | **refuted**: none exists |
| B8 | what is embedded per doc (`passage: {item_name} + 4 category levels`) | `config.yaml:49-54` | ✅ config |
| B10 | doc **id assignment** = positional row order of the parquet | `evaluator.py:142-144` | ❌ row order is load-bearing and unrecorded |
| C1/C2 | **`query:` / `passage:` prefixes** — both confirmed present | `config.yaml:48-54` | ✅ config |
| C5 | multitask class prefixes (off; if on, the metric becomes a **mean of per-class recalls**) | `config.yaml:26-38` | ✅ config |
| D2 | inference **precision** = **fp16** | `config.yaml:217` | ✅ config |
| D5/D6 | **truncation** — `max_len_doc: 256`, `max_len_query: 64` | `config.yaml:229-230` | ✅ config |
| D8 | corpus encode **batch size** — no `batch_size` passed, so ST's default **32** for the 641k pass | `retriever.py:85-87` | ❌ hardcoded omission |
| D11 | **query order nondeterminism** — polars `.unique()` does not maintain order | `evaluator.py:344` | ❌ changes batch padding run to run |
| D12 | **pooling** = CLS | `1_Pooling/config.json` in the model dir | ✅ (an artefact) |
| D13 | **L2 normalisation** — from the `2_Normalize` module in `modules.json`, never from `encode(normalize_embeddings=…)`, so an agent deleting that module silently changes the metric | model dir | ✅ `final/modules.json` |
| E2/E3 | **FAISS index type** `Flat_GPU` (exact `IndexFlatIP`) and **inner product** as the similarity | `config.yaml:222`, `index.py:34` | ✅ config |
| E4 | torch-GPU vs faiss-CPU search backend (recall@1000 differs by 3.5e-7, just under the harness `drift_tolerance` of 1e-6) | `exact_search.py:234-269`, silent OOM downgrade | log line only |
| E5/E6 | search dtype float64; faiss tie-order convention | `exact_search.py:82,87-102` | ❌ code only |
| F1/F2 | **judged-positive definition** — label column `qwen_72_v1`, positive set `"esc"` (exact **+ substitute + complement**; only `i` excluded) | `config.yaml:44,56` | ✅ config |
| F3 | **match key = `item_name` string equality**, not `item_guid` | `evaluator.py:160-164` | ✅ config |
| F5 | **recall denominator** counts positives absent from the corpus against you | `evaluator.py:166-177` | ❌ code only |
| F7 | macro average over queries; queries with no positives skipped | `evaluator.py:376,439` | ❌ code only |
| G1/G2 | **k** = 100 from `recall_top_k`; `max_k`=1000 retrieved | `config.yaml:201` | ✅ config |
| G8 | harness-side regex + `drift_tolerance` | the task snapshot | ✅ |

**Delivered.** `looplab/runtime/metric_inputs.py` (`eval.inputs` bound to content identity at the
metric read, through `metric_subject.bind_one` with the two policies INVERTED — no confinement, no
freshness floor, because an input is by definition foreign, shared and old) and
`looplab/engine/comparability.py` (the key, its three authorities, and the tri-state). Wired:
`engine/eval_dispatch.py` binds, `engine/evaluate.py` folds `metric_provenance.eval_inputs` +
`.comparability`, `engine/champion_caveats.py` gains `mixed_comparability`,
`serve/run_projections.py` publishes `best_metric_comparability`, `ui/src/runIndex.js::
metricComparable` refuses a proven cross-key set, `ui/src/crossRunRank.js` sub-partitions each
`(task_id, direction)` group by key, `ui/src/panels.jsx` banners a split Pareto front,
`engine/lessons.py::store_case` + `engine/memory.py::JsonlCaseLibrary` elect a cross-run champion
within one key only, and `looplab comparability` exits 3 on DIFFERENT / 4 on UNKNOWN.

**THE INVERSION IS THE FEATURE.** An absent key reads `unknown`, and `unknown` vs `unknown` is
`unknown` — never `same`. Two rows that recorded nothing have not agreed. Equality at the `inferred`
authority is also `unknown`: a weak authority may REFUSE a comparison and may never CERTIFY one,
which is the only rule that separates v2 from v4. Everything fails open on `unknown` and makes it
VISIBLE instead — the corpus does not move by one row, verified by
`tests/test_metric_comparability.py::test_the_existing_case_store_elects_exactly_as_it_did` and
`ui/test/comparabilityGate.test.js::'the corpus as it stands today does not move by one row'`.

⬜ **What is NOT recoverable, and is therefore `unknown` forever for the runs already on disk.**
No key can be retro-fitted to `runs/e5small-dr-unified-v{2,3,4}`, `rubertlite-dr-unified-v{6,7,8,9}`
or `rubertlite-dense-retrieval`. Their logs record no digest of `test.parquet` or
`smkt_all.index.parquet`; those files carry no internal version marker; and both are reachable at one
path with different contents at different times. The per-node `…/tests/final/config.yaml` preserved
under `runs/<run>/nodes/node_N/` *does* keep the config **keys** (A1, B1, B2, C1-C5, D1-D6, D9, E1,
E2, F1-F3, G1-G4), so the DECLARED half is partially reconstructible by hand — but a declaration is
`inferred` authority at best and may not certify, and the two things that could actually have
differed between those numbers (the bytes of the test set and the bytes of the index) are gone.
**The honest answer is `unknown`, and that is what every surface now prints for them.** Retro-fitting
a guessed key would be the one failure this whole mechanism exists to prevent.

**And one difference IS already visible in the record, which is the shape of what was being missed.**
`config.snapshot.json → settings.eval_env` reads `{"VS_LOCAL_DATA_ROOT": "/home/jovyan/data/dr-local"}`
on `e5small-dr-unified-v{2,3,4}` and `rubertlite-dr-unified-v{8,9}`, and **`null` on
`rubertlite-dr-unified-v6`** — and `vectorsearch/config.py:53-69::get_dataset_path` branches on
exactly that variable: set, it reads `…/v{ver}/{split}.parquet` from the local disk; unset, it reads
`s3:/{datasets.path}/v{ver}/{split}.parquet` from **S3**. So v6's champion (0.727991) was scored
against a different storage path from v8's (0.762048), the fact was sitting in a snapshot field
nobody joins on, and no surface refused the comparison. The mtimes are *not* the evidence here and
should not be quoted as if they were — `smkt_all.index.parquet` (2026-08-11 13:15) and `test.parquet`
(2026-08-11 08:53) both predate all five unified runs, and a geesefs mtime is not a content claim
either way. That is the point: **a path and a timestamp are what the record has, and neither is an
identity.**

⬜ **Still open, in priority order.**
1. **Nothing yet DECLARES `eval.inputs`.** The field, the binding and every refusal are shipped and
   the mechanism is therefore INERT on this box until a task sets it — exactly the state
   `eval.metric.subject` was in for two days. It is one line in the task file
   (`["/home/jovyan/data/dr-local/v2/test.parquet", "/home/jovyan/data/dr-local/v2/smkt_all.index.parquet"]`)
   and it is deliberately NOT written into any task goal: the record must make incomparability
   VISIBLE, which is a different thing from telling an agent what to use. Effort S.
2. **The `protocol` half has no measured authority.** `k`, normalisation, the prefixes, the positive
   label set, the pooling mode, the recall denominator — all of them move the metric and all of them
   live inside the candidate's repo, where a reader would be deciding comparability from bytes the
   candidate controls. Today they reach the key only through a `comparison_contract` the operator
   writes (authority `declared`). A sanctioned route for the eval to REPORT its own protocol, signed
   in a way the candidate cannot forge, is the next rung and is not obviously affordable. Effort L.
3. **The cross-run STORE rows still carry no key** except `cases.jsonl`. Lessons, research claims,
   capsules and meta-notes (132 rows on the live store, 0 of which carry any contract identity —
   §0.6) are written by other paths; `store_case` was stamped because it is the one whose *election*
   is a metric comparison. Effort S each, and each must ship with a fail-OPEN reader.

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
  OPEN[deletion-identity-leaked-before-refusal] proof:present:save_deletion_identity@looplab/serve/routers/org.py
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

~~**STILL OPEN.** ⬜ **The UI's own remedy for this state does not work on a naturally-finished run.**~~
**CONFIRMED AND FIXED 2026-08-17 (second pass).** The filing re-derived and held, with three
corrections and one measurement it did not have.

`ui/src/runIndex.js` diagnoses exactly this shape as `finalization-stalled` (*"Finalization stopped
before wrap-up completed"*) and offered a **“Reattach finalization”** button — which maps through
`Dock.jsx::TRANSPORT_INTENTS` to a durable `run_abort` command, and `_decide_run_abort` answers
`attach` while `submit`'s attach arm requires a matching `run_abort` ALREADY in the log
(`_pending_finalize_intent`). `live-deps4-0804` finished *naturally*
(`stop_reason: no_eligible_candidate`, no `stop_requested`), so there is none and the record is
rejected `command_intent_missing` — **driven, not inferred**:
`tests/test_stalled_finalization_affordance.py` builds the shape, POSTs the exact UI payload
(`{"type": "run_abort", "data": {"reason": "finalized"}}`) at the real `/commands` endpoint and reads
`rejected / command_intent_missing / retryable false`, with a spawn recorder proving nothing was
launched. **No other client path reaches it either**: `_decide_resume` REJECTS `finalize_in_progress`
while a finalize is pending, so the Resume control is refused too, and the legacy `POST /control`
refuses through `reject_if_active` for the same reason. `POST /api/runs/{id}/resume` really is the
only endpoint that acts (`allow_incomplete_finalize=True`), and `ui/src/` still contains no `/resume`
URL.

**THREE CORRECTIONS to the filing.** (1) The surface is not "the run card": the empty-canvas card
only renders with ZERO active nodes and `live-deps4-0804` had **3**
(`docs/audit/2026-08-07-search-loop.md`), so the button the operator saw is **Dock's transport row**
(`Dock.jsx`, mode `finalization-stalled`), which renders at any node count — which is also the line
`docs/audit/2026-08-07-operator-surfaces.md` cited when it listed this state under *"What is fine …
the UI reaches the same command"*. That bullet was read, not driven, and now carries a correction.
Both surfaces are fixed. (2) `POST /api/runs/{id}/resume` is a weaker remedy than "the server-side remedy
exists" suggests: `_append_resume_request` classifies mode from `stop_requested and last_stop >
last_finish`, which is FALSE for a naturally-finished run, so it spawns `looplab resume` and not
`looplab finalize`. That happens to complete the wrap-up (`classify_prior_run` → `finalization_pending`
→ `wrap_up_only`), but the verdict is re-derived in a second process at a second instant, and
`run_cmds.py::resume`'s own comment records that race being MEASURED: a `finalize` finishing while a
`resume` waited for the singleton turned a warning into a lift and burned the remaining budget as
four identical fallback nodes. (3) The button is inert but not harmful: a `rejected` record with
`retryable: false` does not block `reject_if_active`. What it does leave is the operator-facing
damage — the rejection's own remediation reads *"inspect/repair the event log"*, about a log that is
not damaged.

**POPULATION.** The card appears for any run in `finalization-stalled`; the button works iff the log
carries a still-effective `run_abort` (`stop_requested == "finalized"`, which is exactly what
`_attached_finalize_intact` re-checks). So the question is how many stalls would be of each kind, and
the corpus answers it the only way it can — by how runs FINISH. Re-scanned all 41 surviving
`events.jsonl` on 2026-08-17 (`live-deps4-0804` is gone: finalized and then deleted, which is the
whole point): **39 finished runs, and 2 of them carry a `run_abort` at all** (`rubertlite-dense-retrieval`
and `-v7`, both `stop_reason: aborted`). Zero are currently half-finalized. So on this box the shape
where Reattach CAN work is 2 of 39 (5 %) and the shape where it cannot is 37 of 39 — **the defect is
WIDER than "an edge case", not narrower**, even though instances of the stall itself are rare (1 in
42 historically). That asymmetry is what decided the fix: the working case is the minority, so it
could not be treated as the default and papered over.

**WHAT SHIPPED — the card states the remedy; it does not grow a second button.** `runIndex.js` gains
`pendingFinalizeIntent` + `stalledFinalizationRemedy` (one model, both surfaces). With a pending
finalize, Dock and the canvas card are **unchanged, field for field** — pinned by a deep-equal
negative control against the shipped presentation literal. Without one, they say so and print
`looplab finalize <runs>/<run id>`, the same command the deletion refusal already names, with the
disclosure that the wrap-up uses the configured model if one is reachable. Four mutations on a
throwaway `/tmp` copy prove non-vacuity (predicate stuck true / stuck false, the overlay dropping the
command, and `_decide_run_abort` returning `append` instead of `attach`); each turns a listed test red.

**ALTERNATIVES REJECTED.**

1. **Point the affordance at `POST /api/runs/{id}/resume`.** It works, and it is still wrong here.
   It SPAWNS AN ENGINE — paid work, on a run the operator asked only to tidy up — so honouring "must
   not silently start paid work" would need a confirmation dialog naming the spawn, i.e. a new client
   call plus a new consent surface for a state measured at 1 in 42. It would also entrench the route
   the tree calls *legacy* and whose `allow_incomplete_finalize=True` is the one opt-out
   `durable_op.py` says no destructive caller may have. And per correction (2) it fires the WRONG
   VERB at this shape, resolved by a classification that two processes re-derive at two instants.
2. **A server-side finalize-only endpoint** doing what `cli/run_cmds.py::finalize`'s crash-boundary
   branch does. Cost: a new route, its own durable identity/idempotency, its own liveness and
   lifecycle-lock ladder, and it STILL spawns (the wrap-up needs an `Engine` + the task snapshot +
   the singleton), so it buys a narrower promise at the price of a second spawn path — and it is
   `/resume` with better manners. Reconsider if stalled finalizations stop being a population of one.
3. **Make `_decide_run_abort` APPEND when there is no pending intent.** One line, and it edits the
   run's authoritative record to make a button work: the appended `run_abort` leaves
   `last_stop_request_seq > last_finish_seq`, which `cli/run_cmds.py::finalize` explicitly refuses to
   create and says why (a later raw `resume` then reads as an unserved FINALIZE), and which
   `_append_resume_request` classifies on. Rejected: a control-plane change that rewrites what the
   log MEANS, to fix what a card SAYS.
4. **Keep the button and improve the failure copy.** Free, and it still asks the operator to fire a
   command that cannot succeed and leaves a rejected durable record behind. A control whose only
   outcome is a good error message is not a control.

**STILL OPEN after this.** ⬜ The surviving Reattach button spawns a driver process (proved by the
negative control's spawn recorder) and its label and tooltip do not say so. That is pre-existing
behaviour on the path this change deliberately left byte-for-byte intact, so it is recorded rather
than half-changed here. ⬜ The TUI's `finalize` verb and the Assistant's `/finalize` still take the
durable-command route and get the raw `command_intent_missing` sentence; only the two web surfaces
that OFFER the action unprompted were fixed.

### §0.12 The concept view showed one hard-negative experiment where the run had four (2026-08-17)

The operator, in their own words: *"нафига нам eval/recall_top_k и data/esci концепты если они всегда
будут? вот их надо на ран вешать. А так они просто захламляют."* — why carry a concept on a NODE when
it is true of every node in the run; those belong to the RUN, and on nodes they are clutter. They then
asked a question the concept lane could not answer: **"is there really only one hard-negative experiment
in v9?"**

**THE MEASUREMENT.** `runs/rubertlite-dr-unified-v9`, folded: eight experiments, all eight tagged, 48
tag slots, and **40 of them (83.3 %) are the same five ids on every node** —
`data/esci`, `eval/recall_at_k`, `loss/contrastive/dcl/nll_cos`, `regularization/rdrop/symmetric-kl`,
`training/negative_mining`. Exactly ONE tag per node carries information, and on a six-id list it is
fifth. Across `runs/` (41 event logs, 576 nodes, 1,933 tag slots, 50.1 % run-constant overall):

    run                        nodes  tagged  slots  run-constant  provenance            concept cadence
    rubertlite-dr-unified-v9       8       8     48   40  (83.3 %)  researcher-authored   never fired
    rubertlite-dr-unified-v7       8       8     25   16  (64.0 %)  researcher-authored   never fired
    specgate*/seed*-depth*  (36 logs)         1,368  912  (66.7 %)  researcher-authored   never fired
    rubertlite-dr-unified-v8      16      16    120    0  ( 0.0 %)  classifier            fired once
    rubertlite-dr-unified-v6       7       3     23    0  ( 0.0 %)  classifier            fired once
    rubertlite-dense-retrieval    81      80    349    0  ( 0.0 %)  classifier            159 rows

So **v9 is not an accident and the shape is not universal**: it is the AUTHORED-tag regime. Where the
classifier ran, each experiment was tagged on its own evidence, inherited nothing, and the intersection
is empty. Where it did not, the only taxonomy is the proposer's own declaration.

**TWO THINGS THE FIRST READING GOT WRONG, and both are worth writing down.** (1) *"nodes 3-7 carry no
tags"* is an artifact of reading `node_created.idea.concepts` off the wire. Those five nodes authored
`concept_mode: "delta"` and the fold materializes base ∪ delta — **every node in v9 is tagged in
state**, and corpus-wide only 5 of 576 nodes (0.9 %) carry no membership. The fold is not the defect.
(2) The redundancy is therefore not "some nodes are empty, others repeat"; it is that the delta
contract re-inflates the base onto every node and no reader could tell an inherited id from an
authored one.

**WHY THE RUN BASE IS THE WRONG PLACE TO LOOK.** `RunState.run_base_concepts` already IS a run-level
concept home, which is what makes it tempting. But `engine/concept_cadence.py::_maybe_seed_run_base_concepts`
SEEDS it from the first evaluated node's authored set, and `replay.py::_materialize_concept_deltas` then
gives it back to every delta node — so **the base is self-confirming**: the derived intersection can
never be smaller than it, whatever the later Researchers meant. On v9 that is exactly how
`training/negative_mining` — node 0's OWN subject, whose hypothesis reads *"Experiment D (scale
hard-negative mining): raise n_negatives from 2 to 4-8"* — became everybody's background.

**WHAT IT COST.** Ground truth from the recorded hypotheses: v9 has FOUR hard-negative experiments —
node 0 (mining `n_negatives` 2→4), node 2 (`dcl_threshold=0.05` aggressive selection in the loss), node
5 (mining ENCODER swapped to e5-small-en-ru), node 6 (BM25 lexical negatives, `mining_type=3`). Asked
"which experiments are about hard negatives", the concept lane answered two ways and both were useless:
match `training/negative_mining` and you get all EIGHT (it is in the base); match the specific ids and
you get 2, 5 and 6, each buried under five constants. **Node 0 is invisible either way, because its
entire authored membership IS the run constant.**

**THE FIX IS A READER, AND THE DECIDABILITY ARGUMENT IS THE WHOLE DESIGN.** "Constant across the run"
is a CROSS-NODE property. When node 0 is tagged nothing is known about node 7, so a writer-side
`run_constant` flag would mean *"constant among the nodes that existed when I fired"* and would say
different things about the same concept depending on when it was stamped — the identical ruling
`extra_metrics` already carries ("constancy … is undecidable at capture anyway because variance is a
cross-node property, so a tag derived from it would change as later nodes arrived"). So:
`search/concept_lens.py::run_constant_split` derives it in the projection, where the whole population is
in hand. **No event type, no `RunState` field, no fold change** — invariants #5 and #7 have nothing to
satisfy because nothing new is written, and replayed over all 41 logs the corpus digest is unchanged
(576 nodes, 40 champions, 494 metrics, 1 violation, `b8d15e68062ff3b9` before and after).

FAIL-CLOSED ON COVERAGE, because it is a claim about EVERY experiment: made only when every current
experiment has an exact membership and there are at least two. One unclassified node, one inexact
receipt or a one-node run ⇒ empty `run_constant` + a reason ⇒ today's rendering exactly. That is what
makes v6, `rubertlite-dense-retrieval` and v8 byte-identical negative controls rather than assertions.

Readers wired: `GET /api/runs/{id}/concepts` gains the additive `run_scope` block (withheld with
`bounded_frame` when the frame's own membership projection was capped or torn — the frame may never
name a constant it did not include); `tools/run_tools.py::node_concepts` leads a node's line with its
OWN concepts; the DAG's chip strip orders the experiment's own first and marks the run-wide ones dashed.
All three ANNOTATE — every id still appears — so no metric, champion, selectability or violation can
move. `tests/test_concept_run_scope.py` drives it over v9's and v8's real folded shapes
(`tests/fixtures/concept_run_scope_corpus.json`) and `ui/test/conceptRunScope.test.js` drives the
browser half.

**THE SECOND OUTPUT IS THE POINT, and it is a different defect from the redundancy.**
`no_distinguishing` names the experiments whose whole membership is the run constant — v9 nodes 0 and 4,
v7 nodes 0 and 7. Today node 0 wears five chips and reads as classified; named, it reads as what it is.
**This does NOT get the operator to four**, and that is stated rather than engineered around: no
derivation over what v9 recorded yields exactly {0, 2, 5, 6}. Params do not (node 3 also carries
`n_negatives=4` and is a batch-scaling experiment, so a param rule gives five); authorship does not
(node 1 authored `training/negative_mining` in its full set, so an authorship rule gives five); and the
deterministic alias tagger finds nothing at all, because `graph_from_node_concepts` synthesizes no
aliases (re-derived: `tag_text` returns `[]` for all eight v9 nodes against v9's own vocabulary). The
only thing that separates node 0 from node 4 is the English of its hypothesis, which is the classifier's
job.

**CLOSED 2026-08-18 by §0.14 — ⬜ the concept classifier cadence was structurally unreachable on a
run that is never quiescent, and that is why node 0 had no tag of its own.** The remedy prescribed
below (a THIRD pace) turned out to be forbidden by `cadence.py`'s own rule, and the blast radius was
wider than stated — read §0.14 for what shipped, for the correction that v8 is NOT an older code
baseline, and for the two findings that survive the fix: `skeleton_for()` matches no run on this box,
and the classifier REWRITES an authored membership rather than reconciling it.

The original diagnosis, kept verbatim: `engine/concept_cadence.py::_should_consult_concepts`
opens `if state.pending_nodes(): return False`. Since backlog F1f made evaluation children outlive the
turn that admitted them, a run with continuously-overlapping multi-hour evals never has a quiescent
moment. Measured by walking each log's node lifecycle:

    run                        prefixes with >0 nodes and 0 pending    classifier tags
    rubertlite-dr-unified-v9                                      0    0 of 8
    rubertlite-dr-unified-v7                                      0    0 of 8
    rubertlite-dr-unified-v8                                    148    16 of 16
    rubertlite-dr-unified-v6                                    850    3 of 7
    rubertlite-dense-retrieval                                  903    80 of 81

A perfect correlation, and v9's whole event census confirms the blast radius: **zero**
`concept_coverage_snapshot`, **zero** `coverage_snapshot` and **zero** strategy rows — the Strategist
consult and both coverage snapshots share that same `pending_nodes()` gate, so the entire cadence family
was starved for the run, not just the concept half.

It is **deliberately not fixed here**, on this file's own rules: relaxing that gate would newly emit
`node_concepts` with CLASSIFIER provenance, which the `graded_novelty` admission precheck reads — v9
runs with `graded_novelty=True` and has a `novelty_rejected` row — so it reaches **selectability**,
which is exactly what concepts may not do. It would also spend unbudgeted LLM money mid-eval, tag from a
node whose result/log excerpts do not exist yet, and let the coverage snapshot's uncovered-region
directive (deliberately behavioral) steer proposals from a different cadence. The remedy is a separate,
measured change: a THIRD pace, on `cadence.py`'s stated rule that a new pace must record no `at_node`,
scoped to the concept classifier only, with the graded-novelty evidence channel gated on it explicitly.
Whoever takes it should re-read `engine/cadence.py::occupancy_due` first — it is the existing answer to
"a node count cannot express that an evaluation has been running for four hours".

⬜ **Second residue:** the run base could be re-derived at finalization from the intersection rather than
seeded from node 0, which would stop it being self-confirming for the NEXT run's cross-run capsule. Not
done: `run_base_concepts` is an inheritance source the fold reads, so changing what is written to it is a
writer change with replay consequences, and the reader-side split makes it unnecessary for the operator's
view. Recorded so the self-confirming property is not rediscovered.
### §0.14 The Strategist has adapted nothing since v8, and the reason is six words written in June (2026-08-18)

**THE MEASUREMENT.** Folded every event log under `runs/` (42 of them) and walked each one prefix by
prefix, asking the question the cadence family asks: are there nodes, and is none of them pending?

    run                          quiescent prefixes   windows   strategy_decision   classifier node_concepts
    rubertlite-dense-retrieval (07-09→07-18)   683        43            25                    159
    rubertlite-dr-unified-v6   (08-12→08-13)   850         5             1                      3
    rubertlite-dr-unified-v7   (08-14)           0         0             0                      0
    rubertlite-dr-unified-v8   (08-14→08-16)   148         1             1                     16
    rubertlite-dr-unified-v9   (08-16→08-17)     0         0             0                      0
    e5small-dr-unified-v2      (live, 11.6 h)    0         0             0                      0

(`rubertlite-dense-retrieval`'s row is a LENIENT read: `EventStore.read_all` fails closed at a seq
gap that an operator trace-clear left at line 20 of 1,624, so the strict fold sees 20 records. Its
counts are over the raw lines and are quoted for the shape, not as a folded projection.)

A perfect correlation, and **every single quiescence-gated firing in the corpus landed inside a
quiescent window** — measuring the state BEFORE each event was appended, v6 is 23 of 23, v8 is all
of its, and dense-retrieval is 159 of 159 `node_concepts`. So nothing else was ever the binding
constraint, and the two members that DID keep firing on v7/v9/e5 prove the same thing from the other
side: `research_attempted`/`research_completed` (whose manual branch precedes the gate and whose
concurrent `_spawn_research` half carries no `pending_nodes()` check) and `lessons_refreshed` (the
READ side, on a different gate).

**v8 IS NOT A DIFFERENT CODE BASELINE, AND THAT IS THE FINDING.** It is tempting to read the table
as a regression between v8 and v9. It is not: v8 started 2026-08-14 16:25, *after* every commit in
the F1f chain, `git log -S` over its whole window touches neither `_eval_inflight` nor
`pending_nodes`, and its config snapshot is byte-identical to v9's on every relevant knob. Its 148
quiescent prefixes are **one** window — the last 2.3 % of a 47.6-hour log, 8.1 minutes, opening on
the run's FINAL `node_evaluated` — and every cadence firing that run ever made is inside it. So the
difference between v8 and v9 is **run shape**: since 2026-08-13 the family fires at most once per
run, in the end-of-run drain, and not at all in a run that ends with an evaluation still going.
v7, v9 and the live e5small run each end with three pending nodes.

**THE MECHANISM, AND THE REASON THE GUARD EXISTED.** Five periodic phases opened with
`if state.pending_nodes(): return False`: `strategy.py::_should_consult` (→ `strategy_decision` AND
`coverage_snapshot`), `concept_cadence.py::_should_consult_concepts` (→ `node_concepts`,
`concept_consolidation`, `concept_edge`, `hypothesis_concepts`, `concept_coverage_snapshot`),
`lessons.py::maybe_distill_lessons`, and `research_cadence.py`'s serial deep-research and
`_maybe_refresh_report`. The whole stated reason is **six words**, in the docstring `bb421e0f7`
(2026-06-24) shipped with the Strategist — *"only at a creation decision point (no pending evals)"*
— and that commit's message never mentions the guard. Four consumers then copied it by imitation
over three weeks; `_maybe_refresh_report` states no reason at all. The parenthesis is the tell:
"no pending evals" was never the requirement, it was the OBSERVABLE that coincided with it under
serial evaluation. `ba08f1f9` (2026-08-13, F1f) hoisted the eval task group to run scope so a
session returns while its evals burn — its own comment says *"Owning the group HERE makes a session
turn a DECISION boundary instead of a QUIESCENCE one"* — and the requirement survived that while
the observable did not. `docs/audit/2026-08-07-search-loop.md` had already recorded the same guard
as *"under speculation is almost never true"* six days earlier, filed as "Decision needed".

`run_concepts` is the control: it is the ONE member of the concept subsystem NOT behind the gate,
and it is exactly what v9 recorded — `run_concepts 1` and nothing else.

**THE FIX IS A PRECONDITION, NOT A PACE, and `cadence.py`'s own rule is what refused the obvious
alternative.** `engine/cadence.py::at_creation_boundary(pending, while_evaluating=…)` reads no `n`,
no `last`, no `every`, and records nothing. A "third pace scoped to the concept classifier" — the
remedy §0.12 proposed — cannot be built: `cadence.py` states that *a pace which records an `at_node`
mark is a node-count pace whatever it is called*, and every consumer here records one
(`node_concepts`, `concept_coverage_snapshot`, `coverage_snapshot`, `strategy_decision`). It would
also have had no self-clearing condition to bound its money the way `occupancy_due` does. The pace
count stays two.

**THE MONEY, measured as a rule rather than asserted.** The interval and each consumer's `at_node`
idempotence twin are untouched, so paid passes per node count are unchanged. What genuinely changes
is that the outer loop now reaches the gate many times at the SAME `n` — and two consumers record
no mark on their "nothing changed" path: the Strategist records only a CHANGED strategy, and the
concept snapshot records nothing when its producer yields None *after* paying for the tagging. Left
alone, each is one paid LLM call per outer-loop turn. Both carry an in-process attempted-at-`n`
memo, spent BEFORE the provider call so the error path cannot evade it; the memo only ever skips
work whose outcome was "return state unchanged", so no event, replay or resume can see it.

**THE RESUME RULE, and it costs something on purpose.** `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` gets a
row pinning the knob `false`, so a run resumed from a snapshot written before the field keeps the
DEAD cadence — which on this box is v9's and the live e5small's shape, i.e. exactly the runs the fix
exists for. The first draft exempted it on the reasoning that pinning a resumed run to the defect
preserves the defect. `tests/test_config.py::test_every_product_on_divergence_is_grandfathered_for_a_pre_versioning_snapshot`
refused that, and it was right on the table's own conditions: the historical value IS pointable at a
commit (`false` is the literal guard every consumer carried from 2026-06-24), and the change adds
paid calls AND an intervention to an old run. "The new behaviour is better" is precisely the
argument that rule exists to refuse. An operator opts a resumed run in by setting the knob
explicitly; every new run gets it without asking.

**HOLDING THE LINE (docs/36).** Relaxing the gate newly emits CLASSIFIER-provenance `node_concepts`,
which `_graded_novelty_precheck` reads — and a level-4/5 grade there SHORT-CIRCUITS the flat dedup
gate, i.e. it is an admission decision. Even identical tags would move it, because they would EXIST
EARLIER. So the OUTPUT is gated, not the input: each row carries `at_pending` (how many nodes were
pending when it was produced; absent == 0 == quiescent, which every pre-fix log provably was), and
`core/models.py::classifier_verified_node_concepts` plus the precheck both refuse a non-zero one.
An in-flight row is **invisible** to the precheck rather than "present but rejected" — a non-empty
`classifier_ids` arms its completeness rule, so counting-then-rejecting would flip a run that grades
on the curated skeleton today into one that returns None. A never-quiescent run's precheck is
therefore byte-identical to its pre-fix self, and `_reusable_node_tags` re-tags an in-flight row at
the next quiescent pass (bounded by the EXISTING `_RETAG_CAP`), so a run that DOES drain ends with
exactly the evidence it would have had. The read models are untouched, which is the entire point:
the operator sees node 0's tag; selection does not.

**ONE OUTPUT IS WITHHELD ENTIRELY, and it is the one a per-row stamp cannot fence.** An in-flight
pass records no `EV_CONCEPT_CONSOLIDATION`. A rename is RETROACTIVE and RUN-WIDE — the fold applies
it backwards to every authored-delta node's stored membership (`_materialize_concept_deltas`
resolves `added`/`removed`, the run base and each parent's set through the map) and every read
surface resolves ids through it (`events/digest.py::_folded_axes`/`folded_concepts`,
`serve/concept_frame.py`, `search/coverage.py`). Measured on v8, its 9 recorded renames change what
**11 of its 16 nodes** are reported as being about. Withholding costs nothing that exists today: a
run that never quiesces records no consolidation now either.

**WHAT THIS DOES NOT FIX, and it is the more expensive finding.** ⬜ **`skeleton_for()` matches no
  OPEN[concept-skeleton-matches-no-run] proof:present:skeleton_for@looplab/search/concept_graph.py
run on this box.** The curated taxonomy (`search/concept_graph.py`: 26 leaves + 10 axis roots + 10
`<axis>/*` placeholders = 46 ids) is resolved from `state.task_id` against ONE registered pack,
`dense-retrieval`, plus seven substring aliases. Every run here answers `repo_task`,
`e5small-dr-unified-v2` or `toy_quadratic`, none of which contains an alias — so `seed=None` and
`build_concept_map` grows a vocabulary from scratch. The consequence refutes the natural assumption
that the dead classifier is why v9's vocabulary is invented: measured per run (distinct ids /
invented), the classifier-LIVE group is **58.8 %** invented and the classifier-DEAD group **40.7 %**.
On the operator's own axis, v8's LIVE classifier minted SEVEN spellings — `mining/hard_negative`,
`mining/false_negative_filtering`, `train/negatives/mining`, `train/negatives/in-batch`,
`sampling/in-batch/negatives`, `training/negative_mining`, `training/negatives/count` — for the four
curated `negatives/*` ids, in one run. The only 100 %-curated population on the box
(`rubertlite-dense-retrieval`, 159 rows) was tagged by an OFFLINE CLI pass eight days after the run
ended, with the task type given explicitly. Turning the classifier on gives node 0 the tag of its
own it needs; it does **not** unify the paths the operator asked about, and saying otherwise would
be wrong.

⬜ **The classifier REWRITES, it does not add.** `_on_node_concepts` assigns
  OPEN[classifier-rewrites-authored-membership] proof:present:_on_node_concepts@looplab/events/replay.py
(`st.node_concepts[nid] = bounded`), authored provenance has no protection (only OPERATOR does), and
the authored ids survive only in the raw log — `events/digest.py` explicitly forbids readers from
resurrecting `idea.concepts`. Measured on v8, which is the precedent: **2 of 24 authored ids survive
into their own node's classifier row (91.7 % replaced)**, node 0's authored set and its folded set
share nothing, and node 3's exactly-curated `regularization/r-drop` was replaced by the invented
`regularization/rdrop`. A classifier row on a PARENT also re-materializes every authored-delta
child's inherited set. This is the designed behaviour — the proposer must not certify its own
taxonomy, and §0.12 measured the authored regime as self-confirming — but it is a record-side effect
and it is recorded here rather than discovered later. No live run is disturbed by this change: a
running engine does not reload its source.

**ALTERNATIVES REJECTED.**
1. *A third pace under `cadence.py`* (§0.12's own proposal). Refused by `cadence.py`'s stated rule:
   every consumer records an `at_node`, so the "new pace" is the node-count pace renamed, and it
   would close that window for a full `every` nodes while `already_covered_at` refused the next
   firing at the same count. It also has no self-clearing condition — `occupancy_due`'s money bound
   is its CONDITION, and "the classifier has something to say" never clears on its own.
2. *Redefine quiescence as "no eval is mid-STAGE".* A node is `pending` for its whole evaluation and
   is mid-stage for essentially all of it; on this corpus the predicate is the same predicate. It
   also invents a second meaning of quiescent beside invariant #1's, which already had to be
   restated once for F1f.
3. *Fire at a boundary that provably exists — a node terminal, or finalization.* A terminal is a
   real boundary, but `_run_cadences` has ONE call site and it is the outer loop's creation decision
   point; routing a second entry through a terminal would put a paid LLM pass on the eval-completion
   path and give the family two firing sites with two idempotence stories. Finalization is where v8
   already fires, and it is exactly the behaviour being called a defect.
4. *Leave the gate and add a `run_constant`-style reader.* That is §0.12, and it got the operator to
   "node 0 has no distinguishing tag" and stopped — a reader cannot mint the tag only a classifier
   can produce.
5. *Relax the gate with no output fence.* The measured hazard: v9 runs `graded_novelty=True` and has
   a `novelty_rejected` row, so newly-visible classifier evidence reaches a level-4/5 admission
   override. Refused; this is what `at_pending` exists for.
6. *Give in-flight rows a new PROVENANCE tier instead of a stamp.* Cheaper at the precheck (the
   `== CLASSIFIER` compare filters them for free) but the tier is enumerated in
   `INHERITABLE_CONCEPT_PROVENANCE`, `card_ledger._CARD_NODE_CONCEPT_PROVENANCE`,
   `search/concept_projection.py` and the CLI, and it would strip the tag out of the read models —
   which is the one thing the operator asked for.
7. *Also let `lessons_distilled`, serial deep research and the report refresh through.* Deliberately
   not in this change: deep research already has its own overlap path (`_spawn_research`, which is
   why `research_attempted`/`research_completed` are the two things that DID keep firing on v7/v9/e5)
   and the other two are recorded below as the remaining share of the same gate.

**PROVEN BY:** `tests/test_cadence_while_evaluating.py` — 21 tests over the real predicate, a real
`EventStore`/`fold` run shaped like v9, and a real `Engine` with a counting Strategist: the property
(a never-quiescent run fires the family), the money bound (25 outer-loop turns at one node count buy
exactly one paid concept pass and one consult, including through a raising provider), the line (a
run whose only classifier rows are in-flight grades on EXACTLY what a run with no classifier rows
grades on, while a quiescent row still reaches the channel), the parity re-tag and its `_RETAG_CAP`
bound, the consolidation withholding, and the fold's fail-closed receipt. Non-vacuity re-verified by
mutating a throwaway `tar` copy of the tree with NINE mutations — the gate reverted, the novelty
filter removed, the evidence boundary removed, each memo removed, the parity rule removed, the
withholding removed, the stamp removed, and a comment-only evasion of the gate — **all nine killed**.
Replayed over all 42 event logs under `runs/`, metrics, champions, feasible sets, violations,
memberships and provenance are byte-identical: digest `3eda8c9d95dadd1b` before and after.

⬜ **Remaining share of the same gate, not taken here:** `lessons.py::maybe_distill_lessons`
(`lessons_distilled`: 0 on v7/v9/e5, 2 on v8) and `research_cadence.py::_maybe_refresh_report`
(`report_generated`: 0 on v9 and e5). Both are one call to the same predicate; they are left out
because neither was measured for what it costs to run mid-eval, and this change's whole claim is
that each consumer's output was fenced deliberately rather than by inheritance.

### §0.13 A red stage chip about an attempt that ended 177 minutes ago, over a node that was training (2026-08-17)

Reported by the operator: the Inspector's TRACE showed experiments #5 and #6 of the live
`rubertlite-dr-unified-v9` **training**, while the Overview's eval-pipeline chips showed them
**failed**, and the node graph gave no sign either way.

**MEASURED AT 12:48 UTC, on the live run.** Both nodes fold to `status=pending`,
`eval_started=True`, with **nine** and **five** live `vectorsearch.train` processes in their
workdirs. Their event history, and the whole defect is in the last line of each:

    #5  seq 2842/2843  stage_finished mine ok · stage_finished train fail
        seq 2889       node_repaired attempt=1 crash
        seq 2894/2895  stage_finished mine reused · stage_finished train fail
        seq 2938       node_repaired attempt=2 crash
        seq 2943       stage_finished mine expect_failed
        seq 2976       node_repaired attempt=3 expect_failed   → then NOTHING, for 177 minutes
    #6  seq 3153/3154  stage_finished mine ok · stage_finished train fail
        seq 3194       node_repaired attempt=1 crash            → then NOTHING, for 56 minutes

So the folded strip read `✗ mine (expect_failed)` and `✗ train (fail)` — statements from repair
cycles **2** and **1** — while cycle 3 trained. The chips are `Inspector.jsx::StagePipeline` over
`Node.stages`, which is `events/replay.py::_on_stage_finished`, last-wins BY STAGE NAME; the trace
is `events/traceview.py` over `spans.jsonl`. **Two sources, and only one of them had the answer.**

**BOTH HALVES OF THE ROOT CONFIRMED.** (1) There is no stage-START event: `events/types.py`
registers `EV_STAGE_FINISHED` and no counterpart, and v9's log holds 21 `stage_finished` and
nothing else matching `stage` — while `spans.jsonl` holds **24** `stage_started` spans
(`runtime/command_eval.py:2441`), which is exactly why the trace was right. (2) `stage_finished`
carries `{node_id, name, status, exit_code, seconds, generation}` and no repair counter, while
`node_repaired` carries `{attempt, generation, reason, verified}` — and an inline repair bumps
`attempt` (the repair ordinal) and NOT `generation`, so rows from either side of a repair are
indistinguishable in the fold. **One correction to the report**: the NODE GRAPH does not show these
nodes as failed. `util.js::nodeClass` paints `s-pending` from `node.status`, which is `pending` —
the complaint's accurate half is its second sentence, that the visual gives no sign of training.

**HOW BIG, AND IT IS NOT COSMETIC.** Walking every log for windows where the folded strip held a
failing row and a `node_repaired` had landed after it: **44 windows**, **median 66.1 minutes**, p75
191.9 min, max 560.8 min, **99.7 hours in total**, 26 of them over an hour, across **18 distinct
node lifecycles**. Restricted to the statuses the strip paints RED (excluding `timeout`, which it
paints amber): 41 windows, median 61.1 min, 89.8 h. That is a LOWER bound — a window is closed as
soon as ANY stage speaks again, even though the other stages' rows stay stale. All of it is in
**v6-v9**: a pre-2026-08-07 log wrote every stage row at the TERMINAL, after all repairs, so it
cannot express this shape at all (`rubertlite-dense-retrieval` has 184 stage rows and 33 repairs and
zero windows). The defect is a consequence of moving the rows into the attempt loop — which was
itself a fix, and the right one.

**FIXED BY DERIVING THE ATTRIBUTION FROM THE LOG'S ORDER, adding no event and no event field.** The
fold sees `node_repaired` and `stage_finished` in order, so it can stamp each stage row with the
repair epoch it was recorded in (`Node.stages[].repairs`) beside the node's current one
(`Node.repairs`); a row whose epoch is smaller is one no later attempt has spoken about
(`core/models.py::stage_row_superseded`). A `reused` marker ADVANCES the epoch of the record it
declines to clobber, because a reuse is the later attempt's own statement that the result stands —
without that clause the rule convicts three genuinely-current nodes (v8 #3, v8 #10,
dense-retrieval #1, whose `mine` rows are older only because every later attempt reused them).
Replayed over all 41 preserved event logs the fold is **byte-identical** once the two new keys are
stripped, and the golden fixture moves by exactly eight `"repairs": 0` lines and nothing else.

**THE ALTERNATIVES, AND WHY EACH LOSES TO THIS ONE.**

1. **Add the repair counter to `stage_finished`** (additive, reader-side default). Correct in
   principle and INSUFFICIENT in practice: a writer-side column answers only for rows written after
   the change, so v9 #5 and #6 — the nodes the operator was looking at — stay unattributable, along
   with all 44 measured windows. It also needs `Node.repairs` anyway (a row's epoch means nothing
   without the node's current one), i.e. it is this fix plus a schema change plus a blind spot.
2. **A stage-START event.** Doubles the folded stage rows on the append path (21 → 42 on v9,
   bounded by attempts × stages) — and `engine/evaluate.py` states at that very append site that
   these rows can move `speculation.py::_proposal_authority_seq` and discard a paid proposal under
   `eval_parallel > 1`. Making it DIAGNOSTIC dodges that (the fence excludes `DIAGNOSTIC_EVENTS`
   wholesale) but then the fold — the authoritative state — still cannot answer, which is defect
   (3). `phase_progress` is already the general beacon for this and deliberately covers only
   `build`; its own comment says a stage added there needs its own append site. And it fixes
   nothing retroactively.
3. **Have the UI read the `stage_started` SPAN.** Cheapest to write and wrong at the boundary:
   `spans.jsonl` is an explicit sidecar that replay does not rebuild (CLAUDE.md's opening
   paragraph), so History, `looplab replay`, the report exporters and the reviewer scope would keep
   answering the old way while the live tab answered the new one; `serve/trace_clear.py` can delete
   the evidence outright; and a trace read costs 3.4 ms/span plus the absent-fence probes. The chips
   are folded state and must be answered from the fold.
4. **Say "superseded" from what the fold knows today, with no new field at all.** This is what
   shipped, with ONE correction to the premise: the fold did NOT already know a `node_repaired` came
   after the last stage row. `_on_node_repaired` mutates `code`/`files` and nothing else — the repair
   ledger lives in `engine/evaluate.py::_durable_repair_ledger`, which re-reads the raw log — so
   `RunState` carried no repair count anywhere. The two integers ARE the "no new data" fix: they are
   derived, not carried, and they cost one `max()` per repair row.

**STILL OPEN.** ⬜ **The node graph still cannot say which experiment is running.** `util.js::
  OPEN[node-graph-cannot-name-running-experiment] proof:present:workingId@ui/src/util.js
workingId` returns the HIGHEST-ID pending node, and `Node.eval_started` — the folded durable proof
that an evaluation was announced — is `exclude=True`, so it never reaches the wire
(`narration.js::pendingWork` re-derives it from the raw event tail and says so in a comment). On v9
at the measured instant that made node **7** the "working" node, which had not begun, while #5 and
#6 held fourteen live training processes between them. Recorded rather than fixed here: it is a
change to the state payload's field set and to a heuristic three surfaces read, it wants its own
measurement of which runs evaluate in parallel, and it is a different defect from the one the chips
had.

### §0.15 The engine asked the agent to choose a GPU footprint and told it the choice was free (2026-08-19)

**THE QUESTION.** Is it faster to run two experiments at one GPU each, or one experiment on both
cards? Asked of the live `e5small-dr-unified-v2`, every node of which declared `{"gpus": 1}`.

**THE HISTORICAL DATA CANNOT ANSWER IT, and that is the first finding.** Every footprint in every
preserved event log on this box is `{"gpus": 1}` — v2, v3 (the `vec-backups` copy), v6, v7, v8, v9,
132 recorded footprint values, zero exceptions. The only 2-GPU population is `runs/rubertlite-dense-retrieval`:
all 80 of its nodes ran Lightning DDP with `--gpus 2` (`LOCAL_RANK: 0/1 - CUDA_VISIBLE_DEVICES:
[0,1]`, `distributed_backend=nccl`) — a different repo, a different backbone (`rubert-tiny-lite`, 3
layers / hidden 256, against `e5-small-en-ru`), a different framework and a different dataset. There
is no controlled arm, so any speedup number quoted from `runs/` would be a comparison of two
workloads.

**AND THE UNIFORMITY IS NOT THE AGENT'S CHOICE.** The `run_started.goal` of v2, v6, v7, v8 and v9
all end with the same operator sentence:

    EACH EXPERIMENT GETS EXACTLY ONE GPU. Declare footprint {"gpus": 1} on every card and write
    single-GPU training code — do not use accelerate --multi_gpu. Two experiments run concurrently,
    one per device.

`rubert_dr_0804`'s task file says the same thing in its own words. So on the runs that prompted the
question the role was obeying an instruction, and no engine change reaches that: **the largest lever
here is the operator's own goal text**, and the cue this section ships defers to it explicitly.

**WHAT THE CORPUS DOES DECIDE.** Two things, both on the live run's own bytes:

* **One H200 does not hold this recipe.** Four of v2's nine nodes died of `torch.OutOfMemoryError`
  inside `train` — node 0 at per-device batch 8192 (the manual recipe ported unchanged), node 1 at
  2048x8, node 7 at 2048x2, node 2 at 1750 — each traceback reporting the node's OWN process holding
  ~139 of 139.8 GiB, i.e. its own allocation and not a sibling's. The nodes that ran to completion
  did so at 512-2048. So for this backbone the second card is not a speed preference; at the
  recipe's batch it is the difference between running and not running.
* **Whether the footprint changes the OPTIMIZATION or only the clock is a property of the LOSS,
  and that is the sharper finding.** The manual champion's repo gathers with gradient flow —
  `train.py::_gather_cross_batch_zoneids_and_embeddings` calls `self.all_gather(..., sync_grads=True)`
  — so its in-batch negative pool is `world_size x train_bs`, and `looplab-knowledge` records the
  recipe exactly that way: *"bs 8k x 2gpu = 16k/step, accumulate 2 (eff ~32k)"*. There, a 1-GPU node
  at 8192 x 4 accumulation is a DIFFERENT experiment from a 2-GPU node at 8192 x 2, because
  accumulation restores the effective batch and never the negative pool. In `vectorizer-unified` it
  depends on which loss the node picked: `CrossBatchMultipleNegativesRankingLoss`,
  `CrossBatchClassAwareMNRLoss`, `SigLIPLoss` and `Qwen3EmbeddingLoss` all call
  `torch.distributed.nn.functional.all_gather`, while `NLLCosLoss` — the loss ALL FIVE evaluated v2
  nodes configured (`"loss": {"type": "nll_cos"}`) — states in its own docstring: *"Operates on the
  per-device batch (no cross-process gather)"*. So on the live run's own recipe the second card
  would NOT enlarge the negative pool and the two footprints ARE comparable; on the recipe the
  operator benchmarks against, they are not. No fixed engine rule can know which — the agent that
  chose the loss can, which is the argument for putting the choice there.

**EVERY WALL CLOCK QUOTED FROM `runs/` CARRIES ~2 h/NODE OF CPU-ONLY SEARCH THAT NO LONGER
HAPPENS, and that makes this decision BIGGER rather than smaller.** The `faiss-gpu-cu12` wheel on
this box ships cubins for arch 70 and 80 with no sm_90 and no PTX, so every faiss GPU probe aborted
and every index fell back to exact CPU — 20 of 20 probes on the live run, which is why v2's own
`train_monitor_alert` rows narrate a "graceful faiss GPU->CPU index fallback (rc=-6)". Replaced with
an exact torch IP search (local commit `1eff7c1`, equivalence proven against a float64 arbiter) and
measured on interleaved arms: evaluation (641,261 x 384, k=1000) 12.00 -> 0.056 s/batch (~210x),
mining (k=1000) 4.79 -> 0.080 s/batch (~60x). Against v2's stage ledger — `train` 40.75 h, `mine`
8.17 h, `score` 5.00 h over 33 stage rows, 53.93 h of stage time in a 36.2 h run — that means the
~13.2 h outside `train` was very largely CPU search and collapses. So a node's critical path becomes
essentially its TRAINING, and the footprint (which is what decides training's wall clock) goes from
governing ~76 % of a node's stage time to governing nearly all of it. **Any per-node figure taken
from `runs/` — node 1's 14.6 h, node 5's rejected 10.5 h burn — is an OLD-search number and must not
be used to size a future node.** The three-arm probe below is unaffected: it measures training only,
and it is now a cleaner measurement than it would have been.

**AND ON THE SEARCH SIDE A SECOND CARD CURRENTLY BUYS NOTHING.** faiss replicated the index across
every card named in `IndexSettings.gpus`; the torch path uses `gpus[0]` only, and the replication was
not measured. At the new numbers that is the right default and probably not worth restoring yet: a
`score` stage's search has gone from ~1:11 per node to seconds, so a perfect 2x on it saves seconds
while adding an index-sharding/merge correctness surface — and it would be spent on the ONE stage
where a 2-GPU node's second card is otherwise idle, which is a real but now-tiny waste. Revisit it
only if the corpus grows enough that search re-enters the node's critical path (re-measure, do not
assume: the ratio it has to beat is now `search_seconds / train_seconds`, which is ~0 on this data).

**THE ARITHMETIC, which needs no measurement and is what the role was missing.** Holding the
PER-DEVICE batch fixed, K devices do K x the examples per optimizer step, so the same epochs take
~1/K the steps: about the SAME experiments per hour, each finishing ~K x sooner — but a different
experiment (K x effective batch, K x negatives). Holding the GLOBAL batch fixed, K devices split one
step K ways at a speedup below K: the same experiment, FEWER experiments per hour, each sooner. So
"two 1-GPU experiments in T" and "one 2-GPU experiment in T/1.6" are not two answers to one question
— they are answers to two, and which one is being asked depends on whether the batch is scaled with
the device count. Nothing in either direction makes the shipped prompt's claim true.

**THE DEFECT.** Both paragraphs the Researcher reads about `footprint.gpus` closed on the same
sentence — `proposal_cues.py::_gpu_budget_hint_text` ("declaring more does NOT get this experiment
more hardware … the run serialises at the same per-experiment cost") and the code-owned
`roles.py::_FOOTPRINT_GUIDANCE` ("the run SERIALISES at the same per-experiment cost"). The
scheduler contradicts both halves: `resources.py::_resource_request_for_node` takes a DECLARED count
over AUTO, `_acquire_gpus` reserves exactly that many devices all-or-nothing, and
`_resource_eval_env` writes them into the child's `CUDA_VISIBLE_DEVICES` — pinned by
`tests/test_gpu_footprint_choice.py::test_the_scheduler_honours_the_declaration_the_old_text_denied`,
which passes BEFORE the change. The wording came from the `rubertlite-dr-unified-v5` incident, which
was a WIDTH defect (`run_started` claimed 2 while one node held both cards); `Settings.proposal_width`
closed that in the scheduler in 2026-08-13 and the sentence outlived its cause.

**WHAT SHIPPED.** `Settings.gpu_footprint_cue` (ON) replaces that clause, at the SAME splice
position in both prompts (`developer_probe`'s `_system_body` pattern — the two alternatives say
OPPOSITE things about one declaration, so appending would leave the prompt carrying both readings).
The share is stated as the ORDINARY default rather than a wall, a larger count is stated as
HONOURED with the width consequence named, both directions of the arithmetic above are stated, the
per-device memory comes from the scheduler's OWN `_gpu_mem` inventory (silent unless it joined
losslessly — `detect_gpu_inventory` returns `({}, {})` rather than guess), the box's speedup is
stated as UNMEASURED with a short fixed-step probe invited (`_cue_experiment_time_budget`'s remedy
for per-step time, one axis over), and a count named in the operator's task statement is stated to
win. `false` reproduces BOTH historical paragraphs byte for byte, and an UNSTAMPED role — a library
caller with no engine — keeps the historical clause, so the two spellings of "off" agree.

docs/36: a wider ACTION space, never a wider trusted set. The role may now ask for a different
footprint and must say why in its rationale; it still cannot exceed the pool
(`_clamp_resource_footprint`), still does not own the width (`proposal_derived_width`), and touches
no metric, champion, selectability or violation.

**A SECOND DEFECT, found while wiring the first.** A flag set only in `Engine.__init__` reaches the
PRIMARY role and nothing else: `_build_role_pairs` builds fan-out pairs from `role_factory()` after
`__init__` and caches them in `_role_pool`, and `_prepare_node_idea` is handed one of those as the
proposing `researcher`. So a run with a build fan-out would have had its primary role asking the
corrected question while every pooled sibling asked the pre-change one — the two-variants-disagree
drift `_researcher_capability_suffix` exists to stop, arriving through a different door. The boolean
therefore rides on `_stamp_gpu_budget_hint`, which already runs per proposal on whichever role is
proposing and already documents the pooling reason. Driven by
`test_a_POOLED_researcher_asks_the_same_question_as_the_primary`, and proved non-vacuous by deleting
the two-line stamp in a throwaway copy: both parametrizations go red. (`memo_verdict_cue` is wired
the `__init__`-only way and has the same exposure — not touched here, because its clause is spliced
into `_state_brief`, which is a different delivery path and would need its own measurement.)

**SPECULATION UNDER A 2-GPU NODE: already correct, now pinned.** A speculative card BUILD is a
Developer call on a producer lane and reserves no device — the freshness envelope it is gated on is
the PERMANENT machine (`speculation.py::_resource_envelope` reads `_gpu_ids`/`_gpu_mem`, never
`_free_gpus`), which is `card_selection.py`'s stated rule: *"a busy GPU makes a Card wait; only a
declaration that cannot fit on this machine makes speculative work stale"*. Verified with both
devices reserved: the envelope and `_speculative_prefetch_ceiling()` are unchanged, and the waiting
1-GPU node's reservation returns `None` (retry) rather than a refusal, then succeeds on release.
What a full pool stops is DISPATCH, which is the intended cost of the choice.

**STILL OPEN — do not re-discover these.**

1. **The measurement itself.** The cheapest decisive experiment is ~45 GPU-minutes and was NOT run
   (both cards were busy with the live run): a fixed 200-step training of one v2 recipe at three
   footprints — (a) `gpus=1`, per-device batch B; (b) `gpus=2`, per-device batch B; (c) `gpus=2`,
   per-device batch B/2 — reporting s/step and samples/s. (a) vs (c) is the strong-scaling speedup S
   (the only number that decides the same-global-batch case); (a) vs (b) is the weak-scaling case
   the contrastive recipe actually wants; and the largest B that survives on one card is the memory
   answer the OOM evidence above only brackets. **Interleave the arms** — sequential A/B on this box
   measures the box's other load, not the arms. Measure TRAINING only: with the faiss fallback fixed
   the search cost is no longer part of the comparison, and folding it in would re-import the
   confound this section's wall-clock paragraph exists to remove.
2. **`core/hardware.py::operational_attention_points` still says the opposite**, in the same system
   prompt: *"By DEFAULT use ALL available GPUs (e.g. `--gpus <N>` / DataParallel/DDP for N GPUs)
   unless the task says otherwise; don't leave GPUs idle or run a tiny single-GPU job on a multi-GPU
   box without reason."* That block reaches Genesis, Boss, Researcher, Developer AND Strategist and
   has no flag, so it was not touched here on the strength of a measurement nobody has. It is now
   the only remaining contradiction of the footprint contract, and closing it needs its own change.
3. **The prompt ceiling and the scheduler grant still disagree** whenever `eval_parallel != pool`
   (`widths.py::per_experiment_gpu_budget` says `pool // eval_parallel`, the undeclared-footprint
   branch of `_resource_request_for_node` grants a flat `1`). Unchanged by this section and still
   recorded in that helper's own docstring.

### §0.14 A repair that retuned `train` forfeited a 61-minute `mine` it provably could not reach (2026-08-18)

Reported against the live `e5small-dr-unified-v2`. Node 0's whole history, four repairs, every
failure a CUDA OOM inside `train`:

    stage mine ok    · stage train fail · repair changed ['looplab_stages.json']                   -> mine RECOMPUTED
    stage mine ok    · stage train fail · repair changed []                                        -> mine REUSED
    stage mine reused· stage train fail · repair changed ['looplab_stages.json','config.yaml']     -> mine RECOMPUTED
    stage mine ok    · stage train fail · repair changed ['looplab_stages.json','config.yaml']     -> mine RECOMPUTED

Reuse fired exactly once, on the one repair whose change set was EMPTY. **`mine`'s manifest entry is
byte-identical across all five manifests the engine committed** — re-derived by digesting each
`materialized_stages` entry out of `node_created.files`/`node_repaired.files`: `mine` is
`1aa54dcf9de9ed9c` five times over, while `train` goes `347cf4d7` → `fc8a3f40` → `fc8a3f40` →
`d0023327` → `64d932b9` (argv 17 → 14 tokens, then its `expect.assert` from 15 epochs to 10). The
mining never had anything to do with the failure, and it cost 3,593 / 3,667 / 3,662 s each time.

**WHAT THE CLAUSE ACTUALLY COMPARED.** `_safe_reuse_start` asked *"is `looplab_stages.json` in the
change set?"* — a question about a FILE. The manifest carries every stage's argv, so the answer is
yes for any repair that retunes the stage that just failed, which is what a repair for an OOM *is*.

**THE ONE COST FIGURE IN THE REPORT IS WRONG AND THE CORRECTION IS THE POINT.** The report charged
all three re-mines (~3 GPU-h) to this clause. Only ONE is: repairs 3 and 4 also rewrote
`vectorsearch/configs/config.yaml`, and the **non-`.py` clause forfeits those independently. That
clause is deliberately not touched** — its `needs` widening was examined and refused on 2026-08-14
(a `needs` is a precondition, not a bound, and 2 of 129 corpus stages declare one at all), and
`engine/champion_caveats.py` records what leaning on it costs.

**MEASURED over every `node_repaired` row in `runs/`** — 85 rows, 6 runs:

| | rows |
|---|---|
| had a completed earlier stage to forfeit | 21 |
| …of those, changed the manifest | 7 |
| …of those, confined STRICTLY AFTER the reuse point | 6 |
| …not confined (`rubertlite-dr-unified-v8` node 0 attempt 4 — its `mine` entry really moved) | 1 |
| …confined but ALSO changed `config.yaml`, still refused by the non-`.py` clause | 4 |
| **newly admitted** | **2**, across 2 runs |

Replayed through the SHIPPED `manifest_prefix_unchanged` and not a copy of it, which is what caught
a first pass of this table over-counting the admitted rows at three: `e5small-dr-unified-v2` node 1
attempt 3 is confined, and it also rewrote `config.yaml`. The two that remain are worth **5,966 s
(1.66 h) of re-run stage time** — `e5small-dr-unified-v2` node 0 attempt 1 (3,667 s) and
`rubertlite-dr-unified-v8` node 9 attempt 2 (2,300 s), both settled. **This is a small population
and is stated as one**; the case for the change is as much the incentive it removes as the hours.
The earlier reading — "all 3 rows with a completed stage also changed `looplab_stages.json`, so the
widening buys ZERO reuse" — is TRUE about the file and false about the entry, and it is the sentence
this entry splits.

**AND ZERO WOULD HAVE BEEN WRONG**, on evidence from both directions. Directly:
`stage_outputs` (shipped 2026-08-17) records each declared artifact's sha256, and node 0 mined THREE
times, writing the same `01627a8c47f66efb…` / 218,707,487 bytes every time — the artifact a recompute
produced is the artifact the reuse would have kept. Indirectly for v8, which predates that field:
node 9's two `mine` runs are separated by a change set of `looplab_stages.json` + `train.py`, and
`train.py` is not in `mine`'s import closure (`mine_stage.py`, `vectorsearch/{__init__,config,utils}
.py`, `vectorsearch/data/{__init__,mine_negatives,preprocess}.py`), so every input the predicate can
name is identical; and that miner is demonstrably reproducible — the preserved `hard_negatives
.parquet` of nodes **3, 9 and 10** are all `13db44775c306253…` / 79,586,058 bytes, produced by
miners whose own closures and `config.yaml`s genuinely differed (§0.12), across four `mine` runs on
node 3 alone. Note the CONTENT key does not already answer this — node 0's three
`mine` rows carry three DIFFERENT `stage_input_key`s (`41ac8c21…`/`4aa0789c…`/`3739ed81…`) for
identical output, because `stage_identity` digests every non-`.py` workdir file and the manifest is
one. It refuses here for exactly the reason the old clause did (§0.12).

**FIXED as a PREFIX rule, `eval_stages.py::manifest_prefix_unchanged`** — hoisted to module level so
the rule has a truth table a test can reach. It is TRUE only when both manifests resolve through
`materialized_stages`, the failed stage sits at the SAME INDEX in both, and every entry before that
index is equal as canonical JSON. The clean entry's key set is CLOSED (`name`, `command`, `timeout`,
`check`, `expect`, `needs`, `env`), so the compare covers the stage's argv, its declared artifacts
AND their `assert` sentence, its declared inputs, its environment and its leash, and prefix equality
subsumes every reorder and rename. The index check is the clause that cannot be dropped: a manifest
that only **shrinks** — a completed `prep` removed — leaves `prep`'s outputs on disk, and reusing
across it would feed `train` an artifact a full re-run provably would not have produced. Acquitting
the manifest REMOVES it from the change set and nothing else: the deletion, non-`.py`, `cwd`, opaque
and reachability clauses all then run over what is left.

**WHY "STRICTLY AFTER" IS SUFFICIENT, re-derived rather than assumed.** Stages run in pipeline order
in one workdir, so nothing declared at index > i is read by the process at index i: `_run_stages`
resolves `env` PER STAGE onto `ex.env` rather than accumulating it, there is no per-stage `cwd` to
re-base an earlier one, `needs` is checked before its own command and `expect` after it, and the
protected `score` stage is built from the operator's `EvalSpec` and never from the manifest. There is
no field by which a later entry reaches backwards. A later edit CAN make the earlier artifact the
wrong INPUT for the new later stage — and that is answered by the later stage's own `needs` check and
by it running and failing loudly, not by silently scoring a checkpoint nobody trained.

**THE PREVIOUS MANIFEST IS READ OFF THE FOLD, NEVER OFF DISK, and that is the trust model.** The
2026-08-17 rejection of this loosening (§0.9 residue (c)) was right about the hazard — "the one
change that would let something the agent WRITES decide whether a stale checkpoint is scored" — and
it is answered by WHERE the bytes come from: `prev_files` is `node.files` off the event log,
snapshotted above the repair call, i.e. what the ENGINE committed and materialized. The workdir is
the candidate's and an eval can rewrite its own manifest while it runs; a predicate that read the
pre-repair chain from disk would let a stage that rewrote the manifest at the end of its own run
declare its output still valid. Both sides of this compare are engine-authenticated. Nor does the
narrowing hand the agent a new lever: any edit to the reused stage's own entry still forfeits, so
"only later stages changed" is the one thing it can express — which is the true statement.
`attempt_freshness_floor` is untouched, so `verify_stage_artifacts` still holds every stage that RAN
to its own start and the "skip if the file already exists" shape stays caught.

**THE INCENTIVE MAY BE THE BIGGER HALF, and `rubertlite-dr-unified-v8` node 9 is BOTH of its
halves in two consecutive repairs.** Attempt 1 changed `vectorsearch/train.py` alone and its
committed bytes say why, in the agent's own words:

    # train.py-only change (no config.yaml edit) that leaves the completed `mine` stage reusable.
    # change (overrides the CLI --n_epochs 10) so the completed `mine` stage stays reusable.
    config.train.training.n_epochs = 6

That is the `params_overridden` mechanism exactly — the running code at a coordinate no declaration
carries, which is the run's only champion caveat (node 3, `batch_size` 8192/2 declared and 4096/4
run) — and it is the engine's own price list being read back to it. Attempt 2 then did the HONEST
thing: it deleted that override (`# n_epochs comes from the CLI … and is NOT overridden here`) and
edited the manifest's `train` argv instead — **and paid 2,300 s of re-`mine` for it**. So the old
clause charged an agent for editing the DECLARATION and charged it nothing for hiding the same
change in a `.py`, and this corpus contains the agent taking both options one attempt apart. That
2,300 s is one of the two rows this change recovers.

**Driven by `tests/test_stage_reuse_manifest_prefix.py`** (25 tests, tier 1 throughout, and the
suite never reads `runs/` — node 0's five real manifests are copied into
`tests/fixtures/stage_manifest_prefix_corpus.json`). The predicate half runs the real
`_safe_reuse_start` over a real workdir: later-stage-only edit reuses; an edit to the reused stage's
own entry, to its `expect.files`, to its `expect.assert`, a reorder, a rename, an insertion, a
REMOVAL, a `score`-stage failure, a deletion, a non-default `cwd`, a nested `looplab_stages.json`, a
`config.yaml` beside the manifest and a reachable `.py` beside it all still forfeit; and the
keyword's ABSENT value reproduces the historical answer byte for byte. **The last two drive the REAL
repair loop** (a real `Engine`, a real `RepoTask`, `run_command_eval` stubbed only so the test can
read back the `start_stage` the engine CHOSE) and observe the EFFECT rather than the predicate's
answer: a Developer that rewrites only the failed stage's argv yields `start_stage` `[None, "train"]`
and charges the re-train cap nothing, and the same Developer pointed at `mine`'s own argv yields
`[None, None]` and one `full_retrain_charged`. Under the historical clause the first of those reads
`[None, None]` — the defect, reproduced end to end. **EIGHT MUTATIONS on a throwaway
copy of the tree, each going red**: drop the reuse-point index equality (1 test); compare stage NAMES
instead of whole entries (3); leave the acquitted manifest in the change set (3); make the rule
always true (14); stop expanding `%params%` on the previous side (1); let the failed stage be absent
from the previous manifest (1); acquit by BASENAME instead of exact path (1) — the old clause matched
`looplab_stages.json` anywhere in the tree, which is right when the answer is always "forfeit" and
exactly wrong when it can be "acquit": `_resolve_stages` reads only `<workdir>/looplab_stages.json`,
so clearing a nested one out of the change set would walk a non-`.py` the predicate never examined
straight past the clause that would otherwise have refused it; and restore the historical per-FILE
forfeit outright (4, including the end-to-end one, which then reads `[None, None]`).

**STILL OPEN.** ⬜ **The `timeout`-only manifest edit** §0.9 residue (c) also asked for is NOT
exempted. A stage's `timeout` is a leash and not an input — `stage_identity` already excludes it from
`stage_input_key` for that reason — so raising the FAILED stage's ceiling is now free, but raising an
EARLIER stage's still forfeits it. Left closed deliberately: no corpus row asks for it, and a field
carved out of an entry compare is a hole that has to be re-argued every time the entry gains a key.

### §0.15 A pre-launch liveness audit of the whole feature stack, and the two cadences §0.14 left behind (2026-08-19)

**WHY THIS EXISTS.** Before a multi-day run, "the unit tests are green" is not the question. The
question is whether each feature FIRED on a real run and did its job, and the only evidence that
answers it is `runs/`. Every subsystem here writes something durable, so the method is a COUNT per
run and a last-seen — the same method that found §0.14. Corpus: the six event logs under `runs/`
(`rubertlite-dense-retrieval`, `-v6`, `-v7`, `-v8`, `-v9`, and the live `e5small-dr-unified-v2`,
started 2026-08-17 16:38 and therefore PINNED to code from before every 2026-08-18 fix).

**THE LEDGER.** FIRING / DEAD / DEGRADED, with the count that decides it.

| feature | verdict | evidence |
|---|---|---|
| novelty gate (`novelty_mode=llm` in all six) | FIRING | 18 adjudications on the live run (17 of them ≥2 s of agentic deliberation, `phase_progress{phase:"novelty"}` paired started/finished), 14 `novelty_rejected` corpus-wide (dr 10, v7 3, v9 1). Live 0/18 rejected — accepted, not skipped |
| repair critic | FIRING | 6 consultations (live 1, v7 1, v8 4); only 1 `repair_critic_verdict` because the event type postdates v7/v8. The gate is `attempt >= repair_critic_after(3)` and only three runs ever reached a 4th repair |
| memo verifier (`trust/memo_verify.py`) | FIRING | 89 of 89 eligible memos verified, all six runs, up to the live run's latest. 438 `unsupported` vs 328 `supported` — the healthiest critic in the engine is the one saying the memos are mostly unevidenced |
| plan critic (`trust/critic.py`) | FIRING | wired in all six (`critic_check=true`); sole producer of the corpus's only trust signals (4 `critic:params_ignored` on v6) |
| reward-hack + code-leakage detectors | DEGRADED | they run unconditionally per evaluated node, but a CLEAN scan writes nothing at all — no event, no span, no `node_evaluated` field. A run where the call was deleted is byte-identical to a clean one. Also `trust_gate="audit"` in all six, so neither could have moved a selection |
| confirm phase, verifier tie-break | DEAD (off) | `confirm_top_k=0`/`confirm_seeds=0` and `select_verifier=false` in all six snapshots. An operator choice, not a defect |
| Strategist + concept cadence | was DEAD, fixed 2026-08-18 | §0.14. Unprovable on disk: the fix postdates every run |
| lesson distillation, report refresh | **DEAD — FIXED HERE** | see below |
| auto-skill promotion | DEAD | the shared `skills/` store is EMPTY. `n_skills` went 4 → 12 (dense-retrieval, July) → 0 (v7) → 0 (v8), and v6/v9/live never reached the phase at all because it is RUN-END only and none of them finished |
| training monitor | DEGRADED | 205 alerts corpus-wide (live 96, of which 70 `broken` at up to 0.97 confidence with a 41-tick streak), **0 acts, ever**. Five of six kill conjuncts cleared simultaneously on the live run; only `log_role` refused, because a 3-stage pipeline can never earn `LOG_ROLE_TRAINING`. Fixed by `a5412bb8`/`364cef55` — after the live run pinned its code |
| ASHA monitor | DEAD — now SAYS SO | zero `asha_rank` AND zero `asha_verdict` rows in seven runs (re-derived 2026-08-19), all with `asha_live` and `asha_live_kill` on. `min_siblings` is NOT the blocker: the tick bails at `sample is None` because `RECALL@100:` is printed ONCE, on the LAST line of a 5-10 hour training. A successive-halving watchdog pointed at a task with no intermediate curve. Still inert; no longer silent about it (`kill_reachable: false` + `inert_reason`, below) — and the corpus DISQUALIFIES every engine-side proxy curve |
| every phase of a node's life | FIRING | live run: stages 11/11, plan 11, propose 18, card build 11, novelty 18, create 11, seed 9, eval-start 9, stage exec 33, terminal 8, repair 13. The 11/9/8 gaps are fully accounted for by one Card-freshness supersede and three nodes genuinely in flight — no phase is silently skipped |
| card board / speculation | FIRING | `card_added` 11, `card_build_*` 11 each, `card_enriched` 35, last 2026-08-19 00:38 |

**THE DEFECT FIXED HERE: §0.14 CONVERTED TWO OF THE FIVE CONSUMERS IT NAMED.** `engine/cadence.py`'s
own docstring, `core/config.py`'s field comment and `tests/test_cadence_while_evaluating.py`'s module
docstring all enumerate FIVE phases that opened with `if state.pending_nodes(): return False`. The
2026-08-18 change moved `strategy.py::_should_consult` and `concept_cadence.py::_should_consult_concepts`
onto `cadence.at_creation_boundary` and left the other three on the dead proxy, and the reader of
those docstrings could not tell — they name the copiers and then read as if the family were done.

The corpus decides it, on the SAME configuration in every run (`lessons_every=4`, `report_every=3`,
`comparative_lessons=true`, `reflection_priors=true`) and re-derived here independently of §0.14:

    quiescent prefixes (nodes exist, none pending)     dr 903/81w   v6 850/5w   v7 0   v8 148/1w   v9 0   live 0
    lessons_distilled   (trigger=cadence)                 19           1          0        1          0      0
    report_generated    (trigger=cadence)                 26           1          0        1          0      0
    research_completed  (trigger=cadence)                 27           5          2       14          6      9

The last row is the control and it is what keeps `_maybe_deep_research` OUT of this fix. That gate is
the SERIAL half of a decision whose CONCURRENT half (`orchestrator._spawn_research` →
`_due_research_trigger`) never carried the guard, so deep research is alive in all six runs including
the three with zero quiescent prefixes. Opening the serial half mid-eval would put a main-task think
and a background think at the same node count with only a read-then-write window between their shared
`_cadence_research_marks` check and their receipts — a double-spend bought to reach work already being
done. So four of five now call `at_creation_boundary` and the fifth is a stated refusal, pinned by
`test_the_serial_deep_research_gate_is_deliberately_left_on_the_old_predicate`.

**THE MONEY, and why these two need no memo.** §0.14's two consumers carry an in-process
attempted-at-`n` memo because they record no `at_node` on their "nothing changed" path. These two
record one on EVERY path: `lessons_distilled` is appended even with zero lessons (its own comment
says why), and `serve/report.py` sets `content["at_node"]` OUTSIDE its try, so even a provider failure
that degrades to the minimal report closes the durable window. The pace is unchanged — one paid pass
per node count however many times the outer loop turns at it — and
`test_a_fixed_node_count_buys_exactly_one_distill_and_one_report` drives 25 turns at a fixed `n`
against both. The one shape that does change is that a lesson window can now open before any pair is
comparable; that costs nothing (`select_comparison_pairs` returns `([], [])` with no provider call)
and loses nothing (an empty receipt spends no pair), it only delays that batch by one interval — against
a status quo of never distilling at all.

**THE THREE NEW TESTS FAIL AGAINST THE PRE-CHANGE TREE** (verified before the edit, in the worktree at
`086ca5b4`): `test_lessons_distil_on_a_run_with_evaluations_in_flight` and
`test_the_report_refreshes_on_a_run_with_evaluations_in_flight` both `assert (0 == 1)`, and the money
test reports `paid 0 distillations at one node count`. Each carries its kill-switch negative control
in the same body, so `cadence_while_evaluating=false` still reproduces the historical predicate.

**STILL OPEN — filed rather than patched.**

⬜ **F1i-b · the serial deep-research gate under `concurrent_research=false`.** Not the shipped default
  OPEN[f1i-b-serial-deep-research-gate] proof:present:cadence_due@looplab/engine/cadence.py
(`Settings.concurrent_research = True`), so no run on this box is affected, and every run in `runs/`
carries `true`. Under `false` the concurrent half does not exist and the serial gate is the only path,
which in a GPU-shaped run means deep research never fires at all. The fix is not the one-liner the
other four got: it needs the two paths to agree on a single spend, i.e. the mark check and the receipt
under one claim rather than two reads. Do it when someone actually wants serial research.

**THE WHOLE ENGINE STOPS FOR A PROPOSE PHASE — measured 2026-08-21 on a live run, and this is the
mechanism the "free GPU sits idle" family has been circling.**

`_handle_create_actions` is awaited on the main task, but everything under it is SYNCHRONOUS:
`_stage_card_creates` → `_prepare_node_idea` → `foresight.propose` → `agent.run_phase` →
`tool_loop.drive_tool_loop` → `llm.chat` → `_bounded_create` → `threading.join`. A py-spy dump of the
live engine (pid 3423806, `runs/e5small-dr-unified-v4`, sampled twice 8 minutes apart) puts asyncio's
own `_run_once` BELOW that join with **no coroutine frame in between** — so an entire propose phase,
call/parse/call/parse with no `await` anywhere, executes as ONE event-loop callback.

Two observations that looked contradictory are both explained by it: **243 provider calls COMPLETED**
between 21:26 and 21:45 (nothing was wedged) while **node 4's terminal never landed** (nothing else
ran). All 243 happened inside one callback; the loop had not turned since the phase began.

The bill, on one evening: node 4's train stage OOM'd and its process EXITED at 21:03:40. Sixty-two
minutes later the engine had emitted no `stage_finished`, no terminal and no repair for it — last
node-4 event 21:01:03 — with `pgrep -P` showing no children and all three anyio worker threads idle
on empty queues. Both H200s sat idle ~59 minutes. Propose phases in that run take a median 10.8 min
(n=12, max 38.9), and node 5 ran SIX back to back, producing **10 cards added against 5 ever
requested for build**: the board filled while the machine that consumes it never got a turn.

`orchestrator.py:1902` already documents the neighbouring half — card production is reachable "only
in the instants when NOTHING is running... Production was gated on occupancy ZERO, which is exactly
backwards." That comment fixes WHEN creates become reachable. This entry is the other half: once
reachable, a create holds the loop for as long as it takes, and eval finalisation, terminal writes
and GPU dispatch all wait behind it.

Separate and not to be conflated: `_nonstream_bounded` bounds ONE attempt at
`llm_timeout + header_timeout + 10` (415 s on this run) and `_post` retries `range(max_retries + 1)`
= 9 times, so a genuinely wedged call can hold the loop ~62 minutes on its own. That is a worst case
this run did not hit.

**IT IS THE WHOLE CREATE HANDLER, not one phase — mapped 2026-08-21 after the stack was read.**
`_handle_create_actions` spans orchestrator.py 2172–2523 and every producer call inside it is a
plain `def`, none of them awaited: `_stage_card_creates` (2300, the one on the live stack),
`_claim_existing_card_builds` (2366), `_consume_batch_proposal` (2403) and `_create_node` (2494 and
2506). The last is the DEVELOPER — the phase that writes a node's code, and the longest of the lot.
So the loop is held not only while a proposal is drafted but for the whole of node building, which
is the same freeze arriving through four more doors. A fix is a five-point change, not a one-liner.

**And the offload looks feasible on every axis checkable without running it** (recorded here because
this entry previously said the opposite): `llm_broker`'s fairness is a plain `threading.Condition`,
so it is thread-native and blocking it ON the loop is itself wrong; `card_reservation.py` and
`search/foresight.py` contain ZERO `anyio.`/`asyncio.`/`await`/`async def`; the broker's lane
contextvar survives `anyio.to_thread.run_sync` (driven — "build" set on the loop reads back "build"
in the worker); `EventStore.append` already runs under a writer lock and tail CAS because the UI and
the engine write the same file from different processes; and an AST pass finds ZERO RunState
mutations in either file. What remains is empirical — no live test — and `_prepare_node_idea` lives
in orchestrator.py, where that AST pass has not been run.

**NO MARKER, on the rule that refused one for §0.1 item 9: an item without a re-derivable falsifier
must not be tagged.** Every candidate here fails. A pin on the synchronous call site
(`if self._stage_card_creates(lane, state):`) stays TRUE after an offload lands INSIDE
`_stage_card_creates` — a marker that can never go green. A pin on `to_thread`/`run_sync` names a
mechanism the fix may not use. And the honest fix is not obvious: `llm_broker` fairness and
`card_reservation` both sit in that chain, so whether the phase can move off the loop thread at all
is unmeasured. What this row needed was a stack and a number, and it has both.
*Evidence: `py-spy dump` captures, two samples plus a final one, in the session scratchpad.*

**A clean trust scan commits to nothing — CLOSED 2026-08-19, see §0.18.** The `trust_scan` receipt
is written for EVERY evaluated node, hit or no hit. One correction to the ledger row above that this
entry owes: **"they run unconditionally per evaluated node" is FALSE** — `reward_hack_detect` and
`code_leakage_detect` are `false` in four of the six snapshots (`rubertlite-dense-retrieval`, `-v6`,
`-v7`, `-v8`, together 100 evaluated nodes), so on those runs neither detector ran at all, and the
log was identical to the two runs where they did.

**ASHA cannot work on this task family — the SILENCE is fixed here, the CURVE is refused on evidence.**
Re-derived 2026-08-19 over all seven event logs in `runs/`: **`asha_rank` 0, `asha_verdict` 0**, against
205 `train_monitor_alert` rows over the same evals, with `asha_live: true` AND `asha_live_kill: true`
in every snapshot that carries them. Not `min_siblings`, not configuration: the tick `continue`s at
`sample is None` because the objective is printed ONCE, on the last line. Counted per training log,
`RECALL@100:` appears **0-3 times in a whole multi-hour run** (the 2s and 3s are repeat attempts, not
points): `e5small-dr-unified-v2` node 2 is 53 MB and 13,337 lines with exactly one, the last. Node 5
of that run trained from 09:32:00 to 20:14:13 — **10 h 42 min** — and was rejected 106 s later
(`node_failed reason=idea_rejected`); nodes 2 and 4 ran to completion for 0.0 and 2e-05.
The 2026-08-07 audit (`docs/audit/2026-08-07-search-loop.md` F3) had already asked for exactly the
diagnostic this entry ships, twelve days earlier.

**SHIPPED: the watchdog now says it cannot act.** `asha_inert_reason` is a pure three-rung truth table
over the METRIC CONTRACT (a kind with no live reader at all / a readable `stdout_regex` that
`sibling_metrics_at_resource` will not accept / a missing `resource_key`), stated on the FIRST tick
because — like the training monitor's role gate — it is a property of the contract and not of the run's
health. The observational rung is the one the corpus measured: three consecutive ticks whose log is
WRITING and has still never named the objective are reported as "no curve to halve"; an EMPTY tail is
not counted, because "nothing written yet" is the stall watchdog's question. Each is said at most once
per eval, on one `asha_monitor` span carrying `kill_reachable: false` + `inert_reason` — the training
monitor's attribute name and meaning, deliberately one vocabulary across both watchdogs — plus one
`_LOG.warning`, because an operator reading a config is reading a console and not a trace (the GPU-pool
lease's precedent). It is NOT a widening: no event, nothing the fold reads, no model reading made
load-bearing, `should_asha_kill`'s conjuncts untouched. `tests/test_asha_inert_is_visible.py`; both loop
properties verified red against `4138e7ef` in a throwaway tree, as assertions rather than ImportErrors.

**REFUSED: no engine-side proxy curve. The corpus disqualifies it.** The tempting move is to rank the
signal these logs DO carry — `train_monitor.LossTrajectoryTracker` already reduces one per tick — but a
successive-halving race assumes every arm is on ONE axis, and here each arm is a different LOSS
FUNCTION. Replayed over the corpus (median `loss` over each node's first half, and the first `eval_loss`
point, ranked at the median bar, against each node's final `RECALL@100`):

    run                        signal      n    spearman(signal, final)   would kill   killed the run's BEST
    e5small-dr-unified-v2      loss        5          +0.600                  2               YES (0.7934)
    e5small-dr-unified-v2      eval_loss   5          -0.300                  2               no
    rubertlite-dense-retrieval loss       69          -0.531                 34               no
    rubertlite-dr-unified-v6   loss        6          -0.371                  3               no
    rubertlite-dr-unified-v6   eval_loss   6          -0.600                  3               no
    rubertlite-dr-unified-v8   loss       12          +0.350                  6               YES (0.7620)
    rubertlite-dr-unified-v8   eval_loss  11          -0.445                  5               no
    rubertlite-dr-unified-v9   loss        4          -0.800                  2               no
    rubertlite-dr-unified-v9   eval_loss   4          +0.400                  2               YES (0.7406)

A working signal is NEGATIVE here (lower loss, higher recall). **The sign is not even stable across runs
of one task family** — +0.600, -0.531, -0.371, +0.350, -0.800 — and the rule kills the run's single best
node in 3 of the 9 pairs. Node-by-node it is worse than the summary: on `-v8` it stops **6 of the 10**
evaluated nodes including the top three (0.7620, 0.7618, 0.7527); on `-v2` two of the top three
(0.7934 — the champion — and 0.7742); on `-v6` three of the top five. On `rubertlite-dense-retrieval` it
spares all ten best and still kills **34 of 69**, and it spares them for the wrong reason: that run's
leaders sit in a cluster whose custom loss runs to -2.4e6..-3.7e8, so they rank "best" on SCALE. The
rule this repo set itself — a rule that would have killed a node that went on to produce a good number
is disqualified — is failed in three runs out of four. **Not built.** Reading what exists would be the
stronger claim only if what exists were comparable; measured, it is not.

    OPEN[asha-inert-on-this-task-family] ASHA still has no curve to halve on this task family, and
    `resource_key` — the one declaration that makes its kill reachable — is unnamed in the task schema
    an operator authors against. proof:absent:resource_key@looplab/adapters/repo_task.py

**What the remaining half would cost, measured, so the next agent argues from a number.** The only
comparable quantity is the objective itself, and only the training script can emit it mid-run — so this
half is a TASK-side contract (`/home/jovyan/data/vectorizer-unified`), which is why the engine's honest
move was to name the missing declaration rather than invent a proxy. It is not free: in `-v2` the
objective evaluation (encode the corpus, build the index, search, score) took **74.6-75.9 min** on every
one of the four nodes measured, against a `train_runtime` of 18,273 s — **25 % of the training per
point**. Emitting the five-point curve ASHA wants would MORE THAN DOUBLE the run it is meant to shorten.
So the task-side fix is a SUBSAMPLED intermediate eval (a query slice, reported as
`{"recall": …, "step": …}` on one stdout_json line) plus `eval.metric.resource_key` in the task file —
and the engine-side residue is that `resource_key` reaches the watchdog through a free-form
`EvalSpec.metric: dict` — it is named in `docs/guide/tasks.md` and in no schema field, no validator and
no Genesis prompt, so nothing an operator authors against ever mentions the one switch that arms the
kill. That is what the marker above is pointed at.

⬜ **Auto-skill promotion still runs only from the wrap-up pass — NARROWED 2026-08-19, see §0.18.**
  OPEN[auto-skill-promotion-run-end-only] proof:absent:promote_settled_skills@looplab/engine/lessons_distill.py
The TWIN question is settled and needed no new run: `n_skills: 0` on v7/v8 is not the classifier
over-rejecting, because **zero cards reached it** (v7 has no evaluated node at all; all three of v8's
`supported` cards are record setters with `best_delta = None`). That rung now writes its own
`skill_candidates` row, so the next run says so on disk instead of being re-derived by hand. What
stays open is the TRIGGER, and it is the trigger and not the phase: `looplab finalize` reaches the
identical run-end pass on a stopped run — driven end to end, a 38-node run stopped mid-flight and
then finalized produced its `reflection_note` with 4 lessons and 14 candidate receipts — so nothing
in the engine prevents a stopped run from getting this pass. Nothing SURFACES it either: `looplab
stop` says so once, in a terminal, and a KILLED run (v6, v9 — no `pause`, no finish) is never told
anything at all. The named fix a future change would land is a per-card settled-promotion pass
(`promote_settled_skills`); this marker's proof is its absence.

### §0.18 Two receipts that never existed, and the audit question each of them decides (2026-08-19)

*(§0.15's liveness audit left two items whose common shape is not "a feature is dead" but "the
evidence a feature produces does not survive". Both are closed or narrowed here on measurement.)*

**METHOD.** Every number below is re-derived from `runs/` — six preserved event logs plus the live
one — by folding each log and reading its own `config.snapshot.json`. Nothing is taken from the
§0.15 ledger, and one row of that ledger turned out to be wrong.

---

#### A. A CLEAN SCAN NOW LEAVES A RECEIPT — and the ledger's premise was false

**The defect.** `reward_hack.py`, `leakage.py`, `critic.py`, the hardened exploit suite and the
workdir-write audit all run per evaluated node and write to the log ONLY on a hit, so a run in which
every node was scanned clean is byte-identical to a run whose scan call was deleted — the 2026-08-05
mutation audit's own finding (deleting both `sigs +=` lines left 117 trust tests green).

**THE LEDGER SAID "they run unconditionally per evaluated node" AND THAT IS FALSE.** Re-derived from
the snapshots, `trust_gate="audit"` in all six as stated, but:

    run                          reward_hack_detect  code_leakage_detect  evaluated  trust rows
    rubertlite-dense-retrieval        false               false               1*          0
    rubertlite-dr-unified-v6          false               false               6           4   (critic only)
    rubertlite-dr-unified-v7          false               false               0           0
    rubertlite-dr-unified-v8          false               false              13           0
    rubertlite-dr-unified-v9          true                true                4           0
    e5small-dr-unified-v2             true                true                5           0

    * folded under today's code; that July log is pre-Card and does not re-derive.

So the corpus holds BOTH populations the log could not distinguish — 9 evaluated nodes that were
genuinely scanned by both detectors and came back clean, and 100 on which those two detectors never
ran — and an auditor reading `events.jsonl` gets the same silence from either.

**What shipped.** `trust_scan` (fold-ignored, `DIAGNOSTIC_EVENTS`), one row per evaluated node,
carrying `detectors` (which ones ran, in `TRUST_DETECTORS` order), `findings` (a COUNT), the
`evidence_version` and `code_digest` of the exact scanned surface. It says nothing about the code:
no signal payload, no rule name, no candidate text — the flagged `reward_hack_suspected` row already
owns the detail, and both rows now take that digest from ONE function (`trust/scan_receipt.py::
scan_subject_digest`), reached through the module so a test that moves the rule moves both writers.

**Three design decisions worth keeping.**

1. **The reader-side default is the point.** `trust_scan_status` answers `unknown` for a node with no
   receipt, `unscanned` for a receipt naming no detector, and `clean` only for one that names a
   detector and zero findings. An absent receipt reading as `clean` is the exact inversion this
   closes, and it is the state every log on this box is in.
2. **A separate row, not a `node_evaluated` field** — this is a deviation from the sketch in §0.15
   and it is deliberate. Stamping the terminal requires the scan to run BEFORE the terminal append,
   which puts five detector calls (three over agent-authored source, one a filesystem walk) between
   an evaluation and the one row a run cannot lose. A separate row can instead be lost to a kill in
   that window — and then it reads `unknown`, which is the correct answer.
3. **The detector list is the scan's OWN decision.** `_trust_scan_detectors` is the single predicate
   the scan branches on and the receipt reports; a receipt derived from a second copy of the
   settings reads would claim a detector looked when it did not.

**Nothing can move a selection today, and that is checked rather than asserted:** the type is in
`DIAGNOSTIC_EVENTS` (so the fold has no handler and `_proposal_authority_seq` skips it), and
`test_the_receipt_is_fold_ignored_so_no_selection_can_move` folds a real run with and without the
rows and compares the full `RunState` dump. `trust_gate` is `audit` in all six snapshots and this
change does not read it.

**Where an auditor sees it.** `looplab inspect` now closes with one line — on a preserved log,
`trust scan: 13 evaluated node(s); 13 with NO scan receipt — unknown, not clean (…)`; on a fresh
default-profile run, `8 evaluated node(s); 8 scanned by NO detector (all of them configured off)`.

---

#### B. AUTO-SKILL PROMOTION: THE TWIN QUESTION IS ANSWERED, THE TRIGGER QUESTION IS NARROWED

**The twin question §0.15 called undecidable is decidable, and the answer is neither classifier.**
Folding every log and re-running the promotion loop's own gates:

    run    cards   supported   supported AND best_delta > 0   evaluated
    v6      15         0                    0                     6
    v7      11         0                    0                     0
    v8      22         3                    0                    13
    v9      13         1                    1                     4
    v2      17         2                    1                     5

v7 recorded `n_skills: 0` because it never evaluated a node. v8 recorded `n_skills: 0` because all
three of its `supported` cards carry `best_delta = None`: `card_ledger.py::_evidence_verdict` makes
a card supported when one of its nodes SETS the run record, and leaves `best_delta` None when that
node has no feasible evaluated parent to measure against. `(h.best_delta or 0) > 0` therefore dropped
every one of them **before the deterministic prefilter, let alone the rubric model**. The classifier
was never asked. (Caveat, stated because it is real: this is today's fold over an August-16 log, so a
change to `_evidence_verdict` since then would move these numbers. v7 needs no such caveat — a run
with zero evaluated nodes can have no positive delta under any verdict rule.)

**What shipped: the rung that refused gets a receipt.** The eligibility gate was a bare `continue`,
so the `skill_candidates` receipt built on 2026-08-18 to answer "which statements were refused and
why" was blind to the rung that refuses most. It now writes a row for a card the run ITSELF called
`supported` — `no_measured_delta` (the record setter, v8's shape) or `no_positive_delta` (a measured
regression) — under `classifier: "skill-eligibility/v1"`. Only for `supported` cards: a
`tested`/`open`/`abandoned` card is not a candidate in any sense, and a row per card would bury the
three that matter under v8's other nineteen. **The eligibility RULE is unchanged and that is
deliberate** — a technique card claims "this improved the metric over its baseline", and a card
supported by setting the record has no baseline to have improved over.

**The trigger question, argued.** *Should a phase whose whole value is cross-run depend on a clean
finish?* No — and it does not. It depends on the WRAP-UP PASS, which `looplab finalize` performs on a
stopped run, and that is the phase, not the trigger. Driven end to end: a 38-node toy run stopped
mid-flight (`looplab stop`: 38 `node_created`, one `pause`, no `run_finished`, no `reflection_note` —
the exact shape of v2, v6 and v9 on disk) and then `looplab finalize`d produced its `reflection_note`
with 4 lessons and 14 candidate receipts, 3 of them from the new rung. **The gap is the trigger.**

**Mid-run promotion was NOT built, and the honest reason is that the corpus cannot yet justify it.**
Replaying each log prefix-by-prefix at every terminal boundary and re-asking the promotion gate:
across v6/v7/v8/v9/v2, exactly **2 cards ever qualified**, both on the final state, and **0 were
retracted** — no card was promotable at one boundary and not at the end. So the obvious argument
against mid-run promotion ("it would promote claims the run later withdraws") is *unsupported by this
corpus*, and so is the argument for it: two candidates over five runs is not a population you design
a paid cadence against. What a safe intermediate point would have to settle is the card's EVIDENCE
SET and its IDENTITY — no pending evidence, no pending merge (a merged card's `statement` is
rewritten by the fold, so a skill promoted early is filed under a title the run later replaces), and
no route to `abandoned`, which overrides every verdict. That is a per-CARD settlement, not a run-level
one, and it is the shape a `promote_settled_skills` pass would need.

**What is left open** is therefore narrow and is not an engine defect: nothing SURFACES that a
stopped or killed run holds unclaimed cross-run value. `looplab stop` says "`looplab finalize` to
wrap it up" once, into a terminal that scrolled away days ago; a KILLED run (v6 and v9 carry no
`pause` and no finish at all) is never told anything, and `classify_prior_run` reads both as `paused`
/ `live` rather than as work owing a wrap-up. Three of the last four runs on this box ended that way,
and `e5small-dr-unified-v2` is sitting there right now with exactly ONE card that is `supported`,
positive-delta and passes the deterministic prefilter — the only promotable skill candidate this box
has produced since July, unpromoted because nobody typed one command.

### §0.17 The truncation cuts the END, and we put the answer at the end (2026-08-19)

Two independent fixes today turned out to be one habit, and the habit is worth its own entry because
neither fix generalises on its own.

**Deep-research memos** were rendered through a blind head cut: median 9,083 chars against a
4,000-char keep, 89 of 90 memos over it, and `Recommended directions` — the section the whole
pipeline exists to produce — past the cut in **89 of 89**, in 194 of 212 real tool calls.

**Cases** had a reader all along, and it delivered nothing: a `kb_search` hit is clipped at 600
chars and the record led with the task goal, so `best params=` began at char **691** of 1,610. Of
the 7 `PAST CASE` blocks ever delivered in the corpus, 4 were from an unrelated task and the 3
exact-task ones were cut mid-goal. The store was written, kept, matched and delivered — and the
reader received a restatement of its own prompt every time.

Same shape both times: the mechanism is intact end to end, the bound cuts the TAIL, and the payload
is at the tail. A bound that removes the answer is worse than no answer, because the caller cannot
tell a short record from a truncated one.

OPEN[tail-truncation-drops-the-payload] no rule stops the next bounded surface putting its answer past its own cut. proof:present:RESULT_CAP@looplab/tools/_base.py

  Both fixes are LOCAL: memos gained sections, the case record leads with its params. Neither
  establishes the general rule, which is what this entry is for — every bounded surface in the tree
  should be checked for the same shape (does the FIRST thing the caller needs survive the bound?),
  and the module rule "a bounded answer names what it did not cover, beside the call that returns
  it" (`tools/log_tools.py`, rule 3) should be the ceiling everywhere rather than in the two places
  that happened to be measured.

### §0.16 Two costs measured and deliberately NOT paid down (2026-08-19)

*(Numbered after §0.15 rather than merged into it: that section is a LIVENESS
audit — what fired on a real run — and this one is two measured costs left
unpaid. Two agents reached for the same number on the same day; keeping them
apart keeps each answerable on its own terms.)*

Both fell out of the `REVIEW 2026-08-18` efficiency sweep. Each is real, each is sized, and each is
left open because the cheap version of the fix is weaker than the thing it would replace.

DECLINED[dev-command-seed-cache] **A `run_dev_command` rebuilds the candidate from the operator's
source on every call.** measured: 2.4-2.7 s vs 0.12 s to clone and 0.09 s to fingerprint; refused
because the invalidation would be a SECOND definition of `seed_mode` — docs/BACKLOG.md §0.16.
Measured on this box, seeding the LoopLab tree itself (1,318 tracked files / 31.7 MB, `seed_mode:
auto`) off the geesefs mount: **2.4-2.7 s**, against **0.12 s** to clone the same tree from a local
copy and **0.09 s** to fingerprint the source (`git ls-files` + one `lstat` per file). So the
proposed seed-once/clone-per-call cache is worth ~91 % of the setup of a tool whose intended use is
a repeated compile→fix→test loop, and the disposable-candidate property is untouched by it — the
cache would hold the operator's own source, never a candidate's output.

What blocks it is not the saving. **The cache has to be invalidated against what `seed_repo_tree`
actually copied, and that function publishes a COUNT.** `auto`/`tracked` copies the git-tracked set
and falls back to a full `copytree` outside a worktree, so a fingerprint derived independently at
the call site would be a SECOND definition of `seed_mode` — the exact thing `engine/workspace_seed.py`
exists to prevent, one function away from the ordering that was just deduplicated — and it fails in
the wrong direction: a file dropped from `git ls-files` while its bytes stay put keeps a stale
candidate, and a stale candidate makes the Developer check its edit against bytes the eval will
never run. That is the `rubertlite-dr-unified-v6` node 4 class of defect, produced by the tool built
to catch it. **What a cache must publish first:** `seed_repo_tree` returning the SET it copied (not
a count), so the fence is `atomicio.file_identity` over exactly those paths and nothing re-derives
the mode. `tests/test_dev_commands.py::test_each_call_sees_the_operators_CURRENT_source` pins the
property any such cache has to keep. Note the tool has never run in production here — no
`task.snapshot.json` under `runs/` declares `developer_commands` — so the 2.4 s is a real cost of a
surface with no measured load, which is the other half of why it is not paid down blind.

DECLINED[watch-scheduler-idle-exit] **The watch scheduler thread never exits.** measured: one
`scandir` + one `lstat` per record every 2 s, no file opened; refused because the retirement has to
be observed under `_start_lock` — docs/BACKLOG.md §0.16. `WatchService.stop()` has no production caller and
`_loop` has no self-terminating condition, so a server that has ever held one watch ticks every 2 s
for the rest of its life, including after the last watch settles. What a tick COSTS is now bounded
(`WatchStore.due`: one `scandir` plus one `lstat` per record, no file opened, both memos in
`__init__`), which is why this is residue rather than a defect. The idle-exit is not free: the
retirement has to be observed by `ensure_started` under `_start_lock`, or a watch armed in the
window between "the loop decided to stop" and "the thread ended" is never serviced — a watch that
silently does not watch, which is the failure the whole module exists to prevent.

### §0.19 Every prompt edit in this repo was unmeasured, and for one judge it no longer is (2026-08-20)

**The standing problem.** The training-log monitor's kill authority was changed twice in one week.
Nothing said whether either change made it better or worse, because there was no way to ask. The
same is true of every other judge prompt here: `render(prompts, key, default)` is overridable, prompt
strings are contracts, and not one of them has a number attached.

**The material was already on disk, and the missing piece was never the data.** `spans.jsonl`
records both the `input` and the `output` of every `generation` span — 3,950 with both in
`runs/e5small-dr-unified-v2` alone. What was missing is the LABEL, and the label does not exist for
every judge. Two disjoint classes, and the split is the whole design:

* **Outcome-labelled.** The run itself later supplies a fact the judge did not author. The monitor
  called a node `broken`; the node then scored `0.0`. A correctness claim is possible.
* **Unlabelled, permanently.** The novelty gate REJECTS an idea, so the idea is never run and
  nothing on disk says whether it would have worked. It can be scored only for CONSISTENCY with the
  old verdict — a score maximised by reproducing the old model's mistakes. **A consistency number
  presented as an accuracy number is the failure mode this whole thing has to avoid**, so the
  scorer keeps them in two fields, prints them under two headings, and has no code path that
  averages them; `label_accuracy` returns `None` rather than `0.0` where nothing is labelled,
  because "no evidence" and "always wrong" are different answers.

**What shipped.** `looplab/judgebench/` + `tests/data/judge_bench/train_monitor.v1.jsonl.gz` (450
decisions, 278 KB gzipped from 4.8 MB raw) + `docs/guide/judge-bench.md`. Committed rather than
generated on demand on one fact: `runs/` is not in the repository, so an on-demand dataset cannot be
read by a reviewer judging a prompt change, cannot gate a merge and cannot be diffed. It is derived
and never hand-edited — `test_every_label_rederives` recomputes every label from the row's own
stored facts through the production rule, offline, with no corpus present.

**Three label decisions that each moved the headline, and each is arguable:**

1. **Stage status alone is not the label.** `e5small-dr-unified-v2` node 2 trained 6.4 h, the stage
   exited `ok`, and the model scored `0.0`. That is the case the judge exists for and a
   stage-status label scores it `productive`.
2. **`timeout` is its own class, excluded from accuracy.** 60 of 450 decisions. The compute was
   wasted, but the judge's own system prompt tells it a slow-but-progressing run is `watch`, not
   `broken` — charging those as missed stops penalises it for obeying its instructions.
3. **`ts - seconds` is not a stage's window.** Every `stage_finished` row of an eval attempt is
   flushed together at the END (measured spread inside a burst: < 0.15 s, on a `mine` that ran an
   hour), so placing a decision by that arithmetic put 32 of 168 decisions in the wrong attempt or
   in none. Attempts are clustered from the bursts instead.

**The incumbent's numbers, which are now the baseline every candidate is read against.** Over 354
outcome-labelled decisions: accuracy **0.701**; it said `broken` about a run that finished fine
**5 times**, and never said `broken` about a wasted run 101 times. Per eval attempt — the unit
compute is paid in — **7 of 27 wasted attempts caught, 3 of 49 productive attempts falsely stopped,
≈18.9 h saveable.** On the case it exists for it is strong: of 53 decisions watching a training that
completed and produced a dead model, **48 called it `broken`**. **4 of the 5 false alarms are the
two runs where the judge had no log tools** — which read as the tools change being the fix, and was
a CONFOUND rather than an A/B. It is settled below, and the tools are NOT the explanation.

**What this corpus cannot answer, stated in the artefact itself and not only here:** seven runs, one
task family, one operator, one judge model (`deepseek-v4-flash`, 450 of 450). `CORPUS_LIMITS` is
stored in the dataset header and printed above every report, because a caveat that lives only in a
doc is a caveat nobody reading the number sees. A prompt optimised against this corpus will overfit
it.

OPEN[judge-bench-covers-two-judges-of-four] failure triage LANDED 2026-08-20
(`looplab/judgebench/triage_corpus.py` + `triage_score.py`,
`tests/data/judge_bench/failure_triage.v1.jsonl.gz`, **122 rows, 118 labelled**) and it is smaller
than the span count suggested for a reason worth keeping: the unit that can carry a label is the
CLASSIFICATION, not the agentic decision, so it is enumerated from the durable event log and reaches
runs whose spans were pruned. What is still uncovered is the other two, and neither is a matter of
effort. The **repair critic** has **7 decisions in the whole corpus** and a bench on it would report
noise with a decimal point. The **novelty gate** can never be scored for correctness at all — the
idea it rejects is never run, so nothing on disk says whether it would have worked, and only
`score.py`'s consistency field can ever exist for it.
proof:absent:extract_critic@looplab/judgebench/__main__.py

OPEN[torch-oom-markers-miss-the-allocator-body]
proof:absent:PYTORCH_CUDA_ALLOC_CONF@looplab/engine/triage.py — `_TORCH_OOM_MARKERS` lists the
EXCEPTION NAMES (`torch.OutOfMemoryError`, `CUDA out of memory`) and none of the allocator's message
BODY, so a capture truncated past the exception line — the common shape when a chatty stage pushes
the diagnosis out of the clamp — is invisible to it. Measured on `failure_triage.v1`:
`_failure_reason` replayed at HEAD over the WIDE evidence gets **16 of 23** OOMs, and the **7 it
misses are exactly the rows whose surviving text is the body** ("Tried to allocate 8.79 GiB. GPU 0
has a total capacity of 139.80 GiB of which 4.59 GiB is free" / `PYTORCH_CUDA_ALLOC_CONF`). Over the
DURABLE 500-char tail it gets **0 of 23**, because not one of the 122 recorded tails carries a
marker at all — so the marker rule's whole win is the WINDOW it is handed, which is the argument for
the agentic diagnostician that reads the stage log rather than for a longer literal list.

**[2026-08-20 — the live arm was RUN, and the tools are not what made the difference.]** 450
provider calls, one per row, 0 errors, 799 s, `deepseek-v4-flash` at the runs' own temperature 0.6,
replaying each row's recorded prompt with `look_invitation` overridden to empty — byte-for-byte the
message the engine builds when `train_monitor_tools` is off. On the 285 outcome-labelled rows the
incumbent answered WITH tools, removing them costs **0.017 accuracy (0.782 → 0.765) and 5 of 51
true stops, and adds no false stop (1 → 1)**. The fidelity control — the 81 rows nobody ever had
tools for, replayed under conditions identical to how they were recorded — reproduces the error
rates (0.362 → 0.391, 4 → 4 false stops) while moving 41 % of the individual answers, so the model
is noisy per decision and stable in aggregate and a 0.017 gap is inside that noise. **The 0.420
headline gap is the RUNS**: v6/v7 hold **zero** decisions of the class this judge exists for (a
training that completes and produces a dead model, caught 48 of 53 elsewhere) and 100 % of their
wasted population is `stage_failed`, the class caught at 5.1 % with tools and 4.8 % without.
Standardised to the v6/v7 basis mix the with-tools slice scores **0.420** against 0.362 observed;
the reverse standardisation is not computable, because two of the four cells are empty on the v6/v7
side. **So: the tools are a real but small win, they were never the explanation, and the corpus
cannot identify a tools effect on accuracy at all.** The residue — a 14.8 % false-stop rate on v6/v7
productive decisions against 0.6 % elsewhere — survives the arm unchanged, so it is not the tools
either; it tracks the measured TRAJECTORY (0 of the 107 productive decisions carrying a trajectory
block were called `broken`), which is **not** measurable here, because the tracker postdates v6/v7
and there is no stored measurement to splice into their prompts.

**[2026-08-20 — the bench's own two anti-hand-edit guards were VACUOUS, both fixed.]** Found by an
adversarial audit, and they are the same shape as the other two vacuous greens found the same day
(the open-item index's 900-char proof window inheriting a neighbour's falsifier, 17 of 77 markers;
the claim-pin checker printing "0 claim defect(s)" over zero pins). (1)
`test_prompt_splits_round_trip_exactly` asserted `render_prompt(row["prompt"]) == messages_of(row)`,
and `messages_of` RETURNS `render_prompt(row["prompt"])` for a row with no stored `messages` — which
the assertion two lines above pins as all 450 of them. `f(x) == f(x)`, unfailable for any input the
corpus can contain. (2) `test_the_dataset_regenerates_from_the_runs_it_names` — the ONLY guard on
how the 278 KB committed artefact was DERIVED — always skipped: `runs/` is gitignored and
`LOOPLAB_BENCH_RUNS` is set nowhere, so it had never executed, and nothing in CI could tell a
derived corpus from a hand-edited one. Both replaced by guards that run everywhere and were verified
by MUTATION on a throwaway tree: reordering `render_prompt`'s blocks kills two tests, hard-coding
`prompt_split_exact = True` kills a third, flipping one recorded verdict kills four, and editing one
stored PROMPT — a field no label and no headline number reads — kills the derivation guard and the
sha tripwire. The regeneration test now runs and passes when pointed at the runs
(`LOOPLAB_BENCH_RUNS=runs`), which is the first time it has ever executed.

OPEN[judge-bench-cannot-see-a-post-exit-stage-failure] the missed-stop class is not a prompt
problem: all **20** uncaught wasted attempts are `stage_failed`, and of the 20.1 h an oracle could
have saved by stopping each at its first look, **13.4 h across 7 attempts is
`check_failed`/`expect_failed`** — the stage exited rc 0 and the ENGINE then failed it, over
artifacts the judged log never showed. What reaches that is EVIDENCE, not wording: the declared
`expect`/`assert` contract, in front of the judge while the stage still runs.
proof:absent:monitor_expect_context@looplab/engine/train_monitor.py

The other 6.6 h is 13 real crashes, 5 of them under six minutes after the look charged for missing
them. Both dead-model attempts were caught, at 31 of 33 and 17 of 20 decisions. **The checker half
of this shipped 2026-08-20** (`6eecd786`, the whole-log trajectory floor under
`loss_unchanged_from_first_step`), so an unknown share of the 13.4 h was a wrong checker rather than
a blind monitor; the corpus is not re-labelled against a rule that postdates its runs. **Read this
beside the failure bench's refutation above**: the same stage check that failed those 7 attempts called ten
converged trainings "no learning progress" off the last 4,000 characters of their logs, in a
different run. The monitor is charged for missing a verdict that is itself a 4,000-character read —
which does not rescue it (the compute was still discarded) but does say the remedy is the same one
at both layers, more evidence rather than better wording.

**[2026-08-20 — `fault` was measured for the first time, and the finding is that it is UNSTABLE.]**
It is unmeasurable from the RECORD, not unmeasurable: two live arms over the 88 rows where either
the incumbent or the tool-less replay said `broken`. Without the code, 51 of 79 `broken` answers say
`implementation` (65 %); with `monitor_code_tools` over the preserved workdir, 35 of 69 (51 %). Of
the **67 rows both arms call `broken`, reading the code moves `fault` on 34 — half** (14
`implementation` → `hypothesis`, 8 the other way), and the gated repair-stop volume halves from 39
decisions to 19. **So how many repair-stops a run pays for is a function of what the judge was
allowed to see, and the affordance shipped specifically to make this field reliable is what moves
half its answers.** Two limits: the workdir on disk is the post-repair state, so for a repaired node
the judge read code newer than the log; and neither arm is agentic the way the incumbent was. What
holds the line meanwhile is the same conjunct as everywhere else — through `--gate` the repair-stop's
own false-stop rate is 1 decision / 1 of 49 productive attempts in BOTH arms, because all four
`implementation` verdicts on productive runs are below the 0.8 bar.

OPEN[monitor-fault-has-no-outcome-label] `TrainingVerdict.fault` routes a stop to REPAIR instead of
a terminal, and nothing measures it. `recorded.fault` is `None` in 450 of 450 rows because the field
postdates every preserved run — the extractor already reads it, so the corpus repairs itself the
moment a run records one. What does NOT arrive with those rows is the LABEL: the outcome that says
whether `implementation` was right is what the REPAIR then did, which is the same shape the triage
bench needs (`node_repaired` + the next attempt's terminal) and is not the `wasted`/`productive`
rule this dataset has. A run must also actually reach the branch — `train_monitor_kill` on, a
`broken` at ≥ 0.8 confirmed twice, and `fault="implementation"` — which no preserved run did.
proof:absent:LABEL_REPAIRED@looplab/judgebench/judge_corpus.py

### §0.20 A goal sentence nobody could check killed three nodes, and the prompt telling five roles to use every GPU outlived the correction (2026-08-20)

Root cause pass over seven claim failures from the two days to 2026-08-20; the argument, the surface
inventory, the full agent-facing audit table and the options refused are in
`docs/45-claim-surfaces-2026-08-20.md`. The shape, in one sentence: **a fact recorded in ONE place
whose truth lives in ANOTHER, with nothing connecting them.**

**The measurement that decides the mechanism.** Four of the seven were FALSE ON THE DAY THEY WERE
WRITTEN — §0.1 rows 8 and 10 (closed 2026-08-14, the day the list was written), doc 27's eval-corpus
banner (both tests it calls missing predate the document), and `runs/e5small-dr-unified-v3`'s goal.
So an EXPIRY catches none of them: an expiry that has not elapsed is green, and a born-false claim
is green for its whole window. Age is the wrong primitive. What is actually missing is a
forcing function at AUTHORING time, because writing a claim costs nothing and checking one costs a
lookup.

**The cost.** That goal stated the manual e5-small recipe as "16k overall = 8k x 2 GPUs" and
labelled it VERIFIED. The row belongs to `sergeyzh/rubert-tiny-lite`; the file's own
`BASELINE — e5-small` block says batch 1750 / `n_gpus` 4 / `n_negatives` 0 -> recall@100 0.89. All
three of that run's nodes died of `torch.OutOfMemoryError` (recorded as `crash` — §the triage fix
`50ab168e`) chasing a per-device 8192 that needs ~530 GiB on a 139.8 GiB card.

**Two more, both live on master and both aimed at agents.** `core/hardware.py::operational_attention_points`
told FIVE roles to "use ALL available GPUs … don't run a tiny single-GPU job on a multi-GPU box" —
while `engine/resources.py::_resource_request_for_node` gives an undeclared footprint exactly ONE
device and `_resource_eval_env` fences `CUDA_VISIBLE_DEVICES` to it, and `nvidia-smi` (the tool the
same bullet recommends) reports the physical box either way. It contradicted, in the SAME prompt,
the two paragraphs that govern the declaration — which were corrected on 2026-08-19 under
`Settings.gpu_footprint_cue` while this copy, gated by nothing, outlived the correction. And
`engine/genesis.py` said `data` entries are "copied to ./<name>" eighteen lines above saying they
are "mounted (symlinked) …, never deep-copied" (`adapters/repo_task.py::DataSpec.mount` defaults
True), and instructed the goal author to "Put operational guidance the agent needs (use all GPUs, …)
in the task `goal`" — the upstream half of the whole defect, since Genesis WRITES goal text.

**Rule 1, adopted from the operator: a constraint of the MACHINE is discovered by the thing that
runs on it, never asserted in prose an agent reads.** A memory ceiling is a fact about (model,
sequence length, `n_negatives`, card) and no typed number survives a change to any of the four. This
repo already applies the rule to TIME — `proposal_cues.py::_cue_experiment_time_budget` asks for a
short probe when per-step time is unknown — and did not to MEMORY. Both cues now carry the twin.
`tools/dev_probe.py` cannot host it (rule 4, `CUDA_VISIBLE_DEVICES=""`, deliberately: the host GPU
lease is one file per OS user and a probe on a device corrupts a SIBLING node's training); the lever
is a short calibration step at the head of the node's own declared pipeline.

**Rule 2, the guard**: `CLAIM[<slug>] … decided:<predicate>` over `looplab/core/claimpin.py`, with
`tests/test_open_item_index.py` refactored onto the SAME evaluator (§0.8's four-implementations
lesson). Its zero-adoption-cost half re-derives all 653 `<mod>.py::<symbol>` citations in `looplab/`
and refuses the `<mod>.py:NNN` form outright; it found 4 dead symbol citations and 6 live line ones,
all fixed. `docs/guide/concepts.md`'s inline-repair list enumerated ELEVEN reasons under a sentence
saying twelve, and the enumeration is now derived from `FAILURE_REASONS`.

OPEN[claim-legacy-prompt-branches] Both pre-correction GPU paragraphs still ship as the
`gpu_footprint_cue=false` branch AND as what an UNSTAMPED role gets — a bare `LLMResearcher` in a
library caller reads "declaring MORE than the ceiling does not get this experiment more hardware",
which the scheduler contradicts. The engine path always stamps, so no run gets it; the byte-for-byte
restoration is deliberate. What is missing is a decision about whether a false sentence may be the
off-switch's value at all. proof:present:SERIALISES@looplab/agents/roles.py

OPEN[claim-effective-batch-event] `auto_find_batch_size` is refused as the memory answer on a
measurement (transformers 4.51.0 keeps the DECLARED `per_device_train_batch_size` on `args` and the
reduced one only in `trainer_state.json` + a `logger.debug`), so a run would report a batch it never
trained at. It becomes admissible the moment the EFFECTIVE batch is lifted into a durable LoopLab
event — an `extra_metrics`-shaped problem, since the number comes off the candidate's own process.
proof:absent:effective_train_batch@looplab/events/types.py

OPEN[claim-ui-line-citations] Four dead `file.py:NNN` citations survive in `ui/src/` —
`cardBoardModel.js` cites `core/models.py:349` (that line is `return int(v.strip())`) and
`cards.py:809` (blank), and `CardBoard.jsx` cites `card_ledger.py:1757`. `citation_defects()` scans
`looplab/` only; widening it to `ui/` is one argument away, and the fix is to locate by SYMBOL.
proof:present:cards.py:809@ui/src/cardBoardModel.js

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
- ✅ **P1 · B7 claim ratification vs trust flags (S). [added 2026-08-14; the row below is STALE —
  the fix landed 2026-08-15, the day after it was written]** D8 memo-claim
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
  **[2026-08-19 — ALREADY DONE; this row's "0 grep hits" is what went stale.**
  `trust/memo_verify.py::finalize_verified_evidence` calls `engine/memory.py::unreliable_metric_ids`
  (function-local, because `engine` imports `trust` and the module edge would be a cycle) and
  refuses with *"verification evidence rests on a node whose metric this run refuses to select on"*
  — a DISTINCT refusal from the lifecycle one, because the remedies differ. It fails CLOSED on an
  unreadable state, which is the opposite containment from the predicate's own and is argued in
  place. `tests/test_memo_verify_evidence_trust.py` drives it; green. The row was written on
  2026-08-14 and the fix landed 2026-08-15, so nothing was missed — only the checklist was.]
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

  **[2026-08-20 — RETRACTED, AND THE SCOPE NOTE ABOVE IS WHY. This row's "DONE" was true of the code
  and false of the corpus: `data_provenance` fires ZERO times.** Counted over every run directory
  under `runs/` that has an event log — `e5small-dr-unified-v2` (4,691 records), `-v3` (2,309), the
  live `-v4` (133), `rubertlite-dense-retrieval` (1,624), `rubertlite-dr-unified-v6` (2,539), `-v7`
  (2,456), `-v8` (6,415), `-v9` (3,330): **0 occurrences in all eight.** The emission is gated on
  `if prov:` over `self._assets` (`engine/orchestrator.py:3229-3236`, not `:3140-3147` — this row's
  line numbers had also drifted, as had `replay.py:1552`→`:1675` and `models.py:1148`→`:1453`), and
  `adapters/repo_task.py:1261-1262::assets()` returns `{}` for every repo task with the comment
  *"repo/data are tree-mounted, not flat assets"*. So the ONE mechanism aimed at input content has
  never covered the tasks this box actually runs. The scope note said so and the ✅ did not, which is
  how a shipped-and-inert mechanism survives a re-derivation: **an emission site is not a record.**

  The claimed fallback does not exist either. `engine/workspace.py::workspace_fingerprint` records a
  git HEAD SHA for an editable repo and a `(relpath, size, mtime)` shallow signature for a `data:`
  mount — deliberately NOT content — and the three dense-retrieval runs declare `data: {}`, so they
  get neither. Their entire input record is `{"editable:.": "git:d97be313…"}`: the SHA of the code
  tree, which says nothing about a dataset the tree does not track.

  **Superseded by `eval.inputs` + `engine/comparability.py` (2026-08-20)** — the input side bound by
  CONTENT at the metric read, at absolute paths outside the workdir, which is where the data actually
  lives. See the ✅ row below.]

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
- ✅ **P1 · F6 fork-to-branch from any checkpoint (M).** Fuse time-travel + `inject_node` + reopen into
  one "branch from this seq with edited idea" gesture (top verified steering UX).
  **[2026-08-14 — the gesture and its panel both landed; see §1 survivor #11 for the shape chosen
  (`inject_node` + a server-stamped `forked_from` receipt, no new control event) and the four wiring
  steps, all four done. The ⬜ and the "STILL OPEN" note this row carried until 2026-08-19 were
  themselves stale by five days.**
  **Not to be confused with docs/29's F6**, which is the conversation-trace episode seek that landed
  2026-08-13 (`events/traceview.py::node_episodes`, `/nodes/{n}/episodes`, `?before=` via
  `events/span_index.py::_anchored`) — different namespace, different item.]
  **[2026-08-19 — DONE, and the closing half was PROVENANCE, which the row's own framing had left
  implicit: a branched node's idea is part operator-authored and part inherited, and until today
  nothing could tell a reader which.** Three things were wrong, all on the READ side of a record
  that was itself sound. (1) `changed_fields` was being treated as "what the operator changed" when
  it is a raw diff of two `Idea`s, and a branch differs from its parent for two unrelated reasons —
  an edit, and the gesture deliberately not carrying `card_id`/`hypothesis`/`footprint`/`theme`/the
  concept envelope across. Measured: two edits produce a three-field diff on the toy run and eight
  against a Researcher-built parent. `_normalize_fork_receipt` now also stamps `authored_fields` and
  `not_carried_fields` (refusing a client that supplies either, same rule as the other two), because
  only the server holds both ideas — the node's own idea drifts after intake when the Developer
  finalizes a `footprint`, and the parent may since have been reset out of the fold. (2) NOTHING in
  the browser read `Node.forked_from` at all: the DAG drew chips for a cross-run `origin` and a
  `research_origin` and none for an operator branch, and the Inspector rendered an inherited
  rationale under a bare "Rationale" heading — i.e. as this experiment's own justification. New pure
  model `ui/src/forkProvenance.js` (+ `Dag.jsx` chip, `Inspector.jsx` block and attributed idea
  headings), with the `stamped`/`legacy`/`unrecorded` ladder so a pre-split receipt degrades to "not
  recorded" rather than to "the operator changed nothing". Inherited is the complement of the DIFF
  and never of `authored_fields`, and is attributed to the PARENT NODE rather than to "the
  Researcher" — a branch can be taken from another branch. (3) `/api/runs/{id}/prov`, the one
  surface whose whole job is provenance, associated every activity with the engine's
  `prov:SoftwareAgent`; a branched node's activity now carries `agent:operator` (`prov:Person`,
  `ll:idea-author`) beside it and the split travels with it. Two `OPEN[…]` markers in the fork files
  closed on the way: `fork-draft-phantom-rationale-edit` (an untouched form submitted as edited and
  the receipt stamped `rationale` as an operator change — the exact field the new readers now
  display) and `fork-editable-fields-dead-export`, both resolved by `forkDraftIdea` owning the
  draft's shape. Driven by `tests/test_fork_from_seq.py` (+3), `ui/test/forkProvenance.test.js` (9)
  and `ui/test/forkFromSeqModel.test.js` (+3); docs in `docs/guide/concepts.md`
  §"Branching from a snapshot".]**
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
  **[2026-08-19 — THE FLIP IS NOT THE NEXT STEP; THE MEASUREMENT IS, and it now exists.**
  Two things are wrong with flipping on this row's evidence. The ~20 % vs ~92-94 % is a DIFFERENT
  deployment's benchmark, and it predates **H1**, which shipped `guided_json` — the endpoint-side
  constrained decoding that repairs precisely the native-FC weakness the number describes. And the
  fallback was **unobservable**: `parse_structured` returns a validated object whichever parser
  answered, so a native collapse rescued by a SECOND provider call left no span, no counter and no
  event. Nobody could say what our own endpoints do, which makes a global default touching every
  model call in the system a coin flip rather than a decision.
  So `core/tracing.py::structured_parse` now opens one observation per structured ask carrying
  `parser_used`, `attempts`, `repaired` and `failed_<parser>`, and `looplab parser-stats RUN_DIR`
  tallies it PER PHASE — a Researcher ask and a Developer ask hit different models and one run-wide
  number would average them into something no operator can act on. `repaired` is deliberately its
  own fact: a `tool_call` that only validated after schema-aligned coercion is a native call that
  nearly collapsed, and counting it as a clean first-try win would hide the exact signal the flip
  needs. Untraced callers are byte-for-byte unchanged (the observation no-ops), which a test pins.
  NEXT: let a run record under this, read `parser-stats`, and flip — or decline — on our numbers.]
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

## The training watchdog's kill path was unreachable for every real pipeline

  `should_monitor_kill` needs `LOG_ROLE_TRAINING`, and `eval_log_plan` grants that only to a log
  that is provably the WHOLE eval — a single-command eval, or a one-stage pipeline. Every real
  task on this box resolves a multi-stage pipeline (`mine -> train -> score`), so every stage of
  it is `LOG_ROLE_WORK`: judged, alerted, narrated, advisory. The narrowing was correct about the
  danger it names (a `data_prep` stage printing a flat `loss: 0.6931` drew `broken` at 0.9 and
  armed the gate) and unbounded in reach: the early stop had never once been reachable on a real
  run, and nothing in the record said so.

  **[2026-08-18 — MEASURED, then FIXED.** `e5small-dr-unified-v2` node 2: 31 `train_monitor_alert`
  rows with `status: broken` at confidence 0.85-0.95 over 7.3 hours — *"Loss descended to ~5.04
  then jumped to 8.8534 and stayed perfectly flat (iqr=0, min=max=8.8534) for the final 4+ hours,
  grad_norm collapsed to ~0.0002"* — the node ran all 57,600 steps and scored
  `RECALL@100: 0.000000`, every nDCG and MRR at every k also exactly 0.00 (representation
  collapse: a constant embedding retrieves nothing). Node 4 repeated it live, 17 broken rows in
  3.1 hours at a loss pinned to 4.88. The other four conjuncts all cleared — `train_monitor_kill`
  on, threshold 0.8, confidence to 0.95, streak far past 2, and the stamped trajectory reads
  `direction: rising`, which `trajectory_vetoes_kill` never refuses on. The role was the whole of
  it, on all 46 alerts of the run: `('work', 'broken'): 31`, and never once `training`.

  THE FIX is a declaration, not a better guess. A stage manifest may carry `role: "training"` on
  exactly one stage, validated by `command_eval.validate_stages` — the single definition of a
  valid stage, so the three declaring sites cannot drift — and `eval_log_plan` grants that stage
  `LOG_ROLE_TRAINING`. A stage NAME proves nothing and still proves nothing; what makes a
  declaration admissible is that it can only ever be spent in ONE direction. A kill carries no
  repair, no retry and no refunded `max_nodes` slot, so the only thing `role: "training"` buys its
  declarer is the power to have its own stage stopped; omit it and the stage keeps precisely the
  advisory role it has today. There is no spelling of the key that makes a stage LESS killable.

  REJECTED: admitting `LOG_ROLE_WORK` to the kill set whenever the measured trajectory
  corroborates (here, frozen at iqr=0 with a collapsed grad_norm). It fails on the original worked
  example — a `data_prep` stage printing a flat `loss: 0.6931` IS a frozen curve, so the
  corroboration fires hardest on exactly the false positive it was meant to filter — and it would
  promote the deterministic half from VETO to CONFIRM, a widening of authority over text the
  candidate's own script wrote (docs/36: a wider action space, never a wider trusted set).

  THE PRICE, paid in the same currency: this run's `train` stage does not only train. Its log ends
  with the retrieval evaluation it runs in-process (`RECALL@100: 0.793344` is a line in
  `train.log`), which is the H-1 defect — a judge holding kill authority reading scorer output —
  moved INSIDE one stage, where no plan can split it by filename. So the authority is SPENT the
  moment the stage's declared `expect.files` artifact exists (`training_authority_spent`): after
  that the role falls back to `LOG_ROLE_WORK` and the verdict is advisory again. That is the
  manifest's own output contract and an exact filesystem fact, not a reading of the text, and it
  fails closed — a stat that cannot be taken counts as spent.

  AND THE RECORD. When the role is what refused an otherwise-complete kill, the alert row now
  carries `kill_role_withheld` (additive, fold-ignored, mirroring the existing `trajectory_veto`
  counterfactual). 31 rows over 7.3 hours read as ordinary `broken` verdicts, indistinguishable
  from ones the gate had simply not confirmed yet; that is why the unreachability survived three
  runs. Driven in `tests/test_watchdog_declared_training_stage.py` against the real tails — the
  collapsed node becomes stoppable, v7 node 1's descending curve is still vetoed even when
  declared, an undeclared pipeline is byte-for-byte what it was, and the positional scorer can
  never buy the role.]

  **[2026-08-18 — TEETH, because a permission nobody exercises is worth nothing.** The declaration
  makes the kill REACHABLE; it does not make it happen. Three things now carry it: the STAGES phase
  prompt asks for it directly, naming what it costs to omit (a node that ran all 57,600 steps after
  its loss froze while the watchdog said `broken` thirty-one times); the `declare_stages` schema
  describes the key where a model reliably reads a field's shape, in both the authoring spec and
  the repair tool; and every `train_monitor` span of a pipeline that declared nothing carries
  `kill_reachable: false` from its FIRST tick — the role gate is a property of the resolved
  pipeline, not of the run's health, so "nothing here can be stopped" is knowable before any
  verdict exists rather than after the hours it takes for one to matter. Also raised a rung on the
  ladder that this change broke: `test_config.py::test_watchdog_ticks_do_not_share_the_thread_pool`
  pinned the dedicated watch pool by looking 200 characters past one call marker, and moving the
  log read two lines down turned it RED for a property that never changed. It is now AST and TOTAL
  over both monitor loops — a threaded call either draws on the pool or is the deliberately
  un-abandonable provider call — and the mutation that removes the limiter names the offending
  call instead of printing a window of source.]

## A watchdog that can only condemn cannot tell a bug from a bad idea

  **[2026-08-18 — the question underneath the role gate.** The gate answered "may this stage be
  stopped". It could not answer *stopped as what*. `monitor_broken` is not in `FAILURE_REASONS`,
  so a kill was terminal — no repair, no retry, no refunded slot — while the deterministic diverge
  watchdog one layer down fails a stage with `diverged`, which IS repairable and on this very run
  bought node 5 a Developer repair. Same illness, two outcomes: a NON-finite loss read as
  "probably a bug, go fix it", a loss frozen at a perfectly finite 8.8534 read as "the idea
  failed". And node 2 was not even stopped — it ran to completion and recorded **metric 0.0 as the
  result of its hypothesis**, which is the worst version of the same error: a verdict about an idea
  whose implementation was never checked, handed to the Strategist as evidence.

  `TrainingVerdict` now carries `fault` — implementation / hypothesis / environment / unknown, with
  `unknown` named in the schema as the safe answer and the two costs stated ("a hypothesis wrongly
  called a bug costs a repair round; a bug wrongly called a hypothesis records a verdict about an
  idea that was never actually tested"). `should_monitor_repair` stops the stage with
  `MONITOR_REPAIR_REASON = "not_learning"`, which is in `FAILURE_REASONS`, therefore in the default
  `inline_repair_reasons`, therefore picked up by the inline repair loop with no new machinery: the
  only plumbing is `_evaluate`'s watchdog branch falling through instead of returning when the
  reason is one the operator's setting selects. `cancel` and `kill_signal` are built per ATTEMPT
  inside that loop, so the retry starts from a clean signal, and the repair critic, the attempt cap
  and the redone-work floor bound it exactly as they bound a crash. A `hypothesis` verdict still
  terminalizes — a sound implementation of a bad idea is a real finding and must not be repaired
  away.

  `not_learning` is its OWN word for the reason `oom` and `diverged` are. Three directives, three
  different fixes: "reduce memory", "stabilise the numerics", "make the objective able to descend"
  — and `diverged`'s first move (lower the learning rate) is if anything backwards for a model that
  is not learning at all. Its directive names the mechanical causes a frozen loss usually has (a
  reduction over the wrong axis, inconsistent normalization between towers, a temperature that
  makes every pair identical, a loader yielding one batch, a schedule that drove the LR to ~0, a
  regulariser whose minimum is a constant embedding) and ends by telling the Developer to say so
  plainly if it concludes the code is right — a real negative result is worth more than a repair
  that hides it.

  **AND THIS IS WHY THE ROLE GATE CAN OPEN.** `should_monitor_repair` admits every JUDGED role,
  `LOG_ROLE_WORK` included. What made the role necessary was that the only available action was
  terminal; the cost of being wrong about a repair-stop is one restart of a run the judge has
  already called wasted. A `mine` stage feeding empty negatives, a post-train stage exporting a
  broken checkpoint and a five-stage pipeline's third stage are all things the code can be wrong
  about, and refusing to act on them was never a judgement that they are healthy. The arithmetic
  conjuncts are unchanged — same confidence bar, same repeated-verdict requirement, same measured
  trajectory veto — `_NON_TRAINING_ROLES` still cannot reach it because they are never judged at
  all, and the terminal kill keeps the narrow gate it had. The `fault` lands on the durable alert
  row whether or not it led anywhere, because "the code is wrong" and "the idea is wrong" are what
  the search must tell apart afterwards.

  STILL OPEN: the judge attributes the fault from the LOG alone. It can already read the whole log
  and search it for a traceback (`_LOOK_INVITATION`, `read_log`, `metric_series`), and a run's own
  log is where its parameters, its device and its data shapes are echoed — but it cannot read the
  CODE. Giving it a bounded read of the node's own authored files would make `implementation` a
  much better-evidenced answer than it is today. The fence question is answerable (the node
  workspace is the one region that provably holds only what this node produced, which
  `monitor_log_sources` already relies on), and the direction of harm is favourable — reading the
  candidate's code can only ever route a stop toward the cheap action.]

  **[2026-08-18 — CLOSED, same day. The judge reads the code.** `monitor_code_tools` gives the live
  judge the same read-only scouts every other agent has — `read_file` / `grep` / `find_files` /
  `list_dir` via `RepoScoutTools` — composed with the log tools by `monitor_tools` and rooted at
  the NODE WORKDIR. The root is the whole argument, twice over: it is the code that is ACTUALLY
  RUNNING (the Developer's scouts are rooted at the editable SOURCE, which is a different
  filesystem — the distinction that cost `runs/rubert-dr-0807` node 2 a repair loop with no move
  left), and it is the one region that provably holds only this node's own product, which
  `monitor_log_sources` already relies on and `read_allowlist`/`read_fence` already grant. Reused
  rather than re-derived because `RepoScoutTools` is already the right shape for this mount:
  path-safe, secret-filtered, bounded per page and per walk, and already skipping the gigabyte
  directories a trainer workdir carries (`ckpt`, `checkpoints`, `wandb`, `lightning_logs`), which
  on geesefs is the difference between a grep and a stall. `_MONITOR_LOOK_TURNS` 6 -> 9: a fault
  attribution costs a `grep` plus a `read_file` on top of the curve work, and a budget that forces
  the judge to choose between looking at the curve and looking at the code produces exactly the
  guess the attribution exists to replace. Driven in `tests/test_watchdog_declared_training_stage.py`
  section G — a symbol the log never mentions is findable, three escape shapes are refused, both
  halves fail closed, the two name sets are disjoint (pinned as disjointness, not as an
  unfalsifiable ordering claim), and the OFF path is the historical one-shot call with the
  invitation absent.]

## The 2026-08-18 review annotations, worked

  36 findings arrived as in-code `REVIEW 2026-08-18` annotations, each with driven evidence and a
  fix direction. Worked in order of what they touch: the judge's own evidence first, then the
  repair path the fault-routing change had just widened.

  **[log_tools — four, all about a RESUMED sweep.** Everything a resumed sweep reports is relative
  to something, and four of those relations were wrong. (1) Hit numbers were minted from the resume
  point while `_record_range_scan` counts from this attempt's FLOOR, so the number the receipt tells
  the caller to spend addressed a DIFFERENT record — two numberings for one log, the spent-remedy
  defect the module was rewritten to remove, one mode over. `_records_before` re-walks the prefix to
  seed the count: split-only, no caller pattern, which is the expensive half and the whole reason a
  resume exists; a record straddling the resume point is counted in the prefix and skipped by the
  sweep, so once either way. (2) The first record after a resume was dropped as torn, though every
  byte a receipt hands back is a record BOUNDARY — a match in the record starting at a ceiling stop
  was counted by NEITHER sweep while the resumed one reported reaching the end of the log; one
  1-byte read answers it, and a resume INSIDE a record is still torn. (3) A deadline in the first
  batch made `hi == lo`, so "continue with from_byte=<lo>" named the call just made; withholding
  the byte would break the never-skip rule it exists for, so it is given AND labelled. (4) `of
  {seen}` was suffixed "+" only for the satisfied stop, so a range cut short by the ceiling read
  "records 40-45 of 45" on a 100-record log — three false claims in one line.]

  **[repair_verify — two, both flippable by an adversarial one-liner.** The `.` in `_CLAUSE_ENDS`
  also matches a decimal point, so a citation clause quoting a value ended INSIDE the number and
  every cited token after it fell outside the span: "Node 1 used lr 0.5 and nll_cos throughout its
  training" was charged with promising `nll_cos`, the exact false positive the rung shipped to
  remove. `_clause_end_at` exempts digit-dot-digit only — a trailing `2.` still ends the clause, or
  one sentence about another node would swallow the paragraph after it. And `_assigned_numeric_paths`
  ordered by `(lineno, col)`, which let DEAD code win: a `def _unused(): cfg.a.b = <declared value>`
  placed anywhere acquitted a real module-level divergence, and a nested default convicted an
  agreeing module body as `params_overridden` on the run's best number. Scope DEPTH is now computed
  and the sort runs deepest-first so the module body — the code that certainly executes — is written
  last; within one depth the textual rule is unchanged.]

  **[evaluate — the repair license was priced under the wrong declaration.** `chain_seconds`
  accumulates wall-clock earned under every manifest the chain has run, while the pipeline cost was
  re-resolved from the CURRENT one — so a repair that legitimately SHRINKS its declared stage
  timeouts (right-sizing after an epoch cut) retroactively re-priced seconds that were inside the
  license when they were spent, leaving the fix exactly ONE eval and, if it failed for any reason, a
  terminal charging old-declaration work at the new rate. The floor is now fed the chain's largest
  declaration. It cannot be gamed upward past the operator's own number — `stage_budget_refusal`
  already refuses a declaration above `eval_spec_time_budget` — and the residual is stated: the
  high-water mark is per-PROCESS, so a resumed chain is priced exactly as it was before and only a
  live one is priced honestly.]

  **[2026-08-18 — three more, worked.**
  *One join, three populations.* The memo verdict join is POSITIONAL, so every reader has to
  enumerate what the writer enumerated. `_check_claims` emits one row per claim, dict-coercing a
  non-dict and filtering nothing, and `sanitize_research_memo_payload` keeps a whitespace-only
  statement verbatim — so `memo_verification_view` (which dropped blanks after strip) and
  `run_tools` (which dropped them on a TRUTHY test, keeping `" "`) each shifted the join by one per
  blank above. Both real claims came back `unverified` with "verification alignment mismatch",
  their true verdicts were counted as unmatched rows, that false tally went into `verdict_tally` /
  `memo_verdict_cue` prompts, and the rendered memo tagged "A" with "B"'s verdict. The view now
  enumerates the writer's population — same coercion, same `MAX_RESEARCH_CLAIMS` cap — and blankness
  is a DISPLAY concern applied after pairing; `run_tools` uses the view's own rule and cap.
  *An allow-list that failed OPEN.* `_live_lifecycle_digests` swallowed an OSError per entry, and a
  missing digest does not read as "unknown" there — it reads as "that run is gone". One live run's
  `resolve()` raising ESTALE on geesefs therefore made the plan call its lock "fences no surviving
  run directory and is cold" (a lock's mtime is its CREATION time, so any day-old run is cold), and
  `--apply` would unlink a lock a live engine holds via flock — the per-inode fresh-lock race this
  module exists to prevent, driven. It now returns `(digests, blind)` and every lifecycle-lock entry
  is a KEEP with a stated reason when the walk was incomplete, mirroring `_age_ok` one screen up.
  *An epoch that plateaued on the legacy shape.* The repair epoch took `max(repairs, attempt)`,
  which is idempotent — and blind to the per-process ordinal restart the engine used before
  `_durable_repair_ledger`: a real eight-repair chain reads [1,2,1,2,1,2,1,2] and ended at 2, so the
  last failed stage row carried the CURRENT epoch and `stage_row_superseded` called a stale row
  live. Idempotence moved onto the SEQ (`charged_repair_seqs`, the same shape as the ledger's other
  de-dup sets), which is what a re-folded row really shares; two identical rows at different seqs
  ARE the legacy shape and must both count, and `EventStore.append` mints seq from the tail and
  never retries, so a duplicate at a new seq is not a shape this event can have. A
  `salvage_cause_fix` row still charges nothing.]

  **[2026-08-18 — three more, one of them a correction to the finding.**
  *A claim nothing could settle.* `WatchService._wake` moved a record `armed -> waking` and every
  settling write after it goes through `atomic_write_text`, which has no containment — so a
  transient OSError on the geesefs mount was swallowed by `_loop`'s per-tick containment and left
  the record `waking` FOREVER: `due()` returns only `armed` records, `claimed_at` is written and
  read nowhere, and nothing reclaims it. Silently dead monitoring that still counts against the
  session's active cap until the server restarts. The claimed half is now its own method under one
  settling guard: an unplanned escape re-arms the watch the way `WatchDeferred` does (one short
  interval out, no wake-up counted, `attempts` untouched — an escape here is evidence about the
  STORE, not about the condition) and is then re-raised. `_unclaim` never raises, because losing
  the original escape to a second one would hide the cause.
  *A comment that was false.* `Card.discarded_nodes` promised disjointness from `evidence` "by
  construction", and `_apply_unexecuted_discards` only empties `evidence` when the single discard IS
  the whole evidence set — a mixed set deliberately keeps it (so `gated` stays unreachable) and the
  two-discard retirement keeps both. Restated: disjoint only in that one shape, and a reader wanting
  "evidence that actually ran" must SUBTRACT rather than assume the subtraction was done.
  *The mlflow channel tag — and a CORRECTION.* The tag was published on `v is not None` while the
  `log_metric` beside it has its own containment, so provenance could name a metric absent from the
  run's table. The gate is right and costs nothing, but the finding's premise does not hold end to
  end: through the FOLD the state is unreachable — `node_evaluated` with a non-numeric extra metric
  folds that key away, so `float()` never raises in production and the containment is defensive
  only. The first test written for it PASSED on the pre-fix code, which is the tell; it now drives
  the branch directly and a second test pins the fold as the rung that really protects the reader.]

  **[2026-08-18 — the two maintenance commands whose EXIT CODE lied.** `--json` exists for a
  scripted caller and a scripted caller reads the exit code, so both of these were worse than a
  missing feature. `memory-orphans --json` emitted `{"available": false, ...}` and exited 0 for a
  missing or mistyped store while the human path exited 1 — a health check keyed on the code read a
  misconfigured cross-run store as healthy; the availability check now runs FIRST in both modes and
  the JSON body is still emitted, because a machine caller needs the reason and not just the code.
  `reap-service-files` had the same shape one layer down: `plan_service_file_reap` catches the
  `iterdir` OSError and reports "0 service files", so a mistyped runs root read exactly like a clean
  one, exit 0, in both modes — checked at the command rather than inside the planner, which is also
  used on a live tree where a partially-readable root is a legitimate and separately-reported state.
  And BOTH silently ignored `--json` on `--apply` — the one invocation of each that writes or
  deletes — so a caller that asked for JSON got neither the plan nor a receipt; both now emit one.
  The `conventions` finding beside them is closed by amending CLAUDE.md's cli row rather than moving
  the command: `memory_cmds` is named, and the governance sentence now says what it actually means
  (money on a steward, or authoring cross-run memory CONTENT) with `memory-orphans` — which only
  ever REMOVES rows whose run is gone — stated as the deliberate exception.]

  **[2026-08-18 — the two remaining correctness findings.**
  *A seam a refactor quietly stopped honouring.* `link_input` falls back to a COPY when `os.symlink`
  fails — on geesefs, where symlinks flatten, not a hypothetical path. Before the rule was extracted
  out of `workspace.py` that fallback read `self.copy_input(...)`, so an override or monkeypatch of
  it covered both branches; as a module-level call it still intercepted the `mount:false` copy in
  `seed_workspace` and silently stopped reaching this one — "patch resolves but reaches nothing",
  invisible until a symlink actually fails at runtime. `link_input(src, dst, copy_fallback=None)`
  restores the dispatch, `WorkspaceSeeder.link_input` passes `self.copy_input`, and a caller that
  passes nothing keeps the module function.
  *A snapshot cursor a LIVE node invalidated on every walk.* The cursor was minted from the newest
  band's `anchor`, which is its operation span only once that span has FLUSHED; an OPEN band falls
  back to its latest CONTENT span, which moves on every append and then becomes the op span id when
  the band closes. Either way the cursor echoed back on page 2 matched no band, `next(...)` raised,
  and backward paging 409ed with `trace_episode_cursor_unknown` — on exactly the live node the
  feature was built for. Cursors are now minted from `band` (the band span's own id, which does not
  depend on anything having flushed) and matched by `_cursor_matches` against BOTH spellings, so a
  cursor an older client still holds keeps working instead of becoming a 409 on the next click. An
  unplaceable cursor is still refused — the fix widens what MATCHES, never what is accepted.]

  **[2026-08-19 — the last ten: efficiency, reuse, simplification, dead code, altitude.** Nothing
  here changes what the engine decides; two of the ten changed what a REFUSAL says, and both are
  stated at their call sites.
  *One canonical JSON, three more callers.* `tools/_base.py::capability_manifest` and
  `tools/dev_commands.py::_canonical_digest` each re-spelled `core/jsonutil`'s four strict options to
  mint a durable identity. The manifest one is a pure reuse (`capability_manifest_sha256` is
  byte-identical) whose only visible change is that an unencodable spec now raises the contract's
  `ValueError` naming the value instead of a bare `TypeError` out of the router constructor. The
  dev-command one was the hazard `_lenient_json_bytes` was renamed for: it added `default=str` and
  called the result CANONICAL, so `Path('/x')` and `'/x'` — two different operator-pinned commands —
  minted ONE `policy_sha256`; a value with no canonical form now yields NO digest.
  *One digest for one claim.* `lessons_distill`'s candidate receipt (`source_sha256`) and
  `write_auto_skill`'s card frontmatter (`source_statement_sha256`) hashed the same statement twice,
  in two files, with nothing comparing them — the audit join from "this run considered this belief"
  to "…and here is the card" would have broken silently on a cap added to either side.
  `memory.skill_source_digest` is the one rule; the empty-statement case stays each caller's own and
  says why. Driven by MOVING the rule, because two equal digests is what the defect looked like too.
  *One ordering for one candidate.* `tools/dev_commands.py::_materialize` was a second hand-written
  copy of `WorkspaceSeeder.seed_workspace`'s ORCHESTRATION — the shadow guard, the protected files
  BETWEEN it and the mounts, the mount/copy split — and the ordering IS the safety argument, so the
  next fix to it would have landed in one body. `workspace_seed.seed_candidate_workspace` now owns
  the sequence and returns receipt ROWS; the engine keeps its span and its `workspace_seeded` event,
  the tool keeps its Docker bind list (read off the RESULT, since `link_input` may have fallen back
  to a copy) and its staged overlay, which the engine must not have. `SeedOps` leaves an unset
  primitive UNSET rather than defaulting to the function object — a dataclass default captured
  `seed_repo_tree` at import and made `monkeypatch.setattr(workspace_seed, …)` resolve and reach
  nothing, the same "patch resolves but reaches nothing" shape `link_input`'s docstring records, and
  the existing test caught it the moment the bundle was introduced.
  *Two costs on the instrument path.* `stage_input_key` ran the whole-workdir sha256 walk BEFORE the
  `unresolved_entry` clause, so the wrapper shape that clause exists to refuse (`sh -c "python
  mine.py"`) paid multiple seconds per stage per attempt to be refused unconditionally afterwards;
  the reachable-only `exists()` probe is now first, and when both would refuse the recorded reason
  is the one that names something fixable. And `workdir_content` now takes a caller-owned
  `{rel -> (file_identity, digest)}` memo that `_stage_key_fn` scopes to ONE eval attempt, so the N
  stages of a pipeline re-`lstat` the tree but re-hash only what moved — each stage still keyed at
  its own instant, the memo bound to the same stat tuple `reuse_refusal` trusts.
  *Two named rules where there were literals, and one deletion.* The wake-up observation window is
  `_MAX_OBSERVATION_CHARS`, spelled once instead of in both `wakeup_instruction` branches and a
  route docstring; `WatchStore.due` gained a second memo so an ARMED watch backed off at the 60 s
  ceiling is `lstat`ed rather than read, parsed and fully re-validated 30 times a minute (the stat
  is taken BEFORE the read, which is what keeps a re-armed record from being skipped); and
  `core/models.py::superseded_stage_rows` — a list wrapper with zero production callers, since the
  stage strip is rendered by the browser and `routers/reviews.py` hands the client `stages` +
  `repairs` on purpose — is gone, with the ROW predicate kept and a negative pin so it stays gone.
  *Two findings NOT taken, both measured:* the `run_dev_command` seed cache and the watch
  scheduler's endless tick — §0.15 holds the numbers and what each would have to prove first.]

### §0.18 The Agentless recipe was measured against our own failures, and the corpus refuses it (2026-08-19)

**THE ASK.** §0.1 row 5 (Theme C, C5) proposes making an *agentless* Developer the DEFAULT for repo
tasks — `localize → generate-N → validate`, on the Agentless paper's SWE-bench Lite result (32 % at
$0.70). The row's own instruction is that the paper's number is from a different task shape and must
be re-argued against ours before anything is built. This is that re-derivation, over every event log
in `runs/` for the five real repo runs (`rubertlite-dr-unified-v6/v7/v8/v9`, `e5small-dr-unified-v2`;
`e5small-dr-unified-v3` was live and is excluded, `rubertlite-dense-retrieval` is the pre-repo
dataset adapter and carries no `reason`/`verified` fields). Every one of the five ran
`developer_backend: "default"` with `best_of_n: 1` — the pure agentic loop, no wrapper.

**WHAT THE AGENTIC LOOP ACTUALLY COSTS US.** 41 nodes reached an evaluation; **25 of them (61 %)
failed their first eval** and needed at least one inline repair — 5/7, 2/3, 6/14, 5/7 and 7/10 across
the five runs. Repair rounds per node: 16 nodes 0, 8 nodes 1, 7 nodes 2, 6 nodes 3, 2 nodes 4, 2
nodes 5 — mean 1.41 over all nodes, 2.32 over the repaired ones. **9 of the 33 consecutive repair
pairs re-hit the SAME failure class** (27 %), 4 repairs were `inert` (changed no file at all) and 10
were `unmet` (the tree does not contain the change the repair's own rationale claimed). Stage wall
clock: 220.2 h total, of which **77.2 h (35.1 %) went to stages that failed**, plus **10.0 h (7.0 %
of the successful time) re-running a `(node, stage)` pair that had already passed** before a later
stage failed — 87.2 h, **39.6 % of all stage time, spent on work the run threw away or repeated.**

**THE FAILURES ARE NOT THE SHAPE AGENTLESS FIXES.** Classifying all 58 repairs by their `error_in`
(falling back to the triage rationale only where the recorded tail is a bare progress bar; the
`manifest_vs_code` row folds in one v6 repair that predates the `reason` field and whose 4,000-char
tail is truncated past the phrase the classifier keys on) and joining each to the wall clock of the
stage it followed — the 14 rows are all 58 repairs and the hours sum to the 62.8 h of failing-stage
time a repair follows:

    class                     n   failing-stage h   median s     what would have caught it
    timeout_or_slow           4        23.56          18,203     nothing static — hours of GPU
    stalled                   2        16.27          29,287     nothing static — a wedged CUDA op
    declared_condition        4         6.86           6,178     2 of 4 are a static manifest↔config
                                                                 disagreement (declared 50 epochs,
                                                                 config n_epochs: 1); 2 are budget
    manifest_vs_code          6         5.85           3,613     partly: declared path vs written path
    gpu_oom                  18         4.79             267     nothing static — the real batch
    path_mismatch             3         2.01           2,639     partly
    own_guardrail_assert      3         1.80           2,658     nothing static — 43 % mining coverage
    diverged                  5         1.28             192     nothing static — 50 real train steps
    attribute_error           2         0.13             233     no (Series.to_set, nn.Module order)
    name_error                2         0.12             219     yes — pyflakes
    shape_error               1         0.06             224     no — needs a real forward pass
    argv_vs_parser            4         0.01               7     yes — one `--help` of the stage argv
    env_credentials           2         0.00               9     no — an S3 key, not the node's code
    missing_import_or_dep     2         0.00               2     yes — one import

**The cheaply-detectable failures are also the cheap ones to suffer.** Everything a pre-execution
check could see — `argv_vs_parser`, `missing_import_or_dep`, `name_error` — is **8 of 58 repairs and
0.13 h of the 220.2 h**, i.e. **0.06 % of stage time**. Two thirds of the wasted time
(`timeout_or_slow` + `stalled` + `declared_condition` = 46.7 h, 74 % of the 62.8 h attributable to a
repair) is a property of a multi-hour GPU training run: no localization, no static validator and no
`dev_probe` can predict it, and the only honest way to rank N candidates on it is to RUN all N.

**LOCALIZE HAS NOTHING TO LOCALIZE.** SWE-bench's phase 1 finds the 1 file to change among thousands.
Here the Developer's authored working set is **2–10 files, median 5**, and the median repair changes
**exactly one** of them (38 of 58 change 1 file, mean 1.24). Worse, `engine/localize.py` ranks
`*.py` **only**, and **48.6 % of the files repairs actually change are not Python**: the two most-edited
files in the whole corpus are `vectorsearch/configs/config.yaml` (18 of 72 changed-file mentions) and
`looplab_stages.json` (17) — the stage manifest and the training config, both invisible to the
ranker. The one clean localization failure in the corpus is v9 node 1 attempt 1, which edited
`vectorsearch/config.py` + `loss.py` while the executed configuration was `configs/config.yaml`; it
is **1 of 58**, and the file it should have found is one `localize()` cannot return.

**VALIDATE HAS NOTHING TO SEPARATE, AND THIS IS THE DECIDING NUMBER.** docs/36 bounds what may pick
a winner: the selector may WIDEN the action space, never the trusted set, so it must be something the
engine checks itself — does it parse, does it satisfy the manifest, does `dev_probe` run it. Over all
222 authored working sets preserved in `runs/`, **683 `.py` files were parsed and 0 failed** — the
execution-free floor would have refused nothing, ever. The remaining engine-checkable predicate (the
declared artifact contract) is `expect`, which already runs, after the stage.

**AND THE GENERATION IS THE EXPENSIVE HALF.** Joining `llm_usage` to the `phase` on each generation
span: the corpus spent **593.3M prompt / 22.7M completion tokens** over 5 runs, of which the Developer
build phases (`stages` + `plan` + `implement`) are **383.0M in / 12.3M out over 8,831 calls** — over
52 card builds that is **7.37M prompt tokens and 170 provider calls per node build** (per-run range
4.95M–8.16M). An inline repair is **0.86M in / 21 calls**, 12 % of a build. So N=3 over the implement
phase alone is +200M prompt tokens, **+34 % of the entire corpus's LLM spend**, to remove at most the
8 repairs (6.9M repair tokens, 0.13 GPU-h) a check could have seen — and N=3 over the whole build is
+766M, more than double everything five runs of this engine have ever spent.

DECLINED[agentless-developer-backend] `localize → generate-N → validate` is refused as the repo
Developer, permanently and on our own corpus rather than on SWE-bench's. measured: 8 of 58 repairs
(0.13 h of 220.2 h of stage time) are visible to any pre-execution check; the median repair edits 1
file out of a working set of 5; the execution-free validator scores 683 of 683 authored `.py` files
valid, separating 0 candidates; and N=3 costs +34 % of all LLM spend — docs/BACKLOG.md §0.18.
The three numbers each kill a different phase: 1-of-5 kills LOCALIZE (there is nothing to find,
and 48.6 % of the files repairs change are not `.py`, i.e. outside `engine/localize.py` by
construction), 683/683 kills VALIDATE (the only selector docs/36 permits separates nothing), and
+34 % against 0.13 h kills GENERATE-N. 61 % of 41 nodes do fail their first eval — the loop is
genuinely unreliable — but not in the shape this recipe repairs.

**WHAT THE MEASUREMENT DOES SUPPORT, and it is not this row.** The 46.7 h of `timeout`/`stalled`/
`declared_condition` are all the same sentence — *the declared schedule does not fit the declared
budget* — and every one of them is arithmetic the triage model performs AFTER the fact from the
node's own progress bar ("73 % done at 1.77 s/it, 10 epochs needs ~5h22m against a 4 h stage"). That
is a **budget probe**, not an agentless pipeline: one short run of the declared stage, an it/s
extrapolation against `timeout`, and a manifest↔config epoch cross-check. It is single-candidate,
costs no extra generation, and is the shape the corpus actually asks for. It is deliberately NOT
built here (the box is mid-GPU-run and it needs its own measurement of what a representative first
minute predicts); it is written down so the next agent starts from the number rather than the paper.

**AND ONE LIVE DEFECT THE MEASUREMENT TURNED UP, now fixed.** `best_of_n` — C5's item (d) — was
silently inert on every repo task and billing for it. `search/best_of_n.py::BestOfNDeveloper` ranks
the STRING `implement()` returns; `adapters/repo_developer.py::LLMRepoDeveloper.implement` returns a
SENTINEL (`""` = "the files are the answer"; the artifact travels on `last_files`). Driven through
the shipped classes: with `n=2` and a deliberately broken candidate 0, both candidates score
**-1.0**, `top` holds both, the FOREAGENT ranker and the D10 list-wise tie-break are both skipped
because `len({"", ""}) == 1`, and `chosen = top[0]` — **the broken one wins**, after two full builds
were paid for. At the corpus's 7.37M prompt tokens per build that is **+14.7M tokens per node at
`best_of_n=3` for an outcome the setting has no influence over.** `search/best_of_n.py::refuse_unrankable_best_of_n`, called
from `make_roles`, now REFUSES the combination (`ConfigRefusal`, so `cli/__init__.py`'s boundary
prints one line at exit 2, and a live Strategist developer swap records the
`developer_application: refused` receipt `engine/strategy.py::_prepare_strategy_developer` already
writes) instead of a silent drop to N=1 —
the same reason `DEVELOPER_BACKEND_ALIASES` is wider than the launch set. The capability is asked of
the Developer, not inferred from the backend: `agents/roles.py::LLMDeveloper.answers_with_code`, a
POSITIVE marker (absent means no) forwarded read-through by `WrapsDeveloper`, exactly like
`honors_idea_space`. Making the selection read `last_files` instead was considered and refused on the
683/683 number above: a selector with measured discrimination of zero is the unverified claim this
file exists to end. `tests/test_best_of_n.py` drives the broken selection AND the refusal.

## The classifier decided from TEXT what the engine already knew, and where it did not know, it guessed

  **[2026-08-20 — the sibling of the 2026-08-05 heuristic deletion, found by the same failure.**
  `engine/triage.py`'s header records why two heuristics were removed on 2026-08-05 — *"a bound that
  depends on the TEXT QUALITY of a program's error output is not a bound"*, after one of them ran
  1,741 repairs because an identifier happened to be Cyrillic — and moved the STOPPING decision to
  the triage model. The CLASSIFICATION stayed a rule in the same file, and it has now failed the
  same way.

  `runs/e5small-dr-unified-v3` finished with **three nodes, zero metrics and the systemic-failure
  stop**. All three died of `torch.OutOfMemoryError`. `_failure_reason`'s `oom` branch recognises
  exactly one signature — the KERNEL kill, `exit_code in (-9, 137)` with no `Traceback` — and an
  allocator OOM is its mirror image: it *raises*, prints a full traceback and exits 1, so every
  conjunct is false. All eight `node_repaired` rows read `reason: crash`; the Developer got
  "diagnose the root cause" instead of the memory-reduction directive that exists one branch away
  and is exactly right; two of the repairs returned byte-identical files. Replayed over `runs/`:
  **26 of the 41 text-read failures in the five modern runs were out-of-memory failures recorded as
  `crash`** (v2 9, v3 8, v7 1, v8 4, v9 4), including the terminal rows of three dead nodes.

  **THE CORPUS WIN IS THE MARKER'S, NOT THIS ROW'S, and that is stated first because the opposite
  claim would be easy to make.** `triage.py::_is_torch_oom` landed the same day, scans the whole
  64,000-byte `res.stderr` clamp, and replayed over `runs/` resolves **all 26** — including the 9
  rows where `torchrun`/`accelerate` swallowed the child exception and the recorded 500-character
  `error_in` is nothing but a `Root Cause … exitcode: 1` block (the allocator string is still inside
  the clamp, one screen further up). **This change reclassifies nothing further on today's corpus.**

  **What it is for, given that.** Three things a marker list structurally cannot be extended to.
  (1) It answers *what failed*, so it reaches the readings the marker does not touch at all —
  `crash` vs `no_metric` — and the next allocator nobody has enumerated (a host `MemoryError`,
  `DefaultCPUAllocator: can't allocate memory`, an OOM re-raised inside another library's
  exception); each of those is otherwise another literal and another incident first. (2) It reads
  the STAGE LOG rather than the captured stream (`repair_log_tools`, since 2026-08-15), so it still
  answers when a chatty stage pushes the diagnosis past the clamp — which the 9 rows above miss by
  about one clamp width. (3) A substring rule cannot tell a string that is present from a string
  that is present for the wrong reason: a script that CATCHES an OOM, prints the traceback, backs
  off and then dies of something else reads as `oom` to any marker and is not one. **None of the
  three is measured on today's corpus.** What IS enforced rather than hoped for is the split, the
  refusal of an out-of-vocabulary reason, and the record columns.

  **The fix is a LINE, not a model.** `_failure_reason` answers two different kinds of question.
  Eight of its twelve outcomes are **authenticated facts** the engine recorded out of band about what
  *it* did — `diverged`/`stalled` from `run_argv`'s `signals`, `timeout` from its own clock, `drift`
  from the cross-reader's refusal, `setup` from its own short-circuit, and the three stage-contract
  statuses `_run_stages` reports structurally — and those are **final**: the judge is not consulted
  about one and `judged_failure_reason` would not read an answer about one if it arrived. That is
  what keeps this from re-creating `tests/test_watchdog_kill_is_not_an_oom.py`'s incident from the
  other side. Three are **readings** of the dead process's own text (`crash`, `oom`, `no_metric`) and
  go to the judge over a closed vocabulary; a reason outside it is refused and the engine keeps its
  own answer, because `Settings.inline_repair_reasons` selects on that vocabulary and an invented
  reason would silently make a failure class unrepairable.

  **Why the split is the safety argument.** `reason` is not only a "what happens next" input:
  `metric_salvage.NEVER_SALVAGED_REASONS` reads it, and salvage decides whether a number enters the
  RECORD. Every member of that set (`drift`, `setup`, `timeout`, `diverged`) is on the authenticated
  side and none of the three judged reasons is in it, so a model's answer cannot move a node into or
  out of the salvage refusal in either direction. `tests/test_failure_ownership_split.py`
  asserts the disjointness rather than restating it.

  **[2026-08-20, later the same day — the split was re-cut on OWNERSHIP and every text rule in
  `_failure_reason` was deleted.]** The half-measure above kept the text rules and let a judge
  RE-READ their three answers. That was the wrong axis: the defect is not that the rules used
  regexes, it is that in that one function **text got the LAST WORD** — nothing downstream re-checks
  a `reason`, while everywhere else in this tree that text touches a decision, `runtime/deps.py`
  already had the answer (a traceback and a model's prose NOMINATE a distribution; `is_present`
  DECIDES by spawning the eval interpreter). So the question became *"does an out-of-band channel
  exist?"*, and four things moved:

  * **`setup` came back to the ENGINE, and got stronger.** `run_command_eval` had just read the setup
    step's `rc`, threw it into stderr as `"setup failed:\n"`, and the classifier read it back with
    `.startswith()` — a fact round-tripping through a channel the CANDIDATE also writes, and since
    `setup` is in `NEVER_SALVAGED_REASONS`, a training script whose stderr opened with those twelve
    characters had a metric it really produced SUPPRESSED. `RunResult.setup_failed` replaces it.
  * **`check_failed` MOVED to the diagnostician and `needs_failed`/`expect_failed` did NOT.** The
    first is written from `_call_stage_check`, i.e. another MODEL's reading of the stage's stdout;
    the other two are the engine's own `stat`. Measured: 21 `check_failed` rows in `runs/` hide at
    least three different real causes (16 of them a training that never learned), against 8 of 8
    `expect_failed` rows whose repair rationale AGREES with the label.
  * **`oom` became ANSWER-ONLY.** Both of its producers were text rules, so no engine path can name
    it at all; an out-of-memory failure is now `crash` until something looks. The marker list did
    resolve all 26 misclassified corpus rows and that win was real — it is not why it went.
  * **`unclassified`** is the new thirteenth `FAILURE_REASON`, minted when a WIRED diagnostician was
    asked and could not answer, so that a failed diagnosis and a confirmed one stop writing the same
    row.

  The diagnostician is the triage call rather than a second agent, argued from the meter: that call
  already spends **8.82 provider calls per failure** (335 calls / 38 decisions / 6,898 s across
  v8+v9+v3, 3.3 % of a run's generations), so a separate agent doubles the failure-path cost and can
  contradict the directive it is building. See `looplab/engine/failure_diagnosis.py`.

  **The record says who chose the word.** `node_repaired` and `node_failed` carry `reason_source`
  (`engine`/`triage`) and `engine_reason` (the deterministic classification, kept whatever the model
  said) beside `reason`. That is the `at_pending` shape — an ENGINE-written column recording what the
  engine independently held beside a model-derived value — and deliberately not the
  `TrainingVerdict.fault` shape, which is a field the model emits and so cannot witness that a model
  wrote it. The authenticated column is never destroyed, so the corpus replay above stays runnable
  against future logs.

  **It costs no extra call.** The judge is already consulted exactly once per failed attempt, is
  already handed the evidence the question needs (the error text, the repair history, and since
  2026-08-15 the stage logs), so the classification rides on that emit as a `failure_kind` property.
  Corpus rate: 6–52 failed attempts per run, median ~11 — a separate ask would have added that many
  triage calls per run to re-read a string the judge has already read.

  **STILL OPEN, and named rather than done.** (1) The `setup` branch is the one authenticated reason
  that is *read from text* — `stderr.startswith("setup failed:")`, an engine-authored prefix in front
  of the candidate's own bytes — so a candidate whose stderr opens with those twelve characters is
  classified `setup`. It stays on the authenticated side because losing it is the only direction that
  OPENS a salvage gate, but the honest fix is a flag on `RunResult` from
  `command_eval.run_command_eval`'s early return, which is a different seam. (2) The 16 `no_metric`
  terminals in `runs/rubertlite-dense-retrieval` are all *"stage 'train' failed verification: loss
  constant at 14.8"* — 12 `not_learning` and 3 `diverged` in everything but the word. Under today's
  code they classify as `check_failed`, an authenticated fact, so the judge is never asked; the
  check-stage judge already names the cause in prose and the vocabulary flattens it. Whether
  `check_failed` should carry that judge's own attribution is the next question of this shape.]
