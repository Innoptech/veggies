# Runbook - garden

Operational procedures. Each section names the phase that implements and tests
it. Until a section's phase lands, treat it as a stub.

## 1. Rebuild from zero

TODO - phase 9. Target: a fresh machine goes from OVH order to fully converged
using only this repo, your ssh config, and `~/.config/infra/vault-password`.

## 2. Bootstrap a fresh VPS

TODO - phase 4. VPS variant (ADR 0008): no `tofu apply`. Order with the Fedora
image + your ssh key, point `Host garden` at the public IP, run
`mask bootstrap`, verify tailnet SSH, then the playbook closes public SSH via
firewalld. The verification step cannot be skipped (the playbook enforces it).

## 3. Secrets: create, edit, rotate

TODO - phase 4 (create/edit) and phase 9 (full rotation matrix). Quick ref:

```bash
mask vault-edit secrets/model.yml    # create or edit
mask vault-view secrets/model.yml    # decrypt to stdout
mask vault-rekey                     # change the vault password everywhere
```

## 4. Resize the VPS

TODO - phase 9. OVH panel operation; afterwards adjust `github_runner_*`
variables in `ansible/inventory/group_vars/all.yml` and re-converge.

## 5. Recover a wedged runner

TODO - phase 5. `systemctl --user -M gh-runner@ list-units 'gh-runner*'`,
restart the instance unit, verify re-registration.

## 6. Restore from backup

TODO - phase 8. `ansible/roles/backup/files/restore.sh` on a fresh instance;
exercised end-to-end by the phase 9 rebuild checklist.

## 7. Add a model provider key

TODO - phase 6. Three edits in one PR: `mask vault-edit secrets/model.yml`,
`agent-config/litellm/config.yaml` (model entry), and
`egress_model_endpoints` in group_vars. Converge.

## 8. Add an agent or a skill

TODO - phase 6. Drop a markdown file in `agent-config/agents/` or a
`skills/<name>/SKILL.md` directory; PR; converge. Superpowers bumps are
submodule-ref PRs (ADR 0012).
