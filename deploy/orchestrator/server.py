"""veggies orchestrator server (ADR 0017). Runs inside the pod, drives the
harness over its loopback HTTP API. Boring stdlib + yaml + jinja2 (core.py).

Endpoints (127.0.0.1:4400, pod-internal):
  GET  /healthz
  POST /draft    {task, pipeline?}            -> {workflow_yaml} (sync; slow)
  POST /run      {task, workflow_yaml}        -> {run_id}        (async)
  GET  /status   -> {runs: [...], pending: n}
  POST /approve/<run_id>                       -> releases a parked run

State: sqlite at $ORCH_STATE/runs.db. Permissions from the harness's
`permission.asked` SSE are auto-answered per the workflow's policy
(default deny - fail loud, never park silently).
"""

import json
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
import urllib.error
import uuid
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import importlib.util as _ilu


def _load_core():
    """core.py ships alongside this file as orchestrator-core.py
    (config_files); dashes aren't importable module names, so load by path."""
    here = Path(__file__).with_name("orchestrator-core.py")
    spec = _ilu.spec_from_file_location("orchestrator_core", here)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _load_core()

PORT = 4400
HARNESS = None          # set from HARNESS_URL env
AUTH = None             # basic auth header for the harness
STATE_DIR = Path("/stack-state/orchestrator")
WORKSPACE = Path("/workspace")
DB = None
DB_LOCK = threading.Lock()
RUN_EVENTS: dict[str, threading.Event] = {}   # run_id -> approval event
SESSION_RUN: dict[str, str] = {}              # harness session id -> run_id
RUN_POLICY: dict[str, str] = {}               # run_id -> permission policy


# --- harness API ---------------------------------------------------------------

def hapi(method, path, body=None, timeout=300, stream=False):
    url = f"{HARNESS}{path}{'&' if '?' in path else '?'}directory=/workspace"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", AUTH)
    if data:
        req.add_header("Content-Type", "application/json")
    r = urllib.request.urlopen(req, timeout=timeout)
    if stream:
        return r
    raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def roster():
    agents = hapi("GET", "/agent")
    return [{"name": a.get("name"), "description": a.get("description", "")}
            for a in agents]


def session_new(title):
    return hapi("POST", "/session", {"title": title})["id"]


def session_prompt(sid, text, agent=None, model=None, wait=True):
    body = {"parts": [{"type": "text", "text": text}]}
    if agent:
        body["agent"] = agent
    if model:
        body["model"] = {"providerID": "litellm", "modelID": model}
    if wait:
        return hapi("POST", f"/session/{sid}/message", body, timeout=600)
    hapi("POST", f"/session/{sid}/prompt_async", body)
    return None


def session_idle(sid):
    st = hapi("GET", "/session/status", timeout=30)
    return sid not in st  # verified: only non-idle sessions are listed


def session_complete(sid):
    """True once an assistant message exists after our user message AND no
    tool part is still running (verified 2026-09-04: the question tool parks
    a headless session forever - that is still-working, not complete)."""
    msgs = hapi("GET", f"/session/{sid}/message", timeout=60)
    replied = False
    for m in msgs:
        if m.get("info", {}).get("role") != "assistant":
            continue
        replied = True
        for part in m.get("parts", []):
            if part.get("type") == "tool" and part.get("state", {}).get("status") == "running":
                return False
    return replied


def session_output(sid):
    msgs = hapi("GET", f"/session/{sid}/message", timeout=60)
    texts = [p.get("text", "") for m in reversed(msgs)
             for p in m.get("parts", []) if p.get("type") == "text"]
    return texts[0] if texts else ""


def session_diff(sid):
    try:
        return json.dumps(hapi("GET", f"/session/{sid}/diff", timeout=60))[:8000]
    except Exception:
        return ""


def session_replied(sid):
    msgs = hapi("GET", f"/session/{sid}/message", timeout=60)
    return any(m.get("info", {}).get("role") == "assistant" for m in msgs)


def pending_question(sid):
    """The question tool's ask, if one is parked (verified 2026-09-04: it
    waits forever headless; there is no answer API - abort-on-sight)."""
    try:
        msgs = hapi("GET", f"/session/{sid}/message", timeout=30)
    except Exception:
        return None
    for m in msgs:
        for part in m.get("parts", []):
            if (part.get("type") == "tool" and part.get("tool") == "question"
                    and part.get("state", {}).get("status") == "running"):
                qs = part.get("state", {}).get("input", {}).get("questions", [])
                return "; ".join(q.get("question", "?") for q in qs) or "?"
    return None


def wait_session(sid, deadline):
    """-> True (complete) | False (timeout) | str (parked on a question -
    the session is aborted by the caller)."""
    last_q_check = 0.0
    while time.time() < deadline:
        if session_idle(sid):
            if session_complete(sid):
                return True
        elif time.time() - last_q_check > 15:
            last_q_check = time.time()
            q = pending_question(sid)
            if q is not None:
                return f"agent asked a question (headless impossible): {q}"
        time.sleep(3)
    return False


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)  # podman logs


# --- run store -----------------------------------------------------------------

def db():
    return DB


def run_create(wf, wf_raw, task):
    rid = uuid.uuid4().hex[:8]
    detail = {"steps": {s["id"]: {"status": "pending", "why": s.get("why", "")}
                        for s in wf["steps"]},
              "awaiting": None, "error": None}
    with DB_LOCK:
        db().execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
            (rid, wf["name"], task, wf_raw, "running",
             json.dumps(detail), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        db().commit()
    RUN_EVENTS[rid] = threading.Event()
    RUN_POLICY[rid] = wf["permissions"]
    return rid


def run_update(rid, status=None, detail_mut=None):
    with DB_LOCK:
        row = db().execute("SELECT status, detail FROM runs WHERE id=?", (rid,)).fetchone()
        if not row:
            return
        st, detail = row[0], json.loads(row[1])
        if detail_mut:
            detail_mut(detail)
        db().execute("UPDATE runs SET status=?, detail=? WHERE id=?",
                     (status or st, json.dumps(detail), rid))
        db().commit()


def run_get(rid):
    with DB_LOCK:
        row = db().execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "task": row[2], "workflow_yaml": row[3],
            "status": row[4], "detail": json.loads(row[5]), "created": row[6]}


def runs_all():
    with DB_LOCK:
        rows = db().execute("SELECT * FROM runs ORDER BY created DESC LIMIT 20").fetchall()
    return [{"id": r[0], "name": r[1], "task": r[2][:80], "status": r[4],
             "detail": json.loads(r[5]), "created": r[6]} for r in rows]


# --- executor --------------------------------------------------------------------

def step_context(run, wf):
    ctx = {"task": run["task"], "steps": {}}
    for s in wf["steps"]:
        sd = run["detail"]["steps"].get(s["id"], {})
        ctx["steps"][s["id"]] = {"output": sd.get("output", ""), "diff": sd.get("diff", "")}
    return ctx


def set_step(rid, sid, **kw):
    def mut(detail):
        detail["steps"][sid].update(kw)
    run_update(rid, detail_mut=mut)


def park_until_approved(rid, kind, sid, extra=None):
    def mut(detail):
        detail["awaiting"] = {"kind": kind, "step": sid, **(extra or {})}
    run_update(rid, status="awaiting-approval", detail_mut=mut)
    webhook_notify(run_get(rid))
    RUN_EVENTS[rid].clear()
    # park until approved (event set by /approve); re-check hourly forever
    while not RUN_EVENTS[rid].wait(timeout=3600):
        pass
    run_update(rid, status="running", detail_mut=lambda d: d.update(awaiting=None))


def webhook_notify(run):
    hook = yaml.safe_load(run["workflow_yaml"]).get("notify_webhook")
    if not hook:
        return
    try:
        payload = json.dumps({"run": run["id"], "name": run["name"],
                              "awaiting": run["detail"].get("awaiting")}).encode()
        req = urllib.request.Request(hook, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # notification is best-effort; the parked run is the source of truth


def exec_agent_step(run, wf, step):
    rid = run["id"]
    ctx = step_context(run, wf)
    prompt = core.render_prompt(step["prompt"], ctx)
    n = step.get("parallel")
    combine = step.get("combine")
    if n and combine == "shard":
        split = session_new(f"{rid}:{step['id']}:split")
        SESSION_RUN[split] = rid
        r = session_prompt(split, "Split this task into exactly "
                           f"{n} disjoint subtasks. Reply with ONLY a JSON array of "
                           f"strings.\n\nTask:\n{prompt}")
        subs = re.search(r"\[.*\]", session_output(split) or "", re.S)
        prompts = json.loads(subs.group(0))[:n] if subs else [prompt]
        hapi("DELETE", f"/session/{split}")
    else:
        prompts = [prompt] * (n or 1)

    sessions, results = [], []
    for i, p in enumerate(prompts):
        log(f"run {rid} step {step['id']}: creating session {i}")
        sid = session_new(f"{rid}:{step['id']}:{i}")
        log(f"run {rid} step {step['id']}: session {sid} created; prompting async")
        SESSION_RUN[sid] = rid
        session_prompt(sid, p, agent=step["agent"], model=step.get("model"), wait=False)
        log(f"run {rid} step {step['id']}: prompt dispatched to {sid}")
        sessions.append(sid)
    deadline = time.time() + step["timeout"]
    ok = True
    log(f"run {rid} step {step['id']}: waiting on {len(sessions)} session(s)")
    for sid in sessions:
        res = wait_session(sid, deadline)
        if res is not True:
            if isinstance(res, str):  # question-parked: abort, fail loud
                try:
                    hapi("POST", f"/session/{sid}/abort")
                except Exception:
                    pass
                set_step(rid, step["id"], status="failed", error=res)
                log(f"run {rid} step {sid}: {res[:120]}")
                return False
            set_step(rid, step["id"], status="failed",
                     error=f"timeout after {step['timeout']}s")
            return False
        if not session_replied(sid):
            set_step(rid, step["id"], status="failed",
                     error="session ended with no assistant reply "
                           "(dispatch failed? unknown agent/model?)")
            return False
        if run_get(rid)["status"] == "failed":  # permission denied mid-step
            ok = False
    for sid in sessions:
        results.append({"output": session_output(sid), "diff": session_diff(sid)})
        hapi("DELETE", f"/session/{sid}")
        SESSION_RUN.pop(sid, None)
    set_step(rid, step["id"], status="done" if ok else "failed",
             output="\n---\n".join(r["output"] for r in results)[:8000],
             diff="\n".join(r["diff"] for r in results if r["diff"])[:8000],
             sessions=sessions)
    return ok


def exec_shell_step(run, step):
    rid = run["id"]
    try:
        p = subprocess.run(["sh", "-c", step["run"]], cwd=WORKSPACE,
                           capture_output=True, text=True, timeout=step["timeout"])
        out = (p.stdout + p.stderr)[-4000:]
        set_step(rid, step["id"], status="done" if p.returncode == 0 else "failed",
                 output=out)
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        set_step(rid, step["id"], status="failed",
                 error=f"timeout after {step['timeout']}s")
        return False


def execute(rid):
    try:
        run = run_get(rid)
        wf = core.validate_workflow(yaml.safe_load(run["workflow_yaml"]),
                                    [a["name"] for a in roster()])
        log(f"run {rid} ({wf['name']}): {len(wf['steps'])} steps, "
            f"policy={wf['permissions']}")
        for tier in wf["order"]:
            for sid in tier:
                step = next(s for s in wf["steps"] if s["id"] == sid)
                log(f"run {rid} step {sid} ({step['kind']}) started")
                ok = (exec_agent_step(run, wf, step) if step["kind"] == "agent"
                      else exec_shell_step(run, step))
                if not ok:
                    log(f"run {rid} step {sid} FAILED")
                    run_update(rid, status="failed",
                               detail_mut=lambda d: d.update(error=f"step {sid} failed"))
                    return
                log(f"run {rid} step {sid} done")
                if step.get("gate") == "approval":
                    log(f"run {rid} parked at gate {sid}")
                    park_until_approved(rid, "gate", sid)
        run_update(rid, status="done")
        log(f"run {rid} DONE")
    except Exception as e:
        log(f"run {rid} ERROR: {type(e).__name__}: {e}")
        run_update(rid, status="failed",
                   detail_mut=lambda d: d.update(error=f"{type(e).__name__}: {e}"))


# --- permission watcher (SSE) ----------------------------------------------------

def answer_permission(sid, pid, response):
    hapi("POST", f"/session/{sid}/permissions/{pid}", {"response": response})


def watch_permissions():
    while True:  # reconnect forever; the harness restarts occasionally
        try:
            r = hapi("GET", "/event", stream=True, timeout=None)
            buf = b""
            while True:
                chunk = r.read(1)
                if not chunk:
                    break
                buf += chunk
                if chunk != b"\n":
                    continue
                line, buf = buf.decode(errors="replace").strip(), b""
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "permission.asked":
                    continue
                props = ev.get("properties", {})
                sid, pid = props.get("sessionID"), props.get("id")
                rid = SESSION_RUN.get(sid)
                if rid is None:
                    continue  # an interactive session's permission: not ours
                policy = RUN_POLICY.get(rid, "deny")
                if policy == "allow":
                    answer_permission(sid, pid, "once")
                elif policy == "deny":
                    answer_permission(sid, pid, "reject")
                    run_update(rid, status="failed", detail_mut=lambda d: d.update(
                        error=f"permission denied by policy: {props.get('permission')}"))
                else:  # ask: park until a human approves
                    park_until_approved(rid, "permission", "?", {"permission_id": pid,
                                                                 "session_id": sid})
                    answer_permission(sid, pid, "once")
        except Exception:
            time.sleep(5)


# --- draft + run entry points -----------------------------------------------------

def draft(task, pipeline=None):
    ros = roster()
    log(f"draft: task={task[:60]!r} roster={len(ros)} agents")
    prompt = core.build_drafter_prompt(task, ros, pipeline)
    last_err = None
    for _ in range(2):  # one retry with the error fed back
        sid = session_new("drafter")
        try:
            session_prompt(sid, prompt, agent="plan")
            reply = session_output(sid)
            raw = core.extract_workflow_yaml(reply)
            wf = core.validate_workflow(yaml.safe_load(raw),
                                        [a["name"] for a in ros])
            return yaml.safe_dump(core.to_submission(wf))
        except (core.WorkflowError, yaml.YAMLError) as e:
            last_err = e
            prompt += f"\n\nYour previous reply failed validation: {e}. Fix it."
        finally:
            hapi("DELETE", f"/session/{sid}")
    raise core.WorkflowError(f"drafter failed twice: {last_err}")


# --- HTTP surface --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if self.path == "/status":
            runs = runs_all()
            return self._send(200, {"runs": runs, "pending": sum(
                1 for r in runs if r["status"] in ("running", "awaiting-approval"))})
        self._send(404, {"error": "unknown path"})

    def do_POST(self):
        try:
            if self.path == "/draft":
                b = self._body()
                return self._send(200, {"workflow_yaml": draft(b["task"], b.get("pipeline"))})
            if self.path == "/run":
                b = self._body()
                ros = [a["name"] for a in roster()]
                wf = core.validate_workflow(yaml.safe_load(b["workflow_yaml"]), ros)
                rid = run_create(wf, b["workflow_yaml"], b.get("task", ""))
                log(f"run {rid} created ({wf['name']})")
                threading.Thread(target=execute, args=(rid,), daemon=True).start()
                return self._send(200, {"run_id": rid})
            m = re.fullmatch(r"/approve/([0-9a-f]{8})", self.path)
            if m:
                rid = m.group(1)
                if run_get(rid) and rid in RUN_EVENTS:
                    RUN_EVENTS[rid].set()
                    return self._send(200, {"approved": rid})
                return self._send(404, {"error": "no such parked run"})
            m = re.fullmatch(r"/abort/([0-9a-f]{8})", self.path)
            if m:
                rid = m.group(1)
                run = run_get(rid)
                if not run or run["status"] not in ("running", "awaiting-approval"):
                    return self._send(404, {"error": "no such live run"})
                log(f"run {rid} aborted by operator")
                for sid, owner in list(SESSION_RUN.items()):
                    if owner == rid:
                        try:
                            hapi("POST", f"/session/{sid}/abort")
                        except Exception:
                            pass
                run_update(rid, status="failed",
                           detail_mut=lambda d: d.update(error="aborted by operator"))
                if rid in RUN_EVENTS:
                    RUN_EVENTS[rid].set()  # wake a parked gate; it re-reads status
                return self._send(200, {"aborted": rid})
            self._send(404, {"error": "unknown path"})
        except (core.WorkflowError, yaml.YAMLError, KeyError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    global DB, HARNESS, AUTH
    import os
    HARNESS = os.environ["HARNESS_URL"]
    AUTH = "Basic " + b64encode(
        f"opencode:{os.environ['HARNESS_PASSWORD']}".encode()).decode()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DB = sqlite3.connect(STATE_DIR / "runs.db", check_same_thread=False)
    DB.execute("CREATE TABLE IF NOT EXISTS runs "
               "(id TEXT PRIMARY KEY, name TEXT, task TEXT, workflow_yaml TEXT, "
               " status TEXT, detail TEXT, created TEXT)")
    n = DB.execute("UPDATE runs SET status='failed' "
                   "WHERE status IN ('running','awaiting-approval')").rowcount
    DB.commit()
    if n:
        log(f"boot: marked {n} orphaned run(s) failed (orchestrator restarted)")
    threading.Thread(target=watch_permissions, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
