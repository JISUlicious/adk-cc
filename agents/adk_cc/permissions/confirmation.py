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

from typing import Literal, Optional

from pydantic import BaseModel

from .rules import _RULE_KEY_EXTRACTORS

# Subjects longer than this get truncated in the title so the prompt
# header stays readable. The full args are still recoverable from the
# tool's function_call payload — this is just for the title.
_MAX_SUBJECT_LENGTH = 80


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

    `with_comment=True` asks the frontend to surface an optional
    free-form text field alongside the option buttons. Useful for
    approval flows where the operator might want to give feedback on
    a denied plan (e.g. `exit_plan_mode`). The destructive-tool gate
    leaves it off — Allow once / Allow always / Deny carry enough
    signal on their own.

    `with_persist_toggle=True` asks the frontend to surface an optional
    boolean toggle ("Persist across sessions"). Only meaningful when
    the prompt has an `allow_always` option — the toggle promotes the
    resulting session-scope rule to a USER-scope rule that survives
    across the user's future sessions. Default unchecked, so the
    operator gets the same per-session behavior as before unless they
    deliberately opt into broader scope.
    """

    style: Literal["confirm_deny", "single_select"]
    title: str
    detail: str
    options: list[ConfirmOption]
    with_comment: bool = False
    with_persist_toggle: bool = False


def extract_subject(tool_name: str, args: dict) -> Optional[str]:
    """Pull a short summary of a tool's args for use in the prompt title.

    Reuses `_RULE_KEY_EXTRACTORS` — the same per-tool arg-key map the
    permission engine uses to match rule_content patterns. So for
    `run_bash` you get the `command`, for `write_file` you get the
    `path`, etc. Returns None when the tool has no extractor or the
    extracted value is empty.

    Long subjects (e.g. multi-line bash commands) are truncated with an
    ellipsis to keep the prompt title readable.
    """
    extractor = _RULE_KEY_EXTRACTORS.get(tool_name)
    if extractor is None:
        return None
    try:
        raw = extractor(args)
    except Exception:
        return None
    if not isinstance(raw, str) or not raw:
        return None
    # Collapse internal newlines so multi-line commands don't blow up
    # the title; full args remain visible in the function_call payload.
    single_line = " ".join(raw.split())
    if len(single_line) > _MAX_SUBJECT_LENGTH:
        return single_line[: _MAX_SUBJECT_LENGTH - 1] + "…"
    return single_line


def _title(tool_name: str, subject: Optional[str]) -> str:
    """Compose the prompt title. With `subject`, include it after a colon
    so the operator can tell two concurrent prompts apart."""
    if subject:
        return f"Confirm {tool_name}: {subject}?"
    return f"Confirm {tool_name}?"


def confirm_deny_prompt(
    tool_name: str,
    reason: str,
    *,
    subject: Optional[str] = None,
) -> ConfirmPrompt:
    """Two-button payload — kept for callers that want a strict binary
    gate. The destructive-tool gate uses `allow_once_always_deny_prompt`
    instead so users can opt into a session-scope rule.

    Pass `subject` (e.g. the file path or command) to disambiguate
    concurrent prompts for the same tool.
    """
    return ConfirmPrompt(
        style="confirm_deny",
        title=_title(tool_name, subject),
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


def allow_once_always_deny_prompt(
    tool_name: str,
    reason: str,
    *,
    subject: Optional[str] = None,
    allow_always_preview: Optional[str] = None,
) -> ConfirmPrompt:
    """Three-option payload used by the permission plugin's "ask" branch.

    "Allow always" tells the plugin to add a SESSION-scope ALLOW rule
    keyed by (tool, extracted rule key) so the same operation isn't
    re-gated for the rest of the session. Scope is intentionally
    narrow — exact rule-key match — so a user who approves
    `git status` does NOT thereby allow `git push`.

    Pass `subject` (the tool's rule key, e.g. the `command` for
    `run_bash` or the `path` for `write_file`) to disambiguate
    concurrent prompts. The title becomes
    `"Confirm run_bash: git status?"` instead of `"Confirm run_bash?"`,
    so an operator faced with three pending `write_file` confirmations
    can tell which file each one is gating.

    Pass `allow_always_preview` (e.g. `"pip install *"` for a command, or
    `"/path/to/project/*"` for a file tool) to make the Allow always
    description show the broadened pattern explicitly — the operator sees
    the exact rule scope at click time, not a vague "this exact operation"
    claim that no longer matches the storage behavior since adk-cc started
    broadening run_bash commands and workspace-anchoring path tools.
    """
    always_desc = (
        f"Allow `{allow_always_preview}` for the rest of the session "
        "(covers similar calls, not just this exact one)."
        if allow_always_preview
        else "Run, and stop asking about this exact operation for the rest of the session."
    )
    return ConfirmPrompt(
        style="single_select",
        title=_title(tool_name, subject),
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
                description=always_desc,
            ),
            ConfirmOption(
                id="deny",
                label="Deny",
                description="Cancel; the model will see the denial and adjust.",
            ),
        ],
        # NOTE: the inbound plugin still understands a
        # `persist_across_sessions` payload field (it promotes the
        # resulting allow rule to USER scope), but we deliberately do
        # NOT surface a toggle on this high-frequency destructive
        # prompt — cross-session promotion is too consequential to be
        # one careless click away from a denial. A future designated
        # admin UI is expected to set `with_persist_toggle=True` for
        # its own deliberate-promotion flow.
    )


def grant_scope_prompt(
    tool_name: str,
    resolved_path: str,
    parent_dir: str,
    *,
    allow_folder: bool = True,
) -> ConfirmPrompt:
    """Scope-expansion prompt: a desktop path tool is targeting a path OUTSIDE
    the bound project (and outside any already-granted directory).

    Distinct `chose_id`s from the destructive gate so the plugin can tell the two
    HITL flows apart on resume (`grant_folder` / `grant_once` / `grant_deny`):

      - `grant_folder` → widen scope to `parent_dir` for the session (+ a
        `<dir>/*` allow rule so writes there don't re-prompt). Suppressed for
        protected paths (`allow_folder=False`) so a single sensitive file can't
        drag its whole parent (e.g. `$HOME`) into scope.
      - `grant_once` → allow just this one operation on `resolved_path`.
      - `grant_deny` → cancel.

    `with_persist_toggle=allow_folder` surfaces an optional "remember across
    sessions" box that promotes a folder grant to the persistent "Working
    directories" set (`user:` scope), mirroring the destructive gate's
    session-vs-user split.
    """
    options = []
    if allow_folder:
        options.append(
            ConfirmOption(
                id="grant_folder",
                label="Allow this folder",
                description=(
                    f"Grant read/write access to `{parent_dir}` for this session. "
                    "It is OUTSIDE the project — not covered by Undo/checkpoints."
                ),
            )
        )
    options.append(
        ConfirmOption(
            id="grant_once",
            label="Allow once",
            description="Allow just this one operation. Future access will ask again.",
        )
    )
    options.append(
        ConfirmOption(
            id="grant_deny",
            label="Deny",
            description="Cancel; the model will see the denial and adjust.",
        )
    )
    return ConfirmPrompt(
        style="single_select",
        title=f"Allow {tool_name} outside the project?",
        detail=(
            f"{tool_name} targets `{resolved_path}`, which is outside the bound "
            "project and any granted directory. Grant access?"
        ),
        options=options,
        with_persist_toggle=allow_folder,
    )
