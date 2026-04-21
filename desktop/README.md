# Minion Desktop

A Tauri + SvelteKit shell around Minion. The whole app is one window: drop a
file, see it in memory, ask it a question — then connect Claude Desktop (or
anything else that speaks MCP) with one click.

```
┌───────────────────────────────────────────────────┐
│  Minion  memory any agent can read    [Connect…]  │
├───────────────────────────────────────────────────┤
│                                                   │
│             ↓   Drop files anywhere               │
│             They land in ~/…/Minion/inbox         │
│                                                   │
├───────────────────────────────────────────────────┤
│  [ Ask your memory anything…            ] [Search]│
├───────────────────────────────────────────────────┤
│  In memory          [All] [PDF 3] [Code 12] …     │
│  ▸ meeting-notes.md    48 KB · 2m ago  [Reveal] …│
│  ▸ invoice-2026.pdf    320 KB · 1h ago [Reveal] …│
└───────────────────────────────────────────────────┘
```

## Architecture

```
┌──────────────┐     HTTP/WS      ┌─────────────────┐
│  SvelteKit   │ ◄──────────────► │  FastAPI        │
│  (webview)   │ 127.0.0.1:<port> │  sidecar (py)   │
└──────┬───────┘                  └────────┬────────┘
       │ invoke()                          │
       ▼                                    ▼
┌──────────────┐                  ┌─────────────────┐
│  Tauri (Rust)│ ── spawn ──────► │ watcher + store │
│  - inbox     │                  │ memory.db       │
│  - drag/drop │                  │ (sqlite-vec)    │
└──────────────┘                  └─────────────────┘
```

- **Rust shell** (`src-tauri/`): owns the window, resolves the data dir
  (`~/Library/Application Support/Minion/data` unless `MINION_DATA_DIR` is
  set), bootstraps a venv + `pip install` on first launch, spawns the Python
  sidecar, exposes `app_config`, `copy_into_inbox`, `reveal_in_finder`
  (macOS: `open -R` so Finder reveals the file in its folder).
- **Sidecar** (`chatgpt_mcp_memory/src/api.py`): the same ingest + store
  modules the MCP uses, exposed as HTTP. WebSocket `/events` pushes live
  ingest progress and heartbeats. Release builds ship sources under
  `Resources/sidecar` (synced by `src-tauri/scripts/sync_sidecar.sh` in
  `beforeBuildCommand` from `../chatgpt_mcp_memory`).
- **Frontend** (`src/`): SvelteKit SPA. Dropped files are copied into the
  inbox; the watcher already running inside the sidecar picks them up.
  **Settings** surfaces resolved paths; **Connect** writes Claude Desktop MCP
  config with matching `MINION_DATA_DIR` and `MINION_INBOX`.
- **File logs (release `.app`)**: `<MINION_DATA_DIR>/logs/minion-desktop.log`
  (shell) and `sidecar.log` (Python / uvicorn). **Settings → File logs** shows
  paths and **Reveal logs folder**. `cargo tauri dev` keeps the sidecar on the
  terminal instead of duplicating into `sidecar.log`.

## Dev

```bash
# One-time: python env for the sidecar (same one the MCP uses)
cd ../chatgpt_mcp_memory
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Then, from desktop/:
cd ../desktop
npm install
npm run tauri dev
```

Dev quirks:

- The Rust shell looks for `../chatgpt_mcp_memory/.venv/bin/python`; if absent
  it falls back to `python3` on `PATH`.
- The sidecar binds `127.0.0.1` on `MINION_API_PORT` (default `8765`).
- Override data location with `MINION_DATA_DIR=/path`.
- Disable the background watcher with `MINION_DISABLE_WATCHER=1` (useful
  when iterating on ingest logic).

## Build

```bash
npm run tauri build
```

Produces `src-tauri/target/release/bundle/macos/Minion.app` and a `.dmg`
under `.../bundle/dmg/`. `beforeBuildCommand` runs `sync_sidecar.sh` then the
static frontend build; bundled resources include the Python tree the sidecar
runs from. First launch of the `.app` still creates the venv and installs
dependencies into the user’s data directory.

## Connect any MCP client

The **Connect** control merges Minion into
`~/Library/Application Support/Claude/claude_desktop_config.json` under
`mcpServers.minion`, with **`MINION_DATA_DIR`** and **`MINION_INBOX`** set to
the same paths the desktop app uses (so Claude and the app share one
`memory.db`). For Cursor or other MCP clients, mirror those env vars and the
`command` / `args` from **Settings** or from
`chatgpt_mcp_memory/claude_desktop_config.example.json`.
