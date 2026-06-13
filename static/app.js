/* Branch Compare — frontend */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = {
  repo: null,            // validated repo info from /api/repo-info
  files: [],             // [{name, content}]
  parsed: { requests: [], files: [] },
  branchA: "", branchB: "",
  jobId: null, poller: null,
};

/* ---------------- generic combo dropdown ---------------- */
function attachCombo(input, list, getItems, onPick) {
  let items = [], active = -1;

  const render = () => {
    list.innerHTML = items.map((it, i) =>
      it.group
        ? `<div class="combo-group">${esc(it.label)}</div>`
        : `<div class="combo-item ${i === active ? "active" : ""}" data-i="${i}">
             ${it.badge ? `<span class="badge ${it.badgeClass || ""}">${esc(it.badge)}</span>` : ""}
             <span>${esc(it.label)}</span></div>`
    ).join("");
    list.classList.toggle("open", items.length > 0);
  };

  const refresh = async () => {
    items = await getItems(input.value);
    active = -1;
    render();
  };

  input.addEventListener("input", refresh);
  input.addEventListener("focus", refresh);
  input.addEventListener("keydown", (e) => {
    const sel = items.map((it, i) => (it.group ? null : i)).filter((i) => i !== null);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!sel.length) return;
      const pos = sel.indexOf(active);
      active = sel[(pos + (e.key === "ArrowDown" ? 1 : -1) + sel.length) % sel.length];
      render();
      list.querySelector(".combo-item.active")?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      pick(active);
    } else if (e.key === "Escape") {
      list.classList.remove("open");
    }
  });
  list.addEventListener("mousedown", (e) => {
    const el = e.target.closest(".combo-item");
    if (el) { e.preventDefault(); pick(+el.dataset.i); }
  });
  document.addEventListener("click", (e) => {
    if (!input.parentElement.contains(e.target)) list.classList.remove("open");
  });

  function pick(i) {
    list.classList.remove("open");
    onPick(items[i]);
  }
}

/* ---------------- step 1: repository ---------------- */
attachCombo($("repoInput"), $("repoSuggest"),
  async (q) => {
    try {
      const r = await fetch(`/api/suggest-path?q=${encodeURIComponent(q)}`);
      return (await r.json()).map((d) => ({
        label: d.path, value: d.path,
        badge: d.is_git ? "git" : "dir", badgeClass: d.is_git ? "git" : "",
      }));
    } catch { return []; }
  },
  (item) => { $("repoInput").value = item.value; loadRepo(); }
);
$("repoInput").addEventListener("change", loadRepo);
$("repoInput").addEventListener("blur", () => setTimeout(loadRepo, 150));

let lastRepoChecked = null;
async function loadRepo() {
  const path = $("repoInput").value.trim();
  if (!path || path === lastRepoChecked) return;
  lastRepoChecked = path;
  const meta = $("repoMeta");
  meta.classList.remove("hidden");
  meta.innerHTML = `<div class="meta-item"><div class="k">checking</div><div class="v">…</div></div>`;
  try {
    const r = await fetch(`/api/repo-info?repo=${encodeURIComponent(path)}`);
    const info = await r.json();
    if (!info.valid) {
      state.repo = null;
      meta.innerHTML = `<div class="meta-item"><div class="k">repository</div><div class="v bad">✕ ${esc(info.error)}</div></div>`;
      setBranchInputs(false);
      updateRunBar();
      return;
    }
    state.repo = info;
    meta.innerHTML = `
      <div class="meta-item"><div class="k">git repo</div><div class="v ok">✓ valid · on ${esc(info.current_branch)}</div></div>
      <div class="meta-item"><div class="k">branches</div><div class="v">${info.local_branches.length} local · ${info.remote_branches.length} remote</div></div>
      <div class="meta-item"><div class="k">virtualenv</div>
        <div class="v ${info.venv_python ? "ok" : "bad"}">${info.venv_python ? "✓ Python " + esc(info.python_version) : "✕ not found"}</div></div>
      <div class="meta-item"><div class="k">entrypoint</div>
        <div class="v ${info.entry ? "ok" : ""}">${info.entry ? "src/" + esc(info.entry) : "auto-detect per branch"}</div></div>
      <div class="meta-item"><div class="k">env config</div>
        <div class="v ${info.has_env_config ? "ok" : "bad"}">${info.has_env_config ? "✓ envs_test/api/config.py" : "✕ envs_test/api/config.py missing"}</div></div>`;
    // Pre-fill the venv-python override with the auto-detected path so the
    // user can simply edit it if their deps live in a different venv.
    if (info.venv_python && !$("venvPython").value) $("venvPython").value = info.venv_python;
    setBranchInputs(true);
    // sensible defaults: dev vs current branch
    if (!state.branchA && info.local_branches.includes("dev")) setBranch("A", "dev");
    if (!state.branchB && info.current_branch && info.current_branch !== state.branchA) setBranch("B", info.current_branch);
  } catch (e) {
    state.repo = null;
    meta.innerHTML = `<div class="meta-item"><div class="k">error</div><div class="v bad">${esc(e.message)}</div></div>`;
  }
  updateRunBar();
}

/* ---------------- step 2: files ---------------- */
const dz = $("dropzone");
const fileInput = $("fileInput");
// Only the dropzone itself should open the picker — ignore clicks bubbling
// from the hidden <input> (programmatic .click() bubbles too, which caused a
// re-entrant open/cancel cycle that made the picker appear to do nothing).
dz.addEventListener("click", (e) => {
  if (e.target === fileInput) return;
  fileInput.click();
});
fileInput.addEventListener("change", (e) => {
  addFiles([...e.target.files]).catch(showFileError);
  e.target.value = ""; // allow re-selecting the same file later
});
["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => addFiles([...e.dataTransfer.files]).catch(showFileError));

function showFileError(err) {
  console.error("[upload]", err);
  $("fileList").innerHTML =
    `<div class="file-pill"><span>⚠️</span><span class="err">upload failed: ${esc(err.message || err)}</span></div>`;
}

async function addFiles(files) {
  if (!files.length) return;
  for (const f of files) {
    if (state.files.some((x) => x.name === f.name)) continue;
    const content = await f.text();
    state.files.push({ name: f.name, content });
  }
  await reparse();
}

async function reparse() {
  if (!state.files.length) {
    state.parsed = { requests: [], files: [] };
    renderFiles();
    updateRunBar();
    return;
  }
  const r = await fetch("/api/parse", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files: state.files }),
  });
  if (!r.ok) throw new Error(`/api/parse returned HTTP ${r.status}`);
  state.parsed = await r.json();
  renderFiles();
  updateRunBar();
}

function renderFiles() {
  $("fileList").innerHTML = state.parsed.files.map((f) => `
    <div class="file-pill">
      <span>📄</span><span class="name">${esc(f.name)}</span>
      ${f.error
        ? `<span class="err">parse error: ${esc(f.error)}</span>`
        : `<span class="counts">${f.count} requests · ${Object.entries(f.methods || {}).map(([m, n]) => `${n} ${m}`).join(" · ")}</span>`}
      <button class="rm" data-name="${esc(f.name)}" title="remove">✕</button>
    </div>`).join("");
  $("fileList").querySelectorAll(".rm").forEach((btn) =>
    btn.addEventListener("click", () => {
      state.files = state.files.filter((x) => x.name !== btn.dataset.name);
      reparse();
    }));
}

/* ---- tab switcher (upload vs paste) ---- */
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-pane").forEach((p) =>
      p.classList.toggle("hidden", p.id !== btn.dataset.pane));
  });
});

/* ---- paste-JSON handler: adds the text as a synthetic "pasted-N.json" ---- */
$("pasteAddBtn").addEventListener("click", async () => {
  const txt = $("pasteInput").value.trim();
  const hint = $("pasteHint");
  if (!txt) { hint.textContent = "Paste some JSON first."; return; }
  // Validate locally so we can show a friendly error before round-tripping.
  try { JSON.parse(txt); }
  catch (e) { hint.innerHTML = `<span class="err">invalid JSON: ${esc(e.message)}</span>`; return; }
  // Pick a unique synthetic filename so multiple pastes coexist with uploads.
  let n = 1;
  while (state.files.some((f) => f.name === `pasted-${n}.json`)) n++;
  const name = `pasted-${n}.json`;
  state.files.push({ name, content: txt });
  $("pasteInput").value = "";
  hint.innerHTML = `Added as <code>${esc(name)}</code>.`;
  try { await reparse(); } catch (e) { showFileError(e); }
});

/* ---- MongoDB build: fetch logs per route, AI-filter, inject as a file ---- */
let mongoPoller = null;
let mongoBuildId = null;

function startMongoBuild() {
  const url = $("mongoUrl").value.trim();
  const routes = $("routesInput").value.trim();
  const model = $("mongoModel").value;
  const box = $("mongoProgress");
  if (!url) { $("mongoHint").innerHTML = `<span class="err">Enter a MongoDB URL first.</span>`; return; }
  if (!routes) { $("mongoHint").innerHTML = `<span class="err">Enter at least one route.</span>`; return; }

  setMongoRunning(true);
  box.classList.remove("hidden");
  box.innerHTML = `<div class="mongo-status">Starting build…</div>`;
  if (mongoPoller) clearInterval(mongoPoller);

  fetch("/api/mongo-build", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mongo_url: url, routes, model }),
  }).then(async (r) => {
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    mongoBuildId = j.build_id;
    mongoPoller = setInterval(() => pollMongo(mongoBuildId), 700);
  }).catch((e) => {
    box.innerHTML = `<div class="mongo-status err">Build failed to start: ${esc(e.message)}</div>`;
    setMongoRunning(false);
  });
}

async function pollMongo(buildId) {
  try {
    const r = await fetch(`/api/build/${buildId}`);
    const b = await r.json();
    renderMongoProgress(b);
    if (b.status === "done" || b.status === "error" || b.status === "stopped") {
      clearInterval(mongoPoller); mongoPoller = null;
      setMongoRunning(false);
      if (b.status === "done" || b.status === "stopped") await finishMongoBuild(buildId, b);
    }
  } catch (e) {
    clearInterval(mongoPoller); mongoPoller = null;
    setMongoRunning(false);
    $("mongoProgress").innerHTML = `<div class="mongo-status err">Lost connection: ${esc(e.message)}</div>`;
  }
}

function setMongoRunning(running) {
  $("mongoBuildBtn").classList.toggle("hidden", running);
  $("mongoStopBtn").classList.toggle("hidden", !running);
  // Restart only shows after a build has finished/stopped (i.e. not running and we have an id).
  $("mongoRestartBtn").classList.toggle("hidden", running || !mongoBuildId);
}

$("mongoBuildBtn").addEventListener("click", startMongoBuild);
$("mongoRestartBtn").addEventListener("click", startMongoBuild);
$("mongoStopBtn").addEventListener("click", async () => {
  if (!mongoBuildId) return;
  $("mongoStopBtn").disabled = true;
  try {
    await fetch(`/api/build/${mongoBuildId}/stop`, { method: "POST" });
  } catch (e) { /* poller will surface the resulting state */ }
  $("mongoStopBtn").disabled = false;
});

function renderMongoProgress(b) {
  const box = $("mongoProgress");
  const p = b.progress || {};
  const phase = p.phase || b.status;
  const stateClass = b.status === "error" ? "err" : (b.status === "stopped" ? "warn" : "");
  let head;
  if (b.status === "error") head = "✕ " + esc(b.error || "error");
  else if (b.status === "stopped") head = `■ stopped · ${b.doc_count || 0} request(s) kept`;
  else {
    const where = p.collection ? ` · scanning <code>${esc(p.collection)}</code>` : "";
    head = `${esc(phase)} · route ${p.done || 0}/${p.total || 0}`
         + ` · <b>${p.fetched || 0}</b> logs fetched`
         + ` · ${b.doc_count || 0} kept${where}`;
  }

  const rows = (b.per_route || []).map((r) =>
    `<tr><td>${esc(r.route)}</td><td>${r.found}</td><td>${r.success}</td>
       <td class="${r.kept ? "ok" : ""}">${r.kept}</td></tr>`).join("");

  // Live AI stream for the route currently being filtered.
  const aiBlock = (p.ai_route && p.ai_text)
    ? `<div class="mongo-ai"><div class="mongo-ai-head">🤖 AI filtering <code>${esc(p.ai_route)}</code> — keeping:</div>
         <pre class="mongo-ai-out">${esc(p.ai_text)}</pre></div>`
    : "";

  const lastLog = (b.log || []).slice(-8).map((l) =>
    `<div class="ml ${esc(l.tag || "")}">${esc(l.msg)}</div>`).join("");

  box.innerHTML = `
    <div class="mongo-status ${stateClass}">${head}</div>
    ${rows ? `<table class="mongo-table"><thead><tr><th>route</th><th>found</th><th>ok</th><th>kept</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
    ${aiBlock}
    <div class="mongo-logs">${lastLog}</div>`;
}

async function finishMongoBuild(buildId, b) {
  const r = await fetch(`/api/build/${buildId}/result`);
  const j = await r.json();
  if (!r.ok) { $("mongoProgress").innerHTML += `<div class="mongo-status err">${esc(j.error)}</div>`; return; }
  if (!j.count) { $("mongoProgress").innerHTML += `<div class="mongo-status err">No matching logs were kept — check the routes / URL.</div>`; return; }
  // Inject the kept logs as a synthetic file so the normal parse/run flow takes over.
  const name = `mongo-build-${buildId}.json`;
  state.files = state.files.filter((f) => !f.name.startsWith("mongo-build-"));
  state.files.push({ name, content: JSON.stringify(j.requests) });
  await reparse();
  const note = b.status === "stopped" ? " (partial — build was stopped)" : "";
  $("mongoProgress").innerHTML += `<div class="mongo-status ok">✓ Added ${j.count} request(s)${note} as <code>${esc(name)}</code> — proceed to pick branches &amp; run.</div>`;
}

/* ---------------- step 3: branches ---------------- */
function branchItems(query) {
  if (!state.repo) return [];
  const q = (query || "").toLowerCase();
  const f = (arr) => arr.filter((b) => b.toLowerCase().includes(q));
  const local = f(state.repo.local_branches), remote = f(state.repo.remote_branches).slice(0, 40);
  const items = [];
  if (local.length) items.push({ group: true, label: "Local branches" },
    ...local.map((b) => ({ label: b + (b === state.repo.current_branch ? "  (current)" : ""), value: b })));
  if (remote.length) items.push({ group: true, label: "Remote branches" },
    ...remote.map((b) => ({ label: b, value: b })));
  return items;
}
attachCombo($("branchA"), $("branchAList"), async (q) => branchItems(q), (it) => setBranch("A", it.value));
attachCombo($("branchB"), $("branchBList"), async (q) => branchItems(q), (it) => setBranch("B", it.value));
["branchA", "branchB"].forEach((id) =>
  $(id).addEventListener("input", () => { state["branch" + id.slice(-1)] = $(id).value.trim(); updateRunBar(); }));

function setBranch(which, value) {
  state["branch" + which] = value;
  $("branch" + which).value = value;
  updateRunBar();
}
function setBranchInputs(enabled) {
  $("branchA").disabled = $("branchB").disabled = !enabled;
}
$("swapBtn").addEventListener("click", () => {
  const a = state.branchA;
  setBranch("A", state.branchB);
  setBranch("B", a);
});

/* ---------------- step 4: advanced ---------------- */
$("advToggle").addEventListener("click", () => $("step-advanced").classList.toggle("collapsed"));

/* ---------------- run bar ---------------- */
function updateRunBar() {
  const n = state.parsed.requests.length;
  const ready = state.repo && n > 0 && state.branchA && state.branchB && state.branchA !== state.branchB;
  $("runBtn").disabled = !ready;
  const bits = [];
  bits.push(state.repo ? `repo <b>${esc(state.repo.repo.split("/").pop())}</b>` : "pick a repo");
  bits.push(n ? `<b>${n}</b> requests` : "upload logs");
  bits.push(state.branchA && state.branchB
    ? `<b>${esc(state.branchA)}</b> vs <b>${esc(state.branchB)}</b>${state.branchA === state.branchB ? " (must differ!)" : ""}`
    : "choose two branches");
  $("runSummary").innerHTML = bits.join(" &nbsp;·&nbsp; ");
}

/* ---------------- run + poll ---------------- */
// Persist Advanced text fields across reloads so users don't lose their token
// every time they refresh the page.
const PERSIST_FIELDS = ["venvPython", "serverCmd", "baseUrl", "headerOverrides", "requestTimeout", "concurrency", "mongoUrl", "routesInput", "mongoModel"];
for (const id of PERSIST_FIELDS) {
  const el = $(id);
  if (!el) continue;
  const saved = localStorage.getItem("bc:" + id);
  if (saved && !el.value) el.value = saved;
  el.addEventListener("input", () => localStorage.setItem("bc:" + id, el.value));
}

$("runBtn").addEventListener("click", async () => {
  const overrides = $("headerOverrides").value.trim();
  if (!overrides) {
    const proceed = confirm(
      "⚠️  Header overrides is EMPTY.\n\n" +
      "Most replayed requests will likely 401 because the auth token in your " +
      "log file is expired. Add X-Auth-Token / X-Api-Key / requestcompanyid " +
      "in Advanced › Header overrides.\n\n" +
      "Continue anyway?"
    );
    if (!proceed) return;
  }
  $("runBtn").disabled = true;
  const cfg = {
    repo: state.repo.repo,
    branch_a: state.branchA, branch_b: state.branchB,
    requests: state.parsed.requests,
    venv_python: $("venvPython").value.trim(),
    server_cmd: $("serverCmd").value.trim(),
    base_url: $("baseUrl").value.trim(),
    startup_timeout: +$("startupTimeout").value || 180,
    request_timeout: +$("requestTimeout").value || 120,
    concurrency: +$("concurrency").value || 6,
    header_overrides: $("headerOverrides").value,
  };
  const r = await fetch("/api/run", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cfg),
  });
  const data = await r.json();
  if (data.error) { alert(data.error); updateRunBar(); return; }
  state.jobId = data.job_id;
  reportRows = [];
  $("reportBody").innerHTML = "";
  $("progressCard").classList.remove("hidden");
  // Show the report card immediately so rows can stream in as each request
  // finishes on both branches. The download button + summary chips fill in
  // once the job actually completes.
  $("reportCard").classList.remove("hidden");
  $("dlBtn").classList.add("hidden");
  $("thA").textContent = `Response · ${state.branchA}`;
  $("thB").textContent = `Response · ${state.branchB}`;
  $("reportMeta").textContent = `${state.branchA} vs ${state.branchB} · streaming…`;
  $("summaryChips").innerHTML = "";
  $("progressCard").scrollIntoView({ behavior: "smooth" });
  state.poller = setInterval(poll, 900);
  poll();
});

async function poll() {
  const r = await fetch(`/api/job/${state.jobId}`);
  const job = await r.json();
  renderProgress(job);
  updateControls(job);
  // Stream rows into the report table as soon as each request completes on
  // both branches. The backend appends to job.results in order, so we only
  // need to draw the new tail.
  if (Array.isArray(job.results) && job.results.length > reportRows.length) {
    reportRows = job.results;
    drawRows();
    updateLiveSummary();
  }
  // The download button is wired up the moment we have at least one row,
  // so users can export the partial report mid-run.
  if (reportRows.length > 0) {
    const dl = $("dlBtn");
    dl.href = `/api/report/${state.jobId}.md`;
    dl.classList.remove("hidden");
    dl.textContent = job.status === "done"
      ? "⬇  Download report.md"
      : `⬇  Export report.md (${reportRows.length} so far)`;
  }
  if (job.status === "done" || job.status === "error" || job.status === "stopped") {
    clearInterval(state.poller);
    state.poller = null;
    if (job.status === "done" || job.status === "stopped") renderReport(job);
  }
}

/* ---------------- run controls (pause / resume / stop / restart) ---------------- */
function updateControls(job) {
  const running = job.status === "running";
  const stopped = job.status === "stopped" || job.status === "error";
  $("pauseBtn").hidden = !running || job.paused;
  $("resumeBtn").hidden = !running || !job.paused;
  $("stopBtn").hidden = !running;
  $("restartBtn").hidden = !(stopped || job.status === "done");
  $("resumeJobBtn").hidden = !job.can_resume;
}

async function jobAction(path, opts = {}) {
  if (!state.jobId) return null;
  const r = await fetch(`/api/job/${state.jobId}${path}`, { method: "POST", ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) alert(data.error || `Request failed: ${r.status}`);
  return data;
}

function startPolling() {
  if (state.poller) return;
  state.poller = setInterval(poll, 900);
  poll();
}

$("pauseBtn").addEventListener("click", () => jobAction("/pause").then(poll));
$("resumeBtn").addEventListener("click", () => jobAction("/resume").then(poll));
$("stopBtn").addEventListener("click", () => {
  if (!confirm("Stop the run? Servers will be torn down. A partial report will be saved.")) return;
  jobAction("/stop").then(poll);
});

async function startFollowUpJob(path) {
  const data = await jobAction(path);
  if (!data || !data.job_id) return;
  state.jobId = data.job_id;
  reportRows = [];
  $("reportBody").innerHTML = "";
  $("reportCard").classList.remove("hidden");
  $("dlBtn").classList.add("hidden");
  $("reportMeta").textContent = `${state.branchA} vs ${state.branchB} · streaming…`;
  $("summaryChips").innerHTML = "";
  $("progressCard").scrollIntoView({ behavior: "smooth" });
  startPolling();
}

$("restartBtn").addEventListener("click", () => startFollowUpJob("/restart"));
$("resumeJobBtn").addEventListener("click", () => startFollowUpJob("/restart?resume=1"));

function updateLiveSummary() {
  const matches = reportRows.filter((r) => r.match).length;
  $("summaryChips").innerHTML = `
    <span class="chip">Done <b>${reportRows.length}</b></span>
    <span class="chip c-ok">✅ Matching <b>${matches}</b></span>
    <span class="chip c-bad">❌ Differing <b>${reportRows.length - matches}</b></span>`;
}

function renderProgress(job) {
  const total = job.progress.total || 1;
  const branches = Object.keys(job.branches);
  const phaseLabel = { pending: "waiting…", setup: "creating worktree + config", starting: "starting server",
                       running: "replaying requests", done: "✓ complete", failed: "✗ failed" };
  $("pipelines").innerHTML = branches.map((b) => {
    const st = job.branches[b];
    const cls = st.status === "failed" ? "fail" : st.status === "done" ? "ok"
              : st.status === "pending" ? "" : "active";
    let detail = phaseLabel[st.status] || st.status;
    // Requests now run on both branches in parallel, so the per-branch detail
    // line shows the shared progress counter once we're in the requests phase.
    if (st.status === "running" && job.progress.phase === "requests") {
      detail = `replaying requests · ${job.progress.done}/${total}`;
    }
    if (st.base_url) detail += ` · ${st.base_url}`;
    if (st.error) detail = `✗ ${st.error.split("\n")[0]}`;
    return `<div class="pipe ${cls}"><div class="pname"><span class="dot"></span>${esc(b)}</div>
            <div class="pstate">${esc(detail)}</div></div>`;
  }).join("");

  // Single shared progress bar (both branches advance together).
  const frac = job.status === "done" ? 1 : (job.progress.done || 0) / total;
  $("progFill").style.width = (frac * 100).toFixed(1) + "%";

  $("progTitle").textContent = job.status === "done" ? "Run complete"
    : job.status === "stopped" ? "Run stopped"
    : job.status === "error" ? "Run failed"
    : job.paused ? "Paused"
    : "Running…";
  $("progSub").textContent = job.error || "";
  $("progNum").classList.toggle("pulse", job.status === "running" && !job.paused);
  if (job.status === "done") { $("progNum").classList.add("done"); $("progNum").textContent = "✓"; }
  else if (job.status === "stopped") { $("progNum").classList.remove("done"); $("progNum").textContent = "■"; }
  else if (job.paused) { $("progNum").textContent = "⏸"; }

  const con = $("console");
  const stick = con.scrollHeight - con.scrollTop - con.clientHeight < 40;
  con.innerHTML = job.log.map((l) =>
    `<span class="ln-t">${l.t}</span> <span class="ln-${l.level}">${esc(l.msg)}</span>`).join("\n");
  if (stick) con.scrollTop = con.scrollHeight;
}

/* ---------------- report ---------------- */
let reportRows = [];
function renderReport(job) {
  reportRows = job.results;
  const a = state.branchA, b = state.branchB;
  const matches = reportRows.filter((r) => r.match).length;
  $("reportCard").classList.remove("hidden");
  $("thA").textContent = `Response · ${a}`;
  $("thB").textContent = `Response · ${b}`;
  $("reportMeta").textContent = `${a} vs ${b} · ${reportRows.length} requests`;
  if (job.report_ready) {
    $("dlBtn").href = `/api/report/${state.jobId}.md`;
    $("dlBtn").classList.remove("hidden");
  }
  $("summaryChips").innerHTML = `
    <span class="chip">Total <b>${reportRows.length}</b></span>
    <span class="chip c-ok">✅ Matching <b>${matches}</b></span>
    <span class="chip c-bad">❌ Differing <b>${reportRows.length - matches}</b></span>`;
  drawRows();
  $("reportCard").scrollIntoView({ behavior: "smooth" });
}

function statusPill(r) {
  const s = r.status;
  // Distinguish a read-timeout from a generic connection error so a merely
  // slow endpoint doesn't look like a hard failure.
  const isTimeout = s == null && /timed out|timeout/i.test(r.body || "");
  const label = s ?? (isTimeout ? "TIMEOUT" : "ERR");
  const cls = s == null ? (isTimeout ? "st" : "sx") : "s" + String(s)[0];
  return `<span class="status-pill ${cls}">${label}</span><span class="ms">${r.time_ms} ms</span>`;
}

function drawRows() {
  const q = $("filterInput").value.toLowerCase();
  const diffOnly = $("diffOnly").checked;
  const body = $("reportBody");
  body.innerHTML = reportRows.map((r, i) => {
    if (diffOnly && r.match) return "";
    if (q && !r.route.toLowerCase().includes(q)) return "";
    return `
    <tr class="row-main" data-i="${i}">
      <td>${i + 1}</td>
      <td class="route-cell"><span class="method-pill m-${esc(r.method)}">${esc(r.method)}</span>${esc(r.route)}</td>
      <td><div class="cell-snip">${esc(r.request || "—")}</div></td>
      <td>${statusPill(r.a)}<div class="cell-snip">${esc(r.a.body)}</div></td>
      <td>${statusPill(r.b)}<div class="cell-snip">${esc(r.b.body)}</div></td>
      <td class="match-badge">${r.match ? "✅" : "❌"}</td>
    </tr>`;
  }).join("");

  body.querySelectorAll(".row-main").forEach((tr) =>
    tr.addEventListener("click", () => toggleDetail(tr)));
}

function pretty(text) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text || "(empty)"; }
}

function toggleDetail(tr) {
  const next = tr.nextElementSibling;
  if (next?.classList.contains("row-detail")) { next.remove(); return; }
  const r = reportRows[+tr.dataset.i];
  const det = document.createElement("tr");
  det.className = "row-detail";
  det.innerHTML = `<td colspan="6"><div class="detail-grid">
    <div class="detail-box detail-req"><h4>Request payload</h4><pre>${esc(pretty(r.request))}</pre></div>
    <div class="detail-box"><h4>${esc(state.branchA)} — ${r.a.status ?? "ERR"} · ${r.a.time_ms} ms</h4><pre>${esc(pretty(r.a.body))}</pre></div>
    <div class="detail-box"><h4>${esc(state.branchB)} — ${r.b.status ?? "ERR"} · ${r.b.time_ms} ms</h4><pre>${esc(pretty(r.b.body))}</pre></div>
  </div></td>`;
  tr.after(det);
}

$("filterInput").addEventListener("input", drawRows);
$("diffOnly").addEventListener("change", drawRows);

/* init */
updateRunBar();
loadRepo();
