terraform {
  required_version = ">= 1.9, < 2.0"

  required_providers {
    # Verified against the OpenTofu registry 2026-09-03: resolves to v6.13.0.
    github = {
      source  = "integrations/github"
      version = ">= 6.0"
    }
    # Only used by the gated ovh/ scaffold module (ADRs 0002, 0008).
    # Never applied while var.enable_ovh = false.
    # Verified against the OpenTofu registry 2026-09-03: resolves to v3.4.0.
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = ">= 3.0"
    }
  }
}
