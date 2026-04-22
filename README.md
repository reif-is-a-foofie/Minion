# Minion

**Minion** is a macOS desktop app that turns your machine into **private, searchable long-term memory** for AI assistants. Drop exports, PDFs, notes, media, and code into one inbox; everything is chunked, embedded, and stored locally in SQLite. **Claude Desktop** (and any other MCP client) can call Minion over stdio to search that archive, browse conversations, and maintain an evolving **voice** profile—without sending your corpus to a hosted service.

---

## Screenshots

| Main window — drop zone and ingest | Activity — live parse / embed log |
|:---:|:---:|
| ![Minion main window: drop files or folders, supported types, Settings](docs/readme/main-window.png) | ![Activity log showing ingested markdown files and chunk counts](docs/readme/activity-log.png) |

| Claude Desktop — Minion MCP in a real thread | macOS — Minion in Launchpad |
|:---:|:---:|
| ![Claude using Minion tools to answer from indexed memory](docs/readme/claude-desktop-memory.png) | ![Minion app icon in the macOS Launchpad grid](docs/readme/macos-launchpad.png) |

**Preferences** — Status (sources, chunks, sidecar, paths), **Claude (MCP)** one-click config, and **Ingest & file types**:

| Status | Claude (MCP) | Ingest & types |
|:---:|:---:|:---:|
| ![Preferences: Status tab with sources, chunks, inbox watch, sidecar](docs/readme/preferences-status.png) | ![Preferences: Claude MCP tab with Add to Claude and success message](docs/readme/preferences-claude-mcp.png) | ![Preferences: Ingest and file types with toggles per format](docs/readme/preferences-ingest.png) |

---

## What you get

- **Local index** — One `memory.db` (plus vectors) under `~/Library/Application Support/Minion/data` by default. Override with `MINION_DATA_DIR` if needed.
- **Drop zone + watcher** — Drag files or folders onto the app (or use **Choose files…**). The sidecar ingests in the background; the **Activity** list shows progress and chunk counts.
- **MCP tools for Claude** — Semantic / keyword / temporal **`ask_minion`**, **`get_chunk`**, conversation helpers, **`index_info`**, voice tools (`commit_voice` / `append_to_voice`), and identity helpers where enabled. Claude chooses when to call them.
- **Settings hub** — Restart the Python sidecar, inspect API and DB paths, tune which file kinds are ingested, and **Add to Claude** to merge Minion into `claude_desktop_config.json`.

For tool tables, parsers, env flags, and CLI-only workflows, see **[`chatgpt_mcp_memory/README.md`](./chatgpt_mcp_memory/README.md)**. For Tauri architecture and `tauri dev` / `tauri build`, see **[`desktop/README.md`](./desktop/README.md)**.

### Where the app lives in this repo (on GitHub)

All of it is in **this monorepo**—nothing is shipped from a private subtree:

| Path | What it is |
|------|----------------|
| **[`desktop/`](https://github.com/reif-is-a-foofie/Minion/tree/main/desktop)** | macOS app: **SvelteKit** UI + **Tauri 2** Rust shell (`src-tauri/`). |
| **[`chatgpt_mcp_memory/`](https://github.com/reif-is-a-foofie/Minion/tree/main/chatgpt_mcp_memory)** | **Python** sidecar (FastAPI, ingest, SQLite, MCP). |

On `tauri build`, **`desktop/src-tauri/scripts/sync_sidecar.sh`** copies `chatgpt_mcp_memory`’s `src/` and `requirements*.txt` into `desktop/src-tauri/resources/sidecar/`. That `sidecar/` folder is **gitignored** (generated each build); the canonical source is always **`chatgpt_mcp_memory/`** on GitHub.

---

## Install (macOS app)

### 1. Pick the right download (Intel vs Apple Silicon)

On [**GitHub Releases**](https://github.com/reif-is-a-foofie/Minion/releases) there are **two** macOS downloads. **Read the file name**—it says which kind of Mac it is for (no “arm64” / “x64” jargon):

| Download file name contains… | Use for | How to tell on the Mac |
|------------------------------|---------|---------------------------|
| **`…-macOS-Apple-Silicon.zip`** | **Apple Silicon** — M1, M2, M3, M4, … | Apple menu → **About This Mac** → **Chip:** “Apple M2” (or any Apple M…). |
| **`…-macOS-Intel.zip`** | **Intel** — Core i5 / i7 / i9, Xeon, … | **About This Mac** → **Processor:** line includes **Intel**. |

If you pick the wrong one, macOS may refuse to open the app or show an architecture error. Delete **Minion.app**, download the **other** zip, unzip, and drag **Minion.app** to **Applications** again.

### 2. Install and run

1. Download the matching zip from [**GitHub Releases**](https://github.com/reif-is-a-foofie/Minion/releases) (or build from source — below).
2. Unzip, then move **Minion.app** to **Applications** when prompted (avoid running forever from the disk image or a translocated copy; that can confuse paths and file access).
3. **First launch** can take a few minutes while Minion prepares its embedded Python environment and starts the sidecar. Later launches are quick.
4. In Minion, open **Settings → Claude (MCP)** and click **Add to Claude**. Then **fully quit and reopen Claude Desktop** so it loads the new MCP entry.
5. Optional: import a **ChatGPT export** (zip or folder) via the drop zone so the index has history on day one.

---

## Build from source (developers)

```bash
git clone https://github.com/reif-is-a-foofie/Minion.git
cd Minion/chatgpt_mcp_memory
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cd ../desktop
npm install
npm run tauri dev
```

The Rust shell prefers `../chatgpt_mcp_memory/.venv/bin/python`. Release builds produce `desktop/src-tauri/target/release/bundle/macos/Minion.app`; bundling details may evolve — see `desktop/README.md` and `desktop/src-tauri/` notes.

---

## CLI (`minion` command)

For a **terminal-first** setup (export path, `minion doctor`, `minion setup`, inbox CRUD without the GUI), use the launcher in **`bin/minion`** and the instructions in **`chatgpt_mcp_memory/README.md`**. The desktop app and the CLI share the same store and MCP server code.

---

## Privacy

Indexing and search run **on your machine**. MCP speaks **stdio** to Claude Desktop by default—no cloud “memory service” in the loop for your chunks. Optional components (for example **Ollama** for voice synthesis, or **HF Hub** for some embedding downloads) only touch the network if you configure them; keep exports and large corpora **outside** the git tree if you prefer (e.g. a sibling folder).

---

## Credits

Built and dogfooded by **Reif** — questions: [reif@thegoodproject.net](mailto:reif@thegoodproject.net).
