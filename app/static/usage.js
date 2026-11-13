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

// ---------------------------------------------------------------- this year vs last year
// Uses every day of data (not the period picker): the latest 12 months against the same months a year earlier.
// Each month only counts the days present in both years, so a part month (this month so far, or the first
// month of readings) is compared like for like.
let yoyDaily = null;

function yoyData(daily) {
  const byDate = new Map(daily.map((d) => [d.date, d]));
  const last = daily[daily.length - 1].date.slice(0, 7);
  const shift = (ym, n) => { const d = new Date(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
  const rows = [];
  for (let i = 11; i >= 0; i--) {
    const m = shift(last, -i), p = shift(m, -12);
    const inMonth = daily.filter((d) => d.date.startsWith(m));
    if (!inMonth.length) continue;
    const pairs = inMonth.map((d) => [d, byDate.get(p + d.date.slice(7))]).filter(([, q]) => q);
    const use = pairs.length ? pairs : inMonth.map((d) => [d, null]);
    const sum = (k, j) => use.reduce((a, pr) => a + (pr[j] ? pr[j][k] || 0 : 0), 0);
    const monthDays = new Date(+m.slice(0, 4), +m.slice(5, 7), 0).getDate();
    const prior = pairs.length > 0;
    rows.push({
      month: m, prev: p, days: use.length, monthDays, prior,
      kwh: sum("kwh", 0), cost: sum("cost_gbp", 0),
      pkwh: prior ? sum("kwh", 1) : null, pcost: prior ? sum("cost_gbp", 1) : null,
      firstDay: +use[0][0].date.slice(8), lastDay: +use[use.length - 1][0].date.slice(8),
    });
  }
  return rows;
}

const change = (a, b) => (a == null || b == null || !b ? null : (a - b) / b);
const changeCell = (c) => {
  if (c == null) return "<td>–</td>";
  const n = Math.round(c * 100);
  return `<td class="${n < 0 ? "down" : n > 0 ? "up" : ""}">${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n)}%</td>`;
};
const shortMonth = (m) => new Date(m + "-01T12:00:00").toLocaleDateString("en-GB", { month: "short" });

function renderYoY() {
  if (!yoyDaily || !yoyDaily.length) return;
  const rows = yoyData(yoyDaily);
  const both = rows.filter((r) => r.prior);
  const tot = (k) => both.reduce((a, r) => a + r[k], 0);
  const T = { kwh: tot("kwh"), cost: tot("cost"), pkwh: tot("pkwh"), pcost: tot("pcost") };
  const span = (r) => `${shortMonth(r.month)} ${r.month.slice(2, 4)}`;
  const thisLabel = `${span(rows[0])} – ${span(rows[rows.length - 1])}`;
  const lastLabel = `${shortMonth(rows[0].prev)} ${rows[0].prev.slice(2, 4)} – ${shortMonth(rows[rows.length - 1].prev)} ${rows[rows.length - 1].prev.slice(2, 4)}`;
  const partNote = (r) => (r.days < r.monthDays ? (r.lastDay - r.firstDay + 1 === r.days ? `${r.firstDay}–${r.lastDay} only` : `${r.days} days only`) : "");

  const kc = change(T.kwh, T.pkwh), cc = change(T.cost, T.pcost);
  const word = (c, up, down) => (c == null ? "" : `${Math.abs(Math.round(c * 100))}% ${c >= 0 ? up : down}`);
  $("yoy-hint").innerHTML = both.length
    ? `This year is ${thisLabel}, last year is the same months a year earlier. Part months compare the same days in both years. ` +
      `Over the ${both.length} month${both.length === 1 ? "" : "s"} with both years: <b>${num(T.kwh)} kWh</b> vs ${num(T.pkwh)} kWh (${word(kc, "more", "less")}), ` +
      `<b>${gbp(T.cost)}</b> vs ${gbp(T.pcost)} (${word(cc, "more", "less")}).`
    : "Year-on-year needs readings from a year ago; there are none yet.";

  const x = rows.map((r) => shortMonth(r.month) + (r.days < r.monthDays ? "*" : ""));
  const cd = rows.map((r) => [span(r), `${shortMonth(r.prev)} ${r.prev.slice(2, 4)}`, partNote(r) ? ` (${partNote(r)})` : ""]);
  const yoyLayout = (title) => baseLayout({
    barmode: "group",
    margin: { l: 52, r: 12, t: 28, b: 40 },
    title: { text: title, font: { size: 13, color: css("--muted") }, x: 0.01, y: 0.98 },
    yaxis: { gridcolor: css("--grid"), zerolinecolor: css("--border"), rangemode: "tozero" },
  });
  const bars = (key, pkey, fmt) => [
    { type: "bar", name: "Last year", x, y: rows.map((r) => r[pkey]), customdata: cd, marker: { color: css("--prev") },
      hovertemplate: `%{customdata[1]}%{customdata[2]}: ${fmt}<extra></extra>` },
    { type: "bar", name: "This year", x, y: rows.map((r) => r[key]), customdata: cd, marker: { color: css("--accent") },
      hovertemplate: `%{customdata[0]}%{customdata[2]}: ${fmt}<extra></extra>` },
  ];
  Plotly.react("yoy-kwh-chart", bars("kwh", "pkwh", "%{y:,.0f} kWh"), yoyLayout("Energy used (kWh)"), plotConfig);
  Plotly.react("yoy-cost-chart", bars("cost", "pcost", "£%{y:,.2f}"), yoyLayout("Cost incl. standing (£)"), plotConfig);

  const ap = (c, k) => (k ? c / k * 100 : null);
  $("yoy-rows").innerHTML = [...rows].reverse().map((r) => {
    const note = partNote(r);
    return `<tr><td>${monthName(r.month)}${note ? ` <span class="part">${note}</span>` : ""}</td>` +
      `<td>${num(r.kwh)}</td><td>${num(r.pkwh)}</td>${changeCell(change(r.kwh, r.pkwh))}` +
      `<td>${gbp(r.cost)}</td><td>${gbp(r.pcost)}</td>${changeCell(change(r.cost, r.pcost))}` +
      `<td>${num(ap(r.cost, r.kwh), 1)}</td><td>${r.prior ? num(ap(r.pcost, r.pkwh), 1) : "–"}</td></tr>`;
  }).join("");
  $("yoy-foot").innerHTML = both.length
    ? `<tr><td>Total (${both.length} months)</td><td>${num(T.kwh)}</td><td>${num(T.pkwh)}</td>${changeCell(kc)}` +
      `<td>${gbp(T.cost)}</td><td>${gbp(T.pcost)}</td>${changeCell(cc)}<td>${num(ap(T.cost, T.kwh), 1)}</td><td>${num(ap(T.pcost, T.pkwh), 1)}</td></tr>`
    : "";
}

async function load() {
  const days = $("period").value;
  const [r, all] = await Promise.all([
    getJSON(`/api/usage/report?days=${days}`),
    yoyDaily ? null : days === "0" ? null : getJSON("/api/usage/report?days=0"),
  ]);
  if (!yoyDaily) yoyDaily = (all || r).daily;
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
  renderYoY();
}

async function init() {
  const prefs = loadPref();
  if (prefs.period && [...$("period").options].some((o) => o.value === prefs.period)) $("period").value = prefs.period;
  $("period").addEventListener("change", () => { savePref({ period: $("period").value }); load().catch(fail); });
  if (!(await gate())) return;
  await load();
  addEventListener("resize", () => { for (const id of ["monthly-chart", "yoy-kwh-chart", "yoy-cost-chart", "hourly-chart", "daily-chart", "cost-chart"]) Plotly.Plots.resize(id); });
}

init().catch(fail);
