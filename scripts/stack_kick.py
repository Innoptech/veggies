#!/usr/bin/env python3
"""Kick a veggies stack from a GitHub issue (ADR 0033).

Creates a fresh opencode session on the repo's long-lived stack and durably
queues the issue as a prompt; the agent works it autonomously (branch,
code, `mask ci`, PR). Used by .github/workflows/agent-trigger.yml on the
self-hosted runners, and by hand from an operator machine:

    ssh -L 4099:127.0.0.1:4099 veggies   # if the stack is remote
    STACK_URL=http://127.0.0.1:4099 STACK_PASSWORD=... \
    ISSUE_NUMBER=12 ISSUE_TITLE="..." ISSUE_BODY="..." \
    ISSUE_URL=https://github.com/o/r/issues/12 REPO=o/r \
        python3 scripts/stack_kick.py

Stdlib-only. Exits non-zero with a message on any API failure.

Env:
    STACK_URL        base URL of the stack's opencode serve (no trailing /)
    STACK_PASSWORD   opencode serve basic-auth password
    ISSUE_NUMBER     GitHub issue number
    ISSUE_TITLE      issue title
    ISSUE_BODY       issue body (truncated to 4000 chars; may be empty)
    ISSUE_URL        issue html_url
    REPO             owner/name
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
SKIP_DONE = 3  # exit code: issue already handled (ADR 0035)


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
- Sync first: git fetch origin, then branch agent/issue-{number} from
  origin/main - the clone may be stale.
- python/mask/ansible/tofu/tflint/pre-commit/pytest are preinstalled in
  this image - do NOT run `mask setup` or build a venv.
- `SKIP=actionlint-docker mask ci` must pass before you push (the docker
  hook and molecule cannot run in this environment - note that in the PR
  body).
- Push the branch and open a PR with `gh pr create` whose body contains
  "Closes #{number}". The PR is the deliverable - work is not done until
  it exists and points at the issue.
- NEVER fork the repo or push anywhere but origin. If push is denied,
  report the exact missing token permission in your final message (the
  operator grants it) and stop.
- If you are truly blocked, push what you have as a draft PR and explain
  the blocker in the PR body.
- Finish the task completely; never end your turn with a next step
  unexecuted.
"""

COMMENT_SECTION = """
Triggered by a comment from @{author} on the issue:
\"\"\"
{comment}
\"\"\"
"""


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
    missing = [k for k in ("STACK_URL", "STACK_PASSWORD", "ISSUE_NUMBER",
                           "ISSUE_TITLE", "ISSUE_URL", "REPO")
               if not os.environ.get(k)]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return 2
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


if __name__ == "__main__":
    sys.exit(main())
