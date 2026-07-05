"""Position sizer for long stock trades.

Calculates risk-based position sizes using Fixed Fractional, ATR-based,
or Kelly Criterion methods. Applies portfolio constraints (max position %,
max sector %) and outputs a final recommended share count.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime


@dataclass
class SizingParameters:
    account_size: float
    entry_price: float | None = None
    stop_price: float | None = None
    risk_pct: float | None = None
    atr: float | None = None
    atr_multiplier: float = 2.0
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    max_position_pct: float | None = None  # None → defaults to 5% hard cap (kelly_sizing.py compatible)
    max_sector_pct: float | None = None
    sector: str | None = None
    current_sector_exposure: float = 0.0
    n_trades: int = 0               # closed trade count; below MIN_TRADES → fallback flat 5%


def validate_parameters(params: SizingParameters) -> None:
    """Validate input parameters. Raise ValueError on invalid input."""
    if params.account_size <= 0:
        raise ValueError("account_size must be positive")
    if params.entry_price is not None and params.entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if params.stop_price is not None and params.entry_price is not None:
        if params.stop_price >= params.entry_price:
            raise ValueError("stop_price must be below entry_price for long trades")
    if params.risk_pct is not None and params.risk_pct <= 0:
        raise ValueError("risk_pct must be positive")
    if params.atr is not None and params.atr <= 0:
        raise ValueError("atr must be positive")
    if params.win_rate is not None:
        if params.win_rate <= 0 or params.win_rate > 1.0:
            raise ValueError("win_rate must be between 0 (exclusive) and 1.0 (inclusive)")
    if params.avg_win is not None and params.avg_win <= 0:
        raise ValueError("avg_win must be positive")
    if params.avg_loss is not None and params.avg_loss <= 0:
        raise ValueError("avg_loss must be positive")


def calculate_fixed_fractional(params: SizingParameters) -> dict:
    """Fixed fractional position sizing.

    Calculates shares based on a fixed percentage of account risked per trade.
    risk_per_share = entry_price - stop_price
    dollar_risk = account_size * risk_pct / 100
    shares = int(dollar_risk / risk_per_share)
    """
    risk_per_share = params.entry_price - params.stop_price
    dollar_risk = params.account_size * params.risk_pct / 100
    shares = int(dollar_risk / risk_per_share)
    return {
        "method": "fixed_fractional",
        "shares": shares,
        "risk_per_share": round(risk_per_share, 2),
        "dollar_risk": round(dollar_risk, 2),
        "stop_price": params.stop_price,
    }


def calculate_atr_based(params: SizingParameters) -> dict:
    """ATR-based position sizing.

    Uses Average True Range to determine stop distance:
    stop_distance = atr * atr_multiplier
    stop_price = entry_price - stop_distance
    """
    stop_distance = params.atr * params.atr_multiplier
    stop_price = round(params.entry_price - stop_distance, 2)
    risk_per_share = stop_distance
    dollar_risk = params.account_size * params.risk_pct / 100
    shares = int(dollar_risk / risk_per_share)
    return {
        "method": "atr_based",
        "shares": shares,
        "risk_per_share": round(risk_per_share, 2),
        "dollar_risk": round(dollar_risk, 2),
        "stop_price": stop_price,
        "atr": params.atr,
        "atr_multiplier": params.atr_multiplier,
    }


def calculate_kelly(params: SizingParameters) -> dict:
    """Kelly Criterion calculation.

    Kelly % = W - (1-W)/R
    where W = win_rate, R = avg_win / avg_loss
    Half-Kelly = kelly_pct / 2 (recommended conservative amount)
    Negative expectancy floors at 0%.
    """
    w = params.win_rate
    r = params.avg_win / params.avg_loss
    kelly_pct = w - (1 - w) / r
    kelly_pct = max(0.0, kelly_pct) * 100  # Convert to percentage, floor at 0
    half_kelly_pct = kelly_pct / 2
    return {
        "method": "kelly",
        "kelly_pct": round(kelly_pct, 2),
        "half_kelly_pct": round(half_kelly_pct, 2),
    }


def apply_constraints(shares: int, params: SizingParameters) -> tuple[int, list[dict], str | None]:
    """Apply portfolio constraints and return (final_shares, constraints, binding).

    Evaluates max position % and max sector % constraints, then returns
    the minimum of all candidate share counts (strictest constraint wins).
    When entry_price is None ( Kelly budget mode), the max position cap is
    applied as a notional ceiling directly; shares is returned unchanged.
    """
    constraints: list[dict] = []
    candidates = [shares]
    binding: str | None = None

    if params.max_position_pct is not None:
        if params.entry_price:
            max_by_pos = int(params.account_size * params.max_position_pct / 100 / params.entry_price)
            candidates.append(max_by_pos)
            constraints.append(
                {
                    "type": "max_position_pct",
                    "limit": params.max_position_pct,
                    "max_shares": max_by_pos,
                    "binding": False,
                }
            )
        else:
            # Kelly budget mode (no entry price): apply cap as notional ceiling
            constraints.append(
                {
                    "type": "max_position_pct",
                    "limit": params.max_position_pct,
                    "binding": False,
                    "note": "budget mode: notional cap",
                }
            )

    if params.max_sector_pct is not None and params.entry_price:
        remaining_pct = params.max_sector_pct - params.current_sector_exposure
        remaining_dollars = remaining_pct / 100 * params.account_size
        max_by_sector = max(0, int(remaining_dollars / params.entry_price))
        constraints.append(
            {
                "type": "max_sector_pct",
                "limit": params.max_sector_pct,
                "current": params.current_sector_exposure,
                "max_shares": max_by_sector,
                "binding": False,
            }
        )
        candidates.append(max_by_sector)

    final = max(0, min(candidates))

    # Identify binding constraint
    for c in constraints:
        if c["max_shares"] == final and final < shares:
            c["binding"] = True
            binding = c["type"]

    return final, constraints, binding


def calculate_position(params: SizingParameters, min_trades: int = 20) -> dict:
    """Main calculation entry point.

    Determines mode (budget or shares), runs the appropriate sizing method,
    applies constraints, and returns the full result dictionary.

    Guardrails matching kelly_sizing.py:
    - min_trades gate: if n_trades < min_trades, returns flat 5% budget (no Kelly).
    - Hard max_position_pct cap (default 5.0%): applied to Kelly fraction in both
      budget and shares mode, even when entry_price is absent.
    """
    validate_parameters(params)

    default_max_pct = 0.05            # kelly_sizing.py default
    max_pct = (params.max_position_pct / 100) if params.max_position_pct is not None else default_max_pct

    is_kelly_mode = params.win_rate is not None
    has_entry = params.entry_price is not None
    insufficient_trades = is_kelly_mode and params.n_trades > 0 and params.n_trades < min_trades

    result: dict = {
        "schema_version": "1.0",
        "parameters": {},
    }

    # ── Trade-count gate: fall back to flat max_pct (kelly_sizing.py compatible) ─
    if insufficient_trades:
        budget_pct = max_pct
        budget = params.account_size * budget_pct
        result["mode"] = "budget" if not has_entry else "shares"
        result["calculations"] = {
            "kelly": {
                "method": "kelly",
                "kelly_pct": 0.0,
                "half_kelly_pct": 0.0,
                "reason": f"fallback flat {budget_pct:.0%} (n={params.n_trades} < {min_trades})",
            },
            "fixed_fractional": None,
            "atr_based": None,
        }
        if not has_entry:
            result["recommended_risk_budget"] = round(budget, 2)
            result["recommended_risk_budget_pct"] = budget_pct
            result["note"] = f"Tail-risk fallback: only {params.n_trades} closed trades < {min_trades} minimum."
        else:
            risk_per_share = params.entry_price - params.stop_price if params.stop_price else 0
            shares = int(budget / risk_per_share) if risk_per_share else 0
            result["final_recommended_shares"] = shares
            result["final_position_value"] = round(shares * params.entry_price, 2)
            result["final_risk_dollars"] = (
                round(shares * risk_per_share, 2) if risk_per_share else None
            )
            result["final_risk_pct"] = (
                round(shares * risk_per_share / params.account_size, 4) if risk_per_share else None
            )
        return result

    # ── Budget mode (Kelly, no entry price) ───────────────────────────────────
    if is_kelly_mode and not has_entry:
        kelly = calculate_kelly(params)
        raw_budget_pct = kelly["half_kelly_pct"] / 100
        capped_budget_pct = min(raw_budget_pct, max_pct)           # hard cap
        capped_budget = params.account_size * capped_budget_pct

        result["mode"] = "budget"
        result["parameters"] = {
            "win_rate": params.win_rate,
            "avg_win": params.avg_win,
            "avg_loss": params.avg_loss,
            "account_size": params.account_size,
            "n_trades": params.n_trades or None,
        }
        result["calculations"] = {
            "kelly": {
                **kelly,
                "raw_half_kelly_pct": kelly["half_kelly_pct"],
                "capped_half_kelly_pct": round(capped_budget_pct * 100, 2),
                "hard_cap_pct": round(max_pct * 100, 2),
                "was_capped": raw_budget_pct > max_pct,
            },
            "fixed_fractional": None,
            "atr_based": None,
        }
        result["recommended_risk_budget"] = round(capped_budget, 2)
        result["recommended_risk_budget_pct"] = round(capped_budget_pct * 100, 2)
        return result

    # Shares mode
    result["mode"] = "shares"
    result["parameters"] = {
        "entry_price": params.entry_price,
        "account_size": params.account_size,
    }

    calculations: dict = {
        "fixed_fractional": None,
        "atr_based": None,
        "kelly": None,
    }
    risk_shares = 0

    if is_kelly_mode:
        kelly_raw = calculate_kelly(params)
        raw_budget_pct_kelly = kelly_raw["half_kelly_pct"] / 100
        capped_budget_pct_kelly = min(raw_budget_pct_kelly, max_pct)  # hard cap
        kelly = {
            **kelly_raw,
            "raw_half_kelly_pct": kelly_raw["half_kelly_pct"],
            "capped_half_kelly_pct": round(capped_budget_pct_kelly * 100, 2),
            "hard_cap_pct": round(max_pct * 100, 2),
            "was_capped": raw_budget_pct_kelly > max_pct,
        }
        calculations["kelly"] = kelly
        budget = params.account_size * capped_budget_pct_kelly  # capped budget
        if params.stop_price:
            risk_per_share = params.entry_price - params.stop_price
            risk_shares = int(budget / risk_per_share)
            result["parameters"]["stop_price"] = params.stop_price
        else:
            risk_shares = int(budget / params.entry_price)
        result["parameters"]["n_trades"] = params.n_trades or None
    elif params.atr is not None:
        atr_result = calculate_atr_based(params)
        calculations["atr_based"] = atr_result
        risk_shares = atr_result["shares"]
        result["parameters"]["stop_price"] = atr_result["stop_price"]
        result["parameters"]["risk_pct"] = params.risk_pct
    else:
        ff_result = calculate_fixed_fractional(params)
        calculations["fixed_fractional"] = ff_result
        risk_shares = ff_result["shares"]
        result["parameters"]["stop_price"] = params.stop_price
        result["parameters"]["risk_pct"] = params.risk_pct

    result["calculations"] = calculations

    # Apply constraints
    final_shares, constraints, binding = apply_constraints(risk_shares, params)
    result["constraints_applied"] = constraints
    result["final_recommended_shares"] = final_shares
    result["final_position_value"] = round(final_shares * params.entry_price, 2)

    # Calculate actual risk for final shares
    if params.stop_price:
        risk_per_share = params.entry_price - params.stop_price
        result["final_risk_dollars"] = round(final_shares * risk_per_share, 2)
        result["final_risk_pct"] = round(
            final_shares * risk_per_share / params.account_size * 100, 2
        )
    elif params.atr:
        risk_per_share = params.atr * params.atr_multiplier
        result["final_risk_dollars"] = round(final_shares * risk_per_share, 2)
        result["final_risk_pct"] = round(
            final_shares * risk_per_share / params.account_size * 100, 2
        )
    else:
        # Kelly shares mode without stop/ATR: risk per share is undefined
        result["final_risk_dollars"] = None
        result["final_risk_pct"] = None
        result["risk_note"] = "Stop-loss not defined. Specify --stop to calculate risk dollars."
    result["binding_constraint"] = binding

    return result


def generate_markdown_report(result: dict) -> str:
    """Generate a markdown report from the calculation result."""
    lines = [
        "# Position Sizing Report",
        "**Generated:** {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "**Mode:** {}".format(result["mode"]),
        "",
        "## Parameters",
    ]
    for k, v in result.get("parameters", {}).items():
        lines.append(f"- **{k}:** {v}")
    lines.append("")

    if result["mode"] == "budget":
        lines.append("## Kelly Criterion")
        kelly = result["calculations"]["kelly"]
        kelly_reason = kelly.get("reason", "")
        if kelly_reason:
            lines.append(f"- ⚠️ {kelly_reason}")
        else:
            lines.append(
                "- Half Kelly (recommended): {}%".format(kelly.get("capped_half_kelly_pct", kelly["half_kelly_pct"]))
            )
            raw = kelly.get("raw_half_kelly_pct")
            cap = kelly.get("hard_cap_pct", 5.0)
            if kelly.get("was_capped") and raw is not None:
                cap_raw = kelly.get("capped_half_kelly_pct", "N/A")
                lines.append(
                    f"  ⚠️ Raw half-Kelly {raw:.2f}% exceeds hard cap {cap:.2f}% → capped at {cap_raw}%"
                )
            lines.append(f"- Hard cap: {cap:.2f}% of account")
            if result["parameters"].get("n_trades"):
                lines.append(f"- Sample size: {result['parameters']['n_trades']} closed trades")
        lines.append(
            "- **Recommended Risk Budget:** ${:,.2f}".format(result["recommended_risk_budget"])
        )
        lines.append("  ({}% of account)".format(result["recommended_risk_budget_pct"]))
        if result.get("note"):
            lines.append("")
            lines.append("*{}*".format(result["note"]))
    else:
        lines.append("## Calculations")
        for method, calc in result.get("calculations", {}).items():
            if calc:
                lines.append("### {}".format(method.replace("_", " ").title()))
                for k, v in calc.items():
                    if k != "method":
                        if k == "was_capped":
                            label = "⚠️ hard-capped" if v else "ok"
                            lines.append(f"- {k}: {v} ({label})")
                        else:
                            lines.append(f"- {k}: {v}")
                lines.append("")

        if result.get("constraints_applied"):
            lines.append("## Constraints")
            for c in result["constraints_applied"]:
                binding_label = " **[BINDING]**" if c.get("binding") else ""
                lines.append(
                    "- {}: limit={}%, max_shares={}{}".format(
                        c["type"], c["limit"], c["max_shares"], binding_label
                    )
                )
            lines.append("")

        lines.append("## Final Recommendation")
        lines.append("- **Shares:** {}".format(result["final_recommended_shares"]))
        lines.append("- **Position Value:** ${:,.2f}".format(result["final_position_value"]))
        if result.get("final_risk_dollars") is not None:
            lines.append(
                "- **Risk:** ${:,.2f} ({}%)".format(
                    result["final_risk_dollars"], result["final_risk_pct"]
                )
            )
        if result.get("risk_note"):
            lines.append("- **Note:** {}".format(result["risk_note"]))
        if result.get("binding_constraint"):
            lines.append("- **Binding Constraint:** {}".format(result["binding_constraint"]))

    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for CLI usage."""
    parser = argparse.ArgumentParser(
        description=("Calculate risk-based position sizes for long stock trades")
    )
    parser.add_argument(
        "--account-size",
        type=float,
        required=True,
        help="Total account value in dollars",
    )
    parser.add_argument("--entry", type=float, help="Entry price per share")
    parser.add_argument("--stop", type=float, help="Stop-loss price per share")
    parser.add_argument(
        "--risk-pct",
        type=float,
        help="Risk percentage per trade (e.g. 1.0 for 1%%)",
    )
    parser.add_argument("--atr", type=float, help="Average True Range value")
    parser.add_argument(
        "--atr-multiplier",
        type=float,
        default=2.0,
        help="ATR multiplier for stop distance (default: 2.0)",
    )
    parser.add_argument(
        "--win-rate",
        type=float,
        help="Historical win rate (0-1) for Kelly criterion",
    )
    parser.add_argument(
        "--avg-win",
        type=float,
        help="Average win amount for Kelly criterion",
    )
    parser.add_argument(
        "--avg-loss",
        type=float,
        help="Average loss amount for Kelly criterion",
    )
    parser.add_argument(
        "--max-position-pct",
        type=float,
        help="Maximum position as %% of account (default: 5%%, always enforced for Kelly mode)",
    )
    parser.add_argument(
        "--n-trades",
        type=int,
        default=0,
        help="Number of closed trades used to derive win_rate/avg_win/avg_loss (default: 0). "
             "Below 20 trades Kelly is skipped and flat 5%% budget is returned "
             "(kelly_sizing.py compatible minimum-trade gate).",
    )
    parser.add_argument(
        "--max-sector-pct",
        type=float,
        help="Maximum sector exposure as %% of account",
    )
    parser.add_argument("--sector", type=str, help="Sector name for concentration check")
    parser.add_argument(
        "--current-sector-exposure",
        type=float,
        default=0.0,
        help="Current sector exposure as %% of account",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="reports/",
        help="Output directory for reports",
    )
    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    # Validate mutual exclusivity
    if args.risk_pct is not None and args.win_rate is not None:
        parser.error("position sizing requires either risk-pct mode OR kelly mode, not both")

    # Validate required combinations
    if args.win_rate is not None:
        if args.avg_win is None or args.avg_loss is None:
            parser.error("Kelly mode requires --win-rate, --avg-win, and --avg-loss")
    elif args.risk_pct is not None:
        if args.entry is None:
            parser.error("Risk-pct mode requires --entry")
        if args.stop is None and args.atr is None:
            parser.error("Risk-pct mode requires either --stop or --atr")
    else:
        parser.error("Must specify either --risk-pct or --win-rate mode")

    params = SizingParameters(
        account_size=args.account_size,
        entry_price=args.entry,
        stop_price=args.stop,
        risk_pct=args.risk_pct,
        atr=args.atr,
        atr_multiplier=args.atr_multiplier,
        win_rate=args.win_rate,
        avg_win=args.avg_win,
        avg_loss=args.avg_loss,
        max_position_pct=args.max_position_pct,
        max_sector_pct=args.max_sector_pct,
        sector=args.sector,
        current_sector_exposure=args.current_sector_exposure,
        n_trades=args.n_trades,
    )

    try:
        result = calculate_position(params)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Output
    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    json_path = os.path.join(args.output_dir, f"position_sizer_{timestamp}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"JSON report: {json_path}")

    md_report = generate_markdown_report(result)
    md_path = os.path.join(args.output_dir, f"position_sizer_{timestamp}.md")
    with open(md_path, "w") as f:
        f.write(md_report)
    print(f"Markdown report: {md_path}")

    # Also print summary to stdout
    if result["mode"] == "shares":
        print(
            "\nFinal: {} shares @ ${}".format(
                result["final_recommended_shares"], params.entry_price
            )
        )
        print("Position: ${:,.2f}".format(result["final_position_value"]))
        if result.get("final_risk_dollars") is not None:
            print(
                "Risk: ${:,.2f} ({}%)".format(
                    result["final_risk_dollars"], result["final_risk_pct"]
                )
            )
        if result.get("risk_note"):
            print("Note: {}".format(result["risk_note"]))
    else:
        print("\nRecommended risk budget: ${:,.2f}".format(result["recommended_risk_budget"]))
        print("({}% of account)".format(result["recommended_risk_budget_pct"]))


if __name__ == "__main__":
    main()
