terraform {
  required_version = ">= 1.9, < 2.0"

  required_providers {
    # Verified against the OpenTofu registry 2026-09-03: resolves to v6.13.0.
    github = {
      source  = "integrations/github"
      version = ">= 6.0"
    }
  }
}
