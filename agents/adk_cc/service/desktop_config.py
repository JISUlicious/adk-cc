"""Desktop settings file — a user-editable `settings.env` in the desktop data
dir that supplies the model API key/endpoint (and a few optional knobs) for the
packaged desktop app, so config is NOT baked into the binary.

The dotenv bootstrap (``adk_cc/__init__.py``) loads this file FIRST (highest
priority) in desktop context, so the user's settings win over any repo/cwd
``.env``. This module owns the path resolution and the template; the template is
all-commented, so a fresh (unedited) file loads nothing and can't shadow other
config.

Location: ``$ADK_CC_SETTINGS_FILE`` if set, else
``$ADK_CC_DESKTOP_DATA/settings.env``, else ``~/.adk-cc-desktop/settings.env``.
"""

from __future__ import annotations

import os
from pathlib import Path

# All lines commented: an untouched file is a no-op (won't shadow real env or a
# repo .env). Users uncomment + fill the keys they want to set.
_TEMPLATE = """\
# adk-cc desktop settings — edit these, then restart the app.
# Each line is KEY=value (a .env file). A commented (#) line uses the built-in
# default; uncomment and set a value to override it.

# --- Model API (required to talk to a model) --------------------------------
# An OpenAI-compatible endpoint + key. Get a key from your model provider.
# ADK_CC_API_KEY=sk-...
# ADK_CC_API_BASE=https://integrate.api.nvidia.com/v1
# ADK_CC_MODEL=openai/z-ai/glm-5.1

# --- Optional ---------------------------------------------------------------
# Cap requests/min against a rate-limited endpoint.
# ADK_CC_MODEL_MAX_RPM=30
"""


def settings_env_path() -> Path:
    """Resolve the desktop settings.env path (does not create it)."""
    explicit = os.environ.get("ADK_CC_SETTINGS_FILE")
    if explicit:
        return Path(explicit).expanduser()
    data = os.environ.get("ADK_CC_DESKTOP_DATA")
    base = Path(data) if data else Path.home() / ".adk-cc-desktop"
    return base / "settings.env"


def ensure_settings_template() -> Path:
    """Create the settings.env template if it doesn't exist yet; return its path.
    Idempotent — never overwrites an edited file."""
    path = settings_env_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEMPLATE, encoding="utf-8")
    return path
