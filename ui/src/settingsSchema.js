// One declarative description of every engine Settings field (looplab/config.py), grouped for a
// readable form. Both the Settings page and the New-run dialog render from this — so the launch
// dialog exposes exactly the same knobs the CLI `run` does, no more hand-maintained subset.
// types: int | float | bool | text | enum | list | secret
//   (float/text accept "" = unset → engine default; secret is a write-only credential — see below)
//
// Groups are the TABS in the settings UI, so each is a coherent topic of roughly balanced size.

export const SETTINGS_GROUPS = [
  {
    title: 'Search & policy', sub: 'how the loop explores the solution tree',
    fields: [
      { key: 'profile', label: 'Profile', type: 'enum', options: ['default', 'fast', 'thorough'],
        help: 'Preset bundle. default/fast = lean defaults; thorough = turn on multi-seed confirmation, the novelty gate, the reward-hack/leakage/critic monitors AND gate them (trust_gate=gate), ablation refinement and the prompt cues — in one word. A profile only fills fields you have not set yourself; any explicit knob here still wins.' },
      { key: 'policy', label: 'Policy', type: 'enum', options: ['greedy', 'evolutionary', 'mcts', 'asha', 'bohb'],
        agents: ['strategist'],
        help: 'Search strategy: greedy, evolutionary, MCTS, ASHA (multi-fidelity racing), or BOHB (ASHA racing × surrogate proposal).' },
      { key: 'max_nodes', label: 'Max nodes', type: 'int', agents: ['strategist', 'boss'],
        help: 'Node (experiment) budget — the loop stops after this many.' },
      { key: 'n_seeds', label: 'Seeds', type: 'int', agents: ['strategist'], help: 'Eval seeds per node (variance handling).' },
      { key: 'max_parallel', label: 'Max parallel', type: 'int', agents: ['strategist', 'boss'],
        help: '>1 fans out concurrent evals; 1 = deterministic single eval at a time.' },
      { key: 'ablate_every', label: 'Ablate every', type: 'int', agents: ['strategist'],
        help: 'Ablation-driven refinement every N improves (0 = off; greedy only).' },
      { key: 'archive_resolution', label: 'Archive resolution', type: 'float',
        help: 'Diversity-archive niche width in parameter space.' },
      { key: 'asha_eta', label: 'ASHA η (reduction)', type: 'int',
        help: 'Successive-halving factor: keep top 1/η survivors per rung (asha policy).' },
      { key: 'asha_rung_nodes', label: 'ASHA rung-0 width', type: 'int',
        help: 'Width of the cheap rung-0 draft base for asha/bohb. 0 = use n_seeds (default).' },
      { key: 'surrogate_proposer', label: 'Surrogate proposer', type: 'bool',
        help: 'A2: BO-lite k-NN surrogate over (params→metric) proposes the next point (numeric tasks).' },
      { key: 'surrogate_explore', label: 'Surrogate explore', type: 'float',
        help: 'A2: UCB-style exploration weight for the surrogate (0 = pure exploit).' },
      { key: 'researcher_panel', label: 'Researcher panel (K)', type: 'int',
        help: 'E2: propose K ideas and keep the best by an empirical surrogate (not LLM-judge). 1 = off.' },
    ],
  },
  {
    title: 'Strategist & operators', sub: 'A7 adaptive meta-control + richer proposal operators',
    fields: [
      { key: 'strategist_backend', label: 'Strategist', type: 'enum', options: ['off', 'rule', 'llm', 'agent'],
        help: 'Optional meta-controller that picks policy/operators/fidelity at runtime. off = static config; rule = deterministic heuristics; llm = model-driven single call (default); agent = tool-using — reads the run, data, sibling runs, knowledge base & memory before deciding (B1 stuck-guarded). All fall back to rule.' },
      { key: 'strategist_every', label: 'Consult every', type: 'int',
        help: 'Strategist consult cadence in created nodes (bounded, so it never thrashes).' },
      { key: 'merge_mode', label: 'Merge mode', type: 'enum', options: ['auto', 'mean', 'ensemble'], agents: ['strategist'],
        help: 'A0b: auto (default) = ensemble whenever the Developer generates real code, mean otherwise; mean = legacy mean-param merge; ensemble = Developer recombines parent solutions (code-level).' },
      { key: 'complexity_cue', label: 'Complexity cue', type: 'bool', agents: ['strategist'],
        help: 'A0d: inject a complexity hint keyed on a branch’s breadth (few children = minimal; many = advanced).' },
      { key: 'ablate_code_blocks', label: 'Ablate code blocks', type: 'bool', agents: ['strategist'],
        help: 'A0a: ablate generated pipeline code blocks (MLE-STAR), not just numeric params.' },
      { key: 'budget_aware', label: 'Budget-aware proposals', type: 'bool',
        help: 'A5: surface remaining eval budget into the proposal prompt (needs a max eval-time budget).' },
      { key: 'feature_engineering', label: 'Feature engineering', type: 'bool',
        help: 'I1: CAAFE-style — propose engineered features; the CV eval keeps only those that improve (tabular tasks).' },
      { key: 'failure_reflection', label: 'Failure reflection', type: 'bool',
        help: 'A4: feed recent failed-branch summaries into the proposal prompt so the proposer avoids repeating them.' },
      { key: 'localize_faults', label: 'Fault localization', type: 'bool',
        help: 'C1: rank the repo files most relevant to a failure and surface them in the prompt (repo tasks).' },
      { key: 'novelty_gate', label: 'Novelty gate', type: 'bool',
        help: 'E1: nudge near-duplicate proposals off each other so the search stops re-trying the same idea.' },
      { key: 'novelty_epsilon', label: 'Novelty ε', type: 'float',
        help: 'E1: normalized param-space distance below which a proposal counts as a near-duplicate.' },
      { key: 'novelty_semantic', label: 'Semantic novelty', type: 'bool',
        help: 'T5 (needs novelty gate): reject a proposal whose idea TEXT near-duplicates an existing node, with one informed re-propose surfacing the duplicate’s outcome (ShinkaEvolve: duplicate rejection before eval).' },
      { key: 'novelty_semantic_threshold', label: 'Semantic threshold', type: 'float',
        help: 'T5: cosine similarity above which two idea texts count as duplicates (default 0.92).' },
      { key: 'debug_depth', label: 'Debug depth', type: 'int',
        help: 'T10: how many error-feedback repairs a failing lineage gets before abandonment (default 2; deeper debugging is a verified lever).' },
      { key: 'operator_bandit', label: 'Operator bandit', type: 'bool',
        help: 'P4: replace fixed merge/ablate cadences with a deterministic UCB over per-operator yield (Δmetric per eval-second). On under profile=thorough.' },
      { key: 'track_hypotheses', label: 'Track hypotheses', type: 'bool',
        help: 'P1 (on by default): ask the Researcher to state the one-line hypothesis each experiment tests, register deep-research directions as hypotheses, and track them to a verdict on the Hypotheses board. Audit-only — never changes which node wins.' },
      { key: 'reflection_priors', label: 'Cross-run memory (priors + lessons)', type: 'bool',
        help: 'E4/M2/M3 (on by default): at run end distill the winner + structured LESSONS (incl. negative results — tested/abandoned hypotheses & failure themes) with a task fingerprint; at run start inject exact-task notes + fingerprint-matched lessons from SIMILAR past runs. No-op until a Memory dir is set (below).' },
    ],
  },
  {
    title: 'Resilience & efficiency', sub: 'crash recovery, dependency self-prep, and skipping doomed evals',
    fields: [
      { key: 'deep_repair', label: 'Deep repair', type: 'bool',
        help: 'C3: give the Developer the failure taxonomy + a “reproduce then fix” directive on debug/inline repair. On by default.' },
      { key: 'inline_repair', label: 'Inline crash repair', type: 'bool',
        help: 'Hybrid: when a node crashes, the agent triages it (repair/abandon/reject_idea) and repairs mechanical crashes IN PLACE — no new node, no node-budget spent. On by default; off = every crash waits for a budgeted debug node.' },
      { key: 'inline_repair_attempts', label: '↳ inline repair attempts', type: 'int',
        help: 'Max in-place repair retries per node before it fails normally (and stays eligible for the budgeted inter-node debug operator).' },
      { key: 'inline_repair_reasons', label: '↳ inline repair reasons', type: 'list',
        help: 'Comma-separated failure reasons eligible for inline repair (crash | timeout | oom | setup | no_metric | drift). Default "crash, timeout, oom" — a timeout is repaired by reducing compute, an OOM by reducing memory, not abandoned. Drop "timeout"/"oom" to leave those nodes to fail.' },
      { key: 'auto_install_deps', label: 'Auto-install missing libs', type: 'bool',
        help: 'When a node crashes purely because a KNOWN library is missing (ModuleNotFoundError — e.g. torch/xgboost/catboost), pip-install it into the eval interpreter and re-run instead of rejecting the idea. Trusted_local tier only. On by default.' },
      { key: 'dep_install_timeout', label: '↳ install timeout (s)', type: 'float',
        help: 'Per-package wall-clock budget for an auto-install. Generous default (900s) — large wheels like torch take minutes.' },
      { key: 'proxy_scoring', label: 'Proxy scoring', type: 'bool',
        help: 'A6: early-signal score candidates to skip doomed full evals.' },
      { key: 'proxy_kill_fraction', label: 'Proxy kill fraction', type: 'float',
        help: 'A6: skip the bottom fraction of candidates by proxy score (0 = never skip).' },
    ],
  },
  {
    title: 'Budgets & confirmation', sub: 'hard ceilings (blank = unbounded) + guarding seed-lucky winners',
    fields: [
      { key: 'max_seconds', label: 'Max wall-clock (s)', type: 'float', placeholder: 'unbounded',
        help: 'Abort the run cleanly after this many seconds of wall-clock.' },
      { key: 'max_eval_seconds', label: 'Max eval time (s)', type: 'float', placeholder: 'unbounded',
        agents: ['strategist', 'boss'],
        help: 'Ceiling on cumulative time spent INSIDE evals (survives resume).' },
      { key: 'timeout', label: 'Per-eval timeout (s)', type: 'float', agents: ['researcher', 'strategist', 'boss'],
        help: 'Kill a single eval after this long. Researcher = the “auto” per-node mode (it sizes heavy/neural-net experiments; this is the fallback).' },
      { key: 'confirm_top_k', label: 'Confirm top-k', type: 'int',
        help: 'Re-evaluate the best k nodes under multiple seeds before finishing (0 = off).' },
      { key: 'confirm_seeds', label: 'Confirm seeds', type: 'int', help: 'Seeds used in the confirmation pass.' },
      { key: 'confirm_seed_base', label: 'Confirm seed base', type: 'int',
        help: 'D1: first confirm seed. Default 1 keeps confirm splits DISJOINT from the search’s implicit seed 0, so the confirm metric is a generalization signal (0 = legacy overlap).' },
      { key: 'holdout_fraction', label: 'Holdout fraction', type: 'float',
        help: 'D1/B6: fraction of host-held labels reserved as a FINAL holdout the search never sees; search/confirm evals score on the rest. Host-graded tasks. 0 = off.' },
      { key: 'holdout_select', label: 'Holdout selects champion', type: 'bool',
        help: 'D1 (on by default): champion = best HOLDOUT metric among the val-top-k (the anti-validation-overfitting gate; AIRA val-test gap 15–16.6%). Off = holdout is audit-only (gap still shown).' },
      { key: 'holdout_top_k', label: 'Holdout top-k', type: 'int',
        help: 'D1: how many val-leaders get a holdout re-score at finish (free — re-scores existing predictions).' },
    ],
  },
  {
    title: 'LLM', sub: 'the model endpoint that proposes ideas and writes code',
    fields: [
      { key: 'backend', label: 'Role backend', type: 'enum', options: ['toy', 'llm'],
        help: 'toy = offline optimizer (no model); llm = live model Researcher/Developer.' },
      { key: 'llm_model', label: 'Model', type: 'text', placeholder: 'qwen3:8b', help: 'LLM model id (when backend = llm).' },
      { key: 'llm_base_url', label: 'Base URL', type: 'text', placeholder: 'http://localhost:11434/v1',
        help: 'OpenAI-compatible endpoint (Ollama by default).' },
      { key: 'llm_api_key', label: 'API key', type: 'secret',
        help: 'Stored securely server-side (owner-only file, never written to a run snapshot or returned by the API) and passed to runs as LOOPLAB_LLM_API_KEY. Leave blank to keep the saved key; local servers (Ollama / vLLM with no auth) ignore it.' },
      { key: 'llm_temperature', label: 'Temperature', type: 'float', help: 'Sampling temperature.' },
      { key: 'llm_parser', label: 'Structured parser', type: 'enum', options: ['tool_call', 'baml', 'outlines'],
        help: 'How structured ideas are parsed from the model.' },
      { key: 'llm_guided_json', label: 'Guided JSON decoding', type: 'bool',
        help: 'H1: constrain structured calls with the endpoint schema (vLLM/SGLang guided_json). Leave off for Ollama.' },
      { key: 'llm_reasoning', label: 'Reasoning', type: 'enum', options: ['', 'off', 'on', 'low', 'medium', 'high'],
        help: 'Send a thinking toggle in the request (provider-aware): Qwen3→enable_thinking, OpenAI/Ollama→reasoning_effort. Blank = server default (unchanged). Needs a reasoning-capable model.' },
      { key: 'llm_reasoning_style', label: 'Reasoning param style', type: 'enum', options: ['auto', 'qwen', 'effort', 'none'],
        help: 'Which request param shapes reasoning. auto picks qwen (enable_thinking) for qwen* models, else effort (reasoning_effort).' },
      { key: 'trace_llm_io', label: 'Capture LLM I/O', type: 'bool',
        help: 'Record each LLM prompt + completion into spans (the per-node Trace tab).' },
    ],
  },
  {
    title: 'Agent loop & models', sub: 'the self-driving agent loop + per-role model / endpoint overrides',
    fields: [
      { key: 'unified_agent', label: 'Unified agent', type: 'bool',
        help: 'One self-driving agent plays Researcher + Developer (+ Strategist) across stages, incl. crash triage. The per-role model fields below act as per-stage overrides (Researcher→propose, Developer→implement/repair). On by default; off = split roles.' },
      { key: 'agent_drives_actions', label: '↳ agent drives actions', type: 'bool',
        help: 'When Unified agent is on: let the agent pick the next macro action from a pure legal-action gate (it can never escape the pipeline). Off = the search policy still decides; the agent only fills the role slots.' },
      { key: 'agent_max_turns', label: 'Agent tool-loop turns', type: 'int', placeholder: 'unlimited',
        help: 'Max tool-call turns ANY agent (Researcher, pilot, crash-triage, run-chat Boss, deep-research, repo-scout) may take before its result is forced. Blank/0 = unlimited — the agent loops until done, never cut off mid-reasoning. Set a positive cap only to bound latency/cost.' },
      { key: 'agent_time_budget_s', label: 'Agent tool-loop time (s)', type: 'float', placeholder: 'unlimited',
        help: 'Wall-clock ceiling across an agent’s tool-loop turns. Blank/0 = no cap. Raise/clear this if a slow reasoning model gets cut off before it emits (replaces the old hardcoded 45s boss limit).' },
      { key: 'context_budget_chars', label: 'Context budget (chars)', type: 'int',
        help: 'H4: cap the agentic researcher’s tool-call history by middle-truncating stale output (0 = unbounded).' },
      { key: 'agent_stuck_detection', label: 'Stuck detection', type: 'bool',
        help: 'B1: the safety net that makes unlimited turns safe. Stops an agent that repeats the same call (or ping-pongs between two, or keeps hitting the same error) with no progress — forces its final emit instead of looping forever. On by default; reading different files / one long command never trips it.' },
      { key: 'agent_stuck_repeat', label: '↳ repeat threshold', type: 'int', placeholder: '4',
        help: 'How many identical call+result turns in a row count as “stuck” (min 2).' },
      { key: 'agent_stuck_alternate', label: '↳ alternate threshold', type: 'int', placeholder: '4',
        help: 'How many ping-pong cycles between two calls count as “stuck” (min 2).' },
      { key: 'agent_self_plan', label: 'Self-plan (TODO)', type: 'bool',
        help: 'C1: give long-running agents a TodoWrite-style update_plan tool so they keep their OWN checklist; the current plan is re-surfaced every few turns to keep the goal in view across a long loop. On by default.' },
      { key: 'agent_plan_reinject_every', label: '↳ plan re-inject every (turns)', type: 'int', placeholder: '5',
        help: 'How often (in tool-loop turns) to re-surface the agent’s current plan as a reminder.' },
      { key: 'agent_auto_summary', label: 'Auto-summary', type: 'bool',
        help: 'C2: when the tool-loop history grows long, LLM-summarize the stale middle instead of only truncating it. Trigger is Context budget if set, else a built-in ~120k-char high-water mark, so short loops are untouched. On by default; one extra model call per over-budget turn.' },
      { key: 'researcher_model', label: 'Researcher model', type: 'text', placeholder: 'shared',
        help: 'H3: per-role override — run the Researcher on its own model (blank = shared). Under Unified agent this is the propose-stage model.' },
      { key: 'developer_model', label: 'Developer model', type: 'text', placeholder: 'shared',
        help: 'H3: per-role override — e.g. Qwen3-Coder-30B for the Developer (blank = shared). Under Unified agent this is the implement/repair-stage model.' },
      { key: 'researcher_base_url', label: 'Researcher base URL', type: 'text', placeholder: 'shared',
        help: 'H3: per-role endpoint for the Researcher (blank = shared).' },
      { key: 'developer_base_url', label: 'Developer base URL', type: 'text', placeholder: 'shared',
        help: 'H3: per-role endpoint for the Developer (blank = shared).' },
    ],
  },
  {
    title: 'Developer agent', sub: 'optional external coding agent',
    fields: [
      { key: 'developer_backend', label: 'Developer backend', type: 'enum',
        options: ['default', 'opencode', 'aider', 'goose', 'continue'],
        help: 'default = in-house templated/LLM developer; or drive an external CLI coding agent.' },
      { key: 'best_of_n', label: 'Best-of-N', type: 'int',
        help: 'C2: generate N candidate implementations per node, keep the best by an execution-free reward (1 = off; in-house LLM developer only).' },
      { key: 'agent_cmd', label: 'Agent command', type: 'text', placeholder: 'auto',
        help: 'Override the external agent launcher (path / wrapper).' },
      { key: 'validate_agent', label: 'Validate agent output', type: 'bool',
        help: 'Audit each agent result (no-op/syntax/crash), retry with feedback, fall back to the LLM developer.' },
      { key: 'agent_max_retries', label: 'Agent retries', type: 'int', help: 'Re-prompts of the agent on an invalid result.' },
      { key: 'agent_patch_gate', label: 'Patch gate', type: 'bool',
        help: 'Run the agent in a git worktree and accept only edits within the edit-surface.' },
      { key: 'agent_surface', label: 'Edit surface', type: 'list', placeholder: '*.py',
        help: 'Comma-separated globs the agent may edit.' },
    ],
  },
  {
    title: 'Safety & trust', sub: 'isolation, human-in-the-loop, and audit-only monitors',
    fields: [
      { key: 'trust_mode', label: 'Sandbox tier', type: 'enum', options: ['trusted_local', 'untrusted', 'hostile'],
        help: 'trusted_local = subprocess; untrusted = Docker --network none; hostile = + gVisor (runsc) true-isolation runtime (B4+).' },
      { key: 'docker_image', label: 'Docker image', type: 'text', placeholder: 'python:3.12-slim',
        help: 'Image for the untrusted eval tier.' },
      { key: 'eval_trust_mode', label: 'Eval trust', type: 'enum',
        options: ['ratify_freeze', 'autonomous', 'ratify_freeze_drift'],
        help: 'Trust policy for an agent-authored eval/metric adapter.' },
      { key: 'require_approval', label: 'Require approval (HITL)', type: 'bool',
        help: 'Pause for human approval of the final best before finishing.' },
      { key: 'redact_output', label: 'Redact output secrets', type: 'bool',
        help: 'B3: mask credentials/high-entropy tokens in the persisted stdout/stderr tail (recommended for untrusted tiers).' },
      { key: 'reward_hack_detect', label: 'Reward-hack detector', type: 'bool',
        help: 'B5: flag suspicious wins (grader/answer-key access, frozen-file writes, perfect metrics) in the Trust panel. Audit-only.' },
      { key: 'code_leakage_detect', label: 'Code-leakage scan', type: 'bool',
        help: 'I3: static scan of each solution for train→test leakage (fit-before-split, fit-on-test); surfaced in the Trust panel.' },
      { key: 'critic_check', label: 'Independent critic', type: 'bool',
        help: 'C4: execution-free critic of each solution (stub / hardcoded-metric / params-ignored); surfaced in the Trust panel.' },
      { key: 'trust_gate', label: 'Trust enforcement', type: 'enum', options: ['audit', 'gate', 'block'],
        help: 'T2: what a reward-hack / data-leakage flag DOES to selection. audit = surface only (default); gate = a flagged node can no longer be selected as best (still repairable); block = also mark it infeasible so the policy won’t breed from it. The heuristic critic signal always stays advisory.' },
    ],
  },
  {
    title: 'Knowledge & memory', sub: 'directories the scientist reads + literature/deep research',
    fields: [
      { key: 'researcher_tools', label: 'Run-introspection tools', type: 'bool',
        help: 'On (default): the Researcher (and Deep-Research) can read its own experiments (list/read/find-analogous/themes/code) and the task data (schema/profile) mid-loop. Off: legacy single-shot proposer (richer default digest still added).' },
      { key: 'knowledge_dir', label: 'Knowledge dir', type: 'text', placeholder: 'unset',
        help: 'Markdown notes for agentic retrieval (also where pre-research is saved).' },
      { key: 'skills_dir', label: 'Skills dir', type: 'text', placeholder: 'unset', help: 'SKILL.md files the Researcher can load.' },
      { key: 'literature_search', label: 'Literature grounding', type: 'bool',
        help: 'E3: give the agentic Researcher an arXiv search tool (network-optional; fails gracefully if blocked).' },
      { key: 'web_search', label: 'Web search (deep research)', type: 'bool',
        help: 'P2: give the Deep-Research stage a general web search/fetch tool on top of arXiv (network-optional).' },
      { key: 'deep_research_every', label: 'Deep research every N nodes', type: 'int', placeholder: '0',
        help: 'Run the Deep-Research stage automatically every N created nodes (0 = manual button / Strategist only).' },
      { key: 'concurrent_research', label: 'Concurrent research (overlap eval)', type: 'bool',
        help: 'Overlap a due deep-research "think" with the GPU-bound eval, so the agent works while the node trains. Best with a REMOTE LLM (no GPU contention with eval); off by default — validate on your setup before enabling.' },
      { key: 'prompt_dir', label: 'Prompt dir', type: 'text', placeholder: 'unset', help: 'Editable, hot-reloaded role prompt .md files.' },
      { key: 'memory_dir', label: 'Memory dir', type: 'text', placeholder: 'unset', help: 'Cross-run case memory.' },
      { key: 'embed_model', label: 'Embedding model', type: 'text', placeholder: 'hash (lexical)',
        help: 'T4: model for an OpenAI-compatible /embeddings endpoint (e.g. nomic-embed-text) → SEMANTIC kb_search / case retrieval. Blank = dependency-free lexical hashing. A misconfigured/offline endpoint degrades back to lexical (never crashes).' },
      { key: 'embed_base_url', label: 'Embedding base URL', type: 'text', placeholder: 'reuse llm_base_url',
        help: 'Endpoint for embeddings if different from the chat model’s (blank = reuse llm_base_url).' },
    ],
  },
]

// Agent-governance pills (Settings.agent_control): for a field with `agents`, render one toggle per
// role showing whether that autonomous role may change the setting at runtime. R = Researcher (per
// experiment, e.g. sizes a neural-net's eval timeout), S = Strategist (run-wide), B = Boss (run chat).
export const AGENT_ROLE_PILLS = {
  researcher: { short: 'R', title: 'Researcher may set this per experiment (auto)' },
  strategist: { short: 'S', title: 'Strategist may change this run-wide' },
  boss: { short: 'B', title: 'Boss (run chat) may change this run-wide' },
}

// Flat key → field-spec index (used to coerce values on the way out).
export const FIELD_BY_KEY = Object.fromEntries(
  SETTINGS_GROUPS.flatMap(g => g.fields.map(f => [f.key, f])))

// Coerce a form value (mostly strings from inputs) to the JSON type the API expects, or null when
// blank/unset so the engine falls back to its own default.
export function coerce(field, raw) {
  if (field.type === 'bool') return !!raw
  if (raw === '' || raw == null) return null
  if (field.type === 'int') { const n = parseInt(raw, 10); return Number.isFinite(n) ? n : null }
  if (field.type === 'float') { const n = parseFloat(raw); return Number.isFinite(n) ? n : null }
  if (field.type === 'list') return String(raw).split(',').map(s => s.trim()).filter(Boolean)
  return raw   // text / enum
}

// Turn a settings object into the form's editable shape (lists → comma string, null → '').
// `secret` fields are write-only: the API only ever returns the masked "***", never the value, so
// the input always starts BLANK (a non-empty edit means "set a new key" — see Settings.onSave).
export function toForm(settings) {
  const out = {}
  for (const [k, f] of Object.entries(FIELD_BY_KEY)) {
    const v = settings?.[k]
    if (f.type === 'secret') out[k] = ''
    else if (f.type === 'bool') out[k] = !!v
    else if (f.type === 'list') out[k] = Array.isArray(v) ? v.join(', ') : (v ?? '')
    else out[k] = v == null ? '' : v
  }
  return out
}

// Turn the form shape back into a coerced settings object (for PUT /api/settings or run launch).
// `secret` fields are SKIPPED — they never travel in the settings payload (they go through the
// dedicated, owner-only secret endpoint instead), so a credential can't land in ui_settings.json.
export function fromForm(form) {
  const out = {}
  for (const [k, f] of Object.entries(FIELD_BY_KEY)) {
    if (f.type === 'secret') continue
    out[k] = coerce(f, form[k])
  }
  return out
}
