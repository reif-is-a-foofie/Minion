# ChatGPT Export → Claude Desktop MCP (Local)

This turns a freshly-downloaded ChatGPT export **ZIP** into:

- a few **pasteable persona** files (`core_profile.md`, `retrieval_policy.md`)
- an **on-demand semantic memory** MCP server (Claude Desktop calls tools like `search_memory`)

Nothing is uploaded. The index lives on local disk.

## 0) Prereqs (Intel macOS)

- Python 3.10+ recommended (`python3 --version`)
- Ollama installed + running (`ollama serve`)
- Pull the default model once: `ollama pull mistral:7b`
- Claude Desktop installed

### Recommended (non-technical friendly): `uv`

`uv` installs a modern Python and dependencies for you (no system Python/pip drama).

Install `uv` (macOS):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart your terminal so `uv` is on PATH.

## 1) Install dependencies

### Option A: `uv` (recommended)

From this folder:

```bash
uv python install 3.11
uv venv --python 3.11
uv pip install -r requirements.txt
```

### Option B: classic venv (depends on system Python)

From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 2) Ingest a ChatGPT export ZIP

Put the ChatGPT export zip somewhere (example: `~/Downloads/chatgpt-export.zip`), then:

```bash
source .venv/bin/activate
python src/ingest_chatgpt_export.py ~/Downloads/chatgpt-export.zip
```

This prints the **export root directory** it found (contains `conversations-*.json`).

## 3) Build the semantic index

Use the printed export root:

```bash
source .venv/bin/activate
python src/build_index.py --export "/path/printed/by/ingest"
```

Outputs:

- `data/derived/chunks.jsonl`
- `data/derived/embeddings.npy`
- `data/derived/manifest.json`

Optional: include assistant messages too:

```bash
python src/build_index.py --export "/path/to/export" --include-assistant
```

## 4) Build persona artifacts (optional but recommended)

```bash
source .venv/bin/activate
python src/persona_extract.py --export "/path/to/export"
```

Outputs:

- `data/derived/persona_sourcebook.md`
- `data/derived/persona_quote_bank.md`

## 5) Generate `core_profile.md` (recommended)

This produces a **derived** `core_profile.md` from the export-backed persona evidence (no hardcoded personality).

```bash
source .venv/bin/activate
python src/generate_core_profile.py --model mistral:7b
```

Outputs:

- `core_profile.md` (generated)
- `data/derived/core_profile_manifest.json` + `data/derived/core_profile.built` (build marker + metadata)

## 5b) `ask_minion` — chunk-native strategic profile (Claude agent workflow)

After `chunks.jsonl` exists (from step 3), you can synthesize a longer **strategic / identity** document from the **same chunks** the MCP searches—decisions, frameworks, beliefs, projects—via map→reduce and local Ollama:

```bash
source .venv/bin/activate
python src/ask_minion.py --derived-dir data/derived --model mistral:7b
```

Or from the repo / Homebrew CLI:

```bash
minion ask_minion --derived-dir "/path/to/derived" --model mistral:7b
```

Pilot on a subset (recommended before a full corpus):

```bash
python src/ask_minion.py --derived-dir data/derived --max-conversations 20 --dry-run
python src/ask_minion.py --derived-dir data/derived --max-conversations 50
```

Outputs:

- `data/derived/identity_profile.md` — ~800–1200 words, structured sections
- `data/derived/identity_profile_manifest.json` — model, filters, counts
- Copy also written to `agent/identity_profile.md` when the repo layout is present

This complements `core_profile.md` (persona from the quote bank / sourcebook). Paste either or both into Claude’s system context.

## 6) Pasteable persona for Claude

**MCP vs instructions:** `claude_desktop_config.json` (updated by `minion mcp-config` / setup) only **registers** the Minion server so **tools exist**. To get good **invocation** of `search_memory`, paste the files below into **Claude → Custom Instructions** (and/or project instructions).

In Claude, paste content from:

- `core_profile.md`
- `retrieval_policy.md` (includes **proactive** `search_memory` guidance)
- `identity_profile.md` (from `ask_minion` — same workflow, strategic layer over the chunk corpus)

Optionally also attach / paste selected sections from:

- `data/derived/persona_sourcebook.md`
- `data/derived/persona_quote_bank.md`

## 7) Wire it into Claude Desktop (MCP)

Claude Desktop reads (macOS):

- `~/Library/Application Support/Claude/claude_desktop_config.json`

**Default:** `minion setup` **writes this file for you** (merges the `chatgpt-memory-local` entry; backs up the previous file to `claude_desktop_config.json.minion.bak` when it existed).

To point at an existing index later (same paths as `minion setup` would use):

```bash
minion mcp-config --derived-dir "/path/to/derived"
```

That **merges** into `claude_desktop_config.json`—no copy-paste. Restart Claude Desktop after it runs.

- `--print-only` — print a JSON fragment only; **does not** write (for debugging).
- `--config-path` / env **`CLAUDE_DESKTOP_CONFIG`** — non-default config file location.
- `--server-name` — if `chatgpt-memory-local` collides with another server.
- `--quiet` — less output when writing; with `--print-only`, JSON only on stdout.

**Manual:** see `claude_desktop_config.example.json` (`command`, `args`, `CHATGPT_MCP_DATA_DIR`).

**Injected directions (no paste required for retrieval policy):** On MCP connect, the Minion server sends **`retrieval_policy.md`** in the protocol’s `initialize.instructions` field (so Claude sees *when* to call `search_memory`, not only that tools exist). The file must live next to your index: **`CHATGPT_MCP_DATA_DIR/retrieval_policy.md`**. `minion setup` and `minion mcp-config` copy it from this package into that folder. Override path with env **`CHATGPT_MCP_RETRIEVAL_POLICY`**; cap length with **`CHATGPT_MCP_INSTRUCTIONS_MAX_CHARS`** (default `20000`). You can still paste the same policy into Custom Instructions for emphasis, or paste only **`core_profile.md`** there to avoid duplication.

Restart Claude Desktop after any config change.

## 8) Verify inside Claude

Ask Claude:

- “Call `index_info`.”
- “Call `search_memory` for `Good Capital` with `top_k=6`.”
- “Take the top hit `chunk_id` and call `get_chunk`.”

## Privacy + token discipline (how this stays cheap)

- The full index stays on disk (`embeddings.npy`, `chunks.jsonl`).
- Claude only receives **top-k short snippets** (default `top_k=8`, `max_chars=900`).

## Future sources (extensibility)

Add new `ingest_*.py` scripts that convert a source into the same internal message/chunk format, then reuse `build_index.py`.

Suggested connector pattern:

- Add `src/ingest_<source>.py` that outputs either:
  - an unzipped “raw” directory under `data/raw/`, plus a manifest, or
  - a normalized JSONL of messages/documents
- Keep the indexing surface stable:
  - `build_index.py` consumes the export directory (or future normalized format) and always writes:
    - `data/derived/chunks.jsonl`
    - `data/derived/embeddings.npy`
    - `data/derived/manifest.json`
- Keep privacy defaults:
  - local disk only
  - MCP over stdio (no ports)
  - strict `top_k` + `max_chars` caps

## Packaging: Minion (macOS)

This project is intended to be packaged as a macOS app/binary called **Minion**, so non-technical users don’t need Python.

See `scripts/build_macos.sh` (builds a local `dist/minion-mcp` executable you can point Claude Desktop at).

When using the packaged binary, your Claude Desktop config `command` should point to:

- `.../chatgpt_mcp_memory/dist/minion-mcp`

