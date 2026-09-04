# AGENTS.md - rules for coding agents working in this repo

This repository manages a production machine. The human approves plans and
reviews PRs; agents implement. These rules are not negotiable:

1. **Secrets**: never write, print, or commit plaintext secrets. Everything
   under `secrets/*.yml` is ansible-vault encrypted (pre-commit + CI enforce).
   Never paste decrypted content into a conversation.
2. **Mutation discipline**: never run `mask tofu-apply`, register runners, join
   tailnets, or call account-mutating APIs without explicit human approval in
   the conversation. `tofu plan`, Molecule, and lint runs are fine.
3. **Verify before finishing**: run `mask ci` and make it pass. Every Ansible
   role needs a Molecule scenario that converges and is idempotent. Every
   Terraform change needs `tofu fmt`, `validate`, and `tflint` clean.
4. **Commits**: conventional-commit messages, small and single-purpose.
5. **Decisions**: read `docs/adr/README.md` first. A new decision gets a new
   ADR; never edit a decided ADR. Keep the README deviation ledger in sync.
6. **Placeholders**: `TODO(you)` = human supplies the value; never invent one.
   `TODO(verify)` = uncertain upstream detail; check docs before relying on it,
   and say so in the phase summary.
7. **Boring over clever**: prefer well-known modules/tools. No shell scripts
   over ~30 lines - use an Ansible module or a tested Python script instead.
8. **Task runner**: `maskfile.md` (mask 0.11.x). CI does not use mask - it
   invokes tools directly.
9. **veggies CLI** (`cli/veggies.py`, ADR 0013/0014): repo-scoped agent stacks.
   Pure renderers + state are pytest-covered in `tests/test_veggies.py`;
   `tests/golden/pod.yaml` is machine-generated (lint-excluded) - regenerate
   it whenever the renderer changes. Never add subPath mounts or tcpSocket
   probes (both verified broken here; see code comments). The vault is read
   only via `scripts/vault_get.py`; secrets travel over stdin only.
