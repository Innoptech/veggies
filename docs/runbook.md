# Runbook - veggies

Operational procedures for the agent host. Tested where a test is possible;
the rebuild checklist (section 1) is the acceptance test for the whole repo.

## Common tasks

`mask help` shows everything. The frequent ones:

| Task | Does |
|------|------|
| `mask ci` | everything CI runs, locally |
| `mask precommit` | all pre-commit hooks (gitleaks, yamllint, actionlint, tofu fmt, vault check) |
| `mask tofu-plan` / `mask tofu-apply` | plan is read-only; apply requires typing `apply` (live-account mutation) |
| `mask vault-edit secrets/model.yml` | create/edit encrypted secrets |
| `mask vault-check` | fail if any secrets file is plaintext |
| `mask molecule-test <role>` / `mask molecule-all` | role tests |
| `mask converge` / `mask bootstrap` | Ansible against veggies |

Prerequisites: mask 0.11.x, tofu 1.12.6, python 3.14, podman 5.8.x
(workstation and veggies are both Fedora 44); the pinned Python tooling is in
`requirements-dev.txt` (`mask setup`).

## 1. Rebuild-from-zero checklist (the acceptance test)

Execute this on a second VPS (or a reinstalled veggies) to prove the repo
rebuilds the machine. Every box must be ticked in order.

Fast path (the demo): after the three provisioning boxes below,
`mask demo-stack` runs bootstrap -> converge -> prepare -> up -> ui as one
idempotent command, streaming logs throughout. Indicative timings on a
clean box (2026-09): converge ~10 min, prepare ~5-8 min (image layers
through the egress proxy), up ~2 min once images are warm. For a live
log tail in a second terminal during the ansible parts:
`ssh veggies "sudo journalctl -f"`. The checklist below is the same flow
broken out for verification.

Provision manually (ADR 0008):

- [ ] Order/reinstall the VPS: Fedora 44 image, your ssh key attached.
- [ ] Point `Host veggies` in `~/.ssh/config` at the new public IP.
- [ ] `ssh veggies true` works as `fedora`.

Prepare the repo locally:

- [ ] `mask setup` ran; `~/.config/infra/vault-password` exists (600).
- [ ] `cp ansible/inventory/group_vars/all.yml.example ansible/inventory/group_vars/all.yml` and fill every `TODO(you)`.
- [ ] `cp terraform/terraform.tfvars.example terraform/terraform.tfvars` and fill it.
- [ ] Vault files filled: `mask vault-edit secrets/model.yml`, `secrets/github.yml`, `secrets/infra.yml`.
- [ ] VPS disk size noted in `all.yml` (informs backup cleanup thresholds).
- [ ] `mask ci` is green.

Bootstrap:

- [ ] `mask bootstrap` - it MUST prove tailnet SSH before closing public SSH
      (that play cannot be skipped; if it fails, public SSH stays open and you
      fix tailscale first).
- [ ] Update `Host veggies` to the tailnet name (MagicDNS).
- [ ] `ssh veggies true` over the tailnet.

Converge + verify:

- [ ] `mask converge` finishes green and idempotent on rerun.
- [ ] `veggies ls` from the operator machine shows stacks (remote hosts
      included); `ssh veggies systemctl --user list-units 'gh-runner*' -M gh-runner@` shows runners.
- [ ] A test PR in a governed repo: `/opencode` comment triggers a job on
      veggies; the PR cannot merge without your review (ADR 0007).
- [ ] Deny test: in a runner, `curl https://example.com` fails and
      `journalctl -k -g infra-egress-deny` shows the drop (ADR 0006).
- [ ] `systemctl list-timers 'backup*'` shows the timers; a manual
      `systemctl start backup` succeeds (needs real restic creds).
- [ ] Restore drill on a scratch dir: section 6.

Tear down the test VPS when done; record deltas as PRs.

## 2. Bootstrap a fresh VPS (details)

VPS variant (ADR 0008): no `tofu apply` - there is no security group on the
VPS product line. The flow is: OVH image + your key -> `mask bootstrap`
(base, crowdsec, tailscale with `base_public_ssh=true`) -> unskippable
tailnet verification -> firewalld removes public ssh. If you ever need public
SSH again: OVH console -> `firewall-cmd --permanent --zone=public
--add-service=ssh && firewall-cmd --reload`, and close it again after
(`mask converge` re-asserts the closed state because `base_public_ssh`
defaults to false).

If tailscale is deferred (`tailscale_enabled: false`, ADR 0024): the
bootstrap gate and close-out are skipped by design - public SSH stays open
(key-only, CrowdSec-guarded) and `~/.ssh/config` keeps the public IP. Remote
stack attach then works over SSH forwarding: `ssh -L 4096:127.0.0.1:4096
veggies` + attach to `http://127.0.0.1:4096`. Do NOT set
`base_public_ssh: false` until a tailnet is live - converge would lock you
out.

If tailscale breaks on the host: OVH console (web shell) ->
`tailscale status`, `journalctl -u tailscaled`, re-run with a fresh auth key:
`mask vault-edit secrets/infra.yml` + `mask converge`.

## 3. Secrets: create, edit, rotate

Three domain files, all committed **encrypted** (pre-commit + CI enforce):

| File | Holds | Consumed by |
|------|-------|-------------|
| `secrets/model.yml` | Fireworks key, litellm master key | injected per stack by `veggies` (podman secrets) |
| `secrets/github.yml` | GitHub App creds or bot PAT | tofu github module, runner registration, `GH_TOKEN` in github-enabled stacks (ADR 0030) |
| `secrets/infra.yml` | tailscale auth key, restic password + S3 creds | tailscale/backup roles |

Structure templates: `secrets/*.yml.example`. Rules: never decrypt to disk;
tofu consumes secrets via env-only export (`scripts/tfvars_from_vault.py`).

```bash
mask vault-edit secrets/model.yml     # create/edit (ansible-vault)
mask vault-view secrets/infra.yml     # decrypt to stdout
mask vault-rekey                      # change the vault password on all files
mask vault-check                      # CI-style check: everything encrypted
```

Rotation matrix (do these in one PR each):

| Secret | Rotate by |
|--------|-----------|
| fireworks_api_key | new key in Fireworks console -> vault-edit model.yml -> `veggies down <name>` + `veggies up` per stack -> revoke old |
| per-stack litellm keys | random per stack; rotate with `veggies down <name> --purge` + `veggies up` |
| github_token / App key | new credential -> vault-edit github.yml -> `mask tofu-apply` + converge -> `veggies up` per github-enabled stack |
| tailscale_auth_key | new pre-auth key (tagged) -> vault-edit infra.yml -> converge (no-op while Running; only used at join) |
| restic_password | vault-edit infra.yml -> converge; old snapshots need the OLD password - keep it until you prune or re-key the repo |
| converge_ssh_private_key | new keypair -> pubkey into admin_ssh_public_keys (group_vars) -> converge -> vault-edit infra.yml |

Never paste decrypted vault content anywhere - including agent conversations.

## 4. Resize the VPS

OVH panel: VPS -> resize (keeps IP, reboots). Afterwards: adjust
`github_runner_count` / `github_runner_memory_max` / `github_runner_cpu_quota`
in group_vars to the new size, `mask converge`. On the future Public Cloud
path (ADR 0002): change `ovh_flavor_name`, `tofu apply`, confirm the resize.

## 5. Recover a wedged runner

```bash
ssh veggies
systemctl --machine=gh-runner@ --user list-units 'gh-runner@*'
journalctl --machine=gh-runner -M? # see note
systemctl --machine=gh-runner@ --user restart gh-runner@<repo>-1.service
```

- Stuck registration: check `~gh-runner/.config/gh-runner/*.env` freshness and
  `journalctl _UID=<gh-runner uid> -g fetch_runner_token`.
- Token fetch failing: the proxy must be up (`systemctl --machine=egress-proxy@
  --user status squid.service`) - the fetcher egresses through it.
- Full reset of one runner: stop the unit, `rm -rf /srv/gh-runner/<inst>`,
  start the unit (it re-registers and re-creates the work dir).

## 6. Restore from backup

On a fresh machine (after base converge so users exist):

```bash
sudo /usr/local/sbin/restore.sh /etc/restic/restic.env   # or any env file copy
# lists snapshots, requires typing "restore", restores latest into /
```

Verify permissions under /home/* afterwards, then `mask converge` to
re-assert the current state. Exercise this in the section-1 checklist.

Operator-machine note: the tofu state is NOT on veggies (ADR 0008). Back it up
from the workstation with the same restic repo (separate prefix suggested):

```bash
mask vault-view secrets/infra.yml   # source the restic vars from it
# then: restic -r <repo>:terraform backup terraform/*.tfstate*
```

## 7. Add a model provider key

One PR, three edits:

1. `mask vault-edit secrets/model.yml` - add the provider key.
2. `agent-config/litellm/config.yaml` - add the `model_list` entry (and any
   fallback rule).
3. `ansible/inventory/group_vars/all.yml` - add the endpoint to
   `egress_model_endpoints`, then `mask converge` (updates the VPS squid
   allowlist - substrate, ADR 0016).

Stacks pick the new model up at next `veggies up` (agent-config is mounted
at render time); select it via `litellm/<alias>` in agent frontmatter or
`/models`. Existing stacks: `veggies down <name>` + `veggies up` - the
recreate re-injects the current vault keys.

## 8. Add an agent or a skill

- Agent: new file in `agent-config/agents/<name>.md` (frontmatter:
  description, mode, model, permission). PR, merge; stacks pick it up at
  next `veggies up` (recreate running stacks to apply).
- Removing/renaming an agent or skill: the wrapper copies but never prunes,
  so the stale file survives on the opencode-home volume (verified
  2026-09-04). After `veggies up`, also
  `podman exec veggies-<name>-opencode rm /root/.config/opencode/agents/<old>.md`
  (same under `skills/`) or `veggies down <name> --purge` for a clean slate.
- Skill: `agent-config/skills/<name>/SKILL.md` with `name` + `description`
  frontmatter (see opencode skills docs). Same flow.
- Superpowers bump: change the pinned tag in `agent-config/opencode.json`
  (`#vX.Y.Z`; check the tag's resolved commit with
  `git ls-remote --tags https://github.com/obra/superpowers.git`), PR; same
  stack-recreate rollout. opencode/bun caches git plugins under the
  opencode-home volume, so a bump may not take effect on recreate alone
  (upstream-documented): clear the cache with
  `podman exec veggies-<name>-opencode rm -rf /root/.cache/opencode /root/.config/opencode/node_modules`
  then restart the stack. Telemetry is disabled pod-wide via
  `SUPERPOWERS_DISABLE_TELEMETRY=1` (set by the opencode component).

## 9. veggies stacks (ADR 0013/0014)

Daily: `veggies up` in a repo; `veggies attach <name>`; `veggies ls`;
`veggies status <name>` (health + model/agents/sessions via the API);
`veggies logs <name> [-f] [container]`; `veggies down <name> [--purge]`;
`veggies sync <name>` (clone-mode stacks: pull + re-up, see below).
Remote: `veggies up --host veggies --clone --repo <git-url>` then attach over the
tailnet (or an `ssh -L` forward while tailscale is deferred - ADR 0024). Per-repo customization: `veggies.yml` (schema v1: `model`,
`components`, capability keys, `mcps`, `github`; ADR 0016/0023).

### Syncing a stack with the repo

Three things people mean by "the stack is up to date", each synced
differently:

| What | Source of truth | How it goes live |
|------|-----------------|------------------|
| Stack definition (`agent-config/`, component code, images) | the operator's LOCAL infra checkout (the CLI is a shim into it) | every `veggies up` re-ships rendered config over ssh and rebuilds changed images - `git pull` locally, then up |
| Event path (`agent-trigger.yml`, `scripts/stack_kick.py`) | `origin/main` | automatic on merge: the self-hosted runner does `actions/checkout` every run |
| The workspace clone (what `/workspace` is; where `veggies.yml` is read from at up time) | `origin/main` | **kicked sessions self-sync** - the kick prompt fetches and branches each worktree off `origin/main` (ADR 0037). The shared checkout itself is only refreshed by `veggies sync` |

So the recipe after merging a feature to main: `git pull` in your local
infra checkout (so the CLI and the agent-config it ships are current), then:

```bash
veggies sync veggie
```

One command: pulls the VPS clone (`--ff-only`, as the stacks user through
the substrate proxy; private repos get the vault token command-scoped,
exactly like the initial clone), then re-ups - fresh `veggies.yml`
(model/components/mcps/supervision), re-shipped agent-config, rebuilt
images, recreated pod. Port, name and creation date survive (state record);
the `github: true` opt-in survives too - dropping it is a deliberate
`down` + `up`.

Two guards:

- **Busy sessions**: sync refuses while a session is busy (the pod recreate
  would kill it) - wait, or `--force`. A stack whose API is unreachable is
  considered quiet and syncs normally (that is also how a down stack comes
  back).
- **Non-fast-forward pull**: the pull FAILS rather than resetting - kicked
  sessions may never touch the shared `/workspace` checkout (ADR 0037), so a
  dirty clone means something misbehaved; investigate (`sudo -u stacks git
  -C /home/stacks/.local/state/veggies/clones/<name> status`), or go nuclear
  with `down --purge` + `up` (deletes the clone, volumes and session
  history).

Mount-mode stacks need no sync: the workspace IS your live checkout, so a
plain `veggies up` is all there is (`sync` refuses them with that hint).

MCP servers (ADR 0018): opt-in sidecars selected via `mcps: [<name>]` in
veggies.yml (registry in `cli/veggies_stack.py:MCP_REGISTRY`). They serve
streamable HTTP on pod loopback only - no published ports - and opencode
learns them via the rendered `mcp:` block (check `GET /config`). Secrets,
when an MCP needs them, are pod secrets rendered into headers via `{env:}`;
egress, when needed, is the component's `egress_domains()` merged into the
squid allowlist. New MCP = one file in `cli/components/` + one registry
line. Verify a live one: a session prompt "use the <name> MCP ..." should
produce a `<name>_<tool>` tool call in the session messages.

MCPs are opencode-only: the canvas probe (2026-09-09, agent-canvas
1.16.0) showed its profile API rejects `mcp_config`, and canvas is now
retired entirely (ADR 0028) - opencode is the only harness.

Browser attach (verified 2026-09-08): the published port serves opencode's
official web UI - sessions list, live multi-session view, permissions; it
shares state with any attached TUI. Local: open `http://127.0.0.1:<port>`.
Remote: `ssh -L <port>:127.0.0.1:<port> veggies`, then open the same URL.
The basic-auth password is printed at `up` and stored in
`~/.local/state/veggies/state.json`.

### Permission posture (ADR 0029)

Unattended sessions must never park on a prompt: the envelope in
`agent-config/opencode.json` allows routine in-workspace work and DENIES
(rather than asks) the rest - denial is instant feedback the agent routes
around; `ask` in a headless session parks forever (verified 2026-09-09,
25 minutes on one prompt).

- Out-of-workspace paths: denied except `/tmp/**`. The `rm -rf` shapes
  that could kill the workspace or home volume: denied. `.env` /
  `secrets/*.yml`: unreadable (vault ciphertext never enters transcripts).
- `doom_loop` (3 identical tool calls) denies - the agent must change
  approach instead of burning tokens.
- ADR 0031: NO permission value anywhere in `agent-config/` may be `ask`
  (pytest-enforced) - that includes per-agent frontmatter, which overrides
  the global block. `question` is denied too: a blocking question fails
  fast instead of parking; agents say what they need in their final
  message.
- Widen a rule: one pattern line in `agent-config/opencode.json`, PR,
  `veggies up` (recreate).
- If a session still stalls, `veggies supervise` prints pending
  permissions/questions once each; answer them in the web UI.

### Supervision: the critic loop (ADR 0028/0036)

Canvas is retired (ADR 0028: upstream archived, two-worlds problem, and
the critic only covered canvas conversations). The loop is ours now, in
two shapes:

**Operator-driven**: `veggies supervise <stack> --session <id>
[--threshold 0.6] [--max 2]` watches one opencode session; every time the
agent finishes (session goes idle with an unjudged assistant message),
the transcript is judged by a DIFFERENT model (default deepseek-v4
judging kimi-k3) via the in-pod router - the judge call runs inside the
litellm container, so the master key never leaves the pod. Below
threshold, a `[critic] score ... issues: ...` message is posted into the
session (visible in the web UI) and the agent iterates; exit 0 on PASS, 1
on max-iterations/timeout. Get session ids from the web UI or
`GET /session` on the stack port.

**Always-on for kicked sessions** (ADR 0036): stacks that opt in via
`supervision: supervisor` in veggies.yml run a `supervisor` sidecar in
the pod that supervises every issue-kicked session (titled `#N: ...`)
created after it started - same rubric, same threshold/max semantics, but
self-driving: no human invokes anything. Refinements arrive as `[critic]
...` messages in the session; PASS and STOP are LOG-ONLY (any posted
message re-runs the agent, so a visible marker would loop forever). Watch
it: `veggies logs <name> supervisor -f` (one `critic: <title> score ...
-> pass|refine` line per judgment) and `veggies status <name>` (the
`critic` probe shows the heartbeat age). The loop's defaults live in the
daemon (judge deepseek-v4, threshold 0.6, max 2 refinements, 15s poll) -
keep the judge model different from the stack's author model; changing a
default is a PR, like everything else here. Sessions that predate the
daemon start are skipped with one log line each (the gate being off must
be distinguishable from the gate passing), and judgment state is
in-memory: a pod recreate simply never judges sessions created before
the restart - run `veggies supervise` by hand if one matters.

### Open PRs from a stack (github: true)

Opt-in per repo (ADR 0030): `github: true` in veggies.yml, or `--github`
at `up`. A github-enabled stack's opencode container gets the vault's
`github_token` (secrets/github.yml) as `GH_TOKEN` via a per-stack podman
secret, a git credential helper that expands `$GH_TOKEN` at use time (the
token is never written to `.git/config`), the `gh` CLI, and bot commit
identity (`veggies-agent`); `git@github.com:` remotes are normalized to
HTTPS. Sessions in the stack can push branches and open PRs as the bot.

- PAT scopes: Contents read/write + Pull requests read/write on the target
  repos - the same `github_token` in secrets/github.yml that clone uses.
- Applies at recreate: `veggies up`. The up output shows
  `github:  GH_TOKEN + gh push/PR access enabled (ADR 0030)` when enabled.
- On github-enabled stacks the serve password is the vault key
  `veggies_stack_password` (not per-stack random) so the repo's
  `VEGGIES_STACK_PASSWORD` Actions secret stays valid across re-ups
  (ADR 0033).

### The in-container toolchain (ADR 0032)

The opencode image carries python3/pip, `mask`, `ansible`/`ansible-vault`,
`tofu`, `tflint`, `gitleaks`, `actionlint`, `pre-commit`, `pytest`,
`yamllint` - the agent runs the repo's own checks inside the stack, and
since ADR 0046 every hook in `mask ci` runs in-pod with zero skips.
The declared in-pod verify gate is `mask ci` (AGENTS.md rule 3's
`veggies-verify-gate` marker, ADR 0045; see "Declaring a repo's verify
gate" below). Molecule was never part of `mask ci` and stays
host/CI-only (no podman socket in-pod, ADR 0028). All binaries are
version+sha256 pinned in
`deploy/images/opencode.Containerfile`; `tests/test_tool_pins.py` keeps
the image, `.github/workflows/infra-ci.yml`, and `mask setup` in
agreement. A stale `SKIP=actionlint-docker` is now a harmless no-op -
the hook id is `actionlint` today and pre-commit ignores unknown SKIP
ids. The python hooks (yamllint, ansible-lint,
pre-commit-hooks) still pip-install into pre-commit's cache on a cold
cache - expected; pypi is allowlisted, and ADR 0046 names their
conversion as the follow-up. In-container checks are a CLONE-stack story (the VPS
stack's clone has no `.venv`); in a mount-mode stack the mounted `.venv`
is the host's and shadows the image's tools on the maskfile's PATH -
run host-side there instead. On merging hook/binary changes, rebuild the
image (`veggies prepare`/`up`, or `mask demo-stack`'s prepare step)
before the next `agent-task` label: kicks branch off `origin/main`, so
the hooks take effect at merge while the binaries arrive with the
rebuild. Hosts re-run `mask setup` after pulling - it installs the same
pinned binaries into `~/.local/bin`. Smoke-test a rebuilt image:
`podman run --rm --entrypoint sh localhost/veggies-opencode:<ver> -c 'python3 --version && mask --version && ansible-vault --version && gitleaks version && actionlint --version'`.

### Issue-triggered agent kicks (ADR 0033)

`.github/workflows/agent-trigger.yml` (self-hosted runners, this repo)
kicks the repo's long-lived stack when:

- an issue gets the `agent-task` label, or
- an OWNER/MEMBER/COLLABORATOR comment **starts with** `/opencode`
  (command-style; prose mentions never fire) - on an issue (the agent
  works it, ADR 0033) or on a **discussion** (the agent reads the whole
  thread and distills it into issues with Plan / Happy path / Criteria of
  success sections, ADR 0038) - or **starts with** `/elaborate` on a
  discussion (five persona POV comments, ADR 0041). INTERIM (ADR 0043):
  while the agent shares the operator's `olgam4` identity, olgam4 MAY
  trigger - the self-kick loop is bounded by command anchoring, the
  in-flight guard (issues AND discussions now) and the issue done-guard;
  kick prompts forbid the agent from starting any comment with a command.
  The hard bot exclusion returns with the GitHub App identity.

`agent-task` is one-shot (ADR 0035): the label is cleared after a
successful kick or a skip - re-add it to retrigger. Done-issues are never
re-kicked: the kick skips (with a comment saying why) when the issue is
closed or an `agent/issue-N` PR is merged or marked ready (ADR 0046), or
when a session titled `#N: ...` is currently busy on the stack (the
in-flight guard, ADR 0040). An open DRAFT never blocks a re-kick - it is
the session's workbench, and the in-flight guard still covers a busy
session. A stale or conflicted draft means the session died: comment
`/opencode` on the issue (or re-add the label) - the next session
reconciles the branch and continues the same draft. A ready PR that fell
behind main is done-guarded (ready = handled) and PR comments never kick -
convert it back with `gh pr ready --undo`, then re-kick the issue; the
session rebases and re-runs the ready-gate.
A failed kick keeps the label. Discussions have no done-guard: every
`/opencode` comment is a deliberate kick, and re-kicking an evolving
discussion is normal (the agent dedupes against issues it already created
from that discussion; ADR 0038).

`/elaborate` on a discussion (ADR 0041) kicks one session that fans out
to the vendored persona roster and posts one `**<Role> POV**` comment
per persona back on the discussion - no branch, no PR. No done-guard:
re-comment `/elaborate` to re-run. Personas register at stack boot (ADR
0019): run `veggies up veggie` after this merges before `/elaborate` works.

The job runs `scripts/stack_kick.py`: create session, fire the issue as an
async prompt, exit in milliseconds. The agent then works the issue in its
own worktree (ADR 0037, see below) through the mandated pipeline (ADR
0036): it posts a plan as an issue comment BEFORE writing code (veto the
direction by commenting, while it works). The plan is first refined by
the persona roster (ADR 0042 - the same roster `/elaborate` uses): one
task subagent per persona reviews the draft, and the posted comment
carries a `## Role review` section with each role's input or explicit
no-objection. It then executes through task subagents, runs the
`adversarial-review` subagent on the diff, and pushes to a draft PR
opened at the first commit (ADR 0046); ready is the last act - only when
checks are green and the PR merges cleanly against current main (rebasing
first). On stacks with `supervision: supervisor` the in-pod critic
additionally
judges every finish and injects refinements (see the supervision section).
Plumbing: Actions variable
`VEGGIES_STACK_HOST`/`VEGGIES_STACK_PORT` + secret `VEGGIES_STACK_PASSWORD`
(all tofu-managed from the vault); the runner reaches the stack at
`http://host.containers.internal:<port>` (NO_PROXY bypass, no inbound
ports on the VPS; allowed by `egress_extra_local_dports` in the egress
role).

TODO(you): the bot PAT (account `olgam4`, fine-grained) is missing
`Discussions: write` on Innoptech/veggies: the distilling agent's closing
comment on the source discussion and the `/elaborate` persona POV
comments (ADR 0041) both degrade to a named-permission final
message until granted (the created issues still link the discussion, so
the back-reference appears regardless). The four permissions verified
missing on 2026-09-10 (`Contents`/`Pull requests`/`Secrets`/`Variables`
write) were granted on 2026-09-11.

Watch a kicked run (the demo path):

```bash
veggies ui veggie                  # background tunnel + prints URL/password
veggies sessions veggie            # live first; idle capped at 10 (--all shows everything)
veggies sessions veggie --issue 15 # the sessions working one issue (never capped)
veggies supervise veggie --session <id>      # critic loop
veggies logs veggie -f                       # raw pod logs
veggies ui veggie --stop           # close the tunnel
```

Every issue kick comments on the issue (ADR 0034): session id, deep link
(`http://127.0.0.1:<port+1000>/L3dvcmtzcGFjZQ/session/<id>` once
tunneled), and the password one-liner. Discussion kicks get a minimal ack
only - the results feedback is the agent's closing comment listing the
created issues, which themselves link the discussion in `## Context`
(ADR 0039; discussions need `addDiscussionComment`, issues `addComment`).
A failed kick always comments, on either subject. Kicked sessions are
titled `#N: <issue title>` (`D#N: <discussion title>` for discussions).

`busy` = working now; `idle` = between prompts or done - the API cannot
tell, and neither can we, so an attached-but-thinking interactive session
shows idle (`--all` never hides anything).

Web UI map (1.18.27, verified 2026-09-11): `/` is Home - the all-sessions
view (Today/Yesterday/Older + search), but its project list is
browser-local state: this build has no server-side project-registration
API (`GET /api/project` is unimplemented, the SPA catch-all answers its
HTML). Add the project once per browser profile. The Open-project picker
defaults to the container's `$HOME` (you'll see dotdirs, not /workspace) -
its Search folders accepts absolute paths, so type `/workspace` (backend
verified: `/api/fs/list?path=/workspace` lists the worktree). `/<dir>`
(dir = base64url of the workspace path, no padding) opens a new-session
composer, NOT a session list; the session view `/<dir>/session/<id>` is
the watch target, with project-wide session search in its header and a
Recent sessions sidebar. `veggies ui` prints the Home URL and deep links -
live sessions first, then newest finished, 5 shown, overflow pointed at
`veggies sessions` - links need no registration at all.

Tunnel gotcha (verified 2026-09-10): if a LOCAL stack already publishes the
same port, `ssh -L <port>:...` cannot bind it - and anything pointed at
`127.0.0.1:<port>` silently talks to the LOCAL stack instead. `veggies ui`
picks a really-free local port for you (stack port + 1000 by default).

Manual fallback (workflow down, demo must go on): run `scripts/stack_kick.py`
by hand - the header comment has the exact env. From the operator machine,
tunnel first or run it on the VPS (`ssh veggies`, then STACK_URL is
`http://127.0.0.1:<port>`).

### Declaring a repo's verify gate (ADR 0045)

A repo declares its in-pod verify gate as ONE machine-readable marker in
its agent-instruction file: `<!-- veggies-verify-gate: CMD -->`, a single
command string alone on its own line (an example quoted inside prose is
not a declaration), kept honest by the human prose around it (this repo:
AGENTS.md rule 3, `mask ci`). Search order:
`AGENTS.md`, then `CLAUDE.md`; the first marker wins. A pytest parses the
real AGENTS.md and pins the declared command plus the prose<->marker
lockstep, so an edit that drops either half fails loudly.

`scripts/stack_kick.py` resolves the marker at kick time, anchored to the
vendored script's own repo root (the runner's default-branch checkout),
never cwd. Consequence: a gate change ships as an ordinary PR on the
default branch - no CLI release.

No marker -> the kick prompt degrades to an advisory pointer: the
agent-instruction file's prose is the contract and the agent claims only
what it actually ran. That fallback is the design, not a failure mode: a
repo with a decent agent-instruction file already gets a repo-native
agent with zero veggies markup - the marker just makes the gate
deterministic. An EMPTY marker (`<!-- veggies-verify-gate: -->`) is an
explicit opt-out to that same fallback, not a parse error - and it
suppresses a marker in the later file (an AGENTS.md opt-out beats
CLAUDE.md).

The resolved gate is echoed per kick so a typo'd marker is a visible
event, not a silent degrade: the `VERIFY_GATE` line in the workflow log,
and one `Verify gate:` line in the session-link comment the workflow
posts on the issue.

Scope the gate to the diff in the declaration's prose, not in the marker
(this repo's convention): a narrow diff runs file-scoped pre-commit hooks
plus targeted tests instead of the flattened `--all-files` run; the
security hooks (gitleaks, vault-encrypted) always run full-scope,
whatever the diff. A repo that outgrows one command points the marker
at a mask/make target.

The gate executes inside the harness image (ADR 0032): a foreign gate
needs an image that can run it - the repo owner owns that toolchain
story.

Scope: the marker is ONLY the verify gate. Broader respect for a repo's
own agent files (instructions, skills, rosters) is issue #54's territory.

### Session worktrees (ADR 0037)

Sessions on a stack share one clone, so every kicked session works in its
own git worktree: `/workspace/.veggies/wt/issue-N` on branch
`agent/issue-N`, created `--lock`ed by the kick prompt's bootstrap. The
shared checkout at `/workspace` itself is read-only for kicked sessions.
`/.veggies/` is excluded two ways: committed in this repo's `.gitignore`,
and written per-clone to `.git/info/exclude` by `veggies up` (so worktrees
never show up in anyone's `git status`; `git worktree list` shows them).

Inspect on the stack's host - locally `~/.local/state/veggies/clones/<name>`,
on the VPS as the stacks user:

```bash
sudo -u stacks git -C /home/stacks/.local/state/veggies/clones/<name> worktree list
```

Clean up a stale worktree (a crashed run leaves one behind):

```bash
# 1. check no live session owns it - the default view never hides LIVE
#    sessions, so this check stands regardless; --all also shows the
#    FINISHED titled session, the ownership record to have in view before
#    rescuing unpushed work
veggies sessions <name> --all
# 2. rescue unpushed work FIRST if it matters - removing the worktree does
#    NOT delete the branch, and the next kick's `-B` resets the surviving
#    branch to origin/main, discarding unpushed commits
sudo -u stacks git -C /home/stacks/.local/state/veggies/clones/<name> \
    branch backup-issue-N agent/issue-N    # optional rescue
# 3. locked trees need the deliberate double -f
sudo -u stacks git -C /home/stacks/.local/state/veggies/clones/<name> \
    worktree remove -f -f .veggies/wt/issue-N
```

Never remove a live session's tree - that is exactly the clobbering this
design exists to prevent. While the stale tree exists, a re-kick takes the
`-2` suffix and says so in its PR body. `down --purge` on a clone-mode
stack deletes the whole clone, worktrees included; in mount mode the
worktrees live in YOUR repo and cleanup is yours.

Re-up caveat: the Actions variable `VEGGIES_STACK_PORT` must match the
live stack (`veggies ls`); the password can never drift (both sides read
the same vault key).

### Teammate onboarding (stack user, not operator)

```bash
git clone <this-repo> && cd veggie && mask setup
# get the vault password from the operator (out-of-band), then:
$EDITOR ~/.config/infra/vault-password && chmod 600 ~/.config/infra/vault-password
mask veggies-install
cd ~/code/your-repo && veggies up     # as a normal user - root is untested
```

One vault password unlocks every `secrets/*.yml`; there is no per-teammate
credential. Model usage by stacks bills to the operator's provider key.

Reference:

- Only the opencode port is published (127.0.0.1 locally, tailnet-only on the
  VPS via firewalld); litellm and squid are pod-internal.
- Mount mode bind-mounts your checkout rw and relabels it
  `container_file_t` (harmless for your user; that's the `:z` equivalent).
  `--clone` keeps the clone inside the veggies state dir instead.
- Boot persistence locally needs linger once: `sudo loginctl enable-linger
  $USER` (the CLI warns you).
- Env overrides for scripts: `VEGGIES_REPO VEGGIES_NAME VEGGIES_HOST
  VEGGIES_CLONE=1 VEGGIES_YES=1 VEGGIES_NO_ATTACH=1 VEGGIES_NO_INSTALL=1`.

Troubleshooting:

- Stack flapping right after up: `veggies logs <name> <container>`. Known-good
  invariants: no subPath mounts (SELinux), exec-probes only (minimal images
  have no `nc`), `pid_filename none` and no `cache_dir null` in squid.conf,
  stack-config mounted readOnly at /stack-config (opencode needs a WRITABLE
  ~/.config/opencode - the wrapper copies opencode.json there).
- API endpoints 500/hang right after up: cold bootstrap (plugin cache) takes
  ~20s; concurrent requests during bootstrap pile up - wait and retry. If it
  never settles, suspect the superpowers plugin pin (must be a commit sha;
  tags are mutable and v5.0.3 was deleted upstream).
- SELinux denials on a stack: re-run `veggies up` (it re-asserts
  `chcon -R -t container_file_t -l s0` on every bind source). If you
  hand-mount anything new, label it the same way.
- A container stays `exited` under the quadlet: that is expected (systemd
  owns restart there); the `veggies-watchdog.timer` revives it within ~30s.
  Check: `systemctl --user status veggies-watchdog.timer`.
- Remote ops fail with sudo/ssh errors: tailnet up? `ssh veggies true`? The
  stacks user exists only after `mask converge` (base role).
- Remote ops as the stacks user fail with `cannot chdir to /home/fedora`:
  rootless podman chdirs to $cwd - prefix remote commands with `cd /` (the
  CLI's host_run does this).
- git/network ops in a remote stack hang ~35s per connection: the
  chained-squid DNS stall, fixed 2026-09-10 - stacks created before it
  need one `veggies up` recreate; verify with `veggies logs <name> squid`
  (CONNECT lines should complete in <1s).
- `tofu init` fails in-pod with "failed to request discovery document": the
  registry fetch lost to tofu's 10s default client timeout on cold chained
  egress (issue #50). `mask tofu-validate` and the opencode image now export
  TF_REGISTRY_CLIENT_TIMEOUT=120; existing stacks pick up the image ENV on
  the next `veggies up` (the maskfile export covers `mask ci` immediately).
  If git ops also hang ~35s per connection the stack predates the DNS fix
  above - recreate it (`veggies up`).
- Pre-fix remote clones may carry the clone-time token in `.git/config` -
  check with `git config --get http.extraheader` and unset it; private-repo
  pulls in-pod now require `github: true`.
- Rotate a stack's keys: `veggies down <name> --purge && veggies up ...`
  (fresh random master key + fresh copy of the vault's Fireworks key).
- `vault lookup failed ... password file missing or empty`: create
  `~/.config/infra/vault-password` (one line, chmod 600) - the password
  comes from the operator, out-of-band. `... decryption failed` = wrong
  password in that file.
- `cannot derive a stack name from '<path>'`: the resolved repo dir (or URL
  basename) has no valid DNS-1123 characters; pass `--name`.
- `!! running as root`: warning only - stacks assume a rootless user
  (systemd --user, linger); use a normal account.
