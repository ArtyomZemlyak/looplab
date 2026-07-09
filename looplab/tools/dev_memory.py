"""Developer memory — a SEPARATE cross-run store of IMPLEMENTATION lessons, distinct from the
Researcher's `lessons.jsonl`.

Where the Researcher's lessons are about WHICH experiment to run (a larger batch helped, polynomial
features overfit), the Developer's are about HOW to realise one in code on THIS kind of repo: dataset
loading gotchas, framework/version API traps, build/train pitfalls, "orchestrate the repo's own train
script rather than reimplementing the loader". They live at `<memory_dir>/dev_lessons.jsonl` and are:

  1. SELF-AUTHORED by the Developer mid-session via `remember_dev_lesson` (the "приколюхи для
     разработчика пиши в его память" channel);
  2. pulled on demand via `search_dev_lessons` / `list_dev_lessons`;
  3. previewed (top-5, one line each) up front in the Developer prompt (see `dev_lesson_preview`);
  4. topped up by a small run-end engine distillation (see `LessonMemory.write_dev_lessons`).

The read/write tools mirror `tools/memory_tools.py` (Researcher lessons) and `tools/knowledge_tools.py`
(the `remember` write). The store schema mirrors `lessons.jsonl` so it reuses the SAME pure helpers in
`engine/memory.py` (`consolidate_lessons`, `fingerprint_similarity`) — one lesson shape everywhere.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import orjson

from looplab.tools._base import fn_spec

_WORD = re.compile(r"[a-z0-9@._]+")
DEV_LESSONS_FILE = "dev_lessons.jsonl"


def _toks(s: str) -> set:
    return {w for w in _WORD.findall((s or "").lower()) if len(w) > 2}


def _load(path: Path) -> list[dict]:
    """Read a dev_lessons.jsonl into a list of dicts (skips blank/corrupt lines) — never raises."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = orjson.loads(line)
        except Exception:  # noqa: BLE001 — a corrupt line must not poison the read
            continue
        if isinstance(d, dict) and d.get("statement"):
            out.append(d)
    return out


def append_dev_lessons(memory_dir, lessons: list[dict], *, consolidate: bool = True) -> int:
    """Append IMPLEMENTATION lessons to the shared `<memory_dir>/dev_lessons.jsonl`, under the SAME
    best-effort interprocess lock the event store uses (a concurrent run's write between our read and
    rewrite would otherwise be clobbered). `consolidate` (default on) runs the SAME lexical D2 hygiene
    the Researcher store uses — merge duplicate claims into an `evidence_count`, newest verdict wins —
    so repeated self-authored notes don't pile up. Returns how many rows were handed in (0 = no-op)."""
    if not (lessons and memory_dir):
        return 0
    from looplab.events.eventstore import _interprocess_lock
    base = Path(memory_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / DEV_LESSONS_FILE
    with _interprocess_lock(Path(str(path) + ".lock")):
        if consolidate:
            from looplab.engine.memory import consolidate_lessons
            from looplab.core.atomicio import atomic_write_text
            merged = consolidate_lessons(_load(path) + list(lessons))   # lexical group+sum, no LLM
            # ATOMIC whole-file rewrite (temp + os.replace), like the researcher store's hygiene pass
            # (engine/lessons.py) — a plain write_text truncates first, so a SIGKILL mid-write (budget/
            # OOM/sandbox teardown) between truncate and flush would lose the WHOLE accumulated store.
            atomic_write_text(str(path), "".join(orjson.dumps(lz).decode() + "\n" for lz in merged))
        else:
            with open(path, "a", encoding="utf-8") as f:                # append is already crash-safe
                f.write("".join(orjson.dumps(lz).decode() + "\n" for lz in lessons))
    return len(lessons)


def dev_lesson_preview(memory_dir, task_id: str = "", fingerprint: Optional[list] = None,
                       *, k: int = 5, width: int = 100) -> str:
    """A compact top-`k` PREVIEW of the Developer-memory lessons most relevant to the current task —
    one truncated line each — for up-front injection (the full text is pulled on demand via
    `search_dev_lessons`, so this stays small). Ranks exact-task lessons first (sim 1.0), then those
    whose task FINGERPRINT is similar (Jaccard ≥ 0.34), by corroboration then recency. Empty string
    when off/absent/no match — so the caller can splice it unconditionally."""
    if not memory_dir:
        return ""
    rows = _load(Path(memory_dir) / DEV_LESSONS_FILE)
    if not rows:
        return ""
    fp = [t for t in (fingerprint or []) if not str(t).startswith("param:")]
    from looplab.engine.memory import fingerprint_similarity
    scored: list[tuple[float, int, dict]] = []
    for idx, o in enumerate(rows):
        stored = o.get("fingerprint")
        stored = [t for t in stored if not str(t).startswith("param:")] if isinstance(stored, list) else []
        sim = 1.0 if (task_id and o.get("task_id") == task_id) else fingerprint_similarity(fp, stored)
        if sim >= 0.34:                           # exact task (sim 1.0) or a fingerprint-similar one
            scored.append((sim, idx, o))
    if not scored:
        return ""
    # rank: similarity, then corroboration (evidence_count), then recency (later line wins)
    scored.sort(key=lambda t: (t[0], int(t[2].get("evidence_count") or len(t[2].get("evidence") or []) or 1),
                               t[1]), reverse=True)
    seen: set[str] = set()
    lines: list[str] = []
    for _sim, _idx, o in scored:
        stmt = " ".join(str(o.get("statement", "")).split())
        key = stmt.lower()[:80]
        if not stmt or key in seen:
            continue
        seen.add(key)
        outcome = o.get("outcome") or ""
        tag = f" [{outcome}]" if outcome else ""
        lines.append(f"  • {stmt[:width]}{tag}")
        if len(lines) >= k:
            break
    if not lines:
        return ""
    # Only `search_dev_lessons` is named here: the preview rides the SHARED system prompt (all phases),
    # but the `remember_dev_lesson` WRITE tool is bound only in the implement/repair phases — its own
    # description advertises it there, so naming it here would point the read-only phases at an unbound tool.
    return ("\n\nDEVELOPER MEMORY — implementation lessons from past runs (preview; "
            "`search_dev_lessons` reads more):\n" + "\n".join(lines))


class DevMemoryTools:
    """READ side: `search_dev_lessons` + `list_dev_lessons` over `<memory_dir>/dev_lessons.jsonl`.
    Same provider shape (`.specs()`/`.execute()`) as `MemoryTools`; every execute returns a string
    and soft-fails so a junk tool call never crashes a Developer session."""

    def __init__(self, memory_dir: str | None):
        self.dir = Path(memory_dir) if memory_dir else None

    def specs(self) -> list[dict]:
        if not self.dir:
            return []
        return [
            fn_spec("search_dev_lessons",
                "Search the DEVELOPER-memory ledger — IMPLEMENTATION lessons from past runs (how to "
                "realise things in code on this kind of repo: dataset-loading gotchas, framework/version "
                "API traps, build/train pitfalls, what orchestration worked). Use it before writing code "
                "to reuse a known-good approach and avoid a known trap.",
                {"query": {"type": "string", "description": "What to find lessons about (e.g. 'dataset "
                           "loader', 'lightning precision', 'checkpoint path')."},
                 "limit": {"type": "integer", "description": "Max lessons (default 6)."}},
                ["query"]),
            fn_spec("list_dev_lessons",
                "List the most recent DEVELOPER-memory implementation lessons (no query). Use to skim "
                "what past runs learned about building on this kind of repo.",
                {"limit": {"type": "integer", "description": "Max lessons (default 8)."}}),
        ]

    def execute(self, name: str, args: dict) -> str:
        if not self.dir:
            return "(no developer memory configured)"
        args = args or {}
        rows = _load(self.dir / DEV_LESSONS_FILE)
        if not rows:
            return "(no developer lessons yet)"
        if name == "list_dev_lessons":
            lim = max(1, int(args.get("limit") or 8))
            return "\n".join(self._fmt(o) for o in rows[-lim:][::-1])
        if name == "search_dev_lessons":
            lim = max(1, int(args.get("limit") or 6))
            qt = _toks(str(args.get("query") or ""))
            ranked = sorted(rows, key=lambda o: len(qt & _toks(o.get("statement", ""))), reverse=True)
            hits = [o for o in ranked if (not qt) or (qt & _toks(o.get("statement", "")))][:lim]
            return "\n".join(self._fmt(o) for o in hits) if hits else "(no matching dev lessons yet)"
        return f"(unknown tool: {name})"

    @staticmethod
    def _fmt(o: dict) -> str:
        n = int(o.get("evidence_count") or len(o.get("evidence") or []) or 1)
        conf = f", conf {o['confidence']:.2f}" if isinstance(o.get("confidence"), (int, float)) else ""
        outcome = o.get("outcome") or "note"
        return (f"[{outcome}] {o.get('statement', '')} "
                f"(seen {n} time{'s' if n != 1 else ''}{conf})")


class DevMemoryWriteTools:
    """WRITE side: `remember_dev_lesson` lets the Developer SELF-AUTHOR an implementation gotcha/technique
    mid-session — the "write your dev tricks to dev memory" channel. Writes to
    `<memory_dir>/dev_lessons.jsonl` (a scoped memory file, NOT the repo and NOT a domain event — same
    safe class as `KnowledgeWriteTools.remember`), stamped with the current task/run so a later run can
    retrieve it by fingerprint. Construct per Developer session with the live run's identifiers."""

    def __init__(self, memory_dir: str | None, *, task_id: str = "", run_id: str = "",
                 fingerprint: Optional[list] = None, kind: str = ""):
        self.dir = Path(memory_dir) if memory_dir else None
        self.task_id = task_id
        self.run_id = run_id
        self.fingerprint = list(fingerprint or [])
        self.kind = kind

    def specs(self) -> list[dict]:
        if not self.dir:
            return []
        return [
            fn_spec("remember_dev_lesson",
                "Save an IMPLEMENTATION lesson to DEVELOPER memory so future runs (this task or a "
                "similar one) reuse it: a concrete, GENERALIZABLE coding gotcha or technique you just "
                "learned — a dataset-loading trap, a framework/version API quirk, a build/train pitfall, "
                "an orchestration that worked. Write ONE transferable sentence (not this run's exact "
                "numbers). Skip trivia; save what would have saved you time.",
                {"statement": {"type": "string", "description": "One generalizable implementation lesson."},
                 "outcome": {"type": "string", "description": "'technique' (do this) or 'pitfall' (avoid "
                             "this). Default 'technique'.",
                             "enum": ["technique", "pitfall"]}},
                ["statement"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        if not self.dir:
            return "(no developer memory configured)"
        if name != "remember_dev_lesson":
            return f"(unknown tool: {name})"
        args = args or {}
        stmt = " ".join(str(args.get("statement") or "").split())
        if len(stmt) < 8:
            return "(refused: give a concrete, generalizable implementation lesson — at least a sentence)"
        outcome = str(args.get("outcome") or "technique").strip().lower()
        if outcome not in ("technique", "pitfall"):
            outcome = "technique"
        lesson = {"task_id": self.task_id, "fingerprint": self.fingerprint, "kind": self.kind,
                  "statement": stmt[:400], "outcome": outcome, "confidence": 0.7,
                  "run_id": self.run_id, "evidence": [], "source": "developer"}
        try:
            append_dev_lessons(str(self.dir), [lesson])
        except Exception as e:  # noqa: BLE001 — a memory write must never crash the session
            return f"(could not save the lesson: {e})"
        return f"saved a developer lesson [{outcome}]: {stmt[:120]}"
