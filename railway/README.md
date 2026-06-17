# Railway Cron Services Setup

Project: **strong-charisma**

## Services

| Service Name | Routine | Cron (UTC) | Schedule |
|---|---|---|---|
| `pre-market` | `pre_market` | `0 6 * * 1-5` | 06:00 Mon-Fri |
| `market-open` | `market_open` | `30 8 * * 1-5` | 08:30 Mon-Fri |
| `midday-review` | `midday_review` | `0 12 * * 1-5` | 12:00 Mon-Fri |
| `market-close` | `market_close` | `0 15 * * 1-5` | 15:00 Mon-Fri |
| `weekly-review` | `weekly_review` | `0 16 * * 5` | 16:00 Friday |

## Create Each Service

In Railway dashboard (strong-charisma project):

1. **New Service** → **GitHub Repo** → select `claude-trading-skills`
2. **Settings → Deploy**:
   - Start Command: `python3 routines/run.py`
   - Cron Schedule: copy from table above
3. **Variables** → add:
   - `ROUTINE` = routine name from table (e.g. `pre_market`)
   - All other env vars are shared from the project (Alpaca, Anthropic, Telegram)
4. **Deploy**

Repeat for all 5 services. The `ROUTINE` env var is the only thing that differs.

## How It Works

```
Railway cron fires
  → python3 routines/run.py
    → reads ROUTINE env var
    → runs Alpaca auto-connect chain (if routine needs Alpaca)
    → dispatches to routines/<ROUTINE>.py → run()
```

## Config Files

Per-service TOML configs in `railway/services/` can be copy-pasted into
each Railway service's settings if you prefer config-as-code over the dashboard.
