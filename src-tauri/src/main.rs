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
use std::os::unix::process::CommandExt;
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

            // #98: own the port. A stale orphan from an unclean exit used to
            // hold 8765 — the fresh child died on bind failure and the
            // splash-wait silently adopted YESTERDAY'S code. Reclaim first.
            match reclaim_port() {
                PortState::Free => {}
                PortState::OtherInstanceAlive => {
                    show_error(app.handle(), "adk-cc desktop is already running.");
                    return Ok(());
                }
                PortState::Foreign(who) => {
                    show_error(
                        app.handle(),
                        &format!(
                            "Port {PORT} is in use by another process:<br><code>{who}</code><br>\
                             Quit it and relaunch adk-cc desktop."
                        ),
                    );
                    return Ok(());
                }
            }

            let child = spawn_backend(&data).expect("failed to spawn adk-cc backend");
            let child_pid = child.id();
            let _ = std::fs::write(pidfile(&data), child_pid.to_string());
            app.manage(BackendChild(Mutex::new(Some(child))));

            // Wait for the sidecar, then swap the splash for the served UI —
            // but only after verifying the responder IS the child we spawned
            // (pid via /api/desktop/version), never whatever answers HTTP.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                for _ in 0..240 {
                    if backend_ready(PORT) {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(500));
                }
                if backend_pid_matches(PORT, child_pid) {
                    if let Some(w) = handle.get_webview_window("main") {
                        let _ = w.eval(&format!(
                            "window.location.replace('http://127.0.0.1:{PORT}/')"
                        ));
                    }
                } else {
                    show_error(
                        &handle,
                        &format!(
                            "The process answering on port {PORT} is not the \
                             backend this app started. Refusing to attach."
                        ),
                    );
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building adk-cc desktop");

    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            let mut our_pid: Option<u32> = None;
            if let Some(state) = handle.try_state::<BackendChild>() {
                if let Ok(mut guard) = state.0.lock() {
                    if let Some(mut child) = guard.take() {
                        our_pid = Some(child.id());
                        // TERM first (uvicorn shuts down cleanly), KILL as
                        // the backstop.
                        terminate_child(&mut child);
                    }
                }
            }
            // Remove the pidfile ONLY if it is ours: an instance that lost
            // the port race (error page, no child) must not delete the
            // winner's pidfile on quit.
            if let Some(pid) = our_pid {
                let data = data_dir(handle);
                let recorded = std::fs::read_to_string(pidfile(&data))
                    .ok()
                    .and_then(|s| s.trim().parse::<u32>().ok());
                if recorded == Some(pid) {
                    let _ = std::fs::remove_file(pidfile(&data));
                }
            }
        }
    });
}

fn pidfile(data: &PathBuf) -> PathBuf {
    data.join("backend.pid")
}

/// TERM the child's process GROUP → up to 3s grace → KILL the group.
/// Group-wide so the backend's own children (in-flight run_bash commands)
/// go with it; the backend is spawned as its group leader (pgid == pid).
fn terminate_child(child: &mut Child) {
    let group = format!("-{}", child.id());
    let _ = Command::new("kill").args(["-TERM", "--", &group]).status();
    for _ in 0..30 {
        if let Ok(Some(_)) = child.try_wait() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = Command::new("kill").args(["-KILL", "--", &group]).status();
    let _ = child.wait();
}

enum PortState {
    Free,
    OtherInstanceAlive,
    Foreign(String),
}

/// If something already answers on PORT, classify THE PROCESS THAT OWNS THE
/// PORT (via lsof) — never the pidfile's claim: a recycled or copy-pasted
/// pid could name an innocent adk-cc process bound elsewhere (e.g. a test
/// server on another port), and killing it while the real owner survived
/// would be worse than the bug this fixes. The pidfile is corroboration
/// only. Never adopt.
fn reclaim_port() -> PortState {
    if TcpStream::connect(("127.0.0.1", PORT)).is_err() {
        return PortState::Free; // nothing listening
    }
    let Some(owner) = port_owner(PORT) else {
        return PortState::Foreign(format!("unknown listener on port {PORT}"));
    };
    let command = ps_field(owner, "command=").unwrap_or_default();
    if !(command.contains("uvicorn") && command.contains("adk_cc")) {
        return PortState::Foreign(if command.is_empty() {
            format!("pid {owner} on port {PORT}")
        } else {
            command
        });
    }
    // A backend whose parent app still lives means a SECOND app instance —
    // killing its backend out from under it would be worse than declining.
    if let Some(ppid) = ps_field(owner, "ppid=").and_then(|s| s.trim().parse::<u32>().ok()) {
        if ppid != 1 {
            return PortState::OtherInstanceAlive;
        }
    }
    // Our orphan (an adk-cc backend, re-parented to launchd, HOLDING our
    // port). Group TERM → grace → group KILL → per-pid KILL fallback for
    // legacy orphans that were not group leaders.
    let group = format!("-{owner}");
    let _ = Command::new("kill").args(["-TERM", "--", &group]).status();
    let _ = Command::new("kill").args(["-TERM", &owner.to_string()]).status();
    for _ in 0..30 {
        if TcpStream::connect(("127.0.0.1", PORT)).is_err() {
            return PortState::Free;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = Command::new("kill").args(["-KILL", "--", &group]).status();
    let _ = Command::new("kill").args(["-KILL", &owner.to_string()]).status();
    for _ in 0..20 {
        if TcpStream::connect(("127.0.0.1", PORT)).is_err() {
            return PortState::Free;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    PortState::Foreign(format!("stale backend pid {owner} refused to die"))
}

/// Pid that owns the LISTEN socket on `port` (lsof; macOS/Linux).
fn port_owner(port: u16) -> Option<u32> {
    let out = Command::new("lsof")
        .args(["-tnP", &format!("-iTCP:{port}"), "-sTCP:LISTEN"])
        .output()
        .ok()?;
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .next()?
        .trim()
        .parse::<u32>()
        .ok()
}

fn ps_field(pid: u32, field: &str) -> Option<String> {
    let out = Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", field])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if text.is_empty() { None } else { Some(text) }
}

/// GET /api/desktop/version and check the reported pid — "the responder is
/// the child I spawned", not "something answered HTTP". Retries briefly so
/// one dropped request during a cold boot can't strand a healthy pair on
/// the error page.
fn backend_pid_matches(port: u16, expect_pid: u32) -> bool {
    for attempt in 0..3 {
        if attempt > 0 {
            std::thread::sleep(Duration::from_millis(700));
        }
        if let Some(body) = http_get(port, "/api/desktop/version") {
            if body.contains(&format!("\"pid\":{expect_pid}"))
                || body.contains(&format!("\"pid\": {expect_pid}"))
            {
                return true;
            }
        }
    }
    false
}

fn http_get(port: u16, path: &str) -> Option<String> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let req = format!(
        "GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut body = String::new();
    let _ = stream.read_to_string(&mut body);
    Some(body)
}

fn show_error(handle: &tauri::AppHandle, message: &str) {
    if let Some(w) = handle.get_webview_window("main") {
        let html = format!(
            "document.body.innerHTML = '<div style=\"font-family:system-ui;\
             padding:3em;max-width:34em;margin:auto\"><h2>adk-cc desktop</h2>\
             <p>{}</p></div>'",
            message.replace('\'', "\\'")
        );
        let _ = w.eval(&html);
    }
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
    // 0600: this key decrypts every stored secret (model API keys, MCP tokens,
    // and now remote SSH passwords). It was created with the process umask,
    // which on a stock system leaves it world-readable.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
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
        // #98: the backend's parent watchdog exits the child when this pid
        // dies — no orphan can hold the port, even after SIGKILL of the app.
        .env("ADK_CC_PARENT_PID", std::process::id().to_string())
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
    // Own process group: the backend's own children (run_bash commands,
    // provisioning subprocesses) die with it — group TERM/KILL from both the
    // watchdog and our exit path reaches them, and a killpg can never touch
    // an unrelated shell.
    cmd.process_group(0);
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
