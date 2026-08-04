"use strict";
/* Dashboard for the loopback read-only API (R17).
 *
 * Every value rendered here comes from real session data — file paths, git
 * refs, model-authored finding text. It is set with textContent, never
 * innerHTML, so a path containing markup cannot become markup.
 *
 * No framework, no bundler, no external asset: the page must work from an
 * installed wheel with the network disabled. */

const app = document.querySelector("#app");
const pollState = document.querySelector("#poll-state");
const nextRun = document.querySelector("#next-run");

let pollTimer = null;

/* ---------- helpers ---------- */

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function getJSON(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const code = body && body.error ? body.error.code : "http_" + res.status;
    const err = new Error(code);
    err.code = code;
    throw err;
  }
  return body;
}

function fmtMs(ms) {
  if (!ms) return "—";
  const d = new Date(Number(ms));
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().replace("T", " ").slice(0, 16) + "Z";
}

function fmtDuration(ms) {
  if (!ms || ms < 0) return "—";
  const s = ms / 1000;
  if (s < 60) return s.toFixed(1) + "s";
  const m = Math.floor(s / 60);
  return m + "m" + String(Math.floor(s % 60)).padStart(2, "0") + "s";
}

function fmtUsd(v) {
  return "$" + Number(v || 0).toFixed(4);
}

/* The states every view must be able to render (addendum C.10). */
function statePanel(kind, glyph, message, hint) {
  const box = el("div", undefined, "state " + kind);
  box.append(el("span", glyph, "glyph"));
  box.append(el("span", message));
  if (hint) box.append(el("span", hint, "hint"));
  return box;
}

function showLoading(what) {
  app.setAttribute("aria-busy", "true");
  app.replaceChildren(statePanel("loading", "◐", "Loading " + what + "…"));
}

function showEmpty(message, hint) {
  app.setAttribute("aria-busy", "false");
  app.replaceChildren(statePanel("empty", "○", message, hint));
}

function showError(err) {
  app.setAttribute("aria-busy", "false");
  const unavailable = err && err.code === "state_unavailable";
  app.replaceChildren(
    statePanel(
      "error",
      "▲",
      unavailable ? "State database unavailable" : "Could not load this view",
      unavailable
        ? "Run `dreamy run` to create the state database, then reload."
        : "The server reported: " + (err && err.code ? err.code : "unknown error")
    )
  );
}

function card(title, cls) {
  const c = el("article", undefined, "card" + (cls ? " " + cls : ""));
  if (title) c.append(el("h2", title));
  return c;
}

function kv(pairs) {
  const dl = el("dl", undefined, "kv");
  for (const [k, v] of pairs) {
    dl.append(el("dt", k));
    dl.append(el("dd", v === undefined || v === null || v === "" ? "—" : v));
  }
  return dl;
}

function table(headers, rows, emptyText) {
  if (!rows.length) {
    const box = el("div");
    box.append(statePanel("empty", "○", emptyText || "Nothing to show"));
    return box;
  }
  const wrap = el("div", undefined, "table-wrap");
  const t = el("table");
  const thead = el("thead");
  const hr = el("tr");
  for (const h of headers) hr.append(el("th", h));
  thead.append(hr);
  t.append(thead);
  const tb = el("tbody");
  for (const cells of rows) {
    const tr = el("tr");
    for (const cell of cells) {
      if (cell instanceof Node) {
        const td = el("td");
        td.append(cell);
        tr.append(td);
      } else if (cell && typeof cell === "object") {
        tr.append(el("td", cell.text, cell.cls));
      } else {
        tr.append(el("td", cell));
      }
    }
    tb.append(tr);
  }
  t.append(tb);
  wrap.append(t);
  return wrap;
}

function provenanceCell(p) {
  // (D) mechanical vs (A) model inference. The letter is the signal; the
  // colour only reinforces it.
  const span = el("span", "(" + (p || "D") + ")", "prov prov-" + (p === "A" ? "A" : "D"));
  span.title = p === "A" ? "agent — model inference" : "deterministic — git + code evidence";
  return span;
}

function render(children) {
  app.setAttribute("aria-busy", "false");
  app.replaceChildren(...children);
}

/* ---------- views ---------- */

async function viewOverview() {
  showLoading("run overview");
  const [data, runs] = await Promise.all([
    getJSON("/api/v1/overview"),
    getJSON("/api/v1/runs?limit=10"),
  ]);
  const grid = el("section", undefined, "grid");

  const run = data.latest_run;
  const last = card("Last run");
  if (run) {
    last.append(
      kv([
        ["run", run.id],
        ["started", fmtMs(run.started_ms)],
        ["duration", fmtDuration(run.duration_ms)],
        ["status", run.status],
      ])
    );
  } else {
    last.append(statePanel("empty", "○", "No runs yet", "Run `dreamy run` to reconcile."));
  }
  grid.append(last);

  const sources = card("Sources");
  const srcRows = (data.source_stats || []).map((s) => {
    // Every label states what was OBSERVED, never what is inferred about the
    // machine. This panel is built from run counts and watermarks; no
    // discovery probe reaches it, so it cannot know whether a harness is
    // installed. Saying so was a real defect: four installed sources with
    // 6136, 84, 1382, and 1 files on disk were rendered "not installed"
    // purely because an incremental window produced no new records.
    //
    // "no data yet" is the honest floor for the third state — dreamy has
    // never ingested this source, and the reason is deliberately left open
    // (absent, or present but never scanned).
    let state;
    if (s.available) state = { text: "● active this run", cls: "ok" };
    else if (s.ever_ingested) state = { text: "◐ idle this run", cls: "dim" };
    else state = { text: "○ no data yet", cls: "dim" };
    return [
      s.source_id,
      state,
      { text: String(s.record_count ?? 0), cls: "num" },
    ];
  });
  sources.append(table(["source", "state", "records"], srcRows, "No sources recorded"));
  grid.append(sources);

  const spend = card("Agent spend");
  spend.append(
    kv([
      ["total", fmtUsd(data.total_agent_spend)],
      ["projects", data.counts ? data.counts.projects : "—"],
      ["findings", data.counts ? data.counts.findings : "—"],
    ])
  );
  grid.append(spend);

  const hist = card("History", "span");
  hist.append(
    table(
      ["run", "started", "duration", "status"],
      (runs.runs || []).map((r) => [
        r.id,
        fmtMs(r.started_ms),
        fmtDuration(r.duration_ms),
        { text: r.status, cls: r.status === "ok" ? "ok" : "warn" },
      ]),
      "No run history"
    )
  );
  grid.append(hist);

  render([grid]);
}

let findingsState = "default";

async function viewFindings() {
  showLoading("findings");
  const query = findingsState === "default" ? "" : "?state=" + encodeURIComponent(findingsState);
  const data = await getJSON("/api/v1/findings" + query);
  const rows = data.findings || [];

  const controls = el("div", undefined, "controls");
  const label = el("label", "state filter");
  label.htmlFor = "state-filter";
  const select = el("select");
  select.id = "state-filter";
  for (const [value, text] of [
    ["default", "new + regressed (default)"],
    ["all", "all"],
    ["new", "new"],
    ["regressed", "regressed"],
    ["persisting", "persisting"],
    ["resolved", "resolved"],
    ["dismissed", "dismissed"],
  ]) {
    const opt = el("option", text);
    opt.value = value;
    if (value === findingsState) opt.selected = true;
    select.append(opt);
  }
  select.addEventListener("change", () => {
    findingsState = select.value;
    viewFindings().catch(showError);
  });
  controls.append(label, select);
  // Make the filter's effect legible, so a filtered list is never mistaken
  // for the whole list.
  controls.append(el("span", rows.length + " shown", "dim"));

  const body = card("Findings", "span");
  body.append(
    table(
      ["sev", "category", "title", "prov", "state", "first seen"],
      rows.map((f) => [
        { text: f.severity, cls: "sev-" + f.severity },
        f.category,
        { text: f.title, cls: "wrap" },
        provenanceCell(f.provenance),
        { text: f.delta_state, cls: "st-" + f.delta_state },
        fmtMs(f.created_ms),
      ]),
      findingsState === "default"
        ? "No new or regressed findings"
        : "No findings match this filter"
    )
  );

  const grid = el("section", undefined, "grid");
  grid.append(body);
  render([controls, grid]);
}

async function viewProjects() {
  showLoading("projects");
  const data = await getJSON("/api/v1/projects");
  const projects = data.projects || [];
  if (!projects.length) {
    showEmpty("No projects reconciled yet", "Run `dreamy run` to ingest sessions.");
    return;
  }
  const grid = el("section", undefined, "grid");
  const body = card("Projects", "span");
  body.append(
    table(
      ["name", "sessions", "last seen", ""],
      projects.map((p) => {
        const link = el("a", "detail");
        link.href = "#/project/" + encodeURIComponent(p.id);
        return [
          { text: p.name || p.path, cls: "wrap" },
          { text: String(p.session_count ?? 0), cls: "num" },
          fmtMs(p.last_seen_ms),
          link,
        ];
      }),
      "No projects"
    )
  );
  grid.append(body);
  render([grid]);
}

async function viewProjectDetail(projectId) {
  showLoading("project");
  let data;
  try {
    data = await getJSON("/api/v1/projects/" + encodeURIComponent(projectId));
  } catch (err) {
    // R17g: the API answers a missing project with 404 `not_found`, which
    // `getJSON` raises. Without this branch the empty state below is
    // unreachable and a mistyped id renders the generic error panel, which
    // reads as a broken dashboard rather than a project that isn't there.
    if (err && err.code === "not_found") {
      showEmpty("Project not found", "It may have been pruned from the lookback window.");
      return;
    }
    throw err;
  }
  const detail = data.project;
  if (!detail) {
    showEmpty("Project not found");
    return;
  }
  const grid = el("section", undefined, "grid");

  const summary = detail.summary || {};
  const head = card("Project");
  head.append(
    kv([
      ["name", summary.name],
      ["path", summary.path],
      ["git remote", summary.git_remote],
      ["sessions", summary.session_count],
    ])
  );
  grid.append(head);

  const cost = card("Cost (30d)");
  if (detail.cost_30d) {
    cost.append(
      kv([
        ["total", fmtUsd(detail.cost_30d.total_usd)],
        ["per completed intent", fmtUsd(detail.cost_30d.per_completed_intent_usd)],
        ["episodes", detail.cost_30d.episode_count],
      ])
    );
  } else {
    cost.append(statePanel("empty", "○", "No attributed cost"));
  }
  grid.append(cost);

  const eps = card("Episodes", "span");
  eps.append(
    table(
      ["started", "intent", "completion", "drift"],
      (detail.episodes || []).map((e) => [
        fmtMs(e.started_ms),
        { text: e.original_intent, cls: "wrap" },
        e.completion_status,
        e.drift_type || "—",
      ]),
      "No episodes recorded"
    )
  );
  grid.append(eps);

  const prompts = card("Prompt artifacts", "span");
  prompts.append(
    table(
      ["type", "hash", "created"],
      (detail.prompt_artifacts || []).map((p) => [p.prompt_type, p.stable_hash, fmtMs(p.created_ms)]),
      "No prompts generated for this project"
    )
  );
  grid.append(prompts);

  render([grid]);
}

async function viewPrompts() {
  showLoading("prompts");
  const projects = await getJSON("/api/v1/projects");
  const list = projects.projects || [];
  if (!list.length) {
    showEmpty("No projects yet", "Prompts are compiled per project during a run.");
    return;
  }
  const controls = el("div", undefined, "controls");
  const label = el("label", "project");
  label.htmlFor = "prompt-project";
  const select = el("select");
  select.id = "prompt-project";
  for (const p of list) {
    const opt = el("option", p.name || p.path);
    opt.value = p.id;
    select.append(opt);
  }
  controls.append(label, select);

  const holder = el("section", undefined, "grid");

  async function load(projectId) {
    holder.replaceChildren(statePanel("loading", "◐", "Loading prompts…"));
    const data = await getJSON("/api/v1/prompts?project=" + encodeURIComponent(projectId));
    const rows = data.prompts || [];
    const body = card("Artifacts", "span");
    body.append(
      table(
        ["type", "hash", "created"],
        rows.map((p) => [p.prompt_type, p.stable_hash, fmtMs(p.created_ms)]),
        "No artifacts for this project"
      )
    );
    holder.replaceChildren(body);
  }

  select.addEventListener("change", () => load(select.value).catch(showError));
  render([controls, holder]);
  await load(select.value);
}

async function viewSchedule() {
  showLoading("schedule");
  const data = await getJSON("/api/v1/schedule");
  const s = data.schedule;
  const grid = el("section", undefined, "grid");
  const body = card("launchd job");
  if (!s) {
    body.append(statePanel("empty", "○", "Schedule state unavailable"));
  } else {
    body.append(
      kv([
        ["label", s.label],
        ["installed", s.installed ? "● yes" : "○ no"],
        ["interval", s.interval_seconds ? s.interval_seconds + "s" : "—"],
        ["next run", fmtMs(s.next_run_ms)],
        ["plist", s.plist_path],
        ["last exit", s.last_exit === null || s.last_exit === undefined ? "—" : s.last_exit],
      ])
    );
    body.append(
      el(
        "p",
        "Install and uninstall are deliberately absent here — schedule mutation stays in the CLI and TUI, where it is confirmed.",
        "dim"
      )
    );
  }
  grid.append(body);
  render([grid]);
}

async function loadMonitor(holder) {
  const data = await getJSON("/api/v1/monitor?limit=100");
  const events = data.events || [];
  const body = card("Live log", "span");
  if (!events.length) {
    body.append(
      statePanel("empty", "○", "No recent events", "Events appear here while a run is in flight.")
    );
  } else {
    const list = el("div", undefined, "log");
    for (const e of events) {
      const line = el("div", undefined, "lvl-" + (e.level || "INFO"));
      line.textContent = fmtMs(e.ts_ms) + "  " + (e.topic || "") + "  " + (e.msg || "");
      list.append(line);
    }
    body.append(list);
  }
  holder.replaceChildren(body);
}

async function viewMonitor() {
  showLoading("monitor");
  const holder = el("section", undefined, "grid");
  render([holder]);
  await loadMonitor(holder);
  // Polling, not a socket: this is a local read-only view, and a websocket
  // would add a protocol for no gain. Polling stops while the tab is hidden
  // so a forgotten background tab cannot keep hitting the database.
  pollTimer = setInterval(() => {
    if (document.hidden) return;
    // R17g: swallowing the rejection left the last good frame on screen
    // forever, so a state db that went away mid-poll looked like a healthy
    // but idle run. Surface it in-place instead, and keep polling so the
    // view recovers on its own when the backend returns.
    loadMonitor(holder).catch((err) => {
      const unavailable = err && err.code === "state_unavailable";
      holder.replaceChildren(
        statePanel(
          "error",
          "▲",
          unavailable ? "State database unavailable" : "Live log interrupted",
          "Retrying every 2s…"
        )
      );
    });
  }, 2000);
  pollState.textContent = "monitor polling every 2s";
}

/* ---------- routing ---------- */

const VIEWS = {
  overview: viewOverview,
  findings: viewFindings,
  projects: viewProjects,
  prompts: viewPrompts,
  schedule: viewSchedule,
  monitor: viewMonitor,
};

function markTab(name) {
  for (const a of document.querySelectorAll(".tabs a")) {
    if (a.dataset.view === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

async function refreshChrome() {
  try {
    const data = await getJSON("/api/v1/schedule");
    const s = data.schedule;
    nextRun.textContent =
      s && s.installed && s.next_run_ms
        ? "next run: " + fmtMs(s.next_run_ms)
        : "next run: not scheduled";
  } catch {
    nextRun.textContent = "next run: unavailable";
  }
}

async function route() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  pollState.textContent = "";
  const hash = location.hash.replace(/^#\/?/, "") || "overview";
  try {
    if (hash.startsWith("project/")) {
      markTab("projects");
      await viewProjectDetail(hash.slice("project/".length));
      return;
    }
    const view = VIEWS[hash] ? hash : "overview";
    markTab(view);
    await VIEWS[view]();
  } catch (err) {
    showError(err);
  }
}

addEventListener("hashchange", route);
route();
refreshChrome();
