# Railway Deployment

Project: **trading ai master**

The bot runs as **two services**, both deployed from this repo. Start commands
come from the root [`Procfile`](../Procfile); there are no per-service TOML
config files.

## Services

| Service | Procfile entry | Command | Role |
|---|---|---|---|
| `web` | `web:` | `streamlit run examples/daily-market-dashboard/app.py` | Dashboard UI |
| `worker` | `worker:` | `python3 worker.py` | Trading daemon |

## How the worker runs the routines

The `worker` service is a long-lived daemon — it does **not** use Railway cron.

```
worker.py  (startup health check: Alpaca + FMP connectivity)
  → loops forever, fires scheduler.py every 600s (10 min)
    → scheduler.py reads the current time in America/New_York (pytz)
      → dispatches the matching routine by ET window:
          06:00  pre_market
          09:30  market_open
          12:00  midday_review
          15:00  market_close
          16:00  weekly_review   (Friday only)
      → catch-up: re-runs a missed market_open / midday_review after redeploys
```

Scheduling is ET-correct regardless of host timezone because `scheduler.py`
uses `pytz.timezone("America/New_York")` explicitly. `TZ=America/New_York` is
also set on both services for consistency.

## Environment variables

Set on each service (Railway → service → Variables):

| Var | Notes |
|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Brokerage creds |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` (paper) |
| `ALPACA_PAPER_TRADE` | `true` = paper, `false` = live. Controls `core/broker.py`. |
| `ANTHROPIC_API_KEY` | Claude analyst |
| `FMP_API_KEY` | Market data |
| `GMAIL_PASSWORD` | Gmail app password for email alerts |
| `TZ` | `America/New_York` |

> **Paper vs live:** live/paper is decided by `ALPACA_PAPER_TRADE` (not the
> base URL). Keep the keys, base URL, and this flag consistent — a paper key
> (`PK…`) with `ALPACA_PAPER_TRADE=false` will 401 against the live endpoint.
