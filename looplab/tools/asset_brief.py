"""D1 · Seed-time asset & prior-art brief (PART IV, §21.2 — the cheap independent win, §21.13).

**Why this exists.** The `rubertlite` run's winning recipe — teacher-mined hard negatives + NV-0.95
false-negative filtering — was **already proven on that exact benchmark** (+0.03–0.04 recall@100 on a
sibling model, recorded in the repo's own `results_last.xlsx`), and the stronger teacher checkpoints sat
in the repo with their metrics literally in the filename (`…@0.899`). **Both were never read.** The loop
was blind not to *ideas* but to *on-disk assets*. Measured payoff (§21.10): with a repo-derived asset
brief the proposer's #1 direction is grounded in the exact existing infra + proven params
(`n_negatives`/`negatives_path`, `loss.type='mnr'`, the NV-0.95 filter, distill-from-teacher) — one line,
"hard-neg + NV-0.95 gave +0.04 here", short-circuits ~50 nodes. This is the highest-ROI PART IV lever and
depends on nothing (§21.13 keystone-independent).

**What this does — agentic FIRST, deterministic FALLBACK.** The PRIMARY path (`agentic_asset_brief`) is
an LLM agent that EXPLORES the task repo with read-only tools (`RepoScoutTools`: list_dir / find_files /
grep / read_file) and writes a grounded "prior art & available assets" brief — reading real files and
citing them, exactly the §21.10 meta-lesson ("grounding beats model strength; feed the model the repo").
The agent is universal: it works on ANY task repo and decides for itself which capabilities/results
matter, with no hardcoded domain vocabulary.

The deterministic `scan_assets`/`format_brief` layer is the OFFLINE FALLBACK (no LLM client) and the
agent's starting hint: a bounded, read-only walk that surfaces the three §21.2 asset classes — (a) result
logs / experiment tables (`results_last.*`, README result sections), (b) sibling checkpoints and the
metrics in their filenames, (c) training configs / trainers — task-AGNOSTICALLY. Any domain-specific
vocabulary (e.g. "hard-negative mining", "distillation") lives ONLY in a pluggable per-task-type
`AssetLexicon` (dense-retrieval is one registered pack via `lexicon_for`); with no lexicon the fallback
still reports every asset + its metrics generically. Metric-name recognition uses a cross-domain family
(retrieval + classification + regression + NLG), not a task-specific list.

**Discipline (mirrors `tools/env_inspect.py`).** Purely read-only local I/O — nothing is executed, no
network, no writes; the agent's tools are secret-guarded and root-scoped. Traversal is bounded (ignored
dirs pruned, file-count and per-file byte caps). This is D1's *local-asset* complement to §10's
*external-literature* grounding, so it needs **no network policy** and belongs on the early lane (§6.6).

**Phase 0 scope.** The brief producer (agentic + fallback). Feeding the brief into `roles._state_brief`
and seeding hypotheses from it is the live wiring (Phase 2), deliberately not done here — Phase 0 stays
additive and event-free.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from looplab.trust.redact import redact_secrets

# Directories never worth walking for assets (VCS/build/venv/cache) — pruned for speed and to avoid
# reading a checkout's dependencies as if they were the task's own prior art.
_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", "env", ".env", "site-packages", ".tox", "dist", "build",
    ".idea", ".vscode", ".ipynb_checkpoints", "wandb", ".cache",
}

# Filename patterns that mark a RESULT log / experiment table.
_RESULT_NAME = re.compile(
    r"(result|results|metrics|scores?|leaderboard|experiments?|eval|report)[\w.\-]*"
    r"\.(csv|tsv|json|jsonl|md|txt|xlsx|xls)$", re.I)
# README files — §21.2 names "README result sections" explicitly; scanned as result tables.
_README_NAME = re.compile(r"^readme[\w.\-]*\.(md|rst|txt)$", re.I)
# Checkpoint / model-weight files (the metric is often IN the filename).
_CKPT_NAME = re.compile(r"\.(ckpt|pt|pth|bin|safetensors|onnx|h5|pkl|npz)$", re.I)
# Config / trainer files worth grepping for capability tokens.
_CONFIG_NAME = re.compile(r"(config|conf|params|hparams|train|training|finetune|recipe)[\w.\-]*"
                          r"\.(ya?ml|json|toml|ini|cfg|py)$", re.I)

# A metric mention: an optional metric NAME (from a known family) + `@k` + a separator + a float, OR a
# bare `@<float>` (the `nomic-moe@0.899` checkpoint spelling). Kept conservative so `lr=0.001` /
# `seed=42` are NOT mistaken for scores — the name must be from a known metric family. This family list
# is intentionally CROSS-DOMAIN (retrieval + classification + regression + NLG), not task-specific: it is
# the universal ML metric vocabulary the generic scanner uses on ANY repo. A task-type lexicon may extend
# it via `AssetLexicon.metric_names`.
_BASE_METRIC_FAMILY = (r"recall|ndcg|mrr|map|acc|accuracy|f1|auc|roc|precision|prec|score|em|"
                       r"bleu|rouge|meteor|spearman|pearson|hit|success|dice|iou|psnr|ssim|"
                       r"rmse|mae|mse|r2|perplexity|ppl|wer|cer")
_AT_METRIC = re.compile(r"@\s*(0?\.\d+|[01]\.\d+)")

# Metric-name stems where LOWER is better (error/loss family) — excluded from the "best on-disk result"
# headline, which is a max and would otherwise call the WORST error the best. Matched as a substring of
# the (lowercased) metric name, so `val_rmse`/`test_wer` resolve too.
_LOWER_IS_BETTER = ("rmse", "mae", "mse", "wer", "cer", "perplexity", "ppl", "loss", "error")


def _named_metric_re(extra: tuple[str, ...] = ()) -> "re.Pattern":
    fam = _BASE_METRIC_FAMILY + ("|" + "|".join(re.escape(e) for e in extra) if extra else "")
    # Boundaries are `(?<![a-z0-9])` / `(?![a-z0-9])`, NOT `\b`: checkpoint filenames glue the metric to a
    # preceding token with an underscore (`..._step=5262_val_recall@100=0.873.ckpt`), and `\b` treats `_` as
    # a WORD char so it never anchors before `val` — the exact reason the metric read as "(no metric in
    # filename)". Underscore-as-separator is the whole point of surfacing a checkpoint's score.
    return re.compile(
        rf"(?<![a-z0-9])((?:val[_\-]?|test[_\-]?|dev[_\-]?)?(?:{fam})(?:@\d+)?)\s*[=:_\-]\s*"
        r"(0?\.\d+|[01]\.\d+|\d{1,3}\.\d+|\d{1,3})(?![a-z0-9])", re.I)


# --------------------------------------------------------------------------- #
# Task-type asset lexicon (the ONLY domain-specific layer — pluggable, never hardcoded into the scanner)
# --------------------------------------------------------------------------- #
#
# The scanner itself is universal: it surfaces result tables, checkpoints-with-metrics, and config/
# trainer files on ANY repo with zero domain assumptions. A *lexicon* is an optional per-task-type pack
# that, when the caller knows the task family, additionally NAMES the domain capabilities a config/trainer
# already implements — e.g. for dense-retrieval, whether the repo has hard-negative mining or teacher
# distillation. This mirrors the concept-graph's `skeleton_for` registry: dense-retrieval is ONE
# registered pack, not baked into the tool. With no lexicon the scanner still reports every asset + its
# metrics + a config snippet (generic perception); a lexicon only enriches config findings with named
# capabilities.

@dataclass(frozen=True)
class AssetLexicon:
    """A pluggable per-task-type vocabulary. `capability_patterns` = {capability_name: regex} of infra a
    config/trainer might implement; `metric_names` extends the universal metric family with task-specific
    metric tokens. Both optional — the empty lexicon is the fully-generic default."""
    task_type: str = ""
    capability_patterns: dict[str, str] = field(default_factory=dict)
    metric_names: tuple[str, ...] = ()


# The dense-retrieval pack — the ONE validated instance (the exact levers the `rubertlite` run had on
# disk but never used). Registered like a plugin; the scanner does not depend on it.
DENSE_RETRIEVAL_LEXICON = AssetLexicon(
    task_type="dense-retrieval",
    # Patterns are word-boundaried and use SPECIFIC tokens — a deterministic fallback must not smear
    # (bare `ance` matched "balance", bare `gpl` matched a license line). The agentic path is the primary
    # one; this pack only enriches the offline scan when the caller opts into dense-retrieval.
    capability_patterns={
        "hard-negative-mining": (r"\bn_negatives\b|\bnum_negatives\b|\bnegatives_path\b|"
                                 r"\bhard[_\- ]?negative|\bmined[_\- ]?negative|\bmine_negatives\b|"
                                 r"\bANCE\b|\brocketqa\b"),
        "false-negative-filtering": (r"\bnv[_\-](?:retriever|filter)\b|\bnv-0?\.\d+|\bfalse[_\- ]?negative|"
                                     r"\bdenoise|\bpositive[_\- ]?aware\b"),
        "distillation": (r"\bdistill|\bteacher\b|\bmargin[_\- ]?mse\b|\bknowledge[_\- ]?distill|"
                         r"\bcross[_\- ]?encoder\b"),
        "mnr-loss": r"\bmnr\b|\bmultiple[_\- ]?negatives|\bmultiplenegatives\b",
        "contrastive-loss": r"\bcontrastive\b|\binfonce\b|\bdcl\b|\bdecoupled[_\- ]?contrastive",
        "synthetic-data": r"\bdoc2query\b|\bquery[_\- ]?generation\b|\bpseudo[_\- ]?quer|\bsynthetic[_\- ]?quer",
    },
)

# task-type -> lexicon. Additive: a new task family registers one row (mirrors `_SKELETONS`).
_LEXICONS = {
    "dense-retrieval": DENSE_RETRIEVAL_LEXICON,
}
# Fuzzy task-id -> registered-pack aliases, so a run whose task is named e.g. "vectorizer" still resolves
# the dense-retrieval pack. Substring match, first hit wins; unknown -> the empty generic lexicon.
_LEXICON_ALIASES = {
    "dense-retrieval": ("dense-retrieval", "dense_retrieval", "retrieval", "vectorizer", "embedding",
                        "sentence-transformer", "bi-encoder", "biencoder"),
}


def lexicon_for(task_type: Optional[str]) -> AssetLexicon:
    """Resolve a task type to its asset lexicon; the empty (fully-generic) lexicon when unregistered."""
    t = (task_type or "").strip().lower()
    if not t:
        return AssetLexicon()
    if t in _LEXICONS:
        return _LEXICONS[t]
    for pack, aliases in _LEXICON_ALIASES.items():
        if any(a in t for a in aliases):
            return _LEXICONS[pack]
    return AssetLexicon(task_type=task_type or "")


@dataclass
class AssetFinding:
    """One discovered asset. `metrics` is the parsed {name: value} (best-effort); `detail` is a short
    human string (extracted tokens / a snippet). `path` is repo-relative."""
    kind: str                    # "result-table" | "checkpoint" | "config"
    path: str
    detail: str = ""
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class AssetBrief:
    """The structured scan. `format_brief` renders it to the string the proposer would read."""
    repo_root: str
    results: list[AssetFinding] = field(default_factory=list)
    checkpoints: list[AssetFinding] = field(default_factory=list)
    configs: list[AssetFinding] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)     # detected capability tokens (sorted)
    best_known: Optional[dict] = None                          # {metric, value, source} top on-disk score
    notes: list[str] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False                                    # hit the file-count cap


# --------------------------------------------------------------------------- #
# Safe read-only helpers (mirror env_inspect: bounded, errors-replaced, no execution)
# --------------------------------------------------------------------------- #

def _read_text(path: Path, max_bytes: int) -> str:
    try:
        # Read-then-REDACT at the boundary: the returned lines can be copied verbatim into the agentic
        # LLM seed prompt (Phase 2), so scrub credentials HERE — any key/token in a scanned config/log
        # is masked before it can reach a finding, a snippet, or the seed. `redact_secrets` masks known
        # credential patterns + high-entropy blobs; it never touches short low-entropy tokens, so metric
        # values, metric names (`recall@100`) and capability words survive intact. Bounded by max_bytes.
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return redact_secrets(f.read(max_bytes))
    except (OSError, ValueError):
        return ""


def _extract_metrics(text: str, *, limit: int = 40, named_re: "re.Pattern" = None) -> dict[str, float]:
    """Best-effort {metric_name: value} from arbitrary text (a filename, a table row, a config). Only
    known-family metric NAMES match, so hyperparameters are not mistaken for scores. Keeps the BEST
    (max) value per name — higher-is-better is the common ML convention (a caller that knows a min-metric
    can reinterpret). `named_re` lets a task lexicon widen the metric family; None = the universal set."""
    named = named_re or _named_metric_re()
    out: dict[str, float] = {}
    for m in list(named.finditer(text))[:limit]:
        name = m.group(1).lower().strip("_-")
        raw = m.group(2)
        try:
            val = float(raw)
        except ValueError:
            continue
        # `precision=16` / `bf16` / `fp16` is MIXED-PRECISION training config, not a metric score — a
        # real precision metric is a fraction (0.82) or `precision@k`. Drop a bare-integer precision match
        # so the "hyperparameters are not mistaken for scores" guarantee holds for this common collision.
        if name.startswith("prec") and "@" not in name and "." not in raw:
            continue
        # percent-style tables (91.2) coexist with fraction-style (0.912); don't cross-normalize —
        # just keep the max seen under each name (comparisons stay within one convention per name).
        if name not in out or val > out[name]:
            out[name] = val
    return out


def _extract_table_metrics(text: str, *, max_rows: int = 500, fam: str = "") -> dict[str, float]:
    """Metrics from a DELIMITED table (csv/tsv/markdown), where a metric NAME heads a column and the
    values sit below it in separate cells — the `name=value` adjacency `_extract_metrics` needs is
    absent there. Detects the delimiter, finds the header row, and keeps the max value under each
    metric-family column. Best-effort and forgiving of ragged rows. `fam` = the metric-name alternation
    (defaults to the universal family)."""
    fam = fam or _BASE_METRIC_FAMILY
    lines = [ln for ln in text.splitlines() if ln.strip()][:max_rows]
    if len(lines) < 2:
        return {}
    # pick the delimiter that appears most across the file — the header may not be line 0 (a markdown
    # table is preceded by a `## Results` heading), so counting only the first line misses it.
    delim = max((",", "\t", "|", ";"), key=lambda d: sum(ln.count(d) for ln in lines))
    if sum(ln.count(delim) for ln in lines) == 0:
        return {}

    def cells(line: str) -> list[str]:
        return [c.strip().strip("|").strip() for c in line.split(delim)]

    # header = the first row that names at least one metric-family column
    header = None
    body_start = 0
    for i, ln in enumerate(lines[:20]):
        row = cells(ln)
        if any(re.search(rf"\b(?:{fam})\b", c, re.I) for c in row if c):
            header, body_start = row, i + 1
            break
    if header is None:
        return {}
    metric_cols = {j: c.lower().strip("_- ") for j, c in enumerate(header)
                   if re.search(rf"\b(?:{fam})(?:@\d+)?\b", c, re.I)}
    out: dict[str, float] = {}
    for ln in lines[body_start:]:
        row = cells(ln)
        # skip a markdown separator row (---|:--:|---)
        if all(set(c) <= set("-: ") for c in row if c):
            continue
        for j, name in metric_cols.items():
            if j >= len(row):
                continue
            m = re.match(r"^(0?\.\d+|[01]\.\d+|\d{1,3}\.\d+|\d{1,3})$", row[j])
            if not m:
                continue
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            if name not in out or val > out[name]:
                out[name] = val
    return out


def _detect_capabilities(text: str, patterns: dict[str, str]) -> set[str]:
    """Named capabilities a config/trainer implements, per the task lexicon's `capability_patterns`.
    Empty patterns (the generic default) -> no capabilities, so the scanner stays task-agnostic."""
    found: set[str] = set()
    low = text.lower()
    for cap, pat in (patterns or {}).items():
        if re.search(pat, low, re.I):
            found.add(cap)
    return found


# --------------------------------------------------------------------------- #
# The scan
# --------------------------------------------------------------------------- #

def scan_assets(repo_root, *, task_type: Optional[str] = None, lexicon: Optional[AssetLexicon] = None,
                max_files: int = 4000, max_read_files: int = 400,
                max_bytes: int = 200_000) -> AssetBrief:
    """Walk `repo_root` read-only and collect result tables, checkpoints, and configs. Bounded by
    `max_files` (entries examined) and `max_read_files`/`max_bytes` (content actually read). Pure I/O —
    never executes anything, never writes.

    The scan is task-AGNOSTIC by default (surfaces every asset + its metrics generically). Pass a
    `task_type` (or an explicit `lexicon`) to additionally NAME the domain capabilities a config/trainer
    implements — dense-retrieval is one registered pack; unknown types get the empty generic lexicon.

    This is the deterministic FALLBACK for `agentic_asset_brief` (the primary, LLM-driven path) and the
    fully-offline path used when no LLM client is wired."""
    lex = lexicon if lexicon is not None else lexicon_for(task_type)
    named_re = _named_metric_re(lex.metric_names)
    fam = _BASE_METRIC_FAMILY + ("|" + "|".join(re.escape(e) for e in lex.metric_names)
                                 if lex.metric_names else "")
    root = Path(repo_root)
    brief = AssetBrief(repo_root=str(root))
    if not root.exists() or not root.is_dir():
        brief.notes.append(f"repo root does not exist or is not a directory: {root}")
        return brief

    caps: set[str] = set()
    reads_left = max_read_files
    examined = 0
    hit_file_cap = False        # only the ENTRY cap (max_files) stops the walk; read-budget exhaustion
    #                             marks the brief truncated but keeps walking (name-based assets — checkpoints,
    #                             spreadsheets — need NO read, so exhausting reads must not hide later ones).
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored dirs in place (also skips hidden dumping grounds) AND sort the traversal — os.walk
        # yields entries in arbitrary os.scandir order, so which files survive the max_files/max_read_files
        # cap on an over-cap repo would otherwise be filesystem-dependent (non-reproducible brief).
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            examined += 1
            if examined > max_files:
                brief.truncated = True
                hit_file_cap = True
                break
            fp = Path(dirpath) / fn
            try:
                rel = str(fp.relative_to(root))
            except ValueError:
                rel = str(fp)

            if _CKPT_NAME.search(fn):
                # the metric is usually in the filename or its parent dir (checkpoints rarely opened)
                stem_text = f"{fp.parent.name} {fn}"
                metrics = _extract_metrics(stem_text, named_re=named_re)
                for at in _AT_METRIC.findall(stem_text):
                    try:
                        metrics.setdefault("score@", float(at))
                    except ValueError:
                        pass
                detail = _fmt_metrics(metrics) or "(no metric in filename)"
                brief.checkpoints.append(AssetFinding("checkpoint", rel, detail, metrics))
                caps |= _detect_capabilities(fn, lex.capability_patterns)
                continue

            is_readme = bool(_README_NAME.search(fn))
            is_result = bool(_RESULT_NAME.search(fn)) or is_readme
            is_config = bool(_CONFIG_NAME.search(fn))
            if not (is_result or is_config):
                continue

            # xlsx/xls can't be parsed without an optional dependency — note presence, read nothing.
            if fn.lower().endswith((".xlsx", ".xls")):
                brief.results.append(AssetFinding("result-table", rel,
                                                  "(binary spreadsheet — not parsed; open to read)"))
                continue

            if reads_left <= 0:
                # Read budget spent: mark the brief truncated but DON'T stop the walk — later checkpoints /
                # spreadsheets are found by NAME (no read), so exhausting reads must not hide them.
                brief.truncated = True
                continue
            reads_left -= 1
            # CODEX AGENT: [P1] os.walk entries can be symlinks or special files. _read_text follows a
            # file symlink outside repo_root and can block indefinitely on a POSIX FIFO, then forwards the
            # result to the LLM. Resolve-and-contain paths and require a regular non-symlink file first.
            text = _read_text(fp, max_bytes)
            if not text:
                continue
            here_caps = _detect_capabilities(text, lex.capability_patterns)
            caps |= here_caps
            metrics = _extract_metrics(text, named_re=named_re)
            if is_result:
                # result tables carry metric NAME and VALUE in separate columns — merge the delimited-
                # table parse in so `results_last.csv`'s recall column is surfaced, not just tokens.
                for name, val in _extract_table_metrics(text, fam=fam).items():
                    if name not in metrics or val > metrics[name]:
                        metrics[name] = val
                # a generic README is prior-art ONLY when it actually carries a metric or a capability;
                # a purpose-named results/metrics file is always kept (its very name declares intent).
                if is_readme and not (metrics or here_caps):
                    continue
                detail = _fmt_metrics(metrics) or _first_line(text)
                brief.results.append(AssetFinding("result-table", rel, detail, metrics))
            else:  # config / trainer
                caps_here = sorted(here_caps)
                detail = ("capabilities: " + ", ".join(caps_here)) if caps_here else _first_line(text)
                brief.configs.append(AssetFinding("config", rel, detail, metrics))
        if hit_file_cap:
            break

    brief.files_scanned = examined
    brief.capabilities = sorted(caps)
    # Sort the finding lists by path so the WHOLE AssetBrief is order-stable at the source (os.walk order
    # is filesystem-dependent) — any consumer, not just format_brief, then gets a deterministic object.
    brief.results.sort(key=lambda f: f.path)
    brief.configs.sort(key=lambda f: f.path)
    brief.checkpoints.sort(key=lambda f: f.path)
    brief.best_known = _best_known(brief)
    return brief


def _first_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:120]
    return ""


def _fmt_metrics(metrics: dict[str, float]) -> str:
    if not metrics:
        return ""
    items = sorted(metrics.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    return ", ".join(f"{k}={v:g}" for k, v in items)


def _best_known(brief: AssetBrief) -> Optional[dict]:
    """The single strongest on-disk score across results + checkpoints, for the brief's headline
    ('the best result already achieved here is …'). Higher-is-better only: error/loss metrics
    (`_LOWER_IS_BETTER`) are skipped so a max never reports the WORST error as the best. Percent-form
    values (>1 under a name that also appears as a fraction elsewhere would mix) are normalized to a
    fraction for COMPARISON so 91.2% doesn't out-rank a real 0.95; the original value is still reported.
    Heuristic — None when nothing comparable parsed; the agentic path interprets tables directly."""
    best: Optional[dict] = None
    best_cmp = -1.0
    for coll in (brief.results, brief.checkpoints):
        for f in coll:
            for name, val in f.metrics.items():
                low = name.lower()
                if any(s in low for s in _LOWER_IS_BETTER):
                    continue                     # lower-is-better: a max is meaningless, skip the headline
                # normalize a percentage (1 < v <= 100) to a fraction for comparison only — score-family
                # metrics are in [0,1], so a value above 1 is a percent spelling of the same quantity.
                cmp = val / 100.0 if 1.0 < val <= 100.0 else val
                if cmp > best_cmp:
                    best_cmp = cmp
                    best = {"metric": name, "value": val, "source": f.path}
    return best


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def format_brief(brief: AssetBrief, *, max_items: int = 8) -> str:
    """Render the scan into the compact prior-art brief a proposer would read. Deterministic."""
    if not (brief.results or brief.checkpoints or brief.configs or brief.capabilities):
        return ("PRIOR ART & AVAILABLE ASSETS: none found in the task repo "
                f"({brief.files_scanned} files scanned).")
    lines = ["PRIOR ART & AVAILABLE ASSETS (scanned from the task repo — read-only):"]
    if brief.best_known:
        b = brief.best_known
        lines.append(f"- Best on-disk result: {b['metric']}={b['value']:g}  (source: {b['source']}) "
                     "— beat THIS, and note the recipe that produced it.")
    if brief.capabilities:
        lines.append("- Existing infra/capabilities already in the repo: "
                     + ", ".join(brief.capabilities)
                     + "  — reuse these instead of re-deriving them.")
    if brief.checkpoints:
        lines.append("- Sibling checkpoints (metrics from filenames):")
        for f in _ranked(brief.checkpoints, max_items):
            lines.append(f"    · {f.path}  [{f.detail}]")
    if brief.results:
        lines.append("- Result logs / experiment tables:")
        # sort by path before the max_items truncation so WHICH items show is deterministic — os.walk
        # append order is filesystem-dependent, but `format_brief` is documented deterministic.
        for f in sorted(brief.results, key=lambda x: x.path)[:max_items]:
            lines.append(f"    · {f.path}  [{f.detail}]")
    if brief.configs:
        lines.append("- Training configs / trainers:")
        for f in sorted(brief.configs, key=lambda x: x.path)[:max_items]:
            lines.append(f"    · {f.path}  [{f.detail}]")
    if brief.truncated:
        lines.append("- (scan truncated at the file cap — more assets may exist)")
    return "\n".join(lines)


def _ranked(findings: list[AssetFinding], k: int) -> list[AssetFinding]:
    """Checkpoints with a parsed metric first (best value first), then the rest — so the strongest
    sibling model heads the list."""
    def key(f: AssetFinding):
        top = max(f.metrics.values()) if f.metrics else None
        return (top is None, -(top or 0.0), f.path)
    return sorted(findings, key=key)[:k]


# --------------------------------------------------------------------------- #
# Agentic brief (the PRIMARY path) — an LLM explores the repo with read-only tools
# --------------------------------------------------------------------------- #

_ASSET_SYSTEM = (
    "You are a research engineer doing a PRIOR-ART & ASSET sweep of a task repository BEFORE any "
    "experiment is proposed. The single biggest failure mode you are guarding against: a strong recipe "
    "was ALREADY proven in this repo (in a results table, a sibling checkpoint's filename metric, or an "
    "existing trainer capability) and the search never read it. Use the read-only tools (list_dir, "
    "find_files, grep, read_file) to FIND and report, GROUNDED in files you actually read (cite each "
    "path):\n"
    "  1. Result logs / experiment tables (results*, metrics*, scores*, a README results section) and "
    "the BEST score already achieved here — WITH the recipe/config that produced it.\n"
    "  2. Sibling model checkpoints and the metrics carried in their filenames (e.g. `...@0.899`).\n"
    "  3. Existing training infra / configs: which advanced capabilities the trainer ALREADY supports "
    "(alternative losses, data pipelines, hard-negative mining, distillation, …) that a proposal should "
    "REUSE rather than re-derive — determine these from the actual code/config, not assumptions.\n"
    "Then write a COMPACT brief (≤ ~15 lines): the best known result + its recipe, the reusable "
    "capabilities, and the single strongest UNUSED lever the assets imply. Do NOT propose experiments — "
    "report only what already exists on disk."
)


def agentic_asset_brief(repo_root, *, client=None, parser: str = "tool_call",
                        loop_opts: Optional[dict] = None, task_type: Optional[str] = None,
                        seed_scan: bool = True) -> str:
    """The PRIMARY D1 brief: an LLM agent explores the task repo with read-only tools (RepoScoutTools)
    and writes a grounded prior-art & available-assets brief — the agentic realisation of §21.2 (and the
    §21.10 meta-lesson: ground the proposer, don't hardcode the domain). Degrades to the deterministic
    `scan_assets`/`format_brief` when no `client` is wired (offline) or the agentic step yields nothing,
    so the offline suite and no-LLM runs still get a brief. `seed_scan` primes the agent with the cheap
    heuristic scan as a starting point to verify/expand (never as the final answer)."""
    if client is None:
        return format_brief(scan_assets(repo_root, task_type=task_type))
    from looplab.agents.agent import agentic_text
    from looplab.tools.reposcout import RepoScoutTools

    root = str(Path(repo_root))
    fallback = format_brief(scan_assets(repo_root, task_type=task_type))
    try:
        tools = RepoScoutTools(roots=[root], default_root=root)
    except Exception:  # noqa: BLE001 — can't build the scout (bad root) => deterministic brief
        return fallback
    user = (f"Repo root: {root}\nSweep this repository and write the prior-art & available-assets brief.")
    if seed_scan:
        user += ("\n\nA quick heuristic pre-scan is below — VERIFY and EXPAND it against the real files "
                 "(it may miss assets or mislabel them; it is only a starting point):\n" + fallback)
    msgs = [{"role": "system", "content": _ASSET_SYSTEM}, {"role": "user", "content": user}]
    out = agentic_text(client, tools, msgs, loop_opts=loop_opts or {"max_turns": 20},
                       answer_desc="the prior-art & available-assets brief",
                       fallback=lambda m: fallback)
    return (out or "").strip() or fallback


def asset_brief(repo_root, *, client=None, task_type: Optional[str] = None, **kwargs) -> str:
    """Convenience: return the prior-art brief for `repo_root`. Uses the agentic path when a `client` is
    given (the primary, grounded route), else the deterministic offline scan."""
    if client is not None:
        return agentic_asset_brief(repo_root, client=client, task_type=task_type,
                                   loop_opts=kwargs.get("loop_opts"),
                                   parser=kwargs.get("parser", "tool_call"))
    scan_kwargs = {k: v for k, v in kwargs.items()
                   if k in ("max_files", "max_read_files", "max_bytes", "lexicon")}
    return format_brief(scan_assets(repo_root, task_type=task_type, **scan_kwargs))
