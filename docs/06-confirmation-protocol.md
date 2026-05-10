# Tool-confirmation protocol

When `PermissionPlugin` decides a tool call needs human confirmation (a destructive operation in DEFAULT mode, or a matching ASK rule), it pauses the tool via ADK's `tool_context.request_confirmation(hint=..., payload=...)`. ADK surfaces the pause to the frontend via `requested_tool_confirmations[<function_call_id>]`; when the user responds, ADK re-invokes the tool with `tool_context.tool_confirmation` populated.

adk-cc uses ADK's `payload` field to send a **structured prompt** the frontend can render as labelled buttons, and reads back a **structured response** describing the user's choice. The bundled `adk web` UI ignores the payload and shows a checkbox + submit; the plugin handles that legacy path too. Both work side-by-side with no frontend changes required to upgrade.

This document is the wire contract for any frontend that wants the richer rendering.

## Outbound (plugin → frontend)

`requested_tool_confirmations[<call_id>].payload` is a `ConfirmPrompt` dict:

```json
{
  "style": "single_select",
  "title": "Confirm run_bash?",
  "detail": "destructive run_bash requires confirmation",
  "options": [
    {"id": "allow_once",   "label": "Allow once",   "description": "Run this one time. Future similar calls will ask again."},
    {"id": "allow_always", "label": "Allow always", "description": "Run, and stop asking about this exact operation for the rest of the session."},
    {"id": "deny",         "label": "Deny",         "description": "Cancel; the model will see the denial and adjust."}
  ]
}
```

**Fields**

- `style`: discriminator. Two values defined today:
  - `"single_select"` — N options; user picks one. Used by the destructive-tool gate (3 options as above).
  - `"confirm_deny"` — binary. Two options with ids `"allow"` and `"deny"`. Available via the `confirm_deny_prompt` helper but **not** currently the default at the gate.
- `title` — short summary, suitable for a dialog header.
- `detail` — the engine's reason text. Mirrors the `hint` field on `ToolConfirmation` (frontends that don't render `payload` see this string).
- `options` — list of `{id, label, description}`:
  - `id` is the stable routing key. The plugin keys behavior off this; UI can change `label`/`description` freely.
  - `label` is the button text (1–4 words).
  - `description` is the per-option explanation (one sentence).

Frontends should switch rendering on `style`. Unknown styles SHOULD fall back to rendering `hint` and submitting `confirmed: bool` — the plugin's back-compat path handles that.

## Inbound (frontend → plugin)

The frontend submits the response via ADK's standard `ToolConfirmation` shape:

```python
ToolConfirmation(confirmed: bool, payload: Any | None, hint: str)
```

To use the structured protocol, set `payload` to:

```json
{"chose_id": "allow_once" | "allow_always" | "deny"}
```

The plugin reads `chose_id` first. If absent (or not a string), it falls back to `confirmed: bool` — the bundled `adk web` UI never sends `payload`, so `confirmed: True` runs the tool and `confirmed: False` denies it.

### `chose_id` semantics

| `chose_id`                 | Behavior                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------- |
| `"allow_once"`             | Tool runs this time. No persistent change.                                                        |
| `"allow"` (legacy)         | Same as `"allow_once"` — kept for back-compat with the two-button protocol's first cut.           |
| `"allow_always"`           | Tool runs **and** the plugin injects a SESSION-scope ALLOW rule (see below). Future matching calls are auto-allowed for the rest of the session. |
| `"deny"`                   | Returns `{"status": "permission_denied_by_user", ...}`; the model sees the denial and adjusts.    |
| (any other string)         | Fail-closed: treated as deny.                                                                     |

### "Allow always" rule scope

When the user picks `allow_always`, the plugin creates:

```python
PermissionRule(
    source=RuleSource.SESSION,
    behavior=RuleBehavior.ALLOW,
    tool_name=<the tool name>,
    rule_content=<extracted rule key>,
)
```

The "rule key" is the per-tool string most operators write rules against (see `_RULE_KEY_EXTRACTORS` in `adk_cc/permissions/rules.py`):

| Tool          | Rule key       | Example for "Allow always" approval                     |
| ------------- | -------------- | -------------------------------------------------------- |
| `run_bash`    | `command` arg  | Approving `git status` covers `git status` exactly.      |
| `read_file`   | `path` arg     | Approving `/etc/hosts` covers `/etc/hosts` exactly.      |
| `write_file`  | `path` arg     | Approving `/tmp/foo` covers `/tmp/foo` exactly.          |
| `edit_file`   | `path` arg     | Same shape as `write_file`.                              |
| `glob_files`  | `root` arg     | Approving root `.` covers `.` exactly.                   |
| `grep`        | `path` arg     | Same shape as `read_file`.                               |

Scope is **intentionally narrow** — exact rule-key match. The user explicitly approved THIS operation; broadening (e.g. fnmatch wildcards on the command) would be unsafe. If a user wants broader scope, they should write a config rule directly (`adk_cc/permissions/permissions.yaml` etc).

For tools without an extractor entry (custom user tools), the rule omits `rule_content`, which means "any invocation of this tool for the session." That's a conservative fallback — the user approving an unknown tool once shouldn't open it forever, but we have no information to scope tighter.

Session rules live in memory on the `SettingsHierarchy` held by the `PermissionPlugin`. They DO NOT persist across server restarts.

## Back-compat fallback (no payload)

Frontends that don't speak the payload protocol — including ADK's bundled `adk web` UI — see `hint` and submit `ToolConfirmation(confirmed: bool)` with no payload. The plugin handles this exactly as before:

| Input                                 | Plugin behavior          |
| ------------------------------------- | ------------------------ |
| `payload=None`, `confirmed=True`      | Run the tool (allow_once equivalent). No session rule. |
| `payload=None`, `confirmed=False`     | Deny.                    |

So you can upgrade gradually: ship the payload-aware frontend on your own schedule; the gate works end-to-end with or without it.

## Implementation pointers

- Outbound prompt construction: `adk_cc/permissions/confirmation.py` (`allow_once_always_deny_prompt` for the gate; `confirm_deny_prompt` for binary cases).
- Wire-out: `PermissionPlugin.before_tool_callback` calls `tool_context.request_confirmation(hint=..., payload=prompt.model_dump())` at `adk_cc/plugins/permissions.py`.
- Wire-in: same callback reads `_read_choice_id(tool_context.tool_confirmation)` and routes on the id.
- Session rule injection: `PermissionPlugin._add_session_allow`.
- Unit tests: `tests/test_permissions_confirmation.py` covers every documented path.

## Not in scope (yet)

- A "pick one of these alternatives" flow where the **model** asks the user to pick among options (rather than the **plugin** asking for approval). That would be a new tool similar to `ask_user_question` but with single-pick semantics. The `style` discriminator on `ConfirmPrompt` is ready for it; the plumbing is not.
- Persisting session rules across restarts. They're in-memory on the plugin instance today.
- A way for the user to revoke an "allow always" decision mid-session. The settings hierarchy supports adding session rules, not removing them.
