const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw Object.assign(new Error((await r.json().catch(() => ({}))).detail || r.statusText), { status: r.status });
  return r.json();
}

const gbp = (v) => (v == null ? "–" : (v < 0 ? "−£" : "£") + Math.abs(v).toLocaleString("en-GB", { maximumFractionDigits: v >= 100 ? 0 : 2, minimumFractionDigits: v >= 100 ? 0 : 2 }));
const num = (v, d = 0) => (v == null ? "–" : v.toLocaleString("en-GB", { maximumFractionDigits: d, minimumFractionDigits: d }));
const pct = (v) => (v == null ? "–" : Math.round(v * 100) + "%");
const monthName = (m) => new Date(m + "-01T12:00:00").toLocaleDateString("en-GB", { month: "short", year: "2-digit" });

function baseLayout(extra = {}) {
  const narrow = innerWidth < 600;
  return {
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: css("--text"), family: "system-ui, sans-serif", size: 12 },
    margin: { l: 52, r: 52, t: 8, b: 40 },
    legend: { orientation: "h", y: narrow ? -0.35 : -0.2, font: { color: css("--muted") } },
    hovermode: "x unified",
    xaxis: { gridcolor: css("--grid"), linecolor: css("--border"), automargin: true },
    yaxis: { gridcolor: css("--grid"), zerolinecolor: css("--border") },
    ...extra,
  };
}
const axisTitle = (text) => ({ text, font: { color: css("--muted") } });
const plotConfig = { responsive: true, displaylogo: false, displayModeBar: false };

function card(label, value, sub, cls = "") {
  return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="sub">${sub || "&nbsp;"}</div></div>`;
}

// Shared page states for the personal pages: setup instructions, "no data yet", or the content itself.
function show(which) {
  for (const id of ["setup", "empty", "content"]) $(id).hidden = id !== which;
}

function fail(e) {
  $("empty-msg").textContent = e.message;
  show("empty");
}

// Resolves true when the page can load its data; otherwise shows the right message and resolves false.
async function gate() {
  const st = await getJSON("/api/usage/status");
  if (!st.configured) { $("status").textContent = "Not set up"; show("setup"); return false; }
  $("status").textContent = st.last_sync
    ? `Synced ${new Date(st.last_sync).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })}`
    : "Not synced yet";
  if (!st.synced) {
    $("empty-msg").innerHTML = "No readings downloaded yet. Run <code>systemctl start agile-forecast-update.service</code> on the server (or <code>python -m app.usage</code>), then reload.";
    show("empty");
    return false;
  }
  return true;
}

const FLEX_NAME = (r) => (r.compare_tariff && r.compare_tariff.includes("VAR-22-11-01") ? "Flexible Octopus" : "comparison tariff");
