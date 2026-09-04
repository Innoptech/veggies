---
status: accepted
date: 2026-09-03
---

# 0005. Ephemeral containerised runners

## Context and problem statement

`/opencode` and `/oc` comments on GitHub issues/PRs execute agent workloads on
this machine. Those workloads process untrusted input. A persistent,
long-lived runner with a dirty workspace would let one job poison the next.

## Decision drivers

- Every job starts from a clean container; blast radius = one container.
- No long-lived runner registration token on disk.
- Resource use must be capped so agents cannot starve the host.
- Rootless (ADR 0009): even a container escape lands in an unprivileged user.

## Considered options

- Ephemeral runner containers under systemd (quadlet template + instances)
- One long-lived runner service per repo
- actions-runner-controller / k8s

## Decision outcome

A quadlet template `gh-runner@.container` under the `gh-runner` user with one
symlinked instance per (repo x slot). systemd restarts the unit after each
job; `ExecStartPre` fetches a fresh short-lived registration token from the
GitHub API (`fetch_runner_token`, Python + unit tests) using a PAT/App from
the vault - the runner registers with `--ephemeral`, runs one job, exits.
Mounts: only the per-instance work dir and the user's rootless podman socket
(workflow `docker` steps become sibling rootless containers). Defaults:
2 runners per repo, `MemoryMax=4G`, `CPUQuota=250%`.

## Consequences

- Positive: clean jobs, capped resources, no stored runner tokens, per-repo
  scaling via variables.
- Negative: token fetch at every start needs the API reachable (it is, via
  the host); job latency includes registration (~seconds).
- Limitation accepted: no privileged jobs / no true DinD under rootless
  podman.

## Pros and cons of the options

### Ephemeral quadlets

- Good: simple systemd lifecycle; tested with Molecule; no orchestrator.
- Bad: registration chatter per job.

### Long-lived runner

- Good: less API traffic.
- Bad: state leaks between jobs; a stored config token is a standing secret.

### Kubernetes

- Good: scales.
- Bad: a k8s cluster for one box is the opposite of boring.

## Links

- ansible/roles/github_runner, ADR 0006 (egress), ADR 0009 (podman/quadlet)
