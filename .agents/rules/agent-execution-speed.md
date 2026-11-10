---
name: agent-execution-speed
description: Enforces parallel 800-line file chunk reads, fast git-aware search (git grep / rg), and targeted inner-loop verification to eliminate serial tool latency and dist/node_modules scanning.
---

# Agent Execution & Search Speed Standards (`agent-execution-speed`)

Follow these execution rules on every turn to prevent serial tool-call round-trips and redundant filesystem scans:

## 1. Parallel Chunk Reads (`read_file`)
- **Batch Multiple `read_file` Calls in One Turn:** Whenever inspecting multiple files (or multiple sections of a file `>800` lines), emit all `read_file` tool calls **in parallel in the same response step** rather than reading one file/slice per turn.
- **Use Full 400–800 Line Windows:** Each `read_file` call supports up to `800` lines (`46,080` bytes). Never slice a file into `70–150` line serial micro-reads across consecutive turns.
  - For files `<= 800` lines: omit `StartLine` and `EndLine` to read the entire file in 1 call.
  - For files `800–2,000` lines: issue 2–3 parallel `read_file` calls in the **same turn** (`1..800`, `801..1600`, `1601..2400`), or jump directly to the target line window found via `git grep -n`.

## 2. Fast Scoped Search (Never Scan `dist/` or `node_modules/`)
- **Canonical `.aiignore` + Symlinks:** Repositories maintain a single canonical `.aiignore` file with symlinks for all agent/search tools (`.geminiignore`, `.aiexclude`, `.cursorignore`, `.claudeignore`, `.clineignore`, `.rooignore`, `.aiderignore`, `.codeiumignore`, `.rgignore`).
- **Never Run Unscoped `grep -rn`:** Raw POSIX `grep -rn` ignores `.gitignore` and `.aiignore`, scanning compiled bundles (`dist/index.js`, `*.js.map`) and `node_modules/`.
  - **Preferred:** Use `git grep -n "pattern"` (or `rg -n "pattern"` when installed), which automatically obey `.gitignore` / `.rgignore`.
  - **Fallback with POSIX `grep`:** Scope explicitly to `src/` directories (`grep -rn "pattern" shared/src server/src client/src`) or pass `--exclude-dir={node_modules,dist,coverage,.git}`.

## 3. Inner-Loop Build & Test Verification
- **During Iteration:** Run targeted single-file Vitest suites (`npx vitest run server/tests/<file>.test.ts` or `npx vitest run client/tests/<file>.test.ts`) and fast typechecking (`make typecheck`) instead of rebuilding presskit zips or running all suites after every edit.
- **Final Gate:** Run `make build && make test` once at the end of the task when all edits are complete.
