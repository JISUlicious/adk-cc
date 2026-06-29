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

import asyncio
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Optional

from google.adk.models.base_llm import BaseLlm

if TYPE_CHECKING:
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse

    from .endpoints import ModelEndpointConfig, ModelEndpointRegistry


# Always cap output by default (like Claude Code: CAPPED_DEFAULT_MAX_TOKENS=8000,
# escalated to ESCALATED_MAX_TOKENS=64000 on truncation). A cap prevents a model
# stopping mid tool-call when an endpoint's own default output limit is too low —
# the root cause behind truncated tool-call JSON (plugins/tolerant_tool_json).
_DEFAULT_MAX_OUTPUT_TOKENS = 8192
_DEFAULT_ESCALATED_MAX_TOKENS = 32768


def resolve_max_output_tokens(cfg: "Optional[ModelEndpointConfig]" = None) -> Optional[int]:
    """The BASE output-token cap (litellm ``max_tokens``).

    Precedence: per-endpoint ``max_tokens`` override > ``ADK_CC_MAX_OUTPUT_TOKENS``
    > a sane default (8192). ``ADK_CC_MAX_OUTPUT_TOKENS=0`` (or negative) opts OUT
    (uncapped — the endpoint's own default applies)."""
    per_endpoint = getattr(cfg, "max_tokens", None) if cfg is not None else None
    if per_endpoint:
        return int(per_endpoint)
    raw = os.environ.get("ADK_CC_MAX_OUTPUT_TOKENS")
    if raw is None or raw == "":
        return _DEFAULT_MAX_OUTPUT_TOKENS
    try:
        n = int(raw)
    except ValueError:
        _log.warning("ADK_CC_MAX_OUTPUT_TOKENS=%r is not an int — using default", raw)
        return _DEFAULT_MAX_OUTPUT_TOKENS
    return n if n > 0 else None  # 0 / negative → explicitly uncapped


def escalated_max_output_tokens() -> Optional[int]:
    """The cap to escalate to after a model truncates mid tool-call
    (finish_reason=MAX_TOKENS) — cf. Claude Code escalating 8k→64k so the retry
    has headroom. ``ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED`` overrides; 0 disables."""
    raw = os.environ.get("ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED")
    if raw is None or raw == "":
        return _DEFAULT_ESCALATED_MAX_TOKENS
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_ESCALATED_MAX_TOKENS
    return n if n > 0 else None


# --------------------------------------------------------------------------
# Process-global model rate-limit throttle (opt-in).
#
# Hosted endpoints often enforce a shared requests-per-minute cap, and a
# single agent turn fans out into several model calls (coordinator + tool
# round-trips + out-of-band session-title / memory-capture / librarian
# classification + synthesis). Bursting that trips 429/500s and starves
# follow-up calls. When ADK_CC_MODEL_MAX_RPM (or ADK_CC_MODEL_MIN_INTERVAL_S)
# is set, every model call through SelectableLlm waits so call STARTS are
# spaced by the configured minimum — paces all callers (agent, plugins,
# crons) uniformly without limiting their concurrency. Default off (no cost).
# --------------------------------------------------------------------------
_pace_creation_lock = threading.Lock()
_pace_lock: Optional["asyncio.Lock"] = None
_pace_last_at: float = 0.0

_log = logging.getLogger(__name__)


def _model_min_interval() -> float:
    """Minimum seconds between model-call starts; 0 disables the throttle."""
    rpm = os.environ.get("ADK_CC_MODEL_MAX_RPM")
    if rpm:
        try:
            v = float(rpm)
            return 60.0 / v if v > 0 else 0.0
        except ValueError:
            return 0.0
    try:
        return max(0.0, float(os.environ.get("ADK_CC_MODEL_MIN_INTERVAL_S", "")))
    except ValueError:
        return 0.0


async def _pace_model_call() -> None:
    interval = _model_min_interval()
    if interval <= 0:
        return
    global _pace_lock, _pace_last_at
    if _pace_lock is None:
        with _pace_creation_lock:
            if _pace_lock is None:
                _pace_lock = asyncio.Lock()
    async with _pace_lock:
        now = time.monotonic()
        wait = _pace_last_at + interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        _pace_last_at = time.monotonic()


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
        # Claude-Code-style escalation: once the model hits finish_reason=
        # MAX_TOKENS, raise the effective cap for subsequent calls (the model's
        # truncated-tool-call retry then has more room). Monotonic-sticky for the
        # process; only ever RAISES a cap. The escalated default delegate is a
        # higher-cap rebuild of the boot delegate, lazily built on first need.
        self._escalated = False
        self._escalated_default_delegate: Optional[BaseLlm] = None

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
            return self._escalated_default() if self._escalated else self._default_delegate

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
        max_tokens = self._effective_cap(resolve_max_output_tokens(cfg))
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        return LiteLlm(**kwargs)

    def _effective_cap(self, base: Optional[int]) -> Optional[int]:
        """The cap to use for the next call: the escalated value once the model
        has hit MAX_TOKENS, else the base. Never lowers a cap; never caps an
        already-uncapped base."""
        if base is None or not self._escalated:
            return base
        esc = escalated_max_output_tokens()
        return esc if (esc and esc > base) else base

    def _escalated_default(self) -> BaseLlm:
        """A higher-cap rebuild of the boot/default delegate (it was built once
        with the base cap; escalation needs a copy at the escalated cap). Reads
        the boot delegate's own kwargs (api_base/api_key live in LiteLlm's
        _additional_args). Best-effort — falls back to the base delegate."""
        with self._lock:
            if self._escalated_default_delegate is not None:
                return self._escalated_default_delegate
            base = self._default_delegate
            esc = base
            cap = self._effective_cap(resolve_max_output_tokens())
            try:
                from google.adk.models.lite_llm import LiteLlm

                args = dict(getattr(base, "_additional_args", {}) or {})
                if cap:
                    args["max_tokens"] = cap
                esc = LiteLlm(model=getattr(base, "model", self.model), **args)
            except Exception as e:  # noqa: BLE001 — never break on a rebuild
                _log.debug("escalated default rebuild failed (%s) — using base", e)
                esc = base
            self._escalated_default_delegate = esc
            return esc

    def _on_max_tokens(self) -> None:
        """Handle finish_reason=MAX_TOKENS: log the root cause and, the first
        time, escalate the effective cap for subsequent calls (rebuild delegates
        at the higher cap). Mirrors Claude Code's 8k→64k escalation."""
        esc = escalated_max_output_tokens()
        if self._escalated or not esc:
            _log.warning(
                "SelectableLlm: model %s hit finish_reason=MAX_TOKENS — a tool "
                "call may be truncated (output cap reached).", self.model,
            )
            return
        with self._lock:
            self._escalated = True
            self._cache.clear()  # rebuild registry delegates at the escalated cap
            self._escalated_default_delegate = None
        _log.warning(
            "SelectableLlm: model %s hit finish_reason=MAX_TOKENS — escalating "
            "max_tokens to %d for subsequent calls (set "
            "ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED=0 to disable).",
            self.model, esc,
        )

    # -- BaseLlm interface (delegate everything) -----------------------

    async def generate_content_async(
        self, llm_request: "LlmRequest", stream: bool = False
    ) -> "AsyncGenerator[LlmResponse, None]":
        await _pace_model_call()  # opt-in global rate-limit throttle
        # Resolve the delegate OFF the event loop: the first call (cache miss)
        # builds the LiteLlm delegate — and litellm's cold import is ~hundreds of
        # ms — and EVERY call reads the model-registry file. Both are blocking;
        # on the loop they'd stall all requests (health checks included) during
        # the first model turn. to_thread keeps the loop free. (The build is
        # cached, so steady-state resolves are a tiny off-loop file read.)
        delegate = await asyncio.to_thread(self._resolve_delegate)
        async for resp in delegate.generate_content_async(llm_request, stream=stream):
            # Root cause for tolerant_tool_json's truncation recovery: a
            # MAX_TOKENS finish means the model ran out of output budget — likely
            # mid tool-call. Log it AND escalate the cap (Claude-Code-style) so
            # the model's truncated-tool-call retry has more room.
            if getattr(getattr(resp, "finish_reason", None), "name", None) == "MAX_TOKENS":
                self._on_max_tokens()
            yield resp

    async def warm(self) -> None:
        """Pre-build the active delegate OFF the loop (e.g. at server startup),
        so the first request doesn't pay litellm's cold import. Best-effort:
        config errors (no active endpoint / missing key) are swallowed — the
        request path's offloaded resolve is the fallback."""
        try:
            await asyncio.to_thread(self._resolve_delegate)
            _log.info("SelectableLlm: model delegate warmed (%s)", self.model)
        except Exception as e:  # noqa: BLE001 — warm-up must never break startup
            _log.debug("SelectableLlm.warm skipped (%s: %s)", type(e).__name__, e)

    def connect(self, llm_request: "LlmRequest"):
        return self._resolve_delegate().connect(llm_request)
