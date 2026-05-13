"""Unit tests for `ProjectContextPlugin`.

Covers:
  - Discovery: CLAUDE.md / .adk-cc/CONTEXT.md in cwd; walking up
    to parent dirs; user-level files.
  - Multi-tenant: TenantContext in session state → tenant-scoped
    paths read; missing TenantContext → silently skipped.
  - Operator extras: ADK_CC_CONTEXT_FILES adds absolute paths;
    non-absolute entries are silently dropped with a warning.
  - Opt-out: ADK_CC_DISABLE_PROJECT_CONTEXT=1 → plugin no-ops on
    every turn (no audit event, no system_instruction mutation).
  - Size cap: file larger than max_bytes is loaded truncated with
    a marker.
  - Missing / empty files: silently skipped.
  - Cache + hot reload: same mtime → cache hit (no re-read);
    mtime drift → re-read + new audit event.
  - Prepend behavior: existing system_instruction is preserved
    AFTER the context block; None / str / Part / list[Part] all
    handled.
  - Audit emit shape: project_context_loaded event has the
    documented field set.

Run: `.venv/bin/python tests/test_project_context.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.plugins.audit import clear_global_sink, is_audit_enabled, set_global_sink
from adk_cc.plugins.project_context import (
    ProjectContextPlugin,
    _parse_extra_paths,
    _truncate,
)
from google.adk.models.llm_request import LlmRequest
from google.genai import types


# --- Fakes ---------------------------------------------------------


class _FakeState:
    """Minimal stand-in supporting .get() and __setitem__."""

    def __init__(self, data: Optional[dict] = None) -> None:
        self._d: dict = dict(data or {})

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __getitem__(self, k):
        return self._d[k]

    def __setitem__(self, k, v):
        self._d[k] = v


class _FakeSession:
    def __init__(self, sid: str = "sess-test", state: Optional[dict] = None) -> None:
        self.id = sid
        self.state = _FakeState(state)


class _FakeCallbackContext:
    def __init__(self, sid: str = "sess-test", state: Optional[dict] = None) -> None:
        self._session = _FakeSession(sid, state)

    @property
    def session(self):
        return self._session


def _build_req(system_instruction: Any = None) -> LlmRequest:
    return LlmRequest(
        model="openai/gpt-4",
        contents=[],
        config=types.GenerateContentConfig(system_instruction=system_instruction),
    )


def _capture_audit() -> tuple[list[dict], callable]:
    events: list[dict] = []
    set_global_sink(lambda e: events.append(e))
    return events, lambda: clear_global_sink()


def _run(plugin: ProjectContextPlugin, ctx: _FakeCallbackContext, req: LlmRequest) -> None:
    asyncio.run(
        plugin.before_model_callback(callback_context=ctx, llm_request=req)
    )


# --- Discovery ----------------------------------------------------


def test_claude_md_in_cwd_is_loaded() -> None:
    """CLAUDE.md in the current working directory is discovered and
    prepended to the system_instruction."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("project conventions: use uv")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req("original system text")
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
        si = req.config.system_instruction
        assert isinstance(si, str)
        assert "project conventions: use uv" in si
        assert "original system text" in si
        # Project content appears FIRST (prepend, not append).
        assert si.index("project conventions") < si.index("original system text")
    print("OK test_claude_md_in_cwd_is_loaded")


def test_adk_cc_context_md_in_cwd_is_loaded() -> None:
    """`.adk-cc/CONTEXT.md` is the adk-cc-namespaced alternative."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".adk-cc").mkdir()
        (Path(tmp) / ".adk-cc" / "CONTEXT.md").write_text("adk-cc-specific notes")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req()
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
        assert "adk-cc-specific notes" in req.config.system_instruction
    print("OK test_adk_cc_context_md_in_cwd_is_loaded")


def test_walks_up_to_parent_dirs() -> None:
    """A CLAUDE.md in a parent dir is found by walking up from cwd."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        (root / "CLAUDE.md").write_text("parent dir conventions")
        sub = root / "sub" / "nested"
        sub.mkdir(parents=True)
        old_cwd = os.getcwd()
        os.chdir(sub)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req()
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
        assert "parent dir conventions" in req.config.system_instruction
    print("OK test_walks_up_to_parent_dirs")


def test_both_files_in_same_dir_both_loaded() -> None:
    """CLAUDE.md AND .adk-cc/CONTEXT.md in the same dir → both loaded,
    CLAUDE.md first (per `_PROJECT_FILENAMES` order)."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("CLAUDE-named content")
        (Path(tmp) / ".adk-cc").mkdir()
        (Path(tmp) / ".adk-cc" / "CONTEXT.md").write_text("ADK-CC-named content")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req()
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
        si = req.config.system_instruction
        assert "CLAUDE-named content" in si
        assert "ADK-CC-named content" in si
        assert si.index("CLAUDE-named content") < si.index("ADK-CC-named content")
    print("OK test_both_files_in_same_dir_both_loaded")


# --- Multi-tenant -------------------------------------------------


def test_tenant_context_paths_resolved() -> None:
    """When `temp:tenant_context` is present in session state, the
    plugin reads <tenant_workspace>/CONTEXT.md and
    <tenant_workspace>/<user_id>/CONTEXT.md."""
    with tempfile.TemporaryDirectory() as tenant_root:
        (Path(tenant_root) / "CONTEXT.md").write_text("tenant-wide notes")
        user_dir = Path(tenant_root) / "alice"
        user_dir.mkdir()
        (user_dir / "CONTEXT.md").write_text("user-specific notes")

        class _TC:
            workspace_root_path = tenant_root
            user_id = "alice"
            tenant_id = "acme"

        state = {"temp:tenant_context": _TC()}
        plugin = ProjectContextPlugin()
        req = _build_req()
        _run(plugin, _FakeCallbackContext(state=state), req)
        si = req.config.system_instruction
        assert "tenant-wide notes" in si
        assert "user-specific notes" in si
    print("OK test_tenant_context_paths_resolved")


def test_no_tenant_context_skips_tenant_paths_silently() -> None:
    """Local-CLI sessions have no `temp:tenant_context` — tenant paths
    are silently skipped without error."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("project only")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req()
            # state={} → no temp:tenant_context.
            _run(plugin, _FakeCallbackContext(state={}), req)
        finally:
            os.chdir(old_cwd)
        # Project file loaded fine despite missing tenant context.
        assert "project only" in req.config.system_instruction
    print("OK test_no_tenant_context_skips_tenant_paths_silently")


# --- Operator extras ----------------------------------------------


def test_operator_extras_appended() -> None:
    """ADK_CC_CONTEXT_FILES paths are loaded after discovery."""
    with tempfile.TemporaryDirectory() as tmp:
        extra1 = Path(tmp) / "extra1.md"
        extra1.write_text("operator-supplied context A")
        extra2 = Path(tmp) / "extra2.md"
        extra2.write_text("operator-supplied context B")
        os.environ["ADK_CC_CONTEXT_FILES"] = f"{extra1},{extra2}"
        try:
            plugin = ProjectContextPlugin()
            req = _build_req()
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            del os.environ["ADK_CC_CONTEXT_FILES"]
        si = req.config.system_instruction
        assert "operator-supplied context A" in si
        assert "operator-supplied context B" in si
        # Order preserved.
        assert si.index("context A") < si.index("context B")
    print("OK test_operator_extras_appended")


def test_relative_extra_paths_dropped() -> None:
    """Non-absolute ADK_CC_CONTEXT_FILES entries are dropped silently
    (logged WARN). Operator typo doesn't crash the agent."""
    parsed = _parse_extra_paths("./not-absolute.md,/abs/path.md")
    assert len(parsed) == 1
    assert parsed[0] == Path("/abs/path.md")
    print("OK test_relative_extra_paths_dropped")


# --- Opt-out ------------------------------------------------------


def test_disabled_via_env_no_op() -> None:
    """ADK_CC_DISABLE_PROJECT_CONTEXT=1 → plugin no-ops. No
    system_instruction change, no audit emit."""
    events, cleanup = _capture_audit()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("should not be loaded")
        os.environ["ADK_CC_DISABLE_PROJECT_CONTEXT"] = "1"
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req("untouched")
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
            del os.environ["ADK_CC_DISABLE_PROJECT_CONTEXT"]
            cleanup()
    assert req.config.system_instruction == "untouched"
    assert events == []
    print("OK test_disabled_via_env_no_op")


# --- Size cap -----------------------------------------------------


def test_size_cap_truncates_with_marker() -> None:
    """File larger than max_bytes is loaded truncated; marker
    appended so the model knows it's a partial file."""
    big = "x" * 1000
    cut, was_truncated = _truncate(big, 200)
    assert was_truncated
    assert len(cut.encode("utf-8")) <= 200 + len("\n\n(... truncated by ProjectContextPlugin ...)")
    assert "truncated" in cut

    small = "y" * 50
    cut2, was_trunc2 = _truncate(small, 200)
    assert not was_trunc2
    assert cut2 == small
    print("OK test_size_cap_truncates_with_marker")


def test_max_bytes_env_applied_to_file_load() -> None:
    """End-to-end: a large file is loaded into the prompt truncated
    when ADK_CC_CONTEXT_MAX_BYTES caps it."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("x" * 5000)
        os.environ["ADK_CC_CONTEXT_MAX_BYTES"] = "500"
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req()
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
            del os.environ["ADK_CC_CONTEXT_MAX_BYTES"]
        si = req.config.system_instruction
        assert "truncated by ProjectContextPlugin" in si
    print("OK test_max_bytes_env_applied_to_file_load")


# --- Missing / empty files ----------------------------------------


def test_missing_files_silently_skipped() -> None:
    """No CLAUDE.md anywhere → plugin no-ops cleanly (no error, no
    system_instruction mutation, no audit event)."""
    events, cleanup = _capture_audit()
    with tempfile.TemporaryDirectory() as tmp:
        # No files created.
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req("untouched")
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
            cleanup()
    assert req.config.system_instruction == "untouched"
    assert events == []
    print("OK test_missing_files_silently_skipped")


def test_empty_files_skipped() -> None:
    """A zero-length CLAUDE.md is treated as if absent."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req("untouched")
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
    assert req.config.system_instruction == "untouched"
    print("OK test_empty_files_skipped")


# --- Cache + hot reload -------------------------------------------


def test_cache_hit_on_second_turn() -> None:
    """Second `before_model_callback` reuses cached content (no
    second read) when mtime hasn't changed."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "CLAUDE.md"
        target.write_text("v1 content")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req1 = _build_req()
            _run(plugin, _FakeCallbackContext(), req1)
            # Wrap read_text to detect a second call.
            calls = {"n": 0}
            original = type(target).read_text

            def counting_read_text(self, *a, **kw):
                if self == target:
                    calls["n"] += 1
                return original(self, *a, **kw)

            with patch.object(Path, "read_text", counting_read_text):
                req2 = _build_req()
                _run(plugin, _FakeCallbackContext(), req2)
            assert calls["n"] == 0, "should not have re-read on cache hit"
        finally:
            os.chdir(old_cwd)
    print("OK test_cache_hit_on_second_turn")


def test_mtime_drift_triggers_reload_and_new_audit() -> None:
    """File edited mid-session → next turn re-reads + emits a new
    project_context_loaded audit event."""
    events, cleanup = _capture_audit()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "CLAUDE.md"
        target.write_text("v1 content")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req1 = _build_req()
            _run(plugin, _FakeCallbackContext(), req1)
            assert len(events) == 1, events
            # Bump the file content + mtime. `time.sleep` ensures
            # st_mtime actually moves on fast filesystems.
            time.sleep(0.01)
            target.write_text("v2 content")
            os.utime(target, (time.time() + 1, time.time() + 1))
            req2 = _build_req()
            _run(plugin, _FakeCallbackContext(), req2)
            # New event fired because mtime drifted.
            assert len(events) == 2, events
            assert "v2 content" in req2.config.system_instruction
        finally:
            os.chdir(old_cwd)
            cleanup()
    print("OK test_mtime_drift_triggers_reload_and_new_audit")


# --- Prepend behavior ---------------------------------------------


def test_prepend_with_none_system_instruction() -> None:
    """system_instruction starts as None → assigned to just the
    context block."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("conventions")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req(None)
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
        si = req.config.system_instruction
        assert isinstance(si, str)
        assert "conventions" in si
    print("OK test_prepend_with_none_system_instruction")


def test_prepend_preserves_existing_str() -> None:
    """Existing string system_instruction lands AFTER the context
    block."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("prepend-me")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req("original-text")
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
        si = req.config.system_instruction
        assert si.index("prepend-me") < si.index("original-text")
    print("OK test_prepend_preserves_existing_str")


# --- Audit emit shape ---------------------------------------------


def test_audit_event_shape() -> None:
    """`project_context_loaded` event carries `sources`,
    `total_bytes`, `ts`, plus ctx fields (session_id from the fake
    session)."""
    events, cleanup = _capture_audit()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "CLAUDE.md").write_text("hello world")
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req()
            _run(plugin, _FakeCallbackContext("sess-audit"), req)
        finally:
            os.chdir(old_cwd)
            cleanup()
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "project_context_loaded"
    assert isinstance(e["ts"], float)
    assert e["total_bytes"] > 0
    assert isinstance(e["sources"], list)
    assert len(e["sources"]) == 1
    src = e["sources"][0]
    assert "path" in src
    assert "bytes" in src
    assert "mtime" in src
    # Ctx field from the fake session.
    assert e.get("session_id") == "sess-audit"
    print("OK test_audit_event_shape")


def test_no_emit_when_no_files_found() -> None:
    """Empty load → no audit event. Operators without a CLAUDE.md
    don't get noisy JSONL entries every turn."""
    events, cleanup = _capture_audit()
    with tempfile.TemporaryDirectory() as tmp:
        old_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            plugin = ProjectContextPlugin()
            req = _build_req()
            _run(plugin, _FakeCallbackContext(), req)
        finally:
            os.chdir(old_cwd)
            cleanup()
    assert events == []
    print("OK test_no_emit_when_no_files_found")


# --- Driver -------------------------------------------------------


def main() -> None:
    test_claude_md_in_cwd_is_loaded()
    test_adk_cc_context_md_in_cwd_is_loaded()
    test_walks_up_to_parent_dirs()
    test_both_files_in_same_dir_both_loaded()
    test_tenant_context_paths_resolved()
    test_no_tenant_context_skips_tenant_paths_silently()
    test_operator_extras_appended()
    test_relative_extra_paths_dropped()
    test_disabled_via_env_no_op()
    test_size_cap_truncates_with_marker()
    test_max_bytes_env_applied_to_file_load()
    test_missing_files_silently_skipped()
    test_empty_files_skipped()
    test_cache_hit_on_second_turn()
    test_mtime_drift_triggers_reload_and_new_audit()
    test_prepend_with_none_system_instruction()
    test_prepend_preserves_existing_str()
    test_audit_event_shape()
    test_no_emit_when_no_files_found()
    print("\nall project-context tests passed")


if __name__ == "__main__":
    main()
