"""Price model.

The national-average Agile price is modelled from generation/demand fundamentals with gradient-boosted
quantile regression (10/50/90%). Regional prices are then derived with a per-region linear map fitted on
recent actuals: Agile is linear in the wholesale price plus a 16:00-19:00 peak adder, so
    price_region ~= a * price_national + b * is_peak + c

When the daily wholesale gas price is available, the model predicts price as a multiple of it (gas sets the
price most of the time, so the daily shape learned when gas was cheap scales up when it is dear).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from . import config

FEATURES = ["demand", "wind", "emb_wind", "emb_solar", "residual", "renewable_share",
            "hour", "dow", "is_weekend", "is_peak", "price_level", "gas"]
QUANTILES = {"p10": 0.1, "p50": 0.5, "p90": 0.9}
LEVEL_WINDOW_DAYS = 14
LEVEL_LAG_DAYS = 2
# Backtest on Oct-Nov 2026 (35 daily origins, live forecast inputs): price-as-a-multiple-of-gas with a
# 30-day half-life cut mean error from 6.40 to 5.95 p/kWh (day ahead 6.28 -> 5.18) against the old
# no-gas model with a 90-day half-life.
RECENCY_HALF_LIFE_DAYS = 30
GAS_FLOOR = 0.5        # p/kWh; guards the price/gas ratio against a bad near-zero gas value
BAND_TARGET = 0.8      # share of actual prices the 10-90% range should contain
BAND_MIN_SCORED = 96   # two days of half-hours before a day-ahead gets its own widening factor
BAND_MAX_SCALE = 6.0
BAND_MIN_HALF = 0.5    # p/kWh; stops a near-zero model width producing a huge ratio


def is_peak(index: pd.DatetimeIndex) -> np.ndarray:
    local = index.tz_convert(config.TZ)
    return ((local.hour >= 16) & (local.hour < 19)).astype(int)


def make_features(df: pd.DataFrame) -> pd.DataFrame:
    """df: UTC half-hour index with demand, wind, emb_wind, emb_solar, price_level."""
    local = df.index.tz_convert(config.TZ)
    X = pd.DataFrame(index=df.index)
    for c in ["demand", "wind", "emb_wind", "emb_solar"]:
        X[c] = df[c] / 1000.0  # GW
    # ND already nets off embedded generation, so residual = what dispatchable plant must cover.
    X["residual"] = X["demand"] - X["wind"]
    X["renewable_share"] = (X["wind"] + X["emb_wind"] + X["emb_solar"]) / (X["demand"] + X["emb_wind"] + X["emb_solar"])
    X["hour"] = local.hour + local.minute / 60
    X["dow"] = local.dayofweek
    X["is_weekend"] = (local.dayofweek >= 5).astype(int)
    X["is_peak"] = is_peak(df.index)
    X["price_level"] = df["price_level"]
    X["gas"] = df["gas"] if "gas" in df else np.nan
    return X[FEATURES]


def daily_price_level(national: pd.Series) -> pd.Series:
    """Trailing mean price, a proxy for gas/carbon costs that the fundamentals can't see."""
    daily = national.tz_convert(config.TZ).resample("D").mean()
    return daily.rolling(LEVEL_WINDOW_DAYS, min_periods=7).mean().shift(LEVEL_LAG_DAYS)


def attach_price_level(df: pd.DataFrame, level: pd.Series) -> pd.DataFrame:
    days = df.index.tz_convert(config.TZ).normalize()
    out = df.copy()
    out["price_level"] = level.reindex(days).values
    return out


def attach_gas(df: pd.DataFrame, gas: pd.Series | None) -> pd.DataFrame:
    """Gas price known when each day's prices are set: the previous gas day's SAP (the day-ahead auction runs
    the day before). Days after the latest published value carry it forward."""
    out = df.copy()
    if gas is None or gas.empty:
        out["gas"] = np.nan
        return out
    g = pd.Series(gas.values, index=pd.DatetimeIndex(pd.to_datetime(list(gas.index)))).sort_index()
    days = pd.DatetimeIndex(df.index.tz_convert(config.TZ).tz_localize(None).normalize()) - pd.Timedelta(days=1)
    out["gas"] = g.reindex(days, method="ffill").values
    return out


def _fit(X: pd.DataFrame, y: pd.Series, w: np.ndarray, feats=FEATURES) -> dict[str, HistGradientBoostingRegressor]:
    models = {}
    for name, q in QUANTILES.items():
        m = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=400, learning_rate=0.05,
                                          max_leaf_nodes=31, min_samples_leaf=40, l2_regularization=1.0,
                                          categorical_features=[list(feats).index("dow")], random_state=0)
        m.fit(X, y, sample_weight=w)
        models[name] = m
    return models


def _predict(models, X: pd.DataFrame, scale: np.ndarray | None = None) -> pd.DataFrame:
    out = pd.DataFrame({k: m.predict(X) for k, m in models.items()}, index=X.index)
    if scale is not None:
        out = out.mul(scale, axis=0)
    out[["p10", "p50", "p90"]] = np.sort(out[["p10", "p50", "p90"]].values, axis=1)  # no quantile crossing
    return out.clip(upper=config.PRICE_CAP)


def train_and_predict(history: pd.DataFrame, national: pd.Series, future: pd.DataFrame,
                      gas: pd.Series | None = None):
    """
    history: actual gen/demand (UTC index), national: actual national price, future: forecast inputs,
    gas: daily wholesale gas price (p/kWh) by gas day; without it the model falls back to price alone.
    Returns (predictions DataFrame p10/p50/p90, train_rows, holdout_mae).
    """
    level = daily_price_level(national)
    train = attach_gas(attach_price_level(history, level), gas).join(national.rename("price"), how="inner")
    use_gas = bool(train["gas"].notna().sum() >= 48 * 30)
    feats = FEATURES if use_gas else [f for f in FEATURES if f != "gas"]
    train = train.dropna(subset=[c for c in train if c != "gas" or use_gas])
    if len(train) < 48 * 30:
        raise RuntimeError(f"Only {len(train)} training rows available; need at least 30 days of history")

    age_days = (train.index.max() - train.index).total_seconds() / 86400
    weights = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    X = make_features(train)[feats]
    scale = np.maximum(X["gas"].to_numpy(), GAS_FLOOR) if use_gas else np.ones(len(X))
    y = train["price"] / scale  # price as a multiple of the gas price (or plain price without gas)

    # Honest error estimate: hold out the last 14 days, then refit on everything.
    cutoff = train.index.max() - pd.Timedelta(days=14)
    tr = train.index <= cutoff
    holdout = _predict(_fit(X[tr], y[tr], weights[tr], feats), X[~tr], scale[~tr])
    holdout_mae = float(np.mean(np.abs(holdout["p50"] - train["price"][~tr])))

    models = _fit(X, y, weights, feats)

    # For the forecast, the price level is simply the mean of the most recent actuals, and gas is the latest price.
    recent = national[national.index > national.index.max() - pd.Timedelta(days=LEVEL_WINDOW_DAYS)]
    fut = attach_gas(future, gas)
    fut["price_level"] = recent.mean()
    Xf = make_features(fut)[feats]
    if use_gas:
        Xf["gas"] = Xf["gas"].fillna(train["gas"].iloc[-1])
    fscale = np.maximum(Xf["gas"].to_numpy(), GAS_FLOOR) if use_gas else None
    preds = _predict(models, Xf, fscale)
    return preds, len(train), holdout_mae


def horizon_days(ts, made) -> np.ndarray:
    """Whole days ahead of the forecast run (1 = within 24 hours), as used for scoring and calibration."""
    days = (pd.Series(ts).values - pd.Series(made).values) / np.timedelta64(1, "D")
    return np.clip(np.ceil(days), 1, config.MAX_HORIZON_DAYS).astype(int)


def band_calibration(scored: pd.DataFrame) -> dict[int, tuple[float, float, float]]:
    """
    scored: past national forecast points with created_at, ts, p10, p50, p90 and the published price.
    Returns {days_ahead: (scale, low_offset, high_offset)}.

    The quantile models are over-confident, so their 10-90% range holds far fewer than 80% of actual prices.
    Two corrections are worked out for each day ahead, and `widen` uses whichever gives the wider range:
      - scale: how far the half-widths (p50-p10 below, p90-p50 above) must be stretched for 80% of recent
        actuals to have landed inside. This keeps the model's sense of which half-hours are less certain.
      - offsets: the 10th and 90th percentiles of recent misses (actual - p50), so the range is never narrower
        than recent errors, even where the model's own range is close to zero.
    A later day is never more certain than an earlier one; days with too little data use all days pooled.
    """
    if scored.empty:
        return {}
    p10, p50, p90, price = (scored[c].to_numpy(float) for c in ("p10", "p50", "p90", "price"))
    miss = price - p50
    half = np.where(miss >= 0, p90 - p50, p50 - p10)
    ratio = np.abs(miss) / np.maximum(half, BAND_MIN_HALF)
    horizon = horizon_days(scored["ts"], scored["created_at"])
    lo_q, hi_q = (1 - BAND_TARGET) / 2, (1 + BAND_TARGET) / 2

    def stats(mask):
        r, m = (ratio, miss) if mask.sum() < BAND_MIN_SCORED else (ratio[mask], miss[mask])
        if len(r) < BAND_MIN_SCORED:
            return 1.0, 0.0, 0.0
        return float(np.quantile(r, BAND_TARGET)), float(np.quantile(m, lo_q)), float(np.quantile(m, hi_q))

    days = range(1, config.MAX_HORIZON_DAYS + 1)
    raw = np.array([stats(horizon == h) for h in days])
    scale = np.clip(np.maximum.accumulate(raw[:, 0]), 1.0, BAND_MAX_SCALE)
    low = np.minimum.accumulate(raw[:, 1])
    high = np.maximum.accumulate(raw[:, 2])
    return {h: (round(float(s), 3), round(float(lo), 2), round(float(hi), 2))
            for h, s, lo, hi in zip(days, scale, low, high)}


def widen(p10, p50, p90, scale=1.0, low_offset=0.0, high_offset=0.0):
    """Calibrated range: the wider of the stretched model range and p50 + recent-miss offsets.
    The defaults leave the model range unchanged."""
    lo = np.minimum(p50 - scale * (p50 - p10), p50 + low_offset)
    hi = np.maximum(p50 + scale * (p90 - p50), p50 + high_offset)
    return lo, hi


def fit_region_coeffs(prices_wide: pd.DataFrame, days: int = 60) -> dict[str, tuple[float, float, float]]:
    """prices_wide: UTC index, one column per region including 'X'."""
    recent = prices_wide[prices_wide.index > prices_wide.index.max() - pd.Timedelta(days=days)].dropna()
    peak = is_peak(recent.index)
    A = np.column_stack([recent["X"].values, peak, np.ones(len(recent))])
    coeffs = {"X": (1.0, 0.0, 0.0)}
    for r in config.DNO_REGIONS:
        if r in recent:
            sol, *_ = np.linalg.lstsq(A, recent[r].values, rcond=None)
            coeffs[r] = tuple(float(v) for v in sol)
    return coeffs


def apply_region(values: np.ndarray, peak: np.ndarray, abc: tuple[float, float, float]) -> np.ndarray:
    a, b, c = abc
    return np.minimum(a * values + b * peak + c, config.PRICE_CAP)
