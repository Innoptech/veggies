terraform {
  required_version = ">= 1.9, < 2.0"

  required_providers {
    # Verified against the OpenTofu registry 2026-09-03: resolves to v3.4.0.
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = ">= 3.0"
    }
  }
}
