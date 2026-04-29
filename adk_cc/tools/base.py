"""Base contract for adk-cc tools.

Every tool subclasses `AdkCcTool` and declares:
  - `meta`: a `ToolMeta` instance carrying the upstream-style flags
    (is_read_only, is_concurrency_safe, is_destructive, needs_sandbox,
    long_running) that later stages' policy plugins read to decide
    permission, sandboxing, and concurrency.
  - `input_model`: a Pydantic model class describing the tool's args.
  - `_execute(args, ctx)`: the actual handler.

The base class:
  - Builds a `types.FunctionDeclaration` from `input_model.model_json_schema()`
    via the `parameters_json_schema` field (LiteLlm reads this directly).
  - Validates the LLM-supplied args against `input_model` before calling
    `_execute`, surfacing validation errors back to the model as a
    structured tool result rather than a Python exception.

We deliberately do not subclass `FunctionTool` — `FunctionTool` introspects
a Python function's signature, but we want the metadata flags and the
typed Pydantic input contract that policy plugins later consume.
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import BaseModel, ValidationError


class ToolMeta(BaseModel):
    """Static metadata declared by every adk-cc tool.

    Read by:
      - The permission plugin (Stage B) — `is_destructive` and per-tool
        rules drive deny/ask/allow decisions.
      - The sandbox layer (Stage C) — `needs_sandbox` decides whether
        execution must go through `SandboxBackend`.
      - The audit plugin (Stage D) — `is_read_only` decides log verbosity.
      - ADK itself — `long_running` maps to `BaseTool.is_long_running`.
    """

    name: str
    is_read_only: bool
    is_concurrency_safe: bool
    is_destructive: bool = False
    needs_sandbox: bool = False
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
        return result

    async def _execute(
        self, args: BaseModel, ctx: ToolContext
    ) -> dict[str, Any]:
        raise NotImplementedError(f"{type(self).__name__}._execute")
