"""uv-managed Python environment for code execution (W1 of the skills program).

Why this exists: `SandboxBackedCodeExecutor` used to run `python3 <file>`.
Inside `NoopBackend` (desktop) that resolves to whatever the host ships — on a
stock macOS that is `/usr/bin/python3` = **Python 3.9.6 with no third-party
packages at all**. Every analysis skill (pandas, plotly, sklearn, …) failed on
its first import, and the failure looked like a skill bug rather than a missing
runtime.

The fix is to never invoke a bare interpreter. `uv` supplies BOTH the
interpreter (`uv python install`) and the packages (`uv pip install`), so
neither depends on what the host happens to have.

Design notes:

* **Provisioned inside the backend, not on the agent host.** All work goes
  through `backend.exec`, so the same code path serves Noop (desktop), Docker,
  SSH and the remote sandboxes — and the sandbox boundary is preserved.
* **Workspace-local.** The env lives at `.adk-cc/analysis-env/` relative to the
  workspace root, so it persists per project and is translated correctly by
  backends that remap paths (commands are opaque strings — always pass the
  RELATIVE interpreter path, never an agent-side absolute one).
* **Tiered.** A base env (interpreter only) is cheap and always safe; heavy
  tiers install only when the code actually imports something from them, so a
  trivial script never pays for xgboost.
* **Cached twice.** A marker file records the installed tier set for the
  process-independent case, and an in-process cache avoids re-probing on every
  call within a session.
"""

from __future__ import annotations

import hashlib
import logging
import os

from ..config.schema import as_int
import re
import shlex
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable, Optional

from .backends.base import NetworkConfig, SandboxBackend
from .workspace import WorkspaceRoot

_log = logging.getLogger(__name__)

# Env dir, relative to the workspace root (see module docstring on why).
_ENV_REL = ".adk-cc/analysis-env"
_MARKER_REL = f"{_ENV_REL}/.adk-cc-tiers"
# Written while an install is in flight and removed after, so the UI can say
# "provisioning" instead of showing a silent 20-60s stall. Provisioning is
# triggered by whatever needs the env first — often a TURN, not the UI — so a
# request-scoped spinner would miss it; the state has to live in the workspace.
# NOTE the path: OUTSIDE the env dir. Writing the sentinel inside it created
# `.adk-cc/analysis-env/` before `uv venv` ran, and uv refuses to build into an
# existing directory ("Use --clear ... to replace it") — so the sentinel broke
# the very provisioning it was reporting on.
_BUSY_REL = ".adk-cc/.analysis-env-provisioning"
# The LAST provisioning failure, kept until a later attempt succeeds. Without
# it a failed provision is indistinguishable from a fresh project: both leave no
# interpreter and no marker, so the UI said "not built yet" for "uv is missing"
# — the exact case the status chip exists to surface.
_ERROR_REL = ".adk-cc/.analysis-env-error"

_DEFAULT_PYTHON = "3.12"

# Package tiers. "base" is the interpreter alone — always available, no install.
TIERS: dict[str, tuple[str, ...]] = {
    # openpyxl: pandas cannot open an .xlsx without it, and spreadsheets are
    # the most common thing a non-engineer hands this agent.
    "core": ("pandas>=2.3", "numpy", "scipy", "pyarrow", "matplotlib", "plotly",
             "openpyxl"),
    "modeling": ("scikit-learn", "xgboost", "shap"),
    "stats": ("statsmodels", "ruptures", "dowhy"),
}

# Import name -> tier that provides it. Used to escalate on demand.
_IMPORT_TIER: dict[str, str] = {
    "pandas": "core", "numpy": "core", "scipy": "core", "pyarrow": "core",
    "matplotlib": "core", "plotly": "core", "seaborn": "core",
    "sklearn": "modeling", "xgboost": "modeling", "shap": "modeling",
    "lightgbm": "modeling",
    "statsmodels": "stats", "ruptures": "stats", "dowhy": "stats",
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+([A-Za-z_][\w.]*)|from\s+([A-Za-z_][\w.]*)\s+import)",
    re.M,
)


@dataclass(frozen=True)
class AnalysisEnv:
    """A resolved interpreter. `python` is WORKSPACE-RELATIVE by design."""

    python: str
    tiers: frozenset[str]
    provisioned: bool = False   # True when this call did the install work

    @property
    def is_managed(self) -> bool:
        return self.python != "python3"


class AnalysisEnvError(RuntimeError):
    """Provisioning failed. The message is user-facing — say what to do."""


def _mode() -> str:
    """`auto` (provision on demand) | `off` (legacy bare python3) | a path."""
    return (os.environ.get("ADK_CC_ANALYSIS_ENV") or "auto").strip()


def _python_version() -> str:
    return (os.environ.get("ADK_CC_ANALYSIS_PYTHON") or _DEFAULT_PYTHON).strip()


def _install_timeout_s() -> int:
    try:
        return max(60, as_int(os.environ.get("ADK_CC_ANALYSIS_INSTALL_TIMEOUT_S", "")))
    except ValueError:
        return 900


def _forced_tiers() -> frozenset[str]:
    """`ADK_CC_ANALYSIS_TIERS=core,modeling` pre-installs those tiers."""
    raw = os.environ.get("ADK_CC_ANALYSIS_TIERS", "") or ""
    want = {t.strip() for t in raw.replace(":", ",").split(",") if t.strip()}
    return frozenset(want & set(TIERS))


def required_tiers(code: str) -> frozenset[str]:
    """Tiers the code's imports demand. Conservative: only known names count,
    so an unknown import never triggers a multi-minute install."""
    found: set[str] = set()
    for m in _IMPORT_RE.finditer(code or ""):
        mod = (m.group(1) or m.group(2) or "").split(".")[0]
        tier = _IMPORT_TIER.get(mod)
        if tier:
            found.add(tier)
    return frozenset(found)


def _packages_for(tiers: Iterable[str]) -> list[str]:
    out: list[str] = []
    for t in sorted(set(tiers)):
        out.extend(TIERS.get(t, ()))
    return out


def _tier_token(tiers: Iterable[str]) -> str:
    """Stable marker content: the tier set plus the pinned interpreter."""
    body = ",".join(sorted(set(tiers))) + f"|py{_python_version()}"
    return f"{body}|{hashlib.sha256(body.encode()).hexdigest()[:12]}"


# In-process cache: (workspace, tier-token) -> AnalysisEnv already verified.
_verified: dict[tuple[str, str], AnalysisEnv] = {}


def reset_cache() -> None:
    """Test hook — forget what this process has verified."""
    _verified.clear()


async def _exec(backend: SandboxBackend, ws: WorkspaceRoot, cmd: str, *, timeout_s: int):
    return await backend.exec(
        cmd,
        fs_write=ws.fs_write_config(),
        network=NetworkConfig(),
        timeout_s=timeout_s,
        cwd=ws.abs_path,
    )


async def ensure_env(
    backend: SandboxBackend,
    ws: WorkspaceRoot,
    *,
    tiers: Iterable[str] = (),
) -> AnalysisEnv:
    """Resolve (creating if needed) the interpreter to run code with.

    Returns an `AnalysisEnv` whose `python` is a workspace-RELATIVE path.
    Raises `AnalysisEnvError` with an actionable message when provisioning is
    impossible (no `uv`, install failure) — never silently degrades to system
    python, which is the bug this module exists to prevent.
    """
    mode = _mode()
    if mode == "off":
        return AnalysisEnv(python="python3", tiers=frozenset())
    if mode not in ("auto",):
        # Explicit interpreter path supplied by the operator.
        return AnalysisEnv(python=mode, tiers=frozenset())

    want = frozenset(tiers) | _forced_tiers()
    token = _tier_token(want)
    key = (ws.abs_path, token)
    cached = _verified.get(key)
    if cached is not None:
        return cached

    py_rel = f"{_ENV_REL}/bin/python"
    marker = shlex.quote(_MARKER_REL)
    # One round trip for the fast path: does the interpreter exist AND does the
    # marker already record (at least) the tiers we need?
    # Two bits in one round trip: does the interpreter exist, and what tiers
    # does the marker record? They differ — an env can exist with FEWER tiers
    # than we need, and then venv creation must be skipped (uv refuses to
    # recreate one) while the package delta is still installed.
    probe = (
        f"test -x {shlex.quote(py_rel)} && echo __PY_OK__; "
        f"cat {marker} 2>/dev/null || true"
    )
    res = await _exec(backend, ws, probe, timeout_s=30)
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
    venv_exists = any(ln.strip() == "__PY_OK__" for ln in lines)
    have_token = next((ln.strip() for ln in lines if ln.strip() != "__PY_OK__"), "")
    already: frozenset[str] = frozenset()
    if have_token:
        have_tiers = frozenset(
            t for t in have_token.split("|")[0].split(",") if t
        )
        if want <= have_tiers and have_token.endswith(_tier_token(have_tiers).split("|")[-1]):
            env = AnalysisEnv(python=py_rel, tiers=have_tiers)
            _verified[key] = env
            return env
        # Existing env, missing a tier → install ONLY the delta below.
        already = have_tiers
        want = want | have_tiers

    await _provision(backend, ws, py_rel, want, venv_exists=venv_exists,
                     already=already)
    env = AnalysisEnv(python=py_rel, tiers=want, provisioned=True)
    _verified[(ws.abs_path, _tier_token(want))] = env
    _verified[key] = env
    return env


async def _record_error(backend: SandboxBackend, ws: WorkspaceRoot, msg: str) -> None:
    """Persist why provisioning failed, for `status()` to report."""
    try:
        await backend.write_text(
            os.path.join(ws.abs_path, _ERROR_REL),
            (msg or "provisioning failed").strip()[:2000],
            fs_write=ws.fs_write_config(),
        )
    except Exception:  # noqa: BLE001 — never mask the real failure
        pass


async def _clear_error(backend: SandboxBackend, ws: WorkspaceRoot) -> None:
    try:
        await _exec(backend, ws, f"rm -f {shlex.quote(_ERROR_REL)}", timeout_s=15)
    except Exception:  # noqa: BLE001
        pass


async def _clear_busy(
    backend: SandboxBackend, ws: WorkspaceRoot, token: str = ""
) -> None:
    """Drop the provisioning sentinel — but only if we wrote it.

    `token=""` keeps the old unconditional behaviour for callers that never
    wrote one. With a token, a concurrent provision's sentinel survives, so the
    chip keeps reporting the install that is still running. Best-effort
    throughout: a stale sentinel only mislabels a chip, and `status()` ages it
    out anyway.
    """
    q = shlex.quote(_BUSY_REL)
    cmd = (
        f"rm -f {q}" if not token
        else f'if [ -f {q} ] && grep -q {shlex.quote(token)} {q}; then rm -f {q}; fi'
    )
    try:
        await _exec(backend, ws, cmd, timeout_s=15)
    except Exception:  # noqa: BLE001
        pass


async def _provision(
    backend: SandboxBackend,
    ws: WorkspaceRoot,
    py_rel: str,
    tiers: frozenset[str],
    *,
    venv_exists: bool = False,
    already: frozenset[str] = frozenset(),
) -> None:
    """Create the env (or EXTEND an existing one) and install `tiers`.

    `venv_exists` matters: `uv venv` refuses to touch an existing environment
    ("Use --clear to replace it"), and --clear would throw away tiers already
    installed. Escalating from core→modeling must therefore skip creation and
    install only the delta.
    """
    check = await _exec(backend, ws, "command -v uv || true", timeout_s=30)
    if not (check.stdout or "").strip():
        await _record_error(backend, ws, "uv is not available in the execution environment")
        raise AnalysisEnvError(
            "uv is not available in the execution environment, so a managed "
            "Python cannot be provisioned. Install it "
            "(https://docs.astral.sh/uv/ — `brew install uv` or "
            "`curl -LsSf https://astral.sh/uv/install.sh | sh`), or set "
            "ADK_CC_ANALYSIS_ENV=<path-to-python> to use an existing "
            "interpreter. Set ADK_CC_ANALYSIS_ENV=off to fall back to bare "
            "`python3` (not recommended: on stock macOS that is Python 3.9 "
            "with no data packages)."
        )

    pyver = _python_version()
    steps = []
    if not venv_exists:
        # `uv venv` downloads the pinned interpreter itself when missing, so the
        # host's own python version is irrelevant.
        steps.append(
            (f"uv venv --python {shlex.quote(pyver)} {shlex.quote(_ENV_REL)}",
             f"create a Python {pyver} virtualenv")
        )
    # Install only the DELTA. Re-passing already-installed tiers over-constrains
    # the resolver: asking for pandas+numpy+shap in one shot made uv pick
    # numba 0.53.1 (supports only Python <3.10) and the build failed, whereas
    # resolving just {scikit-learn, xgboost, shap} against the installed set
    # picks a modern numba. Escalation means "add packages", not "re-solve
    # everything".
    pkgs = _packages_for(set(tiers) - set(already))
    if pkgs:
        steps.append((
            "uv pip install --quiet "
            f"--python {shlex.quote(py_rel)} " + " ".join(shlex.quote(p) for p in pkgs),
            f"install the {', '.join(sorted(set(tiers) - set(already)))} package tier(s)",
        ))

    # OWNED sentinel: two sessions in the same project can both reach
    # ensure_env, and an unowned marker let whichever finished FIRST clear it
    # while the other install was still running — the chip flipped to "ready"
    # mid-install. The writer stamps a token and only removes its own.
    busy = os.path.join(ws.abs_path, _BUSY_REL)
    token = uuid.uuid4().hex[:12]
    if steps:
        try:
            await backend.write_text(
                busy,
                f"{token} {','.join(sorted(set(tiers) - set(already))) or 'base'}",
                fs_write=ws.fs_write_config(),
            )
        except Exception:  # noqa: BLE001 — a status hint must never block work
            pass

    for cmd, what in steps:
        _log.info("analysis env: %s", what)
        try:
            res = await _exec(backend, ws, cmd, timeout_s=_install_timeout_s())
        except BaseException:
            await _clear_busy(backend, ws, token)
            raise
        if res.exit_code != 0:
            await _clear_busy(backend, ws, token)
            tail = ((res.stderr or res.stdout or "").strip() or "(no output)")[-1500:]
            await _record_error(backend, ws, f"failed to {what}: {tail[-400:]}")
            raise AnalysisEnvError(
                f"Failed to {what} for the analysis environment.\n\n{tail}\n\n"
                "Retry, or set ADK_CC_ANALYSIS_ENV=<path-to-python> to use an "
                "interpreter you control."
            )

    await _clear_busy(backend, ws, token)
    await _clear_error(backend, ws)      # a success clears the last failure
    # Marker last: a crash mid-install must not look like a complete env.
    await backend.write_text(
        os.path.join(ws.abs_path, _MARKER_REL),
        _tier_token(tiers) + "\n",
        fs_write=ws.fs_write_config(),
    )
    _log.info("analysis env ready (tiers: %s)", ", ".join(sorted(tiers)) or "base")


# --- read-only status (W6.5) ------------------------------------------------

# A killed process leaves the sentinel behind. 30 minutes of "preparing…" was
# far longer than any real install, so age it out sooner and SAY it is stale
# rather than silently reverting to "not built yet".
_BUSY_STALE_S = 5 * 60


def status(workspace_root: str) -> dict:
    """What state the analysis env is in, WITHOUT provisioning anything.

    Deliberately a plain filesystem read: the UI polls this, and a status call
    that could trigger a 60s install (or block on a sandbox round trip) would be
    worse than no chip at all. Reads the same marker `ensure_env` writes, so it
    cannot drift from the truth it reports.
    """
    mode = _mode()
    if mode == "off":
        return {"state": "off", "detail": "using bare python3 (ADK_CC_ANALYSIS_ENV=off)"}
    if mode not in ("auto",):
        return {"state": "external", "python": mode,
                "detail": "operator-supplied interpreter"}

    root = Path(workspace_root)
    busy = root / _BUSY_REL
    try:
        if busy.is_file():
            import time as _time

            age = _time.time() - busy.stat().st_mtime
            body = busy.read_text().strip().split(" ", 1)
            tiers_txt = body[1] if len(body) > 1 else (body[0] if body else "")
            if age < _BUSY_STALE_S:
                return {
                    "state": "provisioning",
                    "tiers": [t for t in tiers_txt.split(",") if t],
                    "seconds": int(age),
                    "detail": "installing packages — first use in this project",
                }
            return {
                "state": "unavailable",
                "detail": (
                    f"a provisioning run started {int(age // 60)}m ago never "
                    "finished (the process was probably killed); the next "
                    "analysis will retry"
                ),
            }
    except OSError:
        pass

    marker = root / _MARKER_REL
    interpreter = root / _ENV_REL / "bin" / "python"
    err = root / _ERROR_REL
    try:
        if not interpreter.exists():
            # A FAILED provision and a fresh project look identical on disk
            # (no interpreter, no marker) — the recorded error is the only
            # thing that tells them apart.
            try:
                reason = err.read_text().strip() if err.is_file() else ""
            except OSError:
                reason = ""
            if reason:
                return {"state": "unavailable", "detail": reason[:400]}
            return {"state": "absent",
                    "detail": "provisions on first analysis in this project"}
        token = marker.read_text().strip() if marker.is_file() else ""
    except OSError as e:
        return {"state": "unknown", "detail": str(e)}

    tiers = [t for t in token.split("|")[0].split(",") if t] if token else []
    return {
        "state": "ready",
        "tiers": tiers or ["base"],
        "python": f"{_ENV_REL}/bin/python",
        "detail": ("ready: " + ", ".join(tiers)) if tiers else "interpreter only",
    }
