#!/usr/bin/env python3
"""Destroy sandbox-service sessions on tenant/user offboarding.

The sandbox service (JISUlicious/sandboxing) auto-stops idle sessions
(`Limits.idle_stop_timer_s`, default 15 min) and eventually destroys
them after `Limits.hard_destroy_ttl_s` (default 24 h). For immediate
teardown — typical for GDPR delete, tenant offboarding, or "user X
asked to drop everything now" — operators run this script.

Modes:

  1. Destroy a single session by id:

         scripts/sandbox_destroy.py --session <id>

  2. Destroy all sessions matching a tenant_id filter (server-side
     filter via `GET /v1/sessions?tenant_id=...`, if the upstream
     service exposes it; otherwise list-and-filter client-side):

         scripts/sandbox_destroy.py --tenant <id>

  3. Destroy all sessions a particular agent host has touched, by
     reading session ids from stdin (one per line):

         cat sessions-to-purge.txt | scripts/sandbox_destroy.py --stdin

Safety:
  - `--dry-run` prints what would be deleted; no HTTP DELETE issued.
  - Refuses to run without an explicit `--confirm` flag in the
    multi-session modes (`--tenant`, `--stdin`); too easy to wipe
    production by typo otherwise.
  - Always logs the response code and body for each destroy call so
    operators can audit.

Reads connection config from env vars (mirrors adk-cc's runtime):

  ADK_CC_SANDBOX_SERVICE_URL          (required)
  ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN (required — admin token)
  ADK_CC_SANDBOX_SERVICE_VERIFY_TLS   (default 1)

Standalone (no adk_cc imports) so it runs anywhere with Python +
`urllib`. Python stdlib only.

Exit codes:
  0  success (zero or more sessions destroyed)
  1  config error (missing env, --root unreachable, etc.)
  2  invalid arguments
  3  partial failure — at least one destroy returned non-2xx
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Iterable, Optional

logger = logging.getLogger("sandbox_destroy")


def _build_opener(verify_tls: bool) -> urllib.request.OpenerDirector:
    if verify_tls:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handler = urllib.request.HTTPSHandler(context=ctx)
    return urllib.request.build_opener(handler)


def _http(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    token: str,
    *,
    timeout: float = 30.0,
) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body


def _list_sessions(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    token: str,
    tenant_id: Optional[str],
) -> list[str]:
    """Best-effort list. The upstream API surface for listing isn't fully
    spec'd in the README we have, so this calls `GET /v1/sessions` and
    filters client-side if a tenant_id is given. If the endpoint isn't
    exposed (404), returns []; the caller should ask the operator to
    pipe session ids via --stdin instead."""
    url = f"{base_url}/v1/sessions"
    if tenant_id:
        url += f"?tenant_id={urllib.parse.quote(tenant_id)}"  # noqa: F821
    code, body = _http(opener, "GET", url, token)
    if code == 404:
        logger.warning(
            "GET /v1/sessions returned 404 — the upstream service may not "
            "expose listing yet. Use --stdin to pipe session ids explicitly."
        )
        return []
    if code >= 400:
        logger.error("list sessions failed: %d %s", code, body)
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        logger.error("list sessions returned non-JSON: %s", body[:200])
        return []
    sessions = data.get("sessions") or data.get("items") or data
    if not isinstance(sessions, list):
        return []
    out: list[str] = []
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id")
        if not sid:
            continue
        if tenant_id and entry.get("tenant_id") != tenant_id:
            continue
        out.append(sid)
    return out


def _destroy_one(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    token: str,
    session_id: str,
    *,
    dry_run: bool,
) -> bool:
    url = f"{base_url}/v1/sessions/{urllib.parse.quote(session_id)}"  # noqa: F821
    if dry_run:
        logger.info("[dry-run] would DELETE %s", url)
        return True
    code, body = _http(opener, "DELETE", url, token)
    if code == 404:
        logger.info("session %s already gone (404)", session_id)
        return True
    if 200 <= code < 300:
        logger.info("destroyed %s (HTTP %d)", session_id, code)
        return True
    logger.error("destroy %s failed: %d %s", session_id, code, body[:300])
    return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # urllib.parse is referenced lazily; import here so the module loads
    # cleanly on systems where the stdlib unpickles oddly.
    import urllib.parse  # noqa: F401 — used by inner functions

    p = argparse.ArgumentParser(
        description="Destroy sandbox-service sessions for tenant/user offboarding.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Destroy a single session by id.")
    group.add_argument(
        "--tenant",
        help="Destroy every session belonging to this tenant_id.",
    )
    group.add_argument(
        "--stdin",
        action="store_true",
        help="Read session ids from stdin (one per line) and destroy each.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be destroyed; do not issue DELETE.",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Required for multi-session modes (--tenant / --stdin). Without "
            "this, those modes refuse to act."
        ),
    )
    args = p.parse_args()

    base_url = os.environ.get("ADK_CC_SANDBOX_SERVICE_URL")
    token = os.environ.get("ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN")
    if not base_url or not token:
        logger.error(
            "ADK_CC_SANDBOX_SERVICE_URL and ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN "
            "must be set."
        )
        return 1
    base_url = base_url.rstrip("/")
    verify_tls = os.environ.get("ADK_CC_SANDBOX_SERVICE_VERIFY_TLS", "1") != "0"
    opener = _build_opener(verify_tls)

    targets: Iterable[str]
    if args.session:
        targets = [args.session]
    elif args.tenant:
        if not args.confirm and not args.dry_run:
            logger.error(
                "--tenant is destructive across many sessions. Pass --confirm "
                "(or --dry-run to preview)."
            )
            return 2
        targets = _list_sessions(opener, base_url, token, args.tenant)
        if not targets:
            logger.info("no sessions to destroy for tenant %s", args.tenant)
            return 0
    else:  # --stdin
        if not args.confirm and not args.dry_run:
            logger.error(
                "--stdin is destructive across many sessions. Pass --confirm "
                "(or --dry-run to preview)."
            )
            return 2
        targets = [
            line.strip() for line in sys.stdin.readlines() if line.strip()
        ]

    failures = 0
    for sid in targets:
        ok = _destroy_one(opener, base_url, token, sid, dry_run=args.dry_run)
        if not ok:
            failures += 1

    if failures:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
