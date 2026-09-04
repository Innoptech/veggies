---
status: proposed
date: 2026-09-04
---

# 0019. Agent rosters and skills library

> **Proposed, needs refining.** Context and open questions only; nothing here
> is decided. Do not implement against this ADR.

## Context

Beyond the two vendored agents (commit/adr): per-repo agent rosters, a
shared skills library, versioned agent definitions — the workforce the
orchestrator (ADR 0017) directs.

## Open questions

- Packaging format: files under agent-config/ vs remote refs with digest
  pins? How are skills/agents versioned and updated across stacks?
- Precedence: per-repo (veggies.yml / repo-local files) vs global defaults
  in this repo.
- How roster entries map to orchestrator workers (0017 dependency).
- Mount mechanics: skills must be read-only directory mounts — subPath is
  broken under rootless SELinux here (see cli/veggies.py comments), so every
  skill is its own directory mount with chcon s0 labeling.
- Trust: are repo-declared agents allowed to differ from the vendored set,
  and does that need a stack-level approval bit?
