# Tooling, Line-Counting & Stacked-PR Reference

Load this reference when overriding CLI/MCP sizing flags, inspecting `size_summary` / `size_metrics` fields, splitting hunks after a sizing warning, or organizing Stacked PR branches.

---

## 1. Full MCP & CLI Signatures

### MCP Tools (`crenet-governance`)
- `atomic_commit_status(workspace_dir=".", count_tests=False, count_comments=False, max_commit_lines=200, max_pr_lines=400)`
  - Returns `repo_root`, `total_dirty_files`, `files` (per-file hunks, dependencies, and categorized line counts), `topological_waves`, `mutual_dependency_cycles`, `size_summary`, and `coverage_onboarding`.
- `atomic_commit_hunks(workspace_dir, file, cached=False)`
  - Returns 1-based hunk indices, line ranges, `section_hint`, `preview`, and per-hunk `logic_lines`, `test_lines`, `comment_lines`, `raw_additions`, and `raw_deletions`. For untracked files (`hunk_count == 0`) or oversized hunks, also returns `symbol_chunks` and `suggested_line_slices`.
- `atomic_commit_stage(workspace_dir, files=None, hunks=None, lines=None, reset=False)`
  - Stages full files (`files=["a.ts"]`), 1-based hunk specs (`hunks=["b.ts:1,3"]`), and/or 1-based working-tree line ranges directly into the Git index (`lines=["c.py:1-60,120-180"]`). Pass `reset=True` to clear the Git index before staging Commit $k$.
- `atomic_commit_verify(workspace_dir=".", commands=None, skip_worktree=False, max_commit_lines=200, max_commit_files=10, count_tests=False, count_comments=False, coverage_cmd=None, coverage_report=None, coverage_mode=None)`
  - Returns `passed`, `staged_files`, `errors`, `warnings`, `size_metrics`, `coverage`, and `command_results`.

### CLI Equivalents (`scripts/atomic_commit.py`)
```bash
# Status & dependency waves
python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py status \
  [--include-tests] [--include-comments] [--max-commit-lines 200] [--max-pr-lines 400] [--json]

# Inspect numbered diff hunks (auto-includes symbol chunks on new/large files)
python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py hunks <file> [--cached] [--json]

# Inspect top-level symbol chunks & suggested `--lines` slices directly
python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py chunks <file> [--max-chunk-lines 200] [--json]

# Non-interactive staging (full files, hunks, or 1-based line ranges on untracked/tracked files)
python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py stage \
  --reset --files <f1> <f2> --hunks <f3>:1,3 --lines <f4>:1-60,120-180

# Isolated worktree verification
python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py verify \
  --cmd "<typecheck>" --cmd "<unit_tests>" \
  [--max-commit-lines 200] [--max-commit-files 10] \
  [--include-tests] [--include-comments] \
  [--coverage-cmd "<cmd>"] [--coverage-report <path>] [--coverage-mode warn|error|off]
```

---

## 2. How `effective_lines` Classification Works

By default, commit (`<= 200`) and PR (`<= 400`) budgets count **only production logic churn** (`added + removed` logic lines) so thorough unit test suites and documentation never penalize commit hygiene:

| Category | Examples | Included in `effective_lines` by Default? | Opt-In Flag |
| :--- | :--- | :--- | :--- |
| **`logic_lines`** | Executable/declarative lines in production files | **Yes** | *(default)* |
| **`test_lines`** | `tests/`, `__tests__/`, `*.test.*`, `*.spec.*`, `test_*.py`, `conftest.py` | **No** (`0`) | `--include-tests` / `count_tests=True` |
| **`comment_lines`** | Python `#` & multi-line `"""` docstrings (resolved via full-blob context), `//`, `/* ... */`, `<!-- ... -->`, Makefile/YAML `#` | **No** (`0`) | `--include-comments` / `count_comments=True` |
| **`blank_lines`** | Whitespace-only additions/deletions | **No** (`0`) | Never counted |
| **`generated_lines`** | `uv.lock`, `pnpm-lock.yaml`, `package-lock.json`, `assets_ledger.json`, `coverage*.json`, `*.snap`, `*.min.js`, binary/3D/image assets | **No** (`0`) | Never counted |

### Repo-Level `.crenet-governance.json` `"sizing"` Config
Extend defaults per repository in `.crenet-governance.json` (explicit CLI/MCP flags override config values):

```json
{
  "sizing": {
    "max_commit_lines": 200,
    "max_commit_files": 10,
    "max_pr_lines": 400,
    "count_tests": false,
    "count_comments": false,
    "generated_basenames": ["schema.graphql"],
    "generated_extensions": [".pb.ts", ".g.dart"],
    "generated_globs": ["src/generated/**", "**/*.gen.ts"],
    "test_globs": ["**/fixtures/**", "**/*.bench.ts"]
  }
}
```

---

## 3. Handling Sizing Warnings, Refinements & Stacked PRs

- **`Commit size warning` (`effective_lines > 200`) or `Commit scope warning` (`production_files > 10`)**:
  1. Warnings are **non-blocking** (`passed` stays `true`).
  2. **File Cohesion != Commit Irreducibility**: Keep cohesive helpers inside one module/file on disk, while still committing them in small `<= 200`-line slices (`foundations/config/helpers + tests` $\rightarrow$ `callers + tests`). Run `hunks <file>` or `chunks <file>` to inspect intra-file symbol dependencies and `suggested_line_slices`, then stage bottom-up with `--lines FILE:START-END,...`.
  3. Only proceed with a `> 200`-line slice when the symbols inside a single chunk or mutual cycle cannot compile/test independently without creating artificial dead stubs. Note that mutual dependency in the commit presentation.
- **Stacked PRs (`size_summary.effective_lines > 400` / `recommended_pr_count >= 2`)**:
  - Group commits in the Step 2 roadmap into ~`200–400`-line PR stacks (`PR 1: Foundation`, `PR 2: Domain`, `PR 3: Client/Wiring`) aligned with `topological_waves`.
  - Ask the user whether they want separate stacked branches (`feat/part-1` $\rightarrow$ `feat/part-2`) or a single branch.
- **Review-Time Refinements on Commit $k$**:
  1. Edit the requested files in the working tree.
  2. Re-run `stage --reset` with Commit $k$'s files/hunks (do **not** stage hunks belonging to commits $k+1 \dots N$).
  3. Re-run `verify` before presenting the updated diff and Conventional Commit message.
