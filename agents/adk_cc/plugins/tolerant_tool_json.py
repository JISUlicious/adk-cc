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
  4. if it still can't parse, re-raises the ORIGINAL error so callers see a
     coherent failure rather than a repair artifact (logging a clear WARNING
     when the cause is truncation rather than malformation).

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
    than failing. Those cases re-raise (with a truncation WARNING) by design.
  - Scoped to ADK's lite_llm module only; the stdlib json is untouched
    everywhere else.
"""

from __future__ import annotations

import json as _stdlib_json
import logging
import os
import re

_log = logging.getLogger(__name__)

_PATCHED_FLAG = "_adk_cc_tolerant_json_patched"

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


def _complete_truncated(s: str) -> str | None:
    r"""Complete a TRUNCATED tool call by appending only the missing closing
    brackets/braces — never a string value or a missing value.

    A model that stops mid-emission leaves JSON that is structurally incomplete.
    When the ONLY thing missing is the closers (the values present are complete,
    e.g. ``{"skill_name": "my-skill"`` → add ``}``), this recovers it
    losslessly. It REFUSES (returns None) when the text ends inside a string
    (``{"skill_name": "my-sk`` — the value itself was cut, and closing the quote
    would fabricate a wrong value) or when there's nothing to close. A dangling
    ``key:`` (``{"skill_name": ``) gets its ``}`` appended but won't parse, so
    the caller's parse attempt fails and the original error is surfaced — we
    never invent a value.
    """
    in_string, stack = _json_scan(s)
    if in_string or not stack:
        return None
    return s + "".join(reversed(stack))


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

        # Try cumulative repairs: text → +escape → +escape+comma → ...
        applied: list[str] = []
        candidate = text
        # First: does strict=False alone (no rewrite) fix it? (raw ctrl chars)
        attempts = [("control-chars(strict=False)", text)]
        for label, transform in _REPAIRS:
            candidate = transform(candidate)
            applied.append(label)
            attempts.append(("+".join(applied), candidate))

        # Final tier: structural completion of a TRUNCATED tool call (the model
        # stopped mid-emission). Appends only the missing }/] closers — never
        # fabricates a string or a missing value (see _complete_truncated) — and
        # composes the malformation repairs on top of the completed text.
        completed = _complete_truncated(text)
        if completed is not None:
            cand = completed
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

        # Nothing recovered it. If the text is structurally TRUNCATED (ends
        # inside a string, or has unbalanced brackets we couldn't safely close),
        # say so explicitly — the model's tool call was cut off mid-emission,
        # which is a different problem (token cap / unreliable model) than a
        # malformed-but-complete argument. We still raise the ORIGINAL error so
        # the failure type is unchanged for callers.
        in_string, stack = _json_scan(text)
        if in_string or stack:
            _log.warning(
                "tolerant_tool_json: tool-call JSON appears TRUNCATED "
                "(%s) and can't be recovered without fabricating a value — "
                "the model stopped mid tool-call. Snippet: %r",
                "unterminated string" if in_string else "unbalanced brackets",
                text[:120],
            )
        raise first_error  # nothing recovered it — surface the original


class _TolerantJsonShim:
    """Delegates every attribute to stdlib json, overriding only ``loads``.

    Installed as the ``json`` name inside ADK's lite_llm module so its
    ``json.loads(tool_call.function.arguments)`` calls become tolerant, while
    ``json.dumps`` and everything else behave exactly as before.
    """

    loads = staticmethod(tolerant_loads)

    def __getattr__(self, name):  # everything except ``loads``
        return getattr(_stdlib_json, name)


def install_tolerant_tool_json() -> None:
    """Swap ADK lite_llm's module-level ``json`` for the tolerant shim.

    Default-ON; disable with ADK_CC_TOLERANT_TOOL_JSON=0. Idempotent. No-op
    (logged) if ADK's module layout changes so the patch can't apply — we
    never want this to break agent import.
    """
    if os.environ.get("ADK_CC_TOLERANT_TOOL_JSON") == "0":
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
    setattr(_ll, _PATCHED_FLAG, True)


# Side-effect on import (mirrors session_retry): a single
# ``from adk_cc.plugins import ...`` triggers the patch before ADK makes any
# model call.
install_tolerant_tool_json()
