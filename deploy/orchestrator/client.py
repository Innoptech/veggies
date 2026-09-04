"""veggies orchestrator in-pod client (ADR 0017).

The CLI reaches the orchestrator via `podman exec ... python3
orchestrator-client.py <cmd>` - uniform local/remote, no published port.

  client.py draft --task TEXT | --task-file -   (task on stdin)
  client.py run [--task TEXT] --file -          (workflow yaml on stdin)
  client.py status [--one-line]
  client.py approve RUN_ID
"""

import json
import sys
import urllib.request
import urllib.error

PORT = 4400  # pod-internal; must match server.py and the component probe


def call(method, path, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode()
        return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "draft":
        task = sys.stdin.read() if "--task-file" in argv else argv[argv.index("--task") + 1]
        r = call("POST", "/draft", {"task": task})
        print(r.get("workflow_yaml") or json.dumps(r))
        return 0 if "workflow_yaml" in r else 1
    if cmd == "run":
        wf_yaml = sys.stdin.read()
        task = argv[argv.index("--task") + 1] if "--task" in argv else ""
        r = call("POST", "/run", {"workflow_yaml": wf_yaml, "task": task})
        print(json.dumps(r))
        return 0 if "run_id" in r else 1
    if cmd == "status":
        r = call("GET", "/status")
        if "--one-line" in argv:
            if "error" in r:
                print(f"error: {r['error']}")
                return 1
            parts = [f"{x['id']}:{x['name']}:{x['status']}" for x in r["runs"][:5]]
            print(f"{r['pending']} active ({'; '.join(parts) or 'none'})")
        else:
            print(json.dumps(r, indent=1))
        return 0
    if cmd == "approve":
        r = call("POST", f"/approve/{argv[1]}")
        print(json.dumps(r))
        return 0 if "approved" in r else 1
    print(f"unknown command {cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
