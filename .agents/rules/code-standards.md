# Code & Infrastructure Standards

The following guidelines apply to code structure, UI template patterns, TypeScript, testing, and infrastructure across Crenet Games projects.

## 1. HTML & Template Structuring
- **Use `<template>` elements**: When building UI dynamically via JavaScript without a framework (e.g., vanilla JS), avoid constructing large, complex HTML strings in JavaScript using template literals. Use HTML `<template>` tags in the markup, clone them with `content.cloneNode(true)`, and query the resulting DOM nodes to set text, classes, and attributes.
- **Maintain Separation of Concerns**: Keep HTML structure inside HTML elements and JavaScript logic inside the `<script>` block.

## 2. Appearance & Semantic CSS
- **Semantic CSS over Inline Utility Clutter**: Do not write overly long lists of utility classes in HTML markup. Use semantic CSS classes (e.g., `.room-card`, `.neon-title`, `.btn-primary`).
- **Tailwind Setup**: Use `<style type="text/tailwindcss">` blocks to define semantic classes using Tailwind's `@apply` directive.

## 3. TypeScript Guidelines
- **POJO Enums instead of standard `enum`**: Due to `erasableSyntaxOnly` compiler option enabled in Vite builds, standard TypeScript enums are prohibited. Define "POJO Enums" using `as const` and extract their type:
  ```typescript
  export const EntityType = {
    Fighter: 'Fighter',
    CapitalShip: 'CapitalShip'
  } as const;
  export type EntityType = typeof EntityType[keyof typeof EntityType];
  ```
- **Discriminated Unions**: Use raw string literals for discriminant properties in interfaces rather than `typeof EntityType.Fighter`.
  ```typescript
  export interface Fighter extends Ship {
    type: 'Fighter';
  }
  ```

## 4. Infrastructure & Workflows
- **Explicit Naming**: Explicitly name resources to avoid auto-naming and random suffixes.
- **Makefile Orchestration**: Use the root `Makefile` for high-level orchestration of build, test, simulation (`test-simulation`), and deployment commands (GCP/Podman).
- **Sensitive Configuration**: Sensitive deployment scripts and passphrases are excluded via `.gitignore`. Never commit secrets.

## 5. Testing Standards
- **Parameterized Tests (Jest)**: Strongly prefer `test.each` or `describe.each` when writing tests with multiple input/output cases to keep tests DRY and readable.
