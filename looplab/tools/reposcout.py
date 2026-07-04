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


from looplab.core import _pathsafe
from looplab.tools._base import fn_spec   # shared OpenAI function-schema builder (one schema shape)

# Path/secret guards now live in _pathsafe (shared with the write/shell/git providers so every tool
# enforces the same rules). Re-exported under the historical private names for back-compat.
_looks_secret = _pathsafe.looks_secret
_readable = _pathsafe.readable

_MAX_READ = 16000          # bytes returned from one read_file
_MAX_ENTRIES = 200         # entries per list_dir / find_files


class RepoScoutTools:
    def __init__(self, roots):
        self._roots = _pathsafe.resolve_roots(roots)

    def _resolve(self, path: str):
        """Resolve a user/model-supplied path and confirm it's inside an allowed root (else None)."""
        return _pathsafe.resolve_within(self._roots, path)

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_dir",
                     "List files and subdirectories under a directory on this machine (read-only). "
                     "Use to explore a repo's structure.",
                     {"path": {"type": "string", "description": "Directory path (absolute or ~-relative)."}},
                     ["path"]),
            fn_spec("read_file",
                     "Read a text file on this machine (first ~16KB, read-only). Use for README, the "
                     "train/eval entry script (e.g. test.py), configs, requirements.",
                     {"path": {"type": "string", "description": "File path (absolute or ~-relative)."}},
                     ["path"]),
            fn_spec("find_files",
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
                # pathlib glob accepts `..` segments and follows symlinks, so a pattern like
                # "../../etc/*" escapes the allowed roots — re-validate every hit against the roots
                # (and run the secret filter on the RESOLVED path so a symlinked secret is caught).
                rm = _pathsafe.resolve_within(self._roots, str(m))
                if rm is None or _looks_secret(rm):
                    continue
                hits.append(str(rm))
                if len(hits) >= _MAX_ENTRIES:
                    break
        except (OSError, ValueError) as e:
            return f"(bad pattern: {e})"
        return "\n".join(hits) if hits else f"(no matches for {pattern!r} under {root})"
