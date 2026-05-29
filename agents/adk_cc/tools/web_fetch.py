"""Fetch a URL, gated by a per-call NetworkConfig allowlist.

Defaults to a small preapproved-hosts set covering common docs sites
that LLMs reach for during research. Operators extend the list via
`ADK_CC_WEB_FETCH_HOSTS` (comma-separated) or by replacing the tool
instance with a custom one.

Stage E note: this uses urllib directly. For full sandbox isolation in a
multi-tenant deployment, route through `backend.exec("curl -s URL")` so
egress is filtered by the same network policy that governs run_bash.
That extension is straightforward but not in this stage.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from google.adk.tools.tool_context import ToolContext

from .base import AdkCcTool, ToolMeta
from .schemas import WebFetchArgs

DEFAULT_PREAPPROVED_HOSTS: tuple[str, ...] = (
    "docs.python.org",
    "pypi.org",
    "github.com",
    "raw.githubusercontent.com",
    "developer.mozilla.org",
    "stackoverflow.com",
    "google.github.io",
    "ai.google.dev",
)


def _load_preapproved_hosts() -> tuple[str, ...]:
    raw = os.environ.get("ADK_CC_WEB_FETCH_HOSTS")
    if not raw:
        return DEFAULT_PREAPPROVED_HOSTS
    extra = tuple(h.strip() for h in raw.split(",") if h.strip())
    return DEFAULT_PREAPPROVED_HOSTS + extra


class WebFetchTool(AdkCcTool):
    meta = ToolMeta(
        name="web_fetch",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = WebFetchArgs
    description = (
        "Fetch a URL and return its body as text. Only hosts on the "
        "preapproved list are allowed. The body is truncated at max_bytes."
    )

    async def _execute(self, args: WebFetchArgs, ctx: ToolContext) -> dict[str, Any]:
        try:
            parsed = urlparse(args.url)
        except Exception as e:
            return {"status": "error", "error": f"bad url: {e}"}
        if parsed.scheme not in ("http", "https"):
            return {"status": "error", "error": f"unsupported scheme: {parsed.scheme}"}
        if not parsed.hostname:
            return {"status": "error", "error": "url has no host"}

        allowed = _load_preapproved_hosts()
        if parsed.hostname not in allowed:
            return {
                "status": "host_not_allowed",
                "error": f"{parsed.hostname} is not on the preapproved list",
                "allowed_hosts": list(allowed),
            }

        req = Request(args.url, headers={"User-Agent": "adk-cc/0.1"})
        try:
            with urlopen(req, timeout=10) as resp:
                body = resp.read(args.max_bytes + 1)
                truncated = len(body) > args.max_bytes
                if truncated:
                    body = body[: args.max_bytes]
                content_type = resp.headers.get("Content-Type", "")
                status_code = resp.status
        except HTTPError as e:
            return {
                "status": "http_error",
                "url": args.url,
                "status_code": e.code,
                "error": str(e),
            }
        except URLError as e:
            return {"status": "error", "url": args.url, "error": str(e)}

        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = repr(body)
        return {
            "status": "ok",
            "url": args.url,
            "status_code": status_code,
            "content_type": content_type,
            "content": text,
            "truncated": truncated,
        }
