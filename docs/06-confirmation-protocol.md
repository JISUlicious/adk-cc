# Tool-confirmation protocol

When `PermissionPlugin` decides a tool call needs human confirmation (a destructive operation in DEFAULT mode, or a matching ASK rule), it pauses the tool via ADK's `tool_context.request_confirmation(hint=..., payload=...)`. ADK surfaces the pause to the frontend via `requested_tool_confirmations[<function_call_id>]`; when the user responds, ADK re-invokes the tool with `tool_context.tool_confirmation` populated.

adk-cc uses ADK's `payload` field to send a **structured prompt** the frontend can render as labelled buttons, and reads back a **structured response** describing the user's choice. Two layers cooperate to make this work across frontends:

- **`PermissionPlugin`** — produces the structured `ConfirmPrompt` payload and reads the resumed answer (`chose_id`). This is the wire contract for any payload-aware frontend.
- **`ConfirmationFormUiPlugin`** (optional, enabled by default) — bridges the protocol to **bundled `adk web`**'s long-running form widget by rewriting the wrapper event name and injecting a `response_schema`. With this plugin enabled, the bundled UI renders an N-option dropdown without code changes elsewhere. Disable it to fall back to bundled `adk web`'s binary checkbox widget — `PermissionPlugin` keeps working underneath either way.

This document is the wire contract for both layers.

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

## Bundled `adk web` UI bridge — `ConfirmationFormUiPlugin`

ADK's bundled `adk web` UI hard-codes a binary widget (checkbox + read-only payload textarea + Submit) for any event whose function-call name is `adk_request_confirmation`. The `ConfirmPrompt.options` list never reaches the screen — the UI shows one checkbox regardless of how many options the payload carries.

`ConfirmationFormUiPlugin` (registered in the default plugin chain) rewrites both sides of the protocol so the bundled UI takes its **form-widget** path instead:

### Outbound rewrite (event → bundled UI)

- Find the `adk_request_confirmation` function-call event ADK emits.
- Derive a JSON schema from `ConfirmPrompt.options`:
  ```json
  {
    "type": "object",
    "properties": {
      "choice": {
        "type": "string",
        "enum": ["allow_once", "allow_always", "deny"],
        "description": "<title>\n\n- allow_once: Allow once — Run this one time. …\n- allow_always: Allow always — …\n- deny: Deny — Cancel; …"
      }
    },
    "required": ["choice"]
  }
  ```
- Inject the schema into `args.response_schema`.
- **Rename** the function-call's `name` from `adk_request_confirmation` to the sentinel `_adk_cc_confirmation_form`. The bundled UI's `isConfirmationRequest = (name === "adk_request_confirmation")` short-circuit no longer triggers; the UI proceeds to the form-widget branch and renders a dropdown of the option ids.
- The function-call **id** is preserved. ADK's resume processor matches on id, not on name, so this rename is transparent to resume.
- The original `toolConfirmation.payload` (rich `ConfirmPrompt`) is **also preserved** in the rewritten event's args. Custom payload-aware frontends can still read it if they listen for the sentinel name in addition to `adk_request_confirmation`.

### Inbound rewrite (bundled UI → plugin)

The bundled UI's form widget submits `{choice: "<enum value>"}` as the function_response's `response`. `ConfirmationFormUiPlugin.on_user_message_callback`:

- Detects function_responses whose name is the sentinel.
- Accepts any of these shapes for the `response`:
  - `{choice: "<chose_id>"}` — bundled UI form widget output.
  - `{chose_id: "<chose_id>"}` — payload-aware custom frontend using the original PR-1 protocol.
  - `{result: "<chose_id>"}` — bundled UI's free-form fallback if the operator typed an id directly.
- Reshapes the response to ADK's standard `{confirmed: <bool>, payload: {chose_id: <id>}}` (where `confirmed = chose_id != "deny"`).
- Renames the function_response back to `adk_request_confirmation`.

ADK's existing `_RequestConfirmationLlmRequestProcessor` then picks up the rewritten response and resumes the gated tool exactly as if the plugin weren't there.

### Disabling the bridge

Remove `ConfirmationFormUiPlugin()` from `adk_cc/agent.py`'s plugin list to revert to the bundled UI's binary widget. The underlying `PermissionPlugin` and ADK's request_confirmation flow keep working — confirmations still gate destructive operations, just via the binary checkbox.

## Back-compat fallback (no payload)

Frontends that don't speak the structured protocol — and the bundled UI's free-form textarea path when `response_schema` doesn't render — submit `ToolConfirmation(confirmed: bool)` with no payload. `PermissionPlugin` handles this:

| Input                                 | Plugin behavior          |
| ------------------------------------- | ------------------------ |
| `payload=None`, `confirmed=True`      | Run the tool (allow_once equivalent). No session rule. |
| `payload=None`, `confirmed=False`     | Deny.                    |

So you can upgrade gradually: ship a payload-aware frontend on your own schedule; the gate works end-to-end with or without it.

## Implementation pointers

- Outbound prompt construction: `adk_cc/permissions/confirmation.py` (`allow_once_always_deny_prompt` for the gate; `confirm_deny_prompt` for binary cases).
- Wire-out: `PermissionPlugin.before_tool_callback` calls `tool_context.request_confirmation(hint=..., payload=prompt.model_dump())` at `adk_cc/plugins/permissions.py`.
- Wire-in: same callback reads `_read_choice_id(tool_context.tool_confirmation)` and routes on the id.
- Session rule injection: `PermissionPlugin._add_session_allow`.
- Bundled-UI bridge: `adk_cc/plugins/confirmation_form_ui.py` (sentinel name + bidirectional reshape).
- Unit tests: `tests/test_permissions_confirmation.py` (PermissionPlugin) and `tests/test_confirmation_form_ui.py` (bundled-UI bridge).
- E2E tests: `tests/e2e_confirmation_flow.py` (PermissionPlugin alone) and `tests/e2e_confirmation_form_ui.py` (full bridge through `InMemoryRunner`).

## Not in scope (yet)

- A "pick one of these alternatives" flow where the **model** asks the user to pick among options (rather than the **plugin** asking for approval). That would be a new tool similar to `ask_user_question` but with single-pick semantics. The `style` discriminator on `ConfirmPrompt` is ready for it; the plumbing is not.
- Persisting session rules across restarts. They're in-memory on the plugin instance today.
- A way for the user to revoke an "allow always" decision mid-session. The settings hierarchy supports adding session rules, not removing them.
