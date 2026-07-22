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
import contextvars
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Optional

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

# --- per-session model override -------------------------------------------
# A session can pin (endpoint_name, model_id) in its state (`/model` in chat);
# ModelSessionPlugin copies that into this contextvar on EVERY before_model
# callback (value or None — never stale). ADK runs the callback and the model
# call in the same async task, and `asyncio.to_thread` copies the context into
# its worker, so `_resolve_delegate` sees the override for exactly the calls
# belonging to that session's turn. Unset/None → the registry's global active
# endpoint (the Settings-managed default).
_SESSION_MODEL: "contextvars.ContextVar[Optional[tuple[str, str]]]" = (
    contextvars.ContextVar("adk_cc_session_model", default=None)
)


def set_session_model_override(value: Optional[tuple[str, str]]) -> None:
    """Set (endpoint_name, model_id) for the current task's model calls, or
    None to follow the global default. Called by ModelSessionPlugin."""
    _SESSION_MODEL.set(value)


def _cap_from(raw, *, default: Optional[int], warn_name: Optional[str] = None) -> Optional[int]:
    """Normalize one max-output-tokens value to a litellm ``max_tokens``.

    Empty/None → ``default``; non-int → ``default`` (warns if ``warn_name`` is
    given); ``n > 0`` → ``n``; ``n <= 0`` → ``None`` (explicitly uncapped). The
    single home of the "0/negative means uncapped" rule, so the per-endpoint and
    env knobs can't drift apart."""
    if raw is None or raw == "":
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        if warn_name:
            _log.warning("%s=%r is not an int — using default", warn_name, raw)
        return default
    return n if n > 0 else None  # 0 / negative → explicitly uncapped


def resolve_max_output_tokens(cfg: "Optional[ModelEndpointConfig]" = None) -> Optional[int]:
    """The BASE output-token cap (litellm ``max_tokens``).

    Precedence: per-endpoint ``max_tokens`` override > ``ADK_CC_MAX_OUTPUT_TOKENS``
    > a sane default (8192). A value of 0 (or negative) — at EITHER the
    per-endpoint or the env level — opts OUT (uncapped; the endpoint's own
    default applies)."""
    per_endpoint = getattr(cfg, "max_tokens", None) if cfg is not None else None
    if per_endpoint is not None:
        return _cap_from(per_endpoint, default=None)  # 0 / negative → uncapped
    return _cap_from(
        os.environ.get("ADK_CC_MAX_OUTPUT_TOKENS"),
        default=_DEFAULT_MAX_OUTPUT_TOKENS,
        warn_name="ADK_CC_MAX_OUTPUT_TOKENS",
    )


def escalated_max_output_tokens() -> Optional[int]:
    """The cap to escalate to after a model truncates mid tool-call
    (finish_reason=MAX_TOKENS) — cf. Claude Code escalating 8k→64k so the retry
    has headroom. ``ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED`` overrides; 0 disables."""
    return _cap_from(
        os.environ.get("ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED"),
        default=_DEFAULT_ESCALATED_MAX_TOKENS,
    )


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
#
# Loop-safety (load-bearing): callers pace from MANY event loops — uvicorn's
# serving loop, and the throwaway `asyncio.run()` loops the memory subsystem
# spins for synth/resolve/canonicalize. An asyncio.Lock here binds to the
# first loop that contends on it and then KILLS every other loop's model
# calls with "bound to a different event loop" (field failure, 2026-07-21;
# pinned by tests/test_model_pacing.py). So the throttle uses slot
# RESERVATION under a threading.Lock — held for arithmetic only, never
# across an await — and sleeps outside it. No asyncio primitive is shared.
# --------------------------------------------------------------------------
_pace_state_lock = threading.Lock()
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
    """Space model-call starts by the configured minimum interval.

    Slot reservation: atomically claim the next start time under the
    threading lock (loop-agnostic; arrival order = slot order), then sleep
    to the claimed slot OUTSIDE any lock. Global across every event loop
    and thread in the process — see the loop-safety note above.

    A caller cancelled mid-sleep has already claimed its slot, so one
    interval goes unused; rare (user abort) and harmless — the call was
    attempted, and the rate cap is still honored.
    """
    interval = _model_min_interval()
    if interval <= 0:
        return
    global _pace_last_at
    with _pace_state_lock:
        now = time.monotonic()
        start = max(now, _pace_last_at + interval)
        _pace_last_at = start
        wait = start - now
    if wait > 0:
        await asyncio.sleep(wait)


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
        default_delegate_factory: "Optional[Callable[[Optional[int]], BaseLlm]]" = None,
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
        self._default_delegate_factory = default_delegate_factory
        self._cache: dict[tuple, BaseLlm] = {}
        self._lock = threading.RLock()
        # Claude-Code-style escalation: once the model hits finish_reason=
        # MAX_TOKENS, raise a process-wide floor under the env/default cap for
        # subsequent calls, so the model's truncated-tool-call retry has more
        # room. `_cap_floor` is None until the first truncation, then the
        # escalated value — monotonic-sticky, only ever raises. The escalated
        # default delegate is a higher-cap rebuild of the boot delegate, built
        # lazily on first need via `default_delegate_factory`.
        self._cap_floor: Optional[int] = None
        self._escalated_default_delegate: Optional[BaseLlm] = None
        # endpoint names we've already warned about (session pin → deleted).
        self._warned_missing_override: set[str] = set()

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

    def _session_override_cfg(self, reg) -> "Optional[ModelEndpointConfig]":  # noqa: ANN001
        """The session-pinned endpoint config, or None to use the global active.

        The override names an (endpoint, model_id) PAIR: the session may pick
        any model the provider offers without mutating the provider's global
        `model` field (a model_copy is handed to the cache/builder instead).
        A pinned endpoint that no longer exists falls back to the global
        default with a warning — a deleted provider must not brick the
        session."""
        ov = _SESSION_MODEL.get()
        if not ov or reg is None:
            return None
        name, model_id = ov
        cfg = reg.get(name)
        if cfg is None:
            if name not in self._warned_missing_override:
                self._warned_missing_override.add(name)
                _log.warning(
                    "session pinned model endpoint %r no longer exists — "
                    "falling back to the global default", name,
                )
            return None
        if model_id and model_id != cfg.model:
            cfg = cfg.model_copy(update={"model": model_id})
        return cfg

    def _resolve_delegate(self) -> BaseLlm:
        """Return the BaseLlm for the session-pinned endpoint if the current
        task carries an override, else the globally-active endpoint.

        Falls back to the default delegate when there's no registry or no
        active endpoint (panel off / not yet configured)."""
        reg = self._get_registry()
        active = self._session_override_cfg(reg)
        if active is None:
            active = reg.get_active() if reg is not None else None
        if active is None:
            if self._default_delegate is None:
                raise RuntimeError(
                    "SelectableLlm has no active endpoint and no default delegate"
                )
            return self._escalated_default() if self._cap_floor is not None else self._default_delegate

        # Cache key includes the INLINE key too — replacing an endpoint's
        # stored api_key must rebuild the delegate, not serve the stale one.
        key = (active.model, active.api_base, active.api_key_env,
               getattr(active, "api_key", None))
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
        # ChatGPT-subscription (Codex OAuth) endpoints are not LiteLLM/OpenAI-key
        # backends — they use a subscription Bearer token against the Codex
        # backend. Model id `chatgpt-codex/<model>` selects this provider.
        if str(cfg.model).startswith("chatgpt-codex/"):
            from .chatgpt_codex import ChatGptCodexLlm

            return ChatGptCodexLlm(
                model=str(cfg.model).split("/", 1)[1],
                effort=getattr(cfg, "reasoning_effort", None),
            )

        from google.adk.models.lite_llm import LiteLlm

        # LiteLLM routes by the id's provider prefix. Registry endpoints are
        # OpenAI-compatible by product contract (ModelEndpointConfig docstring;
        # discovery reads {api_base}/models with the OpenAI wire format), so
        # their ids must route as `openai/<raw-id>` — but select-model stores
        # DISCOVERED ids verbatim (e.g. OpenRouter's
        # `google/gemma-4-31b-it:free`), which LiteLLM can't route ("LLM
        # Provider NOT provided", killing every call). Normalize at RESOLUTION
        # time: heals already-broken registries with no migration and keeps
        # discovery lists raw for display.
        model_id = str(cfg.model)
        if not model_id.startswith("openai/"):
            model_id = f"openai/{model_id}"
        kwargs: dict[str, Any] = {"model": model_id, "api_base": cfg.api_base}
        if cfg.requires_key():
            api_key = cfg.resolve_api_key()
            if not api_key:
                # FAIL LOUD. Previously a missing key was silently omitted,
                # so LiteLlm was built with no key and failed downstream with
                # an opaque "litellm authentication" error against the
                # provider. Surface the actual config problem instead. (Only
                # reachable on the legacy env path — an inline api_key that
                # is non-empty always resolves.)
                raise ValueError(
                    f"model endpoint {cfg.name!r} expects an api key but none "
                    f"resolves: env var {cfg.api_key_env!r} is not set in the "
                    f"server process. Store the key on the endpoint "
                    f"(api_key), set the env var, or use an empty api_key "
                    f"for a keyless local endpoint."
                )
            kwargs["api_key"] = api_key
        # else: intentionally keyless endpoint (empty api_key / api_key_env) — no key.
        base = resolve_max_output_tokens(cfg)
        # Escalation lifts only the env/default cap; an explicit per-endpoint
        # ``max_tokens`` (including 0 = uncapped) is the operator's deliberate
        # choice and is left exactly as set.
        explicit = getattr(cfg, "max_tokens", None) is not None
        max_tokens = base if explicit else self._effective_cap(base)
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        return LiteLlm(**kwargs)

    def _effective_cap(self, base: Optional[int]) -> Optional[int]:
        """Lift ``base`` to the escalation floor once the model has hit
        MAX_TOKENS. Never lowers a cap; never caps an already-uncapped base."""
        floor = self._cap_floor
        if floor is None or base is None:
            return base
        return floor if floor > base else base

    def _escalated_default(self) -> BaseLlm:
        """The boot/default delegate rebuilt at the escalated cap (it was built
        once at the base cap; escalation needs a copy with more room). Rebuilt via
        ``default_delegate_factory`` — the same builder agent.py uses for the boot
        delegate — so there is no scraping of LiteLlm internals. Best-effort:
        falls back to the base delegate if no factory was supplied or it fails."""
        with self._lock:
            if self._escalated_default_delegate is not None:
                return self._escalated_default_delegate
            esc = self._default_delegate
            if self._default_delegate_factory is not None:
                try:
                    esc = self._default_delegate_factory(
                        self._effective_cap(resolve_max_output_tokens())
                    )
                except Exception as e:  # noqa: BLE001 — never break on a rebuild
                    _log.debug("escalated default rebuild failed (%s) — using base", e)
                    esc = self._default_delegate
            self._escalated_default_delegate = esc
            return esc

    def _on_max_tokens(self) -> None:
        """Handle finish_reason=MAX_TOKENS. The FIRST time, escalate the cap floor
        for subsequent calls (so the model's truncated-tool-call retry has more
        room) and rebuild delegates at the higher cap — mirrors Claude Code's
        8k→64k escalation. Once escalated, later hits are silent (the floor is
        already raised), so we don't spam the log."""
        if self._cap_floor is not None:
            return  # already escalated — nothing more to do
        esc = escalated_max_output_tokens()
        base = resolve_max_output_tokens()
        if not esc or (base is not None and esc <= base):
            # Escalation disabled, or it wouldn't actually raise the cap — log the
            # root cause without a misleading "escalating" claim or cache churn.
            _log.warning(
                "SelectableLlm: model %s hit finish_reason=MAX_TOKENS — output cap "
                "reached; a tool call may be truncated.", self.model,
            )
            return
        with self._lock:
            self._cap_floor = esc
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
        # ADK's LiteLlm uses `llm_request.model or self.model` — and the flow
        # stamps llm_request.model from OUR `self.model`, which is the RAW
        # registry id kept for display. That would override the ROUTED id
        # `_build_litellm` configured on the delegate (field failure: raw
        # OpenRouter ids reached litellm unprefixed even after constructor-side
        # normalization). Align the request with the delegate — the delegate's
        # model is authoritative for the wire. Inert for chatgpt-codex, which
        # reads only its own self.model.
        if getattr(delegate, "model", None) and hasattr(llm_request, "model"):
            llm_request.model = delegate.model
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
