---
status: accepted
date: 2026-09-03
---

# 0012. Vendored agent-config baseline; Superpowers as a pinned opencode plugin

## Context and problem statement

The brief put all agent configuration in project repos. The operator also
wants a shared baseline on this machine: a known set of agents
(swe-expert, adversarial-review, pr-explainer, tdd-tester), easy-to-add
skills, and the Superpowers methodology as the base layer.

## Decision drivers

- Baseline changes must be PRs like everything else.
- Superpowers must not drift unpinned; no remote installer scripts on the
  server.
- Project repos keep their own AGENTS.md/skills - opencode merges global
  (`~/.config/opencode/`) with project config.

## Considered options

- Vendored `agent-config/` in this repo, synced by the opencode role
- A separate git repo cloned by ansible
- Superpowers' own installer flow on the server

## Decision outcome

Vendored `agent-config/` in this repo: `opencode.json` (litellm provider,
default models, skill permissions, `share: disabled`), `agents/*.md`,
`skills/*/SKILL.md`, and `litellm/config.yaml`. The opencode role copies it
into `~/.config/opencode/` each converge. The original brief's "clone a repo
I name" remains available via `opencode_config_repo`.

Superpowers ships as an opencode plugin pinned by git tag in
`opencode.json` (`superpowers@git+...#v5.0.3` - TODO(verify) latest tag at
converge time); opencode's own plugin manager fetches it (no remote install
script runs). `SUPERPOWERS_DISABLE_TELEMETRY=1` is set in the quadlet.

## Consequences

- Positive: one reviewable place for the baseline; pinned, auditable
  Superpowers; adding an agent/skill/provider key is a one-PR recipe in the
  README/runbook.
- Negative: repo contains both machine config and agent content (accepted;
  they are versioned together deliberately).
- Submodule was considered and dropped: the pinned plugin line is simpler and
  fetched by opencode's own manager.

## Pros and cons of the options

### Vendored in infra

- Good: single PR stream; Molecule-testable sync; no extra repo.
- Bad: baseline and machine config share a review queue.

### Separate config repo

- Good: cleaner separation of concerns.
- Bad: second repo to review; the role would depend on external state.

### Superpowers installer on the server

- Good: exactly upstream's flow.
- Bad: executes remote instructions outside review; rejected.

## Links

- agent-config/, ansible/roles/opencode, ADR 0011
