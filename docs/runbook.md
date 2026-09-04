# Runbook - veggies

Operational procedures for the agent host. Tested where a test is possible;
the rebuild checklist (section 1) is the acceptance test for the whole repo.

## 1. Rebuild-from-zero checklist (the acceptance test)

Execute this on a second VPS (or a reinstalled veggies) to prove the repo
rebuilds the machine. Every box must be ticked in order.

Provision manually (ADR 0008):

- [ ] Order/reinstall the VPS: Fedora 44 image, your ssh key attached.
- [ ] Point `Host veggies` in `~/.ssh/config` at the new public IP.
- [ ] `ssh veggies true` works as `fedora`.

Prepare the repo locally:

- [ ] `mask setup` ran; `~/.config/infra/vault-password` exists (600).
- [ ] `cp ansible/inventory/group_vars/all.yml.example ansible/inventory/group_vars/all.yml` and fill every `TODO(you)`.
- [ ] `cp terraform/terraform.tfvars.example terraform/terraform.tfvars` and fill it.
- [ ] Vault files filled: `mask vault-edit secrets/model.yml`, `secrets/github.yml`, `secrets/infra.yml`.
- [ ] `mask ci` is green.

Bootstrap:

- [ ] `mask bootstrap` - it MUST prove tailnet SSH before closing public SSH
      (that play cannot be skipped; if it fails, public SSH stays open and you
      fix tailscale first).
- [ ] Update `Host veggies` to the tailnet name (MagicDNS).
- [ ] `ssh veggies true` over the tailnet.

Converge + verify:

- [ ] `mask converge` finishes green and idempotent on rerun.
- [ ] `veggies --host veggies ls` from the operator machine shows stacks; `ssh veggies systemctl --user list-units 'gh-runner*' -M gh-runner@` shows runners.
- [ ] A test PR in a governed repo: `/opencode` comment triggers a job on
      veggies; the PR cannot merge without your review (ADR 0007).
- [ ] Deny test: in a runner, `curl https://example.com` fails and
      `journalctl -k -g infra-egress-deny` shows the drop (ADR 0006).
- [ ] `systemctl list-timers 'backup*'` shows the timers; a manual
      `systemctl start backup` succeeds (needs real restic creds).
- [ ] Restore drill on a scratch dir: section 6.

Tear down the test VPS when done; record deltas as PRs.

## 2. Bootstrap a fresh VPS (details)

VPS variant (ADR 0008): no `tofu apply` - there is no security group on the
VPS product line. The flow is: OVH image + your key -> `mask bootstrap`
(base, crowdsec, tailscale with `base_public_ssh=true`) -> unskippable
tailnet verification -> firewalld removes public ssh. If you ever need public
SSH again: OVH console -> `firewall-cmd --permanent --zone=public
--add-service=ssh && firewall-cmd --reload`, and close it again after
(`mask converge` re-asserts the closed state because `base_public_ssh`
defaults to false).

If tailscale breaks on the host: OVH console (web shell) ->
`tailscale status`, `journalctl -u tailscaled`, re-run with a fresh auth key:
`mask vault-edit secrets/infra.yml` + `mask converge`.

## 3. Secrets: create, edit, rotate

```bash
mask vault-edit secrets/model.yml     # create/edit (ansible-vault)
mask vault-view secrets/infra.yml     # decrypt to stdout
mask vault-rekey                      # change the vault password on all files
mask vault-check                      # CI-style check: everything encrypted
```

Rotation matrix (do these in one PR each):

| Secret | Rotate by |
|--------|-----------|
| fireworks_api_key | new key in Fireworks console -> vault-edit model.yml -> `veggies secrets <name>` per stack (or down/up) -> revoke old |
| per-stack litellm keys | random per stack; rotate with `veggies down <name> --purge` + `veggies up` |
| github_token / App key | new credential -> vault-edit github.yml -> `mask tofu-apply` + converge |
| tailscale_auth_key | new pre-auth key (tagged) -> vault-edit infra.yml -> converge (no-op while Running; only used at join) |
| restic_password | vault-edit infra.yml -> converge; old snapshots need the OLD password - keep it until you prune or re-key the repo |
| converge_ssh_private_key | new keypair -> pubkey into admin_ssh_public_keys (group_vars) -> converge -> vault-edit infra.yml |

Never paste decrypted vault content anywhere - including agent conversations.

## 4. Resize the VPS

OVH panel: VPS -> resize (keeps IP, reboots). Afterwards: adjust
`github_runner_count` / `github_runner_memory_max` / `github_runner_cpu_quota`
in group_vars to the new size, `mask converge`. On the future Public Cloud
path (ADR 0002): change `ovh_flavor_name`, `tofu apply`, confirm the resize.

## 5. Recover a wedged runner

```bash
ssh veggies
systemctl --machine=gh-runner@ --user list-units 'gh-runner@*'
journalctl --machine=gh-runner -M? # see note
systemctl --machine=gh-runner@ --user restart gh-runner@<repo>-1.service
```

- Stuck registration: check `~gh-runner/.config/gh-runner/*.env` freshness and
  `journalctl _UID=<gh-runner uid> -g fetch_runner_token`.
- Token fetch failing: the proxy must be up (`systemctl --machine=egress-proxy@
  --user status squid.service`) - the fetcher egresses through it.
- Full reset of one runner: stop the unit, `rm -rf /srv/gh-runner/<inst>`,
  start the unit (it re-registers and re-creates the work dir).

## 6. Restore from backup

On a fresh machine (after base converge so users exist):

```bash
sudo /usr/local/sbin/restore.sh /etc/restic/restic.env   # or any env file copy
# lists snapshots, requires typing "restore", restores latest into /
```

Verify permissions under /home/* afterwards, then `mask converge` to
re-assert the current state. Exercise this in the section-1 checklist.

Operator-machine note: the tofu state is NOT on veggies (ADR 0008). Back it up
from the workstation with the same restic repo (separate prefix suggested):

```bash
mask vault-view secrets/infra.yml   # source the restic vars from it
# then: restic -r <repo>:terraform backup terraform/*.tfstate*
```

## 7. Add a model provider key

One PR, three edits:

1. `mask vault-edit secrets/model.yml` - add the provider key.
2. `agent-config/litellm/config.yaml` - add the `model_list` entry (and any
   fallback rule).
3. `ansible/inventory/group_vars/all.yml` - add the endpoint to
   `egress_model_endpoints`, then `mask converge` (updates the VPS squid
   allowlist - substrate, ADR 0016).

Stacks pick the new model up at next `veggies up` (agent-config is mounted
at render time); select it via `litellm/<alias>` in agent frontmatter or
`/models`. Existing stacks: `veggies down <name>` + `veggies up`, and
`veggies secrets <name>` if the key changed.

## 8. Add an agent or a skill

- Agent: new file in `agent-config/agents/<name>.md` (frontmatter:
  description, mode, model, permission). PR, merge; stacks pick it up at
  next `veggies up` (recreate running stacks to apply).
- Skill: `agent-config/skills/<name>/SKILL.md` with `name` + `description`
  frontmatter (see opencode skills docs). Same flow.
- Superpowers bump: change the pinned tag in `agent-config/opencode.json`
  (`#vX.Y.Z`), PR; same stack-recreate rollout.

## 9. veggies stacks (ADR 0013/0014)

Daily: `veggies up` in a repo; `veggies attach <name>`; `veggies ls`;
`veggies down <name> [--purge]`. Remote: `veggies --host veggies up --clone
<git-url>` then attach over the tailnet.

Troubleshooting:

- Stack flapping right after up: `veggies logs <name> <container>`. Known-good
  invariants: no subPath mounts (SELinux), exec-probes only (minimal images
  have no `nc`), `pid_filename none` and no `cache_dir null` in squid.conf.
- SELinux denials on a stack: re-run `veggies up` (it re-asserts
  `chcon -R -t container_file_t -l s0` on every bind source). If you
  hand-mount anything new, label it the same way.
- A container stays `exited` under the quadlet: that is expected (systemd
  owns restart there); the `veggies-watchdog.timer` revives it within ~30s.
  Check: `systemctl --user status veggies-watchdog.timer`.
- Remote ops fail with sudo/ssh errors: tailnet up? `ssh veggies true`? The
  stacks user exists only after `mask converge` (base role).
- Rotate a stack's keys: `veggies down <name> --purge && veggies up ...`
  (fresh random master key + fresh copy of the vault's Fireworks key).
