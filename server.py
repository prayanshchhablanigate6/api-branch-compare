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
    """Pick a virtualenv python for the target app.

    Strategy:
      1. Collect candidate venvs from inside the repo and from sibling repos
         under the repo's parent directory.
      2. Prefer the one that can actually `import stripe` (and a couple of
         other commonly-required modules) — the app's hard third-party deps.
         If none qualify, fall back to the first existing candidate.
    """
    repo = Path(repo)
    rel_paths = (
        "src/.venv/bin/python", ".venv/bin/python",
        "venv/bin/python", "src/venv/bin/python",
    )
    candidates = []
    seen = set()

    def add(p):
        try:
            rp = p.resolve()
        except OSError:
            return
        if rp in seen or not p.exists():
            return
        seen.add(rp)
        candidates.append(p)

    for rel in rel_paths:
        add(repo / rel)
    # Sibling repos under the same parent (e.g. ~/work/<other-repo>/src/.venv).
    parent = repo.parent
    if parent.exists():
        for sibling in parent.iterdir():
            if not sibling.is_dir() or sibling == repo:
                continue
            for rel in rel_paths:
                add(sibling / rel)

    if not candidates:
        return None

    # Probe candidates for the modules the target app needs at import time.
    probe = "import stripe, ddtrace, botocore"
    for py in candidates:
        try:
            r = subprocess.run([str(py), "-c", probe],
                               capture_output=True, text=True, timeout=8)
            if r.returncode == 0:
                return py
        except (subprocess.TimeoutExpired, OSError):
            continue
    # Nothing fully qualified — just return the first existing candidate so the
    # caller can surface a useful error later.
    return candidates[0]


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


# Appended to the copied config.py: secrets contain server-only paths
# (e.g. /var/app/current/ssl/poolbrain.pem); remap them to files that exist
# next to the source tree so the app can boot locally. Also force the listen
# port to whatever branch-compare assigned (load_secrets() above blows away
# PORT/SOCKET_PORT with values from AWS Secrets Manager, so we use a separate
# env var name that secrets will never touch).
CONFIG_LOCAL_SHIM = '''

# --- appended by branch-compare: remap EC2-only file paths to local ones
# and pin the listen port to the BC_PORT we injected ---
_bc_orig_init = init
def init():
    import os as _os
    env = _bc_orig_init()
    here = _os.path.dirname(_os.path.abspath(__file__))
    fallbacks = {"cert_file": "ssl.cert", "key_file": "ssl.key",
                 "cert_file_socket": "ssl.cert", "key_file_socket": "ssl.key"}
    for key, legacy in fallbacks.items():
        v = env.get(key)
        if v and not _os.path.exists(v):
            for cand in (_os.path.join(here, "ssl", _os.path.basename(v)),
                         _os.path.join(here, _os.path.basename(v)),
                         _os.path.join(here, legacy)):
                if _os.path.exists(cand):
                    env[key] = cand
                    break
    # Force the per-branch port assigned by branch-compare. AWS secrets
    # overwrite PORT/SOCKET_PORT during load_secrets(), so we use BC_*.
    bc_port = _os.environ.get("BC_PORT")
    if bc_port:
        env["port"] = int(bc_port)
    bc_sock = _os.environ.get("BC_SOCKET_PORT")
    if bc_sock:
        env["socketPort"] = bc_sock
    return env
'''


# ---------------------------------------------------------------- job runner

class Job:
    def __init__(self, cfg, prior_results=None, start_index=0):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.status = "queued"
        self.error = None
        self.log = []
        # Carry over results from a previous stopped job when resuming, and
        # skip the matching number of specs at the head of cfg['requests'].
        self.results = list(prior_results or [])
        self.start_index = int(start_index or 0)
        self.progress = {
            "phase": "queued", "branch": None,
            "done": len(self.results), "total": len(cfg["requests"]),
        }
        self.branch_state = {b: {"status": "pending", "error": None, "base_url": None}
                             for b in (cfg["branch_a"], cfg["branch_b"])}
        self.dir = RUNS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.report_path = None
        # Control flags. pause_event SET = running; CLEARED = paused.
        # cancel_event SET = stop requested.
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.cancel_event = threading.Event()

    def emit(self, msg, level="info"):
        line = {"t": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
        self.log.append(line)

    @property
    def paused(self):
        return not self.pause_event.is_set()

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def to_dict(self):
        return {
            "id": self.id, "status": self.status, "error": self.error,
            "log": self.log, "progress": self.progress,
            "branches": self.branch_state, "results": self.results,
            "report_ready": self.report_path is not None,
            "paused": self.paused, "cancelled": self.cancelled,
            "can_resume": self.status in ("stopped", "error")
                          and len(self.results) < len(self.cfg["requests"]),
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


def pick_free_port():
    """Ask the OS for an unused TCP port. Tiny TOCTOU window, but plenty
    good for handing out one port per branch."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BranchServer:
    """Starts the target API server for one branch inside an isolated worktree."""

    def __init__(self, job, branch, port=None, socket_port=None):
        self.job = job
        self.branch = branch
        self.cfg = job.cfg
        self.repo = Path(self.cfg["repo"])
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", branch)
        self.worktree = job.dir / f"wt-{safe}"
        self.proc = None
        self.port = port
        self.socket_port = socket_port
        # When we assign a port ourselves we also know the base URL up front,
        # so we don't need to scrape it from the server's stdout.
        self.base_url = f"http://127.0.0.1:{port}" if port else None
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
            (src / "config.py").write_text(cfg_src.read_text() + CONFIG_LOCAL_SHIM)
            job.emit(f"[{branch}] Copied envs_test/api/config.py → src/config.py (+ local path shim)")
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
        # Force a unique port per branch so the two servers don't collide on
        # whatever the repo's default PORT happens to be. We use BC_PORT (not
        # PORT) because some apps load secrets via os.environ.update(...) and
        # that would clobber PORT — the config shim reads BC_PORT.
        if self.port:
            env["BC_PORT"] = str(self.port)
            env["BC_SOCKET_PORT"] = str(self.socket_port or self.port + 1)
            # Also set PORT/SOCKET_PORT as a fallback for apps that read them
            # directly without going through the config shim.
            env["PORT"] = env["BC_PORT"]
            env["SOCKET_PORT"] = env["BC_SOCKET_PORT"]
            job.emit(f"[{branch}] Assigned BC_PORT={self.port} BC_SOCKET_PORT={env['BC_SOCKET_PORT']}")
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
        url_re = re.compile(r"Running on (https?(?://|\+unix://|s://)?[\d.]+:\d+)")
        # Some apps (this one included) serve over HTTPS with a self-signed
        # cert. Probe both schemes when the port is known.
        candidate_schemes = ("http", "https") if self.port else None
        while time.time() < end:
            if self.proc.poll() is not None:
                tail = self._log_tail()
                raise RuntimeError(f"server exited with code {self.proc.returncode}\n{tail}")
            base = override or self.base_url
            if not base:
                text = self.log_file.read_text(errors="replace") if self.log_file.exists() else ""
                found = re.findall(r"Running on (https?://[\d.]+:\d+)", text)
                if found:
                    local = [u for u in found if "127.0.0.1" in u]
                    base = (local[-1] if local else found[-1]).replace("://0.0.0.0", "://127.0.0.1")
            if base:
                tried = [base] if not candidate_schemes else [
                    f"{s}://127.0.0.1:{self.port}" for s in candidate_schemes
                ]
                for url in tried:
                    try:
                        rq.get(url + "/health_check", verify=False, timeout=5)
                        self.base_url = url
                        job.emit(f"[{branch}] Server is up at {url}", "ok")
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


def _bring_up(job, branch, server):
    """Worktree + start one branch's server. Marks state on success or failure."""
    state = job.branch_state[branch]
    try:
        state["status"] = "setup"
        server.setup()
        state["status"] = "starting"
        server.start()
        state["base_url"] = server.base_url
        state["status"] = "running"
    except Exception as e:
        state["status"] = "failed"
        state["error"] = str(e)
        job.emit(f"[{branch}] FAILED: {e}", "error")


def _row_for(spec, ra, rb):
    same_status = ra["status"] == rb["status"]
    same_body = normalize_body(ra["body"]) == normalize_body(rb["body"])
    req_repr = spec["body"] if spec["body"] else json.dumps(spec["params"]) if spec["params"] else ""
    return {
        "route": spec["uri"], "method": spec["method"], "request": req_repr,
        "source": spec["source"], "match": bool(same_status and same_body),
        "a": {"status": ra["status"], "time_ms": ra["time_ms"], "body": ra["body"]},
        "b": {"status": rb["status"], "time_ms": rb["time_ms"], "body": rb["body"]},
    }


def render_report_md(job):
    """Render the current job.results to a markdown string. Pure — safe to
    call mid-run from a request thread."""
    cfg = job.cfg
    a, b = cfg["branch_a"], cfg["branch_b"]
    rows = list(job.results)  # snapshot
    total = len(rows)
    matches = sum(1 for r in rows if r["match"])
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    in_progress = job.status == "running"
    header_suffix = " (partial)" if in_progress else ""
    md = [
        f"# API Branch Comparison Report{header_suffix}",
        "",
        f"- **Repository:** `{cfg['repo']}`",
        f"- **Branches:** `{a}` vs `{b}`",
        f"- **Generated:** {ts}",
        f"- **Requests:** {total} of {job.progress.get('total', total)}"
        f" &nbsp;|&nbsp; ✅ matching: {matches} &nbsp;|&nbsp; ❌ differing: {total - matches}",
    ]
    if in_progress:
        md.append(f"- **Status:** in progress — export reflects rows completed so far")
    md += [
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
    return "\n".join(md)


def write_report(job):
    """Persist report.md + results.json to disk."""
    path = job.dir / "report.md"
    path.write_text(render_report_md(job))
    job.report_path = path
    (job.dir / "results.json").write_text(json.dumps(job.results, indent=2))
    job.emit(f"Report written → {path}", "ok")


def run_job(job):
    """Bring up both branches in parallel, then replay each request on both
    simultaneously, streaming each row into job.results as soon as it lands."""
    cfg = job.cfg
    a, b = cfg["branch_a"], cfg["branch_b"]
    # Pre-pick non-colliding ports so both branches' servers can coexist.
    port_a, port_b = pick_free_port(), pick_free_port()
    while port_b == port_a:
        port_b = pick_free_port()
    servers = {
        a: BranchServer(job, a, port=port_a, socket_port=pick_free_port()),
        b: BranchServer(job, b, port=port_b, socket_port=pick_free_port()),
    }
    try:
        job.status = "running"
        job.emit(f"Comparing '{a}' vs '{b}' on {cfg['repo']} "
                 f"({len(cfg['requests'])} requests, both branches in parallel)")

        # ---- 1. setup + start both branches in parallel
        job.progress.update(phase="setup", branch=None, done=0)
        threads = [threading.Thread(target=_bring_up, args=(job, br, servers[br]), daemon=True)
                   for br in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        failed = [br for br in (a, b) if job.branch_state[br]["status"] != "running"]
        if failed:
            raise RuntimeError(f"branch(es) failed to start: {', '.join(failed)}")

        # ---- 2. replay each request on both branches in parallel; stream rows
        job.progress["phase"] = "requests"
        specs = cfg["requests"]
        start_at = max(job.start_index, len(job.results))
        if start_at:
            job.emit(f"Resuming — skipping first {start_at} requests already completed")
        for i in range(start_at, len(specs)):
            # Cancellation: bail out cleanly before issuing the next request.
            if job.cancelled:
                job.emit("Stop requested — halting before next request", "warn")
                break
            # Pause: block here until resumed (or cancelled).
            if job.paused:
                job.emit("Paused", "warn")
                while not job.pause_event.wait(timeout=0.5):
                    if job.cancelled:
                        break
                if job.cancelled:
                    job.emit("Stop requested while paused", "warn")
                    break
                job.emit("Resumed", "ok")

            spec = specs[i]
            job.progress["done"] = i
            results = {}

            def hit(br):
                results[br] = servers[br].replay(spec)

            ts = [threading.Thread(target=hit, args=(br,)) for br in (a, b)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

            ra, rb = results[a], results[b]
            for br, r in ((a, ra), (b, rb)):
                tag = "ok" if r["ok"] and (r["status"] or 0) < 500 else "warn"
                job.emit(f"[{br}] {spec['method']} {spec['uri']} → "
                         f"{r['status']} ({r['time_ms']} ms)", tag)
            job.results.append(_row_for(spec, ra, rb))

        job.progress["done"] = len(job.results)

        # ---- 3. final report (always written from whatever results we have)
        job.progress["phase"] = "report"
        write_report(job)
        if job.cancelled:
            for br in (a, b):
                job.branch_state[br]["status"] = "done"
            job.status = "stopped"
            job.progress["phase"] = "stopped"
            job.emit("Run stopped by user — partial report saved", "warn")
        else:
            for br in (a, b):
                job.branch_state[br]["status"] = "done"
            job.status = "done"
            job.progress["phase"] = "done"
            job.emit("All done 🎉", "ok")
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.emit(f"Job failed: {e}\n{traceback.format_exc()}", "error")
    finally:
        for br, srv in servers.items():
            try:
                srv.stop()
            except Exception as e:
                job.emit(f"[{br}] stop error: {e}", "warn")
        for br, srv in servers.items():
            try:
                srv.cleanup()
            except Exception as e:
                job.emit(f"[{br}] cleanup error: {e}", "warn")


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


@app.post("/api/job/<job_id>/pause")
def job_pause(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job.status != "running":
        return jsonify({"error": f"cannot pause job in status '{job.status}'"}), 400
    job.pause_event.clear()
    return jsonify({"ok": True, "paused": True})


@app.post("/api/job/<job_id>/resume")
def job_resume(job_id):
    """Resume a currently paused job in place."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job.status != "running" or not job.paused:
        return jsonify({"error": f"job is not paused (status '{job.status}')"}), 400
    job.pause_event.set()
    return jsonify({"ok": True, "paused": False})


@app.post("/api/job/<job_id>/stop")
def job_stop(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if job.status not in ("running", "queued"):
        return jsonify({"error": f"cannot stop job in status '{job.status}'"}), 400
    job.cancel_event.set()
    # Unblock the worker if it's currently parked on a pause.
    job.pause_event.set()
    job.emit("Stop requested by user — will halt after current request", "warn")
    return jsonify({"ok": True})


@app.post("/api/job/<job_id>/restart")
def job_restart(job_id):
    """Start a fresh job with the same config. With ?resume=1, carry over
    completed results and skip those specs."""
    prev = JOBS.get(job_id)
    if not prev:
        return jsonify({"error": "unknown job"}), 404
    if prev.status in ("running", "queued"):
        return jsonify({"error": "previous job still active — stop it first"}), 400
    resume = request.args.get("resume") in ("1", "true")
    prior = prev.results if resume else None
    start_index = len(prev.results) if resume else 0
    job = Job(prev.cfg, prior_results=prior, start_index=start_index)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify({"job_id": job.id, "resumed": resume,
                    "carried_over": len(prior or [])})


@app.get("/api/report/<job_id>.md")
def report_md(job_id):
    """Render the report on demand from current job.results. Works mid-run
    after even a single request has completed."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    if not job.results:
        return jsonify({"error": "no rows yet — wait for at least one request to complete"}), 404
    from flask import Response
    body = render_report_md(job)
    a, b = job.cfg["branch_a"], job.cfg["branch_b"]
    suffix = "-partial" if job.status == "running" else ""
    fname = f"branch-compare-{a}-vs-{b}{suffix}.md"
    return Response(body, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5599))
    print(f"\n  Branch Compare running → http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
