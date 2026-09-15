---
status: proposed
date: 2026-09-15
---

# 0065. Cloud substrate: GCP Compute Engine in a dedicated project (`terraform/gcp/`)

> **Proposed - team review gate.** Part of the control-plane set 0064-0069.

## Context and problem statement

[0002](0002-public-cloud-over-vps.md) chose OVH Public Cloud as the target
substrate and [0008](0008-interim-platform-vps-fedora-local-state.md)
parked a manually rented OVH VPS with local tofu state as the interim.
[0020](0020-cloud-substrate-module.md) left "GCP or other" as open
questions. Reality on 2026-09-15: the OVH VPS was a quick test on the
operator's personal account, Innoptech's real cloud is GCP (the operator is
authenticated with project rights), and the multi-user design needs one
VM per user plus a control-plane host - a fleet, not a box.

## Decision drivers

- Company-owned, reviewable, reproducible infrastructure; no personal
  accounts in the path.
- One VM per user as the isolation unit
  ([0066](0066-isolation-unit-one-vm-per-user.md)); nodes appear and
  disappear by reviewed change.
- Agent nodes keep zero public ingress; one deliberate public surface on
  the control-plane host ([0064](0064-innoptech-tailnet-headscale-dex-github-oidc.md)).
- Remote, locked tofu state so more than one operator (and CI) can apply.
- Boring: Fedora Cloud images, the `google` provider, no Kubernetes.

## Considered options

- OVH Public Cloud (0002) via the `openstack` provider.
- **GCP Compute Engine** via `terraform/gcp/`, new dedicated project (chosen).
- Keep renting VPSes by hand, one per user.

## Decision outcome

1. A **new dedicated GCP project** (name `TODO(you)`, e.g.
   `innoptech-veggies`), created once by the operator with billing linked
   and the compute / IAM / storage / IAP / billing-budgets APIs enabled
   (runbook recipe, under ten `gcloud` lines).
2. `terraform/gcp/` provisions, from the public `fedora-cloud` image
   family (TODO(verify) the Fedora 44 GCE family name, SELinux Enforcing,
   cloud-init): one **main node** (`veggies-main`, `e2-standard-4`) and a
   `user_nodes` map keyed by lowercase GitHub login -> one
   `veggies-u-<login>` instance each (`e2-standard-2` default, per-user
   override, optional instance schedule). Root module gates it with
   `enable_gcp` (the pattern the ovh call used). `terraform/ovh/` is
   deleted.
3. **Ingress posture**: main node 80/443 public (Caddy) + UDP 41641; tcp:22
   only from `bootstrap_admin_cidr`, a tofu variable emptied at bootstrap
   close-out (the ovh scaffold's `admin_cidr` idea). User nodes: no public
   ingress at all (GCP default-deny) except UDP 41641; ephemeral external
   IPs for direct WireGuard paths, **no Cloud NAT**. IAP TCP forwarding
   (`35.235.240.0/20` -> 22) is the break-glass on every node.
4. **Join at boot**: cloud-init installs tailscale and runs `tailscale up
   --login-server <headscale> --auth-key <tagged single-use key>
   --hostname <name> --ssh`; the keys live in the vault
   (`headscale_node_preauth_keys` map) and reach tofu as a sensitive
   `TF_VAR`. TODO(verify)/accepted: the key is readable in instance
   metadata by project IAM until it is consumed (single-use, one-hour
   expiry).
5. **Inventory is generated**: `scripts/gcp_inventory.py` renders
   `ansible/inventory/20-gcp.yml` (groups `main`, `agents`; hosts by
   MagicDNS name) from `tofu output -json`; `ansible.cfg` points at the
   inventory directory; Ansible reaches nodes only over the tailnet.
6. **Remote state**: a versioned GCS bucket with public-access prevention
   in the new project, created once outside tofu; `backend "gcs"` with
   `tofu init -migrate-state` moving today's local state (github module
   included). Closes 0008's "local state for now".
7. **Lifecycle split**: creation, resize and deletion of nodes are tofu
   changes reviewed as PRs; start/stop of existing user nodes is the
   control plane's job ([0067](0067-control-plane-veggies-core-web-app.md))
   through a service account limited to `compute.instances.{start,stop,
   get,list}` on `veggies-u-*` (TODO(verify) IAM condition support);
   `lifecycle { ignore_changes = [desired_status] }` keeps app stop/start
   out of the plan.
8. OVH `veggies` is transitional: smoke host while the GCP path lands,
   removed from inventory and cancelled at the first GCP user node
   ([0066](0066-isolation-unit-one-vm-per-user.md)).

## Consequences

- Positive: the whole fleet is `tofu plan`-able; adding a user is one map
  entry and one vault key; agent nodes are dark by construction; state is
  shared and locked; the substrate matches the company's cloud.
- Negative / accepted: monthly cost scales with headcount (idle stop
  mitigates, [0067](0067-control-plane-veggies-core-web-app.md)); an
  unconfigured `google` provider at `enable_gcp=false` must be verified to
  plan cleanly (else the module call stays a commented gate like ovh's);
  a public 80/443 exists on `veggies-main` - named in the threat model.
- On acceptance: 0002 and 0008 (platform and state parts) and 0020 ->
  `superseded by ADR-0065`; ledger rows "Public Cloud via tofu" -> GCP,
  "Remote S3 state" -> GCS, new row "zero inbound public ports -> 80/443
  on `veggies-main` accepted; agent nodes stay dark".

## Pros and cons of the options

### OVH Public Cloud

- Good, because 0002 already scaffolded it.
- Bad, because it is not where Innoptech's accounts, IAM and billing live;
  the scaffold was never applied and the VPS was a personal-account test.

### GCP Compute Engine (chosen)

- Good, because company-owned project, mature provider, IAP for
  break-glass, GCS for state, budgets for cost alerts.
- Bad, because a second cloud-provider vocabulary enters the repo; kept
  small by using only instances, firewall rules, service accounts and a
  bucket.

### Hand-rented VPSes per user

- Good, because nothing to learn.
- Bad, because it models buying, not operating (0002's own objection);
  no fleet view, no IAM, no reproducibility.

## Links

- 0002, 0008, 0020, 0024, 0064, 0066, 0067, 0069; `terraform/gcp/README.md`
  (to be written), runbook "GCP project bootstrap" (to be written).
