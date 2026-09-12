#!/usr/bin/env python3
"""Post a GitHub comment, verifying it landed before any retry (#101).

All four comment steps of .github/workflows/agent-trigger.yml call this
helper: post the comment, and on an errors[] payload or a transport
failure VERIFY whether the comment landed before retrying - so a
successful write never goes red and a genuinely lost write never goes
green.

The mutation split is inherited from ADR 0039: issues take addComment,
discussions take addDiscussionComment (discussions reject addComment).

Verified why (2026-09-12, workflow run 34706580396): GitHub executed the
mutation, then 500'd composing the response - the reply was HTTP 200
with errors[] ("Something went wrong while executing your query") while
the comment itself was already on the discussion, and the step's
`jq -e 'has("errors") | not'` failed the job anyway. So errors[] is
checked against ground truth (the subject's actual comment list), never
trusted as "not posted".

Accepted residual: mutation-executed + verify-fails + retry +
verify-fails still fails red with the comment present (the retry may
have posted a duplicate) - duplicate-over-silent is the deliberate
precedence: a false red pings a human, a false green silently drops the
comment.

Usage (env + stdin):
    GH_TOKEN=... SUBJECT_ID=<node id> SUBJECT_KIND=issue \
        python3 scripts/gh_comment.py < body.md

    GH_TOKEN      GitHub token (sent as Bearer)
    SUBJECT_ID    GraphQL node id of the issue or discussion
    SUBJECT_KIND  exactly "issue" or "discussion"

The comment body is read fully from stdin. Exit 0 whenever the comment
is present on the subject (posted cleanly, or verified landed after
errors); exit 1 with the error payloads on stderr only when it genuinely
never landed; exit 2 on usage errors. Stdlib-only: the workflow runs
this on a runner with bare python3 + setup-python.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

TIMEOUT = 60
RETRY_DELAY = 2  # seconds between the failed mutation and the retry
VERIFY_WINDOW = 50  # how many of the subject's newest comments get scanned

MUTATION = {
    "issue": "mutation($id: ID!, $body: String!) { addComment(input: {subjectId: $id, body: $body}) { clientMutationId } }",
    "discussion": "mutation($id: ID!, $body: String!) { addDiscussionComment(input: {discussionId: $id, body: $body}) { clientMutationId } }",
}
VERIFY_QUERY = {
    "issue": f"query($id: ID!) {{ node(id: $id) {{ ... on Issue {{ comments(last: {VERIFY_WINDOW}) {{ nodes {{ body }} }} }} }} }}",
    "discussion": f"query($id: ID!) {{ node(id: $id) {{ ... on Discussion {{ comments(last: {VERIFY_WINDOW}) {{ nodes {{ body }} }} }} }} }}",
}

# The idempotency key embedded in workflow comment bodies: the actions
# run URL - unique per workflow run and immune to GitHub body
# normalization.
MARKER = re.compile(r"https?://\S+/actions/runs/\d+")

USAGE = ("usage: GH_TOKEN=<token> SUBJECT_ID=<node id> "
         "SUBJECT_KIND=issue|discussion "
         "python3 scripts/gh_comment.py < body.md")


def graphql(token: str, query: str, variables: dict) -> dict:
    """POST https://api.github.com/graphql; return the parsed payload.
    Transport failures (HTTPError/URLError/TimeoutError) propagate as
    OSError; a 200-with-errors[] payload is RETURNED, not raised."""
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def marker_of(body: str) -> str | None:
    """The idempotency key embedded in workflow comment bodies: the
    actions run URL, first match, else None. Unique per workflow run and
    immune to GitHub body normalization."""
    match = MARKER.search(body)
    return match.group(0) if match else None


def landed(token: str, kind: str, subject_id: str, body: str) -> bool:
    """True when the subject's last VERIFY_WINDOW comments contain this
    comment: marker containment when marker_of(body) finds a run URL,
    else rstrip() exact equality (GitHub may trim a trailing newline).
    ANY query failure (transport or errors[]) is treated as not-landed:
    retry is then safe-ish by design (duplicate over silent)."""
    try:
        payload = graphql(token, VERIFY_QUERY[kind], {"id": subject_id})
    except OSError:
        return False
    if payload.get("errors"):
        return False
    node = (payload.get("data") or {}).get("node") or {}
    comments = (node.get("comments") or {}).get("nodes") or []
    marker = marker_of(body)
    for comment in comments:
        seen = (comment or {}).get("body") or ""
        if marker:
            if marker in seen:
                return True
        elif seen.rstrip() == body.rstrip():
            return True
    return False


def _mutate(token: str, kind: str, subject_id: str, body: str):
    """One mutation attempt: None on a clean data payload, else the
    offending payload (errors[] or data-less) or the transport
    exception."""
    try:
        payload = graphql(token, MUTATION[kind],
                          {"id": subject_id, "body": body})
    except OSError as e:
        return e
    if payload.get("errors") or not isinstance(payload.get("data"), dict):
        return payload
    return None


def _render(error) -> str:
    """One-line stderr rendering of an errors[] payload or a transport
    exception."""
    if isinstance(error, dict):
        return json.dumps(error)
    return str(error)


def post(token: str, kind: str, subject_id: str, body: str) -> str:
    """Post the comment; return the outcome string. Flow:
    1. mutation; clean data -> "posted".
    2. On errors[]/OSError: echo the raw error to stderr; landed() ->
       "landed-after-errors".
    3. sleep(RETRY_DELAY); mutation again; clean -> "posted-on-retry".
    4. errors[]/OSError again: echo; landed() ->
       "landed-after-retry-errors".
    5. Else raise SystemExit(1); both error payloads are on stderr."""
    first = _mutate(token, kind, subject_id, body)
    if first is None:
        return "posted"
    print(f"comment mutation failed: {_render(first)}", file=sys.stderr)
    if landed(token, kind, subject_id, body):
        return "landed-after-errors"
    time.sleep(RETRY_DELAY)
    second = _mutate(token, kind, subject_id, body)
    if second is None:
        return "posted-on-retry"
    print(f"comment mutation retry failed: {_render(second)}",
          file=sys.stderr)
    if landed(token, kind, subject_id, body):
        return "landed-after-retry-errors"
    print("comment never landed; failing red "
          f"(first: {_render(first)}; retry: {_render(second)})",
          file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    """Read env + stdin, post(), print the outcome to stdout."""
    token = os.environ.get("GH_TOKEN", "")
    subject_id = os.environ.get("SUBJECT_ID", "")
    kind = os.environ.get("SUBJECT_KIND", "")
    if not token or not subject_id or kind not in MUTATION:
        print(f"gh_comment: bad env - {USAGE}", file=sys.stderr)
        raise SystemExit(2)
    body = sys.stdin.read()
    if not body.strip():
        print(f"gh_comment: empty comment body on stdin - {USAGE}",
              file=sys.stderr)
        raise SystemExit(2)
    print(post(token, kind, subject_id, body))


if __name__ == "__main__":
    main()
