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
| `ALPACA_PAPER_TRADE` / `ALPACA_PAPER` | `true` = paper, `false` = live. Controls **`auto_trader.py` only** — see note below. |
| `DRY_RUN` | `true` = full pipeline runs against real market data, but every order is short-circuited right before submission — nothing reaches Alpaca. Controls `core/broker.py`. |
| `ANTHROPIC_API_KEY` | Claude analyst |
| `FMP_API_KEY` | Market data |
| `RESEND_API_KEY` | Resend API key for email alerts |
| `TZ` | `America/New_York` |

> **Paper vs live:** `core/broker.py` (used by the scheduler/worker routines —
> the actual live-money path) always talks to the LIVE Alpaca endpoint;
> `ALPACA_PAPER_TRADE`/`ALPACA_PAPER` do **not** affect it. Those flags only
> gate `auto_trader.py`, a separate entry point. There has been no paper
> account to fall back to since 2026-08-01 (it was deleted), so `broker.py`'s
> only real safety mechanism is `DRY_RUN` — set it `true` to validate the
> pipeline end-to-end without risking real capital. Do not rely on
> `ALPACA_PAPER_TRADE=true` to make the scheduler/worker paper-safe — it
> won't.

## Health checks

There is no `/health` route on either service — don't monitor that path, it
will silently 200 on `web` without checking anything real.

| Service | Real health path | Returns |
|---|---|---|
| `web` | `/_stcore/health` | Streamlit's built-in health endpoint |
| `worker` | `/` (its own `$PORT`) | JSON heartbeat, e.g. `{"last_tick": "..."}` — set by `worker.py`'s heartbeat server |

Because both services share this repo's single `railway.toml`, do not add a
`[deploy] healthcheckPath` there — it would apply to both services, and
`/_stcore/health` doesn't exist on `worker`. Set `healthcheckPath` per-service
in the Railway dashboard instead if you want Railway's own deploy healthcheck
to gate promotion.
