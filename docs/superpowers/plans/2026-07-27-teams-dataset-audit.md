# Teams and Dataset Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the independent team validator and add a read-only, whole-dataset auditor with a human-readable battle renderer.

**Architecture:** `packages/teams` remains a library whose only input is Showdown export text plus a generation and whose database boundary is read-only lookup code. `packages/dataset-audit` independently loads persisted battles into typed records, evaluates six invariants in pure functions, and exposes CLI commands for whole-dataset audit and one-battle rendering.

**Tech Stack:** Node.js 22, TypeScript 5.7, PostgreSQL via `pg` 8.13.1, Vitest 2.1.8, `pokemon-showdown` 0.11.10 only in `packages/teams`.

## Global Constraints

- The real database schema is authoritative: the relational table is `pokemon`; `species` remains the JSON field inside `trajectory_steps.state`.
- All database access in both auditors is read-only. No migrations or new tables.
- Generation is always a parameter or is read from `trajectories.gen_id`; it is never fixed.
- Ignore `.worktrees/` completely.
- Use explicit Git paths, `git commit -m ... -- <paths>`, and never `git add -A`.
- Validate only through `127.0.0.1:15432`.

---

### Task 1: Close `packages/teams`

**Files:**
- Modify: `packages/teams/src/audit.ts`
- Create: `packages/teams/test/audit.test.ts`
- Create: `.superpowers/sdd/gpt-teams.md`
- Modify: `pnpm-lock.yaml`

**Interfaces:**
- Consumes: `validateTeamText(text: string, gen: number, options?)`
- Produces: a learnset audit that creates fresh Showdown sources per `(form, move)` and reports `db_missing`, `db_extra`, base-form comparison mismatches, and oracle errors.

- [ ] **Step 1: Write a regression test for source isolation**

Add an integration assertion that gen 6 does not report the accumulated-source false positive `arceusbug/blastburn`, while gen 9 reports the D14 cases `ninetalesalola/moonblast` as missing and `ninetalesalola/ember` as extra.

- [ ] **Step 2: Run the focused test and verify RED**

Run the package Vitest entrypoint with Node 22 and confirm the test fails because the current audit reuses mutable `PokemonSources`.

- [ ] **Step 3: Isolate the audit function and refresh sources**

Export the generation audit for tests, create `validator.allSources(species)` for every move validation, and retain actionable grouping without changing seed data.

- [ ] **Step 4: Verify package behavior and demonstrations**

Run TypeScript, all package tests, a legal six-Pokémon export, the four-error export, and the full learnset audit against seeded generations 6 and 9.

- [ ] **Step 5: Write report and commit explicit paths**

Record exact input/output, audit findings, verification commands, and concerns in `.superpowers/sdd/gpt-teams.md`, then commit only the teams package, its lockfile importer, the plan, and report.

### Task 2: Build independent dataset invariant engine

**Files:**
- Create: `packages/dataset-audit/package.json`
- Create: `packages/dataset-audit/tsconfig.json`
- Create: `packages/dataset-audit/vitest.config.ts`
- Create: `packages/dataset-audit/src/types.ts`
- Create: `packages/dataset-audit/src/db.ts`
- Create: `packages/dataset-audit/src/invariants.ts`
- Create: `packages/dataset-audit/test/invariants.test.ts`
- Modify: `pnpm-lock.yaml`

**Interfaces:**
- Consumes: read-only rows from `battles`, `battle_turns`, `trajectories`, and `trajectory_steps`.
- Produces: `auditDataset(dataset, requestedGen?): AuditResult` with per-invariant counts and violations carrying battle tag, side, turn/decision index, and detail.

- [ ] **Step 1: Write pure failing fixtures for all six invariants**

Cover punctuation-insensitive line-by-line reveal matching, action/turn mismatch, missing raw protocol, missing reward on completed trajectory, mixed schema versions, and each orphan direction.

- [ ] **Step 2: Verify RED**

Run focused Vitest and confirm failures are due to missing invariant functions.

- [ ] **Step 3: Implement the minimum pure invariant engine**

Normalize species by Unicode decomposition plus removal of all punctuation/non-alphanumerics; derive revealed opponent species from protocol lines through the current step; never search a concatenated protocol blob.

- [ ] **Step 4: Add the read-only loader**

Use only `SELECT` statements, load the complete dataset without battle-tag filters, and filter by generation through `generations.gen_number` only when `--gen` is passed.

- [ ] **Step 5: Verify tests and TypeScript**

Run package tests and `tsc --noEmit`.

### Task 3: Add audit CLI and battle renderer

**Files:**
- Create: `packages/dataset-audit/src/render.ts`
- Create: `packages/dataset-audit/src/cli.ts`
- Create: `packages/dataset-audit/test/render.test.ts`
- Modify: `packages/dataset-audit/package.json`

**Interfaces:**
- Consumes: battle tag or numeric battle ID, optional generation, and dataset records from Task 2.
- Produces: `audit [--gen N]` and `battle <tag-or-id> [--gen N]` terminal commands.

- [ ] **Step 1: Write renderer output tests**

Use a real-shape fixture with multiple decisions in one turn and assert the output shows action, protocol lines, and revealed opponent knowledge in decision order.

- [ ] **Step 2: Verify RED**

Run the renderer test and confirm it fails because rendering is absent.

- [ ] **Step 3: Implement deterministic rendering and CLI parsing**

Render battle metadata followed by every trajectory step in `(turn_number, decision_index)` order, joining matching side/turn protocol without mutating data.

- [ ] **Step 4: Verify package**

Run all tests and TypeScript checks.

### Task 4: Audit real dataset, report, and commit

**Files:**
- Create: `.superpowers/sdd/gpt-dataset-audit.md`

**Interfaces:**
- Consumes: the live dataset count at execution time.
- Produces: full invariant summary, every actionable violation, and one complete rendered battle in the report.

- [ ] **Step 1: Run whole-dataset audit**

Run through `127.0.0.1:15432`, record the live battle count, and retain all reported locations without repairing data.

- [ ] **Step 2: Render one complete real battle**

Select a finished battle from the audited rows and capture the CLI output verbatim.

- [ ] **Step 3: Run fresh verification**

Run TypeScript and all tests for both new packages, inspect Git diff/status excluding `.worktrees/`, and confirm every database statement in `packages/dataset-audit` is a `SELECT`.

- [ ] **Step 4: Write report and commit explicit paths**

Write `.superpowers/sdd/gpt-dataset-audit.md` and commit only the dataset-audit package, its report, and its lockfile importer.
