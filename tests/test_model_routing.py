"""Regression: registry model ids must be LiteLLM-ROUTABLE at delegate build.

Field failure (desktop, 2026-07-22): adding an OpenRouter provider and
picking `google/gemma-4-31b-it:free` from its discovered list stored the raw
upstream id verbatim; LiteLLM then died on every call with "LLM Provider NOT
provided" — the turn (and session title) failed, the app looked hung.

Registry endpoints are OpenAI-compatible by product contract
(ModelEndpointConfig's own docstring; discovery reads {api_base}/models with
the OpenAI wire format), so their ids must route as `openai/<raw-id>`.
Normalization happens at RESOLUTION time in `_build_litellm` — healing
already-broken registries with no migration and keeping discovery lists raw.

Run: `uv run python tests/test_model_routing.py`
"""

from __future__ import annotations

import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.models import ModelEndpointConfig, SelectableLlm  # noqa: E402


def _delegate_model(cfg: ModelEndpointConfig) -> str:
    sel = SelectableLlm(registry=None, default_model_id="boot")
    return str(sel._build_litellm(cfg).model)


def test_raw_discovered_id_gets_routed():
    """THE field failure: a raw OpenRouter id must reach LiteLLM as
    `openai/<raw>` so the api_base route is used."""
    cfg = ModelEndpointConfig(
        name="Openrouter",
        model="google/gemma-4-31b-it:free",  # stored verbatim by select-model
        api_base="https://openrouter.ai/api/v1",
        api_key="sk-or-dummy",
    )
    got = _delegate_model(cfg)
    assert got == "openai/google/gemma-4-31b-it:free", got
    print("OK raw_discovered_id_gets_routed")


def test_already_prefixed_id_unchanged():
    """The env-seeded default (`openai/openai/gpt-oss-120b`) must pass
    through untouched — no double prefixing."""
    cfg = ModelEndpointConfig(
        name="default",
        model="openai/openai/gpt-oss-120b",
        api_base="https://integrate.api.nvidia.com/v1",
        api_key="dummy",
    )
    got = _delegate_model(cfg)
    assert got == "openai/openai/gpt-oss-120b", got
    print("OK already_prefixed_id_unchanged")


def test_plain_local_model_id_gets_routed():
    """A bare local-server id (`Qwen3-...`, keyless) routes the same way."""
    cfg = ModelEndpointConfig(
        name="local",
        model="Qwen3-32B-MLX",
        api_base="http://localhost:18000/v1",
        api_key="",  # keyless
    )
    got = _delegate_model(cfg)
    assert got == "openai/Qwen3-32B-MLX", got
    print("OK plain_local_model_id_gets_routed")


def test_codex_provider_untouched():
    """chatgpt-codex/<model> selects the subscription provider — must NOT be
    rewritten into an openai/ route."""
    cfg = ModelEndpointConfig(
        name="chatgpt",
        model="chatgpt-codex/gpt-5.5",
        api_base="unused",
        api_key="",
    )
    sel = SelectableLlm(registry=None, default_model_id="boot")
    d = sel._build_litellm(cfg)
    assert type(d).__name__ == "ChatGptCodexLlm", type(d).__name__
    print("OK codex_provider_untouched")


def test_request_model_id_is_aligned_to_delegate():
    """THE precedence trap that bit in the field: ADK's LiteLlm uses
    `llm_request.model or self.model`, and the flow stamps llm_request.model
    from SelectableLlm.model — the RAW display id — overriding the routed id
    on the delegate. generate_content_async must align the request with the
    delegate before delegating."""
    import asyncio

    from google.adk.models.llm_request import LlmRequest

    os.environ.pop("ADK_CC_MODEL_MIN_INTERVAL_S", None)
    os.environ.pop("ADK_CC_MODEL_MAX_RPM", None)

    class _FakeDelegate:
        # What _build_litellm would produce for the raw registry id.
        model = "openai/google/gemma-4-31b-it:free"

        async def generate_content_async(self, llm_request, stream=False):
            yield ("seen", llm_request.model)

    sel = SelectableLlm(registry=None, default_model_id="boot")
    sel._resolve_delegate = lambda: _FakeDelegate()  # type: ignore[method-assign]

    req = LlmRequest(model="google/gemma-4-31b-it:free")  # raw display id, as stamped by the flow

    async def drive():
        out = []
        async for item in sel.generate_content_async(req):
            out.append(item)
        return out

    out = asyncio.run(drive())
    assert out == [("seen", "openai/google/gemma-4-31b-it:free")], out
    assert req.model == "openai/google/gemma-4-31b-it:free", req.model
    print("OK request_model_id_is_aligned_to_delegate")


def main() -> None:
    test_raw_discovered_id_gets_routed()
    test_already_prefixed_id_unchanged()
    test_plain_local_model_id_gets_routed()
    test_codex_provider_untouched()
    test_request_model_id_is_aligned_to_delegate()
    print("\nall model-routing tests passed")


if __name__ == "__main__":
    main()
