"""`SecretStr` — a string wrapper that won't leak its value into logs/prompts.

A resolved secret should only ever be revealed at the injection boundary
(sandbox exec env / MCP auth header). Wrapping it in `SecretStr` means an
accidental f-string, log line, repr, or prompt interpolation renders `***`
instead of the value; the raw value is reachable ONLY via the explicit
`.reveal()` call, which is easy to grep/audit.

Note this is a guardrail against ACCIDENTAL exposure, not a cryptographic
container — the value is held in memory in plaintext (it has to be, to be
injected). Egress redaction (SecretRedactionPlugin) is the second layer.
"""

from __future__ import annotations


class SecretStr:
    __slots__ = ("_value", "_name")

    def __init__(self, value: str, *, name: str = "") -> None:
        self._value = value
        self._name = name  # the env-var / credential key name, for placeholders

    def reveal(self) -> str:
        """Return the raw secret. The ONLY way to read the value — audit calls."""
        return self._value

    @property
    def name(self) -> str:
        return self._name

    def __str__(self) -> str:  # f-strings, str(), print()
        return "***"

    def __repr__(self) -> str:  # logging %r, reprs
        return f"SecretStr(name={self._name!r}, value=***)"

    def __bool__(self) -> bool:
        return bool(self._value)

    # Deliberately NOT implementing __len__/__iter__/__contains__/__format__
    # beyond the above — they could leak length or content via side channels.
