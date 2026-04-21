<script lang="ts">
  import { onMount } from "svelte";
  import { listen } from "@tauri-apps/api/event";
  import { open as openDialog } from "@tauri-apps/plugin-dialog";
  import {
    connectClaudeDesktop,
    copyIntoInbox,
    deleteSource,
    fetchSources,
    fetchStatus,
    getConfig,
    openEvents,
    restartSidecar,
    revealInFinder,
    search,
    type Active,
    type AppConfig,
    type ConnState,
    type SearchHit,
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
  let showContents = $state(false);
  let termEl: HTMLUListElement | undefined = $state();
  // Rolling per-file line id, so subsequent progress events for the same
  // path rewrite a single terminal line instead of stacking.
  let currentRow: Record<string, string> = {};
  let conn = $state<ConnState>("connecting");
  let lastHeartbeat = $state<number>(0);
  let restarting = $state(false);


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
  function statusClass(s: string): string {
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
    if (!name || /^(drop|sidecar|vision|watcher)$/.test(name)) {
      if (name === "sidecar")  return { label: "SYS", cls: "sys" };
      if (name === "vision")   return { label: "VIS", cls: "vis" };
      if (name === "watcher")  return { label: "WCH", cls: "sys" };
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
      await Promise.all([refreshStatus(), refreshSources()]);

      // Hydrate active snapshot from /status (in case we started mid-run).
      if (status?.active) active = status.active;

      startHeartbeatWatchdog();
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
        } else if (msg.type === "ingest_skipped") {
          if (msg.active) active = msg.active;
          const r = msg.result as { path?: string; reason?: string };
          if (r.path) endLine(r.path, `skipped (${r.reason ?? ""})`);
        } else if (msg.type === "ingest_failed") {
          if (msg.active) active = msg.active;
          endLine(msg.path, "failed");
        } else if (msg.type === "source_removed") {
          await refreshSources();
        } else if (msg.type === "tree_done") {
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
      <div class="dot"></div>
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
            : "Sidecar unreachable. Click Restart."}
      >
        <span class="status-dot"></span>
        {conn === "open" ? "ready" : conn === "connecting" ? "starting" : "offline"}
      </span>
      <button
        class="ghost"
        onclick={handleRestart}
        disabled={restarting}
        title="Kill and respawn the Python sidecar"
      >
        {restarting ? "restarting…" : "Restart"}
      </button>
      <button class="ghost" onclick={() => (showContents = true)} title="View indexed sources and search">
        Contents
      </button>
      <button class="ghost" onclick={runConnect} disabled={connecting} title="Add Minion to Claude Desktop's mcpServers">
        {connecting ? "connecting…" : "Connect"}
      </button>
    </div>
  </header>

  {#if connectMsg}
    <div class="toast">{connectMsg}</div>
  {/if}

  <section class="drop" class:active={dragging} role="button" tabindex="0" onclick={browseForFiles} onkeydown={(e) => e.key === "Enter" && browseForFiles()}>
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
    <div class="term-foot" aria-hidden="true">
      <span class="term-corner">└─</span>
      <span class="term-rule"></span>
      <span class="term-corner">─┘</span>
    </div>
  </section>
</main>

{#if showContents}
  <div class="modal-overlay" role="button" tabindex="-1" onclick={() => (showContents = false)} onkeydown={(e) => e.key === "Escape" && (showContents = false)}>
    <div class="modal" role="dialog" tabindex="-1" aria-modal="true" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
      <header class="modal-head">
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
    /* Brand system: warm paper ground, teal identity, serif display for
     * moments of presence. This app holds memories — it should feel like a
     * quiet friend, not a corporate dashboard. */
    --bg:            #f5f2ec;  /* site background */
    --panel:         #ffffff;
    --panel-2:       #efeae1;
    --panel-3:       #e7e1d4;
    --border:        #e1dbcd;
    --border-strong: #cfc7b3;
    --ink:           #26221d;  /* site text — warm graphite */
    --ink-2:         #3d382f;
    --heading:       #1a1a1a;  /* pitch headings */
    --text: var(--ink);
    --muted:         #807a6c;
    --dim:           #b4ac9c;
    --accent:        #087074;  /* site primary teal */
    --accent-2:      #1b7a70;  /* pitch primary teal (hover/focus) */
    --accent-soft:   #cfe5e3;
    --glow:          #4ea9a2;  /* halo / status pulse */
    --success:       #0f8a5f;
    --saved:         #7a4fc8;
    --progress:      #2b60d6;
    --warn:          #b45309;
    --danger:        #b91c1c;
    --ok: var(--success);
    --radius-sm: 6px;
    --radius:    10px;
    --radius-lg: 14px;
    --shadow-s: 0 1px 2px rgba(38, 34, 29, 0.05), 0 2px 6px rgba(38, 34, 29, 0.04);
    --shadow-m: 0 2px 10px rgba(38, 34, 29, 0.07), 0 18px 48px -16px rgba(38, 34, 29, 0.14);
    --shadow-glow: 0 0 0 0.5px color-mix(in srgb, var(--accent) 30%, transparent),
                   0 6px 24px -8px color-mix(in srgb, var(--accent) 40%, transparent);
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
  /* A small, living orb: teal core + soft halo that breathes. This is the
   * "presence" — quiet signal that something is listening. */
  .dot {
    display: inline-block;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 30%,
      color-mix(in srgb, var(--accent) 40%, #fff) 0%,
      var(--accent) 60%,
      var(--accent-2) 100%);
    box-shadow:
      0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent),
      0 0 14px 2px color-mix(in srgb, var(--accent) 30%, transparent);
    animation: presence 3.6s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes presence {
    0%, 100% {
      box-shadow:
        0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent),
        0 0 14px 2px color-mix(in srgb, var(--accent) 30%, transparent);
    }
    50% {
      box-shadow:
        0 0 0 5px color-mix(in srgb, var(--accent) 10%, transparent),
        0 0 22px 4px color-mix(in srgb, var(--accent) 45%, transparent);
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
  .status-closed  { color: var(--danger);  border-color: color-mix(in srgb, var(--danger) 35%, var(--border)); }
  .status-open .status-dot       { background: var(--accent); animation: statuspulse 2.4s ease-in-out infinite; }
  .status-connecting .status-dot { background: var(--warn); animation: blink 0.9s ease-in-out infinite; }
  .status-closed .status-dot     { background: var(--danger); }
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

  /* Gum/Bubble Tea-style box chrome around the activity stream. */
  .term-head, .term-foot {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 10px;
    font-family: var(--mono-font);
    font-size: 11px;
    color: var(--dim);
    letter-spacing: 0.04em;
    user-select: none;
  }
  .term-head  { border-bottom: 1px dashed var(--border); }
  .term-foot  { border-top: 1px dashed var(--border); }
  .term-corner { color: var(--border-strong); }
  .term-title {
    color: var(--ink);
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.12em;
    padding: 0 6px;
    background: var(--panel-3);
    border-radius: 2px;
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
  .kind {
    flex-shrink: 0;
    font-family: var(--num-font);
    font-size: 9.5px;
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 1px 7px;
    border-radius: 999px;
    border: 1px solid currentColor;
    line-height: 1.5;
    text-transform: uppercase;
    min-width: 3.25rem;
    text-align: center;
    opacity: 0.95;
  }
  .kind-pdf  { color: #7c2d12; background: #fef3c7; border-color: #fbbf24; }
  .kind-note { color: #166534; background: #dcfce7; border-color: #86efac; }
  .kind-data { color: #0f766e; background: #ccfbf1; border-color: #5eead4; }
  .kind-web  { color: #1e40af; background: #dbeafe; border-color: #93c5fd; }
  .kind-code { color: #1e3a8a; background: #e0e7ff; border-color: #a5b4fc; }
  .kind-img  { color: #9d174d; background: #fce7f3; border-color: #f9a8d4; } /* bubblegum */
  .kind-aud  { color: #9a3412; background: #ffedd5; border-color: #fdba74; }
  .kind-vid  { color: #991b1b; background: #fee2e2; border-color: #fca5a5; }
  .kind-arc  { color: #713f12; background: #fef9c3; border-color: #fde047; }
  .kind-dir  { color: #3730a3; background: #e0e7ff; border-color: #818cf8; }
  .kind-sys  { color: #334155; background: #f1f5f9; border-color: #cbd5e1; }
  .kind-vis  { color: #86198f; background: #fae8ff; border-color: #e9d5ff; }
  .kind-dflt { color: var(--muted); background: var(--panel-3); border-color: var(--border-strong); }

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
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--muted);
    margin: 0 0 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .section-title.small { margin: 0 0 10px; }
  .section-title::before { content: "// "; color: var(--dim); }

  .chips {
    display: flex;
    gap: 0;
    flex-wrap: wrap;
  }
  .chips button {
    background: var(--panel);
    color: var(--muted);
    border: 1px solid var(--border-strong);
    margin-left: -1px; /* collapse borders for TUI strip */
    font-family: var(--mono-font);
    font-size: 10.5px;
    padding: 3px 10px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-radius: 0;
  }
  .chips button:first-child { margin-left: 0; }
  .chips button:hover { color: var(--ink); border-color: var(--ink); z-index: 1; }
  .chips button.chip-active {
    background: var(--ink);
    color: #fff;
    border-color: var(--ink);
    z-index: 2;
  }
  .count { opacity: 0.7; margin-left: 6px; font-variant-numeric: tabular-nums; }

  .hit {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 0;
    padding: 12px 14px;
    margin-bottom: 6px;
  }
  .hit:hover { border-color: var(--border-strong); }
  .hit header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 6px;
    font-family: var(--mono-font);
    font-size: 11px;
    color: var(--muted);
    padding: 0;
    border: none;
  }
  .hit p {
    margin: 0;
    white-space: pre-wrap;
    font-family: var(--ui-font);
    font-size: 12.5px;
    line-height: 1.55;
    color: var(--ink);
  }
  .score {
    background: transparent;
    padding: 0;
    border: none;
    font-family: var(--mono-font);
    font-size: 10.5px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .score::before { content: "["; color: var(--dim); }
  .score::after  { content: "]"; color: var(--dim); }

  /* Modal-side kind aliases (backend emits these full words; reuse the
   * terminal pill palette so the whole app speaks one visual vocabulary). */
  .kind-image          { color: #9d174d; background: #fce7f3; border-color: #f9a8d4; }
  .kind-audio          { color: #9a3412; background: #ffedd5; border-color: #fdba74; }
  .kind-video          { color: #991b1b; background: #fee2e2; border-color: #fca5a5; }
  .kind-html           { color: #1e40af; background: #dbeafe; border-color: #93c5fd; }
  .kind-docx           { color: #1e3a8a; background: #e0e7ff; border-color: #a5b4fc; }
  .kind-text           { color: #166534; background: #dcfce7; border-color: #86efac; }
  .kind-chatgpt-export { color: #0b5e63; background: #d5ecec; border-color: #7fc7cb; }

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
    padding: 28px;
    border: 1px dashed var(--border-strong);
    border-radius: 0;
    font-family: var(--mono-font);
    font-size: 12px;
  }
  .empty::before { content: "$ "; color: var(--dim); }

  .source-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
  }
  .source-list li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    border-radius: 0;
  }
  .source-list li:last-child { border-bottom: none; }
  .source-list li:hover { background: var(--panel-2); }
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

  /* Hide the legacy Svelte scrollbars in favor of the OS's native thin one. */
  .term-log::-webkit-scrollbar,
  .modal-section::-webkit-scrollbar { width: 8px; }
  .term-log::-webkit-scrollbar-thumb,
  .modal-section::-webkit-scrollbar-thumb {
    background: var(--border-strong);
    border-radius: 0;
  }
</style>
