"""Runtime-switchable model endpoints for adk-cc.

`ModelEndpointConfig` describes one OpenAI-compatible model backend (model
id + api_base + api_key reference). `ModelEndpointRegistry` persists a named
set of them plus an "active" pointer to a JSON file. `SelectableLlm` is a
`BaseLlm` delegate the agent holds ONCE; it resolves the active endpoint
fresh on every invocation (ADK reads `agent.canonical_model` per request),
so the admin panel can switch models with no restart.
"""

from .endpoints import ModelEndpointConfig, ModelEndpointRegistry
from .selectable import SelectableLlm

__all__ = [
    "ModelEndpointConfig",
    "ModelEndpointRegistry",
    "SelectableLlm",
]
