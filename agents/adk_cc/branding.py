"""The product's name, in one place.

The name is a DISPLAY layer, not an identity: the frontend discovers the app
dynamically (`/list-apps`), so nothing load-bearing depends on what we call
ourselves. That is what makes a rename cheap here and expensive in the
identifier layers (env prefix, package dir, data dirs) — see
analysis/branding-plan.md.

Two forms, deliberately:

  BRAND      ".jus"  — what a human reads, and what the MODEL calls itself.
  BRAND_SLUG "jus"   — identifier form, for anywhere a leading dot is illegal
                       or meaningless (env vars, package/dist names, log
                       prefixes). A leading dot cannot appear in a Python
                       identifier or an environment variable name, so the
                       split is not cosmetic.

Overridable via ADK_CC_BRAND so a fork or a white-label build changes one
env var rather than patching strings.
"""

from __future__ import annotations

import os

BRAND: str = (os.environ.get("ADK_CC_BRAND") or ".jus").strip() or ".jus"

# Identifier form: the display name without a leading dot, lowercased.
BRAND_SLUG: str = BRAND.lstrip(".").lower() or "jus"

# The prefix on notes injected into TOOL RESULTS (bash hints, skill errors,
# dependency notes). The model reads these; a stable, bracketed marker is how
# it tells our voice from the tool's own output.
NOTE_PREFIX: str = f"[{BRAND}]"

# The verification-failure label the user sees on a card.
WARN_LABEL: str = f"⚠ {BRAND}:"


def note(text: str) -> str:
    """`[.jus] <text>` — one place, so the marker never drifts."""
    return f"{NOTE_PREFIX} {text}"
