# Minion process hygiene and memory

## What is actually “Minion” in `ps`

| What you see | What it is |
|----------------|------------|
| `minion-desktop` (Minion 2.app) | Tauri shell; parent of the **one** HTTP sidecar it spawned |
| `python …/api.py --port N` | FastAPI sidecar: ingest, search, watcher (per **data dir + port**) |
| `python …/mcp_server.py` | Claude Desktop (or another host) spawns MCP **separately** — not a child of the app |
| `qemu … instances/minion` (Multipass) | A **Linux VM** named `minion` — not this product; can use **gigabytes** of RAM |

Stopping the wrong “minion” does not help indexing.

## Stale sidecars and dev noise

Each `MINION_DATA_DIR` + port pair can have its own long‑lived `api.py`. Old experiments (`/tmp/…`, duplicate ports, another macOS user) show up as extra rows in `ps`. They are not automatically merged into one process.

**Preferred cleanup**

1. Quit the Minion app for that data dir, or use **Restart sidecar** from the UI so the shell kills listeners on its port (`desktop/src-tauri/src/lib.rs`).
2. Remove env overrides you do not need: unset `MINION_API_PORT` unless you intentionally run a **second** instance.
3. Manually `kill` stray `api.py` PIDs you recognize (see `chatgpt_mcp_memory/scripts/dev_ps_minion.sh`).

**Do not** rely on Minion to kill unrelated apps (Cursor, browsers, etc.).

## Multipass VM named `minion`

If you use Canonical Multipass with an instance called `minion`, it may be allocated **many GB** (`-m 12288` etc.). That dominates host RAM compared to Python sidecars.

```bash
multipass list
multipass stop minion   # if you do not need the VM
```

## Single data directory discipline

- One **Minion 2.app** session normally maps to one `~/Library/Application Support/Minion/data` (or `MINION_DATA_DIR`).
- Running **multiple** apps or CLI sidecars against the **same** `memory.db` without coordination can corrupt SQLite. Use one writer per DB.

## Embedding memory (JIT / release)

The sidecar and MCP cache the fastembed / ONNX model after first search or ingest. That speeds up repeated queries but holds RAM.

Optional environment variables (see also `desktop/README.md`):

| Variable | Effect |
|----------|--------|
| `MINION_EMBED_IDLE_SEC` | If set to a positive number of seconds, drop the cached embedding model after that much **idle** time (no embed/query). Next search pays a cold load. |
| `MINION_EMBED_BATCH_SIZE` | Chunk batch size during ingest embedding (default 64); lower reduces peak RAM during large ingests. |

## Optional process title (`ps`)

If `setproctitle` is installed in the venv, the sidecar and MCP rename themselves to `Minion-sidecar:port:tag` / `Minion-mcp:tag` (short hash of data dir, not the full path).
