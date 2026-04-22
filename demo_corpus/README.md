# Minion demo corpus

Drop this whole `demo_corpus/` folder into Minion to index it, then use
`questions.md` to probe whether Claude is actually retrieving the right facts.

Design goals:
- Each file has a few **unique, checkable** details.
- Some details are **cross-file** so retrieval has to connect them.
- Dates / codes / names are chosen to be distinctive to avoid collisions.

