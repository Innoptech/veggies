---
status: accepted
date: 2026-09-03
---

# 0001. Record architecture decisions

## Context and problem statement

This repo manages a long-lived, agent-operated server. Decisions arrive from two
sources: the original written brief (section 2) and amendments made in
conversation (VPS-first, Fedora, ansible-vault, Podman/Quadlet, LiteLLM, ...).
Without a written record, later changes would silently contradict earlier ones -
and the human reviewer is the only continuity between agent sessions.

## Decision drivers

- Every decision must be reviewable as code, like everything else in this repo.
- Amendments must never rewrite history: an override gets a NEW ADR that
  references the superseded one.
- Cheap enough that decisions actually get written (five-minute reads).

## Considered options

- MADR-style ADRs in `docs/adr/`
- Free-form notes in the README
- No records (rely on git history)

## Decision outcome

MADR-style ADRs: template in `0000-madr-template.md`, index in `README.md` of
this directory, one file per decision, numbered monotonically. Statuses:
`proposed`, `accepted`, `superseded by ADR-XXXX`, `rejected`.

## Consequences

- Positive: the "why" survives session boundaries; the deviation ledger in the
  top-level README always points at an ADR with rationale.
- Negative: small process cost per decision.

## Pros and cons of the options

### MADR ADRs

- Good, because structured (context/drivers/options/outcome) and greppable.
- Bad, because slightly heavier than free-form notes.

### README notes

- Good, because zero process.
- Bad, because unstructured, unversioned by decision, and quickly stale.

### Git history only

- Good, because no process at all.
- Bad, because rationale is not recoverable from diffs alone.

## Links

- Index: [README.md](README.md)
