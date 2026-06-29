"""Read-only filesystem scout tools for the pre-run genesis BOSS.

So the boss can actually INSPECT a repo on this machine (list dirs, read text files, glob) before
authoring a `repo` task spec + an adaptation checklist — instead of only promising to. It drops in
behind the same tool-provider protocol as RunTools (`.specs()` / `.execute(name, args)`), so it runs
in the shared `agent.drive_tool_loop`.

Trusted-local only: the operator points the boss at their OWN repo via the localhost UI (the genesis
endpoint is also behind the optional UI token). Defensively bounded, because the tool RESULTS are fed
to the model (possibly a REMOTE provider):
  - every path is resolved and must live under an allowed root (home + the run-root); a `..`/symlink
    escape resolves out and is refused;
  - read is an ALLOWLIST — only known source/doc/config extensions (and a few safe extensionless
    names like Makefile/Dockerfile/README) are returned; anything else is "exists, not read", so an
    unrecognized dotfile can't be slurped;
  - on TOP of that, credential files (.env, secrets/keys, ~/.ssh, ~/.aws, ~/.kube, ~/.docker, gcloud,
    and any name containing secret/credential/password/api_key/private/id_rsa) are refused AND hidden
    from list_dir/find_files — so a secret (incl. the LLM API key in the server env) can't reach the
    model via contents OR via a revealed filename.
"""
from __future__ import annotations

import os
from pathlib import Path

from .knowledge_tools import _fn_spec   # shared OpenAI function-schema builder (one schema shape)

_MAX_READ = 16000          # bytes returned from one read_file
_MAX_ENTRIES = 200         # entries per list_dir / find_files

# read-file ALLOWLIST: extensions we'll return the contents of, ...
_TEXT_EXT = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh",
             ".csv", ".tsv", ".rst", ".lock", ".bat", ".ps1", ".r", ".jl", ".sql", ".html", ".css",
             ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cpp", ".cc", ".c", ".h", ".hpp",
             ".xml", ".properties", ".gradle", ".tf"}
# ... plus a few KNOWN-safe files that have no usable suffix (Path.suffix wouldn't match them).
_SAFE_NAMES = {"makefile", "dockerfile", "readme", "license", "licence", "changelog", "notice",
               "authors", "requirements", "pipfile", "procfile", "manifest.in", "containerfile",
               ".gitignore", ".dockerignore", ".env.example"}

# Credential files — never read, and hidden from listings. Defense in depth on TOP of the allowlist.
_SECRET_NAMES = {"secrets.json", "credentials", ".netrc", ".pgpass", ".npmrc", ".pypirc",
                 ".git-credentials", ".dockercfg", ".boto", ".s3cfg", ".pg_service.conf"}
_SECRET_SUFFIX = {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ovpn", ".kdbx", ".asc"}
_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker", "gcloud", ".azure", ".config"}
# Substrings that strongly imply a credential. Deliberately NOT bare "token"/"secret" alone (would
# false-block ML files like tokenizer.py); these are specific enough to avoid that.
_SECRET_SUBSTR = ("credential", "password", "passwd", "_secret", "secret_", ".secret", "secrets.",
                  "apikey", "api_key", "private_key", "privatekey", "id_rsa", "id_ed25519", "id_ecdsa")


def _looks_secret(p: Path) -> bool:
    name = p.name.lower()
    if name in _SECRET_NAMES:
        return True
    if name.startswith(".env") and name != ".env.example":      # .env, .env.local, .env.prod, …
        return True
    if p.suffix.lower() in _SECRET_SUFFIX:
        return True
    if any(tok in name for tok in _SECRET_SUBSTR):              # secrets.yaml, client_secret.json, id_rsa
        return True
    parts = {part.lower() for part in p.parts}                  # any ancestor dir is a known secret dir
    return bool(parts & _SECRET_DIRS)


def _readable(p: Path) -> bool:
    """Allowlist gate: a known source/doc/config extension, or a known safe extensionless name."""
    return p.suffix.lower() in _TEXT_EXT or p.name.lower() in _SAFE_NAMES


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
        """Resolve a user/model-supplied path and confirm it's inside an allowed root (else None).
        resolve() canonicalizes `..` and follows symlinks, so an escape lands outside the roots."""
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
            _fn_spec("list_dir",
                     "List files and subdirectories under a directory on this machine (read-only). "
                     "Use to explore a repo's structure.",
                     {"path": {"type": "string", "description": "Directory path (absolute or ~-relative)."}},
                     ["path"]),
            _fn_spec("read_file",
                     "Read a text file on this machine (first ~16KB, read-only). Use for README, the "
                     "train/eval entry script (e.g. test.py), configs, requirements.",
                     {"path": {"type": "string", "description": "File path (absolute or ~-relative)."}},
                     ["path"]),
            _fn_spec("find_files",
                     "Recursively find files matching a glob under a directory (read-only).",
                     {"root": {"type": "string"},
                      "pattern": {"type": "string", "description": "glob, e.g. **/*.py or **/README*"}},
                     ["root"]),
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
        # Hide credential files/dirs from the listing too — not just from read_file — so a secret's
        # existence + name never reaches the model.
        children = [c for c in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
                    if not _looks_secret(c)]
        rows = []
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
        if not _readable(p):
            try:
                sz = p.stat().st_size
            except OSError:
                sz = "?"
            return f"(unsupported/binary type {p.suffix or '<none>'}; {sz}b — exists, not read)"
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
                if _looks_secret(m):                  # don't reveal secret paths via glob either
                    continue
                hits.append(str(m))
                if len(hits) >= _MAX_ENTRIES:
                    break
        except (OSError, ValueError) as e:
            return f"(bad pattern: {e})"
        return "\n".join(hits) if hits else f"(no matches for {pattern!r} under {root})"
