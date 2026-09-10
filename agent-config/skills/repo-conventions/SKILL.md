---
name: repo-conventions
description: Read and enforce this repository's conventions (AGENTS.md, ADRs, mask tasks) before changing anything
license: MIT
metadata:
  audience: agents
---

## What I do

- Read the repo's AGENTS.md and docs/adr/README.md before making changes.
- Run the repo's own checks (usually `mask ci` or equivalent) rather than
  inventing ad-hoc verification.
- Record non-obvious decisions as ADRs when the repo has an ADR process.
- Respect protected branches: where branch protection applies (veggies:
  main, ADR 0024), work on a feature branch and open a PR - never push to
  main directly, never merge before required checks pass.

## When to use me

Use at the start of any task in a repository that carries an AGENTS.md or an
ADR directory, and again before declaring work finished.
