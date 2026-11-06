const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const PREF_KEY = "agile-usage-prefs";
const loadPref = () => { try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch { return {}; } };
const savePref = (p) => { try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch { /* storage unavailable */ } };

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

function renderCards(t, cmpName) {
  const saving = t.saving_gbp;
  $("cards").innerHTML = [
    card("Energy used", `${num(t.import_kwh)} kWh`, `${num(t.import_kwh / t.days, 1)} kWh a day over ${t.days} days`),
    card("Cost incl. standing charge", gbp(t.all_in_gbp), `${gbp(t.unit_gbp)} units + ${gbp(t.standing_gbp)} standing`),
    card("Average unit price", `${num(t.avg_unit_p, 1)}p`, `${num(t.all_in_p, 1)}p with standing charge`),
    card("Export income", gbp(t.export_gbp), `${num(t.export_kwh)} kWh exported`),
    card("Net cost after export", gbp(t.net_gbp), ""),
    card(`vs ${cmpName}`, (saving >= 0 ? "Saved " : "Paid ") + gbp(Math.abs(saving)),
         `${cmpName} would have been ${gbp(t.compare_all_in_gbp)} (${num(t.compare_all_in_p, 1)}p)`, saving >= 0 ? "good" : ""),
    card("Bought under 15p", pct(t.cheap_share), `Over 30p: ${pct(t.expensive_share)}`),
    card("Peak 4–7pm share", pct(t.peak_share), t.peak_avg_p == null ? "" : `Paid ${num(t.peak_avg_p, 1)}p on average then`),
  ].join("");
}

function renderMonthly(monthly, cmpName) {
  const x = monthly.map((m) => monthName(m.month));
  const layout = baseLayout({
    barmode: "group",
    yaxis: { title: axisTitle("£ per month"), gridcolor: css("--grid"), zerolinecolor: css("--border"), rangemode: "tozero" },
    yaxis2: { title: axisTitle("p/kWh"), overlaying: "y", side: "right", showgrid: false, rangemode: "tozero" },
  });
  Plotly.react("monthly-chart", [
    { type: "bar", name: "Your cost", x, y: monthly.map((m) => m.cost_gbp), marker: { color: css("--accent") },
      hovertemplate: "£%{y:.2f}" },
    { type: "bar", name: `${cmpName} (same usage)`, x, y: monthly.map((m) => m.compare_gbp), marker: { color: css("--prev") },
      hovertemplate: "£%{y:.2f}" },
    { type: "scatter", mode: "lines+markers", name: "Avg unit price (p)", x, y: monthly.map((m) => m.avg_p),
      yaxis: "y2", line: { color: css("--actual"), width: 2 }, hovertemplate: "%{y:.1f}p" },
  ], layout, plotConfig);
}

function renderHourly(hourly) {
  const x = hourly.map((h) => String(h.hour).padStart(2, "0") + ":00");
  Plotly.react("hourly-chart", [
    { type: "bar", name: "Average demand (kW)", x, y: hourly.map((h) => h.kw), marker: { color: css("--demand") },
      hovertemplate: "%{y:.2f} kW" },
    { type: "scatter", mode: "lines", name: "Price you paid (p)", x, y: hourly.map((h) => h.paid_p), yaxis: "y2",
      line: { color: css("--accent"), width: 2 }, hovertemplate: "%{y:.1f}p" },
    { type: "scatter", mode: "lines", name: "Average Agile price (p)", x, y: hourly.map((h) => h.market_p), yaxis: "y2",
      line: { color: css("--muted"), width: 2, dash: "dot" }, hovertemplate: "%{y:.1f}p" },
  ], baseLayout({
    yaxis: { title: axisTitle("kW"), gridcolor: css("--grid"), rangemode: "tozero" },
    yaxis2: { title: axisTitle("p/kWh"), overlaying: "y", side: "right", showgrid: false, rangemode: "tozero" },
    xaxis: { gridcolor: css("--grid"), linecolor: css("--border"), tickangle: 0, dtick: innerWidth < 600 ? 4 : 2 },
  }), plotConfig);
}

function renderDaily(daily) {
  const x = daily.map((d) => d.date);
  Plotly.react("daily-chart", [
    { type: "bar", name: "Used (kWh)", x, y: daily.map((d) => d.kwh), marker: { color: css("--demand") },
      hovertemplate: "%{y:.1f} kWh" },
    { type: "scatter", mode: "lines", name: "Avg unit price (p)", x, y: daily.map((d) => d.avg_p), yaxis: "y2",
      line: { color: css("--accent"), width: 2 }, hovertemplate: "%{y:.1f}p" },
  ], baseLayout({
    yaxis: { title: axisTitle("kWh"), gridcolor: css("--grid"), rangemode: "tozero" },
    yaxis2: { title: axisTitle("p/kWh"), overlaying: "y", side: "right", showgrid: false, rangemode: "tozero" },
    xaxis: { gridcolor: css("--grid"), linecolor: css("--border"), type: "date", tickformat: "%d %b" },
  }), plotConfig);
}

function renderMonths(monthly, cmpName) {
  $("cmp-head").textContent = `${cmpName} (£)`;
  $("month-rows").innerHTML = [...monthly].reverse().map((m) =>
    `<tr><td>${monthName(m.month)}</td><td>${num(m.kwh)}</td><td>${num(m.avg_p, 1)}</td><td>${gbp(m.cost_gbp)}</td>` +
    `<td>${gbp(m.compare_gbp)}</td><td>${num(m.export_kwh)}</td><td>${gbp(m.export_gbp)}</td></tr>`).join("");
}

function show(which) {
  for (const id of ["setup", "empty", "content"]) $(id).hidden = id !== which;
}

async function load() {
  const days = $("period").value;
  const r = await getJSON(`/api/usage/report?days=${days}`);
  const name = r.compare_tariff.includes("VAR-22-11-01") ? "Flexible Octopus" : "comparison tariff";
  const t = r.totals;
  $("subtitle").textContent = `${t.start} to ${t.end} · region ${r.region}`;
  renderCards(t, name);
  renderMonthly(r.monthly, name);
  renderHourly(r.hourly);
  renderDaily(r.daily);
  renderMonths(r.monthly, name);
  show("content");
}

async function init() {
  const prefs = loadPref();
  if (prefs.period && [...$("period").options].some((o) => o.value === prefs.period)) $("period").value = prefs.period;
  $("period").addEventListener("change", () => { savePref({ period: $("period").value }); load().catch(fail); });
  const st = await getJSON("/api/usage/status");
  if (!st.configured) { $("status").textContent = "Not set up"; return show("setup"); }
  $("status").textContent = st.last_sync ? `Synced ${new Date(st.last_sync).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })}` : "Not synced yet";
  if (!st.synced) {
    $("empty-msg").innerHTML = "No readings downloaded yet. Run <code>systemctl start agile-forecast-update.service</code> on the server (or <code>python -m app.usage</code>), then reload.";
    return show("empty");
  }
  await load();
  addEventListener("resize", () => { for (const id of ["monthly-chart", "hourly-chart", "daily-chart"]) Plotly.Plots.resize(id); });
}

function fail(e) {
  $("empty-msg").textContent = e.message;
  show("empty");
}

init().catch(fail);
