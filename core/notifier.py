"""
Email notifier — trading alerts via Resend HTTP API.

Env vars:
    RESEND_API_KEY   Resend API key (required)
    NOTIFY_FROM      sender address (default: onboarding@resend.dev)
    NOTIFY_TO        recipient address (default: ihsanlankan@icloud.com)
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

_FROM    = os.environ.get("NOTIFY_FROM", "onboarding@resend.dev")
_TO      = os.environ.get("NOTIFY_TO",   "ihsanlankan@icloud.com")
_API_KEY = os.environ.get("RESEND_API_KEY", "")

_CSS = """
body{margin:0;padding:0;background:#0E1117;font-family:-apple-system,sans-serif;color:#E2E8F0}
.wrap{max-width:600px;margin:0 auto;padding:24px}
.header{background:linear-gradient(135deg,#1A1F2E,#0E1117);border:1px solid #2D3748;
        border-radius:12px;padding:20px 24px;margin-bottom:20px}
.header h1{margin:0 0 4px;font-size:1.3rem;color:#FAFAFA}
.header p{margin:0;color:#A0AEC0;font-size:0.85rem}
.card{background:#1A1F2E;border:1px solid #2D3748;border-radius:10px;
      padding:16px 20px;margin-bottom:14px}
.card h2{margin:0 0 10px;font-size:1rem;color:#FAFAFA;
         border-bottom:1px solid #2D3748;padding-bottom:8px}
.row{display:flex;justify-content:space-between;padding:4px 0;
     font-size:0.88rem;border-bottom:1px solid #1a2035}
.row:last-child{border-bottom:none}
.label{color:#A0AEC0}.value{color:#FAFAFA;font-weight:600}
.green{color:#48BB78}.red{color:#FC8181}.yellow{color:#ECC94B}.blue{color:#63B3ED}
.badge{display:inline-block;padding:2px 10px;border-radius:4px;
       font-size:0.78rem;font-weight:700}
.badge-buy{background:#22543D;color:#48BB78}
.badge-sell{background:#742A2A;color:#FC8181}
.badge-hold{background:#2A4365;color:#63B3ED}
.badge-cash{background:#744210;color:#ECC94B}
.footer{text-align:center;color:#4A5568;font-size:0.75rem;padding-top:16px}
"""


def _html(title: str, subtitle: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>{_CSS}</style></head>
<body><div class="wrap">
  <div class="header">
    <h1>&#9889; {title}</h1>
    <p>{subtitle}</p>
  </div>
  {body}
  <div class="footer">Trading Bot &bull; Railway &bull; Auto-generated</div>
</div></body></html>"""


def _row(label: str, value: str, color: str = "") -> str:
    cls = f' class="{color}"' if color else ""
    return f'<div class="row"><span class="label">{label}</span><span class="value{cls}">{value}</span></div>'


def send(subject: str, plain: str, html: str | None = None) -> bool:
    """Send email via Resend HTTP API. Silent no-op if RESEND_API_KEY is not set."""
    if not _API_KEY:
        log.debug("RESEND_API_KEY not set — email skipped")
        return False
    try:
        payload: dict = {
            "from": _FROM,
            "to": _TO,
            "subject": subject,
            "text": plain,
        }
        if html:
            payload["html"] = html
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Email sent: %s", subject)
        return True
    except Exception as exc:
        log.error("Email failed (%s): %s", subject, exc)
        return False


# ── Pre-built formatters ───────────────────────────────────────────────────────

def send_premarket_brief(
    date: str,
    regime: str,
    bias: str,
    rationale: str,
    portfolio_value: float,
    cash: float,
    slots: int,
    buy_list: list[dict],
    high_impact_events: list[dict],
) -> bool:
    bias_color = {"cash": "yellow", "defensive": "yellow",
                  "aggressive": "green", "moderate": "blue"}.get(bias.lower(), "blue")
    bias_badge = f'<span class="badge badge-{"hold" if bias == "moderate" else "buy" if bias == "aggressive" else "cash"}">{bias.upper()}</span>'

    candidates_html = ""
    if buy_list:
        rows = "".join(
            f'<div class="row"><span class="label">{c["symbol"]}</span>'
            f'<span class="value green">score {c.get("score","?")} &bull; {c.get("reason","")[:60]}</span></div>'
            for c in buy_list[:6]
        )
        candidates_html = f'<div class="card"><h2>VCP Buy Candidates ({len(buy_list)})</h2>{rows}</div>'
    else:
        candidates_html = '<div class="card"><h2>VCP Buy Candidates</h2><p style="color:#A0AEC0">None found today</p></div>'

    events_html = ""
    if high_impact_events:
        rows = "".join(
            f'<div class="row"><span class="label">{e.get("date","")[:10]} {e.get("country","")}</span>'
            f'<span class="value yellow">{e.get("event","")[:55]}</span></div>'
            for e in high_impact_events[:5]
        )
        events_html = f'<div class="card"><h2>⚡ High-Impact Events</h2>{rows}</div>'

    body = f"""
    <div class="card"><h2>Account</h2>
      {_row("Portfolio", f"${portfolio_value:,.2f}")}
      {_row("Cash", f"${cash:,.2f}")}
      {_row("Open slots", str(slots))}
    </div>
    <div class="card"><h2>Market Regime</h2>
      {_row("Regime", regime.upper())}
      <div class="row"><span class="label">Bias</span><span class="value">{bias_badge}</span></div>
      {_row("Rationale", rationale[:100])}
    </div>
    {candidates_html}
    {events_html}
    """
    html = _html("Pre-Market Brief", date, body)
    plain = (
        f"Pre-Market Brief — {date}\n"
        f"Regime: {regime} | Bias: {bias}\n"
        f"Portfolio: ${portfolio_value:,.2f} | Cash: ${cash:,.2f} | Slots: {slots}\n"
        f"Buy candidates: {len(buy_list)}\n"
        f"High-impact events: {len(high_impact_events)}\n"
        f"Rationale: {rationale}"
    )
    return send(f"📋 Pre-Market Brief — {date}", plain, html)


def send_trade_alert(
    action: str,
    ticker: str,
    shares: int,
    price: float,
    stop: float,
    target: float,
    confidence: int | None = None,
    reason: str = "",
) -> bool:
    action_up = action.upper()
    badge_cls = "buy" if action_up == "BUY" else "sell"
    risk_pct  = round((price - stop) / price * 100, 2) if stop else 0
    rr        = round((target - price) / (price - stop), 1) if stop and target and price != stop else "?"

    body = f"""
    <div class="card"><h2><span class="badge badge-{badge_cls}">{action_up}</span> {ticker}</h2>
      {_row("Price", f"${price:.2f}")}
      {_row("Shares", str(shares))}
      {_row("Stop loss", f"${stop:.2f}  ({risk_pct}% risk)", "red")}
      {_row("Target", f"${target:.2f}", "green")}
      {_row("Risk / Reward", f"1 : {rr}")}
      {_row("Exposure", f"${shares * price:,.0f}")}
      {f'<div class="row"><span class="label">Confidence</span><span class="value">{confidence}/10</span></div>' if confidence else ""}
      {f'<div class="row"><span class="label">Reason</span><span class="value">{reason[:100]}</span></div>' if reason else ""}
    </div>
    """
    html = _html(f"{action_up} {ticker}", f"{shares} shares @ ${price:.2f}", body)
    plain = (
        f"{action_up} {ticker}: {shares} sh @ ${price:.2f}\n"
        f"Stop: ${stop:.2f} | Target: ${target:.2f} | R:R 1:{rr}\n"
        f"{reason}"
    )
    emoji = "🟢" if action_up == "BUY" else "🔴"
    return send(f"{emoji} {action_up} {ticker} — {shares} sh @ ${price:.2f}", plain, html)


def send_eod_summary(
    date: str,
    portfolio_value: float,
    cash: float,
    positions_held: int,
    unrealized_pnl: float,
    regime: str,
    bias: str,
    spy_change_pct: float,
    ftd_detected: bool,
    force_closed: list[str] | None = None,
) -> bool:
    pnl_color = "green" if unrealized_pnl >= 0 else "red"
    spy_color = "green" if spy_change_pct >= 0 else "red"
    pnl_sign  = "+" if unrealized_pnl >= 0 else ""
    spy_sign  = "+" if spy_change_pct >= 0 else ""

    closed_html = ""
    if force_closed:
        rows = "".join(f'<div class="row"><span class="label">{s}</span><span class="value red">Force closed (-3%)</span></div>' for s in force_closed)
        closed_html = f'<div class="card"><h2>Force Closed</h2>{rows}</div>'

    body = f"""
    <div class="card"><h2>Portfolio EOD</h2>
      {_row("Value", f"${portfolio_value:,.2f}")}
      {_row("Cash", f"${cash:,.2f}")}
      {_row("Positions held", str(positions_held))}
      {_row("Unrealized P&L", f"{pnl_sign}${unrealized_pnl:,.2f}", pnl_color)}
    </div>
    <div class="card"><h2>Market</h2>
      {_row("SPY", f"{spy_sign}{spy_change_pct:.2f}%", spy_color)}
      {_row("Regime", regime.upper())}
      {_row("Bias", bias.upper())}
      {_row("FTD detected", "YES ✓" if ftd_detected else "No")}
    </div>
    {closed_html}
    """
    html = _html("EOD Summary", date, body)
    plain = (
        f"EOD Summary — {date}\n"
        f"Portfolio: ${portfolio_value:,.2f} | Cash: ${cash:,.2f}\n"
        f"Unrealized P&L: {pnl_sign}${unrealized_pnl:,.2f}\n"
        f"SPY: {spy_sign}{spy_change_pct:.2f}% | Regime: {regime} | FTD: {ftd_detected}"
    )
    emoji = "📈" if unrealized_pnl >= 0 else "📉"
    return send(f"{emoji} EOD Summary — {date}  ({pnl_sign}${unrealized_pnl:,.0f})", plain, html)


def send_error_alert(routine: str, error: str) -> bool:
    body = f"""
    <div class="card"><h2>Routine Failed</h2>
      {_row("Routine", routine)}
      <div class="row"><span class="label">Error</span>
        <span class="value red" style="word-break:break-all">{error[:300]}</span></div>
    </div>
    """
    html = _html("⚠️ Routine Error", routine, body)
    plain = f"Routine FAILED: {routine}\n\n{error}"
    return send(f"⚠️ Bot Error — {routine}", plain, html)
