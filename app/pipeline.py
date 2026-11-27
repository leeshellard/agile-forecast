"""Update job: fetch fresh data, retrain, store a new forecast run.

Run with:  python -m app.pipeline
"""
from __future__ import annotations

import logging
import sys

import pandas as pd

from . import config, db, model, sources, usage

log = logging.getLogger("agile.pipeline")


def _fill_gaps(future: pd.DataFrame) -> pd.DataFrame:
    """Fill short gaps (e.g. tomorrow's demand before the 2-14 day feed starts) from the same time of day."""
    future = future.asfreq("30min")
    tod = future.index.tz_convert(config.TZ).strftime("%H:%M")
    for col in future:
        by_tod = future[col].groupby(tod)
        future[col] = future[col].fillna(by_tod.transform(lambda s: s.ffill().bfill()))
    return future


def _demand_from_history(future: pd.DataFrame, hist: pd.DataFrame, days: int = 14) -> pd.DataFrame:
    """If NESO has no demand forecast, use the average demand of the last `days` days for the same
    weekday and time of day. Demand follows a strong weekly pattern, so this is a reasonable stand-in."""
    if "demand" not in future:
        future["demand"] = float("nan")
    missing = future["demand"].isna()
    if not missing.any() or "demand" not in hist:
        return future
    recent = hist["demand"].dropna()
    if recent.empty:
        return future
    recent = recent[recent.index >= recent.index.max() - pd.Timedelta(days=days)]
    loc = recent.index.tz_convert(config.TZ)
    profile = recent.groupby([loc.dayofweek, loc.strftime("%H:%M")]).mean()
    floc = future.index.tz_convert(config.TZ)
    fill = pd.Series([profile.get(k, float("nan")) for k in zip(floc.dayofweek, floc.strftime("%H:%M"))],
                     index=future.index)
    filled = int((missing & fill.notna()).sum())
    if filled:
        log.warning("No NESO demand forecast for %d half-hours - using the last %d days' demand "
                    "for the same weekday and time instead", filled, days)
    future["demand"] = future["demand"].fillna(fill)
    return future


def _inputs_from_last_run(future: pd.DataFrame, cols=("wind", "emb_wind", "emb_solar")) -> pd.DataFrame:
    """If a NESO forecast feed is empty, reuse that input's forecast saved with the latest run.
    Half-hours that run didn't cover are left empty and dropped, so the forecast just gets shorter."""
    for c in cols:
        if c not in future:
            future[c] = float("nan")
    gaps = [c for c in cols if future[c].isna().any()]
    if not gaps:
        return future
    last = db.read_df("SELECT ts, " + ", ".join(gaps) + " FROM run_points WHERE run_id = (SELECT MAX(id) FROM runs)")
    if last.empty:
        return future
    last = last.set_index(pd.to_datetime(last.pop("ts"), utc=True)).astype(float).reindex(future.index)
    for c in gaps:
        filled = int((future[c].isna() & last[c].notna()).sum())
        if filled:
            log.warning("No NESO %s forecast for %d half-hours - reusing the previous run's forecast", c, filled)
        future[c] = future[c].fillna(last[c])
    return future


def update():
    db.init()
    now = pd.Timestamp.now(tz="UTC").floor("30min")
    last = db.latest_price_ts()
    first_run = last is None
    start = now - pd.Timedelta(days=config.HISTORY_DAYS if first_run else config.REFRESH_DAYS)
    if first_run:
        log.info("First run: backfilling %d days of history (this takes a minute or two)", config.HISTORY_DAYS)

    db.upsert_prices(sources.fetch_prices(start))

    gen = sources.fetch_demand_history(start)
    wind = sources.fetch_wind_history(start)
    db.upsert_gen(gen.join(wind, how="outer"))
    try:  # gas sharpens the model, but a National Gas outage must not stop the update
        stored = db.read_gas()
        gas_start = start - pd.Timedelta(days=3)
        if stored.empty or pd.Timestamp(stored.index.min()) > (now - pd.Timedelta(days=config.HISTORY_DAYS - 7)).tz_localize(None):
            gas_start = now - pd.Timedelta(days=config.HISTORY_DAYS)  # first run, or gas added to an existing install
        db.upsert_gas(sources.fetch_gas_prices(gas_start))
    except Exception:
        log.exception("Gas price fetch failed - using the gas prices already stored")

    # Load full training history from the DB.
    horizon_start = now - pd.Timedelta(days=config.HISTORY_DAYS)
    hist = db.read_df("SELECT * FROM gen_actual WHERE ts >= ?", (horizon_start.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    hist = hist.set_index(pd.to_datetime(hist.pop("ts"), utc=True)).astype(float)
    prices = db.read_df("SELECT region, ts, price FROM prices WHERE ts >= ?",
                        (horizon_start.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    prices["ts"] = pd.to_datetime(prices["ts"], utc=True)
    wide = prices.pivot_table(index="ts", columns="region", values="price").sort_index()
    national = wide["X"].dropna()

    # Forecast from the first period without a published price, up to 14 days out.
    fc_start = national.index.max() + pd.Timedelta(minutes=30)
    fc_end = min(now + pd.Timedelta(days=config.MAX_HORIZON_DAYS), fc_start + pd.Timedelta(days=config.MAX_HORIZON_DAYS))
    future = sources.fetch_input_forecasts()
    future = _fill_gaps(future)
    future = _demand_from_history(future, hist)
    future = _inputs_from_last_run(future)
    future = future[(future.index >= min(now, fc_start)) & (future.index < fc_end)].dropna()
    to_predict = future[future.index >= fc_start]
    if to_predict.empty:
        raise RuntimeError("No forecast inputs available beyond the last published price")

    gas = db.read_gas()
    if gas.empty:
        log.warning("No gas prices stored - forecasting from prices alone")
    preds, train_rows, holdout_mae = model.train_and_predict(hist, national, to_predict, gas)
    coeffs = model.fit_region_coeffs(wide)
    # Calibrate the 10-90% range on how recent forecasts actually fared (applied when the run is served).
    bands = model.band_calibration(db.scored_points(now - pd.Timedelta(days=config.CALIBRATION_DAYS)))
    # Inputs are kept from 'now' for the generation chart; prices are only predicted where none are published.
    points = future.join(preds)
    run_id = db.save_run(now, train_rows, holdout_mae, points, coeffs, bands)
    db.prune_runs(now - pd.Timedelta(days=config.KEEP_RUNS_DAYS))
    log.info("Run %d: %d forecast periods (%s to %s), trained on %d rows, holdout MAE %.2f p/kWh",
             run_id, len(points), points.index.min(), points.index.max(), train_rows, holdout_mae)
    if bands:
        log.info("Range calibration by day ahead (scale, low, high): %s",
                 ", ".join(f"{h}:({s:.2f}, {lo:+.1f}, {hi:+.1f})" for h, (s, lo, hi) in bands.items()))
    try:  # optional "My usage" data; a failure here must not lose the forecast run above
        if usage.sync():
            log.info("Usage data updated")
    except Exception:
        log.exception("Usage sync failed (forecast run was saved)")
    return run_id


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        update()
    except Exception:
        log.exception("Update failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
