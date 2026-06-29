"""Read-only filesystem scout tools for the pre-run genesis BOSS.

So the boss can actually INSPECT a repo on this machine (list dirs, read text files, glob) before
authoring a `repo` task spec + an adaptation checklist — instead of only promising to. It drops in
behind the same tool-provider protocol as RunTools (`.specs()` / `.execute(name, args)`), so it runs
in the shared `agent.drive_tool_loop`.

Trusted-local only: the operator points the boss at their OWN repo via the localhost UI (the genesis
endpoint is also behind the optional UI token). Still defensively bounded:
  - every path is resolved and must live under an allowed root (home + the run-root) — a `..`
    traversal out of the roots is refused;
  - reads are text-only and size-capped;
  - SECRET files (.env, secrets.json, keys, ~/.ssh, ~/.aws, …) are never read — so a credential
    (incl. the LLM API key the server now keeps in its env) can't be slurped into the boss reply.
"""
from __future__ import annotations

import os
from pathlib import Path

_MAX_READ = 16000          # bytes returned from one read_file
_MAX_ENTRIES = 200         # entries per list_dir / find_files
# Text-ish extensions we'll return the contents of. Anything else (xlsx, bin, images) is reported as
# "exists, not read" so the boss still learns it's there.
_TEXT_EXT = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh",
             ".csv", ".tsv", ".rst", ".env.example", ".gitignore", ".dockerfile", "", ".lock"}
# Never read these — credentials / private keys. (list_dir may still SHOW the name; reading is denied.)
_SECRET_NAMES = {".env", "secrets.json", "credentials", ".netrc", ".pgpass", ".npmrc", ".pypirc"}
_SECRET_SUFFIX = {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks"}
_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".gpg", ".config/gcloud"}


def _looks_secret(p: Path) -> bool:
    name = p.name.lower()
    if name in _SECRET_NAMES or name.startswith(".env") and name != ".env.example":
        return True
    if p.suffix.lower() in _SECRET_SUFFIX or name.startswith("id_rsa") or name.startswith("id_ed25519"):
        return True
    parts = {part.lower() for part in p.parts}
    return bool(parts & _SECRET_DIRS)


class RepoScoutTools:
    def __init__(self, roots):
        self._roots = []
        for r in roots:
            if not r:
                continue
            try:
                self._roots.append(Path(os.path.expanduser(str(r))).resolve())
            except OSError:
                pass

    def _resolve(self, path: str):
        """Resolve a user/model-supplied path and confirm it's inside an allowed root (else None)."""
        if not path:
            return None
        try:
            p = Path(os.path.expanduser(str(path))).resolve()
        except OSError:
            return None
        for r in self._roots:
            if p == r or r in p.parents:
                return p
        return None

    def specs(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": "list_dir",
                "description": "List files and subdirectories under a directory on this machine (read-only). "
                               "Use to explore a repo's structure.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "Directory path (absolute or ~-relative)."}},
                    "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "read_file",
                "description": "Read a text file on this machine (first ~16KB, read-only). Use for README, "
                               "the train/eval entry script (e.g. test.py), configs, requirements.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "File path (absolute or ~-relative)."}},
                    "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "find_files",
                "description": "Recursively find files matching a glob under a directory (read-only).",
                "parameters": {"type": "object", "properties": {
                    "root": {"type": "string"},
                    "pattern": {"type": "string", "description": "glob, e.g. **/*.py or **/README*"}},
                    "required": ["root", "pattern"]}}},
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "list_dir":
                return self._list_dir(args.get("path", ""))
            if name == "read_file":
                return self._read_file(args.get("path", ""))
            if name == "find_files":
                return self._find_files(args.get("root", ""), args.get("pattern", "*"))
        except Exception as e:  # noqa: BLE001 - tools are advisory; never crash the loop
            return f"(error: {e})"
        return f"(unknown tool: {name})"

    def _list_dir(self, path: str) -> str:
        p = self._resolve(path)
        if not p:
            return f"(path not allowed or outside permitted roots: {path})"
        if not p.exists():
            return f"(no such path: {path})"
        if not p.is_dir():
            return f"(not a directory: {path})"
        rows, children = [], sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        for c in children[:_MAX_ENTRIES]:
            if c.is_dir():
                rows.append(f"DIR  {c.name}/")
            else:
                try:
                    sz = c.stat().st_size
                except OSError:
                    sz = "?"
                rows.append(f"FILE {c.name}  ({sz}b)")
        if len(children) > _MAX_ENTRIES:
            rows.append(f"… (+{len(children) - _MAX_ENTRIES} more)")
        return f"{p}:\n" + ("\n".join(rows) if rows else "(empty)")

    def _read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p:
            return f"(path not allowed or outside permitted roots: {path})"
        if not p.is_file():
            return f"(no such file: {path})"
        if _looks_secret(p):
            return f"(refused: {p.name} looks like a credential/secret file — not read)"
        if p.suffix.lower() not in _TEXT_EXT:
            try:
                sz = p.stat().st_size
            except OSError:
                sz = "?"
            return f"(binary/unsupported type {p.suffix or '<none>'}; {sz}b — exists, not read)"
        try:
            data = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            return f"(could not read: {e})"
        return data[:_MAX_READ] + ("\n… (truncated)" if len(data) > _MAX_READ else "")

    def _find_files(self, root: str, pattern: str) -> str:
        p = self._resolve(root)
        if not p:
            return f"(root not allowed or outside permitted roots: {root})"
        if not p.is_dir():
            return f"(not a directory: {root})"
        hits = []
        try:
            for m in sorted(p.glob(pattern or "*")):
                hits.append(str(m))
                if len(hits) >= _MAX_ENTRIES:
                    break
        except (OSError, ValueError) as e:
            return f"(bad pattern: {e})"
        return "\n".join(hits) if hits else f"(no matches for {pattern!r} under {root})"
