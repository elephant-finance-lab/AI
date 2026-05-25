#!/usr/bin/env python
"""Active paper bundle serving-feature readiness gate.

이 스크립트는 KIS 주문을 내지 않고 active QuantAgent가 요구하는
Dual-Source feature artifact만 점검한다. 5/26 paper 운영 전 장전
필수 feature가 준비됐는지 broker 제출 전에 확인하기 위한 좁은 gate다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.agents.hot.quant import QuantAgent  # noqa: E402
from src.data.dual_source_runner import load_latest_scores  # noqa: E402
from src.utils.ticker_utils import pad_ticker  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _asof(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw:
        return raw
    return datetime.now(_KST).isoformat()


def _date_key(asof: str) -> str:
    raw = str(asof)
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_KST)
        else:
            dt = dt.astimezone(_KST)
        return dt.strftime("%Y%m%d")
    except ValueError:
        return raw[:10].replace("-", "")


def _artifact_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_registry_dir(bundle_id: str) -> Path | None:
    bundle = str(bundle_id or "").strip()
    if not bundle:
        return None
    return ROOT / "artifacts" / "lgbm_paper_candidate" / bundle


def _model_bundle_matches(metadata: dict[str, Any] | None, bundle_id: str) -> bool:
    if not bundle_id:
        return True
    if not isinstance(metadata, dict):
        return False
    values = {
        str(metadata.get("bundle_id") or ""),
        str(metadata.get("version") or ""),
    }
    return bundle_id in values


def build_report(
    *,
    bundle_id: str,
    registry_dir: str | None,
    tickers: list[str],
    asof: str,
    quant_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    resolved_registry = Path(registry_dir).expanduser() if registry_dir else _default_registry_dir(bundle_id)
    if resolved_registry is not None:
        if not resolved_registry.is_absolute():
            resolved_registry = ROOT / resolved_registry
        os.environ["ELEPHANT_LGBM_REGISTRY_DIR"] = str(resolved_registry)

    quant = quant_factory() if quant_factory else QuantAgent(dual_source_loader=load_latest_scores)
    normalized_tickers = [pad_ticker(str(t)) for t in tickers if str(t).strip()]
    readiness = quant.serving_feature_readiness(normalized_tickers, asof)
    if not isinstance(readiness, dict):
        readiness = {
            "status": "FAIL",
            "reason": "serving_feature_readiness_invalid",
            "type": type(readiness).__name__,
        }

    metadata = getattr(quant, "model_metadata", None)
    if not isinstance(metadata, dict):
        metadata = None
    model_match = _model_bundle_matches(metadata, bundle_id)
    date_key = _date_key(asof)
    artifact_path = ROOT / "artifacts" / "dual_source" / f"{date_key}.json"
    artifact_rel = str(artifact_path.relative_to(ROOT))

    blockers: list[dict[str, Any]] = []
    if not model_match:
        blockers.append({
            "reason": "bundle_id_mismatch_or_model_missing",
            "required_bundle_id": bundle_id,
            "model_version": metadata.get("version") if metadata else None,
            "model_bundle_id": metadata.get("bundle_id") if metadata else None,
        })
    if readiness.get("status") != "PASS":
        blockers.append({
            "reason": "serving_feature_readiness_not_pass",
            "serving_feature_readiness": readiness,
        })

    required_cols = list(readiness.get("required_dual_source_cols") or [])
    report = {
        "status": "PASS" if not blockers else "FAIL",
        "generated_at": datetime.now(_KST).isoformat(),
        "bundle_id": bundle_id,
        "registry_dir": str(resolved_registry) if resolved_registry else None,
        "model_version": metadata.get("version") if metadata else None,
        "model_bundle_id": metadata.get("bundle_id") if metadata else None,
        "asof": asof,
        "artifact_date": date_key,
        "tickers": normalized_tickers,
        "required": bool(required_cols),
        "required_dual_source_cols": required_cols,
        "required_artifacts": [artifact_rel] if required_cols else [],
        "missing_artifacts": [artifact_rel] if required_cols and not artifact_path.exists() else [],
        "artifact_sha256": _artifact_sha256(artifact_path),
        "serving_feature_readiness": readiness,
        "blockers": blockers,
        "external_kis_api": False,
        "live_trading_allowed": False,
        "production_registry_mutated": False,
    }
    return report


def _write_report(report: dict[str, Any]) -> Path:
    report_dir = ROOT / "artifacts" / "reports" / "serving_feature_readiness"
    report_dir.mkdir(parents=True, exist_ok=True)
    bundle = str(report.get("bundle_id") or "NO_BUNDLE")
    stamp = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"serving_feature_readiness_{bundle}_{stamp}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate paper serving feature readiness")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--registry-dir", default="")
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--asof", default="")
    parser.add_argument("--no-write-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    tickers = [part.strip() for part in str(args.tickers).split(",") if part.strip()]
    report = build_report(
        bundle_id=str(args.bundle_id),
        registry_dir=str(args.registry_dir or ""),
        tickers=tickers,
        asof=_asof(args.asof),
    )
    if not args.no_write_report:
        report["report_path"] = str(_write_report(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
