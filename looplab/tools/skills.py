"""Agent Skills (I18, ADR-9): a directory of SKILL.md (or *.md) files, each with
frontmatter `name`/`description` + a body of instructions. Auto-distilled skills also
carry `provenance`/`status`, which gate their production visibility. Progressive
disclosure — the agent first sees only name+description (cheap), and pulls the full
body on demand via the `use_skill` tool. Drops into the agentic toolset like KnowledgeTools.

BOUNDED AND ADDRESSABLE SINCE 2026-09-06 (doc 52 row 17; doc 51 §3). `use_skill` used to answer
with the whole Markdown body, uncapped — the ONE agent-facing provider in `tools/` that obeyed
none of `_base`'s bounded-output rules — so a library could not grow without eating the context
window a body at a time, and the agent layer's head-keep cut then discarded the END of a long
skill, which is where the caveats live. The unit is now the SECTION (SkillZip's finding, and the
`section=` shape `run_tools.py::_research_memo` already ships for memos): an answer keeps WHOLE
sections under the cap, never bytes (SkillZip Pro measured an unprotected 71 % compression at
-26 accuracy points), and ends by naming every section it left out beside the exact call that
returns it — `log_tools.py` rule 3, a remedy the caller has not already spent. The listing is
TIERED (HASTE: flat loading measured equal to no skills at 2x the tokens): `global` hand-written
skills, `domain` skills confirmed on more than one task family and ordered by their fit to the
bound run, and `task` single-task drafts, which stay out of the production listing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from looplab.tools._base import RESULT_CAP, clip, fit_rows

_FM = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
SKILL_TIERS = ("global", "domain", "task")
# Auto-distilled lifecycle vocabulary (`engine/memory.py::next_auto_skill_status` moves it):
# `candidate` (one task) -> `promoted` (confirmed on a different task family) -> `demoted` (a
# later recorded outcome reversed it) -> `retired` (demoted twice; a human must look). Only
# `promoted` reaches the production listing; the inspection opt-in shows the other three.
AUTO_SKILL_STATUSES = ("candidate", "promoted", "demoted", "retired")
# The answer cap keeps `_base`'s headroom convention (`cross_run_tools._MAX_TOOL_RESULT_CHARS`):
# the loop's own marker sits on top of RESULT_CAP, so a result landing exactly on it is one the
# loop cuts silently.
SKILL_RESULT_CAP = RESULT_CAP - 400
# The Jaccard bar `engine/lessons_priors.py` uses for "a related task" — one number, two readers.
DOMAIN_FIT_MIN = 0.34


@dataclass
class Skill:
    name: str
    description: str
    body: str
    # Auto-distilled skills carry a small trust lifecycle in their frontmatter.  Keep these
    # trailing/defaulted so callers that construct the old three-field Skill directly remain
    # source-compatible.
    provenance: str | None = None
    status: str | None = None
    # Declared `tier:` (one of SKILL_TIERS) — `skill_tier()` settles the effective tier when the
    # frontmatter says nothing; the task fingerprints an auto skill was confirmed on.
    tier: str | None = None
    fingerprints: list[list[str]] = field(default_factory=list)


def skill_tier(skill: Skill) -> str:
    """The tier a skill is LOADED under (HASTE's three): a declared `tier:` wins; otherwise a
    hand-written skill is `global`, a promoted auto skill `domain` and any other auto skill `task`."""
    if skill.tier in SKILL_TIERS:
        return skill.tier
    if skill.provenance == "auto":
        return "domain" if skill.status == "promoted" else "task"
    return "global"


def parse_skill_fingerprints(raw) -> list[list[str]]:
    """Parse the bounded `fingerprints:` shape auto-skill frontmatter carries, failing closed on drift.

    This is trust-bearing lifecycle evidence, not generic JSON.  Accepting a dict/string here makes
    iteration look superficially valid and can falsely satisfy the cross-task promotion test.
    Six histories is the writer's existing retention cap; the generous inner bounds prevent a
    hand-edited file from turning this best-effort path into unbounded work without rejecting normal
    task fingerprints. Shared by the reader (`Skill.fingerprints`, the domain-fit ordering) and the
    writer (`engine/memory.py::write_auto_skill`), so the two cannot drift on what counts.
    """
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list) or len(value) > 6:
        return []
    for fingerprint in value:
        if (not isinstance(fingerprint, list) or len(fingerprint) > 512
                or any(not isinstance(token, str) or len(token) > 1024
                       for token in fingerprint)):
            return []
    return value


def split_sections(body: str) -> list[tuple[str, str]]:
    """The skill body as `(name, text)` sections, one per Markdown heading (any level), with the
    text before the first heading as `intro`. A heading inside a fenced code block is code, not a
    boundary. Duplicate names get `_2`, `_3` suffixes so every section is addressable. The heading
    line stays inside its section's text, so a section served alone still says what it is."""
    sections: list[tuple[str, str]] = []
    name, buf, fenced = "intro", [], False
    for line in str(body or "").splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match:
            text = "\n".join(buf).strip()
            if text:
                sections.append((name, text))
            name, buf = match.group(2).strip() or "section", [line]
            continue
        buf.append(line)
    text = "\n".join(buf).strip()
    if text:
        sections.append((name, text))
    seen: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for sec_name, text in sections:
        seen[sec_name] = seen.get(sec_name, 0) + 1
        out.append((sec_name if seen[sec_name] == 1 else f"{sec_name}_{seen[sec_name]}", text))
    return out


def _use_skill_call(name: str, section: str) -> str:
    return f'use_skill(name={name!r}, section={section!r})'


def _find_section(sections: list[tuple[str, str]], wanted: str) -> tuple[str, str] | None:
    wanted = str(wanted or "").strip()
    for exact in (lambda n: n == wanted, lambda n: n.lower() == wanted.lower(),
                  lambda n: n.lower().startswith(wanted.lower()) if wanted else False):
        hits = [sec for sec in sections if exact(sec[0])]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def render_skill_body(skill: Skill, *, cap: int = SKILL_RESULT_CAP, section: str | None = None) -> str:
    """The body under `cap`, in WHOLE sections — bytes are cut only when one section alone is over
    the cap, and then the answer says so. A body that fits is returned verbatim, byte for byte.
    Every bounded answer ends by naming what it left out and the call that returns each piece."""
    body = str(skill.body or "")
    sections = split_sections(body)
    if section is not None:
        if not sections:
            return f"(skill {skill.name!r} has no sections)"
        hit = _find_section(sections, section)
        if hit is None:
            names = ", ".join(repr(n) for n, _ in sections)
            return (f"(no such section {str(section)!r} in skill {skill.name!r}; "
                    f"sections: {names})")
        sec_name, text = hit
        note = (f"\n\n(section {sec_name!r} of skill {skill.name!r} is longer than the "
                f"{cap}-char answer cap: {{n}} chars cut at a line boundary; the skill file "
                "on disk holds the rest)")
        # `reserve` charges the marker AGAINST the cap: this caller has no headroom of its own, and
        # a marker sitting on top of the cap is exactly what pushes the answer past it.
        return clip(text, cap, line_boundary=True, note=note, reserve=len(note.format(n=len(text))))
    if len(body) <= cap:
        return body
    total = len(sections)
    kept: list[str] = []
    shown = 0
    for index, (sec_name, text) in enumerate(sections):
        omitted = sections[index:]
        receipt = _omission_receipt(skill.name, index, total, omitted, len(body))
        candidate = "\n\n".join(kept + [text])
        if len(candidate) + len(receipt) <= cap:
            kept.append(text)
            shown = index + 1
            continue
        break
    if not kept:
        # The FIRST section alone is over the cap: the one case where bytes are cut, said out loud.
        first_name, first_text = sections[0]
        receipt = _omission_receipt(skill.name, 1, total, sections[1:], len(body))
        note = (f"\n(section {first_name!r} alone exceeds the answer cap: {{n}} chars cut "
                f"at a line boundary; {_use_skill_call(skill.name, first_name)} returns it "
                "with the whole cap to itself)")
        head = clip(first_text, max(0, cap - len(receipt)), line_boundary=True, note=note,
                    reserve=len(note.format(n=len(first_text))))
        return head + receipt
    return "\n\n".join(kept) + _omission_receipt(skill.name, shown, total, sections[shown:], len(body))


def _omission_receipt(name: str, shown: int, total: int, omitted: list[tuple[str, str]],
                      body_chars: int) -> str:
    if not omitted:
        return ""
    calls = "; ".join(_use_skill_call(name, sec_name) for sec_name, _ in omitted[:12])
    more = "" if len(omitted) <= 12 else f" (+{len(omitted) - 12} more sections)"
    return (f"\n\n(skill {name!r}: {shown} of {total} sections shown, whole sections only; the body "
            f"is {body_chars} chars. Not shown: "
            + ", ".join(repr(sec_name) for sec_name, _ in omitted[:12]) + more
            + f". Each returns in full with its own cap through {calls})")


def parse_skill_frontmatter(text: str) -> dict[str, str]:
    """Return scalar fields from a real leading skill frontmatter block.

    Lifecycle metadata is security-relevant: a phrase in the Markdown body must never be read as
    ``status`` or ``fingerprints``.  Keep this parser shared by the skill reader and auto-skill
    writer so the two sides cannot drift on where trusted metadata ends.  Duplicate keys retain the
    historical last-one-wins behavior of ``_parse_skill``; malformed lines are ignored.
    """
    match = _FM.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    # Frontmatter records are physically LF-delimited.  ``str.splitlines()`` also treats Unicode
    # NEL/line/paragraph separators as record boundaries; those characters are valid inside a JSON
    # string and must not let one scalar manufacture a second lifecycle field.
    for line in match.group(1).split("\n"):
        key, separator, value = line.partition(":")
        key = key.strip().lower()
        if separator and key:
            fields[key] = value.strip()
    return fields


def _parse_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8-sig", errors="replace")  # utf-8-sig drops a BOM; won't crash load
    name, desc, body = (path.parent.name if path.name == "SKILL.md" else path.stem), "", text.strip()
    metadata = parse_skill_frontmatter(text)
    m = _FM.match(text)
    if m:
        body = m.group(2).strip()
    if metadata.get("name"):
        name = metadata["name"]
    desc = metadata.get("description", "")
    provenance = metadata.get("provenance") or None
    status = metadata.get("status") or None
    tier = (metadata.get("tier") or "").strip().lower() or None
    return Skill(name=name, description=desc, body=body,
                 provenance=provenance.lower() if provenance else None,
                 status=status.lower() if status else None,
                 tier=tier if tier in SKILL_TIERS else None,
                 fingerprints=parse_skill_fingerprints(metadata.get("fingerprints", "")))


class SkillLibrary:
    def __init__(self, skills_dir, *, include_auto_candidates: bool = False):
        # Accept one dir (str/Path) OR several (list/tuple): hand-written and M4 auto-distilled skills
        # live in different dirs but must share ONE library — two separate SkillTools providers both
        # register list_skills/use_skill and the second shadows the first (the hand-written one becomes
        # unreachable). A single library over both dirs makes every skill visible. Earlier dirs win on
        # a name clash (hand-written overrides an auto-distilled skill of the same name).
        dirs = [skills_dir] if isinstance(skills_dir, (str, Path)) else list(skills_dir or [])
        self.dirs = [Path(d) for d in dirs]
        self.dir = self.dirs[0] if self.dirs else Path(".")   # back-compat single-dir accessor
        self.include_auto_candidates = bool(include_auto_candidates)
        self.skills: dict[str, Skill] = {}
        for d in reversed(self.dirs):     # reversed so an EARLIER dir's skill overwrites (wins)
            paths = list(d.glob("**/SKILL.md")) + list(d.glob("*.md"))
            for p in sorted(set(paths)):
                s = _parse_skill(p)
                # Human-authored and legacy skills have no auto provenance and remain visible
                # byte-for-byte.  Run-authored skills are trusted for the production prompt surface
                # only after cross-task promotion.  An explicit constructor opt-in lets review/tests
                # inspect candidates without turning that into a runtime Settings escape hatch.
                if s.provenance == "auto":
                    # `demoted`/`retired` (2026-09-06) are recognized NON-production states beside
                    # `candidate`: a later recorded outcome moved the skill back, so it leaves the
                    # production listing exactly like a draft that never earned it.
                    visible_auto = s.status == "promoted" or (
                        self.include_auto_candidates and s.status in AUTO_SKILL_STATUSES)
                    if not visible_auto:
                        # Unknown/malformed auto status also fails closed, even in inspection mode:
                        # that opt-in means exactly the recognized lifecycle, not every string.
                        continue
                self.skills[s.name] = s

    def by_tier(self) -> dict[str, list[Skill]]:
        out: dict[str, list[Skill]] = {tier: [] for tier in SKILL_TIERS}
        for skill in self.skills.values():
            out[skill_tier(skill)].append(skill)
        return out


class SkillTools:
    """Tool provider for the agentic Researcher: list_skills / use_skill."""

    def __init__(self, skills_dir, *, include_auto_candidates: bool = False):
        self.lib = SkillLibrary(
            skills_dir, include_auto_candidates=include_auto_candidates)
        self._run_fingerprint: list[str] = []

    def bind_state(self, state, parent=None) -> None:
        # OPTIONAL ToolProvider hook (`tools/_base.py`): the run's own task fingerprint, so the
        # `domain` tier can be ordered by fit to THIS run. Best-effort — a state without a goal
        # leaves the listing in library order.
        try:
            from looplab.engine.memory import task_fingerprint
            self._run_fingerprint = task_fingerprint(
                str(getattr(state, "task_kind", "") or ""), str(getattr(state, "direction", "") or ""),
                str(getattr(state, "goal", "") or ""))
        except Exception as exc:  # noqa: BLE001 — an ordering hint must not break the provider
            from looplab.core.containment import contain
            contain("skill tier fingerprint", exc)
            self._run_fingerprint = []

    def domain_fit(self, skill: Skill) -> float | None:
        """Max Jaccard between the bound run's fingerprint and the task fingerprints this skill was
        confirmed on; None when either side has nothing to compare."""
        if not self._run_fingerprint or not skill.fingerprints:
            return None
        from looplab.core.text import fingerprint_similarity
        return max(fingerprint_similarity(self._run_fingerprint, fp) for fp in skill.fingerprints)

    def specs(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": "list_skills",
                "description": ("List available skills (name + one-line description), grouped by "
                                "tier: global (always relevant), domain (confirmed on more than one "
                                "task family, ordered by fit to this run), task (single-task drafts)."),
                "parameters": {"type": "object",
                               "properties": {"tier": {"type": "string", "enum": list(SKILL_TIERS),
                                                       "description": "only this tier"}},
                               "required": []}}},
            {"type": "function", "function": {
                "name": "use_skill",
                "description": ("Load a skill's instructions by name. The answer keeps whole sections "
                                "under the result cap and names any it left out; pass `section` to "
                                "read one section in full."),
                "parameters": {"type": "object",
                               "properties": {"name": {"type": "string"},
                                              "section": {"type": "string",
                                                          "description": "one section by its heading"}},
                               "required": ["name"]}}},
        ]

    def _row(self, s: Skill) -> str:
        # The per-row bytes are the historical ones (a hand-written row is `name: description`);
        # the tier is the GROUP a row sits under and the fit is a suffix, so nothing that read the
        # old listing has to relearn it.
        row = (f"{s.name}: "
               + (("UNTRUSTED_MEMORY_AUTO_SKILL "
                   f"provenance={s.provenance!r} status={s.status!r} ")
                  if s.provenance == "auto" else "")
               + s.description)
        fit = self.domain_fit(s)
        if fit is not None and skill_tier(s) == "domain":
            row += (f" (fit to this run {fit:.2f})" if fit >= DOMAIN_FIT_MIN
                    else f" (other task family, fit {fit:.2f})")
        return row

    _TIER_HEADERS = {
        "global": "[global — hand-written, always relevant]",
        "domain": "[domain — confirmed on more than one task family; ordered by fit to this run]",
        "task": "[task — single-task drafts, shown only under inspection]",
    }

    def _listing(self, tier: str | None) -> str:
        groups = self.lib.by_tier()
        rows: list[str] = []
        for tier_name in SKILL_TIERS:
            if tier is not None and tier_name != tier:
                continue
            skills = list(groups[tier_name])
            if not skills:
                continue
            if tier_name == "domain":
                skills.sort(key=lambda sk: -(self.domain_fit(sk) if self.domain_fit(sk) is not None
                                             else -1.0))
            rows.append(self._TIER_HEADERS[tier_name])
            rows.extend(self._row(sk) for sk in skills)
        if not rows:
            return "(no skills)" if tier is None else f"(no skills in tier {tier!r})"
        total = sum(len(groups[t]) for t in SKILL_TIERS if tier is None or t == tier)
        return fit_rows([], rows, receipt=f"{total} skills", cap=SKILL_RESULT_CAP,
                        omitted="... ({receipt}{n} more rows omitted to fit the result cap; "
                                "pass tier=... for one tier at a time)")

    def execute(self, name: str, args: dict) -> str:
        # ToolProvider contract: never raise (a junk arg — e.g. an unhashable `name` — must read as a
        # tool error, not propagate out of the agent loop and discard the phase).
        try:
            args = args or {}
            if name == "list_skills":
                tier = args.get("tier")
                tier = str(tier).strip().lower() if isinstance(tier, str) and tier.strip() else None
                if tier is not None and tier not in SKILL_TIERS:
                    return f"(no such tier {tier!r}; tiers: {', '.join(SKILL_TIERS)})"
                return self._listing(tier)
            if name == "use_skill":
                s = self.lib.skills.get(str(args.get("name", "")))
                if not s:
                    return f"(no such skill: {args.get('name')})"
                section = args.get("section")
                section = str(section) if isinstance(section, str) and section.strip() else None
                if s.provenance == "auto":
                    # Auto-distilled prose is model/run-authored evidence, not operator authority.
                    # The Researcher system contract already treats UNTRUSTED_MEMORY as advisory;
                    # label the tool payload at its source instead of relying on directory identity.
                    label = ("UNTRUSTED_MEMORY_AUTO_SKILL "
                             f"provenance={s.provenance!r} status={s.status!r}\n")
                    return label + render_skill_body(
                        s, cap=max(200, SKILL_RESULT_CAP - len(label)), section=section)
                return render_skill_body(s, section=section)
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001
            return f"(tool error: {e})"
