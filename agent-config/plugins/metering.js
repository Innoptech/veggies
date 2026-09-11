/**
 * Veggies cost metering: stamp every harness LLM request with its session
 * identity (ADR 0047).
 *
 * The stack's litellm router copies the request's `x-litellm-tags` header
 * (comma-separated) into call metadata, and
 * agent-config/litellm/custom_callbacks.py parses `caller:` / `session-id:` /
 * `session-title:` (URI-encoded) tags out of that metadata into the per-call
 * JSONL cost log on the host. This plugin is the producer side of that
 * contract: the chat.headers hook fires per LLM request and stamps
 *
 *   caller:opencode,session-id:<sessionID>,session-title:<encodeURIComponent(title)>
 *
 * comma-appended to any header value other plugins already set. The title is
 * the ROOT session's: task subagents run in child sessions, so we walk
 * parentID links upward (max 8 hops) to attribute their spend to the kicked
 * session (titled `#N: <issue>`, immutable by convention - ADR 0034). When no
 * title resolves the title tag is skipped but caller + session-id are still
 * stamped, so the record stays joinable.
 *
 * Fail-open: any failure (API down, weird session shape) stamps nothing and
 * never throws into the request path - a model call must never fail over
 * metering. Positive title resolutions are cached per sessionID; misses are
 * NOT cached because manual sessions get auto-titled after their first call,
 * and a cached null would keep them unstamped forever.
 *
 * Module shape mirrors the vendored superpowers plugin (named export only):
 * opencode loads `{plugin,plugins}/*.js` from its config dirs and calls every
 * function export with ({ client, directory, ... }) (verified against the
 * pinned opencode 1.18.27 binary).
 */

export const VeggiesMetering = async ({ client, directory }) => {
  const titleCache = new Map(); // sessionID -> root title; positives only

  const rootTitle = async (sessionID) => {
    if (titleCache.has(sessionID)) return titleCache.get(sessionID);
    let id = sessionID;
    let title = null; // last non-empty title seen walking upward = root's
    let completed = false; // reached a session with no parentID
    for (let hop = 0; hop < 8 && id; hop++) {
      // Same call the built-in copilot plugin makes from inside this hook.
      const res = await client.session
        .get({ path: { id }, query: { directory }, throwOnError: true })
        .catch(() => undefined);
      const session = res && (res.data || res);
      if (!session) break;
      if (session.title) title = session.title;
      id = session.parentID;
      if (!id) completed = true;
    }
    // Cache only completed walks: a mid-walk fetch failure must not pin the
    // deepest-seen title forever (it is still RETURNED for this call, just
    // not cached). Renames intentionally stay cached for the server
    // lifetime - kicked titles are immutable by convention (ADR 0034).
    if (completed && title) titleCache.set(sessionID, title);
    return title;
  };

  return {
    "chat.headers": async (input, output) => {
      try {
        const sessionID =
          input.sessionID || (input.message && input.message.sessionID);
        if (!sessionID) return;
        const tags = ["caller:opencode", `session-id:${sessionID}`];
        const title = await rootTitle(sessionID);
        // Header-size safety (ADR 0047): an unbounded title tag would make
        // an unbounded x-litellm-tags header and kill every model call at
        // the HTTP layer - outside this hook's fail-open.
        if (title && encodeURIComponent(title).length <= 1024)
          tags.push(`session-title:${encodeURIComponent(title)}`);
        const existing = output.headers["x-litellm-tags"];
        output.headers["x-litellm-tags"] = existing
          ? `${existing},${tags.join(",")}`
          : tags.join(",");
      } catch {
        // fail-open: metering never breaks a model call
      }
    },
  };
};
