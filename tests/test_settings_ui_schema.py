from __future__ import annotations

import re
import json
import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.serve.server import make_app
from looplab.serve.settings_ui_schema import (
    SETTINGS_UI_SCHEMA, SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT,
    SETTINGS_UI_SCHEMA_CATALOGUE_VERSION, SETTINGS_UI_SCHEMA_ETAG,
    SETTINGS_UI_SCHEMA_KEYSET_REVISION, SETTINGS_UI_SCHEMA_REVISION,
    SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT, SETTINGS_UI_SCHEMA_VERSION,
)


def test_settings_ui_schema_is_versioned_revalidated_and_conditionally_cacheable(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.get(f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}")
    assert response.status_code == 200
    assert response.json() == SETTINGS_UI_SCHEMA
    assert response.headers["cache-control"] == "private, no-cache, max-age=0, must-revalidate"
    assert response.headers["etag"] == SETTINGS_UI_SCHEMA_ETAG
    assert response.headers["x-looplab-schema-version"] == str(SETTINGS_UI_SCHEMA_VERSION)
    assert response.json()["revision"] == SETTINGS_UI_SCHEMA_REVISION

    unchanged = client.get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
        headers={"If-None-Match": SETTINGS_UI_SCHEMA_ETAG},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["cache-control"] == response.headers["cache-control"]
    assert unchanged.headers["etag"] == SETTINGS_UI_SCHEMA_ETAG
    strong_equivalent = SETTINGS_UI_SCHEMA_ETAG.removeprefix("W/")
    for validator in (
        strong_equivalent,
        f'"some-other-revision", {SETTINGS_UI_SCHEMA_ETAG}',
        "*",
    ):
        conditional = client.get(
            f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
            headers={"If-None-Match": validator},
        )
        assert conditional.status_code == 304
        assert conditional.content == b""
        assert conditional.headers["etag"] == SETTINGS_UI_SCHEMA_ETAG
    changed = client.get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
        headers={"If-None-Match": 'W/"some-other-revision"'},
    )
    assert changed.status_code == 200
    invalid = client.get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
        headers={"If-None-Match": f"not-an-entity-tag {SETTINGS_UI_SCHEMA_ETAG}"},
    )
    assert invalid.status_code == 200
    assert client.get(f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION + 1}").status_code == 404


def test_settings_schema_keeps_revalidation_policy_with_owner_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    response = TestClient(make_app(tmp_path)).get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION}",
        headers={"X-LoopLab-Token": "owner-secret"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache, max-age=0, must-revalidate"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["etag"] == SETTINGS_UI_SCHEMA_ETAG
    unknown = TestClient(make_app(tmp_path)).get(
        f"/api/settings/schema/{SETTINGS_UI_SCHEMA_VERSION + 1}",
        headers={"X-LoopLab-Token": "owner-secret"},
    )
    assert unknown.status_code == 404
    assert unknown.headers["cache-control"] == "no-store"


def test_packaged_settings_ui_schema_preserves_copy_and_only_known_unique_fields():
    packaged = json.loads(Path(__file__).parents[1].joinpath(
        "looplab", "serve", "settings_ui_schema.json").read_text(encoding="utf-8"))
    catalogue_shape = deepcopy(SETTINGS_UI_SCHEMA)
    catalogue_shape.pop("revision")
    catalogue_shape["schema"] = SETTINGS_UI_SCHEMA_CATALOGUE_VERSION
    for group in catalogue_shape["groups"]:
        for field in group["fields"]:
            for name in ("minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum"):
                field.pop(name, None)
            field.pop("nullable", None)
    assert catalogue_shape == packaged
    assert packaged["schema"] == SETTINGS_UI_SCHEMA_CATALOGUE_VERSION

    fields = [field for group in packaged["groups"] for field in group["fields"]]
    keys = [field["key"] for field in fields]
    assert len(keys) == len(set(keys))
    assert len(keys) == SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT == 211
    # 210 -> 211 on 2026-09-08: `eval_noise_seeds`, the eval NOISE FLOOR (doc 52 row 11) — the
    # number of times ONE candidate is re-evaluated so the run records the spread of its own metric.
    # A ROW rather than an uncurated omission for the reason the spend caps are rows: it buys N full
    # evaluations at the end of the run, and 0 (off) is the shipped behaviour an operator must be
    # able to return to. Verified by INTERSECTION as every entry below prescribes rather than by
    # adding the integer: 210 keys common to the previous keyset plus exactly that one, no
    # duplicate and none removed.
    # 207 + 2 -> 209 on 2026-09-08, at the MERGE: two branches each added one row on the same day,
    # and each pinned 208 against a tree without the other's. Verified by INTERSECTION rather than by
    # adding the integers: 207 keys are common to both files, and removing exactly
    # `lesson_operator_scope` and `mlflow_tracking_uri` gives that set back — two real additions,
    # nothing renamed away underneath either.
    #   `lesson_operator_scope` (doc 52 §4.3) — whether the Developer's cross-run prior is RANKED by
    #   the operator about to fire. A row on `memo_verdict_cue`'s ground (it changes a prompt), and
    #   OFF is the shipped default, so the operator opting IN is the one who needs to find it.
    #   `mlflow_tracking_uri` (docs/BACKLOG.md §16), the live MLflow mirror. A row and not an
    #   uncurated omission because it decides whether this run's params, metrics and champion code
    #   leave the box for an external server — the operator has to see it to turn it off.
    # 206 + 1 -> 207 on 2026-09-07, at the SECOND merge with master: master's ten rows had
    # already met this branch's ten, and this is the one row this branch authored after
    # that merge — `agent_read_loop_nudge_after`. Verified by intersection as every entry
    # below prescribes: 206 keys common to master's file plus exactly that one, no
    # duplicate and none removed.
    # 196 + 196 -> 206 on 2026-09-07, at the MERGE with master, and BOTH histories below are
    # real: each side grew its own copy from a common 186 and neither literal was measured
    # against a tree holding the other's rows. Verified as every entry below prescribes —
    # by INTERSECTION, never by adding the integers: 186 keys are common to the two files,
    # this branch adds ten (diagnosis_hypotheses, endgame_reserve_frac, evidence_envelope,
    # llm_cost_limit, llm_token_limit, mcts_cost_weight, novelty_literature,
    # stage_check_tools, steady_state_build, syscall_fence) and master ten
    # (developer_crash_pause_after, developer_probe_confine, developer_stage_guidance,
    # developer_step_feedback_command, established_context, established_context_bytes,
    # hide_empty_tools, llm_budget_usd, llm_stream_stall_fallback,
    # node_open_budget_floor_usd), giving 206 with no duplicate key. The keyset revision is
    # RE-DERIVED over the merged keyset for the same reason.
    # 196 + 1 -> 197 on 2026-09-07: `agent_read_loop_nudge_after`, found by review. The A9 read-loop
    # nudge shipped ON with a threshold of 25 and NO Settings field, so `loop_opts_from_settings`
    # never populated the bundle and `drive_tool_loop`'s literal was the only value that could ever
    # apply — the "0 = off" its own docstring offers was unreachable from an env var, a snapshot,
    # this form or the guide. A ROW rather than an uncurated entry because it is a plain threshold
    # on the agent loop, exactly like its eight `agent_*` siblings, and it changes the bytes of
    # every tool result the model sees. Verified as 196 keys common to the previous keyset plus
    # exactly that one, no duplicate.
    # 193 + 2 -> 195 on 2026-09-06: A5's pair, `established_context` and `established_context_bytes`
    # — the "already established" block and its byte budget. CURATED because the first is a
    # default-ON prompt splice at every chain root and the second is the only bound on how much of
    # a run's read history that splice can carry; both are things an operator turns down when a
    # prompt is too long, which is the form's own job. (This entry was missing until 2026-09-07;
    # the two rows really did land in `d2f9a54c`, and the ledger's own rule is that every move is
    # argued, not merely balanced.)
    # 195 + 1 -> 196 on 2026-09-06, at the MERGE with master: this branch's ten rows
    # meeting master's `agent_timeout`. Verified as every entry below prescribes rather
    # than by adding the integers: 185 rows are common to the two files, ours adds ten and
    # master one, and removing exactly those eleven gives back 185 with no duplicate key.
    # 190 + 3 -> 193 on 2026-09-06: the three bench-driven knobs of docs/60 §60.9 (A7/A10/A12),
    # `llm_stream_stall_fallback`, `node_open_budget_floor_usd` and `developer_crash_pause_after`,
    # each beside the row it modifies (`llm_stream`, `llm_budget_usd`, `systemic_failure_stop`).
    # Verified as 190 keys common to the previous keyset plus exactly those three, no duplicate.
    # 189 + 1 -> 190 on 2026-08-31, at the MERGE with master, and BOTH histories under it are real.
    # Master's row is `single_command_divergence_watch`, and it is a CORRECTION rather than a
    # feature: the field shipped in `7813032e` with neither a form row nor an uncurated entry, so
    # `_reconcile_settings_fields` was RED on master from that merge until `9a07427f` — a targeted
    # suite that did not include `tests/test_stage_environment.py` is what let it through. A ROW and
    # not an uncurated entry because none of that registry's four honest reasons holds: it is not an
    # open key set, not a legacy alias, not a load-time binding, and the "second-order tuning whose
    # PARENT already has a row" clause is false — the deterministic divergence watchdog has no row
    # of its own, and the `train_monitor_*` family beside it is the LLM judge, a different rung.
    # Verified as the paragraph prescribes rather than by bumping the number: 184 rows are common to
    # the two files, this branch adds five and master one, and removing exactly those six gives back
    # 184 with no duplicate key.
    # 184 + 5 -> 189 on 2026-08-29, at the previous MERGE with master: master's 184 catalogued rows
    # meeting the five this branch authored (`llm_budget_usd`, `hide_empty_tools`,
    # `developer_probe_confine`, `developer_step_feedback_command`, `developer_stage_guidance`).
    # Verified as the paragraph prescribes rather than by bumping the number: master's file
    # carried 184 keys, ours 188, the intersection 183, and re-adding exactly those five to
    # master's catalogue gives 189 with no duplicate key.
    # 195 -> 196 on 2026-09-07: `steady_state_build`, the build fan-out as a refilling lane
    # (doc 52 row 33). A row because it changes how many provider calls a build batch makes.
    # 194 -> 195 on 2026-09-07: `diagnosis_hypotheses`, the competing explanations the crash
    # diagnostician considered (doc 52 row 32). A row for the same reason as the one below
    # it: it changes what a paid call is asked, and an operator has to be able to see that.
    # 193 -> 194 on 2026-09-07: `novelty_literature`, the retrieved papers reaching the
    # novelty gates (doc 52 row 32). A row because it changes what the re-proposal prompt
    # says, which is the kind of switch an operator has to be able to see and turn off.
    # 192 -> 193 on 2026-09-07: `mcts_cost_weight`, the cost term of the cost-constrained
    # MCTS (doc 52 row 31). A row rather than an omission for the same reason the spend caps
    # are rows: it is a number an operator types to trade speed against score, and it is
    # meaningless to anyone who cannot see its unit (relative to the run's mean eval second).
    # 190 -> 191 on 2026-09-06: `endgame_reserve_frac`, the plan's endgame reserve (doc 52 row 18).
    # 188 -> 190 catalogued rows on 2026-09-06: `llm_cost_limit` + `llm_token_limit`, the run's LLM
    # spend caps reserved at the broker's permit (`core/llm_budget.py`, doc 52 row 15). Rows beside
    # `max_seconds` / `max_eval_seconds`: a spend ceiling is exactly the operator-typed knob a form
    # exists for, and until then the run had no LLM cap at all.
    # 187 -> 188 catalogued rows on 2026-09-06: `evidence_envelope`, the ONE untrusted-evidence
    # envelope (`core/evidence.py`, doc 52 row 13) on the Strategist, the crash-triage judge, the
    # repair critic and the arXiv / web tools. A row for the reason the three rows below are:
    # it changes what a paid, decision-moving role is TOLD about its evidence and marks that
    # evidence, i.e. it changes a PROMPT, and the operator must be able to see the switch that
    # restores the historical bytes.
    # 186 -> 187 catalogued rows on 2026-09-06: `stage_check_tools`, whether the INTER-STAGE
    # CHECKER — the one judge whose verdict can end a node — may query the checked stage's own
    # log instead of deciding from `run.out[-4000:]` (doc 52 row 9). A row for the reason
    # `train_monitor_tools` / `repair_log_tools` are: it changes what the evidence for a paid,
    # node-ending judgement IS, buys round trips when on, and restores a PROMPT when off.
    # 185 -> 186 catalogued rows on 2026-09-03: `agent_timeout`, the wall on ONE external
    # coding-agent invocation. A ROW because none of the four honest omission clauses holds — the
    # key set is closed, it is not a legacy alias, it is exactly operator-typed, and its parent
    # feature (the external coding-agent Developer) already has rows. It is also the shape those
    # clauses exist to catch from the other side: `CliAgentDeveloper`'s constructor default was the
    # only value a composed run could have, because `agents/factory.py` never passed the argument.
    # 184 -> 185 catalogued rows on 2026-08-30: `single_command_divergence_watch`, and this one is a
    # CORRECTION rather than a feature. The field shipped in `7813032e` with neither a form row nor
    # an uncurated entry, so `_reconcile_settings_fields` was RED on master from that merge until
    # now — a targeted suite that did not include `tests/test_stage_environment.py` is what let it
    # through. A ROW and not an uncurated entry because none of that registry's four honest reasons
    # holds: it is not an open key set, not a legacy alias, not a load-time binding, and the
    # "second-order tuning whose PARENT already has a row" clause is false — the deterministic
    # divergence watchdog has no row of its own, and the `train_monitor_*` family beside it is the
    # LLM judge, a different rung.
    # 216 -> 217 Settings and 183 -> 184 catalogued rows on 2026-08-27: `triage_time_budget_s`, the
    # wall-clock ceiling on ONE crash/timeout triage call. A row rather than an uncurated omission
    # because it is the operator's only handle on a loop that BLOCKS the eval thread with the GPU
    # dark behind it, and because 0 = unlimited is what this box ran until now: `e5small-dr-unified-v8`
    # node 2 spent 88.3 min and 206 provider calls re-sweeping one 663-line file INSIDE a healthy
    # turn budget, which is the shape no turn count can see. It carries the same 1200 as
    # `developer_session_time_budget_s` because the two bound consecutive phases of one thread.
    # 220 -> 221 Settings and 187 -> 188 catalogued rows on 2026-08-28: +developer_stage_guidance,
    # the switch that drops ~5,000 characters of stage-pipeline advice from the Developer prompt
    # for single-stage tasks. Default TRUE so a resumed run keeps its historical prompt.
    # 219 -> 220 Settings and 186 -> 187 catalogued rows on 2026-08-27:
    # `developer_step_feedback_command`, the operator-pinned command the Developer's plan loop runs
    # BETWEEN steps so a writing session sees a number (doc 53 item 10, our half). A row rather than
    # an uncurated omission on this list's usual grounds: "" is the HISTORICAL behaviour and an
    # operator must be able to get back to it, and it changes a PROMPT — what the agent is SHOWN,
    # which is the measurement. It also spends real wall clock (~40 s per step that edits a file),
    # so the switch that buys it has to be visible.
    # 183 -> 186 on 2026-08-21, at the REBASE onto master: three rows this branch authored
    # (`llm_budget_usd`, `hide_empty_tools`, `developer_probe_confine`) meeting master's own
    # additions. The count moved by exactly the three, which is the check that the rebase carried
    # the branch's catalogue rather than resolving them away — they WERE resolved away first, and
    # were lifted back from the pre-rebase head rather than retyped.
    # 218 -> 219 Settings and 182 -> 183 catalogued rows on 2026-08-20:
    # `train_monitor_contract`, whether the live training-log watchdog is shown the stage's
    # own declared contract (`expect.assert` / `expect.files`) and the engine's reading of
    # the schedule the trainer configured. A row rather than an uncurated omission for the
    # reason `train_monitor_tools` beside it is one — it changes what the evidence for a
    # paid, node-ending judgement IS, and it changes a PROMPT, which is a contract here, so
    # an operator must be able to see the switch that restores the historical bytes. Unlike
    # that neighbour it buys NO extra round trips: it is text in a message already sent.
    # 213 -> 214 Settings and 180 -> 181 catalogued rows on 2026-08-18: `cadence_while_evaluating`,
    # whether the node-count cadences may fire at a creation decision point that still has an
    # evaluation in flight (backlog F1i). A row rather than an uncurated omission on both of this
    # list's usual grounds: OFF is the HISTORICAL behaviour and an operator must be able to get it
    # back, and what ON enables is PAID cadence work beside a running GPU. Measured over `runs/`,
    # the guard it replaces has been false for the whole life of every GPU run since 2026-08-13 —
    # `rubertlite-dr-unified-v7`, `-v9` and the live `e5small-dr-unified-v2` recorded zero
    # `strategy_decision`, zero `coverage_snapshot` and zero classifier `node_concepts` between
    # them. It buys no extra passes per node count and its output is fenced out of the
    # graded-novelty evidence channel, so nothing it enables can reach selection.
    # 212 -> 213 Settings and 179 -> 180 catalogued rows on 2026-08-16: `memo_verdict_cue`,
    # whether the deep-research takeaway PUSHED into every Researcher / crash-triage /
    # repair-critic prompt carries the memo's own verifier tally. A row rather than an
    # uncurated omission for the reason `research_verify` beside it is one — it is the
    # DELIVERY half of that verifier, the verifier only ever checks a memo's CLAIMS so the
    # summary this line pushes is the one field of the memo nothing checks, and it changes a
    # PROMPT, which is a contract here: an operator must be able to see the switch that
    # restores the historical bytes. It buys no paid call and moves nothing.
    # 208 -> 209 Settings and 178 -> 179 catalogued rows on 2026-08-15: `repair_log_tools`,
    # whether the crash/timeout TRIAGE judge may query the failed eval's stage logs instead of
    # diagnosing from `res.stderr[-500:]`. A row rather than an uncurated omission for the same
    # reason `train_monitor_tools` beside it is one — it changes what the evidence for a paid,
    # node-ending judgement IS, and it buys extra model round trips when it is on.
    # 199 -> 201 Settings and 168 -> 170 catalogued rows: TWO rows landed together on 2026-08-13.
    # `assistant_time_budget_s` gave the CHAT its own wall clock — `run_turn` read the engine-wide
    # `agent_time_budget_s` and then applied `or 300.0`, so the neighbouring row's documented
    # "0 = no cap" was false for the assistant and nothing could raise the chat's limit without
    # raising every engine role's. `eval_deadline_grace_s` (doc 39 §2.2) is the one-shot,
    # judge-granted extension a stage may get at its wall-clock deadline — a row rather than an
    # uncurated omission because it is an LLM-judged switch that spends GPU time when an operator
    # turns it on, and they have to be able to see the number they set.
    # (Previously 194 -> 195 Settings and 163 -> 164 catalogued rows when `task_facets_finalize` split the
    # paid-but-behaviorally-inert task-facet call from the concept/claim curation umbrella. The
    # literal is a review tripwire, not a gate (the gate is the two-way reconciliation), and the
    # separate default-off warning row is part of this same contract.
    # 194 -> 195 Settings and 163 -> 164 catalogued rows when `task_facets_finalize` split the
    # paid-but-behaviorally-inert task-facet call from the concept/claim curation umbrella. The
    # literal is a review tripwire, not a gate (the gate is the two-way reconciliation), and the
    # separate default-off warning row is part of this same contract.
    # 199 -> 201 Settings and 168 -> 170 rows for the Developer's PROBE (F2): `developer_probe` and
    # `developer_probe_timeout_s`. Both are ROWS rather than uncurated omissions for the reason
    # `read_fence` is one — the probe is an EXECUTION surface running inside the engine process, so
    # an operator has to be able to see that it exists and close it, and its timeout is the line
    # between "a question" and "a job that belongs in an eval stage".
    # 210 -> 211 Settings with the catalogue UNCHANGED at 178 rows: `sandbox_readonly_rootfs` joins
    # its four `sandbox_*` siblings in `_UNCURATED_SECOND_ORDER`. Uncurated for the same reason
    # `sandbox_memory`/`sandbox_cpus` are — it is meaningless on the shipped `trusted_local` tier
    # (there is no container filesystem to make read-only), so a form row would offer every operator
    # a knob that does nothing on their box, and the operators who DO run the container tiers set
    # them together in a config file.
    # 222 -> 223 Settings on 2026-08-31, at the MERGE: master's `single_command_divergence_watch`.
    # It reached master in `7813032e` WITHOUT this pin or a catalogue row, which is why the
    # reconciliation was red there from that merge until `9a07427f` — see the catalogue note above
    # for why it gets a form row rather than an uncurated entry.
    # 223 -> 224 on 2026-09-04: `developer_probe_max_calls`, the instrument for the arm registered
    # in docs/56 §190. It is UNCURATED on purpose and the reason is in the dict beside the others:
    # a form row would invite operators to set a probe cap the benchmark has not yet shown to be
    # good. §189 is the measurement behind the arm -- of eleven process variables only probe count
    # separates the best `edge_expansion` runs from the worst (20 vs 29, p = 0.037), and a median
    # split at 24 gives champions of 221.81 against 177.84 (p = 0.0077) -- and it is a correlation
    # until the arm runs. It gets a row when an arm says which N is right.
    # 224 -> 227 on 2026-09-06: docs/60 §60.9's three bench-driven knobs (A7 engine half, A10,
    # A12), all CURATED — see the 190 -> 193 note above; the two counts move together.
    # 217 -> 218 Settings on 2026-08-30: `single_command_divergence_watch`. It reached master
    # in `7813032e` WITHOUT this pin or a catalogue row, which is why the reconciliation was
    # red from that merge until now — see the catalogue note above for why it gets a form row
    # rather than an uncurated entry.
    # 218 -> 219 Settings on 2026-09-03: `agent_timeout`. See the catalogue note above for why it
    # is a form row; the reason it is a Settings field at ALL is that it previously was not, and the
    # constructor default it replaced was therefore the only value a composed run could ever have.
    # 230 + 230 -> 241 on 2026-09-07, at the MERGE with master: re-derived from the
    # MERGED model, not added — an AST scan of `Settings` against both parents reports
    # exactly the twenty catalogued additions above plus each side's uncatalogued ones,
    # and none removed.
    # 229 + 1 -> 230 on 2026-09-06, at the MERGE with master: master's `agent_timeout`
    # meeting this branch's ten. Re-derived from the merged model, not added: an AST scan
    # of `Settings` against both parents reports exactly those eleven added and none
    # removed.
    # 219 -> 220 Settings on 2026-09-06: `stage_check_tools`. See the catalogue note above.
    # 220 -> 221 Settings on 2026-09-06: `evidence_envelope`. See the catalogue note above.
    # 221 -> 223 Settings on 2026-09-06: `llm_cost_limit` + `llm_token_limit`. See the catalogue note.
    # 223 -> 224 Settings on 2026-09-06: `endgame_reserve_frac`, the plan's endgame reserve (doc 52 row 18).
    # 224 -> 225 Settings on 2026-09-06: `model_arms`, the operator x model router's arms (doc 52 row 19; uncurated, open-keyed).
    # 225 -> 226 Settings on 2026-09-06: `syscall_fence`, the kernel syscall policy beside `landlock` (doc 52 row 28; a row).
    # 226 -> 227 Settings on 2026-09-07: `mcts_cost_weight`, the cost term of the cost-constrained MCTS (doc 52 row 31; a row).
    # 227 -> 228 Settings on 2026-09-07: `novelty_literature` (doc 52 row 32; a row).
    # 228 -> 229 Settings on 2026-09-07: `diagnosis_hypotheses` (doc 52 row 32; a row).
    # 229 -> 230 Settings on 2026-09-07: `steady_state_build` (doc 52 row 33; a row).
    # 230 + 1 -> 231 on 2026-09-07: `agent_read_loop_nudge_after` — see the catalogue note above;
    # the two counts move together because it is a curated row.
    # 227 -> 229 Settings on 2026-09-06: A5's `established_context` / `established_context_bytes`,
    # the pair whose catalogue entry was also missing until 2026-09-07.
    # 244 + 1 -> 245 on 2026-09-08, at the same merge sequence: `mcts_value_weight` was the day's
    # THIRD field, from a third branch, and it pinned 242 for the same reason. Re-derived with the
    # delta CHECKED by an AST diff against the merge base: exactly that key added, none removed.
    # 242 + 2 -> 244 on 2026-09-08, at the MERGE: `lesson_operator_scope` (doc 52 §4.3) and
    # `mlflow_tracking_uri` (docs/BACKLOG.md §16). Both counts move with the catalogue because both
    # are curated rows — see the note above, and note that neither branch's 243 described a tree
    # holding the other's field.
    # 241 + 1 -> 242 on 2026-09-07, at the SECOND merge: `agent_read_loop_nudge_after`,
    # the one field this branch added after master already carried its ten. The two counts
    # move together because it is a curated row.
    # 245 -> 246 on 2026-09-08: `eval_noise_seeds` (doc 52 row 11; a curated row, so the two counts
    # move together). An AST scan of `Settings`' annotated assignments against the pre-change tree
    # reports exactly `['eval_noise_seeds']` added and `[]` removed, so a +2/-1 cannot hide here.
    assert len(Settings.model_fields) == SETTINGS_UI_SCHEMA_SETTINGS_FIELD_COUNT == 246
    # 199 -> 200 Settings and 168 -> 169 catalogued rows when F8 added `repair_critic_after`
    # (2026-08-13), the cadence at which the repair critic gets its veto. It is catalogued rather
    # than left uncurated because the knob directly above it, `inline_repair_attempts`, changed
    # meaning in the same commit — its default is now 0 — and an operator reading one without the
    # other would conclude that in-node repair had become unbounded.
    assert hashlib.sha256("\0".join(sorted(keys)).encode()).hexdigest() == SETTINGS_UI_SCHEMA_KEYSET_REVISION

    # THE JS HALF OF THIS TRIPWIRE, pinned from the suite that actually gets run.
    # `ui/test/settingsSchemaResource.test.js` carries the same field count as a literal, and its own
    # comment records the count drifting FIVE times — 162->163, 165->167, 167->168, and finally
    # 168->175 across five merged branches. Every occurrence has the same cause, which that comment
    # also names: the Python guard is in `python -m pytest` and the JS one is in `npm test`, so it is
    # always the JS half that is left behind. A note telling the next contributor to grep for the old
    # number is the weakest possible fix for a failure that has now recurred five times; reading the
    # literal here makes the drift impossible instead of merely documented.
    #
    # Read as TEXT on purpose: the point is to fail when the JS source still says the old number, and
    # importing or executing it would need a node toolchain this suite does not assume.
    js = (Path(__file__).resolve().parents[1] / "ui" / "test" / "settingsSchemaResource.test.js")
    if js.exists():                       # a source-only checkout without `ui/` is not a failure
        pinned = re.findall(r"assert\.equal\(Object\.keys\(schema\.fieldByKey\)\.length, (\d+)\)",
                            js.read_text(encoding="utf-8"))
        assert pinned == [str(SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT)], (
            f"ui/test/settingsSchemaResource.test.js pins {pinned}, catalogue has "
            f"{SETTINGS_UI_SCHEMA_CATALOGUE_FIELD_COUNT} rows")
    assert set(keys) <= set(Settings.model_fields)
    by_key = {field["key"]: field for field in fields}
    # CODEX AGENT: curated settings expose the two independent canonical axes; legacy aliases still
    # parse in raw config/snapshots but must not remain as competing operator controls. Eval width is
    # per Run, while the conservative same-user host lease is a separate cross-Run admission layer.
    assert {"eval_parallel", "llm_parallel"} <= set(by_key)
    assert by_key["card_driven_selection"]["type"] == "bool"
    assert "pinned at run start" in by_key["card_driven_selection"]["help"]
    assert by_key["speculation_depth"]["type"] == "int"
    assert "only when Card queue selection is enabled" in by_key["speculation_depth"]["help"]
    assert "speculation-gate" in by_key["speculation_depth"]["help"]
    # The DEFAULT itself belongs to config.py (it flipped 0 -> -1/AUTO on 2026-08-05); what this file
    # pins is that the row was reviewed against whatever ships, which `_check_pinned_default` enforces
    # at load and this asserts on the packaged copy.
    assert by_key["speculation_depth"]["default"] == Settings.model_fields["speculation_depth"].default
    assert Settings.model_fields["speculation_depth"].metadata
    assert "speculation_gate_receipt" not in by_key
    assert {"max_parallel", "parallel_build"}.isdisjoint(by_key)
    assert "0 = AUTO at launch" in by_key["eval_parallel"]["help"]
    assert "inside one Run" in by_key["eval_parallel"]["help"]
    assert "host lease" in by_key["eval_parallel"]["help"]
    assert "Live Strategist/operator updates settle 0" in by_key["llm_parallel"]["help"]
    assert "agents" not in by_key["max_eval_timeout"]
    assert "hard ceiling" in by_key["max_eval_timeout"]["help"].lower()
    assert set(("concept_pivot", "concept_run_base", "concept_retag_every",
                "graded_novelty", "capability_expansion")) <= set(by_key)
    assert "does not itself rank candidates" in by_key["concept_pivot"]["help"]
    assert "materialization receipts" in by_key["concept_run_base"]["help"]
    assert "display-only" in by_key["concept_run_base"]["help"]
    assert "Researcher-authored additions" in by_key["concept_retag_every"]["help"]
    assert "proposal admission" in by_key["graded_novelty"]["help"]
    assert "Concept coverage pivot" in by_key["capability_expansion"]["help"]
    assert "D8" in by_key["cross_run_concepts"]["help"]
    # CODEX AGENT: product-default experimental switches must disclose behavioral and paid-work effects;
    # otherwise the Settings UI is materially less truthful than the config reference it controls.
    assert "affect downstream selection" in by_key["concept_pivot"]["help"]
    assert "model/tool-loop turns" in by_key["cross_run_read_tools"]["help"]
    assert "delay finalization" in by_key["cross_run_curation"]["help"]
    assert "paid model cost" in by_key["cross_run_curation"]["warning"]
    assert by_key["task_facets_finalize"]["default"] is False
    assert "no current retrieval" in by_key["task_facets_finalize"]["help"]
    assert "no behavioral consumer" in by_key["task_facets_finalize"]["warningTitle"]
    assert "synchronous paid" in by_key["task_facets_finalize"]["warning"]
    assert "manual task-facets" in by_key["task_facets_finalize"]["help"]
    assert "never applies" in by_key["cross_run_curation_auto"]["warning"]
    assert "configured run root" in by_key["all_runs_tools"]["help"]
    assert "not machine-wide" in by_key["all_runs_tools"]["help"]
    assert "richer run-root tools" in by_key["all_runs_tools"]["help"]
    assert "over that same root" in by_key["all_runs_tools"]["help"]
    assert "EVERY run on this machine" not in by_key["all_runs_tools"]["help"]
    assert "task's original in-process Developer" in by_key["validate_agent"]["help"]
    assert "LLM developer" not in by_key["validate_agent"]["help"]
    # CODEX AGENT: placeholders/default copy are executable UI behavior, not decoration; pin the two
    # defaults that previously instructed operators to configure the opposite of the product policy.
    assert Settings.model_fields["concurrent_research"].default is True
    assert "on by default" in by_key["concurrent_research"]["help"]
    assert "off by default" not in by_key["concurrent_research"]["help"]
    # CODEX AGENT: fold-neutral telemetry is not necessarily behaviorally inert. These defaults feed
    # later prompts/portfolio evidence, so UI copy must disclose that indirect effect precisely.
    assert "steer the next proposal" in by_key["concurrent_research"]["help"]
    assert "steer later proposals" in by_key["concurrent_research_repeat"]["help"]
    assert "steer later proposals" in by_key["track_hypotheses"]["help"]
    assert "positive D8 cross-run claim evidence" in by_key["research_verify"]["help"]
    assert by_key["deep_research_every"]["placeholder"] == str(
        Settings.model_fields["deep_research_every"].default)
    assert packaged["agent_role_pills"]["researcher"]["short"] == "R"

    served_fields = {
        field["key"]: field
        for group in SETTINGS_UI_SCHEMA["groups"]
        for field in group["fields"]
    }
    assert served_fields["max_nodes"] | {"minimum": 1, "maximum": 1_000_000} == served_fields["max_nodes"]
    assert served_fields["n_seeds"]["minimum"] == 1
    assert served_fields["n_seeds"]["maximum"] == 1024
    assert served_fields["eval_parallel"]["minimum"] == 0
    assert served_fields["eval_parallel"]["maximum"] == 1024
    assert served_fields["llm_parallel"]["minimum"] == 0
    assert served_fields["llm_parallel"]["maximum"] == 64
    assert served_fields["timeout"]["exclusiveMinimum"] == 0
    assert served_fields["timeout"]["nullable"] is False
    assert served_fields["max_seconds"]["nullable"] is True
    assert served_fields["holdout_fraction"]["minimum"] == 0
    assert served_fields["holdout_fraction"]["maximum"] == 0.9
    assert served_fields["select_verifier_samples"]["minimum"] == 1
    assert served_fields["select_verifier_samples"]["maximum"] == 32
    assert not ({"minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum"}
                & served_fields["llm_model"].keys())
