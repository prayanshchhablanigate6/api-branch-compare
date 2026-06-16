#!/usr/bin/env python3
"""Branch Compare — replay logged API requests against two git branches and diff the responses."""
import ast
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    """Accept any of:
      - a dict already
      - a JSON dict string, e.g. {"Authorization": "Bearer ..."}
      - a Python-repr dict string (single-quoted) — our request-log capture
        format dumps headers via str(dict), e.g.
            {'Authorization': 'Bearer ...', 'X-Forwarded-For': '1.2.3.4'}
      - a colon-separated text blob ("Header: value" per line).
    Strips hop-by-hop headers that shouldn't be replayed.
    """
    headers = {}
    if not blob:
        return headers

    parsed = None
    if isinstance(blob, dict):
        parsed = blob
    elif isinstance(blob, str):
        s = blob.strip()
        if s.startswith("{"):
            # Try JSON first, then Python literal (handles single-quoted dicts).
            for loader in (json.loads, ast.literal_eval):
                try:
                    cand = loader(s)
                    if isinstance(cand, dict):
                        parsed = cand
                        break
                except (ValueError, SyntaxError):
                    continue

    if isinstance(parsed, dict):
        for k, v in parsed.items():
            k = str(k).strip()
            if k and k.lower() not in SKIP_HEADERS:
                headers[k] = str(v)
        return headers

    # Fallback: "Header: value" lines
    for line in re.split(r"\r?\n", str(blob)):
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


# ---------------------------------------------------------------- mongo build

# response_code values we treat as "the request actually worked" — anything
# else (4xx/5xx, 405 method-not-allowed, 0/None) is dropped before the AI sees it.
def _resp_code(doc):
    for key in ("response_code", "status_code", "status"):
        v = doc.get(key)
        if v in (None, ""):
            continue
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            continue
    # Some logs only carry the code inside the response JSON {"status": "200"}.
    resp = doc.get("response")
    if isinstance(resp, str) and resp.strip().startswith("{"):
        try:
            j = json.loads(resp)
            if "status" in j:
                return int(str(j["status"]).strip())
        except (ValueError, TypeError):
            pass
    return None


def _doc_is_success(doc):
    code = _resp_code(doc)
    return code is not None and 200 <= code < 300


def normalize_route(route):
    """Turn a user-supplied route token into a clean URI path.

    Accepts 'meid', '/meid', 'config?', 'inventory/inventory_adjustment_from_job'.
    Strips whitespace, comments, a trailing '?' (uncertainty marker), and
    guarantees a single leading slash.
    """
    r = (route or "").strip()
    if not r or r.startswith("#"):
        return None
    r = r.split()[0].strip()          # drop inline notes after whitespace
    r = r.rstrip("?").strip()         # '?' marks "not sure" — ignore it
    r = r.lstrip("/")
    return "/" + r if r else None


def parse_route_list(raw, filename=""):
    """Parse a routes file (newline/CSV list, or a JSON array) into URI paths."""
    raw = (raw or "").strip()
    routes = []
    if raw.startswith("[") or raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("routes") or data.get("uris") or list(data.values())
        for item in data:
            if isinstance(item, dict):
                item = item.get("uri") or item.get("route") or item.get("path") or ""
            r = normalize_route(str(item))
            if r:
                routes.append(r)
    else:
        # newline- and comma-separated tokens
        for tok in re.split(r"[\n,]", raw):
            r = normalize_route(tok)
            if r:
                routes.append(r)
    # de-dup, keep order
    seen, out = set(), []
    for r in routes:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _json_safe(doc):
    """Strip Mongo-only types (ObjectId, datetime) so the doc is JSON-serializable."""
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out[k] = str(v)
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _json_safe(v)
        else:
            out[k] = str(v)
    return out


def _log_signature(doc):
    """A cheap structural fingerprint: method + the *shape* (keys) of params
    and request body. Used to pre-group near-identical logs so the AI input
    stays bounded even with 'no cap' fetching."""
    method = (doc.get("method") or "GET").upper()

    def keyshape(blob):
        if not blob:
            return ()
        try:
            obj = json.loads(blob) if isinstance(blob, str) else blob
        except (ValueError, TypeError):
            return ("_raw_",)
        if isinstance(obj, dict):
            return tuple(sorted(obj.keys()))
        if isinstance(obj, list):
            return ("_list_",)
        return ("_scalar_",)

    return (method, keyshape(doc.get("params")), keyshape(doc.get("request")))


def _compact_log(n, doc):
    """One-line, token-cheap summary of a log for the filtering agent."""
    def trim(blob, limit=300):
        s = "" if blob is None else str(blob)
        return s if len(s) <= limit else s[:limit] + "…"
    return (f"{n} | {(doc.get('method') or 'GET').upper()} {doc.get('uri','')} "
            f"| params={trim(doc.get('params'))} "
            f"| body={trim(doc.get('request'))}")


# ---------------------------------------------------------------- copilot auth
#
# We talk to GitHub Copilot the same way the VS Code extension does — no Copilot
# CLI required. The flow:
#   1. Read the GitHub OAuth token the Copilot extension already cached locally
#      (~/.config/github-copilot/apps.json, written when you sign in to Copilot
#      in VS Code).
#   2. Exchange it for a short-lived Copilot bearer token.
#   3. Call the Copilot chat-completions endpoint with that bearer token.

COPILOT_API_URL = "https://api.githubcopilot.com/chat/completions"
COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
# Official GitHub Copilot OAuth app — the same client id the editor plugins use.
COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
COPILOT_APPS_JSON = Path.home() / ".config" / "github-copilot" / "apps.json"
_COPILOT_EDITOR_VERSION = "vscode/1.95.0"
_COPILOT_PLUGIN_VERSION = "copilot-chat/0.23.0"

_copilot_token_cache = {"token": None, "expires_at": 0}
_copilot_token_lock = threading.Lock()


def _read_github_oauth_token():
    """Return the GitHub OAuth token for Copilot.

    Looks in (in order): the GITHUB_COPILOT_TOKEN env var, then the cache the
    editor plugins / our own `--login` flow write to
    ~/.config/github-copilot/{apps,hosts}.json.
    """
    env = os.environ.get("GITHUB_COPILOT_TOKEN") or os.environ.get("GH_COPILOT_TOKEN")
    if env:
        return env.strip()

    candidates = [
        COPILOT_APPS_JSON,
        Path.home() / ".config" / "github-copilot" / "hosts.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for key, val in data.items():
            if "github.com" in key and isinstance(val, dict) and val.get("oauth_token"):
                return val["oauth_token"]
    return None


def _save_github_oauth_token(token):
    """Persist a GitHub OAuth token in the location the editor plugins use, so
    it's picked up on the next run without re-authenticating."""
    COPILOT_APPS_JSON.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if COPILOT_APPS_JSON.exists():
        try:
            data = json.loads(COPILOT_APPS_JSON.read_text())
            if not isinstance(data, dict):
                data = {}
        except (ValueError, OSError):
            data = {}
    data[f"github.com:{COPILOT_CLIENT_ID}"] = {"user": "", "oauth_token": token}
    COPILOT_APPS_JSON.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(COPILOT_APPS_JSON, 0o600)
    except OSError:
        pass


def copilot_device_login():
    """Run the GitHub OAuth device flow against the Copilot app and persist the
    resulting token. Interactive: prints a URL + code for the user to enter in a
    browser. Returns the oauth token. Requires no CLI — just a browser sign-in to
    the GitHub account that holds the Copilot subscription."""
    r = rq.post(
        GITHUB_DEVICE_CODE_URL,
        headers={"Accept": "application/json", "User-Agent": "GitHubCopilotChat/0.23.0"},
        data={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"},
        timeout=20,
    )
    r.raise_for_status()
    dev = r.json()
    device_code = dev["device_code"]
    user_code = dev["user_code"]
    verify_url = dev.get("verification_uri", "https://github.com/login/device")
    interval = int(dev.get("interval", 5))
    expires_in = int(dev.get("expires_in", 900))

    print("\n  GitHub Copilot sign-in")
    print(f"  1. Open: {verify_url}")
    print(f"  2. Enter code: {user_code}\n")
    print("  Waiting for authorization…", flush=True)

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        pr = rq.post(
            GITHUB_OAUTH_TOKEN_URL,
            headers={"Accept": "application/json", "User-Agent": "GitHubCopilotChat/0.23.0"},
            data={
                "client_id": COPILOT_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=20,
        )
        body = pr.json()
        if body.get("access_token"):
            token = body["access_token"]
            _save_github_oauth_token(token)
            print(f"  Signed in — token saved to {COPILOT_APPS_JSON}\n")
            return token
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += int(body.get("interval", 5))
            continue
        raise RuntimeError(f"device login failed: {err or body}")
    raise RuntimeError("device login timed out — please retry")


def get_copilot_token():
    """Exchange the local GitHub OAuth token for a short-lived Copilot bearer
    token. Cached in-process until shortly before it expires."""
    with _copilot_token_lock:
        now = time.time()
        cached = _copilot_token_cache
        if cached["token"] and now < cached["expires_at"] - 60:
            return cached["token"]

        oauth = _read_github_oauth_token()
        if not oauth:
            raise RuntimeError(
                "No GitHub Copilot credentials found. Authenticate once with: "
                "`python server.py --login` (opens a browser sign-in to your "
                "Copilot-enabled GitHub account — no CLI needed)."
            )

        r = rq.get(
            COPILOT_TOKEN_URL,
            headers={
                "Authorization": f"token {oauth}",
                "Editor-Version": _COPILOT_EDITOR_VERSION,
                "Editor-Plugin-Version": _COPILOT_PLUGIN_VERSION,
                "User-Agent": "GitHubCopilotChat/0.23.0",
                "Accept": "application/json",
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Copilot token exchange failed ({r.status_code}): {r.text[:200]}. "
                "Make sure your GitHub account has an active Copilot subscription "
                "(re-run `python server.py --login` if the token expired)."
            )
        data = r.json()
        token = data.get("token")
        if not token:
            raise RuntimeError("Copilot token exchange returned no token")
        cached["token"] = token
        cached["expires_at"] = float(data.get("expires_at") or (now + 1500))
        return token


AI_FILTER_SYSTEM_PROMPT = (
    "You are a test-coverage curator for an API regression suite. You are given a "
    "numbered list of real request logs that all hit the SAME endpoint. Your job is "
    "to pick the SMALLEST subset of log numbers that still covers EVERY DISTINCT "
    "VARIATION of the request.\n"
    "\n"
    "Two logs are the SAME variation if they exercise the same code path — i.e. the "
    "same HTTP method and the same set of meaningful parameter/body fields (the "
    "VALUES may differ; that does not matter). Keep exactly ONE representative per "
    "distinct variation. If a field that selects behaviour differs (e.g. a 'type', "
    "'action', 'mode', or which id-field is present), treat those as DIFFERENT "
    "variations and keep one of each.\n"
    "\n"
    "Drop logs that look broken, empty, or malformed. Prefer keeping the log with the "
    "richest/most-complete parameters when several are equivalent.\n"
    "\n"
    "Respond with ONLY a JSON array of the log numbers to keep, e.g. [1,4,12]. "
    "No prose, no markdown, no explanation."
)


def ai_filter_logs(job, route, docs, max_to_ai=150):
    """Ask GitHub Copilot which numbered logs to keep, streaming the model's
    output live into job.progress["ai_text"].

    Uses the Copilot chat API directly (authenticated via the OAuth token the
    VS Code Copilot extension caches locally) — no Copilot CLI required.

    `docs` is the candidate list (already success-filtered). Returns the kept
    docs. On any failure (or cancellation) falls back to a structural de-dup so
    the pipeline never stalls. If the build is cancelled mid-stream the HTTP
    response is closed so no further tokens are spent.
    """
    emit = job.emit
    # Pre-group by structural signature so we never blow up the prompt. Keep up
    # to a few representatives per signature; this already preserves variations.
    groups = {}
    for d in docs:
        groups.setdefault(_log_signature(d), []).append(d)
    reps = []
    per_group = max(1, max_to_ai // max(1, len(groups)))
    for sig, items in groups.items():
        reps.extend(items[:per_group])
    reps = reps[:max_to_ai]

    if len(reps) <= 1:
        return reps

    numbered = [_compact_log(i + 1, d) for i, d in enumerate(reps)]
    prompt = (f"Endpoint: {route}\nThere are {len(reps)} candidate logs.\n\n"
              + "\n".join(numbered)
              + "\n\nReturn the JSON array of log numbers to keep.")

    job.progress["ai_route"] = route
    job.progress["ai_text"] = ""
    emit(f"[{route}] asking AI to filter {len(reps)} variation candidates…")

    def fallback():
        return [items[0] for items in groups.values()]

    resp = None
    try:
        token = get_copilot_token()
        resp = rq.post(
            COPILOT_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Copilot-Integration-Id": "vscode-chat",
                "Editor-Version": _COPILOT_EDITOR_VERSION,
                "Editor-Plugin-Version": _COPILOT_PLUGIN_VERSION,
            },
            json={
                "model": job.model,
                "messages": [
                    {"role": "system", "content": AI_FILTER_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "stream": True,
                "temperature": 0,
            },
            stream=True,
            timeout=180,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"copilot api {resp.status_code}: {resp.text[:200]}")
        job.set_proc(resp)

        result_text = ""
        deadline = time.time() + 180
        for line in resp.iter_lines(decode_unicode=True):
            if job.cancelled:
                resp.close()
                emit(f"[{route}] AI filtering cancelled — request closed", "warn")
                return fallback()
            if time.time() > deadline:
                resp.close()
                raise RuntimeError("copilot timed out after 180s")
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            choices = ev.get("choices") or []
            if not choices:
                continue
            chunk = (choices[0].get("delta") or {}).get("content")
            if chunk:
                job.progress["ai_text"] += chunk
                result_text += chunk

        if job.cancelled:
            return fallback()

        text = result_text or job.progress["ai_text"]
        m = re.search(r"\[[\d,\s]*\]", text)
        if not m:
            raise RuntimeError(f"no number array in model output: {text[:200]}")
        keep_nums = json.loads(m.group(0))
        kept = [reps[n - 1] for n in keep_nums if 1 <= n <= len(reps)]
        if not kept:
            raise RuntimeError("model kept nothing")
        emit(f"[{route}] AI kept {len(kept)}/{len(reps)} variations "
             f"(from {len(docs)} success logs)", "ok")
        return kept
    except Exception as e:
        if job.cancelled:
            return fallback()
        emit(f"[{route}] AI filter failed ({e}); falling back to structural de-dup", "warn")
        return fallback()
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        job.set_proc(None)
        job.progress["ai_route"] = None



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
        # Merge the per-job header overrides on top of whatever the log had.
        # Overrides win — case-insensitive — so users can force a fresh
        # X-Auth-Token / X-Api-Key / etc. regardless of stale values in logs.
        overrides = self.cfg.get("_header_overrides_parsed") or {}
        if overrides:
            headers = {k: v for k, v in (spec["headers"] or {}).items()
                       if k.lower() not in {o.lower() for o in overrides}}
            headers.update(overrides)
        else:
            headers = spec["headers"]
        started = time.time()
        try:
            kwargs = dict(headers=headers, params=spec["params"] or None,
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
        overrides = cfg.get("_header_overrides_parsed") or {}
        if overrides:
            job.emit(f"Header overrides forced on every request: {', '.join(overrides.keys())}")

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

        # ---- 2. replay requests with a bounded concurrency pool.
        # Each task hits BOTH branches in parallel; up to `concurrency` tasks
        # run at once, so a slow endpoint (e.g. /address_sync) no longer blocks
        # the rest of the queue. Rows are flushed into job.results strictly
        # in-order so row numbers, the streaming table, and resume stay correct.
        job.progress["phase"] = "requests"
        specs = cfg["requests"]
        start_at = max(job.start_index, len(job.results))
        if start_at:
            job.emit(f"Resuming — skipping first {start_at} requests already completed")

        try:
            concurrency = int(cfg.get("concurrency") or 6)
        except (TypeError, ValueError):
            concurrency = 6
        concurrency = max(1, min(concurrency, 32))
        job.emit(f"Replaying with concurrency={concurrency} "
                 f"(each request runs on both branches in parallel)")

        def process(i):
            """Replay spec[i] on both branches. Honors pause/cancel."""
            # Pause: park here until resumed (or cancelled).
            while job.paused and not job.cancelled:
                job.pause_event.wait(timeout=0.5)
            if job.cancelled:
                return i, None
            spec = specs[i]
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
            return i, _row_for(spec, ra, rb)

        # Buffer out-of-order completions, flush the contiguous prefix in order.
        buffer = {}
        next_flush = start_at
        completed = 0

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(process, i): i for i in range(start_at, len(specs))}
            for fut in as_completed(futures):
                i, row = fut.result()
                completed += 1
                if row is not None:
                    buffer[i] = row
                # Flush every contiguous row we now have.
                while next_flush in buffer:
                    job.results.append(buffer.pop(next_flush))
                    next_flush += 1
                # Progress bar tracks total completions (incl. in-flight order).
                job.progress["done"] = start_at + completed

        # Flush any remaining contiguous rows (e.g. all finished at once).
        while next_flush in buffer:
            job.results.append(buffer.pop(next_flush))
            next_flush += 1

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


# ---------------------------------------------------------------- build job (mongo + AI)

BUILDS = {}
BUILDS_LOCK = threading.Lock()


class BuildJob:
    """Fetches logs from Mongo for a set of routes, AI-filters them down to the
    distinct request variations, and emits a request-log JSON ready to replay."""

    def __init__(self, mongo_url, routes, model, db_name=None):
        self.id = uuid.uuid4().hex[:12]
        self.mongo_url = mongo_url
        self.routes = routes
        self.model = model or "gpt-4o"
        self.db_name = db_name
        self.status = "queued"
        self.error = None
        self.log = []
        self.docs = []          # final kept logs (JSON-safe), ready to replay
        self.per_route = []     # [{route, found, success, kept}]
        self.progress = {
            "phase": "queued", "route": None, "collection": None,
            "done": 0, "total": len(routes),
            "fetched": 0,       # cumulative docs pulled from Mongo (live)
            "ai_route": None,   # route the AI is currently filtering
            "ai_text": "",      # live streamed model output for that route
        }
        # Cancellation: cancel_event is set by /stop; the active Copilot HTTP
        # stream (if any) is tracked so we can close it and stop burning tokens.
        self.cancel_event = threading.Event()
        self._proc = None
        self._proc_lock = threading.Lock()

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def emit(self, msg, tag="info"):
        self.log.append({"t": time.time(), "msg": msg, "tag": tag})

    def set_proc(self, proc):
        with self._proc_lock:
            self._proc = proc

    def kill_proc(self):
        """Stop any in-flight AI work (close the Copilot HTTP stream, or
        terminate a legacy subprocess) so no further tokens are spent."""
        with self._proc_lock:
            p = self._proc
        if p is None:
            return
        try:
            # subprocess.Popen-style handle
            if hasattr(p, "poll") and hasattr(p, "terminate"):
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()
            # requests.Response stream handle
            elif hasattr(p, "close"):
                p.close()
        except Exception:
            pass

    def cancel(self):
        self.cancel_event.set()
        self.kill_proc()

    def to_dict(self):
        return {
            "id": self.id, "status": self.status, "error": self.error,
            "cancelled": self.cancelled,
            "progress": self.progress, "per_route": self.per_route,
            "doc_count": len(self.docs), "log": self.log,
        }


def _iter_cursor(job, cursor):
    """Yield docs from a Mongo cursor, bumping the live fetched counter and
    bailing out fast if the build was cancelled."""
    for doc in cursor:
        if job.cancelled:
            break
        job.progress["fetched"] += 1
        yield doc


def run_build_job(job):
    try:
        from pymongo import MongoClient
    except ImportError:
        job.status = "error"
        job.error = "pymongo is not installed (pip install pymongo dnspython)"
        job.emit(job.error, "error")
        return

    client = None
    try:
        job.status = "running"
        job.progress["phase"] = "connect"
        job.emit("Connecting to MongoDB…")
        client = MongoClient(job.mongo_url, serverSelectionTimeoutMS=8000)
        # Pick the DB: explicit name, else the one in the URI, else first non-admin db.
        db = None
        if job.db_name:
            db = client[job.db_name]
        else:
            try:
                db = client.get_default_database()
            except Exception:
                db = None
            if db is None:
                names = [n for n in client.list_database_names()
                         if n not in ("admin", "local", "config")]
                if not names:
                    raise RuntimeError("no usable database found in this Mongo URL")
                db = client[names[0]]
        collections = db.list_collection_names()
        job.emit(f"Database '{db.name}' · {len(collections)} collection(s): "
                 f"{', '.join(collections) or '(none)'}", "ok")

        job.progress["phase"] = "fetch"
        for idx, route in enumerate(job.routes):
            if job.cancelled:
                break
            job.progress["route"] = route
            job.progress["done"] = idx
            # Gather candidate docs across every collection: exact uri match first,
            # then a substring fallback if exact found nothing. Stream each
            # cursor so the live "fetched" counter ticks up in real time.
            found = []
            for coll in collections:
                if job.cancelled:
                    break
                job.progress["collection"] = coll
                before = len(found)
                for doc in _iter_cursor(job, db[coll].find({"uri": route})):
                    found.append(doc)
                got = len(found) - before
                if got:
                    job.emit(f"[{route}] {coll}: +{got} log(s) (total {len(found)})")
            if not found and not job.cancelled:
                job.emit(f"[{route}] no exact match — trying substring scan", "warn")
                rx = {"uri": {"$regex": re.escape(route.lstrip("/")), "$options": "i"}}
                for coll in collections:
                    if job.cancelled:
                        break
                    job.progress["collection"] = coll
                    for doc in _iter_cursor(job, db[coll].find(rx)):
                        found.append(doc)
            job.progress["collection"] = None

            if job.cancelled:
                break

            success = [d for d in found if _doc_is_success(d)]
            job.emit(f"[{route}] {len(found)} logs found · {len(success)} successful", "ok")

            if not success:
                job.per_route.append({"route": route, "found": len(found),
                                      "success": 0, "kept": 0})
                continue

            job.progress["phase"] = "filter"
            kept = ai_filter_logs(job, route, success)
            if job.cancelled:
                break
            for d in kept:
                safe = _json_safe(d)
                safe["_route"] = route
                job.docs.append(safe)
            job.per_route.append({"route": route, "found": len(found),
                                  "success": len(success), "kept": len(kept)})
            job.progress["phase"] = "fetch"

        if job.cancelled:
            job.status = "stopped"
            job.progress["phase"] = "stopped"
            job.emit(f"Build stopped by user — {len(job.docs)} request(s) kept so far", "warn")
        else:
            job.progress["done"] = len(job.routes)
            job.progress["phase"] = "done"
            job.status = "done"
            job.emit(f"Built {len(job.docs)} request(s) across {len(job.routes)} route(s) 🎉", "ok")
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.emit(f"Build failed: {e}\n{traceback.format_exc()}", "error")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


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


@app.get("/api/copilot-models")
def copilot_models():
    """List the chat models available to this Copilot subscription, so the UI
    dropdown can offer every model the CLI/editor would. Falls back to a small
    static list if Copilot isn't reachable (e.g. not signed in yet)."""
    fallback = [
        {"id": "gpt-4o", "name": "GPT-4o"},
        {"id": "gpt-4.1", "name": "GPT-4.1"},
        {"id": "o4-mini", "name": "o4-mini"},
    ]
    try:
        token = get_copilot_token()
        r = rq.get(
            COPILOT_MODELS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Copilot-Integration-Id": "vscode-chat",
                "Editor-Version": _COPILOT_EDITOR_VERSION,
                "Editor-Plugin-Version": _COPILOT_PLUGIN_VERSION,
            },
            timeout=20,
        )
        if r.status_code != 200:
            return jsonify({"models": fallback, "error": f"copilot api {r.status_code}"})
        data = r.json().get("data") or []
        seen, models = set(), []
        for m in data:
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            caps = m.get("capabilities") or {}
            # Only chat-capable models make sense for the filtering step.
            if caps.get("type") and caps.get("type") != "chat":
                continue
            # Respect Copilot's per-model enablement policy when present.
            policy = (m.get("policy") or {}).get("state")
            if policy and policy != "enabled":
                continue
            seen.add(mid)
            models.append({
                "id": mid,
                "name": m.get("name") or mid,
                "vendor": m.get("vendor") or "",
            })
        if not models:
            models = fallback
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"models": fallback, "error": str(e)})


@app.post("/api/mongo-build")
def mongo_build():
    """Kick off a background build: fetch logs from Mongo for the given routes,
    AI-filter to distinct variations, then expose them as a replayable log set."""
    body = request.json or {}
    mongo_url = (body.get("mongo_url") or "").strip()
    raw_routes = body.get("routes") or ""
    model = (body.get("model") or "gpt-4o").strip()
    db_name = (body.get("db_name") or "").strip() or None
    if not mongo_url:
        return jsonify({"error": "missing 'mongo_url'"}), 400
    try:
        routes = parse_route_list(raw_routes)
    except Exception as e:
        return jsonify({"error": f"could not parse routes: {e}"}), 400
    if not routes:
        return jsonify({"error": "no routes found in input"}), 400
    job = BuildJob(mongo_url, routes, model, db_name)
    with BUILDS_LOCK:
        BUILDS[job.id] = job
    threading.Thread(target=run_build_job, args=(job,), daemon=True).start()
    return jsonify({"build_id": job.id, "routes": routes})


@app.get("/api/build/<build_id>")
def build_status(build_id):
    job = BUILDS.get(build_id)
    if not job:
        return jsonify({"error": "unknown build"}), 404
    return jsonify(job.to_dict())


@app.post("/api/build/<build_id>/stop")
def build_stop(build_id):
    """Cancel an in-flight build. Closes any running Copilot request so no
    further AI tokens are spent."""
    job = BUILDS.get(build_id)
    if not job:
        return jsonify({"error": "unknown build"}), 404
    if job.status not in ("running", "queued"):
        return jsonify({"error": f"build is not running (status '{job.status}')"}), 400
    job.cancel()
    job.emit("Stop requested — cancelling…", "warn")
    return jsonify({"ok": True, "cancelled": True})


@app.get("/api/build/<build_id>/result")
def build_result(build_id):
    """The kept logs as a request-log JSON array, ready to feed into /api/parse.
    Works for both completed and user-stopped builds (returns whatever was kept)."""
    job = BUILDS.get(build_id)
    if not job:
        return jsonify({"error": "unknown build"}), 404
    if job.status not in ("done", "stopped"):
        return jsonify({"error": f"build not finished (status '{job.status}')"}), 400
    return jsonify({"requests": job.docs, "count": len(job.docs)})


@app.post("/api/run")
def start_run():
    cfg = request.json
    for key in ("repo", "branch_a", "branch_b", "requests"):
        if not cfg.get(key):
            return jsonify({"error": f"missing '{key}'"}), 400
    cfg["repo"] = os.path.expanduser(cfg["repo"])
    if not is_git_repo(cfg["repo"]):
        return jsonify({"error": "repo is not a git repository"}), 400
    # Pre-parse header overrides once so replay() doesn't have to re-parse per request.
    cfg["_header_overrides_parsed"] = parse_header_blob(cfg.get("header_overrides") or "")
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
    job.emit("Paused — in-flight requests finish, no new ones start", "warn")
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
    job.emit("Resumed", "ok")
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
    import sys
    if "--login" in sys.argv:
        copilot_device_login()
        sys.exit(0)
    port = int(os.environ.get("PORT", 5599))
    print(f"\n  Branch Compare running → http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
