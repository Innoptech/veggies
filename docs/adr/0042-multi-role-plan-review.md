---
status: accepted
date: 2026-09-11
---

# 0042. Multi-role plan review: the persona roster reviews every kicked plan

## Context and problem statement

Issue #34 (from discussion #32): "writing the issue / the plans is a
multi agent process that each optimizes for a role via skills." A kicked
session's plan phase was a solo act (0036): one agent framed the
problem, chose the architecture, and judged the value, and the first
review the plan got was the human's. While this issue was in flight its
sibling landed first: [0041](0041-discussion-elaboration-persona-roster.md)
shipped the persona roster - domain-expert, infra-architect, marketer,
seller, cto, read-only subagent agents under `agent-config/agents/` -
for `/elaborate` discussion kicks. Issue #34's own plan anticipated
exactly this: reuse that roster; one definition of roles, two consumers.

## Decision drivers

- One definition of the roster, two consumers - the issue's hard
  requirement. 0041's `PERSONAS` tuple (`scripts/stack_kick.py`) and the
  persona agent files are that definition; a parallel `role-*` skill
  roster (the issue's original packaging guess) would fork the roster
  the day it was born.
- Role review must be driven by versioned files, not ad-hoc prompt text:
  the lens (what each role optimizes for) lives in the persona agent
  file; the kick prompt only routes the draft plan to it.
- Personas are read-only (`mode: subagent`, edit/bash/task/webfetch
  denied - 0041, 0031): exactly the envelope a plan reviewer needs.
- The rest of the 0036 pipeline (subagent execution, adversarial review
  on the diff, `mask ci`, PR) is unchanged - role review refines the
  plan, it does not touch the coding.

## Decision

The issue kick prompt's plan phase gains the fan-out: after the
writing-plans draft, one `task` subagent per persona from `PERSONAS`
reviews the draft through its persona lens and returns its POV. A POV
with no concrete challenge counts as that role's explicit no-objection.
The session consolidates (every challenge either changes the plan or is
answered in the comment) and posts the plan with a `## Role review`
section carrying each role's input or its explicit no-objection - still
before any code, so the human vetoes a plan that already survived five
lenses. The roster renders into the prompt from `PERSONAS` itself, so
the two consumers can never drift textually, and 0041's persona roster
tests stay the single sync guard. The persona agent files gain two
sentences so their output contract names both consumers (a discussion
POV comment for `/elaborate`, the plan's role-review section for issue
kicks).

Rejected: a parallel roster of `role-*` skills under
`agent-config/skills/` (the issue's literal packaging) - it duplicates
0041's roster; the packaging note loses to the single-definition
requirement. Rejected: the main session wearing each hat in turn - one
context, one reasoning style; the value of the committee is fresh eyes
per role.

## Consequences

- Positive: every kicked plan is reviewed from five directions before a
  human spends attention; the posted plan shows each role's verdict, so
  approval is informed, not hopeful.
- Positive: `/elaborate` and issue kicks share one roster, one set of
  lenses, one sync guard - a new role is one persona file plus one
  `PERSONAS` entry, and both consumers light up.
- Negative / accepted: five extra subagent runs per issue kick - tokens
  spent at plan time, deliberately, where a wrong plan costs more.
- Accepted: stacks learn the persona agents on their next `veggies up`
  (the vendored tier is baked at bootstrap, 0019). The persona FILES
  ride the repo, so the prompt carries an explicit fallback - read the
  persona file from the worktree, apply the lens by hand, note it in
  `## Role review` - and the every-persona mandate is never contradicted.

## Links

- Builds on the persona roster of [0041](0041-discussion-elaboration-persona-roster.md);
  amends the plan phase of [0036](0036-always-on-critic-for-kicked-sessions.md);
  roster conventions of [0019](0019-agent-rosters-and-skills.md); kick
  path of [0033](0033-issue-triggered-agent-kicks.md).
- Issue: <https://github.com/Innoptech/veggies/issues/34>;
  discussion: <https://github.com/Innoptech/veggies/discussions/32>
