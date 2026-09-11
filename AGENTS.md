# AGENTS.md - rules for coding agents working in this repo

This repository manages a production machine. The human approves plans and
reviews PRs; agents implement. These rules are not negotiable:

1. **Secrets**: never write, print, or commit plaintext secrets. Everything
   under `secrets/*.yml` is ansible-vault encrypted (pre-commit + CI enforce).
   Never paste decrypted content into a conversation.
2. **Mutation discipline**: never run `mask tofu-apply`, register runners, join
   tailnets, or call account-mutating APIs without explicit human approval in
   the conversation. `tofu plan`, Molecule, and lint runs are fine.
3. **Verify before finishing**: run `mask ci` and make it pass. Kicked
   sessions instead run the repo's declared in-pod verify gate,
   `mask ci`, declared machine-readably by the
   marker line below (consumed by scripts/stack_kick.py at kick time, ADR
   0045) - every hook in it runs in-pod (gitleaks/actionlint are
   image-baked, ADR 0046), and molecule stays excluded by the banned
   podman socket (ADR 0028); CI on GitHub-hosted runners covers it.
   <!-- veggies-verify-gate: mask ci -->
   Kicked sessions scale the gate to the diff: a change touching one
   area runs that area's file-scoped pre-commit hooks plus its targeted
   tests (e.g. `pre-commit run --files <files>` and the focused pytest),
   not the flattened `--all-files` run; the security hooks (gitleaks,
   vault-encrypted) always run full-scope, whatever the diff. Every
   Ansible role needs a Molecule scenario that converges and is
   idempotent. Every Terraform change needs `tofu fmt`, `validate`, and
   `tflint` clean.
4. **Commits**: conventional-commit messages, small and single-purpose.
5. **Decisions**: read `docs/adr/README.md` first. A new decision gets a new
   ADR; never edit a decided ADR. Keep the deviation ledger in
   `docs/adr/README.md` in sync.
6. **Placeholders**: `TODO(you)` = human supplies the value; never invent one.
   `TODO(verify)` = uncertain upstream detail; check docs before relying on it,
   and say so in the phase summary.
7. **Boring over clever**: prefer well-known modules/tools. No shell scripts
   over ~30 lines - use an Ansible module or a tested Python script instead.
8. **Task runner**: `maskfile.md` (mask 0.11.x). CI does not use mask - it
   invokes tools directly.
9. **veggies CLI** (`cli/veggies.py` + `cli/veggies_stack.py`, ADR
   0013/0014/0016/0023): repo-scoped agent stacks. The CLI is the ONLY owner
   of stack definition - ansible roles prepare hosts and nothing more (ADR
   0016). Components depend on capability contracts (`cli/capabilities.py`)
   and the PodContext, never on each other; implementations live in
   `cli/components/`, selected via the REGISTRY and per-repo `veggies.yml`
   (schema v1: model, components or capability keys, mcps, github). MCP servers are
   opt-in sidecars on pod loopback selected via `mcps:` (ADR 0018); a
   component wires them in via `mcp_entry()`/`egress_domains()` hooks.
    Supervision = `veggies supervise` (ADR 0028): the critic loop is ours,
    judge calls exec inside the litellm container so the master key never
    leaves the pod. Kicked sessions are instead supervised in-pod by the
    opt-in `supervision: supervisor` component (ADR 0036): an always-on
    sidecar judges each finish over pod loopback and posts async
    refinements; PASS/STOP stay log-only (any posted message re-runs the
    agent). (The canvas control plane was retired - ADR 0028; if a
    component ever needs the podman socket again, that ADR's history and
    0025 document the verified cost.)
    Events (ADR 0033): `.github/workflows/agent-trigger.yml` kicks the repo
    stack on `agent-task` labels / `/opencode` comments via
    `scripts/stack_kick.py`; a trusted `/elaborate` discussion comment
    instead kicks one session that fans out to the vendored persona roster
    (`agent-config/agents/`: domain-expert, infra-architect, marketer,
    seller, cto) and posts one attributed POV comment per persona (ADR
    0041); the kick prompt mandates the pipeline (plan refined by that
    same persona roster and posted on the issue first with each role's
    input, task subagents, adversarial-review subagent on the diff, the
    rule-3 declared verify gate - ADR 0036/0042/0045). `github: true`
    stacks take the serve password from vault key
    `veggies_stack_password`, never per-stack random. Stack
   names are global across hosts (cross-host reuse is refused). The
   permission envelope is allow/deny only - `ask` is banned everywhere in
   `agent-config/` (ADR 0031, pytest-enforced).    Observability (ADR 0034):
   kicked sessions are titled `#N: <issue>`, the workflow comments the
   session link back onto the issue, and `veggies ui` / `veggies sessions`
   are the watch path. `agent-task` is a one-shot label (ADR 0035): the
   workflow clears it on kick/skip, and the done-guard never re-kicks an
   issue that is closed or has a merged or ready `agent/issue-N` PR - an
   open draft no longer blocks re-kicks (ADR 0046).
   `veggies prepare` pre-stages images with build logs; `mask demo-stack`
   is the one-command clean-VPS-to-stack path (runbook §1).
   Session isolation (ADR 0037): every kicked session works in its own git
   worktree at `/workspace/.veggies/wt/issue-N` (the kick prompt mandates
   the bootstrap), never in the shared checkout; `veggies up` excludes
   `.veggies/` via the clone's `.git/info/exclude`.
   Pure renderers + state are pytest-covered in `tests/test_veggies.py`;
   `tests/golden/pod.yaml` is machine-generated (lint-excluded) - regenerate
   it whenever the renderer changes. Never add subPath mounts or tcpSocket
   probes (both verified broken here; see code comments). The vault is read
   only via `scripts/vault_get.py`; secrets travel over stdin only.
10. **Docs**: `docs/` are living docs - edit them freely as reality changes
    (architecture.md = the system today, runbook.md = how to operate it).
    README stays short: pitch, quickstart, pointers - anything operational
    belongs in the runbook. ADRs are append-only history; cite them in code
    comments only for non-obvious verified whys, never as decoration.
