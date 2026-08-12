# LoopLab in a Corporate Contour: the AI Factory Boundary and Opportunistic GPU Capacity (2026-08-12)

> Where LoopLab stops and an existing corporate agent platform starts, and how autonomous
> experiments can consume GPU capacity that is idle because humans went home — without promising
> fairness LoopLab cannot deliver, and without silently changing the experiment a human asked for.

| Metadata | Value |
|---|---|
| **Status** | current research / proposed integration and capacity-operating model |
| **As of code commit** | `70318f025615c8484e9fb5ce0a9025c8810e551c` |
| **Normative for** | the platform-boundary decision (build vs consume vs expose), the opportunistic-capacity operating model, the queue-explainability contract, and the rules an automated footprint reduction must satisfy |
| **Not normative for** | shipped behavior, backend selection already settled by doc 20, or permission to bypass doc 20's D1→D2→D3 order |
| **Depends on** | [doc 20](20-looplab-unified-ds-workspace-and-distributed-execution-2026-07-12.md) (§4.2 evaluation-level dispatch, §9 shared GPU policy, §10 administrative domains, §14 recommended stack), [doc 19](19-ide-integration-and-remote-development-2026-07-12.md), [doc 23](23-hypothesis-card-kanban-2026-07-20.md) (Card footprint vocabulary) |
| **Supersedes** | nothing |
| **Superseded by** | — |

**Evidence discipline.** Statements about LoopLab are code-confirmed against the commit above, with
file and symbol references. Statements about the operator's corporate environment are recorded as
**given** — they come from the operator, not from measurement, and each is marked so that a later
reader can tell an assumption from a fact. External-system claims reuse doc 20 §16's verified
snapshot; anything this document adds about third-party behavior is listed in §9 as *to verify
before implementation* rather than presented as checked.

---

## 1. Executive decision

1. **Unify the borders, not the core.** Identity, tool access, model access, catalog presence and
   billing should be shared with the corporate platform. The execution core — the append-only event
   log, `fold`, resume-by-replay, the per-node terminal invariant, the paid-work ledgers and the
   honesty gates — must not be ported onto an agent-construction framework. Those properties are the
   product; a construction framework offers none of them.
2. **Consume the communal LLM gateway; do not own model serving.** LiteLLM is already the shared
   entry point for both the corporate agent platform and LoopLab (given). That closes the compliance
   question doc 20 §9.5 left open: LoopLab is a *client* of an `ExternalModelEndpoint`, its replicas
   and GPUs are not LoopLab capacity, and LoopLab must not claim it can scale, drain or preempt them.
3. **Consume corporate MCP tools through the existing provider.** `looplab/tools/mcp_tools.py`
   already speaks stdio and streamable HTTP, is configured by `LOOPLAB_MCP_SERVERS` /
   `LOOPLAB_MCP_CONFIG` / `.mcp.json`, and degrades to "no tools" when a server is unreachable. This
   integration is configuration, not code — but it imports a prompt-injection path into a system
   that executes generated code, which §3.2 treats as a first-class security decision.
4. **Expose LoopLab to the platform catalog rather than moving into it.** A thin MCP server over the
   existing FastAPI control plane (start a run, poll status, fetch the champion/report) makes
   LoopLab one entry in the corporate agent catalog. This buys the unification an organization
   actually asks for — one front door, one directory — at the cost of an adapter, not a rewrite.
5. **There is no single GPU admission plane today, and LoopLab must not pretend otherwise.** GPU
   capacity is partitioned per-pool by human agreement (given): a LiteLLM inference pool owned by
   another team, a JupyterHub pool, a product contour out of scope for training. Doc 20 §9.1's
   requirement — every physical allocation enters one accounting plane — is *not* satisfied, so any
   availability, fairness or start-time statement LoopLab makes is scoped to the pool it actually
   controls and must be labelled as such.
6. **Start opportunistic capacity on the JupyterHub pool, not the inference pool.** Idle notebook
   GPUs are idle because nobody is using them; idle inference GPUs are idle because a *service* is
   under-loaded, and reclaiming those means draining a live serving replica under an SLO. The first
   is a scheduling problem, the second is a cross-team service contract. They are not the same
   project and should not ship together (§4.2).
7. **Measure before building.** The premise "there are nights when 16 GPUs stand idle" is an
   assumption, not an observation. The gating deliverable is a utilization study (§4.1) whose
   output is a distribution of *contiguous free windows by width*, because that distribution — not
   an average utilization percentage — decides whether opportunistic execution is worth building at
   all and what maximum evaluation duration it can admit.
8. **A queue that cannot explain itself is a defect.** Three states look identical as "pending" and
   must be separated at submit time: *infeasible* (never runnable at this cluster's maximum shape —
   refuse immediately with the largest feasible shape), *feasible but rare* (admit with a measured
   forecast and standing alternatives), *feasible and ordinary* (normal queue with position). §5.
9. **Wide shapes need reservation-based backfill, or they starve forever.** Under a pure
   priority/first-fit queue with continuous small-shape arrivals, an 8-GPU request never runs. The
   standard fix is a reservation for the head-of-queue wide job plus conservative backfill of small
   jobs that provably finish before it. Without this, §5's honest forecast will honestly report
   "never".
10. **Automated footprint reduction is a proposal with permanent provenance, never a silent clamp.**
    LoopLab already reduces a declared footprint to the machine envelope
    (`core/cards.py::effective_card_footprint`) — and today it does so **silently**, returning a
    number with no record that a reduction occurred. That is benign for a single operator on their
    own box and unacceptable the moment a queue reduces someone's experiment on their behalf (§6).
11. **The strong version of downscoping is already LoopLab's shape: search cheap, confirm
    expensive.** Opportunistic small-footprint search plus a full-footprint confirmation of the
    champion (`engine/confirm_phase.py`) requests the scarce wide slot **once per run**, for a
    winner, instead of once per candidate. This simultaneously reduces queue pressure and preserves
    the scientific claim.

---

## 2. Given: the corporate landscape

Recorded as stated by the operator on 2026-08-12. These are **not** code-verified and not measured;
later sections mark where a decision depends on one of them being true.

| Fact | Consequence for this document |
|---|---|
| LiteLLM is the corporate gateway for all LLMs and embedding/vector models. Both the corporate agent platform and LoopLab already call it. | The "must every LLM call go through the corporate gateway?" question is closed: it already does. §3.1 becomes a set of requirements *on the gateway*, not an integration project. |
| A separate team owns the LiteLLM deployment and negotiates its GPU allocation separately. | LoopLab cannot treat those GPUs as capacity. Reclaiming them at night (§4.2) is a cross-team agreement first and code second. |
| The corporate agent platform is believed to be a CPU-side product (engine/orchestrator), with models served through LiteLLM rather than by the platform itself. | Reinforces §1.1: the overlap with LoopLab is the *agent-construction* layer, not compute. Two agent runtimes competing over one gateway is a policy question, not an architectural conflict. |
| JupyterHub has its own resource allocation: approximately two nodes with eight GPUs each. | This is the candidate opportunistic reservoir. It also bounds the maximum feasible shape at **8 GPUs on one node**, which makes any ≥16-GPU request infeasible by construction (§5.2). |
| A separate product contour exists with its own resources; training is out of scope for it. | Excluded from this analysis. |
| GPU allocation across these pools is decided by people, per pool. | There is no global scheduler and no global quota. §1.5. |

**What the given does not settle.** Whether the JupyterHub nodes are in the same Kubernetes cluster
as any future LoopLab executor; whether GPU idleness is observable today (DCGM/Prometheus present or
not); whether the JupyterHub spawner already applies profiles/priority classes; and whether the
LiteLLM pool has any headroom policy at all. Each is listed in §8.

---

## 3. The platform boundary: four questions, not one

The recurring framing — "should LoopLab be rebuilt inside the corporate agent platform, or stay
separate?" — is unanswerable because it bundles four independent decisions with four different
answers. Decomposed:

### 3.1 Communal LLM access — consume, with stated gateway requirements

LoopLab speaks the OpenAI-compatible protocol (`core/llm.py`), so consumption is already working.
What matters is what the gateway is allowed to do *silently*, because three LoopLab subsystems
depend on the identity and accounting of a call being exactly what LoopLab believes it was:

- **`engine/costs.py`** maintains a durable per-run `llm_usage` / `llm_cost` ledger plus a
  `.llm-usage-outbox`. Gateway-side retries that are not surfaced make that ledger under-report; a
  gateway-side fallback to a different model makes it attribute cost to the wrong model.
- **`core/llm_broker.py`** admits outbound calls into named concurrency lanes. If the gateway
  applies its own queueing or rate limiting without surfacing it, LoopLab's admission decisions are
  made against a fiction, and the symptom is an unexplained stall rather than an error.
- **`search/speculation_quality.py`** issues calibration receipts from paired runs, at a cost the
  module's own docstring records as six GPU runs. Those receipts are bound to a derivation over
  observed run behavior. **Silent model drift behind one endpoint name invalidates issued receipts
  without any signal** — the calibration keeps reporting a bound that was measured against a
  different model.

The requirements to place on the gateway are therefore specific and small: the response must carry
the **resolved** model identifier (not the requested alias); provider fallback and gateway-side
retries must be visible in the response or headers; rate-limit state must be exposed; and per-key
budgets must be readable so LoopLab's ledger can be reconciled rather than guessed. Doc 20 §14.2
already forbids the corresponding LoopLab-side behaviors ("hide backend retries or their cost",
"hide provider fallback/model drift"); this section makes the same rule a cross-team requirement.

**A model change is a governance event.** Because of the calibration coupling above, "the gateway
switched the default model behind our alias" is not a configuration change — it revokes evidence.
Pin the resolved model identifier per run in the run's own snapshot and treat a change as a reason
to re-derive, not as a transparent upgrade.

### 3.2 Corporate MCP tools — consume, with an explicit trust boundary

The seam exists and is provider-neutral by construction (`tools/mcp_tools.py`: origin-pair names,
graceful degradation, a 64 KiB schema cap). Three risks are specific to LoopLab and do not apply to
a chat assistant:

1. **Autonomous execution under a human's authority.** An MCP server authenticated as the operator
   is invoked by a loop that runs unattended for hours. The corporate identity used for tool calls
   must be a service principal with its own scope, and tool authorization must remain LoopLab's
   decision (doc 20 §14.2 already states this for external models; it holds identically for tools).
2. **Prompt injection into a code-executing loop.** A wiki page or ticket body reaching a system
   that *writes and runs code on a GPU* is a materially different exposure from the same text
   reaching a chat agent. The mitigation is not a filter: it is a boundary. Corporate knowledge
   sources should be read-only, their content should be treated as untrusted data rather than as
   instructions, and the existing permission vocabulary (`tools/perm_modes.py`) is where that
   distinction belongs. A candidate's sandbox already has network policy (`runtime/sandbox.py`
   `docker_run_argv --network`); the corresponding rule for retrieved corporate text is that it must
   never widen what the sandbox may do.
3. **Context budget.** Corporate MCP servers commonly publish dozens of tools. The existing schema
   cap protects the request; the population of enabled tools per role is a separate policy decision
   and should be per-project, not global.

### 3.3 Agent-construction platform — do not port; expose instead

The argument against porting is not cost, it is loss of the properties that make LoopLab's outputs
trustworthy. An agent-construction platform composes prompts, tools and control flow. LoopLab is an
event-sourced search engine whose distinguishing guarantees are enumerated in `CLAUDE.md` as engine
invariants: a single writer of domain events, exactly one terminal per node, every side effect gated
on a durable event so resume-by-replay is idempotent, a deterministic order-tolerant `fold`, and
settings-in-`run_started` winning over live config on resume. On top of those sit the honesty gates
(`looplab/trust/`: leakage, reward-hack, CV, redaction, confirmation), the paid-work ledgers
(`engine/costs.py`, `engine/curation_protocol.py`, `serve/paid_ledger.py`) and the calibrated
speculation envelope (`engine/speculation_gate.py`). None of these are expressible as "an agent with
tools", and rebuilding them inside a construction framework recreates the framework LoopLab already
is.

**The productive direction is the reverse one.** Publish LoopLab into the platform's catalog as a
tool/agent: a thin MCP server over the existing FastAPI control plane exposing *start a run against
a described goal*, *report status*, *return the champion and its report*. The corporate platform then
offers "run an ML experiment" as one of its agents, users reach it through the same directory as
everything else, and the execution core stays where it is. This satisfies the organizational demand
for unification at the layer where unification is actually valuable — discovery and entry — without
touching the layer where it would be destructive.

**One consequence to state plainly:** two agent runtimes will exist in the company. That is a
governance fact to be owned explicitly (each has its own audit trail, its own budget, its own
approval path) rather than an inconsistency to be resolved by collapsing them.

### 3.4 Identity, audit, quota, billing — unify

This is where "put it all in one place" is right, and it is cheap precisely because it does not touch
the engine. Doc 20 §14.1 already specifies the shape: the enterprise IdP as the OIDC authority with
`(iss, sub)` as the stable human principal, separate workload identities for execution, and audit
that explains what was possible, promised, queued and actually allocated. Adding LLM spend to the
corporate accounting is a reporting integration on top of the existing per-run ledger, not a
redesign of it.

### 3.5 The rule

> **Unify the borders — identity, tools, models, catalog, billing. Do not unify the execution core.**

The inverse — a shared execution core with bespoke borders — is the expensive failure mode: it
maximizes coupling exactly where semantics differ and minimizes it exactly where the organization
gets value.

---

## 4. Opportunistic GPU capacity

The proposal: interactive/manual GPU selection keeps priority; a queue of autonomous experiments
fills whatever is idle. This is a good fit for LoopLab specifically, because doc 20 §1.1 already
establishes that LoopLab benefits more from many independent one-GPU evaluations than from one wide
one — and one-GPU work is exactly what fits into fragmented idle windows.

### 4.1 Measure first: what to collect and what decides

The distribution that matters is **contiguous free windows by width**, not average utilization. An
80%-idle pool whose idle time arrives as thousands of 90-second gaps supports nothing; a 40%-idle
pool with nightly 6-hour 8-GPU windows supports a great deal.

Collect over **at least three weeks** (to include month-end and at least two full weekends), per
pool, at ≤1-minute resolution:

| Signal | Why |
|---|---|
| Per-GPU allocation state (allocated to a pod/kernel vs free) | The scheduling fact. Distinct from utilization: an allocated-but-idle notebook GPU is *not* available, and §4.3 explains why reclaiming it is a different decision. |
| Per-GPU actual utilization (DCGM `DCGM_FI_DEV_GPU_UTIL`, memory used) | Separates "allocated and working" from "allocated and abandoned" — the latter is the largest realistic source of reclaimable capacity in a notebook pool, and it is a *policy* fix (idle culling), not a scheduling one. |
| Contiguous-free-window length, bucketed by width (1, 2, 4, 8 GPUs; same-node) | The admission constraint of §4.4. An evaluation may only be admitted opportunistically if its expected duration fits the window it is likely to get. |
| Hour-of-day and day-of-week profile | Whether "nights are free" is true here, and whether the effect is large enough to schedule around. |
| Requested-shape histogram of actual human demand | Decides whether wide-shape starvation (§5.4) is a real risk or a theoretical one. |

**Decision rule, stated in advance so the study cannot be rationalized after the fact:**

- If ≥8 h/day of ≥1-GPU contiguous idle exists on most weekdays → opportunistic execution is worth
  building, starting at width 1.
- If ≥2-GPU windows are rare (<2 h/day) → build width-1 only, and treat every wider request as
  §5.2's "feasible but rare" case with an honest forecast rather than building a wide lane.
- If whole-node (8-GPU) windows occur less than a few times per month → **do not build wide
  opportunistic execution at all**; wide work goes through human scheduling, and the product's job
  is to say so clearly (§5).
- If the dominant finding is *allocated-but-unused* notebook GPUs → the highest-value change is
  JupyterHub idle culling and profile hygiene, not a LoopLab scheduler. This outcome is likely
  enough that it must be checked before any code is written.

### 4.2 Two reservoirs, two different problems

**JupyterHub pool (≈2 × 8 GPUs) — the tractable one.** Idle here means no human is using the
allocation. Reclamation preempts nobody's live traffic; the worst case is a notebook that was
abandoned mid-session, which is a culling policy question the Hub already has primitives for. Start
here.

**LiteLLM inference pool — the hard one, and out of scope for a first release.** Idle here means a
*serving* deployment is under-loaded. Reclaiming it requires: consolidating traffic onto fewer
replicas, draining a replica without dropping in-flight requests, accepting a cold-start penalty
when load returns, and owning an SLO jointly with the team that runs it. Doc 20 already models this
correctly — §9.6 (protected floor, elastic burst, scale-to-zero) and §9.7 (reclamation and
checkpoint contracts) — and the model is right; the point here is sequencing. This is a cross-team
service agreement with an SLO attached, and it should not be bundled with the notebook-pool work.
A realistic intermediate step is a *protected floor plus burst* arrangement negotiated with that
team, where the floor is sized from their measured night-time p99 load rather than from a guess.

### 4.3 Priority model: interactive guaranteed, autonomous opportunistic

Two classes, differing in every dimension that matters:

| | Interactive (human/notebook) | Autonomous (LoopLab evaluation) |
|---|---|---|
| Entitlement | Guaranteed quota | Borrowed / opportunistic only |
| Preemptible | No | **Yes, by design** |
| Latency expectation | Seconds to start | Best-effort; may wait days |
| Failure of the class | Visible outage | Wasted GPU-seconds, run continues |

Mechanically, on Kubernetes this is priority classes plus preemption, with Kueue supplying the
entitlement/borrowing semantics doc 20 §14.1 already selected. The essential property is that the
opportunistic class must be *genuinely* preemptible — not "we ask it nicely to stop".

### 4.4 Admission by fit — the rule that makes preemption affordable

Preemption is cheap only if the preempted work was cheap to lose. The rule:

> **Admit an opportunistic evaluation only when its expected duration fits inside the idle window it
> is likely to receive, or when it checkpoints at a granularity smaller than that window.**

Without this, the system degrades into a treadmill: long evaluations are admitted at 19:00, killed
at 09:00, restarted the next evening, and never finish while consuming the entire reservoir. The
window distribution from §4.1 supplies the number; the evaluation's expected duration comes from the
run's own history, which LoopLab already has in `spans.jsonl` (surfaced by `looplab timings`).

A useful corollary: **the opportunistic lane naturally prefers short, wide-search work** — exactly
LoopLab's best-of-N / breadth-first search regime — and naturally rejects long single-candidate
training. That is a feature, and it should be stated to users as one rather than discovered as a
limitation.

### 4.5 Preemption must not corrupt run state

Two code-level constraints, both derived from the engine invariants in `CLAUDE.md`:

1. **Do not add a new terminal event type for preemption.** Invariant 2 is exactly one terminal per
   node (`node_evaluated` | `node_failed`), and the fold is idempotent on duplicates. A preempted
   evaluation should terminate as `node_failed` with a **typed reason**, not as a third terminal
   kind. This keeps the fold contract untouched while still letting the search policy distinguish
   "this candidate is bad" from "this candidate never ran" — a distinction that matters, because
   scoring a preempted candidate as a failure would poison the surrogate.
2. **Preemption is a worker-side event and the coordinator owns the write.** Under doc 20 §4.2's
   evaluation-level dispatch the worker returns a typed outcome and never writes the authoritative
   log; a preempted worker returns a preemption outcome (or fails to return at all, which the
   coordinator's lease expiry handles). This is precisely why evaluation-level dispatch is the
   prerequisite rather than an optimization: run-level dispatch would place the authoritative event
   log inside the preemptible pod.

### 4.6 What LoopLab must *not* claim

The host GPU lease (`engine/resources.py::default_gpu_host_lease_path`, one file per OS user under
the system temp dir) coordinates GPU ownership between processes **on one host filesystem
namespace**. In containers each pod has its own `/tmp`, so between pods the lease is silently
inert — which is correct when the device plugin has already given each pod exclusive devices, and
dangerous the moment anyone enables time-slicing or co-schedules two LoopLab pods on one node. The
module's own docstring states this ("Container/OS-user boundaries still require their own external
scheduler"); a cluster deployment must make it explicit configuration rather than an inherited
default, and the in-pod GPU pool must be exactly what `CUDA_VISIBLE_DEVICES` grants.

---

## 5. The queue must explain itself

This is the operator's stated primary product concern and it deserves first-class treatment: an
experiment submitted a week ago that has not started, for reasons nobody can see, is worse than a
refusal.

### 5.1 The rule

> **Never show "pending". Show: why not now, what would make it run, and — where measurement
> supports it — when it plausibly could.**

### 5.2 Three states that must be separated at submit time

| State | Example (given the ≈8-GPU-per-node ceiling) | Required behavior |
|---|---|---|
| **Infeasible** | 16 GPUs requested; no node has 16 | **Refuse at submit**, naming the largest feasible shape and the reason. Never queue it. A request that can never be satisfied must not be allowed to look like it is waiting. |
| **Feasible but rare** | 8 GPUs; whole-node windows occur a few times a month | Admit, but attach a **measured** forecast ("8-GPU windows occurred N times in the last 30 days; median wait X") and standing alternatives (§6). |
| **Feasible and ordinary** | 1–2 GPUs | Normal queue: position, class, and estimate. |

The first row is the highest-value single behavior in this document, because it converts an
open-ended silent wait into an immediate, actionable answer.

### 5.3 A forecast is a forecast

Doc 20 §14.2 already forbids describing a cached free-GPU count as a reservation or a guaranteed
start time. The same applies here: label the forecast as derived from the last N days of measured
windows, show the sample it came from, and let it be wrong. An honest wide interval beats a
confident wrong number, and a forecast with its evidence attached survives the first time it misses.

### 5.4 Anti-starvation: reservation plus conservative backfill

Under a pure priority/first-fit queue with continuous small-shape arrivals, a wide request starves
indefinitely — not as an edge case but as the normal outcome, because every time the 8th GPU frees
up a 1-GPU job takes it. This is a solved problem in batch scheduling: give the head-of-queue wide
job a **reserved future start**, and let smaller jobs backfill only when they provably complete
before that reservation. Aging-based priority boosts alone do not fix it; they only change how long
the starvation lasts.

Two consequences for LoopLab:

- Backfill needs a **duration estimate** per evaluation to be safe, which §4.4 already requires.
  Estimates will be wrong; the conservative variant (backfill only jobs whose worst case fits) is
  the right default, and its cost is some idle time, which is acceptable in an opportunistic lane.
- If the operator's scheduler (Kueue, or Slurm where it exists) already implements reservation and
  backfill, **use it and do not reimplement**. Doc 20 §14.2's prohibition on double scheduling
  applies: LoopLab must not choose placement while a cluster scheduler is also choosing it.

### 5.5 Explanations are pushed, not polled

A user should not have to open a page to discover their experiment has waited a week. The owner
attention feed (`serve/attention.py`) is the correct home: it already emits a small allow-listed,
redacted envelope with stable opaque ids so that polling and replay do not duplicate a signal.
Queue-explanation items fit that shape directly. The message should carry the reason, the measured
context, and the concrete options — reduce the shape, schedule for a named window, or cancel — so
that it is actionable inside the notification rather than a prompt to go investigate.

---

## 6. Automated footprint reduction

The proposed next step — agents decide an experiment could be made lighter so it can actually run —
is attractive and genuinely risky, and the risk is not the one it first appears to be.

### 6.1 It is already half-built, and the built half is silent

The Card footprint vocabulary from doc 23 is shipped: `Idea.footprint` is a researcher-proposed
`{gpus, gpu_mem_mib}` declaration (`core/models.py:355-378`, validated by
`core/cards.py::valid_researcher_footprint`), an operator may override it with an independent
resource pin, and `core/cards.py::effective_card_footprint` merges the two and **clamps the result
to the live machine envelope**:

```python
out["gpus"] = min(out["gpus"], gpu_count)                       # core/cards.py:129
...
out["gpu_mem_mib"] = min(out["gpu_mem_mib"], envelope)          # core/cards.py:135
```

The design around it is careful in the right way: `Card.footprint` is part of the immutable action
ownership receipt and is never rewritten, the override is merged only at admission/freshness time,
and re-clamping is what makes a pin accepted on a larger host safe after resume on a smaller one
without mutating replayed history.

**What is missing is a record that the clamp happened.** The function returns a number, not a number
plus its provenance. On a single operator's own machine that is benign — they know their box has two
GPUs. In a shared queue that reduces someone's experiment on their behalf, a silent `min()` is the
exact failure this document's §5.1 exists to prevent, one layer deeper: the experiment did not just
wait for unclear reasons, it *ran as a different experiment* and reported a result. Before any
automated reduction ships, the effective footprint needs to carry whether it was reduced, from what,
and by which authority (researcher declaration / operator pin / envelope clamp / queue policy).

### 6.2 The rules an automated reduction must satisfy

1. **A reduction is a proposal, not an edit.** The immutable declaration stays; the reduction is a
   separate, attributed decision — which is already the shape `effective_card_footprint` uses for
   operator pins.
2. **Provenance is permanent.** A result obtained under a reduced footprint carries that fact for
   the life of the record, into every comparison, leaderboard and cross-run lesson. LoopLab's
   comparison surfaces already refuse to compare incomparable metrics (`ui/src/runIndex.js`
   `metricComparable`); reduced-footprint results need the same treatment.
3. **Never compare across footprints as if they were the same.** Batch size, effective learning
   rate and wall-clock budget all move when GPU count moves. A 2-GPU result is evidence about a
   2-GPU configuration.
4. **Reduction must be scientifically stated, not just numerically applied.** "8 GPUs → 2 GPUs" is
   not a resource decision; it implies a change to batch size or gradient accumulation, which is a
   change to the experiment. The Researcher is the right author for that (it already reasons about
   experiment design and already declares the footprint), and the reduced variant should be a
   distinct idea with its own rationale rather than a rewritten copy of the original.
5. **Staged trust.** Phase 1: propose only, human approves in the attention feed. Phase 2:
   auto-apply for runs explicitly marked exploratory. Phase 3: auto-apply with full-footprint
   confirmation of the winner. Do not start at phase 3.

### 6.3 The strong form: cheap search, expensive confirmation

Phase 3 above deserves emphasis because it is not a compromise — it is a better experimental design
that happens to also solve the queueing problem. LoopLab already separates search from confirmation
(`engine/confirm_phase.py`: multi-seed top-k confirmation of the selected candidates). Running the
*search* at a reduced footprint in the opportunistic lane and the *confirmation* at the full
declared footprint means:

- the scarce 8-GPU slot is requested **once per run, for a champion**, instead of once per
  candidate — which is what actually relieves §5.4's starvation pressure;
- the scientific claim is made at the footprint the human asked for;
- the reduced-footprint search results are honest evidence for what they are (relative ranking under
  a smaller configuration), and their limitation — that ranking under a reduced footprint need not
  preserve ranking at full scale — is a known, statable risk rather than a hidden one.

That last caveat is the real cost and should be measured rather than assumed: rank correlation
between reduced-footprint and full-footprint outcomes is exactly the kind of paired-run calibration
`search/speculation_quality.py` already knows how to express.

---

## 7. Code-level consequences

No implementation is proposed here. These are the seams a future implementation touches, recorded so
that the first change does not land in the wrong layer.

| Area | Consequence |
|---|---|
| `EvaluationService` seam (doc 20 D1) | Still prerequisite #1 for everything in §4. Preemption, remote dispatch and opportunistic admission all require that an evaluation be a portable request/outcome pair rather than an in-process call. |
| `core/cards.py::effective_card_footprint` | Needs reduction provenance (§6.1) before any queue-driven reduction exists. This is a small, self-contained change with a truth table, and it is the single highest-value preparatory step. |
| `engine/resources.py` | Host lease must be explicitly inert in a cluster deployment; in-pod pool = `CUDA_VISIBLE_DEVICES` (§4.6). |
| Terminal events | Preemption maps onto `node_failed` with a typed reason; do **not** add a terminal type (invariant 2). The search policy must not score a preempted candidate as a failed one. |
| `serve/attention.py` | Home for queue explanations and reduction proposals (§5.5, §6.2). |
| `engine/costs.py`, `core/llm_broker.py`, `search/speculation_quality.py` | The three subsystems that break under silent gateway behavior (§3.1). Pin the resolved model per run. |
| `tools/mcp_tools.py`, `tools/perm_modes.py` | Corporate MCP servers arrive as configuration; the trust boundary for their content is a permission-mode decision (§3.2). |
| Cross-run memory (`engine/memory.py`, `~/.looplab/memory`) | Mutating stores are guarded by an interprocess file lock (`memory.py::add`), which works on a shared POSIX filesystem and not across pods without RWX. Multi-user shared lessons — the main value of a shared deployment — require solving this, and it is a storage-authority decision (doc 20 §4.4), not a locking tweak. |

---

## 8. Open questions requiring human answers

1. **Is the JupyterHub GPU pool in a Kubernetes cluster whose scheduler LoopLab work could also
   target?** If yes, priority classes and Kueue do the work of §4.3–§4.5. If no, opportunistic
   execution needs a different mechanism and is substantially more expensive.
2. **Is GPU allocation and utilization observable today** (DCGM exporter, Prometheus retention ≥3
   weeks)? Without it, §4.1's study is itself a project, and that project is the first deliverable.
3. **Who owns the decision to preempt?** Preempting an opportunistic LoopLab job is trivially fine;
   the policy question is whether an *abandoned interactive* allocation may be culled, and that is a
   Hub policy owned by whoever runs the Hub.
4. **Is there any night-time headroom in the LiteLLM pool at all?** Answering this before designing
   anything for §4.2 avoids negotiating an SLO for capacity that does not exist.
5. **What is the actual demand shape?** If nobody genuinely needs 8 GPUs, §5.4's reservation
   machinery is unnecessary and the honest answer is a much simpler queue.
6. **Do the administrative domains persist?** Doc 20 §10.6's cellular model is recommended either
   way; the answer changes only how much replication machinery is worth building.
7. **Who owns the shared-lessons boundary** (person / team / project) in a multi-user deployment?
   This decides whether multi-user LoopLab is worth building at all, since cross-run memory sharing
   is its principal advantage over per-user JupyterHub sessions.

---

## 9. To verify before implementation

This document deliberately does not present un-checked external claims as verified. Reuse doc 20
§16's snapshot for Kubernetes, Kueue, GPU Operator, JupyterHub and Slurm behavior, and re-check the
following against current upstream documentation before building:

- Kueue preemption/borrowing semantics for a low-priority opportunistic ClusterQueue, and whether
  reservation-style protection for a wide head-of-queue workload is available or must come from the
  cluster scheduler.
- Kubernetes priority classes and preemption behavior for GPU-holding pods, including the
  termination grace window available for checkpointing.
- JupyterHub idle-culler capabilities and whether the deployment's spawner profiles can express a
  distinct non-preemptible interactive class.
- DCGM exporter metric names and retention needed for §4.1's window-distribution study.
- LiteLLM's exposure of the resolved model identifier, fallback/retry visibility, rate-limit headers
  and per-key budget readback (§3.1) — this is a question for the team operating it, not only for
  the documentation.
- MCP Python SDK behavior for streamable-HTTP servers behind corporate authentication, including
  token lifetime for long unattended sessions.
