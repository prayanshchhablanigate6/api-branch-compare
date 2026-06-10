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
dz.addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => addFiles([...e.target.files]));
["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => addFiles([...e.dataTransfer.files]));

async function addFiles(files) {
  for (const f of files) {
    if (state.files.some((x) => x.name === f.name)) continue;
    state.files.push({ name: f.name, content: await f.text() });
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
$("runBtn").addEventListener("click", async () => {
  $("runBtn").disabled = true;
  const cfg = {
    repo: state.repo.repo,
    branch_a: state.branchA, branch_b: state.branchB,
    requests: state.parsed.requests,
    server_cmd: $("serverCmd").value.trim(),
    base_url: $("baseUrl").value.trim(),
    startup_timeout: +$("startupTimeout").value || 180,
    request_timeout: +$("requestTimeout").value || 120,
  };
  const r = await fetch("/api/run", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cfg),
  });
  const data = await r.json();
  if (data.error) { alert(data.error); updateRunBar(); return; }
  state.jobId = data.job_id;
  $("progressCard").classList.remove("hidden");
  $("reportCard").classList.add("hidden");
  $("progressCard").scrollIntoView({ behavior: "smooth" });
  state.poller = setInterval(poll, 900);
  poll();
});

async function poll() {
  const r = await fetch(`/api/job/${state.jobId}`);
  const job = await r.json();
  renderProgress(job);
  if (job.status === "done" || job.status === "error") {
    clearInterval(state.poller);
    if (job.status === "done") renderReport(job);
  }
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
    if (st.status === "running" && job.progress.branch === b) detail = `replaying requests · ${job.progress.done}/${total}`;
    if (st.base_url) detail += ` · ${st.base_url}`;
    if (st.error) detail = `✗ ${st.error.split("\n")[0]}`;
    return `<div class="pipe ${cls}"><div class="pname"><span class="dot"></span>${esc(b)}</div>
            <div class="pstate">${esc(detail)}</div></div>`;
  }).join("");

  const idx = branches.indexOf(job.progress.branch);
  const frac = job.status === "done" ? 1
    : Math.max(0, idx) / branches.length + (job.progress.done / total) / branches.length;
  $("progFill").style.width = (frac * 100).toFixed(1) + "%";

  $("progTitle").textContent = job.status === "done" ? "Run complete"
    : job.status === "error" ? "Run failed" : "Running…";
  $("progSub").textContent = job.error || "";
  $("progNum").classList.toggle("pulse", job.status === "running");
  if (job.status === "done") { $("progNum").classList.add("done"); $("progNum").textContent = "✓"; }

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
  $("dlBtn").href = `/api/report/${state.jobId}.md`;
  $("summaryChips").innerHTML = `
    <span class="chip">Total <b>${reportRows.length}</b></span>
    <span class="chip c-ok">✅ Matching <b>${matches}</b></span>
    <span class="chip c-bad">❌ Differing <b>${reportRows.length - matches}</b></span>`;
  drawRows();
  $("reportCard").scrollIntoView({ behavior: "smooth" });
}

function statusPill(r) {
  const s = r.status;
  const cls = s == null ? "sx" : "s" + String(s)[0];
  return `<span class="status-pill ${cls}">${s ?? "ERR"}</span><span class="ms">${r.time_ms} ms</span>`;
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
