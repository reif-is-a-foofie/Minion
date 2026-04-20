// Tiny typed client for the Python sidecar. The base URL comes from Rust
// (app_config command) so we stay in sync with whatever port the sidecar
// actually bound to.

import { invoke } from "@tauri-apps/api/core";

export type AppConfig = {
  data_dir: string;
  inbox: string;
  api_port: number;
  api_base: string;
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

export type Status = {
  data_dir: string;
  inbox: string;
  db_path: string;
  supported_extensions: string[];
  counts: { sources: number; chunks: number };
  watcher: { running: boolean };
};

export type EventMsg =
  | { type: "snapshot"; counts: { sources: number; chunks: number } }
  | { type: "ready"; counts: { sources: number; chunks: number } }
  | { type: "heartbeat"; counts: { sources: number; chunks: number } }
  | { type: "ingest_started"; path: string }
  | { type: "ingest_skipped"; result: Record<string, unknown> }
  | { type: "source_updated"; result: Record<string, unknown>; counts: any }
  | { type: "source_removed"; key: string; counts: any }
  | { type: "tree_done"; root: string; added: number; skipped: number; counts: any };

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

export async function openEvents(onMessage: (e: EventMsg) => void): Promise<() => void> {
  const cfg = await getConfig();
  const ws = new WebSocket(`${cfg.api_base.replace("http", "ws")}/events`);
  ws.onmessage = (ev) => {
    try {
      onMessage(JSON.parse(ev.data));
    } catch {
      // ignore malformed
    }
  };
  // Auto-reconnect on close with a small backoff. The sidecar restart during
  // dev happens fast enough that a 1.5s retry is usually enough.
  let closed = false;
  ws.onclose = () => {
    if (closed) return;
    setTimeout(() => openEvents(onMessage), 1500);
  };
  return () => {
    closed = true;
    ws.close();
  };
}

export async function copyIntoInbox(paths: string[]): Promise<string[]> {
  return (await invoke("copy_into_inbox", { paths })) as string[];
}

export async function revealInFinder(path: string): Promise<void> {
  await invoke("reveal_in_finder", { path });
}
