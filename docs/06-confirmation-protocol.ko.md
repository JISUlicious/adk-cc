# Tool-confirmation 프로토콜

`PermissionPlugin`이 tool 호출에 사용자 확인이 필요하다고 결정하면(DEFAULT 모드의 파괴적 작업, 또는 매칭되는 ASK 규칙), ADK의 `tool_context.request_confirmation(hint=..., payload=...)`을 통해 tool을 일시정지합니다. ADK는 `requested_tool_confirmations[<function_call_id>]`을 통해 frontend에 일시정지를 노출하고, 사용자가 응답하면 `tool_context.tool_confirmation`이 채워진 채로 tool을 다시 호출합니다.

adk-cc는 ADK의 `payload` 필드를 사용하여 frontend가 라벨 있는 버튼으로 렌더링할 수 있는 **구조화된 prompt**를 보내고, 사용자 선택을 설명하는 **구조화된 응답**을 읽습니다. 두 계층이 협력하여 frontend 간에 동작:

- **`PermissionPlugin`** — 구조화된 `ConfirmPrompt` 페이로드를 생성하고 재개된 답변(`chose_id`)을 읽음. 이것이 모든 payload-aware frontend에 대한 wire 계약.
- **`ConfirmationFormUiPlugin`** (선택, 기본 활성) — wrapper event 이름을 변경하고 `response_schema`를 주입하여 프로토콜을 **번들 `adk web`**의 long-running form 위젯에 브리지. 이 플러그인이 활성이면 번들 UI가 코드 변경 없이 N-option dropdown을 렌더링. 비활성으로 하면 번들 `adk web`의 이진 체크박스 위젯으로 회귀 — `PermissionPlugin`은 어느 쪽이든 그 아래에서 동작.

이 문서는 두 계층 모두에 대한 wire 계약입니다.

## Outbound (plugin → frontend)

`requested_tool_confirmations[<call_id>].payload`는 `ConfirmPrompt` dict입니다:

```json
{
  "style": "single_select",
  "title": "Confirm run_bash?",
  "detail": "destructive run_bash requires confirmation",
  "options": [
    {"id": "allow_once",   "label": "Allow once",   "description": "Run this one time. Future similar calls will ask again."},
    {"id": "allow_always", "label": "Allow always", "description": "Run, and stop asking about this exact operation for the rest of the session."},
    {"id": "deny",         "label": "Deny",         "description": "Cancel; the model will see the denial and adjust."}
  ]
}
```

**필드**

- `style`: 판별자. 오늘 정의된 두 값:
  - `"single_select"` — N 옵션, 사용자가 하나 선택. 파괴적 tool gate에서 사용 (위의 3 옵션).
  - `"confirm_deny"` — 이진. id가 `"allow"`와 `"deny"`인 두 옵션. `confirm_deny_prompt` 헬퍼로 사용 가능하지만 **현재** gate의 기본은 아님.
- `title` — 짧은 요약, dialog 헤더에 적합.
- `detail` — 엔진의 이유 텍스트. `ToolConfirmation`의 `hint` 필드를 미러 (payload를 렌더링하지 않는 frontend는 이 문자열을 봄).
- `options` — `{id, label, description}` 목록:
  - `id`는 안정적인 라우팅 키. 플러그인이 이를 기반으로 동작; UI는 `label`/`description`을 자유롭게 변경 가능.
  - `label`은 버튼 텍스트 (1–4 단어).
  - `description`은 옵션별 설명 (한 문장).

Frontend는 `style`에 따라 렌더링을 switch해야 합니다. 알 수 없는 style은 `hint`를 렌더링하고 `confirmed: bool`을 제출하는 것으로 fallback — 플러그인의 back-compat 경로가 이를 처리합니다.

## Inbound (frontend → plugin)

Frontend는 ADK의 표준 `ToolConfirmation` 형태로 응답을 제출:

```python
ToolConfirmation(confirmed: bool, payload: Any | None, hint: str)
```

구조화된 프로토콜을 사용하려면 `payload`를 다음으로 설정:

```json
{"chose_id": "allow_once" | "allow_always" | "deny"}
```

플러그인은 먼저 `chose_id`를 읽습니다. 없으면(또는 string이 아니면) `confirmed: bool`로 fallback — 번들 `adk web` UI는 절대 `payload`를 보내지 않으므로 `confirmed: True`는 tool 실행, `confirmed: False`는 거부.

### `chose_id` semantics

| `chose_id` | 동작 |
|---|---|
| `"allow_once"` | Tool이 이번 한 번 실행. 영속 변경 없음. |
| `"allow"` (legacy) | `"allow_once"`와 동일 — 두 버튼 프로토콜 첫 컷과의 back-compat. |
| `"allow_always"` | Tool 실행 **및** 플러그인이 SESSION 스코프 ALLOW 규칙 주입 (아래 참고). 향후 매칭 호출은 세션 내내 자동 허용. |
| `"deny"` | `{"status": "permission_denied_by_user", ...}` 반환; 모델이 거부를 보고 조정. |
| (기타 문자열) | Fail-closed: deny로 처리. |

### "Allow always" 규칙 스코프

사용자가 `allow_always`를 선택하면 플러그인이 생성:

```python
PermissionRule(
    source=RuleSource.SESSION,
    behavior=RuleBehavior.ALLOW,
    tool_name=<the tool name>,
    rule_content=<extracted rule key>,
)
```

"Rule key"는 대부분의 운영자가 규칙을 작성하는 per-tool 문자열입니다 (`adk_cc/permissions/rules.py`의 `_RULE_KEY_EXTRACTORS` 참고):

| Tool | Rule key | "Allow always" 승인 예시 |
|---|---|---|
| `run_bash` | `command` arg | `git status` 승인은 정확히 `git status`를 cover. |
| `read_file` | `path` arg | `/etc/hosts` 승인은 정확히 `/etc/hosts`를 cover. |
| `write_file` | `path` arg | `/tmp/foo` 승인은 정확히 `/tmp/foo`를 cover. |
| `edit_file` | `path` arg | `write_file`과 같은 형태. |
| `glob_files` | `root` arg | root `.` 승인은 정확히 `.`을 cover. |
| `grep` | `path` arg | `read_file`과 같은 형태. |

스코프는 **의도적으로 좁음** — 정확한 rule-key 매치. 사용자는 명시적으로 이 작업을 승인했고, 광범위화(예: command에 fnmatch wildcard)는 안전하지 않을 것입니다. 사용자가 더 넓은 스코프를 원한다면 직접 config 규칙을 작성해야 합니다 (`adk_cc/permissions/permissions.yaml` 등).

Extractor 엔트리가 없는 tool(커스텀 user tool)의 경우, 규칙은 `rule_content`를 생략하여 "세션 동안 이 tool의 모든 호출"을 의미합니다. 이는 보수적 fallback — 사용자가 알 수 없는 tool을 한 번 승인했다고 영원히 열어주면 안 되지만, 더 좁게 스코프할 정보가 없음.

세션 규칙은 `PermissionPlugin`이 보유한 `SettingsHierarchy`에서 메모리에 상주합니다. 서버 재시작 시 영속되지 **않습니다**.

## 번들 `adk web` UI 브리지 — `ConfirmationFormUiPlugin`

ADK의 번들 `adk web` UI는 function-call name이 `adk_request_confirmation`인 모든 event에 대해 이진 위젯(체크박스 + 읽기 전용 payload textarea + Submit)을 하드코딩합니다. `ConfirmPrompt.options` 목록은 절대 화면에 도달하지 않음 — payload가 몇 개 옵션을 운반하든 UI는 체크박스 하나만 보여줍니다.

`ConfirmationFormUiPlugin`(기본 플러그인 체인에 등록)이 프로토콜의 양쪽을 다시 작성하여 번들 UI가 **form-widget** 경로를 대신 취하도록 합니다:

### 옵션당 boolean 필드 (string/enum이 아닌) 이유

번들 `adk web`의 form 위젯은 JSON Schema **`type`**만으로 필드를 렌더링 (`main-*.js`의 `initForm()` 참고):

```js
n === "boolean"          → checkbox
n === "integer/number"   → numeric input
else (incl. "string")    → free-form text input
```

`enum`은 **참고되지 않음** — `{type: "string", enum: [...]}` schema는 운영자가 id 중 하나를 수동 타이핑해야 하는 plain textbox로 렌더링 (그리고 오타는 작업을 조용히 deny). 번들 form에서 실제 "하나 선택" UI에 도달하는 유일한 경로는 **옵션당 boolean 필드 하나** — 각 옵션이 자체 체크박스로 렌더링, 운영자가 하나 체크, submit이 `{<chose_id>: true, ...}` 생성. 플러그인은 첫 `true` 값 키를 `chose_id`에 매핑하고 ADK의 resume processor를 위해 reshape.

"N 중 하나 선택"이 radio group이나 dropdown이 아닌 N개 체크박스로 렌더링되는 것이 어색하지만, 번들 UI는 둘 다 제공하지 않습니다. 각 체크박스의 label + description이 의도를 명확히 합니다.

### Outbound rewrite (event → 번들 UI)

- ADK가 emit한 `adk_request_confirmation` function-call event를 찾음.
- 각 `ConfirmPrompt.options[i]`가 옵션 id로 키된 boolean 속성이 되는 JSON schema 도출; description은 `<label> — <option description>`:
  ```json
  {
    "type": "object",
    "properties": {
      "allow_once":   {"type": "boolean", "description": "Allow once — Run this one time. …"},
      "allow_always": {"type": "boolean", "description": "Allow always — Stop asking …"},
      "deny":         {"type": "boolean", "description": "Deny — Cancel; the model …"}
    }
  }
  ```
- Schema를 `args.response_schema`에 주입.
- `args.prompt`에 title (+ 설정 시 detail) 작성하여 번들 UI의 form 위젯이 체크박스 목록 위에 보여줌.
- Function-call의 `name`을 `adk_request_confirmation`에서 sentinel `adk_cc_confirmation_form`으로 **변경** (선행 언더스코어 없음 — sglang 포함 일부 OpenAI 호환 백엔드는 `_`로 시작하는 function 이름을 거부). 번들 UI의 `isConfirmationRequest = (name === "adk_request_confirmation")` short-circuit이 더 이상 트리거되지 않음; UI는 form-widget 분기로 진행하여 옵션당 체크박스 하나를 렌더링.
- Function-call **id**는 보존. ADK의 resume processor는 name이 아닌 id에 매칭하므로 이 rename은 resume에 투명.
- 원래 `toolConfirmation.payload`(풍부한 `ConfirmPrompt`)도 rewritten event의 args에 **보존**. 커스텀 payload-aware frontend는 `adk_request_confirmation` 외에 sentinel name도 listen하면 여전히 읽을 수 있음.

### Inbound rewrite (번들 UI → plugin)

번들 UI의 form 위젯은 form 모델을 function_response의 `response`로 제출. Boolean-per-option schema에서 모델은 `{<chose_id_a>: false, <chose_id_b>: true, ...}`. `ConfirmationFormUiPlugin.on_user_message_callback`:

- Sentinel name을 가진 function_response 감지.
- `response`에 대해 다음 형태 중 어느 것이든 수용:
  - `{<chose_id>: true, ...}` — 현재 번들 UI form 위젯 출력. 첫 `true` 값 키가 선택. 예약 키(`confirmed`, `choice`, `chose_id`, `result`)는 실제 chose_id를 가리지 않도록 스캔 중 skip.
  - `{chose_id: "<id>"}` — 원래 PR-1 프로토콜을 사용하는 payload-aware 커스텀 frontend.
  - `{choice: "<id>"}` — legacy 번들 UI string-enum 형태 (이 플러그인 구 버전과의 back-compat).
  - `{result: "<id>"}` — 운영자가 id를 직접 타이핑했을 때의 번들 UI free-form textarea fallback.
- 응답을 ADK 표준 `{confirmed: <bool>, payload: {chose_id: <id>}}` (where `confirmed = chose_id != "deny"`)으로 reshape.
- Function_response를 `adk_request_confirmation`으로 다시 rename.

운영자가 아무것도 체크하지 않고 form을 제출하면(`{<all>: false}`), chose_id를 추출할 수 없음; 플러그인은 응답을 그대로 두어 ADK processor가 조용히 allow나 deny로 처리하기보다 깔끔한 "no confirmation" 에러를 surface하게 함.

ADK의 기존 `_RequestConfirmationLlmRequestProcessor`가 rewritten 응답을 pick up하여 플러그인이 없는 것처럼 정확히 gated tool을 재개합니다.

### 브리지 비활성화

번들 UI의 이진 위젯으로 회귀하려면 `adk_cc/agent.py`의 플러그인 목록에서 `ConfirmationFormUiPlugin()`을 제거하세요. 아래의 `PermissionPlugin`과 ADK의 request_confirmation flow는 계속 동작 — 확인은 여전히 파괴적 작업을 게이트하지만 이진 체크박스를 통해.

## Back-compat fallback (no payload)

구조화된 프로토콜을 말하지 않는 frontend — 그리고 `response_schema`가 렌더링되지 않을 때 번들 UI의 free-form textarea 경로 — 는 payload 없이 `ToolConfirmation(confirmed: bool)`을 제출합니다. `PermissionPlugin`이 이를 처리:

| Input | Plugin 동작 |
|---|---|
| `payload=None`, `confirmed=True` | Tool 실행 (allow_once 동등). 세션 규칙 없음. |
| `payload=None`, `confirmed=False` | Deny. |

따라서 점진적으로 업그레이드 가능: 자신의 일정에 따라 payload-aware frontend 출하; gate는 이것과 무관하게 end-to-end로 동작.

## React 챗 UI는 다른 경로

`web/`의 커스텀 React UI는 wire에서 구조화된 `ConfirmPrompt` 페이로드를 직접 읽고 (`web/src/components/ConfirmationCard.tsx`) 응답으로 `{chose_id, comment?, persist_across_sessions?}`를 회신합니다. 번들 UI rewrite 플러그인을 건너뜁니다 — `adk_request_confirmation`과 `adk_cc_confirmation_form` (rewrite plugin이 활성인 경우의 sentinel name) 두 function-call name을 모두 매칭하고 어느 경우에도 `args.toolConfirmation.payload`에서 동일한 페이로드를 추출. 자세한 내용은 [07-web-ui.ko.md](./07-web-ui.ko.md) 참고.

## 구현 포인터

- Outbound prompt 구성: `adk_cc/permissions/confirmation.py` (gate용 `allow_once_always_deny_prompt`; 이진 케이스용 `confirm_deny_prompt`).
- Wire-out: `PermissionPlugin.before_tool_callback`이 `adk_cc/plugins/permissions.py`에서 `tool_context.request_confirmation(hint=..., payload=prompt.model_dump())` 호출.
- Wire-in: 같은 콜백이 `_read_choice_id(tool_context.tool_confirmation)`을 읽고 id에 따라 라우팅.
- 세션 규칙 주입: `PermissionPlugin._add_session_allow`.
- 번들 UI 브리지: `adk_cc/plugins/confirmation_form_ui.py` (sentinel name + 양방향 reshape).
- Unit 테스트: `tests/test_permissions_confirmation.py` (PermissionPlugin)과 `tests/test_confirmation_form_ui.py` (번들 UI 브리지).
- E2E 테스트: `tests/e2e_confirmation_flow.py` (PermissionPlugin 단독)과 `tests/e2e_confirmation_form_ui.py` (`InMemoryRunner`를 통한 전체 브리지).

## Scope 밖 (아직)

- **모델**이 사용자에게 옵션 중 하나를 선택하라고 묻는 "다음 대안 중 하나 선택" flow (**플러그인**이 승인을 묻는 것이 아닌). 그것은 single-pick 의미를 가진 `ask_user_question`과 유사한 새 tool이 될 것. `ConfirmPrompt`의 `style` 판별자는 준비됨; plumbing은 아님.
- 재시작 간 세션 규칙 영속화. 오늘은 플러그인 인스턴스의 메모리에 있음.
- 사용자가 세션 중간에 "allow always" 결정을 철회하는 방법. Settings hierarchy는 세션 규칙 추가는 지원하지만 제거는 지원하지 않음.
