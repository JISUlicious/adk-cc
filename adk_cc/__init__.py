"""adk-cc package entry.

We load `.env` here, before the agent module imports. `adk web` /
`adk run` do the dotenv bootstrap in ADK's CLI runner before they
even import the agent module, so `agent.py`'s eager
`LiteLlm(api_key=os.environ["ADK_CC_API_KEY"])` finds the key.
Operators running `uvicorn adk_cc.service.server:make_app --factory`
skip that bootstrap, so we mirror it here.

Lookup order (`override=False` always, so a real process env wins):

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


def _bootstrap_dotenv() -> None:
    if _os.environ.get("ADK_CC_SKIP_DOTENV") == "1":
        return
    try:
        from dotenv import load_dotenv as _load_dotenv
    except ImportError:
        return

    candidates: list[_Path] = []
    agents_dir = _os.environ.get("ADK_CC_AGENTS_DIR")
    if agents_dir:
        candidates.append(_Path(agents_dir) / ".env")
    # <package-dir>/../.env — the typical editable-install repo root.
    candidates.append(_Path(__file__).resolve().parent.parent / ".env")
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
