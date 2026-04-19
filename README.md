# What the heck is this thing and why should I care about it?

Well, like me, you are probably realizing by now that you have wasted a lot of your life reading slop from ChatGPT when you could have been making progress with Claude.
I made my move to Claude recently and I realized, well, I have my whole life on GPT. That thing knows more about me than my doctor, my shrink, and Siri combined times 6.
And Claude doesn't know me from Adam other than it knows I'm not Adam. At least... most likely not Adam.

In any case, OpenAI allows you to export all of your chats. It takes a few days.
Then I needed to push that (context) back into Claude.
But how?? I can’t just load 30 million messages into Claude (real message count BTW, wild).
And given Claude spends more tokens than a kid at nickel-mania who just drank his first Red Bull....

I needed a solution.

Naturally we go the encoding + semantic search route. Hook it up to an MCP that runs locally — so at run time, Claude can query my personal data and ONLY take in the context that matters.

It’s alpha. If you stretch alpha hard enough — let’s be honest — bad things could happen. But at least I am committed to using my own software on my own personal setup.

Ping me with questions —

Reif

-reif@thegoodproject.net


## Easiest way (guided)

**Prefer the interactive flow:** open Terminal, type **`minion`**, press Enter — no subcommands, no flags. It starts a short **prompted setup** (paste your export path, confirm where files go, pick your Ollama model or accept the default). Same entry also works as **`minion start`**, **`minion go`**, or **`minion wizard`**.

**One run does the whole pipeline** — you are not supposed to chase five separate scripts. After you point Minion at your export, it unpacks, builds the search index, pulls persona quotes, calls your **local** Ollama model to write `core_profile.md`, and **merges the MCP entry into Claude Desktop’s config** for you (with a backup of the old file).

1. Install Minion (Homebrew or from this repo). Have **Ollama** installed with the model you use for the profile (default `mistral:7b`).
2. Run **`minion`** (interactive, as above). **Or** for scripts/CI: `minion /path/to/export.zip` when that path exists (same as `minion setup …`), or `minion setup /path/to/export.zip` / `minion setup --export …`.
3. When asked, paste the path to your **ChatGPT export `.zip`** from OpenAI (or drag the file into the window).
4. Wait until it finishes (this can take a while on a big export).
5. **You:** put text **into Claude itself** so the model knows how to behave — not just “connecting wires.” Minion copies **`retrieval_policy.md`** next to your profile in each run folder; paste **both** that file and **`core_profile.md`** into **Claude → Custom Instructions** (and/or project instructions). That tells Claude **when to call** `search_memory` (MCP), not only that the tool exists.
6. **Quit and reopen Claude Desktop** so it loads the MCP server from the config Minion already wrote.

**Two different mechanisms:** (1) **`claude_desktop_config.json`** — Minion merges this so Claude Desktop **starts** the Minion MCP and exposes **tools**. (2) **Directions to the model** — the MCP server **injects `retrieval_policy.md`** via the MCP handshake (`initialize.instructions`), so Claude gets retrieval discipline automatically when the server connects; you should still paste **`core_profile.md`** into Custom Instructions (and optionally the policy again if you want it duplicated there).

Strategic **`identity_profile.md`** (`minion ask_minion`) is the chunk-synthesized layer you add to that same Claude-side workflow when you want it — see `chatgpt_mcp_memory/README.md`.

## Install (internal teammates)

If you’re on a Mac and have Homebrew:

```bash
brew tap reif-is-a-foofie/minion
brew install minion
minion doctor
```

Maintainers: bumping the tap after a release is documented in [docs/homebrew.md](docs/homebrew.md).

To run the **same full pipeline** non-interactively (creates a workspace; default is `~/minion_private` unless you pass `--workspace`):

```bash
minion setup "/path/to/chatgpt-export.zip"
# equivalent:
minion setup --export "/path/to/chatgpt-export.zip"
```

(`--export` and `--export-zip` are the same flag.)

## What you do with it

- Rebuild a usable “you” profile from your own writing (not hand-authored personality)
- Keep an assistant consistent over time without stuffing long history into every chat
- Pull up real “how you said it” evidence when writing, deciding, or delegating (projects, priorities, preferences)
- Make the profile auditable: it’s generated from quotes + emits a build manifest so you can see how it was produced

## Privacy

- This repo is meant to stay clean-by-default (code + docs only).
- Your raw export, derived embeddings, and quote banks are generated locally and kept out of git.
- Recommended: keep private artifacts outside the repo entirely (e.g. a sibling `minion_private/` folder).

## What actually runs (so you know it’s one pipeline)

If you use **`minion`** (wizard) or **`minion setup --export …`**, Minion runs this in order:

1. Ingest / unzip the export (nothing uploaded)
2. Build the semantic index (`chunks.jsonl` + embeddings)
3. Extract persona evidence (sourcebook + quote bank)
4. Generate **`core_profile.md`** with local Ollama + write build marker / manifest under that run’s `derived/`, and **copy `retrieval_policy.md`** into that run folder (for you to paste into Claude)
5. **Merge** the memory MCP into **Claude Desktop’s** `claude_desktop_config.json` (backup saved as `*.minion.bak` when needed)

After that: **restart Claude Desktop** (MCP config), then paste **`core_profile.md`** + **`retrieval_policy.md`** (copied into your run folder by setup) into **Custom Instructions**. Add **`identity_profile.md`** from `ask_minion` to the same workflow when generated — see `chatgpt_mcp_memory/README.md`.

