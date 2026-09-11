---
status: accepted
date: 2026-09-11
---

# 0044. No-ask over the merged roster; repo instruction files stay configuration-free

## Context

Issue #54 (discussion #39): a repo veggies is installed on may ship
CLAUDE.md (not AGENTS.md) and its own `.opencode/`/`.claude/` agents and
skills. ADR 0019 verified roster/skill discovery; two gaps remained, plus
a trust edge the thread said must ship with them.

Verified 2026-09-11 against the pinned harness (opencode 1.18.27 source):

- `session/instruction.ts`: `instructionFiles = ["AGENTS.md", "CLAUDE.md",
  "CONTEXT.md"]` - the harness walks up from the session dir to the
  worktree root and loads the first match into the system prompt. A
  CLAUDE.md-only repo's conventions were already entering every session;
  only the kick prompt's wording ("Read AGENTS.md first") lagged.
- `config.ts` + `config/paths.ts`: the project tier (`opencode.json[c]`,
  `.opencode/opencode.json[c]`, `{agent,agents,mode,modes}/**/*.md` under
  config dirs, plus the `.claude/`/`.agents/` compat paths of 0019) merges
  OVER the global envelope - and inline `agent:`/`mode:` blocks in a JSON
  config carry their own `permission` trees. A project-tier `ask`
  therefore always reaches the live session: it reintroduces the park 0031
  banned.

## Decision

1. **Instruction files stay configuration-free.** No `instructions` globs
   in the rendered opencode.json (the default already covers
   AGENTS.md/CLAUDE.md; a duplicate mechanism drifts on every image bump)
   and no veggies.yml keys. The kick prompt names "the repo's own
   agent-instruction file - whichever of AGENTS.md/CLAUDE.md (or
   equivalent) the repo ships". A dated tripwire comment at
   `IMAGE_OPENCODE` pins the verified discovery list to 1.18.27.
2. **No-ask spans the merged roster** (amends 0031's `agent-config/`
   scope). The enforcement artifact is `cli/permission_envelope.py`,
   stdlib-only: `scan_project_tier()` flags any `ask` in a repo's project
   tier (parsed JSON configs - top-level `permission` AND inline
   `agent.*.permission`; agent/mode frontmatter via text scan; JSONC
   comments stripped; unparseable fails closed with "cannot verify
   ask-free"). pytest covers it over fixtures and this repo
   (`test_no_ask_anywhere` now scans the merged tier).
3. **The bite for target repos is the kick gate**: `stack_kick.py`
   refuses to kick (exit 3 + a `skip_reason` naming file and dotted path,
   which the workflow comments onto the subject) when the checked-out
   repo's project tier carries `ask`. Scanner/import errors degrade to
   proceeding - a hiccup never blocks a deliberate kick (the done-guard
   posture); only a verified `ask` does.

Rejected / deferred:

- **`veggies up` gate**: half-blind by construction - mount-mode post-up
  edits reintroduce `ask` without passing any up-time check, and the only
  true chokepoint is upstream's config load. The CLI<->repo boundary is
  veggies.yml-only by design (0016/0019). The validator is placed so a
  future up-time call is a one-liner if a concrete external repo ever
  needs it.
- **Blocking permission *widening*** (project allowing what global
  denies): `ask` is policed because it is a *liveness* failure in
  unattended sessions; widening is *trust*, already answered by 0019's
  "do not run stacks on repos you wouldn't hand a shell to" plus PR review
  of `.opencode/` as code.
- **Full JSONC parse** (trailing commas et al.): comments are stripped
  string-aware; residue fails closed with a clear message. No dependency,
  no clever parser (AGENTS.md rule 7).

## Consequences

- A CLAUDE.md-native repo: kicked sessions read its file, its `.claude/`
  roster works as-is, and a config carrying `ask` gets its kick refused
  with a comment naming the offending file - "install veggies" never
  becomes "convert your repo".
- Enforcement boundary, stated exactly: the pytest invariant pins the
  rule's shape and this repo's own tiers (global + project); the kick
  gate enforces the kicked path over the workflow checkout; interactive
  mount-mode stacks ride 0019's trust model.
- The gate scans origin/main (the workflow checkout) while the live merge
  reads the mounted clone; a lagging clone can refuse a kick whose `ask`
  has not reached the stack yet - fail direction is safe and `veggies
  sync` closes the gap.

## Links

- Amends: [0031](0031-no-ask-anywhere.md)
- Builds on: [0019](0019-agent-rosters-and-skills.md)
