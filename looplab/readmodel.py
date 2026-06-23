"""Rebuildable SQLite read-model (I1, ADR-17). Derived projection for UI/queries;
never a source of truth — always reconstructable from events.jsonl.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Event, RunState
from .replay import fold


def _tri(v) -> int | None:
    """Tri-state -> nullable SQLite int: None (no audit) / 0 / 1."""
    return None if v is None else (1 if v else 0)


def build_readmodel(events: Iterable[Event], db_path: str | os.PathLike) -> RunState:
    st = fold(events)
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    try:
        cur = con.cursor()
        cur.execute("DROP TABLE IF EXISTS nodes")
        cur.execute(
            "CREATE TABLE nodes("
            "id INTEGER PRIMARY KEY, parent_ids TEXT, operator TEXT, "
            "metric REAL, status TEXT, is_best INTEGER, "
            "agent_ok INTEGER, agent_fell_back INTEGER)"  # external-agent audit (ADR-7)
        )
        for n in sorted(st.nodes.values(), key=lambda n: n.id):
            rep = n.agent_report or {}
            cur.execute(
                "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)",
                (
                    n.id,
                    ",".join(map(str, n.parent_ids)),
                    n.operator,
                    n.metric,
                    n.status.value,
                    1 if n.id == st.best_node_id else 0,
                    _tri(rep.get("ok")),
                    _tri(rep.get("fell_back")),
                ),
            )
        con.commit()
    finally:
        con.close()
    return st
