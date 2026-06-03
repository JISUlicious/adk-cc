"""`SelectableLlm` — a BaseLlm delegate that resolves the active endpoint
per request.

The agent holds ONE SelectableLlm as its model. ADK reads
`agent.canonical_model` fresh on every invocation and calls
`generate_content_async` / `connect` on it, so resolving the active endpoint
inside those methods means an admin switching the active endpoint takes
effect on the NEXT request — no agent rebuild, no restart.

Design choices:
  - Delegates to a per-endpoint LiteLlm, built lazily and CACHED by
    (model, api_base, api_key) so we don't reconstruct a client each call.
  - Never mutates a shared LiteLlm's attributes (that would race across
    concurrent requests); a switch just selects a different cached delegate.
  - If no registry / no active endpoint, falls back to a fixed default
    LiteLlm (the boot model) so behavior is unchanged when the panel is off.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any, AsyncGenerator, Optional

from google.adk.models.base_llm import BaseLlm

if TYPE_CHECKING:
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse

    from .endpoints import ModelEndpointRegistry


class SelectableLlm(BaseLlm):
    """A BaseLlm whose underlying model is chosen per-request from a registry.

    `model` (the BaseLlm field) is kept in sync with the resolved endpoint's
    model id for display/telemetry, but the actual generation always goes
    through the freshly-resolved delegate.
    """

    # Pydantic model (BaseLlm is a pydantic BaseModel) — declare our extra
    # attributes so assignment is allowed.
    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def __init__(
        self,
        *,
        registry: "Optional[ModelEndpointRegistry]" = None,
        registry_path_env: Optional[str] = None,
        default_delegate: Optional[BaseLlm] = None,
        default_model_id: str = "",
    ) -> None:
        # Initialize the BaseLlm `model` field with the current active id (or
        # the default) for display.
        super().__init__(model=default_model_id or "selectable")
        # Use object.__setattr__-friendly assignment via pydantic extra.
        self._registry = registry
        # When `registry_path_env` is given, the registry is resolved LAZILY
        # from that env var on first use rather than at construction. This is
        # deliberate: the agent module (and thus this object) is imported
        # eagerly at package load, BEFORE make_app's _prepare_admin_env sets
        # the registry-file env var. Resolving lazily means the admin panel's
        # config is picked up regardless of import order.
        self._registry_path_env = registry_path_env
        self._default_delegate = default_delegate
        self._cache: dict[tuple, BaseLlm] = {}
        self._lock = threading.RLock()

    def _get_registry(self) -> "Optional[ModelEndpointRegistry]":
        if self._registry is not None:
            return self._registry
        if self._registry_path_env:
            path = os.environ.get(self._registry_path_env)
            if path:
                from .endpoints import ModelEndpointRegistry

                self._registry = ModelEndpointRegistry(path)
                return self._registry
        return None

    # -- delegate resolution -------------------------------------------

    def _resolve_delegate(self) -> BaseLlm:
        """Return the BaseLlm for the currently-active endpoint.

        Falls back to the default delegate when there's no registry or no
        active endpoint (panel off / not yet configured)."""
        reg = self._get_registry()
        active = reg.get_active() if reg is not None else None
        if active is None:
            if self._default_delegate is None:
                raise RuntimeError(
                    "SelectableLlm has no active endpoint and no default delegate"
                )
            return self._default_delegate

        key = (active.model, active.api_base, active.api_key_env)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self.model = active.model
                return cached
            delegate = self._build_litellm(active)
            self._cache[key] = delegate
            self.model = active.model
            return delegate

    def _build_litellm(self, cfg) -> BaseLlm:  # noqa: ANN001
        from google.adk.models.lite_llm import LiteLlm

        kwargs: dict[str, Any] = {"model": cfg.model, "api_base": cfg.api_base}
        if cfg.requires_key():
            api_key = cfg.resolve_api_key()
            if not api_key:
                # FAIL LOUD. Previously a missing key was silently omitted,
                # so LiteLlm was built with no key and failed downstream with
                # an opaque "litellm authentication" error against the
                # provider. Surface the actual config problem instead.
                raise ValueError(
                    f"model endpoint {cfg.name!r} references api_key_env "
                    f"{cfg.api_key_env!r}, but that environment variable is "
                    f"not set in the server process. Set it (or, for an "
                    f"endpoint that needs no auth, clear api_key_env)."
                )
            kwargs["api_key"] = api_key
        # else: intentionally keyless endpoint (api_key_env == "") — no key.
        return LiteLlm(**kwargs)

    # -- BaseLlm interface (delegate everything) -----------------------

    async def generate_content_async(
        self, llm_request: "LlmRequest", stream: bool = False
    ) -> "AsyncGenerator[LlmResponse, None]":
        delegate = self._resolve_delegate()
        async for resp in delegate.generate_content_async(llm_request, stream=stream):
            yield resp

    def connect(self, llm_request: "LlmRequest"):
        return self._resolve_delegate().connect(llm_request)
