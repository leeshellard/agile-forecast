# Agile Forecast

A self-hosted forecaster for Octopus **Agile** electricity prices, up to 14 days ahead, for all 14 regions. It was inspired by [AgilePredict](https://agilepredict.com).

- Web UI with forecast vs actual prices, a 10–90% range, earlier-forecast overlays and generation/demand inputs
- JSON API (easy to use from Home Assistant)
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
4. **Schedule:** the update job runs 5 times a day, including just after Octopus publishes tomorrow's prices (around 16:00). Each run re-fetches the data, retrains, and saves a new forecast run.

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

## API

Interactive docs are at `/docs`. Regions are `X` (national average) or `A`–`P`.

| Endpoint | Returns |
|---|---|
| `GET /api/{region}?days=7&forecast_count=1` | Latest forecast run(s): `[{name, region, created_at, prices: [{date_time, agile_pred, agile_low, agile_high}]}]` |
| `GET /api/{region}/actual?days=7` | Published Agile prices |
| `GET /api/{region}/chart?days=7&history_days=2&previous=3` | Everything the chart uses (forecast, actuals, earlier runs, inputs) |
| `GET /api/{region}/accuracy?days=30` | MAE and range coverage by days ahead, plus the MAE of each run |
| `GET /api/status` | Time of the latest run and its holdout error |
| `GET /api/regions` | Region codes and names |

All times are UTC (ISO 8601). Prices are in p/kWh including VAT.

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

## Notes and limitations

- **Upstream sources can change.** The NESO resource IDs are in `app/sources.py`; if NESO restructures a dataset, update the ID there.
- **Gas prices are only captured through the recent price level.** A sharp move in the gas market takes a few days to show up.
- **The training inputs are *actual* generation and demand, but the forecast uses *forecast* inputs.** That makes the holdout error optimistic. The accuracy panel scores real forecasts, so trust it once a week or two of runs have built up.
- **Terms of use.** Data is used under the providers' terms: Octopus API; NESO Open Data Licence; Elexon BMRS (© Elexon Limited). This is for personal, non-commercial use, and comes with no warranty of accuracy.

## Licence

The code is released under the MIT licence (see `LICENSE`). The data sources keep their own terms, listed above.
