---
status: accepted
date: 2026-09-11
---

# 0055. Generate the ADR index; status and title are living metadata

## Context and problem statement

Every ADR-adding PR hand-appended a row to the same table in
`docs/adr/README.md`, so two parallel ADR PRs (the norm with kicked agent
sessions, ADR 0033/0037) guaranteed an adjacent-line merge conflict. The
index also drifted: it listed 0004/0009, which had no files (retroactive
records landed via #92 while this work was in flight), claimed
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
`test_render_matches_golden`. (The hook runs everywhere pre-commit does,
in-pod included; the byte-compare runs where pytest runs - the focused
verify gate and CI.) Duplicate numbers, bad filenames,
missing/invalid frontmatter, unknown statuses, and H1/filename mismatches
are hard validator errors.

(b) is rejected: it needs a ruleset bypass for bot pushes to main
(ADR 0007/0053 - the main-branch ruleset has no bypass actors), moves
frontmatter validation from PR time to
post-merge-red-main, and catches duplicate numbers no earlier than (a),
which catches them at the second PR's rebase (the ruleset's strict status
policy requires up-to-date branches). The conflict-bot spends tokens
forever on the same
toil and is a trust risk the day it resolves wrong; `merge=union` silently
garbles ordering.

**The duplicate-number race is detected, not prevented**: two parallel PRs
can both claim the next number; the second one's rebase fails the
validator; fix = rename one file, re-run the script. The race fired on
this very PR: main took 0053 (rulesets) and then 0054 (backups) while
this ADR was in flight; each rebase failed the validator on the duplicate,
and this ADR was renamed
to 0055 per the recipe. No allocation
service - that is perpetual machinery to solve a formatting problem.

**The `status:` frontmatter line and the H1 title are living metadata, not
decision text**: decision text stays immutable (AGENTS.md rule 5), but a
PR that supersedes or amends an ADR updates that ADR's `status:` line in
the same PR - MADR's own lifecycle (`superseded by ADR-XXXX` is template
vocabulary). The H1 is the index's title source - this PR corrected the
pre-rename `garden` titles of 0013/0014 (the rename record is 0015). This
PR synced 18 stale status lines (0001 included - this ADR amends its
blanket "never edit" with the metadata exception; 0007/0046/0024 carried
the index's "amended by 0053"/"amended by 0054" claims into the
frontmatter). The 0004/0009 rows
were dropped by the first regeneration as phantoms, then restored by #92's
landed records - the index now renders them from the files like any other
ADR.

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

- Issue #72; discussion #66; #92 (retroactive 0004/0009 records - landed
  while this PR was in flight).
- `scripts/adr_index.py`; `tests/test_adr_index.py`; ADR 0001, 0045, 0047.
