"""Tests for the desktop settings.env file (packaged-app config).

Covers path resolution, template creation/idempotency, the important safety
property that a fresh (all-commented) template loads NOTHING (so it can't shadow
a real API key), and the end-to-end dotenv bootstrap loading a filled file.

Run: `.venv/bin/python tests/test_desktop_settings_file.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_TMP = tempfile.mkdtemp(prefix="adk-cc-settings-test-")

from dotenv import dotenv_values

from adk_cc.service.desktop_config import ensure_settings_template, settings_env_path

_REPO = Path(__file__).resolve().parents[1]


def test_settings_env_path_resolution() -> None:
    # explicit file wins
    os.environ["ADK_CC_SETTINGS_FILE"] = f"{_TMP}/explicit.env"
    assert settings_env_path() == Path(f"{_TMP}/explicit.env")
    del os.environ["ADK_CC_SETTINGS_FILE"]
    # data dir
    os.environ["ADK_CC_DESKTOP_DATA"] = _TMP
    assert settings_env_path() == Path(_TMP) / "settings.env"
    del os.environ["ADK_CC_DESKTOP_DATA"]
    # home default
    assert settings_env_path() == Path.home() / ".adk-cc-desktop" / "settings.env"
    print("OK test_settings_env_path_resolution")


def test_template_created_and_idempotent() -> None:
    os.environ["ADK_CC_DESKTOP_DATA"] = _TMP
    try:
        p = ensure_settings_template()
        assert p.is_file() and p == Path(_TMP) / "settings.env"
        first = p.read_text()
        # editing then re-ensuring must NOT overwrite
        p.write_text(first + "\nADK_CC_MODEL=edited\n")
        ensure_settings_template()
        assert "edited" in p.read_text(), "ensure_settings_template overwrote an edited file"
    finally:
        del os.environ["ADK_CC_DESKTOP_DATA"]
    print("OK test_template_created_and_idempotent")


def test_fresh_template_loads_nothing() -> None:
    # A brand-new template is all-commented → dotenv_values yields no real keys,
    # so it can't shadow a repo .env / real env API key.
    d = Path(_TMP) / "fresh.env"
    os.environ["ADK_CC_SETTINGS_FILE"] = str(d)
    try:
        ensure_settings_template()
        vals = {k: v for k, v in dotenv_values(d).items() if v is not None}
        assert vals == {}, f"fresh template set values: {vals}"
    finally:
        del os.environ["ADK_CC_SETTINGS_FILE"]
    print("OK test_fresh_template_loads_nothing")


def test_bootstrap_loads_filled_settings_end_to_end() -> None:
    # Write a filled settings.env with a distinctive marker, then import adk_cc
    # in a subprocess with ADK_CC_SETTINGS_FILE pointing at it — the dotenv
    # bootstrap must load it into the environment.
    sf = Path(_TMP) / "filled.env"
    sf.write_text("ADK_CC_TEST_MARKER=from-settings\n")
    env = dict(os.environ)
    env["ADK_CC_SETTINGS_FILE"] = str(sf)
    out = subprocess.run(
        [sys.executable, "-c", "import adk_cc, os; print(os.environ.get('ADK_CC_TEST_MARKER'))"],
        cwd=str(_REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    got = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else ""
    assert got == "from-settings", f"marker not loaded (stdout={out.stdout!r} stderr={out.stderr[-400:]!r})"
    print("OK test_bootstrap_loads_filled_settings_end_to_end")


def main() -> None:
    test_settings_env_path_resolution()
    test_template_created_and_idempotent()
    test_fresh_template_loads_nothing()
    test_bootstrap_loads_filled_settings_end_to_end()
    print("\nall desktop settings-file tests passed")


if __name__ == "__main__":
    main()
