#!/usr/bin/env python3
"""Generate a daily market dashboard by running skills in parallel."""

from __future__ import annotations

import argparse
import os
import glob
import json
import logging
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parent / "knowledge"
RETENTION_DAYS = 3

_I18N: dict[str, dict[str, str]] = {
    "en": {
        "title": "Daily Market Dashboard",
        "signal_dashboard": "Signal Dashboard",
        "col_skill": "Skill", "col_score": "Score", "col_zone": "Zone / Status",
        "ftd_status": "FTD Status", "market_state": "Market State", "signal": "Signal",
        "quality_score": "Quality Score", "guidance": "Guidance",
        "exposure_range": "Exposure Range", "breadth_uptrend": "Breadth & Uptrend",
        "uptrend_composite": "Uptrend Composite", "breadth_composite": "Breadth Composite",
        "theme_highlights": "Theme Highlights (Top 3)", "vcp_candidates": "VCP Candidates",
        "col_ticker": "Ticker", "col_rating": "Rating", "col_pivot_dist": "Pivot Dist",
        "candidates": "candidates", "bullish": "bullish", "bearish": "bearish",
        "no_data": "did not produce output", "no_breadth": "No breadth/uptrend data available.",
        "no_themes": "No bullish themes detected.", "no_vcp": "No VCP candidates found.",
        "market_top": "Market Top Detector", "econ_cal": "Economic Calendar",
        "no_market_top": "No market top signals detected.",
        "no_econ_cal": "No upcoming events found.",
        "generated_at": "Generated at",
        # New skills
        "options_flow": "Options Flow (Unusual Activity)",
        "earnings_momentum": "Earnings Momentum (PEAD)",
        "sector_rotation": "Sector Rotation",
        "technical_indicators": "Technical Indicators",
        "news_sentiment": "News Sentiment",
        "mean_reversion": "Mean Reversion Setups",
        "breakouts": "Breakout Scanner",
        "insider_buying": "Insider Buying",
        "short_squeeze": "Short Squeeze Setups",
        "macro_signals": "Macro Signal Monitor",
        "no_options_flow": "No unusual options activity detected.",
        "no_earnings_momentum": "No PEAD candidates found.",
        "no_sector_rotation": "Sector rotation data unavailable.",
        "no_tech_indicators": "No technical indicator data.",
        "no_news_sentiment": "No news sentiment data.",
        "no_mean_reversion": "No mean reversion setups found.",
        "no_breakouts": "No breakout candidates found.",
        "no_insider_buying": "No significant insider buying detected.",
        "no_short_squeeze": "No short squeeze setups found.",
        "no_macro_signals": "Macro signal data unavailable.",
    },
    "ja": {
        "title": "デイリーマーケットダッシュボード",
        "signal_dashboard": "シグナル一覧",
        "col_skill": "スキル", "col_score": "スコア", "col_zone": "ゾーン / 状態",
        "ftd_status": "FTD ステータス", "market_state": "市場状態", "signal": "シグナル",
        "quality_score": "品質スコア", "guidance": "ガイダンス",
        "exposure_range": "エクスポージャー範囲", "breadth_uptrend": "市場の広がり & 上昇トレンド",
        "uptrend_composite": "上昇トレンド総合", "breadth_composite": "市場の広がり総合",
        "theme_highlights": "注目テーマ (上位3)", "vcp_candidates": "VCP 候補銘柄",
        "col_ticker": "ティッカー", "col_rating": "評価", "col_pivot_dist": "ピボット距離",
        "candidates": "候補", "bullish": "強気", "bearish": "弱気",
        "no_data": "データ取得失敗", "no_breadth": "市場の広がり/上昇トレンドのデータがありません。",
        "no_themes": "強気テーマは検出されませんでした。", "no_vcp": "VCP候補銘柄はありません。",
        "market_top": "マーケットトップ検出", "econ_cal": "経済カレンダー",
        "no_market_top": "マーケットトップシグナルなし。",
        "no_econ_cal": "今後のイベントなし。",
        "generated_at": "生成日時",
        # New skills
        "options_flow": "オプションフロー（異常活動）",
        "earnings_momentum": "決算モメンタム（PEAD）",
        "sector_rotation": "セクターローテーション",
        "technical_indicators": "テクニカル指標",
        "news_sentiment": "ニュースセンチメント",
        "mean_reversion": "平均回帰セットアップ",
        "breakouts": "ブレイクアウトスキャナー",
        "insider_buying": "インサイダー買い",
        "short_squeeze": "ショートスクイーズ",
        "macro_signals": "マクロシグナルモニター",
        "no_options_flow": "異常なオプション活動なし。",
        "no_earnings_momentum": "PEAD候補なし。",
        "no_sector_rotation": "セクターローテーションデータなし。",
        "no_tech_indicators": "テクニカル指標データなし。",
        "no_news_sentiment": "ニュースセンチメントデータなし。",
        "no_mean_reversion": "平均回帰セットアップなし。",
        "no_breakouts": "ブレイクアウト候補なし。",
        "no_insider_buying": "インサイダー買いシグナルなし。",
        "no_short_squeeze": "ショートスクイーズセットアップなし。",
        "no_macro_signals": "マクロシグナルデータなし。",
    },
}

def _t(lang: str, key: str) -> str:
    return _I18N.get(lang, _I18N["en"]).get(key, _I18N["en"].get(key, key))

def _load_latest_vcp(project_root: Path) -> Any:
    """Load most recent VCP JSON — from dashboard dir or project root."""
    search_dirs = [
        Path(__file__).resolve().parent,
        project_root,
        project_root / "reports",
        project_root / "skills" / "vcp-screener" / "scripts",
    ]
    candidates = []
    for d in search_dirs:
        candidates.extend(d.glob("vcp_screener_*.json"))
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    logger.info("Reusing saved VCP JSON: %s", latest)
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load VCP JSON: %s", exc)
        return None

_VCP_WATCHLIST = [
    "AAPL","MSFT","NVDA","AMD","META","GOOGL","AMZN","TSLA","NFLX","CRM",
    "ADBE","PANW","CRWD","SNOW","DDOG","MELI","SQ","SHOP","NET","ZS",
    "CELH","ENPH","FSLR","ON","AEHR","SMCI","AXON","COCO","DUOL","PINS",
]

# Compact list for FMP-heavy skills to respect 250 calls/day free tier
_COMPACT_WATCHLIST = [
    "AAPL","MSFT","NVDA","AMD","META","GOOGL","AMZN","TSLA","NFLX","CRM",
    "ADBE","PANW","SNOW","DDOG","SQ","SHOP","CELH","AXON","COIN","UBER",
]


def _fmp_args(flag: str, key: str) -> list[str]:
    """Only include API key flag when key is non-empty (avoids passing empty string)."""
    return [flag, key] if key else []


def _skill_defs(project_root: Path) -> list[dict[str, Any]]:
    skills_dir = project_root / "skills"
    fmp_key = os.environ.get("FMP_API_KEY") or ""
    return [
        # ── Original 7 skills ──────────────────────────────────────────────
        {
            "name": "FTD Detector",
            "script": str(skills_dir / "ftd-detector" / "scripts" / "ftd_detector.py"),
            "args": ["--output-dir", "{tmpdir}", *_fmp_args("--api-key", fmp_key)],
            "glob": "ftd_detector_*.json",
        },
        {
            "name": "Uptrend Analyzer",
            "script": str(skills_dir / "uptrend-analyzer" / "scripts" / "uptrend_analyzer.py"),
            "args": ["--output-dir", "{tmpdir}"],
            "glob": "uptrend_analysis_*.json",
        },
        {
            "name": "Market Breadth",
            "script": str(skills_dir / "market-breadth-analyzer" / "scripts" / "market_breadth_analyzer.py"),
            "args": ["--output-dir", "{tmpdir}"],
            "glob": "market_breadth_*.json",
        },
        {
            "name": "Theme Detector",
            "script": str(skills_dir / "theme-detector" / "scripts" / "theme_detector.py"),
            "args": ["--output-dir", "{tmpdir}", *_fmp_args("--fmp-api-key", fmp_key)],
            "glob": "theme_detector_*.json",
        },
        {
            "name": "Market Top Detector",
            "script": str(skills_dir / "market-top-detector" / "scripts" / "market_top_detector.py"),
            "args": ["--output-dir", "{tmpdir}", *_fmp_args("--api-key", fmp_key)],
            "glob": "market_top_*.json",
        },
        {
            "name": "Economic Calendar",
            "script": str(skills_dir / "economic-calendar-fetcher" / "scripts" / "get_economic_calendar.py"),
            "args": ["--output", "{tmpdir}/economic_calendar_latest.json", *_fmp_args("--api-key", fmp_key)],
            "glob": "economic_calendar_latest.json",
        },
        {
            "name": "VCP Screener",
            "script": str(skills_dir / "vcp-screener" / "scripts" / "screen_vcp.py"),
            "args": [
                "--output-dir", "{tmpdir}",
                *_fmp_args("--api-key", fmp_key),
                "--universe", *_VCP_WATCHLIST,
                "--top", "10",
            ],
            "glob": "vcp_screener_*.json",
        },
        # ── 10 new skills ─────────────────────────────────────────────────
        {
            "name": "Options Flow",
            "script": str(skills_dir / "options-flow-scanner" / "scripts" / "scan_options_flow.py"),
            "args": [
                "--symbols", *_COMPACT_WATCHLIST,
                "--top", "10",
                "--output-dir", "{tmpdir}",
            ],
            "glob": "options_flow_*.json",
        },
        {
            "name": "Earnings Momentum",
            "script": str(skills_dir / "earnings-momentum-tracker" / "scripts" / "track_earnings_momentum.py"),
            "args": [*_fmp_args("--api-key", fmp_key), "--output-dir", "{tmpdir}"],
            "glob": "earnings_momentum_*.json",
        },
        {
            "name": "Sector Rotation",
            "script": str(skills_dir / "sector-rotation-detector" / "scripts" / "detect_sector_rotation.py"),
            "args": ["--output-dir", "{tmpdir}"],
            "glob": "sector_rotation_*.json",
        },
        {
            "name": "Technical Indicators",
            "script": str(skills_dir / "technical-indicator-suite" / "scripts" / "calculate_indicators.py"),
            "args": [
                "--symbols", *_COMPACT_WATCHLIST[:10],
                "--output-dir", "{tmpdir}",
            ],
            "glob": "indicators_*.json",
        },
        {
            "name": "News Sentiment",
            "script": str(skills_dir / "news-sentiment-analyzer" / "scripts" / "analyze_sentiment.py"),
            "args": [
                *_fmp_args("--api-key", fmp_key),
                "--symbols", "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "AMZN",
                "--days", "3",
                "--output-dir", "{tmpdir}",
            ],
            "glob": "sentiment_*.json",
        },
        {
            "name": "Mean Reversion",
            "script": str(skills_dir / "mean-reversion-screener" / "scripts" / "screen_mean_reversion.py"),
            "args": [
                "--symbols", *_COMPACT_WATCHLIST,
                "--top", "10",
                "--output-dir", "{tmpdir}",
            ],
            "glob": "mean_reversion_*.json",
        },
        {
            "name": "Breakout Scanner",
            "script": str(skills_dir / "breakout-scanner" / "scripts" / "scan_breakouts.py"),
            "args": [
                "--symbols", *_COMPACT_WATCHLIST,
                "--top", "10",
                "--output-dir", "{tmpdir}",
            ],
            "glob": "breakouts_*.json",
        },
        {
            "name": "Insider Buying",
            "script": str(skills_dir / "insider-buying-detector" / "scripts" / "detect_insider_buying.py"),
            "args": [
                *_fmp_args("--api-key", fmp_key),
                "--symbols", *_COMPACT_WATCHLIST,
                "--days", "30",
                "--min-grade", "C",
                "--output-dir", "{tmpdir}",
            ],
            "glob": "insider_buying_*.json",
        },
        {
            "name": "Short Squeeze",
            "script": str(skills_dir / "short-squeeze-scanner" / "scripts" / "scan_short_squeeze.py"),
            "args": [
                *_fmp_args("--api-key", fmp_key),
                "--symbols", *_COMPACT_WATCHLIST,
                "--top", "10",
                "--output-dir", "{tmpdir}",
            ],
            "glob": "short_squeeze_*.json",
        },
        {
            "name": "Macro Signals",
            "script": str(skills_dir / "macro-signal-monitor" / "scripts" / "monitor_macro_signals.py"),
            "args": ["--output-dir", "{tmpdir}"],
            "glob": "macro_signals_*.json",
        },
    ]

def _run_skill(name: str, script: str, args: list[str], tmpdir: str) -> dict[str, Any]:
    resolved_args = [a.replace("{tmpdir}", tmpdir) for a in args]
    cmd = [sys.executable, script, *resolved_args]
    logger.info("Running %s: %s", name, " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out", name)
        return {"name": name, "status": "timeout", "data": None}
    except Exception as exc:
        logger.warning("%s failed: %s", name, exc)
        return {"name": name, "status": "error", "data": None}
    if result.returncode != 0:
        logger.warning("%s exit %d: %s", name, result.returncode, (result.stderr or "")[:300])
        return {"name": name, "status": "partial", "data": None}
    return {"name": name, "status": "ok", "data": None}

def _collect_json(tmpdir: str, pattern: str) -> Any | None:
    matches = sorted(glob.glob(f"{tmpdir}/{pattern}"))
    matches = [m for m in matches if "_history" not in Path(m).stem]
    if not matches:
        return None
    try:
        return json.loads(Path(matches[-1]).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", matches[-1], exc)
        return None

def run_all_skills(project_root: Path) -> dict[str, Any]:
    defs = _skill_defs(project_root)
    results: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="dashboard_") as tmpdir:
        futures = {}
        with ProcessPoolExecutor(max_workers=10) as executor:
            for skill_def in defs:
                future = executor.submit(
                    _run_skill, skill_def["name"], skill_def["script"],
                    skill_def["args"], tmpdir,
                )
                futures[future] = skill_def
            for future in as_completed(futures):
                skill_def = futures[future]
                try:
                    run_result = future.result()
                except Exception as exc:
                    logger.warning("%s raised: %s", skill_def["name"], exc)
                    run_result = {"name": skill_def["name"], "status": "error", "data": None}
                data = _collect_json(tmpdir, skill_def["glob"])
                run_result["data"] = data
                results[skill_def["name"]] = run_result

    # VCP fallback: if live run failed, use the most recent cached JSON
    vcp = results.get("VCP Screener", {})
    if not vcp.get("data"):
        cached = _load_latest_vcp(project_root)
        results["VCP Screener"] = {
            "name": "VCP Screener",
            "status": "cached" if cached else vcp.get("status", "error"),
            "data": cached,
        }

    return results

def _safe_get(data: Any, *keys: str, default: Any = "N/A") -> Any:
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, default)
        else:
            return default
    return current

def generate_json_summary(results: dict[str, Any], today: date) -> dict[str, Any]:
    """Build a structured JSON summary for the Streamlit dashboard UI."""
    ftd = results.get("FTD Detector", {}).get("data")
    uptrend = results.get("Uptrend Analyzer", {}).get("data")
    breadth = results.get("Market Breadth", {}).get("data")
    theme = results.get("Theme Detector", {}).get("data")
    vcp = results.get("VCP Screener", {}).get("data")
    mktop = results.get("Market Top Detector", {}).get("data")
    econ = results.get("Economic Calendar", {}).get("data")
    # New skills
    options_flow_data = results.get("Options Flow", {}).get("data")
    earnings_mom_data = results.get("Earnings Momentum", {}).get("data")
    sector_rot_data = results.get("Sector Rotation", {}).get("data")
    tech_ind_data = results.get("Technical Indicators", {}).get("data")
    news_sent_data = results.get("News Sentiment", {}).get("data")
    mean_rev_data = results.get("Mean Reversion", {}).get("data")
    breakout_data = results.get("Breakout Scanner", {}).get("data")
    insider_data = results.get("Insider Buying", {}).get("data")
    squeeze_data = results.get("Short Squeeze", {}).get("data")
    macro_data = results.get("Macro Signals", {}).get("data")

    summary: dict[str, Any] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": today.isoformat(),
        "ftd": {
            "state": _safe_get(ftd, "market_state", "combined_state") if ftd else "N/A",
            "score": _safe_get(ftd, "quality_score", "total_score") if ftd else "N/A",
            "signal": _safe_get(ftd, "quality_score", "signal") if ftd else "N/A",
            "guidance": _safe_get(ftd, "quality_score", "guidance") if ftd else "N/A",
            "exposure_range": _safe_get(ftd, "quality_score", "exposure_range") if ftd else "N/A",
        },
        "uptrend": {
            "score": _safe_get(uptrend, "composite", "composite_score") if uptrend else "N/A",
            "zone": _safe_get(uptrend, "composite", "zone") if uptrend else "N/A",
        },
        "breadth": {
            "score": _safe_get(breadth, "composite", "composite_score") if breadth else "N/A",
            "zone": _safe_get(breadth, "composite", "zone") if breadth else "N/A",
        },
        "themes": [],
        "vcp_candidates": [],
        "market_top": {
            "signal": _safe_get(mktop, "signal", default="N/A") if mktop else "N/A",
            "score": _safe_get(mktop, "score", default="N/A") if mktop else "N/A",
            "details": {},
        },
        "economic_calendar": [],
        # New skill sections
        "options_flow": [],
        "earnings_momentum": [],
        "sector_rotation": [],
        "technical_indicators": [],
        "news_sentiment": [],
        "mean_reversion": [],
        "breakouts": [],
        "insider_buying": [],
        "short_squeeze": [],
        "macro_signals": {},
        "skill_status": {},
    }

    if theme and isinstance(theme, dict):
        bullish = theme.get("themes", {}).get("bullish", []) if isinstance(theme.get("themes"), dict) else []
        for t in bullish[:5]:
            if isinstance(t, dict):
                summary["themes"].append({
                    "name": t.get("name", t.get("theme", "Unknown")),
                    "stage": t.get("stage", t.get("lifecycle_stage", "")),
                    "heat": t.get("heat", ""),
                    "bias": "bullish",
                })
        ts = theme.get("summary", {})
        if isinstance(ts, dict):
            summary["theme_summary"] = {
                "bullish_count": ts.get("bullish_count", 0),
                "bearish_count": ts.get("bearish_count", 0),
            }

    if vcp and isinstance(vcp, dict):
        for item in vcp.get("results", [])[:10]:
            if isinstance(item, dict):
                # Score: new format uses composite_score; old format uses contractions (e.g. "3C")
                raw_score = item.get("composite_score")
                if raw_score is None:
                    raw_score = item.get("score")
                score_out: Any = round(float(raw_score), 1) if isinstance(raw_score, (int, float)) else (raw_score or "?")

                # Pivot: new format → distance_from_pivot_pct (%) then vcp_pattern.pivot_price ($)
                #        old format → pivot_price ($) at top level
                pct = item.get("distance_from_pivot_pct")
                px  = (item.get("pivot_price")
                       or item.get("vcp_pattern", {}).get("pivot_price"))
                if isinstance(pct, (int, float)):
                    pivot_out: Any = round(float(pct), 1)
                elif isinstance(px, (int, float)):
                    pivot_out = f"${px:.2f}"
                else:
                    pivot_out = "?"

                summary["vcp_candidates"].append({
                    "ticker": item.get("symbol") or item.get("ticker") or "?",
                    "score": score_out,
                    "rating": (item.get("rating") or item.get("status")
                               or str(item.get("stage", "?"))),
                    "pivot_dist": pivot_out,
                })

    if mktop and isinstance(mktop, dict):
        summary["market_top"]["details"] = {
            k: v for k, v in list(mktop.items())[:8] if not isinstance(v, (dict, list))
        }

    econ_list = []
    if econ:
        econ_list = econ if isinstance(econ, list) else econ.get("events", [])
    for ev in (econ_list or [])[:10]:
        if isinstance(ev, dict):
            summary["economic_calendar"].append({
                "date": ev.get("date", ev.get("time", "?")),
                "event": ev.get("event", ev.get("name", "?")),
                "impact": ev.get("impact", ev.get("importance", "?")),
            })

    # ── New skill data extraction ─────────────────────────────────────────
    if options_flow_data and isinstance(options_flow_data, dict):
        for item in options_flow_data.get("results", [])[:8]:
            if isinstance(item, dict):
                summary["options_flow"].append({
                    "symbol": item.get("symbol", "?"),
                    "type": item.get("option_type", "?"),
                    "vol_oi_ratio": item.get("vol_oi_ratio"),
                    "score": item.get("score"),
                    "signal": item.get("signal", ""),
                })

    if earnings_mom_data and isinstance(earnings_mom_data, dict):
        for item in earnings_mom_data.get("results", [])[:8]:
            if isinstance(item, dict):
                summary["earnings_momentum"].append({
                    "symbol": item.get("symbol", "?"),
                    "grade": item.get("pead_grade", "?"),
                    "gap_pct": item.get("gap_pct"),
                    "momentum_5d": item.get("momentum_5d"),
                    "momentum_10d": item.get("momentum_10d"),
                })

    if sector_rot_data and isinstance(sector_rot_data, dict):
        summary["sector_rotation_signal"] = sector_rot_data.get("metadata", {}).get("rotation_signal", "N/A")
        for item in sector_rot_data.get("results", [])[:11]:
            if isinstance(item, dict):
                summary["sector_rotation"].append({
                    "ticker": item.get("ticker", "?"),
                    "name": item.get("name", "?"),
                    "rank": item.get("rank"),
                    "momentum_1m": item.get("momentum_1m"),
                    "composite_score": item.get("composite_score"),
                })

    if tech_ind_data and isinstance(tech_ind_data, dict):
        for item in tech_ind_data.get("results", [])[:10]:
            if isinstance(item, dict):
                summary["technical_indicators"].append({
                    "symbol": item.get("symbol", "?"),
                    "price": item.get("price"),
                    "rsi_14": item.get("rsi_14"),
                    "macd_crossover": (item.get("macd") or {}).get("crossover"),
                    "bb_position": (item.get("bollinger_bands") or {}).get("position"),
                    "signal_summary": item.get("signal_summary", ""),
                })

    if news_sent_data and isinstance(news_sent_data, dict):
        for item in news_sent_data.get("results", []):
            if isinstance(item, dict):
                summary["news_sentiment"].append({
                    "symbol": item.get("symbol", "?"),
                    "articles": item.get("articles", 0),
                    "sentiment": item.get("sentiment"),
                    "signal": item.get("signal", "neutral"),
                })

    if mean_rev_data and isinstance(mean_rev_data, dict):
        for item in mean_rev_data.get("results", [])[:8]:
            if isinstance(item, dict):
                summary["mean_reversion"].append({
                    "symbol": item.get("symbol", "?"),
                    "price": item.get("price"),
                    "rsi_14": item.get("rsi_14"),
                    "pullback_pct": item.get("pullback_pct"),
                    "score": item.get("reversion_score"),
                    "target": item.get("target"),
                })

    if breakout_data and isinstance(breakout_data, dict):
        for item in breakout_data.get("results", [])[:8]:
            if isinstance(item, dict):
                summary["breakouts"].append({
                    "symbol": item.get("symbol", "?"),
                    "price": item.get("price"),
                    "volume_ratio": item.get("volume_ratio"),
                    "breakout_type": item.get("breakout_type", []),
                    "score": item.get("breakout_score"),
                })

    if insider_data and isinstance(insider_data, dict):
        for item in insider_data.get("results", [])[:8]:
            if isinstance(item, dict):
                summary["insider_buying"].append({
                    "symbol": item.get("symbol", "?"),
                    "grade": item.get("grade", "?"),
                    "unique_insiders": item.get("unique_insiders"),
                    "total_value_usd": item.get("total_value_usd"),
                    "score": item.get("conviction_score"),
                })

    if squeeze_data and isinstance(squeeze_data, dict):
        for item in squeeze_data.get("results", [])[:8]:
            if isinstance(item, dict):
                summary["short_squeeze"].append({
                    "symbol": item.get("symbol", "?"),
                    "short_float_pct": item.get("short_float_pct"),
                    "days_to_cover": item.get("days_to_cover"),
                    "score": item.get("squeeze_score"),
                    "setup": item.get("setup", ""),
                })

    if macro_data and isinstance(macro_data, dict):
        meta = macro_data.get("metadata", {})
        signals = macro_data.get("signals", {})
        summary["macro_signals"] = {
            "regime": meta.get("regime", "N/A"),
            "yield_curve": meta.get("yield_curve", {}),
            "vix": (signals.get("^VIX") or {}).get("current"),
            "ten_year_yield": (signals.get("^TNX") or {}).get("current"),
            "dxy": (signals.get("DX-Y.NYB") or {}).get("current"),
            "gold": (signals.get("GLD") or {}).get("current"),
            "spy_1m": (signals.get("SPY") or {}).get("momentum_1m"),
        }

    for name, result in results.items():
        summary["skill_status"][name] = {
            "status": result.get("status", "unknown"),
            "has_data": result.get("data") is not None,
        }

    return summary


def generate_markdown(results: dict[str, Any], today: date, lang: str = "en") -> str:
    lines: list[str] = []
    lines.append(f"# {_t(lang, 'title')}  {today.isoformat()}")
    lines.append("")
    lines.append(f"## {_t(lang, 'signal_dashboard')}")
    lines.append("")
    lines.append(f"| {_t(lang, 'col_skill')} | {_t(lang, 'col_score')} | {_t(lang, 'col_zone')} |")
    lines.append("|-------|------:|---------------|")

    ftd = results.get("FTD Detector", {}).get("data")
    ftd_state = _safe_get(ftd, "market_state", "combined_state") if ftd else "N/A"
    ftd_score = _safe_get(ftd, "quality_score", "total_score") if ftd else "N/A"
    ftd_signal = _safe_get(ftd, "quality_score", "signal") if ftd else ""
    ftd_label = f"{ftd_state}" + (f" ({ftd_signal})" if ftd_signal and ftd_signal != "N/A" else "")
    lines.append(f"| FTD Detector | {ftd_score} | {ftd_label} |")

    uptrend = results.get("Uptrend Analyzer", {}).get("data")
    up_score = _safe_get(uptrend, "composite", "composite_score") if uptrend else "N/A"
    up_zone = _safe_get(uptrend, "composite", "zone") if uptrend else "N/A"
    lines.append(f"| Uptrend Analyzer | {up_score} | {up_zone} |")

    breadth = results.get("Market Breadth", {}).get("data")
    br_score = _safe_get(breadth, "composite", "composite_score") if breadth else "N/A"
    br_zone = _safe_get(breadth, "composite", "zone") if breadth else "N/A"
    lines.append(f"| Market Breadth | {br_score} | {br_zone} |")

    theme = results.get("Theme Detector", {}).get("data")
    theme_bullish = "N/A"
    theme_bearish = "N/A"
    if theme:
        summary = _safe_get(theme, "summary", default={})
        if isinstance(summary, dict):
            theme_bullish = f"{summary.get('bullish_count','?')} {_t(lang,'bullish')}"
            theme_bearish = f"{summary.get('bearish_count','?')} {_t(lang,'bearish')}"
    lines.append(f"| Theme Detector | {theme_bullish} | {theme_bearish} |")

    vcp = results.get("VCP Screener", {}).get("data")
    vcp_count = 0
    if vcp and isinstance(vcp, dict):
        vcp_results = vcp.get("results", [])
        vcp_count = len(vcp_results) if isinstance(vcp_results, list) else 0
    lines.append(f"| VCP Screener | {vcp_count} | {_t(lang, 'candidates')} (cached) |")

    mktop = results.get("Market Top Detector", {}).get("data")
    mktop_signal = _safe_get(mktop, "signal", default="N/A") if mktop else "N/A"
    mktop_score = _safe_get(mktop, "score", default="N/A") if mktop else "N/A"
    lines.append(f"| Market Top Detector | {mktop_score} | {mktop_signal} |")

    econ = results.get("Economic Calendar", {}).get("data")
    econ_list = []
    if econ:
        econ_list = econ if isinstance(econ, list) else econ.get("events", [])
    lines.append(f"| Economic Calendar | {len(econ_list) if econ_list else 0} | events this week |")
    lines.append("")

    lines.append(f"## {_t(lang, 'ftd_status')}")
    lines.append("")
    if ftd:
        lines.append(f"- **{_t(lang, 'market_state')}**: {ftd_state}")
        lines.append(f"- **{_t(lang, 'signal')}**: {ftd_signal}")
        lines.append(f"- **{_t(lang, 'quality_score')}**: {ftd_score}")
        lines.append(f"- **{_t(lang, 'guidance')}**: {_safe_get(ftd, 'quality_score', 'guidance')}")
        lines.append(f"- **{_t(lang, 'exposure_range')}**: {_safe_get(ftd, 'quality_score', 'exposure_range')}")
    else:
        lines.append(f"*FTD Detector {_t(lang, 'no_data')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'breadth_uptrend')}")
    lines.append("")
    if uptrend:
        lines.append(f"- **{_t(lang, 'uptrend_composite')}**: {up_score} ({up_zone})")
    if breadth:
        lines.append(f"- **{_t(lang, 'breadth_composite')}**: {br_score} ({br_zone})")
    if not uptrend and not breadth:
        lines.append(f"*{_t(lang, 'no_breadth')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'theme_highlights')}")
    lines.append("")
    if theme and isinstance(theme, dict):
        bullish_themes = theme.get("themes", {}).get("bullish", []) if isinstance(theme.get("themes"), dict) else []
        if bullish_themes:
            for i, t in enumerate(bullish_themes[:3], 1):
                if isinstance(t, dict):
                    name = t.get("name", t.get("theme", "Unknown"))
                    stage = t.get("stage", t.get("lifecycle_stage", ""))
                    heat = t.get("heat", "")
                    heat_str = f", Heat {heat:.0f}" if isinstance(heat, (int, float)) else ""
                    lines.append(f"{i}. **{name}** ({stage}{heat_str})")
                elif isinstance(t, str):
                    lines.append(f"{i}. {t}")
        else:
            lines.append(f"*{_t(lang, 'no_themes')}*")
    else:
        lines.append(f"*Theme Detector {_t(lang, 'no_data')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'vcp_candidates')}")
    lines.append("")
    if vcp and isinstance(vcp, dict):
        vcp_list = vcp.get("results", [])
        if isinstance(vcp_list, list) and vcp_list:
            lines.append(f"| {_t(lang,'col_ticker')} | {_t(lang,'col_score')} | {_t(lang,'col_rating')} | {_t(lang,'col_pivot_dist')} |")
            lines.append("|--------|------:|-------|--------|")
            for item in vcp_list[:10]:
                if isinstance(item, dict):
                    ticker = item.get("symbol", item.get("ticker", "?"))
                    score = item.get("composite_score", item.get("score", "?"))
                    if isinstance(score, float): score = f"{score:.1f}"
                    rating = item.get("rating", item.get("stage", "?"))
                    pivot_dist = item.get("distance_from_pivot_pct", "?")
                    if isinstance(pivot_dist, float): pivot_dist = f"{pivot_dist:+.1f}%"
                    lines.append(f"| {ticker} | {score} | {rating} | {pivot_dist} |")
        else:
            lines.append(f"*{_t(lang, 'no_vcp')}*")
    else:
        lines.append(f"*VCP Screener {_t(lang, 'no_data')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'market_top')}")
    lines.append("")
    if mktop and isinstance(mktop, dict):
        for k, v in list(mktop.items())[:8]:
            if not isinstance(v, (dict, list)):
                lines.append(f"- **{k}**: {v}")
    else:
        lines.append(f"*{_t(lang, 'no_market_top')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'econ_cal')}")
    lines.append("")
    if econ_list and isinstance(econ_list, list):
        lines.append("| Date | Event | Impact |")
        lines.append("|------|-------|--------|")
        for ev in econ_list[:10]:
            if isinstance(ev, dict):
                dt = ev.get("date", ev.get("time", "?"))
                name = ev.get("event", ev.get("name", "?"))
                impact = ev.get("impact", ev.get("importance", "?"))
                lines.append(f"| {dt} | {name} | {impact} |")
    else:
        lines.append(f"*{_t(lang, 'no_econ_cal')}*")
    lines.append("")

    # ── 10 new skills ─────────────────────────────────────────────────────
    lines.append(f"## {_t(lang, 'macro_signals')}")
    lines.append("")
    macro = results.get("Macro Signals", {}).get("data")
    if macro and isinstance(macro, dict):
        meta = macro.get("metadata", {})
        sig = macro.get("signals", {})
        lines.append(f"**Regime:** {meta.get('regime', 'N/A')}")
        yc = meta.get("yield_curve", {})
        if yc:
            lines.append(f"**Yield Curve:** {yc.get('shape','?').upper()} | 10Y-3M spread: {yc.get('spread','?')}% | {yc.get('signal','')}")
        rows = [
            ("VIX", "^VIX"), ("10Y Yield", "^TNX"), ("3M Yield", "^IRX"),
            ("DXY", "DX-Y.NYB"), ("Gold", "GLD"), ("Oil (WTI)", "CL=F"),
            ("SPY", "SPY"), ("HYG (Credit)", "HYG"),
        ]
        lines.append("")
        lines.append("| Indicator | Value | 1M % |")
        lines.append("|-----------|------:|-----:|")
        for label, ticker in rows:
            d = sig.get(ticker, {})
            val = d.get("current", "N/A")
            m1 = d.get("momentum_1m", "N/A")
            m1_str = f"{m1:+.1f}%" if isinstance(m1, (int, float)) else str(m1)
            lines.append(f"| {label} | {val} | {m1_str} |")
    else:
        lines.append(f"*{_t(lang, 'no_macro_signals')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'sector_rotation')}")
    lines.append("")
    sector = results.get("Sector Rotation", {}).get("data")
    if sector and isinstance(sector, dict):
        rotation_signal = sector.get("metadata", {}).get("rotation_signal", "N/A")
        lines.append(f"**Signal:** {rotation_signal}")
        lines.append("")
        lines.append("| Rank | ETF | Sector | 1M% | 3M% | Composite |")
        lines.append("|-----:|-----|--------|----:|----:|----------:|")
        for r in sector.get("results", [])[:11]:
            if isinstance(r, dict):
                lines.append(
                    f"| {r.get('rank','?')} | {r.get('ticker','?')} | {r.get('name','?')} "
                    f"| {r.get('momentum_1m','N/A')}% | {r.get('momentum_3m','N/A')}% "
                    f"| {r.get('composite_score','N/A')} |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_sector_rotation')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'technical_indicators')}")
    lines.append("")
    tech = results.get("Technical Indicators", {}).get("data")
    if tech and isinstance(tech, dict) and tech.get("results"):
        lines.append("| Symbol | Price | RSI | MACD | BB | Signal |")
        lines.append("|--------|------:|----:|------|-----|--------|")
        for r in tech.get("results", [])[:10]:
            if isinstance(r, dict):
                macd_cross = (r.get("macd") or {}).get("crossover", "N/A")
                bb_pos = (r.get("bollinger_bands") or {}).get("position", "N/A")
                lines.append(
                    f"| {r.get('symbol','?')} | ${r.get('price','?')} | {r.get('rsi_14','?')} "
                    f"| {macd_cross} | {bb_pos} | {r.get('signal_summary','?')} |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_tech_indicators')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'news_sentiment')}")
    lines.append("")
    news = results.get("News Sentiment", {}).get("data")
    if news and isinstance(news, dict) and news.get("results"):
        lines.append("| Symbol | Articles | Sentiment | Signal |")
        lines.append("|--------|--------:|----------:|--------|")
        for r in news.get("results", []):
            if isinstance(r, dict):
                sent = r.get("sentiment", 0)
                sent_str = f"{sent:+.3f}" if isinstance(sent, (int, float)) else str(sent)
                lines.append(
                    f"| {r.get('symbol','?')} | {r.get('articles',0)} "
                    f"| {sent_str} | {r.get('signal','neutral')} |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_news_sentiment')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'breakouts')}")
    lines.append("")
    breakouts = results.get("Breakout Scanner", {}).get("data")
    if breakouts and isinstance(breakouts, dict) and breakouts.get("results"):
        lines.append("| Symbol | Price | Vol Ratio | Type | Score |")
        lines.append("|--------|------:|----------:|------|------:|")
        for r in breakouts.get("results", [])[:8]:
            if isinstance(r, dict):
                btype = " + ".join(r.get("breakout_type", [])) or "?"
                lines.append(
                    f"| {r.get('symbol','?')} | ${r.get('price','?')} "
                    f"| {r.get('volume_ratio','?')}x | {btype} | {r.get('breakout_score','?')} |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_breakouts')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'mean_reversion')}")
    lines.append("")
    mean_rev = results.get("Mean Reversion", {}).get("data")
    if mean_rev and isinstance(mean_rev, dict) and mean_rev.get("results"):
        lines.append("| Symbol | Price | RSI | Pullback% | Target | Score |")
        lines.append("|--------|------:|----:|----------:|-------:|------:|")
        for r in mean_rev.get("results", [])[:8]:
            if isinstance(r, dict):
                lines.append(
                    f"| {r.get('symbol','?')} | ${r.get('price','?')} "
                    f"| {r.get('rsi_14','?')} | {r.get('pullback_pct','?')}% "
                    f"| ${r.get('target','?')} | {r.get('reversion_score','?')} |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_mean_reversion')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'options_flow')}")
    lines.append("")
    opts = results.get("Options Flow", {}).get("data")
    if opts and isinstance(opts, dict) and opts.get("results"):
        lines.append("| Symbol | Type | Vol/OI Ratio | Signal | Score |")
        lines.append("|--------|------|-------------:|--------|------:|")
        for r in opts.get("results", [])[:8]:
            if isinstance(r, dict):
                lines.append(
                    f"| {r.get('symbol','?')} | {r.get('option_type','?')} "
                    f"| {r.get('vol_oi_ratio','?')}x | {r.get('signal','')} "
                    f"| {r.get('score','?')} |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_options_flow')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'earnings_momentum')}")
    lines.append("")
    earn_mom = results.get("Earnings Momentum", {}).get("data")
    if earn_mom and isinstance(earn_mom, dict) and earn_mom.get("results"):
        lines.append("| Symbol | Grade | Gap% | 5D Momentum | 10D Momentum |")
        lines.append("|--------|-------|-----:|------------:|-------------:|")
        for r in earn_mom.get("results", [])[:8]:
            if isinstance(r, dict):
                lines.append(
                    f"| {r.get('symbol','?')} | {r.get('pead_grade','?')} "
                    f"| {r.get('gap_pct','?')}% | {r.get('momentum_5d','?')}% "
                    f"| {r.get('momentum_10d','?')}% |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_earnings_momentum')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'insider_buying')}")
    lines.append("")
    insider = results.get("Insider Buying", {}).get("data")
    if insider and isinstance(insider, dict) and insider.get("results"):
        lines.append("| Symbol | Grade | Score | Insiders | Total Value |")
        lines.append("|--------|-------|------:|---------:|------------:|")
        for r in insider.get("results", [])[:8]:
            if isinstance(r, dict):
                val = r.get("total_value_usd", 0)
                val_str = f"${val:,.0f}" if isinstance(val, (int, float)) else str(val)
                lines.append(
                    f"| {r.get('symbol','?')} | {r.get('grade','?')} "
                    f"| {r.get('conviction_score','?')} | {r.get('unique_insiders','?')} "
                    f"| {val_str} |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_insider_buying')}*")
    lines.append("")

    lines.append(f"## {_t(lang, 'short_squeeze')}")
    lines.append("")
    squeeze = results.get("Short Squeeze", {}).get("data")
    if squeeze and isinstance(squeeze, dict) and squeeze.get("results"):
        lines.append("| Symbol | Short Float% | DTC | Score | Setup |")
        lines.append("|--------|-------------:|----:|------:|-------|")
        for r in squeeze.get("results", [])[:8]:
            if isinstance(r, dict):
                lines.append(
                    f"| {r.get('symbol','?')} | {r.get('short_float_pct','?')}% "
                    f"| {r.get('days_to_cover','?')} | {r.get('squeeze_score','?')} "
                    f"| {r.get('setup','')} |"
                )
    else:
        lines.append(f"*{_t(lang, 'no_short_squeeze')}*")
    lines.append("")

    lines.append("---")
    lines.append(f"*{_t(lang, 'generated_at')} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    return "\n".join(lines)

def cleanup_old_dashboards(retention_days: int = RETENTION_DAYS) -> None:
    cutoff = date.today() - timedelta(days=retention_days)
    for pattern in ["daily_dashboard_*.md", "daily_dashboard_*.json"]:
        for path in DASHBOARD_DIR.glob(pattern):
            try:
                stem = path.stem.replace("daily_dashboard_", "").split("_")[0]
                file_date = date.fromisoformat(stem)
                if file_date < cutoff:
                    path.unlink()
            except ValueError:
                continue

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent.parent)
    parser.add_argument("--lang", choices=["en", "ja"], default="en")
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    if not (project_root / "skills").is_dir():
        logger.error("skills/ not found at %s", project_root)
        sys.exit(1)
    logger.info("Project root: %s", project_root)
    logger.info("Running 17 skills in parallel...")
    results = run_all_skills(project_root)
    for name, result in results.items():
        logger.info("  %s: status=%s, has_data=%s", name, result.get("status"), result.get("data") is not None)
    today = date.today()
    markdown = generate_markdown(results, today, lang=args.lang)
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DASHBOARD_DIR / f"daily_dashboard_{today.isoformat()}.md"
    output_path.write_text(markdown, encoding="utf-8")
    logger.info("Dashboard saved to %s", output_path)

    json_summary = generate_json_summary(results, today)
    json_path = DASHBOARD_DIR / f"daily_dashboard_{today.isoformat()}.json"
    json_path.write_text(json.dumps(json_summary, indent=2), encoding="utf-8")
    logger.info("JSON summary saved to %s", json_path)
    cleanup_old_dashboards()
    logger.info("Done.")

if __name__ == "__main__":
    main()
