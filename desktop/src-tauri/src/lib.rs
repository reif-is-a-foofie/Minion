// Minion desktop shell.
//
// Responsibilities:
// - Resolve (and create) the user's Minion data dir + inbox
// - Discover the Python sidecar source (bundled under `Resources/sidecar` in
//   the shipped .app, or a dev checkout walking up from current_exe)
// - First-launch bootstrap: find a system `python3 >= 3.10` (or download a
//   pinned arch-matched CPython), create a venv under `<data_dir>/venv`, pip
//   install the bundled sidecar requirements. Streams `sidecar://status`.
// - Ollama: if no `ollama` on the system, download the official universal
//   macOS zip into `<data_dir>/managed-ollama/` (override with
//   MINION_SKIP_MANAGED_OLLAMA=1).
// - Spawn the Python API sidecar as a managed child process, using the
//   bootstrapped venv and the bundled source tree (no compile-time paths).
// - Expose minimal Tauri commands the frontend uses:
//     app_config, copy_into_inbox, reveal_in_finder, restart_sidecar
// Native OS file drops are delivered to the frontend by Tauri v2 as the
// `tauri://drag-drop` event; the frontend forwards the paths to
// `copy_into_inbox`.

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
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
    /// Ollama CLI path (bundled, Homebrew, PATH, or Minion-managed download).
    ollama_bin: Mutex<Option<PathBuf>>,
    /// Model currently wired into the Python sidecar via MINION_VISION_MODEL.
    /// `None` means captioning is off.
    vision_model: Mutex<Option<String>>,
    data_dir: PathBuf,
    inbox: PathBuf,
    api_port: u16,
    /// Shared with the Python sidecar when set (HTTP mutation auth).
    api_token: String,
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

/// Official Ollama macOS zip (fat/universal). Update tag + SHA together when bumping.
/// https://github.com/ollama/ollama/releases
const MANAGED_OLLAMA_TAG: &str = "v0.21.1";
const MANAGED_OLLAMA_ZIP: &str = "Ollama-darwin.zip";
const MANAGED_OLLAMA_SHA256: &str =
    "56163f12d8e7a7386812575d2e1073bdd96966ec788df7921fe54dd7d3beb979";

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

/// `MINION_NEW_API_PORT` — truthy values: `1`, `true`, `yes`, `on` (case-insensitive).
fn minion_env_truthy(name: &str) -> bool {
    std::env::var(name)
        .ok()
        .map(|s| {
            matches!(
                s.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn ensure_api_token_file(data_dir: &Path) -> String {
    let p = data_dir.join(".minion_api_token");
    if let Ok(s) = fs::read_to_string(&p) {
        let t = s.trim().to_string();
        if !t.is_empty() {
            return t;
        }
    }
    let mut buf = [0u8; 32];
    let tok = if getrandom::getrandom(&mut buf).is_ok() {
        buf.iter().map(|b| format!("{b:02x}")).collect::<String>()
    } else {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        format!("minion-{nanos:x}")
    };
    let _ = fs::write(&p, &tok);
    tok
}

/// Token forwarded to the sidecar as `MINION_API_TOKEN` for mutation auth.
fn sidecar_api_token(data_dir: &Path) -> String {
    match std::env::var("MINION_API_TOKEN") {
        Ok(t) => t.trim().to_string(),
        Err(_) => ensure_api_token_file(data_dir),
    }
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

/// Prefer `requirements-docs.txt` when bundled (PDF/DOCX/HTML — it already
/// `-r requirements.txt`). Fall back to `requirements.txt` only if docs file
/// is missing (old bundles).
fn pick_sidecar_requirements_in(dir: &Path) -> Option<PathBuf> {
    let docs = dir.join("requirements-docs.txt");
    if docs.exists() {
        return Some(docs);
    }
    let core = dir.join("requirements.txt");
    if core.exists() {
        return Some(core);
    }
    None
}

/// Locate the requirements file bundled alongside the sidecar source.
fn resolve_sidecar_requirements(app: &AppHandle, src_dir: &Path) -> Option<PathBuf> {
    // Layout: <sidecar>/requirements-docs.txt sibling of <sidecar>/src/
    if let Some(parent) = src_dir.parent() {
        if let Some(p) = pick_sidecar_requirements_in(parent) {
            return Some(p);
        }
    }
    if let Ok(res_dir) = app.path().resource_dir() {
        if let Some(p) = pick_sidecar_requirements_in(&res_dir.join("sidecar")) {
            return Some(p);
        }
    }
    if let Some(grand) = src_dir.parent().and_then(Path::parent) {
        if let Some(p) = pick_sidecar_requirements_in(grand) {
            return Some(p);
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

fn managed_python_path(data_dir: &Path) -> PathBuf {
    data_dir.join("managed-python").join("python").join("bin").join("python3")
}

fn ensure_managed_python(app: &AppHandle, data_dir: &Path) -> Result<(PathBuf, String), String> {
    let py = managed_python_path(data_dir);
    if py.exists() {
        let out = Command::new(&py)
            .arg("--version")
            .output()
            .map_err(|e| format!("managed python failed: {e}"))?;
        let ver = String::from_utf8_lossy(&out.stdout).to_string()
            + &String::from_utf8_lossy(&out.stderr);
        return Ok((py, ver.trim().to_string()));
    }

    let emit = |stage: &str, message: &str| {
        let _ = app.emit(
            "sidecar://status",
            serde_json::json!({"state": stage, "message": message}),
        );
    };

    // Pinned python-build-standalone artifact (install_only) + checksum.
    // This is downloaded only when the host doesn't have Python 3.10+.
    let tag = "20260320";
    let (triple, sha_expected) = if cfg!(target_arch = "aarch64") {
        (
            "aarch64-apple-darwin",
            "235c98abd103755852a27e4126a46b64adac9ba2dda547e0f9c97d216df095a0",
        )
    } else {
        (
            "x86_64-apple-darwin",
            "d3d0bdfd5e53e5911807bf0942fcc6220912263f292fd203642c2b93b5ab1f8e",
        )
    };
    let file = format!("cpython-3.11.15+{tag}-{triple}-install_only.tar.gz");
    let base = format!("https://github.com/astral-sh/python-build-standalone/releases/download/{tag}");
    let url = format!("{base}/{file}");
    let sums_url = format!("{base}/SHA256SUMS");

    let mp = data_dir.join("managed-python");
    let _ = fs::create_dir_all(&mp);
    let tarball = mp.join(&file);

    emit("installing", "Downloading managed Python (first launch)…");
    dbg("managed_python", serde_json::json!({"state": "download", "url": url}));

    let status = Command::new("curl")
        .args(["-fL", "-o"])
        .arg(&tarball)
        .arg(&url)
        .status()
        .map_err(|e| format!("curl launch failed: {e}"))?;
    if !status.success() {
        return Err(format!("failed to download managed python (exit {})", status.code().unwrap_or(-1)));
    }

    // Verify checksum by pulling SHA256SUMS and matching the line.
    emit("installing", "Verifying managed Python…");
    let sums = Command::new("curl")
        .args(["-fsSL"])
        .arg(&sums_url)
        .output()
        .map_err(|e| format!("curl SHA256SUMS failed: {e}"))?;
    if !sums.status.success() {
        return Err("failed to fetch SHA256SUMS".to_string());
    }
    let sums_txt = String::from_utf8_lossy(&sums.stdout);
    let mut ok = false;
    for line in sums_txt.lines() {
        if line.ends_with(&file) && line.starts_with(sha_expected) {
            ok = true;
            break;
        }
    }
    if !ok {
        return Err("managed python checksum mismatch".to_string());
    }

    // Extract under managed-python/python/
    emit("installing", "Installing managed Python…");
    let target = mp.join("python");
    let _ = fs::remove_dir_all(&target);
    let status = Command::new("tar")
        .args(["-xzf"])
        .arg(&tarball)
        .arg("-C")
        .arg(&mp)
        .status()
        .map_err(|e| format!("tar launch failed: {e}"))?;
    if !status.success() {
        return Err(format!("tar extract failed (exit {})", status.code().unwrap_or(-1)));
    }

    // The tarball extracts into `python/` at the archive root.
    if !py.exists() {
        return Err("managed python install missing python3".to_string());
    }

    let out = Command::new(&py)
        .arg("--version")
        .output()
        .map_err(|e| format!("managed python failed: {e}"))?;
    let ver = String::from_utf8_lossy(&out.stdout).to_string()
        + &String::from_utf8_lossy(&out.stderr);
    Ok((py, ver.trim().to_string()))
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
        let _ = app.emit(
            "sidecar://status",
            serde_json::json!({
                "state": "bootstrapping",
                "message": "Python environment ready (cached). Launching indexer…",
            }),
        );
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
    let (system_py, ver) = match find_system_python() {
        Some(v) => v,
        None => {
            dbg("bootstrap", serde_json::json!({"state": "no_python"}));
            ensure_managed_python(app, data_dir).map_err(|e| {
                let msg = format!("Python 3.10+ not found; managed Python install failed: {e}");
                emit("error", &msg);
                msg
            })?
        }
    };
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
        "Installing dependencies — core stack + PDF, Word, HTML parsers (first launch, ~2–4 min)…",
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

    emit(
        "bootstrapping",
        "Dependencies installed. Launching indexer…",
    );
    Ok(py)
}

/// Quick sanity check: core HTTP/embed stack plus document parsers shipped with
/// the desktop app (PDF/DOCX/HTML). If this fails, `bootstrap_venv` re-runs
/// pip so upgrades pick up `requirements-docs.txt`.
fn venv_has_core(py: &Path) -> bool {
    Command::new(py)
        .args([
            "-c",
            "import fastapi, uvicorn, fastembed, watchdog, numpy; import pypdf; import trafilatura; import docx",
        ])
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
    let port_arg = format!("-iTCP:{api_port}");
    let args = ["-nP", "-iTCP", &port_arg, "-sTCP:LISTEN", "-t"];
    #[cfg(target_os = "macos")]
    let candidates: &[&str] = &["/usr/sbin/lsof", "lsof"];
    #[cfg(not(target_os = "macos"))]
    let candidates: &[&str] = &["/usr/sbin/lsof", "/usr/bin/lsof", "lsof"];

    for bin in candidates {
        let out = match Command::new(bin).args(args).output() {
            Ok(o) if o.status.success() => o.stdout,
            _ => continue,
        };
        return String::from_utf8_lossy(&out)
            .lines()
            .filter_map(|l| l.trim().parse::<u32>().ok())
            .collect();
    }
    Vec::new()
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
        if find_port_listeners(api_port).is_empty()
            && !tcp_port_open("127.0.0.1", api_port, Duration::from_millis(100))
        {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    find_port_listeners(api_port).is_empty()
        && !tcp_port_open("127.0.0.1", api_port, Duration::from_millis(100))
}

#[cfg(unix)]
fn pid_is_current_user(pid: u32) -> bool {
    let out = match Command::new("ps")
        .args(["-o", "uid=", "-p", &pid.to_string()])
        .output()
    {
        Ok(o) if o.status.success() => o.stdout,
        _ => return false,
    };
    let their: u32 = match String::from_utf8_lossy(&out).trim().parse() {
        Ok(u) => u,
        Err(_) => return false,
    };
    let me_out = match Command::new("id").arg("-u").output() {
        Ok(o) if o.status.success() => o.stdout,
        _ => return false,
    };
    let me: u32 = match String::from_utf8_lossy(&me_out).trim().parse() {
        Ok(u) => u,
        Err(_) => return false,
    };
    their == me
}

#[cfg(not(unix))]
fn pid_is_current_user(_pid: u32) -> bool {
    true
}

fn http_get_body(port: u16, path: &str, timeout_ms: u64) -> Option<String> {
    let addr: SocketAddr = format!("127.0.0.1:{}", port).parse().ok()?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(timeout_ms)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_millis(timeout_ms)));
    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > 2_000_000 {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    let text = String::from_utf8_lossy(&buf).into_owned();
    if let Some(idx) = text.find("\r\n\r\n") {
        return Some(text[idx + 4..].to_string());
    }
    text.find("\n\n").map(|idx| text[idx + 2..].to_string())
}

/// True when `GET /status` returns a JSON body that looks like Minion's status.
fn sidecar_status_responds(port: u16, per_try_timeout_ms: u64) -> bool {
    let Some(body) = http_get_body(port, "/status", per_try_timeout_ms) else {
        return false;
    };
    let t = body.trim_start();
    t.starts_with('{') && t.contains("\"counts\"") && t.contains("\"db_path\"")
}

/// Poll until the sidecar answers `/status` or `timeout_ms` elapses (cold start
/// can take tens of seconds while Python imports load).
fn wait_for_sidecar_http_ready(port: u16, timeout_ms: u64) -> bool {
    let deadline = Instant::now() + Duration::from_millis(timeout_ms);
    while Instant::now() < deadline {
        if sidecar_status_responds(port, 900) {
            return true;
        }
        thread::sleep(Duration::from_millis(100));
    }
    false
}

fn listener_is_minion_with_nuke(port: u16) -> bool {
    let body = match http_get_body(port, "/openapi.json", 800) {
        Some(b) if b.trim_start().starts_with('{') => b,
        _ => return false,
    };
    body.contains("\"/nuke\"")
}

/// Ask the OS for a free loopback port (bind `127.0.0.1:0`, read port, release).
/// Used when the preferred range is crowded so we still get a listener slot.
fn propose_ephemeral_loopback_port() -> Option<u16> {
    let listener = TcpListener::bind("127.0.0.1:0").ok()?;
    listener.local_addr().ok().map(|a| a.port())
}

/// Fresh port for a new desktop process when `MINION_API_PORT` is unset.
/// Ephemeral bind + TCP probe (and lsof when available) avoids joining another
/// user's listener on the old default `8765`.
fn allocate_sidecar_port_ephemeral() -> Option<u16> {
    const ATTEMPTS: u32 = 48;
    for attempt in 0..ATTEMPTS {
        let port = propose_ephemeral_loopback_port()?;
        if find_port_listeners(port).is_empty()
            && !tcp_port_open("127.0.0.1", port, Duration::from_millis(120))
        {
            dbg(
                "allocate_sidecar_port_ephemeral",
                serde_json::json!({"picked": port, "attempt": attempt}),
            );
            return Some(port);
        }
        thread::sleep(Duration::from_millis(8));
    }
    None
}

/// Port the sidecar should bind on for this app instance.
///
/// - **`MINION_NEW_API_PORT`** (truthy) — prefer a fresh verified ephemeral
///   port even if `MINION_API_PORT` is set; on failure fall through to the rules
///   below (escape hatch when the fixed port is wedged).
/// - **`MINION_API_PORT` set** — use `resolve_sidecar_port` (reclaim our stale
///   processes, skip foreign listeners, scan / ephemeral fallback).
/// - **both unset** — verified-free ephemeral port per launch (shared Mac safe).
fn resolve_initial_sidecar_port() -> u16 {
    if minion_env_truthy("MINION_NEW_API_PORT") {
        if let Some(port) = allocate_sidecar_port_ephemeral() {
            return port;
        }
        dbg(
            "resolve_initial_sidecar_port",
            serde_json::json!({"reason": "MINION_NEW_API_PORT_ephemeral_failed_fallback"}),
        );
    }
    if let Ok(raw) = std::env::var("MINION_API_PORT") {
        if let Ok(preferred) = raw.trim().parse::<u16>() {
            return resolve_sidecar_port(preferred);
        }
    }
    if let Some(port) = allocate_sidecar_port_ephemeral() {
        return port;
    }
    dbg(
        "resolve_initial_sidecar_port",
        serde_json::json!({"reason": "ephemeral_exhausted_fallback_scan"}),
    );
    resolve_sidecar_port(8765)
}

/// Pick a localhost port for the sidecar: prefer `preferred`, but if something
/// else is listening (another user, an old Minion without `/nuke`, or any
/// foreign service), advance until we find a free port or reclaim one from
/// our own stale processes.
fn resolve_sidecar_port(preferred: u16) -> u16 {
    const MAX_TRIES: u32 = 64;
    let pref = u32::from(preferred);
    for i in 0..MAX_TRIES {
        let port_u32 = pref + i;
        if port_u32 > u32::from(u16::MAX) {
            break;
        }
        let port = port_u32 as u16;
        let listeners = find_port_listeners(port);
        let accepts_tcp = tcp_port_open("127.0.0.1", port, Duration::from_millis(150));
        // GUI .app bundles often lack PATH: lsof may fail and return no PIDs even
        // while uvicorn is listening. A successful TCP connect proves occupancy.
        if listeners.is_empty() && !accepts_tcp {
            dbg(
                "resolve_sidecar_port",
                serde_json::json!({"picked": port, "reason": "free"}),
            );
            return port;
        }
        if listeners.is_empty() && accepts_tcp {
            dbg(
                "resolve_sidecar_port",
                serde_json::json!({"skip": port, "reason": "tcp_probe_busy_lsof_miss"}),
            );
            continue;
        }
        let all_mine = listeners.iter().all(|p| pid_is_current_user(*p));
        if !all_mine {
            dbg(
                "resolve_sidecar_port",
                serde_json::json!({"skip": port, "reason": "foreign_uid"}),
            );
            continue;
        }
        let sidecars = find_sidecar_pids(port);
        let is_minion = listener_is_minion_with_nuke(port);
        let our_sidecar_listening = listeners.iter().any(|p| sidecars.contains(p));
        if is_minion || our_sidecar_listening {
            for pid in find_sidecar_pids(port) {
                kill_pid_graceful(pid);
            }
            for pid in &listeners {
                kill_pid_graceful(*pid);
            }
            let _ = wait_for_port_free(port, 3_000);
            if find_port_listeners(port).is_empty()
                && !tcp_port_open("127.0.0.1", port, Duration::from_millis(120))
            {
                dbg(
                    "resolve_sidecar_port",
                    serde_json::json!({"picked": port, "reason": "reclaimed"}),
                );
                return port;
            }
            continue;
        }
        dbg(
            "resolve_sidecar_port",
            serde_json::json!({"skip": port, "reason": "other_service"}),
        );
    }
    // Dense multi-user machines: linear scan from `preferred` may hit only
    // foreign listeners. Grab an ephemeral free port instead of returning
    // `preferred` (often still held by another account's sidecar).
    for attempt in 0..24 {
        if let Some(port) = propose_ephemeral_loopback_port() {
            if find_port_listeners(port).is_empty() && !tcp_port_open("127.0.0.1", port, Duration::from_millis(80))
            {
                dbg(
                    "resolve_sidecar_port",
                    serde_json::json!({"picked": port, "reason": "ephemeral", "attempt": attempt}),
                );
                return port;
            }
        }
        std::thread::sleep(Duration::from_millis(15));
    }
    dbg(
        "resolve_sidecar_port",
        serde_json::json!({"picked": preferred, "reason": "fallback_busy"}),
    );
    preferred
}

fn spawn_sidecar(
    python: &Path,
    src_dir: &Path,
    data_dir: &Path,
    inbox: &Path,
    api_port: u16,
    vision_model: Option<&str>,
    api_token: &str,
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
    if !api_token.is_empty() {
        cmd.env("MINION_API_TOKEN", api_token);
    }
    // Optional: forward distributor analytics endpoint (see chatgpt_mcp_memory analytics_remote).
    if let Ok(u) = std::env::var("MINION_ANALYTICS_URL") {
        let t = u.trim();
        if !t.is_empty() {
            cmd.env("MINION_ANALYTICS_URL", t);
        }
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

fn managed_ollama_cli_path(data_dir: &Path) -> PathBuf {
    data_dir
        .join("managed-ollama")
        .join("Ollama.app")
        .join("Contents")
        .join("Resources")
        .join("ollama")
}

/// Prefer a binary bundled inside the .app, then system installs, then a
/// Minion-managed copy under `<data_dir>/managed-ollama/`.
fn find_ollama_binary(data_dir: &Path) -> Option<PathBuf> {
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
    for p in &["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"] {
        let pb = PathBuf::from(p);
        if pb.exists() {
            return Some(pb);
        }
    }
    // 3) `ollama` on PATH.
    if let Ok(out) = Command::new("which").arg("ollama").output() {
        if out.status.success() {
            if let Ok(path) = String::from_utf8(out.stdout) {
                let path = path.trim().to_string();
                if !path.is_empty() {
                    return Some(PathBuf::from(path));
                }
            }
        }
    }
    // 4) Prior Minion-managed install (official universal darwin zip).
    let managed = managed_ollama_cli_path(data_dir);
    if managed.is_file() {
        return Some(managed);
    }
    None
}

/// Download the official Ollama macOS app bundle into `<data_dir>/managed-ollama/`.
/// The published zip is a single macOS build (works on Apple Silicon and Intel).
/// Set `MINION_SKIP_MANAGED_OLLAMA=1` to disable (locked-down environments).
fn ensure_managed_ollama(app: &AppHandle, data_dir: &Path) -> Result<PathBuf, String> {
    if std::env::var("MINION_SKIP_MANAGED_OLLAMA").ok().as_deref() == Some("1") {
        return Err("managed Ollama disabled (MINION_SKIP_MANAGED_OLLAMA=1)".into());
    }
    let bin = managed_ollama_cli_path(data_dir);
    if bin.is_file() {
        let ok = Command::new(&bin)
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        if ok {
            return Ok(bin);
        }
        let _ = fs::remove_dir_all(data_dir.join("managed-ollama"));
    }

    let emit = |stage: &str, message: &str| {
        let _ = app.emit(
            "sidecar://status",
            serde_json::json!({"state": stage, "message": message}),
        );
    };

    let root = data_dir.join("managed-ollama");
    let _ = fs::create_dir_all(&root);
    let url = format!(
        "https://github.com/ollama/ollama/releases/download/{MANAGED_OLLAMA_TAG}/{MANAGED_OLLAMA_ZIP}"
    );
    let zip_path = root.join("Ollama-darwin.download.zip");

    emit(
        "installing",
        "Downloading Ollama for macOS (first launch, ~120 MB)…",
    );
    dbg(
        "managed_ollama",
        serde_json::json!({"state": "download", "url": url}),
    );
    let status = Command::new("curl")
        .args(["-fL", "-o"])
        .arg(&zip_path)
        .arg(&url)
        .status()
        .map_err(|e| format!("curl launch failed: {e}"))?;
    if !status.success() {
        let _ = fs::remove_file(&zip_path);
        return Err(format!(
            "failed to download Ollama (exit {})",
            status.code().unwrap_or(-1)
        ));
    }

    emit("installing", "Verifying Ollama download…");
    let out = Command::new("shasum")
        .args(["-a", "256"])
        .arg(&zip_path)
        .output()
        .map_err(|e| format!("shasum failed: {e}"))?;
    if !out.status.success() {
        let _ = fs::remove_file(&zip_path);
        return Err("shasum exited with error".into());
    }
    let line = String::from_utf8_lossy(&out.stdout);
    let got = line
        .split_whitespace()
        .next()
        .ok_or_else(|| "shasum produced no hash".to_string())?;
    if got != MANAGED_OLLAMA_SHA256 {
        let _ = fs::remove_file(&zip_path);
        return Err(format!("Ollama checksum mismatch (got {got})"));
    }

    emit("installing", "Unpacking Ollama…");
    let _ = fs::remove_dir_all(root.join("Ollama.app"));
    let st = Command::new("unzip")
        .args(["-q", "-o", "-d"])
        .arg(&root)
        .arg(&zip_path)
        .status()
        .map_err(|e| format!("unzip launch failed: {e}"))?;
    if !st.success() {
        let _ = fs::remove_file(&zip_path);
        return Err(format!("unzip failed (exit {})", st.code().unwrap_or(-1)));
    }
    let _ = fs::remove_file(&zip_path);

    if !bin.is_file() {
        return Err("managed Ollama install missing ollama binary".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&bin, fs::Permissions::from_mode(0o755));
    }
    Ok(bin)
}

fn resolve_ollama_for_minion(app: &AppHandle, data_dir: &Path) -> Option<PathBuf> {
    if let Some(p) = find_ollama_binary(data_dir) {
        return Some(p);
    }
    ensure_managed_ollama(app, data_dir).ok()
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
        "api_token": state.api_token,
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
        if !pid_is_current_user(*pid) {
            continue;
        }
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
        &state.api_token,
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
///   "unavailable" — no ollama binary yet (managed download may still be running)
///   "off"         — ollama present but model not pulled
///   "pulling"     — model download in progress (progress events stream separately)
///   "ready"       — model is pulled AND wired into the Python sidecar env
#[tauri::command]
fn vision_status(state: tauri::State<AppState>) -> serde_json::Value {
    let bin = state
        .ollama_bin
        .lock()
        .ok()
        .and_then(|g| g.clone());
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
    let bin = {
        let mut guard = state
            .ollama_bin
            .lock()
            .map_err(|e| format!("ollama_bin lock poisoned: {e}"))?;
        if let Some(p) = guard.as_ref() {
            p.clone()
        } else {
            let p = find_ollama_binary(&state.data_dir)
                .or_else(|| ensure_managed_ollama(&app, &state.data_dir).ok())
                .ok_or_else(|| {
                    "Ollama is not available (download failed or MINION_SKIP_MANAGED_OLLAMA=1)."
                        .to_string()
                })?;
            *guard = Some(p.clone());
            p
        }
    };
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
            &state.api_token,
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
    let api_port = resolve_initial_sidecar_port();
    let api_token = sidecar_api_token(&data_dir);

    // Sidecar + Ollama setup run in `setup` below (needs AppHandle for
    // managed Ollama download progress and bundled path resolution).
    let state = AppState {
        sidecar: Mutex::new(None),
        ollama: Mutex::new(None),
        ollama_bin: Mutex::new(None),
        vision_model: Mutex::new(None),
        data_dir: data_dir.clone(),
        inbox: inbox.clone(),
        api_port,
        api_token: api_token.clone(),
        sidecar_src_dir: Mutex::new(None),
        sidecar_python: Mutex::new(None),
    };

    let api_token_bg = api_token.clone();
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
            thread::spawn(move || {
                let state = match handle.try_state::<AppState>() {
                    Some(s) => s,
                    None => {
                        dbg("setup", serde_json::json!({"state": "no_state"}));
                        return;
                    }
                };
                let target_model = std::env::var("MINION_VISION_MODEL")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| DEFAULT_VISION_MODEL.to_string());
                let mut needs_pull_local = false;
                let mut vision_opt: Option<String> = None;

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
                let _ = handle.emit(
                    "sidecar://status",
                    serde_json::json!({
                        "state": "starting",
                        "message": format!("Sidecar source: {}", src_dir.display()),
                    }),
                );

                let requirements = match resolve_sidecar_requirements(&handle, &src_dir) {
                    Some(r) => r,
                    None => {
                        let _ = handle.emit(
                            "sidecar://status",
                            serde_json::json!({
                                "state": "error",
                                "message": "Bundled Python requirements missing. Reinstall the app.",
                            }),
                        );
                        return;
                    }
                };

                let _ = handle.emit(
                    "sidecar://status",
                    serde_json::json!({
                        "state": "starting",
                        "message": "Preparing Ollama (image captions)…",
                    }),
                );
                if let Some(ref bin) = resolve_ollama_for_minion(&handle, &data_dir_bg) {
                    if let Ok(mut og) = state.ollama_bin.lock() {
                        *og = Some(bin.clone());
                    }
                    let ochild = spawn_ollama(bin);
                    if let Ok(mut og) = state.ollama.lock() {
                        *og = ochild;
                    }
                    if wait_for_ollama(Duration::from_secs(15)) {
                        if ollama_has_model(bin, &target_model) {
                            vision_opt = Some(target_model.clone());
                        } else {
                            needs_pull_local = true;
                        }
                    }
                }
                if let Ok(mut vm) = state.vision_model.lock() {
                    *vm = vision_opt.clone();
                }

                let python = match bootstrap_venv(&handle, &data_dir_bg, &requirements) {
                    Ok(p) => p,
                    Err(e) => {
                        dbg("setup", serde_json::json!({"state": "bootstrap_err", "error": &e}));
                        let _ = handle.emit(
                            "sidecar://status",
                            serde_json::json!({"state": "error", "message": e}),
                        );
                        return;
                    }
                };
                if let Ok(mut g) = state.sidecar_python.lock() {
                    *g = Some(python.clone());
                }

                let _ = handle.emit(
                    "sidecar://status",
                    serde_json::json!({
                        "state": "starting",
                        "message": format!("Spawning sidecar on port {}…", port_bg),
                    }),
                );

                let child = spawn_sidecar(
                    &python,
                    &src_dir,
                    &data_dir_bg,
                    &inbox_bg,
                    port_bg,
                    vision_opt.as_deref(),
                    &api_token_bg,
                );
                if let Some(c) = child {
                    if let Ok(mut g) = state.sidecar.lock() {
                        *g = Some(c);
                    }
                    let _ = handle.emit(
                        "sidecar://status",
                        serde_json::json!({
                            "state": "starting",
                            "message": format!(
                                "Waiting for indexer HTTP on 127.0.0.1:{} (first start can take a minute)…",
                                port_bg
                            ),
                        }),
                    );
                    // Do not tell the UI we're ready until `/status` actually works — otherwise
                    // the webview races fetches against a process still importing deps.
                    if !wait_for_sidecar_http_ready(port_bg, 120_000) {
                        if let Ok(mut g) = state.sidecar.lock() {
                            if let Some(mut ch) = g.take() {
                                let _ = ch.kill();
                                let _ = ch.wait();
                            }
                        }
                        let log_hint = data_dir_bg.join("logs").join("sidecar.log");
                        let _ = handle.emit(
                            "sidecar://status",
                            serde_json::json!({
                                "state": "error",
                                "message": format!(
                                    "Sidecar started but never answered /status. See {}",
                                    log_hint.display()
                                ),
                            }),
                        );
                        return;
                    }
                    let _ = handle.emit(
                        "sidecar://status",
                        serde_json::json!({"state": "ready"}),
                    );
                    if needs_pull_local {
                        let handle2 = handle.clone();
                        let model = target_model.clone();
                        thread::spawn(move || {
                            let state = match handle2.try_state::<AppState>() {
                                Some(s) => s,
                                None => return,
                            };
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
                            let _ = emit_vision(&handle2, "start", &format!("pulling {model}…"));
                            if let Err(e) =
                                ensure_vision_model(handle2.clone(), state, Some(model.clone()))
                            {
                                let _ = emit_vision(
                                    &handle2,
                                    "error",
                                    &format!("auto-enable failed: {e}"),
                                );
                            }
                        });
                    }
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
