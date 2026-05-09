# Negative capability — what Minion refuses by default

This complements consent-aware MCP retrieval (`consent_policy.json`) and local-first storage policy.

## Not collected silently by default

- Global keystroke logging, synthetic typing transcripts, or keychain scraping.
- Unrestricted clipboard / pasteboard harvesting (danger-tier only when explicitly modeled later).
- Screen pixels **unless** `MINION_SCREEN_CAPTURE` opts in (`1` / `true` / `on`); Accessibility-derived **text** is preferred (`desktop/src-tauri/src/screen_context.rs`).
- Cross-app sandbox data without Apple-supported APIs plus explicit user prompts where required.

## MCP-specific withholding

`ask_minion` drops chunk hits matching:

- `readers.mcp.deny_chunk_source_kinds` (default includes `ambient`).
- `readers.mcp.deny_path_substrings` (default includes `/screen-memory/`).

HTTP desktop search intentionally stays **full vault** for the human operator.

Screen-context MCP helpers (`what_am_i_working_on`, `search_screen_memory`, `summarize_recent_activity`) honor `readers.mcp.allow_screen_context_tools` — set `false` to hard-block without tearing down capture locally.

## Audit trail

Sensitive mutations (`identity_propose`, `identity_patch`, `PUT /settings/consent`) append rows to `identity_audit_log` (SQLite).
