"""Desktop mode must serve the DESKTOP UI bundle.

The shell a user sees is baked in at BUILD time (`VITE_ADK_CC_DESKTOP=1` →
`web/dist-desktop`), while the backend's mode is a runtime env var. Those are
two independent switches, and the server defaulted to `web/dist` regardless —
so anyone who started the backend themselves with ADK_CC_DESKTOP=1 got the WEB
shell: no projects rail, no file tree, no model chip, and nothing on screen
saying why. Reported by a user who thought the desktop app was broken.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_ui_bundle_selection.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_REPO = Path(__file__).resolve().parent.parent


def _resolved_dist(*, desktop: bool, explicit: str | None = None) -> str | None:
    """What make_app would serve, without booting the whole app."""
    import adk_cc.deployment as deployment

    prev = dict(os.environ)
    try:
        os.environ["ADK_CC_SERVE_UI"] = "1"
        os.environ.pop("ADK_CC_UI_DIST", None)
        if explicit:
            os.environ["ADK_CC_UI_DIST"] = explicit
        if desktop:
            os.environ["ADK_CC_DESKTOP"] = "1"
        else:
            os.environ.pop("ADK_CC_DESKTOP", None)

        # Mirror the resolution in service/server.py:make_app.
        if explicit:
            return explicit
        web = _REPO / "web"
        desktop_dist = web / "dist-desktop"
        if deployment.is_desktop() and desktop_dist.is_dir():
            return str(desktop_dist)
        return str(web / "dist")
    finally:
        os.environ.clear()
        os.environ.update(prev)


def test_desktop_mode_serves_the_desktop_bundle() -> None:
    got = _resolved_dist(desktop=True)
    assert got.endswith("dist-desktop"), got
    print("OK desktop_mode_serves_the_desktop_bundle")


def test_web_mode_is_unchanged() -> None:
    got = _resolved_dist(desktop=False)
    assert got.endswith("web/dist"), got
    print("OK web_mode_is_unchanged")


def test_explicit_override_still_wins() -> None:
    custom = tempfile.mkdtemp(prefix="ui-dist-")
    assert _resolved_dist(desktop=True, explicit=custom) == custom
    print("OK explicit_override_still_wins")


def test_the_real_server_resolves_the_same_way() -> None:
    """Guard against the mirror above drifting from make_app."""
    src = (_REPO / "agents/adk_cc/service/server.py").read_text()
    assert 'desktop_dist = web / "dist-desktop"' in src, "resolution moved"
    assert "_is_desktop() and desktop_dist.is_dir()" in src, "desktop branch gone"
    assert "build:desktop" in src, "the warning must name the fix"
    print("OK the_real_server_resolves_the_same_way")


def main() -> None:
    test_desktop_mode_serves_the_desktop_bundle()
    test_web_mode_is_unchanged()
    test_explicit_override_still_wins()
    test_the_real_server_resolves_the_same_way()
    print("\nall UI-bundle-selection tests passed")


if __name__ == "__main__":
    main()
