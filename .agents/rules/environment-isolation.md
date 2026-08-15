---
name: environment-isolation
description: Pre-commit strict environment isolation checks & Git config email validation.
---

# Environment Isolation Constraint (`environment-isolation`)

## Goal
Prevent any code commits, file writes, or cloud deployments if the environment detects unauthorized network profiles or restricted configurations.

## Instructions
1. **Git Config Email Verification**:
   - Before executing `git push`, `git commit`, or initializing a new remote, verify the git config user email matches an authorized domain (default: `@crenet.games`).
   - Run `git config user.email` and confirm compliance.

2. **Pre-Execution Check**:
   - Always check active processes or network configuration before initiating Git actions or remote connections.

## Constraints
> [!CAUTION]
> Do not execute any cloud deployments or code commits if an unauthorized network profile is detected in the shell.
