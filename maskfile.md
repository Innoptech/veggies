# Infra task runner

Commands run from the repo root (`mask help` lists all).
Python tools live in `.venv/` (see README setup); binaries (tofu, gitleaks,
tflint, actionlint) live in `~/.local/bin`.

## setup

> One-time local setup: python venv, pinned tools, pre-commit hooks.

```bash
set -euo pipefail
if [ -e .venv ] && ! .venv/bin/python --version >/dev/null 2>&1; then
  echo ".venv exists but its interpreter is gone (checkout moved? venvs are not relocatable) - recreating"
  rm -rf .venv
fi
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pre-commit install
# Pinned lint/infra binaries for the language:system pre-commit hooks and
# mask's tofu/tflint tasks (ADR 0032/0045): the header's "~/.local/bin"
# claim made true. Keep versions+sha256 in sync with
# deploy/images/opencode.Containerfile - tests/test_tool_pins.py enforces.
mkdir -p "$HOME/.local/bin"
[ "$(uname -sm)" = "Linux x86_64" ] ||
  { echo "mask setup installs linux/amd64 binaries only - install tofu/tflint/gitleaks/actionlint manually on this host"; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
(
  cd "$tmp"
  curl -fsSL -o tofu.zip "https://github.com/opentofu/opentofu/releases/download/v1.12.6/tofu_1.12.6_linux_amd64.zip"
  echo "5dc43da4f750f33873dc25e94587128709e819e544b7be9016b255316153c3a8  tofu.zip" | sha256sum -c -
  unzip -q tofu.zip tofu
  curl -fsSL -o tflint.zip "https://github.com/terraform-linters/tflint/releases/download/v0.64.0/tflint_linux_amd64.zip"
  echo "cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537  tflint.zip" | sha256sum -c -
  unzip -q tflint.zip tflint
  curl -fsSL -o gitleaks.tar.gz "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"
  echo "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  gitleaks.tar.gz" | sha256sum -c -
  tar xzf gitleaks.tar.gz gitleaks
  curl -fsSL -o actionlint.tar.gz "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz"
  echo "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8  actionlint.tar.gz" | sha256sum -c -
  tar xzf actionlint.tar.gz actionlint
  install -m 0755 tofu tflint gitleaks actionlint "$HOME/.local/bin/"
)
echo "Done. Now create ~/.config/infra/vault-password (chmod 600) - see README."
```

## precommit

> Run all pre-commit hooks over the whole tree (gitleaks, yamllint, actionlint, tofu fmt, vault check, ansible-lint).

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$HOME/.local/bin:$PATH"
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

> Init without backend and validate the root module (no state; init still fetches the registry, see export).

```bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
# Verified in-pod (issue #50): tofu init fetches the registry discovery doc
# on EVERY run, even with all providers cached, and the default registry
# client timeout is 10s - cold in-pod egress through the chained squid
# measured 20-35s, so init flaked. 120s = ~3x the worst observed cold fetch,
# headroom for parallel sessions contending for the same proxy.
export TF_REGISTRY_CLIENT_TIMEOUT=120
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
export GITHUB_TOKEN="${TF_VAR_github_token:-}" # the github provider's auth
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
export GITHUB_TOKEN="${TF_VAR_github_token:-}" # the github provider's auth
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
if [ -d ansible/roles ] && [ -n "$(ls -A ansible/roles)" ]; then
  ansible-lint
else
  echo "No roles yet; nothing to lint."
fi
```

## molecule-images

> Build the shared systemd-enabled Fedora 44 test image (idempotent).

```bash
set -euo pipefail
podman build -t localhost/fedora44-systemd:latest -f ansible/molecule/fedora44-systemd.Containerfile ansible/molecule
```

## molecule-test (role)

> Run the Molecule scenario for one role: mask molecule-test base

```bash
set -euo pipefail
# NOTE: no ANSIBLE_CONFIG here - ansible.cfg's relative roles_path would
# shadow molecule's own role-path injection.
export PATH="$PWD/.venv/bin:$PATH"
mask molecule-images
[ -d "ansible/roles/$role/molecule" ] || { echo "No molecule scenario for role '$role'"; exit 1; }
cd "ansible/roles/$role"
molecule test
```

## molecule-all

> Run every role's Molecule scenario (podman driver).

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
mask molecule-images
found=0
for d in ansible/roles/*/; do
  [ -d "$d/molecule" ] || continue
  found=1
  (cd "$d" && molecule test)
done
[ "$found" -eq 1 ] || echo "No roles with molecule scenarios yet."
```

## vault-edit (file)

> Create or edit an encrypted secrets file: mask vault-edit secrets/model.yml

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
ansible-vault edit "$file"
```

## vault-view (file)

> Decrypt to stdout: mask vault-view secrets/model.yml

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
ansible-vault view "$file"
```

## vault-rekey

> Change the vault password on all encrypted files under secrets/.

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
ansible-vault rekey secrets/*.yml
```

## vault-check

> Fail if any committed secrets/*.yml is not vault-encrypted.

```bash
set -euo pipefail
scripts/check_vault_encrypted.sh
```

## converge

> Full Ansible run against veggies.

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
[ -f ansible/playbooks/site.yml ] || { echo "ansible/playbooks/site.yml missing"; exit 1; }
ansible-playbook ansible/playbooks/site.yml --limit veggies
```

## bootstrap

> First-run playbook over the public IP. Ends by closing public SSH.

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$PATH"
[ -f ansible/playbooks/bootstrap.yml ] || { echo "ansible/playbooks/bootstrap.yml missing"; exit 1; }
ansible-playbook ansible/playbooks/bootstrap.yml --limit veggies
```

## veggies-install

> Install the `veggies` CLI into ~/.local/bin (wrapper around the
> repo venv). This shim - not mask - is the interface, because mask parses
> subcommand flags as its own.

```bash
set -euo pipefail
mkdir -p ~/.local/bin
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/cli/veggies.py "$@"\n' "$PWD" "$PWD" > ~/.local/bin/veggies
chmod +x ~/.local/bin/veggies
echo "installed: ~/.local/bin/veggies"
```

## demo-stack

> Clean VPS -> running dogfood stack, one command: bootstrap, converge,
> image prepare (streamed), up, ui URL. Idempotent: safe to re-run
> (ansible idempotent, images layer-cached, up is a refresh).
> Precondition (the only manual step): `Host veggies` in ~/.ssh/config
> points at the fresh box (Fedora 44 + your ssh key); `mask setup` and the
> vault password file exist locally.

```bash
set -euo pipefail
export PATH="$PWD/.venv/bin:$HOME/.local/bin:$PATH"
ssh -o BatchMode=yes -o ConnectTimeout=5 veggies true || {
  echo "veggies unreachable: order the VPS (Fedora 44, your ssh key) and"
  echo "point 'Host veggies' in ~/.ssh/config at its public IP first"
  exit 1
}
mask bootstrap
mask converge
veggies prepare --host veggies --repo .
veggies up --host veggies --clone --repo "$(git remote get-url origin)" --name veggie -y
veggies ui veggie
```
