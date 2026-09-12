---
status: accepted
date: 2026-09-03
---

# 0004. Secrets in git via ansible-vault (supersedes the brief's sops+age)

## Context and problem statement

The machine this repo manages needs real credentials: provider API keys,
GitHub tokens, a tailscale auth key. Everything else here is reviewable git
content, so secrets belong in git too - as ciphertext only. The original
project brief specified sops + age for secrets in git; this decision
supersedes that line. The decision was taken on 2026-09-03 in the founding
conversation and recorded retroactively on 2026-09-12 per issue #92,
reconstructed from the deviation ledger, the ADRs that cite it (0007,
0008), and the code that embodies it.

## Decision drivers

- Ansible is already in the pinned toolchain; ansible-vault adds no new
  binary and no new key material. An age keypair would be a second secret
  to protect.
- One vault password; ciphertext diffs ride the normal git review flow.
- Enforcement must be mechanical: a pre-commit hook
  (`scripts/check_vault_encrypted.sh`) plus CI, with gitleaks as the
  second net.

## Considered options

- ansible-vault on `secrets/*.yml`
- sops + age (the brief's pick)

## Decision outcome

ansible-vault for everything under `secrets/*.yml`, with plaintext
`.example` templates documenting the key structure. One password, kept at
`~/.config/infra/vault-password` (0600) on the operator machine and handed
out of band. Secrets travel over stdin/env only: `scripts/vault_get.py`
prints a single key to stdout, `scripts/tfvars_from_vault.py` exports
`TF_VAR_*` to the calling shell's environment - never to disk. The
`vault-encrypted` pre-commit hook and the CI pre-commit job fail any
`secrets/*.yml` that is not ciphertext; gitleaks scans for what slips
past. AGENTS.md rule 1 states the rule to every agent.

## Consequences

- Positive: reviewable ciphertext diffs in the normal PR flow; no extra
  tooling beyond the pinned ansible; encryption enforced at the hook and
  in CI, not by convention.
- Negative / accepted: one password unlocks everything - the threat model
  carries a leaked-vault-password row for it. Rotating a secret =
  vault-edit + converge (runbook section 3); rotating the password =
  `mask vault-rekey`.

## Pros and cons of the options

### ansible-vault

- Good: ansible is already required and reads the files natively; the
  password file is the only new secret; diffs are reviewable ciphertext.
- Bad: encryption is whole-file - a one-key change rewrites the whole
  blob - and one password is an all-or-nothing trust boundary.

### sops + age

- Good: per-value encryption keeps the file structure readable in diffs.
- Bad: a second tool to pin and an age keypair that becomes a standing
  secret itself; ansible has no native reader for it.

## Links

- Cited by: ADR 0007 (github policy as code), ADR 0008 (interim
  platform), [docs/threat-model.md](../threat-model.md) (vault-password
  row)
- Later: ADR 0030 (opt-in GitHub write credentials from the vault),
  ADR 0033 (vault-fed kicks), ADR 0048 (serve password from the vault key)
- Issue: <https://github.com/Innoptech/veggies/issues/92>
