"""Shared path/secret safety guards for every tool provider that touches the filesystem.

Factored out of `reposcout.py` so the READ scout, the WRITE/edit provider, the shell provider and the
git provider all enforce the SAME rules — a path that a read refuses to reveal must not be writable or
runnable either, and there is one place to audit. `reposcout` re-exports these for back-compat.

Rules (defense in depth, because tool RESULTS are fed to a possibly-REMOTE model):
  - `resolve_within(roots, path)`: resolve() canonicalizes `..` and follows symlinks, so a traversal
    or symlink escape lands outside the allowed roots and is refused (returns None).
  - `looks_secret(path)`: credential files (.env, keys, ~/.ssh, ~/.aws, …, anything containing
    secret/credential/password/api_key/private/id_rsa) are refused for read AND write AND hidden from
    listings — so a secret (incl. the server's LLM API key) can't reach the model or be clobbered.
  - `readable(path)`: an ALLOWLIST of known source/doc/config extensions (+ a few safe extensionless
    names) — used by the read scout so an unrecognized dotfile can't be slurped.
"""
from __future__ import annotations

import os
from pathlib import Path

# read-file ALLOWLIST: extensions whose contents we'll return, ...
TEXT_EXT = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh",
            ".csv", ".tsv", ".rst", ".lock", ".bat", ".ps1", ".r", ".jl", ".sql", ".html", ".css",
            ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cpp", ".cc", ".c", ".h", ".hpp",
            ".xml", ".properties", ".gradle", ".tf"}
# ... plus a few KNOWN-safe files that have no usable suffix (Path.suffix wouldn't match them).
SAFE_NAMES = {"makefile", "dockerfile", "readme", "license", "licence", "changelog", "notice",
              "authors", "requirements", "pipfile", "procfile", "manifest.in", "containerfile",
              ".gitignore", ".dockerignore", ".env.example"}

# Credential files — never read/written, and hidden from listings. Defense in depth ON TOP of the allowlist.
SECRET_NAMES = {"secrets.json", "credentials", ".netrc", ".pgpass", ".npmrc", ".pypirc",
                ".git-credentials", ".dockercfg", ".boto", ".s3cfg", ".pg_service.conf"}
SECRET_SUFFIX = {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ovpn", ".kdbx", ".asc"}
SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker", "gcloud", ".azure", ".config"}
# Substrings that strongly imply a credential. Deliberately NOT bare "token"/"secret" alone (would
# false-block ML files like tokenizer.py); these are specific enough to avoid that.
SECRET_SUBSTR = ("credential", "password", "passwd", "_secret", "secret_", ".secret", "secrets.",
                 "apikey", "api_key", "private_key", "privatekey", "id_rsa", "id_ed25519", "id_ecdsa")


def looks_secret(p: Path) -> bool:
    name = p.name.lower()
    if name in SECRET_NAMES:
        return True
    if name.startswith(".env") and name != ".env.example":      # .env, .env.local, .env.prod, …
        return True
    if p.suffix.lower() in SECRET_SUFFIX:
        return True
    if any(tok in name for tok in SECRET_SUBSTR):                # secrets.yaml, client_secret.json, id_rsa
        return True
    parts = {part.lower() for part in p.parts}                   # any ancestor dir is a known secret dir
    return bool(parts & SECRET_DIRS)


def readable(p: Path) -> bool:
    """Allowlist gate: a known source/doc/config extension, or a known safe extensionless name."""
    return p.suffix.lower() in TEXT_EXT or p.name.lower() in SAFE_NAMES


def resolve_roots(roots) -> list[Path]:
    out = []
    for r in roots:
        if not r:
            continue
        try:
            out.append(Path(os.path.expanduser(str(r))).resolve())
        except OSError:
            pass
    return out


def resolve_within(roots, path: str):
    """Resolve a user/model-supplied path and confirm it's inside an allowed root (else None).
    `roots` may be raw strings or resolved Paths. resolve() canonicalizes `..` and follows symlinks,
    so an escape lands outside the roots and is refused."""
    if not path:
        return None
    rroots = [r if isinstance(r, Path) else Path(os.path.expanduser(str(r))).resolve() for r in roots]
    try:
        p = Path(os.path.expanduser(str(path))).resolve()
    except OSError:
        return None
    for r in rroots:
        if p == r or r in p.parents:
            return p
    return None
