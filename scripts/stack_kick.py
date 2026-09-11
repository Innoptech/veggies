#!/usr/bin/env python3
"""Kick a veggies stack from a GitHub issue (ADR 0033) or discussion
(ADR 0038).

Creates a fresh opencode session on the repo's long-lived stack and durably
queues the prompt; an issue kick works it autonomously in its own
git worktree (/workspace/.veggies/wt/issue-N, ADR 0037) through the
mandated pipeline (plan posted on the issue, subagent execution,
adversarial review, `mask ci`, PR - ADR 0036), a discussion kick distills
the thread into issues (plan / happy path / criteria of success). Used by
.github/workflows/agent-trigger.yml on the self-hosted runners, and by hand
from an operator machine:

    ssh -L 4099:127.0.0.1:4099 veggies   # if the stack is remote
    STACK_URL=http://127.0.0.1:4099 STACK_PASSWORD=... \
    ISSUE_NUMBER=12 ISSUE_TITLE="..." ISSUE_BODY="..." \
    ISSUE_URL=https://github.com/o/r/issues/12 REPO=o/r \
        python3 scripts/stack_kick.py

Stdlib-only. Exits non-zero with a message on any API failure.

Env:
    STACK_URL          base URL of the stack's opencode serve (no trailing /)
    STACK_PASSWORD     opencode serve basic-auth password
    REPO               owner/name
    ISSUE_NUMBER       GitHub issue number (issue mode)
    ISSUE_TITLE        issue title
    ISSUE_BODY         issue body (truncated to 4000 chars; may be empty)
    ISSUE_URL          issue html_url
    DISCUSSION_NUMBER  discussion number (discussion mode; beats ISSUE_*)
    DISCUSSION_TITLE   discussion title
    DISCUSSION_BODY    opening post (payload copy; truncated like ISSUE_BODY)
    DISCUSSION_URL     discussion html_url
    DISCUSSION_COMMAND discussion sub-command: "elaborate" fans the thread
                       out to five persona subagents, each posting one
                       attributed POV comment (issue #33); anything else
                       (incl. unset) distills the thread into issues
    GITHUB_TOKEN       issue mode: done-guard (ADR 0035); discussion mode:
                       fetches the comment thread (REST) - without it the
                       prompt carries the opening post only
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 60
BODY_LIMIT = 4000
COMMENT_LIMIT = 2000  # per discussion comment
THREAD_BUDGET = 12000  # total chars of rendered discussion thread
SKIP_DONE = 3  # exit code: issue already handled (ADR 0035)

# The elaborate persona roster (issue #33): (agent name, display role) per
# persona. Each definition lives in agent-config/agents/<name>.md, and
# test_persona_roster_matches_agent_files binds this tuple to those files.
PERSONAS: tuple[tuple[str, str], ...] = (
    ("domain-expert", "Domain expert"),
    ("infra-architect", "Infra/architecture"),
    ("marketer", "Marketer"),
    ("seller", "Seller"),
    ("cto", "CTO"),
)


def gh_api(token: str, path: str) -> object:
    """GET api.github.com with a Bearer token (the done-guard; the kick
    itself never touches GitHub)."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def done_reason(repo: str, number: str, token: str) -> str | None:
    """Why this issue should NOT be (re-)kicked, or None. 'Done' = the issue
    is closed, or an agent/issue-N PR already exists (any state - open means
    in flight, merged means shipped). Re-kicking a done issue burns a
    session and produces duplicate branches (ADR 0035)."""
    issue = gh_api(token, f"/repos/{repo}/issues/{number}")
    if issue.get("state") != "open":
        return f"issue state is {issue.get('state')}"
    owner = repo.split("/", 1)[0]
    prs = gh_api(token, f"/repos/{repo}/pulls"
                        f"?head={owner}:agent/issue-{number}&state=all&per_page=1")
    if prs:
        pr = prs[0]
        return f"PR {pr.get('html_url')} already exists ({pr.get('state')})"
    return None

PROMPT_TEMPLATE = """You are the veggies agent for {repo}, working unattended in the stack's clone at /workspace.

GitHub issue #{number}: {title}
{url}

{body}

Rules of engagement:
- Work autonomously. Never block waiting for a human - decide, and record
  your assumptions in the PR body.
- Read AGENTS.md first and follow it (ADR rules, conventional commits,
  the `mask ci` gate).
- Isolate first (ADR 0037): other sessions share this clone, so this issue
  works in its own git worktree. Run exactly, in order; if a command
  fails, stop and read the error before improvising:
    git -C /workspace fetch origin
    git -C /workspace worktree add --lock --reason 'session issue-{number}' -B agent/issue-{number} /workspace/.veggies/wt/issue-{number} origin/main
    ex=/workspace/.git/info/exclude; mkdir -p "$(dirname "$ex")"; grep -qxF '/.veggies/' "$ex" 2>/dev/null || echo '/.veggies/' >> "$ex"
    cd /workspace/.veggies/wt/issue-{number}
  ALL work (edits, `mask ci`, commits, push) happens inside that worktree.
  File tools resolve relative paths against the session dir (/workspace),
  not the shell's cwd. Use absolute paths under /workspace/.veggies/wt/issue-{number} for every read/edit.
  The shared checkout at /workspace itself is read-only to you - never
  edit, commit, or switch branches there.
  Recovery: if `worktree add` fails with "already used by worktree" or
  "already exists", another (possibly crashed) session owns the branch or
  path - take agent/issue-{number}-2 and wt/issue-{number}-2 and say so in
  the PR body; never pass -f/--force to `git worktree add` (it overrides
  the tripwire) and never remove a worktree you did not create. Transient
  lock errors under parallel kicks ("cannot lock ref", "Unable to
  create...lock"): wait a few seconds and retry. If
  origin/agent/issue-{number} exists from a crashed run, reconcile
  (merge/rebase); force-push only with --force-with-lease.
- python/mask/ansible/tofu/tflint/pre-commit/pytest are preinstalled in
  this image - do NOT run `mask setup` or build a venv.
- Push the branch and open a PR with `gh pr create` whose body contains
  "Closes #{number}". The PR is the deliverable - work is not done until
  it exists and points at the issue.

Workflow (mandatory, in order - this project exists to run the full
pipeline, not to dive straight into code):
1. Plan: invoke the superpowers `brainstorming` skill to pin down the
   issue's intent (no human is available to answer - decide and record
   assumptions), then `writing-plans` for a step-by-step plan. Post the
   plan as a comment on the issue (`gh issue comment {number} --body
   "..."`) BEFORE writing code, so a human can veto the direction cheaply.
2. Subagents: execute the plan through the `task` tool (superpowers
   `subagent-driven-development`) - dispatch implementation to subagents
   (swe-expert, tdd-tester, ...) instead of doing everything in the main
   loop.
3. Adversarial review: before pushing, dispatch the `adversarial-review`
   subagent on the full diff (it runs a different model on purpose).
   Fix, or explicitly rebut in the PR body, every critical/major finding.
4. Verify: `SKIP=actionlint-docker mask ci` must pass before you push
   (the docker hook and molecule cannot run in this environment - note
   that in the PR body). Claim only what you actually ran.
- NEVER fork the repo or push anywhere but origin. If push is denied,
  report the exact missing token permission in your final message (the
  operator grants it) and stop.
- If you are truly blocked, push what you have as a draft PR and explain
  the blocker in the PR body.
- Finish the task completely; never end your turn with a next step
  unexecuted.
"""

COMMENT_SECTION = """
Triggered by a comment from @{author}:
\"\"\"
{comment}
\"\"\"
"""

DISCUSSION_PROMPT_TEMPLATE = """You are the veggies agent for {repo}, working unattended in the stack's clone at /workspace.

GitHub discussion #{number}: {title}
{url}

{body}

{thread}

Mission: distill this discussion into GitHub issues. Read the whole thread
first - the value is in what we had been talking about, not only in the
opening post. Never invent work the thread does not call for.

For each distinct piece of work the discussion asks for:
1. Check it is not already tracked: `gh issue list --repo {repo} --search "<keywords>"`.
   Issues previously spawned from this discussion link back to it; refine
   your plan instead of duplicating them (never edit issues you did not
   create).
2. Create it with `gh issue create --repo {repo} --title "..." --body-file -`.
   Every issue body carries exactly these sections:
   - ## Context - one short paragraph, linking back to this discussion.
   - ## Plan - the approach as the thread converged on it.
   - ## Happy path - the walkthrough of the thing working as intended.
   - ## Criteria of success - the checkable conditions that make it done.
3. Do NOT add the `agent-task` label: a human reviews the new issues first
   and labels deliberately (the label kicks another agent, ADR 0035).
4. When every issue exists, comment the created issue links back on the
   discussion (best effort - the bot PAT may lack Discussions: write).
   Discussions reject addComment; use addDiscussionComment (ADR 0039):
   fetch the node id with
   `gh api repos/{repo}/discussions/{number} --jq .node_id`, then
   `gh api graphql -f query='mutation($id: ID!, $body: String!) {{ addDiscussionComment(input: {{discussionId: $id, body: $body}}) {{ clientMutationId }} }}' -f id=<node id> -f body="..."`.

Rules of engagement:
- Work autonomously. Never block waiting for a human - decide, and record
  your assumptions in the issue bodies (or the discussion comment).
- Read AGENTS.md first and follow it.
- No code changes: do not branch, commit, push, or open a PR - the
  deliverable is the set of issues plus the summary comment.
- gh is authenticated as the veggies bot (GH_TOKEN, ADR 0030). If issue
  creation is denied, name the exact missing token permission in your
  final message and stop (the operator grants it).
- Finish the task completely; never end your turn with a next step
  unexecuted.
"""

ELABORATE_PROMPT_TEMPLATE = """You are the veggies agent for {repo}, working unattended in the stack's clone at /workspace.

GitHub discussion #{number}: {title}
{url}

{body}

{thread}

Mission: elaborate this discussion with five attributed expert POVs (issue
#33) - one per rostered persona. The roster, as <agent name> (<role>);
each persona's definition lives in agent-config/agents/<name>.md:

- domain-expert (Domain expert)
- infra-architect (Infra/architecture)
- marketer (Marketer)
- seller (Seller)
- cto (CTO)

Read the whole thread first - every POV must engage what the thread has
been discussing, not only the opening post. Disagreeing with the thread is
allowed; being generic is not.

1. Dispatch one task subagent per persona - five in total (the house
   pattern, ADR 0036). Each task prompt carries the persona's agent name
   and the WHOLE thread - the opening post plus every comment - and asks
   for that persona's POV on the discussion. Each subagent returns its POV
   text only; it never calls gh or touches files.
2. Post exactly one comment per persona on the discussion - five in
   total, each body starting with its attribution header line, the
   `**<Role> POV**` of its role, verbatim:
   - **Domain expert POV**
   - **Infra/architecture POV**
   - **Marketer POV**
   - **Seller POV**
   - **CTO POV**
   Post each comment as its POV arrives. Discussions reject addComment;
   use addDiscussionComment (ADR 0039): fetch the node id with
   `gh api repos/{repo}/discussions/{number} --jq .node_id`, then
   `gh api graphql -f query='mutation($id: ID!, $body: String!) {{ addDiscussionComment(input: {{discussionId: $id, body: $body}}) {{ clientMutationId }} }}' -f id=<node id> -f body="..."`.

Rules of engagement:
- Work autonomously. Never block waiting for a human - decide, and record
  your assumptions in the persona comments.
- Read AGENTS.md first and follow it.
- No code changes, no tracking artifacts: do not branch, commit, push,
  open a PR, or create issues - the deliverable is exactly the five
  attributed POV comments on this discussion.
- gh is authenticated as the veggies bot (GH_TOKEN, ADR 0030). If a
  posting call is denied, name the exact missing token permission in your
  final message and stop (the operator grants it).
- Finish the task completely; never end your turn with a next step
  unexecuted.
"""


def fetch_discussion_comments(repo: str, number: str,
                              token: str) -> list[tuple[str, str]]:
    """(author, body) per discussion comment, oldest first (REST; ADR 0033
    feared GraphQL but discussions read fine over REST - verified 2026-09).
    The fetch degrades to [] like the done-guard degrades to proceeding
    (ADR 0035): a hiccup must never block a deliberate kick."""
    try:
        raw = gh_api(token, f"/repos/{repo}/discussions/{number}/comments"
                            "?per_page=100")
    except Exception as e:
        print(f"comment fetch failed ({e}); kicking with the opening post "
              "only", file=sys.stderr)
        return []
    return [((c.get("user") or {}).get("login") or "?", c.get("body") or "")
            for c in raw]


def render_thread(comments: list[tuple[str, str]]) -> str:
    """Pure: the thread section, chronological, per-comment and total
    budgets. Over budget, the tail is what gets omitted - the opening
    context frames the whole discussion, so it is kept verbatim first."""
    if not comments:
        return "Discussion thread: (no comments yet)"
    parts, used, shown = [], 0, 0
    for author, body in comments:
        chunk = f"@{author}:\n{body.strip()[:COMMENT_LIMIT]}"
        if used + len(chunk) > THREAD_BUDGET:
            break
        parts.append(chunk)
        used += len(chunk)
        shown += 1
    out = "Discussion thread:\n" + "\n\n".join(parts)
    if shown < len(comments):
        out += (f"\n\n(thread truncated: {len(comments) - shown} more "
                "comment(s) omitted)")
    return out


def build_discussion_prompt(repo: str, number: str, title: str, body: str,
                            url: str, comments: list[tuple[str, str]],
                            comment: str = "",
                            comment_author: str = "") -> str:
    """Pure: the discussion->issues kick prompt. The triggering comment
    rides along like on issue kicks - it is the only context a token-less
    manual kick has beyond the opening post."""
    body = (body or "").strip()[:BODY_LIMIT] or "(no description)"
    prompt = DISCUSSION_PROMPT_TEMPLATE.format(
        repo=repo, number=number, title=title, body=body, url=url,
        thread=render_thread(comments))
    if comment.strip():
        prompt += COMMENT_SECTION.format(author=comment_author or "?",
                                         comment=comment.strip()[:2000])
    return prompt


def build_elaborate_prompt(repo: str, number: str, title: str, body: str,
                           url: str, comments: list[tuple[str, str]],
                           comment: str = "",
                           comment_author: str = "") -> str:
    """Pure: the discussion->persona-POVs kick prompt (issue #33). Same
    shape as build_discussion_prompt - same body budget, same rendered
    thread, and the triggering comment rides along."""
    body = (body or "").strip()[:BODY_LIMIT] or "(no description)"
    prompt = ELABORATE_PROMPT_TEMPLATE.format(
        repo=repo, number=number, title=title, body=body, url=url,
        thread=render_thread(comments))
    if comment.strip():
        prompt += COMMENT_SECTION.format(author=comment_author or "?",
                                         comment=comment.strip()[:2000])
    return prompt


def build_prompt(repo: str, number: str, title: str, body: str,
                 url: str, comment: str = "", comment_author: str = "") -> str:
    """Pure: the kick prompt for one issue (label trigger) or for a
    comment on it (comment trigger - the comment text rides along)."""
    body = (body or "").strip()[:BODY_LIMIT] or "(no description)"
    prompt = PROMPT_TEMPLATE.format(repo=repo, number=number, title=title,
                                    body=body, url=url)
    if comment.strip():
        prompt += COMMENT_SECTION.format(author=comment_author or "?",
                                         comment=comment.strip()[:2000])
    return prompt


def api(url: str, password: str, method: str, path: str,
        body: dict | None = None) -> dict:
    """One opencode API call with the stack's basic auth. Raises on
    transport/HTTP error; the caller turns that into an exit code."""
    req = urllib.request.Request(
        f"{url}{path}?directory=/workspace", method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"opencode:{password}".encode()).decode())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else {}


def inflight_reason(url: str, password: str, number: str) -> str | None:
    """Why this issue should not be kicked RIGHT NOW, or None: a session
    titled '#<number>: ...' is busy on the stack. The done-guard covers
    finished work (closed issue, existing PR); this covers the race window
    where a kick is mid-flight - the PR does not exist yet, so without it
    every stray trigger (a bot comment mentioning /opencode, a label plus a
    comment, a rapid re-label) double-books the issue (ADR 0040; verified
    2026-09-11 on issue #33: the agent's own plan comment re-kicked it).
    Raises on API failure; the caller degrades to proceeding, like the
    done-guard."""
    sessions = api(url, password, "GET", "/session")
    status = api(url, password, "GET", "/session/status")
    prefix = f"#{number}: "
    for s in sessions if isinstance(sessions, list) else []:
        sid = s.get("id", "?")
        if str(s.get("title", "")).startswith(prefix) and \
                (status.get(sid) or {}).get("type") == "busy":
            return f"session {sid} is already working this issue (busy)"
    return None


def kick(url: str, password: str, prompt: str, title: str = "") -> str:
    """Create a session and fire the prompt async. Returns session id.
    The title shows in the web UI session list (ADR 0034)."""
    created = api(url, password, "POST", "/session",
                  {"title": title} if title else {})
    sid = created.get("id")
    if not sid:
        raise RuntimeError(f"session create returned no id: {created!r}")
    # prompt_async admits and starts the loop, returning 204 immediately
    # (verified live against opencode 1.18.27, 2026-09-10). The newer
    # /api/session/{id}/prompt only *admits* to a durable queue - an idle
    # session never starts - and the sync /message POST would hold this
    # HTTP call for the whole agent run.
    api(url, password, "POST", f"/session/{sid}/prompt_async",
        {"parts": [{"type": "text", "text": prompt}]})
    return sid


def main() -> int:
    discussion = os.environ.get("DISCUSSION_NUMBER", "")
    required = ["STACK_URL", "STACK_PASSWORD", "REPO"]
    if discussion:
        required += ["DISCUSSION_NUMBER", "DISCUSSION_TITLE",
                     "DISCUSSION_URL"]
    else:
        required += ["ISSUE_NUMBER", "ISSUE_TITLE", "ISSUE_URL"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 2
    if discussion:
        return main_discussion(discussion)
    url = os.environ["STACK_URL"].rstrip("/")
    # Done-guard (ADR 0035): never re-kick a handled issue. Needs a GitHub
    # token (the workflow's own GITHUB_TOKEN); without one, warn and proceed
    # - a manual kick is the operator's call.
    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if gh_token:
        try:
            reason = done_reason(os.environ["REPO"],
                                 os.environ["ISSUE_NUMBER"], gh_token)
        except Exception as e:  # the guard degrades, it never blocks
            print(f"guard check failed ({e}); proceeding", file=sys.stderr)
            reason = None
        if reason:
            print(f"SKIP: {reason}")
            print(f"SKIP_REASON={reason}")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                    f.write(f"skip_reason={reason}\n")
            return SKIP_DONE
    else:
        print("no GITHUB_TOKEN/GH_TOKEN in env; done-guard skipped",
              file=sys.stderr)
    # In-flight guard (ADR 0040): never double-book an issue a busy session
    # holds. Same degrade-to-proceed posture as the done-guard.
    try:
        reason = inflight_reason(url, os.environ["STACK_PASSWORD"],
                                 os.environ["ISSUE_NUMBER"])
    except Exception as e:
        print(f"in-flight check failed ({e}); proceeding", file=sys.stderr)
        reason = None
    if reason:
        print(f"SKIP: {reason}")
        print(f"SKIP_REASON={reason}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"skip_reason={reason}\n")
        return SKIP_DONE
    title = f"#{os.environ['ISSUE_NUMBER']}: {os.environ['ISSUE_TITLE']}"
    prompt = build_prompt(os.environ["REPO"], os.environ["ISSUE_NUMBER"],
                          os.environ["ISSUE_TITLE"],
                          os.environ.get("ISSUE_BODY", ""),
                          os.environ["ISSUE_URL"],
                          comment=os.environ.get("COMMENT_BODY", ""),
                          comment_author=os.environ.get("COMMENT_AUTHOR", ""))
    try:
        sid = kick(url, os.environ["STACK_PASSWORD"], prompt, title=title)
    except (urllib.error.URLError, RuntimeError, TimeoutError,
            json.JSONDecodeError) as e:
        print(f"kick failed: {e}", file=sys.stderr)
        return 1
    print(f"session queued: {sid} on {url} "
          f"(issue #{os.environ['ISSUE_NUMBER']})")
    print(f"SESSION_ID={sid}")  # machine-readable, one per line
    if os.environ.get("GITHUB_OUTPUT"):  # Actions convention
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"session_id={sid}\n")
    return 0


def main_discussion(number: str) -> int:
    """Discussion mode (ADR 0038): no done-guard - unlike a lingering label,
    a /opencode (or /elaborate, issue #33) comment is a deliberate act, and
    re-kicking an evolving discussion is the point (the prompt dedupes
    against existing issues)."""
    url = os.environ["STACK_URL"].rstrip("/")
    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if gh_token:
        comments = fetch_discussion_comments(os.environ["REPO"], number,
                                             gh_token)
    else:
        print("no GITHUB_TOKEN/GH_TOKEN in env; discussion thread not "
              "fetched (opening post + triggering comment only)",
              file=sys.stderr)
        comments = []
    # DISCUSSION_COMMAND selects the discussion sub-command (issue #33):
    # "elaborate" -> persona POV comments; anything else (incl. unset) ->
    # the ADR 0038 distill path, byte-identical.
    if os.environ.get("DISCUSSION_COMMAND", "") == "elaborate":
        title = f"D#{number} elaborate: {os.environ['DISCUSSION_TITLE']}"
        prompt = build_elaborate_prompt(
            os.environ["REPO"], number, os.environ["DISCUSSION_TITLE"],
            os.environ.get("DISCUSSION_BODY", ""),
            os.environ["DISCUSSION_URL"], comments,
            comment=os.environ.get("COMMENT_BODY", ""),
            comment_author=os.environ.get("COMMENT_AUTHOR", ""))
    else:
        title = f"D#{number}: {os.environ['DISCUSSION_TITLE']}"
        prompt = build_discussion_prompt(
            os.environ["REPO"], number, os.environ["DISCUSSION_TITLE"],
            os.environ.get("DISCUSSION_BODY", ""), os.environ["DISCUSSION_URL"],
            comments, comment=os.environ.get("COMMENT_BODY", ""),
            comment_author=os.environ.get("COMMENT_AUTHOR", ""))
    try:
        sid = kick(url, os.environ["STACK_PASSWORD"], prompt, title=title)
    except (urllib.error.URLError, RuntimeError, TimeoutError,
            json.JSONDecodeError) as e:
        print(f"kick failed: {e}", file=sys.stderr)
        return 1
    print(f"session queued: {sid} on {url} (discussion #{number})")
    print(f"SESSION_ID={sid}")  # machine-readable, one per line
    if os.environ.get("GITHUB_OUTPUT"):  # Actions convention
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"session_id={sid}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
