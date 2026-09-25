"""What a node actually BUILT, as its Developer reported it on `done`.

Measured 2026-09-25 on MiniOneRec inf12: three nodes recorded under "de-duplicate the first decode
step" (13, 14, 17) contained no de-duplication at all — two a one-flag revert, one a fused MLP —
and nothing said so. Each got a metric, its Card read VERDICT=tested, the novelty gate rejected the
idea four more times as "already tried", and the Researcher, reading the code, re-proposed it. An
activation marker cannot catch this: a substitute path prints its own markers and passes. So the
Developer's `done` states whether it built the idea it was given, and what it built instead.
"""
from __future__ import annotations

import json
from typing import Optional

IDEA_REPORT_NAME = "looplab_idea_report.json"
IDEA_IMPLEMENTED = ("as_proposed", "partly", "different", "not_implemented")
_BUILT_INSTEAD_CHARS = 400


def idea_report_text(args) -> Optional[str]:
    """The report a `done` declared, as file text, or None when it declared nothing usable."""
    if not isinstance(args, dict) or args.get("idea_implemented") not in IDEA_IMPLEMENTED:
        return None
    built = " ".join(str(args.get("built_instead") or "").split())[:_BUILT_INSTEAD_CHARS]
    return json.dumps({"idea_implemented": args["idea_implemented"], "built_instead": built},
                      indent=1)


def idea_report_note(node) -> str:
    """" [idea partly/different/not_implemented — built: …]" for a node whose Developer said it did
    not build its idea as proposed; "" otherwise (as proposed, or no report)."""
    try:
        data = json.loads((getattr(node, "files", None) or {}).get(IDEA_REPORT_NAME) or "{}")
    except (ValueError, TypeError, AttributeError):
        return ""
    value = data.get("idea_implemented") if isinstance(data, dict) else None
    if value not in IDEA_IMPLEMENTED or value == "as_proposed":
        return ""
    built = str(data.get("built_instead") or "").strip()
    return f" [idea {value}" + (f" — built instead: {built[:160]}" if built else "") + "]"
