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

1. Install Minion (Homebrew or from this repo).
2. In Terminal, run **`minion`** with nothing after it (or `minion start`).
3. When asked, paste the path to your **ChatGPT export `.zip`** from OpenAI (or drag the file into the window).
4. Wait while it unpacks, builds search, and writes your profile.
5. Follow the on-screen steps to paste the profile into Claude and connect Claude Desktop.

Technical details and manual steps: `chatgpt_mcp_memory/README.md`.

## Install (internal teammates)

If you’re on a Mac and have Homebrew:

```bash
brew tap reif-is-a-foofie/minion
brew install minion
minion doctor
```

To run the full setup from an export ZIP (creates a private workspace in `~/minion_private`):

```bash
minion setup --export-zip "/path/to/chatgpt-export.zip"
```

## What you do with it

- Rebuild a usable “you” profile from your own writing (not hand-authored personality)
- Keep an assistant consistent over time without stuffing long history into every chat
- Pull up real “how you said it” evidence when writing, deciding, or delegating (projects, priorities, preferences)
- Make the profile auditable: it’s generated from quotes + emits a build manifest so you can see how it was produced

## Privacy

- This repo is meant to stay clean-by-default (code + docs only).
- Your raw export, derived embeddings, and quote banks are generated locally and kept out of git.
- Recommended: keep private artifacts outside the repo entirely (e.g. a sibling `minion_private/` folder).

## Quick start (high level)

1) Download your ChatGPT data export
2) Ingest/unzip it locally (nothing uploaded)
3) Build two things from it:
   - a semantic memory index (fast lookup later)
   - persona evidence (sourcebook + quote bank)
4) Generate `core_profile.md` from that evidence using a local LLM (Ollama)
   - this also writes a build marker + manifest in `data/derived/`
5) Use:
   - `core_profile.md` as stable context
   - memory search when you need grounded past details
6) Optional — chunk-native strategic profile (local Ollama): `minion ask_minion --derived-dir …` or see `chatgpt_mcp_memory/README.md` (`ask_minion` writes `identity_profile.md` from `chunks.jsonl`).

In Claude, combine `core_profile.md` with `chatgpt_mcp_memory/retrieval_policy.md` (it tells the model to call `search_memory` **proactively**, not only on explicit recall requests).

Technical instructions live in `chatgpt_mcp_memory/README.md`.

