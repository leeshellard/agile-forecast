"""Web UI and JSON API.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, model

app = FastAPI(title="Agile Forecast", version="1.0")
STATIC = config.BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.on_event("startup")
def _startup():
    db.init()


def _iso(t: pd.Timestamp) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _region(region: str) -> str:
    region = region.upper()
    if region not in config.REGIONS:
        raise HTTPException(404, f"Unknown region {region!r}; use one of {', '.join(config.REGIONS)}")
    return region


def _runs(limit: int | None = None, since: pd.Timestamp | None = None,
          until: pd.Timestamp | None = None) -> pd.DataFrame:
    sql, where, params = "SELECT id, created_at, train_rows, holdout_mae FROM runs", [], []
    if since is not None:
        where.append("created_at >= ?")
        params.append(_iso(since))
    if until is not None:
        where.append("created_at <= ?")
        params.append(_iso(until))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    runs = db.read_df(sql, params)
    runs["created_at"] = pd.to_datetime(runs["created_at"], utc=True)
    return runs


def _run_prices(run_ids: list[int], region: str) -> pd.DataFrame:
    """Forecast points for the given runs, converted to the requested region."""
    if not run_ids:
        return pd.DataFrame()
    marks = ",".join("?" * len(run_ids))
    pts = db.read_df(f"SELECT * FROM run_points WHERE run_id IN ({marks}) ORDER BY run_id, ts", run_ids)
    coeffs = db.read_df(f"SELECT run_id, a, b, c FROM run_regions WHERE region = ? AND run_id IN ({marks})",
                        [region, *run_ids]).set_index("run_id")
    pts["ts"] = pd.to_datetime(pts["ts"], utc=True)
    peak = model.is_peak(pd.DatetimeIndex(pts["ts"]))
    for col in ["p10", "p50", "p90"]:
        out = np.empty(len(pts))
        for rid, idx in pts.groupby("run_id").indices.items():
            abc = tuple(coeffs.loc[rid, ["a", "b", "c"]]) if rid in coeffs.index else (1.0, 0.0, 0.0)
            out[idx] = model.apply_region(pts[col].values[idx], peak[idx], abc)
        pts[col] = out.round(2)
    return pts


def _actual(region: str, since: pd.Timestamp) -> pd.DataFrame:
    df = db.read_df("SELECT ts, price FROM prices WHERE region = ? AND ts >= ? ORDER BY ts", (region, _iso(since)))
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


# --------------------------------------------------------------------------- pages

def _asset_version() -> str:
    """Changes whenever a static file changes, so CDNs/browsers fetch the new copy after a deploy."""
    return format(int(max(f.stat().st_mtime for f in STATIC.iterdir() if f.is_file())), "x")


@app.get("/", include_in_schema=False)
def index():
    html = (STATIC / "index.html").read_text()
    v = _asset_version()
    for asset in ("/static/app.js", "/static/style.css"):
        html = html.replace(f'"{asset}"', f'"{asset}?v={v}"')
    # The page itself must never be cached, or it would keep pointing at old asset versions.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- API

@app.get("/api/regions")
def regions():
    return [{"region": k, "name": v} for k, v in config.REGIONS.items()]


@app.get("/api/status")
def status():
    runs = _runs(limit=1)
    last_price = db.latest_price_ts()
    if runs.empty:
        return {"ready": False, "last_price": None, "latest_run": None}
    r = runs.iloc[0]
    return {
        "ready": True,
        "last_price": last_price and _iso(last_price),
        "latest_run": {"id": int(r["id"]), "created_at": _iso(r["created_at"]),
                       "train_rows": int(r["train_rows"]), "holdout_mae": round(float(r["holdout_mae"]), 2)},
    }


@app.get("/api/{region}")
def forecast(region: str,
             days: int = Query(14, ge=1, le=14, description="Days ahead to return"),
             forecast_count: int = Query(1, ge=1, le=50, description="Number of most recent forecast runs")):
    """Latest forecast(s) for a region: [{name, created_at, prices: [{date_time, agile_pred, agile_low, agile_high}]}]."""
    region = _region(region)
    runs = _runs(limit=forecast_count)
    pts = _run_prices(runs["id"].tolist(), region)
    out = []
    for _, run in runs.iterrows():
        p = pts[(pts["run_id"] == run["id"]) & pts["p50"].notna()
                & (pts["ts"] < run["created_at"] + pd.Timedelta(days=days))]
        out.append({
            "name": f"{config.REGIONS[region]} forecast {run['created_at']:%Y-%m-%d %H:%M}",
            "region": region,
            "created_at": _iso(run["created_at"]),
            "prices": [{"date_time": _iso(t), "agile_pred": a, "agile_low": lo, "agile_high": hi}
                       for t, a, lo, hi in zip(p["ts"], p["p50"], p["p10"], p["p90"])],
        })
    return out


@app.get("/api/{region}/actual")
def actual(region: str, days: int = Query(7, ge=1, le=365)):
    region = _region(region)
    df = _actual(region, pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days))
    return [{"date_time": _iso(t), "price": p} for t, p in zip(df["ts"], df["price"])]


@app.get("/api/{region}/chart")
def chart(region: str,
          days: int = Query(7, ge=1, le=14),
          history_days: int = Query(2, ge=0, le=14),
          previous: int = Query(0, ge=0, le=7, description="Earlier forecasts to overlay, one per day back"),
          date: Optional[str] = Query(None, description="View a past day (YYYY-MM-DD, UK date) instead of now")):
    """Everything the front-end chart needs in one call."""
    region = _region(region)
    now = pd.Timestamp.now(tz="UTC")
    if date:
        try:
            day = pd.Timestamp(date).tz_localize(config.TZ)
        except (ValueError, TypeError):
            raise HTTPException(400, "date must be YYYY-MM-DD")
        # Use the forecast as it stood just before that day's prices were published (~16:00 the day before).
        as_of = min(now, day - pd.Timedelta(hours=8))
        start = day.tz_convert("UTC") - pd.Timedelta(days=history_days)
        end = day.tz_convert("UTC") + pd.Timedelta(days=days)
    else:
        as_of = now
        start = now.tz_convert(config.TZ).normalize().tz_convert("UTC") - pd.Timedelta(days=history_days)
        end = now + pd.Timedelta(days=days)

    runs = _runs(since=as_of - pd.Timedelta(days=previous + 1), until=as_of)
    if runs.empty and not date:
        raise HTTPException(503, "No forecast yet - the update job has not completed a run")
    chosen = []
    if not runs.empty:
        latest = runs.iloc[0]
        chosen = [latest]
        for k in range(1, previous + 1):
            older = runs[runs["created_at"] <= latest["created_at"] - pd.Timedelta(days=k)]
            if not older.empty and older.iloc[0]["id"] not in [c["id"] for c in chosen]:
                chosen.append(older.iloc[0])

    pts = _run_prices([int(c["id"]) for c in chosen], region)
    if not pts.empty:
        pts = pts[(pts["ts"] >= start) & (pts["ts"] < end)]
    act = _actual(region, start)
    act = act[act["ts"] < end]

    gen = db.read_df("SELECT * FROM gen_actual WHERE ts >= ? AND ts < ? ORDER BY ts", (_iso(start), _iso(end)))
    gen["ts"] = pd.to_datetime(gen["ts"], utc=True)

    def series(df, cols):
        return {"ts": [_iso(t) for t in df["ts"]], **{c: [None if pd.isna(v) else v for v in df[c]] for c in cols}}

    first_run = db.read_df("SELECT MIN(created_at) AS t FROM runs")["t"].iloc[0]
    latest_out = None
    if chosen:
        latest_pts = pts[pts["run_id"] == chosen[0]["id"]]
        latest_out = {"created_at": _iso(chosen[0]["created_at"]),
                      **series(latest_pts, ["p10", "p50", "p90", "demand", "wind", "emb_wind", "emb_solar"])}
    return {
        "region": region,
        "name": config.REGIONS[region],
        "date": date,
        "first_forecast": first_run,
        "actual": series(act, ["price"]),
        "latest": latest_out,
        "previous": [{"created_at": _iso(c["created_at"]), **series(pts[pts["run_id"] == c["id"]], ["p50"])}
                     for c in chosen[1:]],
        "gen_actual": series(gen, ["demand", "wind", "emb_wind", "emb_solar"]),
    }


@app.get("/api/{region}/week")
def week(region: str, start: Optional[str] = Query(None, description="Any date in the week (YYYY-MM-DD); default this week")):
    """Monday-Sunday view: each day is paired with the forecast made just before its prices were published."""
    region = _region(region)
    now = pd.Timestamp.now(tz="UTC")
    try:
        day = pd.Timestamp(start) if start else now.tz_convert(config.TZ).tz_localize(None).normalize()
    except (ValueError, TypeError):
        raise HTTPException(400, "start must be YYYY-MM-DD")
    monday = (day - pd.Timedelta(days=day.dayofweek)).normalize()
    days = [(monday + pd.Timedelta(days=i)).tz_localize(config.TZ) for i in range(7)]
    w_start, w_end = days[0].tz_convert("UTC"), (days[-1] + pd.Timedelta(days=1)).tz_convert("UTC")

    runs = _runs(since=w_start - pd.Timedelta(days=4), until=min(now, w_end))
    # For each day: the newest run made before ~16:00 the previous day (or the newest run, for days not yet priced).
    picks = []
    for d in days:
        as_of = min(now, d - pd.Timedelta(hours=8))
        cand = runs[(runs["created_at"] <= as_of) & (runs["created_at"] >= as_of - pd.Timedelta(days=3))]
        picks.append(None if cand.empty else cand.iloc[0])

    run_ids = sorted({int(r["id"]) for r in picks if r is not None})
    pts = _run_prices(run_ids, region)
    parts = []
    for d, r in zip(days, picks):
        if r is None or pts.empty:
            continue
        lo, hi = d.tz_convert("UTC"), (d + pd.Timedelta(days=1)).tz_convert("UTC")
        part = pts[(pts["run_id"] == r["id"]) & (pts["ts"] >= lo) & (pts["ts"] < hi)].copy()
        part["made"] = _iso(r["created_at"])
        parts.append(part)
    fc = pd.concat(parts) if parts else pd.DataFrame(columns=["ts", "p10", "p50", "p90", "demand", "wind",
                                                              "emb_wind", "emb_solar", "made"])
    act = _actual(region, w_start)
    act = act[act["ts"] < w_end]
    gen = db.read_df("SELECT * FROM gen_actual WHERE ts >= ? AND ts < ? ORDER BY ts", (_iso(w_start), _iso(w_end)))
    gen["ts"] = pd.to_datetime(gen["ts"], utc=True)

    def series(df, cols):
        return {"ts": [_iso(t) for t in df["ts"]], **{c: [None if pd.isna(v) else v for v in df[c]] for c in cols}}

    joined = fc.dropna(subset=["p50"]).merge(act, on="ts") if not fc.empty else pd.DataFrame()
    summary = []
    for d, r in zip(days, picks):
        lo, hi = d.tz_convert("UTC"), (d + pd.Timedelta(days=1)).tz_convert("UTC")
        a = act[(act["ts"] >= lo) & (act["ts"] < hi)]
        j = joined[(joined["ts"] >= lo) & (joined["ts"] < hi)] if not joined.empty else joined
        summary.append({
            "date": d.strftime("%Y-%m-%d"),
            "forecast_made": None if r is None else _iso(r["created_at"]),
            "actual_mean": None if a.empty else round(float(a["price"].mean()), 2),
            "actual_min": None if a.empty else round(float(a["price"].min()), 2),
            "actual_min_at": None if a.empty else _iso(a.loc[a["price"].idxmin(), "ts"]),
            "forecast_mae": None if j.empty else round(float((j["p50"] - j["price"]).abs().mean()), 2),
        })
    first_run = db.read_df("SELECT MIN(created_at) AS t FROM runs")["t"].iloc[0]
    return {
        "region": region,
        "name": config.REGIONS[region],
        "week_start": days[0].strftime("%Y-%m-%d"),
        "first_forecast": first_run,
        "actual": series(act, ["price"]),
        "latest": None if fc.empty else {"created_at": None, **series(fc, ["p10", "p50", "p90", "demand", "wind",
                                                                        "emb_wind", "emb_solar", "made"])},
        "previous": [],
        "gen_actual": series(gen, ["demand", "wind", "emb_wind", "emb_solar"]),
        "days": summary,
        "week_mae": None if joined.empty else round(float((joined["p50"] - joined["price"]).abs().mean()), 2),
    }


@app.get("/api/{region}/accuracy")
def accuracy(region: str, days: int = Query(30, ge=1, le=180)):
    """How past forecasts compared with the prices that were eventually published."""
    region = _region(region)
    now = pd.Timestamp.now(tz="UTC")
    runs = _runs(since=now - pd.Timedelta(days=days))
    empty = {"by_horizon": [], "runs": [], "overall_mae": None}
    if runs.empty:
        return empty
    pts = _run_prices(runs["id"].tolist(), region)
    act = _actual(region, runs["created_at"].min())
    df = pts.dropna(subset=["p50"]).merge(act, on="ts").merge(runs[["id", "created_at"]], left_on="run_id", right_on="id")
    if df.empty:
        return empty
    df["err"] = (df["p50"] - df["price"]).abs()
    df["inside"] = (df["price"] >= df["p10"]) & (df["price"] <= df["p90"])
    df["horizon"] = np.ceil((df["ts"] - df["created_at"]).dt.total_seconds() / 86400).clip(lower=1).astype(int)

    by_h = df.groupby("horizon").agg(mae=("err", "mean"), coverage=("inside", "mean"), n=("err", "size")).reset_index()
    by_run = (df.groupby(["run_id", "created_at"])
                .agg(mae=("err", "mean"), n=("err", "size")).reset_index()
                .sort_values("created_at", ascending=False))
    return {
        "overall_mae": round(float(df["err"].mean()), 2),
        "by_horizon": [{"days_ahead": int(h), "mae": round(m, 2), "coverage": round(c, 3), "n": int(n)}
                       for h, m, c, n in by_h.itertuples(index=False)],
        "runs": [{"id": int(r), "created_at": _iso(t), "mae": round(m, 2), "n": int(n)}
                 for r, t, m, n in by_run.itertuples(index=False)],
    }
