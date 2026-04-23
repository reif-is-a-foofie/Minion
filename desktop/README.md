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
│  (webview)   │  127.0.0.1:8765  │  sidecar (py)   │
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
  (`~/Library/Application Support/Minion/data`), spawns the Python sidecar,
  can **download Ollama** into `<data_dir>/managed-ollama/` if no system
  `ollama` exists (official universal macOS zip — same binary for Intel and
  Apple Silicon). Set `MINION_SKIP_MANAGED_OLLAMA=1` to disable.
  Exposes `app_config`, `copy_into_inbox`, `reveal_in_finder` commands.
- **Sidecar** (`chatgpt_mcp_memory/src/api.py`): the same ingest + store
  modules the MCP uses, exposed as HTTP. WebSocket `/events` pushes live
  ingest progress and heartbeats.
- **Frontend** (`src/`): SvelteKit SPA. Dropped files are copied into the
  inbox; the watcher already running inside the sidecar picks them up.

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
- The sidecar binds `127.0.0.1:8765`. Override with `MINION_API_PORT`.
- **One app per data dir:** leave `MINION_API_PORT` unset for normal use so each
  launch picks a free loopback port and can reclaim stale `api.py` PIDs on that
  port. Setting a **fixed** `MINION_API_PORT` is for intentional second instances
  or automation; two desktop sessions on the same port + dir risk confusion.
  Restart from the app (or `resolve_sidecar_port` in `src-tauri/src/lib.rs`) kills
  orphaned `api.py --port …` for the chosen port before spawn.
- Override data location with `MINION_DATA_DIR=/path`.
- Disable the background watcher with `MINION_DISABLE_WATCHER=1` (useful
  when iterating on ingest logic).
- **Anonymous telemetry (opt-out):** the shell forwards a default HTTPS collector
  URL to the sidecar (see `DEFAULT_MINION_ANALYTICS_URL` in `src-tauri/src/lib.rs`).
  Override with `MINION_ANALYTICS_URL`, or disable entirely with
  `MINION_DISABLE_REMOTE_ANALYTICS=1`. Users turn off sending under **Settings → Support**.
  Payloads are coarse counters only (`chatgpt_mcp_memory/src/analytics_remote.py`).
- **RAM / processes:** see [`../chatgpt_mcp_memory/docs/process-hygiene.md`](../chatgpt_mcp_memory/docs/process-hygiene.md)
  (Multipass VM name clash, stale sidecars, optional `MINION_EMBED_IDLE_SEC` /
  `MINION_EMBED_BATCH_SIZE`).

## Release zips: Intel vs Apple Silicon

[**GitHub Releases**](https://github.com/reif-is-a-foofie/Minion/releases) ship two zips per version. File names spell out the hardware in plain language (users should not need “arm64” / “x64”):

| Filename contains | CPU | Typical Macs |
|-------------------|-----|----------------|
| **`macOS-Apple-Silicon`** | Apple Silicon | M1, M2, M3, M4, … — **About This Mac** shows **Chip:** Apple M… |
| **`macOS-Intel`** | Intel | **About This Mac** shows **Processor:** … **Intel** … |

**Prep for clients:** “If you see **Chip** in About This Mac, get the zip with **Apple-Silicon** in the name. If you see an **Intel** processor, get the zip with **Intel** in the name.”

## Build

```bash
# Default: matches the machine you build on (Apple Silicon → arm64, Intel → x64)
npm run tauri build

# Apple Silicon .app from any host that has the Rust target installed:
npm run tauri build -- --target aarch64-apple-darwin

# Intel .app:
npm run tauri build -- --target x86_64-apple-darwin
```

Produces `src-tauri/target/<triple>/release/bundle/macos/Minion.app`. The dev
build shells out to a repo-local Python; for a standalone `.app` we need to
bundle the sidecar (PyInstaller) — see `TODO(sidecar-bundle)` in
`src-tauri/src/lib.rs`. Until then, the packaged app still needs a repo
checkout + venv.

## Automatic updates (Tauri updater)

Signed updates use **`tauri-plugin-updater`**. The app reads
`https://github.com/reif-is-a-foofie/Minion/releases/latest/download/latest.json`
(see `src-tauri/tauri.conf.json` → `plugins.updater`).

1. **Signing key** — generate once (`CI=1 npx tauri signer generate --ci -p "" -w src-tauri/updater/minion.key`).
   Put **`minion.key` in `.gitignore`** (already ignored) and store the same key in
   GitHub Actions as `TAURI_SIGNING_PRIVATE_KEY` (or `TAURI_SIGNING_PRIVATE_KEY_PATH`).
   Paste the **`.pub` contents** into `tauri.conf.json` → `plugins.updater.pubkey`
   (must match the private key used at build time).

2. **Build** — with the private key in the environment:

   ```bash
   export TAURI_SIGNING_PRIVATE_KEY_PATH="$PWD/src-tauri/updater/minion.key"
   npm run tauri build -- --target aarch64-apple-darwin
   npm run tauri build -- --target x86_64-apple-darwin
   ```

   Each build emits `Minion.app.tar.gz` and `.sig` under `src-tauri/target/.../bundle/macos/`.

3. **Publish** — upload both tarballs to a GitHub Release, then generate **`latest.json`**:

   ```bash
   python3 scripts/write_latest_json.py \
     --version 1.0.2 \
     --notes "…" \
     --darwin-aarch64-url "https://github.com/…/Minion_1.0.2_aarch64.app.tar.gz" \
     --darwin-aarch64-sig path/to/Minion_1.0.2_aarch64.app.tar.gz.sig \
     --darwin-x86_64-url "https://github.com/…/Minion_1.0.2_x64.app.tar.gz" \
     --darwin-x86_64-sig path/to/Minion_1.0.2_x64.app.tar.gz.sig \
     > latest.json
   ```

   Attach **`latest.json`** to the release as `latest.json` so the
   `releases/latest/download/latest.json` URL resolves.

Release builds prompt in **Settings → Support → Check for updates**; a
background check also runs ~18s after the app connects (production only).

## Connect any MCP client

The **Connect Claude Desktop** button merges Minion into
`~/Library/Application Support/Claude/claude_desktop_config.json` under
`mcpServers.minion`. For Cursor or other MCP clients, copy the same entry
shape from `chatgpt_mcp_memory/claude_desktop_config.example.json`.
