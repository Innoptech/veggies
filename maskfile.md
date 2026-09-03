# Infra task runner

Commands run from the repo root (`mask help` lists all).
Python tools live in `.venv/` (see README setup); binaries (tofu, gitleaks,
tflint, actionlint) live in `~/.local/bin`.

## setup

> One-time local setup: python venv, pinned tools, pre-commit hooks.

```bash
set -euo pipefail
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pre-commit install
echo "Done. Now create ~/.config/infra/vault-password (chmod 600) - see README."
```

## precommit

> Run all pre-commit hooks over the whole tree (gitleaks, yamllint, actionlint, tofu fmt, vault check, ansible-lint).

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$HOME/.local/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
pre-commit run --all-files
```

## ci

> Run the same checks the infra-ci workflow runs.

```bash
set -euo pipefail
mask precommit
mask tofu-validate
mask tflint
mask ansible-lint
```

## tofu-fmt

> Check HCL formatting (fails like CI). Use tofu-fmt-write to fix.

```bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
tofu fmt -check -recursive terraform/
```

## tofu-fmt-write

> Auto-format all HCL.

```bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
tofu fmt -recursive terraform/
```

## tofu-validate

> Init without backend and validate the root module (offline-safe, no state).

```bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd terraform
tofu init -backend=false -input=false
tofu validate
```

## tofu-plan

> Read-only plan. Exports TF_VAR_* from the vault to the process env (never to disk).

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$HOME/.local/bin:$PATH"
eval "$(python scripts/tfvars_from_vault.py secrets/github.yml secrets/model.yml secrets/infra.yml)"
cd terraform && tofu plan
```

## tofu-apply

> MUTATES LIVE ACCOUNTS / MAY COST MONEY. Requires typing "apply".

```bash
set -euo pipefail
echo "STOP: 'tofu apply' mutates live GitHub/cloud accounts and may cost money."
echo "Only proceed with explicit human approval in the conversation."
printf "Type 'apply' to continue: "
read -r answer
[ "$answer" = "apply" ] || { echo "Aborted."; exit 1; }
export PATH="$PWD/.venv/bin:$HOME/.local/bin:$PATH"
eval "$(python scripts/tfvars_from_vault.py secrets/github.yml secrets/model.yml secrets/infra.yml)"
cd terraform && tofu apply
```

## tflint

> Lint HCL with the bundled terraform ruleset.

```bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
tflint --recursive
```

## ansible-lint

> Lint all Ansible content with the production profile.

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
if [ -d ansible/roles ] && [ -n "$(ls -A ansible/roles)" ]; then
  ansible-lint
else
  echo "No roles yet (roles arrive in phase 4); nothing to lint."
fi
```

## molecule-test (role)

> Run the Molecule scenario for one role: mask molecule-test base

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
[ -d "ansible/roles/$role/molecule" ] || { echo "No molecule scenario for role '$role'"; exit 1; }
cd "ansible/roles/$role"
molecule test
```

## molecule-all

> Run every role's Molecule scenario (podman driver).

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
found=0
for d in ansible/roles/*/; do
  [ -d "$d/molecule" ] || continue
  found=1
  (cd "$d" && molecule test)
done
[ "$found" -eq 1 ] || echo "No roles with molecule scenarios yet (phase 4+)."
```

## vault-edit (file)

> Create or edit an encrypted secrets file: mask vault-edit secrets/model.yml

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
ansible-vault edit "$file"
```

## vault-view (file)

> Decrypt to stdout: mask vault-view secrets/model.yml

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
ansible-vault view "$file"
```

## vault-rekey

> Change the vault password on all encrypted files under secrets/.

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
ansible-vault rekey secrets/*.yml
```

## vault-check

> Fail if any committed secrets/*.yml is not vault-encrypted.

```bash
set -euo pipefail
scripts/check_vault_encrypted.sh
```

## converge

> Full Ansible run against garden (playbooks arrive in phase 4).

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
[ -f ansible/playbooks/site.yml ] || { echo "ansible/playbooks/site.yml arrives in phase 4"; exit 1; }
cd ansible && ansible-playbook playbooks/site.yml --limit garden
```

## bootstrap

> First-run playbook over the public IP (phase 4). Ends by closing public SSH.

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"
[ -f ansible/playbooks/bootstrap.yml ] || { echo "ansible/playbooks/bootstrap.yml arrives in phase 4"; exit 1; }
cd ansible && ansible-playbook playbooks/bootstrap.yml --limit garden
```
