"""Tests for WorkspaceHintPlugin (plugins/workspace_hint.py).

The plugin appends the resolved workspace directory to FS/exec tool
declaration descriptions on before_model, so the model is told its working
directory. Covers: injection into path tools, NON-path tools left alone,
workspace read from state (per-session) vs ADK_CC_WORKSPACE_ROOT fallback,
idempotency, and the disable env. Hand-rolled.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.adk.models.llm_request import LlmRequest

from adk_cc.plugins.workspace_hint import WorkspaceHintPlugin, _MARKER, _in_context_cwd
from adk_cc.tools import GrepTool, ReadFileTool, WebFetchTool, WriteFileTool


def _request_with(*tools) -> LlmRequest:
    req = LlmRequest()
    req.append_tools(list(tools))
    return req


def _decl(req: LlmRequest, name: str):
    for t in req.config.tools or []:
        for d in getattr(t, "function_declarations", []) or []:
            if d.name == name:
                return d
    raise AssertionError(f"declaration {name!r} not found")


def _run(plugin, req, state=None):
    cc = SimpleNamespace(state=state or {}, session=None)
    asyncio.run(plugin.before_model_callback(callback_context=cc, llm_request=req))


def test_injects_workspace_into_path_tools_from_state():
    os.environ.pop("ADK_CC_DISABLE_WORKSPACE_HINT", None)
    plugin = WorkspaceHintPlugin()
    req = _request_with(ReadFileTool(), WriteFileTool(), GrepTool(), WebFetchTool())
    state = {"temp:sandbox_workspace": SimpleNamespace(abs_path="/work/acme/alice")}
    _run(plugin, req, state)
    for name in ("read_file", "write_file", "grep"):
        desc = _decl(req, name).description or ""
        assert _MARKER in desc and "/work/acme/alice" in desc, (name, desc[-120:])
    # non-path tool (web_fetch) is untouched
    assert _MARKER not in (_decl(req, "web_fetch").description or "")
    print("OK injects_workspace_into_path_tools_from_state")


def test_falls_back_to_env_then_cwd():
    plugin = WorkspaceHintPlugin()
    os.environ["ADK_CC_WORKSPACE_ROOT"] = "/tmp/ws-env"
    try:
        req = _request_with(ReadFileTool())
        _run(plugin, req, state={})  # no workspace in state → env
        assert os.path.abspath("/tmp/ws-env") in (_decl(req, "read_file").description or "")
    finally:
        os.environ.pop("ADK_CC_WORKSPACE_ROOT", None)
    # no state, no env → CWD (still injects something non-empty)
    req2 = _request_with(ReadFileTool())
    _run(plugin, req2, state={})
    d = _decl(req2, "read_file").description or ""
    assert _MARKER in d and os.path.abspath(os.getcwd()) in d
    print("OK falls_back_to_env_then_cwd")


def test_idempotent_within_request():
    plugin = WorkspaceHintPlugin()
    req = _request_with(ReadFileTool())
    state = {"temp:sandbox_workspace": {"abs_path": "/w"}}  # dict form too
    _run(plugin, req, state)
    _run(plugin, req, state)  # second pass on same declarations
    assert (_decl(req, "read_file").description or "").count(_MARKER) == 1
    print("OK idempotent_within_request")


def test_disabled_via_env():
    os.environ["ADK_CC_DISABLE_WORKSPACE_HINT"] = "1"
    try:
        plugin = WorkspaceHintPlugin()
        req = _request_with(ReadFileTool())
        _run(plugin, req, {"temp:sandbox_workspace": {"abs_path": "/w"}})
        assert _MARKER not in (_decl(req, "read_file").description or "")
    finally:
        os.environ.pop("ADK_CC_DISABLE_WORKSPACE_HINT", None)
    print("OK disabled_via_env")


class _FakeDockerBackend:
    """Stand-in: reports /workspace as its in-context cwd, like DockerBackend."""
    def container_cwd(self, host_abs_path):
        return "/workspace"


def test_backends_report_in_context_cwd():
    # Each backend maps the host workspace to the path the model actually sees.
    from adk_cc.sandbox.backends.noop_backend import NoopBackend
    from adk_cc.sandbox.backends.docker_backend import DockerBackend, CONTAINER_WORKSPACE
    from adk_cc.sandbox.backends.sandbox_service_backend import SandboxServiceBackend
    from adk_cc.sandbox.backends.daytona_backend import DaytonaBackend
    host = "/srv/ws/acme/alice"
    assert NoopBackend().container_cwd(host) == host  # host-exec → identity
    # __new__ bypasses __init__ (no daemon); container_cwd is stateless.
    assert object.__new__(DockerBackend).container_cwd(host) == CONTAINER_WORKSPACE == "/workspace"
    assert object.__new__(SandboxServiceBackend).container_cwd(host) == "/workspace"
    d = object.__new__(DaytonaBackend)
    d._workspace_path = "/home/daytona"
    assert d.container_cwd(host) == "/home/daytona"
    print("OK backends_report_in_context_cwd")


def test_hint_uses_container_path_under_sandbox():
    plugin = WorkspaceHintPlugin()
    req = _request_with(ReadFileTool())
    state = {
        "temp:sandbox_workspace": {"abs_path": "/work/acme/alice"},
        "temp:sandbox_backend": _FakeDockerBackend(),
    }
    _run(plugin, req, state)
    desc = _decl(req, "read_file").description or ""
    assert "/workspace" in desc, desc[-160:]       # container path surfaced
    assert "/work/acme/alice" not in desc           # NOT the host path
    print("OK hint_uses_container_path_under_sandbox")


def test_in_context_cwd_fallbacks():
    host = {"abs_path": "/h/acme/alice"}
    # noop-shaped backend (identity) → host
    cc = SimpleNamespace(state={"temp:sandbox_workspace": host,
                                "temp:sandbox_backend": _FakeDockerBackend()}, session=None)
    assert _in_context_cwd(cc) == "/workspace"
    # no backend seeded → host path
    cc2 = SimpleNamespace(state={"temp:sandbox_workspace": host}, session=None)
    assert _in_context_cwd(cc2) == "/h/acme/alice"
    print("OK in_context_cwd_fallbacks")


def test_skill_resource_contract_present():
    import inspect
    from adk_cc.tools import skills
    src = inspect.getsource(skills._LenientLoadSkillResourceTool.__init__)
    assert "NOT in your workspace" in src and "by skill name" in src
    print("OK skill_resource_contract_present")


def main():
    test_injects_workspace_into_path_tools_from_state()
    test_falls_back_to_env_then_cwd()
    test_idempotent_within_request()
    test_disabled_via_env()
    test_backends_report_in_context_cwd()
    test_hint_uses_container_path_under_sandbox()
    test_in_context_cwd_fallbacks()
    test_skill_resource_contract_present()
    print("\nall workspace-hint tests passed")


if __name__ == "__main__":
    main()
