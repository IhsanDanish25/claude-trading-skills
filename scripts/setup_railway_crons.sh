#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Railway Cron Services Setup for Trading Bot
# Project: strong-charisma / production
# ─────────────────────────────────────────────────────────────────────────────
# Prerequisites:
#   1. Railway CLI installed & authenticated:
#        npm i -g @railway/cli && railway login
#   2. Linked to the project:
#        railway link   # select "strong-charisma" → "production"
#
# Required env vars (export before running):
#   ANTHROPIC_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY
#
# Usage:
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   export ALPACA_API_KEY="PK..."
#   export ALPACA_SECRET_KEY="..."
#   ./scripts/setup_railway_crons.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Validate prerequisites ───────────────────────────────────────────────────
: "${ANTHROPIC_API_KEY:?Export ANTHROPIC_API_KEY before running}"
: "${ALPACA_API_KEY:?Export ALPACA_API_KEY before running}"
: "${ALPACA_SECRET_KEY:?Export ALPACA_SECRET_KEY before running}"

command -v railway >/dev/null 2>&1 || { echo "Error: railway CLI not found"; exit 1; }
railway whoami >/dev/null 2>&1 || { echo "Error: not logged in — run 'railway login'"; exit 1; }

REPO="ihsandanish25/claude-trading-skills"
BRANCH="main"
ALPACA_BASE_URL="https://paper-api.alpaca.markets"
ALPACA_PAPER_TRADE="true"
FMP_API_KEY="ZSYGfYsxXeVb2f8Ohz2wESvrDVcmdZRE"

echo "═══════════════════════════════════════════════════════════════"
echo "  Railway Cron Services — Trading Bot Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Helper: create one cron service ──────────────────────────────────────────
setup_service() {
    local name="$1"
    local routine="$2"
    local schedule="$3"

    echo "── ${name} (ROUTINE=${routine}, cron: ${schedule})"

    # Create service linked to GitHub repo
    railway add \
        --service "${name}" \
        --repo "${REPO}" \
        --branch "${BRANCH}" \
        --json 2>/dev/null \
    || echo "  ⚠  Service '${name}' may already exist — setting variables anyway"

    # Set all env vars (--skip-deploys to avoid 5 separate deploys)
    railway variable set "ROUTINE=${routine}" \
        --service "${name}" --skip-deploys --json >/dev/null
    railway variable set "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
        --service "${name}" --skip-deploys --json >/dev/null
    railway variable set "ALPACA_API_KEY=${ALPACA_API_KEY}" \
        --service "${name}" --skip-deploys --json >/dev/null
    railway variable set "ALPACA_SECRET_KEY=${ALPACA_SECRET_KEY}" \
        --service "${name}" --skip-deploys --json >/dev/null
    railway variable set "ALPACA_BASE_URL=${ALPACA_BASE_URL}" \
        --service "${name}" --skip-deploys --json >/dev/null
    railway variable set "ALPACA_PAPER_TRADE=${ALPACA_PAPER_TRADE}" \
        --service "${name}" --skip-deploys --json >/dev/null
    railway variable set "FMP_API_KEY=${FMP_API_KEY}" \
        --service "${name}" --skip-deploys --json >/dev/null

    echo "  ✓ Variables set"

    # NOTE: Cron schedule must be set via Railway dashboard:
    #   Dashboard → service → Settings → Cron Schedule → paste: ${schedule}
    # The Railway CLI does not have a 'cron set' command as of v5.x.
    echo "  ⚠  Set cron schedule in dashboard: ${schedule}"
    echo ""
}

# ── Create all 5 cron services ───────────────────────────────────────────────
setup_service "pre-market"     "pre_market"     "0 6 * * 1-5"
setup_service "market-open"    "market_open"     "30 8 * * 1-5"
setup_service "midday-review"  "midday_review"   "0 12 * * 1-5"
setup_service "market-close"   "market_close"    "0 15 * * 1-5"
setup_service "weekly-review"  "weekly_review"    "0 16 * * 5"

# ── Set start command on each (Railway uses this instead of Procfile) ────────
echo "── Setting start commands"
for svc in pre-market market-open midday-review market-close weekly-review; do
    railway variable set "RAILWAY_START_COMMAND=python3 routines/run.py" \
        --service "${svc}" --json >/dev/null 2>&1 \
    || echo "  ⚠  Could not set start command on ${svc} — set manually in dashboard"
done
echo ""

# ── Verification ─────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  Verification — checking each service"
echo "═══════════════════════════════════════════════════════════════"
echo ""

for svc in pre-market market-open midday-review market-close weekly-review; do
    echo "── ${svc}"
    railway variable list --service "${svc}" --kv 2>/dev/null \
        | grep -E "^(ROUTINE|ALPACA_BASE_URL|FMP_API_KEY|ALPACA_PAPER_TRADE)" \
    || echo "  (could not list variables — verify in dashboard)"

    railway service status 2>/dev/null || true
    echo ""
done

echo "═══════════════════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  MANUAL STEPS REQUIRED in Railway dashboard:"
echo "  ─────────────────────────────────────────────"
echo "  1. Set cron schedules on each service:"
echo "       pre-market:     0 6 * * 1-5"
echo "       market-open:    30 8 * * 1-5"
echo "       midday-review:  0 12 * * 1-5"
echo "       market-close:   0 15 * * 1-5"
echo "       weekly-review:  0 16 * * 5"
echo ""
echo "  2. Verify each service type is set to 'Cron' (not 'Web')"
echo ""
echo "  3. Confirm start command is: python3 routines/run.py"
echo "     (Settings → Deploy → Start Command)"
echo ""
echo "  4. Trigger a test run on any service to verify"
echo "═══════════════════════════════════════════════════════════════"
