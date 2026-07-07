// adk-cc desktop shell.
//
// Runs the Python backend as a single-user local sidecar (no login) and points
// the window at the backend-served UI — so the webview loads everything
// same-origin and the React app needs no desktop-specific networking.
//
//   1. setup(): spawn `uvicorn make_app` on a fixed port with the single-user
//      env (no-auth, sqlite sessions, encrypted-file secrets under ~/.adk-cc-
//      desktop, noop sandbox), serving web/dist-desktop.
//   2. a background thread polls /list-apps until the sidecar answers, then
//      navigates the window from the splash to http://127.0.0.1:8765/.
//   3. on app exit, the child is killed.
//
// Path resolution is relocatable (see `resolve_layout`): in a packaged build
// the backend interpreter + agents + frontend live under `$APPDIR/usr/lib/adk-cc`
// (the AppImage mount); in dev they resolve from the compile-time repo dir.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

const PORT: u16 = 8765;

/// Holds the backend child so we can kill it when the app exits.
struct BackendChild(Mutex<Option<Child>>);

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let data = data_dir(app.handle());
            std::fs::create_dir_all(&data).ok();

            let child = spawn_backend(&data).expect("failed to spawn adk-cc backend");
            app.manage(BackendChild(Mutex::new(Some(child))));

            // Wait for the sidecar, then swap the splash for the served UI.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                for _ in 0..240 {
                    if backend_ready(PORT) {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(500));
                }
                if let Some(w) = handle.get_webview_window("main") {
                    let _ = w.eval(&format!(
                        "window.location.replace('http://127.0.0.1:{PORT}/')"
                    ));
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building adk-cc desktop");

    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(state) = handle.try_state::<BackendChild>() {
                if let Ok(mut guard) = state.0.lock() {
                    if let Some(mut child) = guard.take() {
                        let _ = child.kill();
                    }
                }
            }
        }
    });
}

/// Per-user data dir: `~/.adk-cc-desktop` (no spaces — keeps the sqlite URL
/// clean, unlike macOS "Application Support").
fn data_dir(handle: &tauri::AppHandle) -> PathBuf {
    let home = handle
        .path()
        .home_dir()
        .unwrap_or_else(|_| PathBuf::from("."));
    home.join(".adk-cc-desktop")
}

/// Dev: the repo is the parent of src-tauri (compile-time path).
fn repo_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

/// A Fernet key for the encrypted-file credential store: 32 random bytes,
/// url-safe base64 — generated once and persisted so secrets survive restarts.
fn ensure_fernet_key(data: &PathBuf) -> String {
    let path = data.join("credential.key");
    if let Ok(existing) = std::fs::read_to_string(&path) {
        let trimmed = existing.trim().to_string();
        if !trimmed.is_empty() {
            return trimmed;
        }
    }
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).expect("rng");
    use base64::Engine;
    let key = base64::engine::general_purpose::URL_SAFE.encode(bytes);
    std::fs::write(&path, &key).ok();
    key
}

/// Where the backend interpreter + app files live. Packaged: bundled inside the
/// AppImage under `$APPDIR/usr/lib/adk-cc`. Dev: the compile-time repo.
struct Layout {
    python: PathBuf, // interpreter — we run `-m uvicorn`
    agents: PathBuf, // ADK_CC_AGENTS_DIR (+ PYTHONPATH when bundled)
    dist: PathBuf,   // ADK_CC_UI_DIST
    cwd: PathBuf,
    bundled: bool,
}

fn resolve_layout() -> Layout {
    if let Ok(appdir) = std::env::var("APPDIR") {
        let base = PathBuf::from(appdir).join("usr/lib/adk-cc");
        return Layout {
            python: base.join("python/bin/python3"),
            agents: base.join("agents"),
            dist: base.join("dist-desktop"),
            cwd: base.clone(),
            bundled: true,
        };
    }
    let repo = repo_dir();
    Layout {
        python: repo.join(".venv/bin/python"),
        agents: repo.join("agents"),
        dist: repo.join("web/dist-desktop"),
        cwd: repo,
        bundled: false,
    }
}

fn spawn_backend(data: &PathBuf) -> std::io::Result<Child> {
    let layout = resolve_layout();
    let key = ensure_fernet_key(data);
    let session_dsn = format!("sqlite:///{}/sessions.db", data.display());

    // Run `python -m uvicorn` (works for both the dev venv and the bundled
    // standalone python). Model config (API key/endpoint) comes from the user's
    // settings.env in the data dir, loaded by the backend's dotenv bootstrap;
    // load_dotenv does not override these explicit vars.
    let mut cmd = Command::new(&layout.python);
    cmd.current_dir(&layout.cwd)
        .args([
            "-m",
            "uvicorn",
            "adk_cc.service.server:make_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
        ])
        .env("ADK_CC_AGENTS_DIR", &layout.agents)
        .env("ADK_CC_ALLOW_NO_AUTH", "1")
        .env("ADK_CC_DESKTOP", "1")
        .env("ADK_CC_DESKTOP_DATA", data)
        .env("ADK_CC_TENANCY_MODE", "single")
        .env("ADK_CC_GLOBAL_TENANT_ID", "local")
        .env("ADK_CC_SERVE_UI", "1")
        .env("ADK_CC_UI_DIST", &layout.dist)
        .env("ADK_CC_SESSION_DSN", session_dsn)
        .env("ADK_CC_CREDENTIAL_PROVIDER", "encrypted_file")
        .env("ADK_CC_CREDENTIAL_STORE_DIR", data.join("secrets"))
        .env("ADK_CC_CREDENTIAL_KEY", key)
        // Personal skills store: enables Settings → Skills (list / add folder /
        // upload / delete) and the agent's tenant-skill discovery reads the same dir.
        .env("ADK_CC_TENANT_SKILLS_DIR", data.join("skills"))
        // Wiki + per-project memory + the knowledge-graph view (/knowledge). Stores
        // live under the data dir; the graph is scoped to the current project.
        .env("ADK_CC_WIKI", "1")
        .env("ADK_CC_WIKI_ROOT", data.join("wiki"))
        .env("ADK_CC_MEMORY", "1")
        .env("ADK_CC_MEMORY_ROOT", data.join("memory"))
        .env("ADK_CC_KNOWLEDGE_UI", "1")
        .env("ADK_CC_SANDBOX_BACKEND", "noop");
    // Packaged: adk_cc isn't pip-installed — import it from the shipped source
    // (deps live in the bundled python). Mirrors the dev editable install.
    if layout.bundled {
        cmd.env("PYTHONPATH", &layout.agents);
    }
    cmd.spawn()
}

/// Readiness probe — a raw HTTP/1.0 GET /list-apps (the endpoint the desktop
/// BackendReady gate also uses; no-auth mode doesn't mount /auth/config).
fn backend_ready(port: u16) -> bool {
    let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let req = format!(
        "GET /list-apps HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 64];
    match stream.read(&mut buf) {
        Ok(n) => std::str::from_utf8(&buf[..n])
            .map(|t| t.contains(" 200"))
            .unwrap_or(false),
        Err(_) => false,
    }
}
