"""W1: uv-managed analysis environment (sandbox/analysis_env.py).

The bug this prevents: `SandboxBackedCodeExecutor` ran `python3 <file>`, which
under NoopBackend is the HOST interpreter — on stock macOS Python 3.9.6 with no
third-party packages, so every analysis skill died on `import pandas` and it
looked like a skill bug.

Fast tests use a fake backend (no installs). The final test is REAL: it
provisions an env with uv and runs pandas in it — skipped only when uv is
absent, because the whole point is that we stop trusting the host.

Run: `uv run python tests/test_analysis_env.py`
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.sandbox.analysis_env import (  # noqa: E402
    AnalysisEnvError,
    ensure_env,
    required_tiers,
    reset_cache,
)
from adk_cc.sandbox.workspace import WorkspaceRoot  # noqa: E402


class _FakeResult:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code


class _FakeBackend:
    """Records commands; scripted stdout/exit per substring match."""

    def __init__(self, *, has_uv=True, marker="", fail_on=None):
        self.cmds: list[str] = []
        self.writes: dict[str, str] = {}
        self._has_uv, self._marker, self._fail_on = has_uv, marker, fail_on

    async def exec(self, cmd, **kw):
        self.cmds.append(cmd)
        if self._fail_on and self._fail_on in cmd:
            return _FakeResult(stderr="boom: resolution failed", exit_code=1)
        if "command -v uv" in cmd:
            return _FakeResult(stdout="/opt/homebrew/bin/uv\n" if self._has_uv else "")
        if cmd.startswith("test -x"):
            return _FakeResult(stdout=self._marker)
        return _FakeResult()

    async def write_text(self, path, content, **kw):
        self.writes[path] = content


def _ws(tmp):
    return WorkspaceRoot(abs_path=tmp, tenant_id="t", session_id="s")


def _clean_env():
    for k in ("ADK_CC_ANALYSIS_ENV", "ADK_CC_ANALYSIS_TIERS",
              "ADK_CC_ANALYSIS_PYTHON"):
        os.environ.pop(k, None)
    reset_cache()


def test_required_tiers_from_imports():
    assert required_tiers("import pandas as pd") == frozenset({"core"})
    assert required_tiers("from sklearn.ensemble import X") == frozenset({"modeling"})
    assert required_tiers("import statsmodels.api as sm") == frozenset({"stats"})
    assert required_tiers("import pandas\nimport shap") == frozenset({"core", "modeling"})
    # unknown imports must NOT trigger a multi-minute install
    assert required_tiers("import json, os\nimport frobnicator") == frozenset()
    assert required_tiers("") == frozenset()
    print("OK required_tiers_from_imports")


def test_provisions_and_escalates_tiers():
    _clean_env()
    with tempfile.TemporaryDirectory() as tmp:
        b = _FakeBackend()
        env = asyncio.run(ensure_env(b, _ws(tmp), tiers={"core"}))
        assert env.python == ".adk-cc/analysis-env/bin/python", env.python
        assert env.is_managed and env.provisioned
        joined = " ".join(b.cmds)
        assert "uv venv --python 3.12" in joined, joined
        assert "uv pip install" in joined and "pandas>=2.3" in joined
        # modeling packages must NOT be installed for a core-only request
        assert "xgboost" not in joined
        # marker written LAST so a crash mid-install isn't mistaken for done
        assert any(k.endswith(".adk-cc-tiers") for k in b.writes), b.writes
    print("OK provisions_and_escalates_tiers")


def test_reuses_existing_env_without_reinstalling():
    _clean_env()
    with tempfile.TemporaryDirectory() as tmp:
        b1 = _FakeBackend()
        asyncio.run(ensure_env(b1, _ws(tmp), tiers={"core"}))
        marker = next(v for k, v in b1.writes.items() if k.endswith(".adk-cc-tiers"))

        reset_cache()  # force the on-disk path, not the in-process cache
        b2 = _FakeBackend(marker=marker)
        env = asyncio.run(ensure_env(b2, _ws(tmp), tiers={"core"}))
        assert not env.provisioned, "should have reused the existing env"
        assert not any("uv pip install" in c for c in b2.cmds), b2.cmds

        # asking for a tier the marker lacks DOES reinstall (with the union)
        b3 = _FakeBackend(marker=marker)
        env3 = asyncio.run(ensure_env(b3, _ws(tmp), tiers={"modeling"}))
        assert env3.provisioned
        joined = " ".join(b3.cmds)
        assert "xgboost" in joined and "pandas>=2.3" in joined, joined
    print("OK reuses_existing_env_without_reinstalling")


def test_missing_uv_is_actionable_not_silent_fallback():
    _clean_env()
    with tempfile.TemporaryDirectory() as tmp:
        b = _FakeBackend(has_uv=False)
        try:
            asyncio.run(ensure_env(b, _ws(tmp), tiers={"core"}))
            raise AssertionError("expected AnalysisEnvError")
        except AnalysisEnvError as e:
            msg = str(e)
            # must tell the user how to fix it, and must NOT quietly use python3
            assert "uv" in msg and "ADK_CC_ANALYSIS_ENV" in msg, msg
            assert "install" in msg.lower()
        assert not any("uv venv" in c for c in b.cmds)
    print("OK missing_uv_is_actionable_not_silent_fallback")


def test_install_failure_surfaces_output():
    _clean_env()
    with tempfile.TemporaryDirectory() as tmp:
        b = _FakeBackend(fail_on="uv pip install")
        try:
            asyncio.run(ensure_env(b, _ws(tmp), tiers={"core"}))
            raise AssertionError("expected AnalysisEnvError")
        except AnalysisEnvError as e:
            assert "resolution failed" in str(e), str(e)
        # no marker written on failure → next run retries
        assert not any(k.endswith(".adk-cc-tiers") for k in b.writes)
    print("OK install_failure_surfaces_output")


def test_modes_off_and_explicit_path():
    _clean_env()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADK_CC_ANALYSIS_ENV"] = "off"
        reset_cache()
        env = asyncio.run(ensure_env(_FakeBackend(), _ws(tmp)))
        assert env.python == "python3" and not env.is_managed

        os.environ["ADK_CC_ANALYSIS_ENV"] = "/usr/local/bin/python3.12"
        reset_cache()
        env = asyncio.run(ensure_env(_FakeBackend(), _ws(tmp)))
        assert env.python == "/usr/local/bin/python3.12"
    _clean_env()
    print("OK modes_off_and_explicit_path")


def test_forced_tiers_from_env():
    _clean_env()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADK_CC_ANALYSIS_TIERS"] = "core,stats"
        reset_cache()
        b = _FakeBackend()
        asyncio.run(ensure_env(b, _ws(tmp)))  # no tiers requested by the code
        joined = " ".join(b.cmds)
        assert "pandas>=2.3" in joined and "statsmodels" in joined, joined
    _clean_env()
    print("OK forced_tiers_from_env")


def test_real_provisioning_and_pandas_import():
    """REAL: uv provisions the interpreter AND pandas, then runs it. This is
    the property the unit tests can only approximate — the host python is
    3.9 with nothing installed, so success here proves we stopped using it."""
    if not shutil.which("uv"):
        print("SKIP real_provisioning (uv not installed)")
        return
    _clean_env()
    from adk_cc.sandbox.backends.noop_backend import NoopBackend

    tmp = tempfile.mkdtemp(prefix="adkcc-analysis-real-")
    try:
        ws = _ws(tmp)
        backend = NoopBackend()
        env = asyncio.run(ensure_env(backend, ws, tiers={"core"}))
        assert env.is_managed, env

        code = (
            "import sys, pandas as pd\n"
            "print(sys.version.split()[0], pd.__version__)\n"
        )
        script = os.path.join(tmp, "probe.py")
        with open(script, "w") as f:
            f.write(code)
        out = subprocess.run(
            [os.path.join(tmp, ".adk-cc/analysis-env/bin/python"), script],
            capture_output=True, text=True, timeout=180,
        )
        assert out.returncode == 0, out.stderr[-500:]
        pyver, pdver = out.stdout.split()
        assert pyver.startswith("3.12"), f"expected pinned 3.12, got {pyver}"
        assert int(pdver.split(".")[0]) >= 2, pdver
        # and the host interpreter genuinely could NOT have done this
        host = subprocess.run(["/usr/bin/python3", "-c", "import pandas"],
                              capture_output=True, text=True)
        print(f"   provisioned python {pyver} + pandas {pdver}; "
              f"host /usr/bin/python3 import pandas -> rc={host.returncode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _clean_env()
    print("OK real_provisioning_and_pandas_import")


def main():
    test_required_tiers_from_imports()
    test_provisions_and_escalates_tiers()
    test_reuses_existing_env_without_reinstalling()
    test_missing_uv_is_actionable_not_silent_fallback()
    test_install_failure_surfaces_output()
    test_modes_off_and_explicit_path()
    test_forced_tiers_from_env()
    test_real_provisioning_and_pandas_import()
    print("\nall analysis-env tests passed")


if __name__ == "__main__":
    main()
