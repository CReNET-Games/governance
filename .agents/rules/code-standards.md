---
name: code-standards
description: Guidelines for HTML templates, semantic CSS, TypeScript POJO enums, Makefile orchestration, and Vite/Vitest monorepo and testing standards.
---

# Code & Infrastructure Standards

## 1. HTML & Templates
- **Use `<template>` elements**: Avoid large JS template-literal HTML strings. Put HTML in `<template>` tags, clone via `content.cloneNode(true)`, and set text/classes/attributes via DOM queries.
- **Separation of Concerns**: Keep HTML in markup and logic in `<script>`.

## 2. Appearance & Semantic CSS
- **Semantic CSS**: Replace long utility-class lists in HTML with semantic classes (e.g., `.room-card`, `.neon-title`, `.btn-primary`).
- **Tailwind Setup**: Define semantic classes inside `<style type="text/tailwindcss">` using `@apply`.

## 3. TypeScript Guidelines
- **POJO Enums (`as const`)**: With `erasableSyntaxOnly`, standard TS `enum` is prohibited. Use `as const` objects and union types:
  ```typescript
  export const EntityType = { Fighter: 'Fighter', CapitalShip: 'CapitalShip' } as const;
  export type EntityType = typeof EntityType[keyof typeof EntityType];
  ```
- **Discriminated Unions**: Use raw string literals in interfaces rather than `typeof EntityType.Fighter`:
  ```typescript
  export interface Fighter extends Ship { type: 'Fighter'; }
  ```

## 4. Infrastructure & Workflows
- **Explicit Naming**: Name resources explicitly to avoid random suffixes.
- **Makefile**: Orchestrate builds, tests, `test-simulation`, and deploys (GCP/Podman) via root `Makefile`.
- **Secrets**: Exclude sensitive scripts and passphrases in `.gitignore`; never commit secrets.

## 5. Testing Standards
- **Parameterized Tests (Jest / Vitest)**: Prefer `test.each`, `it.each`, or `describe.each` for boundary cases and state transitions.
- **Reusable Fixture Factories**: Share composable factories (e.g., `createPlayer(overrides)`, `createWorldState(overrides)`) with defaults instead of repeating inline object literals.

## 6. Vite & Vitest Monorepo Configuration
- **Cross-OS `cacheDir`**: Use `path.join(os.tmpdir(), '<repo>-vite-cache', '<pkg>')` instead of `node_modules/.vite` or `/tmp` to prevent `EROFS` and lock contention in AI sandboxes, symlinked worktrees, and FUSE mounts.
- **Source Aliasing**: Alias internal packages to TS sources (`shared/src/index.ts`) in `resolve.alias` and `tsconfig.json` `paths` so tests never read stale `dist/` builds.
- **Project Split & Pre-Bundling**: Split `vitest.config.mts` `test.projects` into parallel unit (`maxWorkers: 4`) and sequential stress (`fileParallelism: false`, `maxWorkers: 1`) suites, with `test.deps.optimizer.web` for heavy ESM libs (`pixi.js`).
