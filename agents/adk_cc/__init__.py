"""adk-cc package entry.

We load `.env` here, before the agent module imports. `adk web` /
`adk run` do the dotenv bootstrap in ADK's CLI runner before they
even import the agent module, so `agent.py`'s eager
`LiteLlm(api_key=os.environ["ADK_CC_API_KEY"])` finds the key.
Operators running `uvicorn adk_cc.service.server:make_app --factory`
skip that bootstrap, so we mirror it here.

Lookup order (`override=False` always, so a real process env wins; earlier
files win over later ones):

  0. The desktop settings file (packaged app) — `$ADK_CC_SETTINGS_FILE`, or
     `$ADK_CC_DESKTOP_DATA/settings.env` / `~/.adk-cc-desktop/settings.env` in
     desktop context. Loaded first so a user's settings.env beats a repo .env.
  1. `<ADK_CC_AGENTS_DIR>/.env` if `ADK_CC_AGENTS_DIR` is set.
  2. `<this-package-dir>/../.env`  (repo root when installed editable).
  3. `<cwd>/.env`.

Disable by setting `ADK_CC_SKIP_DOTENV=1` before launching the
process — useful for CI / containers where the env is already
fully populated and a stray `.env` shouldn't shadow it.
"""

from __future__ import annotations

import os as _os
from pathlib import Path as _Path

# stdlib-only; safe pre-dotenv. THE canonical bool-env parse (see config/schema.py).
from .config.schema import env_bool as _env_bool


def _bootstrap_dotenv() -> None:
    if _env_bool("ADK_CC_SKIP_DOTENV"):
        return
    try:
        from dotenv import load_dotenv as _load_dotenv
    except ImportError:
        return

    candidates: list[_Path] = []
    # 0. Desktop settings file (installer / desktop app) — highest priority so a
    #    user's settings.env wins over any repo/cwd .env. Scoped to desktop
    #    context (explicit file, or ADK_CC_DESKTOP/_DATA set) so a plain web
    #    server doesn't pick it up.
    _settings = _os.environ.get("ADK_CC_SETTINGS_FILE")
    if _settings:
        candidates.append(_Path(_settings).expanduser())
    elif _env_bool("ADK_CC_DESKTOP") or _os.environ.get("ADK_CC_DESKTOP_DATA"):
        _data = _os.environ.get("ADK_CC_DESKTOP_DATA")
        _base = _Path(_data) if _data else _Path.home() / ".adk-cc-desktop"
        candidates.append(_base / "settings.env")
    agents_dir = _os.environ.get("ADK_CC_AGENTS_DIR")
    if agents_dir:
        candidates.append(_Path(agents_dir) / ".env")
    # Repo root .env. This file is at <repo>/agents/adk_cc/__init__.py,
    # so parents[2] is the repo root (parents[0]=adk_cc, [1]=agents).
    candidates.append(_Path(__file__).resolve().parents[2] / ".env")
    candidates.append(_Path.cwd() / ".env")

    seen: set[_Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            try:
                _load_dotenv(resolved, override=False)
            except Exception:
                pass


def _boot_config_check() -> None:
    """Validate os.environ against the central schema, logging errors/warnings.

    Lives HERE — after the dotenv bootstrap, before the agent import — so it
    genuinely runs before anything is built and covers EVERY entrypoint that
    imports the package (`uvicorn …make_app`, `adk web` / `adk run`, the
    desktop sidecar, scripts). It previously lived in make_app(), which the
    `adk web` path never calls and which runs only after the whole agent graph
    was already constructed at import time.

    Non-fatal by design (log-and-continue), and a failure of the check itself
    must never block boot — but that failure is logged at WARNING, not DEBUG:
    a silently-skipped validator is invisible exactly when the env is broken.
    `ADK_CC_SKIP_CONFIG_CHECK=1` disables (the config CLI does its own
    explicit check/print, so import-time log noise can be turned off)."""
    import logging as _logging

    log = _logging.getLogger("adk_cc.config")
    if _env_bool("ADK_CC_SKIP_CONFIG_CHECK"):
        return
    try:
        from .config.schema import check as _check

        errors, warnings = _check(dict(_os.environ))
    except Exception:  # pragma: no cover — validation must never block boot
        log.warning("config self-check skipped (error while validating)", exc_info=True)
        return
    for w in warnings:
        log.warning("config: %s", w)
    for e in errors:
        log.error("config: %s", e)
    if errors:
        log.error(
            "config self-check found %d error(s) above — the deployment may "
            "misbehave; fix the environment (see `python -m adk_cc.config check`).",
            len(errors),
        )


_bootstrap_dotenv()
_boot_config_check()

from . import agent  # noqa: E402 — must follow the dotenv bootstrap above
