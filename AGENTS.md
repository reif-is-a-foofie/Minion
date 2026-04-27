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
- `telemetry-collector/` — Cloudflare Worker + D1 for remote collect + operator dashboard; deploy via GitHub Actions workflow `Deploy telemetry collector` (see workflow file for required secrets).
- `~/Library/Application Support/Minion/data/` — live DB, inbox, telemetry.

## Testing methodology (for every agent)

Use one entrypoint from the repo root so CI and humans stay aligned:

```bash
python scripts/minion_test.py doctor    # deps OK? (fast)
python scripts/minion_test.py core      # Python: unit + real sidecar HTTP/WS smoke
python scripts/minion_test.py desktop-quick   # Svelte check (needs desktop/node_modules)
python scripts/minion_test.py all         # core then desktop-quick
python scripts/minion_test.py all --ci    # like CI: core + npm ci + check
python scripts/minion_test.py e2e       # Playwright: Chromium + Vite + real sidecar (heavy)
python scripts/minion_test.py e2e-quick # same as e2e but skip npm ci (needs chromium installed)
```

**Tiers**

| Tier | What | When |
|------|------|------|
| **0** | `doctor` | Before claiming “tests pass”; verifies pytest/httpx/websockets/FastAPI imports. |
| **1** | `core` (`pytest chatgpt_mcp_memory/tests`) | Default gate for every PR. Spins a real `api.py` process per test where needed — not mocked HTTP. |
| **2** | `mcp-eval` / `eval/test_mcp_golden.py` | Regression on retrieval against a **real indexed corpus**. Needs `MINION_DERIVED_DIR` or `--derived-dir`. Skips in CI if unset; run locally before shipping retrieval changes. |
| **3** | `desktop` / `desktop-quick` | UI contract: `npm run check` (types + Svelte). Full `desktop` runs `npm ci` first (CI-style). |
| **4** | `e2e` / `e2e-quick` | **Playwright** against `vite dev` with **Tauri IPC stubbed** (`desktop/e2e/stubs/`) and a **real** `api.py` sidecar from `desktop/scripts/e2e-desktop-webserver.sh`. Uses the same Python rules as **tier 1** (needs loadable SQLite extensions + deps — **not** Apple CLT Python 3.9). Set `MINION_PYTHON` to your venv interpreter when running locally. First browser install: `cd desktop && npm run test:e2e:install`. |

**Setup (once per machine)**

```bash
cd chatgpt_mcp_memory
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cd ../desktop && npm ci
```

Then either `export MINION_PYTHON=$PWD/../chatgpt_mcp_memory/.venv/bin/python` from repo root, or activate `.venv` before `python scripts/minion_test.py …` so `core` uses the same interpreter.

**Pytest forwards** (only for `core` / `all`): put pytest args after `--`:

`python scripts/minion_test.py core -- -k test_status_ready`

**Playwright forwards** (only for `e2e` / `e2e-quick`):

`python scripts/minion_test.py e2e-quick -- --headed`

**Golden / MCP eval**

```bash
python scripts/minion_test.py mcp-eval --derived-dir /path/to/derived
# or: pytest eval/ --derived-dir /path/to/derived
```

**Principles**

- Prefer the **smallest tier** that covers your change; do not skip tier 1 before merge.
- Sidecar tests use isolated `MINION_DATA_DIR` under pytest `tmp_path`; they do not touch the user’s live app data.
- If you add a new “must never break” behavior, add a test in tier 1 (fast, no derived dir) when possible; use tier 2 only when behavior depends on real embeddings or export shape.
