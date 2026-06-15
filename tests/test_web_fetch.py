"""Tests for WebFetchTool host gating (tools/web_fetch.py).

Default posture is OPEN with an SSRF guard; an opt-in allowlist mode locks it
down. These cover the gating decisions only (no network): the pure host/SSRF
helpers, and the _execute branches that short-circuit BEFORE urlopen (private
IP block in open mode, allowlist reject). Hand-rolled.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

import adk_cc.tools.web_fetch as wf
from adk_cc.tools.web_fetch import (
    WebFetchTool,
    _host_allowed,
    _resolves_to_internal,
)
from adk_cc.tools.schemas import WebFetchArgs


def _run(url: str) -> dict:
    return asyncio.run(WebFetchTool()._execute(WebFetchArgs(url=url), None))


def _run_no_network(url: str) -> dict:
    """Run with urlopen stubbed to raise — so a request that PASSES the gate
    returns 'error' (network) instead of making a real call. Lets us assert
    the gate let it through without hitting the network."""
    orig = wf.urlopen

    def _stub(*_a, **_k):
        raise wf.URLError("stub: network disabled in test")

    wf.urlopen = _stub
    try:
        return asyncio.run(WebFetchTool()._execute(WebFetchArgs(url=url), None))
    finally:
        wf.urlopen = orig


class _FakeResp:
    """Minimal urlopen() return: context manager with .read/.headers/.status."""

    def __init__(self, body: bytes, content_type: str, status: int = 200):
        self._body = body
        self.headers = {"Content-Type": content_type}
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n is not None and n >= 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_with(url: str, body: bytes, content_type: str) -> dict:
    """Run with urlopen stubbed to return a canned (body, content_type)."""
    orig = wf.urlopen
    wf.urlopen = lambda *_a, **_k: _FakeResp(body, content_type)
    try:
        return asyncio.run(WebFetchTool()._execute(WebFetchArgs(url=url), None))
    finally:
        wf.urlopen = orig


def _make_pdf(text: str = "Hello PDF World") -> bytes:
    """A valid minimal single-page PDF with extractable text."""
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode() + b") Tj ET"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n%s\nendstream" % (len(stream), stream),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_off = len(out)
    n = len(objs) + 1
    out += b"xref\n0 %d\n" % n + b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (n, xref_off)
    return out


def _clear_env():
    for k in ("ADK_CC_WEB_FETCH_MODE", "ADK_CC_WEB_FETCH_HOSTS",
              "ADK_CC_WEB_FETCH_ALLOW_PRIVATE"):
        os.environ.pop(k, None)


def test_host_allowed_suffix_match():
    allowed = ("arxiv.org", "github.com")
    assert _host_allowed("arxiv.org", allowed)
    assert _host_allowed("www.arxiv.org", allowed)       # subdomain
    assert _host_allowed("export.arxiv.org", allowed)
    assert not _host_allowed("notarxiv.org", allowed)    # not a subdomain
    assert not _host_allowed("arxiv.org.evil.com", allowed)
    assert not _host_allowed("evilarxiv.org", allowed)
    print("OK host_allowed_suffix_match")


def test_resolves_to_internal_literals():
    for bad in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1",
                "169.254.169.254", "::1", "0.0.0.0", "localhost",
                "foo.local", "svc.internal"):
        assert _resolves_to_internal(bad), bad
    for ok in ("8.8.8.8", "1.1.1.1"):
        assert not _resolves_to_internal(ok), ok
    print("OK resolves_to_internal_literals")


def test_open_mode_blocks_ssrf():
    _clear_env()  # default = open
    r = _run("http://169.254.169.254/latest/meta-data/")  # cloud metadata
    assert r["status"] == "host_not_allowed", r
    assert "SSRF" in r["error"] or "private" in r["error"]
    r2 = _run("http://127.0.0.1:8000/")
    assert r2["status"] == "host_not_allowed", r2
    print("OK open_mode_blocks_ssrf")


def test_open_mode_allows_private_with_escape_hatch():
    _clear_env()
    os.environ["ADK_CC_WEB_FETCH_ALLOW_PRIVATE"] = "1"
    # gate passes → proceeds to urlopen (stubbed) → 'error', NOT
    # 'host_not_allowed'. That proves the gate let the private host through.
    r = _run_no_network("http://127.0.0.1:59999/")
    assert r["status"] != "host_not_allowed", r
    _clear_env()
    print("OK open_mode_allows_private_with_escape_hatch")


def test_bad_scheme_and_url():
    _clear_env()
    assert _run("ftp://example.com")["status"] == "error"
    assert _run("file:///etc/passwd")["status"] == "error"
    assert _run("notaurl")["status"] == "error"  # no host
    print("OK bad_scheme_and_url")


def test_allowlist_mode_rejects_unlisted():
    _clear_env()
    os.environ["ADK_CC_WEB_FETCH_MODE"] = "allowlist"
    r = _run("https://arxiv.org/abs/1706.03762")  # not in defaults
    assert r["status"] == "host_not_allowed", r
    assert "allowlist" in r["error"]
    assert "arxiv.org" not in r["allowed_hosts"]
    _clear_env()
    print("OK allowlist_mode_rejects_unlisted")


def test_allowlist_mode_accepts_listed_host_gate():
    _clear_env()
    os.environ["ADK_CC_WEB_FETCH_MODE"] = "allowlist"
    os.environ["ADK_CC_WEB_FETCH_HOSTS"] = "arxiv.org"
    # gate passes (host allowlisted) → proceeds to urlopen (stubbed). Assert
    # it did NOT reject at the gate; the network result is irrelevant.
    r = _run_no_network("https://arxiv.org/abs/1706.03762")
    assert r["status"] != "host_not_allowed", r
    # default preapproved hosts still allowed in allowlist mode
    r2 = _run_no_network("https://github.com/x/y")
    assert r2["status"] != "host_not_allowed", r2
    _clear_env()
    print("OK allowlist_mode_accepts_listed_host_gate")


def test_pdf_is_extracted_to_text():
    _clear_env()
    r = _run_with("https://arxiv.org/pdf/1706.03762", _make_pdf(), "application/pdf")
    assert r["status"] == "ok", r
    assert r["content_kind"] == "pdf_text", r
    assert "Hello PDF World" in r["content"], r["content"][:80]
    assert r["pages"] == 1 and r["extracted_via"] == "pypdf", r
    print("OK pdf_is_extracted_to_text")


def test_pdf_detected_by_magic_bytes_without_content_type():
    _clear_env()
    # content-type lies (octet-stream) but body starts with %PDF-
    r = _run_with("https://x.example/paper", _make_pdf("Magic Bytes Win"),
                  "application/octet-stream")
    assert r["content_kind"] == "pdf_text", r
    assert "Magic Bytes Win" in r["content"]
    print("OK pdf_detected_by_magic_bytes_without_content_type")


def test_unparseable_pdf_returns_marker_not_garbage():
    _clear_env()
    r = _run_with("https://x.example/broken.pdf", b"%PDF-1.7 not a real pdf",
                  "application/pdf")
    assert r["status"] == "ok" and r["content_kind"] == "pdf", r
    assert r["content"] == "" and "note" in r, r
    print("OK unparseable_pdf_returns_marker_not_garbage")


def test_binary_content_returns_marker_not_garbage():
    _clear_env()
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
    r = _run_with("https://x.example/img.png", png, "image/png")
    assert r["status"] == "ok" and r["content_kind"] == "binary", r
    assert r["content"] == "" and r["bytes"] == len(png), r
    print("OK binary_content_returns_marker_not_garbage")


def test_html_still_returned_as_text():
    _clear_env()
    r = _run_with("https://x.example/", b"<html><body>hi there</body></html>",
                  "text/html; charset=utf-8")
    assert r["status"] == "ok" and r["content_kind"] == "text", r
    assert "hi there" in r["content"]
    print("OK html_still_returned_as_text")


def main():
    test_host_allowed_suffix_match()
    test_resolves_to_internal_literals()
    test_open_mode_blocks_ssrf()
    test_open_mode_allows_private_with_escape_hatch()
    test_bad_scheme_and_url()
    test_allowlist_mode_rejects_unlisted()
    test_allowlist_mode_accepts_listed_host_gate()
    test_pdf_is_extracted_to_text()
    test_pdf_detected_by_magic_bytes_without_content_type()
    test_unparseable_pdf_returns_marker_not_garbage()
    test_binary_content_returns_marker_not_garbage()
    test_html_still_returned_as_text()
    print("\nall web-fetch tests passed")


if __name__ == "__main__":
    main()
