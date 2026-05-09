# Minion delivery process

Canonical loop from idea → shippable change. **Agents follow this without being reminded.** Humans use the same checklist for PRs.

## 1. Intake (product framing — short)

Answer in writing (issue/PR description or chat):

- **Problem / outcome**: What changes for the user?
- **Non-goals**: What we are explicitly not doing this round.
- **Success**: Observable signal (test passes, behavior X, metric Y — even qualitative).

If unclear, stop and clarify before coding.

## 2. Technical slice

- Smallest diff that achieves **one** outcome.
- Name touchpoints: code paths, data dirs, MCP/tool surfaces, migrations.
- Identify **risks**: privacy, permissions, backwards compatibility.

## 3. Implement

- Match existing style and file placement (`AGENTS.md` → **Where things live**).
- No drive-by refactors; every changed line serves the slice.

## 4. Verify (mandatory before “done”)

Run what applies — **do not hand results back as finished without executing or reporting a concrete blocker** (missing toolchain, missing secrets, etc.):

| Area | Command |
| ---- | ------- |
| Python | `cd chatgpt_mcp_memory && PYTHONPATH=src .venv/bin/python -m pytest tests/ -q --tb=short` |
| Rust shell | `cd desktop/src-tauri && cargo test` |
| Desktop types | `cd desktop && npm run check` |
| Desktop unit | `cd desktop && npm run test:unit` |

Optional after risky UI changes: `cd desktop && npm run test:e2e` (Playwright stack).

Use **`chatgpt_mcp_memory/.venv/bin/python`**, not bare macOS `python3`, for pytest (SQLite extension preflight).

## 5. Integration narrative

In the PR / final reply:

- **What** changed, **why**, **how to validate**.
- MCP / protocol bumps called out if tools or instructions changed.
- Telemetry / retrieval invariants from `AGENTS.md` if relevant.

## 6. Ship / deploy

- **Merge gate**: CI green on `.github/workflows/ci.yml` for the branch.
- **Release builds** (Tauri signing): require secrets (`TAURI_SIGNING_PRIVATE_KEY`, etc.); agent documents steps, human or CI runs them.
- Never imply a release went out without confirming artifact + signing path.

## Escalation

If verification cannot run in this environment, say exactly **what failed**, **what human must run**, and still leave code merge-ready.
