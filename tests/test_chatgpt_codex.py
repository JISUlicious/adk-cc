"""Offline tests for the ChatGPT-subscription (Codex) provider.

Pure mapping / routing / auth-file assertions — NO network, NO token needed.
The live inference path (text, tools, multi-turn) is exercised manually against
the real subscription backend; this pins the request-shaping + routing so a
refactor can't silently break them.
"""
from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from adk_cc.models import codex_auth
from adk_cc.models.chatgpt_codex import ChatGptCodexLlm
from adk_cc.models.endpoints import ModelEndpointConfig
from adk_cc.models.selectable import SelectableLlm


def _llm() -> ChatGptCodexLlm:
    return ChatGptCodexLlm(model="gpt-5.5", effort="low")


def test_input_items_mapping() -> None:
    llm = _llm()
    fc = types.FunctionCall(id="call_1", name="get_weather", args={"city": "Paris"})
    contents = [
        types.Content(role="user", parts=[types.Part(text="hi")]),
        types.Content(role="model", parts=[types.Part(text="hello"), types.Part(function_call=fc)]),
        types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            id="call_1", name="get_weather", response={"temp": 18}))]),
    ]
    items = llm._input_items(contents)
    # user text -> input_text message
    assert items[0] == {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    # assistant text -> output_text message
    assert items[1] == {"role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}
    # function_call -> its own item with call_id/name/arguments(json string)
    assert items[2]["type"] == "function_call"
    assert items[2]["call_id"] == "call_1" and items[2]["name"] == "get_weather"
    assert json.loads(items[2]["arguments"]) == {"city": "Paris"}
    # function_response -> function_call_output linked by the same call_id
    assert items[3]["type"] == "function_call_output" and items[3]["call_id"] == "call_1"
    assert json.loads(items[3]["output"]) == {"temp": 18}
    print("OK test_input_items_mapping")


def test_build_body_shape() -> None:
    llm = _llm()
    req = LlmRequest(
        model="gpt-5.5",
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
        config=types.GenerateContentConfig(system_instruction="be terse"),
    )
    body = llm._build_body(req)
    assert body["model"] == "gpt-5.5"
    assert body["store"] is False              # mandatory for the codex backend
    assert body["stream"] is True
    assert body["instructions"] == "be terse"
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}
    assert "max_output_tokens" not in body and "max_completion_tokens" not in body
    print("OK test_build_body_shape")


def test_tools_mapping() -> None:
    llm = _llm()
    tool = types.Tool(function_declarations=[types.FunctionDeclaration(
        name="run", description="run a thing",
        parameters=types.Schema(type=types.Type.OBJECT,
                                properties={"cmd": types.Schema(type=types.Type.STRING)},
                                required=["cmd"]))])
    req = LlmRequest(model="gpt-5.5", contents=[],
                     config=types.GenerateContentConfig(tools=[tool]))
    body = llm._build_body(req)
    tools = body["tools"]
    assert tools[0]["type"] == "function"       # FLAT Responses shape, not {function:{...}}
    assert tools[0]["name"] == "run"
    assert tools[0]["parameters"]["type"] == "object"
    assert "cmd" in tools[0]["parameters"]["properties"]
    assert body["tool_choice"] == "auto"
    print("OK test_tools_mapping")


def test_instructions_default_never_empty() -> None:
    llm = _llm()
    req = LlmRequest(model="gpt-5.5", contents=[], config=types.GenerateContentConfig())
    # the codex backend requires a non-empty instructions field
    assert llm._build_body(req)["instructions"]
    print("OK test_instructions_default_never_empty")


def test_selectable_routes_to_codex() -> None:
    reg_path = os.path.join(tempfile.mkdtemp(), "reg.json")
    with open(reg_path, "w") as f:
        json.dump({"endpoints": [{"name": "cx", "model": "chatgpt-codex/gpt-5.5",
                                  "api_base": "https://chatgpt.com/backend-api/codex",
                                  "api_key_env": "", "reasoning_effort": "low"}],
                   "active": "cx"}, f)
    os.environ["ADK_CC_TEST_CODEX_REG"] = reg_path
    llm = SelectableLlm(registry_path_env="ADK_CC_TEST_CODEX_REG",
                        default_delegate=None, default_model_id="chatgpt-codex/gpt-5.5")
    delegate = llm._resolve_delegate()
    assert type(delegate).__name__ == "ChatGptCodexLlm"
    assert delegate.model == "gpt-5.5"
    assert delegate._effort == "low"
    print("OK test_selectable_routes_to_codex")


def test_auth_load_status_and_writeback() -> None:
    # Minimal fake auth.json (structurally like the Codex CLI's) — a syntactic
    # JWT access token with an exp far in the future; no real credentials.
    import base64, time

    def jwt(payload: dict) -> str:
        seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"h.{seg}.s"

    access = jwt({"exp": int(time.time()) + 3600,
                  "https://api.openai.com/auth": {"chatgpt_plan_type": "plus",
                                                  "chatgpt_account_id": "acc-123456"}})
    path = os.path.join(tempfile.mkdtemp(), "auth.json")
    with open(path, "w") as f:
        json.dump({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                   "tokens": {"id_token": "x", "access_token": access,
                              "refresh_token": "rt", "account_id": "acc-123456"},
                   "last_refresh": "2026-01-01T00:00:00Z"}, f)
    os.environ["ADK_CC_CODEX_AUTH_FILE"] = path
    try:
        st = codex_auth.connection_status()
        assert st["connected"] and st["plan"] == "plus" and st["account_id_tail"] == "123456"
        assert st["expired"] is False
        tok = codex_auth.load_tokens()
        assert tok.account_id == "acc-123456"
        codex_auth._save_tokens(tok)  # write-back preserves structure + perms
        after = json.load(open(path))
        assert set(after) == {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    finally:
        os.environ.pop("ADK_CC_CODEX_AUTH_FILE", None)
    print("OK test_auth_load_status_and_writeback")


def test_oauth_pkce_and_url() -> None:
    import base64
    import hashlib
    import urllib.parse

    from adk_cc.models import codex_oauth as ox

    v, c = ox._pkce()
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    url = ox._authorize_url("STATE", c, "http://localhost:1455/auth/callback")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["client_id"] == codex_auth.CLIENT_ID
    assert q["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert q["code_challenge_method"] == "S256" and q["code_challenge"]
    assert q["codex_cli_simplified_flow"] == "true" and q["originator"] == "codex_cli_rs"
    print("OK test_oauth_pkce_and_url")


def test_oauth_start_binds_and_cancels() -> None:
    import time
    import urllib.parse

    from adk_cc.models import codex_oauth as ox

    try:
        url = ox.start()
    except RuntimeError:
        print("SKIP test_oauth_start_binds_and_cancels (callback port busy)")
        return
    assert ox.status()["state"] == "waiting"
    assert urllib.parse.urlparse(url).netloc == "auth.openai.com"
    ox.cancel()
    time.sleep(0.2)
    print("OK test_oauth_start_binds_and_cancels")


def test_own_store_priority_and_clear() -> None:
    import base64
    import time

    os.environ.pop("ADK_CC_CODEX_AUTH_FILE", None)
    store_dir = tempfile.mkdtemp()
    empty_codex_home = tempfile.mkdtemp()  # no auth.json -> CLI login "absent"
    os.environ["ADK_CC_CODEX_STORE_DIR"] = store_dir
    os.environ["CODEX_HOME"] = empty_codex_home
    try:
        acc = "h." + base64.urlsafe_b64encode(json.dumps(
            {"exp": int(time.time()) + 3600,
             "https://api.openai.com/auth": {"chatgpt_plan_type": "pro",
                                             "chatgpt_account_id": "acc-XYZ789"}}
        ).encode()).decode().rstrip("=") + ".s"
        p = codex_auth.save_new_login(access_token=acc, refresh_token="rt2")
        assert p == codex_auth.own_store_path()
        st = codex_auth.connection_status()
        assert st["connected"] and st["plan"] == "pro" and st["mode"] == "own"
        assert st["account_id_tail"] == "XYZ789"
        assert codex_auth.clear_login() is True
        # own login gone AND no CLI login -> disconnected
        assert codex_auth.connection_status()["connected"] is False
    finally:
        os.environ.pop("ADK_CC_CODEX_STORE_DIR", None)
        os.environ.pop("CODEX_HOME", None)
    print("OK test_own_store_priority_and_clear")


def main() -> None:
    test_input_items_mapping()
    test_build_body_shape()
    test_tools_mapping()
    test_instructions_default_never_empty()
    test_selectable_routes_to_codex()
    test_auth_load_status_and_writeback()
    test_oauth_pkce_and_url()
    test_oauth_start_binds_and_cancels()
    test_own_store_priority_and_clear()
    print("\nall chatgpt-codex offline tests passed")


if __name__ == "__main__":
    main()
