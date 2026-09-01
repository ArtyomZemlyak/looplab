#!/usr/bin/env python3
"""One meter in front of the LLM, for both arms.

WHY THIS EXISTS
---------------
Both arms enforce their per-task budget by reading a cost the PROVIDER reports:

  * arm A  `AlgoTuner/models/lite_llm_model.py::_extract_cost_from_response` -- method 2 is
    `response.usage.cost`, the OpenRouter shape;
  * arm B  `looplab/core/llm.py::_usage_cost` -- the same `usage.cost` key, and
    `cost_is_reported()` deliberately treats an ABSENT cost as unpriced rather than as free.

The corporate gateway (`llm-core-olap.samokat.ru/v1`, SGLang behind it) returns
`usage {prompt_tokens, completion_tokens, total_tokens}` and NO cost. Measured 2026-08-20.
So on that endpoint `spend_limit: 0.02` (arm A) and `LOOPLAB_LLM_BUDGET_USD` (arm B) never bind,
and the budget parity docs/50 5f settles on -- equal SPEND, not equal wall-clock -- silently
becomes no budget at all on either side.

This proxy prices each response from a pinned table (`pricing.json`) and stamps the result into
`usage.cost`, where both arms already look. Neither framework is modified.

It is also the single meter docs/50 5a asks for: one call log, one token count, one dollar figure,
identical for both arms -- which is what makes the reported cost columns comparable.

WHAT IT DOES AND DOES NOT TOUCH
-------------------------------
It does not rewrite prompts, cache, or alter routing. Request body bytes go up unchanged; the
response body comes back unchanged except for `usage.cost` / `usage.cost_basis` / `usage.cost_source`.

It DOES shape traffic, and that is deliberate: one shared RPM budget and one 429-retry policy for
both arms (see `RateLimiter`), because the endpoint's own limit is shared and each framework would
otherwise absorb it with its own private backoff.

A streamed response is forwarded frame by frame and priced from its usage frame; a stream the
GATEWAY cut without one is priced from the content deltas it forwarded plus a prompt side recovered
from the request (`PromptTokens`), labelled `cost_basis: estimated_from_deltas`; a stream that
produced neither is recorded `metered=false` -- never as $0.

A stream that runs past `--delta-ceiling` content deltas is ended BY THIS
PROXY, with the same frames and the same price, and the upstream socket is closed so the generation
stops being billed. That is a cut this file chooses rather than one it survives, and the difference
is the point: see `abort_is_not_retryable`. The ceiling never enters the request.

THE DEFAULT IS OFF (`DELTA_CEILING_DEFAULT = 0`). Until 2026-09-01 this paragraph named 135_000 as
the default instead -- phrased that way here on purpose, because a test forbids the literal claim and
would read a verbatim quotation of it as the claim itself; that is the third time in this repository
a guard has been tripped by citing what it forbids. The two disagreed for as long as both existed,
and the disagreement cost a real
investigation: a `plan_step` generation on `remDL4` ran to **241,943** content deltas -- 1,820 s, one
call, $0.0706, carrying that run 5 % past its $1.00 budget -- and the first question asked was why
the ceiling had not cut it at 135,000. It was never armed. Whether to arm it is a separate decision
(see `DELTA_CEILING_VALUE` below); saying so accurately is not.

That synthetic frame is SENT TO THE CLIENT, in the two-chunk shape `stream_options.include_usage`
uses -- the cut on a chunk with the `finish_reason`, the price on a chunk with `choices: []`,
followed by the `data: [DONE]` sentinel every OpenAI-compatible client ends a stream on. It
has to be that shape and not one combined chunk: measured 2026-08-26 against arm A's own litellm
1.97.0, a chunk carrying both is dropped, and with `include_usage` on litellm mints a
`Usage(prompt_tokens=0, completion_tokens=0)` of its own instead. That is how a ceiling comes to
fire at twice the real spend -- `rbf_interpolation` logged `Spend limit of $1.0000 reached. Current
spend: $1.0025` while this meter had it at $2.009.

    OPEN[meter-delta-estimator-is-uncalibrated] `estimated_from_deltas` charges ONE token per
    content delta; this proxy's own log says that is low by a length-dependent factor. Over 4,874
    complete streams carrying both numbers, deltas/completion_tokens has median 0.156 (<100 tokens),
    0.803 (1k-5k) and 0.996 (>20k), and the counter is blind to `delta.tool_calls` entirely.
    DEFERRED: the 23 aborted streams on record are 6.9 % of a live campaign's $21.21 of metered
    spend, so re-pricing them mid-campaign charges the tasks before and after the change by two
    different instruments. The rate is derivable in-process from streams already priced.
    proof:absent:tokens_per_delta@benchmarks/meter/proxy.py

USAGE
-----
    python proxy.py --port 8801 --upstream https://host/v1 --api-key sk-... [--log meter.jsonl]

Arms address it with a path prefix that attributes each call without either framework knowing:

    http://127.0.0.1:8801/m/<arm>/<task>/<attempt>/v1/chat/completions -> <upstream>/chat/completions

`/v1/...` also works and is attributed to arm `?`.

WHAT AN ATTEMPT SEGMENT IS FOR, AND HOW TO READ A LOG WRITTEN BEFORE IT EXISTED
------------------------------------------------------------------------------
`(arm, task)` is not an identity: a task-arm gets RE-RUN, and until 2026-08-23 every attempt at one
task added to one bucket. Measured on `/var/tmp/looplab-bench/meter/meter.jsonl`: `B/kcenters`
holds **$2.0086 over 816 calls** against ONE `.done` marker whose run really cost **$1.0070**, so a
naive per-task sum reads 2x the $1.00 ceiling and looks like a budget breach that never happened --
a colleague read it as one. `B/discrete_log` is $1.4749 over 526 calls and
`B/count_riemann_zeta_zeros` $0.8386 over 127.

The third segment is that identity, and it is **the campaign's**, not this proxy's:
`benchmarks/algotune/campaign.sh::next_attempt` allocates `a1`, `a2`, ... per task-arm, appends the
allocation to `$CAMPAIGN_OUT/<arm>-<task>.attempts`, stamps it into the `.done` marker as
`attempt=aN`, and puts it in the URL. A proxy that invented one instead (a start-up counter, a
first-seen-at timestamp) would renumber itself on every restart and could not be joined to a marker.
So this file only ever COPIES what the path says, and copies the empty string when the path says
nothing -- `attempt` is never synthesised here.

Both forms are accepted, and the two-segment one is not deprecated: `docs/52`, `setup_gateway_arm.py`
and any hand-built curl still spell it, and a metered call that arrives on the short path must be
metered, not refused. It is recorded with `"attempt": ""`.

**Reading the 9,456-row log that predates this**: rows written before the change carry NO `attempt`
key at all, and `row.get("attempt")` returning `None` (key absent) rather than `""` (key present,
caller named no attempt) is exactly that distinction -- do not collapse them. Such a row is
attributable to `(arm, task)` and to nothing finer, and **the attempts inside it are not
recoverable**: the only handle left is a gap heuristic over `ts`, and its answer is whatever
threshold you picked. `B/count_riemann_zeta_zeros` splits into 19 / 16 / 14 / 12 / 2 sessions at a
5 / 10 / 15 / 20 / 40-minute gap. So sum an OLD log per `(arm, task)` and label it "all attempts";
sum a NEW one per `(arm, task, attempt)` and label it with the marker's own `attempt=`. Do not mix
the two in one total -- a task whose log spans the change has both shapes, and the honest split is
"before" and "after", not a re-sessionised guess.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding",
    "host",
}

# The `finish_reason` this proxy stamps on a completion it watched the gateway CUT -- one word for
# both transports, the streamed frame and the reassembled body, because the two saying different
# things about the same cut is half of the defect this constant was added for.
#
# It is deliberately the SAME word `looplab/core/llm.py::STREAM_TRUNCATED_FINISH_REASON` already
# stamps on its own salvaged envelopes, for the reason stated there: not OpenAI's `"length"`, which
# names a token limit the MODEL ran into and would assert something false about the generation on
# every gateway cut. This file is a standalone script (it imports no `looplab`), so the word is
# COPIED rather than imported -- `tests/test_meter_stream_adapter.py` asserts the two spellings are
# equal, so the copy cannot drift in silence.
#
# Neither arm reads the VALUE, which is what makes the honest word free: LoopLab reads only a
# finish_reason's PRESENCE (`_envelope_is_truncated`, `_keepalive_stall`), and AlgoTuner names
# `finish_reason` only in a retry-pattern list matched against ERROR STRINGS. Measured 2026-08-24
# against that arm's own litellm 1.97.0: `ModelResponse` accepts it, logs `Unmapped finish_reason
# 'truncated', defaulting to 'stop'` and normalises it away. So the mark is honest on the wire, in
# the meter row and to a human reading either -- and the half of this fix arm A's ACCOUNTANT reads
# is the `usage` beside it, never this word.
STREAM_TRUNCATED_FINISH_REASON = "truncated"

# HOW MANY CONTENT DELTAS THIS METER WILL FORWARD BEFORE IT ENDS THE CALL ITSELF. 0 disables it.
#
# Measured on `meter/meter.jsonl` (9,235 rows, campaign of 2026-08-20..26). 8,830 streams completed
# with the gateway's own usage frame; their delta counts are p99 2,577 / max 132,269 for arm A and
# p99 24,939 / max 126,559 for arm B. 135,000 is above BOTH maxima, so on the whole recorded corpus
# it truncates zero complete answers in either arm -- which is the property `max_tokens` could not
# have (doc 53 9b: a cap low enough to matter cut 7.7 % of arm B against 0.06 % of arm A).
#
# It is deliberately a DELTA COUNT and not a clock. A wall ceiling low enough to pay for itself has
# measured false positives -- at 900 s it cuts two complete arm-A calls and two arm-B ones, and arm
# A's largest legitimate completion ran 1,204 s -- because "slow" and "runaway" are the same
# observable in seconds and different observables in tokens.
# THE VALUE, when the ceiling is switched on. It clears the largest COMPLETE answer either arm has
# produced -- 132,269 forwarded deltas on arm A, 126,559 on arm B -- so nothing a model finished
# saying is ever cut. At 8,192 it would be 2 arm-A calls against 468 arm-B ones, which is a
# 37x asymmetric handicap between the two arms of a comparison.
DELTA_CEILING_VALUE = 135_000

# OFF UNLESS ASKED, and this reverses the value it shipped with a few hours ago. The ceiling is a
# CHANGE TO THE RULER: a stream it cuts is priced differently from one the gateway cut. This box
# runs `benchmarks/watchdog.sh` on a 300 s loop, which pings `/healthz` and calls
# `meter/start_meter.sh --restart` when it fails -- so a default-on ceiling means ANY transient
# proxy failure silently re-meters the rest of a live campaign with a different instrument, and
# task-arms before and after the blip are priced by two rulers. That is not hypothetical: the
# watchdog is running right now (pid 3249487) beside a 23-hour campaign, and the on-disk file
# already carries this code while the loaded process does not.
#
# An instrument that changes the measurement is CHOSEN, never inherited -- the same rule
# `rules_clause` states for the goal card: adopt between arms, never inside one. Turn it on with
# `--delta-ceiling 135000` or `METER_DELTA_CEILING=135000` for the NEXT campaign.
DELTA_CEILING_DEFAULT = 0


def abort_is_not_retryable(model: str, usage: dict, *, stamp: int,
                           truncated: bool = True) -> tuple[list, list]:
    """The ending that makes a cut generation an ANSWER rather than an exception.

    Returns `(frames, wire)` -- the same three-part ending twice, once as objects for the
    reassembling (non-streaming) client and once as SSE bytes for the streaming one.

    WHY A SHAPE, AND NOT A STATUS OR AN ERROR. The measured cost of this defect is not the cut, it
    is the RETRY of the cut: 134 aborted streams in `meter/meter.jsonl` hold $6.65 and 62.9 h, and
    77 of them sit in four CONSECUTIVE runs of 15-23 on one task-arm each. Arm B's longest such run
    is 2. The multiplier is arm A's client-side loop, and reading it settles what the proxy can and
    cannot do about it: `AlgoTuner/interfaces/llm_interface.py` catches
    `(RateLimitError, APIError, APIConnectionError)` and retries TEN times, and its only escape is a
    substring match on a payment/quota list (`"402"`, `"insufficient credits"`, `"quota exceeded"`,
    ...). No HTTP status and no error body makes that loop stop -- litellm's own classifier already
    logged `LiteLLM API non-retryable error` for every one of these and was overridden by the layer
    above it. Five runs in the campaign's logs reached `Exceeded max retries (10)`.
    The only shape that would stop it is a claim about the ACCOUNT that is not true, so the retry
    cannot be refused honestly.
    What can be done honestly is to stop making the call an error. It is not one: the request
    partially SUCCEEDED, the tokens were generated and this meter has already billed them. So the
    cut is delivered as what it is -- a truncated completion with a finish reason and a price -- and
    a loop that retries errors has nothing to catch.

    THE THIRD PART IS THE ONE THAT WAS MISSING. `data: [DONE]` is the sentinel every
    OpenAI-compatible client ends a stream on, and until this function existed the proxy's cut ended
    without it: two minted chunks and then the body simply stopped. The two frames alone are what
    2afb287c fixed and they are preserved exactly -- the cut on a chunk carrying `finish_reason` and
    no usage, the price on a chunk with `choices: []`, because `stream_options.include_usage` reads
    usage only off the empty-choices chunk. This adds the sentinel behind them.
    """
    # `finish_reason` ONLY WHEN THE STREAM REALLY WAS CUT (`truncated`). On the adapted
    # (non-streaming) route `_reassemble` takes the LAST non-empty finish_reason it sees, so an
    # UNCONDITIONAL `truncated` here overwrote a perfectly good `stop` and handed the client a
    # completed answer labelled as cut -- which LoopLab's `_envelope_is_truncated` then refuses to
    # cache while warning that the provider cut it. A stream that saw `[DONE]` is not cut.
    _choice = {"index": 0, "delta": {}}
    if truncated:
        _choice["finish_reason"] = STREAM_TRUNCATED_FINISH_REASON
    cut = {"id": "meter-estimate", "object": "chat.completion.chunk", "model": model,
           "created": stamp, "choices": [_choice]}
    priced = {"id": "meter-estimate", "object": "chat.completion.chunk", "model": model,
              "created": stamp, "choices": [], "usage": usage}
    wire = [b"data: " + json.dumps(cut).encode() + b"\n\n",
            b"data: " + json.dumps(priced).encode() + b"\n\n",
            b"data: [DONE]\n\n"]
    # WITHOUT `id`/`model` for the reassembling caller. `_reassemble` carries every key it does not
    # handle from the LAST frame that had one, so appending these whole replaced the real
    # completion's `id` with "meter-estimate" and its `model` with whatever the REQUEST asked for --
    # on exactly the cut calls that most need correlating with the gateway's own record. Only
    # `choices` and `usage` are wanted there; both are handled explicitly by the reassembler.
    collectable = [{k: v for k, v in f.items() if k in ("choices", "usage")} for f in (cut, priced)]
    return collectable, wire


class Pricing:
    """The price table, loaded once. A reload would let two arms be priced differently."""

    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.path = path
        self.source = raw.get("source", "")
        self.fetched_at = raw.get("fetched_at", "")
        self.reference_slug = raw.get("reference_slug", "")
        self.basis = raw.get("cost_basis", "imputed")
        self.default = raw.get("default") or {}
        self.models = raw.get("models") or {}

    def rate(self, model: str) -> tuple[float, float, str]:
        """(input_per_token, output_per_token, basis) for a model id.

        An unknown model falls back to `default` and says so in the basis, because a silent
        fallback would price a model nobody chose at a price nobody checked.
        """
        entry = self.models.get(model)
        if entry is None:
            # OpenRouter-style ids arrive as `vendor/model`; the gateway serves the bare name.
            entry = self.models.get(model.split("/")[-1])
        if entry is not None:
            return float(entry["input_per_token"]), float(entry["output_per_token"]), self.basis
        return (
            float(self.default.get("input_per_token", 0.0)),
            float(self.default.get("output_per_token", 0.0)),
            self.basis + "-default-fallback",
        )


def _prompt_chars(body: bytes) -> int:
    """Characters of prompt this request carries, counted the SAME way on every call.

    The count itself is exact -- the proxy is holding the request -- and it is deliberately a
    CHARACTER count, not a token count: this file is a standalone stdlib script and has no tokenizer
    for the model behind the gateway, so any token number it produced from the text alone would be a
    guess. What the character count is for is `PromptTokens` below, which turns it into tokens using
    a ratio measured from this endpoint's OWN priced calls.

    `messages` + `tools` are counted rather than the whole body because the rest of a request
    (model, temperature, stream_options) is not prompt and does not scale with it. The exact
    definition matters less than its STABILITY: the same function feeds the calibration and the
    estimate, so the JSON punctuation it includes cancels out of the ratio.
    """
    if not body:
        return 0
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return len(body)
    if not isinstance(payload, dict):
        return len(body)
    parts = [json.dumps(payload[key], ensure_ascii=False)
             for key in ("messages", "tools", "functions", "system", "prompt") if key in payload]
    return sum(len(part) for part in parts) if parts else len(body)


class PromptTokens:
    """How many characters of request the gateway's tokenizer puts in one prompt token.

    WHY THIS EXISTS. A stream the gateway cuts at ~1800 s carries no usage frame, so the proxy
    prices it from the deltas it forwarded -- the COMPLETION side. The prompt side was reported as
    `prompt_tokens: 0`, and that is not a floor, it is a false measurement: the prompt was submitted
    and processed before the first token came back, and on this campaign it is where the money is.
    Measured 2026-08-26 over `meter/meter.jsonl`: across arm A's 1,773 complete streams the prompt
    side is **97.3 %** of metered spend ($13.93 of $14.31), median prompt 42,698 tokens against a
    median completion of 537. On the 129 ABORTED streams -- where the completion side is a 190k-token
    runaway -- the missing prompt side is still **18.6 %** of what they cost ($1.20 on top of $6.44,
    21.5 % for arm A alone), estimated by charging each abort the prompt of the nearest complete call
    in its own task-arm.

    So there are three options and no fourth: report 0 (wrong by the whole prompt), report nothing
    (wrong by the whole call, which is the defect), or measure the ratio. This measures it, from
    traffic that is ALREADY PRICED by the gateway's own usage frames -- the same in-process
    calibration the delta estimator's open item says is derivable and defers.

    IT IS AN ESTIMATE AND IT SAYS SO. Validated 2026-08-26 against `tiktoken` cl100k over 93
    request-sized blocks of this campaign's own prompt text (arm-A logs and AlgoTuner sources):
    per-request chars/token spans 4.05 (p05) to 5.51 (p95) around a pooled 4.91, and predicting each
    block's token count from the POOLED ratio is 6.5 % out at the median, 15.2 % at p90, 31.2 % worst
    case. That is the residual after the systematic part -- the tokenizer's own density -- has been
    measured away, which is exactly what calibrating in-process removes and a hard-coded constant
    would not. Against `prompt_tokens: 0` being 100 % out, it is the better instrument; against the
    gateway's own frame it is not, which is why the synthetic frame names its basis in its own
    fields instead of letting a reader assume the number was reported.

    WITH NO SAMPLE IT DOES NOT GUESS. `estimate()` returns 0 and the basis `unmeasured` until this
    process has seen at least one priced call, so a proxy restarted into a cut stream under-reports
    rather than invents.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.chars = 0
        self.tokens = 0
        self.calls = 0

    def observe(self, chars: int, prompt_tokens: int) -> None:
        """Fold one call the GATEWAY priced into the ratio. Anything else is not evidence."""
        if chars <= 0 or prompt_tokens <= 0:
            return
        with self._lock:
            self.chars += chars
            self.tokens += prompt_tokens
            self.calls += 1

    def estimate(self, chars: int) -> tuple[int, str, float | None, int]:
        """`(prompt_tokens, basis, chars_per_token, calibrating_calls)`.

        `basis` is `measured_by_upstream`'s absence made explicit: `estimated_from_request_chars`
        when there is a ratio, `unmeasured` when there is not -- and in the second case the token
        count is 0, never a number this file made up.
        """
        with self._lock:
            chars_total, tokens_total, calls = self.chars, self.tokens, self.calls
        if chars <= 0 or calls <= 0 or tokens_total <= 0 or chars_total <= 0:
            return 0, "unmeasured", None, calls
        per_token = chars_total / tokens_total
        return max(1, int(round(chars / per_token))), "estimated_from_request_chars", per_token, calls


def _header_cost(headers: dict) -> float | None:
    """A positive cost the upstream reported in a header, or None.

    Zero is NOT a price here: LiteLLM emits `x-litellm-response-cost-original: 0.0` for a model
    group it has no price for, which is the unpriced case, not a free one.
    """
    for key, value in (headers or {}).items():
        if key.lower() == "x-litellm-response-cost-original":
            try:
                amount = float(value)
            except (TypeError, ValueError):
                return None
            return amount if amount > 0 else None
    return None


class RateLimiter:
    """One shared request budget for both arms, because the endpoint applies one.

    Measured 2026-08-20 on the corporate gateway: it is itself a LiteLLM proxy and publishes
    `x-litellm-key-rpm-limit: 50` (team 150). A 20-lane campaign trips that constantly -- a burst of
    20 concurrent calls returned 9 x HTTP 429, and further SEQUENTIAL calls kept 429-ing until the
    window rolled.

    Shaping the traffic HERE rather than letting each arm hit the wall is a parity decision, not
    only a politeness one: AlgoTuner retries with its own backoff and LoopLab with its own, so an
    unshaped 429 storm would charge the two loops differently for the same endpoint condition. One
    queue in front of both makes the endpoint's limit a constant of the experiment instead of a
    variable that correlates with how each framework happens to retry.
    """

    def __init__(self, rpm: int):
        self.rpm = rpm
        self._stamps: list[float] = []
        self._lock = threading.Lock()
        self.waited_s = 0.0

    def acquire(self) -> float:
        """Block until a request slot is free. Returns seconds waited (metered per call)."""
        if self.rpm <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.time()
                self._stamps = [t for t in self._stamps if now - t < 60.0]
                if len(self._stamps) < self.rpm:
                    self._stamps.append(now)
                    self.waited_s += waited
                    return waited
                sleep_for = max(0.05, 60.0 - (now - self._stamps[0]))
            time.sleep(min(sleep_for, 5.0))
            waited += min(sleep_for, 5.0)


class Meter:
    """Append-only JSONL call log. One row per upstream request, written after it completes."""

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()
        self.calls = 0
        self.cost = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.errors = 0

    def record(self, row: dict) -> None:
        with self._lock:
            self.calls += 1
            self.cost += float(row.get("cost") or 0.0)
            self.prompt_tokens += int(row.get("prompt_tokens") or 0)
            self.completion_tokens += int(row.get("completion_tokens") or 0)
            if int(row.get("status") or 0) >= 400 or row.get("error"):
                self.errors += 1
            if not self.path:
                return
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as exc:  # a meter failure must not fail the call it is metering
                print(f"[meter] log write failed: {exc}", file=sys.stderr, flush=True)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "cost_usd": round(self.cost, 6),
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "errors": self.errors,
            }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "looplab-meter/1"

    # -- plumbing ---------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # the meter log is the log; keep stderr for real problems
        return

    def _split_path(self) -> tuple[str, str, str, str]:
        """`/m/<arm>/<task>/<attempt>/v1/x` -> (arm, task, attempt, '/v1/x').

        THE TWO-SEGMENT FORM STILL WORKS and yields `attempt=''`. That is not a courtesy: `docs/52`
        and `setup_gateway_arm.py` document the short path, and refusing a call because its URL is
        the old shape would drop a real, paid request out of the ledger -- the one thing this file
        must never do.

        The two are told apart by WHERE THE UPSTREAM PATH BEGINS, not by a length count, because both
        forms have the same number of `/` once the tail is long enough: `/m/A/t/v1/chat/completions`
        and `/m/A/t/a3/v1/chat` both split into six.

        The test is that the REMAINDER starts with `v1`, not merely that the fourth segment does not.
        Those differ on a shape the repo documents: a client whose `base_url` already carries `/v1`
        sends `/m/A/task/chat/completions`, whose fourth segment is `chat`. Reading "not v1" as "an
        attempt id" made that request proxy to `<upstream>/completions` -- a 404 at the gateway --
        and filed its row under `attempt="chat"`, inventing a bucket the campaign never allocated.
        A metered call arriving on the short path must be METERED, not refused, which is exactly
        what that shape stopped being.

        An attempt id may therefore not be spelled `v1`; `campaign.sh` spells them `a<N>`, and it
        always emits the attempt form WITH `/v1` (`$METER_BASE/m/$ARM/$T/$ATTEMPT/v1`). The residual
        is the un-emitted `/m/A/t/a3/chat/completions` -- an attempt id with a `/v1`-less tail -- 
        which reads as no attempt and a tail of `/a3/chat/completions`; nothing in this repo
        produces it, and guessing would re-open the defect above.
        """
        path = self.path
        if path.startswith("/m/"):
            parts = path.split("/", 5)          # ['', 'm', arm, task, seg, rest]
            if (len(parts) >= 6 and parts[4] != "v1"
                    and (parts[5] == "v1" or parts[5].startswith("v1/"))):
                return parts[2], parts[3], parts[4], "/" + parts[5]
            if len(parts) >= 5:
                return parts[2], parts[3], "", "/" + "/".join(parts[4:])
        return "?", "?", "", path

    def _send(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, message: str, arm: str, task: str, attempt: str,
              t0: float) -> None:
        body = json.dumps({"error": {"message": message, "type": "meter_proxy"}}).encode()
        self.server.meter.record({
            "ts": time.time(), "arm": arm, "task": task, "attempt": attempt, "status": status,
            "latency_ms": round((time.time() - t0) * 1000, 1), "error": message, "metered": False,
        })
        self._send(status, body, {"Content-Type": "application/json"})

    # -- the actual proxy -------------------------------------------------------------------
    def do_GET(self):
        self._proxy(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._proxy(self.rfile.read(length) if length else b"")

    # THE NGINX ASYMMETRY, and why this exists.
    #
    # The gateway in front of the models is nginx with `proxy_read_timeout` at 300 s: measured
    # 2026-08-23, forty non-streaming arm-A calls returned `504 Gateway Time-out` at latency
    # 300.011 s each, byte-identical nginx HTML. A STREAM keeps bytes flowing, so it survives to the
    # model server's own ~1800 s limit -- which is why arm B (100% streaming) never saw a 504 and
    # arm A (0% streaming) saw forty. One task-arm, `count_riemann_zeta_zeros`, spent three and a
    # half of its four wall-clock hours inside those timeouts and reached $0.14 of its $1.00.
    #
    # That is a difference between TRANSPORTS, not between agents, and neither framework chose it.
    # Fixing it inside either one would mean editing a thing under measurement, so it is fixed here:
    # the meter asks upstream for a stream on the client's behalf and hands back the ordinary
    # non-streaming JSON the client asked for. Both arms then face the same gateway window.
    #
    # OFF BY DEFAULT (`METER_STREAM_ADAPT=1` to enable). Turning it on mid-campaign would split an
    # arm into two halves measured through two transports, which is the defect it exists to remove.
    def _adapt_to_stream(self, payload: dict) -> tuple:
        """`(upstream_body, adapted)` — the same request, asked for as a stream."""
        if os.environ.get("METER_STREAM_ADAPT") != "1" or payload.get("stream"):
            return None, False
        out = dict(payload)
        out["stream"] = True
        opts = dict(out.get("stream_options") or {})
        opts["include_usage"] = True             # or the reassembled answer carries no usage at all
        out["stream_options"] = opts
        return json.dumps(out).encode(), True

    @staticmethod
    def _reassemble(frames: list) -> dict:
        """One non-streaming completion, rebuilt from the deltas of a streamed one.

        Content, REASONING, role, finish_reason, usage and tool_call fragments, in arrival order. A
        field this does not know about is carried from the LAST frame that had it, so an unfamiliar
        extension survives instead of being silently dropped -- the client is the framework under
        measurement and must see what the provider sent.

        Reasoning is carried under the delta's OWN key (`reasoning_content` here, `reasoning` on an
        OpenRouter-shaped stream) rather than folded into `content`, because they are different
        fields to every reader and merging them would put a model's private thinking into the answer
        the arm parses for commands. It has to be carried at all for the reason this whole path
        exists: a reasoning model the gateway cuts mid-think has spent EVERYTHING on
        `reasoning_content` with `content` still empty -- one real cut on this box carried 220,685
        deltas, all of them reasoning -- so dropping it handed the client an empty answer for a call
        that had produced a great deal. `looplab/core/llm.py::salvaged_lengths` is the same reading
        one layer up, and the arm's own litellm keeps the key (measured 2026-08-24, 1.97.0).
        """
        content: list = []
        reasoning: dict = {}
        tool_calls: dict = {}
        finish = None
        usage = None
        base: dict = {}
        for f in frames:
            if not isinstance(f, dict):
                continue
            for k, v in f.items():
                if k not in ("choices", "usage", "object"):
                    base[k] = v
            if isinstance(f.get("usage"), dict):
                usage = f["usage"]
            for ch in (f.get("choices") or []):
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                for key in ("reasoning_content", "reasoning"):
                    if d.get(key):
                        reasoning.setdefault(key, []).append(d[key])
                for tc in (d.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(idx, {"index": idx, "type": tc.get("type", "function"),
                                                       "id": tc.get("id"),
                                                       "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
        message = {"role": "assistant", "content": "".join(content)}
        for key, parts in reasoning.items():
            message[key] = "".join(parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        out = dict(base)
        out["object"] = "chat.completion"
        out["choices"] = [{"index": 0, "message": message, "finish_reason": finish}]
        if usage is not None:
            out["usage"] = usage
        return out

    def _proxy(self, body: bytes) -> None:
        t0 = time.time()
        arm, task, attempt, tail = self._split_path()

        if tail in ("/healthz", "/m/healthz"):
            self._send(200, json.dumps(self.server.meter.snapshot()).encode(),
                       {"Content-Type": "application/json"})
            return

        upstream = self.server.upstream.rstrip("/")
        if tail.startswith("/v1/"):
            tail = tail[3:]                      # upstream base already ends in /v1
        url = upstream + tail

        streaming = False
        model = ""
        if body:
            try:
                payload = json.loads(body)
                streaming = bool(payload.get("stream"))
                model = str(payload.get("model") or "")
            except (ValueError, AttributeError):
                pass

        req_headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        # Ask upstream for an uncompressed body. The alternative -- forwarding the client's
        # `Accept-Encoding: gzip` -- hands us bytes we would have to decompress before we can find
        # `usage`, and returning them with the encoding header stripped is how a litellm client
        # reads `0x8b` and reports an InternalServerError from a 200 OK.
        req_headers["Accept-Encoding"] = "identity"
        if self.server.api_key:
            req_headers["Authorization"] = "Bearer " + self.server.api_key
        req = urllib.request.Request(url, data=body or None, headers=req_headers,
                                     method=self.command)

        if streaming:
            self._proxy_stream(req, arm, task, attempt, tail, model, t0)
            return

        # The client asked for a whole answer. With the adapter on, ask UPSTREAM for a stream (so
        # nginx's 300 s read window never fires) and give the client the whole answer regardless.
        adapted_body, adapted = (None, False)
        if tail.endswith("/chat/completions"):
            try:
                adapted_body, adapted = self._adapt_to_stream(json.loads(body) if body else {})
            except (ValueError, TypeError):
                adapted_body, adapted = None, False
        if adapted:
            req = urllib.request.Request(url, data=adapted_body, headers=req_headers,
                                         method=self.command)
            self._proxy_stream(req, arm, task, attempt, tail, model, t0, collect_for_client=True)
            return

        tally = {"attempts": 1, "queued_s": 0.0}
        attempts, queued_s = 1, 0.0
        try:
            # No proxy env: the gateway is reached directly, and http_proxy is set on this box
            # for the outside world. urlopen would otherwise send corporate traffic through it.
            resp, attempts, queued_s = self._open_upstream(req, tally)
            with resp:
                status = resp.status
                raw = resp.read()
                resp_headers = {k: v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            resp_headers = {k: v for k, v in (exc.headers or {}).items()}
            # The tuple above never bound: take what the retry loop actually did.
            attempts, queued_s = tally["attempts"], tally["queued_s"]
        except Exception as exc:  # noqa: BLE001 - upstream failures are data, not crashes
            self._fail(502, f"upstream {type(exc).__name__}: {exc}", arm, task, attempt, t0)
            return

        latency_ms = round((time.time() - t0) * 1000, 1)
        row = {
            "ts": time.time(), "arm": arm, "task": task, "attempt": attempt, "path": tail,
            "model": model, "status": status, "latency_ms": latency_ms, "stream": streaming,
            "attempts": attempts, "queued_s": round(queued_s, 2),
        }

        out = raw
        if status < 400:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                usage = data["usage"]
                pin = int(usage.get("prompt_tokens") or 0)
                pout = int(usage.get("completion_tokens") or 0)
                rate_in, rate_out, basis = self.server.pricing.rate(model or data.get("model", ""))
                header_cost = _header_cost(resp_headers)
                if "cost" in usage and usage.get("cost") is not None:
                    # The upstream priced it itself (a real OpenRouter, say). Never overwrite a
                    # provider's own invoice with an imputed one.
                    # OPEN[meter-upstream-zero-cost-is-an-invoice] a body `usage.cost` of 0.0 is
                    # accepted as an authoritative $0 while the header rule refuses the same zero.
                    # proof:`present:if "cost" in usage and usage.get("cost") is not None:@benchmarks/meter/proxy.py`
                    # REVIEW 2026-08-30 (money): `_header_cost` thirty lines up states the rule —
                    # "Zero is NOT a price here: LiteLLM emits ... 0.0 for a model group it has no
                    # price for, which is the unpriced case, not a free one" — and this branch (and
                    # its streaming twin in `_proxy_stream`) skips it: `cost: 0.0` in the BODY lands
                    # as `{metered: true, cost: 0.0, cost_basis: "upstream"}` and no imputation
                    # runs. The corporate gateway IS LiteLLM and already emits the header form; the
                    # day it stamps the same zero into the body, every call prices at $0 with a
                    # green `metered: true` — the founding "budget never binds" defect back, now
                    # asserted as an invoice. Treat a zero body cost like the zero header cost
                    # (fall through to imputation), or label it so no reader mistakes it.
                    cost = float(usage["cost"])
                    basis = "upstream"
                elif header_cost is not None:
                    # The corporate gateway is itself LiteLLM and publishes its own figure in
                    # `x-litellm-response-cost-original`. Measured 2026-08-20 it is 0.0 for this
                    # model group (`x-litellm-model-name: openai/default`, i.e. unpriced there too),
                    # so this branch is dormant today -- but if that endpoint ever starts pricing,
                    # its own number outranks anything imputed here.
                    cost = header_cost
                    usage["cost"] = cost
                    basis = "upstream-header"
                else:
                    cost = pin * rate_in + pout * rate_out
                    usage["cost"] = cost
                usage["cost_basis"] = basis
                usage["cost_source"] = self.server.pricing.fetched_at
                # The gateway just told us how many tokens this exact prompt was. That is the only
                # evidence there is for what a character is worth here, and it is free.
                self.server.prompt_scale.observe(_prompt_chars(body), pin)
                out = json.dumps(data).encode()
                row.update({
                    "prompt_tokens": pin, "completion_tokens": pout, "cost": cost,
                    "cost_basis": basis, "metered": True,
                    "model_reported": data.get("model", ""),
                    "id": data.get("id", ""),
                })
                if pout:
                    row["tok_per_s"] = round(pout / max(latency_ms / 1000.0, 1e-6), 2)
            else:
                row["metered"] = False
                row["note"] = "no usage object in response"
        else:
            row["metered"] = False
            # OPEN[meter-error-body-shape-skips-the-ledger] a non-object JSON error body raises out
            # of the handler BEFORE the row is recorded and before anything is sent to the client.
            # proof:`line:upstream_error&&json.loads(raw)@benchmarks/meter/proxy.py`
            # REVIEW 2026-08-30 (money): `json.loads(b'"internal error"')` (a JSON string, array or
            # number — shapes a busy gateway's own edge really produces) succeeds and `.get` then
            # raises AttributeError, which `except ValueError` does not catch. The exception
            # escapes before `self.server.meter.record(row)` below, so the paid request VANISHES
            # from the ledger — the one thing this file says it must never do — and the client gets
            # a bare connection drop instead of the legible 500, which the arms' retry loops
            # classify differently. Catch `(ValueError, AttributeError)` or guard with
            # `isinstance(parsed, dict)`. Driven live: upstream 500 with body `"internal error"`
            # -> client RemoteDisconnected, rows recorded: 0.
            try:
                row["upstream_error"] = json.loads(raw).get("error")
            except ValueError:
                row["upstream_error"] = raw[:400].decode("utf-8", "replace")

        self.server.meter.record(row)
        resp_headers["Content-Type"] = resp_headers.get("Content-Type", "application/json")
        if row.get("cost") is not None:
            resp_headers["x-meter-cost-usd"] = f"{row.get('cost', 0.0):.8f}"
        self._send(status, out, resp_headers)


    def _open_upstream(self, req, tally: dict | None = None):
        """Shape, send, and absorb a 429 the way BOTH arms should have it absorbed.

        Returns (response, attempts, waited_s) or raises. A 429 is retried here with the endpoint's
        own `Retry-After` when it sends one, so neither framework's private retry policy becomes
        part of the measurement.

        `tally` IS THE COUNT THAT SURVIVES THE RAISE. Both callers used to bind the returned tuple,
        which means a request that was retried five times and then failed was written down as
        `attempts: 1, queued_s: 0.0` -- a false statement about exactly the calls a retry counter
        exists to count. Anything passed here is updated in place, before each attempt and after
        each sleep, so the row can be honest on the failure path too.
        """
        limiter = self.server.limiter
        attempts = 0
        waited = 0.0
        while True:
            attempts += 1
            waited += limiter.acquire()
            if tally is not None:
                tally["attempts"], tally["queued_s"] = attempts, waited
            try:
                return self.server.opener.open(req, timeout=self.server.timeout), attempts, waited
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempts > self.server.max_retries:
                    raise
                retry_after = (exc.headers or {}).get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 0.0
                except (TypeError, ValueError):
                    delay = 0.0
                if delay <= 0:
                    delay = min(2.0 ** attempts, 30.0)
                exc.close()
                time.sleep(delay)
                waited += delay
                if tally is not None:
                    tally["queued_s"] = waited

    def _proxy_stream(self, req, arm: str, task: str, attempt: str, tail: str, model: str,
                      t0: float, *, collect_for_client: bool = False) -> None:
        """Forward an SSE stream chunk-by-chunk, pricing the usage frame on its way past.

        LoopLab streams by default (`Settings.llm_stream`) and AlgoTuner does not. Metering only
        the non-streaming half would price one arm and not the other -- and switching LoopLab's
        streaming off to dodge that would change the loop under measurement (its stall watchdogs
        read the token stream). So the usage frame is rewritten in flight instead.

        Frames are forwarded the moment they arrive: a proxy that buffered them would make every
        liveness signal in the run a property of this file.
        """
        row = {"ts": time.time(), "arm": arm, "task": task, "attempt": attempt, "path": tail,
               "model": model, "stream": True}
        # Counted from the request the proxy is HOLDING, before anything upstream can go wrong with
        # it. This is the only prompt-side evidence that survives a cut, so it is taken up front
        # rather than looked for later among objects the abort path may not have.
        prompt_chars = _prompt_chars(req.data or b"")
        if collect_for_client:
            # The client is NOT expecting SSE. Frames are collected here and answered as one
            # JSON body, so nothing about this is visible to it except that the call did not
            # time out at nginx's 300 s read window.
            row["stream_adapted"] = True
        collected: list = []
        tally = {"attempts": 1, "queued_s": 0.0}
        answered = False        # did the CLIENT get an HTTP response? see the tail below
        try:
            resp, attempts, queued_s = self._open_upstream(req, tally)
            row.update({"attempts": attempts, "queued_s": round(queued_s, 2)})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            # Set INSIDE the try above, so a failure used to leave both fields absent entirely.
            row.update({"attempts": tally["attempts"],
                        "queued_s": round(tally["queued_s"], 2)})
            row.update({"status": exc.code, "metered": False,
                        "latency_ms": round((time.time() - t0) * 1000, 1),
                        "upstream_error": raw[:400].decode("utf-8", "replace")})
            self.server.meter.record(row)
            self._send(exc.code, raw, {"Content-Type": "application/json"})
            return
        except Exception as exc:  # noqa: BLE001
            self._fail(502, f"upstream {type(exc).__name__}: {exc}", arm, task, attempt, t0)
            return

        # Read ONCE from the upstream response, before any frame is parsed: it is a property of the
        # response, not of any frame, and re-reading it per frame would say the same thing N times.
        _hdr_cost = _header_cost(dict(resp.headers))

        if not collect_for_client:
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

        # NOTHING A CLIENT MUST SEE MAY LEAVE THROUGH `emit()` ALONE. It is the SSE writer and it
        # returns immediately while collecting, so a frame this proxy MINTS -- as opposed to one it
        # forwards, which `collected` already holds -- reaches the streaming client and nobody else.
        # That is exactly how the `estimated_from_deltas` usage frame below came to be dropped on
        # the one path that had no other copy of it. Mint into a variable, then route it BOTH ways.
        def emit(payload: bytes) -> None:
            if collect_for_client:      # the client gets ONE JSON body at the end, not frames
                return
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        pin = pout = 0
        cost = 0.0
        basis = ""
        # Where `prompt_tokens` in the row below came from. Empty until something establishes it, so
        # a row can never imply the gateway reported a prompt count it did not.
        prompt_basis = ""
        # Did the GATEWAY send a usage frame? Its own flag rather than `bool(basis)`, because the
        # synthesis below overwrites `basis` and the reassembled body has to be able to tell "the
        # provider closed the books on this call" from "we closed them for it".
        usage_frame_seen = False
        # Content deltas SEEN, counted as they pass. They are the only evidence left when a stream
        # ends without its usage frame, and measured 2026-08-22 that is not a corner case: this
        # gateway CUTS a generation at ~1800 s (eight streams ended at 1817-1824 s, status 200, no
        # exception on our side and therefore not our socket timeout). LoopLab prices from
        # `usage.cost`, so a stream with no usage frame produced NO `llm_usage` event at all -- the
        # run's own accounting was short four calls on one task and never said so. A budget that
        # silently under-counts is worse than one that stops early: the arm looks cheap.
        deltas = 0
        # THE METER'S OWN CEILING, and the reason it exists is not the money it caps.
        #
        # Cutting at N deltas saves 26 % of those 62.9 h on its own (recomputed over the same log at
        # N=135,000); refusing the RETRY saves 69 %. The ceiling is here because it is what makes the
        # refusal RELIABLE. `abort_is_not_retryable` can only be delivered on a call this proxy is
        # still holding: when the GATEWAY ends the generation, what reaches the client is whatever
        # the dying socket leaves behind, and this proxy is in the salvage business by then --
        # 31 of the 134 recorded aborts carry a `BrokenPipeError` from trying. When the METER ends
        # it, the ending is chosen, complete and identical every time. Together they are 78 % of the
        # money and 77 % of the clock on the recorded corpus.
        #
        # It never enters the request: `max_tokens` is a generation parameter and this module does
        # not rewrite requests (doc 53 9b). The client sees exactly the observable the gateway's own
        # ~1800 s cut already produces, so nothing downstream learns a new shape.
        ceiling = int(getattr(self.server, "delta_ceiling", DELTA_CEILING_DEFAULT) or 0)
        # Did THIS PROXY end the call, as opposed to watching the gateway end it? The two are priced
        # the same way and are the same observable to a client, and they are still different events:
        # one is a runaway generation we stopped, the other is a gateway we outlived. A row that
        # could not tell them apart would make the ceiling's own false-positive rate unmeasurable.
        cut_by_meter = False
        # THE LAST TWO BYTES THIS PROXY PUT ON THE WIRE, and they decide whether a minted frame is
        # readable at all. SSE events are separated by a BLANK LINE, and `for line in resp` hands
        # that blank line over as a line of its own -- so a stream that stops between a `data:` line
        # and its terminator leaves the event OPEN. Anything minted after it is then glued into the
        # previous event as a second `data:` line, the two payloads are concatenated, and a
        # conformant parser reports `Extra data: line 2 column 1`. Measured 2026-08-26 against arm
        # A's own litellm 1.97.0: that is a `MidStreamFallbackError` wrapping an
        # `APIConnectionError` -- one of the three exception types AlgoTuner's ten-attempt loop
        # retries. The frames were correct and unreadable, which is the same thing as absent.
        wire_tail = b""
        # `[DONE]` IS HELD BACK, NOT FORWARDED IN PLACE. A spec-compliant SSE client stops at the
        # terminator -- openai-python's `Stream.__stream__` BREAKS on it and LoopLab's
        # `defer_inband_error` keeps that rule -- so an estimate emitted after it reaches nobody,
        # which is exactly where the synthesized usage frame used to go whenever a stream ended
        # tidily WITHOUT a usage frame. It is also the flag the row below needs: a stream that
        # reached `[DONE]` ended, whatever else it did or did not carry.
        done_seen = False
        swallow_blank = False
        # OPEN[meter-swallows-the-done-sentinel] the held-back `[DONE]` is re-emitted on exactly one
        # of the three exit paths, so every COMPLETE stream loses its terminator.
        # proof:`absent:if done_seen@benchmarks/meter/proxy.py`
        # REVIEW 2026-08-30 (protocol): the swallow below says "re-emitted at the very end, after
        # the estimate", and that is true only of the `elif not basis and deltas:` estimate branch
        # (whose `wire` carries the sentinel). On the dominant path — usage frame seen, stream ended
        # tidily (8,830 of 9,235 recorded rows) — and on the empty-stream path, the sentinel is
        # consumed and the chunked body just ends. Driven live: 2 content frames -> finish -> usage
        # -> `[DONE]` reaches the client with no `[DONE]`. Both current arms end on body EOF, so
        # nothing breaks TODAY; but the module header promises "unchanged except usage.cost", and by
        # this file's own reading (a stream that never reached its terminator was CUT) any strict
        # SSE observer downstream must classify every clean answer as cut. The one fixture that
        # sends `[DONE]` (`_frame_done` in tests/test_meter_delta_ceiling_is_not_retryable.py)
        # never asserts the client saw it, which is how this shipped. Re-emit on the other exits.
        try:
            for line in resp:
                out = line
                if line.startswith(b"data: [DONE]"):
                    done_seen = True
                    swallow_blank = True         # ...and the blank line that terminates IT
                    continue                     # re-emitted at the very end, after the estimate
                if swallow_blank:
                    # The `\n` that closed the held `[DONE]` event goes with it; anything else on
                    # this line is an ordinary frame and is forwarded normally.
                    swallow_blank = False
                    if not line.strip():
                        continue
                if line.startswith(b"data: "):
                    try:
                        frame = json.loads(line[6:])
                    except ValueError:
                        frame = None
                    if isinstance(frame, dict) and collect_for_client:
                        collected.append(frame)
                    if isinstance(frame, dict):
                        for ch in (frame.get("choices") or []):
                            d = ch.get("delta") or {}
                            # `reasoning` too: `_reassemble` above reads BOTH spellings
                            # (`reasoning_content` here, `reasoning` on an OpenRouter-shaped
                            # gateway) and two halves of one file disagreeing about what a
                            # reasoning delta is means a cut reasoning stream in the second
                            # shape counts ZERO deltas, skips the estimate entirely, and is
                            # recorded `metered: false, cost: 0.0` -- the silent under-count
                            # the estimator exists to close.
                            if (d.get("content") or d.get("reasoning_content")
                                    or d.get("reasoning")):
                                deltas += 1
                    if isinstance(frame, dict) and isinstance(frame.get("usage"), dict):
                        usage_frame_seen = True
                        usage = frame["usage"]
                        pin = int(usage.get("prompt_tokens") or 0)
                        pout = int(usage.get("completion_tokens") or 0)
                        rate_in, rate_out, basis = self.server.pricing.rate(
                            model or frame.get("model", ""))
                        if usage.get("cost") is not None:
                            cost, basis = float(usage["cost"]), "upstream"
                        elif _hdr_cost is not None:
                            # THE GATEWAY'S OWN INVOICE, read on this route too. `_header_cost` was
                            # consulted only in `_proxy`, so with `METER_STREAM_ADAPT=1` the very
                            # same non-streaming call was priced by IMPUTATION while the unadapted
                            # one was priced by the header -- a price that depends on the transport,
                            # which is the difference the adapter exists to remove. Latent today
                            # (this gateway emits 0.0, which `_header_cost` rightly refuses as "not
                            # a price") and load-bearing the day it stops being.
                            cost, basis = _hdr_cost, "upstream-header"
                            usage["cost"] = cost
                        else:
                            cost = pin * rate_in + pout * rate_out
                            usage["cost"] = cost
                        usage["cost_basis"] = basis
                        usage["cost_source"] = self.server.pricing.fetched_at
                        self.server.prompt_scale.observe(prompt_chars, pin)
                        prompt_basis = "reported_by_upstream"
                        out = b"data: " + json.dumps(frame).encode() + b"\n"
                emit(out)
                wire_tail = (wire_tail + out)[-2:]
                # AFTER the frame is forwarded, never instead of it: a delta this proxy has already
                # counted and already priced must reach the client, or the ceiling would bill for
                # tokens it swallowed. `usage_frame_seen` excludes the one case where cutting could
                # destroy evidence -- the gateway has closed the books, so there is nothing left to
                # protect the client from and the synthesis below would not run anyway.
                if ceiling and deltas >= ceiling and not usage_frame_seen:
                    cut_by_meter = True
                    break
            if cut_by_meter:
                # STOP PAYING FOR TOKENS NOBODY WILL SEE. The whole saving is here: the generation
                # runs until someone hangs up, so the socket goes NOW rather than after the frames
                # are built. `finally` closes it again, which is a no-op.
                resp.close()
            if usage_frame_seen:
                pass                            # the gateway priced it; nothing to estimate
            elif not basis and deltas:
                # THE STREAM PRODUCED TOKENS AND NOBODY PRICED THEM. Synthesise the usage frame the
                # gateway did not send, from the deltas actually forwarded, and label it for what it
                # is. Injected rather than merely logged because the arm's accountant reads the
                # stream, not this file: logging it here would fix the report and leave the run's own
                # `llm_budget_usd` blind, which is the defect, not a smaller version of it.
                #
                # WHAT ONE DELTA IS WORTH, measured against this proxy's OWN log rather than assumed.
                # This block used to assert "one delta per token is the SSE shape every provider on
                # this box emits". That is false as a general claim and the evidence is in
                # `meter/meter.jsonl`: 4,874 COMPLETE streams carry both `deltas_seen` and the
                # gateway's authoritative `completion_tokens`, and the ratio has a strong length
                # dependence -- median 0.156 below 100 completion tokens, 0.384 at 100-1k, 0.803 at
                # 1k-5k, 0.981 at 5k-20k and 0.996 above 20k (overall median 0.476, and deltas are
                # BELOW completion_tokens on 99.88 % of rows). The short-stream gap is structural,
                # not noise: this counter reads `delta.content` / `delta.reasoning_content` and a
                # completion delivered as a TOOL CALL arrives on `delta.tool_calls[].function
                # .arguments`, which it never sees. 505 complete streams of >500 completion tokens
                # spent under 20 % of them on content deltas -- 1.17 M tokens this estimator would
                # have valued at less than a fifth.
                #
                # It is still a FLOOR in every one of those directions, which is the honest side to
                # be wrong on for a budget, and the 23 aborts actually on record are all long
                # runaway generations (226k-238k deltas at 1817-1830 s) where the ratio is ~1.0. But
                # a floor that is 5x low on a tool-call stream is a different instrument from the one
                # the old comment described, so the number is not corrected here: the open item
                # in this module's docstring holds the measurement and says why it is deferred.
                rate_in, rate_out, est_basis = self.server.pricing.rate(model or "")
                # THE PROMPT SIDE IS NOT ZERO AND MUST NOT SAY IT IS. `prompt_tokens: 0` used to
                # stand here. It is not the honest floor the completion side is -- it is a false
                # measurement, and on this campaign it is the false one that matters: arm A's prompt
                # side is 97.3 % of its metered spend, and charging each of the 129 aborts the prompt
                # of the nearest complete call in its own task-arm puts $1.20 on top of their $6.44.
                # See `PromptTokens` for the ratio, its calibration and its measured error.
                pin, prompt_basis, per_token, cal_calls = self.server.prompt_scale.estimate(
                    prompt_chars)
                pout = deltas
                cost = pin * rate_in + deltas * rate_out
                basis = "estimated_from_deltas"
                usage = {
                    "prompt_tokens": pin, "completion_tokens": deltas,
                    "total_tokens": pin + deltas,
                    "cost": cost, "cost_basis": basis,
                    "cost_source": self.server.pricing.fetched_at,
                    # EVERY NUMBER ABOVE NAMES WHERE IT CAME FROM, in the frame itself rather than
                    # only in this proxy's log, because the reader that has to act on it is the
                    # client's accountant and it never sees the log.
                    "meter_prompt_tokens_basis": prompt_basis,
                    "meter_prompt_chars": prompt_chars,
                    "meter_completion_tokens_basis": "counted_from_forwarded_deltas",
                    "meter_note": "upstream ended the stream without a usage frame; "
                                  "completion_tokens is a FLOOR counted from forwarded deltas and "
                                  f"prompt_tokens is {prompt_basis}",
                }
                if per_token is not None:
                    usage["meter_chars_per_prompt_token"] = round(per_token, 4)
                    usage["meter_prompt_calibration_calls"] = cal_calls
                if cut_by_meter:
                    # SAY WHO CUT IT, in the frame and not only in the log. The completion count
                    # stops being a floor on the gateway's generation here and becomes an exact
                    # count of what this proxy forwarded, and a client's accountant that read the
                    # sentence above without this one would understate the same call twice.
                    usage["meter_cut_by"] = "delta_ceiling"
                    usage["meter_delta_ceiling"] = ceiling
                    usage["meter_completion_tokens_basis"] = "counted_from_forwarded_deltas_at_ceiling"
                    usage["meter_note"] = (
                        f"the meter stopped forwarding at its delta ceiling of {ceiling}; the "
                        "generation was still running upstream and was not billed beyond this "
                        f"point. prompt_tokens is {prompt_basis}")

                # TWO FRAMES, NOT ONE, AND THAT IS THE WHOLE FIX ON THE STREAMING SIDE. A single
                # frame carrying BOTH a populated `choices` array and a `usage` block is not the
                # shape an OpenAI-compatible client reads usage out of, and measured 2026-08-26
                # against arm A's own litellm 1.97.0 it is silently dropped: streaming that frame
                # gives `usage=None`, and streaming it with `stream_options={"include_usage": true}`
                # gives a MINTED `Usage(prompt_tokens=0, completion_tokens=0)` with no cost -- the
                # client's accountant reading a zero this proxy never sent. The conformant shape is
                # the one `include_usage` itself uses: the cut arrives on a chunk with the
                # `finish_reason` and no usage, then the price arrives on a chunk with `choices: []`.
                # Both survive `_reassemble` (finish from the first, usage from the second), and arm
                # B, which reads `usage` off ANY chunk (`looplab/core/llm.py`, `if ev.usage`), is
                # unaffected either way.
                #
                # BUILT ONCE, DELIVERED BY WHICHEVER ROUTE THE CLIENT IS ON. Writing it through
                # `emit()` alone is how it came to be lost: `emit()` returns immediately when the
                # client is being collected for, so the adapted (non-streaming) client got a body
                # with no `usage` at all and a null finish reason -- a truncated answer arriving as a
                # clean 200 with nothing for that arm's accountant to read.
                #
                # AND THE SENTINEL BEHIND THEM. `abort_is_not_retryable` owns all three parts and
                # the reasoning for them; what matters here is that a cut leaves through ONE door,
                # so the streamed client and the reassembled one cannot end differently.
                frames, wire = abort_is_not_retryable(model, usage, stamp=int(time.time()),
                                                          truncated=not done_seen)
                if collect_for_client:
                    collected.extend(frames)    # `[DONE]` is a wire sentinel, not a frame
                else:
                    # CLOSE THE EVENT THE STREAM DIED INSIDE, before minting into it. See
                    # `wire_tail`: without this the first minted frame is swallowed by the last
                    # forwarded one and the client raises where it used to be handed an answer.
                    if wire_tail and not wire_tail.endswith(b"\n\n"):
                        emit(b"\n" if wire_tail.endswith(b"\n") else b"\n\n")
                    for payload in wire:
                        emit(payload)
            emit(b"")           # terminating zero-length chunk
            if collect_for_client:
                # ONE body, built from the frames, with the usage frame this proxy already priced
                # in place — so the client sees exactly the shape it asked for and the ledger sees
                # exactly what it would have seen either way.
                whole = json.dumps(self._reassemble(collected)).encode()
                self._send(resp.status, whole, {"Content-Type": "application/json"})
                answered = True
            else:
                self.wfile.flush()
        except Exception as exc:  # noqa: BLE001 - a broken client must not take the server down
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            resp.close()

        # THE ADAPTED ROUTE MUST ALWAYS ANSWER. On the streaming route the headers went out before
        # the loop, so a mid-stream failure still leaves the client a truncated body it can salvage.
        # On this route nothing has been written yet and `_send` lives inside the `try`, so an
        # upstream reset or a decode error returned with NO status line at all: a keep-alive client
        # (httpx/litellm pool every one of them) blocks until its own timeout, while the ledger
        # records the call as a 200. Driven 2026-08-25 against an upstream that resets mid-chunk.
        #
        # The partial reassembly is sent rather than a bare 502 whenever any frame arrived, because
        # preserving a cut generation is what this adapter is FOR -- discarding it here would repeat,
        # one layer down, the loss `core/llm.py`'s stream salvage was written to stop.
        if collect_for_client and not answered:
            try:
                if collected:
                    partial = self._reassemble(collected)
                    for _ch in (partial.get("choices") or []):
                        _ch["finish_reason"] = STREAM_TRUNCATED_FINISH_REASON
                    self._send(200, json.dumps(partial).encode(),
                               {"Content-Type": "application/json"})
                else:
                    self._send(502, json.dumps({"error": {
                        "message": row.get("error") or "upstream stream failed before any frame",
                        "type": "meter_stream_failed"}}).encode(),
                        {"Content-Type": "application/json"})
            except Exception:  # noqa: BLE001 - the socket may already be gone; the row still lands
                pass

        # OPEN[meter-midstream-death-holds-the-keepalive-socket] a mid-stream failure returns with
        # no terminating chunk and the connection left open, so pooled clients hang until their own
        # read timeout.
        # proof:absent:close_connection@benchmarks/meter/proxy.py
        # REVIEW 2026-08-30 (robustness): the `emit(b"")` terminator lives inside the `try`; the
        # exception path records the row and returns with the chunked body unfinished on a
        # keep-alive connection. httpx/litellm pool every connection here (this file says so), so
        # after an upstream death the arm sits on a half-finished body for its own stall timeout —
        # minutes of dead wall clock per failure, times the retry that follows. urllib probes hide
        # it (they send Connection: close). One line in the except path turns that into an
        # immediate, salvageable EOF.
        latency_ms = round((time.time() - t0) * 1000, 1)
        # OPEN[meter-stream-rows-are-anonymous] streaming rows carry neither the response `id` nor
        # `model` the non-stream rows record, the estimate discards its own pricing-fallback basis,
        # and nothing names the upstream when two meter instances share one default log path.
        # proof:absent:upstream_host@benchmarks/meter/proxy.py
        # REVIEW 2026-08-30 (auditability): `_proxy` records `model_reported` and `id` for exactly
        # the correlate-with-the-gateway need `abort_is_not_retryable`'s comment states, and the cut
        # streams — the rows that most need correlating — get neither, though every frame carries
        # both. `est_basis` from `pricing.rate` is dropped, so an estimate priced off the `default`
        # table is indistinguishable from one priced at the pinned model rate, precisely for the
        # unknown-model rows where a wrong rate is likeliest. And `start_meter.sh` defaults every
        # instance to one `meter.jsonl` with no per-row upstream identity, so a second instance
        # started without METER_LOG interleaves unattributable spend. Three one-line row additions.
        row.update({"status": resp.status, "latency_ms": latency_ms, "prompt_tokens": pin,
                    "completion_tokens": pout, "cost": cost, "cost_basis": basis,
                    "metered": bool(basis)})
        row["deltas_seen"] = deltas
        if prompt_basis:
            row["prompt_tokens_basis"] = prompt_basis
        if basis == "estimated_from_deltas":
               row["prompt_chars"] = prompt_chars
               # `stream_aborted` MEANS THE STREAM NEVER REACHED ITS TERMINATOR, not "we had to
               # estimate it". Keyed on the basis, a stream that ended cleanly with `[DONE]` and
               # merely carried no usage frame was stamped aborted -- a claim about the TRANSPORT
               # drawn from a fact about the PRICING, and the two are independent.
               #
               # A METER CEILING CUT IS STILL AN ABORT, and unconditionally so: this proxy closed the
               # socket itself, so the stream demonstrably did not finish. The cause goes in its own
               # key rather than changing what the flag means -- `compare_arms.py` counts money on
               # `stream_aborted`, and making it mean "gateway only" would drop ceiling cuts out of
               # every spend column that already reads it.
               if cut_by_meter:
                   row["stream_aborted"] = True
                   row["stream_cut_by"] = "meter_delta_ceiling"
                   row["meter_delta_ceiling"] = ceiling
                   row["note"] = (f"the meter cut this stream at its {ceiling}-delta ceiling after "
                                  f"{latency_ms/1000:.0f}s; priced from {deltas} forwarded deltas "
                                  f"(EXACT for what was forwarded, a floor for what upstream generated)"
                                  f" and a prompt of {pin} tokens ({prompt_basis})")
               else:
                   if not done_seen:
                       row["stream_aborted"] = True
                   row["note"] = ("upstream ended the stream with no usage frame after "
                                  f"{latency_ms/1000:.0f}s; priced from {deltas} forwarded deltas "
                                  f"(a FLOOR) and a prompt of {pin} tokens ({prompt_basis})")
        elif not basis and row.get("error"):
            # OPEN[meter-exception-cut-is-served-unpriced] an upstream that dies by EXCEPTION (an
            # RST, a socket timeout) can still deliver a usable 200 answer that costs the ledger $0.
            # proof:absent:priced_exception_cut@benchmarks/meter/proxy.py
            # REVIEW 2026-08-30 (money): the paragraph below deliberately declines to price the
            # client-hung-up case, and that reasoning does not cover the other tenant of this same
            # `except`: an UPSTREAM-side death. Driven live with an upstream sending 3 deltas then
            # RST — the adapted client receives status 200, the full forwarded content and a
            # truncation mark, and the row lands `{metered: false, cost: 0.0, deltas_seen: 3}`.
            # A served answer priced at nothing is the exact silent-under-count shape the delta
            # estimator was built to close, live whenever the ~1800 s gateway cut arrives as an
            # exception instead of a clean EOF. The estimate has all its inputs in scope at the
            # except site; at minimum an upstream-typed exception on the adapted path should price
            # like a clean cut, with its own basis label.
            # THE ROW MUST NOT CONTRADICT ITSELF. The synthesis above lives inside the `try`, so any
            # exception while forwarding -- a client that hung up is the one that happens; five
            # `BrokenPipeError` rows are in `meter/meter.jsonl` -- skips it and lands here. Two of
            # those five carry `deltas_seen` 3149 and 1 while the note beside them said, in words,
            # that there were no deltas. Whether those tokens should be PRICED is a separate
            # question (the client left; the provider still generated and still billed) and this
            # branch deliberately does not answer it -- `metered` stays false and `cost` stays 0.0,
            # which is the module's "unpriced, never $0" rule. What it must not do is state a fact
            # about the row that the field next to it falsifies.
            row["note"] = (f"stream ended in an error after {deltas} forwarded delta(s) and no "
                           f"usage frame; nothing was priced ({row['error']})")
        elif not basis:
            # No usage frame AND no deltas: nothing was produced to price. Unpriced, and recorded as
            # unpriced -- never as $0.
            row["note"] = "streamed response carried no usage frame and no deltas"
        if pout:
            row["tok_per_s"] = round(pout / max(latency_ms / 1000.0, 1e-6), 2)
        self.server.meter.record(row)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # PER SERVER, not per class: a class attribute would let one test's traffic calibrate the
        # next test's estimate, and two proxies pointed at two gateways share one tokenizer ratio.
        # Everything else on this object is assigned by `main()` (or by a test); this one is not,
        # because a missing calibrator would price an abort at zero prompt tokens in silence.
        self.prompt_scale = PromptTokens()
        # ON BY DEFAULT, and that is the decision, not an oversight. A guard against a runaway
        # generation that has to be switched on is not a guard: the 62.9 h in this module's
        # docstring were spent by a proxy that had every other protection and no ceiling. `main()`
        # and tests may reassign it; `0` disables it.
        # OPEN[meter-ceiling-comment-contradicts-its-default] the paragraph above and the constant
        # it assigns state opposite decisions about a money guard.
        # proof:`line:ON BY DEFAULT&&not an oversight@benchmarks/meter/proxy.py`
        # REVIEW 2026-08-30 (stale-claim): `DELTA_CEILING_DEFAULT` is 0 — off — and its own comment
        # says "OFF UNLESS ASKED, and this reverses the value it shipped with a few hours ago",
        # with `test_the_ceiling_is_off_unless_someone_asks_for_it` pinning the zero. The reversal
        # updated the constant and its test and left this site instructing the next editor in
        # exactly the wrong direction. One of the two paragraphs is wrong; today it is this one.
        # Rewrite it to state the shipped decision (and why), or flip the default back on purpose.
        self.delta_ceiling = DELTA_CEILING_DEFAULT

    # A CLIENT HANGING UP IS NOT AN INCIDENT. `http.server` prints a full traceback for every
    # exception in a handler thread, and an httpx/aiohttp connection pool closes idle keep-alive
    # sockets constantly: measured 2026-08-23, 1,301 `ConnectionResetError` tracebacks — 2,604 log
    # entries — in one campaign's meter log, every one of them raised at `handle_one_request`'s
    # `readline` while WAITING for a request that never came. Not one was a lost call.
    #
    # They are suppressed rather than reduced because a log that is 99% noise is a log nobody reads
    # the exception in, and this file's whole purpose is to be believed about money. Anything else
    # still prints, including the same error class raised anywhere other than an idle socket.
    def handle_error(self, request, client_address) -> None:      # noqa: D102 - stdlib override
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            tb = sys.exc_info()[2]
            frames = []
            while tb is not None:
                frames.append(tb.tb_frame.f_code.co_name)
                tb = tb.tb_next
            if "handle_one_request" in frames or "readline" in frames:
                return
        super().handle_error(request, client_address)


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--upstream", default=os.environ.get("METER_UPSTREAM", ""),
                    help="upstream OpenAI-compatible base URL, ending in /v1")
    ap.add_argument("--api-key", default=os.environ.get("METER_API_KEY", ""),
                    help="bearer token for the upstream; replaces whatever the client sent")
    ap.add_argument("--pricing", default=os.path.join(here, "pricing.json"))
    ap.add_argument("--log", default=os.environ.get("METER_LOG", ""),
                    help="JSONL call log; one row per upstream request")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--rpm", type=int, default=int(os.environ.get("METER_RPM", "45")),
                    help="requests/minute allowed upstream, shared by both arms (0 = unlimited). "
                         "The gateway publishes x-litellm-key-rpm-limit: 50.")
    ap.add_argument("--max-retries", type=int, default=5,
                    help="429 retries absorbed here instead of by each framework's own policy")
    ap.add_argument("--delta-ceiling", type=int,
                    default=int(os.environ.get("METER_DELTA_CEILING", DELTA_CEILING_DEFAULT)),
                    help="stop forwarding a stream after this many content deltas and close it as "
                         "a truncated, priced answer (0 = never). Above both arms' largest measured "
                         "complete answer, so it is symmetric by construction")
    args = ap.parse_args()

    if not args.upstream:
        print("--upstream is required (or set METER_UPSTREAM)", file=sys.stderr)
        return 2

    pricing = Pricing(args.pricing)
    server = Server((args.host, args.port), Handler)
    server.upstream = args.upstream
    server.api_key = args.api_key
    server.pricing = pricing
    server.timeout = args.timeout
    server.meter = Meter(args.log or None)
    server.limiter = RateLimiter(args.rpm)
    server.max_retries = args.max_retries
    server.delta_ceiling = max(0, args.delta_ceiling)
    # WHICH EGRESS PATH THE UPSTREAM NEEDS IS THE UPSTREAM'S BUSINESS, NOT THIS FILE'S.
    #
    # `ProxyHandler({})` — an EMPTY mapping — force-disables proxying for every upstream. That was
    # written for the corporate gateway, which lives inside the perimeter and must be reached
    # directly. The second instance of this meter points at `openrouter.ai`, which is outside it, and
    # inherited the same override: every call went direct, around the box's egress proxy.
    #
    # MEASURED 2026-08-27, same endpoint, no API key, eight attempts per cell:
    #     curl   via 127.0.0.1:18080   0/8 blocked
    #     urllib via 127.0.0.1:18080   0/8 blocked
    #     curl   direct                1/8 blocked
    #     urllib direct                2/8 blocked
    # A blocked request is `403 {"success": false, "error": "Access denied by security policy."}` —
    # not OpenRouter's error shape, and it arrives for a request carrying NO credentials at all, so
    # it is neither the key nor the account. The direct path is filtered; the proxied path is not.
    # Sustained direct traffic makes it worse: the 8802 meter's refusal rate climbed 13% -> 25% ->
    # 48% -> 72% -> ~100% across the day and finally stalled two $1 probes at a fifth of their
    # budget. I spent an hour blaming the key for that.
    #
    # The environment already spells the rule correctly — `no_proxy` names the corporate gateway, so
    # `proxy_bypass('llm-core-olap.samokat.ru')` is True and `proxy_bypass('openrouter.ai')` is
    # False. So: read it, rather than overriding it. `ProxyHandler()` with no argument does exactly
    # that, and the gateway still goes direct because the environment says so.
    server.opener = urllib.request.build_opener(urllib.request.ProxyHandler())

    print(f"[meter] {args.host}:{args.port} -> {args.upstream}", flush=True)
    print(f"[meter] pricing {pricing.reference_slug} in={pricing.default.get('input_per_token')} "
          f"out={pricing.default.get('output_per_token')} basis={pricing.basis} "
          f"fetched_at={pricing.fetched_at}", flush=True)
    print(f"[meter] log {args.log or '(none)'}", flush=True)
    print(f"[meter] rpm cap {args.rpm or 'unlimited'} | 429 retries {args.max_retries}", flush=True)
    print(f"[meter] delta ceiling {server.delta_ceiling or 'off'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"[meter] final {json.dumps(server.meter.snapshot())}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
