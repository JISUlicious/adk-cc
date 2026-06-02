"""Artifact tools are gated out under the noop sandbox backend.

Two layers:
  1. Listing — agent._artifacts_supported() is env-driven, so the coordinator
     omits save_as_artifact / load_artifact_to_sandbox when
     ADK_CC_SANDBOX_BACKEND is noop (the default), and includes them otherwise.
  2. Runtime guard — even if a per-session backend override resolves to noop,
     both tools refuse at call time with a clear error (never touching the
     host filesystem / hitting the base UTF-8 decode on binary bytes).

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.sandbox import (
    NoopBackend,
    default_workspace,
    is_noop_backend,
    set_backend,
    set_workspace,
)
from adk_cc.tools.load_artifact_to_sandbox import LoadArtifactToSandboxTool
from adk_cc.tools.save_as_artifact import SaveAsArtifactTool
from adk_cc.tools.schemas import LoadArtifactToSandboxArgs, SaveAsArtifactArgs


def _run(coro):
    return asyncio.run(coro)


# --- is_noop_backend helper -----------------------------------------------

def test_is_noop_backend():
    assert is_noop_backend(NoopBackend()) is True

    class _Fake:
        name = "docker"

    assert is_noop_backend(_Fake()) is False
    assert is_noop_backend(object()) is False  # no .name → not noop
    print("OK test_is_noop_backend")


# --- runtime guard --------------------------------------------------------

class _Ctx:
    def __init__(self, backend):
        self.state = {}
        set_backend(self, backend)
        set_workspace(self, default_workspace())


def test_load_refuses_under_noop():
    out = _run(
        LoadArtifactToSandboxTool()._execute(
            LoadArtifactToSandboxArgs(filename="x.bin", dest_path="x.bin"),
            _Ctx(NoopBackend()),
        )
    )
    assert out["status"] == "error", out
    assert "noop" in out["error"], out
    print("OK test_load_refuses_under_noop")


def test_save_refuses_under_noop():
    out = _run(
        SaveAsArtifactTool()._execute(
            SaveAsArtifactArgs(path="x.txt"), _Ctx(NoopBackend())
        )
    )
    assert out["status"] == "error", out
    assert "noop" in out["error"], out
    print("OK test_save_refuses_under_noop")


# --- agent listing (env-driven) -------------------------------------------

def _coordinator_tool_names(backend_env: str | None) -> set[str]:
    """Import the agent module fresh under a given ADK_CC_SANDBOX_BACKEND and
    return the coordinator's tool names."""
    import importlib
    import sys

    prev = os.environ.get("ADK_CC_SANDBOX_BACKEND")
    if backend_env is None:
        os.environ.pop("ADK_CC_SANDBOX_BACKEND", None)
    else:
        os.environ["ADK_CC_SANDBOX_BACKEND"] = backend_env
    os.environ.setdefault("ADK_CC_AGENTS_DIR", os.path.join(os.getcwd(), "agents"))
    os.environ["ADK_CC_ALLOW_NO_AUTH"] = "1"
    try:
        # Drop any cached agent module so module-load gating re-runs.
        for m in list(sys.modules):
            if m == "adk_cc.agent" or m.startswith("adk_cc.agent."):
                del sys.modules[m]
        agent = importlib.import_module("adk_cc.agent")
        agent = importlib.reload(agent)
        return {
            getattr(getattr(t, "meta", None), "name", None)
            for t in agent.root_agent.tools
        }
    finally:
        if prev is None:
            os.environ.pop("ADK_CC_SANDBOX_BACKEND", None)
        else:
            os.environ["ADK_CC_SANDBOX_BACKEND"] = prev


def test_listing_omits_under_noop_default():
    names = _coordinator_tool_names(None)  # unset → defaults to noop
    assert "save_as_artifact" not in names, names
    assert "load_artifact_to_sandbox" not in names, names
    # sanity: a normal tool is still there
    assert "read_file" in names, names
    print("OK test_listing_omits_under_noop_default")


def test_listing_includes_under_real_backend():
    names = _coordinator_tool_names("docker")
    assert "save_as_artifact" in names, names
    assert "load_artifact_to_sandbox" in names, names
    print("OK test_listing_includes_under_real_backend")


if __name__ == "__main__":
    test_is_noop_backend()
    test_load_refuses_under_noop()
    test_save_refuses_under_noop()
    test_listing_omits_under_noop_default()
    test_listing_includes_under_real_backend()
    print("\nall artifact-noop-gating tests passed")
