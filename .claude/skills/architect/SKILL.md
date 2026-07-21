---
name: architect
description: Use when starting non-trivial features, refactors, ambiguous bug work, or any task that needs problem framing, options, sequencing, and risk analysis before implementation.
---

# Architect

## Overview

Turn vague work into executable shape. Define scope, constraints, options, and sequence before code starts.

## When to Use

- New features with multiple moving parts
- Multi-file or cross-layer changes
- Refactors with behavior or migration risk
- Bugs without confirmed root cause
- Requests where success criteria or scope still fuzzy

## Core Workflow

1. Read current context before proposing changes.
2. State problem, goals, constraints, and unknowns.
3. Propose 2-3 viable approaches with tradeoffs.
4. Recommend one approach with clear reasoning.
5. Break work into small execution slices.
6. Call out risks, assumptions, and rollback concerns.
7. Define how success will be verified.

## Deliverables

- Problem framing
- Constraints and assumptions
- Options with tradeoffs
- Recommended approach
- Ordered implementation slices
- Risk list
- Verification strategy

## Boundaries

- Do not jump into production code while problem shape still unclear.
- Do not over-design simple work.
- Do not invent repo conventions; derive them from actual code and docs.
- If user explicitly wants direct implementation, compress planning to minimum useful form.

## Handoff

- Use `senior-software-engineer` when plan is approved and code changes should begin.
- Use `qc-qa-specialist` when defining test strategy, acceptance coverage, or proof of completion.
