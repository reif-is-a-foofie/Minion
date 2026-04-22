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

---

## Install (macOS app)

1. Download the latest **Minion** build from [**GitHub Releases**](https://github.com/reif-is-a-foofie/Minion/releases) (or build from source — below).
2. Open the `.app` and, when macOS prompts you, move it to **Applications** (avoid running forever from the disk image or a translocated copy; that can confuse paths and file access).
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
