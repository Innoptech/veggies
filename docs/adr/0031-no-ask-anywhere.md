---
status: accepted (amended by 0049)
date: 2026-09-10
---

# 0031. No ask anywhere: agent frontmatter joins the permission envelope

## Context and problem statement

ADR 0029 set a deny-over-ask envelope in `agent-config/opencode.json` but
left two gaps, both verified 2026-09-10 during a dogfooding feature:

- Per-agent frontmatter takes precedence over the global config (0029 said
  so explicitly), and two vendored subagents (`adversarial-review`,
  `security-audit`) carried `bash: "*": ask` with small allowlists. Every
  delegation to them parked or nagged on `rg`, `awk`, `sed`, pipelines, and
  most git subcommands - exactly the tools a reviewer needs.
- The `question` tool stayed at its default (`ask`), the residual park
  risk 0029 deferred. It bites as soon as a headless agent asks a blocking
  question.

## Decision drivers

- Unattended sessions must never park silently (unchanged from 0029).
- Reviewer/auditor roles are read-only by *deny*, not by *ask*: a denied
  edit returns an error immediately and the agent routes around it
  (verified in 0029); an asked anything waits forever.
- One envelope, one place: policy drift between global config and
  frontmatter is what made this tedious.

## Decision

No permission value in `agent-config/` may be `ask` - enforced by
`tests/test_veggies.py::test_no_ask_anywhere` over the global config and
every agent's frontmatter. The two offending agents switch to
`bash: allow` (their deliberate `edit: deny` role constraint stays), and
the global envelope adds `question: deny` so a blocking question fails
fast instead of parking. Widening/narrowing is still one pattern line, a
PR, and a re-up (runbook).

Rejected alternatives:

- **Keeping `ask` for interactive sessions only**: one opencode.json
  serves both TUI and API sessions (0029); splitting it is machinery for
  no gain - an attached user loses nothing when tools just run.
- **Allowlisting more commands per agent**: whack-a-mole; the ask itself
  is the bug, not the size of the allowlist.

## Consequences

- 0029's "per-agent frontmatter overrides still take precedence" and its
  residual `question` risk are amended by this ADR; the rest of 0029
  stands.
- Reviewers can now run the full read-only toolbox (rg, awk, git show...)
  without supervision.
- An agent that genuinely needs input must say so in its final message
  instead of blocking on the `question` tool.

## Links

- Amends: [0029](0029-deny-over-ask-permission-envelope.md)
