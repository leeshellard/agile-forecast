const PREF_KEY = "agile-usage-prefs";
const loadPref = () => { try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch { return {}; } };
const savePref = (p) => { try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch { /* storage unavailable */ } };

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

let report = null;

function renderMonthCost() {
  const month = $("month").value;
  const days = report.daily.filter((d) => d.date.startsWith(month));
  const x = days.map((d) => d.date);
  const sum = (k) => days.reduce((a, d) => a + (d[k] || 0), 0);
  const worst = days.reduce((a, d) => (a == null || d.cost_gbp > a.cost_gbp ? d : a), null);
  $("month-hint").innerHTML = days.length
    ? `<b>${gbp(sum("cost_gbp"))}</b> over ${days.length} day${days.length === 1 ? "" : "s"} (${gbp(sum("cost_gbp") / days.length)} a day on average), ` +
      `${gbp(sum("export_gbp"))} export income. Dearest day: ${new Date(worst.date + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })} at ${gbp(worst.cost_gbp)}.`
    : "No data for this month.";
  Plotly.react("cost-chart", [
    { type: "bar", name: "Electricity", x, y: days.map((d) => d.unit_gbp), marker: { color: css("--accent") },
      hovertemplate: "£%{y:.2f}" },
    { type: "bar", name: "Standing charge", x, y: days.map((d) => d.standing_gbp), marker: { color: css("--prev") },
      hovertemplate: "£%{y:.2f}" },
    { type: "scatter", mode: "lines+markers", name: "Flexible Octopus (same usage)", x, y: days.map((d) => d.compare_gbp),
      line: { color: css("--actual"), width: 2, dash: "dot" }, marker: { size: 4 }, hovertemplate: "£%{y:.2f}" },
    { type: "scatter", mode: "lines+markers", name: "Export income", x, y: days.map((d) => d.export_gbp),
      line: { color: css("--solar"), width: 2 }, marker: { size: 4 }, hovertemplate: "£%{y:.2f}" },
  ], baseLayout({
    barmode: "stack",
    margin: { l: 52, r: 12, t: 8, b: 40 },
    yaxis: { title: axisTitle("£ per day"), gridcolor: css("--grid"), zerolinecolor: css("--border"), rangemode: "tozero" },
    xaxis: { gridcolor: css("--grid"), linecolor: css("--border"), type: "date", tickformat: "%-d", dtick: 864e5 * (innerWidth < 600 ? 3 : 1),
             hoverformat: "%a %d %b" },
  }), plotConfig);
}

function setupMonthPicker(monthly) {
  const sel = $("month"), prev = sel.value;
  const months = monthly.map((m) => m.month);
  sel.innerHTML = [...months].reverse().map((m) => `<option value="${m}">${new Date(m + "-01T12:00:00").toLocaleDateString("en-GB", { month: "long", year: "numeric" })}</option>`).join("");
  sel.value = months.includes(prev) ? prev : months[months.length - 1];
  const step = (d) => { const i = months.indexOf(sel.value) + d; if (i >= 0 && i < months.length) { sel.value = months[i]; sync(); } };
  const sync = () => { $("month-prev").disabled = months.indexOf(sel.value) <= 0; $("month-next").disabled = months.indexOf(sel.value) >= months.length - 1; renderMonthCost(); };
  sel.onchange = sync;
  $("month-prev").onclick = () => step(-1);
  $("month-next").onclick = () => step(1);
  sync();
}

function renderDaily(daily) {
  daily = daily.slice(-120);
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

async function load() {
  const days = $("period").value;
  const r = await getJSON(`/api/usage/report?days=${days}`);
  const name = FLEX_NAME(r);
  const t = r.totals;
  $("subtitle").textContent = `${t.start} to ${t.end} · region ${r.region}`;
  renderCards(t, name);
  renderMonthly(r.monthly, name);
  renderHourly(r.hourly);
  renderDaily(r.daily);
  renderMonths(r.monthly, name);
  report = r;
  show("content");  // the chart needs a visible container to size itself
  setupMonthPicker(r.monthly);
}

async function init() {
  const prefs = loadPref();
  if (prefs.period && [...$("period").options].some((o) => o.value === prefs.period)) $("period").value = prefs.period;
  $("period").addEventListener("change", () => { savePref({ period: $("period").value }); load().catch(fail); });
  if (!(await gate())) return;
  await load();
  addEventListener("resize", () => { for (const id of ["monthly-chart", "hourly-chart", "daily-chart", "cost-chart"]) Plotly.Plots.resize(id); });
}

init().catch(fail);
