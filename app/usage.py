"""Your own smart-meter usage, costed against tariff rates. Switched on by OCTOPUS_API_KEY + OCTOPUS_ACCOUNT.

The key stays on the server: half-hourly readings, your meter and tariff history are pulled from the Octopus API
into SQLite, and the web page only ever sees the costed results. Run a sync on its own with:  python -m app.usage

Costs use the tariff you were actually on at the time (from your account's agreement history), with the lowest
published rate where a tariff has several (direct debit). Standing charges are included. Export income needs an
export tariff with a single unit rate (Outgoing Octopus); time-of-use export tariffs such as Flux are not costed.
"""
from __future__ import annotations

import json
import logging
import sys

import numpy as np
import pandas as pd
import requests

from . import config, db

log = logging.getLogger("agile.usage")
API = "https://api.octopus.energy/v1"
PEAK_HOURS = (16, 17, 18)  # local hours of the Agile evening peak
# Tariffs to price your usage on at today's rates. A trailing "-" means "newest available product with this prefix"
# (the fixed deals get a new dated product code each time they are re-issued).
COMPARE_TARIFFS = [
    ("Cosy Octopus", "COSY-22-12-08"),
    ("Cosy 12-month fixed", "COSY-FIX-12M-"),
    ("Octopus Go", "GO-VAR-22-10-14"),
    ("Go 12-month fixed", "GO-FIX-12M-"),
]


def configured() -> bool:
    return bool(config.OCTOPUS_API_KEY and config.OCTOPUS_ACCOUNT)


# --------------------------------------------------------------------------- Octopus API

def _get(url: str, params: dict | None = None) -> requests.Response:
    r = requests.get(url, params=params, auth=(config.OCTOPUS_API_KEY, ""), timeout=config.HTTP_TIMEOUT,
                     headers={"User-Agent": config.USER_AGENT})
    return r


def _pages(url: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    while url:
        r = _get(url, params)
        if r.status_code in (400, 404):
            return rows
        r.raise_for_status()
        body = r.json()
        rows += body["results"]
        url, params = body.get("next"), None
    return rows


def _iso(t: pd.Timestamp) -> str:
    return t.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _discover() -> dict:
    """Meter points and tariff agreements of the account's current property."""
    r = _get(f"{API}/accounts/{config.OCTOPUS_ACCOUNT}/")
    r.raise_for_status()
    props = [p for p in r.json().get("properties", []) if not p.get("moved_out_at")]
    if not props:
        raise RuntimeError("No current property found on the account")
    out: dict = {}
    for mp in props[0].get("electricity_meter_points", []):
        kind = "export" if mp.get("is_export") else "import"
        out[kind] = {
            "mpan": mp["mpan"],
            "serials": [m["serial_number"] for m in mp.get("meters", [])],
            "agreements": [{"tariff_code": a["tariff_code"], "valid_from": a["valid_from"], "valid_to": a["valid_to"]}
                           for a in mp.get("agreements", [])],
        }
    if "import" not in out:
        raise RuntimeError("No electricity import meter found on the account")
    return out


def _region(meta: dict) -> str:
    return max(meta["import"]["agreements"], key=lambda a: a["valid_from"])["tariff_code"][-1]


def _meta_get(key: str, default=None):
    df = db.read_df("SELECT value FROM usage_meta WHERE key = ?", (key,))
    return json.loads(df["value"].iloc[0]) if not df.empty else default


def _meta_set(key: str, value) -> None:
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO usage_meta VALUES (?, ?)", (key, json.dumps(value)))


def _sync_consumption(kind: str, point: dict, now: pd.Timestamp) -> int:
    last = db.read_df("SELECT MAX(ts) AS t FROM usage WHERE kind = ?", (kind,))["t"].iloc[0]
    since = pd.Timestamp(last) - pd.Timedelta(days=2) if last else now - pd.Timedelta(days=config.USAGE_HISTORY_DAYS)
    n = 0
    for serial in point["serials"]:
        url = f"{API}/electricity-meter-points/{point['mpan']}/meters/{serial}/consumption/"
        # Fetch in month-sized windows. The API's multi-page results for long ranges can repeat and skip
        # half-hours, but a window of about a month (<1,500 rows) always arrives complete on one page.
        rows, start = [], since.floor("D")
        while start < now:
            end = min(start + pd.Timedelta(days=30), now + pd.Timedelta(days=1))
            rows += _pages(url, {"page_size": 25000, "order_by": "period",
                                 "period_from": _iso(start), "period_to": _iso(end)})
            start = end
        if rows:
            n += len(rows)
            with db.connect() as con:
                con.executemany("INSERT OR REPLACE INTO usage VALUES (?, ?, ?)",
                                [(kind, _iso(pd.Timestamp(x["interval_start"])), float(x["consumption"])) for x in rows])
    return n


def _sync_rates(tariff: str, kind: str, now: pd.Timestamp) -> None:
    if not tariff.startswith("E-1R-"):
        return  # multi-register tariffs (Economy 7, Flux, ...) are not costed
    product = tariff[len("E-1R-"):-2]
    endpoint = {"unit": "standard-unit-rates", "standing": "standing-charges"}[kind]
    last = db.read_df("SELECT MAX(valid_from) AS t FROM tariff_rates WHERE tariff = ? AND kind = ?", (tariff, kind))["t"].iloc[0]
    since = pd.Timestamp(last) - pd.Timedelta(days=2) if last else now - pd.Timedelta(days=config.USAGE_HISTORY_DAYS + 30)
    rows = _pages(f"{API}/products/{product}/electricity-tariffs/{tariff}/{endpoint}/",
                  {"page_size": 1500, "period_from": _iso(since)})
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["valid_from"] = [_iso(pd.Timestamp(t)) for t in df["valid_from"]]
    df = df.groupby("valid_from", as_index=False)["value_inc_vat"].min()  # lowest = direct debit
    with db.connect() as con:
        con.executemany("INSERT OR REPLACE INTO tariff_rates VALUES (?, ?, ?, ?)",
                        [(tariff, kind, t, float(p)) for t, p in zip(df["valid_from"], df["value_inc_vat"])])


def _resolve_product(spec: str) -> str | None:
    if not spec.endswith("-"):
        return spec
    r = _get(f"{API}/products/", {"page_size": 100, "is_business": "false"})
    r.raise_for_status()
    found = [p for p in r.json()["results"] if p["code"].startswith(spec) and p["direction"] == "IMPORT"
             and p["available_to"] is None]
    return max(found, key=lambda p: p["available_from"])["code"] if found else None


def _current_rates(product: str, region: str) -> dict | None:
    """Today's price for each half-hour of the day (local time) and the standing charge, for one tariff."""
    tariff = f"E-1R-{product}-{region}"
    now = pd.Timestamp.now(tz=config.TZ)
    day = now.normalize() - pd.Timedelta(days=1)
    window = {"page_size": 1500, "period_from": _iso(day - pd.Timedelta(days=1)), "period_to": _iso(now + pd.Timedelta(days=2))}
    units = _pages(f"{API}/products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/", window)
    standing = _pages(f"{API}/products/{product}/electricity-tariffs/{tariff}/standing-charges/", window)
    if not units or not standing:
        return None
    u = pd.DataFrame(units)
    u["vf"] = pd.to_datetime(u["valid_from"], utc=True)
    u["vt"] = pd.to_datetime(u["valid_to"], utc=True)
    slots = []
    for i in range(48):
        t = pd.Timestamp(f"{day:%Y-%m-%d} {i // 2:02d}:{30 * (i % 2):02d}").tz_localize(
            config.TZ, ambiguous=True, nonexistent="shift_forward").tz_convert("UTC")
        hit = u[(u["vf"] <= t) & (u["vt"].isna() | (u["vt"] > t))]
        if hit.empty:
            return None
        slots.append(float(hit["value_inc_vat"].min()))  # lowest = direct debit
    sc = pd.DataFrame(standing)
    sc["vf"] = pd.to_datetime(sc["valid_from"], utc=True)
    sc = sc[sc["vf"] == sc["vf"].max()]
    return {"tariff": tariff, "slots": slots, "standing": float(sc["value_inc_vat"].min())}


def _sync_comparison(region: str) -> None:
    out = {}
    for label, spec in COMPARE_TARIFFS + [("Flexible Octopus", config.USAGE_COMPARE_PRODUCT)]:
        try:
            product = _resolve_product(spec)
            cur = _current_rates(product, region) if product else None
        except requests.RequestException as e:  # one tariff being unavailable must not stop the rest
            log.warning("Could not fetch %s: %s", label, e)
            continue
        if cur:
            out[label] = cur
    _meta_set("compare_current", out)


def sync() -> bool:
    """Pull new readings and rates. Returns False when the feature is not configured."""
    if not configured():
        return False
    db.init()
    now = pd.Timestamp.now(tz="UTC")
    meta = _discover()
    _meta_set("account", meta)
    region = _region(meta)
    compare = f"E-1R-{config.USAGE_COMPARE_PRODUCT}-{region}"
    _meta_set("compare_tariff", compare)
    for kind in ("import", "export"):
        if kind in meta:
            log.info("%s: %d new half-hours", kind, _sync_consumption(kind, meta[kind], now))
    for kind in ("import", "export"):
        for tariff in {a["tariff_code"] for a in meta.get(kind, {}).get("agreements", [])}:
            _sync_rates(tariff, "unit", now)
            if kind == "import":
                _sync_rates(tariff, "standing", now)
    _sync_rates(compare, "unit", now)
    _sync_rates(compare, "standing", now)
    _sync_comparison(region)
    _meta_set("last_sync", _iso(now))
    return True


# --------------------------------------------------------------------------- costing

def _assign(ts: pd.DatetimeIndex, agreements: list[dict]) -> np.ndarray:
    """Tariff code in force at each timestamp (later agreements win; zero-length ones are ignored)."""
    out = np.full(len(ts), None, dtype=object)
    for a in sorted(agreements, key=lambda a: a["valid_from"]):
        vf = pd.Timestamp(a["valid_from"])
        vt = pd.Timestamp(a["valid_to"]) if a["valid_to"] else None
        if vt is not None and vt <= vf:
            continue
        mask = np.asarray(ts >= vf)
        if vt is not None:
            mask &= np.asarray(ts < vt)
        out[mask] = a["tariff_code"]
    return out


def _lookup(ts: pd.DatetimeIndex, rates: pd.DataFrame, tol_minutes: int | None = None) -> np.ndarray:
    """Rate in force at each timestamp (NaN before the first known rate, or beyond `tol_minutes` of the last)."""
    out = np.full(len(ts), np.nan)
    if rates.empty:
        return out
    vf = rates["vf"].to_numpy()
    t = ts.tz_convert("UTC").tz_localize(None).to_numpy().astype("datetime64[ns]")
    idx = np.searchsorted(vf, t, side="right") - 1
    ok = idx >= 0
    out[ok] = rates["p"].to_numpy()[idx[ok]]
    if tol_minutes is not None:
        stale = ok & ((t - vf[np.where(ok, idx, 0)]) > np.timedelta64(tol_minutes, "m"))
        out[stale] = np.nan
    return out


def _rate_frames() -> dict[tuple[str, str], pd.DataFrame]:
    df = db.read_df("SELECT tariff, kind, valid_from, p FROM tariff_rates ORDER BY valid_from")
    df["vf"] = pd.to_datetime(df["valid_from"], utc=True).dt.tz_localize(None).astype("datetime64[ns]")
    return {k: g[["vf", "p"]].reset_index(drop=True) for k, g in df.groupby(["tariff", "kind"])}


def _priced(df: pd.DataFrame, agreements: list[dict], rates: dict, kind: str = "unit") -> np.ndarray:
    """Rate (p) for each row of df (needs a UTC `ts` column) under the tariff in force."""
    ts = pd.DatetimeIndex(df["ts"])
    tariff = _assign(ts, agreements)
    out = np.full(len(df), np.nan)
    for t in {x for x in tariff if x}:
        m = tariff == t
        tol = 1 if kind == "unit" and "AGILE" in t else None  # Agile rates are per half-hour: a gap must not reuse an older price
        out[m] = _lookup(ts[m], rates.get((t, kind), pd.DataFrame(columns=["vf", "p"])), tol)
    return out


def _clean(v):
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else round(float(v), 4)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def status() -> dict:
    if not configured():
        return {"configured": False}
    db.init()
    df = db.read_df("SELECT MIN(ts) AS a, MAX(ts) AS b, COUNT(*) AS n FROM usage WHERE kind = 'import'")
    meta = _meta_get("account")
    return {"configured": True, "synced": bool(df["n"].iloc[0]), "first": df["a"].iloc[0], "last": df["b"].iloc[0],
            "last_sync": _meta_get("last_sync"), "region": _region(meta) if meta else None}


def report(days: int = 365, cheap: float = 15.0, expensive: float = 30.0) -> dict:
    """Costed usage for the last `days` days of data (0 = everything)."""
    meta = _meta_get("account")
    if not meta:
        raise LookupError("No usage data yet - run: python -m app.usage")
    rates = _rate_frames()
    compare = _meta_get("compare_tariff")

    raw = db.read_df("SELECT kind, ts, kwh FROM usage ORDER BY ts")
    raw["ts"] = pd.to_datetime(raw["ts"], utc=True)
    imp = raw[raw["kind"] == "import"].drop(columns="kind").reset_index(drop=True)
    exp = raw[raw["kind"] == "export"].drop(columns="kind").reset_index(drop=True)
    if imp.empty:
        raise LookupError("No usage data yet - run: python -m app.usage")
    if days:
        cut = imp["ts"].max() - pd.Timedelta(days=days)
        imp, exp = imp[imp["ts"] > cut], exp[exp["ts"] > cut]

    imp["rate"] = _priced(imp, meta["import"]["agreements"], rates)
    imp["cmp_rate"] = _lookup(pd.DatetimeIndex(imp["ts"]), rates.get((compare, "unit"), pd.DataFrame(columns=["vf", "p"])))
    imp = imp.dropna(subset=["rate", "cmp_rate"]).reset_index(drop=True)  # same slots on both sides of the comparison
    if imp.empty:
        raise LookupError("Tariff rates have not been downloaded yet - run: python -m app.usage")
    if exp.empty or "export" not in meta:
        exp = pd.DataFrame({"ts": pd.DatetimeIndex([], tz="UTC"), "kwh": []})
        exp["rate"] = []
    else:
        exp["rate"] = _priced(exp, meta["export"]["agreements"], rates)
        exp["rate"] = exp["rate"].fillna(0.0)

    local = imp["ts"].dt.tz_convert(config.TZ)
    imp["date"], imp["hour"] = local.dt.strftime("%Y-%m-%d"), local.dt.hour
    imp["cost"], imp["cmp_cost"] = imp["kwh"] * imp["rate"], imp["kwh"] * imp["cmp_rate"]
    elocal = exp["ts"].dt.tz_convert(config.TZ)
    exp["date"], exp["income"] = elocal.dt.strftime("%Y-%m-%d"), exp["kwh"] * exp["rate"]

    daily = imp.groupby("date").agg(kwh=("kwh", "sum"), cost=("cost", "sum"), cmp_cost=("cmp_cost", "sum"),
                                    first_ts=("ts", "min"))
    mid = pd.DatetimeIndex([pd.Timestamp(d + " 12:00", tz=config.TZ) for d in daily.index]).tz_convert("UTC")
    probe = pd.DataFrame({"ts": mid})
    daily["sc"] = _priced(probe, meta["import"]["agreements"], rates, "standing")
    daily["cmp_sc"] = _lookup(mid, rates.get((compare, "standing"), pd.DataFrame(columns=["vf", "p"])))
    daily[["sc", "cmp_sc"]] = daily[["sc", "cmp_sc"]].fillna(0.0)
    ed = exp.groupby("date").agg(exp_kwh=("kwh", "sum"), income=("income", "sum")) if len(exp) else pd.DataFrame(
        columns=["exp_kwh", "income"])
    daily = daily.join(ed).fillna({"exp_kwh": 0.0, "income": 0.0}).drop(columns="first_ts")
    daily["month"] = daily.index.str[:7]

    def money(s):  # pence -> pounds
        return float(s.sum()) / 100

    tot_kwh = float(imp["kwh"].sum())
    unit_gbp, sc_gbp = money(daily["cost"]), money(daily["sc"])
    cmp_unit, cmp_sc = money(daily["cmp_cost"]), money(daily["cmp_sc"])
    exp_gbp = money(daily["income"])
    peak = imp[imp["hour"].isin(PEAK_HOURS)]
    totals = {
        "start": daily.index.min(), "end": daily.index.max(), "days": int(len(daily)),
        "import_kwh": tot_kwh, "unit_gbp": unit_gbp, "standing_gbp": sc_gbp, "all_in_gbp": unit_gbp + sc_gbp,
        "avg_unit_p": unit_gbp / tot_kwh * 100 if tot_kwh else None,
        "all_in_p": (unit_gbp + sc_gbp) / tot_kwh * 100 if tot_kwh else None,
        "market_avg_p": float(imp["rate"].mean()),
        "export_kwh": float(daily["exp_kwh"].sum()), "export_gbp": exp_gbp,
        "net_gbp": unit_gbp + sc_gbp - exp_gbp,
        "compare_all_in_gbp": cmp_unit + cmp_sc,
        "compare_all_in_p": (cmp_unit + cmp_sc) / tot_kwh * 100 if tot_kwh else None,
        "saving_gbp": (cmp_unit + cmp_sc) - (unit_gbp + sc_gbp),
        "cheap_share": float(imp.loc[imp["rate"] < cheap, "kwh"].sum() / tot_kwh) if tot_kwh else None,
        "expensive_share": float(imp.loc[imp["rate"] > expensive, "kwh"].sum() / tot_kwh) if tot_kwh else None,
        "peak_share": float(peak["kwh"].sum() / tot_kwh) if tot_kwh else None,
        "peak_avg_p": float((peak["kwh"] * peak["rate"]).sum() / peak["kwh"].sum()) if peak["kwh"].sum() else None,
    }

    m = daily.groupby("month").agg(kwh=("kwh", "sum"), cost=("cost", "sum"), sc=("sc", "sum"), cmp_cost=("cmp_cost", "sum"),
                                   cmp_sc=("cmp_sc", "sum"), exp_kwh=("exp_kwh", "sum"), income=("income", "sum"))
    monthly = [{"month": k, "kwh": r.kwh, "cost_gbp": (r.cost + r.sc) / 100, "avg_p": r.cost / r.kwh if r.kwh else None,
                "compare_gbp": (r.cmp_cost + r.cmp_sc) / 100, "export_kwh": r.exp_kwh, "export_gbp": r.income / 100}
               for k, r in m.iterrows()]
    daily_out = [{"date": k, "kwh": r.kwh, "unit_gbp": r.cost / 100, "standing_gbp": r.sc / 100,
                  "cost_gbp": (r.cost + r.sc) / 100, "avg_p": r.cost / r.kwh if r.kwh else None,
                  "compare_gbp": (r.cmp_cost + r.cmp_sc) / 100, "export_kwh": r.exp_kwh, "export_gbp": r.income / 100}
                 for k, r in daily.iterrows()]

    local_slot = (local.dt.hour * 2 + local.dt.minute // 30).to_numpy()
    n_days_costed = len(daily)
    agile_all_in = unit_gbp + sc_gbp
    tariffs = [{"name": "Your Agile (actual)", "tariff": None, "unit_gbp": unit_gbp, "standing_gbp": sc_gbp,
                "all_in_gbp": agile_all_in, "all_in_p": totals["all_in_p"], "diff_gbp": 0.0,
                "min_p": float(imp["rate"].min()), "max_p": float(imp["rate"].max())}]
    for label, cur in (_meta_get("compare_current", {}) or {}).items():
        slot_p = np.asarray(cur["slots"])[local_slot]
        unit = float((imp["kwh"].to_numpy() * slot_p).sum()) / 100
        stand = cur["standing"] * n_days_costed / 100
        tariffs.append({"name": label, "tariff": cur["tariff"], "unit_gbp": unit, "standing_gbp": stand,
                        "all_in_gbp": unit + stand, "all_in_p": (unit + stand) / tot_kwh * 100 if tot_kwh else None,
                        "diff_gbp": unit + stand - agile_all_in, "min_p": min(cur["slots"]), "max_p": max(cur["slots"])})

    h = imp.groupby("hour")
    hourly = [{"hour": int(k), "kw": float(g["kwh"].mean() * 2),  # kWh per half-hour -> average kW
               "paid_p": float((g["kwh"] * g["rate"]).sum() / g["kwh"].sum()) if g["kwh"].sum() else None,
               "market_p": float(g["rate"].mean())} for k, g in h]
    return json.loads(json.dumps({"compare_tariff": compare, "region": _region(meta), "totals": totals,
                                  "monthly": monthly, "daily": daily_out, "hourly": hourly,
                                  "tariffs": tariffs},
                                 default=_clean), parse_constant=lambda c: None)


_energy_cache: dict = {"at": 0.0, "data": None}


def energy() -> dict:
    """Daily solar generated and whole-house use from ENERGY_URL (a Battery Planner install), cached for 30 min.
    Never raises: when it is not set or not reachable the page simply leaves the solar figures out."""
    import time
    if not config.ENERGY_URL:
        return {"available": False, "days": []}
    now = time.time()
    if _energy_cache["data"] is not None and now - _energy_cache["at"] < 1800:
        return _energy_cache["data"]
    try:
        r = requests.get(config.ENERGY_URL, timeout=90, headers={"User-Agent": config.USER_AGENT})
        r.raise_for_status()
        days = [{"date": d["date"], "solar_kwh": d.get("solar_kwh"), "house_kwh": d.get("house_kwh")}
                for d in r.json().get("days", [])]
        out = {"available": bool(days), "days": days}
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        log.warning("Could not fetch solar/house totals from %s: %s", config.ENERGY_URL, e)
        return _energy_cache["data"] or {"available": False, "days": [], "error": "Solar figures unavailable"}
    _energy_cache.update(at=now, data=out)
    return out


def _shift_day(u: np.ndarray, r: np.ndarray, frac: float) -> tuple[float, float]:
    """Move `frac` of a day's energy from its dearest half-hours into its cheapest ones.

    Energy only comes out of a slot you really used, and a cheap slot never takes you above your own busiest
    half-hour that day (so the plan never needs a bigger heater or charger than you already ran). Stops when no
    cheaper slot is left. Returns (saving in pence, kWh moved).
    """
    left = frac * float(u.sum())
    room = np.maximum(float(u.max()) - u, 0.0)
    dst = np.argsort(r)
    di, saved, moved = 0, 0.0, 0.0
    for s in np.argsort(-r):
        if left <= 1e-9 or di >= len(dst):
            break
        take = min(float(u[s]), left)
        while take > 1e-9 and di < len(dst):
            d = dst[di]
            if r[d] >= r[s]:
                di = len(dst)
                break
            put = min(take, room[d])
            if put <= 1e-9:
                di += 1
                continue
            saved += put * (r[s] - r[d])
            room[d] -= put
            take -= put
            left -= put
            moved += put
    return saved, moved


def winter(season: int | None = None, shift: float = 0.15, slots: int = 8) -> dict:
    """What a winter (Nov-Feb) cost, and what moving `shift` of the energy out of the dearest half-hours would save.

    `season` is the year the winter starts in (2025 = Nov 2025 to Feb 2026); default is the latest with data.
    The saving is a what-if on your own readings and the half-hourly prices of the tariff you were on.
    """
    meta = _meta_get("account")
    if not meta:
        raise LookupError("No usage data yet - run: python -m app.usage")
    rates = _rate_frames()
    imp = db.read_df("SELECT ts, kwh FROM usage WHERE kind = 'import' ORDER BY ts")
    if imp.empty:
        raise LookupError("No usage data yet - run: python -m app.usage")
    imp["ts"] = pd.to_datetime(imp["ts"], utc=True)
    imp["rate"] = _priced(imp, meta["import"]["agreements"], rates)
    imp = imp.dropna(subset=["rate"])
    local = imp["ts"].dt.tz_convert(config.TZ)
    imp["month"] = local.dt.strftime("%Y-%m")
    imp["date"], imp["hour"] = local.dt.strftime("%Y-%m-%d"), local.dt.hour
    imp["season"] = np.where(local.dt.month >= 11, local.dt.year, local.dt.year - 1)
    winter_only = imp[local.dt.month.isin([11, 12, 1, 2]).to_numpy()]
    seasons = sorted(int(x) for x in winter_only["season"].unique())
    if not seasons:
        raise LookupError("No winter (November to February) readings yet")
    if season not in seasons:  # default: the latest winter with a reasonable amount of data (not one just starting)
        full = [y for y in seasons if winter_only.loc[winter_only["season"] == y, "date"].nunique() >= 60]
        season = (full or seasons)[-1]
    w = winter_only[winter_only["season"] == season].copy()

    shift = min(max(shift, 0.0), 0.6)
    by_month: dict[str, list[float]] = {}  # month -> [cost p, saving p, kwh]
    moved_kwh, dear_kwh, dear_cost = 0.0, 0.0, 0.0
    cheap_by_hour, dear_by_hour, days_used = np.zeros(24), np.zeros(24), 0
    for _, day in w.groupby("date"):
        if len(day) < 46:  # a partial day (or a data gap) would make "cheapest slot" meaningless
            m = day["month"].iloc[0]
            acc = by_month.setdefault(m, [0.0, 0.0, 0.0])
            acc[0] += float((day["kwh"] * day["rate"]).sum()); acc[2] += float(day["kwh"].sum())
            continue
        u, r, hr = day["kwh"].to_numpy(), day["rate"].to_numpy(), day["hour"].to_numpy()
        saved, moved = _shift_day(u, r, shift)
        acc = by_month.setdefault(day["month"].iloc[0], [0.0, 0.0, 0.0])
        acc[0] += float((u * r).sum()); acc[1] += saved; acc[2] += float(u.sum())
        moved_kwh += moved
        order = np.argsort(r)
        dear = order[-slots:]
        dear_kwh += float(u[dear].sum()); dear_cost += float((u[dear] * r[dear]).sum())
        np.add.at(cheap_by_hour, hr[order[:slots]], 1)
        np.add.at(dear_by_hour, hr[dear], 1)
        days_used += 1

    tot_kwh = float(w["kwh"].sum())
    cost = sum(v[0] for v in by_month.values()) / 100
    saving = sum(v[1] for v in by_month.values()) / 100
    monthly = [{"month": k, "kwh": v[2], "cost_gbp": v[0] / 100, "new_cost_gbp": (v[0] - v[1]) / 100, "saving_gbp": v[1] / 100}
               for k, v in sorted(by_month.items())]
    n = max(days_used, 1)
    hourly = [{"hour": int(h), "kw": float(g["kwh"].mean() * 2),
               "paid_p": float((g["kwh"] * g["rate"]).sum() / g["kwh"].sum()) if g["kwh"].sum() else None,
               "market_p": float(g["rate"].mean()), "cheap_slots": float(cheap_by_hour[h] / n),
               "dear_slots": float(dear_by_hour[h] / n)} for h, g in w.groupby("hour")]
    out = {
        "season": season, "seasons": seasons, "start": w["date"].min(), "end": w["date"].max(),
        "days": int(w["date"].nunique()), "shift": shift, "slots": slots,
        "totals": {"kwh": tot_kwh, "cost_gbp": cost, "avg_p": cost / tot_kwh * 100 if tot_kwh else None,
                   "new_cost_gbp": cost - saving, "saving_gbp": saving, "saving_pct": saving / cost if cost else None,
                   "new_avg_p": (cost - saving) / tot_kwh * 100 if tot_kwh else None, "moved_kwh": moved_kwh,
                   "dear_kwh": dear_kwh, "dear_share": dear_kwh / tot_kwh if tot_kwh else None,
                   "dear_cost_gbp": dear_cost / 100, "dear_avg_p": dear_cost / dear_kwh if dear_kwh else None},
        "monthly": monthly, "hourly": hourly,
    }
    return json.loads(json.dumps(out, default=_clean), parse_constant=lambda c: None)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not configured():
        print("Set OCTOPUS_API_KEY and OCTOPUS_ACCOUNT first.")
        sys.exit(2)
    sync()


if __name__ == "__main__":
    main()
