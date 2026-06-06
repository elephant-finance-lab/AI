#!/usr/bin/env python
"""Liquidate all current KIS virtual paper positions.

This is a paper-only helper for local service rehearsals. It reuses
PaperTradingRunner, so KIS_MODE must be virtual and every submitted sell order
still requires the configured paper confirm phrase.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.execution.paper_trading import PaperTradingRunner  # noqa: E402
from src.utils.safe_cast import safe_float, safe_lossless_int  # noqa: E402
from src.utils.ticker_utils import is_valid_ticker, pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger(__name__)


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _position_qty(position: dict[str, Any]) -> int:
    for key in ("available_qty", "sellable_qty", "qty", "quantity"):
        qty = safe_lossless_int(position.get(key), default=0)
        if qty > 0:
            return qty
    return 0


def _position_price(position: dict[str, Any]) -> float:
    for key in ("current_price", "price", "last_price", "avg_price"):
        price = safe_float(position.get(key), default=0.0)
        if price > 0:
            return price
    return 0.0


def _sell_plan(positions: list[dict[str, Any]], *, chunk_qty: int) -> list[dict[str, Any]]:
    chunk = max(1, safe_lossless_int(chunk_qty, default=1))
    plan: list[dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        ticker = pad_ticker(str(position.get("ticker") or ""))
        if not is_valid_ticker(ticker) or ticker == "000000":
            continue
        qty = _position_qty(position)
        price = _position_price(position)
        if qty <= 0 or price <= 0:
            continue
        remaining = qty
        unit = 1
        while remaining > 0:
            order_qty = min(chunk, remaining)
            plan.append({
                "ticker": ticker,
                "name": position.get("name"),
                "unit": unit,
                "qty": order_qty,
                "price": price,
            })
            remaining -= order_qty
            unit += 1
    return plan


def _write_summary(report: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"paper_liquidate_positions_{ts}.json"
    report["summary_path"] = _repo_relative(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def _paper_env_mode() -> str:
    return os.getenv("KIS_MODE", "virtual").strip().lower()


def _is_virtual_paper_mode(mode: str) -> bool:
    return str(mode or "").strip().lower() in {"virtual", "kis_virtual_paper_only"}


def _empty_summary(*, args: argparse.Namespace, status: str = "PASS") -> dict[str, Any]:
    return {
        "status": status,
        "action": "paper_liquidate_positions",
        "generated_at": datetime.now(_KST).isoformat(),
        "mode": "kis_virtual_paper_only",
        "live_enabled": False,
        "dry_run": bool(args.dry_run),
        "initial_position_count": 0,
        "initial_positions": [],
        "planned_order_count": 0,
        "orders": [],
        "failures": [],
    }


def _finish_blocked(summary: dict[str, Any], *, args: argparse.Namespace) -> int:
    path = _write_summary(summary, ROOT / str(args.output_dir))
    logger.error(
        "[paper_liquidate] 차단 종료: reason=%s failures=%d",
        summary.get("reason"),
        len(summary.get("failures") or []),
    )
    print(
        json.dumps(
            {"status": summary["status"], "summary_path": _repo_relative(path)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-phrase", required=True)
    parser.add_argument("--chunk-qty", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--output-dir",
        default="artifacts/reports/paper_trading",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    env_mode = _paper_env_mode()
    summary = _empty_summary(args=args)
    if not _is_virtual_paper_mode(env_mode):
        summary["status"] = "BLOCKED"
        summary["reason"] = "kis_virtual_mode_required"
        summary["kis_mode"] = env_mode
        logger.error("[paper_liquidate] KIS paper 모드 아님: mode=%s", env_mode)
        return _finish_blocked(summary, args=args)

    logger.info("[paper_liquidate] 잔고 조회 시작")
    runner = PaperTradingRunner()
    runner_mode = str(getattr(runner, "_client_mode", lambda: env_mode)()).lower()
    if not _is_virtual_paper_mode(runner_mode):
        summary["status"] = "BLOCKED"
        summary["reason"] = "runner_virtual_mode_required"
        summary["runner_mode"] = runner_mode
        logger.error("[paper_liquidate] Runner paper 모드 아님: mode=%s", runner_mode)
        return _finish_blocked(summary, args=args)

    try:
        balance_report = runner.run_balance_reconciliation(write_report=True)
    except Exception as e:
        summary["status"] = "BLOCKED"
        summary["reason"] = "balance_reconciliation_error"
        summary["error"] = str(e)
        logger.exception("[paper_liquidate] 잔고 조회 중 예외")
        return _finish_blocked(summary, args=args)

    if not isinstance(balance_report, dict):
        balance_report = {"status": "FAIL", "reason": "balance_report_not_dict"}
    balance_stage = (
        (balance_report.get("stages") or {}).get("balance")
        if isinstance(balance_report.get("stages"), dict)
        else {}
    )
    positions = (
        balance_stage.get("positions")
        if isinstance(balance_stage, dict) and isinstance(balance_stage.get("positions"), list)
        else []
    )
    plan = _sell_plan(positions, chunk_qty=int(args.chunk_qty))

    summary.update(
        {
            "balance_report_status": balance_report.get("status"),
            "initial_position_count": len(positions),
            "initial_positions": positions,
            "planned_order_count": len(plan),
            "kis_mode": env_mode,
            "runner_mode": runner_mode,
        }
    )
    logger.info(
        "[paper_liquidate] 잔고 조회 완료: status=%s positions=%d plan=%d",
        balance_report.get("status"),
        len(positions),
        len(plan),
    )
    if balance_report.get("status") != "PASS":
        summary["status"] = "BLOCKED"
        summary["reason"] = "balance_reconciliation_not_pass"
        return _finish_blocked(summary, args=args)

    if bool(args.dry_run):
        logger.info("[paper_liquidate] dry-run 모드: 주문 제출 생략")
        summary["orders"] = plan
        path = _write_summary(summary, ROOT / str(args.output_dir))
        print(json.dumps({"status": summary["status"], "summary_path": _repo_relative(path), "planned_order_count": len(plan)}, ensure_ascii=False, indent=2))
        return 0

    logger.info("[paper_liquidate] 주문 %d개 제출 시작", len(plan))
    for order in plan:
        try:
            result = runner.submit_probe_order(
                ticker=str(order["ticker"]),
                side="sell",
                qty=int(order["qty"]),
                price=float(order["price"]),
                order_type="00",
                confirm_phrase=str(args.confirm_phrase),
                write_report=True,
            )
        except Exception as e:
            result = {"status": "ERROR", "error": str(e)}
            logger.exception("[paper_liquidate] 주문 제출 중 예외: ticker=%s", order.get("ticker"))
        row = {
            **order,
            "status": result.get("status") if isinstance(result, dict) else "ERROR",
            "report_path": result.get("report_path") if isinstance(result, dict) else None,
        }
        if isinstance(result, dict) and result.get("error"):
            row["error"] = result.get("error")
        summary["orders"].append(row)
        logger.info(
            "[paper_liquidate] 주문 제출 결과: ticker=%s qty=%s status=%s",
            row.get("ticker"),
            row.get("qty"),
            row.get("status"),
        )
        if row.get("status") != "PASS":
            summary["failures"].append(row)

    logger.info("[paper_liquidate] 최종 잔고 재조회 시작")
    try:
        final_report = runner.run_balance_reconciliation(write_report=True)
    except Exception as e:
        final_report = {"status": "FAIL", "reason": "final_balance_reconciliation_error"}
        summary["failures"].append({"status": "ERROR", "reason": "final_balance_reconciliation_error", "error": str(e)})
        logger.exception("[paper_liquidate] 최종 잔고 조회 중 예외")
    if not isinstance(final_report, dict):
        final_report = {"status": "FAIL", "reason": "final_balance_report_not_dict"}
    final_stage = (
        (final_report.get("stages") or {}).get("balance")
        if isinstance(final_report.get("stages"), dict)
        else {}
    )
    final_positions = (
        final_stage.get("positions")
        if isinstance(final_stage, dict) and isinstance(final_stage.get("positions"), list)
        else []
    )
    summary["submitted_order_count"] = len(summary["orders"])
    summary["failure_count"] = len(summary["failures"])
    summary["final_position_count"] = len(final_positions)
    summary["final_positions"] = final_positions
    summary["final_balance_report_status"] = final_report.get("status")
    summary["status"] = (
        "PASS"
        if summary["failure_count"] == 0 and summary["final_position_count"] == 0
        else "BLOCKED"
    )
    logger.info(
        "[paper_liquidate] 최종 요약 작성: status=%s orders=%d failures=%d final_positions=%d",
        summary["status"],
        summary["submitted_order_count"],
        summary["failure_count"],
        summary["final_position_count"],
    )
    path = _write_summary(summary, ROOT / str(args.output_dir))
    print(
        json.dumps(
            {
                "status": summary["status"],
                "summary_path": _repo_relative(path),
                "submitted_order_count": summary["submitted_order_count"],
                "failure_count": summary["failure_count"],
                "final_position_count": summary["final_position_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
