#!/usr/bin/env python3
"""Branch Compare — replay logged API requests against two git branches and diff the responses."""
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import requests as rq
import urllib3
from flask import Flask, jsonify, request, send_file, send_from_directory

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")

JOBS = {}
JOBS_LOCK = threading.Lock()

# Headers that only make sense on the original hop — never replay these.
SKIP_HEADERS = {
    "host", "content-length", "connection", "accept-encoding", "cookie",
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-port",
    "x-amzn-trace-id", "x-real-ip",
}


# ---------------------------------------------------------------- helpers

def git(repo, *args, timeout=30):
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return out


def is_git_repo(path):
    p = Path(path).expanduser()
    if not p.is_dir():
        return False
    return git(p, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def find_venv_python(repo):
    repo = Path(repo)
    for cand in ("src/.venv/bin/python", ".venv/bin/python", "venv/bin/python", "src/venv/bin/python"):
        p = repo / cand
        if p.exists():
            return p
    return None


def python_version(py):
    try:
        out = subprocess.run([str(py), "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().replace("Python ", "")
    except Exception:
        return None


def detect_entry(src_dir):
    for name in ("api.py", "application.py", "app.py", "main.py", "run.py"):
        if (Path(src_dir) / name).exists():
            return name
    return None


def parse_header_blob(blob):
    headers = {}
    if not blob:
        return headers
    for line in re.split(r"\r?\n", blob):
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k and k.lower() not in SKIP_HEADERS:
            headers[k] = v
    return headers


def parse_log_entries(raw, filename):
    """Parse one uploaded request-log JSON file into replayable request specs."""
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    specs = []
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("uri"):
            continue
        params = {}
        if entry.get("params"):
            try:
                parsed = json.loads(entry["params"])
                if isinstance(parsed, dict):
                    params = parsed
            except (ValueError, TypeError):
                pass
        specs.append({
            "uri": entry["uri"],
            "method": (entry.get("method") or "GET").upper(),
            "headers": parse_header_blob(entry.get("headers", "")),
            "params": params,
            "body": entry.get("request") or "",
            "source": filename,
        })
    return specs


def normalize_body(text):
    """Parse JSON if possible so the comparison ignores key order / whitespace."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def md_cell(text, limit=400):
    if text is None:
        text = ""
    text = str(text)
    if len(text) > limit:
        text = text[:limit] + " … [truncated]"
    return text.replace("|", "\\|").replace("\n", "<br>").replace("\r", "")


# ---------------------------------------------------------------- job runner

class Job:
    def __init__(self, cfg):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.status = "queued"
        self.error = None
        self.log = []
        self.progress = {"phase": "queued", "branch": None, "done": 0, "total": len(cfg["requests"])}
        self.branch_state = {b: {"status": "pending", "error": None, "base_url": None}
                             for b in (cfg["branch_a"], cfg["branch_b"])}
        self.results = []
        self.dir = RUNS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.report_path = None

    def emit(self, msg, level="info"):
        line = {"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
        self.log.append(line)

    def to_dict(self):
        return {
            "id": self.id, "status": self.status, "error": self.error,
            "log": self.log, "progress": self.progress,
            "branches": self.branch_state, "results": self.results,
            "report_ready": self.report_path is not None,
        }


def wait_port_free(host, port, job, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex((host, port)) != 0:
                return True
        time.sleep(1)
    job.emit(f"Port {port} still busy after {timeout}s — continuing anyway", "warn")
    return False


class BranchServer:
    """Starts the target API server for one branch inside an isolated worktree."""

    def __init__(self, job, branch):
        self.job = job
        self.branch = branch
        self.cfg = job.cfg
        self.repo = Path(self.cfg["repo"])
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", branch)
        self.worktree = job.dir / f"wt-{safe}"
        self.proc = None
        self.base_url = None
        self.log_file = job.dir / f"server-{safe}.log"

    def setup(self):
        job, branch = self.job, self.branch
        job.emit(f"[{branch}] Creating isolated worktree …")
        res = git(self.repo, "worktree", "add", "--force", "--detach", str(self.worktree), branch, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {res.stderr.strip()}")

        src = self.worktree / "src"
        if not src.is_dir():
            raise RuntimeError(f"branch '{branch}' has no src/ directory")

        # Setup step: envs_test/api/config.py -> src/config.py
        cfg_src = self.worktree / "envs_test" / "api" / "config.py"
        if not cfg_src.exists():
            cfg_src = self.repo / "envs_test" / "api" / "config.py"
        if cfg_src.exists():
            (src / "config.py").write_text(cfg_src.read_text())
            job.emit(f"[{branch}] Copied envs_test/api/config.py → src/config.py")
        else:
            job.emit(f"[{branch}] envs_test/api/config.py not found — skipping config copy", "warn")

    def start(self):
        job, branch = self.job, self.branch
        src = self.worktree / "src"

        py = self.cfg.get("venv_python") or find_venv_python(self.repo)
        if not py or not Path(py).exists():
            raise RuntimeError("No virtualenv python found (looked for src/.venv/bin/python)")
        ver = python_version(py)
        job.emit(f"[{branch}] Using venv python {ver} ({py})")
        if ver and not ver.startswith("3.9"):
            job.emit(f"[{branch}] Expected Python 3.9.x but venv has {ver}", "warn")

        cmd_tpl = (self.cfg.get("server_cmd") or "").strip()
        if cmd_tpl:
            parts = shlex.split(cmd_tpl)
            cmd = [str(py) if p == "python" else p for p in parts]
        else:
            entry = detect_entry(src)
            if not entry:
                raise RuntimeError("No server entrypoint (api.py / application.py) found in src/ on this branch")
            cmd = [str(py), entry]
        job.emit(f"[{branch}] Starting server: {' '.join(cmd)} (cwd={src})")

        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        logf = open(self.log_file, "w")
        self.proc = subprocess.Popen(
            cmd, cwd=str(src), env=env,
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._wait_ready()

    def _wait_ready(self):
        job, branch = self.job, self.branch
        override = (self.cfg.get("base_url") or "").strip().rstrip("/")
        timeout = int(self.cfg.get("startup_timeout") or 180)
        end = time.time() + timeout
        url_re = re.compile(r"Running on (https?://[\d.]+:\d+)")
        while time.time() < end:
            if self.proc.poll() is not None:
                tail = self._log_tail()
                raise RuntimeError(f"server exited with code {self.proc.returncode}\n{tail}")
            base = override
            if not base:
                text = self.log_file.read_text(errors="replace") if self.log_file.exists() else ""
                found = url_re.findall(text)
                if found:
                    base = found[-1].replace("://0.0.0.0", "://127.0.0.1")
            if base:
                try:
                    rq.get(base + "/health_check", verify=False, timeout=5)
                    self.base_url = base
                    job.emit(f"[{branch}] Server is up at {base}", "ok")
                    return
                except rq.RequestException:
                    pass
            time.sleep(1.5)
        raise RuntimeError(f"server did not become ready within {timeout}s\n{self._log_tail()}")

    def _log_tail(self, n=25):
        if not self.log_file.exists():
            return ""
        lines = self.log_file.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])

    def replay(self, spec):
        url = self.base_url + spec["uri"]
        timeout = int(self.cfg.get("request_timeout") or 120)
        started = time.time()
        try:
            kwargs = dict(headers=spec["headers"], params=spec["params"] or None,
                          verify=False, timeout=timeout)
            if spec["method"] in ("POST", "PUT", "PATCH", "DELETE") and spec["body"]:
                kwargs["data"] = spec["body"].encode("utf-8")
            resp = rq.request(spec["method"], url, **kwargs)
            return {
                "ok": True, "status": resp.status_code,
                "time_ms": round((time.time() - started) * 1000),
                "body": resp.text,
            }
        except rq.RequestException as e:
            return {
                "ok": False, "status": None,
                "time_ms": round((time.time() - started) * 1000),
                "body": f"REQUEST ERROR: {e}",
            }

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                    self.proc.wait(timeout=10)
            except (ProcessLookupError, PermissionError):
                pass
        self.job.emit(f"[{self.branch}] Server stopped")
        if self.base_url:
            m = re.match(r"https?://([\d.]+):(\d+)", self.base_url)
            if m:
                wait_port_free(m.group(1), int(m.group(2)), self.job)

    def cleanup(self):
        if self.worktree.exists():
            git(self.repo, "worktree", "remove", "--force", str(self.worktree), timeout=120)
            git(self.repo, "worktree", "prune")
            self.job.emit(f"[{self.branch}] Worktree removed")


def run_branch(job, branch):
    """Run all requests against one branch; returns list of per-request results."""
    state = job.branch_state[branch]
    server = BranchServer(job, branch)
    out = []
    try:
        state["status"] = "setup"
        job.progress.update(phase="setup", branch=branch, done=0)
        server.setup()
        state["status"] = "starting"
        job.progress["phase"] = "starting"
        server.start()
        state["base_url"] = server.base_url
        state["status"] = "running"
        job.progress["phase"] = "requests"
        for i, spec in enumerate(job.cfg["requests"]):
            job.progress["done"] = i
            res = server.replay(spec)
            tag = "ok" if res["ok"] and (res["status"] or 0) < 500 else "warn"
            job.emit(f"[{branch}] {spec['method']} {spec['uri']} → {res['status']} ({res['time_ms']} ms)", tag)
            out.append(res)
        job.progress["done"] = len(job.cfg["requests"])
        state["status"] = "done"
    except Exception as e:
        state["status"] = "failed"
        state["error"] = str(e)
        job.emit(f"[{branch}] FAILED: {e}", "error")
        missing = len(job.cfg["requests"]) - len(out)
        out += [{"ok": False, "status": None, "time_ms": 0,
                 "body": f"BRANCH FAILED: {e}"}] * missing
    finally:
        server.stop()
        server.cleanup()
    return out


def build_report(job, res_a, res_b):
    cfg = job.cfg
    a, b = cfg["branch_a"], cfg["branch_b"]
    rows, matches = [], 0
    for spec, ra, rb in zip(cfg["requests"], res_a, res_b):
        same_status = ra["status"] == rb["status"]
        same_body = normalize_body(ra["body"]) == normalize_body(rb["body"])
        match = bool(same_status and same_body)
        matches += match
        req_repr = spec["body"] if spec["body"] else json.dumps(spec["params"]) if spec["params"] else ""
        rows.append({
            "route": spec["uri"], "method": spec["method"],
            "request": req_repr, "source": spec["source"],
            "a": ra, "b": rb, "match": match,
        })
        job.results.append({
            "route": spec["uri"], "method": spec["method"], "request": req_repr,
            "source": spec["source"], "match": match,
            "a": {"status": ra["status"], "time_ms": ra["time_ms"], "body": ra["body"]},
            "b": {"status": rb["status"], "time_ms": rb["time_ms"], "body": rb["body"]},
        })

    total = len(rows)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = [
        "# API Branch Comparison Report",
        "",
        f"- **Repository:** `{cfg['repo']}`",
        f"- **Branches:** `{a}` vs `{b}`",
        f"- **Generated:** {ts}",
        f"- **Requests:** {total} &nbsp;|&nbsp; ✅ matching: {matches} &nbsp;|&nbsp; ❌ differing: {total - matches}",
        "",
        f"| # | Route | Method | Request | Response by `{a}` | Response by `{b}` | Match |",
        "|---|-------|--------|---------|---------|---------|:-----:|",
    ]
    for i, r in enumerate(rows, 1):
        ca = f"`{r['a']['status']}` ({r['a']['time_ms']} ms)<br>{md_cell(r['a']['body'])}"
        cb = f"`{r['b']['status']}` ({r['b']['time_ms']} ms)<br>{md_cell(r['b']['body'])}"
        md.append(f"| {i} | `{r['route']}` | {r['method']} | {md_cell(r['request'], 200)} "
                  f"| {ca} | {cb} | {'✅' if r['match'] else '❌'} |")

    diffs = [(i, r) for i, r in enumerate(rows, 1) if not r["match"]]
    if diffs:
        md += ["", "---", "", "## Full responses for differing requests", ""]
        for i, r in diffs:
            md += [
                f"### {i}. {r['method']} `{r['route']}`",
                "",
                "**Request:**", "```json", (r["request"] or "(empty)")[:5000], "```",
                f"**Response by `{a}`** — status {r['a']['status']}, {r['a']['time_ms']} ms:",
                "```json", (r["a"]["body"] or "(empty)")[:8000], "```",
                f"**Response by `{b}`** — status {r['b']['status']}, {r['b']['time_ms']} ms:",
                "```json", (r["b"]["body"] or "(empty)")[:8000], "```",
                "",
            ]

    path = job.dir / "report.md"
    path.write_text("\n".join(md))
    job.report_path = path
    (job.dir / "results.json").write_text(json.dumps(job.results, indent=2))
    job.emit(f"Report written → {path}", "ok")


def run_job(job):
    cfg = job.cfg
    try:
        job.status = "running"
        job.emit(f"Comparing '{cfg['branch_a']}' vs '{cfg['branch_b']}' on {cfg['repo']} "
                 f"({len(cfg['requests'])} requests)")
        res_a = run_branch(job, cfg["branch_a"])
        res_b = run_branch(job, cfg["branch_b"])
        job.progress["phase"] = "report"
        build_report(job, res_a, res_b)
        job.status = "done"
        job.progress["phase"] = "done"
        job.emit("All done 🎉", "ok")
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.emit(f"Job failed: {e}\n{traceback.format_exc()}", "error")


# ---------------------------------------------------------------- API routes

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/suggest-path")
def suggest_path():
    q = request.args.get("q", "").strip() or "~/"
    p = Path(os.path.expanduser(q))
    if q.endswith(os.sep):
        base, prefix = p, ""
    else:
        base, prefix = p.parent, p.name.lower()
    out = []
    try:
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name.startswith(".") and not prefix.startswith("."):
                continue
            if prefix and not child.name.lower().startswith(prefix):
                continue
            out.append({"path": str(child), "is_git": (child / ".git").exists()})
            if len(out) >= 15:
                break
    except OSError:
        pass
    out.sort(key=lambda d: (not d["is_git"], d["path"].lower()))
    return jsonify(out)


@app.get("/api/repo-info")
def repo_info():
    repo = os.path.expanduser(request.args.get("repo", ""))
    if not is_git_repo(repo):
        return jsonify({"valid": False, "error": "Not a git repository"})
    local = git(repo, "for-each-ref", "refs/heads", "--format=%(refname:short)").stdout.split()
    remote = [b for b in git(repo, "for-each-ref", "refs/remotes", "--format=%(refname:short)").stdout.split()
              if not b.endswith("/HEAD")]
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    py = find_venv_python(repo)
    src = Path(repo) / "src"
    return jsonify({
        "valid": True,
        "repo": str(Path(repo).resolve()),
        "current_branch": current,
        "local_branches": local,
        "remote_branches": remote,
        "venv_python": str(py) if py else None,
        "python_version": python_version(py) if py else None,
        "entry": detect_entry(src) if src.is_dir() else None,
        "has_env_config": (Path(repo) / "envs_test" / "api" / "config.py").exists(),
    })


@app.post("/api/parse")
def parse_files():
    files = request.json.get("files", [])
    all_specs, summary = [], []
    for f in files:
        try:
            specs = parse_log_entries(f["content"], f["name"])
            all_specs += specs
            methods = {}
            for s in specs:
                methods[s["method"]] = methods.get(s["method"], 0) + 1
            summary.append({"name": f["name"], "count": len(specs), "methods": methods})
        except Exception as e:
            summary.append({"name": f["name"], "count": 0, "error": str(e)})
    return jsonify({"requests": all_specs, "files": summary})


@app.post("/api/run")
def start_run():
    cfg = request.json
    for key in ("repo", "branch_a", "branch_b", "requests"):
        if not cfg.get(key):
            return jsonify({"error": f"missing '{key}'"}), 400
    cfg["repo"] = os.path.expanduser(cfg["repo"])
    if not is_git_repo(cfg["repo"]):
        return jsonify({"error": "repo is not a git repository"}), 400
    job = Job(cfg)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify({"job_id": job.id})


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job.to_dict())


@app.get("/api/report/<job_id>.md")
def report_md(job_id):
    job = JOBS.get(job_id)
    if not job or not job.report_path:
        return jsonify({"error": "report not ready"}), 404
    return send_file(job.report_path, as_attachment=True,
                     download_name=f"branch-compare-{job.cfg['branch_a']}-vs-{job.cfg['branch_b']}.md")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5599))
    print(f"\n  Branch Compare running → http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
