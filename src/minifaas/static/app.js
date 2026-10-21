// miniFaaS dashboard: plain JavaScript, no framework, no build step.
// Every view is rebuilt from the JSON API; text always goes in through textContent, never innerHTML.
"use strict";

const view = document.getElementById("view");
const dialog = document.getElementById("dialog");
let status = null;
let cleanup = () => {};

// ---------------------------------------------------------------- helpers

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, options = {}) {
  const init = { ...options, headers: { "Content-Type": "application/json" } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function toast(message) {
  const node = h("div", { class: "toast", role: "status" }, message);
  document.body.append(node);
  setTimeout(() => node.remove(), 3000);
}

function ago(ts) {
  if (!ts) return "—";
  const seconds = Math.round(Date.now() / 1000 - ts);
  const future = seconds < 0;
  const s = Math.abs(seconds);
  const text =
    s < 45 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
  return future ? `in ${text}` : `${text} ago`;
}

function when(ts) {
  return h("span", { title: ts ? new Date(ts * 1000).toLocaleString() : "" }, ago(ts));
}

function clock(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function duration(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`;
}

function commitLink(sha) {
  if (!sha) return "—";
  const url = status && status.repo.url.startsWith("https://github.com/") ? `${status.repo.url}/commit/${sha}` : null;
  const label = h("code", {}, sha.slice(0, 7));
  return url ? h("a", { href: url, target: "_blank", rel: "noopener" }, label) : label;
}

function runPill(run) {
  if (!run) return h("span", { class: "pill" }, "never run");
  if (!run.finished) return h("span", { class: "pill warn live" }, h("span", { class: "dot" }), "running");
  if (run.error || run.status >= 500) return h("span", { class: "pill err" }, run.status ? `failed · ${run.status}` : "failed");
  if (run.status >= 400) return h("span", { class: "pill warn" }, String(run.status));
  return h("span", { class: "pill ok" }, `ok · ${run.status}`);
}

function workerPill(worker) {
  const kind = { running: "ok live", starting: "warn live", crashed: "err", stopped: "", "no release": "" }[worker.status];
  return h("span", { class: `pill ${kind ?? ""}` }, h("span", { class: "dot" }), `worker ${worker.status}`);
}

function triggerLabel(fn) {
  if (fn.trigger === "http") return h("span", {}, "HTTP");
  return h("span", { class: "stack" }, h("code", {}, fn.schedule), h("span", { class: "muted small" }, fn.timezone));
}

function openDialog(title, ...body) {
  document.getElementById("dialog-title").textContent = title;
  document.getElementById("dialog-body").replaceChildren(...body);
  if (!dialog.open) dialog.showModal();
}

function logLines(lines, { showFn = false } = {}) {
  const box = h("pre", { class: "log" });
  appendLines(box, lines, showFn);
  if (!lines.length) box.append(h("div", {}, h("span", { class: "l muted" }, "No output.")));
  return box;
}

function appendLines(box, lines, showFn) {
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  for (const entry of lines) {
    box.append(
      h(
        "div",
        { class: entry.stream },
        h("span", { class: "t" }, clock(entry.ts)),
        showFn ? h("span", { class: "f" }, entry.fn || entry.stream.replace("-error", "")) : null,
        h("span", { class: "l" }, entry.line),
      ),
    );
  }
  while (box.childElementCount > 2000) box.firstElementChild.remove();
  if (stick) box.scrollTop = box.scrollHeight;
}

// The live feed: one EventSource, shared by whichever view is open.
const listeners = new Set();
function listen(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
const feed = new EventSource("/api/stream");
feed.onmessage = (event) => {
  const item = JSON.parse(event.data);
  for (const fn of listeners) fn(item);
};

async function loadStatus() {
  status = await api("/api/status");
  document.getElementById("version").textContent = status.version ? `v${status.version}` : "";
  return status;
}

// ---------------------------------------------------------------- run dialog

async function showRun(id) {
  const run = await api(`/api/runs/${encodeURIComponent(id)}`);
  openDialog(
    `${run.fn} · ${new Date(run.started * 1000).toLocaleString()}`,
    h(
      "div",
      { class: "row" },
      runPill(run),
      h("span", { class: "pill" }, run.trigger),
      h("span", { class: "muted small" }, duration(run.ms)),
      h("span", { class: "muted small" }, "commit ", commitLink(run.sha)),
    ),
    run.error ? h("div", { class: "banner err" }, run.error) : null,
    logLines(run.logs),
  );
}

function runsTable(runs, { showFn = false } = {}) {
  if (!runs.length) return h("div", { class: "empty" }, "No runs in the last days.");
  return h(
    "table",
    {},
    h(
      "thead",
      {},
      h("tr", {}, showFn ? h("th", {}, "Function") : null, h("th", {}, "Started"), h("th", {}, "Status"), h("th", { class: "wide" }, "Trigger"), h("th", {}, "Duration")),
    ),
    h(
      "tbody",
      {},
      runs.map((run) =>
        h(
          "tr",
          { class: "link", onclick: () => showRun(run.id) },
          showFn ? h("td", {}, h("span", { class: "name" }, run.fn)) : null,
          h("td", {}, when(run.started)),
          h("td", {}, runPill(run)),
          h("td", { class: "wide muted" }, run.trigger),
          h("td", { class: "muted" }, duration(run.ms)),
        ),
      ),
    ),
  );
}

// ---------------------------------------------------------------- views

async function functionsView() {
  const s = await loadStatus();
  const release = s.release;
  const deployButton = h("button", { class: "primary", onclick: deploy }, s.build ? "Building…" : "Deploy now");
  if (s.build) deployButton.disabled = true;

  const summary = h(
    "section",
    { class: "card" },
    h(
      "div",
      { class: "card-head" },
      h(
        "div",
        { class: "stack" },
        h("h1", {}, release?.commit?.subject || "No release yet"),
        h(
          "div",
          { class: "row muted small" },
          h("a", { class: "repo", href: s.repo.url, target: "_blank", rel: "noopener" }, s.repo.url.replace(/^https:\/\//, "")),
          h("span", {}, "·"),
          h("code", {}, s.repo.branch),
        ),
      ),
      h("div", { class: "row" }, workerPill(s.worker), deployButton),
    ),
    h(
      "dl",
      { class: "facts" },
      h("div", {}, h("dt", {}, "Deployed commit"), h("dd", {}, release ? commitLink(release.sha) : "—")),
      h("div", {}, h("dt", {}, "Committed"), h("dd", {}, release?.commit ? when(Date.parse(release.commit.date) / 1000) : "—")),
      h("div", {}, h("dt", {}, "Checked for commits"), h("dd", {}, when(s.last_check), s.next_check ? h("span", { class: "muted small" }, " · next ", when(s.next_check)) : null)),
      h("div", {}, h("dt", {}, "Functions port"), h("dd", {}, h("code", {}, `:${s.ports.functions}/<name>`))),
    ),
  );

  const banners = [];
  if (s.build) {
    banners.push(
      h("div", { class: "banner warn" }, h("div", { class: "row" }, h("strong", {}, `Building ${s.build.sha.slice(0, 7)}…`), h("a", { href: "#/deploys", onclick: () => showDeploy(s.build.id) }, "View log"))),
    );
  }
  if (s.failed) {
    banners.push(
      h(
        "div",
        { class: "banner err" },
        h("strong", {}, s.failed.sha ? `Commit ${s.failed.sha.slice(0, 7)} was not deployed` : "Deploy failed"),
        h("pre", {}, s.failed.error),
        release ? h("span", { class: "small" }, "The previous release keeps serving.") : null,
      ),
    );
  }

  const rows = s.functions.map((fn) =>
    h(
      "tr",
      { class: "link", onclick: () => (location.hash = `#/fn/${encodeURIComponent(fn.name)}`) },
      h(
        "td",
        {},
        h("div", { class: "name" }, fn.name),
        h("div", { class: "narrow muted small" }, fn.trigger === "http" ? "HTTP" : fn.paused ? `${fn.schedule} · paused` : fn.schedule),
        fn.missing.length ? h("span", { class: "pill err", title: fn.missing.join(", ") }, `missing ${fn.missing.length} secret${fn.missing.length > 1 ? "s" : ""}`) : null,
      ),
      h("td", { class: "wide" }, triggerLabel(fn)),
      h(
        "td",
        { class: "wide" },
        fn.trigger === "schedule" ? (fn.paused ? h("span", { class: "pill" }, "paused") : when(fn.next_run)) : h("code", { class: "muted" }, `/${fn.name}`),
      ),
      h("td", {}, fn.running ? runPill({}) : runPill(fn.last), fn.last ? h("div", { class: "muted small" }, when(fn.last.started)) : null),
      h("td", { class: "wide muted small" }, `${fn.day.runs} runs`, fn.day.failed ? h("span", { class: "pill err" }, `${fn.day.failed} failed`) : null),
    ),
  );

  const table = h(
    "section",
    { class: "card flush" },
    h("div", { class: "card-head" }, h("h2", {}, "Functions"), h("span", { class: "muted small" }, `${s.functions.length} in this release`)),
    s.functions.length
      ? h(
          "table",
          {},
          h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", { class: "wide" }, "Trigger"), h("th", { class: "wide" }, "Next run / path"), h("th", {}, "Last run"), h("th", { class: "wide" }, "Last 24 h"))),
          h("tbody", {}, rows),
        )
      : h("div", { class: "empty" }, "Nothing is deployed yet."),
    s.unsupported.length
      ? h("div", { class: "empty small" }, "Skipped: ", s.unsupported.map((u) => `${u.name} (${u.reason})`).join(", "))
      : null,
  );

  view.replaceChildren(...banners, summary, table);
  const timer = setInterval(refresh, s.build ? 2000 : 10000);
  const stop = listen((item) => item.kind === "run" && refresh());
  return () => {
    clearInterval(timer);
    stop();
  };
}

async function deploy() {
  await api("/api/deploy", { method: "POST" });
  toast("Checking the branch…");
  setTimeout(refresh, 800);
}

async function functionView(name) {
  const s = await loadStatus();
  const fn = s.functions.find((f) => f.name === name);
  if (!fn) {
    view.replaceChildren(h("div", { class: "card empty" }, `No function named ${name} in this release. `, h("a", { href: "#/" }, "Back")));
    return () => {};
  }
  const runs = await api(`/api/runs?fn=${encodeURIComponent(name)}&limit=50`);
  const result = h("div", { class: "stack" });

  const method = h("select", { "aria-label": "Method" }, ["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => h("option", { selected: m === "GET" }, m)));
  const query = h("input", { placeholder: "query string, e.g. name=Ana", "aria-label": "Query string" });
  const body = h("textarea", { placeholder: "Request body (sent as JSON when it parses)", "aria-label": "Body" });
  const runButton = h("button", { class: "primary", onclick: runNow }, "Run now");

  async function runNow() {
    runButton.disabled = true;
    result.replaceChildren(h("span", { class: "muted" }, "Running…"));
    let contentType = "";
    if (body.value.trim()) {
      try {
        JSON.parse(body.value);
        contentType = "application/json";
      } catch {
        contentType = "text/plain";
      }
    }
    try {
      const r = await api(`/api/functions/${encodeURIComponent(name)}/run`, {
        method: "POST",
        body: { method: method.value, query: query.value, body: body.value, content_type: contentType },
      });
      result.replaceChildren(
        h("div", { class: "row" }, runPill({ finished: 1, status: r.status, error: r.error }), h("a", { href: "#", onclick: (e) => (e.preventDefault(), showRun(r.run)) }, "View log")),
        r.error ? h("div", { class: "banner err" }, r.error) : h("pre", { class: "log" }, h("div", {}, h("span", { class: "l" }, r.body || "(empty response)"))),
      );
    } catch (error) {
      result.replaceChildren(h("div", { class: "banner err" }, error.message));
    } finally {
      runButton.disabled = false;
    }
  }

  async function togglePause() {
    await api(`/api/functions/${encodeURIComponent(name)}/${fn.paused ? "resume" : "pause"}`, { method: "POST" });
    toast(fn.paused ? "Schedule resumed" : "Schedule paused");
    refresh();
  }

  const isHttp = fn.trigger === "http";
  const head = h(
    "section",
    { class: "card" },
    h(
      "div",
      { class: "card-head" },
      h("div", { class: "stack" }, h("a", { href: "#/", class: "small" }, "← Functions"), h("h1", {}, fn.name)),
      h("div", { class: "row" }, fn.trigger === "schedule" ? h("button", { onclick: togglePause }, fn.paused ? "Resume schedule" : "Pause schedule") : null, isHttp ? null : runButton),
    ),
    h(
      "dl",
      { class: "facts" },
      h("div", {}, h("dt", {}, "Trigger"), h("dd", {}, triggerLabel(fn))),
      fn.trigger === "schedule"
        ? h("div", {}, h("dt", {}, "Next run"), h("dd", {}, fn.paused ? "paused" : when(fn.next_run)))
        : h("div", {}, h("dt", {}, "URL"), h("dd", {}, h("code", {}, `${location.protocol}//${location.hostname}:${s.ports.functions}/${fn.name}`))),
      h("div", {}, h("dt", {}, "Declared with"), h("dd", {}, fn.source === "firebase" ? "firebase_functions" : "functions_framework")),
      h("div", {}, h("dt", {}, "Secrets"), h("dd", {}, fn.secrets.length ? fn.secrets.map((k) => h("span", { class: `pill ${fn.missing.includes(k) ? "err" : "ok"}` }, k)) : "none")),
    ),
    isHttp
      ? h("div", { class: "run-form" }, method, query, h("div", { class: "full" }, body), h("div", { class: "full row" }, runButton, h("span", { class: "muted small" }, "Calls the function through the worker, like a request would.")))
      : null,
    h("div", { style: "margin-top:14px" }, result),
  );

  const history = h("section", { class: "card flush" }, h("div", { class: "card-head" }, h("h2", {}, "Runs"), h("span", { class: "muted small" }, `kept ${s.retention_days} days`)), runsTable(runs));
  view.replaceChildren(head, history);

  const stop = listen(async (item) => {
    if (item.kind !== "run" || item.fn !== name) return;
    const fresh = await api(`/api/runs?fn=${encodeURIComponent(name)}&limit=50`);
    history.lastElementChild.replaceWith(runsTable(fresh));
  });
  return stop;
}

async function logsView(params) {
  const s = status || (await loadStatus());
  const fn = params.get("fn") || "";
  const select = h(
    "select",
    { "aria-label": "Function", onchange: () => (location.hash = select.value ? `#/logs?fn=${encodeURIComponent(select.value)}` : "#/logs") },
    h("option", { value: "" }, "All functions and miniFaaS"),
    s.functions.map((f) => h("option", { value: f.name, selected: f.name === fn }, f.name)),
  );
  let paused = false;
  const pause = h("button", { onclick: () => ((paused = !paused), (pause.textContent = paused ? "Resume" : "Pause")) }, "Pause");
  const live = h("span", { class: "pill ok live" }, h("span", { class: "dot" }), "live");
  const lines = await api(`/api/logs?limit=500${fn ? `&fn=${encodeURIComponent(fn)}` : ""}`);
  const box = logLines(lines, { showFn: !fn });
  box.style.maxHeight = "calc(100vh - 220px)";
  view.replaceChildren(h("section", { class: "card flush" }, h("div", { class: "card-head" }, h("div", { class: "row" }, h("h2", {}, "Logs"), live), h("div", { class: "row" }, select, pause)), box));
  box.scrollTop = box.scrollHeight;
  return listen((item) => {
    if (item.kind !== "log" || paused || (fn && item.fn !== fn)) return;
    if (box.querySelector(".muted")) box.replaceChildren();
    appendLines(box, [item], !fn);
  });
}

async function showDeploy(id) {
  const d = await api(`/api/deploys/${id}`);
  const box = logLines((d.log || "").split("\n").map((line) => ({ ts: d.started || Date.now() / 1000, stream: "worker", line })));
  for (const t of box.querySelectorAll(".t")) t.remove();
  openDialog(`Deploy ${d.sha ? d.sha.slice(0, 7) : `#${id}`}`, d.error ? h("div", { class: "banner err" }, h("pre", {}, d.error)) : null, box);
  box.scrollTop = box.scrollHeight;
  if (d.status === "building") setTimeout(() => dialog.open && showDeploy(id), 1500);
}

async function deploysView() {
  const deploys = await api("/api/deploys");
  const pill = (d) =>
    h("span", { class: `pill ${{ ok: "ok", failed: "err", building: "warn live" }[d.status] || ""}` }, d.status === "building" ? h("span", { class: "dot" }) : null, d.status);
  view.replaceChildren(
    h(
      "section",
      { class: "card flush" },
      h("div", { class: "card-head" }, h("h2", {}, "Deploys"), h("button", { class: "primary", onclick: deploy }, "Deploy now")),
      deploys.length
        ? h(
            "table",
            {},
            h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Result"), h("th", {}, "Commit"), h("th", { class: "wide" }, "Trigger"))),
            h(
              "tbody",
              {},
              deploys.map((d) =>
                h(
                  "tr",
                  { class: "link", onclick: () => showDeploy(d.id) },
                  h("td", {}, when(d.started)),
                  h("td", {}, pill(d)),
                  h("td", {}, h("div", {}, commitLink(d.sha)), h("div", { class: "muted small" }, d.subject || (d.error || "").split("\n")[0])),
                  h("td", { class: "wide muted" }, d.trigger),
                ),
              ),
            ),
          )
        : h("div", { class: "empty" }, "No deploys yet."),
    ),
  );
  const timer = setInterval(() => deploys.some((d) => d.status === "building") && refresh(), 2000);
  return () => clearInterval(timer);
}

async function envView() {
  const data = await api("/api/env");
  const list = h("div", {});

  function addRow(name = "", value = "") {
    const key = h("input", { value: name, placeholder: "NAME", "aria-label": "Name", autocomplete: "off", spellcheck: "false" });
    const val = h("input", { value, type: "password", placeholder: "value", "aria-label": "Value", autocomplete: "off", spellcheck: "false" });
    const show = h("button", { type: "button", class: "icon", title: "Show", onclick: () => (val.type = val.type === "password" ? "text" : "password") }, "👁");
    const row = h("div", { class: "env-row" }, key, val, show, h("button", { type: "button", class: "icon danger", title: "Remove", onclick: () => row.remove() }, "✕"));
    list.append(row);
    return key;
  }

  for (const [name, value] of Object.entries(data.variables)) addRow(name, value);
  if (!Object.keys(data.variables).length) addRow();

  async function save() {
    const variables = {};
    for (const row of list.children) {
      const [key, val] = row.querySelectorAll("input");
      if (key.value.trim()) variables[key.value.trim()] = val.value;
    }
    try {
      await api("/api/env", { method: "PUT", body: { variables } });
      toast("Saved. The worker is restarting.");
      refresh();
    } catch (error) {
      toast(error.message);
    }
  }

  const required = data.required.length
    ? h(
        "section",
        { class: "card" },
        h("h2", {}, "Secrets the code asks for"),
        h("p", { class: "muted small" }, "Declared with secrets=[…] in a Firebase decorator. Set each one here or in the container's environment."),
        h(
          "div",
          { class: "row" },
          data.required.map((r) =>
            h("span", { class: `pill ${r.source ? "ok" : "err"}`, title: r.source ? `set in the ${r.source}` : "not set" }, r.name, r.source ? ` · ${r.source}` : " · missing"),
          ),
        ),
      )
    : null;

  view.replaceChildren(
    required || "",
    h(
      "section",
      { class: "card flush" },
      h(
        "div",
        { class: "card-head" },
        h("div", { class: "stack" }, h("h2", {}, "Environment variables"), h("span", { class: "muted small" }, "Passed to every function on top of the container's environment. Stored on the data volume.")),
        h("div", { class: "row" }, h("button", { onclick: () => addRow().focus() }, "Add"), h("button", { class: "primary", onclick: save }, "Save & restart")),
      ),
      list,
    ),
  );
  return () => {};
}

// ---------------------------------------------------------------- settings and first-run wizard

// [field, label, kind, hint]. Defaults come from the server and show as placeholders.
const SETTINGS = {
  repository: [
    ["git_url", "Repository URL", "url", "HTTPS URL of the GitHub repository with your functions."],
    ["git_branch", "Branch", "text", "Every push to this branch is deployed."],
    ["git_token", "Access token", "token", tokenHint],
  ],
  runtime: [
    ["timezone", "Time zone", "timezone", "Used by schedules that don't name their own time zone."],
    [
      "update_schedule",
      "Check for new commits",
      "text",
      "Cron, in the time zone above (default 06:00 and 18:00). Commits are also checked before every scheduled run, after HTTP requests, and with Deploy now. off: only then.",
    ],
  ],
  advanced: [
    ["source_root", "Source folder", "text", "Folder with your functions module. Empty: found automatically."],
    ["entry_module", "Entry module", "text", "The file to import, without .py. Empty: the one with decorated functions."],
    ["git_username", "Git username", "text", "Paired with the token. GitLab uses oauth2."],
    ["threads", "Concurrent requests", "number", "Requests the worker serves at once."],
    ["function_timeout", "Run timeout (seconds)", "number", "How long a scheduled run may take, unless its decorator sets timeout_sec."],
    ["misfire_grace", "Late-start grace (seconds)", "number", "A scheduled run that is late by more than this is skipped."],
    ["retention_days", "Keep history for (days)", "number", "Run history, logs and deploys."],
    ["keep_releases", "Releases kept", "number", "Old builds kept on disk for rollback."],
    ["build_timeout", "Build timeout (seconds)", "number", "Clone, install and discovery together."],
    ["max_restarts", "Crashes before rollback", "number", "Consecutive worker crashes before going back to the previous release."],
    ["shutdown_grace", "Shutdown grace (seconds)", "number", "Time a stopping worker gets before it is killed."],
    ["log_level", "miniFaaS log level", "level", "Only miniFaaS's own messages; your functions' output is always kept."],
  ],
};

// GitHub's new fine-grained token page, prefilled with a name and read-only Contents. Only the
// repository has to be picked by hand.
const TOKEN_URL =
  "https://github.com/settings/personal-access-tokens/new?" +
  new URLSearchParams({ name: "miniFaaS", description: "Read-only access for miniFaaS to deploy functions", contents: "read" });

function tokenHint() {
  return h(
    "span",
    {},
    "Only for a private repo. ",
    h("a", { href: TOKEN_URL, target: "_blank", rel: "noopener" }, "Create one on GitHub ↗"),
    ": a fine-grained token with access to just that repository and Contents set to read-only.",
  );
}

function timezones(current) {
  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  return [...new Set(["UTC", current, ...zones].filter(Boolean))];
}

async function settingsView({ wizard = false } = {}) {
  const data = await api("/api/settings");
  const inputs = {};
  let clearToken = null;

  function field([name, label, kind, hint]) {
    const locked = data.locked[name];
    const value = data.values[name] ?? "";
    const fallback = data.defaults[name];
    let input;
    if (kind === "timezone") {
      const chosen = value || (wizard ? Intl.DateTimeFormat().resolvedOptions().timeZone : "UTC");
      input = h("select", {}, timezones(chosen).map((z) => h("option", { selected: z === chosen }, z)));
    } else if (kind === "level") {
      input = h("select", {}, ["DEBUG", "INFO", "WARNING", "ERROR"].map((l) => h("option", { selected: l === String(value).toUpperCase() }, l)));
    } else if (kind === "token") {
      input = h("input", {
        type: "password",
        autocomplete: "off",
        placeholder: data.token_set ? "Saved. Leave empty to keep it." : "github_pat_…",
      });
    } else {
      input = h("input", {
        type: kind === "number" ? "number" : kind === "url" ? "url" : "text",
        min: kind === "number" ? "0" : null,
        value: kind === "number" || value !== fallback || name === "git_branch" ? value : "",
        placeholder: kind === "url" ? "https://github.com/you/your-functions.git" : fallback === "" ? "auto" : String(fallback),
        spellcheck: "false",
        autocomplete: "off",
        required: name === "git_url" || null,
      });
    }
    input.disabled = Boolean(locked);
    inputs[name] = input;
    const extra =
      kind === "token" && data.token_set && !locked
        ? h("label", { class: "check" }, (clearToken = h("input", { type: "checkbox" })), "Remove the saved token")
        : null;
    return h(
      "label",
      { class: "field" },
      h("span", { class: "field-name" }, label),
      input,
      extra,
      h(
        "span",
        { class: "hint" },
        locked ? h("span", {}, "Set by ", h("code", {}, locked), " in the container, which overrides this page.") : typeof hint === "function" ? hint() : hint,
      ),
    );
  }

  const testResult = h("div", { class: "test-result" });
  const testButton = h("button", { type: "button", onclick: test }, "Test connection");

  async function test() {
    testButton.disabled = true;
    testResult.replaceChildren(h("span", { class: "muted small" }, "Checking…"));
    try {
      const r = await api("/api/settings/test", {
        method: "POST",
        body: {
          git_url: inputs.git_url.value,
          git_branch: inputs.git_branch.value || "main",
          git_token: inputs.git_token.value,
          git_username: inputs.git_username.value,
        },
      });
      testResult.replaceChildren(
        r.ok
          ? h("span", { class: "pill ok" }, `Found ${inputs.git_branch.value || "main"} at ${r.sha.slice(0, 7)}`)
          : h("div", { class: "banner err" }, h("pre", {}, r.error)),
      );
    } catch (error) {
      testResult.replaceChildren(h("div", { class: "banner err" }, error.message));
    } finally {
      testButton.disabled = false;
    }
  }

  const saveError = h("div", {});
  const saveButton = h("button", { class: "primary", type: "submit" }, wizard ? "Save and deploy" : "Save");

  async function save(event) {
    event.preventDefault();
    const values = {};
    for (const [name, input] of Object.entries(inputs)) {
      if (input.disabled) continue;
      if (name === "git_token" && !input.value) continue;
      values[name] = input.value.trim();
    }
    saveButton.disabled = true;
    saveError.replaceChildren();
    try {
      await api("/api/settings", { method: "PUT", body: { values, clear_token: Boolean(clearToken?.checked) } });
      toast(wizard ? "Saved. Building your functions…" : "Settings saved.");
      status = null;
      if (location.hash === "#/" || location.hash === "") render();
      else location.hash = "#/";
    } catch (error) {
      saveError.replaceChildren(h("div", { class: "banner err" }, error.message));
    } finally {
      saveButton.disabled = false;
    }
  }

  const section = (title, fields, ...extra) => h("fieldset", {}, h("legend", {}, title), h("div", { class: "fields" }, fields.map(field)), ...extra);

  const form = h(
    "form",
    { class: "card settings", onsubmit: save },
    wizard
      ? h(
          "div",
          { class: "stack" },
          h("h1", {}, "Set up miniFaaS"),
          h(
            "p",
            { class: "muted" },
            "Point miniFaaS at the repository with your functions. It deploys the branch now and again on every push.",
          ),
        )
      : h("div", { class: "card-head" }, h("h1", {}, "Settings"), h("span", { class: "muted small" }, "Saved to ", h("code", {}, data.config_file))),
    section("Repository", SETTINGS.repository, h("div", { class: "row" }, testButton, testResult)),
    section("Schedules and updates", SETTINGS.runtime),
    h("details", {}, h("summary", {}, "Advanced"), section("Advanced", SETTINGS.advanced)),
    saveError,
    h(
      "div",
      { class: "row" },
      saveButton,
      h("span", { class: "muted small" }, "Container environment variables (MINIFAAS_…) always win over these."),
    ),
  );

  const warning = data.data_volume
    ? null
    : h(
        "div",
        { class: "banner warn" },
        h("strong", {}, "No volume is mounted on the data folder"),
        h(
          "span",
          {},
          "These settings, the run history and the builds will be lost when the container is recreated. Mount one, for example ",
          h("code", {}, "-v minifaas-data:/data"),
          ".",
        ),
      );

  view.replaceChildren(...[warning, form].filter(Boolean));
  if (wizard) inputs.git_url.focus();
  return () => {};
}

// ---------------------------------------------------------------- router

const routes = [
  [/^#\/fn\/(.+)$/, (m) => functionView(decodeURIComponent(m[1])), "functions"],
  [/^#\/logs(?:\?(.*))?$/, (m) => logsView(new URLSearchParams(m[1] || "")), "logs"],
  [/^#\/deploys$/, () => deploysView(), "deploys"],
  [/^#\/env$/, () => envView(), "env"],
  [/^#\/settings$/, () => settingsView(), "settings"],
  [/^.*$/, () => functionsView(), "functions"],
];

let rendering = false;
async function render() {
  if (rendering) return;
  rendering = true;
  const hash = location.hash || "#/";
  // Until a repository is set, every page is the setup wizard.
  if (!status) await loadStatus().catch(() => null);
  const setup = Boolean(status && !status.configured);
  document.body.classList.toggle("setup", setup);
  const table = setup ? [[/^.*$/, () => settingsView({ wizard: true }), "settings"]] : routes;
  for (const [pattern, handler, tab] of table) {
    const match = hash.match(pattern);
    if (!match) continue;
    for (const a of document.querySelectorAll("nav a")) a.classList.toggle("active", a.dataset.tab === tab);
    cleanup();
    try {
      cleanup = (await handler(match)) || (() => {});
    } catch (error) {
      cleanup = () => {};
      view.replaceChildren(h("div", { class: "banner err" }, `Could not load this page: ${error.message}`));
    }
    break;
  }
  rendering = false;
}

// A refresh keeps the scroll position; it only redraws the open view.
function refresh() {
  if (document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (location.hash.startsWith("#/logs") || ["#/env", "#/settings"].includes(location.hash)) return;
  if (document.body.classList.contains("setup")) return;
  const y = window.scrollY;
  render().then(() => window.scrollTo(0, y));
}

window.addEventListener("hashchange", render);
render();
