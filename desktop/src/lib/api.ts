// Tiny typed client for the Python sidecar. The base URL comes from Rust
// (app_config command) so we stay in sync with whatever port the sidecar
// actually bound to.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export type SidecarStatus = {
  state: "starting" | "bootstrapping" | "installing" | "ready" | "error";
  message?: string;
};

/// Subscribe to `sidecar://status` events emitted by Rust during first-launch
/// bootstrap (locating sidecar, creating venv, pip-installing). Returns an
/// unsubscribe fn. Used to render a "Setting up Minion…" overlay.
export async function onSidecarStatus(fn: (s: SidecarStatus) => void): Promise<() => void> {
  const unlisten = await listen<SidecarStatus>("sidecar://status", (ev) => fn(ev.payload));
  return unlisten;
}

export type AppConfig = {
  data_dir: string;
  inbox: string;
  api_port: number;
  api_base: string;
  /** `<data_dir>/logs` — sidecar stdout/stderr and shell events (release .app). */
  logs_dir: string;
  desktop_log: string;
  sidecar_log: string;
};

export type Source = {
  source_id: string;
  path: string;
  kind: string;
  sha256: string;
  mtime: number;
  bytes: number;
  parser: string;
  updated_at: number;
  chunk_count?: number;
  meta?: Record<string, unknown>;
};

export type SearchHit = {
  score: number;
  chunk_id: string;
  role?: string | null;
  source_id: string;
  path: string;
  kind: string;
  mtime: number;
  text: string;
  meta?: Record<string, unknown>;
};

export type Active = {
  root: string | null;
  total: number;
  done: number;
  added: number;
  skipped: number;
};

export type Status = {
  data_dir: string;
  inbox: string;
  db_path: string;
  supported_extensions: string[];
  counts: { sources: number; chunks: number };
  active: Active;
  watcher: { running: boolean };
};

export type EventMsg =
  | { type: "snapshot"; counts: { sources: number; chunks: number }; active?: Active }
  | { type: "ready"; counts: { sources: number; chunks: number }; active?: Active }
  | { type: "heartbeat"; counts: { sources: number; chunks: number }; active?: Active }
  | { type: "ingest_started"; path?: string; source?: string; count?: number; active?: Active }
  | { type: "ingest_progress"; path: string; index: number; total: number }
  | { type: "file_progress"; path: string; index: number; total: number; stage: string; [k: string]: any }
  | { type: "ingest_skipped"; result: Record<string, unknown>; active?: Active }
  | { type: "ingest_failed"; path: string; active?: Active }
  | { type: "source_updated"; result: Record<string, unknown>; counts: any; active?: Active }
  | { type: "source_removed"; key: string; counts: any }
  | { type: "tree_done"; root: string; added: number; skipped: number; counts: any }
  | { type: "ingest_delight"; path: string; line: string };

let cachedConfig: AppConfig | null = null;

export async function getConfig(): Promise<AppConfig> {
  if (cachedConfig) return cachedConfig;
  cachedConfig = (await invoke("app_config")) as AppConfig;
  return cachedConfig;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const cfg = await getConfig();
  const res = await fetch(`${cfg.api_base}${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as T;
}

export async function fetchStatus(): Promise<Status> {
  return apiFetch<Status>("/status");
}

export async function fetchSources(params: {
  kind?: string;
  path_glob?: string;
  since?: number;
  limit?: number;
} = {}): Promise<{ sources: Source[]; counts: { sources: number; chunks: number } }> {
  const q = new URLSearchParams();
  if (params.kind) q.set("kind", params.kind);
  if (params.path_glob) q.set("path_glob", params.path_glob);
  if (params.since) q.set("since", String(params.since));
  if (params.limit) q.set("limit", String(params.limit));
  const qs = q.toString();
  return apiFetch(`/sources${qs ? `?${qs}` : ""}`);
}

export async function search(body: {
  query: string;
  top_k?: number;
  kind?: string;
  path_glob?: string;
}): Promise<{ results: SearchHit[] }> {
  return apiFetch("/search", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function ingestPath(path: string, move = false): Promise<{ queued: string }> {
  return apiFetch("/ingest", {
    method: "POST",
    body: JSON.stringify({ path, move }),
  });
}

export async function connectClaudeDesktop(body: { server_name?: string; config_path?: string } = {}): Promise<{
  config_path: string;
  backup_path: string | null;
  server_name: string;
  restart_required: boolean;
}> {
  return apiFetch("/connect/claude-desktop", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deleteSource(body: { path?: string; source_id?: string }): Promise<{ removed_chunks: number }> {
  return apiFetch("/sources", {
    method: "DELETE",
    body: JSON.stringify(body),
  });
}

export type ConnState = "connecting" | "open" | "closed" | "unreachable";

/// Connect to the sidecar's `/events` WebSocket with bounded retries.
/// Backoff schedule: 1.5s, 3s, 6s, 12s, 20s (capped). After ~8 attempts
/// without a single successful open, flip to "unreachable" so the UI can
/// show an actionable error instead of silently reconnecting forever.
export async function openEvents(
  onMessage: (e: EventMsg) => void,
  onStatus?: (s: ConnState) => void,
): Promise<() => void> {
  let closed = false;
  let ws: WebSocket | null = null;
  let attempts = 0;
  let everOpened = false;
  const MAX_ATTEMPTS_BEFORE_UNREACHABLE = 8;
  const backoff = (n: number) => Math.min(1500 * Math.pow(1.6, n), 20000);

  const connect = async () => {
    if (closed) return;
    attempts += 1;
    onStatus?.("connecting");
    try {
      const cfg = await getConfig();
      ws = new WebSocket(`${cfg.api_base.replace("http", "ws")}/events`);
    } catch (err) {
      // Tauri invoke can reject early if backend is still spinning up.
      if (!everOpened && attempts >= MAX_ATTEMPTS_BEFORE_UNREACHABLE) {
        onStatus?.("unreachable");
      } else {
        onStatus?.("closed");
      }
      setTimeout(connect, backoff(attempts));
      return;
    }
    ws.onopen = () => {
      everOpened = true;
      attempts = 0;
      onStatus?.("open");
    };
    ws.onmessage = (ev) => {
      try {
        onMessage(JSON.parse(ev.data));
      } catch {
        // ignore malformed
      }
    };
    ws.onclose = () => {
      if (closed) return;
      if (!everOpened && attempts >= MAX_ATTEMPTS_BEFORE_UNREACHABLE) {
        onStatus?.("unreachable");
      } else {
        onStatus?.("closed");
      }
      setTimeout(connect, backoff(attempts));
    };
    ws.onerror = () => {
      if (!everOpened && attempts >= MAX_ATTEMPTS_BEFORE_UNREACHABLE) {
        onStatus?.("unreachable");
      } else {
        onStatus?.("closed");
      }
    };
  };
  await connect();

  return () => {
    closed = true;
    ws?.close();
  };
}

export async function restartSidecar(): Promise<{ pid: number; api_port: number }> {
  return (await invoke("restart_sidecar")) as { pid: number; api_port: number };
}

export type Settings = {
  disabled_kinds: string[];
};

export type SettingsResponse = {
  settings: Settings;
  all_kinds: string[];
};

export async function fetchSettings(): Promise<SettingsResponse> {
  return apiFetch<SettingsResponse>("/settings");
}

export async function updateSettings(body: Partial<Settings>): Promise<SettingsResponse> {
  return apiFetch<SettingsResponse>("/settings", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export type VisionState = "unavailable" | "off" | "pulling" | "ready";
export type VisionStatus = {
  state: VisionState;
  model: string;
  installed: boolean;
  server_up: boolean;
};

export async function visionStatus(): Promise<VisionStatus> {
  return (await invoke("vision_status")) as VisionStatus;
}

export async function ensureVisionModel(model?: string): Promise<{ state: VisionState; model: string }> {
  return (await invoke("ensure_vision_model", { model })) as { state: VisionState; model: string };
}

export type CopyDrop = {
  source: string;
  kind: "file" | "directory" | "missing" | "unsupported" | "duplicate";
  dest?: string;
  copied: number;
  bytes: number;
  skipped_dirs?: number;
  skipped_dotfiles?: number;
  errors?: string[];
  paths?: string[];
};

export type CopyResult = {
  drops: CopyDrop[];
  inbox: string;
};

export async function copyIntoInbox(paths: string[]): Promise<CopyResult> {
  return (await invoke("copy_into_inbox", { paths })) as CopyResult;
}

/** Tell the sidecar these inbox paths came from an explicit drop / picker (stronger pin harvest). */
export async function registerSharedPaths(paths: string[]): Promise<{ registered: number }> {
  if (!paths.length) return { registered: 0 };
  return apiFetch("/pins/register_shared", {
    method: "POST",
    body: JSON.stringify({ paths }),
  });
}

export async function revealInFinder(path: string): Promise<void> {
  await invoke("reveal_in_finder", { path });
}
