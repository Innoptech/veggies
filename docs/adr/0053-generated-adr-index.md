---
status: accepted
date: 2026-09-11
---

# 0053. Generate the ADR index; the frontmatter status line is living metadata

## Context and problem statement

Every ADR-adding PR hand-appended a row to the same table in
`docs/adr/README.md`, so two parallel ADR PRs (the norm with kicked agent
sessions, ADR 0033/0037) guaranteed an adjacent-line merge conflict. The
index also drifted: it listed 0004/0009, which have no files, claimed
statuses the frontmatter contradicted (0017/0025/0027 said `accepted` while
the index said superseded), and reworded eight titles. The infra review of
issue #72 named a second race: two in-flight PRs both claiming the next
free number - already observed in 0051's mid-flight renumbering.

## Decision drivers

- The index is a derived artifact: number from the filename, title from the
  H1, status/date from the YAML frontmatter every ADR already carries.
- Parallel kicked sessions must never hand-resolve index conflicts.
- Enforcement must run networkless in-pod, in `mask ci`, and in CI (ADR
  0045/0047).

## Considered options

- (a) Generate the index; ADR PRs carry the regenerated table (the
  `tests/golden/pod.yaml` precedent).
- (b) A workflow regenerates and commits the index on `main` after merge.
- A conflict-resolving bot/agent; a `merge=union` git attribute (both
  rejected in discussion #66).

## Decision outcome

Chosen: (a). `scripts/adr_index.py` (stdlib-only, ADR 0047) renders the
table between `adr-index` markers; a `language: system` pre-commit hook
regenerates it and fails on drift; a pytest byte-compare mirrors
`test_render_matches_golden`. Duplicate numbers, bad filenames,
missing/invalid frontmatter, unknown statuses, and H1/filename mismatches
are hard validator errors.

(b) is rejected: it needs a branch-protection bypass for bot pushes to main
(ADR 0007/0024), moves frontmatter validation from PR time to
post-merge-red-main, and catches duplicate numbers no earlier than (a),
which catches them at the second PR's rebase (branch protection requires
up-to-date branches). The conflict-bot spends tokens forever on the same
toil and is a trust risk the day it resolves wrong; `merge=union` silently
garbles ordering.

**The duplicate-number race is detected, not prevented**: two parallel PRs
can both claim the next number; the second one's rebase fails the
validator; fix = rename one file, re-run the script. No allocation
service - that is perpetual machinery to solve a formatting problem.

**The `status:` frontmatter line is living metadata, not decision text**:
decision text stays immutable (AGENTS.md rule 5), but a PR that supersedes
or amends an ADR updates that ADR's `status:` line in the same PR - MADR's
own lifecycle (`superseded by ADR-XXXX` is template vocabulary). This PR
synced 14 stale status lines and two pre-rename H1s (0013/0014, renamed at
0015) under that rule; the 0004/0009 rows dropped (records never landed;
follow-up #92).

## Consequences

- Positive: the index can never drift from the files again; a rebase-time
  README conflict becomes "keep either side, run the script"; the validator
  turns `docs/adr/` into structured data future tooling can consume.
- Negative / accepted trade-offs: eight index titles now render their true
  H1s (hand-written annotations were lost); ADR PRs still touch README.md,
  so adjacent rows can still textually conflict - but conflicts in a
  derived artifact are never semantic: resolution is a command, not an
  edit.

## Links

- Issue #72; discussion #66; follow-up #92 (retroactive 0004/0009 records).
- `scripts/adr_index.py`; `tests/test_adr_index.py`; ADR 0001, 0045, 0047.
