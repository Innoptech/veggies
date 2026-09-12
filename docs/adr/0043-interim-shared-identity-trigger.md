---
status: accepted (interim - sunsets at the GitHub App agent identity; extended by 0050)
date: 2026-09-11
---

# 0043. Interim: the shared olgam4 identity may trigger kicks

## Context and problem statement

[0040](0040-self-trigger-guard.md) hardcoded `comment.user.login != 'olgam4'`
into both comment triggers, on the assumption that olgam4 is a dedicated bot
account. It is not: olgam4 is the operator's *main* GitHub account, and the
GitHub App migration (the vault's `github_app_*` block, deferred in
[0030](0030-opt-in-github-write-credentials-in-stacks.md)) has not happened.
Effect, verified 2026-09-11 on discussion #39: the operator's own `/elaborate`
comments were skipped by the gate twice (runs 34622735672, 34622617844) -
the human was locked out of triggering by a guard aimed at the agent.

With a shared identity, no server-side signal distinguishes human-olgam4 from
agent-olgam4 (same login, same association). The choice is binary: either
olgam4 can trigger or it cannot. The operator needs to trigger.

## Decision

Drop the login exclusion from both comment triggers in
`agent-trigger.yml`; the command anchoring (`startsWith`) and the
OWNER/MEMBER/COLLABORATOR association gate stay. Bound the self-kick loop
that 0040's exclusion used to prevent:

1. **In-flight guard extends to discussions** (`stack_kick.py`): a busy
   session titled `D#<N>: ` or `D#<N> elaborate: ` skips the kick with
   exit 3, exactly like the issue-mode guard. Any self-kick loop becomes
   serial - a new kick cannot start while the previous session on the same
   subject is still working.
2. **Prompt hygiene**: every kick-prompt template forbids the agent from
   *starting* a GitHub comment with `/opencode` or `/elaborate` - the only
   remaining self-kick vector under the command-anchored gate.
3. Unchanged bounds: the issue done-guard (0035) caps issue re-kicks once a
   PR exists; the one-shot label clears after kicks; every kick/skip
   comments on the subject (0034/0039), and the skip feedback now covers
   discussions too.

## Consequences

- Positive: the operator triggers kicks as themselves again; discussions
  gain the double-book protection issues already had; skip feedback lands
  on discussions (previously silent).
- Negative / accepted: **there is no hard stop on discussion self-kicks
  until the App identity lands.** A sufficiently prompt-injected session
  could post a leading-command comment; issues are bounded by the
  done-guard, discussions only serially - but every kick is loud (Actions
  comment + titled session + supervisor log line). This trade is accepted
  because the alternative is the operator being unable to use the system.
- Sunset: the GitHub App agent identity (agent comments become
  `<app>[bot]`, association NONE) restores a hard exclusion - this ADR is
  superseded by it. Amends the bot-exclusion part of
  [0040](0040-self-trigger-guard.md); its command anchoring and in-flight
  guard stand.
