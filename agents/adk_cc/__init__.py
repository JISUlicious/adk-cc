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


_bootstrap_dotenv()

from . import agent  # noqa: E402 — must follow the dotenv bootstrap above
