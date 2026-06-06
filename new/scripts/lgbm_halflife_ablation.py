"""LightGBM half-life recency weighting ablation grid.

정책:
- primary grid: off + 15/30/45/60/90/120 (walk-forward train_window_days=60 기준).
- sensitivity grid: 180/365 (final-window 추가 확인용 별도 섹션).
- validation은 unweighted 유지 (train fold만 weighted).
- candidate evidence only. champion/active 승격 X.

실행 예:
  python new/scripts/lgbm_halflife_ablation.py \\
    --tickers 005930,000660,042700,058470,403870,329180,042660,010140,009540,267250,006400,051910,373220,096770,247540,012450,047810,079550,298040,272210,105560,055550,086790,024110,000810,005380,000270,012330,011210,086280 \\
    --start-date 20260101 --end-date 20260601 \\
    --bundle-id BUNDLE-20260601-HLAB \\
    --grid primary

산출물:
  artifacts/reports/halflife_ablation/halflife_ablation_{bundle_id}_{timestamp}.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.models.lgbm_trainer import LGBMTrainer  # noqa: E402
from src.models.registry import ModelRegistry  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

logger = get_logger("halflife_ablation")

# primary grid (train_window_days=60 기준)
_PRIMARY_GRID: tuple[int | None, ...] = (None, 15, 30, 45, 60, 90, 120)
# sensitivity grid (final-window 추가 확인)
_SENSITIVITY_GRID: tuple[int, ...] = (180, 365)


def _half_life_label(half_life: int | None) -> str:
    return "off" if half_life is None else f"h{half_life}"


def _to_float(value: Any) -> float | None:
    """numpy.float64 등을 표준 Python float로 캐스팅 (json.dump 호환)."""
    return float(value) if value is not None else None


def _clean_metrics(metrics: Any) -> dict[str, float | None]:
    """metrics dict 전체를 표준 Python float로 캐스팅."""
    if not isinstance(metrics, dict):
        return {}
    return {str(k): _to_float(v) for k, v in metrics.items()}


def _safe_filename_slug(text: str) -> str:
    """파일명에 안전한 문자만 남김 (path traversal + Windows 금지 문자 차단).

    operator 입력(bundle_id)에 '/', '..', ':' 등이 섞여도 output_dir 밖으로
    새지 않게 영숫자/하이픈/언더스코어/점만 허용. 원본은 JSON 본문에 유지.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(text))
    safe = safe.lstrip(".")  # leading dot (..) 차단
    return safe[:100] or "unknown"


def run_ablation(
    *,
    tickers: list[str],
    start_date: str,
    end_date: str,
    bundle_id: str,
    grid: tuple[int | None, ...],
    output_dir: Path,
) -> dict[str, Any]:
    """주어진 grid의 각 half_life로 LGBMTrainer.train() 실행.

    candidate evidence 전용. is_latest=False + bundle_id 명시로
    production registry mutation 방지.

    LGBMTrainer 인스턴스는 loop 밖에서 1회만 생성 (config_load 중복 호출 회피).
    LGBMTrainer.train()이 stateless하여 grid 간 재사용 안전.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    safe_bundle = _safe_filename_slug(bundle_id)
    results: list[dict[str, Any]] = []

    # 연구용 registry 명시 주입: production registry와 산출물 분리.
    # is_latest=False만으로는 candidate artifact 저장 자체를 막지 않으므로
    # 연구용 base_dir로 ModelRegistry instance를 만들어야 production mutation 방지가 실효.
    research_dir = ROOT / "artifacts" / "lgbm_research" / "halflife_ablation" / safe_bundle
    research_dir.mkdir(parents=True, exist_ok=True)
    research_registry = ModelRegistry(artifacts_dir=research_dir)

    # loop 밖에서 1회 생성: config_load 등 helper 초기화 중복 회피.
    trainer = LGBMTrainer(registry=research_registry)

    for half_life in grid:
        label = _half_life_label(half_life)
        logger.info("[halflife_ablation] half_life=%s 시작", label)
        try:
            result = trainer.train(
                tickers=tickers,
                start_date=start_date,
                end_date=end_date,
                version=f"ablation-{label}-{timestamp}",
                bundle_id=bundle_id,
                is_latest=False,
                sample_weight_half_life=half_life,
            )
            metrics = _clean_metrics(result.get("metrics"))
            fold_metrics = result.get("fold_metrics") or []
            results.append({
                "half_life": half_life,
                "label": label,
                "status": "PASS",
                "metrics": metrics,
                "n_folds": result.get("n_folds"),
                "n_train_rows": result.get("n_train_rows"),
                "n_val_rows": result.get("n_val_rows"),
                # 키 이름 정정: list of fold values (mean 아님).
                "fold_ics": [_to_float(fm.get("ic")) for fm in fold_metrics],
                "fold_rank_ics": [_to_float(fm.get("rank_ic")) for fm in fold_metrics],
                "fold_srs": [_to_float(fm.get("sr")) for fm in fold_metrics],
                "version": result.get("version"),
                "model_path": result.get("model_path"),
            })
            logger.info(
                "[halflife_ablation] half_life=%s PASS: IC=%.4f, RankIC=%.4f, SR=%.4f",
                label,
                _to_float(metrics.get("ic")) or 0.0,
                _to_float(metrics.get("rank_ic")) or 0.0,
                _to_float(metrics.get("sr")) or 0.0,
            )
        except Exception as e:
            logger.warning(
                "[halflife_ablation] half_life=%s 실패: %s", label, e,
            )
            results.append({
                "half_life": half_life,
                "label": label,
                "status": "FAIL",
                "error": str(e),
            })

    report = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,  # 원본 유지 (파일명에는 slug 적용)
        "candidate_only": True,
        "validation_unweighted": True,
        "research_registry_dir": str(research_dir),
        "grid_policy": (
            "primary: off+15/30/45/60/90/120 (train_window_days=60 기준), "
            "sensitivity: 180/365 (final-window 추가 확인)"
        ),
        "grid": [_half_life_label(h) for h in grid],
        "tickers": list(tickers),
        "start_date": start_date,
        "end_date": end_date,
        "results": results,
    }
    # 파일명에는 slug 적용 (path traversal + 특수문자 차단). 원본은 JSON 본문에 보존.
    report_path = output_dir / f"halflife_ablation_{safe_bundle}_{timestamp}.json"
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    logger.info("[halflife_ablation] report 저장: %s", report_path)
    report["report_path"] = str(report_path)
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LightGBM half-life recency weighting ablation",
    )
    parser.add_argument(
        "--tickers", required=True, help="comma-separated tickers",
    )
    parser.add_argument("--start-date", required=True, help="YYYYMMDD")
    parser.add_argument("--end-date", required=True, help="YYYYMMDD")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument(
        "--grid",
        choices=["primary", "sensitivity", "all"],
        default="primary",
        help="primary: off+15/30/45/60/90/120 (default), "
             "sensitivity: 180/365 별도, all: 양쪽 합산",
    )
    parser.add_argument(
        "--output-dir",
        default="artifacts/reports/halflife_ablation",
    )
    return parser.parse_args(argv)


def _resolve_grid(grid_name: str) -> tuple[int | None, ...]:
    if grid_name == "primary":
        return _PRIMARY_GRID
    if grid_name == "sensitivity":
        return _SENSITIVITY_GRID
    return _PRIMARY_GRID + _SENSITIVITY_GRID


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        raise ValueError("tickers 비어있음")

    grid = _resolve_grid(args.grid)
    output_dir = ROOT / args.output_dir
    report = run_ablation(
        tickers=tickers,
        start_date=args.start_date,
        end_date=args.end_date,
        bundle_id=args.bundle_id,
        grid=grid,
        output_dir=output_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
