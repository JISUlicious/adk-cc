"""W6.1 trigger: a chart the agent writes shows up in the conversation.

The plugin turns "a tool call wrote analysis/chart.html" into a session
artifact, which is what makes ADK record `actions.artifactDelta` — the signal
the existing ArtifactChip already listens for and renders inline.

Pinned here: candidate extraction from a tool's own args/output (no directory
scan), containment inside the workspace, the dot-directory exclusion, the
size cap, and the content-hash dedupe.

Run: uv run python tests/test_analysis_artifact.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.plugins.analysis_artifact import (  # noqa: E402
    AnalysisArtifactPlugin,
    _candidates,
)

_WS = tempfile.mkdtemp(prefix="w61-")


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    agent_name = "coordinator"

    def __init__(self):
        self.state = {}


class _Backend:
    """Reads straight off disk — the noop/desktop shape."""

    async def read_bytes(self, path, fs_read=None):
        return Path(path).read_bytes()


class _Ws:
    abs_path = _WS

    def fs_read_config(self):
        return None


def _patch(monkey_saves):
    """Point the plugin at this test's workspace/backend and capture saves."""
    import adk_cc.plugins.analysis_artifact as mod
    import adk_cc.sandbox as sandbox
    import adk_cc.sandbox.workspace as wsmod
    import adk_cc.tools._fs as fs

    sandbox.get_backend = lambda ctx: _Backend()
    wsmod.get_workspace = lambda ctx: _Ws()
    fs.resolve = lambda p, ctx=None: Path(_WS) / p if not os.path.isabs(p) else Path(p)

    async def fake_save(ctx, *, filename, part, scope):
        monkey_saves.append((filename, len(part.inline_data.data), scope))
        return {"status": "ok", "filename": filename, "version": len(monkey_saves)}

    mod.save_part_as_artifact = fake_save


def _write(rel: str, body: bytes = b"<html><body>chart</body></html>") -> str:
    p = Path(_WS) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return rel


def test_candidate_extraction() -> None:
    """Paths come from the call itself — args, command line, or printed output.
    A directory scan per tool call would be the obvious implementation and the
    wrong one: it costs a sandbox round trip on every call."""
    assert _candidates("write_file", {"path": "analysis/chart.html"}, {}) == \
        ["analysis/chart.html"]
    cmd = _candidates(
        "run_bash",
        {"command": "python make_chart.py --out analysis/dashboard.html"},
        {"stdout": "wrote analysis/dashboard.html\n"},
    )
    assert cmd == ["analysis/dashboard.html"], cmd
    # a path only mentioned in stdout still counts
    assert "reports/eda.png" in _candidates("run_bash", {"command": "python eda.py"},
                                            {"stdout": "saved reports/eda.png"})
    # nothing previewable → no work at all
    assert _candidates("run_bash", {"command": "pytest -q"}, {"stdout": "3 passed"}) == []
    # a report is an OUTPUT inside analysis/, and noise everywhere else — a
    # README edit must not become an artifact chip.
    assert _candidates("write_file", {"path": "analysis/eda.md"}, {}) == ["analysis/eda.md"]
    assert _candidates("write_file", {"path": "README.md"}, {}) == []
    assert _candidates("write_file", {"path": "analysis/metrics.csv"}, {}) == ["analysis/metrics.csv"]
    print("OK candidate_extraction")


def test_registers_a_written_chart() -> None:
    saves = []
    _patch(saves)
    _write("analysis/chart.html")
    plugin = AnalysisArtifactPlugin()
    ctx = _Ctx()
    asyncio.run(plugin.after_tool_callback(
        tool=_Tool("write_file"), tool_args={"path": "analysis/chart.html"},
        tool_context=ctx, result={"status": "ok"}))
    assert [f for f, _, _ in saves] == ["chart.html"], saves
    assert saves[0][2] == "session", "must be session-scoped to produce an artifactDelta"
    print("OK registers_a_written_chart")


def test_dedupes_unchanged_content_but_not_updates() -> None:
    saves = []
    _patch(saves)
    _write("analysis/chart.html")
    plugin, ctx = AnalysisArtifactPlugin(), _Ctx()
    call = dict(tool=_Tool("write_file"), tool_args={"path": "analysis/chart.html"},
                result={"status": "ok"})
    asyncio.run(plugin.after_tool_callback(tool_context=ctx, **call))
    asyncio.run(plugin.after_tool_callback(tool_context=ctx, **call))
    assert len(saves) == 1, f"regenerating an identical chart re-stored it: {saves}"
    _write("analysis/chart.html", b"<html><body>chart v2 with more bars</body></html>")
    asyncio.run(plugin.after_tool_callback(tool_context=ctx, **call))
    assert len(saves) == 2, "an UPDATED chart must be surfaced again"
    print("OK dedupes_unchanged_content_but_not_updates")


def test_scope_guards() -> None:
    """Three ways a candidate must be refused."""
    saves = []
    _patch(saves)
    plugin, ctx = AnalysisArtifactPlugin(), _Ctx()

    # 1. dot-directory: .adk-cc/analysis-env ships matplotlib's own HTML
    #    templates — a real false positive already seen in a test harness.
    _write(".adk-cc/analysis-env/lib/single_figure.html")
    asyncio.run(plugin.after_tool_callback(
        tool=_Tool("run_bash"),
        tool_args={"command": "cat .adk-cc/analysis-env/lib/single_figure.html"},
        tool_context=ctx, result={"stdout": ".adk-cc/analysis-env/lib/single_figure.html"}))
    assert not saves, f"library file inside a dot-dir was surfaced: {saves}"

    # 2. outside the workspace root
    outside = Path(tempfile.mkdtemp(prefix="outside-")) / "secret.html"
    outside.write_bytes(b"<html>not yours</html>")
    asyncio.run(plugin.after_tool_callback(
        tool=_Tool("run_bash"), tool_args={"command": f"cat {outside}"},
        tool_context=ctx, result={"stdout": str(outside)}))
    assert not saves, f"a path outside the workspace was surfaced: {saves}"

    # 3. over the size cap
    os.environ["ADK_CC_ANALYSIS_ARTIFACT_MAX_MB"] = "0.001"
    try:
        _write("analysis/big.html", b"x" * 50_000)
        asyncio.run(plugin.after_tool_callback(
            tool=_Tool("write_file"), tool_args={"path": "analysis/big.html"},
            tool_context=ctx, result={"status": "ok"}))
        assert not saves, f"an oversized file was stored: {saves}"
    finally:
        os.environ.pop("ADK_CC_ANALYSIS_ARTIFACT_MAX_MB", None)
    print("OK scope_guards")


def test_disabled_and_non_writing_tools_cost_nothing() -> None:
    saves = []
    _patch(saves)
    _write("analysis/chart.html")
    plugin, ctx = AnalysisArtifactPlugin(), _Ctx()
    asyncio.run(plugin.after_tool_callback(
        tool=_Tool("read_file"), tool_args={"path": "analysis/chart.html"},
        tool_context=ctx, result={"status": "ok"}))
    assert not saves, "a read-only tool must not trigger registration"

    os.environ["ADK_CC_ANALYSIS_ARTIFACTS"] = "0"
    try:
        asyncio.run(plugin.after_tool_callback(
            tool=_Tool("write_file"), tool_args={"path": "analysis/chart.html"},
            tool_context=ctx, result={"status": "ok"}))
        assert not saves, "the kill switch did not disable the plugin"
    finally:
        os.environ.pop("ADK_CC_ANALYSIS_ARTIFACTS", None)
    print("OK disabled_and_non_writing_tools_cost_nothing")


def main() -> None:
    test_candidate_extraction()
    test_registers_a_written_chart()
    test_dedupes_unchanged_content_but_not_updates()
    test_scope_guards()
    test_disabled_and_non_writing_tools_cost_nothing()
    print("\nall analysis-artifact tests passed")


if __name__ == "__main__":
    main()
