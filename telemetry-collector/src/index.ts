/**
 * Cloudflare Worker: receives Minion `analytics_remote` POSTs and serves an operator dashboard.
 * Contract: POST /v1/collect — JSON bodies from chatgpt_mcp_memory/src/analytics_remote.py
 */

export interface Env {
  DB: D1Database;
  /** Set with `wrangler secret put DASHBOARD_TOKEN` */
  DASHBOARD_TOKEN: string;
}

type CollectBody = Record<string, unknown>;

const MAX_BODY = 48_000;

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

async function sha256Prefix16(s: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(digest).slice(0, 8))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function asBool(v: unknown): number | null {
  if (v === true) return 1;
  if (v === false) return 0;
  return null;
}

function asInt(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
  if (typeof v === "string" && v.trim() && /^-?\d+$/.test(v.trim())) return parseInt(v, 10);
  return null;
}

function validateEvent(body: CollectBody): string | null {
  if (body.schema !== 1) return "schema";
  const ev = body.event;
  if (ev !== "session" && ev !== "search" && ev !== "ingest") return "event";
  return null;
}

async function handleCollect(request: Request, env: Env): Promise<Response> {
  const ct = request.headers.get("content-type") || "";
  if (!ct.includes("application/json")) {
    return json({ error: "expected application/json" }, 415);
  }
  const raw = await request.text();
  if (raw.length > MAX_BODY) {
    return json({ error: "body too large" }, 413);
  }
  let body: CollectBody;
  try {
    body = JSON.parse(raw) as CollectBody;
  } catch {
    return json({ error: "invalid json" }, 400);
  }
  const bad = validateEvent(body);
  if (bad) {
    return json({ error: `invalid ${bad}` }, 400);
  }

  const receivedAt = Date.now();
  const event = body.event as string;
  const installId = typeof body.install_id === "string" ? body.install_id : "";
  const installBucket = installId ? await sha256Prefix16(installId) : null;
  const appVersion = typeof body.app_version === "string" ? body.app_version.slice(0, 64) : null;
  const os = typeof body.os === "string" ? body.os.slice(0, 64) : null;
  const arch = typeof body.arch === "string" ? body.arch.slice(0, 64) : null;
  const python = typeof body.python === "string" ? body.python.slice(0, 32) : null;

  let mode: string | null = null;
  let rerank: string | null = null;
  let returned: number | null = null;
  let topK: number | null = null;
  let hasKindFilter: number | null = null;
  let hasPathGlob: number | null = null;
  let hasRoleFilter: number | null = null;
  let hasQuery: number | null = null;
  let hitKinds: string | null = null;
  let fileKind: string | null = null;
  let parser: string | null = null;
  let chunks: number | null = null;
  let skipped: number | null = null;
  let result: string | null = null;
  let reasonClass: string | null = null;

  if (event === "search") {
    mode = typeof body.mode === "string" ? body.mode.slice(0, 64) : null;
    rerank = typeof body.rerank === "string" ? body.rerank.slice(0, 32) : null;
    returned = asInt(body.returned);
    topK = asInt(body.top_k);
    hasKindFilter = asBool(body.has_kind_filter);
    hasPathGlob = asBool(body.has_path_glob);
    hasRoleFilter = asBool(body.has_role_filter);
    hasQuery = asBool(body.has_query);
    if (Array.isArray(body.hit_kinds)) {
      try {
        hitKinds = JSON.stringify(body.hit_kinds.slice(0, 32).map((x) => String(x).slice(0, 48)));
      } catch {
        hitKinds = "[]";
      }
    }
  } else if (event === "ingest") {
    fileKind = typeof body.file_kind === "string" ? body.file_kind.slice(0, 64) : null;
    parser = typeof body.parser === "string" ? body.parser.slice(0, 128) : null;
    chunks = asInt(body.chunks);
    skipped = asBool(body.skipped);
    result = typeof body.result === "string" ? body.result.slice(0, 64) : null;
    reasonClass = typeof body.reason_class === "string" ? body.reason_class.slice(0, 96) : null;
  }

  await env.DB.prepare(
    `INSERT INTO telemetry_row (
      received_at, event, install_bucket, app_version, os, arch, python,
      mode, rerank, returned, top_k, has_kind_filter, has_path_glob, has_role_filter, has_query, hit_kinds,
      file_kind, parser, chunks, skipped, result, reason_class
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  )
    .bind(
      receivedAt,
      event,
      installBucket,
      appVersion,
      os,
      arch,
      python,
      mode,
      rerank,
      returned,
      topK,
      hasKindFilter,
      hasPathGlob,
      hasRoleFilter,
      hasQuery,
      hitKinds,
      fileKind,
      parser,
      chunks,
      skipped,
      result,
      reasonClass,
    )
    .run();

  return json({ ok: true }, 200);
}

function extractDashboardToken(request: Request): string | null {
  const auth = request.headers.get("Authorization");
  if (auth?.startsWith("Bearer ")) return auth.slice(7).trim();
  const h = request.headers.get("X-Minion-Dashboard-Token");
  if (h) return h.trim();
  return null;
}

async function handleSummary(request: Request, env: Env): Promise<Response> {
  const expected = (env.DASHBOARD_TOKEN || "").trim();
  if (!expected) {
    return json({ error: "DASHBOARD_TOKEN not configured on worker" }, 503);
  }
  const got = extractDashboardToken(request);
  if (!got || got !== expected) {
    return json({ error: "unauthorized" }, 401);
  }

  const now = Date.now();
  const d7 = now - 7 * 86400000;
  const d30 = now - 30 * 86400000;

  const countRow = await env.DB.prepare(`SELECT COUNT(*) as n FROM telemetry_row`).first<{ n: number }>();
  const allRows = Number(countRow?.n ?? 0);

  const totals7d = await env.DB.prepare(
    `SELECT event, COUNT(*) as n FROM telemetry_row WHERE received_at >= ? GROUP BY event ORDER BY n DESC`,
  )
    .bind(d7)
    .all<{ event: string; n: number }>();

  const totals30d = await env.DB.prepare(
    `SELECT event, COUNT(*) as n FROM telemetry_row WHERE received_at >= ? GROUP BY event ORDER BY n DESC`,
  )
    .bind(d30)
    .all<{ event: string; n: number }>();

  const byDay = await env.DB.prepare(
    `SELECT strftime('%Y-%m-%d', received_at / 1000, 'unixepoch') AS day, event, COUNT(*) AS n
     FROM telemetry_row WHERE received_at >= ?
     GROUP BY 1, 2 ORDER BY 1 ASC, 2 ASC`,
  )
    .bind(d7)
    .all<{ day: string; event: string; n: number }>();

  const dauByDay = await env.DB.prepare(
    `SELECT strftime('%Y-%m-%d', received_at / 1000, 'unixepoch') AS day,
            COUNT(DISTINCT install_bucket) AS n
     FROM telemetry_row
     WHERE received_at >= ? AND install_bucket IS NOT NULL AND install_bucket != ''
     GROUP BY 1 ORDER BY 1 ASC`,
  )
    .bind(d7)
    .all<{ day: string; n: number }>();

  const versions = await env.DB.prepare(
    `SELECT app_version AS v, COUNT(*) AS n FROM telemetry_row
     WHERE received_at >= ? AND app_version IS NOT NULL AND app_version != ''
     GROUP BY app_version ORDER BY n DESC LIMIT 16`,
  )
    .bind(d7)
    .all<{ v: string; n: number }>();

  const platforms = await env.DB.prepare(
    `SELECT os, COUNT(*) AS n FROM telemetry_row
     WHERE received_at >= ? AND os IS NOT NULL AND os != ''
     GROUP BY os ORDER BY n DESC LIMIT 12`,
  )
    .bind(d7)
    .all<{ os: string; n: number }>();

  const ingestResults = await env.DB.prepare(
    `SELECT result, COUNT(*) AS n FROM telemetry_row
     WHERE received_at >= ? AND event = 'ingest' AND result IS NOT NULL
     GROUP BY result ORDER BY n DESC LIMIT 20`,
  )
    .bind(d7)
    .all<{ result: string; n: number }>();

  return json({
    generated_at_ms: now,
    all_time_rows: allRows,
    last_7d: {
      totals: totals7d.results,
      by_day: byDay.results,
      approx_distinct_installers_by_day: dauByDay.results,
      top_versions: versions.results,
      top_os: platforms.results,
      ingest_results: ingestResults.results,
    },
    last_30d_totals: totals30d.results,
  });
}

function dashboardHtml(): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Minion telemetry</title>
  <style>
    :root { color-scheme: dark; --bg: #0f1115; --fg: #e8eaed; --muted: #9aa0a6; --accent: #8ab4f8; --card: #1a1d24; --border: #2d323c; }
    * { box-sizing: border-box; }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--fg); margin: 0; padding: 1.25rem; line-height: 1.45; }
    h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.5rem; }
    p.sub { color: var(--muted); margin: 0 0 1rem; font-size: 0.9rem; }
    .row { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: flex-end; margin-bottom: 1rem; }
    label { display: flex; flex-direction: column; gap: 0.35rem; font-size: 0.8rem; color: var(--muted); }
    input[type="password"] { width: min(28rem, 100%); padding: 0.5rem 0.65rem; border-radius: 6px; border: 1px solid var(--border); background: #000; color: var(--fg); }
    button { padding: 0.5rem 1rem; border-radius: 6px; border: 1px solid var(--border); background: var(--card); color: var(--fg); cursor: pointer; }
    button:hover { border-color: var(--accent); }
    .grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 1rem; }
    .card h2 { margin: 0 0 0.75rem; font-size: 0.95rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th, td { text-align: left; padding: 0.35rem 0.25rem; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-weight: 500; }
    .bar-wrap { height: 6px; background: #000; border-radius: 3px; margin-top: 0.15rem; overflow: hidden; }
    .bar { height: 100%; background: var(--accent); border-radius: 3px; }
    pre.err { color: #f28b82; white-space: pre-wrap; font-size: 0.85rem; }
    .muted { color: var(--muted); font-size: 0.8rem; }
  </style>
</head>
<body>
  <h1>Minion usage</h1>
  <p class="sub">Last 7 days (and all-time row count). Token is your worker secret <code>DASHBOARD_TOKEN</code> — never share a URL that embeds it.</p>
  <div class="row">
    <label>Dashboard token
      <input type="password" id="tok" placeholder="Paste token" autocomplete="off"/>
    </label>
    <button type="button" id="go">Load</button>
  </div>
  <p class="muted" id="hint">Token is kept in <code>sessionStorage</code> for this tab only.</p>
  <pre class="err" id="err" hidden></pre>
  <div id="out" class="grid" hidden></div>
  <script>
(function () {
  const KEY = "minion_dash_token";
  const inp = document.getElementById("tok");
  const go = document.getElementById("go");
  const err = document.getElementById("err");
  const out = document.getElementById("out");
  try {
    const s = sessionStorage.getItem(KEY);
    if (s) inp.value = s;
  } catch (_) {}
  function showError(msg) {
    err.textContent = msg || "";
    err.hidden = !msg;
  }
  function maxN(rows) {
    let m = 1;
    for (const r of rows || []) m = Math.max(m, r.n || 0);
    return m;
  }
  function table(title, rows, k, label) {
    const m = maxN(rows);
    const card = document.createElement("div");
    card.className = "card";
    const h = document.createElement("h2");
    h.textContent = title;
    card.appendChild(h);
    const tbl = document.createElement("table");
    const head = document.createElement("tr");
    head.innerHTML = "<th>" + label + "</th><th style='text-align:right'>n</th><th></th>";
    tbl.appendChild(head);
    for (const r of rows || []) {
      const tr = document.createElement("tr");
      const pct = Math.round(((r.n || 0) / m) * 100);
      tr.innerHTML = "<td>" + String(r[k] ?? "").replace(/</g, "&lt;") + "</td><td style='text-align:right'>" + r.n + "</td><td style='width:38%'><div class='bar-wrap'><div class='bar' style='width:" + pct + "%'></div></div></td>";
      tbl.appendChild(tr);
    }
    card.appendChild(tbl);
    return card;
  }
  async function load() {
    showError("");
    out.hidden = true;
    out.innerHTML = "";
    const token = inp.value.trim();
    if (!token) { showError("Enter token"); return; }
    try { sessionStorage.setItem(KEY, token); } catch (_) {}
    const r = await fetch("/v1/summary", { headers: { "Authorization": "Bearer " + token } });
    const j = await r.json().catch(function () { return {}; });
    if (!r.ok) {
      showError(j.error || ("HTTP " + r.status));
      return;
    }
    const t7 = j.last_7d || {};
    const totals = t7.totals || [];
    const byDay = t7.by_day || [];
    const dau = t7.approx_distinct_installers_by_day || [];
    const meta = document.createElement("div");
    meta.className = "card";
    meta.innerHTML = "<h2>Overview</h2><p>All-time rows: <strong>" + (j.all_time_rows ?? "—") + "</strong></p><p class='muted'>Generated " + new Date(j.generated_at_ms).toISOString() + "</p>";
    out.appendChild(meta);
    out.appendChild(table("Events (7d)", totals, "event", "event"));
    out.appendChild(table("Top versions (7d)", t7.top_versions || [], "v", "version"));
    out.appendChild(table("Top OS (7d)", t7.top_os || [], "os", "os"));
    out.appendChild(table("Ingest results (7d)", t7.ingest_results || [], "result", "result"));
    out.appendChild(table("≈ distinct installers / day", dau, "day", "day"));
    const dayCard = document.createElement("div");
    dayCard.className = "card";
    dayCard.innerHTML = "<h2>Volume by day + event</h2>";
    const tbl = document.createElement("table");
    tbl.innerHTML = "<tr><th>day</th><th>event</th><th style='text-align:right'>n</th></tr>";
    const m = maxN(byDay);
    for (const r of byDay) {
      const pct = Math.round(((r.n || 0) / m) * 100);
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>" + r.day + "</td><td>" + String(r.event).replace(/</g, "&lt;") + "</td><td style='text-align:right'>" + r.n + "</td>";
      const td = document.createElement("td");
      td.style.width = "35%";
      td.innerHTML = "<div class='bar-wrap'><div class='bar' style='width:" + pct + "%'></div></div>";
      tr.appendChild(td);
      tbl.appendChild(tr);
    }
    dayCard.appendChild(tbl);
    out.appendChild(dayCard);
    const t30 = document.createElement("div");
    t30.className = "card";
    t30.innerHTML = "<h2>Events (30d)</h2>";
    const t30tbl = document.createElement("table");
    t30tbl.innerHTML = "<tr><th>event</th><th style='text-align:right'>n</th></tr>";
    for (const r of (j.last_30d_totals || [])) {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>" + String(r.event).replace(/</g, "&lt;") + "</td><td style='text-align:right'>" + r.n + "</td>";
      t30tbl.appendChild(tr);
    }
    t30.appendChild(t30tbl);
    out.appendChild(t30);
    out.hidden = false;
  }
  go.addEventListener("click", function () { load().catch(function (e) { showError(String(e)); }); });
  if (inp.value) load().catch(function () {});
})();
  </script>
</body>
</html>`;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/$/, "") || "/";

    if (path === "/v1/collect" && request.method === "POST") {
      try {
        return await handleCollect(request, env);
      } catch (e) {
        return json({ error: "collect failed", detail: String(e) }, 500);
      }
    }

    if (path === "/v1/summary" && request.method === "GET") {
      try {
        return await handleSummary(request, env);
      } catch (e) {
        return json({ error: "summary failed", detail: String(e) }, 500);
      }
    }

    if (path === "/" && request.method === "GET") {
      return new Response(dashboardHtml(), {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
      });
    }

    return new Response("Not found", { status: 404 });
  },
};
