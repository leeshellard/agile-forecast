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
// month of readings) is compared like for like. Solar generated and whole-house use (grid + solar + battery)
// come from the inverter via /api/usage/energy when the server has ENERGY_URL set; they use the same days.
let yoyDaily = null, yoyEnergy;  // yoyEnergy: undefined = loading, null = failed, [] = not set up, else daily rows
let energyRequested = false;

function yoyData(daily, energy) {
  const byDate = new Map(daily.map((d) => [d.date, d]));
  const en = new Map((energy || []).map((d) => [d.date, d]));
  const last = daily[daily.length - 1].date.slice(0, 7);
  const shift = (ym, n) => { const d = new Date(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
  // A sum over days, or null when any day is missing that figure (so a gap never looks like a drop).
  const total = (dates, get) => { let t = 0; for (const x of dates) { const v = get(x); if (v == null) return null; t += v; } return dates.length ? t : null; };
  const rows = [];
  for (let i = 11; i >= 0; i--) {
    const m = shift(last, -i), p = shift(m, -12);
    const inMonth = daily.filter((d) => d.date.startsWith(m)).map((d) => d.date);
    if (!inMonth.length) continue;
    const paired = inMonth.filter((d) => byDate.has(p + d.slice(7)));
    const prior = paired.length > 0;
    const use = prior ? paired : inMonth;
    const before = use.map((d) => p + d.slice(7));
    const meter = (k) => (dates) => total(dates, (x) => byDate.get(x)?.[k]);
    const inv = (k) => (dates) => total(dates, (x) => en.get(x)?.[k]);
    rows.push({
      month: m, prev: p, days: use.length, monthDays: new Date(+m.slice(0, 4), +m.slice(5, 7), 0).getDate(), prior,
      firstDay: +use[0].slice(8), lastDay: +use[use.length - 1].slice(8),
      kwh: meter("kwh")(use), pkwh: prior ? meter("kwh")(before) : null,
      cost: meter("cost_gbp")(use), pcost: prior ? meter("cost_gbp")(before) : null,
      solar: inv("solar_kwh")(use), psolar: prior ? inv("solar_kwh")(before) : null,
      house: inv("house_kwh")(use), phouse: prior ? inv("house_kwh")(before) : null,
    });
  }
  return rows;
}

const change = (a, b) => (a == null || b == null || !b ? null : (a - b) / b);
// up = "bad" (pink) for usage and cost, but "good" (green) for solar
const changeCell = (c, upGood = false) => {
  if (c == null) return "<td>–</td>";
  const n = Math.round(c * 100);
  const cls = n === 0 ? "" : (n > 0) === upGood ? "down" : "up";
  return `<td class="${cls}">${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n)}%</td>`;
};
const shortMonth = (m) => new Date(m + "-01T12:00:00").toLocaleDateString("en-GB", { month: "short" });

function renderYoY() {
  if (!yoyDaily || !yoyDaily.length) return;
  const rows = yoyData(yoyDaily, yoyEnergy);
  const hasEnergy = rows.some((r) => r.solar != null);
  const both = rows.filter((r) => r.prior);
  const tot = (k, pk) => { const rs = both.filter((r) => r[k] != null && r[pk] != null); return rs.length ? [rs.reduce((a, r) => a + r[k], 0), rs.reduce((a, r) => a + r[pk], 0), rs.length] : [null, null, 0]; };
  const [tk, tpk] = tot("kwh", "pkwh"), [tc, tpc] = tot("cost", "pcost"), [ts, tps, ns] = tot("solar", "psolar"), [th, tph, nh] = tot("house", "phouse");
  const span = (ym) => `${shortMonth(ym)} ${ym.slice(2, 4)}`;
  const partNote = (r) => (r.days < r.monthDays ? (r.lastDay - r.firstDay + 1 === r.days ? `${r.firstDay}–${r.lastDay} only` : `${r.days} days only`) : "");
  const word = (c) => (c == null ? "" : ` (${Math.abs(Math.round(c * 100))}% ${c >= 0 ? "more" : "less"})`);

  let hint = `This year is ${span(rows[0].month)} – ${span(rows[rows.length - 1].month)}, last year is the same months a year earlier. ` +
    "Part months compare the same days in both years. ";
  if (both.length) {
    hint += `Over the ${both.length} month${both.length === 1 ? "" : "s"} with both years you bought <b>${num(tk)} kWh</b> from the grid vs ${num(tpk)} kWh${word(change(tk, tpk))} ` +
      `for <b>${gbp(tc)}</b> vs ${gbp(tpc)}${word(change(tc, tpc))}.`;
    if (ns) hint += ` Solar made <b>${num(ts)} kWh</b> vs ${num(tps)} kWh${word(change(ts, tps))}` +
      (ns < both.length ? ` over the ${ns} month${ns === 1 ? "" : "s"} with inverter data for both years` : "") +
      (nh ? `, and the house used <b>${num(th)} kWh</b> in all vs ${num(tph)} kWh${word(change(th, tph))}.` : ".");
  } else hint += "There are no readings from a year earlier yet.";
  if (yoyEnergy === null) hint += " <span class=\"part\">Solar figures unavailable right now.</span>";
  $("yoy-hint").innerHTML = hint;

  const x = rows.map((r) => shortMonth(r.month) + (r.days < r.monthDays ? "*" : ""));
  const cd = rows.map((r) => [span(r.month), span(r.prev), partNote(r) ? ` (${partNote(r)})` : ""]);
  const yoyLayout = (title) => baseLayout({
    barmode: "group",
    margin: { l: 52, r: 12, t: 28, b: 40 },
    title: { text: title, font: { size: 13, color: css("--muted") }, x: 0.01, y: 0.98 },
    yaxis: { gridcolor: css("--grid"), zerolinecolor: css("--border"), rangemode: "tozero" },
  });
  const bars = (key, pkey, fmt, color) => [
    { type: "bar", name: "Last year", x, y: rows.map((r) => r[pkey]), customdata: cd, marker: { color: css("--prev") },
      hovertemplate: `%{customdata[1]}%{customdata[2]}: ${fmt}<extra></extra>` },
    { type: "bar", name: "This year", x, y: rows.map((r) => r[key]), customdata: cd, marker: { color: css(color) },
      hovertemplate: `%{customdata[0]}%{customdata[2]}: ${fmt}<extra></extra>` },
  ];
  Plotly.react("yoy-kwh-chart", bars("kwh", "pkwh", "%{y:,.0f} kWh", "--demand"), yoyLayout("Bought from the grid (kWh)"), plotConfig);
  Plotly.react("yoy-cost-chart", bars("cost", "pcost", "£%{y:,.2f}", "--accent"), yoyLayout("Cost incl. standing (£)"), plotConfig);
  $("yoy-energy").hidden = !hasEnergy;
  if (hasEnergy) {
    Plotly.react("yoy-solar-chart", bars("solar", "psolar", "%{y:,.0f} kWh", "--solar"), yoyLayout("Solar generated (kWh)"), plotConfig);
    Plotly.react("yoy-house-chart", bars("house", "phouse", "%{y:,.0f} kWh", "--actual"), yoyLayout("Whole house use: grid + solar + battery (kWh)"), plotConfig);
  }

  const groups = [["Bought from grid (kWh)", 3], ["Cost incl. standing (£)", 3]]
    .concat(hasEnergy ? [["Solar generated (kWh)", 3], ["Whole house use (kWh)", 3]] : [], [["All-in grid price (p/kWh)", 2]]);
  const sub = (n) => ["This year", "Last year", "Change"].slice(0, n).map((t) => `<th>${t}</th>`).join("");
  $("yoy-head").innerHTML = `<tr><th rowspan="2">Month</th>${groups.map(([t, n]) => `<th colspan="${n}">${t}</th>`).join("")}</tr>` +
    `<tr>${groups.map(([, n]) => sub(n)).join("")}</tr>`;

  const ap = (c, k) => (c != null && k ? c / k * 100 : null);
  const trio = (a, b, f, upGood) => `<td>${f(a)}</td><td>${f(b)}</td>${changeCell(change(a, b), upGood)}`;
  const line = (label, v, extra = "") =>
    `<tr><td>${label}${extra}</td>${trio(v.kwh, v.pkwh, num)}${trio(v.cost, v.pcost, gbp)}` +
    (hasEnergy ? trio(v.solar, v.psolar, num, true) + trio(v.house, v.phouse, num) : "") +
    `<td>${num(ap(v.cost, v.kwh), 1)}</td><td>${num(ap(v.pcost, v.pkwh), 1)}</td></tr>`;
  $("yoy-rows").innerHTML = [...rows].reverse().map((r) => {
    const note = partNote(r);
    return line(monthName(r.month), r, note ? ` <span class="part">${note}</span>` : "");
  }).join("");
  $("yoy-foot").innerHTML = both.length
    ? line(`Total (${both.length} months)`, { kwh: tk, pkwh: tpk, cost: tc, pcost: tpc, solar: ns ? ts : null, psolar: ns ? tps : null,
                                              house: nh ? th : null, phouse: nh ? tph : null })
    : "";
}

async function load() {
  const days = $("period").value;
  const [r, all] = await Promise.all([
    getJSON(`/api/usage/report?days=${days}`),
    yoyDaily ? null : days === "0" ? null : getJSON("/api/usage/report?days=0"),
  ]);
  if (!yoyDaily) yoyDaily = (all || r).daily;
  if (!energyRequested) {
    energyRequested = true;  // fetched once; the page renders without it and redraws when it arrives
    getJSON("/api/usage/energy").then((e) => { yoyEnergy = e.available ? e.days : e.error ? null : []; renderYoY(); })
      .catch(() => { yoyEnergy = null; renderYoY(); });
  }
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
  addEventListener("resize", () => { for (const id of ["monthly-chart", "yoy-kwh-chart", "yoy-cost-chart", "yoy-solar-chart", "yoy-house-chart", "hourly-chart", "daily-chart", "cost-chart"]) Plotly.Plots.resize(id); });
}

init().catch(fail);
