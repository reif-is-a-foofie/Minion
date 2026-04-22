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
  /** Empty when sidecar runs without MINION_API_TOKEN. */
  api_token: string;
  sidecar_bootstrapped: boolean;
  sidecar_running: boolean;
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

export type DatabaseStatus = {
  ok: boolean;
  error: string | null;
  journal_mode: string | null;
};

export type Status = {
  data_dir: string;
  inbox: string;
  db_path: string;
  supported_extensions: string[];
  counts: { sources: number; chunks: number };
  active: Active;
  /** Present on newer sidecars; when ok is false, ingest/search are blocked. */
  database?: DatabaseStatus;
  watcher: { running: boolean; mode?: string };
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
  | { type: "db_error"; message: string };

/** Always ask the Rust shell — never cache. Stale `api_base` after a port
 * change or sidecar restart caused POST /nuke to hit the wrong listener (404). */
export async function getConfig(): Promise<AppConfig> {
  return (await invoke("app_config")) as AppConfig;
}

async function assertSidecarHasNukeRoute(apiBase: string): Promise<void> {
  const maxAttempts = 18;
  const delayMs = 300;
  let lastNet: Error | undefined;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    let res: Response;
    try {
      res = await fetch(`${apiBase}/openapi.json`, { headers: { accept: "application/json" } });
    } catch (e) {
      lastNet = e instanceof Error ? e : new Error(String(e));
      if (attempt < maxAttempts - 1) {
        await new Promise((r) => setTimeout(r, delayMs));
        continue;
      }
      throw new Error(
        `Cannot reach sidecar at ${apiBase}: ${lastNet.message}. Try Settings → Restart.`,
      );
    }
    if (!res.ok) {
      if (attempt < maxAttempts - 1 && (res.status === 502 || res.status === 503 || res.status === 404)) {
        await new Promise((r) => setTimeout(r, delayMs));
        continue;
      }
      throw new Error(`Sidecar at ${apiBase} returned ${res.status}. Try Settings → Restart or update Minion.`);
    }
    const text = await res.text();
    if (!text.includes('"/nuke"')) {
      throw new Error(
        `The server at ${apiBase} is not this Minion build (missing /nuke — often another user or app on the same port). Click Restart in Settings.`,
      );
    }
    return;
  }
}

function authHeaders(cfg: AppConfig, extra?: HeadersInit): Record<string, string> {
  const h: Record<string, string> = { "content-type": "application/json" };
  if (extra) {
    if (extra instanceof Headers) {
      extra.forEach((v, k) => {
        h[k] = v;
      });
    } else if (Array.isArray(extra)) {
      for (const [k, v] of extra) h[k] = v;
    } else {
      Object.assign(h, extra as Record<string, string>);
    }
  }
  if (cfg.api_token) {
    h["authorization"] = `Bearer ${cfg.api_token}`;
  }
  return h;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const cfg = await getConfig();
  const res = await fetch(`${cfg.api_base}${path}`, {
    ...init,
    headers: authHeaders(cfg, init?.headers as HeadersInit | undefined),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as T;
}

export function isNotFoundError(e: unknown): boolean {
  const msg = (e as any)?.message ? String((e as any).message) : String(e);
  return msg.includes("404") || msg.includes("Not Found");
}

export async function fetchStatus(init?: RequestInit): Promise<Status> {
  const cfg = await getConfig();
  const res = await fetch(`${cfg.api_base}/status`, {
    ...init,
    headers: authHeaders(cfg, init?.headers as HeadersInit | undefined),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as Status;
}

/** Poll GET /status until it succeeds (e.g. after sidecar restart the port is bound before accept()). */
export async function waitForHealthySidecar(maxMs = 20_000, init?: RequestInit): Promise<Status> {
  const deadline = Date.now() + maxMs;
  let last: unknown;
  while (Date.now() < deadline) {
    try {
      return await fetchStatus(init);
    } catch (e) {
      last = e;
      await new Promise((r) => setTimeout(r, 350));
    }
  }
  throw last instanceof Error ? last : new Error(String(last));
}

export async function fetchSources(
  params: {
    kind?: string;
    path_glob?: string;
    since?: number;
    limit?: number;
  } = {},
  init?: RequestInit,
): Promise<{ sources: Source[]; counts: { sources: number; chunks: number } }> {
  const q = new URLSearchParams();
  if (params.kind) q.set("kind", params.kind);
  if (params.path_glob) q.set("path_glob", params.path_glob);
  if (params.since) q.set("since", String(params.since));
  if (params.limit) q.set("limit", String(params.limit));
  const qs = q.toString();
  const cfg = await getConfig();
  const res = await fetch(`${cfg.api_base}/sources${qs ? `?${qs}` : ""}`, {
    ...init,
    headers: authHeaders(cfg, init?.headers as HeadersInit | undefined),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as { sources: Source[]; counts: { sources: number; chunks: number } };
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

export type IdentityClaim = {
  claim_id: string;
  kind: string;
  text: string;
  status: string;
  confidence?: number | null;
  source_agent?: string | null;
  created_at: number;
  updated_at: number;
  superseded_by?: string | null;
  meta?: Record<string, unknown>;
};

export type IdentityEdge = {
  edge_id: string;
  claim_id: string;
  chunk_id: string | null;
  source_id: string | null;
  rationale: string | null;
  created_at: number;
};

export async function fetchIdentityClaims(params: {
  status?: string;
  kind?: string;
  limit?: number;
} = {}): Promise<{ claims: IdentityClaim[]; count: number }> {
  const q = new URLSearchParams();
  if (params.status) q.set("status", params.status);
  if (params.kind) q.set("kind", params.kind);
  if (params.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  return apiFetch(`/identity/claims${qs ? `?${qs}` : ""}`);
}

export async function patchIdentityClaim(
  claimId: string,
  body: { status: string; superseded_by?: string },
): Promise<{ claim: IdentityClaim | null }> {
  return apiFetch(`/identity/claims/${encodeURIComponent(claimId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function fetchIdentityClaimEdges(
  claimId: string,
): Promise<{ edges: IdentityEdge[]; count: number }> {
  return apiFetch(`/identity/claims/${encodeURIComponent(claimId)}/edges`);
}

export async function fetchChunk(
  chunkId: string,
  max_chars?: number,
): Promise<{
  chunk_id: string;
  source_id: string;
  role: string | null;
  path: string;
  kind: string;
  mtime: number;
  text: string;
  meta: Record<string, unknown>;
}> {
  const q = max_chars != null ? `?max_chars=${max_chars}` : "";
  return apiFetch(`/chunks/${encodeURIComponent(chunkId)}${q}`);
}

export async function exportIdentityBundle(body: {
  out_path?: string;
  include_chunk_index?: boolean;
} = {}): Promise<{ path: string; manifest: Record<string, unknown> }> {
  return apiFetch("/identity/export", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function rebuildPreferenceClusters(body: {
  sample_limit?: number;
  k?: number;
  use_llm?: boolean;
} = {}): Promise<Record<string, unknown>> {
  return apiFetch("/identity/clusters/rebuild", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Full inbox scan → DB. Use `force: true` to re-embed even when sha unchanged (slow). */
export async function reconcileInbox(body: { force?: boolean } = {}): Promise<{ started: boolean; force: boolean }> {
  return apiFetch("/reconcile", {
    method: "POST",
    body: JSON.stringify({ force: body.force ?? false }),
  });
}

/// Subscribe to GET /search/stream (SSE). Calls onHit for each result; onDone when finished.
export function openSearchStream(
  query: string,
  opts: { top_k?: number; kind?: string; path_glob?: string; role?: string; max_chars?: number } = {},
  handlers: {
    onMeta?: (n: number) => void;
    onHit: (hit: SearchHit) => void;
    onDone?: () => void;
    onError?: (msg: string) => void;
  },
): () => void {
  let cancelled = false;
  (async () => {
    const cfg = await getConfig();
    const q = new URLSearchParams({ query });
    if (opts.top_k != null) q.set("top_k", String(opts.top_k));
    if (opts.kind) q.set("kind", opts.kind);
    if (opts.path_glob) q.set("path_glob", opts.path_glob);
    if (opts.role) q.set("role", opts.role);
    if (opts.max_chars != null) q.set("max_chars", String(opts.max_chars));
    const url = `${cfg.api_base}/search/stream?${q}`;
    try {
      const res = await fetch(url, {
        headers: cfg.api_token ? { authorization: `Bearer ${cfg.api_token}` } : undefined,
      });
      if (!res.ok || !res.body) {
        handlers.onError?.(`${res.status} ${res.statusText}`);
        return;
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (!cancelled) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const lines = block.split("\n");
          let ev = "";
          let data = "";
          for (const ln of lines) {
            if (ln.startsWith("event:")) ev = ln.slice(6).trim();
            if (ln.startsWith("data:")) data = ln.slice(5).trim();
          }
          if (ev === "meta") {
            try {
              const o = JSON.parse(data) as { count?: number };
              handlers.onMeta?.(o.count ?? 0);
            } catch {
              /* ignore */
            }
          } else if (ev === "hit") {
            try {
              handlers.onHit(JSON.parse(data) as SearchHit);
            } catch {
              /* ignore */
            }
          } else if (ev === "done") {
            handlers.onDone?.();
          } else if (ev === "error") {
            try {
              const o = JSON.parse(data) as { message?: string };
              handlers.onError?.(o.message ?? "stream error");
            } catch {
              handlers.onError?.("stream error");
            }
          }
        }
      }
    } catch (e) {
      if (!cancelled) handlers.onError?.((e as Error).message ?? String(e));
    }
  })();
  return () => {
    cancelled = true;
  };
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

export async function nukeDb(): Promise<{ removed: string[]; missing: string[]; db_path: string }> {
  const cfg = await getConfig();
  await assertSidecarHasNukeRoute(cfg.api_base);
  return apiFetch("/nuke", { method: "POST" });
}

export async function factoryReset(): Promise<{
  removed: string[];
  missing: string[];
  db_path: string;
  inbox: string;
  inbox_removed: string[];
  inbox_missing: string[];
}> {
  const cfg = await getConfig();
  await assertSidecarHasNukeRoute(cfg.api_base);
  return apiFetch("/factory-reset", { method: "POST" });
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

export async function revealInFinder(path: string): Promise<void> {
  await invoke("reveal_in_finder", { path });
}
