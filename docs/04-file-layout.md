# LoopLab — File Layer Structure (on-disk design)

**Version:** 0.1 · **Date:** 2026-06-20
**Companion docs:** [01-product-design.md](01-product-design.md) · [02-architecture.md](02-architecture.md) · [03-decisions.md](03-decisions.md) (ADR-1 UI / ADR-4 tracking) · [05-build-decisions.md](05-build-decisions.md) (concrete libs) · research basis: [autoresearch-systems-exploration.md](autoresearch-systems-exploration.md)

> **⚠ Design vs. shipped (2026-07):** this is the original on-disk *design*. The implementation deliberately simplified it: a run dir holds `events.jsonl` (at the run root, not `_logs/`), `config.snapshot.json` + `task.snapshot.json` (not `config.yaml`/`manifest.json`), `engine.lock`, per-node workdirs, and derived projections (`readmodel.sqlite`, `trace.json`, `tree.html`). There is no content-addressed `store/objects/` and no `commands.jsonl` (control intents are events in `events.jsonl`). The current contract is documented in [guide/concepts.md](guide/concepts.md).
>
> This spec answers: **how do we lay out files on disk** when one run produces user *clicks*, learning *artifacts* (weights/data), training *logs*, final *metrics*, and generated *docs* (.md) — and all of it must be **human-readable** (except clicks) *and* **UI-readable** (the UI may keep its own viz formats). It supersedes the short `run_dir/` sketch in [ADR-1](03-decisions.md).

---

## 1. The governing principle — store once (human form), derive the rest

**One canonical, human-readable source of truth on disk; everything the UI wants for fast rendering is a *projection* that can be deleted and rebuilt from that source.** This is event-sourcing + CQRS applied to a directory.

- **Derive, don't dual-write.** Writing the same fact to two stores has no cross-system atomicity → silent divergence (the "dual-write problem"). Instead: one atomic write to the canonical file; the UI's SQLite/Parquet/search index is rebuilt from it.
- **Projections are disposable.** A corrupted index is fixed by `rm -rf _derived/ && rebuild`, never hand-repaired. This sidesteps cache-invalidation (key each projection entry on the **content hash** of its source, à la Docker — not mtime).
- **The UI may read canonical files and write only into `_derived/`.** It must never mutate canonical files. (Mirrors ADR-1's single-writer rule: the engine is the sole writer of run state; the UI appends intents only.)

This directly satisfies your requirement: **humans get plain text everywhere that matters; the UI builds its own formats in a throwaway cache.**

---

## 2. The five data classes → format decisions

Your five concerns map to distinct file classes. Labels used throughout:
**[HC]** human-readable-canonical · **[MA]** machine-append-only · **[BIN]** large-binary-artifact · **[DUI]** derived-UI-projection (regenerable, gitignored).

| Your concern | Class | Format | Why (evidence) |
|--------------|-------|--------|----------------|
| **User clicks / commands** | [MA] | append-only **JSONL** (`commands.jsonl`) | Machine record of *intent*, not a doc — replayable/auditable; corrections are new lines, never edits. **Explicitly NOT human-curated** (your requirement). |
| **Event/flow trace** | [MA] | append-only **JSONL** (`events.jsonl`) | Source of truth for the run timeline; streamable (`tail -f`/`jq`), git-diffable, one-line-per-event. Add a SQLite/WAL projection only when `grep` stops scaling. |
| **Training logs (stdout/stderr)** | [MA] | **structured JSONL** (pretty console in dev) | Typed per-line fields (`step`,`loss`,`lr`,`ts`) let the UI filter/plot instead of regex-scraping. Same stream, two renderers (structlog). |
| **Metrics — per-step time series** | [MA] | **CSV** (fixed cols) or **JSONL** (sparse); derive **Parquet+DuckDB** | Text canonical stays diffable + crash-safe on the append hot-path; columnar derived store gives ~5× size / ~7–10× query speed for the UI. This is exactly what Lightning/Keras (text) + MLflow/W&B (binary under UI) do. |
| **Metrics — final/summary scalars** | [HC] | **JSON** (`summary.json`) | A few scalars: strict, universal, zero-ambiguity. *Avoid YAML here* — its type coercion corrupts numeric results (`1.70`→`1.7`, the "Norway problem"). |
| **Config / hyperparameters** | [HC] | **YAML** (`config.yaml`), loaded by **`pydantic-settings`** | Readable + comments; one typed `BaseSettings` model validates it and layers `.env`/env/CLI on top (ADR-11). Quote SHAs/strings defensively under PyYAML. |
| **Generated experiment docs** | [HC] | **Markdown + YAML frontmatter** (`report.md`, `README.md`) | Parses losslessly for *both* audiences: humans read prose; UI extracts `{metadata, content}` (gray-matter / python-frontmatter) and a deterministic CommonMark AST. Structured data in frontmatter; metrics as GFM tables; **figures/artifacts by relative path, never inlined bytes**. |
| **Model weights** | [BIN] | **safetensors** (never pickle) | safetensors removes pickle's arbitrary-code-exec risk and gives zero-copy/mmap load; it carries an embedded JSON header (dtype/shape/offsets) so even the binary is introspectable. |
| **Datasets / large outputs** | [BIN] | raw, in a **content-addressed store** | Kept *out* of git and the human tree (see §4). |

---

## 3. Recommended layout — ONE experiment run

> **Not shipped as designed** — see the banner at the top; the shipped run-dir contract is [guide/concepts.md](guide/concepts.md).

```
runs/2026-06-20T142530__resnet50-lr3e4__a1b2c3/
├── README.md                  [HC]  hypothesis + "how to reproduce"; frontmatter mirrors key metrics  (write FIRST)
├── report.md                  [HC]  curated results narrative + inline key plots (by path)
├── config.yaml                [HC]  full hyperparams / run config (canonical input)
├── summary.json               [HC]  final scalar metrics (the handful that matter)
├── manifest.json              [HC]* machine-generated index: every artifact as {path, sha256, size, role};
│                                    carries apiVersion + kind; integrity check + restore plan; NEVER hand-edit
├── metrics.csv                [MA]  per-step metrics (fixed columns)  — or metrics.jsonl if sparse
├── state.json                 [HC]  current run status (atomic rewrite; the engine's live state snapshot)
├── desired_state.json         [HC]  control plane — UI writes intent here; engine reconciles toward it (ADR-1)
├── _logs/                            (gitignored; each file carries a "DO NOT EDIT" header)
│   ├── events.jsonl           [MA]  append-only event/flow log (run timeline = source of truth)
│   ├── commands.jsonl         [MA]  machine-only user clicks / UI→engine intents  ← your "clicks"
│   └── train.jsonl            [MA]  structured training stdout/stderr
├── artifacts/                       (bytes tracked out-of-band; see §4)
│   ├── model.safetensors          [BIN] weights
│   ├── model.safetensors.meta.yaml[HC]  sidecar: provenance + sha256 of the primary
│   └── data/                      [BIN] datasets / large outputs
├── .tmp/                            atomic-write staging (write here → rename into place); gitignored
└── _derived/                        rebuildable UI cache; `rm -rf`-safe; gitignored
    ├── summary.json               [DUI] denormalized card/list view
    ├── metrics.parquet            [DUI] columnar metrics for fast UI charts (DuckDB-queryable)
    ├── thumbnails/                [DUI] plot/preview images (UI's own viz formats live here)
    └── search-index.json          [DUI] UI search/filter index
```
\* `manifest.json` is canonical (committed, integrity-bearing) but machine-generated — give it the `DO NOT EDIT` marker.

**Run-dir naming:** `<ISO8601-timestamp>__<self-identifying-slug>__<shorthash>` → sorts chronologically in any file browser, is self-describing, and collision-safe under concurrency. Keep the opaque tracker/MLflow run-id *inside* `README.md`, not in the path. (Synthesis of Hydra timestamps + W&B id + a `latest` symlink convenience.)

---

## 4. Large binary artifacts — content-addressed store + pointer manifest

> **Not shipped as designed** — see the banner at the top; the shipped run-dir contract is [guide/concepts.md](guide/concepts.md).

Keep big binaries **out of git and out of the human-readable tree**:
- **Content-addressed store**: blobs named by **SHA-256**, sharded by 2-char prefix (`store/objects/4d/7a2146…`) → automatic dedup + immutability (Git/git-lfs/DVC pattern).
- **Materialize into the run** via **reflink → hardlink → symlink → copy** fallback (DVC `cache.type`). ⚠️ Windows without Developer Mode loses symlink dedup (falls back to copy) — plan for it (you're on Windows 11).
- **Reference from a small, diffable pointer** in the human tree (and from `manifest.json`):
```yaml
# artifacts/model.safetensors.meta.yaml  (sidecar — appended extension form)
artifact: model.safetensors
oid: sha256:4d7a2146...e2393     # content address (also embedded in manifest.json)
size: 4831838208
format: safetensors
metadata: { arch: resnet50, params: 25M, dtype: bf16 }
```
This makes weights/data reviewable in `git diff` (the pointer) without dragging the binary into git. It also aligns with the MLflow Tracker ([ADR-4](03-decisions.md)): MLflow's artifact store is the same idea; we can point both at one CAS.

---

## 5. Recommended layout — overall PROJECT

> **Not shipped as designed** — see the banner at the top; the shipped run-dir contract is [guide/concepts.md](guide/concepts.md).

```
project/
├── README.md                  [HC]  project landing page (purpose, how to run the engine)
├── conf/                      [HC]  config.yaml + profiles, loaded by pydantic-settings (YAML<.env<env<CLI); secrets are env/SecretStr refs, never values (ADR-11)
├── prompts/                   [HC]  prompts-as-files, MD+YAML frontmatter, one per role×operator (ADR-8)
│   ├── researcher/propose.md
│   └── developer/{draft,debug,improve,ablate,ensemble}.md
├── skills/                    [HC]  Agent Skills (SKILL.md + scripts) — ML recipes; migrated seed-knowledge (ADR-9)
│   └── <skill>/SKILL.md
├── knowledge/                       unified knowledge/memory (ADR-10) — markdown canonical, index derived
│   ├── seed|tasks|experiments|lessons/*.md   [HC]
│   └── index/                 [DUI] vector index — pluggable VectorStore, LanceDB default (+ optional [[wikilinks]] graph); gitignored, rebuildable (ADR-16)
├── schemas/                   [HC]  versioned JSON Schema per file-kind ($id/$schema/$ref/$defs)
│   ├── event.v1.schema.json · manifest.v1.schema.json · config.v1.schema.json
├── runs/                            one self-describing dir per run (§3); each run also gets a generated AGENTS.md in its agent worktree (ADR-8)
├── store/objects/             [BIN] content-addressed blob store (sha256, 2-char sharded)
├── mlflow.db                        MLflow tracking (sqlite; ADR-4) — OPTIONAL exporter over events.jsonl, not core ([ADR-6](03-decisions.md))
├── index.json                 [DUI] cross-run leaderboard/index (rebuilt from each run's manifest)
├── _derived/                  [DUI] project-wide UI cache (gitignored, regenerable)
├── rebuild  (script)          [HC]  idempotent: wipe _derived/ + index.json + knowledge/index, re-derive
└── .gitignore                       ignores _logs/ _derived/ .tmp/ store/ knowledge/index/ ; keeps *.md, *.yaml, schemas/, prompts/, skills/
```

---

## 6. Hard rules (invariants for the file layer)

1. **Atomic writes (the Maildir guarantee).** Write to `.tmp/` then `rename()` into place; `fsync` temp before rename and the parent dir after; keep temp + final on one filesystem. The concurrently-reading UI must **never** see a torn file.
2. **Canonical files are append-only or human-authored; never machine-mutated in place.** Corrections are new appended lines/events.
3. **Structural human/machine split, enforced by path + gitignore.** Curated docs (`*.md`, `config.yaml`) at run-dir root; append-only streams in `_logs/`; derived in `_derived/`. Machine-generated files carry a `DO NOT EDIT` header.
4. **Clicks are data, not docs.** `commands.jsonl` is append-only machine intent — never expected to be human-readable/curated (directly per your requirement).
5. **Version every file.** Embed `apiVersion`/`kind` (or `"v"`) + ship a JSON Schema per kind; the UI **upcasts old versions on read** and reads tolerantly (ignore unknown fields). For JSONL, repeat the version per record so streams are self-contained.
6. **Reference big bytes by hash+path, never inline.** Manifest/sidecar/Markdown all point to artifacts; bytes live in the CAS.
7. **Derived = rebuildable.** Anything under `_derived/` or `index.json` must be regenerable by `rebuild` from canonical files alone; it is gitignored and `rm -rf`-safe.

---

## 7. How this satisfies your two-audience requirement

| Requirement | How it's met |
|-------------|--------------|
| **Human-readable files (everything except clicks)** | `README.md`/`report.md` (Markdown), `config.yaml`, `summary.json`, `metrics.csv`, structured-but-readable `events.jsonl`/`train.jsonl`, and small YAML/JSON pointers for artifacts. All open/grep/diff cleanly. |
| **Clicks NOT human-curated** | `_logs/commands.jsonl` — append-only machine intent log, separated structurally, marked DO-NOT-EDIT. |
| **UI-readable, with its own viz formats** | UI reads the canonical files and builds **its own** `_derived/` projections (Parquet for charts, thumbnails, search index, denormalized summaries) — disposable and rebuildable, never coupled to the engine. |
| **Big artifacts (weights/data)** | safetensors in a content-addressed `store/`, referenced by hash from manifest + sidecar; kept out of the human/git tree. |
| **Reproducibility** | MLflow `mlflow.db` (ADR-4) + git commit + `config.yaml` + seeds + `manifest.json` integrity hashes ⇒ any run reproducible/branchable. |

---

## 8. Changes to fold back into the other docs

- **[02-architecture.md](02-architecture.md) §3.11 / §4 / §14:** the file contract here replaces the short `run_dir/` block; storage stack = JSONL canonical + CAS `store/` + MLflow `mlflow.db` + `_derived/` projections.
- **[03-decisions.md](03-decisions.md) ADR-1:** event/command schema unchanged; this doc adds the *full* class taxonomy (artifacts, metrics, docs) and the canonical-vs-derived rule.
- **New invariants** (atomic writes, version-every-file, clicks-are-data, derive-don't-dual-write) join the architecture's rules list.
