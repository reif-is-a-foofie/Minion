<script lang="ts">
  import { onMount } from "svelte";
  import { listen } from "@tauri-apps/api/event";
  import { open as openDialog } from "@tauri-apps/plugin-dialog";
  import {
    connectClaudeDesktop,
    copyIntoInbox,
    deleteSource,
    exportIdentityBundle,
    factoryReset,
    fetchChunk,
    fetchIdentityClaimEdges,
    fetchIdentityClaims,
    fetchSettings,
    fetchSources,
    fetchStatus,
    getConfig,
    ingestPath,
    nukeDb,
    onSidecarStatus,
    openEvents,
    patchIdentityClaim,
    reconcileInbox,
    rebuildPreferenceClusters,
    restartSidecar,
    revealInFinder,
    search,
    updateSettings,
    waitForHealthySidecar,
    isNotFoundError,
    type Active,
    type AppConfig,
    type ConnState,
    type IdentityClaim,
    type SearchHit,
    type SidecarStatus,
    type Source,
    type Status,
  } from "$lib/api";

  // UI state
  let config = $state<AppConfig | null>(null);
  let status = $state<Status | null>(null);
  let sources = $state<Source[]>([]);
  let filterKind = $state<string>("");
  let queryText = $state<string>("");
  let searchResults = $state<SearchHit[]>([]);
  let searching = $state(false);
  let dragging = $state(false);
  let ingestFeed = $state<{ id: string; path: string; status: string; ts: number }[]>([]);
  let active = $state<Active>({ root: null, total: 0, done: 0, added: 0, skipped: 0 });
  let connecting = $state(false);
  let connectMsg = $state<string>("");
  let showSettings = $state(false);
  type SettingsNav = "status" | "library" | "identity" | "claude" | "ingest" | "advanced";
  let settingsNav = $state<SettingsNav>("status");
  const SETTINGS_PANE_TITLE: Record<SettingsNav, string> = {
    status: "Status",
    library: "Library & search",
    identity: "Identity",
    claude: "Claude (MCP)",
    ingest: "Ingest & file types",
    advanced: "Advanced",
  };
  let settingsError = $state<string | null>(null);
  let allKinds = $state<string[]>([]);
  let disabledKinds = $state<Set<string>>(new Set());
  let settingsLoaded = $state(false);
  let savingSettings = $state(false);
  let rescanning = $state(false);
  let termEl: HTMLUListElement | undefined = $state();
  // Rolling per-file line id, so subsequent progress events for the same
  // path rewrite a single terminal line instead of stacking.
  let currentRow: Record<string, string> = {};
  let conn = $state<ConnState>("connecting");
  let lastHeartbeat = $state<number>(0);
  let restarting = $state(false);
  // First-launch sidecar bootstrap status. When `state === "ready"` the
  // overlay hides; any other non-null state shows a full-screen progress
  // card so the window isn't a silent void while pip runs for ~2 minutes.
  let sidecar = $state<SidecarStatus | null>(null);
  /** Scrollable lines for bootstrap (Rust `sidecar://status` + local notes). */
  let bootstrapLog = $state<string[]>([]);

  let identityClaims = $state<IdentityClaim[]>([]);
  let identityLoading = $state(false);
  let identityTab = $state<"proposed" | "active">("proposed");
  let evidencePopup = $state<{ path: string; text: string } | null>(null);
  let clusterBusy = $state(false);
  let exportBusy = $state(false);

  async function refreshIdentity() {
    identityLoading = true;
    try {
      const st = identityTab === "proposed" ? "proposed" : "active";
      const res = await fetchIdentityClaims({ status: st, limit: 100 });
      identityClaims = res.claims;
    } catch (e) {
      pushFeed("identity", `load failed: ${(e as Error).message}`);
    } finally {
      identityLoading = false;
    }
  }

  function closeSettingsHub() {
    showSettings = false;
    evidencePopup = null;
  }

  async function selectSettingsNav(nav: SettingsNav) {
    settingsNav = nav;
    if (nav === "library") await refreshSources();
    if (nav === "identity") await refreshIdentity();
    if (nav === "ingest" && !settingsLoaded) await loadSettings();
  }

  async function openSettings(nav?: SettingsNav) {
    showSettings = true;
    if (nav) settingsNav = nav;
    if (!settingsLoaded) await loadSettings();
    if (settingsNav === "library") await refreshSources();
    if (settingsNav === "identity") await refreshIdentity();
  }

  function switchIdentityTab(tab: "proposed" | "active") {
    identityTab = tab;
    void refreshIdentity();
  }

  async function approveClaim(id: string) {
    try {
      await patchIdentityClaim(id, { status: "active" });
      pushFeed("identity", "claim approved");
      await refreshIdentity();
    } catch (e) {
      pushFeed("identity", `approve failed: ${(e as Error).message}`);
    }
  }

  async function rejectClaim(id: string) {
    try {
      await patchIdentityClaim(id, { status: "rejected" });
      pushFeed("identity", "claim rejected");
      await refreshIdentity();
    } catch (e) {
      pushFeed("identity", `reject failed: ${(e as Error).message}`);
    }
  }

  async function showClaimEvidence(claimId: string) {
    try {
      const { edges } = await fetchIdentityClaimEdges(claimId);
      const first = edges.find((e) => e.chunk_id);
      if (!first?.chunk_id) {
        pushFeed("identity", "no evidence chunks linked");
        return;
      }
      const ch = await fetchChunk(first.chunk_id, 1500);
      evidencePopup = { path: ch.path, text: ch.text };
    } catch (e) {
      pushFeed("identity", `evidence: ${(e as Error).message}`);
    }
  }

  async function runClusterRebuild() {
    if (clusterBusy) return;
    clusterBusy = true;
    try {
      const r = await rebuildPreferenceClusters({ use_llm: true });
      const msg = typeof r.status === "string" ? r.status : "ok";
      pushFeed("identity", `preference clusters: ${msg}`);
    } catch (e) {
      pushFeed("identity", `cluster rebuild: ${(e as Error).message}`);
    } finally {
      clusterBusy = false;
    }
  }

  async function runIdentityExport() {
    if (exportBusy) return;
    exportBusy = true;
    try {
      const r = await exportIdentityBundle({});
      pushFeed("identity", `exported → ${r.path.split("/").pop() ?? r.path}`);
    } catch (e) {
      pushFeed("identity", `export: ${(e as Error).message}`);
    } finally {
      exportBusy = false;
    }
  }

  // If we stop hearing from the sidecar for >12s, assume it's wedged even
  // if the socket is nominally "open" -- the UI should flag it.
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  function startHeartbeatWatchdog() {
    if (heartbeatTimer) return;
    heartbeatTimer = setInterval(() => {
      if (conn === "open" && lastHeartbeat && Date.now() - lastHeartbeat > 12_000) {
        conn = "closed";
      }
    }, 2_000);
  }

  async function handleRestart() {
    if (restarting) return;
    restarting = true;
    pushFeed("sidecar", "restart requested");
    try {
      const r = await restartSidecar();
      config = await getConfig();
      pushFeed("sidecar", `restarted (pid ${r.pid}) · ${config.api_base}`);
    } catch (e: any) {
      pushFeed("sidecar", `restart failed: ${e?.message ?? e}`);
    } finally {
      // WS will auto-reconnect; watch for the next "open" before clearing.
      setTimeout(() => (restarting = false), 1500);
    }
  }

  async function revealDataFolder() {
    if (!config?.data_dir) return;
    try {
      await revealInFinder(config.data_dir);
    } catch (e: any) {
      pushFeed("settings", `could not open folder: ${e?.message ?? e}`);
    }
  }

  async function runNukeDb() {
    const ok = confirm(
      "This will DELETE Minion's local memory database (memory.db) and telemetry, and you will lose all indexed content. Continue?",
    );
    if (!ok) return;
    pushFeed("settings", "nuking database…");
    try {
      const res = await nukeDb();
      pushFeed("settings", `db wiped: ${res.db_path.split("/").pop()}`);
      // Restart sidecar so it recreates the DB cleanly and reconnects watchers.
      await handleRestart();
      // HTTP listener can lag the process — avoid WebKit "Load failed" on immediate /status.
      status = await waitForHealthySidecar();
      await refreshSources();
    } catch (e: any) {
      pushFeed("settings", `db wipe failed: ${e?.message ?? e}`);
    }
  }

  async function runFactoryReset() {
    const ok = confirm(
      "Factory reset will DELETE Minion's local database and CLEAR the inbox directory. This is irreversible. Continue?",
    );
    if (!ok) return;
    pushFeed("settings", "factory reset…");
    try {
      const res = await factoryReset();
      pushFeed("settings", `reset complete: ${res.db_path.split("/").pop()}`);
      await handleRestart();
      status = await waitForHealthySidecar();
      await refreshSources();
    } catch (e: any) {
      const msg = e?.message ?? e;
      if (isNotFoundError(e)) {
        pushFeed("settings", "factory reset failed: your sidecar is out of date. Update Minion and click Restart.");
      } else {
        pushFeed("settings", `factory reset failed: ${msg}`);
      }
    }
  }

  async function runConnect(): Promise<boolean> {
    connecting = true;
    connectMsg = "";
    let ok = false;
    try {
      const res = await connectClaudeDesktop({});
      connectMsg = `Added to ${res.config_path.split("/").pop()}. Restart Claude Desktop to load.`;
      ok = true;
    } catch (e) {
      const msg = (e as Error).message ?? String(e);
      const short = formatHttpErrorMessage(msg);
      // If the sidecar can't write the default path (common on locked-down setups),
      // fall back to a user-picked config file path and retry once.
      if (msg.includes("403") || msg.toLowerCase().includes("permission") || msg.toLowerCase().includes("cannot write")) {
        try {
          const picked = await openDialog({
            title: "Select claude_desktop_config.json",
            multiple: false,
            directory: false,
            filters: [{ name: "JSON", extensions: ["json"] }],
          });
          if (typeof picked === "string" && picked) {
            const res = await connectClaudeDesktop({ config_path: picked });
            connectMsg = `Added to ${res.config_path.split("/").pop()}. Restart Claude Desktop to load.`;
            ok = true;
          } else {
            connectMsg = `Couldn’t add MCP: ${short}`;
          }
        } catch (e2) {
          connectMsg = `Couldn’t add MCP: ${short}`;
        }
      } else {
        connectMsg = `Couldn’t add MCP: ${short}`;
      }
    } finally {
      connecting = false;
    }
    return ok;
  }

  const KIND_DESCRIPTIONS: Record<string, string> = {
    text:             "plain text, markdown, json, yaml, csv",
    html:             "web pages, saved html",
    pdf:              "pdfs (text + OCR fallback)",
    docx:             "word documents",
    image:            "photos & screenshots (OCR + local vision caption)",
    audio:            "voice notes, recordings (transcribed via whisper)",
    video:            "video files (transcribed + keyframe OCR)",
    code:             "source code in any supported language",
    "chatgpt-export": "full chatgpt / claude archive exports",
  };

  async function loadSettings() {
    settingsError = null;
    try {
      const res = await fetchSettings();
      allKinds = res.all_kinds;
      disabledKinds = new Set(res.settings.disabled_kinds ?? []);
      settingsLoaded = true;
    } catch (e) {
      const msg = formatHttpErrorMessage((e as Error).message);
      settingsError = msg;
      pushFeed("settings", `Couldn’t load settings: ${msg}`);
    }
  }

  async function toggleKind(kind: string) {
    const next = new Set(disabledKinds);
    if (next.has(kind)) next.delete(kind);
    else next.add(kind);
    disabledKinds = next;
    savingSettings = true;
    try {
      const res = await updateSettings({ disabled_kinds: Array.from(next) });
      disabledKinds = new Set(res.settings.disabled_kinds ?? []);
    } catch (e) {
      pushFeed("settings", `Save failed: ${formatHttpErrorMessage((e as Error).message)}`);
    } finally {
      savingSettings = false;
    }
  }

  async function runRescanInbox(force: boolean) {
    if (rescanning || conn !== "open") return;
    rescanning = true;
    try {
      await reconcileInbox({ force });
      pushFeed(
        "settings",
        force ? "Re-index started (re-embedding all files)…" : "Inbox rescan started…",
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      pushFeed("settings", msg.includes("409") ? "Rescan already running — wait for it to finish." : `Rescan failed: ${msg}`);
    } finally {
      rescanning = false;
    }
  }

  const KIND_LABELS: Record<string, string> = {
    text: "Text",
    html: "Web",
    pdf: "PDF",
    docx: "Docs",
    image: "Image",
    audio: "Audio",
    video: "Video",
    code: "Code",
    "chatgpt-export": "ChatGPT",
  };

  /** Turn `404 Not Found: {"detail":"…"}` into a short line for Settings and toasts. */
  function formatHttpErrorMessage(msg: string): string {
    const jsonStart = msg.indexOf("{");
    if (jsonStart >= 0) {
      try {
        const parsed = JSON.parse(msg.slice(jsonStart)) as { detail?: unknown };
        if (typeof parsed.detail === "string") return parsed.detail;
        if (Array.isArray(parsed.detail))
          return parsed.detail.map((d) => (typeof d === "string" ? d : JSON.stringify(d))).join("; ");
      } catch {
        /* keep unparsed */
      }
    }
    const noStatus = msg.replace(/^\d{3}\s+[\w\s]+:\s*/i, "").trim();
    const core = noStatus.length < msg.length ? noStatus : msg;
    return core.length > 200 ? `${core.slice(0, 197)}…` : core;
  }

  function prettyBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  function prettyTime(ts: number): string {
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch {
      return "";
    }
  }

  async function refreshSources() {
    const res = await fetchSources({
      kind: filterKind || undefined,
      limit: 500,
    });
    sources = res.sources;
  }

  async function refreshStatus() {
    status = await fetchStatus();
  }

  async function runSearch() {
    if (!queryText.trim()) {
      searchResults = [];
      return;
    }
    searching = true;
    try {
      const res = await search({ query: queryText, top_k: 8 });
      searchResults = res.results;
    } catch (e) {
      console.error(e);
    } finally {
      searching = false;
    }
  }

  /**
   * Map a status line to a color class. Order matters: match terminal states
   * before in-flight ones so "ingested" doesn't get tagged as "parsing".
   */
  function statusClass(s: string): string {
    const t = s.toLowerCase();
    if (/\b(error|failed|parse-error)\b/.test(t)) return "err";
    // "deferred" = the app will retry this one automatically (e.g. waiting
    // for the vision model to finish pulling). Not a user-visible failure.
    if (/\bdeferred\b|awaiting\s+vision/.test(t)) return "progress";
    if (/\bingested\b|\bdone\b/.test(t)) return "ok";
    if (/\bre-indexing\b/.test(t)) return "progress";
    if (/\bduplicate\b|\bunchanged\b|already\s*(saved|present|exists)/.test(t))
      return "saved";
    if (/\bcopied\b|\bunpacked\b|\bextracted\b|\bloaded\b|\bparsed\b|\bready\b|\brestarted\b/.test(t))
      return "ok";
    if (/\bskipped\b|\bempty\b|image-only|missing-deps|no-text|unsupported/.test(t))
      return "warn";
    if (/copying…|parsing|embedding|unpacking|extracting|pulling|restart requested/.test(t))
      return "progress";
    return "";
  }

  function pushFeed(path: string, state: string): string {
    const ts = Date.now();
    const id = `${path}:${ts}:${Math.random().toString(36).slice(2, 6)}`;
    // Append at the bottom and cap history, like a standard terminal.
    ingestFeed = [...ingestFeed.slice(-299), { id, path, status: state, ts }];
    scheduleScroll();
    return id;
  }

  function updateFeed(id: string, patch: { path?: string; status?: string }) {
    ingestFeed = ingestFeed.map((row) =>
      row.id === id
        ? { ...row, path: patch.path ?? row.path, status: patch.status ?? row.status, ts: Date.now() }
        : row,
    );
    scheduleScroll();
  }

  /** Rewrite the rolling line for `path`, or start a new one if none is open. */
  function logLine(path: string, status: string) {
    const id = currentRow[path];
    if (id) updateFeed(id, { status });
    else currentRow[path] = pushFeed(path, status);
  }

  /** Finalize the rolling line with a terminal status; next event opens a new line. */
  function endLine(path: string, status: string) {
    logLine(path, status);
    delete currentRow[path];
  }

  let scrollPending = false;
  function scheduleScroll() {
    if (scrollPending) return;
    scrollPending = true;
    requestAnimationFrame(() => {
      scrollPending = false;
      if (termEl) termEl.scrollTop = termEl.scrollHeight;
    });
  }

  function fileName(p: string): string {
    if (!p) return "";
    const idx = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
    return idx >= 0 ? p.slice(idx + 1) : p;
  }

  /**
   * Classify a log row by file/source type. Returns a short TTY-friendly
   * badge label and a CSS class. Non-file pseudo-paths (sidecar, vision,
   * drop, watcher) get their own slot so they don't read as "untyped".
   */
  function fileKind(path: string, status: string): { label: string; cls: string } {
    const p = (path || "").toLowerCase();
    const name = fileName(p);
    if (!name || /^(drop|sidecar|vision|watcher|settings|identity|database)$/.test(name)) {
      if (name === "sidecar")  return { label: "SYS", cls: "sys" };
      if (name === "vision")   return { label: "VIS", cls: "vis" };
      if (name === "watcher")  return { label: "WCH", cls: "sys" };
      if (name === "settings") return { label: "SET", cls: "sys" };
      if (name === "identity") return { label: "ID", cls: "sys" };
      if (name === "database") return { label: "DB", cls: "sys" };
      return { label: "···", cls: "dflt" };
    }
    // Folder drop results come back as the folder path.
    if (/folder\s+done/i.test(status) || !/\.[a-z0-9]+$/i.test(name)) {
      return { label: "DIR", cls: "dir" };
    }
    const ext = name.slice(name.lastIndexOf(".") + 1);
    const table: Record<string, { label: string; cls: string }> = {
      pdf:  { label: "PDF", cls: "pdf" },
      md:   { label: "MD",  cls: "note" },
      txt:  { label: "TXT", cls: "note" },
      rst:  { label: "TXT", cls: "note" },
      json: { label: "JSON",cls: "data" },
      yaml: { label: "YML", cls: "data" },
      yml:  { label: "YML", cls: "data" },
      toml: { label: "TOML",cls: "data" },
      csv:  { label: "CSV", cls: "data" },
      tsv:  { label: "TSV", cls: "data" },
      html: { label: "HTML",cls: "web"  },
      htm:  { label: "HTML",cls: "web"  },
      py:   { label: "PY",  cls: "code" },
      ts:   { label: "TS",  cls: "code" },
      tsx:  { label: "TSX", cls: "code" },
      js:   { label: "JS",  cls: "code" },
      jsx:  { label: "JSX", cls: "code" },
      rs:   { label: "RS",  cls: "code" },
      go:   { label: "GO",  cls: "code" },
      rb:   { label: "RB",  cls: "code" },
      java: { label: "JAVA",cls: "code" },
      c:    { label: "C",   cls: "code" },
      cpp:  { label: "C++", cls: "code" },
      h:    { label: "H",   cls: "code" },
      sh:   { label: "SH",  cls: "code" },
      png:  { label: "IMG", cls: "img"  },
      jpg:  { label: "IMG", cls: "img"  },
      jpeg: { label: "IMG", cls: "img"  },
      gif:  { label: "IMG", cls: "img"  },
      webp: { label: "IMG", cls: "img"  },
      heic: { label: "IMG", cls: "img"  },
      svg:  { label: "SVG", cls: "img"  },
      mp3:  { label: "AUD", cls: "aud"  },
      wav:  { label: "AUD", cls: "aud"  },
      m4a:  { label: "AUD", cls: "aud"  },
      flac: { label: "AUD", cls: "aud"  },
      ogg:  { label: "AUD", cls: "aud"  },
      mp4:  { label: "VID", cls: "vid"  },
      mov:  { label: "VID", cls: "vid"  },
      webm: { label: "VID", cls: "vid"  },
      mkv:  { label: "VID", cls: "vid"  },
      zip:  { label: "ZIP", cls: "arc"  },
      tar:  { label: "TAR", cls: "arc"  },
      gz:   { label: "GZ",  cls: "arc"  },
      tgz:  { label: "TGZ", cls: "arc"  },
    };
    return table[ext] ?? { label: ext.slice(0, 4).toUpperCase() || "FILE", cls: "dflt" };
  }

  /** Spinner tick for in-flight rows; driven by a single interval. */
  let spinnerTick = $state(0);
  const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  function statusGlyph(cls: string): string {
    switch (cls) {
      case "ok":       return "✓";
      case "err":      return "✗";
      case "warn":     return "!";
      case "saved":    return "=";
      case "progress": return SPINNER_FRAMES[spinnerTick % SPINNER_FRAMES.length];
      default:         return "·";
    }
  }

  function fmtClock(ts: number): string {
    const d = new Date(ts);
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  async function handleDropped(paths: string[]) {
    if (!paths.length) return;
    // Show one "copying…" row per drop, keyed by source path so we can
    // replace it in place when the Rust copy returns.
    const rowBySource: Record<string, string> = {};
    for (const p of paths) rowBySource[p] = pushFeed(p, "copying…");
    try {
      const res = await copyIntoInbox(paths);
      for (const d of res.drops) {
        const id = rowBySource[d.source];
        const landed = d.dest ?? d.source;
        let status: string;
        if (d.kind === "missing") status = "missing (no such path)";
        else if (d.kind === "unsupported") status = "skipped (not a file or folder)";
        else if (d.kind === "duplicate") {
          status = `duplicate · already in inbox (${prettyBytes(d.bytes)})`;
          if (id) updateFeed(id, { path: landed, status: `${status} · re-indexing…` });
          else pushFeed(landed, `${status} · re-indexing…`);
          if (d.dest) {
            try {
              await ingestPath(d.dest, false);
            } catch (e) {
              const msg = `re-ingest failed: ${(e as Error).message}`;
              if (id) updateFeed(id, { status: `${status} · ${msg}` });
              else pushFeed(landed, msg);
            }
          }
          for (const err of d.errors ?? []) pushFeed(d.source, `error: ${err}`);
          continue;
        } else if (d.copied === 0) status = "empty (nothing to index)";
        else if (d.kind === "directory") {
          const extra = d.skipped_dirs ? `, pruned ${d.skipped_dirs} dirs` : "";
          status = `copied ${d.copied} files · ${prettyBytes(d.bytes)}${extra}`;
        } else {
          status = `copied · ${prettyBytes(d.bytes)}`;
        }
        if (id) updateFeed(id, { path: landed, status });
        else pushFeed(landed, status);
        for (const err of d.errors ?? []) pushFeed(d.source, `error: ${err}`);
      }
    } catch (e) {
      const msg = `error: ${(e as Error).message}`;
      const first = paths[0];
      const id = first ? rowBySource[first] : undefined;
      if (id) updateFeed(id, { status: msg });
      else pushFeed(first ?? "drop", msg);
    }
    // The watcher will pick them up; the WS stream will emit source_updated.
  }

  async function browseForFiles() {
    const picked = await openDialog({ multiple: true, directory: false });
    if (!picked) return;
    const arr = Array.isArray(picked) ? picked : [picked];
    await handleDropped(arr);
  }

  async function browseForFolder() {
    const picked = await openDialog({ multiple: false, directory: true });
    if (!picked) return;
    await handleDropped([picked as string]);
  }

  async function removeSource(src: Source) {
    if (!confirm(`Forget "${src.path}"?\n(This removes it from Minion; your original file is untouched.)`)) return;
    await deleteSource({ source_id: src.source_id });
    await refreshSources();
  }

  onMount(() => {
    let unlistens: Array<() => void> = [];

    (async () => {
      // Default: show a bootstrap overlay until we learn the sidecar is ready.
      sidecar = { state: "starting", message: "Starting Minion…" };
      bootstrapLog = [];
      const pushBoot = (line: string) => {
        const t = new Date().toLocaleTimeString(undefined, { hour12: false });
        bootstrapLog = [...bootstrapLog.slice(-39), `[${t}] ${line}`];
      };
      pushBoot("Listening for setup progress…");

      // Subscribe *before* any HTTP to the sidecar. Otherwise `fetch(/status)`
      // can block until the browser gives up while pip runs, and we'd miss
      // every `sidecar://status` line and stay frozen on "Starting Minion…".
      const unlistenSidecar = await onSidecarStatus((s) => {
        sidecar = s;
        pushBoot(s.message ? `${s.state}: ${s.message}` : s.state);
      });
      unlistens.push(unlistenSidecar);

      config = await getConfig();
      if (config.sidecar_bootstrapped && config.sidecar_running) {
        sidecar = { state: "ready" };
      }
      try {
        const sig =
          typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
            ? AbortSignal.timeout(20_000)
            : undefined;
        const [st, sr] = await Promise.all([
          sig ? fetchStatus({ signal: sig }) : fetchStatus(),
          sig ? fetchSources({ limit: 500 }, { signal: sig }) : fetchSources({ limit: 500 }),
        ]);
        status = st;
        sources = sr.sources;
      } catch {
        pushBoot("Sidecar HTTP not ready yet (normal during first launch). Will load when the indexer is up.");
      }

      // Hydrate active snapshot from /status (in case we started mid-run).
      if (status?.active) active = status.active;

      startHeartbeatWatchdog();
      const closeWs = await openEvents(
        async (msg) => {
        lastHeartbeat = Date.now();
        // If we're receiving messages the socket is obviously open, even
        // if the onopen hook was missed (race during sidecar restart).
        if (conn !== "open") conn = "open";
        if (msg.type === "snapshot" || msg.type === "ready") {
          // Re-fetch /status so counts and data_dir/db_path stay one snapshot (avoids
          // merged WS counts against stale paths after port/sidecar changes).
          await refreshStatus();
          if (msg.active) active = msg.active;
        } else if (msg.type === "heartbeat") {
          if (status) status = { ...status, counts: msg.counts };
          if (msg.active) active = msg.active;
        } else if (msg.type === "ingest_started") {
          if (msg.active) active = msg.active;
          if (msg.path) logLine(msg.path, `[${msg.active?.done ?? 0}/${msg.active?.total ?? "?"}] parsing…`);
        } else if (msg.type === "ingest_progress") {
          logLine(msg.path, `[${msg.index}/${msg.total}] parsing…`);
        } else if (msg.type === "file_progress") {
          const stage = msg.stage as string;
          const done = Number(msg.done ?? 0);
          const total = Number(msg.total ?? 0);
          if (stage === "unpack_start") {
            logLine(msg.path, "unpacking…");
          } else if (stage === "unpack_progress") {
            const pct = total ? Math.floor((done / total) * 100) : 0;
            logLine(msg.path, `unpacking ${done}/${total} (${pct}%)`);
          } else if (stage === "unpack_done") {
            logLine(msg.path, `unpacked ${msg.extracted ?? 0} file(s)${msg.skipped ? ` (${msg.skipped} skipped)` : ""}`);
          } else if (stage === "extract_start") {
            logLine(msg.path, "extracting…");
          } else if (stage === "extract_done") {
            logLine(msg.path, `extracted ${msg.files ?? 0} manifest(s)`);
          } else if (stage === "load_done") {
            logLine(msg.path, `loaded ${msg.conversations ?? 0} conversations`);
          } else if (stage === "parse_progress") {
            const cd = Number(msg.conversations_done ?? 0);
            const ct = Number(msg.conversations_total ?? 0);
            const pct = ct ? Math.floor((cd / ct) * 100) : 0;
            logLine(msg.path, `parsing ${cd}/${ct} (${pct}%) · ${msg.chunks ?? 0} chunks`);
          } else if (stage === "parsed") {
            logLine(msg.path, `parsed · ${msg.chunks ?? 0} chunks`);
          } else if (stage === "embed") {
            const pct = total ? Math.floor((done / total) * 100) : 0;
            logLine(msg.path, `embedding ${done}/${total} (${pct}%)`);
          }
        } else if (msg.type === "source_updated") {
          if (msg.active) active = msg.active;
          const r = msg.result as { path?: string; chunk_count?: number };
          if (r.path) endLine(r.path, `ingested · ${r.chunk_count ?? 0} chunks`);
          await refreshSources();
          await refreshStatus();
        } else if (msg.type === "ingest_skipped") {
          if (msg.active) active = msg.active;
          const r = msg.result as { path?: string; reason?: string };
          if (r.path) endLine(r.path, `skipped (${r.reason ?? ""})`);
        } else if (msg.type === "ingest_failed") {
          if (msg.active) active = msg.active;
          endLine(msg.path, "failed");
        } else if (msg.type === "source_removed") {
          await refreshSources();
          await refreshStatus();
        } else if (msg.type === "tree_done") {
          active = { root: null, total: 0, done: 0, added: 0, skipped: 0 };
          pushFeed(msg.root, `done · +${msg.added} ${msg.skipped ? `· ⊘${msg.skipped}` : ""}`);
          await refreshSources();
          await refreshStatus();
        } else if (msg.type === "db_error") {
          pushFeed("database", msg.message);
          await refreshStatus();
        }
        },
        (s) => {
          conn = s;
          if (s === "open") lastHeartbeat = Date.now();
        },
      );
      unlistens.push(closeWs);
      unlistens.push(() => {
        if (heartbeatTimer) clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      });

      // Tauri v2 native drag-drop events.
      const unlistenDrop = await listen<{ paths: string[] }>("tauri://drag-drop", async (e) => {
        dragging = false;
        const paths = (e.payload as any)?.paths ?? [];
        await handleDropped(paths);
      });
      const unlistenEnter = await listen("tauri://drag-enter", () => (dragging = true));
      const unlistenLeave = await listen("tauri://drag-leave", () => (dragging = false));

      // Vision (llava/ollama) lifecycle — emitted by Rust. We dump every line
      // into the same terminal log so there is no separate UI surface.
      const unlistenVision = await listen<{ stage: string; line: string }>(
        "vision://progress",
        (e) => {
          const { stage, line } = (e.payload ?? {}) as { stage?: string; line?: string };
          if (!line) return;
          if (stage === "ready") endLine("vision", line);
          else logLine("vision", line);
        },
      );
      unlistens.push(() => unlistenDrop(), () => unlistenEnter(), () => unlistenLeave(), () => unlistenVision());
    })();

    // Braille spinner tick for in-flight rows. 10 fps is smooth without thrash.
    const spin = setInterval(() => { spinnerTick = (spinnerTick + 1) % 10_000; }, 100);

    return () => {
      clearInterval(spin);
      unlistens.forEach((fn) => fn());
    };
  });

  // Derived groupings.
  const grouped = $derived(() => {
    const g: Record<string, Source[]> = {};
    for (const s of sources) {
      (g[s.kind] ||= []).push(s);
    }
    return g;
  });
  const kinds = $derived(() => Object.keys(grouped()).sort());

  /** True when HTTP /status is served by a different data_dir than the Tauri shell (wrong listener). */
  const dataDirMismatch = $derived(() => {
    if (!status?.data_dir || !config?.data_dir) return false;
    const sn = status.data_dir.replace(/\/+$/, "");
    const cn = config.data_dir.replace(/\/+$/, "");
    return sn !== cn;
  });
</script>

<svelte:head>
  <title>Minion</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous" />
  <link
    rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&family=DM+Serif+Display:ital@0;1&family=Inter:wght@400;500;600&family=Nanum+Myeongjo:wght@400;700&display=swap"
  />
</svelte:head>

<main class="app" class:dragging>
  <header class="app-header-min">
    <div class="brand">
      <img src="/minion.png" alt="" class="brand-icon" />
      <h1>Minion</h1>
    </div>
    <button type="button" class="btn-settings-main" onclick={() => void openSettings()} title="Status, library, Claude, and preferences">
      Settings
    </button>
  </header>

  {#if status?.database?.ok === false}
    <div class="db-error-banner" role="alert">
      <strong>Database error</strong>
      <span class="db-error-msg">{status.database.error ?? "SQLite unavailable"}</span>
      <span class="db-error-hint">Check disk space; avoid iCloud or network-backed folders; set MINION_DATA_DIR to a local path; quit Minion and remove stale memory.db-wal / memory.db-shm if needed.</span>
    </div>
  {/if}

  {#if sidecar && sidecar.state !== "ready"}
    <div class="bootstrap-overlay" class:error={sidecar.state === "error"}>
      <div class="bootstrap-card">
        <img src="/minion.png" alt="" class="bootstrap-icon" />
        <div class="bootstrap-title">
          {#if sidecar.state === "error"}
            Minion can't start
          {:else if sidecar.state === "installing"}
            Setting up Minion
          {:else if sidecar.state === "bootstrapping"}
            Creating environment
          {:else}
            Starting
          {/if}
        </div>
        {#if sidecar.state !== "error"}
          <p class="bootstrap-tagline">Preparing the local indexer (first launch only).</p>
        {/if}
        <div class="bootstrap-msg">
          {sidecar.message ?? "Working…"}
        </div>
        {#if bootstrapLog.length}
          <pre class="bootstrap-log" aria-label="Setup log">{bootstrapLog.join("\n")}</pre>
        {/if}
        {#if sidecar.state !== "error"}
          <div class="bootstrap-spinner"></div>
          <div class="bootstrap-hint">First launch only — this won't happen again.</div>
        {:else}
          <button class="ghost" onclick={() => (sidecar = null)}>Dismiss</button>
        {/if}
      </div>
    </div>
  {/if}

  <section class="drop" class:active={dragging} role="button" tabindex="0" onclick={browseForFiles} onkeydown={(e) => e.key === "Enter" && browseForFiles()}>
    <img src="/minion.png" alt="" class="drop-watcher" aria-hidden="true" />
    <div class="drop-inner">
      <div class="drop-title">Drop files or folders</div>
      <div class="drop-hint">ChatGPT export (zip or folder) · PDFs · notes · media · code</div>
      <div class="drop-actions">
        <button class="linklike" onclick={(e) => { e.stopPropagation(); browseForFiles(); }}>Choose files…</button>
        <span class="drop-actions-sep">·</span>
        <button class="linklike" onclick={(e) => { e.stopPropagation(); browseForFolder(); }}>Choose folder…</button>
      </div>
    </div>
  </section>

  <section class="term" aria-label="Activity log">
    <div class="activity-head">
      <span class="activity-title">Activity</span>
      <span class="activity-count">{ingestFeed.length === 0 ? "No events yet" : `${ingestFeed.length} event${ingestFeed.length === 1 ? "" : "s"}`}</span>
    </div>
    <ul class="term-log" bind:this={termEl}>
      {#if ingestFeed.length === 0}
        <li class="term-empty">Ingest and watcher messages show up here.</li>
      {/if}
      {#each ingestFeed as item (item.id)}
        {@const cls = statusClass(item.status)}
        {@const kind = fileKind(item.path, item.status)}
        <li
          title={item.path + "\n" + item.status}
          class="{cls} row"
        >
          <span class="term-ts">{fmtClock(item.ts)}</span>
          <span class="term-sep">│</span>
          <span class="term-glyph" class:spin={cls === "progress"}>{statusGlyph(cls)}</span>
          <span class="kind kind-{kind.cls}">{kind.label}</span>
          <span class="term-line">
            <span class="fn">{fileName(item.path)}</span>
            <span class="msg">{item.status}</span>
          </span>
        </li>
      {/each}
    </ul>
  </section>
</main>

{#if showSettings}
  <div
    class="modal-overlay settings-overlay"
    role="button"
    tabindex="-1"
    onclick={closeSettingsHub}
    onkeydown={(e) => e.key === "Escape" && closeSettingsHub()}
  >
    <div
      class="modal settings-hub"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-label="Minion preferences"
      onclick={(e) => e.stopPropagation()}
      onkeydown={(e) => e.stopPropagation()}
    >
      <aside class="settings-sidebar" aria-label="Sections">
        <div class="settings-sidebar-head">
          <img src="/minion.png" alt="" class="modal-avatar" aria-hidden="true" />
          <div>
            <div class="settings-sidebar-title">Minion</div>
            <div class="settings-sidebar-sub">Preferences</div>
          </div>
        </div>
        <nav class="settings-nav-list">
          <button type="button" class="settings-nav-item" class:active={settingsNav === "status"} onclick={() => void selectSettingsNav("status")}>Status</button>
          <button type="button" class="settings-nav-item" class:active={settingsNav === "library"} onclick={() => void selectSettingsNav("library")}>Library &amp; search</button>
          <button type="button" class="settings-nav-item" class:active={settingsNav === "identity"} onclick={() => void selectSettingsNav("identity")}>Identity</button>
          <button type="button" class="settings-nav-item" class:active={settingsNav === "claude"} onclick={() => void selectSettingsNav("claude")}>Claude (MCP)</button>
          <button type="button" class="settings-nav-item" class:active={settingsNav === "ingest"} onclick={() => void selectSettingsNav("ingest")}>Ingest &amp; types</button>
          <button type="button" class="settings-nav-item settings-nav-item-muted" class:active={settingsNav === "advanced"} onclick={() => void selectSettingsNav("advanced")}>Advanced</button>
        </nav>
      </aside>
      <div class="settings-pane">
        <header class="settings-pane-head">
          <h2 class="settings-pane-h2">{SETTINGS_PANE_TITLE[settingsNav]}</h2>
          <button type="button" class="ghost" onclick={closeSettingsHub}>Close</button>
        </header>
        <div class="settings-pane-body">
          {#if settingsNav === "status"}
            <div class="settings-section settings-section-flush">
              <div class="status-summary-grid">
                <div class="status-card">
                  <div class="status-card-k">Sources</div>
                  <div class="status-card-v">{status?.counts.sources ?? "—"}</div>
                </div>
                <div class="status-card">
                  <div class="status-card-k">Chunks</div>
                  <div class="status-card-v">{status?.counts.chunks ?? "—"}</div>
                </div>
                <div class="status-card">
                  <div class="status-card-k">Inbox watch</div>
                  <div class="status-card-v" class:live={status?.watcher.running}>
                    {#if !status}
                      …
                    {:else if status.watcher.running}
                      {status.watcher.mode === "polling" ? "Polling" : "Live"}
                    {:else}
                      Paused
                    {/if}
                  </div>
                </div>
                <div class="status-card">
                  <div class="status-card-k">Sidecar</div>
                  <div class="status-card-v" class:live={conn === "open"}>
                    {conn === "open" ? "Ready" : conn === "connecting" ? "Starting" : conn === "unreachable" ? "Offline" : "Reconnecting"}
                  </div>
                </div>
              </div>

              {#if dataDirMismatch()}
                <div class="settings-error-box settings-spaced" role="status">
                  <p><strong>Data folder mismatch</strong> — another Minion may be on this port. Restart the sidecar from here.</p>
                  <div class="mismatch-paths"><span class="label">App</span><span class="mono">{config?.data_dir}</span></div>
                  <div class="mismatch-paths"><span class="label">Sidecar</span><span class="mono">{status?.data_dir}</span></div>
                </div>
              {/if}

              <div class="setting-row">
                <div class="setting-main">
                  <div class="setting-label">Restart sidecar</div>
                  <div class="setting-desc">Reloads the Python process, watchers, and HTTP API.</div>
                </div>
                <button type="button" class="ghost" onclick={handleRestart} disabled={restarting}>{restarting ? "restarting…" : "Restart"}</button>
              </div>

              <div class="detail-block">
                <div class="detail-block-title">Paths</div>
                {#if conn === "open" && config?.api_base}
                  <div class="detail-row"><span class="detail-k">API</span><span class="detail-v mono">{config.api_base}</span></div>
                {/if}
                {#if status?.db_path}
                  <div class="detail-row"><span class="detail-k">Database</span><span class="detail-v mono">{status.db_path}</span></div>
                {/if}
                {#if status?.database?.journal_mode}
                  <div class="detail-row">
                    <span class="detail-k">SQLite journal</span><span class="detail-v">{status.database.journal_mode}</span>
                  </div>
                {/if}
                {#if config?.data_dir}
                  <div class="detail-row"><span class="detail-k">Data</span><span class="detail-v mono">{config.data_dir}</span></div>
                {/if}
                {#if status?.watcher?.mode}
                  <div class="detail-row"><span class="detail-k">Watcher mode</span><span class="detail-v">{status.watcher.mode}</span></div>
                {/if}
              </div>
            </div>
          {:else if settingsNav === "library"}
            <div class="settings-section settings-section-flush">
              <div class="modal-search library-search">
                <input
                  placeholder="Search your memory…"
                  bind:value={queryText}
                  onkeydown={(e) => e.key === "Enter" && runSearch()}
                />
                <button type="button" onclick={runSearch} disabled={searching}>{searching ? "…" : "Search"}</button>
              </div>
              {#if searchResults.length}
                <div class="library-results">
                  <div class="section-title small">Results</div>
                  {#each searchResults as hit}
                    <article class="hit">
                      <header>
                        <span class="score">{hit.score.toFixed(3)}</span>
                        <span class="kind kind-{hit.kind}">{KIND_LABELS[hit.kind] ?? hit.kind}</span>
                        <span class="path" title={hit.path}>{hit.path.split("/").pop()}</span>
                      </header>
                      <p>{hit.text}</p>
                    </article>
                  {/each}
                </div>
              {/if}
              <div class="section-title small library-sources-head">
                Indexed sources
                <div class="chips">
                  <button type="button" class:chip-active={!filterKind} onclick={() => { filterKind = ""; refreshSources(); }}>All</button>
                  {#each kinds() as k}
                    <button type="button" class:chip-active={filterKind === k} onclick={() => { filterKind = k; refreshSources(); }}>
                      {KIND_LABELS[k] ?? k} <span class="count">{grouped()[k].length}</span>
                    </button>
                  {/each}
                </div>
              </div>
              {#if sources.length === 0}
                <div class="empty">Nothing indexed yet. Drop files on the main window.</div>
              {:else}
                <ul class="source-list">
                  {#each sources as s}
                    <li>
                      <div class="file-main">
                        <span class="kind kind-{s.kind}">{KIND_LABELS[s.kind] ?? s.kind}</span>
                        <span class="path" title={s.path}>{s.path.split("/").pop()}</span>
                        <span class="meta">{prettyBytes(s.bytes)} · {prettyTime(s.mtime)}</span>
                      </div>
                      <div class="file-actions">
                        <button type="button" class="ghost" onclick={() => revealInFinder(s.path)}>Reveal</button>
                        <button type="button" class="ghost danger" onclick={() => removeSource(s)}>Forget</button>
                      </div>
                    </li>
                  {/each}
                </ul>
              {/if}
            </div>
          {:else if settingsNav === "identity"}
            <div class="settings-section settings-section-flush">
              <div class="chips identity-toolbar">
                <button type="button" class:chip-active={identityTab === "proposed"} onclick={() => switchIdentityTab("proposed")}>Proposed</button>
                <button type="button" class:chip-active={identityTab === "active"} onclick={() => switchIdentityTab("active")}>Active</button>
                <button type="button" class="ghost" onclick={() => refreshIdentity()} disabled={identityLoading}>{identityLoading ? "…" : "Refresh"}</button>
                <button type="button" class="ghost" onclick={runClusterRebuild} disabled={clusterBusy}>{clusterBusy ? "Clustering…" : "Rebuild clusters"}</button>
                <button type="button" class="ghost" onclick={runIdentityExport} disabled={exportBusy}>{exportBusy ? "Export…" : "Export zip"}</button>
              </div>
              {#if identityClaims.length === 0}
                <div class="empty">
                  {identityTab === "proposed"
                    ? "No proposed claims. Agents can add them via MCP (`propose_identity_update`)."
                    : "No active claims yet — approve proposals from the Proposed tab."}
                </div>
              {:else}
                <ul class="source-list identity-claim-list">
                  {#each identityClaims as c}
                    <li>
                      <div class="file-main">
                        <span class="kind">{c.kind}</span>
                        <span class="path mono" title={c.claim_id}>{c.claim_id}</span>
                        {#if c.source_agent}
                          <span class="meta">via {c.source_agent}</span>
                        {/if}
                      </div>
                      <p class="claim-text">{c.text}</p>
                      <div class="file-actions">
                        <button type="button" class="ghost" onclick={() => showClaimEvidence(c.claim_id)}>Evidence</button>
                        {#if identityTab === "proposed"}
                          <button type="button" class="ghost" onclick={() => approveClaim(c.claim_id)}>Approve</button>
                          <button type="button" class="ghost danger" onclick={() => rejectClaim(c.claim_id)}>Reject</button>
                        {/if}
                      </div>
                    </li>
                  {/each}
                </ul>
              {/if}
              {#if evidencePopup}
                <div class="evidence-box settings-spaced">
                  <div class="section-title small">Evidence preview</div>
                  <div class="meta mono">{evidencePopup.path}</div>
                  <pre class="evidence-pre">{evidencePopup.text}</pre>
                  <button type="button" class="ghost" onclick={() => (evidencePopup = null)}>Dismiss</button>
                </div>
              {/if}
            </div>
          {:else if settingsNav === "claude"}
            <div class="settings-section settings-section-flush">
              <div class="setting-row setting-row-stack">
                <div class="setting-main">
                  <div class="setting-label">Claude Desktop</div>
                  <div class="setting-desc">Writes Minion into your Claude Desktop MCP config. Fully quit and reopen Claude after it succeeds.</div>
                  {#if connectMsg}
                    <div class="setting-callout" class:setting-callout-warn={connectMsg.startsWith("Couldn’t")}>
                      {connectMsg}
                    </div>
                  {/if}
                </div>
                <button type="button" class="ghost" onclick={() => void runConnect()} disabled={connecting}>{connecting ? "…" : "Add to Claude"}</button>
              </div>
            </div>
          {:else if settingsNav === "ingest"}
            <div class="settings-section settings-section-flush">
              <div class="setting-row">
                <div class="setting-main">
                  <div class="setting-label">Data folder</div>
                  <div class="setting-desc">Indexed files are copied or linked from your inbox under this account’s data directory.</div>
                  {#if config?.data_dir}
                    <div class="path-one-line mono" title={config.data_dir}>{config.data_dir}</div>
                  {/if}
                </div>
                <button type="button" class="ghost" onclick={revealDataFolder} disabled={!config?.data_dir}>Reveal</button>
              </div>
              <div class="setting-row">
                <div class="setting-main">
                  <div class="setting-label">Rescan inbox</div>
                  <div class="setting-desc">
                    Sync everything on disk into the database.
                    <span class="setting-tip" title="Re-parses and re-embeds every file.">Re-index all</span> is the heavy option.
                  </div>
                </div>
                <div class="setting-actions">
                  <button type="button" class="ghost" onclick={() => void runRescanInbox(false)} disabled={rescanning || conn !== "open"}>{rescanning ? "…" : "Rescan"}</button>
                  <button type="button" class="ghost" onclick={() => void runRescanInbox(true)} disabled={rescanning || conn !== "open"}>Re-index all</button>
                </div>
              </div>
              <div class="section-title small ingest-types-head">
                File types
                <span class="section-hint">{savingSettings ? "saving…" : "what Minion ingests"}</span>
              </div>
              {#if !settingsLoaded && settingsError}
                <div class="settings-error-box">
                  <p>Couldn’t load file-type preferences.</p>
                  <p class="settings-error-detail">{settingsError}</p>
                  <button type="button" class="ghost" onclick={loadSettings}>Retry</button>
                </div>
              {:else if !settingsLoaded}
                <div class="empty">Loading…</div>
              {:else}
                <ul class="kind-list">
                  {#each allKinds as k}
                    {@const enabled = !disabledKinds.has(k)}
                    <li class="kind-row" class:kind-off={!enabled}>
                      <label class="kind-toggle">
                        <input type="checkbox" checked={enabled} onchange={() => toggleKind(k)} disabled={savingSettings} />
                        <span class="kind-name">
                          <span class="kind kind-{k === 'chatgpt-export' ? 'chatgpt-export' : k}">{(KIND_LABELS[k] ?? k).toLowerCase()}</span>
                        </span>
                        <span class="kind-desc">{KIND_DESCRIPTIONS[k] ?? ""}</span>
                      </label>
                    </li>
                  {/each}
                </ul>
                <div class="settings-note">
                  Disabled kinds are skipped on ingest. Turn one back on and restart the sidecar to pick up those files.
                </div>
              {/if}
            </div>
          {:else if settingsNav === "advanced"}
            <div class="settings-advanced-card">
              <div class="section-title small">Danger zone</div>
              <div class="setting-row">
                <div class="setting-main">
                  <div class="setting-label">Nuke local database</div>
                  <div class="setting-desc">Deletes <span class="mono">memory.db</span> and telemetry. Indexed content is lost.</div>
                </div>
                <button type="button" class="ghost danger" onclick={runNukeDb}>Nuke DB</button>
              </div>
              <div class="setting-row">
                <div class="setting-main">
                  <div class="setting-label">Factory reset</div>
                  <div class="setting-desc">Clears the database <em>and</em> the inbox.</div>
                </div>
                <button type="button" class="ghost danger" onclick={runFactoryReset}>Factory reset</button>
              </div>
            </div>
          {/if}
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  :global(:root) {
    /* Brand system: Minion's palette — soft sky-blue fluff, deep navy core,
     * cool paper ground. Keeps the "quiet friend" feel from before, just
     * wearing the Minion's own fur instead of borrowed teal. */
    --bg:            #f3f6fb;  /* cool cream, faint blue undertone */
    --panel:         #ffffff;
    --panel-2:       #eaf2fa;
    --panel-3:       #dbe7f2;
    --border:        #d5e0ec;
    --border-strong: #a9bdd0;
    --ink:           #102238;  /* deep navy — Minion's core */
    --ink-2:         #1d3854;
    --heading:       #0a1628;  /* pupil black */
    --text: var(--ink);
    --muted:         #6f819a;
    --ink-dim:       #4a5d73;
    --dim:           #a6b5c5;
    --accent:        #2a7fbf;  /* mid fluff blue — primary */
    --accent-2:      #1d6ab0;  /* deep fluff — hover/focus */
    --accent-soft:   #d3e5f4;
    --glow:          #5ca8d6;  /* fluff highlight — halo/pulse */
    --success:       #15886e;
    --saved:         #6c5bd6;
    --progress:      #2a7fbf;
    --warn:          #c27a17;
    --danger:        #c8352c;
    --ok: var(--success);
    --radius-sm: 6px;
    --radius:    10px;
    --radius-lg: 14px;
    --shadow-s: 0 1px 2px rgba(16, 34, 56, 0.05), 0 2px 6px rgba(16, 34, 56, 0.04);
    --shadow-m: 0 2px 10px rgba(16, 34, 56, 0.07), 0 18px 48px -16px rgba(16, 34, 56, 0.16);
    --shadow-glow: 0 0 0 0.5px color-mix(in srgb, var(--accent) 30%, transparent),
                   0 6px 24px -8px color-mix(in srgb, var(--accent) 45%, transparent);
    --ui-font:      "DM Sans", "Inter", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
    --display-font: "DM Serif Display", "Nanum Myeongjo", Georgia, serif;
    --serif-font:   "Nanum Myeongjo", "DM Serif Display", Georgia, serif;
    --num-font:     "Inter", "DM Sans", system-ui, sans-serif;
    --mono-font:    "JetBrains Mono", "SF Mono", ui-monospace, Menlo, Consolas, monospace;
    font-family: var(--ui-font);
    background: var(--bg);
    color: var(--text);
    font-feature-settings: "ss01", "cv11";
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }
  :global(body) { margin: 0; background: var(--bg); color: var(--ink); }
  :global(*) { box-sizing: border-box; }
  :global(::selection) { background: var(--accent); color: #fff; }

  /* A slow, almost-imperceptible warm wash on the whole app — the "presence"
   * cue that this thing is listening. Pure CSS, zero layout cost. */
  .app::before {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 0;
    background:
      radial-gradient(55% 40% at 12% 0%, color-mix(in srgb, var(--accent) 10%, transparent), transparent 70%),
      radial-gradient(45% 35% at 92% 100%, color-mix(in srgb, var(--accent-2) 8%, transparent), transparent 75%);
    animation: breath 12s ease-in-out infinite alternate;
  }
  @keyframes breath {
    from { opacity: 0.65; }
    to   { opacity: 1; }
  }
  .app > * { position: relative; z-index: 1; }

  .app {
    height: 100vh;
    display: grid;
    grid-template-rows: auto auto 1fr;
    gap: 16px;
    padding: 20px 28px 24px;
    max-width: 820px;
    margin: 0 auto;
    font-size: 13px;
    color: var(--ink);
  }

  .db-error-banner {
    margin: 0 1rem 0.75rem;
    padding: 0.65rem 0.85rem;
    border-radius: 10px;
    background: rgba(180, 40, 40, 0.14);
    border: 1px solid rgba(220, 80, 80, 0.45);
    color: var(--text, #e8e4dc);
    font-size: 0.82rem;
    line-height: 1.45;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .db-error-banner strong {
    font-weight: 600;
    letter-spacing: 0.02em;
  }
  .db-error-msg {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.78rem;
    opacity: 0.95;
    word-break: break-word;
  }
  .db-error-hint {
    font-size: 0.76rem;
    opacity: 0.85;
  }

  header.app-header-min {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .btn-settings-main {
    background: var(--accent);
    color: #fff;
    border: 1px solid var(--accent);
    padding: 8px 18px;
    font-size: 13px;
    font-weight: 600;
    border-radius: 999px;
    box-shadow: var(--shadow-s);
  }
  .btn-settings-main:hover {
    background: var(--accent-2);
    border-color: var(--accent-2);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand h1 {
    margin: 0;
    font-family: var(--display-font);
    font-size: 22px;
    font-weight: 400;
    letter-spacing: -0.01em;
    color: var(--heading);
    line-height: 1;
  }
  /* The Minion: a fuzzy blue observer. Wears a soft halo that breathes so
   * you can feel it watching even when idle. */
  .brand-icon {
    width: 34px;
    height: 34px;
    object-fit: contain;
    flex-shrink: 0;
    filter: drop-shadow(0 1px 2px rgba(16, 34, 56, 0.18))
            drop-shadow(0 0 6px color-mix(in srgb, var(--accent) 28%, transparent));
    animation: presence 3.6s ease-in-out infinite;
  }
  /* Aura hugs the Minion's silhouette, not a bounding box — so it works with
   * the transparent-bg cut-out. Gently breathes between a soft and strong halo. */
  @keyframes presence {
    0%, 100% {
      filter: drop-shadow(0 1px 2px rgba(16, 34, 56, 0.18))
              drop-shadow(0 0 6px color-mix(in srgb, var(--accent) 28%, transparent));
    }
    50% {
      filter: drop-shadow(0 2px 4px rgba(16, 34, 56, 0.22))
              drop-shadow(0 0 12px color-mix(in srgb, var(--accent) 50%, transparent));
    }
  }
  .toast {
    background: color-mix(in srgb, var(--accent) 8%, var(--panel));
    border: 1px solid color-mix(in srgb, var(--accent) 25%, var(--border));
    border-left: 3px solid var(--accent);
    color: var(--ink);
    padding: 10px 14px;
    font-family: var(--ui-font);
    font-size: 12.5px;
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-s);
  }
  .toast-warn {
    border-left-color: #c45c26;
    background: color-mix(in srgb, #c45c26 10%, var(--panel));
  }
  .mismatch-paths {
    display: grid;
    grid-template-columns: 5rem 1fr;
    gap: 4px 10px;
    margin-top: 8px;
    font-size: 11px;
    line-height: 1.35;
    color: var(--muted);
  }
  .mismatch-paths .label {
    color: var(--dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 10px;
  }

  .bootstrap-overlay {
    position: fixed;
    inset: 0;
    background: color-mix(in srgb, var(--panel) 92%, transparent);
    backdrop-filter: blur(6px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 50;
  }
  .bootstrap-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 28px 32px;
    max-width: 440px;
    text-align: center;
    box-shadow: var(--shadow-m);
  }
  .bootstrap-icon {
    width: 96px;
    height: 96px;
    object-fit: contain;
    margin: 0 auto 14px;
    display: block;
    filter: drop-shadow(0 4px 10px rgba(16, 34, 56, 0.18))
            drop-shadow(0 0 14px color-mix(in srgb, var(--accent) 35%, transparent));
    animation: presence 3.6s ease-in-out infinite;
  }
  .bootstrap-title {
    font-family: var(--ui-font);
    font-size: 16px;
    font-weight: 600;
    color: var(--ink);
    margin-bottom: 8px;
  }
  .bootstrap-tagline {
    margin: 0 0 12px;
    font-family: var(--ui-font);
    font-size: 13px;
    line-height: 1.45;
    color: var(--muted);
  }
  .bootstrap-msg {
    font-family: var(--ui-font);
    font-size: 13px;
    color: var(--ink-dim);
    margin-bottom: 12px;
    white-space: pre-wrap;
  }
  .bootstrap-log {
    width: 100%;
    max-height: 140px;
    overflow-y: auto;
    margin: 0 0 16px;
    padding: 10px 12px;
    box-sizing: border-box;
    text-align: left;
    font-family: var(--num-font);
    font-size: 10.5px;
    line-height: 1.45;
    color: var(--ink-dim);
    background: color-mix(in srgb, var(--panel) 92%, var(--border));
    border: 1px solid var(--border);
    border-radius: 10px;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .bootstrap-spinner {
    width: 28px;
    height: 28px;
    margin: 0 auto 14px;
    border: 3px solid color-mix(in srgb, var(--accent) 20%, transparent);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 900ms linear infinite;
  }
  .bootstrap-hint {
    font-family: var(--ui-font);
    font-size: 11.5px;
    color: var(--ink-faint, var(--ink-dim));
    opacity: 0.75;
  }
  .bootstrap-overlay.error .bootstrap-card {
    border-color: color-mix(in srgb, var(--danger) 40%, var(--border));
  }
  .bootstrap-overlay.error .bootstrap-title {
    color: var(--danger);
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  /* Buttons: warm, soft, friendly. Teal primary, ghost for secondary. */
  button {
    background: var(--accent);
    color: #fff;
    border: 1px solid var(--accent);
    padding: 7px 14px;
    font-family: var(--ui-font);
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    border-radius: var(--radius-sm);
    transition: background 160ms ease, color 160ms ease, border-color 160ms ease,
                box-shadow 160ms ease, transform 80ms ease;
    box-shadow: var(--shadow-s);
  }
  button:hover {
    background: var(--accent-2);
    border-color: var(--accent-2);
    box-shadow: var(--shadow-glow);
  }
  button:active { transform: translateY(1px); }
  button:disabled { opacity: 0.45; cursor: not-allowed; box-shadow: none; }
  button.ghost {
    background: transparent;
    color: var(--ink);
    border: 1px solid var(--border-strong);
    padding: 5px 12px;
    font-size: 11.5px;
    font-weight: 500;
    letter-spacing: 0.01em;
    box-shadow: none;
  }
  button.ghost:hover {
    border-color: var(--accent);
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 6%, transparent);
  }
  button.ghost.danger:hover {
    border-color: var(--danger);
    color: var(--danger);
    background: color-mix(in srgb, var(--danger) 6%, transparent);
  }
  button.linklike {
    background: transparent;
    color: var(--accent);
    border: none;
    border-bottom: 1px dotted color-mix(in srgb, var(--accent) 50%, transparent);
    padding: 0;
    font-family: var(--ui-font);
    font-size: inherit;
    cursor: pointer;
    box-shadow: none;
    border-radius: 0;
  }
  button.linklike:hover { border-bottom-color: var(--accent); background: transparent; }

  /* Drop zone: a warm welcoming surface with a soft breathing halo.
   * This is where the user "greets" Minion — it should feel alive. */
  .drop {
    position: relative;
    border: 1px solid var(--border-strong);
    background: var(--panel);
    padding: 42px 24px;
    cursor: pointer;
    transition: background 200ms ease, border-color 200ms ease, box-shadow 240ms ease;
    outline: none;
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-s);
    overflow: hidden;
  }
  .drop::after {
    content: "";
    position: absolute;
    inset: -30%;
    background: radial-gradient(closest-side,
      color-mix(in srgb, var(--accent) 14%, transparent) 0%,
      transparent 70%);
    opacity: 0.55;
    animation: dropglow 6s ease-in-out infinite alternate;
    pointer-events: none;
    z-index: 0;
  }
  @keyframes dropglow {
    from { opacity: 0.35; transform: translate3d(0, 0, 0) scale(1); }
    to   { opacity: 0.70; transform: translate3d(0, -6px, 0) scale(1.03); }
  }
  .drop > * { position: relative; z-index: 1; }
  .drop:hover {
    background: color-mix(in srgb, var(--accent) 4%, var(--panel));
    border-color: color-mix(in srgb, var(--accent) 35%, var(--border-strong));
    box-shadow: var(--shadow-glow);
  }
  .drop.active, .app.dragging .drop {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 10%, var(--panel));
    box-shadow: var(--shadow-glow);
  }
  .drop:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .drop-inner {
    position: relative;
    z-index: 1;
    text-align: center;
    max-width: 420px;
    margin: 0 auto;
    padding: 8px 12px 12px;
  }
  .drop-title {
    font-family: var(--display-font);
    font-size: 1.35rem;
    font-weight: 500;
    letter-spacing: -0.02em;
    color: var(--heading);
    line-height: 1.25;
  }
  .drop-hint {
    margin-top: 8px;
    font-family: var(--ui-font);
    font-size: 12px;
    color: var(--muted);
    line-height: 1.45;
  }
  .drop-actions {
    margin-top: 14px;
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 6px 10px;
    font-family: var(--ui-font);
    font-size: 12.5px;
    color: var(--muted);
  }
  .drop-actions-sep {
    opacity: 0.45;
    user-select: none;
  }

  /* Minion peeking from the drop zone's corner. Low-key when idle, alert
   * when the user drags a file toward him. */
  .drop-watcher {
    position: absolute;
    right: 22px;
    bottom: 8px;
    width: 96px;
    height: 96px;
    object-fit: contain;
    opacity: 0.55;
    transform: rotate(-8deg);
    transition: opacity 240ms ease, transform 240ms ease, filter 240ms ease;
    pointer-events: none;
    user-select: none;
    z-index: 0;
    filter: drop-shadow(0 4px 10px rgba(16, 34, 56, 0.22))
            drop-shadow(0 0 10px color-mix(in srgb, var(--accent) 25%, transparent));
  }
  .drop:hover .drop-watcher {
    opacity: 0.85;
    transform: rotate(-4deg) translateY(-4px);
    filter: drop-shadow(0 6px 14px rgba(16, 34, 56, 0.28))
            drop-shadow(0 0 16px color-mix(in srgb, var(--accent) 40%, transparent));
  }
  .drop.active .drop-watcher,
  .app.dragging .drop-watcher {
    opacity: 1;
    transform: rotate(0deg) translateY(-10px) scale(1.1);
    filter: drop-shadow(0 8px 18px rgba(16, 34, 56, 0.32))
            drop-shadow(0 0 22px color-mix(in srgb, var(--accent) 55%, transparent));
  }

  /* Terminal: a warm scrolling log. One line per event. */
  .term {
    background: var(--panel);
    border: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    border-radius: var(--radius);
    font-family: var(--mono-font);
    box-shadow: var(--shadow-s);
  }
  .term-log {
    list-style: none;
    margin: 0;
    padding: 10px 14px;
    overflow-y: auto;
    font-family: var(--mono-font);
    font-size: 12px;
    line-height: 1.55;
    flex: 1;
    color: var(--ink);
  }
  .term-log li {
    display: flex;
    gap: 8px;
    padding: 0;
  }
  /* Row status paints the glyph + trailing message. The filename + badge keep
   * their own type-colors so you can still scan by file kind. */
  .term-log li.row .msg        { color: var(--ink); }
  .term-log li.err   .msg,
  .term-log li.err   .term-glyph { color: var(--danger); }
  .term-log li.warn  .msg,
  .term-log li.warn  .term-glyph { color: var(--warn); }
  .term-log li.ok    .msg,
  .term-log li.ok    .term-glyph { color: var(--success); }
  .term-log li.saved .msg,
  .term-log li.saved .term-glyph { color: var(--saved); }
  .term-log li.progress .msg,
  .term-log li.progress .term-glyph { color: var(--progress); }

  .term-glyph {
    flex-shrink: 0;
    width: 1ch;
    display: inline-block;
    text-align: center;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .term-glyph.spin { animation: pulse 1.1s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 0.55; } 50% { opacity: 1; } }

  .activity-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    background: color-mix(in srgb, var(--panel-2) 88%, var(--accent));
    font-family: var(--ui-font);
    user-select: none;
  }
  .activity-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--ink);
    letter-spacing: 0.01em;
  }
  .activity-count {
    font-size: 11px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }

  /* Per-kind badge. Uppercase mono pill colored by the type class. */
  /* Kind tag: quiet colored letters with a leading dot. No pill, no
   * background — the badge is the *color*, not a sticker. Keeps 30+ rows
   * scannable without turning the log into a candy aisle. */
  .kind {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-family: var(--num-font);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.06em;
    line-height: 1.4;
    padding: 0;
    border: none;
    background: none;
    text-transform: uppercase;
    min-width: 3.1rem;
  }
  .kind::before {
    content: "";
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
    flex-shrink: 0;
  }
  .kind-pdf  { color: #b45309; }
  .kind-note { color: #15803d; }
  .kind-data { color: #0f766e; }
  .kind-web  { color: #1d4ed8; }
  .kind-code { color: #4338ca; }
  .kind-img  { color: #be185d; }
  .kind-aud  { color: #c2410c; }
  .kind-vid  { color: #b91c1c; }
  .kind-arc  { color: #a16207; }
  .kind-dir  { color: #4f46e5; }
  .kind-sys  { color: #64748b; }
  .kind-vis  { color: #a21caf; }
  .kind-dflt { color: var(--muted); }

  .fn { color: var(--ink); font-weight: 500; margin-right: 10px; }
  .msg { color: var(--muted); }
  .term-empty { color: var(--muted); }
  .term-ts {
    color: var(--dim);
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
  }
  .term-sep {
    color: var(--dim);
    flex-shrink: 0;
  }
  .term-line {
    flex: 1;
    min-width: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  /* Modal: warm paper card with a soft shadow. */
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: color-mix(in srgb, var(--ink) 28%, transparent);
    backdrop-filter: blur(4px);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 24px;
    z-index: 100;
    overflow-y: auto;
  }
  .settings-overlay {
    align-items: center;
  }
  .settings-hub {
    max-width: 1000px;
    width: 100%;
    max-height: calc(100vh - 48px);
    display: flex;
    flex-direction: row;
    padding: 0;
    overflow: hidden;
    border-radius: var(--radius-lg);
  }
  .settings-sidebar {
    width: 208px;
    flex-shrink: 0;
    border-right: 1px solid var(--border);
    background: color-mix(in srgb, var(--panel-2) 55%, var(--panel));
    display: flex;
    flex-direction: column;
  }
  .settings-sidebar-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 12px;
    border-bottom: 1px solid var(--border);
  }
  .settings-sidebar-head .modal-avatar {
    width: 28px;
    height: 28px;
  }
  .settings-sidebar-title {
    font-family: var(--ui-font);
    font-size: 13px;
    font-weight: 600;
    color: var(--ink);
    line-height: 1.2;
  }
  .settings-sidebar-sub {
    font-size: 10.5px;
    color: var(--muted);
    line-height: 1.2;
    margin-top: 1px;
  }
  .settings-nav-list {
    display: flex;
    flex-direction: column;
    padding: 8px;
    gap: 2px;
    flex: 1;
    overflow-y: auto;
  }
  .settings-nav-item {
    display: block;
    width: 100%;
    text-align: left;
    background: transparent;
    color: var(--ink);
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    padding: 8px 10px;
    font-family: var(--ui-font);
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    box-shadow: none;
    transition: background 120ms ease, color 120ms ease, border-color 120ms ease;
  }
  .settings-nav-item:hover {
    background: color-mix(in srgb, var(--accent) 8%, var(--panel));
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 15%, transparent);
  }
  .settings-nav-item.active {
    background: color-mix(in srgb, var(--accent) 14%, var(--panel));
    color: var(--accent-2);
    border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
  }
  .settings-nav-item-muted {
    color: var(--muted);
    font-size: 12px;
  }
  .settings-pane {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    background: var(--panel);
  }
  .settings-pane-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 18px;
    border-bottom: 1px solid var(--border);
    background: color-mix(in srgb, var(--accent) 5%, var(--panel-2));
    flex-shrink: 0;
  }
  .settings-pane-h2 {
    margin: 0;
    font-family: var(--ui-font);
    font-size: 16px;
    font-weight: 600;
    color: var(--heading);
    letter-spacing: -0.01em;
  }
  .settings-pane-body {
    padding: 18px 20px 22px;
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }
  .settings-section-flush {
    padding: 0;
    margin: 0;
    border: none;
    background: transparent;
    box-shadow: none;
  }
  .status-summary-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 18px;
  }
  @media (max-width: 720px) {
    .status-summary-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .settings-hub {
      flex-direction: column;
      max-height: calc(100vh - 32px);
    }
    .settings-sidebar {
      width: 100%;
      border-right: none;
      border-bottom: 1px solid var(--border);
      max-height: 40vh;
    }
    .settings-nav-list {
      flex-direction: row;
      flex-wrap: wrap;
    }
    .settings-nav-item {
      width: auto;
      flex: 1 1 auto;
    }
  }
  .status-card {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 10px 12px;
    background: var(--panel-2);
  }
  .status-card-k {
    font-size: 9.5px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  .status-card-v {
    margin-top: 6px;
    font-size: 1.25rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
    line-height: 1.15;
  }
  .status-card-v.live {
    color: var(--accent);
  }
  .settings-spaced {
    margin-top: 16px;
  }
  .detail-block {
    margin-top: 18px;
    padding: 12px 14px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }
  .detail-block-title {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    margin-bottom: 10px;
  }
  .detail-row {
    display: grid;
    grid-template-columns: 92px 1fr;
    gap: 6px 12px;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    line-height: 1.35;
  }
  .detail-row:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }
  .detail-k {
    color: var(--dim);
    font-weight: 500;
  }
  .detail-v {
    color: var(--ink);
    word-break: break-word;
  }
  .library-search {
    margin-bottom: 4px;
  }
  .library-results {
    margin-bottom: 18px;
  }
  .library-sources-head {
    margin-top: 4px;
  }
  .identity-toolbar {
    margin-bottom: 14px;
  }
  .ingest-types-head {
    margin-top: 6px;
  }
  .modal {
    width: 100%;
    max-width: 860px;
    max-height: calc(100vh - 48px);
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: var(--shadow-m);
  }
  .modal.settings-hub {
    max-width: 1000px;
    flex-direction: row;
    padding: 0;
  }
  .modal-head {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 16px 22px;
    border-bottom: 1px solid var(--border);
    background: color-mix(in srgb, var(--accent) 4%, var(--panel-2));
  }
  .modal-avatar {
    width: 30px;
    height: 30px;
    object-fit: contain;
    flex-shrink: 0;
    filter: drop-shadow(0 1px 2px rgba(16, 34, 56, 0.15))
            drop-shadow(0 0 5px color-mix(in srgb, var(--accent) 22%, transparent));
  }
  .modal-meta {
    flex: 1;
    color: var(--muted);
    font-family: var(--num-font);
    font-size: 12px;
    font-feature-settings: "tnum";
  }
  .modal-search {
    display: flex;
    gap: 8px;
    padding: 14px 22px;
    border-bottom: 1px solid var(--border);
    align-items: stretch;
    background: var(--panel);
  }
  .modal-search input {
    flex: 1;
    padding: 10px 14px;
    background: var(--panel);
    border: 1px solid var(--border-strong);
    color: var(--ink);
    border-radius: var(--radius-sm);
    font-family: var(--ui-font);
    font-size: 13px;
    outline: none;
    transition: border-color 160ms ease, box-shadow 160ms ease;
  }
  .modal-search input::placeholder { color: var(--dim); font-style: italic; }
  .modal-search input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent);
  }
  .modal-search button { border-radius: var(--radius-sm); padding: 8px 16px; }
  .library-search.modal-search {
    padding: 0;
    border-bottom: none;
    background: transparent;
  }

  .modal-section {
    padding: 16px 20px;
    overflow-y: auto;
    min-height: 0; /* let flex children actually shrink */
  }
  .modal-section + .modal-section { border-top: 1px solid var(--border); }
  /* Sections keep their natural height at the top; the *last* one (sources)
   * is the growable + scrollable region. This is what keeps the search bar
   * reachable no matter how long the list gets. */
  .modal > .modal-head,
  .modal > .modal-search     { flex: 0 0 auto; }
  .modal > .modal-section    { flex: 0 1 auto; max-height: 40vh; }
  .modal > .modal-section:last-of-type { flex: 1 1 auto; max-height: none; }

  .section-title {
    font-family: var(--ui-font);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--muted);
    margin: 0 0 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .section-title.small { margin: 0 0 12px; }

  .chips {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .chips button {
    background: var(--panel);
    color: var(--muted);
    border: 1px solid var(--border-strong);
    font-family: var(--ui-font);
    font-size: 11px;
    font-weight: 500;
    padding: 4px 11px;
    text-transform: none;
    letter-spacing: 0;
    border-radius: 999px;
    box-shadow: none;
  }
  .chips button:hover {
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 50%, var(--border-strong));
    background: color-mix(in srgb, var(--accent) 6%, var(--panel));
  }
  .chips button.chip-active {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
  }
  .count { opacity: 0.75; margin-left: 6px; font-variant-numeric: tabular-nums; font-family: var(--num-font); }

  .hit {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 14px 16px;
    margin-bottom: 8px;
    transition: border-color 160ms ease, box-shadow 160ms ease;
  }
  .hit:hover {
    border-color: color-mix(in srgb, var(--accent) 35%, var(--border-strong));
    box-shadow: var(--shadow-s);
  }
  .hit header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
    font-family: var(--num-font);
    font-size: 11.5px;
    color: var(--muted);
    padding: 0;
    border: none;
  }
  .hit p {
    margin: 0;
    white-space: pre-wrap;
    font-family: var(--ui-font);
    font-size: 13px;
    line-height: 1.6;
    color: var(--ink);
  }
  .score {
    background: color-mix(in srgb, var(--accent) 8%, var(--panel));
    color: var(--accent);
    padding: 2px 8px;
    border: 1px solid color-mix(in srgb, var(--accent) 25%, transparent);
    border-radius: 999px;
    font-family: var(--num-font);
    font-size: 10.5px;
    font-weight: 600;
    font-feature-settings: "tnum";
  }

  /* Modal-side kind aliases (backend emits these full words; reuse the
   * terminal palette so the whole app speaks one visual vocabulary). */
  .kind-image          { color: #be185d; }
  .kind-audio          { color: #c2410c; }
  .kind-video          { color: #b91c1c; }
  .kind-html           { color: #1d4ed8; }
  .kind-docx           { color: #4338ca; }
  .kind-text           { color: #15803d; }
  .kind-chatgpt-export { color: var(--accent); }

  .path {
    font-family: var(--mono-font);
    font-size: 11.5px;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .empty {
    text-align: center;
    color: var(--muted);
    padding: 36px 24px;
    border: 1px dashed var(--border-strong);
    border-radius: var(--radius);
    font-family: var(--serif-font);
    font-size: 14px;
    line-height: 1.5;
  }

  .source-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }
  .source-list li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
  }
  .source-list li:last-child { border-bottom: none; }
  .source-list li:hover { background: color-mix(in srgb, var(--accent) 3%, var(--panel-2)); }
  .file-main {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    flex: 1;
  }
  .file-main .path {
    color: var(--ink);
    font-family: var(--ui-font);
    font-size: 12.5px;
  }
  .meta {
    color: var(--muted);
    font-family: var(--mono-font);
    font-size: 10.5px;
    font-variant-numeric: tabular-nums;
  }
  .file-actions {
    display: flex;
    gap: 4px;
  }
  .mono {
    font-family: var(--mono-font);
    font-size: 11px;
  }
  .claim-text {
    margin: 8px 0 4px;
    font-size: 13px;
    line-height: 1.45;
    color: var(--ink);
  }
  .identity-claim-list li {
    flex-direction: column;
    align-items: stretch;
  }
  .evidence-box {
    margin-top: 8px;
  }
  .evidence-pre {
    margin: 8px 0;
    padding: 10px;
    max-height: 220px;
    overflow: auto;
    font-family: var(--mono-font);
    font-size: 11px;
    line-height: 1.4;
    background: var(--panel-2);
    border-radius: 6px;
    border: 1px solid var(--border);
    white-space: pre-wrap;
  }

  .settings-advanced-card {
    background: color-mix(in srgb, var(--danger) 6%, var(--panel));
    border: 1px solid color-mix(in srgb, var(--danger) 22%, var(--border));
    border-radius: var(--radius-md);
    padding: 14px 16px 16px;
  }
  .settings-section-danger .section-title {
    color: color-mix(in srgb, var(--danger) 55%, var(--muted));
  }
  .setting-meta {
    margin-top: 6px;
    font-size: 10.5px;
    color: var(--dim);
    word-break: break-all;
    line-height: 1.35;
  }
  .path-one-line {
    margin-top: 8px;
    padding: 8px 10px;
    border-radius: var(--radius-sm);
    background: var(--panel-2);
    border: 1px solid var(--border);
    font-size: 10.5px;
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 100%;
  }
  .setting-tip {
    font-weight: 600;
    color: var(--ink);
    border-bottom: 1px dotted color-mix(in srgb, var(--muted) 70%, transparent);
    cursor: help;
  }
  .setting-callout {
    margin-top: 10px;
    padding: 8px 11px;
    border-radius: var(--radius-sm);
    font-size: 12px;
    line-height: 1.45;
    color: var(--ink);
    background: color-mix(in srgb, var(--accent) 8%, var(--panel));
    border: 1px solid color-mix(in srgb, var(--accent) 22%, var(--border));
  }
  .setting-callout-warn {
    background: color-mix(in srgb, var(--danger) 8%, var(--panel));
    border-color: color-mix(in srgb, var(--danger) 28%, var(--border));
    color: color-mix(in srgb, var(--danger) 92%, var(--ink));
  }
  .setting-row-stack {
    align-items: flex-start;
  }
  .setting-row-stack .setting-main {
    flex: 1 1 220px;
  }
  .settings-error-box {
    padding: 14px 16px;
    border: 1px solid color-mix(in srgb, var(--danger) 35%, var(--border));
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--danger) 6%, var(--panel));
    text-align: left;
  }
  .settings-error-box p {
    margin: 0 0 8px;
    font-family: var(--ui-font);
    font-size: 13px;
    color: var(--ink);
  }
  .settings-error-detail {
    font-size: 12px !important;
    color: var(--muted) !important;
    word-break: break-word;
    margin-bottom: 12px !important;
  }
  /* Settings modal: roomy rows, plain-language toggles. */
  .settings-section .section-hint {
    font-family: var(--ui-font);
    font-size: 10.5px;
    font-weight: 500;
    color: var(--muted);
    text-transform: none;
    letter-spacing: 0;
  }
  .setting-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 14px;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 0;
  }
  .setting-row:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }
  .setting-row:last-child { margin-bottom: 0; }
  .setting-main { min-width: 0; flex: 1; }
  .setting-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    flex-shrink: 0;
  }
  .setting-label {
    font-family: var(--ui-font);
    font-size: 13px;
    font-weight: 600;
    color: var(--ink);
  }
  .setting-desc {
    margin-top: 2px;
    font-family: var(--ui-font);
    font-size: 11.5px;
    color: var(--muted);
  }

  .kind-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }
  .kind-row {
    background: var(--panel);
    border-bottom: 1px solid var(--border);
  }
  .kind-row:last-child { border-bottom: none; }
  .kind-row:hover { background: color-mix(in srgb, var(--accent) 3%, var(--panel-2)); }
  .kind-toggle {
    display: grid;
    grid-template-columns: 20px auto 1fr;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    cursor: pointer;
  }
  .kind-toggle input[type="checkbox"] {
    width: 16px;
    height: 16px;
    accent-color: var(--accent);
    cursor: pointer;
  }
  .kind-name {
    font-family: var(--num-font);
    font-size: 11.5px;
    font-weight: 600;
    min-width: 6rem;
  }
  .kind-desc {
    font-family: var(--ui-font);
    font-size: 12px;
    color: var(--muted);
  }
  .kind-off .kind-name,
  .kind-off .kind-desc { opacity: 0.55; }
  .kind-off .kind-name .kind::before { background: var(--dim); }

  .settings-note {
    margin-top: 12px;
    padding: 10px 14px;
    background: color-mix(in srgb, var(--accent) 5%, var(--panel-2));
    border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--border));
    border-left: 3px solid var(--accent);
    border-radius: var(--radius-sm);
    font-family: var(--ui-font);
    font-size: 11.5px;
    color: var(--ink-2);
    line-height: 1.5;
  }

  /* Hide the legacy Svelte scrollbars in favor of the OS's native thin one. */
  .term-log::-webkit-scrollbar,
  .modal-section::-webkit-scrollbar { width: 8px; }
  .term-log::-webkit-scrollbar-thumb,
  .modal-section::-webkit-scrollbar-thumb {
    background: var(--border-strong);
    border-radius: 0;
  }
</style>
