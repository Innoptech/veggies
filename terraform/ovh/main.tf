# OVH Public Cloud compute - SCAFFOLD ONLY (ADRs 0002, 0008).
# Gated by var.enable_ovh at the root; never applied while today's veggies is a
# manually-rented VPS. Exists so the future migration is a reviewed PR, not an
# afternoon of clicking.

resource "openstack_compute_keypair_v2" "admin" {
  name       = "${var.instance_name}-admin"
  public_key = var.ssh_public_key
  region     = var.region != "" ? var.region : null
}

# Security group: Tailscale UDP + TEMPORARY ssh from the operator's current IP.
# The SSH rule's source is emptied by bootstrap close-out (ADR 0003): after the
# tailnet is verified, admin_cidr becomes null and public SSH disappears.
resource "openstack_networking_secgroup_v2" "veggies" {
  name        = var.instance_name
  description = "veggies: tailscale UDP + temporary bootstrap SSH"
  region      = var.region != "" ? var.region : null
}

resource "openstack_networking_secgroup_rule_v2" "tailscale_udp" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "udp"
  port_range_min    = var.tailscale_udp_port
  port_range_max    = var.tailscale_udp_port
  remote_ip_prefix  = "0.0.0.0/0" # WireGuard endpoint; peer auth happens in WireGuard itself
  security_group_id = openstack_networking_secgroup_v2.veggies.id
  region            = var.region != "" ? var.region : null
}

resource "openstack_networking_secgroup_rule_v2" "bootstrap_ssh" {
  count             = var.admin_cidr != "" ? 1 : 0
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = var.admin_cidr
  security_group_id = openstack_networking_secgroup_v2.veggies.id
  region            = var.region != "" ? var.region : null
}

resource "openstack_blockstorage_volume_v3" "data" {
  name   = "${var.instance_name}-data"
  size   = var.volume_size_gb
  region = var.region != "" ? var.region : null
}

resource "openstack_compute_instance_v2" "veggies" {
  name            = var.instance_name
  flavor_name     = var.flavor_name
  image_name      = var.image_name
  key_pair        = openstack_compute_keypair_v2.admin.name
  security_groups = [openstack_networking_secgroup_v2.veggies.name]
  region          = var.region != "" ? var.region : null
  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    admin_user     = var.admin_user
    ssh_public_key = var.ssh_public_key
  })

  # TODO(verify): OVH's public network name per region ("Ext-Net" is typical).
  network {
    name = "Ext-Net"
  }

  # In-place resize (flavor change) is supported by the provider; the runbook
  # documents the confirm/accept step.
  stop_before_destroy = true
}

resource "openstack_compute_volume_attach_v2" "data" {
  instance_id = openstack_compute_instance_v2.veggies.id
  volume_id   = openstack_blockstorage_volume_v3.data.id
  region      = var.region != "" ? var.region : null
}
