"""Config package facade.

`schema` (the typed env-var schema) is stdlib-only and re-exported eagerly —
safe to import anywhere, including the package's import-time boot check.
`load_settings_from_yaml` lives in `settings_loader`, which drags the
permissions subsystem (pydantic) — it is re-exported LAZILY via PEP 562 so
that `from adk_cc.config import check` (the boot self-check path) stays
lightweight and cannot fail on a heavy-dependency import error.
"""

from .schema import (
    BY_NAME,
    FIELDS,
    Profile,
    Tier,
    Var,
    check,
    env_bool,
    render_effective,
    render_env_example,
    resolve,
)

__all__ = [
    "load_settings_from_yaml",
    "FIELDS",
    "BY_NAME",
    "Var",
    "Tier",
    "Profile",
    "resolve",
    "check",
    "env_bool",
    "render_env_example",
    "render_effective",
]


def __getattr__(name: str):
    # Lazy re-export: only pay the settings_loader → permissions → pydantic
    # import when a caller actually asks for the YAML loader.
    if name == "load_settings_from_yaml":
        from .settings_loader import load_settings_from_yaml

        return load_settings_from_yaml
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
