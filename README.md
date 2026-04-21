# What the heck is this thing and why should I care about it?

Well, like me, you are probably realizing by now that you have wasted a lot of your life reading slop from ChatGPT when you could have been making progress with Claude.

I made my move to Claude recently and I realized, well, I have my whole life on GPT. That thing knows more about me than my doctor, my shrink, and Siri combined times 6. And Claude doesn't know me from Adam other than it knows I'm not Adam. At least, most likely not Adam.

OpenAI lets you export all of your chats (takes a few days). Then I needed to push that context back into Claude. But how?? I can't just load 30 million messages into Claude (real message count BTW, wild). And given Claude spends more tokens than a kid at nickel-mania who just drank his first Red Bull, loading the whole thing was off the table.

So we go the encoding + semantic search route. Hook it up to an MCP that runs locally, so at run time Claude can query my personal data and ONLY take in the context that matters.

It's alpha. Stretch alpha hard enough and bad things could happen. But at least I am committed to using my own software on my own personal setup.

Ping me with questions,

Reif

-reif@thegoodproject.net

## What Claude actually gets

Two things, automatically, on every session:

1. **Retrieval**. An MCP server (this repo) that lets Claude search your chat history and any file you drop into the inbox. It exposes eight tools: `ask_minion` (semantic **plus** keyword fusion over the same chunks, with a filename-aware rerank so a dropped `*_roster*.csv` can surface when you say “u9”), `get_chunk`, `browse_conversations`, `conversation_chunks`, `list_sources`, `index_info`, plus the voice tools below. Claude decides when to call them. Indexed paths are usually the **inbox copy** of a file, not your original `~/Desktop/...` path—use `list_sources` or basename tokens in `ask_minion` / `path_glob` when you mean a specific drop.
2. **Voice**. A durable `voice.md` profile (preferences, nevers, style rules, writers you want emulated) that gets injected into Claude's system prompt every session. You don't write it. On first run, Claude reads your chat history, figures out how you actually write, and commits the profile. From then on, if you state a new rule mid-conversation ("actually, I hate bullet lists"), Claude confirms and appends it via `append_to_voice`. The profile evolves from the chats, not from a text editor.

The voice tool (`commit_voice` on first run, `append_to_voice` after) is what turns "Claude knows me" from a one-time paste into something that actually stays current.

## Install

Clone it, set up Python, done. Homebrew formula is dormant for now, running from the repo is the path.

Prereqs: Python 3.10+, [Ollama](https://ollama.com) running with whatever model you want for the profile generator (default `mistral:7b`), Claude Desktop installed.

```bash
git clone https://github.com/reif-is-a-foofie/Minion.git
cd Minion/chatgpt_mcp_memory

# uv is the cleanest way, works even if your system Python is a mess
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.11
uv venv --python 3.11
uv pip install -r requirements-all.txt

# put bin/minion on your PATH or just alias it
alias minion="$PWD/../bin/minion"
minion doctor
```

Classic venv works too if you already have a Python 3.10+ you trust. See `chatgpt_mcp_memory/README.md` for the minimal requirements split (core vs PDFs vs images vs audio vs code).

## Run it

Type `minion`, hit enter. That's the whole interface. It asks you for the path to your ChatGPT export zip, unpacks it, builds the index, pulls persona evidence, writes `core_profile.md`, and merges the MCP entry into Claude Desktop's config (with a backup). One command, one pipeline.

Non-interactive version for scripts:

```bash
minion setup /path/to/chatgpt-export.zip
```

After it finishes: **quit and reopen Claude Desktop** so it picks up the MCP server. First session with the new config does the voice bootstrap silently in the background. Ask Claude something like "what do you know about me?" and it will call `ask_minion` and tell you.

Paste `core_profile.md` and `retrieval_policy.md` from your run folder into Claude's Custom Instructions if you want the strategic layer pinned. Optional but recommended.

## Drop stuff in

There is a **desktop app** (Tauri + SvelteKit) over the same FastAPI sidecar and SQLite + sqlite-vec store as the CLI. Release builds **bundle the Python source** into the `.app` and, on first launch, create a **venv under your data dir** and `pip install` what they need—no separate repo checkout for day-to-day use. Drop files onto the window; watch the activity log (embedding progress eases instead of jumping); open **Contents** to search; **Settings** shows the exact **`MINION_DATA_DIR` / `MINION_INBOX`** paths and a **Connect** button that writes both into Claude Desktop’s MCP entry (mirror those env vars in Cursor or any other MCP host so searches hit the same index the app just wrote).

```bash
cd desktop
npm install
npm run tauri dev
```

`npm run tauri build` produces `Minion.app` and a `.dmg` under `desktop/src-tauri/target/release/bundle/`.

Without the app, drop files into `data/inbox/`. The watcher inside `minion mcp` reconciles on startup and then live-watches. CRUD commands: `minion add`, `minion ls`, `minion rm`, `minion watch`.

See [`desktop/README.md`](./desktop/README.md) for the app, [`chatgpt_mcp_memory/README.md`](./chatgpt_mcp_memory/README.md) for everything else (parsers, tool surface table, voice internals, env knobs).

## Privacy

Nothing leaves the machine. The index is a single SQLite file (`memory.db`) with sqlite-vec for KNN. MCP runs over stdio, no network ports. The only network-adjacent thing is optional Ollama image captioning, off unless `MINION_VISION_MODEL` is set. Keep your raw export and derived artifacts outside the repo (I use a sibling `minion_private/` folder).

## What you actually get out of this

A useful "you" profile rebuilt from your own writing, not a personality you hand-authored. An assistant that stays consistent over time without you restuffing history into every chat. Real "how you said it" evidence when you're writing or deciding or delegating. A voice that sharpens itself session over session instead of drifting.

Auditable all the way down: every profile is generated from quotes, every voice directive ties back to a chunk, every build writes a manifest.
