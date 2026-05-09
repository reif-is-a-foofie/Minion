//! Logs focused-window metadata (and optional window screenshots) for MCP tools.
//!
//! Writes newline-delimited JSON to `<MINION_DATA_DIR>/screen_context/stream.jsonl`.
//! Screenshots go to `<inbox>/screen-memory/` so the ingest watcher OCRs them.

use std::path::PathBuf;

pub fn spawn_watcher(app: tauri::AppHandle, data_dir: PathBuf, inbox: PathBuf) {
    if minion_screen_context_disabled() {
        return;
    }
    imp::spawn(app, data_dir, inbox);
}

fn minion_screen_context_disabled() -> bool {
    let Ok(v) = std::env::var("MINION_SCREEN_CONTEXT") else {
        return false;
    };
    matches!(
        v.trim().to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off"
    )
}

#[cfg(target_os = "macos")]
mod imp {
    use std::fs::OpenOptions;
    use std::io::Write;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use active_win_pos_rs::get_active_window;
    use serde_json::json;
    use tauri::Emitter;

    fn ts_unix_float() -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    }

    fn fingerprint(app_name: &str, title: &str, window_id: &str) -> String {
        format!("{app_name}\x1f{title}\x1f{window_id}")
    }

    fn try_capture_window_png(window_id: &str, out_path: &Path) -> bool {
        let Ok(wid) = window_id.parse::<u32>() else {
            return false;
        };
        if wid == 0 {
            return false;
        }
        let Some(out_str) = out_path.to_str() else {
            return false;
        };
        if let Some(parent) = out_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        match Command::new("/usr/sbin/screencapture")
            .args(["-x", "-t", "png", "-l", &wid.to_string(), out_str])
            .status()
        {
            Ok(st) => st.success(),
            Err(_) => false,
        }
    }

    pub fn spawn(app: tauri::AppHandle, data_dir: PathBuf, inbox: PathBuf) {
        thread::spawn(move || run_loop(app, data_dir, inbox));
    }

    fn screen_capture_enabled() -> bool {
        match std::env::var("MINION_SCREEN_CAPTURE") {
            Ok(s) => {
                let t = s.trim().to_ascii_lowercase();
                !(t.is_empty() || t == "0" || t == "false" || t == "no" || t == "off")
            }
            Err(_) => true,
        }
    }

    fn ax_capture_enabled() -> bool {
        match std::env::var("MINION_AX_CAPTURE") {
            Ok(s) => {
                let t = s.trim().to_ascii_lowercase();
                !(t.is_empty() || t == "0" || t == "false" || t == "no" || t == "off")
            }
            Err(_) => true,
        }
    }

    fn ax_max_chars() -> usize {
        std::env::var("MINION_AX_TEXT_MAX_CHARS")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(12_000usize)
            .clamp(500, 100_000)
    }

    fn ax_max_depth() -> usize {
        std::env::var("MINION_AX_MAX_DEPTH")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(14usize)
            .clamp(4, 64)
    }

    fn poll_interval() -> Duration {
        let secs = std::env::var("MINION_SCREEN_CONTEXT_POLL_SEC")
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(5)
            .clamp(2, 120);
        Duration::from_secs(secs)
    }

    fn run_loop(app: tauri::AppHandle, data_dir: PathBuf, inbox: PathBuf) {
        let stream_dir = data_dir.join("screen_context");
        let _ = std::fs::create_dir_all(&stream_dir);
        let stream_path = stream_dir.join("stream.jsonl");

        let mut last_fp: Option<String> = None;

        loop {
            thread::sleep(poll_interval());

            let win = match get_active_window() {
                Ok(w) => w,
                Err(_) => continue,
            };

            let title = win.title.trim().to_string();
            let app_name = win.app_name.trim().to_string();
            let window_id = win.window_id.trim().to_string();
            let fp = fingerprint(&app_name, &title, &window_id);

            if last_fp.as_ref() == Some(&fp) {
                continue;
            }
            last_fp = Some(fp.clone());

            let ts = ts_unix_float();
            let process_path = win.process_path.to_string_lossy().to_string();

            let pid_i32 = if win.process_id > i32::MAX as u64 {
                -1
            } else {
                win.process_id as i32
            };

            let ax_text_sample = if ax_capture_enabled() && pid_i32 > 0 {
                crate::ax_sample::focused_window_ax_text(pid_i32, ax_max_chars(), ax_max_depth())
            } else {
                None
            };

            let mut screenshot_rel: Option<String> = None;
            if screen_capture_enabled() {
                let png_name = format!("{ts}_{window_id}.png");
                let png_path = inbox.join("screen-memory").join(&png_name);
                if try_capture_window_png(&window_id, &png_path) {
                    screenshot_rel = Some(format!("screen-memory/{png_name}"));
                }
            }

            let record = json!({
                "ts": ts,
                "kind": "window_focus",
                "app_name": app_name,
                "window_title": title,
                "process_path": process_path,
                "window_id": window_id,
                "ax_text_sample": ax_text_sample,
                "screenshot_inbox_rel": screenshot_rel,
            });

            if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&stream_path) {
                let line = format!("{}\n", record);
                let _ = f.write_all(line.as_bytes());
                let _ = f.sync_all();
            }

            let _ = app.emit(
                "screen-context://update",
                json!({
                    "app_name": record["app_name"],
                    "window_title": record["window_title"],
                    "screenshot": screenshot_rel,
                }),
            );
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod imp {
    use std::path::PathBuf;

    pub fn spawn(
        _app: tauri::AppHandle,
        _data_dir: PathBuf,
        _inbox: PathBuf,
    ) {
    }
}
