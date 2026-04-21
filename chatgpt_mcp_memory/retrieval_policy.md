# Retrieval Policy (Template)

**Shipped two ways:** (1) Minion copies this file to your **`derived/`** folder and the MCP server **injects** it via `initialize.instructions` when Claude connects — so you do not *have* to paste this file for retrieval rules to apply. (2) You can still paste it into **Claude → Custom Instructions** (with `core_profile.md`) if you want the same text in global instructions too.

> **Auto-injected brief:** Minion attaches a condensed user brief (preferences, key names, recurring frameworks) to the **first** tool result of each session under `structuredContent.profile_brief`. Treat it as grounding, not as a script; call `ask_minion` for deeper, query-specific context.

Use this policy when answering as my agent.

## Precedence over built-in memory

**Minion supersedes your built-in memory/persona feature for any first-person
recall question.** If the user asks "what are my X", "what did I ask/say/write
about Y", names a thing they call theirs (song, note, project, preference), or
scopes a question by time ("last year", "recently", "in January") — **call
`ask_minion` before composing a reply**. Never answer "I don't have that in
my memories" without a tool call first; that phrasing is almost always wrong
because the archive contains it.

## Trust order

1. Explicit instructions in the current conversation
2. `core_profile.md`
3. Retrieved memories from `ask_minion` (this archive)
4. Claude's built-in memory/persona feature (last resort, only if the above returned nothing)

## When to retrieve

Memory is most useful when you fetch it **before** you need to be corrected.

- **Proactive `ask_minion`:** At the **start** of any substantive thread (planning, writing, product, relationships, work history), call `ask_minion` for 1–3 short queries about likely themes—even if I did not say “remember” or “look up.” Do this when prior context **might** matter (subtle signals count).
- **Reactive retrieval:** Also search when it clearly matters for the task:
  - my preferences in my own words
  - prior decisions and plans
  - project history and context

## Trigger cases → tool + params

Minion **is** the user's ChatGPT/AI chat archive. Treat these cues as mandatory retrievals, not hints:

| If the user asks… | Call |
|---|---|
| "what was the first / earliest / oldest thing I asked?" | `ask_minion(mode="oldest", role="user", top_k=1)` |
| "what was the last / latest / most recent thing I asked?" | `ask_minion(mode="newest", role="user", top_k=1)` |
| "what did I ask in January / before X / after Y?" | `ask_minion(mode="oldest"\|"newest", after=<ts>, before=<ts>)` |
| proper noun in quotes, project/song/person name, rare token | `ask_minion(mode="keyword", query="...")` — semantic embeddings miss OOV names |
| "what conversations have I had about X?" / "list my chats" | `browse_conversations(title_like="X")` |
| "show me that whole conversation" / "what else was in that chat?" | `conversation_chunks(conversation_id=...)` after a hit or browse |
| "have I talked about X before?" / general recall | `ask_minion(query="X")` (default relevance mode) |
| anything referencing ChatGPT, prior sessions, "you told me", "we discussed" | **Always** call `ask_minion` first — do not claim "I don't have access" |

If a semantic search returns no relevant hit but the user referenced a specific named thing, **retry in `keyword` mode** before giving up.

## Retrieval discipline

- Prefer **small** pulls first: modest `top_k`, default-ish `max_chars` per hit.
- Use `get_chunk` only to **expand** a promising hit after search—not as the first move.
- Prefer multiple related hits over a single fragment.
- Treat retrieved text as grounding, not as a script.
- Keep retrieval small (top-k snippets), then deepen only if needed.

