"""The UPSTREAM BOARD: what the run's verified base carries and what is on its way there (doc 73 §2.3,
track 3).

WHY (2026-10-07). The upstream lane (doc 72) promotes a champion's general CAPABILITY into the base
the next lifecycles are seeded from, behind a named flag whose default keeps the old behaviour. The
Researcher was told (`agents/state_brief.py`, the "Active verified capability base" lines); the
Developer — the role that writes the code which can switch the flag on, and the role that repairs a
failure a promoted fix already cured — was told nothing, and the operator had no one-screen answer
to "what is in the base, and what is being checked for it". The fold already holds everything
(`RunState.upstream_history`, `RunState.upstream_base`); this module is the one reading of it.

PURE, total over junk rows (the history is `stored_whole` payloads), no I/O. Two readers:
`adapters/repo_developer.py::LLMRepoDeveloper._upstream_base_note` (under
`Settings.upstream_board_brief`, through the state the engine already binds — no new channel) and
`looplab inspect`.
"""
from __future__ import annotations

from typing import Optional

# Proposal lifecycle, keyed on the LAST upstream row a proposal id carries.
_STATUS_BY_TYPE = {
    "upstream_proposal_started": "proposing",
    "upstream_proposed": "proposed",
    "upstream_proposal_failed": "proposal_failed",
    "upstream_gate_started": "checking",
    "upstream_gate_abandoned": "abandoned",
    "base_advanced": "advanced",
}
# What still waits on someone: a proposal being written, one awaiting its check, one being checked,
# and one whose check PASSED and awaits the explicit `advance`.
IN_FLIGHT_STATUSES = frozenset({"proposing", "proposed", "checking", "passed"})

DEVELOPER_NOTE_MAX_PROMOTIONS = 5
_SUMMARY_CAP = 300


def _text(value, cap: int) -> str:
    return " ".join(str(value).split())[:cap] if isinstance(value, str) else ""


def _node_id(value) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None


def _flag(value) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name.isidentifier():
        return None
    return {"name": name[:128], "default": _text(value.get("default"), 128),
            "enabled": _text(value.get("enabled"), 128)}


def upstream_board(state) -> dict:
    """`{"base": {...}|None, "advanced": [...], "proposals": [...], "in_flight": [...]}`.

    `proposals` holds one row per proposal id in first-seen order — its `status`, the source
    experiment, summary and flag it was proposed with. A proposal whose base was superseded by a
    LATER promotion of another proposal is `superseded`, not in flight: its check (or its advance)
    would be refused against the base it no longer extends."""
    history = getattr(state, "upstream_history", None) or []
    rows: dict[str, dict] = {}
    advanced: list[dict] = []
    for row in history:
        if not isinstance(row, dict):
            continue
        kind, pid = row.get("type"), row.get("proposal_id")
        if kind not in _STATUS_BY_TYPE and kind != "upstream_gate_finished":
            continue
        if not isinstance(pid, str) or not pid:
            continue
        entry = rows.setdefault(pid, {"proposal_id": pid[:80], "status": "proposing",
                                      "source_node_id": None, "summary": "", "flag": None,
                                      "seq": row.get("seq")})
        if kind == "upstream_gate_finished":
            result = row.get("result")
            passed = isinstance(result, dict) and result.get("passed") is True
            entry["status"] = "passed" if passed else "check_failed"
            code = result.get("code") if isinstance(result, dict) else None
            if not passed and isinstance(code, str):
                entry["code"] = code[:80]
        else:
            entry["status"] = _STATUS_BY_TYPE[kind]
            if kind == "upstream_proposal_failed" and isinstance(row.get("code"), str):
                entry["code"] = row["code"][:80]
        if _node_id(row.get("source_node_id")) is not None:
            entry["source_node_id"] = row["source_node_id"]
        if _text(row.get("summary"), _SUMMARY_CAP):
            entry["summary"] = _text(row.get("summary"), _SUMMARY_CAP)
        if _flag(row.get("flag")) is not None:
            entry["flag"] = _flag(row.get("flag"))
        if kind == "base_advanced":
            advanced.append({"proposal_id": pid[:80], "seq": row.get("seq"),
                             "source_node_id": _node_id(row.get("source_node_id")),
                             "summary": _text(row.get("summary"), _SUMMARY_CAP),
                             "flag": _flag(row.get("flag"))})
    base = getattr(state, "upstream_base", None)
    # THE FOLD KEEPS ONLY THE LAST 200 upstream rows (`replay_journals.py::_on_upstream`), and every
    # per-probe `upstream_execution` counts, so a promotion can fall out of the window while the base
    # still carries it (critic 2026-10-08). `upstream_base` is never trimmed: the latest promotion is
    # read from it when the window no longer holds its row.
    if isinstance(base, dict) and not any(a["seq"] == base.get("seq") for a in advanced):
        pid = base.get("proposal_id")
        advanced.append({"proposal_id": pid[:80] if isinstance(pid, str) else "",
                         "seq": base.get("seq"), "source_node_id": _node_id(base.get("source_node_id")),
                         "summary": _text(base.get("summary"), _SUMMARY_CAP),
                         "flag": _flag(base.get("flag"))})
        advanced.sort(key=lambda a: a["seq"] if type(a["seq"]) is int else -1)
    last_promotion = max((a["seq"] for a in advanced if type(a["seq"]) is int), default=None)
    for entry in rows.values():
        if (entry["status"] in IN_FLIGHT_STATUSES and last_promotion is not None
                and type(entry.get("seq")) is int and entry["seq"] < last_promotion):
            entry["status"] = "superseded"
    base_view = None
    if isinstance(base, dict):
        selector = base.get("selector")
        digest = selector.get("digest") if isinstance(selector, dict) else None
        base_view = {"digest": digest[:16] if isinstance(digest, str) else None,
                     "source_node_id": _node_id(base.get("source_node_id")),
                     "summary": _text(base.get("summary"), _SUMMARY_CAP),
                     "flag": _flag(base.get("flag"))}
    proposals = list(rows.values())
    return {"base": base_view, "advanced": advanced, "proposals": proposals,
            "in_flight": [p for p in proposals if p["status"] in IN_FLIGHT_STATUSES]}


def _flag_phrase(flag: Optional[dict]) -> str:
    if not flag:
        return "no flag recorded"
    return f"flag `{flag['name']}` (default `{flag['default']}`, enabled `{flag['enabled']}`)"


def developer_base_note(state) -> str:
    """The Developer's paragraph, or "" when nothing was ever promoted (every run without the
    upstream lane renders its historical bytes). States only PROMOTIONS — what is in the code a
    lifecycle is seeded with — never a proposal still being checked, which is not in that code."""
    promotions = upstream_board(state)["advanced"][-DEVELOPER_NOTE_MAX_PROMOTIONS:]
    if not promotions:
        return ""
    lines = []
    for p in promotions:
        origin = (f"from experiment #{p['source_node_id']}" if p["source_node_id"] is not None
                  else "from an earlier experiment")
        lines.append(f"- {_flag_phrase(p['flag'])} {origin}: {p['summary'] or '(no summary)'}")
    return ("\n\nVERIFIED CAPABILITIES IN THIS RUN'S BASE (promoted from earlier experiments after "
            "measured equivalence and regression checks; a lifecycle that started after a promotion "
            "is seeded with it, an earlier one and its repairs keep the base they started on):\n"
            + "\n".join(lines)
            + "\nWhen this node needs one of them, switch its flag on instead of re-implementing it; "
              "a failure one of them fixed is fixed in that base, so build on it rather than around "
              "it.")


def board_lines(state) -> list[str]:
    """The operator's lines (`looplab inspect`); [] when the run never used the upstream lane."""
    board = upstream_board(state)
    if not board["base"] and not board["proposals"]:
        return []
    out = []
    base = board["base"]
    if base:
        out.append(f"upstream base: {base['digest'] or '?'} from #{base['source_node_id']}"
                   f" — {base['summary'][:120]}")
    else:
        out.append("upstream base: the task's seed (nothing promoted)")
    for p in board["in_flight"]:
        src = f"#{p['source_node_id']}" if p["source_node_id"] is not None else "?"
        out.append(f"  in flight: {p['status']} {p['proposal_id']} from {src} — {p['summary'][:100]}")
    done = [p for p in board["proposals"] if p["status"] not in IN_FLIGHT_STATUSES]
    if done:
        counts: dict[str, int] = {}
        for p in done:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        out.append("  settled: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return out
