"""Structured payload for tool-confirmation prompts.

ADK's `tool_context.request_confirmation(payload=...)` accepts arbitrary
JSON-serializable data, designed for the frontend to render. Today
adk-cc only sent `hint` (a string), which the bundled `adk web` UI
renders as a checkbox + submit button — not great for a binary choice
the user is making in real time. By sending a structured `ConfirmPrompt`,
frontends that opt in can render the prompt as two clear buttons (and,
in a future round, multi-choice selectors).

Protocol (frontend ↔ plugin):

  Outbound (plugin → frontend):
    `requested_tool_confirmations[<call_id>].payload` is a dict with:
      - `style`: discriminator. Today only `"confirm_deny"` is defined.
      - `title`: short summary (e.g. "Confirm run_bash?").
      - `detail`: full reason text (mirrors `hint`).
      - `options`: list of {id, label, description}. For `confirm_deny`,
        exactly two options with ids `"allow"` and `"deny"`.

  Inbound (frontend → plugin):
    The frontend submits `payload = {"chose_id": "allow" | "deny"}`.
    The plugin reads `chose_id` if present; otherwise it falls back to
    the ADK-standard `confirmed: bool` field. This back-compat path
    means frontends that ignore `payload` (including `adk web`'s
    bundled UI) keep working exactly as before.

The `style` field is a discriminator so future rounds can add
`"single_select"` for multi-choice (e.g. "Allow once / Allow always /
Deny") without a breaking schema change.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ConfirmOption(BaseModel):
    """One selectable option in a `ConfirmPrompt`.

    Mirrors the public-facing shape of `AskOption` in
    `adk_cc/tools/schemas.py` (label + description), with an added
    stable `id` the plugin uses to route the decision.
    """

    id: str
    label: str
    description: str


class ConfirmPrompt(BaseModel):
    """Structured prompt for a HITL tool-confirmation request.

    `style` is a discriminator — frontends switch rendering on it and
    fall back to ADK's default `hint` rendering for unknown styles.
    """

    style: Literal["confirm_deny"]
    title: str
    detail: str
    options: list[ConfirmOption]


def confirm_deny_prompt(tool_name: str, reason: str) -> ConfirmPrompt:
    """Canonical two-button payload for the destructive-tool fallback."""
    return ConfirmPrompt(
        style="confirm_deny",
        title=f"Confirm {tool_name}?",
        detail=reason,
        options=[
            ConfirmOption(
                id="allow",
                label="Allow",
                description="Run this once.",
            ),
            ConfirmOption(
                id="deny",
                label="Deny",
                description="Cancel; the model will see the denial and adjust.",
            ),
        ],
    )
