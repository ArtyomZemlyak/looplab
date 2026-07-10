"""Tolerant SEARCH/REPLACE matching for edit_file-style tools (BACKLOG §4 "edit_match").

Extracted VERBATIM from `RepoWriteTools.edit_file` (adapters/repo_developer.py) so the delicate,
test-covered matching logic lives in exactly one place: an exact unique-occurrence match first,
then a whitespace-tolerant line-anchored fallback (models often lose trailing whitespace when
copying a snippet). Note: `WriteTools._edit` (tools/write_tools.py) deliberately does NOT use
this helper — it is exact-match only, with its own arg names and error strings; giving it the
tolerant fallback would be a behavior change.
"""
from __future__ import annotations

from typing import Optional


def apply_search_replace(current: str, search: str, replace: str,
                         *, path: str = "") -> tuple[Optional[str], str]:
    """Apply one SEARCH/REPLACE hunk to `current` and return `(new_text, message)` —
    `new_text` is None when nothing was applied (empty/ambiguous/unmatched `search`), and
    `message` is the human-readable result either way. `path` only labels the messages (they
    are returned to the model verbatim, so they must keep naming the file being edited)."""
    p = path
    cur = current
    if not search:
        return None, "(refused: empty `search` — copy the exact snippet you want to replace)"
    n = cur.count(search)
    if n == 0:
        # Whitespace-tolerant fallback: match ignoring trailing spaces on each line (models
        # often lose trailing whitespace when copying a snippet). The match must be LINE-
        # ANCHORED — start at a line boundary and end at end-of-line/EOF — because the
        # replacement swaps WHOLE lines: a mid-line substring hit would silently eat the
        # line's prefix/suffix (verified corruption), and the reported success would hide it.
        def _norm(t: str) -> str:
            return "\n".join(l.rstrip() for l in t.splitlines())
        cn, sn = _norm(cur), _norm(search)
        idx = cn.find(sn) if sn else -1
        anchored = (idx >= 0 and cn.count(sn) == 1
                    and (idx == 0 or cn[idx - 1] == "\n")
                    and (idx + len(sn) == len(cn) or cn[idx + len(sn)] == "\n"))
        if anchored:
            pre_lines = cn[:idx].count("\n")
            s_len = sn.count("\n") + 1          # lines in the MATCHED span (from sn, not search
            #                                     — a trailing blank line in `search` must not
            #                                     swallow the next real line)
            cur_lines = cur.splitlines(keepends=True)
            tail = "".join(cur_lines[pre_lines + s_len:])
            # Preserve the file's line endings: `cur_lines` (keepends) carry the real EOL, but `rep`
            # arrives from the agent as LF. Splicing an LF `rep` into a CRLF file leaves MIXED endings
            # (later exact-match edits + `git apply` then mis-match). Normalize `rep` to the matched
            # span's EOL before splicing.
            _last = cur_lines[pre_lines + s_len - 1]
            eol = "\r\n" if _last.endswith("\r\n") else "\n"
            last_had_nl = _last.endswith("\n")   # (\r\n also ends with \n)
            rep = replace.replace("\r\n", "\n").replace("\n", eol) if eol != "\n" else replace
            if rep and last_had_nl and not rep.endswith(eol):
                rep += eol                      # keep the file's line structure
            elif rep and not last_had_nl and rep.endswith(eol):
                rep = rep[:-len(eol)]           # match-at-EOF without trailing newline: keep it so
            # empty `replace` = deletion of the matched lines; no stray blank line
            new = "".join(cur_lines[:pre_lines]) + rep + tail
            return new, f"edited {p} (whitespace-tolerant match, 1 hunk applied)"
        first = (search.splitlines() or [""])[0].strip()
        near = next((l for l in cur.splitlines() if first and first in l), "")
        return None, (f"(no match: `search` was not found in {p} — copy it EXACTLY from the current "
                      f"content{', nearest line: ' + near[:120] if near else ''})")
    if n > 1:
        return None, f"(ambiguous: `search` occurs {n} times in {p} — include more surrounding lines to make it unique)"
    return cur.replace(search, replace, 1), f"edited {p} (1 hunk applied)"
