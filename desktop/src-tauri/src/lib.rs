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
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter, Manager, WindowEvent};

// ---------------------------------------------------------------------------
// Persistent file logs (release builds): `<MINION_DATA_DIR>/logs/`
//   - `minion-desktop.log` — shell lifecycle (this file)
//   - `sidecar.log`       — Python FastAPI / uvicorn stdout+stderr
// Dev (`cargo tauri dev`): sidecar stdio stays inherited so the terminal
// stays readable; desktop lines still append to the log files.
// Optional NDJSON: set $MINION_DEBUG_LOG to a path for structured `dbg` lines.
// ---------------------------------------------------------------------------
static LOG_DIR: OnceLock<PathBuf> = OnceLock::new();
static LOG_MUTEX: Mutex<()> = Mutex::new(());

fn logs_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("logs")
}

fn init_file_logging(data_dir: &Path) {
    let dir = logs_dir(data_dir);
    let _ = fs::create_dir_all(&dir);
    let _ = LOG_DIR.set(dir);
}

fn log_ts_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn shell_log(level: &str, msg: &str) {
    let Ok(_guard) = LOG_MUTEX.lock() else {
        return;
    };
    let Some(dir) = LOG_DIR.get() else {
        return;
    };
    let path = dir.join("minion-desktop.log");
    let line = format!(
        "{} {} {}\n",
        log_ts_unix(),
        level,
        msg.replace('\n', " ")
    );
    if let Ok(mut f) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        let _ = f.write_all(line.as_bytes());
    }
    eprintln!("[minion] {msg}");
}

fn append_sidecar_log_separator(data_dir: &Path) {
    let path = logs_dir(data_dir).join("sidecar.log");
    let Ok(_guard) = LOG_MUTEX.lock() else {
        return;
    };
    if let Ok(mut f) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        let _ = writeln!(
            f,
            "\n=== sidecar session {} ===",
            log_ts_unix()
        );
    }
}

// ---------------------------------------------------------------------------
// Debug NDJSON instrumentation.
// Writes one JSON line per significant event to the session logfile defined
// by $MINION_DEBUG_LOG (optional).
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

/// How long `restart_sidecar` / vision respawn will block waiting for the
/// background venv + pip bootstrap (same window as the UI auto-pull waiter).
const SIDECAR_BOOTSTRAP_WAIT: Duration = Duration::from_secs(300);

/// Block until setup thread has stored both paths, or timeout. Otherwise
/// restart and vision respawn hit "sidecar python not yet bootstrapped" if the
/// user acts during first-launch pip.
fn wait_for_sidecar_bootstrap_paths(
    state: &AppState,
    timeout: Duration,
) -> Result<(PathBuf, PathBuf), String> {
    let deadline = Instant::now() + timeout;
    loop {
        let python = state.sidecar_python.lock().ok().and_then(|g| g.clone());
        let src_dir = state.sidecar_src_dir.lock().ok().and_then(|g| g.clone());
        if let (Some(py), Some(src)) = (python, src_dir) {
            return Ok((py, src));
        }
        if Instant::now() >= deadline {
            return Err(
                "Sidecar setup is still running — wait for first-launch setup to finish, then try again."
                    .to_string(),
            );
        }
        thread::sleep(Duration::from_millis(200));
    }
}

// moondream: 1.7GB vs llava's 4.5GB, purpose-built for image captioning,
// noticeably more stable on memory-constrained Macs. Override with the
// MINION_VISION_MODEL env var if you want llava or another vision model.
const DEFAULT_VISION_MODEL: &str = "moondream";
/// Ollama tag for ingest delight one-liners + corpus taste-pin extraction (tiny, local).
/// User may override via `MINION_DELIGHT_MODEL` / `MINION_TASTE_MODEL`.
const DEFAULT_MINION_LLM: &str = "qwen2.5:0.5b";
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

/// Parsed `Python 3.x.y` → minor is usable if >= 10.
fn python310_or_newer(stdout_stderr: &str) -> Option<String> {
    let ver = stdout_stderr.trim();
    if let Some(rest) = ver.strip_prefix("Python 3.") {
        if let Some(minor_str) = rest.split('.').next() {
            if let Ok(minor) = minor_str.trim().parse::<u32>() {
                if minor >= 10 {
                    return Some(ver.to_string());
                }
            }
        }
    }
    None
}

fn try_python_executable(exe: &Path) -> Option<(PathBuf, String)> {
    let out = Command::new(exe).arg("--version").output().ok()?;
    if !out.status.success() {
        return None;
    }
    let combined =
        String::from_utf8_lossy(&out.stdout).to_string() + &String::from_utf8_lossy(&out.stderr);
    let ver = python310_or_newer(&combined)?;
    Some((exe.to_path_buf(), ver))
}

/// Locations that exist on disk but are often missing from the **PATH** of GUI
/// apps launched from Finder / Spotlight (especially Homebrew on Apple Silicon).
fn fixed_python_install_candidates() -> Vec<PathBuf> {
    let mut v = Vec::new();
    #[cfg(not(windows))]
    {
        for base in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"] {
            for name in ["python3.12", "python3.11", "python3.10", "python3"] {
                let p = PathBuf::from(base).join(name);
                if p.is_file() {
                    v.push(p);
                }
            }
        }
    }
    #[cfg(windows)]
    {
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            let base = PathBuf::from(local).join("Programs").join("Python");
            for dir in ["Python312", "Python311", "Python310"] {
                let p = base.join(dir).join("python.exe");
                if p.is_file() {
                    v.push(p);
                }
            }
        }
        if let Ok(home) = std::env::var("USERPROFILE") {
            let p = PathBuf::from(home)
                .join(".pyenv")
                .join("pyenv-win")
                .join("shims")
                .join("python.exe");
            if p.is_file() {
                v.push(p);
            }
        }
    }
    v
}

fn resolve_exe_on_path(name: &str) -> Option<PathBuf> {
    #[cfg(windows)]
    let out = Command::new("where").arg(name).output().ok()?;
    #[cfg(not(windows))]
    let out = Command::new("which").arg(name).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let line = String::from_utf8_lossy(&out.stdout)
        .lines()
        .next()?
        .trim()
        .to_string();
    if line.is_empty() {
        return None;
    }
    Some(PathBuf::from(line))
}

/// Find a usable system `python3 >= 3.10`. GUI-launched apps may not inherit a
/// shell PATH, so we probe well-known install paths before `which python3`.
fn find_system_python() -> Option<(PathBuf, String)> {
    for exe in fixed_python_install_candidates() {
        if let Some(pair) = try_python_executable(&exe) {
            dbg(
                "python_probe",
                serde_json::json!({"path": pair.0, "via": "fixed"}),
            );
            return Some(pair);
        }
    }

    #[cfg(windows)]
    let names: &[&str] = &["python3.12", "python3.11", "python3.10", "python3", "python"];
    #[cfg(not(windows))]
    let names: &[&str] = &["python3.12", "python3.11", "python3.10", "python3"];

    for name in names {
        let out = match Command::new(name).arg("--version").output() {
            Ok(o) => o,
            Err(_) => continue,
        };
        if !out.status.success() {
            continue;
        }
        let combined =
            String::from_utf8_lossy(&out.stdout).to_string() + &String::from_utf8_lossy(&out.stderr);
        if let Some(ver) = python310_or_newer(&combined) {
            let abs = resolve_exe_on_path(name).unwrap_or_else(|| PathBuf::from(name));
            dbg(
                "python_probe",
                serde_json::json!({"path": abs, "via": "path", "name": name}),
            );
            return Some((abs, ver));
        }
    }

    #[cfg(windows)]
    {
        // Windows py launcher: resolve the real interpreter path for `venv`/`pip`.
        for flag in ["-3.12", "-3.11", "-3.10", "-3"] {
            let out = Command::new("py").arg(flag).arg("--version").output().ok()?;
            if !out.status.success() {
                continue;
            }
            let combined = String::from_utf8_lossy(&out.stdout).to_string()
                + &String::from_utf8_lossy(&out.stderr);
            let ver = python310_or_newer(&combined)?;
            let exe_out = Command::new("py")
                .arg(flag)
                .args(["-c", "import sys; print(sys.executable)"])
                .output()
                .ok()?;
            if !exe_out.status.success() {
                continue;
            }
            let raw = String::from_utf8_lossy(&exe_out.stdout).trim().to_string();
            let pb = PathBuf::from(&raw);
            if pb.as_os_str().is_empty() || !pb.exists() {
                continue;
            }
            dbg(
                "python_probe",
                serde_json::json!({"path": pb, "via": "py_launcher", "flag": flag}),
            );
            return Some((pb, ver));
        }
    }

    None
}

/// Path to the venv's Python executable under `<data_dir>/venv`.
fn venv_python(data_dir: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        data_dir.join("venv").join("Scripts").join("python.exe")
    }
    #[cfg(not(windows))]
    {
        data_dir.join("venv").join("bin").join("python")
    }
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
        // Don't emit `ready` here — first paint must wait until spawn_sidecar succeeds.
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
            .map_err(|e| {
                let msg = format!("venv launch failed: {e}");
                emit("error", &msg);
                msg
            })?;
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

    if !py.exists() {
        let msg = format!(
            "venv Python missing at {} after setup — try deleting the venv folder and relaunch.",
            py.display()
        );
        emit("error", &msg);
        return Err(msg);
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
        .map_err(|e| {
            let msg = format!("pip launch failed: {e}");
            emit("error", &msg);
            msg
        })?;
    if !status.success() {
        let msg = format!("pip install failed (exit {})", status.code().unwrap_or(-1));
        emit("error", &msg);
        dbg("bootstrap", serde_json::json!({"state": "pip_failed"}));
        return Err(msg);
    }
    dbg("bootstrap", serde_json::json!({"state": "pip_done"}));
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
    cmd.current_dir(src_dir)
        .arg(api.file_name().unwrap_or_else(|| std::ffi::OsStr::new("api.py")))
        .arg("--port")
        .arg(api_port.to_string())
        .env("MINION_DATA_DIR", data_dir)
        .env("MINION_INBOX", inbox)
        .env("MINION_API_PORT", api_port.to_string())
        .env("PYTHONUNBUFFERED", "1");
    // Release: capture sidecar output (Finder-launched apps have no TTY).
    // Dev: inherit so `tauri dev` stays readable.
    if !cfg!(debug_assertions) {
        let log_dir = logs_dir(data_dir);
        if fs::create_dir_all(&log_dir).is_ok() {
            append_sidecar_log_separator(data_dir);
            let sidecar_log_path = log_dir.join("sidecar.log");
            match fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&sidecar_log_path)
            {
                Ok(out) => match out.try_clone() {
                    Ok(err) => {
                        cmd.stdout(Stdio::from(out)).stderr(Stdio::from(err));
                    }
                    Err(e) => {
                        shell_log(
                            "WARN",
                            &format!(
                                "could not duplicate sidecar log fd ({}): {e}",
                                sidecar_log_path.display()
                            ),
                        );
                        cmd.stdout(Stdio::inherit()).stderr(Stdio::inherit());
                    }
                },
                Err(e) => {
                    shell_log(
                        "WARN",
                        &format!(
                            "could not open sidecar log {}: {e}",
                            sidecar_log_path.display()
                        ),
                    );
                    cmd.stdout(Stdio::inherit()).stderr(Stdio::inherit());
                }
            }
        }
    } else {
        cmd.stdout(Stdio::inherit()).stderr(Stdio::inherit());
    }
    // Turn on delight + taste LLM by default when the sidecar inherits no explicit choice.
    if std::env::var("MINION_DELIGHT_MODEL").is_err() {
        cmd.env("MINION_DELIGHT_MODEL", DEFAULT_MINION_LLM);
    }
    if std::env::var("MINION_TASTE_MODEL").is_err() {
        cmd.env("MINION_TASTE_MODEL", DEFAULT_MINION_LLM);
    }
    if let Some(model) = vision_model {
        cmd.env("MINION_VISION_MODEL", model);
    }

    match cmd.spawn() {
        Ok(child) => {
            dbg("spawn_sidecar", serde_json::json!({"state": "ok", "pid": child.id(), "python": python, "src_dir": src_dir}));
            Some(child)
        }
        Err(e) => {
            shell_log("ERROR", &format!("failed to spawn sidecar: {e}"));
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

fn minion_llm_env_disabled(raw: &str) -> bool {
    matches!(
        raw.trim().to_ascii_lowercase().as_str(),
        "" | "0" | "off" | "false" | "none"
    )
}

/// Which Ollama tags delight + taste need (mirrors Python `ingest_delight` / `corpus_pins_llm`).
fn resolve_minion_llm_pull_tags() -> Vec<String> {
    let delight_resolved: Option<String> = match std::env::var("MINION_DELIGHT_MODEL") {
        Ok(s) => {
            if minion_llm_env_disabled(&s) {
                None
            } else {
                Some(s.trim().to_string())
            }
        }
        Err(_) => Some(DEFAULT_MINION_LLM.to_string()),
    };
    let taste_resolved: Option<String> = match std::env::var("MINION_TASTE_MODEL") {
        Ok(s) => {
            if minion_llm_env_disabled(&s) {
                None
            } else {
                Some(s.trim().to_string())
            }
        }
        Err(_) => delight_resolved.clone(),
    };
    let mut out: Vec<String> = Vec::new();
    for m in [delight_resolved, taste_resolved].into_iter().flatten() {
        if !out.contains(&m) {
            out.push(m);
        }
    }
    out
}

/// Pull one model if missing; stream lines to `vision://progress` (same channel as vision pulls).
fn ollama_pull_stream(app: &AppHandle, bin: &Path, model: &str, line_prefix: &str) -> Result<(), String> {
    if ollama_has_model(bin, model) {
        return Ok(());
    }
    let _ = emit_vision(
        app,
        "pulling_start",
        &format!("{line_prefix}pulling {model}"),
    );
    let mut child = Command::new(bin)
        .arg("pull")
        .arg(model)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("spawn ollama pull: {e}"))?;
    if let Some(stdout) = child.stdout.take() {
        let app2 = app.clone();
        let pre = line_prefix.to_string();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                let _ = emit_vision(
                    &app2,
                    "pulling",
                    &format!("{pre}{line}"),
                );
            }
        });
    }
    if let Some(stderr) = child.stderr.take() {
        let app2 = app.clone();
        let pre = line_prefix.to_string();
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                let _ = emit_vision(
                    &app2,
                    "pulling",
                    &format!("{pre}{line}"),
                );
            }
        });
    }
    let status = child.wait().map_err(|e| format!("wait pull: {e}"))?;
    if !status.success() {
        return Err(format!(
            "ollama pull {model} failed (exit {})",
            status.code().unwrap_or(-1)
        ));
    }
    Ok(())
}

fn ensure_ollama_server_running(state: &AppState, bin: &Path) -> Result<(), String> {
    if tcp_port_open("127.0.0.1", OLLAMA_PORT, Duration::from_millis(200)) {
        return Ok(());
    }
    if let Some(child) = spawn_ollama(bin) {
        if let Ok(mut g) = state.ollama.lock() {
            *g = Some(child);
        }
    }
    if wait_for_ollama(Duration::from_secs(15)) {
        Ok(())
    } else {
        Err("timed out waiting for ollama server".into())
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
fn app_config(state: tauri::State<AppState>) -> serde_json::Value {
    let logs = logs_dir(&state.data_dir);
    serde_json::json!({
        "data_dir": state.data_dir.to_string_lossy(),
        "inbox": state.inbox.to_string_lossy(),
        "api_port": state.api_port,
        "api_base": format!("http://127.0.0.1:{}", state.api_port),
        "logs_dir": logs.to_string_lossy(),
        "desktop_log": logs.join("minion-desktop.log").to_string_lossy(),
        "sidecar_log": logs.join("sidecar.log").to_string_lossy(),
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
    let (python, src_dir) = wait_for_sidecar_bootstrap_paths(&state, SIDECAR_BOOTSTRAP_WAIT)?;
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

    ensure_ollama_server_running(&state, &bin)?;

    if !ollama_has_model(&bin, &model) {
        ollama_pull_stream(&app, &bin, &model, "")?;
    }

    let (python, src_dir) = wait_for_sidecar_bootstrap_paths(&state, SIDECAR_BOOTSTRAP_WAIT)?;

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
    init_file_logging(&data_dir);
    shell_log("INFO", "Minion desktop starting");
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
                // AppState may not be visible for a tick after `.manage`; retry so we never
                // exit silently (otherwise the UI stays on "starting" forever).
                let deadline = Instant::now() + Duration::from_secs(15);
                let state = loop {
                    if let Some(s) = handle.try_state::<AppState>() {
                        break s;
                    }
                    if Instant::now() >= deadline {
                        dbg("setup", serde_json::json!({"state": "no_state_timeout"}));
                        let _ = handle.emit(
                            "sidecar://status",
                            serde_json::json!({
                                "state": "error",
                                "message": "Startup timed out — quit Minion completely and reopen.",
                            }),
                        );
                        return;
                    }
                    thread::sleep(Duration::from_millis(50));
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
                        dbg("setup", serde_json::json!({"state": "bootstrap_err", "error": &e}));
                        let _ = handle.emit(
                            "sidecar://status",
                            serde_json::json!({ "state": "error", "message": e }),
                        );
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
                            "message": format!(
                                "Sidecar failed to start. See {}/ for sidecar.log and minion-desktop.log (Settings → File logs), or relaunch.",
                                logs_dir(&data_dir_bg).display()
                            ),
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

            // Auto-pull delight/taste LLM(s) when Ollama is installed but tags are missing.
            let llm_tags = resolve_minion_llm_pull_tags();
            if !llm_tags.is_empty() {
                if let Some(bin_llm) = ollama_bin.clone() {
                    let handle_llm = app.handle().clone();
                    thread::spawn(move || {
                        thread::sleep(Duration::from_millis(1800));
                        let state = match handle_llm.try_state::<AppState>() {
                            Some(s) => s,
                            None => return,
                        };
                        if ensure_ollama_server_running(&state, &bin_llm).is_err() {
                            let _ = emit_vision(
                                &handle_llm,
                                "error",
                                "llm · could not reach Ollama — delight/taste need a running server",
                            );
                            return;
                        }
                        for m in llm_tags {
                            if ollama_has_model(&bin_llm, &m) {
                                continue;
                            }
                            if let Err(e) =
                                ollama_pull_stream(&handle_llm, &bin_llm, &m, "llm · ")
                            {
                                let _ =
                                    emit_vision(&handle_llm, "error", &format!("llm · {e}"));
                                return;
                            }
                        }
                        let _ = emit_vision(
                            &handle_llm,
                            "ready",
                            "llm · delight/taste models ready",
                        );
                    });
                }
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
