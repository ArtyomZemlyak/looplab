// One declarative description of every engine Settings field (looplab/config.py), grouped for a
// readable form. Both the Settings page and the New-run dialog render from this — so the launch
// dialog exposes exactly the same knobs the CLI `run` does, no more hand-maintained subset.
// types: int | float | bool | text | enum | list  (float/text accept "" = unset → engine default)

export const SETTINGS_GROUPS = [
  {
    title: 'Search & policy', sub: 'how the loop explores the solution tree',
    fields: [
      { key: 'policy', label: 'Policy', type: 'enum', options: ['greedy', 'evolutionary', 'mcts', 'asha', 'bohb'],
        help: 'Search strategy: greedy, evolutionary, MCTS, ASHA (multi-fidelity racing), or BOHB (ASHA racing × surrogate proposal).' },
      { key: 'max_nodes', label: 'Max nodes', type: 'int',
        help: 'Node (experiment) budget — the loop stops after this many.' },
      { key: 'n_seeds', label: 'Seeds', type: 'int', help: 'Eval seeds per node (variance handling).' },
      { key: 'max_parallel', label: 'Max parallel', type: 'int',
        help: '>1 fans out concurrent evals; 1 = deterministic single eval at a time.' },
      { key: 'ablate_every', label: 'Ablate every', type: 'int',
        help: 'Ablation-driven refinement every N improves (0 = off; greedy only).' },
      { key: 'archive_resolution', label: 'Archive resolution', type: 'float',
        help: 'Diversity-archive niche width in parameter space.' },
      { key: 'asha_eta', label: 'ASHA η (reduction)', type: 'int',
        help: 'Successive-halving factor: keep top 1/η survivors per rung (asha policy).' },
      { key: 'surrogate_proposer', label: 'Surrogate proposer', type: 'bool',
        help: 'A2: BO-lite k-NN surrogate over (params→metric) proposes the next point (numeric tasks).' },
      { key: 'surrogate_explore', label: 'Surrogate explore', type: 'float',
        help: 'A2: UCB-style exploration weight for the surrogate (0 = pure exploit).' },
    ],
  },
  {
    title: 'Strategist & operators', sub: 'A7 adaptive meta-control + richer operators (config-first)',
    fields: [
      { key: 'strategist_backend', label: 'Strategist', type: 'enum', options: ['off', 'rule', 'llm'],
        help: 'Optional meta-controller that picks policy/operators/fidelity at runtime. off = static config (default); rule = deterministic heuristics; llm = model-driven (falls back to rule).' },
      { key: 'strategist_every', label: 'Consult every', type: 'int',
        help: 'Strategist consult cadence in created nodes (bounded, so it never thrashes).' },
      { key: 'merge_mode', label: 'Merge mode', type: 'enum', options: ['mean', 'ensemble'],
        help: 'A0b: mean = legacy mean-param merge; ensemble = Developer recombines parent solutions (code-level).' },
      { key: 'complexity_cue', label: 'Complexity cue', type: 'bool',
        help: 'A0d: inject a complexity hint keyed on a branch’s breadth (few children = minimal; many = advanced).' },
      { key: 'budget_aware', label: 'Budget-aware proposals', type: 'bool',
        help: 'A5: surface remaining eval budget into the proposal prompt (needs a max eval-time budget).' },
      { key: 'feature_engineering', label: 'Feature engineering', type: 'bool',
        help: 'I1: CAAFE-style — propose engineered features; the CV eval keeps only those that improve (tabular tasks).' },
      { key: 'ablate_code_blocks', label: 'Ablate code blocks', type: 'bool',
        help: 'A0a: ablate generated pipeline code blocks (MLE-STAR), not just numeric params.' },
      { key: 'proxy_scoring', label: 'Proxy scoring', type: 'bool',
        help: 'A6: early-signal score candidates to skip doomed full evals.' },
      { key: 'proxy_kill_fraction', label: 'Proxy kill fraction', type: 'float',
        help: 'A6: skip the bottom fraction of candidates by proxy score (0 = never skip).' },
      { key: 'reward_hack_detect', label: 'Reward-hack detector', type: 'bool',
        help: 'B5: flag suspicious wins (grader/answer-key access, frozen-file writes, perfect metrics) in the Trust panel. Audit-only.' },
      { key: 'novelty_gate', label: 'Novelty gate', type: 'bool',
        help: 'E1: nudge near-duplicate proposals off each other so the search stops re-trying the same idea.' },
      { key: 'novelty_epsilon', label: 'Novelty ε', type: 'float',
        help: 'E1: normalized param-space distance below which a proposal counts as a near-duplicate.' },
      { key: 'reflection_priors', label: 'Reflection priors', type: 'bool',
        help: 'E4: distill a meta-review at run end and inject prior-run insights into the next run’s prompt (needs a memory dir).' },
    ],
  },
  {
    title: 'Confirmation', sub: 'guard against seed-lucky winners',
    fields: [
      { key: 'confirm_top_k', label: 'Confirm top-k', type: 'int',
        help: 'Re-evaluate the best k nodes under multiple seeds before finishing (0 = off).' },
      { key: 'confirm_seeds', label: 'Confirm seeds', type: 'int', help: 'Seeds used in the confirmation pass.' },
    ],
  },
  {
    title: 'Budgets', sub: 'hard ceilings (blank = unbounded)',
    fields: [
      { key: 'max_seconds', label: 'Max wall-clock (s)', type: 'float', placeholder: 'unbounded',
        help: 'Abort the run cleanly after this many seconds of wall-clock.' },
      { key: 'max_eval_seconds', label: 'Max eval time (s)', type: 'float', placeholder: 'unbounded',
        help: 'Ceiling on cumulative time spent INSIDE evals (survives resume).' },
      { key: 'timeout', label: 'Per-eval timeout (s)', type: 'float', help: 'Kill a single eval after this long.' },
    ],
  },
  {
    title: 'Roles & LLM', sub: 'who proposes ideas and writes code',
    fields: [
      { key: 'backend', label: 'Role backend', type: 'enum', options: ['toy', 'llm'],
        help: 'toy = offline optimizer (no model); llm = live model Researcher/Developer.' },
      { key: 'llm_model', label: 'Model', type: 'text', placeholder: 'qwen3:8b', help: 'LLM model id (when backend = llm).' },
      { key: 'llm_base_url', label: 'Base URL', type: 'text', placeholder: 'http://localhost:11434/v1',
        help: 'OpenAI-compatible endpoint (Ollama by default).' },
      { key: 'llm_temperature', label: 'Temperature', type: 'float', help: 'Sampling temperature.' },
      { key: 'llm_parser', label: 'Structured parser', type: 'enum', options: ['tool_call', 'baml', 'outlines'],
        help: 'How structured ideas are parsed from the model.' },
      { key: 'researcher_model', label: 'Researcher model', type: 'text', placeholder: 'shared',
        help: 'H3: per-role override — run the Researcher on its own model (blank = shared model).' },
      { key: 'developer_model', label: 'Developer model', type: 'text', placeholder: 'shared',
        help: 'H3: per-role override — e.g. Qwen3-Coder-30B for the Developer (blank = shared model).' },
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
    title: 'Sandbox & trust', sub: 'isolation and human-in-the-loop',
    fields: [
      { key: 'trust_mode', label: 'Sandbox tier', type: 'enum', options: ['trusted_local', 'untrusted'],
        help: 'trusted_local = subprocess (no Docker); untrusted = Docker --network none.' },
      { key: 'docker_image', label: 'Docker image', type: 'text', placeholder: 'python:3.12-slim',
        help: 'Image for the untrusted eval tier.' },
      { key: 'eval_trust_mode', label: 'Eval trust', type: 'enum',
        options: ['ratify_freeze', 'autonomous', 'ratify_freeze_drift'],
        help: 'Trust policy for an agent-authored eval/metric adapter.' },
      { key: 'require_approval', label: 'Require approval (HITL)', type: 'bool',
        help: 'Pause for human approval of the final best before finishing.' },
    ],
  },
  {
    title: 'Authoring & memory', sub: 'directories the scientist reads',
    fields: [
      { key: 'knowledge_dir', label: 'Knowledge dir', type: 'text', placeholder: 'unset',
        help: 'Markdown notes for agentic retrieval (also where pre-research is saved).' },
      { key: 'skills_dir', label: 'Skills dir', type: 'text', placeholder: 'unset', help: 'SKILL.md files the Researcher can load.' },
      { key: 'prompt_dir', label: 'Prompt dir', type: 'text', placeholder: 'unset', help: 'Editable, hot-reloaded role prompt .md files.' },
      { key: 'memory_dir', label: 'Memory dir', type: 'text', placeholder: 'unset', help: 'Cross-run case memory.' },
    ],
  },
  {
    title: 'Observability',
    fields: [
      { key: 'trace_llm_io', label: 'Capture LLM I/O', type: 'bool',
        help: 'Record each LLM prompt + completion into spans (the per-node Trace tab).' },
    ],
  },
]

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
export function toForm(settings) {
  const out = {}
  for (const [k, f] of Object.entries(FIELD_BY_KEY)) {
    const v = settings?.[k]
    if (f.type === 'bool') out[k] = !!v
    else if (f.type === 'list') out[k] = Array.isArray(v) ? v.join(', ') : (v ?? '')
    else out[k] = v == null ? '' : v
  }
  return out
}

// Turn the form shape back into a coerced settings object (for PUT /api/settings or run launch).
export function fromForm(form) {
  const out = {}
  for (const [k, f] of Object.entries(FIELD_BY_KEY)) out[k] = coerce(f, form[k])
  return out
}
