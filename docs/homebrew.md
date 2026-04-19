# Homebrew tap (maintainers)

Teammates install with:

```bash
brew tap reif-is-a-foofie/minion
brew install minion
```

The tap repo is **`reif-is-a-foofie/homebrew-minion`** (the `homebrew-` prefix is dropped in `brew tap` — you still run `brew tap reif-is-a-foofie/minion`).

## After you push changes to `reif-is-a-foofie/minion` (this repo)

1. Note the commit SHA you want to ship (usually `main`):

   ```bash
   git rev-parse HEAD
   ```

2. Compute the GitHub archive checksum:

   ```bash
   curl -sL "https://github.com/reif-is-a-foofie/minion/archive/<SHA>.tar.gz" | shasum -a 256
   ```

3. In **homebrew-minion**, edit `Formula/minion.rb`:
   - `url` → `https://github.com/reif-is-a-foofie/minion/archive/<SHA>.tar.gz`
   - `sha256` → output from step 2
   - **`version`** → set an explicit string (e.g. `"9"`) and **bump it whenever you ship a new `url`/SHA**. Recent Homebrew does **not** infer a version from `archive/<commit>.tar.gz` URLs, so omitting `version` yields **`invalid attribute … version (nil)`** and `brew install` fails.

   Minimal shape:

   ```ruby
   class Minion < Formula
     desc "..."
     homepage "https://github.com/reif-is-a-foofie/minion"
     version "9"
     url "https://github.com/reif-is-a-foofie/minion/archive/<SHA>.tar.gz"
     sha256 "..."
     # ...
   end
   ```

4. Commit and push **homebrew-minion** `main`.

5. Teammates upgrade with:

   ```bash
   brew update && brew upgrade minion
   ```

The formula installs `chatgpt_mcp_memory/` and `bin/minion` under Homebrew’s `libexec` layout so paths resolve the same as a git checkout.

## Troubleshooting

| Symptom | Cause | Fix |
|--------|--------|-----|
| `Error: invalid attribute … version (nil)` | Formula has no `version` and the `url` is a commit archive | Add `version "…"` in `Formula/minion.rb` (see step 3 above), push the tap, then `brew update` |

Install after tapping is usually `brew install minion`. The fully qualified name `brew install reif-is-a-foofie/minion/minion` is equivalent once the tap works.
