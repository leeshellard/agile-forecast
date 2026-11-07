let timer = null;

function render(w) {
  const t = w.totals;
  $("subtitle").textContent = `Nov ${w.season} to Feb ${w.season + 1} · ${w.start} to ${w.end} (${w.days} days)`;
  const dearN = w.slots;
  $("cards").innerHTML = [
    card("Winter electricity cost", gbp(t.cost_gbp), `${num(t.kwh)} kWh at ${num(t.avg_p, 1)}p on average`),
    card("Possible saving", gbp(t.saving_gbp), t.saving_pct == null ? "" : `${pct(t.saving_pct)} of the winter cost`, t.saving_gbp > 0 ? "good" : ""),
    card("Average price after", `${num(t.new_avg_p, 1)}p`, `Down from ${num(t.avg_p, 1)}p`),
    card("Energy moved", `${num(t.moved_kwh)} kWh`, `${num(t.moved_kwh / Math.max(1, w.days), 1)} kWh a day`),
    card(`Used in the day's dearest ${dearN} slots`, pct(t.dear_share), t.dear_avg_p == null ? "" : `${gbp(t.dear_cost_gbp)} at ${num(t.dear_avg_p, 1)}p`),
  ].join("");

  const x = w.hourly.map((h) => String(h.hour).padStart(2, "0") + ":00");
  const tick = { gridcolor: css("--grid"), linecolor: css("--border"), tickangle: 0, dtick: innerWidth < 600 ? 4 : 2 };
  Plotly.react("slots-chart", [
    { type: "bar", name: `Cheapest ${dearN} slots`, x, y: w.hourly.map((h) => h.cheap_slots), marker: { color: css("--cheap") }, hovertemplate: "%{y:.2f} a day" },
    { type: "bar", name: `Dearest ${dearN} slots`, x, y: w.hourly.map((h) => h.dear_slots), marker: { color: css("--accent") }, hovertemplate: "%{y:.2f} a day" },
  ], baseLayout({ barmode: "group", margin: { l: 52, r: 12, t: 8, b: 40 }, xaxis: tick,
    yaxis: { title: axisTitle("half-hours a day (max 2)"), gridcolor: css("--grid"), rangemode: "tozero" } }), plotConfig);

  Plotly.react("hourly-chart", [
    { type: "bar", name: "Average demand (kW)", x, y: w.hourly.map((h) => h.kw), marker: { color: css("--demand") }, hovertemplate: "%{y:.2f} kW" },
    { type: "scatter", mode: "lines", name: "Price you paid (p)", x, y: w.hourly.map((h) => h.paid_p), yaxis: "y2", line: { color: css("--accent"), width: 2 }, hovertemplate: "%{y:.1f}p" },
  ], baseLayout({ xaxis: tick, yaxis: { title: axisTitle("kW"), gridcolor: css("--grid"), rangemode: "tozero" },
    yaxis2: { title: axisTitle("p/kWh"), overlaying: "y", side: "right", showgrid: false, rangemode: "tozero" } }), plotConfig);

  const months = w.monthly.map((m) => monthName(m.month));
  Plotly.react("month-chart", [
    { type: "bar", name: "Now", x: months, y: w.monthly.map((m) => m.cost_gbp), marker: { color: css("--prev") }, hovertemplate: "£%{y:.0f}" },
    { type: "bar", name: "After moving", x: months, y: w.monthly.map((m) => m.new_cost_gbp), marker: { color: css("--cheap") }, hovertemplate: "£%{y:.0f}" },
  ], baseLayout({ barmode: "group", margin: { l: 52, r: 12, t: 8, b: 40 },
    yaxis: { title: axisTitle("£ electricity"), gridcolor: css("--grid"), rangemode: "tozero" } }), plotConfig);

  $("month-rows").innerHTML = w.monthly.map((m) =>
    `<tr><td>${monthName(m.month)}</td><td>${num(m.kwh)}</td><td>${gbp(m.cost_gbp)}</td><td>${gbp(m.new_cost_gbp)}</td><td>${gbp(m.saving_gbp)}</td></tr>`).join("");
}

async function load() {
  const season = $("season").value;
  const q = new URLSearchParams({ shift: $("shift").value / 100 });
  if (season) q.set("season", season);
  const w = await getJSON(`/api/usage/winter?${q}`);
  if (!$("season").options.length) {
    $("season").innerHTML = [...w.seasons].reverse().map((y) => `<option value="${y}">Nov ${y} – Feb ${y + 1}</option>`).join("");
  }
  $("season").value = w.season;
  show("content");  // the charts need a visible container to size themselves
  render(w);
}

async function init() {
  $("shift").addEventListener("input", () => {
    $("shift-label").textContent = `${$("shift").value}%`;
    clearTimeout(timer);
    timer = setTimeout(() => load().catch(fail), 150);
  });
  $("season").addEventListener("change", () => load().catch(fail));
  if (!(await gate())) return;
  await load();
  addEventListener("resize", () => { for (const id of ["slots-chart", "hourly-chart", "month-chart"]) Plotly.Plots.resize(id); });
}

init().catch(fail);
