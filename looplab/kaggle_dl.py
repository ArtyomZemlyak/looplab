"""Zero-dependency Kaggle competition downloader using a Bearer API token.

The modern Kaggle ``KGAT_…`` API tokens authenticate as an HTTP **Bearer** token; the
official ``kaggle`` PyPI client only speaks legacy Basic auth (``username:key``) or the new
OAuth web flow, so it cannot use these tokens at all (verified: Basic auth with the token as
the password returns 401, Bearer returns 200). This module therefore talks the Kaggle REST
API directly with stdlib ``urllib`` — no kaggle client, no extra dependencies — so LoopLab
can fetch competition data with nothing but the token.

It is deliberately the ONLY Kaggle-network code in LoopLab: mle-bench's own download path
(``mlebench.data.download_dataset`` → the kaggle client) is bypassed; we feed the raw zip to
mle-bench's real ``prepare_fn`` ourselves (see ``mlebench_prep``).

Token resolution order (first non-empty wins):
  1. an explicit ``token=`` argument
  2. ``$LOOPLAB_KAGGLE_TOKEN``
  3. ``$KAGGLE_KEY``
  4. ``~/.kaggle/kaggle.json`` (the ``"key"`` field)
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

API = "https://www.kaggle.com/api/v1"


class KaggleAuthError(RuntimeError):
    """No usable token, or Kaggle rejected it (401)."""


class KaggleRulesError(RuntimeError):
    """Kaggle returned 403 — the competition rules have not been accepted yet. This is a
    manual, per-competition step on the website (clicking "I Understand and Accept"); the
    API cannot do it for you."""

    def __init__(self, competition_id: str):
        self.competition_id = competition_id
        super().__init__(
            f"Kaggle returned 403 for '{competition_id}'. Accept the competition rules first: "
            f"https://www.kaggle.com/competitions/{competition_id}/rules - click 'I Understand "
            "and Accept', then re-run. (One-time per competition, per account.)")


def resolve_token(explicit: Optional[str] = None) -> str:
    """Find the Kaggle Bearer token; raise KaggleAuthError if none is configured."""
    for cand in (explicit, os.environ.get("LOOPLAB_KAGGLE_TOKEN"), os.environ.get("KAGGLE_KEY")):
        if cand and cand.strip():
            return cand.strip()
    kj = Path.home() / ".kaggle" / "kaggle.json"
    if kj.is_file():
        try:
            key = str(json.loads(kj.read_text(encoding="utf-8")).get("key", "")).strip()
        except (ValueError, OSError):
            key = ""
        if key:
            return key
    raise KaggleAuthError(
        "No Kaggle API token found. Set LOOPLAB_KAGGLE_TOKEN (or KAGGLE_KEY), or place the "
        "token in ~/.kaggle/kaggle.json under the 'key' field.")


class _DropAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    """Strip the Authorization header on a cross-host redirect. The Kaggle download endpoint
    302-redirects to a signed Google Cloud Storage URL whose auth is in the query string; our
    Kaggle bearer must NOT be forwarded there (it would leak the token to a third party and can
    make GCS reject the request). Mirrors curl's default no-``--location-trusted`` behaviour."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            new.headers = {k: v for k, v in new.headers.items() if k.lower() != "authorization"}
            new.unredirected_hdrs = {k: v for k, v in getattr(new, "unredirected_hdrs", {}).items()
                                     if k.lower() != "authorization"}
        return new


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_DropAuthOnHostChange())


def _request(path: str, token: str) -> urllib.request.Request:
    return urllib.request.Request(f"{API}/{path}", headers={"Authorization": f"Bearer {token}"})


def check_auth(token: Optional[str] = None, *, timeout: int = 30) -> bool:
    """True if the token authenticates (a cheap authenticated GET returns 2xx). Raises
    KaggleAuthError on 401, re-raises other HTTP errors."""
    token = resolve_token(token)
    try:
        with _opener().open(_request("competitions/list?page=1", token), timeout=timeout) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise KaggleAuthError("Kaggle rejected the token (401 Unauthorized).") from e
        raise


def download_competition(competition_id: str, dest_dir, *, token: Optional[str] = None,
                         timeout: int = 1800, force: bool = False) -> Path:
    """Download a competition's full data archive to ``dest_dir/<id>.zip`` and return its path.

    Raises KaggleRulesError (403, rules not accepted), KaggleAuthError (401), or the underlying
    HTTPError on anything else. Idempotent: an existing non-empty zip is reused unless ``force``.
    """
    token = resolve_token(token)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{competition_id}.zip"
    if out.is_file() and out.stat().st_size > 0 and not force:
        return out
    tmp = out.with_suffix(".zip.part")
    req = _request(f"competitions/data/download-all/{competition_id}", token)
    try:
        with _opener().open(req, timeout=timeout) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, length=1 << 20)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        if e.code == 403:
            raise KaggleRulesError(competition_id) from e
        if e.code == 401:
            raise KaggleAuthError("Kaggle rejected the token (401 Unauthorized).") from e
        raise
    tmp.replace(out)
    return out
