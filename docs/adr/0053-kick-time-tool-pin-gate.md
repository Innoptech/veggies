---
status: accepted
date: 2026-09-12
---

# 0053. Kick-time tool-pin gate: the image attests its toolchain pins, stale images are refused

## Context and problem statement

[0047](0047-networkless-lint-hooks.md) closed with a rollout-ordering
consequence: merging lands the hooks and the declared gate instantly
(kicks branch off `origin/main`), but the baked binaries exist in-pod
only after the operator rebuilds the image - "do that before the next
`agent-task` label". That ordering was manual and unenforced. In the
window between a pin-affecting merge and the rebuild, every kicked
session lands on a stale image and discovers the skew mid-`mask ci`,
after the session's tokens are already spent: issue #48's failing
in-pod gate, discussion #81's curl bootstrap detour, and issue #87 all
trace to this window. The python-hooks conversion (0047's named
follow-up) changes the same pin surface and would reopen the window the
day it merges.

## Decision drivers

- The agent in-pod cannot fix a stale image itself: rule 2 keeps the
  podman socket out of the pod
  ([0028](0028-retire-canvas-own-the-critic-loop.md)); only the
  operator can rebuild.
- The kick runner reaches the live stack ONLY over the opencode HTTP
  API: the runner's podman socket belongs to the gh-runner user
  ([0005](0005-ephemeral-containerised-runners.md)), and the workflow
  has no host ssh path.
- A kick-time check must not spend a session - the `agent-task` label
  is one-shot ([0035](0035-one-shot-labels-and-done-guard.md)), so the
  check has to run before the session exists and cost no tokens.

Verified constraints (checked 2026-09-11 against the pinned opencode
1.18.27 image): the serve API's only exec channel, `POST /pty`, is
broken on the musl image (node-pty's bundled glibc .so fails dlopen -
HTTP 500); `GET /config` does not round-trip project-tier custom keys,
so config cannot carry the attestation either; `GET /file/content`
reads files under the instance directory - a missing file answers 200
with empty content, basic-auth is enforced, and path escapes are
refused. So the stack cannot be ASKED what it runs - it must ATTEST.

## Decision

Options considered:

A. Keep the documented manual ordering - rejected: an unenforced
   ordering is a when, not an if; it already failed three times (above).
B. Probe the live stack's toolchain at kick time - rejected: the only
   exec channel is verified broken, and no other API answers "what is
   installed" (constraints above).
C. Stamp the expected pins from the CLI at `veggies up` time -
   rejected: that certifies the build the CLI last ran, not the run - a
   rolled-back container or a downgraded CLI re-up would carry a stamp
   the image does not match.
D. **Chosen:** the image attests its own pins; the kick gate compares
   attested vs expected and refuses proven-stale images.

Three parts, as landed:

1. **The image attests.** `deploy/images/opencode.Containerfile`
   printf's its six `<TOOL>_VERSION` ARGs into
   `/etc/veggies-tool-pins` at build time: GITLEAK, ACTIONLINT, TOFU,
   TFLINT (the four 0047 gate tools), MASK (it runs the in-pod gate),
   and OPENCODE (the harness API the kick script speaks).
   `tests/test_tool_pins.py` binds the manifest lines to the ARGs, so
   the two cannot drift.
2. **The running container publishes the attestation.** The opencode
   component's start command copies `/etc/veggies-tool-pins` to
   `/workspace/.veggies/image-tool-pins` (rm-first, tolerant of
   pre-manifest images) before `exec opencode serve`: the container
   self-certifies its own rootfs at every start - exactly what a
   CLI-side stamp (option C) cannot do.
3. **The kick gate compares before spending a session.**
   `scripts/stack_kick.py` reads the expected pins from the checkout's
   Containerfile (script-root anchored, the
   [0045](0045-repo-declared-verify-gate.md) precedent) - kicks branch
   off `origin/main`, so the comparison is precisely "what the session
   will be held to" vs "what the image carries", the skew window itself
   - then GETs the published manifest over `/file/content` and refuses
   (exit 3) a proven-stale image, the reason naming every skewed pin
   expected-vs-found plus the remedy (rebuild on the stack host from a
   pulled infra checkout, re-add the label).

The pip-pinned python layer (yamllint, ansible-lint, pre-commit-hooks)
is the same skew class but carries no ARG/sha pin shape in the image -
deliberately deferred; extending the manifest to it rides the
python-hooks conversion follow-up.

The block set is proven staleness only: any expected pin differing from
or absent in the live manifest; the manifest absent or empty on a
healthy, answering stack (the image predates the gate); the manifest
present but unparseable (corrupt). Exit 3 spends the one-shot label, so
it must mean "we KNOW the image is wrong". Everything else degrades
loud - a stderr note, and the kick proceeds: pins undiscoverable in the
checkout (adopted repos carry no Containerfile), ANY HTTPError incl.
401/403 (the kick itself fails identically on those), URLError/timeout
(a truly down stack fails the kick itself with rc 1, label preserved),
ValueError (opencode API-shape drift - a gate must never deny all kicks
on its own blind spot). This deliberately narrows
[0049](0049-no-ask-merged-roster-and-instruction-discovery.md)'s
unverifiable-fails-closed logic: a false-passed `ask` hangs a session
forever, but a false-passed stale image dies bounded at its own in-pod
gate with PR CI as backstop - while a false-blocked gate is total
automation denial.

Scope: issue-mode kicks only - distill/elaborate sessions never run the
toolchain. Adopted repos degrade with a note (above); a future opt-in
could declare pins in the kicked checkout the way 0045's marker
declares the verify gate.

## Consequences

- Positive: the build-vs-deploy skew closes as a class - for every
  current and future manifest pin, not just the 0047 pair. Skew
  discovery moves from a paid session's twentieth tool call to a
  zero-token GET, and the skip comment on the issue names expected vs
  found plus the remedy.
- Negative / accepted:
  - Enrollment skip: merging arms the gate immediately while a
    pre-manifest image still serves - every issue kick skips with the
    "predates the gate" message until one operator rebuild (`veggies
    sync` from a pulled infra checkout). That is the intended
    enforcement; the first skip comment is the enrollment notice.
  - Irreducible hole: a DOWNGRADED operator CLI re-upping a stack runs
    a start command without the publish stanza, so a stale manifest in
    the workspace can false-pass a rolled-back image (the rm-first
    publish covers rollback-with-current-CLI only). No runner-side
    detection exists - the exec channel is verified dead - so the
    mitigation is operational: `veggies sync` keeps the checkout
    current.
  - The manifest is advisory, not adversarial: a session could
    overwrite `/workspace/.veggies/image-tool-pins` to false-pass a
    future kick, but gains nothing - the false-pass buys one session
    that dies at its own in-pod gate - and the file self-heals at every
    container start.
- Amends [0047](0047-networkless-lint-hooks.md): its manual
  rollout-ordering consequence is now enforced by this gate. Amends
  nothing else.

## Links

- Amends: [0047](0047-networkless-lint-hooks.md)
- Related: [0005](0005-ephemeral-containerised-runners.md) (runner
  podman socket ownership),
  [0028](0028-retire-canvas-own-the-critic-loop.md) (no podman in-pod),
  [0033](0033-issue-triggered-agent-kicks.md) (the kick path),
  [0035](0035-one-shot-labels-and-done-guard.md) (one-shot label
  semantics), [0045](0045-repo-declared-verify-gate.md) (script-root
  anchoring precedent),
  [0049](0049-no-ask-merged-roster-and-instruction-discovery.md) (the
  gate-placement precedent this narrows)
- Issue #87, issue #83, discussion #81, issue #48
