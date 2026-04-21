// Minion desktop shell.
//
// Responsibilities:
// - Resolve (and create) the user's Minion data dir + inbox
// - Spawn the Python API sidecar as a managed child process (dev: use repo venv;
//   prod: use a bundled sidecar binary -- see scripts/build_sidecar.sh)
// - Expose minimal Tauri commands the frontend uses:
//     app_config, copy_into_inbox, reveal_in_finder
// Native OS file drops are delivered to the frontend by Tauri v2 as the
// `tauri://drag-drop` event; the frontend forwards the paths to
// `copy_into_inbox`.

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Emitter, Manager, WindowEvent};

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

fn find_dev_python_sidecar() -> Option<(PathBuf, Vec<String>)> {
    // <repo>/desktop/src-tauri/  -> <repo>/chatgpt_mcp_memory/src/api.py
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo = manifest.parent()?.parent()?;
    let api = repo.join("chatgpt_mcp_memory").join("src").join("api.py");
    if !api.exists() {
        return None;
    }
    let venv = repo.join("chatgpt_mcp_memory").join(".venv").join("bin").join("python");
    let python = if venv.exists() { venv } else { PathBuf::from("python3") };
    Some((python, vec![api.to_string_lossy().into_owned()]))
}

fn spawn_sidecar(
    data_dir: &Path,
    inbox: &Path,
    api_port: u16,
    vision_model: Option<&str>,
) -> Option<Child> {
    let (python, mut args) = find_dev_python_sidecar()?;
    args.push("--port".into());
    args.push(api_port.to_string());

    let mut cmd = Command::new(python);
    cmd.args(&args)
        .env("MINION_DATA_DIR", data_dir)
        .env("MINION_INBOX", inbox)
        .env("MINION_API_PORT", api_port.to_string())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    if let Some(model) = vision_model {
        cmd.env("MINION_VISION_MODEL", model);
    }

    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("[minion] failed to spawn sidecar: {e}");
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
    serde_json::json!({
        "data_dir": state.data_dir.to_string_lossy(),
        "inbox": state.inbox.to_string_lossy(),
        "api_port": state.api_port,
        "api_base": format!("http://127.0.0.1:{}", state.api_port),
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
            if name_str.starts_with('.') {
                stats.skipped_dotfiles += 1;
                continue;
            }
            let src_path = entry.path();
            let file_type = match entry.file_type() {
                Ok(t) => t,
                Err(_) => continue,
            };
            if file_type.is_dir() {
                if should_skip_dir(&name_str) {
                    stats.skipped_dirs += 1;
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

/// Kill the running sidecar (if any) and respawn it with the same data_dir,
/// inbox, and port. Returns the new PID. Used by the UI "Restart" action so
/// users can recover from a hung sidecar or pick up code changes in dev.
#[tauri::command]
fn restart_sidecar(state: tauri::State<AppState>) -> Result<serde_json::Value, String> {
    let mut guard = state
        .sidecar
        .lock()
        .map_err(|e| format!("sidecar lock poisoned: {e}"))?;
    if let Some(mut child) = guard.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    // Small delay lets the OS release the TCP port before the new sidecar
    // tries to bind. 200ms is enough in practice; we also retry-bind below.
    std::thread::sleep(std::time::Duration::from_millis(200));
    let current_model = state.vision_model.lock().ok().and_then(|g| g.clone());
    let new_child = spawn_sidecar(
        &state.data_dir,
        &state.inbox,
        state.api_port,
        current_model.as_deref(),
    )
    .ok_or_else(|| "failed to respawn sidecar".to_string())?;
    let pid = new_child.id();
    *guard = Some(new_child);
    Ok(serde_json::json!({
        "pid": pid,
        "api_port": state.api_port,
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
        let new_child = spawn_sidecar(&state.data_dir, &state.inbox, state.api_port, Some(&model))
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
    let target = if p.is_file() {
        p.parent().map(Path::to_path_buf).unwrap_or(p)
    } else {
        p
    };
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg(target).spawn().map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "windows")]
    {
        Command::new("explorer").arg(target).spawn().map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "linux")]
    {
        Command::new("xdg-open").arg(target).spawn().map_err(|e| e.to_string())?;
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

    let child = spawn_sidecar(&data_dir, &inbox, api_port, vision_model.as_deref());
    let state = AppState {
        sidecar: Mutex::new(child),
        ollama: Mutex::new(ollama_child),
        ollama_bin: ollama_bin.clone(),
        vision_model: Mutex::new(vision_model),
        data_dir: data_dir.clone(),
        inbox: inbox.clone(),
        api_port,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(state)
        .setup(move |app| {
            // First-launch auto-pull: if ollama is present but the default
            // vision model isn't, grab it in the background. Progress streams
            // into the UI terminal via the `vision://progress` event so it
            // shows up in the same log as everything else.
            if needs_pull {
                let handle = app.handle().clone();
                let model = target_model.clone();
                thread::spawn(move || {
                    // Route through ensure_vision_model so pull + sidecar
                    // restart + env wiring all happen in one place.
                    let state = match handle.try_state::<AppState>() {
                        Some(s) => s,
                        None => return,
                    };
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
