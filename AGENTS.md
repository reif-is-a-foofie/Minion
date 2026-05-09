# Agent playbook for Minion

Short notes for any agent (me, future-me, another model) working on this repo.
Stay surgical, ship working diffs, keep the feedback loop intact.

## The feedback loop — read this before changing retrieval

Every search and every ingest writes one JSONL line to:

```
~/Library/Application Support/Minion/data/telemetry.jsonl
```

(the path is `<data_dir>/telemetry.jsonl`; `$MINION_DATA_DIR` overrides.)

Events are cheap, append-only, rotated at 10 MB. Two shapes today:

- `{"kind":"search", "mode":"relevance", "query":..., "returned":..., "top_score":..., "top_path":..., "rerank":"rrf"|"none", "content_dropped":..., "hit_kinds":[...]}`
- `{"kind":"ingest", "path":..., "file_kind":..., "parser":..., "chunks":..., "skipped":..., "reason":..., "result":...}`

### How to use the log when improving the system

Before touching retrieval or parsing, tail the log:

```
tail -n 200 "$HOME/Library/Application Support/Minion/data/telemetry.jsonl" | jq .
```

Patterns to look for:

- **Weak top hits**: lots of `search` rows with `top_score < 0.45`. The query
  shape is probably wrong for the corpus, or the right source isn't indexed.
- **Fusion disagreements**: `rerank=rrf` rows where `top_kind` flips between
  runs of the same query — that usually means a keyword-only artifact sneaked
  to the top. Revisit `semantic_weight` in `_rrf_fuse`.
- **Silent skips**: a burst of `ingest` rows with the same `reason` (e.g.
  `deferred: awaiting vision model`, `unsupported`, `parse-error: ...`) is a
  parser or dependency regression.
- **Content-dedup pressure**: `content_dropped >= returned` means the corpus
  has heavy duplication at query time; probably multiple copies of the same
  export ingested.

### How retrieval is wired (as of this commit)

`ask_minion` (in `chatgpt_mcp_memory/src/mcp_server.py`):

1. Mode `relevance` runs semantic KNN over sqlite-vec.
2. If FTS5 is available and the query is non-empty, a parallel keyword pass
   runs with the same filters.
3. The two lists are fused via weighted Reciprocal Rank Fusion
   (`semantic_weight=1.5`, `k=60`). Semantic copy wins on overlapping chunks
   so the displayed `score` is the real cosine.
4. Results are deduped by `source_id` first, then by content fingerprint
   (first-400-char SHA-1, whitespace-normalized) to collapse near-dupes
   across different sources.
5. Telemetry fires once per call with the top hit and a hit-kind summary.

Keep these invariants when you change anything:

- Telemetry must never raise into the caller. It's best-effort.
- `_content_fingerprint` is keyed by *text shape*, not id. Don't hash ids.
- When you widen the candidate pool, `internal_k` scales with `top_k`; don't
  let it blow past a few hundred without batching.
- The `ask_minion` tool description is load-bearing: Claude reads it to
  decide *whether* to search. Edit with care, diff in a separate commit so
  a regression in Claude's invocation rate is traceable.

## Testing harness

| Layer | Command |
| ----- | ------- |
| Python core | `cd chatgpt_mcp_memory && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q` |
| Rust shell | `cd desktop/src-tauri && cargo test` |
| Svelte/TS (types) | `cd desktop && npm run check` |
| TS unit (Vitest) | `cd desktop && npm run test:unit` |
| Browser E2E | `cd desktop && npm run test:e2e` (Playwright + `scripts/run-e2e-stack.sh`) |

CI entrypoint: `.github/workflows/ci.yml`.

## Code hygiene

- Minimum tokens out. Minimum surface area on edits.
- Don't rewrite parsers when a preflight check will do.
- New deps need a one-line justification. Open-source first; `requests-html`
  before hand-rolled scraping, `trafilatura` before hand-rolled HTML cleanup.
- Comments explain *why*, never *what*. No `# Return the result`.

## Where things live

- `chatgpt_mcp_memory/src/` — Python core: parsers, store, ingest, mcp server.
- `desktop/` — Tauri app (Rust shell + SvelteKit UI).
- `chatgpt_mcp_memory/src/telemetry.py` — the feedback-loop log.
- `~/Library/Application Support/Minion/data/` — live DB, inbox, telemetry.
- `third_party/awesome-cursor-skills/` — curated Cursor **skills** index (submodule); agents should skim `README.md` before multi-step work (see `.cursor/rules/consult-awesome-skills.mdc`).

## First‑party screen & audio capture

Minion ships **its own** macOS capture path (focused window metadata, optional Accessibility-tree text sample, optional window screenshots → inbox OCR). Extend here deliberately — retrieval stays in `chatgpt_mcp_memory`; capture stays in `desktop/src-tauri/`.

Planned / roadmap deltas vs heavier commercial tools: audio transcription (local Whisper), multi-monitor selection, richer event triggers. Do **not** vendor external capture apps into this repo unless there is a narrow library dependency with a clear license.

