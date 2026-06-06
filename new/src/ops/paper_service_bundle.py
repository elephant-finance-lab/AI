"""Read-only paper service bundle manifest builder.

This module does not deploy a production champion. It validates a paper-only
candidate bundle and emits a manifest/report that BE can use as a runtime hint
for the daily paper-safe service schedule.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.ops.paper_candidate_registry_validator import validate_paper_candidate_registry
from src.ops.service_readiness_status import build_service_status
from src.utils.safe_cast import safe_bool, safe_int
from src.utils.ticker_utils import is_valid_ticker, pad_ticker

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_REPORT_DIR = Path("artifacts/reports/paper_service_bundle")
_SELECTED_TICKER_MODES = {
    "selected-paper-service",
    "demo-selected-paper-service",
    "selected-paper-auto",
}
_ACTIVE_UNIVERSE_MODES = {
    "demo-paper-service",
    "paper-service-30t",
    "daily-paper-service",
}


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_schedule(
    *,
    repo_root: Path | None = None,
    schedule_path: Path | None = None,
) -> dict[str, Any]:
    root = repo_root or _REPO_ROOT
    path = schedule_path or root / "new" / "config" / "paper_service_schedule.yaml"
    schedule = _load_yaml(path)
    schedule["_schedule_path"] = _repo_relative(path, root)
    schedule["_schedule_hash"] = _sha256_file(path)
    return schedule


def resolve_active_universe(
    *,
    repo_root: Path | None = None,
    max_tickers: int = 30,
    universe_config_path: Path | None = None,
) -> dict[str, Any]:
    root = repo_root or _REPO_ROOT
    path = universe_config_path or root / "new" / "config" / "universe_config.yaml"
    payload = _load_yaml(path)
    tickers: list[str] = []
    sectors = payload.get("sectors") or {}
    if isinstance(sectors, dict):
        for sector in sectors.values():
            if not isinstance(sector, dict):
                continue
            if str(sector.get("status") or "").lower() != "confirmed":
                continue
            stocks = sector.get("stocks") or []
            if not isinstance(stocks, list):
                continue
            for stock in stocks:
                if not isinstance(stock, dict):
                    continue
                if str(stock.get("status") or "").lower() != "active":
                    continue
                ticker = pad_ticker(str(stock.get("ticker") or ""))
                if ticker.strip():
                    tickers.append(ticker)
    unique_tickers = list(dict.fromkeys(tickers))
    limit = safe_int(max_tickers, default=30, min_value=1)
    resolved = unique_tickers[:limit]
    return {
        "source": "universe_config.yaml",
        "universe_config_path": _repo_relative(path, root),
        "universe_config_hash": _sha256_file(path),
        "expected_count": safe_int(payload.get("active_stock_count"), default=0, min_value=0),
        "resolved_tickers": resolved,
        "resolved_ticker_count": len(resolved),
        "max_tickers": limit,
    }


def production_registry_state(*, repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or _REPO_ROOT
    path = root / "artifacts" / "lgbm" / "registry.json"
    if not path.exists():
        return {
            "status": "MISSING",
            "path": _repo_relative(path, root),
            "active_version": None,
            "registry_mutated": False,
        }
    payload = _load_json(path)
    return {
        "status": "PASS",
        "path": _repo_relative(path, root),
        "active_version": payload.get("active_version"),
        "version_count": len(payload.get("versions") or []),
        "registry_mutated": payload.get("active_version") is not None,
    }


def latest_recommendation_cache_state(
    *,
    repo_root: Path | None = None,
    bundle_id: str,
) -> dict[str, Any]:
    root = repo_root or _REPO_ROOT
    report_dir = root / "artifacts" / "reports" / "recommendation_refresh"
    candidates = sorted(
        report_dir.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = _load_json(path)
        except Exception as e:
            _ = e
            continue
        if str(payload.get("bundle_id") or "") != str(bundle_id):
            continue
        recommendations = payload.get("recommendations")
        count = len(recommendations) if isinstance(recommendations, list) else 0
        diagnostics = payload.get("diagnostics_json")
        return {
            "status": payload.get("status", "UNKNOWN"),
            "report_path": _repo_relative(path, root),
            "generated_at": payload.get("generated_at"),
            "asof": payload.get("asof"),
            "recommendation_count": count,
            "cache_status": "DISPLAYABLE" if count > 0 else "EMPTY",
            "advisory_only": True,
            "diagnostics_present": bool(diagnostics),
        }
    return {
        "status": "MISSING",
        "report_path": None,
        "generated_at": None,
        "recommendation_count": 0,
        "cache_status": "MISSING",
        "advisory_only": True,
    }


def _normalize_requested_tickers(tickers_arg: str) -> tuple[list[str], list[str]]:
    requested: list[str] = []
    invalid: list[str] = []
    for raw_item in str(tickers_arg or "").split(","):
        raw = raw_item.strip()
        if not raw:
            continue
        ticker = pad_ticker(raw)
        if not is_valid_ticker(ticker):
            invalid.append(raw)
            continue
        requested.append(ticker)
    return list(dict.fromkeys(requested)), invalid


def _max_selected_tickers(schedule: dict[str, Any]) -> int:
    policy = schedule.get("execution_policy")
    if not isinstance(policy, dict):
        return 10
    return safe_int(policy.get("max_selected_tickers"), default=10, min_value=1)


def _execution_target(
    *,
    mode: str,
    schedule: dict[str, Any],
    universe: dict[str, Any],
    requested_tickers: list[str],
    invalid_tickers: list[str],
    tickers_arg: str,
) -> dict[str, Any]:
    mode_norm = str(mode or "").strip().lower()
    active_tickers = [
        str(ticker)
        for ticker in universe.get("resolved_tickers", [])
        if str(ticker).strip()
    ]
    active_set = set(active_tickers)
    blockers: list[str] = []
    warnings: list[str] = []

    if invalid_tickers:
        blockers.append("selected_ticker_invalid_format")

    if mode_norm in _SELECTED_TICKER_MODES:
        target_scope = "selected_tickers"
        execution_tickers = requested_tickers
        if not requested_tickers:
            blockers.append("selected_tickers_required")
        max_selected = _max_selected_tickers(schedule)
        if len(requested_tickers) > max_selected:
            blockers.append("selected_ticker_count_exceeds_limit")
        outside_active = sorted(
            ticker for ticker in requested_tickers if ticker not in active_set
        )
        if outside_active:
            blockers.append("selected_ticker_not_active_universe")
        description = (
            "User-selected recommendation tickers only. Recommendation ranking may "
            "be produced from active 30, but paper-auto order decisions are scoped "
            "to selected_tickers."
        )
    else:
        target_scope = "active_universe_30t"
        execution_tickers = active_tickers
        if requested_tickers:
            blockers.append("explicit_tickers_not_allowed_for_30t_paper_service")
        if mode_norm not in _ACTIVE_UNIVERSE_MODES:
            warnings.append("unknown_mode_defaulted_to_active_universe_30t")
        description = (
            "System/default 30-stock paper service. Empty tickers means active "
            "universe up to max_tickers."
        )

    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "target_scope": target_scope,
        "description": description,
        "mode": mode,
        "requested_tickers": requested_tickers,
        "invalid_tickers": invalid_tickers,
        "requested_tickers_arg": tickers_arg,
        "execution_tickers": execution_tickers,
        "execution_ticker_count": len(execution_tickers),
        "active_universe_ticker_count": len(active_tickers),
        "blockers": blockers,
        "warnings": warnings,
        "paper_auto_cli_hint": {
            "tickers_arg": ",".join(execution_tickers)
            if target_scope == "selected_tickers"
            else "",
            "max_tickers": universe.get("max_tickers"),
        },
        "be_start_rpc_contract": {
            "selected_service": "send selected recommendation tickers explicitly",
            "empty_tickers": "AI fallback to active universe, system/default only",
        },
    }


def _safety_state(
    *,
    production_registry: dict[str, Any],
    service_readiness: dict[str, Any],
    schedule: dict[str, Any],
    no_live: bool,
) -> dict[str, Any]:
    blockers: list[str] = []
    schedule_safety = schedule.get("safety") if isinstance(schedule.get("safety"), dict) else {}
    if not no_live:
        blockers.append("no_live_flag_required")
    if production_registry.get("active_version") is not None:
        blockers.append("production_active_version_not_null")
    if bool(production_registry.get("registry_mutated")):
        blockers.append("production_registry_mutated")
    if bool(service_readiness.get("live_trading_allowed")):
        blockers.append("live_trading_allowed_true")
    if safe_bool(schedule_safety.get("live_trading_allowed"), default=False):
        blockers.append("schedule_live_trading_allowed_true")
    if safe_bool(schedule_safety.get("allow_real_order"), default=False):
        blockers.append("schedule_allow_real_order_true")
    if safe_bool(schedule_safety.get("allow_live_order"), default=False):
        blockers.append("schedule_allow_live_order_true")
    if not safe_bool(schedule_safety.get("require_kis_virtual"), default=True):
        blockers.append("schedule_require_kis_virtual_false")
    if safe_bool(schedule_safety.get("registry_mutated"), default=False):
        blockers.append("schedule_registry_mutated_true")
    if schedule_safety.get("production_active_version") is not None:
        blockers.append("schedule_production_active_version_not_null")
    if safe_bool(schedule_safety.get("safe_to_enable_live_actions"), default=False):
        blockers.append("schedule_safe_to_enable_live_actions_true")
    contract = service_readiness.get("be_contract") or {}
    if isinstance(contract, dict) and bool(contract.get("safe_to_enable_live_actions")):
        blockers.append("safe_to_enable_live_actions_true")
    if bool(service_readiness.get("registry_mutated")):
        blockers.append("service_readiness_registry_mutated_true")
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "blockers": blockers,
        "active_version": production_registry.get("active_version"),
        "live_trading_allowed": bool(service_readiness.get("live_trading_allowed"))
        or safe_bool(schedule_safety.get("live_trading_allowed"), default=False),
        "registry_mutated": bool(service_readiness.get("registry_mutated"))
        or bool(production_registry.get("registry_mutated"))
        or safe_bool(schedule_safety.get("registry_mutated"), default=False),
        "real_order_enabled": safe_bool(schedule_safety.get("allow_real_order"), default=False),
        "live_order_enabled": safe_bool(schedule_safety.get("allow_live_order"), default=False),
        "require_kis_virtual": safe_bool(schedule_safety.get("require_kis_virtual"), default=True),
        "safe_to_enable_live_actions": safe_bool(
            schedule_safety.get("safe_to_enable_live_actions"),
            default=False,
        )
        or (
            safe_bool(contract.get("safe_to_enable_live_actions"), default=False)
            if isinstance(contract, dict)
            else False
        ),
    }


def build_paper_service_bundle_report(
    *,
    repo_root: Path | None = None,
    bundle_id: str,
    mode: str = "selected-paper-service",
    max_tickers: int = 30,
    tickers_arg: str = "",
    be_base_url: str | None = None,
    no_live: bool = True,
    allow_readiness_partial: bool = False,
) -> dict[str, Any]:
    root = repo_root or _REPO_ROOT
    schedule = load_schedule(repo_root=root)
    mode = str(mode or schedule.get("mode") or "selected-paper-service")
    universe = resolve_active_universe(repo_root=root, max_tickers=max_tickers)
    requested_tickers, invalid_tickers = _normalize_requested_tickers(tickers_arg)
    execution_target = _execution_target(
        mode=mode,
        schedule=schedule,
        universe=universe,
        requested_tickers=requested_tickers,
        invalid_tickers=invalid_tickers,
        tickers_arg=tickers_arg,
    )
    registry_dir = root / "artifacts" / "lgbm_paper_candidate" / bundle_id
    registry = validate_paper_candidate_registry(
        repo_root=root,
        bundle_id=bundle_id,
        registry_dir=registry_dir,
    )
    readiness = build_service_status(bundle_id=bundle_id, root=root)
    production_registry = production_registry_state(repo_root=root)
    safety = _safety_state(
        production_registry=production_registry,
        service_readiness=readiness,
        schedule=schedule,
        no_live=no_live,
    )
    recommendations = latest_recommendation_cache_state(
        repo_root=root,
        bundle_id=bundle_id,
    )

    blockers: list[str] = []
    warnings: list[str] = []
    blockers.extend(str(item) for item in execution_target.get("blockers", []))
    warnings.extend(str(item) for item in execution_target.get("warnings", []))
    expected_count = safe_int(
        ((schedule.get("universe") or {}) if isinstance(schedule.get("universe"), dict) else {}).get(
            "expected_count"
        ),
        default=30,
        min_value=1,
    )
    if universe.get("resolved_ticker_count") != expected_count:
        blockers.append("resolved_universe_count_mismatch")
    if registry.get("status") != "PASS":
        blockers.append("paper_candidate_registry_blocked")
    readiness_status = readiness.get("ssot_readiness") or readiness.get("status")
    if readiness_status != "PASS":
        if allow_readiness_partial:
            warnings.append("service_readiness_not_pass_demo_allowed")
        else:
            blockers.append("service_readiness_not_pass")
    if safety.get("status") != "PASS":
        blockers.extend(str(item) for item in safety.get("blockers", []))

    status = "PASS" if not blockers else "BLOCKED"
    generated_at = datetime.now(_KST).isoformat()
    return {
        "schema_version": "1.0.0",
        "status": status,
        "mode": mode,
        "bundle_id": bundle_id,
        "generated_at": generated_at,
        "blockers": blockers,
        "warnings": warnings,
        "schedule": {
            "mode": schedule.get("mode"),
            "timezone": schedule.get("timezone"),
            "path": schedule.get("_schedule_path"),
            "hash": schedule.get("_schedule_hash"),
            "stage_groups": [
                key
                for key in ("preopen", "market", "postmarket", "mode_b", "closeout")
                if isinstance(schedule.get(key), list)
            ],
        },
        "universe": {
            **universe,
            "requested_tickers": requested_tickers,
            "invalid_tickers": invalid_tickers,
            "requested_tickers_arg": tickers_arg,
        },
        "execution_target": execution_target,
        "bundle_validation": registry,
        "readiness": {
            "status": readiness_status,
            "safe_to_enable_order_actions": (
                (readiness.get("be_contract") or {}).get("safe_to_enable_order_actions")
                if isinstance(readiness.get("be_contract"), dict)
                else None
            ),
            "safe_to_enable_live_actions": (
                (readiness.get("be_contract") or {}).get("safe_to_enable_live_actions")
                if isinstance(readiness.get("be_contract"), dict)
                else None
            ),
            "live_trading_allowed": readiness.get("live_trading_allowed"),
            "registry_mutated": readiness.get("registry_mutated"),
            "broker_evidence": readiness.get("broker_evidence"),
            "deploy_quality": readiness.get("deploy_quality"),
        },
        "recommendations": recommendations,
        "safety": safety,
        "be_runtime_hint": {
            "AI_PAPER_BUNDLE_ID": bundle_id,
            "AI_RECOMMENDATION_BUNDLE_ID": bundle_id,
            "AUTO_TRADING_OPERATION_ENABLED": "true",
            "AUTO_TRADING_OPERATION_DRY_RUN": "false",
            "PAPER_SERVICE_MODE": mode,
            "PAPER_SERVICE_TARGET_SCOPE": execution_target.get("target_scope"),
            "AI_PAPER_SELECTED_TICKERS": ",".join(
                execution_target.get("execution_tickers", [])
            )
            if execution_target.get("target_scope") == "selected_tickers"
            else "",
            "AI_SERVER_HOST": "localhost",
            "AI_SERVER_PORT": "50051",
            "be_base_url": be_base_url,
            "note": "Hints only; do not write secrets or .env values into reports.",
        },
    }


def write_report(
    report: dict[str, Any],
    *,
    repo_root: Path | None = None,
    output_dir: Path | None = None,
) -> tuple[Path, Path]:
    root = repo_root or _REPO_ROOT
    report_dir = output_dir or root / _DEFAULT_REPORT_DIR
    if not report_dir.is_absolute():
        report_dir = root / report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    bundle_id = str(report.get("bundle_id") or "BUNDLE-UNKNOWN")
    json_path = report_dir / f"paper_service_bundle_{bundle_id}_{ts}.json"
    md_path = report_dir / f"paper_service_bundle_{bundle_id}_{ts}.md"
    report["report_path"] = str(json_path)
    report["report_path_relative"] = _repo_relative(json_path, root)
    report["markdown_report_path_relative"] = _repo_relative(md_path, root)
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    md_path.write_text(_markdown_summary(report), encoding="utf-8")
    return json_path, md_path


def _markdown_summary(report: dict[str, Any]) -> str:
    safety = report.get("safety") if isinstance(report.get("safety"), dict) else {}
    universe = report.get("universe") if isinstance(report.get("universe"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    execution_target = (
        report.get("execution_target")
        if isinstance(report.get("execution_target"), dict)
        else {}
    )
    recommendations = (
        report.get("recommendations")
        if isinstance(report.get("recommendations"), dict)
        else {}
    )
    blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    return "\n".join(
        [
            f"# Paper Service Bundle {report.get('bundle_id')}",
            "",
            f"- status: `{report.get('status')}`",
            f"- mode: `{report.get('mode')}`",
            f"- generated_at: `{report.get('generated_at')}`",
            f"- resolved_ticker_count: `{universe.get('resolved_ticker_count')}`",
            f"- execution_target: `{execution_target.get('target_scope')}` ({execution_target.get('execution_ticker_count')} tickers)",
            f"- readiness: `{readiness.get('status')}`",
            f"- recommendation_cache: `{recommendations.get('cache_status')}` ({recommendations.get('recommendation_count')} items)",
            f"- live_trading_allowed: `{safety.get('live_trading_allowed')}`",
            f"- registry_mutated: `{safety.get('registry_mutated')}`",
            f"- active_version: `{safety.get('active_version')}`",
            "",
            "## Blockers",
            "",
            *(f"- {item}" for item in blockers),
            *(["- none"] if not blockers else []),
            "",
            "## Warnings",
            "",
            *(f"- {item}" for item in warnings),
            *(["- none"] if not warnings else []),
            "",
        ]
    )
