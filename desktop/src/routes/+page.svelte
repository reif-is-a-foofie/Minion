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
    revealInFinder,
    search,
    type AppConfig,
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
  let ingestFeed = $state<{ id: string; path: string; status: string }[]>([]);
  let connecting = $state(false);
  let connectMsg = $state<string>("");

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

  function pushFeed(path: string, state: string) {
    const id = `${path}:${Date.now()}`;
    ingestFeed = [{ id, path, status: state }, ...ingestFeed.slice(0, 19)];
  }

  async function handleDropped(paths: string[]) {
    if (!paths.length) return;
    for (const p of paths) pushFeed(p, "copying…");
    const dests = await copyIntoInbox(paths);
    for (const d of dests) pushFeed(d, "queued");
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

      // WebSocket: live updates from the sidecar.
      const closeWs = await openEvents(async (msg) => {
        if (msg.type === "heartbeat" || msg.type === "ready" || msg.type === "snapshot") {
          if (status) status = { ...status, counts: msg.counts };
        } else if (msg.type === "ingest_started") {
          pushFeed(msg.path, "parsing…");
        } else if (msg.type === "source_updated") {
          const r = msg.result as { path?: string; chunk_count?: number };
          if (r.path) pushFeed(r.path, `ingested (${r.chunk_count ?? 0} chunks)`);
          await refreshSources();
        } else if (msg.type === "ingest_skipped") {
          const r = msg.result as { path?: string; reason?: string };
          if (r.path) pushFeed(r.path, `skipped (${r.reason ?? ""})`);
        } else if (msg.type === "source_removed") {
          await refreshSources();
        } else if (msg.type === "tree_done") {
          pushFeed(msg.root, `folder done (+${msg.added}, skipped ${msg.skipped})`);
          await refreshSources();
        }
      });
      unlistens.push(closeWs);

      // Tauri v2 native drag-drop events.
      const unlistenDrop = await listen<{ paths: string[] }>("tauri://drag-drop", async (e) => {
        dragging = false;
        const paths = (e.payload as any)?.paths ?? [];
        await handleDropped(paths);
      });
      const unlistenEnter = await listen("tauri://drag-enter", () => (dragging = true));
      const unlistenLeave = await listen("tauri://drag-leave", () => (dragging = false));
      unlistens.push(() => unlistenDrop(), () => unlistenEnter(), () => unlistenLeave());
    })();

    return () => unlistens.forEach((fn) => fn());
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
</svelte:head>

<main class="app" class:dragging>
  <header>
    <div class="brand">
      <div class="dot"></div>
      <h1>Minion</h1>
      <span class="tag">memory any agent can read</span>
    </div>
    <div class="counts">
      {#if status}
        <span><strong>{status.counts.sources}</strong> sources</span>
        <span><strong>{status.counts.chunks}</strong> chunks</span>
        <span class="watcher" class:live={status.watcher.running}>
          {status.watcher.running ? "watching" : "paused"}
        </span>
      {/if}
      <button class="ghost" onclick={runConnect} disabled={connecting} title="Add Minion to Claude Desktop's mcpServers">
        {connecting ? "connecting…" : "Connect Claude Desktop"}
      </button>
    </div>
  </header>

  {#if connectMsg}
    <div class="toast">{connectMsg}</div>
  {/if}

  <section class="drop" class:active={dragging} role="button" tabindex="0" onclick={browseForFiles} onkeydown={(e) => e.key === "Enter" && browseForFiles()}>
    <div class="drop-inner">
      <div class="drop-icon">↓</div>
      <div class="drop-title">Drop files or folders</div>
      <div class="drop-sub">
        {#if config}
          They land in <code>{config.inbox}</code> — folders recurse; <code>node_modules</code>, <code>.git</code> etc. are skipped.
        {:else}
          Notes, PDFs, images, audio, code, whole repos — any agent you connect can read them.
        {/if}
      </div>
      <div class="drop-actions">
        <button class="ghost" onclick={(e) => { e.stopPropagation(); browseForFiles(); }}>browse files…</button>
        <button class="ghost" onclick={(e) => { e.stopPropagation(); browseForFolder(); }}>browse folder…</button>
      </div>
    </div>
  </section>

  <section class="search">
    <input
      placeholder="Ask your memory anything…"
      bind:value={queryText}
      onkeydown={(e) => e.key === "Enter" && runSearch()}
    />
    <button onclick={runSearch} disabled={searching}>{searching ? "searching…" : "Search"}</button>
  </section>

  {#if searchResults.length}
    <section class="results">
      <div class="section-title">Results</div>
      {#each searchResults as hit}
        <article class="hit">
          <header>
            <span class="score">{hit.score.toFixed(3)}</span>
            <span class="kind kind-{hit.kind}">{KIND_LABELS[hit.kind] ?? hit.kind}</span>
            <span class="path" title={hit.path}>{hit.path}</span>
          </header>
          <p>{hit.text}</p>
        </article>
      {/each}
    </section>
  {/if}

  <section class="sources">
    <div class="section-title">
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
      <div class="empty">Nothing here yet. Drop a file above to get started.</div>
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
  </section>

  {#if ingestFeed.length}
    <aside class="feed">
      <div class="section-title small">Recent activity</div>
      <ul>
        {#each ingestFeed as item}
          <li><span class="feed-status">{item.status}</span> <span class="feed-path">{item.path.split("/").pop()}</span></li>
        {/each}
      </ul>
    </aside>
  {/if}
</main>

<style>
  :global(:root) {
    --bg: #0e0f12;
    --panel: #16181d;
    --panel-2: #1d2027;
    --border: #262a33;
    --border-strong: #363b47;
    --text: #e8ebf1;
    --muted: #8a92a5;
    --accent: #7c7cff;
    --accent-2: #5959d9;
    --success: #4ade80;
    --danger: #f87171;
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "SF Pro Text", Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  :global(body) {
    margin: 0;
    background: var(--bg);
  }
  :global(*) {
    box-sizing: border-box;
  }

  .app {
    padding: 20px 24px 80px;
    max-width: 960px;
    margin: 0 auto;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .brand h1 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 12px var(--accent);
  }
  .tag {
    font-size: 12px;
    color: var(--muted);
    padding-left: 4px;
  }
  .counts {
    display: flex;
    gap: 14px;
    font-size: 13px;
    color: var(--muted);
  }
  .counts strong {
    color: var(--text);
  }
  .counts button.ghost {
    margin-left: 8px;
  }
  .toast {
    background: color-mix(in srgb, var(--accent) 15%, var(--panel));
    border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border));
    color: var(--text);
    padding: 10px 14px;
    border-radius: 10px;
    font-size: 13px;
    margin: 0 0 12px;
  }
  .watcher {
    padding: 2px 8px;
    border-radius: 99px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .watcher.live {
    color: var(--success);
    border-color: color-mix(in srgb, var(--success) 40%, transparent);
  }

  .drop {
    border: 2px dashed var(--border-strong);
    border-radius: 16px;
    padding: 36px 20px;
    text-align: center;
    background: linear-gradient(180deg, var(--panel) 0%, var(--panel-2) 100%);
    cursor: pointer;
    transition: all 160ms ease;
    outline: none;
  }
  .drop:hover {
    border-color: var(--accent);
  }
  .drop.active,
  .app.dragging .drop {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 10%, var(--panel));
    transform: scale(1.005);
  }
  .drop-icon {
    font-size: 36px;
    color: var(--accent);
    line-height: 1;
    margin-bottom: 8px;
  }
  .drop-title {
    font-size: 18px;
    font-weight: 600;
  }
  .drop-sub {
    color: var(--muted);
    margin: 6px 0 14px;
    font-size: 13px;
  }
  .drop-sub code {
    background: var(--panel-2);
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 12px;
  }
  .drop-actions {
    display: flex;
    gap: 8px;
    justify-content: center;
  }

  .search {
    display: flex;
    gap: 8px;
    margin: 20px 0 8px;
  }
  .search input {
    flex: 1;
    padding: 10px 14px;
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 10px;
    font-size: 14px;
    outline: none;
  }
  .search input:focus {
    border-color: var(--accent);
  }

  button {
    background: var(--accent);
    color: white;
    border: none;
    padding: 10px 16px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background 120ms ease;
  }
  button:hover {
    background: var(--accent-2);
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  button.ghost {
    background: transparent;
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 10px;
    font-size: 12px;
  }
  button.ghost:hover {
    border-color: var(--accent);
    color: var(--accent);
    background: transparent;
  }
  button.ghost.danger:hover {
    border-color: var(--danger);
    color: var(--danger);
  }

  .section-title {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    margin: 24px 0 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .section-title.small {
    margin: 8px 0;
  }
  .chips {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .chips button {
    background: var(--panel);
    color: var(--muted);
    border: 1px solid var(--border);
    font-size: 11px;
    padding: 4px 10px;
    text-transform: none;
    letter-spacing: 0;
  }
  .chips button.chip-active {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .count {
    opacity: 0.7;
    margin-left: 4px;
  }

  .results .hit {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 8px;
  }
  .hit header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    font-size: 12px;
    color: var(--muted);
  }
  .hit p {
    margin: 0;
    white-space: pre-wrap;
    font-size: 13px;
    line-height: 1.5;
  }
  .score {
    background: var(--panel-2);
    padding: 2px 6px;
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
  }
  .kind {
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 4px;
    background: var(--panel-2);
    color: var(--text);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .kind-code { background: #2d3142; color: #a5b4fc; }
  .kind-pdf { background: #3a1f1f; color: #fca5a5; }
  .kind-image { background: #1f3a24; color: #86efac; }
  .kind-audio { background: #3a2e1f; color: #fcd34d; }
  .kind-html { background: #1f2f3a; color: #7dd3fc; }
  .kind-docx { background: #2a1f3a; color: #c4b5fd; }
  .kind-chatgpt-export { background: #3a1f2e; color: #f9a8d4; }

  .path {
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 12px;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sources .empty {
    text-align: center;
    color: var(--muted);
    padding: 30px;
    border: 1px dashed var(--border);
    border-radius: 10px;
  }
  .source-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .source-list li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .source-list li:hover {
    border-color: var(--border-strong);
  }
  .file-main {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    flex: 1;
  }
  .file-main .path {
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
  }
  .meta {
    color: var(--muted);
    font-size: 11px;
  }
  .file-actions {
    display: flex;
    gap: 6px;
  }

  .feed {
    position: fixed;
    right: 20px;
    bottom: 20px;
    width: 280px;
    max-height: 40vh;
    overflow-y: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 10px 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    font-size: 12px;
  }
  .feed ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .feed li {
    padding: 4px 0;
    border-top: 1px solid var(--border);
    display: flex;
    gap: 6px;
  }
  .feed li:first-child {
    border-top: none;
  }
  .feed-status {
    color: var(--accent);
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
  }
  .feed-path {
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
