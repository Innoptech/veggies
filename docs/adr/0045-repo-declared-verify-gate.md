---
status: accepted
date: 2026-09-11
---

# 0045. Repo-declared verify gate for kicked sessions

## Context and problem statement

The kick-prompt template in `scripts/stack_kick.py` hardcoded this repo's
verify command (`SKIP=actionlint-docker mask ci`) and named `AGENTS.md`
literally. The same template is vendored into every repo the stack serves:
the day a stack is installed on a repo with a different toolchain, the
prompt lies to the agent. Origin: discussion #39, distilled to issue #51.

Rejected options:

1. **Drop the in-pod verify and let PR CI catch it.** The in-pod gate is
   what makes unattended autonomy honest: verified claims only, with the
   in-pod supervisor judging finish claims
   ([0036](0036-always-on-critic-for-kicked-sessions.md)). The PR workflow
   remains the authoritative merge gate either way.
2. **A CLI-owned `ci:` key in veggies.yml.** The CLI owns stack definition
   ([0016](0016-substrate-vs-stack-boundary.md)), and a stack's wiring and
   a repo's workforce are different files by design
   ([0019](0019-agent-rosters-and-skills.md)). The verify gate belongs to
   the repo, not to the stack.

## Decision

1. The repo declares its in-pod verify gate once, in its agent-instruction
   file, as one machine-readable marker line
   `<!-- veggies-verify-gate: CMD -->` surrounded by the human prose that
   remains the visible declaration. Search order: `AGENTS.md`, then
   `CLAUDE.md`; first marker wins.
2. The kick renderer (`scripts/stack_kick.py`) reads the marker at kick
   time - anchored to the vendored script's own repo root
   (`Path(__file__).resolve().parents[1]`), never cwd, because manual
   kicks run from anywhere - and interpolates the command into the
   prompt's verify step.
3. No marker -> the verify step degrades to an advisory pointer: the
   agent-instruction file's prose is the contract, and the agent claims
   only what it actually ran. Advisory BY DESIGN: a blocking gate would
   need schema and validation - that is the veggies.yml `ci:` key this
   ADR deliberately does not build.
4. The marker is a single command string, forever. Scoping judgment lives
   in the surrounding prose (this repo: a narrow diff runs file-scoped
   pre-commit hooks plus targeted tests instead of the flattened
   `--all-files` run; the security hooks gitleaks and vault-check always
   run full-scope). A repo that outgrows one command points the marker at
   a mask/make target.
5. The resolved gate is echoed per kick (kick-script stdout,
   `GITHUB_OUTPUT`, one line in the workflow's session-link comment) so a
   typo'd marker is a visible event, not a silent degrade.
6. The PR workflow remains the authoritative full gate; the declared gate
   is the fast in-pod loop.

## Consequences

- Positive: on this repo the declared command is byte-identical to the
  previously hardcoded one (`SKIP=actionlint-docker mask ci`) - pinned by
  a pytest that parses the real `AGENTS.md`, including a prose<->marker
  lockstep assertion so a docs edit that drops either half fails loudly.
  Gate changes ship through ordinary PR review of the default branch - no
  CLI release. A second repo costs one marker line.
- Negative / accepted: the harness image's baked toolchain
  ([0032](0032-dev-toolchain-in-harness-image.md)) is the remaining
  dogfood boundary - a foreign gate needs an image that can run it; the
  repo owner owns that toolchain story, and image generalization is a
  separate seam, not this ADR. The HTML-comment marker is invisible in
  rendered markdown; mitigated by the lockstep test and by the rule-3
  prose that names it.
- Bootstrap self-heal: the PR introducing the marker kicks sessions whose
  prompt renders pre-marker; they degrade to the prose path, which
  post-merge carries the declaration - the fallback covers the race by
  design.

## Links

- Discussion #39, distilled to issue #51.
- [0016](0016-substrate-vs-stack-boundary.md),
  [0019](0019-agent-rosters-and-skills.md),
  [0028](0028-retire-canvas-own-the-critic-loop.md) (molecule's in-pod
  exclusion), [0032](0032-dev-toolchain-in-harness-image.md) (baked
  toolchain), [0033](0033-issue-triggered-agent-kicks.md) (kicks).
