r"""Tolerant parsing of model-emitted tool-call argument JSON.

ADK's LiteLlm wrapper parses a tool call's ``function.arguments`` with a bare
``json.loads(...)`` (google/adk/models/lite_llm.py — multiple sites). Some
models emit tool-call arguments that are NOT strictly valid JSON — most
commonly an invalid backslash escape, e.g. a tool that writes a file whose
content contains a regex (``\d``, ``\s``, ``\.``), a Windows path
(``C:\Users``), or a LaTeX-ish token (``\frac``), inlined as a JSON string
value. Strict json.loads then raises ``JSONDecodeError: Invalid \escape``,
and ADK has no recovery — it propagates up and crashes the whole turn (the
user sees "Error in event_generator: Invalid \escape ...").

This is most acute when the agent passes large blobs (an HTML document for a
Plotly chart, code, etc.) through a tool argument: the bigger the payload,
the likelier it contains a backslash the model didn't escape for JSON.

Mitigation, at the one layer that owns this parse: replace the ``json`` name
inside ADK's lite_llm module with a thin shim that delegates everything to
the real stdlib json, EXCEPT ``loads``, which:
  1. tries strict json.loads first — zero behavior change for valid JSON;
  2. on JSONDecodeError, repairs the most common model malformations (invalid
     backslash escapes / raw control chars / trailing commas) and retries;
  3. as a last tier, completes a TRUNCATED tool call (a model that stopped
     mid-emission) by appending ONLY the missing }/] closers — never a string
     value or a missing value, so it recovers `{"skill_name": "x"` losslessly
     but refuses to fabricate `{"skill_name": "x` or `{"skill_name": `;
  4. if it still can't parse: a TRUNCATED tool call (model cut off mid-value) OR
     a complete arg with an EMPTY value (`{"k": }` — a key with no value) degrades
     to a retry MARKER (TRUNCATED_TOOL_CALL_KEY) instead of raising — so the turn
     survives and TruncatedToolCallPlugin returns a clean retry error. We never
     fabricate the missing value (null would call the tool with a wrong arg). A
     genuinely malformed argument re-raises the ORIGINAL error (with an
     UNRECOVERABLE log: message, position, raw args) so callers see a coherent
     failure, not a repair artifact.

Default-ON: tool-call JSON from a model is untrusted by nature, so tolerant
parsing is the safer default. Disable with ``ADK_CC_TOLERANT_TOOL_JSON=0``.
Every repair is logged at WARNING so operators can see when a model is
emitting malformed tool JSON (a signal to consider a better model/prompt).

Caveats:
  - The repair is heuristic: it escapes backslashes that don't begin a valid
    JSON escape (the valid set is " \\ / b f n r t and u-hex). This recovers
    the overwhelmingly common case (a literal backslash the model forgot to
    double) without touching legitimate escapes. It is NOT a general JSON
    repairer.
  - Truncation completion only appends missing CLOSERS; it deliberately does
    NOT close an unterminated string or supply a missing value, because the
    truncated token (`"my-sk` vs `"my-skill"`) is unknowable from the text —
    fabricating it would call the tool with the WRONG argument, which is worse
    than failing. Those cases degrade to the retry MARKER (item 4 above) rather
    than fabricate a value.
  - Scoped to ADK's lite_llm module only; the stdlib json is untouched
    everywhere else.
"""

from __future__ import annotations

import contextvars
import functools
import json as _stdlib_json
import logging
import os
import re
from ..config.schema import env_bool

_log = logging.getLogger(__name__)

_PATCHED_FLAG = "_adk_cc_tolerant_json_patched"

# Returned by tolerant_loads when a tool call is TRUNCATED mid-value (the model
# was cut off mid-emission) and can't be recovered without fabricating the
# argument. Instead of raising — which crashes the whole turn — we return this
# marker; TruncatedToolCallPlugin intercepts it in before_tool_callback and
# turns it into a clean retry error, so the turn survives. Namespaced so it can
# never collide with a real tool argument.
TRUNCATED_TOOL_CALL_KEY = "__adk_cc_truncated_tool_call__"

# Recovery (escape/comma/control repairs + truncation completion + the marker)
# is applied ONLY while this is True — set just around ADK's FINAL args→dict
# parse (`_message_to_generate_content_response`). It is False everywhere else,
# crucially during ADK's STREAMING per-chunk completeness probe, which
# `json.loads`'s the still-ACCUMULATING tool-call buffer. If recovery ran there,
# a partial chunk like `{` would be "completed" to `{}` (and `{"k": "va` would
# return the marker) — the probe would think the tool call is finished and
# advance ADK's tool-call index, splitting the remaining chunks into bogus
# entries. So the probe must see STRICT json (incomplete → raise → keep
# accumulating); only the final parse recovers.
_RECOVERY_ACTIVE: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "adk_cc_tolerant_recovery", default=False
)

# A backslash that does NOT start a valid JSON escape. Valid escapes are:
#   \" \\ \/ \b \f \n \r \t  and  \uXXXX
# Anything else (\d, \s, \., \x, a trailing \, ...) is invalid JSON and is
# what trips json.loads. We match such a backslash and double it.
_INVALID_ESCAPE = re.compile(r'\\(?![\\"/bfnrt]|u[0-9a-fA-F]{4})')

# A trailing comma before a closing } or ] (with optional whitespace) —
# `{"a":1,}` / `[1,2,]`. Common when a model "lists" fields. We can only
# strip commas that are OUTSIDE string values; doing it after the escape +
# control-char passes (which fix string interiors) keeps this conservative.
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _repair_invalid_escapes(s: str) -> str:
    r"""Double every backslash that doesn't begin a valid JSON escape.

    A literal backslash-d in the model output becomes ``\\d`` so json.loads
    reads it back as the intended single backslash. Legitimate escapes
    (\n, \", \uXXXX, ...) are left untouched.
    """
    return _INVALID_ESCAPE.sub(r"\\\\", s)


def _strip_trailing_commas(s: str) -> str:
    """Remove a comma immediately before a closing } or ] (JSON5-ism)."""
    return _TRAILING_COMMA.sub(r"\1", s)


# A colon whose VALUE is empty — `:` then optional whitespace then a closing
# `}`/`]` or a `,` (`{"k": }`, `{"k": , ...}`). The model emitted a key with no
# value. Used ONLY to DETECT this case so we can route it to the retry marker;
# we never hand the null-filled value to the tool (that would fabricate the
# argument the model failed to produce — see _empty_value_recoverable).
_EMPTY_VALUE = re.compile(r":(\s*)([,}\]])")


def _fill_missing_values(s: str) -> str:
    """Fill empty object values with an explicit null (`{"k": }` → `{"k": null}`).
    Heuristic/conservative; a DETECTOR only — never used as the tool argument."""
    return _EMPTY_VALUE.sub(r": null\1\2", s)


def _empty_value_recoverable(text: str, recover_kwargs: dict) -> bool:
    """True when the parse failure is (only) empty object values: filling them
    with null — composed with the same escape/comma repairs — makes it parse.

    A DETECTOR for the missing-value case; the caller returns the retry MARKER,
    NOT the null-filled dict, so we never fabricate the argument. Returns False
    when there's no empty value to fill (a different malformation → should raise).
    """
    filled = _fill_missing_values(text)
    if filled == text:
        return False  # nothing to fill — not a missing-value failure
    candidate = filled
    for transform in (lambda x: x, _repair_invalid_escapes, _strip_trailing_commas):
        candidate = transform(candidate)
        try:
            _stdlib_json.loads(candidate, **recover_kwargs)
            return True
        except _stdlib_json.JSONDecodeError:
            continue
    return False


def _json_scan(s: str) -> tuple[bool, list[str]]:
    r"""Walk `s` as JSON tracking string state and the open-bracket stack.

    Returns ``(in_string, stack)`` where ``in_string`` is True if the text ends
    INSIDE a string value (an unterminated string), and ``stack`` holds the
    closers (``}`` / ``]``) still needed, innermost last. Brackets and quotes
    inside string values are ignored; ``\\`` escapes are respected.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack:
                stack.pop()
    return in_string, stack


# Ordered repair pipeline. Each entry is (label, transform). They're applied
# CUMULATIVELY — every prefix of the list is attempted as a parse — so a
# payload with two malformations (e.g. a bad escape AND a trailing comma)
# still recovers. Conservative by construction: a transform only rewrites the
# specific malformation it targets, and if the final cumulative repair still
# doesn't parse, the ORIGINAL error is raised (never a repair artifact).
_REPAIRS = (
    ("invalid-escape", _repair_invalid_escapes),
    ("trailing-comma", _strip_trailing_commas),
)


def tolerant_loads(s, *args, **kwargs):
    """json.loads that recovers from the malformations models commonly emit in
    tool-call arguments — invalid backslash escapes, raw control characters in
    string values (newlines/tabs in inlined HTML/code), and trailing commas —
    layered and cumulative. Valid JSON is unaffected (strict parse is tried
    first). If nothing recovers it, the ORIGINAL JSONDecodeError is raised."""
    # `strict` defaults to True in json.loads; we override callers' value only
    # in the recovery path below (strict=False permits raw control chars,
    # which is the single most common model malformation for big string args).
    try:
        return _stdlib_json.loads(s, *args, **kwargs)
    except _stdlib_json.JSONDecodeError as first_error:
        if not isinstance(s, (str, bytes, bytearray)):
            raise
        text = (
            s.decode("utf-8", "replace")
            if isinstance(s, (bytes, bytearray))
            else s
        )
        # Recovery parse permits raw control chars (strict=False) — handles
        # un-escaped newlines/tabs inside string values for free.
        recover_kwargs = dict(kwargs)
        recover_kwargs["strict"] = False

        # Scan the text ONCE — the result drives BOTH the truncation-completion
        # tier below AND (if nothing parses) the final truncated-vs-malformed
        # classification, so we never re-scan the same text.
        in_string, stack = _json_scan(text)

        # Try cumulative repairs: text → +escape → +escape+comma → ...
        applied: list[str] = []
        candidate = text
        # First: does strict=False alone (no rewrite) fix it? (raw ctrl chars)
        attempts = [("control-chars(strict=False)", text)]
        for label, transform in _REPAIRS:
            candidate = transform(candidate)
            applied.append(label)
            attempts.append(("+".join(applied), candidate))

        # Final tier: structurally COMPLETE a TRUNCATED tool call by appending
        # only the missing }/] closers — never a string value or a missing value.
        # Safe only when the text doesn't end inside a string and has unclosed
        # brackets; a dangling ``key:`` (``{"skill_name": ``) still won't parse,
        # so it falls through to the marker below — we never invent a value. The
        # malformation repairs compose on top of the completed text.
        if not in_string and stack:
            cand = text + "".join(reversed(stack))
            attempts.append(("complete-truncated", cand))
            for label, transform in _REPAIRS:
                cand = transform(cand)
                attempts.append(("complete-truncated+" + label, cand))

        for label, cand in attempts:
            try:
                result = _stdlib_json.loads(cand, **recover_kwargs)
            except _stdlib_json.JSONDecodeError:
                continue
            _log.warning(
                "tolerant_tool_json: recovered tool-call JSON via [%s] "
                "(original error: %s at pos %s) — the model emitted "
                "non-strict%s JSON in a tool argument",
                label,
                first_error.msg,
                getattr(first_error, "pos", "?"),
                "/truncated" if label.startswith("complete-truncated") else "",
            )
            return result

        # Nothing recovered it. Classify with the SAME scan computed above: a
        # structurally TRUNCATED arg (ends inside a string, or has brackets we
        # couldn't safely close) degrades to a retry MARKER rather than crash the
        # turn; a malformed-but-complete arg re-raises the ORIGINAL error so the
        # failure type is unchanged for callers.
        if in_string or stack:
            # TRUNCATION (the model was cut off mid-emission). Re-raising here
            # would crash the whole turn. Degrade gracefully instead: return a
            # marker that TruncatedToolCallPlugin converts into a clean retry
            # error in before_tool_callback — the tool never runs with partial
            # args, the model gets a coherent "resend complete arguments"
            # signal, and the turn survives.
            _log.warning(
                "tolerant_tool_json: tool-call JSON appears TRUNCATED (%s) and "
                "can't be recovered without fabricating a value — the model "
                "stopped mid tool-call. Degrading to a retry marker. Snippet: %r",
                "unterminated string" if in_string else "unbalanced brackets",
                text[:120],
            )
            return {TRUNCATED_TOOL_CALL_KEY: True}

        # Balanced but unparseable. Is it the MISSING-VALUE case (`{"k": }` /
        # `{"k": , ...}`)? The model emitted a key with no value. Filling the
        # blanks with null parses, which identifies it as a recoverable "resend"
        # case — but we DON'T hand the tool a fabricated null; we degrade to the
        # same retry marker as truncation, so the turn survives without inventing
        # an argument.
        if _empty_value_recoverable(text, recover_kwargs):
            _log.warning(
                "tolerant_tool_json: tool-call JSON has an EMPTY value (e.g. "
                "`{\"k\": }`) — the model emitted a key with no value. Degrading "
                "to a retry marker (not fabricating null). Snippet: %r", text[:120],
            )
            return {TRUNCATED_TOOL_CALL_KEY: True}

        # Genuinely malformed (not truncated, not a fillable empty value) —
        # surface the ORIGINAL error so the failure type is unchanged for callers,
        # with enough detail for operators to diagnose the model/prompt.
        _log.warning(
            "tolerant_tool_json: UNRECOVERABLE tool-call JSON — %s at pos %s "
            "(not truncation, not an empty value). Surfacing the original error. "
            "Raw args (first 200 chars): %r",
            first_error.msg, getattr(first_error, "pos", "?"), text[:200],
        )
        raise first_error


def _gated_loads(s, *args, **kwargs):
    """The ``json.loads`` ADK's lite_llm sees. Tolerant recovery runs ONLY when
    `_RECOVERY_ACTIVE` is set (the final args→dict parse); otherwise this is
    plain strict ``json.loads`` — so the streaming per-chunk completeness probe
    keeps accumulating an incomplete buffer instead of us 'completing' it."""
    if _RECOVERY_ACTIVE.get():
        return tolerant_loads(s, *args, **kwargs)
    return _stdlib_json.loads(s, *args, **kwargs)


class _TolerantJsonShim:
    """Delegates every attribute to stdlib json, overriding only ``loads``.

    Installed as the ``json`` name inside ADK's lite_llm module. ``loads`` is
    recovery-gated (see `_gated_loads` / `_RECOVERY_ACTIVE`); ``json.dumps`` and
    everything else behave exactly as before.
    """

    loads = staticmethod(_gated_loads)

    def __getattr__(self, name):  # everything except ``loads``
        return getattr(_stdlib_json, name)


def install_tolerant_tool_json() -> None:
    """Swap ADK lite_llm's module-level ``json`` for the tolerant shim.

    Default-ON; disable with ADK_CC_TOLERANT_TOOL_JSON=0. Idempotent. No-op
    (logged) if ADK's module layout changes so the patch can't apply — we
    never want this to break agent import.
    """
    if not env_bool("ADK_CC_TOLERANT_TOOL_JSON", True):
        return
    try:
        from google.adk.models import lite_llm as _ll
    except ImportError:
        return
    if getattr(_ll, _PATCHED_FLAG, False):
        return
    if not hasattr(_ll, "json") or not hasattr(_ll.json, "loads"):
        _log.warning(
            "tolerant_tool_json: ADK lite_llm has no module-level json.loads "
            "to patch — skipping (ADK internals may have changed)."
        )
        return
    _ll.json = _TolerantJsonShim()

    # Turn recovery ON only for the FINAL args→dict parse. ADK's response
    # builder `_message_to_generate_content_response` does the real
    # `json.loads(tool_call.function.arguments)`; the streaming per-chunk
    # completeness probe runs OUTSIDE it, so it stays strict (the streaming-split
    # fix). Best-effort: if the function isn't there, recovery simply never
    # activates and json stays strict (no crash, no premature completion).
    _builder = getattr(_ll, "_message_to_generate_content_response", None)
    if callable(_builder) and not getattr(_builder, "_adk_cc_recovery_wrapped", False):

        @functools.wraps(_builder)
        def _recovering_builder(*args, **kwargs):
            token = _RECOVERY_ACTIVE.set(True)
            try:
                return _builder(*args, **kwargs)
            finally:
                _RECOVERY_ACTIVE.reset(token)

        _recovering_builder._adk_cc_recovery_wrapped = True  # type: ignore[attr-defined]
        _ll._message_to_generate_content_response = _recovering_builder
    else:
        _log.warning(
            "tolerant_tool_json: ADK lite_llm has no _message_to_generate_content_"
            "response to wrap — tolerant recovery stays OFF (strict json) so the "
            "streaming probe is safe, but malformed/truncated args won't recover."
        )
    setattr(_ll, _PATCHED_FLAG, True)


# Side-effect on import (mirrors session_retry): a single
# ``from adk_cc.plugins import ...`` triggers the patch before ADK makes any
# model call.
install_tolerant_tool_json()
