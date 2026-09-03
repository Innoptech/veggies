# terraform/ovh - Public Cloud compute (SCAFFOLD, gated)

**Not applied.** Today's `garden` is a manually-rented VPS (ADR 0008); this
module is the reviewed-in-advance migration path to OVH Public Cloud
(ADR 0002). The root module calls it with `count = var.enable_ovh ? 1 : 0`
and `enable_ovh` defaults to `false`.

## What it would create

- `openstack_compute_instance_v2` (flavor/image from variables - never
  hard-coded), `stop_before_destroy = true`
- keypair, security group (Tailscale UDP 41641 + temporary SSH from
  `admin_cidr` only; emptying `admin_cidr` is the bootstrap close-out),
- 200 GB data volume + attachment (runner work dirs, `/srv`),
- minimal cloud-init: admin user, ssh key, python3 - nothing else.

## Be careful

- `flavor_name` and `image_name` are placeholders: verify against the target
  region's catalog (`openstack flavor list` / `image list`) before ever
  enabling. Flavor names differ per region.
- `Ext-Net` as public network name is a `TODO(verify)`.
- Resize = change `flavor_name` + apply + confirm resize (runbook section 4).
