"""Prepare a real MLE-bench competition locally — without the kaggle client.

mle-bench's own ``download_and_prepare_dataset`` downloads via the kaggle PyPI client, which
can't use a ``KGAT_`` Bearer token (see :mod:`looplab.adapters.kaggle_dl`). This wrapper does the same
job through the Bearer downloader: fetch the data zip, extract it into the competition's
``raw`` dir, then hand off to mle-bench's *real* ``prepare_fn`` to build the official
``public`` (given to the agent) / ``private`` (held-out answers) split. The grading later
uses mle-bench's real grader, so a score produced this way is the genuine MLE-bench metric.

The only thing this skips is mle-bench's zip-checksum check (the published checksum is for the
kaggle-client download; a Bearer download of the same competition is byte-identical content but
may repackage). Pass ``verify=True`` to instead verify the *prepared* public/private checksums
against the committed ``checksums.yaml`` — that proves the split matches mle-bench exactly.

CLI::

    python -m looplab.adapters.mlebench_prep -c spooky-author-identification
    python -m looplab.adapters.mlebench_prep --selected          # the CPU-lite set below
    python -m looplab.adapters.mlebench_prep -c <id> --data-dir D:/mle-data --verify
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from looplab.adapters.kaggle_dl import KaggleRulesError, download_competition

# The CPU-only MLE-bench Lite competitions used for LoopLab live runs (no GPU, small data).
#   spooky-author-identification              text, multi-class log-loss (lower better)
#   nomad2018-predict-transparent-conductors  tabular, mean column-wise RMSLE (lower better)
# NOTE: detecting-insults-in-social-commentary is intentionally NOT in this set — its Kaggle rules
# can't currently be accepted on this account (download stays 403). Its offline baseline still
# exists in mlebench_real._BASELINES, so it can be prepared/run by id if access is restored.
SELECTED = (
    "spooky-author-identification",
    "nomad2018-predict-transparent-conductors",
)


def _registry(data_dir: Optional[str]):
    from mlebench.registry import registry
    return registry if not data_dir else registry.set_data_dir(Path(data_dir).resolve())


def is_prepared(competition_id: str, data_dir: Optional[str] = None) -> bool:
    """True if the competition's public+private split + answers already exist locally."""
    from mlebench.data import is_dataset_prepared
    comp = _registry(data_dir).get_competition(competition_id)
    return is_dataset_prepared(comp)


def prepare_competition(competition_id: str, *, data_dir: Optional[str] = None,
                        token: Optional[str] = None, force: bool = False,
                        verify: bool = False, keep_zip: bool = True):
    """Download (via Bearer token) + prepare one competition using mle-bench's real prepare_fn.

    Returns the mle-bench ``Competition``. Idempotent: a competition already prepared is
    returned untouched unless ``force``. Raises KaggleRulesError if the rules aren't accepted.
    """
    from mlebench.data import create_prepared_dir, is_dataset_prepared
    from mlebench.utils import extract, is_empty

    import shutil

    comp = _registry(data_dir).get_competition(competition_id)
    if is_dataset_prepared(comp) and not force:
        return comp

    if force:
        # Clear any stale raw/prepared so the re-downloaded zip is actually re-extracted and
        # re-prepared — extract() below is gated on an empty raw_dir, so a leftover raw tree would
        # otherwise be reused and prepare_fn would run on stale data.
        shutil.rmtree(comp.raw_dir, ignore_errors=True)
        shutil.rmtree(comp.public_dir.parent, ignore_errors=True)   # prepared/{public,private}

    # Leaderboard ships in the mle-bench repo (committed); get_competition points at it. If it's
    # missing the competition isn't a known/lite one — fail clearly rather than hit the network.
    if not comp.leaderboard.is_file():
        raise FileNotFoundError(
            f"No committed leaderboard for '{competition_id}' at {comp.leaderboard}. Medal "
            "thresholds come from it; only mle-bench's bundled competitions are supported.")

    comp.raw_dir.mkdir(parents=True, exist_ok=True)
    create_prepared_dir(comp)

    # competition_dir = raw_dir.parent (mle-bench convention); the outer data zip lands there.
    zip_path = download_competition(competition_id, comp.raw_dir.parent, token=token, force=force)
    if is_empty(comp.raw_dir):
        extract(zip_path, comp.raw_dir, recursive=False)

    # mle-bench's REAL preparer: deterministic split (random_state=0) → public + private/answers.
    comp.prepare_fn(raw=comp.raw_dir, public=comp.public_dir, private=comp.private_dir)
    (comp.public_dir / "description.md").write_text(comp.description, encoding="utf-8")

    if not is_dataset_prepared(comp):
        raise RuntimeError(f"Preparation finished but '{competition_id}' still looks unprepared.")

    if verify:
        _verify_prepared_checksums(comp)
    if not keep_zip:
        zip_path.unlink(missing_ok=True)
    return comp


def _verify_prepared_checksums(comp) -> None:
    """Verify the prepared public/private dirs match mle-bench's committed checksums (proves our
    Bearer-downloaded split is byte-identical to the official one). Loud ValueError on mismatch."""
    from mlebench.data import generate_checksums
    from mlebench.utils import load_yaml
    if not comp.checksums.is_file():
        return
    expected = load_yaml(comp.checksums)
    for part, d in (("public", comp.public_dir), ("private", comp.private_dir)):
        exp = expected.get(part)
        if exp and generate_checksums(d) != exp:
            raise ValueError(
                f"Prepared '{part}' checksums for {comp.id} do not match mle-bench's committed "
                "checksums — the split differs from the official one.")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("-c", "--competition-id", help="competition id to prepare")
    g.add_argument("--selected", action="store_true",
                   help=f"prepare the CPU-lite set: {', '.join(SELECTED)}")
    ap.add_argument("--data-dir", default=None, help="override the mle-bench data dir")
    ap.add_argument("--force", action="store_true", help="re-download + re-prepare even if present")
    ap.add_argument("--verify", action="store_true", help="verify prepared checksums vs mle-bench")
    args = ap.parse_args(argv)

    ids = list(SELECTED) if args.selected else [args.competition_id]
    rc = 0
    for cid in ids:
        try:
            if is_prepared(cid, args.data_dir) and not args.force:
                print(f"[skip] {cid} already prepared")
                continue
            print(f"[prepare] {cid} …", flush=True)
            comp = prepare_competition(cid, data_dir=args.data_dir, force=args.force, verify=args.verify)
            print(f"[done]  {cid}: public={comp.public_dir}")
            print(f"        answers (held out)={comp.answers}")
        except KaggleRulesError as e:
            print(f"[rules] {e}", file=sys.stderr)
            rc = 2
        except Exception as e:  # noqa: BLE001
            print(f"[error] {cid}: {type(e).__name__}: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
