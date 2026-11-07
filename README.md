# Agile Forecast

A self-hosted forecaster for Octopus **Agile** electricity prices, up to 14 days ahead, for all 14 regions. It was inspired by [AgilePredict](https://agilepredict.com).

- Web UI with forecast vs actual prices, a calibrated 10–90% range, earlier-forecast overlays and generation/demand inputs
- JSON API (easy to use from Home Assistant)
- Optional personal pages, once your Octopus details are added: **My usage** (`/usage`, your half-hourly usage costed on the tariff you were on, with export income and a daily cost chart), **Compare tariffs** (`/compare`, your usage priced on Cosy, Go and Flexible at today's rates) and **Winter savings** (`/winter`, what moving usage out of the dearest half-hours would save)
- Stores every forecast run and scores it against the published prices, so you can see how accurate it is by days ahead

## How it works

| Step | Source |
|---|---|
| Actual Agile prices, 14 regions | Octopus Energy public API |
| National demand + embedded wind/solar (history and 14-day forecast) | NESO Data Portal |
| 14-day metered wind forecast | NESO Data Portal |
| Metered wind outturn (history) | Elexon BMRS (FUELHH) |

1. **Target:** the national average price is the mean of the 14 regional prices.
2. **Model:** scikit-learn `HistGradientBoostingRegressor` with quantile loss (10/50/90%), trained on about a year of half-hourly data. Recent data counts for more (90-day half-life).
   - Features: demand, metered wind, embedded wind and solar, residual demand, renewable share, time of day, day of week, peak flag.
   - A trailing 14-day price level stands in for gas and carbon costs, which the fundamentals can't see.
3. **Regional prices:** Agile is linear in the wholesale price, plus a 16:00–19:00 peak adder. So each region is fitted as `a × national + b × peak + c` over the last 60 days. No tariff constants are hard-coded.
4. **Range calibration:** the quantile models are over-confident. On their own, only about 20–40% of actual prices land inside their 10–90% range, and almost none 9 or more days ahead. So each run also checks the last 28 days of scored forecasts and works out two corrections for each day ahead:
   - how far the model's range has to be stretched around the central forecast for 80% of actual prices to have landed inside; and
   - the 10th and 90th percentiles of recent misses, so the range is never narrower than recent errors.

   The wider of the two is shown. In a backtest on October 2026 data, this raised coverage from 18% to about 73% (about 75% for 1–3 days ahead), and it should get closer to 80% as more history builds up. The stored model output is left as it is and the calibration is applied when the forecast is served. The range is never narrowed, and a later day is never shown as more certain than an earlier one.
5. **Schedule:** the update job runs 5 times a day, including just after Octopus publishes tomorrow's prices (around 16:00). Each run re-fetches the data, retrains, and saves a new forecast run.

The "holdout error" in the header is the mean absolute error on the most recent 14 days. Those days are held out of training, then the model is refit on everything. Treat it as a guide; forecasts more than a few days out depend heavily on the weather forecasts.

## Deploying on Proxmox (LXC, no Docker)

### 1. Create a Debian LXC

Use the Proxmox web UI (**Create CT**) or run this on the Proxmox host shell. Recommended size: 2 cores, **2 GB RAM**, 8 GB disk.

```bash
pveam update
pveam available --section system | grep debian-13
```

```bash
pveam download local debian-13-standard_13.6-1_amd64.tar.zst
```

Use the exact template name that `pveam available` printed above.

```bash
pct create 210 local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst --hostname agile-forecast --cores 2 --memory 2048 --swap 512 --rootfs local-lvm:8 --net0 name=eth0,bridge=vmbr0,ip=dhcp --unprivileged 1 --features nesting=1 --onboot 1 --start 1
```

Change `210`, the storage (`local-lvm`) and the bridge (`vmbr0`) to match your setup. A static IP (`ip=192.168.1.50/24,gw=192.168.1.1`) or a DHCP reservation makes it easier to find later.

### 2. Install

On the Proxmox host, open a shell in the container (`pct enter 210`), then:

```bash
apt-get update && apt-get install -y git
```

```bash
git clone https://github.com/leeshellard/agile-forecast.git /opt/agile-forecast
```

```bash
bash /opt/agile-forecast/deploy/install.sh
```

The installer:
- installs Python, creates an `agile` service user and a virtualenv in `/opt/agile-forecast`
- writes config to `/etc/agile-forecast.env` and keeps data in `/var/lib/agile-forecast/agile.db`
- enables `agile-forecast.service` (web on port 8000) and `agile-forecast-update.timer`
- runs the first update, which backfills a year of data (1–3 minutes)

Then open `http://<container-ip>:8000/`.

**Upgrading:** run the command below. Your config and database are kept.

```bash
cd /opt/agile-forecast && git pull && bash deploy/install.sh
```

### Useful commands (inside the container)

```bash
journalctl -u agile-forecast-update -n 50
```

```bash
systemctl start agile-forecast-update.service
```

```bash
systemctl list-timers agile-forecast-update.timer
```

```bash
nano /etc/agile-forecast.env && systemctl restart agile-forecast
```

## My usage page (optional)

`/usage` shows what you actually used and paid: totals, monthly cost against a comparison tariff, your demand and the price you paid by time of day, and daily usage. It needs a smart meter that reports half-hourly readings.

Add your details to `/etc/agile-forecast.env`:

```
OCTOPUS_API_KEY=sk_live_...
OCTOPUS_ACCOUNT=A-1234ABCD
```

Then restart and run an update (`systemctl restart agile-forecast && systemctl start agile-forecast-update.service`). The first sync downloads about two years of readings. After that each scheduled update adds the new ones.

- Your API key stays on the server. The browser only receives costed results, and nothing personal is committed to the repository (the database is git-ignored).
- The page has no login. Anyone who can reach the dashboard can see your usage, so keep it on your home network.
- Costs use the tariff in force at the time (from your account's agreement history), at the lowest published rate (direct debit), VAT included, plus the standing charge. They can differ slightly from your bills.
- Export income works for single-rate export tariffs such as Outgoing Octopus. Time-of-use tariffs such as Flux are not costed.
- `/compare` prices your usage on the other tariffs at today's rates, so it shows what each would cost for your habits now, not what it cost in the past. `/winter` is a what-if on your own readings and Agile prices.
- You can run just this sync with `python -m app.usage`.

## API

Interactive docs are at `/docs`. Regions are `X` (national average) or `A`–`P`.

| Endpoint | Returns |
|---|---|
| `GET /api/{region}?days=7&forecast_count=1` | Latest forecast run(s): `[{name, region, created_at, prices: [{date_time, agile_pred, agile_low, agile_high}]}]` |
| `GET /api/{region}/actual?days=7` | Published Agile prices |
| `GET /api/{region}/rss?days=14` | RSS feed of published prices, one post per day with a summary and half-hourly table. The next day's post appears after the 16:20 update. |
| `GET /api/{region}/chart?days=7&history_days=2&previous=3` | Everything the chart uses (forecast, actuals, earlier runs, inputs) |
| `GET /api/{region}/accuracy?days=30` | MAE and range coverage by days ahead, plus the MAE of each run |
| `GET /api/usage/report?days=365` | Your costed usage (only when My usage is set up): totals, monthly and daily series, hourly profile |
| `GET /api/usage/winter?season=2025&shift=0.15` | Winter cost and the saving from moving a share of usage to the cheapest half-hours |
| `GET /api/status` | Time of the latest run and its holdout error |
| `GET /api/regions` | Region codes and names |

All times are UTC (ISO 8601), except in the RSS feed, which uses UK time. Prices are in p/kWh including VAT.

### Home Assistant example

```yaml
rest:
  - resource: http://192.168.1.50:8000/api/C?days=2
    scan_interval: 1800
    sensor:
      - name: "Agile forecast next"
        unit_of_measurement: "p/kWh"
        value_template: "{{ value_json[0].prices[0].agile_pred }}"
        json_attributes_path: "$[0]"
        json_attributes: [created_at, prices]
```

Keep `days` small (2–3) for sensors. Home Assistant won't record attributes larger than 16 KB.

## Running locally for development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m app.pipeline
```

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## Configuration (`/etc/agile-forecast.env`)

| Variable | Default | Meaning |
|---|---|---|
| `AGILE_PORT` / `AGILE_HOST` | `8000` / `0.0.0.0` | Web server bind |
| `AGILE_PRODUCT` | auto | Octopus Agile product code; blank = newest Agile import product |
| `AGILE_HISTORY_DAYS` | `365` | Training history to backfill and use |
| `AGILE_REFRESH_DAYS` | `10` | Days re-fetched on each update |
| `AGILE_KEEP_RUNS_DAYS` | `180` | How long stored forecast runs are kept |
| `AGILE_CALIBRATION_DAYS` | `28` | Days of scored past forecasts used to calibrate the 10–90% range |
| `OCTOPUS_API_KEY` / `OCTOPUS_ACCOUNT` | blank | Switch on the My usage page |
| `USAGE_COMPARE_PRODUCT` | `VAR-22-11-01` | Product your cost is compared with (Flexible Octopus) |
| `USAGE_HISTORY_DAYS` | `730` | How far back the first usage sync goes |

## Notes and limitations

- **Upstream sources can change.** The NESO resource IDs are in `app/sources.py`; if NESO restructures a dataset, update the ID there.
- **Gas prices are only captured through the recent price level.** A sharp move in the gas market takes a few days to show up.
- **The training inputs are *actual* generation and demand, but the forecast uses *forecast* inputs.** That makes the holdout error optimistic. The accuracy panel scores real forecasts, so trust it once a week or two of runs have built up. Its range-coverage line uses the range as it was served, so runs made before calibration existed still show the narrow model range.
- **Terms of use.** Data is used under the providers' terms: Octopus API; NESO Open Data Licence; Elexon BMRS (© Elexon Limited). This is for personal, non-commercial use, and comes with no warranty of accuracy.

## Licence

The code is released under the MIT licence (see `LICENSE`). The data sources keep their own terms, listed above.
