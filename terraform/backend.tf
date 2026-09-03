# State: LOCAL for now (ADR 0008).
# terraform.tfstate stays on the operator machine, is gitignored, and is
# covered by the restic backups from phase 8. No remote backend is configured
# on purpose; the absence of a `backend` block IS the local backend.
#
# Migration target (recorded in ADR 0002/0008): OVH S3-compatible object
# storage with lockfile, roughly:
#
#   terraform {
#     backend "s3" {
#       bucket = "<bucket>"    # backend blocks cannot use variables
#       key    = "infra.tfstate"
#       ...
#     }
#   }
#
# TODO(you): when migrating, write the real block with literal values and run
# `tofu init -migrate-state`.
