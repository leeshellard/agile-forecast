import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("AGILE_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "agile.db"

# Leave blank to auto-detect the newest Agile import product from the Octopus API.
PRODUCT_CODE = os.environ.get("AGILE_PRODUCT", "")

# How much history to backfill on first run, and how much to re-fetch on each update.
HISTORY_DAYS = int(os.environ.get("AGILE_HISTORY_DAYS", "365"))
REFRESH_DAYS = int(os.environ.get("AGILE_REFRESH_DAYS", "10"))

# Forecasts older than this are pruned from the history tables.
KEEP_RUNS_DAYS = int(os.environ.get("AGILE_KEEP_RUNS_DAYS", "180"))

MAX_HORIZON_DAYS = 14
PRICE_CAP = 100.0  # Agile import cap, p/kWh inc VAT
TZ = "Europe/London"

REGIONS = {
    "X": "National Average",
    "A": "Eastern England",
    "B": "East Midlands",
    "C": "London",
    "D": "Merseyside and Northern Wales",
    "E": "West Midlands",
    "F": "North Eastern England",
    "G": "North Western England",
    "H": "Southern England",
    "J": "South Eastern England",
    "K": "Southern Wales",
    "L": "South Western England",
    "M": "Yorkshire",
    "N": "Southern Scotland",
    "P": "Northern Scotland",
}
DNO_REGIONS = [r for r in REGIONS if r != "X"]

HTTP_TIMEOUT = 60
USER_AGENT = "agile-forecast-selfhosted/1.0"
