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

    def __init__(self, *, has_uv=True, marker="", fail_on=None, venv_exists=None):
        self.cmds: list[str] = []
        self.writes: dict[str, str] = {}
        self._has_uv, self._marker, self._fail_on = has_uv, marker, fail_on
        # default: a marker implies the interpreter is there too
        self._venv = bool(marker) if venv_exists is None else venv_exists

    async def exec(self, cmd, **kw):
        self.cmds.append(cmd)
        if self._fail_on and self._fail_on in cmd:
            return _FakeResult(stderr="boom: resolution failed", exit_code=1)
        if "command -v uv" in cmd:
            return _FakeResult(stdout="/opt/homebrew/bin/uv\n" if self._has_uv else "")
        if cmd.startswith("test -x"):
            out = ("__PY_OK__\n" if self._venv else "") + (self._marker or "")
            return _FakeResult(stdout=out)
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
        # Pin the pinned-interpreter part, not the whole flag string — the
        # creation step also carries --clear (see the rebuild test).
        assert "uv venv" in joined and "--python 3.12" in joined, joined
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

        # asking for a tier the marker lacks installs ONLY the delta —
        # re-passing core over-constrains the resolver (see the escalation test)
        b3 = _FakeBackend(marker=marker)
        env3 = asyncio.run(ensure_env(b3, _ws(tmp), tiers={"modeling"}))
        assert env3.provisioned
        joined = " ".join(b3.cmds)
        assert "xgboost" in joined, joined
        assert "pandas>=2.3" not in joined, f"delta only, not the union: {joined}"
    print("OK reuses_existing_env_without_reinstalling")


def test_escalation_does_not_recreate_the_venv():
    """LIVE BUG (found by an escalation turn): `uv venv` refuses to touch an
    existing environment ("Use --clear to replace it"), and --clear would
    discard the tiers already installed. Escalating core -> modeling must skip
    creation and install only the delta."""
    _clean_env()
    with tempfile.TemporaryDirectory() as tmp:
        b = _FakeBackend()
        asyncio.run(ensure_env(b, _ws(tmp), tiers={"core"}))
        marker = next(v for k, v in b.writes.items() if k.endswith(".adk-cc-tiers"))

        reset_cache()
        b2 = _FakeBackend(marker=marker, venv_exists=True)
        env = asyncio.run(ensure_env(b2, _ws(tmp), tiers={"modeling"}))
        joined = " ".join(b2.cmds)
        assert "uv venv" not in joined, f"must not recreate the venv: {joined}"
        assert "uv pip install" in joined and "xgboost" in joined, joined
        # DELTA ONLY: re-solving with core made uv pick numba 0.53.1
        # (Python <3.10 only) and the build failed — observed live.
        assert "pandas>=2.3" not in joined, f"must install only the delta: {joined}"
        assert env.provisioned and {"core", "modeling"} <= set(env.tiers), env
    print("OK escalation_does_not_recreate_the_venv")


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


def test_marker_without_interpreter_rebuilds():
    """LIVE BUG: `.adk-cc/analysis-env/bin/python: No such file or directory`.

    Reported right after a sandbox image rebuild, which is the giveaway. The
    probe gathers two INDEPENDENT bits — does the interpreter exist, and what
    do the recorded tiers say — and the marker branch used to return the
    interpreter path having consulted only the second. That is fine while both
    agree and wrong the moment they don't:

        .adk-cc/ lives in the mounted workspace, so the marker survives
        anything. bin/python is a symlink into a uv-managed interpreter inside
        the RUNTIME. Rebuild the image (or move the project between backends)
        and every existing project has a marker whose interpreter is gone.

    `test -x` follows symlinks, so venv_exists was already False and correct —
    the fix is simply to stop ignoring it.

    Two failure modes are covered, because fixing only the first leaves the
    user just as stuck: the early return must not fire, AND the rebuild must
    actually succeed. The marker lives INSIDE the env dir, so a marker on disk
    guarantees the directory exists, and a plain `uv venv` refuses it with
    "Use --clear to replace it".
    """
    _clean_env()
    with tempfile.TemporaryDirectory() as tmp:
        b = _FakeBackend()
        asyncio.run(ensure_env(b, _ws(tmp), tiers={"core"}))
        marker = next(v for k, v in b.writes.items() if k.endswith(".adk-cc-tiers"))

        reset_cache()
        # The exact on-disk state after an image rebuild: tiers recorded,
        # interpreter gone.
        b2 = _FakeBackend(marker=marker, venv_exists=False)
        env = asyncio.run(ensure_env(b2, _ws(tmp), tiers={"core"}))
        joined = " ".join(b2.cmds)

        assert env.provisioned, "a marker with no interpreter must NOT be reused"
        assert "uv venv" in joined, f"must rebuild the venv: {joined}"
        assert "--clear" in joined, (
            "the env dir still exists (the marker is inside it), so uv refuses "
            f"to create without --clear: {joined}")
        # Nothing survived the wipe: this is a rebuild, not an escalation, so
        # the delta optimisation must NOT apply.
        assert "pandas>=2.3" in joined, f"must reinstall, not delta: {joined}"
        assert env.python.endswith("/bin/python"), env.python

        # A rebuild must not silently downgrade a project's capability.
        reset_cache()
        b3 = _FakeBackend(marker=marker, venv_exists=False)
        env3 = asyncio.run(ensure_env(b3, _ws(tmp), tiers={"modeling"}))
        assert {"core", "modeling"} <= set(env3.tiers), (
            f"recorded tiers must survive a rebuild: {env3.tiers}")
        j3 = " ".join(b3.cmds)
        assert "xgboost" in j3 and "pandas>=2.3" in j3, j3
    print("OK marker_without_interpreter_rebuilds")


def test_real_broken_env_self_heals():
    """REAL: break a genuine venv the way an image rebuild does, then recover.

    The unit test above proves the BRANCH is taken. It cannot prove that
    `uv venv --clear` actually succeeds over a venv whose interpreter symlink
    dangles — and that is the whole fix. So this provisions for real, points
    the interpreter symlink at a path that no longer exists (what a rebuilt
    sandbox image leaves behind: `.adk-cc/` persists in the mounted workspace,
    the uv-managed interpreter inside the container does not), and requires
    ensure_env to hand back an interpreter that RUNS.
    """
    if not shutil.which("uv"):
        print("SKIP real_broken_env_self_heals (uv not installed)")
        return
    _clean_env()
    from adk_cc.sandbox.backends.noop_backend import NoopBackend

    tmp = tempfile.mkdtemp(prefix="adkcc-analysis-broken-")
    try:
        ws, backend = _ws(tmp), NoopBackend()
        # base tier only — this is about the interpreter, not the packages.
        env = asyncio.run(ensure_env(backend, ws, tiers=set()))
        interp = os.path.join(tmp, env.python)
        assert os.path.exists(interp), interp

        env_bin = os.path.join(tmp, ".adk-cc/analysis-env/bin")
        for name in os.listdir(env_bin):
            if name.startswith("python"):
                p = os.path.join(env_bin, name)
                os.unlink(p)
                os.symlink("/nonexistent/uv/python/gone/bin/python3", p)
        marker = os.path.join(tmp, ".adk-cc/analysis-env/.adk-cc-tiers")
        assert os.path.isfile(marker), "marker must survive — that IS the bug"
        assert not os.path.exists(interp), "interpreter must be gone"

        reset_cache()      # a fresh process, as after a restart
        env2 = asyncio.run(ensure_env(backend, ws, tiers=set()))
        interp2 = os.path.join(tmp, env2.python)
        assert env2.provisioned, "must rebuild, not reuse the dead marker"
        out = subprocess.run([interp2, "-c", "import sys; print(sys.version)"],
                             capture_output=True, text=True, timeout=180)
        assert out.returncode == 0, (
            f"the recovered interpreter does not run: {out.stderr[-300:]}")
        print(f"   broken env recovered; interpreter runs "
              f"{out.stdout.split()[0]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _clean_env()
    print("OK real_broken_env_self_heals")


def main():
    test_required_tiers_from_imports()
    test_provisions_and_escalates_tiers()
    test_reuses_existing_env_without_reinstalling()
    test_escalation_does_not_recreate_the_venv()
    test_marker_without_interpreter_rebuilds()
    test_missing_uv_is_actionable_not_silent_fallback()
    test_install_failure_surfaces_output()
    test_modes_off_and_explicit_path()
    test_forced_tiers_from_env()
    test_real_provisioning_and_pandas_import()
    test_real_broken_env_self_heals()
    print("\nall analysis-env tests passed")


if __name__ == "__main__":
    main()
