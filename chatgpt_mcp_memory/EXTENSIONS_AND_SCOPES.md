# Claude / MCP extensions and reader scopes

Minion extensions span three planes:

1. **Indexed corpus tools** — `ask_minion`, `get_chunk`, conversations helpers — gated by `consent_policy.json` for the **`mcp` reader only**.
2. **Live macOS context tools** — `what_am_i_working_on`, `search_screen_memory`, `summarize_recent_activity` — toggle via `readers.mcp.allow_screen_context_tools`.
3. **Parser & ingest extensions** — `parser_extensions.json` + `POST /ingest/webhook`; unrelated to MCP retrieval scopes but feed the same DB.

## Configuring consent

File: `<MINION_DATA_DIR>/consent_policy.json`

Manage via:

- `GET /settings/consent`
- `PUT /settings/consent` (Bearer when `MINION_API_TOKEN` is set)

Default schema merges shallowly into shipped defaults — edit keys under `readers.mcp`.

### Useful MCP knobs

| Field | Role |
|-------|------|
| `deny_chunk_source_kinds` | Source kinds withheld from MCP search hits (`ambient` default). |
| `deny_path_substrings` | Case-sensitive substring filters on normalized paths (`/screen-memory/` default). |
| `allow_screen_context_tools` | When `false`, screen MCP helpers return blocked summaries/errors. |

## Ambient companion ingest

`POST /ambient/sync` tails `<MINION_DATA_DIR>/screen_context/stream.jsonl` into `ambient_events` with dedupe keys — vault-local telemetry shaped for summaries without exposing OCR blobs via MCP unless you relax deny lists deliberately.
