"""Update job: fetch fresh data, retrain, store a new forecast run.

Run with:  python -m app.pipeline
"""
from __future__ import annotations

import logging
import sys

import pandas as pd

from . import config, db, model, sources

log = logging.getLogger("agile.pipeline")


def _fill_gaps(future: pd.DataFrame) -> pd.DataFrame:
    """Fill short gaps (e.g. tomorrow's demand before the 2-14 day feed starts) from the same time of day."""
    future = future.asfreq("30min")
    tod = future.index.tz_convert(config.TZ).strftime("%H:%M")
    for col in future:
        by_tod = future[col].groupby(tod)
        future[col] = future[col].fillna(by_tod.transform(lambda s: s.ffill().bfill()))
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
    future = future[(future.index >= min(now, fc_start)) & (future.index < fc_end)].dropna()
    to_predict = future[future.index >= fc_start]
    if to_predict.empty:
        raise RuntimeError("No forecast inputs available beyond the last published price")

    preds, train_rows, holdout_mae = model.train_and_predict(hist, national, to_predict)
    coeffs = model.fit_region_coeffs(wide)
    # Inputs are kept from 'now' for the generation chart; prices are only predicted where none are published.
    points = future.join(preds)
    run_id = db.save_run(now, train_rows, holdout_mae, points, coeffs)
    db.prune_runs(now - pd.Timedelta(days=config.KEEP_RUNS_DAYS))
    log.info("Run %d: %d forecast periods (%s to %s), trained on %d rows, holdout MAE %.2f p/kWh",
             run_id, len(points), points.index.min(), points.index.max(), train_rows, holdout_mae)
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
