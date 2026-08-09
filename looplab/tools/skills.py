"""Agent Skills (I18, ADR-9): a directory of SKILL.md (or *.md) files, each with
frontmatter `name`/`description` + a body of instructions. Auto-distilled skills also
carry `provenance`/`status`, which gate their production visibility. Progressive
disclosure — the agent first sees only name+description (cheap), and pulls the full
body on demand via the `use_skill` tool. Drops into the agentic toolset like KnowledgeTools.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FM = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)


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
    return Skill(name=name, description=desc, body=body,
                 provenance=provenance.lower() if provenance else None,
                 status=status.lower() if status else None)


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
                    visible_auto = s.status == "promoted" or (
                        self.include_auto_candidates and s.status == "candidate")
                    if not visible_auto:
                        # Unknown/malformed auto status also fails closed, even in candidate-inspection
                        # mode: that opt-in means exactly candidates, not every unrecognized lifecycle.
                        continue
                self.skills[s.name] = s


class SkillTools:
    """Tool provider for the agentic Researcher: list_skills / use_skill."""

    def __init__(self, skills_dir, *, include_auto_candidates: bool = False):
        self.lib = SkillLibrary(
            skills_dir, include_auto_candidates=include_auto_candidates)

    def specs(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": "list_skills",
                "description": "List available skills (name + one-line description).",
                "parameters": {"type": "object", "properties": {}, "required": []}}},
            {"type": "function", "function": {
                "name": "use_skill",
                "description": "Load the full instructions of a skill by name.",
                "parameters": {"type": "object",
                               "properties": {"name": {"type": "string"}},
                               "required": ["name"]}}},
        ]

    def execute(self, name: str, args: dict) -> str:
        # ToolProvider contract: never raise (a junk arg — e.g. an unhashable `name` — must read as a
        # tool error, not propagate out of the agent loop and discard the phase).
        try:
            args = args or {}
            if name == "list_skills":
                return "\n".join(
                    f"{s.name}: "
                    + (("UNTRUSTED_MEMORY_AUTO_SKILL "
                        f"provenance={s.provenance!r} status={s.status!r} ")
                       if s.provenance == "auto" else "")
                    + s.description
                    for s in self.lib.skills.values()) or "(no skills)"
            if name == "use_skill":
                s = self.lib.skills.get(str(args.get("name", "")))
                if not s:
                    return f"(no such skill: {args.get('name')})"
                if s.provenance == "auto":
                    # Auto-distilled prose is model/run-authored evidence, not operator authority.
                    # The Researcher system contract already treats UNTRUSTED_MEMORY as advisory;
                    # label the tool payload at its source instead of relying on directory identity.
                    return ("UNTRUSTED_MEMORY_AUTO_SKILL "
                            f"provenance={s.provenance!r} status={s.status!r}\n{s.body}")
                return s.body
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001
            return f"(tool error: {e})"
