"""Fetchers for upstream data: Octopus (prices), NESO (demand, embedded + metered wind forecasts), Elexon (wind actuals).

Every fetcher returns data indexed by the UTC start time of each half-hour period.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

from . import config

log = logging.getLogger(__name__)

OCTOPUS = "https://api.octopus.energy/v1"
NESO = "https://api.neso.energy/api/3/action"
ELEXON = "https://data.elexon.co.uk/bmrs/api/v1"

# NESO resource IDs for rolling forecasts (stable across updates).
NESO_EMBEDDED_FORECAST = "db6c038f-98af-4570-ab60-24d71ebd0ae5"
NESO_METERED_WIND_14D = "b1ae17c2-cc5e-4254-9bbf-3eddcf30d033"
NESO_DEMAND_2_14D = "7c0411cd-2714-4bb5-a408-adb065edf34d"
NESO_DEMAND_UPDATE = "177f6fa4-ae49-4182-81ea-0c6b35f26ca6"

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT


def _get(url: str, **params) -> dict | list:
    r = _session.get(url, params=params or None, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def settlement_to_utc(dates: pd.Series, periods: pd.Series) -> pd.DatetimeIndex:
    """Settlement period 1 starts at 00:00 UK local time; adding in UTC handles clock-change days."""
    midnight = pd.to_datetime(dates).dt.tz_localize(config.TZ, ambiguous="NaT", nonexistent="NaT")
    return pd.DatetimeIndex(midnight.dt.tz_convert("UTC") + pd.to_timedelta((periods.astype(int) - 1) * 30, unit="min"))


# --------------------------------------------------------------------------- Octopus

def resolve_product() -> str:
    if config.PRODUCT_CODE:
        return config.PRODUCT_CODE
    data = _get(f"{OCTOPUS}/products/", brand="OCTOPUS_ENERGY", is_variable="true", page_size=200)
    agile = [p for p in data["results"] if p["code"].startswith("AGILE-") and "OUTGOING" not in p["code"]]
    if not agile:
        raise RuntimeError("No Agile import product found on the Octopus API; set AGILE_PRODUCT")
    return max(agile, key=lambda p: p["available_from"] or "")["code"]


def _region_prices(product: str, region: str, start: pd.Timestamp) -> pd.DataFrame:
    url = f"{OCTOPUS}/products/{product}/electricity-tariffs/E-1R-{product}-{region}/standard-unit-rates/"
    params = {"period_from": start.strftime("%Y-%m-%dT%H:%MZ"), "page_size": 1500}
    rows = []
    while url:
        data = _get(url, **params) if params else _get(url)
        rows += data["results"]
        url, params = data.get("next"), None
    if not rows:
        return pd.DataFrame(columns=["region", "ts", "price"])
    df = pd.DataFrame(rows)
    return pd.DataFrame({
        "region": region,
        "ts": pd.to_datetime(df["valid_from"], utc=True),
        "price": df["value_inc_vat"].astype(float),
    })


def fetch_prices(start: pd.Timestamp) -> pd.DataFrame:
    """All DNO regions plus 'X' (national average = mean of the 14 regions)."""
    product = resolve_product()
    log.info("Fetching Agile prices for %s since %s", product, start)
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = list(pool.map(lambda r: _region_prices(product, r, start), config.DNO_REGIONS))
    prices = pd.concat(frames, ignore_index=True)
    if prices.empty:
        return prices
    wide = prices.pivot_table(index="ts", columns="region", values="price")
    wide = wide[wide.notna().sum(axis=1) >= len(config.DNO_REGIONS) - 1]  # tolerate one missing region
    national = pd.DataFrame({"region": "X", "ts": wide.index, "price": wide.mean(axis=1).round(4).values})
    return pd.concat([prices, national], ignore_index=True)


# --------------------------------------------------------------------------- NESO

def _neso_all(resource_id: str) -> pd.DataFrame:
    rows, offset = [], 0
    while True:
        data = _get(f"{NESO}/datastore_search", resource_id=resource_id, limit=32000, offset=offset)["result"]
        rows += data["records"]
        offset += len(data["records"])
        if not data["records"] or offset >= data.get("total", 0):
            break
    return pd.DataFrame(rows)


def _historic_demand_resources(years: set[int]) -> list[str]:
    pkg = _get(f"{NESO}/package_show", id="historic-demand-data")["result"]
    return [r["id"] for r in pkg["resources"] if any(r["name"].strip().endswith(str(y)) for y in years)]


def fetch_demand_history(start: pd.Timestamp) -> pd.DataFrame:
    """Actual national demand (ND) and embedded wind/solar generation, in MW."""
    now = pd.Timestamp.now(tz="UTC")
    ids = _historic_demand_resources(set(range(start.year, now.year + 1))) + [NESO_DEMAND_UPDATE]
    frames = []
    for rid in ids:
        df = _neso_all(rid)
        if df.empty:
            continue
        if "FORECAST_ACTUAL_INDICATOR" in df:
            df = df[df["FORECAST_ACTUAL_INDICATOR"] != "F"]
        df = df[df["ND"] > 0]
        idx = settlement_to_utc(df["SETTLEMENT_DATE"], df["SETTLEMENT_PERIOD"])
        frames.append(pd.DataFrame({
            "demand": df["ND"].values,
            "emb_wind": df["EMBEDDED_WIND_GENERATION"].values,
            "emb_solar": df["EMBEDDED_SOLAR_GENERATION"].values,
        }, index=idx))
    out = pd.concat(frames)
    out = out[out.index.notna()]
    out = out[~out.index.duplicated(keep="last")].sort_index()  # later resources (the update feed) win
    return out[out.index >= start].astype(float)


def fetch_input_forecasts() -> pd.DataFrame:
    """Latest NESO forecasts of the model inputs for roughly the next 14 days."""
    emb = _neso_all(NESO_EMBEDDED_FORECAST)
    emb_idx = pd.to_datetime(emb["DATE_GMT"]).dt.normalize() + pd.to_timedelta(emb["TIME_GMT"] + ":00")
    emb_df = pd.DataFrame({
        "emb_wind": emb["EMBEDDED_WIND_FORECAST"].values,
        "emb_solar": emb["EMBEDDED_SOLAR_FORECAST"].values,
    }, index=pd.DatetimeIndex(emb_idx).tz_localize("UTC"))

    wind = _neso_all(NESO_METERED_WIND_14D)
    wind_df = pd.DataFrame({"wind": wind["Wind_Forecast"].values},
                           index=pd.DatetimeIndex(pd.to_datetime(wind["Datetime"])).tz_localize("UTC"))

    dem = _neso_all(NESO_DEMAND_2_14D)
    # GDATETIME is the *end* of each half-hour (CTIME runs 0030..2400), so shift back to the period start.
    dem_idx = pd.DatetimeIndex(pd.to_datetime(dem["GDATETIME"])).tz_localize("UTC") - pd.Timedelta(minutes=30)
    dem_df = pd.DataFrame({"demand": dem["NATIONALDEMAND"].values}, index=dem_idx)

    # Short-term: the demand-update feed includes forecast rows for the next few days.
    upd = _neso_all(NESO_DEMAND_UPDATE)
    upd = upd[(upd["FORECAST_ACTUAL_INDICATOR"] == "F") & (upd["ND"] > 0)]
    if not upd.empty:
        upd_df = pd.DataFrame({"demand": upd["ND"].values},
                              index=settlement_to_utc(upd["SETTLEMENT_DATE"], upd["SETTLEMENT_PERIOD"]))
        dem_df = dem_df.combine_first(upd_df)

    frames = [f[~f.index.duplicated(keep="last")] for f in (emb_df, wind_df, dem_df)]
    return pd.concat(frames, axis=1).sort_index().astype(float)


# --------------------------------------------------------------------------- Elexon

def fetch_wind_history(start: pd.Timestamp) -> pd.Series:
    """Actual transmission-metered wind generation (MW) from Elexon FUELHH."""
    end = pd.Timestamp.now(tz="UTC")
    frames, cursor = [], start.normalize()
    while cursor <= end:  # chunk by ~6 months to keep responses modest
        chunk_end = min(cursor + pd.Timedelta(days=180), end)
        data = _get(f"{ELEXON}/datasets/FUELHH/stream", fuelType="WIND",
                    settlementDateFrom=cursor.strftime("%Y-%m-%d"), settlementDateTo=chunk_end.strftime("%Y-%m-%d"))
        if data:
            frames.append(pd.DataFrame(data))
        cursor = chunk_end + pd.Timedelta(days=1)
    if not frames:
        return pd.Series(dtype=float, name="wind")
    df = pd.concat(frames).sort_values("publishTime")
    df["ts"] = pd.to_datetime(df["startTime"], utc=True)
    df = df.drop_duplicates("ts", keep="last").set_index("ts").sort_index()
    return df["generation"].astype(float).rename("wind")
