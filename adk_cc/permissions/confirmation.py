"""Structured payload for tool-confirmation prompts.

ADK's `tool_context.request_confirmation(payload=...)` accepts arbitrary
JSON-serializable data, designed for the frontend to render. adk-cc
uses this seam to send a structured `ConfirmPrompt` so frontends can
render the prompt as labelled buttons rather than a checkbox.

Protocol (frontend ↔ plugin):

  Outbound (plugin → frontend):
    `requested_tool_confirmations[<call_id>].payload` is a dict with:
      - `style`: discriminator. Two styles are defined today —
        `"confirm_deny"` (two options) and `"single_select"` (N options,
        used by the destructive-tool gate as a 3-option allow-once /
        allow-always / deny prompt).
      - `title`: short summary (e.g. "Confirm run_bash?").
      - `detail`: full reason text (mirrors `hint`).
      - `options`: list of {id, label, description}. Each `id` is
        stable; the plugin routes the decision on it. See `chose_id`
        values below.

  Inbound (frontend → plugin):
    The frontend submits `payload = {"chose_id": "<one of the option ids>"}`.
    The plugin reads `chose_id` if present; otherwise it falls back to
    the ADK-standard `confirmed: bool` field. The back-compat path
    means frontends that ignore `payload` (including `adk web`'s
    bundled UI) keep working — they show a checkbox + submit and
    behave as before.

  `chose_id` values:
    - `"allow"` (legacy) or `"allow_once"` — let the tool run this time.
    - `"allow_always"` — let the tool run AND ask the permission plugin
      to inject a session-scope ALLOW rule so the same (tool, rule key)
      pair isn't gated again for the rest of the session.
    - `"deny"` — short-circuit; the model sees a denial and adjusts.

  Unknown `chose_id` values fall through to the denied branch (fail
  closed) — except `"allow"` which the plugin maps to `"allow_once"`
  for back-compat with the first cut of this protocol.

The `style` discriminator leaves room for future variants (e.g.
multi-select, or a "pick one of these alternatives" flow surfaced
by a tool other than the permission gate) without breaking schemas.
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

    style: Literal["confirm_deny", "single_select"]
    title: str
    detail: str
    options: list[ConfirmOption]


def confirm_deny_prompt(tool_name: str, reason: str) -> ConfirmPrompt:
    """Two-button payload — kept for callers that want a strict binary
    gate. The destructive-tool gate uses `allow_once_always_deny_prompt`
    instead so users can opt into a session-scope rule."""
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


def allow_once_always_deny_prompt(tool_name: str, reason: str) -> ConfirmPrompt:
    """Three-option payload used by the permission plugin's "ask" branch.

    "Allow always" tells the plugin to add a SESSION-scope ALLOW rule
    keyed by (tool, extracted rule key) so the same operation isn't
    re-gated for the rest of the session. Scope is intentionally
    narrow — exact rule-key match — so a user who approves
    `git status` does NOT thereby allow `git push`.
    """
    return ConfirmPrompt(
        style="single_select",
        title=f"Confirm {tool_name}?",
        detail=reason,
        options=[
            ConfirmOption(
                id="allow_once",
                label="Allow once",
                description="Run this one time. Future similar calls will ask again.",
            ),
            ConfirmOption(
                id="allow_always",
                label="Allow always",
                description="Run, and stop asking about this exact operation for the rest of the session.",
            ),
            ConfirmOption(
                id="deny",
                label="Deny",
                description="Cancel; the model will see the denial and adjust.",
            ),
        ],
    )
