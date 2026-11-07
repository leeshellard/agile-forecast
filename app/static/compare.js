const PREF_KEY = "agile-compare-prefs";
const loadPref = () => { try { return JSON.parse(localStorage.getItem(PREF_KEY)) || {}; } catch { return {}; } };
const savePref = (p) => { try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch { /* storage unavailable */ } };

function renderTariffs(tariffs) {
  if (!tariffs || tariffs.length < 2) return;
  const best = Math.min(...tariffs.map((t) => t.all_in_gbp));
  $("tariff-rows").innerHTML = tariffs.map((t) => {
    const diff = t.diff_gbp, rng = t.min_p === t.max_p ? `${num(t.min_p, 1)}p` : `${num(t.min_p, 1)}–${num(t.max_p, 1)}p`;
    const vs = t.tariff === null ? "–" : `<span style="color:var(${diff > 0 ? "--accent" : "--good"})">${diff > 0 ? "+" : "−"}${gbp(Math.abs(diff))}</span>`;
    return `<tr><td>${t.name}${t.all_in_gbp === best ? " ✓" : ""}</td><td>${gbp(t.all_in_gbp)}</td><td>${num(t.all_in_p, 1)}p</td><td>${rng}</td><td>${vs}</td></tr>`;
  }).join("");
  const rows = [...tariffs].sort((a, b) => b.all_in_gbp - a.all_in_gbp);
  Plotly.react("tariff-chart", [{
    type: "bar", orientation: "h", y: rows.map((t) => t.name), x: rows.map((t) => t.all_in_gbp),
    marker: { color: rows.map((t) => (t.tariff === null ? css("--accent") : css("--prev"))) },
    text: rows.map((t) => gbp(t.all_in_gbp)), textposition: "auto", hovertemplate: "£%{x:.0f}<extra></extra>",
  }], baseLayout({
    margin: { l: 150, r: 12, t: 8, b: 40 }, showlegend: false, hovermode: "closest",
    xaxis: { title: axisTitle("£ for the period, incl. standing charge"), gridcolor: css("--grid"), rangemode: "tozero" },
    yaxis: { automargin: true },
  }), plotConfig);
}

async function load() {
  const r = await getJSON(`/api/usage/report?days=${$("period").value}`);
  const t = r.totals;
  $("subtitle").textContent = `${t.start} to ${t.end} · region ${r.region}`;
  show("content");  // the chart needs a visible container to size itself
  renderTariffs(r.tariffs);
  if (!r.tariffs || r.tariffs.length < 2) {
    $("empty-msg").textContent = "No comparison rates yet. Run an update on the server (systemctl start agile-forecast-update.service), then reload.";
    show("empty");
  }
}

async function init() {
  const prefs = loadPref();
  if (prefs.period && [...$("period").options].some((o) => o.value === prefs.period)) $("period").value = prefs.period;
  $("period").addEventListener("change", () => { savePref({ period: $("period").value }); load().catch(fail); });
  if (!(await gate())) return;
  await load();
  addEventListener("resize", () => Plotly.Plots.resize("tariff-chart"));
}

init().catch(fail);
