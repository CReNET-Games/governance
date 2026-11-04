---
name: atomic-commit-chain
description: >-
  Decomposes a dirty working tree after high-velocity exploration into a topologically
  ordered, git-bisect-safe chain of atomic commits with an interactive human-in-the-loop
  code review gate. Use this skill whenever the user asks to organize, split, stage,
  review, or commit a set of uncommitted changes as an atomic commit chain or stacked commits.
---

# Post-Exploration Atomic Commit Chain & Code Review

This skill guides you through turning a large set of uncommitted working-tree changes (created during high-velocity exploration across multi-package workspaces such as `shared` $\rightarrow$ `server` $\rightarrow$ `client`) into a **topologically ordered chain of self-contained, `git bisect`-safe commits**, stepping through them one by one with a human review gate.

## Deterministic Tooling (MCP Tools & CLI Fallback)

Always use the deterministic atomic commit tools rather than ad-hoc `git add -p` or manual guesses:

- **Preferred (when `crenet-governance` MCP server is available)**:
  - `atomic_commit_status(workspace_dir)`
  - `atomic_commit_hunks(workspace_dir, file, cached=False)`
  - `atomic_commit_stage(workspace_dir, files=[...], hunks=["path:1,3"], reset=True)`
  - `atomic_commit_verify(workspace_dir, commands=[...])`
- **CLI Fallback (identical Single Source of Truth)**:
  ```bash
  python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py <status|hunks|stage|verify>
  ```
  *(Or `python3 scripts/atomic_commit.py <subcommand>` when running directly inside `governance/`).*

---

## Workflow Steps

### Step 1: Inspect Dirty Tree & Static Dependency Graph

1. Run the deterministic status and dependency analyzer via MCP (`atomic_commit_status`) or CLI:
   ```bash
   python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py status
   ```
2. If any modified file contains changes that belong to different logical commits, inspect its numbered diff hunks via MCP (`atomic_commit_hunks`) or CLI:
   ```bash
   python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py hunks <path/to/file>
   ```
3. Combine the tool's `topological_waves` and `depends_on` edges with your semantic understanding of the changes to partition all modified, deleted, and untracked files (and specific file hunks) into a dependency-ordered sequence of commits ($1 \dots N$):
   - **Foundational Layer First**: Shared types, schemas, interfaces, constants, and pure utilities (`shared`).
   - **Core Domain & Backend Second**: Domain services, state machines, server handlers, paired with their unit tests (`server`).
   - **Client / UI / Wiring Last**: Frontend components, templates, styles, and end-to-end integration (`client`).
   - **No Forward Dependencies**: Commit $k$ must never import a file, type, or symbol that is only staged in Commit $k + m$ ($m > 0$). Files in a `mutual_dependency_cycles` group must be staged together in the same commit.
   - **Account for Every Change**: Ensure 100% of dirty/untracked files and hunks are assigned to at least one commit in the chain.

---

### Step 2: Present the Commit Chain Roadmap (Wait for Approval)

Before staging Commit 1, output the complete proposed roadmap ($1 \dots N$) to the user:

```markdown
### Proposed Atomic Commit Chain (1..N)

1. **`feat(shared): <summary>`**
   - Files/Hunks: `packages/shared/src/types.ts`, `packages/shared/src/utils.ts` (hunks 1, 2)
   - Rationale: Introduces foundational protocol types with zero downstream dependencies.
2. **`feat(server): <summary>`**
   - Files/Hunks: `packages/server/src/handler.ts`, `packages/server/test/handler.test.ts`
   - Rationale: Implements server handler and parameterized unit tests consuming Commit 1 types.
...
```

Ask the user to confirm or adjust the commit boundaries before proceeding to Step 3.

---

### Step 3: Step-by-Step Staging, Verification & Human Review Gate (Commits $1 \dots N$)

For each commit $k \in \{1 \dots N\}$, execute the following loop **one commit at a time**:

#### 3.1 Stage Only Commit $k$
Use `atomic_commit_stage` (with `reset=True`) or `atomic_commit.py stage --reset` so the Git index contains **only** the files and hunks for Commit $k$:

```bash
python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py stage \
  --reset \
  --files <full_file_1> <full_file_2> \
  --hunks <partial_file>:1,3
```

#### 3.2 Deterministically Verify the Staged Slice
Run `atomic_commit_verify` (or `atomic_commit.py verify`) with the appropriate typecheck and targeted unit test commands for the staged files:

```bash
python3 .agents/skills/atomic-commit-chain/scripts/atomic_commit.py verify \
  --cmd "<typecheck_command>" \
  --cmd "<targeted_unit_test_command>"
```

What `verify` enforces automatically:
- **Static Index-vs-Worktree Isolation**: Fails immediately if any staged file imports an untracked/unstaged file or imports a symbol exported only in an *unstaged* hunk of a partially staged file.
- **Hygiene & Governance**: Rejects leftover merge conflict markers and unlogged image assets (`assets_ledger.json`).
- **Ephemeral Worktree Sandbox**: Materializes *only* `HEAD + staged index` via `git write-tree` in a temporary detached worktree (with `node_modules` / `.venv` symlinked) and runs each verification command without disturbing the developer's open IDE buffers.

> [!IMPORTANT]
> If `verify` fails, fix the staged slice (e.g., stage the missing prerequisite hunk/file or fix the failing test) and re-run `verify` **before** presenting Commit $k$ to the user.

#### 3.3 Present Staged Commit $k$ & Pause for Human Review
Once verification passes, present:
1. **Staged Files & Diff Stats** (`git diff --cached --stat`).
2. **Verification Result** (confirming static isolation + commands passed).
3. **Proposed Conventional Commit Message**:
   ```text
   <type>(<scope>): <concise imperative summary>

   - <bullet 1 explaining key change and rationale>
   - <bullet 2>
   ```

Then **STOP and wait for the human**:
- **Do NOT run `git commit` automatically** unless the user explicitly tells you to commit on their behalf. The developer uses this pause to inspect the isolated staged diff in their IDE.
- **Handling Review Refinements**: If the user requests changes to Commit $k$ (for example, "make these unit tests parameterized" or "split this commit"):
  1. Edit the relevant files.
  2. Re-stage Commit $k$'s files/hunks (`atomic_commit_stage` / `atomic_commit.py stage`), taking care not to stage unrelated hunks reserved for commits $k+1 \dots N$.
  3. Re-run `verify`.
  4. Present the updated diff and commit message for Commit $k$.
- **Advancing the Chain**: Once the user confirms Commit $k$ has been committed (or instructs you to commit it and move on), advance to Commit $k + 1$ and repeat Step 3 until the working tree is clean.
