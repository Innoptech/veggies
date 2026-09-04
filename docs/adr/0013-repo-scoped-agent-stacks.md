---
status: accepted
date: 2026-09-04
---

# 0013. Repo-scoped agent stacks, summoned by the `garden` CLI

## Context and problem statement

The infra repo so far builds one shared agent host. The actual day-to-day want
is different: persistent, long-lived coding environments where one stack
touches exactly one repository, summonable with a single command, runnable
identically on a laptop and on the VPS.

## Decision drivers

- Repo blast-radius: one stack sees one repo - code, keys, and egress included.
- One command: `garden up` (interactive; env/flag overrides for scripting).
- Persistence: stacks survive crashes and reboots, locally and on the VPS.
- Reuse: same images, pins, and allowlists as the Ansible-managed host.
- No new heavyweight deps: stdlib+PyYAML CLI, podman-native primitives.

## Considered options

- Self-contained pod per repo (opencode + litellm + squid) - chosen
- Shared platform pod (one litellm/squid) + per-repo opencode pods
- compose.yaml / docker-compose instead of podman kube play
- mask tasks instead of a CLI

## Decision outcome

**Self-contained pod per repo.** `garden-<name>` = three containers in one pod
netns: `opencode serve` (only container with a published port; the only one
that mounts the repo at /workspace), `litellm` (pod-internal :4000, holds the
stack's copy of the Fireworks key and a random per-stack master key), `squid`
(pod-internal :3128, same allowlist as ADR 0006). Keys are podman secrets
namespaced `garden-<name>-*`, sourced from the vault at `up` time; revoking a
stack's keys = deleting its pod + secrets.

**Kube YAML, not compose.** The CLI renders multi-doc Kubernetes YAML
(PVCs + Pod) and pipes it to `podman kube play -` (stdin; nothing rendered
touches disk outside the state dir). No podman.socket, no compose dependency,
and the same YAML can be persisted via `.kube` quadlets (phase 12) locally and
on the VPS - one artifact for both environments.

**`garden` CLI, not mask tasks.** Interactive prompts (repo defaults to cwd),
`GARDEN_REPO/GARDEN_NAME/GARDEN_HOST` env overrides, subcommands
up/down/ls/attach/logs. Mask stays the infra task runner and wraps the CLI.
The 30-line shell rule makes a Python CLI the honest implementation anyway.

**State**: `~/.local/state/garden/state.json` (0600): name -> repo, mode,
port, host, created. Host opencode ports allocated from 4096 upward.

**RAM budget**: ~650 MB/stack with limits (opencode 512Mi, litellm 768Mi,
squid 128Mi) - about 4 concurrent stacks beside the 2 runners on the 12 GB
VPS; effectively unlimited locally.

## Consequences

- Positive: per-repo isolation including credentials; one command; identical
  local/remote artifacts; drift vs the Ansible side guarded by pytest.
- Negative: N litellm copies cost RAM (budgeted above); the Fireworks key is
  copied into each stack's secret store (contained: podman-secret lifecycle
  owned by the CLI; per-stack master keys mean a leaked stack key is one
  stack's problem).
- Risks carried as TODO(verify): official opencode image tag scheme; litellm
  liveness endpoint; squid pid file and litellm sqlite under
  readOnlyRootFilesystem; git presence in the opencode image. All are
  discharged at the first real `garden up` smoke (runbook checklist).

## Links

- cli/garden.py, tests/test_garden.py, tests/golden/pod.yaml
- ADR 0006 (allowlist), ADR 0009 (rootless podman), ADR 0011 (litellm),
  ADR 0014 (remote stacks)
