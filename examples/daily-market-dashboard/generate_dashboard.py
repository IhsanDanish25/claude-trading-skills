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

def _skill_defs(project_root: Path) -> list[dict[str, Any]]:
    skills_dir = project_root / "skills"
    return [
        {
            "name": "FTD Detector",
            "script": str(skills_dir / "ftd-detector" / "scripts" / "ftd_detector.py"),
            "args": ["--output-dir", "{tmpdir}", "--api-key", os.environ.get("FMP_API_KEY", "")],
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
            "args": ["--output-dir", "{tmpdir}", "--fmp-api-key", os.environ.get("FMP_API_KEY", "")],
            "glob": "theme_detector_*.json",
        },
        {
            "name": "Market Top Detector",
            "script": str(skills_dir / "market-top-detector" / "scripts" / "market_top_detector.py"),
            "args": ["--output-dir", "{tmpdir}", "--api-key", os.environ.get("FMP_API_KEY", "")],
            "glob": "market_top_*.json",
        },
        {
            "name": "Economic Calendar",
            "script": str(skills_dir / "economic-calendar-fetcher" / "scripts" / "get_economic_calendar.py"),
            "args": ["--output", "{tmpdir}/economic_calendar_latest.json", "--api-key", os.environ.get("FMP_API_KEY", "")],
            "glob": "economic_calendar_latest.json",
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

    # VCP: reuse saved JSON, no API calls
    vcp_data = _load_latest_vcp(project_root)
    results["VCP Screener"] = {"name": "VCP Screener", "status": "cached", "data": vcp_data}

    with tempfile.TemporaryDirectory(prefix="dashboard_") as tmpdir:
        futures = {}
        with ProcessPoolExecutor(max_workers=6) as executor:
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
    return results

def _safe_get(data: Any, *keys: str, default: Any = "N/A") -> Any:
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, default)
        else:
            return default
    return current

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

    lines.append("---")
    lines.append(f"*{_t(lang, 'generated_at')} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    return "\n".join(lines)

def cleanup_old_dashboards(retention_days: int = RETENTION_DAYS) -> None:
    cutoff = date.today() - timedelta(days=retention_days)
    for path in DASHBOARD_DIR.glob("daily_dashboard_*.md"):
        try:
            file_date = date.fromisoformat(path.stem.replace("daily_dashboard_", ""))
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
    logger.info("Running 6 skills + VCP from cache...")
    results = run_all_skills(project_root)
    for name, result in results.items():
        logger.info("  %s: status=%s, has_data=%s", name, result.get("status"), result.get("data") is not None)
    today = date.today()
    markdown = generate_markdown(results, today, lang=args.lang)
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DASHBOARD_DIR / f"daily_dashboard_{today.isoformat()}.md"
    output_path.write_text(markdown, encoding="utf-8")
    logger.info("Dashboard saved to %s", output_path)
    cleanup_old_dashboards()
    logger.info("Done.")

if __name__ == "__main__":
    main()
