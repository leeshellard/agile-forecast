const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const dates = (arr) => arr.map((t) => new Date(t));  // Plotly renders Date objects in browser-local time

const PREF_KEY = "agile-forecast-prefs";
const controls = ["region", "days", "history", "previous", "show-range", "show-gen", "show-bands", "cheap-threshold", "acc-days"];

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch { return {}; }
}
function savePrefs() {
  const prefs = {};
  for (const id of controls) prefs[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
  try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch { /* storage unavailable */ }
}
function applyPrefs(prefs) {
  const fromUrl = new URLSearchParams(location.search).get("region");
  if (fromUrl) prefs.region = fromUrl.toUpperCase();
  for (const id of controls) {
    if (prefs[id] === undefined) continue;
    if ($(id).type === "checkbox") $(id).checked = prefs[id];
    else if (!$(id).options || [...$(id).options].some((o) => o.value === String(prefs[id]))) $(id).value = prefs[id];
  }
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

function dayTicks(el, xs) {
  // One tick per N days, with N chosen so labels (~90px each) never collide.
  const t = xs.map(Number).filter(Number.isFinite);
  const span = t.length ? (Math.max(...t) - Math.min(...t)) / 864e5 : 1;
  const n = Math.max(1, Math.ceil(span / Math.max(1, Math.floor($(el).clientWidth / 95))));
  return { dtick: n * 864e5, tick0: new Date(new Date().setHours(0, 0, 0, 0)) };
}

function baseLayout(yTitle) {
  const narrow = innerWidth < 600;
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: css("--text"), family: "system-ui, sans-serif", size: 12 },
    margin: { l: 56, r: 12, t: 8, b: 40 },
    legend: { orientation: "h", y: narrow ? -0.3 : -0.18, font: { color: css("--muted") } },
    hovermode: "x unified",
    xaxis: { gridcolor: css("--grid"), linecolor: css("--border"), tickangle: 0, automargin: true,
             tickformat: narrow ? "%a %d" : "%a %d %b", hoverformat: "%a %d %b %H:%M" },
    yaxis: { title: { text: yTitle, font: { color: css("--muted") } }, gridcolor: css("--grid"), zerolinecolor: css("--border") },
  };
}
const plotConfig = { responsive: true, displaylogo: false, displayModeBar: innerWidth >= 600 ? "hover" : false,
                     modeBarButtonsToRemove: ["lasso2d", "select2d"] };

function vLine(t) {
  return { type: "line", xref: "x", yref: "paper", x0: t, x1: t, y0: 0, y1: 1,
           line: { color: css("--muted"), width: 1, dash: "dot" } };
}

// --------------------------------------------------------------------------- viewing past days

const todayStr = () => new Date().toLocaleDateString("en-CA");  // YYYY-MM-DD in browser-local time
let viewDate = new URLSearchParams(location.search).get("date");  // null = live view / this week
let viewMode = new URLSearchParams(location.search).get("view") === "week" ? "week" : "day";

function mondayOf(str) {
  const d = new Date(`${str}T12:00:00`);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return d.toLocaleDateString("en-CA");
}
function setViewMode(mode) {
  viewMode = mode;
  setViewDate(viewDate);
}

function shiftDate(str, days) {
  const d = new Date(`${str}T12:00:00`);
  d.setDate(d.getDate() + days);
  return d.toLocaleDateString("en-CA");
}
function setViewDate(str) {
  if (viewMode === "week") viewDate = !str || mondayOf(str) >= mondayOf(todayStr()) ? null : mondayOf(str);
  else viewDate = !str || str >= todayStr() ? null : str;
  const url = new URL(location.href);
  if (viewDate) url.searchParams.set("date", viewDate); else url.searchParams.delete("date");
  if (viewMode === "week") url.searchParams.set("view", "week"); else url.searchParams.delete("view");
  history.replaceState(null, "", url);
  refreshCharts();
}
function dayWindow() {
  const start = new Date(`${viewDate || todayStr()}T00:00:00`);
  return [start, new Date(new Date(start).setDate(start.getDate() + 1))];
}

// --------------------------------------------------------------------------- price colour bands

// Copies of a price series that are only drawn where the price is below a threshold, so they sit on top
// of the main line and recolour just those stretches. `stepped` matches the Actual line's "hv" shape.
function belowOnly(ys, limit, stepped) {
  return ys.map((v, i) => {
    const prev = ys[i - 1], next = ys[i + 1];
    if (v != null && v < limit) return v;
    if (stepped) return prev != null && prev < limit ? prev : null;  // finish the step that started below
    return (prev != null && prev < limit) || (next != null && next < limit) ? v : null;
  });
}

function bandTraces(x, ys, stepped, showLegend) {
  if (!$("show-bands").checked) return [];
  const cheap = Number($("cheap-threshold").value) || 0;
  const shape = stepped ? "hv" : "linear";
  return [
    { x, y: belowOnly(ys, cheap, stepped), mode: "lines", name: `Under ${cheap}p`, legendgroup: "cheap",
      showlegend: showLegend, hoverinfo: "skip", line: { color: css("--cheap"), width: 2.5, shape } },
    { x, y: belowOnly(ys, 0, stepped), mode: "lines", name: "Negative (paid to use)", legendgroup: "neg",
      showlegend: showLegend, hoverinfo: "skip", line: { color: css("--negative"), width: 2.5, shape } },
  ];
}

function drawPrice(d) {
  const L = d.latest, traces = [];
  const fmt = (t) => new Date(t).toLocaleString([], { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });

  if (L && $("show-range").checked) {
    traces.push({ x: dates(L.ts), y: L.p90, mode: "lines", line: { width: 0 }, hoverinfo: "skip", showlegend: false });
    traces.push({ x: dates(L.ts), y: L.p10, mode: "lines", line: { width: 0 }, fill: "tonexty",
                  fillcolor: css("--range"), name: "10–90% range", hoverinfo: "skip" });
  }
  d.previous.forEach((p, i) => {
    traces.push({ x: dates(p.ts), y: p.p50, mode: "lines", name: `Forecast ${fmt(p.created_at)}`,
                  line: { color: css("--prev"), width: 1, dash: i % 2 ? "dot" : "dash" },
                  opacity: Math.max(0.35, 0.8 - i * 0.1), hovertemplate: "%{y:.1f}p" });
  });
  if (L) {
    const week = viewMode === "week";
    traces.push({ x: dates(L.ts), y: L.p50, mode: "lines",
                  name: week ? "Forecast (made the day before)" : `Forecast (${fmt(L.created_at)})`,
                  line: { color: css("--accent"), width: 2 },
                  customdata: week ? L.made.map(fmt) : undefined,
                  hovertemplate: week ? "%{y:.1f}p <span style='opacity:.6'>(made %{customdata})</span>" : "%{y:.1f}p" });
    traces.push(...bandTraces(dates(L.ts), L.p50, false, false));
  }
  traces.push({ x: dates(d.actual.ts), y: d.actual.price, mode: "lines", name: "Actual",
                line: { color: css("--actual"), width: 2, shape: "hv" }, hovertemplate: "%{y:.2f}p" });
  traces.push(...bandTraces(dates(d.actual.ts), d.actual.price, true, true));

  const layout = baseLayout("Agile price (p/kWh)");
  Object.assign(layout.xaxis, dayTicks("price-chart", [...dates(d.actual.ts), ...dates(L ? L.ts : [])]));
  layout.shapes = [];
  if (viewMode === "week") {
    if (!viewDate) layout.shapes.push(vLine(new Date()));
  } else if (viewDate) {
    const [s, e] = dayWindow();
    layout.shapes.push({ type: "rect", xref: "x", yref: "paper", x0: s, x1: e, y0: 0, y1: 1,
                         fillcolor: css("--highlight"), line: { width: 0 }, layer: "below" });
    if (L) layout.shapes.push(vLine(new Date(L.created_at)));
  } else {
    layout.shapes.push(vLine(new Date()));
  }
  if ($("show-bands").checked) {
    layout.shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: Number($("cheap-threshold").value) || 0,
                         y1: Number($("cheap-threshold").value) || 0, line: { color: css("--cheap"), width: 1, dash: "dot" },
                         opacity: 0.6 });
  }
  Plotly.react("price-chart", traces, layout, plotConfig);
}

function describeDay(d) {
  // Subtitle for the past-day view: which forecast is shown and how close it was.
  if (!viewDate) return "";
  if (!d.latest) {
    const since = d.first_forecast ? new Date(d.first_forecast).toLocaleDateString([], { dateStyle: "medium" }) : "the first update";
    return `Actual prices only — no forecast was stored for this day (forecasts are kept from ${since}).`;
  }
  const [s, e] = dayWindow();
  const actual = new Map(d.actual.ts.map((t, i) => [t, d.actual.price[i]]));
  const errs = d.latest.ts.map((t, i) => {
    const when = new Date(t), a = actual.get(t), p = d.latest.p50[i];
    return when >= s && when < e && a != null && p != null ? Math.abs(p - a) : null;
  }).filter((v) => v != null);
  const made = new Date(d.latest.created_at).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const err = errs.length ? ` · average error for this day <b>${(errs.reduce((a, b) => a + b, 0) / errs.length).toFixed(2)}p/kWh</b>` : "";
  return `Forecast made ${made}, before this day's prices were published${err}`;
}

function drawWeekTable(d) {
  const cheap = Number($("cheap-threshold").value) || 0;
  $("under-head").textContent = `Slots under ${cheap}p`;
  const byDay = {};
  d.actual.ts.forEach((t, i) => {
    const key = new Date(t).toLocaleDateString("en-CA");
    (byDay[key] ||= []).push(d.actual.price[i]);
  });
  const time = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("week-rows").innerHTML = d.days.map((day) => {
    const prices = byDay[day.date] || [];
    const label = new Date(`${day.date}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
    const under = prices.filter((p) => p < cheap).length, neg = prices.filter((p) => p < 0).length;
    const min = day.actual_min == null ? "<span class=muted>—</span>"
      : `<span class="${day.actual_min < 0 ? "neg" : day.actual_min < cheap ? "cheap" : ""}">${day.actual_min.toFixed(1)}p</span> at ${time(day.actual_min_at)}`;
    const slots = !prices.length ? "<span class=muted>—</span>"
      : `${under ? `<span class=cheap>${under}</span>` : 0}${neg ? ` <span class=neg>(${neg} negative)</span>` : ""}`;
    const err = day.forecast_mae != null ? `${day.forecast_mae.toFixed(2)}p`
      : `<span class=muted>${day.forecast_made && !prices.length ? "not yet priced" : "no forecast"}</span>`;
    return `<tr data-date="${day.date}"><td>${label}</td><td>${day.actual_mean == null ? "<span class=muted>—</span>" : day.actual_mean.toFixed(1) + "p"}</td>` +
           `<td>${min}</td><td>${slots}</td><td>${err}</td></tr>`;
  }).join("");
}

function describeWeek(d) {
  const f = (s) => new Date(`${s}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  const range = `${f(d.days[0].date)} – ${f(d.days[6].date)}`;
  if (!d.latest) return `${range} · actual prices only — no forecasts were stored for this week.`;
  const err = d.week_mae != null ? ` · average error <b>${d.week_mae.toFixed(2)}p/kWh</b>` : "";
  return `${range} · each day compared with the forecast made the day before${err}. Click a day for detail.`;
}

function drawGen(d) {
  const L = d.latest || { ts: [], demand: [], wind: [], emb_wind: [], emb_solar: [] }, A = d.gen_actual;
  const gw = (arr) => arr.map((v) => (v == null ? null : v / 1000));
  const lastActual = A.ts.length ? new Date(A.ts[A.ts.length - 1]) : null;
  const series = [
    ["emb_solar", "Embedded solar", "--solar"],
    ["emb_wind", "Embedded wind", "--emb-wind"],
    ["wind", "Metered wind", "--wind"],
  ];
  const traces = [];
  // Stacked areas: actuals, then forecast values for periods after the last actual. They are separate
  // stacks so Plotly doesn't draw a made-up ramp across any gap between the two.
  const keep = L.ts.map((t) => !lastActual || new Date(t) > lastActual);
  const later = (arr) => arr.filter((_, i) => keep[i]);
  for (const [group, x, pick] of [["act", dates(A.ts), (k) => gw(A[k])], ["fc", later(dates(L.ts)), (k) => later(gw(L[k]))]]) {
    for (const [key, name, color] of series) {
      traces.push({ x, y: pick(key), name, legendgroup: key, showlegend: group === "act" || !A.ts.length,
                    stackgroup: group, mode: "lines", line: { width: 0.5, color: css(color) },
                    fillcolor: css(color) + "b3", hovertemplate: "%{y:.1f} GW" });
    }
  }
  traces.push({ x: dates(A.ts), y: gw(A.demand), name: "Demand (actual)", mode: "lines",
                line: { color: css("--demand"), width: 2 }, hovertemplate: "%{y:.1f} GW" });
  if (L.ts.length) {
    traces.push({ x: dates(L.ts), y: gw(L.demand), name: "Demand (forecast)", mode: "lines",
                  line: { color: css("--demand"), width: 2, dash: "dot" }, hovertemplate: "%{y:.1f} GW" });
  }
  const layout = baseLayout("Power (GW)");
  Object.assign(layout.xaxis, dayTicks("gen-chart", [...dates(A.ts), ...dates(L.ts)]));
  layout.shapes = viewDate ? [] : [vLine(new Date())];  // "now" only when the view includes today
  Plotly.react("gen-chart", traces, layout, plotConfig);
}

async function refreshCharts() {
  savePrefs();
  const region = $("region").value;
  const name = $("region").selectedOptions[0]?.dataset.name || region;
  const week = viewMode === "week";
  const dayLabel = viewDate ? new Date(`${viewDate}T12:00:00`).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "";
  $("title").textContent = week ? `${name} — ${viewDate ? "week of " + dayLabel.replace(/^\w+,? /, "") : "this week"}`
                                : viewDate ? `${name} — ${dayLabel}` : name;
  $("view-date").value = viewDate || (week ? mondayOf(todayStr()) : todayStr());
  $("view-date").max = todayStr();
  $("next-day").disabled = !viewDate;
  $("today").hidden = !viewDate;
  $("today").textContent = week ? "Back to this week" : "Back to today";
  $("prev-day").setAttribute("aria-label", week ? "Previous week" : "Previous day");
  $("next-day").setAttribute("aria-label", week ? "Next week" : "Next day");
  document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === viewMode));
  for (const id of ["days", "history", "previous"]) $(id).disabled = week;  // the week view has a fixed window
  $("week-table").hidden = !week;
  $("gen-wrap").hidden = !$("show-gen").checked;
  let url;
  if (week) {
    url = `/api/${region}/week${viewDate ? `?start=${viewDate}` : ""}`;
  } else {
    url = `/api/${region}/chart?days=${$("days").value}&history_days=${$("history").value}&previous=${$("previous").value}`;
    if (viewDate) url += `&date=${viewDate}`;
  }
  try {
    const d = await getJSON(url);
    $("subtitle").innerHTML = week ? describeWeek(d) : describeDay(d);
    if (week) drawWeekTable(d);
    drawPrice(d);
    if ($("show-gen").checked) drawGen(d);
  } catch (e) {
    $("subtitle").textContent = "";
    Plotly.purge("price-chart");
    $("price-chart").innerHTML = `<div class="empty">${e.message}</div>`;
  }
}

async function refreshAccuracy() {
  savePrefs();
  const a = await getJSON(`/api/${$("region").value}/accuracy?days=${$("acc-days").value}`);
  if (!a.by_horizon.length) {
    $("acc-summary").textContent = "No forecasts can be scored yet. Accuracy appears once published prices overlap past forecasts.";
    Plotly.purge("acc-chart");
    $("acc-chart").hidden = true;
    $("acc-runs").innerHTML = "";
    return;
  }
  $("acc-chart").hidden = false;
  $("acc-summary").innerHTML = `Mean absolute error across all scored periods: <b>${a.overall_mae.toFixed(2)}p/kWh</b>`;
  const x = a.by_horizon.map((h) => h.days_ahead);
  const layout = baseLayout("MAE (p/kWh)");
  layout.xaxis = { ...layout.xaxis, title: { text: "Days ahead", font: { color: css("--muted") } }, tickformat: "d", dtick: 1 };
  layout.yaxis2 = { overlaying: "y", side: "right", range: [0, 1], tickformat: ".0%", showgrid: false,
                    title: { text: "Actual inside 10–90% range", font: { color: css("--muted") } } };
  layout.margin.r = 56;
  Plotly.react("acc-chart", [
    { x, y: a.by_horizon.map((h) => h.mae), type: "bar", name: "MAE", marker: { color: css("--accent") },
      customdata: a.by_horizon.map((h) => h.n), hovertemplate: "%{y:.2f}p (n=%{customdata})<extra></extra>" },
    { x, y: a.by_horizon.map((h) => h.coverage), yaxis: "y2", mode: "lines+markers", name: "Range coverage",
      line: { color: css("--good") }, hovertemplate: "%{y:.0%}<extra></extra>" },
  ], layout, plotConfig);
  $("acc-runs").innerHTML = a.runs.map((r) =>
    `<tr><td>${new Date(r.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</td>` +
    `<td>${r.n}</td><td>${r.mae.toFixed(2)}</td></tr>`).join("");
}

async function refreshStatus() {
  const s = await getJSON("/api/status");
  if (!s.ready) { $("status").textContent = "Waiting for the first forecast run…"; return; }
  const run = new Date(s.latest_run.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  $("status").innerHTML = `Updated ${run}<br>Model holdout error ${s.latest_run.holdout_mae}p/kWh`;
}

async function init() {
  const regions = await getJSON("/api/regions");
  $("region").innerHTML = regions.map((r) => `<option value="${r.region}" data-name="${r.name}">${r.region} – ${r.name}</option>`).join("");
  $("days").innerHTML = Array.from({ length: 14 }, (_, i) => `<option${i === 6 ? " selected" : ""}>${i + 1}</option>`).join("");
  applyPrefs(loadPrefs());

  for (const id of ["days", "history", "previous", "show-range", "show-gen", "show-bands", "cheap-threshold"]) $(id).addEventListener("change", refreshCharts);
  $("view-date").addEventListener("change", () => setViewDate($("view-date").value));
  const step = () => (viewMode === "week" ? 7 : 1);
  $("prev-day").addEventListener("click", () => setViewDate(shiftDate(viewDate || todayStr(), -step())));
  $("next-day").addEventListener("click", () => viewDate && setViewDate(shiftDate(viewDate, step())));
  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => setViewMode(b.dataset.mode)));
  $("week-rows").addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-date]");
    if (row) { viewMode = "day"; setViewDate(row.dataset.date); }
  });
  $("today").addEventListener("click", () => setViewDate(null));
  $("region").addEventListener("change", () => { refreshCharts(); refreshAccuracy(); });
  $("acc-days").addEventListener("change", refreshAccuracy);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { refreshCharts(); refreshAccuracy(); });

  await Promise.all([refreshStatus(), refreshCharts(), refreshAccuracy()]);
  setInterval(() => { refreshStatus(); if (!viewDate) refreshCharts(); refreshAccuracy(); }, 15 * 60 * 1000);
}

init();
