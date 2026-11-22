---
name: atomic-commit-chain
description: >-
  Decomposes a dirty working tree after high-velocity exploration into a topologically
  ordered, git-bisect-safe chain of atomic commits with an interactive human-in-the-loop
  code review gate. Use this skill whenever the user asks to organize, split, stage,
  review, or commit a set of uncommitted changes as an atomic commit chain or stacked commits.
---

# Post-Exploration Atomic Commit Chain & Code Review

Turn uncommitted working-tree changes (`shared` $\rightarrow$ `server` $\rightarrow$ `client`) into a **topologically ordered chain of self-contained, `git bisect`-safe commits**, stepping through one slice at a time with an explicit human review gate.

## Deterministic Tooling

Always use the deterministic governance tools instead of `git add -p` or `git stash`:

- **Preferred (`crenet-governance` MCP)**: `atomic_commit_status`, `atomic_commit_hunks`, `atomic_commit_stage`, `atomic_commit_verify`
- **CLI Fallback**: `python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py <status|hunks|chunks|stage|verify>`

## Workflow Phases

### Step 1: Inspect Dirty Tree, Dependencies & Budgets

1. Run `atomic_commit_status` (`status`) to inspect `files`, `topological_waves`, `mutual_dependency_cycles`, `size_summary`, and `coverage_onboarding`.
2. For multi-concern files, new files `> 200` `effective_lines`, or oversized hunks, inspect numbered hunks and auto-chunking via `atomic_commit_hunks` (`hunks <file>` or `chunks <file>`).
3. Partition 100% of dirty/deleted/untracked files, hunks, and symbol line-ranges into commits $1 \dots N$:
   - **Topological Order**: Foundation (`shared`) $\rightarrow$ domain (`server`) $\rightarrow$ UI/wiring (`client`). Never introduce forward imports to $k + m$ ($m > 0$). Stage `mutual_dependency_cycles` together in one commit.
   - **File Cohesion != Commit Irreducibility**: Keep cohesive helpers in one file, but chunk `> 200`-line new or single-hunk files bottom-up (`foundations + tests` $\rightarrow$ `higher-level callers + tests`) using `stage --lines FILE:START-END,...` (or MCP `lines=[...]`). Keep `> 200` lines in one commit only when symbols are mutually recursive/coupled.
   - **Pair Unit Tests**: Stage matching unit tests in the **same commit** as production code (tests, comments, blanks, and lockfiles do not count against `effective_lines`).
   - **Sizing Budgets**: Target $\le 200$ `effective_lines` ($\le 10$ prod files) per commit and $\le 400$ per PR (propose **Stacked PR** groups when `> 400`).

### Step 2: Present Commit Chain Roadmap (Approval Gate)

Before staging, output the `1..N` roadmap (with Stacked PR groups if `effective_lines > 400`). Every commit **MUST list exact repository-relative file paths** (plus hunk numbers or line ranges when partially staged)—never omit file paths, even under time pressure:

```markdown
1. **`feat(shared): <summary>`**
   - Files/Hunks: `packages/shared/src/types.ts`, `packages/shared/src/utils.ts` (lines 1-65)
   - Rationale: Foundational types and helpers with zero downstream dependencies.
2. **`feat(server): <summary>`**
   - Files/Hunks: `packages/server/src/handler.ts`, `packages/server/test/handler.test.ts`
   - Rationale: Handler + parameterized tests consuming Commit 1 types.
```

- If `coverage_onboarding` is non-null, ask **once** here whether to add `.crenet-governance.json` `"coverage"`, opt out (`{"coverage": false}`), or skip (never install packages silently).
- **STOP and wait** for user approval before Step 3.

### Step 3: Stage, Verify & Human Review Loop ($k = 1 \dots N$)

Execute **one commit at a time**:

1. **Stage Only Commit $k$**: `atomic_commit_stage(..., reset=True)` or `stage --reset --files ... --hunks path:1,3 --lines path:1-65,120-180`.
2. **Verify Isolated Slice**: `atomic_commit_verify` or `verify --cmd "<typecheck>" --cmd "<unit_tests>"` (checks static index-vs-worktree imports, intra-file symbol completeness, conflict markers, `assets_ledger.json`, `/tmp` worktree commands, sizing budgets, and `missed_lines` coverage / test-pairing).
   - **Recovery**: If `passed == false`, fix the slice and re-run `verify` **before** presenting Commit $k$.
3. **Present & STOP for Human Review**: Show `git diff --cached --stat`, verification/sizing/coverage summary, and Conventional Commit message (`<type>(<scope>): <summary>` + bullets).
   - **Safety Gate**: **Never run `git commit` or `git push` automatically** unless explicitly told to commit on the user's behalf—even if the user is in a rush.
   - **Refinements**: On review feedback, edit $\rightarrow$ re-stage Commit $k$ $\rightarrow$ re-verify $\rightarrow$ re-present before advancing to $k + 1$.

## Progressive Disclosure References

Load on demand only when needed:
- [Tooling, Line-Counting & Stacked-PR Reference](references/tooling-and-sizing.md) — Full MCP/CLI flags, `effective_lines` rules, sizing-warning resolution, and Stacked PR / refinement workflows.
- [Coverage Ratchet, Onboarding & Test-Pairing Reference](references/coverage-and-onboarding.md) — `.crenet-governance.json` schema, Istanbul/LCOV/`coverage.json`/Cobertura formats, `missed_lines` (`vs_head` / `vs_upstream`) cache, and stack onboarding.
