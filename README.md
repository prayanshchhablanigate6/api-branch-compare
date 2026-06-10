# Branch Compare

Replay logged API requests against **two git branches** of an API repo and get a
side-by-side diff report (browser UI + downloadable `report.md`).

## How it works

1. **Pick the target repo** (defaults to `poolbrain-mobileapi`) — the tool validates
   it's a git repo, finds its `src/.venv` (Python 3.9.18) and entrypoint.
2. **Upload request logs** — JSON files in the `GetRequestLogs.json` /
   `PostRequestLogs.json` format (`uri`, `method`, `headers`, `params`, `request`).
3. **Choose two branches** from searchable dropdowns (local + remote).
4. **Run** — for each branch sequentially the tool:
   - creates an isolated `git worktree` (your working copy is never touched)
   - copies `envs_test/api/config.py` → `src/config.py`
   - starts the server with the repo's own venv python (`python api.py` from `src/`)
   - waits for readiness, replays every request, captures status/body/time
   - stops the server and removes the worktree
5. **Report** — interactive table (route, request, response per branch, match) with
   expandable full responses, plus a downloadable Markdown report.

## Run the tool

```bash
./run.sh            # → http://127.0.0.1:5599
```

or manually:

```bash
.venv/bin/python server.py
```

## Notes

- Hop-by-hop headers (`Host`, `Content-Length`, `X-Forwarded-*`, cookies, …) are
  stripped before replay; auth headers (`X-Auth-Token`, `X-Api-Key`, …) are kept.
- The target server's base URL is auto-detected from its startup output
  (`Running on https://…`); you can override it in Advanced settings, along with
  the server command and timeouts.
- Responses are compared as parsed JSON (key order/whitespace insensitive).
- Run artifacts (server logs, `report.md`, `results.json`) land in `runs/<job-id>/`.
