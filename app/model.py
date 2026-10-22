"""Price model.

The national-average Agile price is modelled from generation/demand fundamentals with gradient-boosted
quantile regression (10/50/90%). Regional prices are then derived with a per-region linear map fitted on
recent actuals: Agile is linear in the wholesale price plus a 16:00-19:00 peak adder, so
    price_region ~= a * price_national + b * is_peak + c
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from . import config

FEATURES = ["demand", "wind", "emb_wind", "emb_solar", "residual", "renewable_share",
            "hour", "dow", "is_weekend", "is_peak", "price_level"]
QUANTILES = {"p10": 0.1, "p50": 0.5, "p90": 0.9}
LEVEL_WINDOW_DAYS = 14
LEVEL_LAG_DAYS = 2
RECENCY_HALF_LIFE_DAYS = 90


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


def _fit(X: pd.DataFrame, y: pd.Series, w: np.ndarray) -> dict[str, HistGradientBoostingRegressor]:
    models = {}
    for name, q in QUANTILES.items():
        m = HistGradientBoostingRegressor(loss="quantile", quantile=q, max_iter=400, learning_rate=0.05,
                                          max_leaf_nodes=31, min_samples_leaf=40, l2_regularization=1.0,
                                          categorical_features=[FEATURES.index("dow")], random_state=0)
        m.fit(X, y, sample_weight=w)
        models[name] = m
    return models


def _predict(models, X: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({k: m.predict(X) for k, m in models.items()}, index=X.index)
    out[["p10", "p50", "p90"]] = np.sort(out[["p10", "p50", "p90"]].values, axis=1)  # no quantile crossing
    return out.clip(upper=config.PRICE_CAP)


def train_and_predict(history: pd.DataFrame, national: pd.Series, future: pd.DataFrame):
    """
    history: actual gen/demand (UTC index), national: actual national price, future: forecast inputs.
    Returns (predictions DataFrame p10/p50/p90, train_rows, holdout_mae).
    """
    level = daily_price_level(national)
    train = attach_price_level(history, level).join(national.rename("price"), how="inner").dropna()
    if len(train) < 48 * 30:
        raise RuntimeError(f"Only {len(train)} training rows available; need at least 30 days of history")

    age_days = (train.index.max() - train.index).total_seconds() / 86400
    weights = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    X, y = make_features(train), train["price"]

    # Honest error estimate: hold out the last 14 days, then refit on everything.
    cutoff = train.index.max() - pd.Timedelta(days=14)
    tr = train.index <= cutoff
    holdout = _predict(_fit(X[tr], y[tr], weights[tr]), X[~tr])
    holdout_mae = float(np.mean(np.abs(holdout["p50"] - y[~tr])))

    models = _fit(X, y, weights)

    # For the forecast, the price level is simply the mean of the most recent actuals.
    recent = national[national.index > national.index.max() - pd.Timedelta(days=LEVEL_WINDOW_DAYS)]
    fut = future.copy()
    fut["price_level"] = recent.mean()
    preds = _predict(models, make_features(fut))
    return preds, len(train), holdout_mae


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
