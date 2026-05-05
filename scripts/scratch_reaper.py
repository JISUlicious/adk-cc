#!/usr/bin/env python3
"""Reap stale per-session scratch dirs.

Walks `<root>/<tenant>/<user>/.sessions/<session>/` and removes any
session dir whose mtime is older than `--max-age-days`. User home dirs
themselves are never touched — only the `.sessions/` subdirectories.

Wire as cron / systemd timer in production:

    # /etc/cron.daily/adk-cc-scratch-reaper
    0 3 * * * /usr/bin/python3 /opt/adk-cc/scripts/scratch_reaper.py \\
      --root /var/lib/adk-cc/wks --max-age-days 7

Standalone (no adk_cc imports) so it runs against the sandbox VM
directly without needing the agent's Python env. Python stdlib only.

Safety:
  - The script ONLY descends into directories matching the
    `<root>/*/*/.sessions/*/` glob — refuses to reap anything that
    doesn't fit that shape.
  - Uses `pathlib.Path.is_relative_to` to confirm each candidate sits
    under the configured root before deleting (defense against an
    operator typo'd `--root`).
  - `--dry-run` prints what would be deleted without touching anything.
  - Logs each deletion with the path and age so operators can audit.

Exit codes:
  0  success (zero or more dirs reaped)
  1  --root doesn't exist or isn't a directory
  2  invalid arguments
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

logger = logging.getLogger("scratch_reaper")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reap stale per-session scratch dirs from an adk-cc workspace root.",
    )
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Workspace root (e.g. /var/lib/adk-cc/wks). Reaps scratch dirs under <root>/*/*/.sessions/*/.",
    )
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=7.0,
        help="Reap scratch dirs whose mtime is older than this many days (default: 7).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted, don't touch anything.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log every dir scanned, including ones too young to reap.",
    )
    return parser.parse_args()


def _reap(root: Path, max_age_seconds: float, dry_run: bool, verbose: bool) -> int:
    """Returns the number of dirs reaped (or, in dry-run, the number that
    would have been reaped)."""
    root_resolved = root.resolve()
    now = time.time()
    reaped = 0
    skipped_young = 0

    # Glob shape: <root>/<tenant>/<user>/.sessions/<session>/
    for session_dir in root_resolved.glob("*/*/.sessions/*"):
        if not session_dir.is_dir():
            continue

        # Defense in depth: confirm the candidate sits under root_resolved.
        # Should be guaranteed by the glob, but operator typos in --root
        # could stamp -- never trust the glob alone.
        try:
            session_dir.resolve().relative_to(root_resolved)
        except ValueError:
            logger.warning(
                "skipping %s — resolves outside --root (symlink?)", session_dir
            )
            continue

        try:
            mtime = session_dir.stat().st_mtime
        except OSError as e:
            logger.warning("skipping %s — stat failed: %s", session_dir, e)
            continue

        age_seconds = now - mtime
        age_days = age_seconds / 86400.0

        if age_seconds < max_age_seconds:
            skipped_young += 1
            if verbose:
                logger.info("keeping %s (age %.1f days)", session_dir, age_days)
            continue

        if dry_run:
            logger.info("[dry-run] would reap %s (age %.1f days)", session_dir, age_days)
            reaped += 1
            continue

        try:
            shutil.rmtree(session_dir)
        except OSError as e:
            logger.error("failed to reap %s: %s", session_dir, e)
            continue

        logger.info("reaped %s (age %.1f days)", session_dir, age_days)
        reaped += 1

    logger.info(
        "%s%d scratch dir(s); %d skipped (too young).",
        "[dry-run] would reap " if dry_run else "reaped ",
        reaped,
        skipped_young,
    )
    return reaped


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()

    if not args.root.exists() or not args.root.is_dir():
        logger.error("--root %s does not exist or is not a directory.", args.root)
        return 1

    if args.max_age_days < 0:
        logger.error("--max-age-days must be non-negative.")
        return 2

    max_age_seconds = args.max_age_days * 86400.0
    _reap(args.root, max_age_seconds, args.dry_run, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
