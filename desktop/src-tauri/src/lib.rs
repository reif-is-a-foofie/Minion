// Minion desktop shell.
//
// Responsibilities:
// - Resolve (and create) the user's Minion data dir + inbox
// - Discover the Python sidecar source (bundled under `Resources/sidecar` in
//   the shipped .app, or a dev checkout walking up from current_exe)
// - First-launch bootstrap: find a system `python3 >= 3.10`, create a venv
//   under `<data_dir>/venv`, pip install the bundled sidecar requirements.
//   Streams `sidecar://status` events to the UI so the window isn't blank.
// - Spawn the Python API sidecar as a managed child process, using the
//   bootstrapped venv and the bundled source tree (no compile-time paths).
// - Expose minimal Tauri commands the frontend uses:
//     app_config, copy_into_inbox, reveal_in_finder, restart_sidecar
// Native OS file drops are delivered to the frontend by Tauri v2 as the
// `tauri://drag-drop` event; the frontend forwards the paths to
// `copy_into_inbox`.

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter, Manager, WindowEvent};

// ---------------------------------------------------------------------------
// Debug NDJSON instrumentation (active until post-release verification).
// Writes one JSON line per significant event to the session logfile defined
// by $MINION_DEBUG_LOG (set during `cargo tauri build` for this session).
// Safe to leave in place — overhead is a single append() when the env var
// is set, zero otherwise.
// ---------------------------------------------------------------------------
fn dbg(event: &str, data: serde_json::Value) {
    let path = match std::env::var("MINION_DEBUG_LOG") {
        Ok(p) if !p.is_empty() => p,
        _ => return,
    };
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    let payload = serde_json::json!({
        "sessionId": "d21adc",
        "location": format!("lib.rs:{event}"),
        "message": event,
        "data": data,
        "timestamp": ts,
    });
    if let Ok(line) = serde_json::to_string(&payload) {
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&path) {
            let _ = writeln!(f, "{line}");
        }
    }
}

// Folders that are almost never what the user meant to index. Skipped while
// walking dropped directories. Keep small and conservative -- the Python
// parser registry already drops unsupported extensions.
const SKIP_DIRS: &[&str] = &[
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
    ".svelte-kit",
    ".next",
    ".nuxt",
    ".cache",
    ".DS_Store",
];

fn should_skip_dir(name: &str) -> bool {
    SKIP_DIRS.iter().any(|s| *s == name)
}

struct AppState {
    sidecar: Mutex<Option<Child>>,
    ollama: Mutex<Option<Child>>,
    ollama_bin: Option<PathBuf>,
    /// Model currently wired into the Python sidecar via MINION_VISION_MODEL.
    /// `None` means captioning is off.
    vision_model: Mutex<Option<String>>,
    data_dir: PathBuf,
    inbox: PathBuf,
    api_port: u16,
    /// Directory containing api.py (bundled resource in prod, dev checkout
    /// otherwise). Set once by setup(); used by every sidecar respawn.
    sidecar_src_dir: Mutex<Option<PathBuf>>,
    /// Path to the venv Python that runs the sidecar. Set after bootstrap.
    sidecar_python: Mutex<Option<PathBuf>>,
}

// moondream: 1.7GB vs llava's 4.5GB, purpose-built for image captioning,
// noticeably more stable on memory-constrained Macs. Override with the
// MINION_VISION_MODEL env var if you want llava or another vision model.
const DEFAULT_VISION_MODEL: &str = "moondream";
const OLLAMA_PORT: u16 = 11434;

// ---------------------------------------------------------------------------
// Path resolution
// ---------------------------------------------------------------------------

fn resolve_data_dir() -> PathBuf {
    if let Ok(p) = std::env::var("MINION_DATA_DIR") {
        return PathBuf::from(p);
    }
    if let Some(base) = dirs::data_dir() {
        return base.join("Minion").join("data");
    }
    PathBuf::from(".minion/data")
}

fn resolve_inbox(data_dir: &Path) -> PathBuf {
    if let Ok(p) = std::env::var("MINION_INBOX") {
        return PathBuf::from(p);
    }
    data_dir.join("inbox")
}

// ---------------------------------------------------------------------------
// Sidecar
// ---------------------------------------------------------------------------

/// Locate the sidecar source directory containing `api.py`. Resolution order:
///   1. $MINION_SIDECAR_DIR env override (user or test harness)
///   2. Tauri bundled resource `<Resources>/sidecar/src/api.py` (shipped path)
///   3. Dev fallback: walk up from current_exe looking for
///      `chatgpt_mcp_memory/src/api.py`
/// Returns the directory containing api.py (cwd for the sidecar process).
fn resolve_sidecar_src_dir(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("MINION_SIDECAR_DIR") {
        let pb = PathBuf::from(p);
        if pb.join("api.py").exists() {
            dbg("sidecar_src_dir", serde_json::json!({"via": "env", "path": pb}));
            return Some(pb);
        }
    }
    if let Ok(res_dir) = app.path().resource_dir() {
        let c1 = res_dir.join("sidecar").join("src");
        if c1.join("api.py").exists() {
            dbg("sidecar_src_dir", serde_json::json!({"via": "resource", "path": c1}));
            return Some(c1);
        }
        let c2 = res_dir.join("_up_").join("sidecar").join("src");
        if c2.join("api.py").exists() {
            dbg("sidecar_src_dir", serde_json::json!({"via": "resource_up", "path": c2}));
            return Some(c2);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut cur = exe.parent().map(Path::to_path_buf);
        for _ in 0..8 {
            let Some(c) = cur.as_ref() else { break };
            let cand = c.join("chatgpt_mcp_memory").join("src");
            if cand.join("api.py").exists() {
                dbg("sidecar_src_dir", serde_json::json!({"via": "dev_walk", "path": cand}));
                return Some(cand);
            }
            cur = c.parent().map(Path::to_path_buf);
        }
    }
    dbg("sidecar_src_dir", serde_json::json!({"via": "none", "path": serde_json::Value::Null}));
    None
}

/// Locate the requirements file bundled alongside the sidecar source. Looks
/// in the same Resources tree as [`resolve_sidecar_src_dir`]; returns `None`
/// in dev if not staged (caller falls back to the repo's requirements.txt).
fn resolve_sidecar_requirements(app: &AppHandle, src_dir: &Path) -> Option<PathBuf> {
    // Shipped layout: <Resources>/sidecar/requirements.txt (sibling of src/)
    if let Some(parent) = src_dir.parent() {
        let r = parent.join("requirements.txt");
        if r.exists() {
            return Some(r);
        }
    }
    if let Ok(res_dir) = app.path().resource_dir() {
        let r = res_dir.join("sidecar").join("requirements.txt");
        if r.exists() {
            return Some(r);
        }
    }
    // Dev fallback: <repo>/chatgpt_mcp_memory/requirements.txt
    if let Some(grand) = src_dir.parent().and_then(Path::parent) {
        let r = grand.join("requirements.txt");
        if r.exists() {
            return Some(r);
        }
    }
    None
}

/// Find a usable system `python3 >= 3.10` on PATH. Returns an absolute path
/// (so relaunches from a different cwd still work) and the version string.
fn find_system_python() -> Option<(PathBuf, String)> {
    for name in ["python3.12", "python3.11", "python3.10", "python3"] {
        let out = match Command::new(name).arg("--version").output() {
            Ok(o) => o,
            Err(_) => continue,
        };
        if !out.status.success() {
            continue;
        }
        let ver = String::from_utf8_lossy(&out.stdout).to_string()
            + &String::from_utf8_lossy(&out.stderr);
        let ver = ver.trim().to_string();
        if let Some(rest) = ver.strip_prefix("Python 3.") {
            if let Some(minor_str) = rest.split('.').next() {
                if let Ok(minor) = minor_str.trim().parse::<u32>() {
                    if minor >= 10 {
                        let abs = Command::new("which")
                            .arg(name)
                            .output()
                            .ok()
                            .and_then(|o| {
                                if o.status.success() {
                                    let s = String::from_utf8_lossy(&o.stdout).trim().to_string();
                                    if s.is_empty() { None } else { Some(PathBuf::from(s)) }
                                } else {
                                    None
                                }
                            })
                            .unwrap_or_else(|| PathBuf::from(name));
                        return Some((abs, ver));
                    }
                }
            }
        }
    }
    None
}

/// Path to the venv's Python executable under `<data_dir>/venv`.
fn venv_python(data_dir: &Path) -> PathBuf {
    data_dir.join("venv").join("bin").join("python")
}

/// Create `<data_dir>/venv` and pip-install the sidecar requirements. Streams
/// `sidecar://status` events so the UI can show "Setting up Minion…" on first
/// launch. Idempotent: if the venv already has the sidecar imports working,
/// returns immediately.
fn bootstrap_venv(
    app: &AppHandle,
    data_dir: &Path,
    requirements: &Path,
) -> Result<PathBuf, String> {
    let py = venv_python(data_dir);
    // Already bootstrapped AND core deps importable? Fast-path return.
    if py.exists() && venv_has_core(&py) {
        dbg("bootstrap", serde_json::json!({"state": "cached", "python": py}));
        let _ = app.emit("sidecar://status", serde_json::json!({"state": "ready"}));
        return Ok(py);
    }

    let emit = |stage: &str, message: &str| {
        let _ = app.emit(
            "sidecar://status",
            serde_json::json!({"state": stage, "message": message}),
        );
    };

    // Find a usable system Python. If none, raise a clear, actionable error
    // that the UI can surface verbatim.
    let (system_py, ver) = find_system_python().ok_or_else(|| {
        let msg = "Python 3.10+ not found on PATH. Install from https://www.python.org/downloads/ and relaunch Minion.".to_string();
        emit("error", &msg);
        dbg("bootstrap", serde_json::json!({"state": "no_python"}));
        msg
    })?;
    dbg("bootstrap", serde_json::json!({"system_python": system_py, "version": ver}));

    if !py.exists() {
        emit("bootstrapping", "Creating Python environment…");
        let status = Command::new(&system_py)
            .arg("-m")
            .arg("venv")
            .arg(data_dir.join("venv"))
            .status()
            .map_err(|e| format!("venv launch failed: {e}"))?;
        if !status.success() {
            let msg = format!("venv creation failed (exit {})", status.code().unwrap_or(-1));
            emit("error", &msg);
            dbg("bootstrap", serde_json::json!({"state": "venv_failed"}));
            return Err(msg);
        }
        // Upgrade pip quietly; old pip on fresh Pythons sometimes can't resolve.
        let _ = Command::new(&py)
            .args(["-m", "pip", "install", "--upgrade", "--quiet", "pip"])
            .status();
    }

    emit(
        "installing",
        "Installing dependencies (first launch, ~2 min)…",
    );
    dbg("bootstrap", serde_json::json!({"state": "pip_start", "requirements": requirements}));
    let status = Command::new(&py)
        .args(["-m", "pip", "install", "--disable-pip-version-check", "-r"])
        .arg(requirements)
        .status()
        .map_err(|e| format!("pip launch failed: {e}"))?;
    if !status.success() {
        let msg = format!("pip install failed (exit {})", status.code().unwrap_or(-1));
        emit("error", &msg);
        dbg("bootstrap", serde_json::json!({"state": "pip_failed"}));
        return Err(msg);
    }
    dbg("bootstrap", serde_json::json!({"state": "pip_done"}));

    emit("ready", "Minion is ready.");
    Ok(py)
}

/// Quick sanity check: does this venv already have our core deps imported?
/// Avoids a ~2min pip re-run on every launch even though the venv exists.
fn venv_has_core(py: &Path) -> bool {
    Command::new(py)
        .args(["-c", "import fastapi, uvicorn, fastembed, watchdog, numpy"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// PIDs of every `api.py` sidecar process belonging to this user, regardless
/// of which app instance spawned it. Used by `restart_sidecar` so a Restart
/// click reliably clears orphans from prior dev runs, translocated copies,
/// or crashed parents — not just the child we happen to hold a handle to.
///
/// We match on a stable cmdline fragment (`api.py --port <port>`) rather
/// than port-listener lookup alone, because a sidecar still booting hasn't
/// bound the port yet but is already stealing it.
fn find_sidecar_pids(api_port: u16) -> Vec<u32> {
    // `ps -axo pid=,command=` is posix-portable and avoids the brittle
    // parsing of lsof. `-a` includes other users' terminals (harmless; we
    // kill only matching PIDs). `-x` includes detached processes.
    let out = match Command::new("ps")
        .args(["-axo", "pid=,command="])
        .output()
    {
        Ok(o) if o.status.success() => o.stdout,
        _ => return Vec::new(),
    };
    let text = String::from_utf8_lossy(&out);
    let needle_port = format!("--port {api_port}");
    let mut pids = Vec::new();
    for line in text.lines() {
        // Only Minion's sidecar: api.py invoked with our port. Prevents
        // collateral damage to any unrelated python process a user might
        // be running.
        if !(line.contains("api.py") && line.contains(&needle_port)) {
            continue;
        }
        let trimmed = line.trim_start();
        if let Some((pid_str, _)) = trimmed.split_once(char::is_whitespace) {
            if let Ok(pid) = pid_str.parse::<u32>() {
                pids.push(pid);
            }
        }
    }
    pids
}

/// PIDs of any process currently LISTENING on `api_port`. This catches
/// cases where the listener isn't one of our api.py sidecars (e.g. a dev
/// run pointed at the same port, or a previous app version). Belt-and-
/// suspenders with `find_sidecar_pids`.
fn find_port_listeners(api_port: u16) -> Vec<u32> {
    let out = match Command::new("lsof")
        .args([
            "-nP",
            "-iTCP",
            &format!("-iTCP:{api_port}"),
            "-sTCP:LISTEN",
            "-t",
        ])
        .output()
    {
        Ok(o) if o.status.success() => o.stdout,
        _ => return Vec::new(),
    };
    String::from_utf8_lossy(&out)
        .lines()
        .filter_map(|l| l.trim().parse::<u32>().ok())
        .collect()
}

/// Terminate a PID: SIGTERM first, then SIGKILL after a 500ms grace window
/// if it's still alive. Safe on PIDs we don't own (kill returns non-zero,
/// which we ignore). Best-effort.
fn kill_pid_graceful(pid: u32) {
    let _ = Command::new("kill").arg(pid.to_string()).status();
    std::thread::sleep(std::time::Duration::from_millis(500));
    // `kill -0` succeeds iff the process still exists. If so, force.
    let still_alive = Command::new("kill")
        .args(["-0", &pid.to_string()])
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if still_alive {
        let _ = Command::new("kill").args(["-9", &pid.to_string()]).status();
    }
}

/// Wait up to `timeout_ms` for nothing to be listening on `api_port`.
/// Returns true once the port is free. Prevents the respawn from racing a
/// slow-to-exit old listener.
fn wait_for_port_free(api_port: u16, timeout_ms: u64) -> bool {
    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
    while std::time::Instant::now() < deadline {
        if find_port_listeners(api_port).is_empty() {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    find_port_listeners(api_port).is_empty()
}

fn spawn_sidecar(
    python: &Path,
    src_dir: &Path,
    data_dir: &Path,
    inbox: &Path,
    api_port: u16,
    vision_model: Option<&str>,
) -> Option<Child> {
    let api = src_dir.join("api.py");
    if !api.exists() {
        dbg("spawn_sidecar", serde_json::json!({"state": "missing_api", "src_dir": src_dir}));
        return None;
    }

    let mut cmd = Command::new(python);
    // cwd = src_dir so `from ingest import ...` sibling imports resolve.
    let logs_dir = data_dir.join("logs");
    let _ = fs::create_dir_all(&logs_dir);
    let sidecar_log = logs_dir.join("sidecar.log");
    cmd.current_dir(src_dir)
        .arg(api.file_name().unwrap_or_else(|| std::ffi::OsStr::new("api.py")))
        .arg("--port")
        .arg(api_port.to_string())
        .env("MINION_DATA_DIR", data_dir)
        .env("MINION_INBOX", inbox)
        .env("MINION_API_PORT", api_port.to_string())
        .env("MINION_LOG_FILE", sidecar_log.to_string_lossy().to_string())
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    if let Some(model) = vision_model {
        cmd.env("MINION_VISION_MODEL", model);
    }

    match cmd.spawn() {
        Ok(child) => {
            dbg("spawn_sidecar", serde_json::json!({"state": "ok", "pid": child.id(), "python": python, "src_dir": src_dir}));
            Some(child)
        }
        Err(e) => {
            eprintln!("[minion] failed to spawn sidecar: {e}");
            dbg("spawn_sidecar", serde_json::json!({"state": "spawn_err", "error": e.to_string()}));
            None
        }
    }
}

// ---------------------------------------------------------------------------
// Ollama sidecar (optional; enables image captioning for pure photos)
// ---------------------------------------------------------------------------

/// Prefer a binary bundled inside the .app, fall back to PATH. Returns the
/// first candidate that exists on disk.
fn find_ollama_binary() -> Option<PathBuf> {
    // 1) Bundled under Resources (from tauri.bundle.resources).
    if let Ok(exe) = std::env::current_exe() {
        if let Some(contents) = exe.parent().and_then(Path::parent) {
            let candidate = contents.join("Resources").join("ollama");
            if candidate.exists() {
                return Some(candidate);
            }
        }
    }
    // 2) System install (Homebrew / Ollama.app installer).
    for p in &["/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"] {
        let pb = PathBuf::from(p);
        if pb.exists() {
            return Some(pb);
        }
    }
    // 3) Fall through to `ollama` on PATH.
    let out = Command::new("which").arg("ollama").output().ok()?;
    if !out.status.success() {
        return None;
    }
    let path = String::from_utf8(out.stdout).ok()?.trim().to_string();
    if path.is_empty() {
        None
    } else {
        Some(PathBuf::from(path))
    }
}

fn spawn_ollama(bin: &Path) -> Option<Child> {
    // If something is already listening on 11434 (e.g. Ollama.app), reuse it.
    if tcp_port_open("127.0.0.1", OLLAMA_PORT, Duration::from_millis(200)) {
        return None;
    }
    let mut cmd = Command::new(bin);
    cmd.arg("serve")
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("[minion] failed to spawn ollama: {e}");
            None
        }
    }
}

fn tcp_port_open(host: &str, port: u16, timeout: Duration) -> bool {
    use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
    let addrs: Vec<SocketAddr> = match format!("{host}:{port}").to_socket_addrs() {
        Ok(a) => a.collect(),
        Err(_) => return false,
    };
    for addr in addrs {
        if TcpStream::connect_timeout(&addr, timeout).is_ok() {
            return true;
        }
    }
    false
}

/// Block up to `timeout` waiting for ollama to start accepting connections.
fn wait_for_ollama(timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if tcp_port_open("127.0.0.1", OLLAMA_PORT, Duration::from_millis(200)) {
            return true;
        }
        thread::sleep(Duration::from_millis(250));
    }
    false
}

fn emit_vision(app: &tauri::AppHandle, stage: &str, line: &str) -> Result<(), String> {
    app.emit(
        "vision://progress",
        serde_json::json!({"stage": stage, "line": line}),
    )
    .map_err(|e| e.to_string())
}

fn ollama_has_model(bin: &Path, model: &str) -> bool {
    let out = match Command::new(bin).arg("list").output() {
        Ok(o) => o,
        Err(_) => return false,
    };
    if !out.status.success() {
        return false;
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    // Ollama prints `NAME   ID   SIZE   MODIFIED` — match by prefix, tag-agnostic.
    let target = model.split(':').next().unwrap_or(model);
    stdout.lines().skip(1).any(|line| {
        let name = line.split_whitespace().next().unwrap_or("");
        let stem = name.split(':').next().unwrap_or("");
        stem == target
    })
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
fn app_config(state: tauri::State<AppState>) -> serde_json::Value {
    let sidecar_bootstrapped = state
        .sidecar_python
        .lock()
        .ok()
        .and_then(|g| g.clone())
        .is_some();
    let sidecar_running = state
        .sidecar
        .lock()
        .ok()
        .and_then(|g| g.as_ref().map(|c| c.id()))
        .is_some();
    serde_json::json!({
        "data_dir": state.data_dir.to_string_lossy(),
        "inbox": state.inbox.to_string_lossy(),
        "api_port": state.api_port,
        "api_base": format!("http://127.0.0.1:{}", state.api_port),
        "sidecar_bootstrapped": sidecar_bootstrapped,
        "sidecar_running": sidecar_running,
    })
}

/// Strip a trailing ` (N)` suffix from a file stem, e.g. `foo (3)` -> `foo`.
/// Used to match inbox copies that we uniquified back to their original name.
fn strip_dup_suffix(stem: &str) -> &str {
    let bytes = stem.as_bytes();
    if !stem.ends_with(')') {
        return stem;
    }
    if let Some(open) = stem.rfind(" (") {
        let inner = &stem[open + 2..stem.len() - 1];
        if !inner.is_empty() && inner.bytes().all(|b| b.is_ascii_digit()) {
            return &stem[..open];
        }
    }
    let _ = bytes;
    stem
}

/// Scan the inbox for an existing file that almost certainly matches `src`
/// (same byte size, same basename after stripping ` (N)` copy suffix).
/// Cheap: metadata only, no hashing of multi-GB payloads.
fn find_existing_duplicate(inbox: &Path, src: &Path) -> Option<PathBuf> {
    let src_meta = fs::metadata(src).ok()?;
    if !src_meta.is_file() {
        return None;
    }
    let src_size = src_meta.len();
    let src_stem = src.file_stem()?.to_string_lossy().into_owned();
    let src_ext = src
        .extension()
        .map(|e| e.to_string_lossy().into_owned())
        .unwrap_or_default();
    let src_key = strip_dup_suffix(&src_stem);
    for entry in fs::read_dir(inbox).ok()?.flatten() {
        let meta = match entry.metadata() {
            Ok(m) => m,
            Err(_) => continue,
        };
        if !meta.is_file() || meta.len() != src_size {
            continue;
        }
        let path = entry.path();
        let ext = path
            .extension()
            .map(|e| e.to_string_lossy().into_owned())
            .unwrap_or_default();
        if ext != src_ext {
            continue;
        }
        let stem = match path.file_stem() {
            Some(s) => s.to_string_lossy().into_owned(),
            None => continue,
        };
        if strip_dup_suffix(&stem) == src_key {
            return Some(path);
        }
    }
    None
}

/// Resolve a non-clashing destination for a single file landing at the top
/// of the inbox (dedupe by `stem (N).ext`).
fn unique_file_dest(inbox: &Path, src: &Path) -> PathBuf {
    let name = src
        .file_name()
        .map(|s| s.to_os_string())
        .unwrap_or_else(|| "unnamed".into());
    let mut dest = inbox.join(&name);
    if !dest.exists() {
        return dest;
    }
    let stem = src
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "file".into());
    let ext = src
        .extension()
        .map(|s| format!(".{}", s.to_string_lossy()))
        .unwrap_or_default();
    let mut n = 1;
    loop {
        let candidate = inbox.join(format!("{stem} ({n}){ext}"));
        if !candidate.exists() {
            dest = candidate;
            return dest;
        }
        n += 1;
    }
}

/// Resolve a non-clashing destination for a dropped *directory* tree (dedupe
/// by `dirname (N)`). We namespace every nested file under this root so two
/// folders of the same name can coexist in the inbox.
fn unique_dir_dest(inbox: &Path, src: &Path) -> PathBuf {
    let name = src
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "folder".into());
    let mut dest = inbox.join(&name);
    if !dest.exists() {
        return dest;
    }
    let mut n = 1;
    loop {
        let candidate = inbox.join(format!("{name} ({n})"));
        if !candidate.exists() {
            dest = candidate;
            return dest;
        }
        n += 1;
    }
}

/// Accumulates per-drop stats so the frontend can show verbose feedback
/// (bytes copied, files skipped, errors) instead of a single "queued" line.
#[derive(Default)]
struct CopyStats {
    copied: Vec<String>,
    bytes: u64,
    skipped_dirs: u64,
    skipped_dotfiles: u64,
    errors: Vec<String>,
}

/// Walk `src_dir` and copy every regular file into `dest_root`, preserving
/// relative structure. Known build/cache folders (see `SKIP_DIRS`) are pruned.
fn copy_tree(src_dir: &Path, dest_root: &Path, stats: &mut CopyStats) -> Result<(), String> {
    fs::create_dir_all(dest_root).map_err(|e| e.to_string())?;
    let mut stack: Vec<(PathBuf, PathBuf)> = vec![(src_dir.to_path_buf(), dest_root.to_path_buf())];
    while let Some((src, dest)) = stack.pop() {
        let entries = match fs::read_dir(&src) {
            Ok(e) => e,
            Err(err) => {
                stats.errors.push(format!("read_dir {}: {err}", src.display()));
                continue;
            }
        };
        for entry in entries.flatten() {
            let name = entry.file_name();
            let name_str = name.to_string_lossy().into_owned();
            // macOS system noise — never useful, always skip.
            if name_str == ".DS_Store" {
                stats.skipped_dotfiles += 1;
                continue;
            }
            let src_path = entry.path();
            let file_type = match entry.file_type() {
                Ok(t) => t,
                Err(_) => continue,
            };
            if file_type.is_dir() {
                // Skip explicit build/SCM dirs AND any hidden dir (tooling
                // config like .vscode, .idea, .obsidian/cache). Dotfiles
                // that are NOT dirs fall through so `.env`, `.eslintrc`,
                // `.prettierrc`, etc. get indexed — previously they were
                // silently dropped.
                if should_skip_dir(&name_str) || name_str.starts_with('.') {
                    stats.skipped_dirs += 1;
                    dbg("copy_tree_skip_dir", serde_json::json!({"name": name_str}));
                    continue;
                }
                let next_dest = dest.join(&name_str);
                if let Err(e) = fs::create_dir_all(&next_dest) {
                    stats.errors.push(format!("mkdir {}: {e}", next_dest.display()));
                    continue;
                }
                stack.push((src_path, next_dest));
            } else if file_type.is_file() {
                let dest_file = dest.join(&name_str);
                if name_str.starts_with('.') {
                    // Track dotfiles kept for visibility in the drop report.
                    dbg("copy_tree_keep_dotfile", serde_json::json!({"name": name_str}));
                }
                match fs::copy(&src_path, &dest_file) {
                    Ok(n) => {
                        stats.bytes += n;
                        stats.copied.push(dest_file.to_string_lossy().into_owned());
                    }
                    Err(e) => stats
                        .errors
                        .push(format!("copy {}: {e}", src_path.display())),
                }
            }
        }
    }
    Ok(())
}

#[tauri::command]
fn copy_into_inbox(
    state: tauri::State<AppState>,
    paths: Vec<String>,
) -> Result<serde_json::Value, String> {
    let inbox = &state.inbox;
    fs::create_dir_all(inbox).map_err(|e| e.to_string())?;

    let mut per_drop: Vec<serde_json::Value> = Vec::new();
    for src in paths {
        let src_path = PathBuf::from(&src);
        if !src_path.exists() {
            per_drop.push(serde_json::json!({
                "source": src,
                "kind": "missing",
                "copied": 0,
                "bytes": 0,
            }));
            continue;
        }
        let mut stats = CopyStats::default();
        let (kind, dest_root) = if src_path.is_dir() {
            let dest_root = unique_dir_dest(inbox, &src_path);
            copy_tree(&src_path, &dest_root, &mut stats)?;
            ("directory", dest_root.to_string_lossy().into_owned())
        } else if src_path.is_file() {
            if let Some(existing) = find_existing_duplicate(inbox, &src_path) {
                per_drop.push(serde_json::json!({
                    "source": src,
                    "kind": "duplicate",
                    "dest": existing.to_string_lossy(),
                    "bytes": fs::metadata(&existing).map(|m| m.len()).unwrap_or(0),
                    "copied": 0,
                }));
                continue;
            }
            let dest = unique_file_dest(inbox, &src_path);
            match fs::copy(&src_path, &dest) {
                Ok(n) => {
                    stats.bytes += n;
                    stats.copied.push(dest.to_string_lossy().into_owned());
                }
                Err(e) => stats.errors.push(format!("copy {}: {e}", src_path.display())),
            }
            ("file", dest.to_string_lossy().into_owned())
        } else {
            per_drop.push(serde_json::json!({
                "source": src,
                "kind": "unsupported",
                "copied": 0,
                "bytes": 0,
            }));
            continue;
        };

        per_drop.push(serde_json::json!({
            "source": src,
            "kind": kind,
            "dest": dest_root,
            "copied": stats.copied.len(),
            "bytes": stats.bytes,
            "skipped_dirs": stats.skipped_dirs,
            "skipped_dotfiles": stats.skipped_dotfiles,
            "errors": stats.errors,
            "paths": stats.copied,
        }));
    }

    Ok(serde_json::json!({
        "drops": per_drop,
        "inbox": inbox.to_string_lossy(),
    }))
}

/// Kill every Minion sidecar belonging to this user and respawn one clean
/// child. Returns the new PID plus a summary of what was swept.
///
/// This is deliberately more aggressive than "kill the child I spawned":
/// the common broken state is a stray sidecar from a prior dev run, a
/// translocated app copy, or a crashed parent that orphaned its child.
/// The UI restart is the user's single escape hatch, so it has to be
/// reliable. We sweep by two independent signals (cmdline match on
/// `api.py --port <port>` + lsof listeners on that port), kill with
/// SIGTERM→SIGKILL, then wait for the port to actually be free before
/// respawning.
#[tauri::command]
fn restart_sidecar(state: tauri::State<AppState>) -> Result<serde_json::Value, String> {
    let port = state.api_port;

    // Drop the handle we hold so the parent no longer waits on it.
    {
        let mut guard = state
            .sidecar
            .lock()
            .map_err(|e| format!("sidecar lock poisoned: {e}"))?;
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    // Two-signal sweep: anything that looks like our sidecar by cmdline,
    // plus anything currently listening on our port. Union both sets so
    // we catch both the "booting but not listening yet" case and the
    // "listening but not ours" case.
    let mut victims: Vec<u32> = find_sidecar_pids(port);
    for pid in find_port_listeners(port) {
        if !victims.contains(&pid) {
            victims.push(pid);
        }
    }
    let swept = victims.len();
    for pid in &victims {
        kill_pid_graceful(*pid);
    }

    // Wait up to 3s for the port to actually be free. Binding before the
    // OS releases it would make the new sidecar crash with "Address
    // already in use" and the UI would stay in "reconnecting".
    let port_free = wait_for_port_free(port, 3_000);
    if !port_free {
        return Err(format!(
            "port {port} still held after killing {swept} process(es); refusing to respawn"
        ));
    }

    let current_model = state.vision_model.lock().ok().and_then(|g| g.clone());
    let python = state
        .sidecar_python
        .lock()
        .ok()
        .and_then(|g| g.clone())
        .ok_or_else(|| "sidecar python not yet bootstrapped".to_string())?;
    let src_dir = state
        .sidecar_src_dir
        .lock()
        .ok()
        .and_then(|g| g.clone())
        .ok_or_else(|| "sidecar source not located".to_string())?;
    let new_child = spawn_sidecar(
        &python,
        &src_dir,
        &state.data_dir,
        &state.inbox,
        port,
        current_model.as_deref(),
    )
    .ok_or_else(|| "failed to respawn sidecar".to_string())?;
    let pid = new_child.id();
    {
        let mut guard = state
            .sidecar
            .lock()
            .map_err(|e| format!("sidecar lock poisoned: {e}"))?;
        *guard = Some(new_child);
    }
    Ok(serde_json::json!({
        "pid": pid,
        "api_port": port,
        "swept": swept,
    }))
}

/// Snapshot for the UI header chip. `state` is one of:
///   "unavailable" — no ollama binary on disk (install it to enable captions)
///   "off"         — ollama present but model not pulled
///   "pulling"     — model download in progress (progress events stream separately)
///   "ready"       — model is pulled AND wired into the Python sidecar env
#[tauri::command]
fn vision_status(state: tauri::State<AppState>) -> serde_json::Value {
    let bin = state.ollama_bin.clone();
    let active = state.vision_model.lock().ok().and_then(|g| g.clone());
    let model = active
        .clone()
        .unwrap_or_else(|| DEFAULT_VISION_MODEL.to_string());
    let ui_state = if bin.is_none() {
        "unavailable"
    } else if active.is_some() {
        "ready"
    } else if bin.as_ref().map(|b| ollama_has_model(b, &model)).unwrap_or(false) {
        "off"
    } else {
        "off"
    };
    serde_json::json!({
        "state": ui_state,
        "model": model,
        "installed": bin.is_some(),
        "server_up": tcp_port_open("127.0.0.1", OLLAMA_PORT, Duration::from_millis(150)),
    })
}

/// Pull `model` if missing, wait for ollama to be reachable, then restart the
/// Python sidecar with MINION_VISION_MODEL set. Streams progress on the
/// `vision://progress` Tauri event as `{ stage: String, line: String }`.
#[tauri::command]
fn ensure_vision_model(
    app: tauri::AppHandle,
    state: tauri::State<AppState>,
    model: Option<String>,
) -> Result<serde_json::Value, String> {
    let bin = state
        .ollama_bin
        .clone()
        .ok_or_else(|| "ollama not installed".to_string())?;
    let model = model.unwrap_or_else(|| DEFAULT_VISION_MODEL.to_string());

    // Start the server if not already up (e.g. user quit Ollama.app).
    if !tcp_port_open("127.0.0.1", OLLAMA_PORT, Duration::from_millis(200)) {
        if let Some(child) = spawn_ollama(&bin) {
            if let Ok(mut g) = state.ollama.lock() {
                *g = Some(child);
            }
        }
        if !wait_for_ollama(Duration::from_secs(15)) {
            return Err("timed out waiting for ollama server".into());
        }
    }

    // Fast path: model already pulled — just wire env.
    if !ollama_has_model(&bin, &model) {
        let _ = app.emit(
            "vision://progress",
            serde_json::json!({"stage": "pulling_start", "line": format!("pulling {model}")}),
        );
        // `ollama pull` streams progress lines on stdout; forward them.
        let mut child = Command::new(&bin)
            .arg("pull")
            .arg(&model)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("spawn ollama pull: {e}"))?;
        if let Some(stdout) = child.stdout.take() {
            let app2 = app.clone();
            thread::spawn(move || {
                for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                    let _ = app2.emit(
                        "vision://progress",
                        serde_json::json!({"stage": "pulling", "line": line}),
                    );
                }
            });
        }
        if let Some(stderr) = child.stderr.take() {
            let app2 = app.clone();
            thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    let _ = app2.emit(
                        "vision://progress",
                        serde_json::json!({"stage": "pulling", "line": line}),
                    );
                }
            });
        }
        let status = child.wait().map_err(|e| format!("wait pull: {e}"))?;
        if !status.success() {
            return Err(format!("ollama pull {model} failed (exit {})", status.code().unwrap_or(-1)));
        }
    }

    // Restart the Python sidecar with the env wired so the image parser picks it up.
    {
        let mut guard = state
            .sidecar
            .lock()
            .map_err(|e| format!("sidecar lock poisoned: {e}"))?;
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        thread::sleep(Duration::from_millis(200));
        let python = state
            .sidecar_python
            .lock()
            .ok()
            .and_then(|g| g.clone())
            .ok_or_else(|| "sidecar python not yet bootstrapped".to_string())?;
        let src_dir = state
            .sidecar_src_dir
            .lock()
            .ok()
            .and_then(|g| g.clone())
            .ok_or_else(|| "sidecar source not located".to_string())?;
        let new_child = spawn_sidecar(
            &python,
            &src_dir,
            &state.data_dir,
            &state.inbox,
            state.api_port,
            Some(&model),
        )
        .ok_or_else(|| "failed to respawn sidecar".to_string())?;
        *guard = Some(new_child);
    }
    if let Ok(mut vm) = state.vision_model.lock() {
        *vm = Some(model.clone());
    }
    let _ = app.emit(
        "vision://progress",
        serde_json::json!({"stage": "ready", "line": format!("ready · {model}")}),
    );
    Ok(serde_json::json!({
        "state": "ready",
        "model": model,
    }))
}

#[tauri::command]
fn reveal_in_finder(path: String) -> Result<(), String> {
    let p = PathBuf::from(&path);
    if !p.exists() {
        return Err(format!("path does not exist: {}", p.display()));
    }
    #[cfg(target_os = "macos")]
    {
        // `open -R` selects the file in a Finder window; opening the parent
        // alone only shows the folder with nothing highlighted.
        if p.is_dir() {
            Command::new("open").arg(&p).spawn().map_err(|e| e.to_string())?;
        } else {
            Command::new("open")
                .arg("-R")
                .arg(&p)
                .spawn()
                .map_err(|e| e.to_string())?;
        }
    }
    #[cfg(target_os = "windows")]
    {
        if p.is_dir() {
            Command::new("explorer").arg(&p).spawn().map_err(|e| e.to_string())?;
        } else {
            // `/select,"path"` highlights the file in Explorer (quotes for spaces).
            let arg = format!("/select,\"{}\"", p.display());
            Command::new("explorer").arg(arg).spawn().map_err(|e| e.to_string())?;
        }
    }
    #[cfg(target_os = "linux")]
    {
        let target = if p.is_file() {
            p.parent().map(Path::to_path_buf).unwrap_or(p)
        } else {
            p
        };
        Command::new("xdg-open")
            .arg(target)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let data_dir = resolve_data_dir();
    let inbox = resolve_inbox(&data_dir);
    let _ = fs::create_dir_all(&data_dir);
    let _ = fs::create_dir_all(&inbox);
    let api_port: u16 = std::env::var("MINION_API_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8765);

    // Start ollama so the Python sidecar can be spawned with the vision env
    // already populated when the model is present.
    let target_model = std::env::var("MINION_VISION_MODEL")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_VISION_MODEL.to_string());
    let ollama_bin = find_ollama_binary();
    let mut ollama_child: Option<Child> = None;
    let mut vision_model: Option<String> = None;
    let mut needs_pull = false;
    if let Some(bin) = ollama_bin.clone() {
        ollama_child = spawn_ollama(&bin);
        if wait_for_ollama(Duration::from_secs(5)) {
            if ollama_has_model(&bin, &target_model) {
                vision_model = Some(target_model.clone());
            } else {
                needs_pull = true;
            }
        }
    }

    // Sidecar spawn moves into `setup` below because it needs the AppHandle
    // to resolve the bundled resource dir. Store None for now; setup fills it.
    let state = AppState {
        sidecar: Mutex::new(None),
        ollama: Mutex::new(ollama_child),
        ollama_bin: ollama_bin.clone(),
        vision_model: Mutex::new(vision_model.clone()),
        data_dir: data_dir.clone(),
        inbox: inbox.clone(),
        api_port,
        sidecar_src_dir: Mutex::new(None),
        sidecar_python: Mutex::new(None),
    };

    let initial_vision_model = vision_model.clone();
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(state)
        .setup(move |app| {
            // Bootstrap + spawn on a background thread so the window paints
            // immediately. UI subscribes to `sidecar://status` for progress.
            let handle = app.handle().clone();
            let data_dir_bg = data_dir.clone();
            let inbox_bg = inbox.clone();
            let port_bg = api_port;
            let vm_bg = initial_vision_model.clone();
            thread::spawn(move || {
                let state = match handle.try_state::<AppState>() {
                    Some(s) => s,
                    None => {
                        dbg("setup", serde_json::json!({"state": "no_state"}));
                        return;
                    }
                };
                let _ = handle.emit(
                    "sidecar://status",
                    serde_json::json!({"state": "starting", "message": "Locating sidecar…"}),
                );
                let src_dir = match resolve_sidecar_src_dir(&handle) {
                    Some(d) => d,
                    None => {
                        let _ = handle.emit(
                            "sidecar://status",
                            serde_json::json!({
                                "state": "error",
                                "message": "Could not locate Minion sidecar. Reinstall the app.",
                            }),
                        );
                        return;
                    }
                };
                if let Ok(mut g) = state.sidecar_src_dir.lock() {
                    *g = Some(src_dir.clone());
                }

                let requirements = match resolve_sidecar_requirements(&handle, &src_dir) {
                    Some(r) => r,
                    None => {
                        let _ = handle.emit(
                            "sidecar://status",
                            serde_json::json!({
                                "state": "error",
                                "message": "Bundled requirements.txt missing. Reinstall the app.",
                            }),
                        );
                        return;
                    }
                };

                let python = match bootstrap_venv(&handle, &data_dir_bg, &requirements) {
                    Ok(p) => p,
                    Err(e) => {
                        dbg("setup", serde_json::json!({"state": "bootstrap_err", "error": e}));
                        return;
                    }
                };
                if let Ok(mut g) = state.sidecar_python.lock() {
                    *g = Some(python.clone());
                }

                let child =
                    spawn_sidecar(&python, &src_dir, &data_dir_bg, &inbox_bg, port_bg, vm_bg.as_deref());
                if let Some(c) = child {
                    if let Ok(mut g) = state.sidecar.lock() {
                        *g = Some(c);
                    }
                    let _ = handle.emit(
                        "sidecar://status",
                        serde_json::json!({"state": "ready"}),
                    );
                } else {
                    let _ = handle.emit(
                        "sidecar://status",
                        serde_json::json!({
                            "state": "error",
                            "message": "Sidecar failed to start. Check logs or relaunch.",
                        }),
                    );
                }
            });

            // First-launch auto-pull: if ollama is present but the default
            // vision model isn't, grab it in the background. Progress streams
            // into the UI terminal via the `vision://progress` event so it
            // shows up in the same log as everything else.
            if needs_pull {
                let handle = app.handle().clone();
                let model = target_model.clone();
                thread::spawn(move || {
                    let state = match handle.try_state::<AppState>() {
                        Some(s) => s,
                        None => return,
                    };
                    // Wait for the initial sidecar bootstrap to finish so the
                    // vision-model respawn has a python + src_dir to reuse.
                    for _ in 0..600 {
                        if state
                            .sidecar_python
                            .lock()
                            .ok()
                            .and_then(|g| g.clone())
                            .is_some()
                        {
                            break;
                        }
                        thread::sleep(Duration::from_millis(500));
                    }
                    let _ = emit_vision(&handle, "start", &format!("pulling {model}…"));
                    if let Err(e) = ensure_vision_model(handle.clone(), state, Some(model.clone())) {
                        let _ = emit_vision(&handle, "error", &format!("auto-enable failed: {e}"));
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_config,
            copy_into_inbox,
            reveal_in_finder,
            restart_sidecar,
            vision_status,
            ensure_vision_model,
        ])
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::Destroyed) {
                if let Some(state) = window.app_handle().try_state::<AppState>() {
                    if let Ok(mut guard) = state.sidecar.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                    // Only kill ollama if *we* spawned it (don't nuke a user's
                    // pre-existing Ollama.app server).
                    if let Ok(mut guard) = state.ollama.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running minion desktop");
}
