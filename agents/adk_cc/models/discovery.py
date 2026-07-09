"""Model discovery — list the models a provider offers via its OpenAI-compatible
``/models`` endpoint, so the UI can offer a picker instead of a free-text field.

Handles both response shapes:
  - OpenAI / LiteLLM:      ``{"data": [{"id": "..."}]}``
  - Codex subscription:    ``{"models": [{"slug": "..."}]}``  (auth via codex_auth)
"""

from __future__ import annotations

from typing import Optional

from . import codex_auth

# api_base for the ChatGPT-subscription (Codex) backend.
CODEX_BASE = "https://chatgpt.com/backend-api/codex"


def _ids(data) -> list[str]:  # noqa: ANN001
    items = data.get("data") or data.get("models") or [] if isinstance(data, dict) else data
    out: list[str] = []
    for m in items or []:
        if isinstance(m, str):
            out.append(m)
        elif isinstance(m, dict):
            mid = m.get("id") or m.get("slug") or m.get("name")
            if mid:
                out.append(mid)
    # stable, de-duped
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


async def list_models(
    api_base: str, *, api_key: Optional[str] = None, use_codex_auth: bool = False
) -> list[str]:
    """Model ids offered by the provider at ``api_base``. ``use_codex_auth`` uses
    the ChatGPT subscription token + Codex headers; otherwise a Bearer ``api_key``
    (or none for a keyless local server)."""
    import httpx

    headers: dict[str, str] = {}
    params: Optional[dict] = None
    if use_codex_auth:
        access, account = await codex_auth.get_access()
        headers = {
            "Authorization": f"Bearer {access}",
            "ChatGPT-Account-ID": account,
            "originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/0.0.0 (adk-cc)",
        }
        params = {"client_version": "0.0.0"}
    elif api_key:
        headers = {"Authorization": f"Bearer {api_key}"}

    url = api_base.rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers, params=params)
    r.raise_for_status()
    return _ids(r.json())
