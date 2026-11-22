# Modular Coverage Ratchet, Onboarding & Test-Pairing Reference

Load this reference when `coverage_onboarding` appears in `status`, when configuring `.crenet-governance.json`, or when diagnosing `Coverage regression` / `Test pairing warning` messages from `verify`.

---

## 1. `.crenet-governance.json` Configuration

`governance` stays unopinionated about test runners by hooking into whatever coverage report the target repository produces:

```json
{
  "coverage": {
    "command": "pnpm exec vitest run --coverage --coverage.reporter=json-summary",
    "report_path": "coverage/coverage-summary.json",
    "mode": "warn"
  }
}
```

- **`command`** *(string, optional)*: Shell command executed inside the isolated `/tmp` verification worktree (with `.venv/bin` and `node_modules/.bin` on `PATH` and `COVERAGE_FILE` isolated to the worktree). Omit if one of the `--cmd` commands already writes the coverage report.
- **`report_path` / `report_paths`** *(string or string[], optional)*: Worktree-relative report path(s). Use `report_paths: ["client/coverage/coverage-summary.json", "coverage.json"]` in polyglot (`TS + Python`) or multi-package monorepos to merge snapshots automatically. When omitted, auto-discovers standard locations (`coverage/coverage-summary.json`, `coverage/coverage-final.json`, `coverage/lcov.info`, `lcov.info`, `coverage.json`, `coverage/coverage.xml`, `coverage.xml`).
- **`mode`** (`"warn"` | `"error"`, default `"warn"`): `"warn"` emits non-blocking warnings (`passed` remains `true`); `"error"` fails `verify` (`passed = false`) on coverage regressions.
- **Explicit Opt-Out**: Set `{"coverage": false}` in `.crenet-governance.json` (or pass `--coverage-mode off` / `coverage_mode="off"`) to disable coverage ratchets, onboarding prompts, and test-pairing warnings.

---

## 2. Supported Report Formats

- **JavaScript / TypeScript (Vitest, Jest, c8, nyc)**:
  - Istanbul `coverage-summary.json` (`total` + per-file `lines`/`statements`)
  - Istanbul `coverage-final.json` (statement hit map `s`)
  - LCOV `lcov.info` (`SF:` / `DA:` / `LF:` / `LH:`)
- **Python (`coverage.py` / `pytest-cov`)**:
  - `coverage.json` (`coverage json -o coverage.json`)
  - Cobertura `coverage.xml` (`--cov-report=xml`)

---

## 3. Why `missed_lines` Ratchets Avoid False Positives

Instead of asserting raw coverage `%` alone, `verify` compares **uncovered statements (`missed_lines = total_lines - covered_lines`)** against two cached baselines:

1. **`vs_head` (Per-Commit Ratchet)**: Compares the staged slice against `HEAD`.
   - **Deleting covered dead code**: `covered_lines` and `total_lines` drop, which can lower `%` (e.g., `90% -> 83%`), **but `delta_missed <= 0`** $\rightarrow$ **no false-positive regression**.
   - **Moving/extracting functions across files**: Repo-wide `delta_missed == 0` $\rightarrow$ **no false-positive regression**.
   - **Adding untested branches/functions**: `delta_missed > 0` $\rightarrow$ flags `Coverage regression` and pinpoints the staged files whose `missed` count grew.
2. **`vs_upstream` (Branch-Wide Anchor)**: Compares against the merge-base with `@{upstream}` / `origin/main` so cumulative uncovered lines across a long commit chain remain visible.

### Bounded Upstream-Pinned Cache
Snapshots are keyed by `(git_tree_sha, config_fingerprint)` in `.git/crenet-coverage-cache.json` (falling back to `/tmp/crenet-coverage-cache-<hash>.json` in read-only sandboxes) and capped at **20 entries** (`16` rolling LRU commits + `4` pinned remote merge-base anchors) so cache size never grows unbounded and `origin/main` is never evicted during long chains.

---

## 4. Self-Healing Onboarding & Test-Pairing Fallback

- **Ask Once in Step 2 (`coverage_onboarding`)**: When `.crenet-governance.json` has neither configured `"coverage"` nor opted out, `status` detects Vitest, Jest, or Pytest and returns `suggested_config`, `opt_out_config`, and `install_hint` (e.g., `pnpm add -D @vitest/coverage-v8` or `uv add --dev coverage`). Ask the user **once** during Step 2 whether to write `.crenet-governance.json`, opt out with `{"coverage": false}`, or skip for this run. **Never install packages silently inside `verify`.**
- **`Test pairing warning` Fallback**: Whenever coverage is not evaluated for a commit (and not opted out) and staged changes add executable production logic with `0` staged test files, `verify` emits a non-blocking `Test pairing warning`. Stage or author matching unit tests in the same commit whenever practical (test lines do not count against `effective_lines`).
