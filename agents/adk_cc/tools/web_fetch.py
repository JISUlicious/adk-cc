"""Fetch a URL's body as text.

Default posture is OPEN: any public http/https host is allowed, so the agent
can reach arbitrary docs/papers (arxiv, vendor blogs, etc.) during research
without an operator pre-listing every domain. "Open" means open to the public
internet, NOT to your internal network — in open mode an SSRF guard blocks
hosts that resolve to private / loopback / link-local / reserved addresses
(this includes localhost and the 169.254.169.254 cloud-metadata endpoint),
because web_fetch can be steered by untrusted content the agent has read.

Two env knobs:
  - ADK_CC_WEB_FETCH_MODE=allowlist  → lock down: only DEFAULT_PREAPPROVED_HOSTS
    plus ADK_CC_WEB_FETCH_HOSTS are allowed (the pre-existing behavior). In
    allowlist mode the operator has vouched for the hosts, so the SSRF guard
    is not applied (lets you allowlist an internal docs server on purpose).
  - ADK_CC_WEB_FETCH_ALLOW_PRIVATE=1 → in OPEN mode, disable the SSRF guard
    (dev convenience for fetching http://localhost:PORT services).

Host matching is suffix-aware: an allowlist entry `arxiv.org` also matches
`www.arxiv.org`.

Stage E note: this uses urllib directly. For full sandbox isolation in a
multi-tenant deployment, route through `backend.exec("curl -s URL")` so egress
is filtered by the same network policy that governs run_bash. The SSRF guard
here resolves the host and re-resolves at fetch time (a small TOCTOU window);
the backend-egress path closes it fully — that hardening is not in this stage.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import os
import socket
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from google.adk.tools.tool_context import ToolContext

from .base import AdkCcTool, ToolMeta
from .schemas import WebFetchArgs

# Convenience defaults used only in allowlist mode (common docs sites LLMs
# reach for). In the default OPEN mode these are irrelevant — everything's
# allowed subject to the SSRF guard.
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

_BLOCKED_NAME_SUFFIXES = (".local", ".internal", ".localhost")


def _mode() -> str:
    return (os.environ.get("ADK_CC_WEB_FETCH_MODE") or "open").strip().lower()


def _allowlist() -> tuple[str, ...]:
    raw = os.environ.get("ADK_CC_WEB_FETCH_HOSTS")
    extra = tuple(h.strip().lower() for h in (raw or "").split(",") if h.strip())
    return DEFAULT_PREAPPROVED_HOSTS + extra


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """Exact or subdomain match: 'arxiv.org' allows 'www.arxiv.org'."""
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowed)


def _allow_private() -> bool:
    return os.environ.get("ADK_CC_WEB_FETCH_ALLOW_PRIVATE") == "1"


def _ip_is_internal(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


# Content types we treat as text (decode + return as-is). Anything else with
# binary-looking bytes gets a structured marker instead of garbled UTF-8.
_TEXTUAL_CT_HINTS = (
    "text/", "json", "xml", "html", "javascript", "ecmascript",
    "csv", "yaml", "x-www-form-urlencoded", "application/rss",
    "application/atom",
)
# PDFs are read whole (a truncated PDF can't be parsed), up to this cap.
_DEFAULT_PDF_MAX_BYTES = 10_000_000


def _pdf_max_bytes() -> int:
    try:
        return max(1, int(os.environ.get("ADK_CC_WEB_FETCH_PDF_MAX_BYTES", "")))
    except ValueError:
        return _DEFAULT_PDF_MAX_BYTES


def _looks_textual(content_type: str, body: bytes) -> bool:
    ct = (content_type or "").lower()
    if ct:
        return any(h in ct for h in _TEXTUAL_CT_HINTS)
    # No content-type: sniff. NUL bytes ⇒ binary.
    return b"\x00" not in body[:2048]


def _extract_pdf_text(body: bytes) -> tuple[Optional[str], int, str]:
    """Best-effort PDF→text. Returns (text|None, num_pages, extractor). Tries
    pypdf (the declared dep), then pdfminer / PyPDF2 if an operator installed
    them. Any failure returns (None, 0, "") so the caller emits a clean
    'binary' marker rather than raising."""
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(body))
        pages = len(reader.pages)
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        return text, pages, "pypdf"
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text  # type: ignore

        return extract_text(io.BytesIO(body)) or "", 0, "pdfminer"
    except Exception:
        pass
    try:
        import PyPDF2  # type: ignore

        reader = PyPDF2.PdfReader(io.BytesIO(body))
        pages = len(reader.pages)
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        return text, pages, "PyPDF2"
    except Exception:
        pass
    return None, 0, ""


def _resolves_to_internal(host: str) -> bool:
    """SSRF guard: True if `host` is, or resolves to, a non-public address.

    Catches literal private/loopback/link-local IPs (incl. the
    169.254.169.254 metadata endpoint), the `localhost`/`*.local`/`*.internal`
    names, and public-looking names that resolve to an internal IP. A
    resolution failure returns False so urlopen surfaces the real DNS error
    rather than a misleading 'blocked'."""
    h = host.lower().rstrip(".")
    if _ip_is_internal(h):  # literal IP
        return True
    if h == "localhost" or h.endswith(_BLOCKED_NAME_SUFFIXES):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return any(_ip_is_internal(info[4][0]) for info in infos)


def _fetch_and_process(url: str, max_bytes: int) -> dict[str, Any]:
    """The BLOCKING part of web_fetch — DNS + connect + read (up to 10s) and the
    CPU-bound PDF/text processing. Run via ``asyncio.to_thread`` so none of it
    runs on the event loop (a single slow fetch would otherwise stall every
    other request, health checks included)."""
    req = Request(url, headers={"User-Agent": "adk-cc/0.1"})
    try:
        with urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "")
            status_code = resp.status
            # PDFs must be read whole to parse, so use a larger cap when
            # the response declares itself a PDF.
            is_pdf_ct = "application/pdf" in content_type.lower()
            read_cap = _pdf_max_bytes() if is_pdf_ct else max_bytes
            body = resp.read(read_cap + 1)
            body_truncated = len(body) > read_cap
            if body_truncated:
                body = body[:read_cap]
    except HTTPError as e:
        return {"status": "http_error", "url": url, "status_code": e.code, "error": str(e)}
    except URLError as e:
        return {"status": "error", "url": url, "error": str(e)}

    base = {"status": "ok", "url": url, "status_code": status_code, "content_type": content_type}

    # PDF → extract text (don't return raw binary as garbled UTF-8).
    if is_pdf_ct or body[:5] == b"%PDF-":
        text, pages, via = _extract_pdf_text(body)
        if text and text.strip():
            clipped = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            return {
                **base, "content_kind": "pdf_text", "pages": pages, "extracted_via": via,
                "content": clipped, "truncated": body_truncated or len(clipped) < len(text),
            }
        return {
            **base, "content_kind": "pdf", "bytes": len(body), "content": "",
            "note": (
                "Fetched a PDF but could not extract text "
                f"({'truncated download' if body_truncated else 'no extractor / empty'})"
                ". Try the HTML page (e.g. an arxiv.org/abs/<id> URL) or "
                "install 'pypdf' to enable extraction."
            ),
        }

    # Non-text binary (images, archives, …) → marker, not garbled text.
    if not _looks_textual(content_type, body):
        return {
            **base, "content_kind": "binary", "bytes": len(body), "content": "",
            "note": "Binary content not decoded as text.",
        }

    return {
        **base, "content_kind": "text",
        "content": body.decode("utf-8", errors="replace"), "truncated": body_truncated,
    }


class WebFetchTool(AdkCcTool):
    meta = ToolMeta(
        name="web_fetch",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = WebFetchArgs
    description = (
        "Fetch a URL and return its body as text. Any public http/https host "
        "is allowed by default; private/loopback/internal addresses are "
        "blocked. PDFs are auto-extracted to text (content_kind='pdf_text'); "
        "other binary content returns a marker, not garbled bytes. Text is "
        "truncated at max_bytes."
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
        host = parsed.hostname

        if _mode() == "allowlist":
            allowed = _allowlist()
            if not _host_allowed(host, allowed):
                return {
                    "status": "host_not_allowed",
                    "error": (
                        f"{host} is not on the allowlist "
                        "(ADK_CC_WEB_FETCH_MODE=allowlist)"
                    ),
                    "allowed_hosts": list(allowed),
                }
        elif not _allow_private() and await asyncio.to_thread(_resolves_to_internal, host):
            # OPEN mode SSRF guard. The DNS resolution is blocking, so it's
            # offloaded too.
            return {
                "status": "host_not_allowed",
                "error": (
                    f"{host} resolves to a private/loopback/internal address "
                    "— blocked to prevent SSRF. Set "
                    "ADK_CC_WEB_FETCH_ALLOW_PRIVATE=1 to fetch local services."
                ),
            }

        # The blocking network round-trip + PDF/text processing run OFF the
        # event loop, so a slow fetch never stalls other requests/health checks.
        return await asyncio.to_thread(_fetch_and_process, args.url, args.max_bytes)
