"""SQLite storage. All timestamps are stored as UTC ISO strings: 2026-09-29T16:00:00Z."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    region TEXT NOT NULL,
    ts     TEXT NOT NULL,
    price  REAL NOT NULL,
    PRIMARY KEY (region, ts)
);
CREATE TABLE IF NOT EXISTS gen_actual (
    ts        TEXT PRIMARY KEY,
    demand    REAL,
    emb_wind  REAL,
    emb_solar REAL,
    wind      REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    train_rows INTEGER,
    holdout_mae REAL
);
CREATE TABLE IF NOT EXISTS run_points (
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts        TEXT NOT NULL,
    p10       REAL,
    p50       REAL,
    p90       REAL,
    demand    REAL,
    emb_wind  REAL,
    emb_solar REAL,
    wind      REAL,
    PRIMARY KEY (run_id, ts)
);
CREATE TABLE IF NOT EXISTS run_regions (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    region TEXT NOT NULL,
    a REAL, b REAL, c REAL,
    PRIMARY KEY (run_id, region)
);
CREATE TABLE IF NOT EXISTS run_bands (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    horizon     INTEGER NOT NULL,
    scale       REAL NOT NULL,
    low_offset  REAL NOT NULL,
    high_offset REAL NOT NULL,
    PRIMARY KEY (run_id, horizon)
);
CREATE INDEX IF NOT EXISTS idx_prices_ts ON prices(ts);
"""


def ts_str(index: pd.DatetimeIndex) -> list[str]:
    return list(index.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"))


@contextmanager
def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript(SCHEMA)


def upsert_prices(prices: pd.DataFrame):
    """prices: columns region, ts (UTC datetime), price."""
    if prices.empty:
        return
    rows = zip(prices["region"], ts_str(pd.DatetimeIndex(prices["ts"])), prices["price"].astype(float))
    with connect() as con:
        con.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?)", rows)


def upsert_gen(gen: pd.DataFrame):
    """gen: UTC DatetimeIndex, columns demand, emb_wind, emb_solar, wind. NaNs keep existing values."""
    if gen.empty:
        return
    gen = gen.reindex(columns=["demand", "emb_wind", "emb_solar", "wind"])
    rows = [
        (t, *[None if pd.isna(v) else float(v) for v in vals])
        for t, vals in zip(ts_str(gen.index), gen.itertuples(index=False))
    ]
    with connect() as con:
        con.executemany(
            """INSERT INTO gen_actual VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ts) DO UPDATE SET
                 demand    = COALESCE(excluded.demand, demand),
                 emb_wind  = COALESCE(excluded.emb_wind, emb_wind),
                 emb_solar = COALESCE(excluded.emb_solar, emb_solar),
                 wind      = COALESCE(excluded.wind, wind)""",
            rows,
        )


def read_df(sql: str, params=()) -> pd.DataFrame:
    with connect() as con:
        return pd.read_sql_query(sql, con, params=params)


def latest_price_ts() -> pd.Timestamp | None:
    df = read_df("SELECT MAX(ts) AS ts FROM prices")
    v = df["ts"].iloc[0]
    return None if v is None else pd.Timestamp(v)


def scored_points(since: pd.Timestamp) -> pd.DataFrame:
    """National forecast points from runs since `since` that now have a published price (for range calibration)."""
    df = read_df(
        """SELECT r.created_at, p.ts, p.p10, p.p50, p.p90, a.price
           FROM run_points p
           JOIN runs r ON r.id = p.run_id
           JOIN prices a ON a.region = 'X' AND a.ts = p.ts
           WHERE r.created_at >= ? AND p.p50 IS NOT NULL""",
        (since.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    for c in ("created_at", "ts"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df


def save_run(created_at: pd.Timestamp, train_rows: int, holdout_mae: float,
             points: pd.DataFrame, coeffs: dict[str, tuple[float, float, float]],
             bands: dict[int, tuple[float, float, float]] | None = None) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO runs (created_at, train_rows, holdout_mae) VALUES (?, ?, ?)",
            (created_at.strftime("%Y-%m-%dT%H:%M:%SZ"), int(train_rows), float(holdout_mae)),
        )
        run_id = cur.lastrowid
        cols = ["p10", "p50", "p90", "demand", "emb_wind", "emb_solar", "wind"]
        con.executemany(
            "INSERT INTO run_points VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, t, *[None if pd.isna(v) else float(v) for v in vals])
                for t, vals in zip(ts_str(points.index), points[cols].itertuples(index=False))
            ],
        )
        con.executemany(
            "INSERT INTO run_regions VALUES (?, ?, ?, ?, ?)",
            [(run_id, r, *abc) for r, abc in coeffs.items()],
        )
        con.executemany(
            "INSERT INTO run_bands VALUES (?, ?, ?, ?, ?)",
            [(run_id, int(h), *map(float, b)) for h, b in (bands or {}).items()],
        )
    return run_id


def prune_runs(before: pd.Timestamp):
    with connect() as con:
        con.execute("DELETE FROM runs WHERE created_at < ?", (before.strftime("%Y-%m-%dT%H:%M:%SZ"),))
