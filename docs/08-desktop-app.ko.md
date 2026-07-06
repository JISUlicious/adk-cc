# 08 — 데스크톱 앱

읽기: [English](./08-desktop-app.md) · **한국어**

로그인·서버 운영이 필요 없는 **단일 사용자 로컬 데스크톱 앱**(Tauri). 네이티브
창이 Python 백엔드를 사이드카로 띄우고, 백엔드가 서빙하는 UI로 창을 이동시킨다.

## 개요

`src-tauri/src/main.rs`가 `uvicorn adk_cc.service.server:make_app`을
`127.0.0.1:8765`에서 단일 사용자 env(no-auth, sqlite 세션, encrypted-file
secrets, `noop` sandbox = 로컬 실행, tenancy `single`)로 실행한 뒤, splash에서
백엔드 URL로 창을 넘긴다. 웹 UI와 **같은** React 앱을 `VITE_ADK_CC_DESKTOP=1`
(`web/dist-desktop`)로 빌드한 것이며, 차이는 오른쪽 패널뿐이다 — 웹의 artifacts
목록 대신 세션 git worktree를 보여주는 **로컬 파일 트리**.

## 데이터 디렉터리

모든 상태는 `~/.adk-cc-desktop/` 아래에 있다(`$ADK_CC_DESKTOP_DATA`로 변경 가능):

```
settings.env            # 사용자 설정 (아래)
sessions.db             # sqlite 세션 저장소
worktrees/<proj>/<sess> # 세션별 git worktree (파일 패널의 루트)
secrets/                # encrypted-file 크리덴셜 저장소
credential.key          # 시크릿 저장소용 Fernet 키
```

## 설정 — `settings.env`

첫 실행 시 데이터 디렉터리에 주석 처리된 `settings.env` 템플릿이 생성된다. 편집
후 재실행하면 된다. 데스크톱 컨텍스트에서는 dotenv 부트스트랩
(`adk_cc/__init__.py`)이 이 파일을 **가장 먼저** 로드하므로 repo/cwd `.env`보다
우선한다(단, 실제 프로세스 env 변수가 있으면 그게 최우선).

```
# ~/.adk-cc-desktop/settings.env
ADK_CC_API_KEY=sk-...
ADK_CC_API_BASE=https://integrate.api.nvidia.com/v1
ADK_CC_MODEL=openai/z-ai/glm-5.1
# ADK_CC_MODEL_MAX_RPM=30      # 선택
```

우선순위: `$ADK_CC_SETTINGS_FILE`, 없으면 `$ADK_CC_DESKTOP_DATA/settings.env`,
없으면 `~/.adk-cc-desktop/settings.env`. 키가 없어도 **부팅은 된다**(UI는 뜨고
경고를 남김) — 키를 넣기 전까지는 모델 호출만 실패한다.

## dev 실행 (repo에서)

Python 환경(`uv sync`)과 데스크톱 프론트엔드 빌드가 필요하다.

**네이티브 창** — `tauri-cli` 필요(`cargo install tauri-cli`):

```
cd src-tauri && cargo tauri dev     # beforeDevCommand가 dist-desktop을 빌드하고,
                                    # main.rs가 repo/.venv에서 백엔드를 실행
```

**서버만** (네이티브 창 없이 빠르게 확인 — 브라우저로 접속):

```
npm --prefix web run build:desktop
ADK_CC_DESKTOP=1 ADK_CC_ALLOW_NO_AUTH=1 ADK_CC_SERVE_UI=1 \
  ADK_CC_UI_DIST="$PWD/web/dist-desktop" ADK_CC_AGENTS_DIR="$PWD/agents" \
  ADK_CC_SANDBOX_BACKEND=noop \
  .venv/bin/uvicorn adk_cc.service.server:make_app --factory --port 8000
# → http://127.0.0.1:8000
```

## 인스톨러 — 단일 파일 AppImage

아무것도 미리 깔려 있지 않은 머신(Python/pip/Node/Rust/WebKit 불필요)을 위해,
단일 파일 x86_64 Linux AppImage를 빌드한다:

```
./scripts/build-appimage.sh          # → dist/adk-cc-x86_64.AppImage  (Docker 필요)
```

대상 머신에서:

```
chmod +x adk-cc-x86_64.AppImage && ./adk-cc-x86_64.AppImage
```

첫 실행 시 `~/.adk-cc-desktop/settings.env`가 생성된다 — 키를 채우고 재실행.
모델 엔드포인트가 그 머신에서 접근 가능해야 한다. 빌드·패키징 상세, 에뮬레이션
노트, 번들 구성은 [`packaging/appimage/README.md`](../packaging/appimage/README.md)
참고.

## 동작 방식 (relocatable)

`main.rs::resolve_layout()`가 앱 자신의 위치에서 경로를 고른다:

| | 패키지 (AppImage) | dev (repo) |
|---|---|---|
| 인터프리터 | `$APPDIR/usr/lib/adk-cc/python/bin/python3` | `repo/.venv/bin/python` |
| agents | `$APPDIR/usr/lib/adk-cc/agents` | `repo/agents` |
| 프론트엔드 | `$APPDIR/usr/lib/adk-cc/dist-desktop` | `repo/web/dist-desktop` |

두 경우 모두 `python -m uvicorn`으로 실행하며, 패키지일 때는
`PYTHONPATH=agents`를 설정해 `adk_cc`를 번들된 소스에서 임포트한다(대상 머신에
pip install 불필요).

## 참고

- 인스톨러는 **x86_64 Linux** 대상. 빌드 아키텍처는
  `ADK_CC_APPIMAGE_PLATFORM=linux/arm64`로 변경.
- 에이전트는 설정된 모델 엔드포인트에 접근할 수 있어야 한다 — 내장 모델은 없다.
- 데스크톱 모드는 `noop` sandbox를 쓴다 — `run_bash`와 파일 도구는 세션의 로컬
  worktree에서 직접 동작한다.
