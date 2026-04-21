# Minion: Local-First Memory MCP for Claude Desktop

Minion turns a folder on disk into Claude's long-term memory. Drop a file into
`data/inbox/`—PDF, image, audio, code, ChatGPT export, plain text—and Minion
parses, embeds, and exposes it to Claude via MCP. Nothing leaves the machine.

What's indexable today:

| Kind | Extensions | Parser |
| ---- | ---------- | ------ |
| Text / Markdown / structured | `.md .txt .rst .org .csv .json .yaml .toml .ini .log` | stdlib |
| HTML | `.html .htm` | `trafilatura` (boilerplate-stripped) |
| PDF | `.pdf` | `pypdf` with `pdfminer.six` fallback |
| DOCX | `.docx` | `python-docx` |
| Image | `.png .jpg .webp .bmp .tif …` | `rapidocr-onnxruntime` OCR (+ optional Ollama `llava` caption) |
| Audio / video | `.mp3 .wav .m4a .mp4 .webm …` | `faster-whisper` (`tiny.en` default) |
| Source code | `.py .js .ts .go .rs .java .c .cpp .rb .php …` | `tree-sitter-language-pack` (function/class chunks) |
| ChatGPT export | `.zip` or unzipped folder | built-in (same as legacy `build_index.py`) |

The storage layer is a single SQLite file (`memory.db`) using
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) for vector KNN. Adding,
updating, and deleting sources are atomic—no full rebuild required.

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

Requirements are split so you only pull in what you need:

| File | What it adds |
| ---- | ------------ |
| `requirements.txt` | core: numpy, fastembed, sqlite-vec, watchdog, ollama, tqdm |
| `requirements-docs.txt` | +PDF / DOCX / HTML parsers |
| `requirements-images.txt` | +image OCR (rapidocr, pure Python) |
| `requirements-audio.txt` | +faster-whisper transcription |
| `requirements-code.txt` | +tree-sitter for code-aware chunking |
| `requirements-all.txt` | everything above |

### Option A: `uv` (recommended)

From this folder:

```bash
uv python install 3.11
uv venv --python 3.11
uv pip install -r requirements-all.txt   # or just requirements.txt for core only
```

### Option B: classic venv (depends on system Python)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-all.txt
```

## 2) Drop files into the inbox (recommended)

The fastest path for arbitrary files is the watched inbox. Minion reconciles
the inbox on MCP startup and then live-watches it.

```bash
mkdir -p data/inbox
cp ~/Desktop/meeting-notes.md data/inbox/
cp ~/Downloads/contract.pdf    data/inbox/
cp ~/Screenshots/whiteboard.png data/inbox/
cp ~/Recordings/standup.mp3    data/inbox/
```

The watcher starts automatically inside `minion mcp`. To run it standalone
(useful while iterating):

```bash
source .venv/bin/activate
python src/watcher.py --data-dir data/derived --verbose
# or from anywhere:
minion watch
```

CRUD without the inbox:

```bash
minion add  ~/path/to/file.pdf another/file.md   # explicit ingest
minion ls   --kind pdf                           # list sources
minion rm   ~/path/to/file.pdf                   # delete (path or src-...)
minion watch --once                              # one-shot reconcile, exit
```

Unchanged files (matching sha256) are skipped. Modified files replace their
prior chunks+embeddings atomically. Deleted files are reaped on the next
reconcile.

## 3) Ingest a ChatGPT export ZIP (optional, legacy path)

Put the ChatGPT export zip somewhere (example: `~/Downloads/chatgpt-export.zip`), then:

```bash
source .venv/bin/activate
python src/ingest_chatgpt_export.py ~/Downloads/chatgpt-export.zip
```

This prints the **export root directory** it found (contains `conversations-*.json`).

## 4) Build the semantic index (ChatGPT export fast path)

Use the printed export root:

```bash
source .venv/bin/activate
python src/build_index.py --export "/path/printed/by/ingest"
```

Outputs:

- `data/derived/memory.db` (SQLite + sqlite-vec, the live index)
- `data/derived/manifest.json` (legacy, kept for introspection tools)

Optional: include assistant messages too:

```bash
python src/build_index.py --export "/path/to/export" --include-assistant
```

### Migrating an existing flat-file index

If you previously ran an older Minion that produced `chunks.jsonl` +
`embeddings.npy`, upgrade in place:

```bash
python src/migrate_to_sqlite.py --derived-dir data/derived
```

Or just start the MCP / watcher — `mcp_server.py` auto-migrates on first
launch when it sees the legacy files and no `memory.db`.

## 5) Build persona artifacts (optional but recommended)

```bash
source .venv/bin/activate
python src/persona_extract.py --export "/path/to/export"
```

Outputs:

- `data/derived/persona_sourcebook.md`
- `data/derived/persona_quote_bank.md`

## 6) Generate `core_profile.md` (recommended)

This produces a **derived** `core_profile.md` from the export-backed persona evidence (no hardcoded personality).

```bash
source .venv/bin/activate
python src/generate_core_profile.py --model mistral:7b
```

Outputs:

- `core_profile.md` (generated)
- `data/derived/core_profile_manifest.json` + `data/derived/core_profile.built` (build marker + metadata)

## 6b) `ask_minion` — chunk-native strategic profile (Claude agent workflow)

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

## 7) Pasteable persona for Claude

**MCP vs instructions:** `claude_desktop_config.json` (updated by `minion mcp-config` / setup) only **registers** the Minion server so **tools exist**. To get good **invocation** of `ask_minion`, paste the files below into **Claude → Custom Instructions** (and/or project instructions).

In Claude, paste content from:

- `core_profile.md`
- `retrieval_policy.md` (includes **proactive** `ask_minion` guidance)
- `identity_profile.md` (from `ask_minion` CLI — same workflow, strategic layer over the chunk corpus)

Optionally also attach / paste selected sections from:

- `data/derived/persona_sourcebook.md`
- `data/derived/persona_quote_bank.md`

## 7b) Voice profile (self-bootstrapping)

Minion maintains a durable **voice profile** at
`<derived>/voice.md` — your style rules, nevers, reference writers, and
exemplars. It is auto-injected into `initialize.instructions` every
session, so Claude respects your voice without you pasting anything.

**How it gets built:** the profile is *not* hand-authored. On first run
(when `voice.md` is empty / stubbed), the server injects a **bootstrap
directive** instead of the profile. That directive instructs Claude to:

1. Run 6–8 HyDE-style semantic queries against your own chats
   (`ask_minion` with `mode='relevance'`, `role='user'` — e.g. *"don't
   use emojis"*, *"write like Ted Chiang"*, *"explain in paragraphs not
   bullets"*) to find evidence of how you actually write.
2. Also run one keyword pass for named authors / references.
3. Synthesize a structured voice document (preferences, nevers, style,
   tone, reference writers, exemplars) grounded in chunk_ids it found.
4. Call **`commit_voice`** once to persist it.

From then on, every new Claude session starts with your voice injected.
The profile is layered: `AUTO_DRAFT` (Claude-generated, replaceable) +
`USER_EDITS` (your hand-written overrides, never touched by Claude).

**Ongoing maintenance:** during any session, if Claude hears a durable
preference from you (*"actually, I hate bullet lists — always paragraphs"*),
it confirms with you and then calls **`append_to_voice`** to add that
directive to the appropriate section. No manual file editing required;
the profile evolves with the chats.

Schema, section headings, and safety rails (length caps, idempotency,
section allow-list) live in `src/build_voice.py`.

## 8) Wire it into Claude Desktop (MCP)

Claude Desktop reads (macOS):

- `~/Library/Application Support/Claude/claude_desktop_config.json`

**Default:** `minion setup` **writes this file for you** (merges the `minion` entry; backs up the previous file to `claude_desktop_config.json.minion.bak` when it existed).

To point at an existing index later (same paths as `minion setup` would use):

```bash
minion mcp-config --derived-dir "/path/to/derived"
```

That **merges** into `claude_desktop_config.json`—no copy-paste. Restart Claude Desktop after it runs.

- `--print-only` — print a JSON fragment only; **does not** write (for debugging).
- `--config-path` / env **`CLAUDE_DESKTOP_CONFIG`** — non-default config file location.
- `--server-name` — if `minion` collides with another server.
- `--quiet` — less output when writing; with `--print-only`, JSON only on stdout.

**Manual:** see `claude_desktop_config.example.json` (`command`, `args`, `CHATGPT_MCP_DATA_DIR`).

**Injected directions (no paste required):** On MCP connect, the Minion server sends three things in the protocol's `initialize.instructions` field so Claude sees *when* to call the tools and *how you want to be written to*:

1. **`retrieval_policy.md`** — proactive `ask_minion` guidance. Must live next to your index (**`CHATGPT_MCP_DATA_DIR/retrieval_policy.md`**); `minion setup` / `mcp-config` copy it from this package. Override path with env **`CHATGPT_MCP_RETRIEVAL_POLICY`**; cap length with **`CHATGPT_MCP_INSTRUCTIONS_MAX_CHARS`** (default `20000`).
2. **`voice.md`** — your durable voice profile (preferences, nevers, style). Auto-injected when built, otherwise a short **bootstrap directive** is injected instead, telling Claude to synthesize your voice from chat history on first run (see §7b).
3. **Core profile** — if **`MINION_PROFILE`** points at a file, its contents are attached once per session.

You can still paste the same policy + `core_profile.md` into Custom Instructions for emphasis.

Restart Claude Desktop after any config change.

## 9) Verify inside Claude

Ask Claude:

- "Call `index_info`." — shows chunk / source counts, db path, inbox path.
- "Call `list_sources` with `kind='pdf'`." — confirms a recently-dropped file.
- "Call `ask_minion` for `Good Capital` with `top_k=6`."
- "Take the top hit `chunk_id` and call `get_chunk`."
- "Call `browse_conversations` with `title_like='profile'`, then call `conversation_chunks` on the top hit."

### Full tool surface (8)

| Tool | Purpose |
| ---- | ------- |
| `ask_minion` | Semantic + keyword + temporal search. Modes: `relevance` (default), `keyword` (FTS, for proper nouns), `temporal` (`first`/`last`). Filters: `kind`, `path_glob`, `since`, `role`. |
| `get_chunk` | Fetch a full chunk by `chunk_id` (expand a search hit). |
| `browse_conversations` | List distinct conversations; filter by `title_like`, `since`, `until`; order newest/oldest. |
| `conversation_chunks` | Fetch every chunk in one conversation in order (whole-thread view). |
| `list_sources` | **Two modes:** list (`kind`/`path_glob`/`since` filters) **or** detail (pass `source_id` for full metadata: parser, sha256, bytes, chunk_count). |
| `index_info` | Aggregate db stats (chunk/source counts, paths). Diagnostic. |
| `commit_voice` | One-shot: persist Claude's synthesized voice profile to `voice.md` (called during bootstrap — see §7b). |
| `append_to_voice` | Mid-session: append one directive to a section of `voice.md` after user confirmation. |

`ask_minion` supports `kind`, `path_glob`, `since`, and `role` filters plus
a `mode` parameter for precise retrieval.

## Privacy + token discipline (how this stays cheap)

- The full index stays on disk (`memory.db`, a single SQLite file).
- Claude only receives **top-k short snippets** (default `top_k=8`, `max_chars=900`).
- MCP runs over stdio (no network ports); optional Ollama captioning is the
  only network-adjacent call and is off unless `MINION_VISION_MODEL` is set.

## Adding a new file type

Each parser is a single file under `src/parsers/` that returns
`ParseResult(chunks=[...], kind, parser, source_meta)`. To support a new
format:

1. Drop a module at `src/parsers/<yourfmt>.py` exporting `def parse(path: Path) -> ParseResult`.
2. Register its extensions in `src/parsers/__init__._EXT_REGISTRY`.
3. If it needs a heavy dep, add it to a new `requirements-<yourfmt>.txt` and
   `import` it lazily inside `parse()` so core installs stay tiny.

The ingest pipeline (`src/ingest.py`), watcher, MCP, and CLI will pick up the
new kind automatically.

## Environment knobs

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `MINION_DATA_DIR` / `CHATGPT_MCP_DATA_DIR` | repo `data/derived` | Where `memory.db` and `voice.md` live |
| `MINION_INBOX` | `<MINION_DATA_DIR>/../inbox` | Watched folder |
| `MINION_DISABLE_WATCHER` | unset | Set to `1` to skip auto-watch inside `minion mcp` |
| `MINION_EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | fastembed model name |
| `MINION_WHISPER_MODEL` | `tiny.en` | faster-whisper model for audio |
| `MINION_VISION_MODEL` | unset | Ollama model name for image captioning (e.g. `llava`) |
| `MINION_RETRIEVAL_POLICY` / `CHATGPT_MCP_RETRIEVAL_POLICY` | `<data>/retrieval_policy.md` | Override policy path |
| `CHATGPT_MCP_INSTRUCTIONS_MAX_CHARS` | `20000` | Cap for injected `initialize.instructions` |
| `MINION_VOICE` | `<data>/voice.md` | Override voice-profile path |
| `MINION_VOICE_MAX_CHARS` | `5000` | Cap for voice-profile injection |
| `MINION_PROFILE` | unset | File path to a core profile, auto-attached on first tool call |

## Packaging: Minion (macOS)

This project is intended to be packaged as a macOS app/binary called **Minion**, so non-technical users don’t need Python.

See `scripts/build_macos.sh` (builds a local `dist/minion-mcp` executable you can point Claude Desktop at).

When using the packaged binary, your Claude Desktop config `command` should point to:

- `.../chatgpt_mcp_memory/dist/minion-mcp`

