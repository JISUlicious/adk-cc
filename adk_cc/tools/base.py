"""Base contract for adk-cc tools.

Every tool subclasses `AdkCcTool` and declares:
  - `meta`: a `ToolMeta` instance carrying a small set of flags
    (`is_read_only`, `is_concurrency_safe`, `long_running`).
  - `input_model`: a Pydantic model class describing the tool's args.
  - `_execute(args, ctx)`: the actual handler.

The base class:
  - Builds a `types.FunctionDeclaration` from `input_model.model_json_schema()`
    via the `parameters_json_schema` field (LiteLlm reads this directly).
  - Validates the LLM-supplied args against `input_model` before calling
    `_execute`, surfacing validation errors back to the model as a
    structured tool result rather than a Python exception.

We deliberately do not subclass `FunctionTool` — `FunctionTool`
introspects a Python function's signature, but we want the typed
Pydantic input contract and explicit meta flags.

Earlier revisions of this base carried `is_destructive`,
`needs_sandbox`, and `requires_user_approval` flags along with a HITL
approval-gate flow in `run_async`. The data-science variant has zero
tools that opt into any of those, so the supporting infrastructure
(permission engine, sandbox backends, confirmation flow) was removed,
and this base trimmed to match. Git history has the previous shape if
ever needed.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, ClassVar

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import BaseModel, ValidationError

_log = logging.getLogger(__name__)


class ToolMeta(BaseModel):
    """Static metadata declared by every adk-cc tool."""

    name: str
    is_read_only: bool
    is_concurrency_safe: bool
    long_running: bool = False


class AdkCcTool(BaseTool):
    """Base class for adk-cc tools.

    Subclasses must set the class-level attributes:
      - `meta: ClassVar[ToolMeta]`
      - `input_model: ClassVar[type[BaseModel]]`
      - `description: ClassVar[str]`

    And implement:
      - `async def _execute(self, args: BaseModel, ctx: ToolContext) -> dict`
    """

    meta: ClassVar[ToolMeta]
    input_model: ClassVar[type[BaseModel]]
    description: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(
            name=self.meta.name,
            description=self.description,
            is_long_running=self.meta.long_running,
        )

    def _get_declaration(self) -> types.FunctionDeclaration:
        schema = self.input_model.model_json_schema()
        # Pydantic emits a "title" field that some model providers reject;
        # strip top-level title and per-property titles for cleaner output.
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=schema,
        )

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        try:
            validated = self.input_model.model_validate(args)
        except ValidationError as e:
            return {"status": "input_validation_error", "errors": e.errors()}

        result = self._execute(validated, tool_context)
        if inspect.isawaitable(result):
            result = await result

        # Long-running tools pause the agent loop while waiting for an
        # asynchronous user/system response. ADK marks the function-CALL
        # event as final via `long_running_tool_ids`, but the function-
        # RESPONSE event built afterwards carries only `actions` — not
        # `long_running_tool_ids`. Without `skip_summarization`, the
        # runner re-invokes the LLM with the awaiting-response status as
        # a normal tool result and cascades into more turns before the
        # asynchronous response actually arrives.
        if self.meta.long_running:
            tool_context.actions.skip_summarization = True

        return result

    async def _execute(
        self, args: BaseModel, ctx: ToolContext
    ) -> dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__}._execute")
