<script lang="ts">
  import { onMount } from "svelte";
  import { listen } from "@tauri-apps/api/event";
  import { open as openDialog } from "@tauri-apps/plugin-dialog";
  import {
    connectClaudeDesktop,
    copyIntoInbox,
    registerSharedPaths,
    deleteSource,
    fetchSettings,
    fetchSources,
    fetchStatus,
    getConfig,
    onSidecarStatus,
    openEvents,
    restartSidecar,
    revealInFinder,
    search,
    updateSettings,
    type Active,
    type AppConfig,
    type ConnState,
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
  type EmbedAnim = { shownDone: number; targetDone: number; total: number };
  let ingestFeed = $state<{ id: string; path: string; status: string; ts: number; embed?: EmbedAnim }[]>([]);
  let active = $state<Active>({ root: null, total: 0, done: 0, added: 0, skipped: 0 });
  let connecting = $state(false);
  let connectMsg = $state<string>("");
  let showContents = $state(false);
  let showSettings = $state(false);
  let settingsError = $state<string | null>(null);
  let allKinds = $state<string[]>([]);
  let disabledKinds = $state<Set<string>>(new Set());
  let settingsLoaded = $state(false);
  let savingSettings = $state(false);
  let termEl: HTMLUListElement | undefined = $state();
  // Rolling per-file line id, so subsequent progress events for the same
  // path rewrite a single terminal line instead of stacking.
  let currentRow: Record<string, string> = {};
  /** One terminal row updating in place while the same skip reason repeats in a batch. */
  let skipRollup: { key: string; feedId: string } | null = null;
  let conn = $state<ConnState>("connecting");
  let lastHeartbeat = $state<number>(0);
  let restarting = $state(false);
  // First-launch sidecar bootstrap status. When `state === "ready"` the
  // overlay hides; any other non-null state shows a full-screen progress
  // card so the window isn't a silent void while pip runs for ~2 minutes.
  let sidecar = $state<SidecarStatus | null>(null);


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
      pushFeed("sidecar", `restarted (pid ${r.pid})`);
    } catch (e: any) {
      pushFeed("sidecar", `restart failed: ${e?.message ?? e}`);
    } finally {
      // WS will auto-reconnect; watch for the next "open" before clearing.
      setTimeout(() => (restarting = false), 1500);
    }
  }

  async function runConnect() {
    connecting = true;
    connectMsg = "";
    try {
      const res = await connectClaudeDesktop({});
      connectMsg = `Added to ${res.config_path.split("/").pop()}. Restart Claude Desktop to load.`;
    } catch (e) {
      connectMsg = `Failed: ${(e as Error).message}`;
    } finally {
      connecting = false;
    }
  }

  function sleep(ms: number) {
    return new Promise<void>((r) => setTimeout(r, ms));
  }

  /** WKWebView often surfaces connection refused as the useless string "Load failed". */
  function humanizeSettingsLoadError(msg: string): string {
    if (msg === "Load failed" || /^failed to fetch$/i.test(msg.trim())) {
      return "The sidecar is not up yet — wait for first-run setup to finish, then Retry or use Restart above.";
    }
    return msg;
  }

  const SETTINGS_LOAD_TIMEOUT_MS = 120_000;
  const SETTINGS_LOAD_STEP_MS = 400;

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
    const deadline = Date.now() + SETTINGS_LOAD_TIMEOUT_MS;
    let lastErr: Error | null = null;
    while (Date.now() < deadline) {
      try {
        const res = await fetchSettings();
        allKinds = res.all_kinds;
        disabledKinds = new Set(res.settings.disabled_kinds ?? []);
        settingsLoaded = true;
        return;
      } catch (e) {
        lastErr = e instanceof Error ? e : new Error(String(e));
        await sleep(SETTINGS_LOAD_STEP_MS);
      }
    }
    const msg = lastErr?.message ?? "unknown";
    settingsError = msg;
    pushFeed("settings", `load failed: ${msg}`);
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
      pushFeed("settings", `save failed: ${(e as Error).message}`);
    } finally {
      savingSettings = false;
    }
  }

  async function openSettings() {
    showSettings = true;
    if (!settingsLoaded) await loadSettings();
    try {
      await refreshStatus();
    } catch {
      /* sidecar may be offline */
    }
  }

  let copyHint = $state<string | null>(null);
  let copyHintTimer: ReturnType<typeof setTimeout> | null = null;
  async function copyToClipboard(label: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      copyHint = `${label} copied`;
    } catch {
      copyHint = "Copy failed";
    }
    if (copyHintTimer) clearTimeout(copyHintTimer);
    copyHintTimer = setTimeout(() => {
      copyHint = null;
      copyHintTimer = null;
    }, 2000);
  }

  const KIND_LABELS: Record<string, string> = {
    text: "Text",
    html: "Web",
    pdf: "PDF",
    docx: "Docs",
    image: "Image",
    audio: "Audio",
    code: "Code",
    "chatgpt-export": "ChatGPT",
  };

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
  function statusClass(s: string, pathHint?: string): string {
    if ((pathHint ?? "").includes("✨")) return "delight";
    const t = s.toLowerCase();
    if (/\b(error|failed|parse-error)\b/.test(t)) return "err";
    // "deferred" = the app will retry this one automatically (e.g. waiting
    // for the vision model to finish pulling). Not a user-visible failure.
    if (/\bdeferred\b|awaiting\s+vision/.test(t)) return "progress";
    if (/\bingested\b|\bdone\b/.test(t)) return "ok";
    if (/\bduplicate\b|\bunchanged\b|already\s*(saved|present|exists)/.test(t))
      return "saved";
    if (/\bcopied\b|\bunpacked\b|\bextracted\b|\bloaded\b|\bparsed\b|\bready\b|\brestarted\b/.test(t))
      return "ok";
    if (/\bskipped\b|\bskipping\b|\bempty\b|image-only|missing-deps|no-text|unsupported/.test(t))
      return "warn";
    if (/copying…|parsing|embedding|unpacking|extracting|pulling|restart requested/.test(t))
      return "progress";
    return "";
  }

  function pushFeed(path: string, state: string, embed?: EmbedAnim): string {
    const ts = Date.now();
    const id = `${path}:${ts}:${Math.random().toString(36).slice(2, 6)}`;
    // Append at the bottom and cap history, like a standard terminal.
    ingestFeed = [
      ...ingestFeed.slice(-299),
      embed ? { id, path, status: state, ts, embed } : { id, path, status: state, ts },
    ];
    scheduleScroll();
    return id;
  }

  function updateFeed(
    id: string,
    patch: { path?: string; status?: string; embed?: EmbedAnim | null },
  ) {
    ingestFeed = ingestFeed.map((row) => {
      if (row.id !== id) return row;
      let embed: EmbedAnim | undefined = row.embed;
      if (patch.embed === null) embed = undefined;
      else if (patch.embed !== undefined) embed = patch.embed;
      return {
        ...row,
        path: patch.path ?? row.path,
        status: patch.status ?? row.status,
        embed,
        ts: Date.now(),
      };
    });
    scheduleScroll();
  }

  let embedRafHandle: number | null = null;
  function cancelEmbedRaf() {
    if (embedRafHandle != null) {
      cancelAnimationFrame(embedRafHandle);
      embedRafHandle = null;
    }
  }

  /** Ease the displayed embed count toward the server target (no big jumps). */
  function scheduleEmbedRaf() {
    if (embedRafHandle != null) return;
    embedRafHandle = requestAnimationFrame(function tick() {
      embedRafHandle = null;
      let changed = false;
      ingestFeed = ingestFeed.map((row) => {
        if (!row.embed) return row;
        const { shownDone, targetDone, total } = row.embed;
        if (shownDone === targetDone) return row;
        const delta = targetDone - shownDone;
        // Larger gaps move faster so long runs still feel responsive.
        const step = Math.max(1, Math.ceil(Math.abs(delta) / 6));
        const next = shownDone + Math.sign(delta) * Math.min(step, Math.abs(delta));
        changed = true;
        const pct = total ? Math.floor((next / total) * 100) : 0;
        return {
          ...row,
          embed: { ...row.embed, shownDone: next, targetDone, total },
          status: `embedding ${next}/${total} (${pct}%)`,
          ts: Date.now(),
        };
      });
      if (changed) {
        scheduleScroll();
        scheduleEmbedRaf();
      }
    });
  }

  function applyEmbedProgress(path: string, done: number, total: number) {
    const id = currentRow[path];
    if (!id) {
      const embed: EmbedAnim = { shownDone: 0, targetDone: done, total };
      currentRow[path] = pushFeed(path, `embedding 0/${total} (0%)`, embed);
      scheduleEmbedRaf();
      return;
    }
    const row = ingestFeed.find((r) => r.id === id);
    const prev = row?.embed;
    const shownDone = prev?.shownDone ?? 0;
    const embed: EmbedAnim = { shownDone, targetDone: done, total };
    const showPct = total ? Math.floor((shownDone / total) * 100) : 0;
    updateFeed(id, {
      status: `embedding ${shownDone}/${total} (${showPct}%)`,
      embed,
    });
    scheduleEmbedRaf();
  }

  /** Rewrite the rolling line for `path`, or start a new one if none is open. */
  function logLine(path: string, status: string) {
    const id = currentRow[path];
    const clearEmbed = !/^embedding\b/.test(status);
    if (id) updateFeed(id, { status, ...(clearEmbed ? { embed: null } : {}) });
    else currentRow[path] = pushFeed(path, status);
  }

  /** Finalize the rolling line with a terminal status; next event opens a new line. */
  function endLine(path: string, status: string) {
    logLine(path, status);
    delete currentRow[path];
  }

  /** Drop the in-progress `[i/n] parsing…` row so we don't stack one line per file. */
  function stripRollingLineForPath(path: string | undefined) {
    if (!path) return;
    const id = currentRow[path];
    if (!id) return;
    ingestFeed = ingestFeed.filter((row) => row.id !== id);
    delete currentRow[path];
    scheduleScroll();
  }

  function rollupReasonKey(reason: string): string {
    const d = /^disabled:\s*'([^']+)'/i.exec(reason);
    if (d) return `disabled:${d[1]!.toLowerCase()}`;
    return reason.trim();
  }

  /** Short headline for the rollup row (same reason → one updating line). */
  function rollupHeadline(reason: string): string {
    const d = /^disabled:\s*'([^']+)'/i.exec(reason);
    if (d) {
      const k = d[1]!.toLowerCase();
      const byKind: Record<string, string> = {
        image: "Skipping images (PNG, JPG, …)",
        audio: "Skipping audio",
        video: "Skipping video",
        pdf: "Skipping PDFs",
        docx: "Skipping Word docs",
        code: "Skipping code files",
        html: "Skipping HTML",
        text: "Skipping plain text",
        "chatgpt-export": "Skipping chat exports",
      };
      const head = byKind[k] ?? `Skipping ‘${k}’ files`;
      return `${head} — change in Settings`;
    }
    if (/no module named ['"]faster_whisper['"]/i.test(reason)) {
      return "Skipping audio/video transcript (install faster-whisper in the sidecar venv)";
    }
    if (/no module named ['"]docx['"]/i.test(reason)) {
      return "Skipping Word docs (pip install python-docx — then restart sidecar)";
    }
    if (/missing-deps:.*pypdf|missing-deps:.*requirements/i.test(reason)) {
      return "Skipping PDF (sidecar venv missing PDF libs — see Settings → File logs / pip-bootstrap.log; delete data/venv and relaunch)";
    }
    const one = reason.replace(/^parse-error:\s*/i, "").trim();
    const short = one.length > 80 ? `${one.slice(0, 77)}…` : one;
    return `Skipping: ${short}`;
  }

  function handleIngestSkipped(msg: {
    active?: Active;
    result?: { path?: string; reason?: string; index?: number; total?: number };
  }) {
    const r = msg.result ?? {};
    const path = r.path as string | undefined;
    const reason = (r.reason ?? "").trim();
    stripRollingLineForPath(path);

    const act = msg.active;
    const done = act?.done ?? (r.index as number | undefined) ?? 0;
    const total = act?.total ?? (r.total as number | undefined) ?? "?";
    const frac = `${done}/${total}`;
    const key = rollupReasonKey(reason);
    const headline = rollupHeadline(reason);
    const status = `${headline} · ${frac}`;

    if (skipRollup && skipRollup.key === key) {
      updateFeed(skipRollup.feedId, { path: "⊘ batch", status });
    } else {
      skipRollup = { key, feedId: pushFeed("⊘ batch", status) };
    }
  }

  let scrollPending = false;
  function scheduleScroll() {
    if (scrollPending) return;
    scrollPending = true;
    requestAnimationFrame(() => {
      scrollPending = false;
      if (!termEl) return;
      termEl.scrollTo({ top: termEl.scrollHeight, behavior: "smooth" });
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
    if (!name || /^(drop|sidecar|vision|watcher)$/.test(name)) {
      if (name === "sidecar")  return { label: "SYS", cls: "sys" };
      if (name === "vision")   return { label: "VIS", cls: "vis" };
      if (name === "watcher")  return { label: "WCH", cls: "sys" };
      return { label: "···", cls: "dflt" };
    }
    if (name === "⊘ batch") return { label: "SKIP", cls: "warn" };
    if (p.includes("✨")) return { label: "✨", cls: "delight" };
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
      case "delight":  return "✨";
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
      const registerPaths: string[] = [];
      for (const d of res.drops) {
        if (d.kind !== "missing" && d.kind !== "unsupported" && d.kind !== "duplicate") {
          if (d.dest) registerPaths.push(d.dest);
          if (d.paths?.length) registerPaths.push(...d.paths);
        }
      }
      const uniqRegister = [...new Set(registerPaths)];
      if (uniqRegister.length) {
        try {
          await registerSharedPaths(uniqRegister);
        } catch {
          /* sidecar may be offline; ingest still proceeds via watcher */
        }
      }
      for (const d of res.drops) {
        const id = rowBySource[d.source];
        const landed = d.dest ?? d.source;
        let status: string;
        if (d.kind === "missing") status = "missing (no such path)";
        else if (d.kind === "unsupported") status = "skipped (not a file or folder)";
        else if (d.kind === "duplicate") status = `duplicate · already in inbox (${prettyBytes(d.bytes)})`;
        else if (d.copied === 0) status = "empty (nothing to index)";
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
      config = await getConfig();

      startHeartbeatWatchdog();

      // Register WS + bootstrap listeners BEFORE any HTTP call to the sidecar.
      // On first launch `refreshStatus`/`fetchSources` reject until pip finishes —
      // if those run first the whole IIFE threw and we never subscribed to
      // `sidecar://status`, so the UI stayed on "starting" forever.
      const closeWs = await openEvents(
        async (msg) => {
        lastHeartbeat = Date.now();
        // If we're receiving messages the socket is obviously open, even
        // if the onopen hook was missed (race during sidecar restart).
        if (conn !== "open") conn = "open";
        if (msg.type === "heartbeat" || msg.type === "ready" || msg.type === "snapshot") {
          if (status) status = { ...status, counts: msg.counts };
          if (msg.active) active = msg.active;
        } else if (msg.type === "ingest_started") {
          skipRollup = null;
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
            applyEmbedProgress(msg.path, done, total);
          }
        } else if (msg.type === "source_updated") {
          if (msg.active) active = msg.active;
          const r = msg.result as { path?: string; chunk_count?: number };
          if (r.path) endLine(r.path, `ingested · ${r.chunk_count ?? 0} chunks`);
          await refreshSources();
        } else if (msg.type === "ingest_delight") {
          const line = msg.line.trim();
          if (line) pushFeed("✨ Minion", line);
        } else if (msg.type === "ingest_skipped") {
          if (msg.active) active = msg.active;
          handleIngestSkipped(msg);
        } else if (msg.type === "ingest_failed") {
          if (msg.active) active = msg.active;
          endLine(msg.path, "failed");
        } else if (msg.type === "source_removed") {
          await refreshSources();
        } else if (msg.type === "tree_done") {
          skipRollup = null;
          active = { root: null, total: 0, done: 0, added: 0, skipped: 0 };
          pushFeed(msg.root, `done · +${msg.added} ${msg.skipped ? `· ⊘${msg.skipped}` : ""}`);
          await refreshSources();
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

      // Sidecar bootstrap progress (venv / pip).
      const unlistenSidecar = await onSidecarStatus((s) => {
        sidecar = s;
      });
      unlistens.push(unlistenSidecar);

      // Tauri v2 native drag-drop events.
      const unlistenDrop = await listen<{ paths: string[] }>("tauri://drag-drop", async (e) => {
        dragging = false;
        const paths = (e.payload as any)?.paths ?? [];
        await handleDropped(paths);
      });
      const unlistenEnter = await listen("tauri://drag-enter", () => {
        dragging = true;
      });
      const unlistenLeave = await listen("tauri://drag-leave", () => {
        dragging = false;
      });

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
      unlistens.push(
        () => unlistenDrop(),
        () => unlistenEnter(),
        () => unlistenLeave(),
        () => unlistenVision(),
      );

      try {
        await Promise.all([refreshStatus(), refreshSources()]);
        if (status?.active) active = status.active;
      } catch {
        /* sidecar still installing — WS + overlay will catch up when it's up */
      }
    })();

    // Braille spinner tick for in-flight rows. 10 fps is smooth without thrash.
    const spin = setInterval(() => { spinnerTick = (spinnerTick + 1) % 10_000; }, 100);

    return () => {
      clearInterval(spin);
      cancelEmbedRaf();
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
  <header>
    <div class="brand">
      <img src="/minion.png" alt="" class="brand-icon" />
      <h1>Minion</h1>
    </div>
    <div class="counts">
      {#if status}
        <span><strong>{status.counts.sources}</strong></span>
        <span class="watcher" class:live={status.watcher.running}>
          {status.watcher.running ? "watching" : "paused"}
        </span>
      {/if}
      <span
        class="status-pill status-{conn}"
        title={conn === "open"
          ? `Sidecar ready · ${config?.api_base ?? ""}`
          : conn === "connecting"
            ? "Connecting to sidecar…"
            : conn === "unreachable"
              ? "Sidecar unreachable. Check that Python 3.10+ is installed and click Settings → Restart."
              : "Sidecar restarting…"}
      >
        <span class="status-dot"></span>
        {conn === "open"
          ? "ready"
          : conn === "connecting"
            ? "starting"
            : conn === "unreachable"
              ? "offline"
              : "reconnecting"}
      </span>
      <button class="ghost" onclick={() => (showContents = true)} title="View indexed sources and search">
        Contents
      </button>
      <button class="ghost" onclick={openSettings} title="Server, Claude Desktop, and file-type preferences">
        Settings
      </button>
    </div>
  </header>

  {#if connectMsg}
    <div class="toast">{connectMsg}</div>
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
          <p class="bootstrap-tagline">
            Hey — I'm Minion. I'm getting ready so I can remember the files you drop here.
          </p>
        {/if}
        <div class="bootstrap-msg">
          {sidecar.message ?? "Working…"}
        </div>
        {#if sidecar.state !== "error"}
          <div class="bootstrap-spinner"></div>
          <div class="bootstrap-hint">First launch only — this won't happen again.</div>
        {:else}
          <button class="ghost" onclick={() => (sidecar = null)}>Dismiss</button>
        {/if}
      </div>
    </div>
  {/if}

  <section
    class="drop"
    class:active={dragging}
    role="button"
    tabindex="0"
    onclick={browseForFiles}
    onkeydown={(e) => e.key === "Enter" && browseForFiles()}
  >
    <img src="/minion.png" alt="" class="drop-watcher" aria-hidden="true" />
    <div class="drop-brackets">
      <span class="bracket">[</span>
      <div class="drop-body">
        <div class="drop-title">DROP FILES OR FOLDERS</div>
        <div class="drop-sub">
          &gt;&nbsp;
          <button class="linklike" onclick={(e) => { e.stopPropagation(); browseForFiles(); }}>select files</button>
          <span class="divider">/</span>
          <button class="linklike" onclick={(e) => { e.stopPropagation(); browseForFolder(); }}>select folder</button>
        </div>
      </div>
      <span class="bracket">]</span>
    </div>
  </section>

  <section class="term" aria-label="Activity log">
    <div class="term-head" aria-hidden="true">
      <span class="term-corner">┌─</span>
      <span class="term-title">activity</span>
      <span class="term-rule"></span>
      <span class="term-count">{ingestFeed.length} ev</span>
      <span class="term-corner">─┐</span>
    </div>
    <ul class="term-log" bind:this={termEl}>
      {#if ingestFeed.length === 0}
        <li class="term-empty">$ waiting for input…</li>
      {/if}
      {#each ingestFeed as item (item.id)}
        {@const cls = statusClass(item.status, item.path)}
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
    <div class="term-foot" aria-hidden="true">
      <span class="term-corner">└─</span>
      <span class="term-rule"></span>
      <span class="term-corner">─┘</span>
    </div>
  </section>
</main>

{#if showSettings}
  <div class="modal-overlay" role="button" tabindex="-1" onclick={() => (showSettings = false)} onkeydown={(e) => e.key === "Escape" && (showSettings = false)}>
    <div class="modal" role="dialog" tabindex="-1" aria-modal="true" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
      <header class="modal-head">
        <img src="/minion.png" alt="" class="modal-avatar" aria-hidden="true" />
        <h2>Settings</h2>
        <div class="modal-meta">server · claude desktop · file types</div>
        <button class="ghost" onclick={() => (showSettings = false)}>Close</button>
      </header>

      <div class="modal-section settings-section">
        <div class="section-title small">Server</div>
        <div class="setting-row">
          <div class="setting-main">
            <div class="setting-label">Sidecar status</div>
            <div class="setting-desc">
              {conn === "open"
                ? `ready · ${config?.api_base ?? ""}`
                : conn === "connecting"
                  ? "starting up…"
                  : "unreachable — try Restart"}
            </div>
          </div>
          <button
            class="ghost"
            onclick={handleRestart}
            disabled={restarting}
            title="Kill and respawn the Python sidecar"
          >
            {restarting ? "restarting…" : "Restart"}
          </button>
        </div>
        <div class="setting-row">
          <div class="setting-main">
            <div class="setting-label">Claude Desktop</div>
            <div class="setting-desc">
              {connectMsg || "Register Minion in Claude Desktop's mcpServers."}
            </div>
          </div>
          <button
            class="ghost"
            onclick={runConnect}
            disabled={connecting}
            title="Add Minion to Claude Desktop's mcpServers"
          >
            {connecting ? "connecting…" : "Connect"}
          </button>
        </div>
        {#if status}
          <div class="setting-row path-row">
            <div class="setting-main">
              <div class="setting-label">Memory index (MCP)</div>
              <div class="setting-desc path-hint">
                Claude, Cursor, and other MCP clients must set
                <code>MINION_DATA_DIR</code> and <code>MINION_INBOX</code> to these paths
                or searches will miss files the app just ingested. Use Connect above for
                Claude Desktop; for Cursor, mirror the same <code>env</code> block.
              </div>
              <pre class="path-block">{status.data_dir}</pre>
              <div class="path-actions">
                <button type="button" class="ghost tiny" onclick={() => copyToClipboard("Data dir", status!.data_dir)}>
                  Copy data dir
                </button>
                <button type="button" class="ghost tiny" onclick={() => copyToClipboard("Inbox", status!.inbox)}>
                  Copy inbox
                </button>
              </div>
              <pre class="path-block">{status.inbox}</pre>
              {#if copyHint}
                <div class="copy-toast">{copyHint}</div>
              {/if}
            </div>
          </div>
        {/if}
        {#if config?.logs_dir}
          <div class="setting-row path-row">
            <div class="setting-main">
              <div class="setting-label">File logs</div>
              <div class="setting-desc path-hint">
                Release builds write the Python sidecar and shell diagnostics under
                <code>logs/</code> next to your index (dev builds keep the sidecar in this terminal).
              </div>
              <pre class="path-block">{config.desktop_log}</pre>
              <pre class="path-block">{config.sidecar_log}</pre>
              <div class="path-actions">
                <button
                  type="button"
                  class="ghost tiny"
                  onclick={() => revealInFinder(config!.logs_dir)}
                  title="Show logs folder in Finder / file manager"
                >
                  Reveal logs folder
                </button>
              </div>
            </div>
          </div>
        {/if}
      </div>

      <div class="modal-section settings-section">
        <div class="section-title small">
          File types
          <span class="section-hint">{savingSettings ? "saving…" : "toggle what Minion ingests"}</span>
        </div>
        {#if !settingsLoaded && settingsError}
          <div class="empty">
            Couldn't reach the sidecar: {humanizeSettingsLoadError(settingsError)}{" "}
            <button class="link" onclick={loadSettings}>Retry</button>
          </div>
        {:else if !settingsLoaded}
          <div class="empty">
            {#if sidecar && sidecar.state !== "ready" && sidecar.state !== "error"}
              Waiting for setup…
              {#if sidecar.message}<div class="setting-desc">{sidecar.message}</div>{/if}
            {:else}
              Connecting to the sidecar…
            {/if}
          </div>
        {:else}
          <ul class="kind-list">
            {#each allKinds as k}
              {@const enabled = !disabledKinds.has(k)}
              <li class="kind-row" class:kind-off={!enabled}>
                <label class="kind-toggle">
                  <input
                    type="checkbox"
                    checked={enabled}
                    onchange={() => toggleKind(k)}
                    disabled={savingSettings}
                  />
                  <span class="kind-name">
                    <span class="kind kind-{k === 'chatgpt-export' ? 'chatgpt-export' : k}">{(KIND_LABELS[k] ?? k).toLowerCase()}</span>
                  </span>
                  <span class="kind-desc">{KIND_DESCRIPTIONS[k] ?? ""}</span>
                </label>
              </li>
            {/each}
          </ul>
          <div class="settings-note">
            Files of disabled kinds are left alone on disk — Minion will log
            them as skipped. Re-enable and restart the sidecar to pick them up.
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

{#if showContents}
  <div class="modal-overlay" role="button" tabindex="-1" onclick={() => (showContents = false)} onkeydown={(e) => e.key === "Escape" && (showContents = false)}>
    <div class="modal" role="dialog" tabindex="-1" aria-modal="true" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
      <header class="modal-head">
        <img src="/minion.png" alt="" class="modal-avatar" aria-hidden="true" />
        <h2>Contents</h2>
        <div class="modal-meta">
          {sources.length} source{sources.length === 1 ? "" : "s"}
          · {status?.counts.chunks ?? 0} chunks
        </div>
        <button class="ghost" onclick={() => (showContents = false)}>Close</button>
      </header>

      <div class="modal-search">
        <input
          placeholder="Ask your memory anything…"
          bind:value={queryText}
          onkeydown={(e) => e.key === "Enter" && runSearch()}
        />
        <button onclick={runSearch} disabled={searching}>{searching ? "…" : "Search"}</button>
      </div>

      {#if searchResults.length}
        <div class="modal-section">
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

      <div class="modal-section">
        <div class="section-title small">
          In memory
          <div class="chips">
            <button class:chip-active={!filterKind} onclick={() => { filterKind = ""; refreshSources(); }}>All</button>
            {#each kinds() as k}
              <button class:chip-active={filterKind === k} onclick={() => { filterKind = k; refreshSources(); }}>
                {KIND_LABELS[k] ?? k} <span class="count">{grouped()[k].length}</span>
              </button>
            {/each}
          </div>
        </div>
        {#if sources.length === 0}
          <div class="empty">Nothing here yet. Drop a file to get started.</div>
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
                  <button class="ghost" onclick={() => revealInFinder(s.path)}>Reveal</button>
                  <button class="ghost danger" onclick={() => removeSource(s)}>Forget</button>
                </div>
              </li>
            {/each}
          </ul>
        {/if}
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

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
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
    transform: none;
    transform-origin: 50% 65%;
    filter: drop-shadow(0 1px 2px rgba(16, 34, 56, 0.18))
            drop-shadow(0 0 6px color-mix(in srgb, var(--accent) 28%, transparent));
    animation: presence 3.6s ease-in-out infinite;
  }
  /* OS drag: pause header halo animation (drop zone carries the motion). */
  .app.dragging .brand-icon {
    animation: none;
    filter: drop-shadow(0 1px 2px rgba(16, 34, 56, 0.18))
            drop-shadow(0 0 6px color-mix(in srgb, var(--accent) 28%, transparent));
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
  .counts {
    display: flex;
    gap: 8px;
    align-items: center;
    font-family: var(--num-font);
    font-size: 11.5px;
    color: var(--muted);
    font-feature-settings: "tnum";
  }
  .counts strong { color: var(--ink); font-weight: 600; }

  /* Watcher + status indicator: soft pills, pill-shaped. */
  .watcher,
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border: 1px solid var(--border);
    background: var(--panel);
    font-family: var(--num-font);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.02em;
    color: var(--muted);
    border-radius: 999px;
  }
  .watcher.live { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); }
  .status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
  }
  .status-open    { color: var(--accent);  border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); background: color-mix(in srgb, var(--accent) 6%, var(--panel)); }
  .status-connecting { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 35%, var(--border)); }
  .status-closed  { color: var(--warn);  border-color: color-mix(in srgb, var(--warn) 35%, var(--border)); }
  .status-unreachable { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 40%, var(--border)); background: color-mix(in srgb, var(--danger) 6%, var(--panel)); }
  .status-open .status-dot       { background: var(--accent); animation: statuspulse 2.4s ease-in-out infinite; }
  .status-connecting .status-dot { background: var(--warn); animation: blink 0.9s ease-in-out infinite; }
  .status-closed .status-dot     { background: var(--warn); animation: blink 0.9s ease-in-out infinite; }
  .status-unreachable .status-dot { background: var(--danger); }
  @keyframes statuspulse {
    0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 45%, transparent); }
    50%      { box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 0%, transparent); }
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.3; }
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
    margin-bottom: 16px;
    white-space: pre-wrap;
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
    perspective: 260px;
    perspective-origin: 88% 92%;
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
  .drop-brackets {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: 18px;
    max-width: 520px;
    margin: 0 auto;
  }
  .bracket {
    font-family: var(--serif-font);
    font-size: 52px;
    font-weight: 400;
    line-height: 1;
    color: color-mix(in srgb, var(--accent) 45%, var(--dim));
    user-select: none;
    transition: color 200ms ease, transform 200ms ease;
  }
  .drop:hover .bracket { color: var(--accent); }
  .drop.active .bracket,
  .app.dragging .bracket {
    color: var(--accent);
    transform: scale(1.06);
  }
  .drop-body { text-align: center; }
  .drop-title {
    font-family: var(--display-font);
    font-size: 22px;
    font-weight: 400;
    letter-spacing: 0;
    color: var(--heading);
    text-transform: none;
    line-height: 1.2;
  }
  .drop-sub {
    margin-top: 8px;
    font-family: var(--ui-font);
    font-size: 12.5px;
    color: var(--muted);
  }
  .drop-sub .divider { margin: 0 8px; color: var(--dim); }

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
    transform-origin: 50% 78%;
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
    transform: rotate(0deg) translateY(-10px) scale(1.08);
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
    scroll-behavior: smooth;
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
  .term-log li.delight .msg,
  .term-log li.delight .term-glyph { color: var(--accent); }
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

  /* Gum/Bubble Tea-style box chrome around the activity stream. */
  .term-head, .term-foot {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 14px;
    font-family: var(--num-font);
    font-size: 10.5px;
    color: var(--muted);
    letter-spacing: 0.02em;
    user-select: none;
    background: color-mix(in srgb, var(--accent) 3%, var(--panel-2));
  }
  .term-head  { border-bottom: 1px dashed var(--border); }
  .term-foot  { border-top: 1px dashed var(--border); }
  .term-corner { color: color-mix(in srgb, var(--accent) 45%, var(--border-strong)); }
  .term-title {
    color: var(--accent);
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.16em;
    padding: 2px 8px;
    background: color-mix(in srgb, var(--accent) 10%, var(--panel));
    border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--border));
    border-radius: 999px;
    font-size: 10px;
  }
  .term-rule {
    flex: 1;
    border-top: 1px dashed var(--border-strong);
    align-self: center;
    margin-top: 1px;
  }
  .term-count {
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
  .kind-delight { color: var(--accent); text-shadow: 0 0 12px color-mix(in srgb, var(--accent) 35%, transparent); }

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
    padding: 56px 24px;
    z-index: 100;
  }
  .modal {
    width: 100%;
    max-width: 860px;
    max-height: calc(100vh - 112px);
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: var(--shadow-m);
  }
  .modal-head {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 16px 22px;
    border-bottom: 1px solid var(--border);
    background: color-mix(in srgb, var(--accent) 4%, var(--panel-2));
  }
  .modal-head h2 {
    margin: 0;
    font-family: var(--display-font);
    font-size: 22px;
    font-weight: 400;
    letter-spacing: -0.005em;
    color: var(--heading);
    text-transform: none;
    line-height: 1;
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
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--panel);
    margin-bottom: 8px;
  }
  .setting-row:last-child { margin-bottom: 0; }
  .setting-row.path-row {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .setting-row.path-row .setting-main {
    width: 100%;
  }
  .path-hint code {
    font-family: var(--mono-font);
    font-size: 11px;
  }
  .path-block {
    margin: 6px 0 0;
    padding: 8px 10px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-family: var(--mono-font);
    font-size: 11px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .path-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 4px;
  }
  button.ghost.tiny {
    font-size: 11px;
    padding: 4px 10px;
  }
  .copy-toast {
    margin-top: 6px;
    font-size: 11px;
    color: var(--accent-2);
  }
  .setting-main { min-width: 0; flex: 1; }
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
