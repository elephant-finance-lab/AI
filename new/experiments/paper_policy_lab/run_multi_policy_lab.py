#!/usr/bin/env python
"""Run personal paper/shadow execution policy lanes.

The harness is tracked in the personal experiment branch, while per-run JSON
reports and logs stay under the ignored runs/ directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import types
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

KST = ZoneInfo("Asia/Seoul")
SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_REPO_ROOT = SCRIPT_PATH.parents[3]
DEFAULT_CONFIG = SCRIPT_PATH.with_name("policies_5track.yaml")
RUNS_DIR = SCRIPT_PATH.with_name("runs")
STABLE_PAPER_BUNDLE_ID = "BUNDLE-20260521-POSTCLOSE"
STABLE_PAPER_REGISTRY_REL = Path(
    "artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE"
)

KIS_TARGET_ENV_KEYS = [
    "KIS_MODE",
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NUMBER",
    "KIS_ACCOUNT_PRODUCT_CODE",
    "KIS_PAPER_APP_KEY",
    "KIS_PAPER_APP_SECRET",
    "KIS_PAPER_ACCOUNT_NUMBER",
    "KIS_PAPER_ACCOUNT_PRODUCT_CODE",
]

RUNTIME_OVERRIDE_KEYS = {
    "max_orders_per_cycle",
    "max_order_qty_per_order",
    "allow_market_order",
    "ppo_max_names",
    "ppo_min_cash",
    "ppo_min_confidence",
    "trade_probability_gate_enabled",
    "min_trade_probability",
    "ppo_weighting",
}

PPO_WEIGHTING_VALUES = {"score", "equal"}

RUN_DEFAULT_KEYS = {
    "python",
    "repo_root",
    "bundle_id",
    "registry_dir",
    "tickers",
    "cycles",
    "interval_sec",
    "end_date",
    "business_days",
    "confirm_phrase",
    "prelive_required",
    "max_tickers",
    "max_parallel",
}

SERVICE_REPLAY_ONLY_KEYS = {
    "top_k_fraction",
    "decision_stride_bars",
    "min_holding_bars",
    "rebalance_cooldown_bars",
    "no_trade_score_spread",
    "min_cash",
}

_TRUE_VALUES = {"true", "yes", "y", "1", "approve", "approved", "승인"}
_FALSE_VALUES = {"false", "no", "n", "0", "veto", "reject", "rejected", "거부", "없음", "아님"}


def _safety_flags(*, external_kis_api: bool = False) -> dict[str, bool | str]:
    return {
        "scope": "personal_research_policy_lab",
        "research_only": True,
        "external_kis_api": external_kis_api,
        "production_registry_mutated": False,
        "paper_registry_mutated": False,
        "live_trading_allowed": False,
        "deploy_quality": False,
    }


def _model_positioning(bundle_id: str) -> dict[str, Any]:
    is_stable_baseline = bundle_id == STABLE_PAPER_BUNDLE_ID
    return {
        "bundle_id": bundle_id,
        "role": "stable_paper_baseline" if is_stable_baseline else "unsupported_non_baseline",
        "global_optimum_claim_allowed": False,
        "production_deploy_candidate": False,
        "research_benchmark_anchor": is_stable_baseline,
        "fixed_for_5track_lab": is_stable_baseline,
        "paper_default_change_allowed": False,
        "paper_default_change_requirements": [
            "C12 real backtest PASS",
            "deploy_candidate dry-run PASS",
            "service_readiness_status PASS",
            "prelive_gate PASS",
            "paper broker evidence PASS",
        ],
        "allowed_claim": (
            "현재 paper 운영을 시작하기에 evidence chain이 가장 완성된 "
            "stable paper baseline이자 research benchmark anchor"
        ),
        "forbidden_claims": [
            "전역 최적 모델",
            "production deploy 후보",
            "C12 없이 paper default로 승격 가능한 research 모델",
            "5-track의 5개 모델 중 하나",
        ],
    }


@dataclass(frozen=True)
class PolicySpec:
    policy_id: str
    label: str
    mode: str
    profile_prefix: str
    description: str
    launch_delay_sec: float
    overrides: dict[str, Any]


def _json_dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {path}")
    return data


def _safe_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    return default


def _require_bool_like(policy_id: str, key: str, value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and value in {0, 1}:
        return
    if isinstance(value, str) and value.strip().lower() in (_TRUE_VALUES | _FALSE_VALUES):
        return
    raise ValueError(f"{policy_id} contains unsupported boolean value for {key}: {value}")


def _require_int_min(policy_id: str, key: str, value: Any, *, min_value: int) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{policy_id} contains invalid integer value for {key}: {value}")
    try:
        parsed = float(str(value).strip())
    except Exception as e:
        raise ValueError(f"{policy_id} contains invalid integer value for {key}: {value}") from e
    if not parsed.is_integer() or int(parsed) < min_value:
        raise ValueError(
            f"{policy_id} requires {key} to be an integer >= {min_value}: {value}"
        )


def _require_float_range(
    policy_id: str,
    key: str,
    value: Any,
    *,
    min_value: float,
    max_value: float,
    max_inclusive: bool = True,
) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{policy_id} contains invalid float value for {key}: {value}")
    try:
        parsed = float(str(value).strip())
    except Exception as e:
        raise ValueError(f"{policy_id} contains invalid float value for {key}: {value}") from e
    upper_ok = parsed <= max_value if max_inclusive else parsed < max_value
    if parsed < min_value or not upper_ok:
        op = "<=" if max_inclusive else "<"
        raise ValueError(
            f"{policy_id} requires {min_value} <= {key} {op} {max_value}: {value}"
        )


def _validate_config(config: dict[str, Any]) -> None:
    defaults = config.get("run_defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("run_defaults must be a mapping")
    _validate_run_defaults(defaults)
    _load_policies(config)


def _validate_run_defaults(defaults: dict[str, Any]) -> None:
    keys = set(defaults)
    replay_only = sorted(keys & SERVICE_REPLAY_ONLY_KEYS)
    if replay_only:
        raise ValueError(
            "run_defaults contains service-policy replay-only keys that "
            f"the paper runtime does not enforce: {replay_only}"
        )
    unsupported = sorted(keys - RUN_DEFAULT_KEYS)
    if unsupported:
        raise ValueError(f"run_defaults contains unsupported keys: {unsupported}")


def _load_policies(config: dict[str, Any]) -> list[PolicySpec]:
    raw_policies = config.get("policies") or []
    if not isinstance(raw_policies, list):
        raise ValueError("policies must be a list")
    policies: list[PolicySpec] = []
    for raw in raw_policies:
        if not isinstance(raw, dict):
            raise ValueError(f"policy entries must be mappings, got {type(raw).__name__}")
        policy_id = str(raw.get("id") or "").strip()
        if not policy_id:
            raise ValueError("policy id is required")
        mode = str(raw.get("mode") or "shadow").strip().lower()
        if mode not in {"paper", "shadow"}:
            raise ValueError(f"unsupported policy mode for {policy_id}: {mode}")
        launch_delay_sec = _parse_nonnegative_float(
            policy_id,
            "launch_delay_sec",
            raw.get("launch_delay_sec", 0),
        )
        overrides = dict(raw.get("overrides") or {})
        _validate_override_keys(policy_id, overrides)
        _validate_override_values(policy_id, overrides)
        policies.append(
            PolicySpec(
                policy_id=policy_id,
                label=str(raw.get("label") or policy_id),
                mode=mode,
                profile_prefix=str(raw.get("profile_prefix") or "KIS_MAIN"),
                description=str(raw.get("description") or ""),
                launch_delay_sec=launch_delay_sec,
                overrides=overrides,
            )
        )
    if not policies:
        raise ValueError("at least one policy is required")
    return policies


def _validate_override_keys(policy_id: str, overrides: dict[str, Any]) -> None:
    keys = set(overrides)
    replay_only = sorted(keys & SERVICE_REPLAY_ONLY_KEYS)
    if replay_only:
        raise ValueError(
            f"{policy_id} contains service-policy replay-only overrides that "
            f"the paper runtime does not enforce: {replay_only}"
        )
    unsupported = sorted(keys - RUNTIME_OVERRIDE_KEYS)
    if unsupported:
        raise ValueError(
            f"{policy_id} contains unsupported paper runtime overrides: {unsupported}"
        )
    if "ppo_weighting" in overrides:
        weighting = str(overrides["ppo_weighting"]).strip().lower()
        if weighting not in PPO_WEIGHTING_VALUES:
            raise ValueError(
                f"{policy_id} contains unsupported ppo_weighting: {weighting}"
            )


def _parse_nonnegative_float(policy_id: str, key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{policy_id} contains invalid nonnegative float for {key}: {value}")
    try:
        parsed = float(str(value).strip())
    except Exception as e:
        raise ValueError(
            f"{policy_id} contains invalid nonnegative float for {key}: {value}"
        ) from e
    if parsed < 0:
        raise ValueError(f"{policy_id} requires {key} >= 0: {value}")
    return parsed


def _validate_override_values(policy_id: str, overrides: dict[str, Any]) -> None:
    int_min_keys = {
        "max_orders_per_cycle": 1,
        "max_order_qty_per_order": 1,
        "ppo_max_names": 1,
    }
    for key, min_value in int_min_keys.items():
        if key in overrides:
            _require_int_min(policy_id, key, overrides[key], min_value=min_value)
    bounded_keys = {
        "ppo_min_cash": (0.0, 1.0, False),
        "ppo_min_confidence": (0.0, 1.0, True),
        "min_trade_probability": (0.0, 1.0, True),
    }
    for key, (min_value, max_value, max_inclusive) in bounded_keys.items():
        if key in overrides:
            _require_float_range(
                policy_id,
                key,
                overrides[key],
                min_value=min_value,
                max_value=max_value,
                max_inclusive=max_inclusive,
            )
    for key in ("allow_market_order", "trade_probability_gate_enabled"):
        if key in overrides:
            _require_bool_like(policy_id, key, overrides[key])


def _repo_root(defaults: dict[str, Any]) -> Path:
    raw = str(defaults.get("repo_root") or DEFAULT_REPO_ROOT)
    return Path(raw).expanduser().resolve()


def _resolve_registry_dir(repo_root: Path, raw: str | None) -> Path | None:
    if raw is None or not str(raw).strip():
        raise ValueError(
            "personal policy lab requires an explicit non-production registry_dir"
        )
    reg_path = Path(str(raw)).expanduser()
    if not reg_path.is_absolute():
        reg_path = repo_root / reg_path
    resolved = reg_path.resolve()
    production_registry = (repo_root / "artifacts" / "lgbm").resolve()
    if resolved == production_registry or production_registry in resolved.parents:
        raise ValueError(
            "personal policy lab cannot use production registry artifacts/lgbm"
        )
    return resolved


def _stable_paper_registry_dir(repo_root: Path) -> Path:
    return (repo_root / STABLE_PAPER_REGISTRY_REL).resolve()


def _require_fixed_stable_baseline(
    *,
    repo_root: Path,
    bundle_id: str,
    registry_dir: Path | None,
) -> None:
    if bundle_id != STABLE_PAPER_BUNDLE_ID:
        raise ValueError(
            "5-track paper lab is fixed to stable baseline "
            f"{STABLE_PAPER_BUNDLE_ID}; requested bundle_id={bundle_id}"
        )
    expected = _stable_paper_registry_dir(repo_root)
    if registry_dir is None or registry_dir.resolve() != expected:
        raise ValueError(
            "5-track paper lab is fixed to registry_dir "
            f"{STABLE_PAPER_REGISTRY_REL}; requested registry_dir={registry_dir}"
        )


def _as_list_tickers(raw: str | list[Any]) -> list[str]:
    if isinstance(raw, list):
        return [str(x).zfill(6) for x in raw if str(x).strip()]
    return [part.strip().zfill(6) for part in str(raw).split(",") if part.strip()]


def _run_id(raw: str | None) -> str:
    if raw:
        return raw
    return datetime.now(KST).strftime("%Y%m%d_%H%M%S")


def _readiness_from_env(env: dict[str, str]) -> dict[str, bool]:
    keys = [
        "KIS_MODE",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCOUNT_NUMBER",
        "KIS_ACCOUNT_PRODUCT_CODE",
        "KIS_PAPER_APP_KEY",
        "KIS_PAPER_APP_SECRET",
        "KIS_PAPER_ACCOUNT_NUMBER",
        "KIS_PAPER_ACCOUNT_PRODUCT_CODE",
    ]
    return {key: bool(str(env.get(key) or "").strip()) for key in keys}


def _profile_source_presence(env: dict[str, str], prefix: str) -> dict[str, bool]:
    keys = [
        "MODE",
        "APP_KEY",
        "APP_SECRET",
        "ACCOUNT_NUMBER",
        "ACCOUNT_PRODUCT_CODE",
        "PAPER_APP_KEY",
        "PAPER_APP_SECRET",
        "PAPER_ACCOUNT_NUMBER",
        "PAPER_ACCOUNT_PRODUCT_CODE",
    ]
    return {f"{prefix}_{key}": bool(str(env.get(f"{prefix}_{key}") or "").strip()) for key in keys}


def _env_for_policy(base_env: dict[str, str], policy: PolicySpec, repo_root: Path) -> dict[str, str]:
    env = dict(base_env)
    prefix = policy.profile_prefix
    for key in KIS_TARGET_ENV_KEYS:
        env.pop(key, None)

    def copy_first(target: str, suffixes: list[str]) -> None:
        for suffix in suffixes:
            value = env.get(f"{prefix}_{suffix}")
            if value:
                env[target] = value
                return

    copy_first("KIS_MODE", ["MODE"])
    copy_first("KIS_APP_KEY", ["APP_KEY", "PAPER_APP_KEY"])
    copy_first("KIS_APP_SECRET", ["APP_SECRET", "PAPER_APP_SECRET"])
    copy_first("KIS_ACCOUNT_NUMBER", ["ACCOUNT_NUMBER", "PAPER_ACCOUNT_NUMBER"])
    copy_first("KIS_ACCOUNT_PRODUCT_CODE", ["ACCOUNT_PRODUCT_CODE", "PAPER_ACCOUNT_PRODUCT_CODE"])
    copy_first("KIS_PAPER_APP_KEY", ["PAPER_APP_KEY", "APP_KEY"])
    copy_first("KIS_PAPER_APP_SECRET", ["PAPER_APP_SECRET", "APP_SECRET"])
    copy_first("KIS_PAPER_ACCOUNT_NUMBER", ["PAPER_ACCOUNT_NUMBER", "ACCOUNT_NUMBER"])
    copy_first(
        "KIS_PAPER_ACCOUNT_PRODUCT_CODE",
        ["PAPER_ACCOUNT_PRODUCT_CODE", "ACCOUNT_PRODUCT_CODE"],
    )

    pythonpath_parts = [str(repo_root / "new")]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(str(env["PYTHONPATH"]))
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return env


def _policy_overview(policy: PolicySpec, env: dict[str, str]) -> dict[str, Any]:
    return {
        "policy_id": policy.policy_id,
        "label": policy.label,
        "mode": policy.mode,
        "profile_prefix": policy.profile_prefix,
        "description": policy.description,
        "launch_delay_sec": policy.launch_delay_sec,
        "policy_hash": _policy_hash(policy),
        "policy_hash_scope": "policy_id+mode+launch_delay_sec+runtime_overrides",
        "profile_source_presence": _profile_source_presence(env, policy.profile_prefix),
        "mapped_env_readiness": _readiness_from_env(env),
        "overrides": policy.overrides,
        "override_semantics": _override_semantics(policy),
        "account_fairness_requirements": _account_fairness_requirements(),
        "profile_guard": _profile_guard([policy], env),
    }


def _policy_hash(policy: PolicySpec) -> str:
    material = {
        "policy_id": policy.policy_id,
        "mode": policy.mode,
        "profile_prefix": policy.profile_prefix,
        "launch_delay_sec": policy.launch_delay_sec,
        "overrides": policy.overrides,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _run_config_hash(
    *,
    policies: list[PolicySpec],
    bundle_id: str,
    registry_dir: str,
    tickers: list[str],
    cycles: int,
    interval_sec: float,
    prelive_required: bool,
) -> str:
    material = {
        "bundle_id": bundle_id,
        "registry_dir": registry_dir,
        "tickers": tickers,
        "cycles": cycles,
        "interval_sec": interval_sec,
        "prelive_required": prelive_required,
        "policies": {
            policy.policy_id: {
                "policy_hash": _policy_hash(policy),
                "mode": policy.mode,
                "profile_prefix": policy.profile_prefix,
            }
            for policy in policies
        },
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _profile_identity_hash(
    *,
    mode: str,
    account_number: str,
    product_code: str,
) -> str:
    material = {
        "mode": mode.strip().lower(),
        "account_number": account_number.strip(),
        "product_code": product_code.strip(),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _profile_guard(policies: list[PolicySpec], env: dict[str, str]) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    profiles: dict[str, dict[str, Any]] = {}
    seen: dict[str, str] = {}
    for policy in policies:
        prefix = policy.profile_prefix
        mode = str(env.get(f"{prefix}_MODE") or "").strip().lower()
        account = str(
            env.get(f"{prefix}_ACCOUNT_NUMBER")
            or env.get(f"{prefix}_PAPER_ACCOUNT_NUMBER")
            or ""
        ).strip()
        product = str(
            env.get(f"{prefix}_ACCOUNT_PRODUCT_CODE")
            or env.get(f"{prefix}_PAPER_ACCOUNT_PRODUCT_CODE")
            or ""
        ).strip()
        has_key = bool(
            str(env.get(f"{prefix}_APP_KEY") or env.get(f"{prefix}_PAPER_APP_KEY") or "").strip()
        )
        has_secret = bool(
            str(env.get(f"{prefix}_APP_SECRET") or env.get(f"{prefix}_PAPER_APP_SECRET") or "").strip()
        )
        identity_hash = (
            _profile_identity_hash(mode=mode, account_number=account, product_code=product)
            if mode and account and product
            else ""
        )
        profiles[policy.policy_id] = {
            "profile_prefix": prefix,
            "mode": mode or None,
            "has_account_number": bool(account),
            "has_account_product_code": bool(product),
            "has_app_key": has_key,
            "has_app_secret": has_secret,
            "account_identity_hash": identity_hash,
        }
        missing = []
        if mode != "virtual":
            missing.append("virtual_mode")
        if not account:
            missing.append("account_number")
        if not product:
            missing.append("account_product_code")
        if not has_key:
            missing.append("app_key")
        if not has_secret:
            missing.append("app_secret")
        if missing:
            blockers.append(
                {
                    "policy_id": policy.policy_id,
                    "reason": "profile_not_ready",
                    "missing_or_invalid": missing,
                    "profile_prefix": prefix,
                }
            )
            continue
        if identity_hash in seen:
            blockers.append(
                {
                    "policy_id": policy.policy_id,
                    "reason": "duplicate_account_identity",
                    "profile_prefix": prefix,
                    "duplicates_policy_id": seen[identity_hash],
                    "account_identity_hash": identity_hash,
                }
            )
        else:
            seen[identity_hash] = policy.policy_id
    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "profiles": profiles,
        "blockers": blockers,
        "secrets_printed": False,
    }


def _account_fairness_requirements() -> dict[str, Any]:
    return {
        "separate_kis_profile_required": True,
        "same_account_shared_across_paper_tracks_allowed": False,
        "starting_nav_snapshot_required": True,
        "starting_holdings_snapshot_required": True,
        "pending_orders_must_be_empty_or_documented": True,
        "comparison_basis": "normalized_nav_bps",
    }


def _evidence_schema() -> dict[str, Any]:
    return {
        "required_run_fields": [
            "track_id",
            "policy_id",
            "mode",
            "bundle_id",
            "policy_hash",
            "run_config_hash",
            "cycles_completed",
            "serving_feature_readiness",
            "score_nonempty_cycles",
            "rankable_score_cycles",
            "order_delta_count",
            "submitted_count",
            "fill_count",
            "reject_count",
            "fail_closed_count",
            "turnover_bps",
            "daily_return_bps",
        ],
        "comparison_basis": "normalized_nav_bps",
        "failure_case_card_required": True,
        "pure_return_ranking_requires_time_alignment": True,
    }


def _prelive_skip_blocker() -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "reason": "prelive_skip_forbidden_for_runtime",
        "message": (
            "--skip-prelive is dry-run only. 5-track paper/shadow runtime must "
            "keep the prelive/service-readiness gate in the evidence chain."
        ),
    }


def _override_semantics(policy: PolicySpec) -> dict[str, Any]:
    return {
        "runtime_enforced_override_keys": sorted(policy.overrides),
        "supported_runtime_override_keys": sorted(RUNTIME_OVERRIDE_KEYS),
        "service_policy_replay_only_keys_rejected": sorted(SERVICE_REPLAY_ONLY_KEYS),
        "ignored_override_keys": [],
        "note": (
            "This personal paper lab enforces only runtime/PPO/order-cap keys. "
            "Use ppo_min_cash for PPO allocation reserve; min_cash is a "
            "service-replay cash guard and is rejected here. "
            "Replay-only knobs are rejected instead of being silently ignored."
        ),
    }


def _run_semantics(tickers: list[str]) -> dict[str, Any]:
    ticker_count = len(tickers)
    return {
        "runtime_scope": "paper_or_shadow_runtime",
        "service_policy_replay_equivalence": False,
        "ticker_count": ticker_count,
        "universe_note": (
            "The weekend service-policy replay was evaluated on the 30-stock "
            "research universe. A smaller paper ticker subset is operational "
            "evidence, not a one-to-one replay reproduction."
        ),
        "runtime_enforced_fields": sorted(RUNTIME_OVERRIDE_KEYS),
        "replay_only_fields_not_enforced_here": sorted(SERVICE_REPLAY_ONLY_KEYS),
        "account_fairness_requirements": _account_fairness_requirements(),
        "evidence_schema": _evidence_schema(),
        "operational_comparison_allowed": True,
        "pure_return_ranking_allowed": False,
        "pure_return_ranking_note": (
            "Policy lab uses staggered launch offsets to reduce KIS API collisions. "
            "Use normalized NAV bps with caveats, not pure same-second alpha ranking."
        ),
    }


def _score_stats(scores: Any) -> dict[str, Any]:
    if not isinstance(scores, dict):
        return {
            "score_count": 0,
            "finite_score_count": 0,
            "nonzero_score_count": 0,
            "score_abs_sum": 0.0,
            "score_std": 0.0,
            "rankable": False,
        }
    values: list[float] = []
    for value in scores.values():
        try:
            parsed = float(value)
        except Exception:
            continue
        if math.isfinite(parsed):
            values.append(parsed)
    nonzero = [v for v in values if abs(v) > 1e-12]
    if len(values) > 1:
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = math.sqrt(variance)
    else:
        std = 0.0
    return {
        "score_count": len(scores),
        "finite_score_count": len(values),
        "nonzero_score_count": len(nonzero),
        "score_abs_sum": float(sum(abs(v) for v in values)),
        "score_std": float(std),
        "rankable": bool(len(values) == 1 and nonzero) or bool(len(values) > 1 and std > 1e-12),
    }


def _summarize_native_paper_report(
    report: dict[str, Any],
    *,
    policy: PolicySpec,
    bundle_id: str,
    run_config_hash: str,
) -> dict[str, Any]:
    cycles_stage = (report.get("stages") or {}).get("cycles")
    items = cycles_stage.get("items") if isinstance(cycles_stage, dict) else []
    if not isinstance(items, list):
        items = []
    serving_feature_readiness = (report.get("stages") or {}).get("serving_feature_readiness")
    if not isinstance(serving_feature_readiness, dict):
        serving_feature_readiness = {}

    score_nonempty = 0
    rankable = 0
    blocked = 0
    order_delta_count = 0
    submitted = 0
    fills = 0
    rejects = 0
    matched = 0
    fail_closed = 0
    max_turnover = 0.0
    for cycle in items:
        if not isinstance(cycle, dict):
            continue
        if cycle.get("status") == "FAIL":
            fail_closed += 1
        hot_result = cycle.get("hot_result")
        if not isinstance(hot_result, dict):
            continue
        quant_output = hot_result.get("quant_output")
        if isinstance(quant_output, dict):
            stats = _score_stats(quant_output.get("scores"))
            if int(stats.get("finite_score_count") or 0) > 0:
                score_nonempty += 1
            if stats.get("rankable"):
                rankable += 1
            if quant_output.get("mode") == "blocked" or quant_output.get("blocker"):
                blocked += 1
        final_decision = hot_result.get("final_decision")
        if isinstance(final_decision, dict):
            deltas = final_decision.get("order_deltas")
            if isinstance(deltas, list):
                order_delta_count += len(deltas)
        pm_result = hot_result.get("pm_result")
        if isinstance(pm_result, dict):
            try:
                max_turnover = max(max_turnover, float(pm_result.get("turnover") or 0.0))
            except Exception:
                pass
        execution = cycle.get("execution")
        if isinstance(execution, dict):
            execution_report = execution.get("execution_report")
            if isinstance(execution_report, dict):
                fills_list = execution_report.get("fills")
                rejects_list = execution_report.get("rejections")
                if isinstance(fills_list, list):
                    fills += len(fills_list)
                    submitted += len(fills_list)
                if isinstance(rejects_list, list):
                    rejects += len(rejects_list)
                    submitted += len(rejects_list)
        history = cycle.get("order_history_verification")
        if isinstance(history, dict):
            queries = history.get("queries")
            if isinstance(queries, list):
                for query in queries:
                    if isinstance(query, dict):
                        matched += int(query.get("matched_order_count") or 0)

    zero_order_false_pass = (
        report.get("status") == "PASS"
        and len(items) > 0
        and score_nonempty == 0
        and order_delta_count == 0
        and submitted == 0
    )
    return {
        "track_id": policy.policy_id,
        "policy_id": policy.policy_id,
        "mode": policy.mode,
        "bundle_id": bundle_id,
        "policy_hash": _policy_hash(policy),
        "run_config_hash": run_config_hash,
        "cycles_requested": ((report.get("params") or {}).get("cycles")),
        "cycles_completed": len(items),
        "serving_feature_readiness": serving_feature_readiness,
        "score_nonempty_cycles": score_nonempty,
        "blocked_quant_cycles": blocked,
        "rankable_score_cycles": rankable,
        "order_delta_count": order_delta_count,
        "submitted_count": submitted,
        "fill_count": fills,
        "reject_count": rejects,
        "matched_order_history_count": matched,
        "fail_closed_count": fail_closed,
        "turnover_bps": float(max_turnover) * 10000.0,
        "daily_return_bps": None,
        "zero_order_false_pass": zero_order_false_pass,
    }


def _summarize_shadow_result(
    *,
    policy: PolicySpec,
    bundle_id: str,
    run_config_hash: str,
    cycle_reports: list[dict[str, Any]],
    status: str,
    serving_feature_readiness: dict[str, Any],
) -> dict[str, Any]:
    score_nonempty = sum(1 for c in cycle_reports if int(c.get("finite_score_count") or 0) > 0)
    rankable = sum(1 for c in cycle_reports if c.get("rankable"))
    fail_closed = sum(1 for c in cycle_reports if c.get("status") == "FAIL")
    order_delta_count = sum(int(c.get("order_delta_count") or 0) for c in cycle_reports)
    return {
        "track_id": policy.policy_id,
        "policy_id": policy.policy_id,
        "mode": policy.mode,
        "bundle_id": bundle_id,
        "policy_hash": _policy_hash(policy),
        "run_config_hash": run_config_hash,
        "cycles_requested": len(cycle_reports),
        "cycles_completed": len(cycle_reports),
        "serving_feature_readiness": serving_feature_readiness,
        "score_nonempty_cycles": score_nonempty,
        "blocked_quant_cycles": sum(1 for c in cycle_reports if c.get("quant_mode") == "blocked"),
        "rankable_score_cycles": rankable,
        "order_delta_count": order_delta_count,
        "submitted_count": 0,
        "fill_count": 0,
        "reject_count": 0,
        "matched_order_history_count": 0,
        "fail_closed_count": fail_closed,
        "turnover_bps": 0.0,
        "daily_return_bps": None,
        "zero_order_false_pass": bool(
            status == "PASS"
            and cycle_reports
            and score_nonempty == 0
            and order_delta_count == 0
        ),
    }


def _apply_policy_overrides(trader: Any, policy: PolicySpec) -> dict[str, Any]:
    overrides = dict(policy.overrides)
    applied: dict[str, Any] = {}

    def set_attr(obj: Any, attr: str, key: str, caster: Any) -> None:
        if key not in overrides:
            return
        value = caster(overrides[key])
        setattr(obj, attr, value)
        applied[key] = value

    set_attr(trader, "_max_orders_per_cycle", "max_orders_per_cycle", int)
    set_attr(trader, "_max_order_qty_per_order", "max_order_qty_per_order", int)
    if "allow_market_order" in overrides:
        value = _safe_bool(overrides["allow_market_order"], default=False)
        setattr(trader, "_allow_market_order", value)
        applied["allow_market_order"] = value

    hot_runner = getattr(trader, "_hot_runner", None)
    ppo = getattr(hot_runner, "_ppo", None)
    if ppo is not None:
        set_attr(ppo, "_max_names", "ppo_max_names", int)
        set_attr(ppo, "_min_cash", "ppo_min_cash", float)
        set_attr(ppo, "_min_confidence", "ppo_min_confidence", float)
        set_attr(
            ppo,
            "_trade_probability_gate_enabled",
            "trade_probability_gate_enabled",
            lambda value: _safe_bool(value, default=False),
        )
        set_attr(ppo, "_min_trade_probability", "min_trade_probability", float)
        weighting = str(overrides.get("ppo_weighting") or "score").strip().lower()
        if weighting == "equal":
            ppo._compute_weights = types.MethodType(_equal_weight_compute_weights, ppo)
            applied["ppo_weighting"] = "equal"
        elif "ppo_weighting" in overrides:
            applied["ppo_weighting"] = "score"

    return applied


def _equal_weight_compute_weights(self: Any, top_k_items: list[tuple[str, float]]) -> dict[str, float]:
    if not top_k_items:
        return {}
    target_alloc = max(0.0, 1.0 - float(getattr(self, "_min_cash", 0.1)))
    weight = target_alloc / len(top_k_items)
    return {ticker: float(weight) for ticker, _score in top_k_items}


def _prepare_repo_imports(repo_root: Path, registry_dir: str | None) -> None:
    src = repo_root / "new"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    reg_path = _resolve_registry_dir(repo_root, registry_dir)
    if reg_path is not None:
        os.environ["ELEPHANT_LGBM_REGISTRY_DIR"] = str(reg_path)


def _prelive_report(
    repo_root: Path,
    defaults: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    import prelive_gate

    return prelive_gate.build_report(
        end_date=str(args.end_date or defaults.get("end_date")),
        business_days=int(args.business_days or defaults.get("business_days")),
        max_tickers=int(args.max_tickers or defaults.get("max_tickers", 30)),
        bundle_id=str(args.bundle_id or defaults.get("bundle_id")),
    )


def _new_trader(repo_root: Path, run_dir: Path, policy: PolicySpec, bundle_id: str) -> Any:
    from src.execution.paper_auto_trading import PaperAutoTrader

    report_dir = run_dir / "native_reports" / policy.policy_id
    trader = PaperAutoTrader(report_dir=report_dir, required_bundle_id=bundle_id)
    _apply_policy_overrides(trader, policy)
    return trader


def _run_paper_child(
    repo_root: Path,
    run_dir: Path,
    defaults: dict[str, Any],
    policy: PolicySpec,
    args: argparse.Namespace,
) -> dict[str, Any]:
    bundle_id = str(args.bundle_id or defaults.get("bundle_id"))
    run_config_hash = str(args.run_config_hash or "")
    trader = _new_trader(repo_root, run_dir, policy, bundle_id)
    applied = _apply_policy_overrides(trader, policy)
    tickers = _as_list_tickers(args.tickers or defaults.get("tickers", ""))
    cycles = int(args.cycles or defaults.get("cycles", 1))
    interval_sec = float(args.interval_sec if args.interval_sec is not None else defaults.get("interval_sec", 0))
    if not run_config_hash:
        run_config_hash = _run_config_hash(
            policies=[policy],
            bundle_id=bundle_id,
            registry_dir=str(args.registry_dir or defaults.get("registry_dir") or ""),
            tickers=tickers,
            cycles=cycles,
            interval_sec=interval_sec,
            prelive_required=not args.skip_prelive,
        )
    report = trader.run(
        tickers=tickers,
        cycles=cycles,
        interval_sec=interval_sec,
        confirm_phrase=str(args.confirm_phrase or defaults.get("confirm_phrase")),
        write_report=True,
    )
    evidence_summary = _summarize_native_paper_report(
        report,
        policy=policy,
        bundle_id=bundle_id,
        run_config_hash=run_config_hash,
    )
    return {
        "status": report.get("status"),
        "mode": "paper",
        "policy_id": policy.policy_id,
        "policy_label": policy.label,
        "policy_hash": _policy_hash(policy),
        "launch_delay_sec": policy.launch_delay_sec,
        "evidence_summary": evidence_summary,
        **_safety_flags(external_kis_api=True),
        "runtime_semantics": _run_semantics(tickers),
        "applied_overrides": applied,
        "native_report": report,
    }


def _run_shadow_child(
    repo_root: Path,
    run_dir: Path,
    defaults: dict[str, Any],
    policy: PolicySpec,
    args: argparse.Namespace,
) -> dict[str, Any]:
    bundle_id = str(args.bundle_id or defaults.get("bundle_id"))
    run_config_hash = str(args.run_config_hash or "")
    trader = _new_trader(repo_root, run_dir, policy, bundle_id)
    applied = _apply_policy_overrides(trader, policy)
    tickers = _as_list_tickers(args.tickers or defaults.get("tickers", ""))
    cycles = int(args.cycles or defaults.get("cycles", 1))
    interval_sec = float(args.interval_sec if args.interval_sec is not None else defaults.get("interval_sec", 0))
    confirm_phrase = str(args.confirm_phrase or defaults.get("confirm_phrase"))
    if not run_config_hash:
        run_config_hash = _run_config_hash(
            policies=[policy],
            bundle_id=bundle_id,
            registry_dir=str(args.registry_dir or defaults.get("registry_dir") or ""),
            tickers=tickers,
            cycles=cycles,
            interval_sec=interval_sec,
            prelive_required=not args.skip_prelive,
        )

    cycle_reports: list[dict[str, Any]] = []
    guards = {
        "start_guard": trader._start_guard(confirm_phrase),
        "market_session_guard": None,
        "mode_guard": None,
        "active_model_guard": None,
        "serving_feature_readiness": None,
    }
    if guards["start_guard"].get("status") == "PASS":
        guards["market_session_guard"] = trader._market_session_check()
    if (guards["market_session_guard"] or {}).get("status") == "PASS":
        guards["mode_guard"] = trader._paper_mode_check()
    if (guards["mode_guard"] or {}).get("status") == "PASS":
        guards["active_model_guard"] = trader._active_model_check()
    if (guards["active_model_guard"] or {}).get("status") == "PASS":
        guards["serving_feature_readiness"] = trader._serving_feature_readiness_check(tickers)

    guard_statuses = [guard.get("status") for guard in guards.values() if isinstance(guard, dict)]
    if any(status not in {"PASS", None} for status in guard_statuses):
        evidence_summary = _summarize_shadow_result(
            policy=policy,
            bundle_id=bundle_id,
            run_config_hash=run_config_hash,
            cycle_reports=[],
            status="BLOCKED",
            serving_feature_readiness=(
                guards["serving_feature_readiness"]
                if isinstance(guards.get("serving_feature_readiness"), dict)
                else {}
            ),
        )
        return {
            "status": "BLOCKED",
            "mode": "shadow",
            "policy_id": policy.policy_id,
            "policy_label": policy.label,
            "policy_hash": _policy_hash(policy),
            "launch_delay_sec": policy.launch_delay_sec,
            "evidence_summary": evidence_summary,
            **_safety_flags(external_kis_api=True),
            "runtime_semantics": _run_semantics(tickers),
            "applied_overrides": applied,
            "guards": guards,
            "cycles": [],
        }

    hot_runner = trader._hot_runner
    try:
        if getattr(hot_runner, "state", None).value != "HOT_RUNNING":
            hot_runner.start()
    except AttributeError:
        hot_runner.start()

    for idx in range(cycles):
        started_at = datetime.now(KST).isoformat()
        try:
            balance = trader._kis_client.get_balance()
            bars_by_ticker = trader._fetch_recent_bars(tickers)
            latest_prices = trader._latest_prices(bars_by_ticker)
            portfolio_value = trader._portfolio_value(balance)
            current_positions = trader._current_positions(
                balance.get("positions", []),
                latest_prices,
                portfolio_value,
            )
            bars_batch = [
                bar
                for ticker in tickers
                for bar in bars_by_ticker.get(ticker, [])
            ]
            hot_result = hot_runner.run_once(
                tickers=tickers,
                bars_batch=bars_batch,
                current_positions=current_positions,
                latest_prices=latest_prices,
                portfolio_value=portfolio_value,
                asof=started_at,
                recent_bars=bars_by_ticker,
                dependency_status={
                    "news": "skipped",
                    "risk": "done",
                    "quant": "done",
                    "debate": "skipped",
                },
            )
            final_decision = dict(hot_result.get("final_decision") or {})
            order_deltas = [
                dict(od) for od in list(final_decision.get("order_deltas", []))
                if isinstance(od, dict)
            ]
            final_decision["order_deltas"] = order_deltas
            order_guard = trader._order_guard(final_decision)
            quant_output = hot_result.get("quant_output") or {}
            scores = quant_output.get("scores") if isinstance(quant_output, dict) else {}
            score_stats = _score_stats(scores)
            if hot_result.get("skipped"):
                cycle_status = "FAIL"
            elif order_guard.get("status") == "FAIL":
                cycle_status = "FAIL"
            else:
                cycle_status = "PASS"
            cycle_reports.append({
                "status": cycle_status,
                "cycle_index": idx,
                "started_at": started_at,
                "portfolio_value": portfolio_value,
                "position_count": len(current_positions),
                **score_stats,
                "quant_mode": quant_output.get("mode") if isinstance(quant_output, dict) else None,
                "order_delta_count": len(order_deltas),
                "order_guard": order_guard,
                "shadow_order_deltas": order_deltas,
                "execution": "not_submitted_shadow_mode",
            })
        except Exception as e:
            cycle_reports.append({
                "status": "FAIL",
                "cycle_index": idx,
                "started_at": started_at,
                "error": str(e),
                "error_type": type(e).__name__,
            })
            break
        if idx < cycles - 1:
            time.sleep(max(0.0, interval_sec))

    score_nonempty_cycles = sum(1 for c in cycle_reports if int(c.get("finite_score_count") or 0) > 0)
    score_rankable_cycles = sum(1 for c in cycle_reports if c.get("rankable"))
    status = "PASS"
    if any(c.get("status") == "FAIL" for c in cycle_reports):
        status = "FAIL"
    elif not cycle_reports:
        status = "BLOCKED"
    elif cycle_reports and score_nonempty_cycles == 0:
        status = "BLOCKED"
    elif score_rankable_cycles == 0:
        status = "BLOCKED"
    evidence_summary = _summarize_shadow_result(
        policy=policy,
        bundle_id=bundle_id,
        run_config_hash=run_config_hash,
        cycle_reports=cycle_reports,
        status=status,
        serving_feature_readiness=(
            guards["serving_feature_readiness"]
            if isinstance(guards.get("serving_feature_readiness"), dict)
            else {}
        ),
    )
    return {
        "status": status,
        "mode": "shadow",
        "policy_id": policy.policy_id,
        "policy_label": policy.label,
        "policy_hash": _policy_hash(policy),
        "launch_delay_sec": policy.launch_delay_sec,
        "evidence_summary": evidence_summary,
        **_safety_flags(external_kis_api=True),
        "runtime_semantics": _run_semantics(tickers),
        "applied_overrides": applied,
        "guards": guards,
        "cycles": cycle_reports,
        "score_nonempty_cycles": score_nonempty_cycles,
        "score_rankable_cycles": score_rankable_cycles,
        "order_delta_count": sum(int(c.get("order_delta_count") or 0) for c in cycle_reports),
    }


def _run_child(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = _load_yaml(config_path)
    defaults = dict(config.get("run_defaults") or {})
    repo_root = _repo_root(defaults)
    run_dir = RUNS_DIR / str(args.run_id)
    policies = _load_policies(config)
    selected = [p for p in policies if p.policy_id == args.policy_id]
    if not selected:
        raise ValueError(f"unknown policy: {args.policy_id}")
    policy = selected[0]
    bundle_id = str(args.bundle_id or defaults.get("bundle_id"))
    registry_dir = _resolve_registry_dir(
        repo_root,
        str(args.registry_dir or defaults.get("registry_dir") or ""),
    )
    _require_fixed_stable_baseline(
        repo_root=repo_root,
        bundle_id=bundle_id,
        registry_dir=registry_dir,
    )
    _prepare_repo_imports(repo_root, str(registry_dir or ""))

    result: dict[str, Any] = {
        "generated_at": datetime.now(KST).isoformat(),
        "run_id": args.run_id,
        "repo_root": str(repo_root),
        "policy": _policy_overview(policy, os.environ),
        **_safety_flags(external_kis_api=False),
        "prelive": None,
        "result": None,
    }
    try:
        profile_guard = _profile_guard([policy], os.environ)
        result["profile_guard"] = profile_guard
        if profile_guard.get("status") != "PASS":
            result["result"] = {
                "status": "BLOCKED",
                "reason": "kis_profile_not_ready",
                "blockers": profile_guard.get("blockers", []),
            }
            _json_dump(run_dir / f"{policy.policy_id}.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        if args.skip_prelive:
            result["result"] = _prelive_skip_blocker()
            _json_dump(run_dir / f"{policy.policy_id}.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        if policy.launch_delay_sec > 0:
            time.sleep(policy.launch_delay_sec)
        prelive_required = _safe_bool(defaults.get("prelive_required", True), default=True)
        if prelive_required:
            prelive = _prelive_report(repo_root, defaults, args)
            result["prelive"] = prelive
            if prelive.get("status") != "PASS":
                result["result"] = {
                    "status": "BLOCKED",
                    "reason": "prelive_gate_not_pass",
                    "blockers": prelive.get("blockers", []),
                }
                _json_dump(run_dir / f"{policy.policy_id}.json", result)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
        if policy.mode == "paper":
            result["result"] = _run_paper_child(repo_root, run_dir, defaults, policy, args)
        else:
            result["result"] = _run_shadow_child(repo_root, run_dir, defaults, policy, args)
        if isinstance(result["result"], dict):
            result["external_kis_api"] = bool(result["result"].get("external_kis_api"))
    except Exception as e:
        result["result"] = {
            "status": "FAIL",
            "error": str(e),
            "error_type": type(e).__name__,
        }
        _json_dump(run_dir / f"{policy.policy_id}.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    _json_dump(run_dir / f"{policy.policy_id}.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (result.get("result") or {}).get("status") == "PASS" else 1


def _dry_run(args: argparse.Namespace, config: dict[str, Any]) -> int:
    defaults = dict(config.get("run_defaults") or {})
    repo_root = _repo_root(defaults)
    registry_dir = _resolve_registry_dir(
        repo_root,
        str(args.registry_dir or defaults.get("registry_dir") or ""),
    )
    bundle_id = str(args.bundle_id or defaults.get("bundle_id"))
    _require_fixed_stable_baseline(
        repo_root=repo_root,
        bundle_id=bundle_id,
        registry_dir=registry_dir,
    )
    policies = _filter_policies(_load_policies(config), args.policy_id)
    tickers = _as_list_tickers(args.tickers or defaults.get("tickers", ""))
    cycles = int(args.cycles or defaults.get("cycles", 1))
    interval_sec = float(args.interval_sec if args.interval_sec is not None else defaults.get("interval_sec", 0))
    prelive_required = _safe_bool(defaults.get("prelive_required", True), default=True)
    items: list[dict[str, Any]] = []
    for policy in policies:
        env = _env_for_policy(os.environ, policy, repo_root)
        items.append(_policy_overview(policy, env))
    run_config_hash = _run_config_hash(
        policies=policies,
        bundle_id=bundle_id,
        registry_dir=str(registry_dir or ""),
        tickers=tickers,
        cycles=cycles,
        interval_sec=interval_sec,
        prelive_required=prelive_required,
    )
    out = {
        "status": "PASS",
        "action": "local_policy_lab_dry_run",
        "repo_root": str(repo_root),
        "registry_dir": str(registry_dir) if registry_dir else None,
        "git_ignored_root": str(RUNS_DIR),
        "git_ignored_runs_dir": str(RUNS_DIR),
        "bundle_id": bundle_id,
        "model_positioning": _model_positioning(bundle_id),
        "tickers": tickers,
        "cycles": cycles,
        "interval_sec": interval_sec,
        "run_config_hash": run_config_hash,
        "policies": items,
        "runtime_semantics": _run_semantics(
            tickers
        ),
        "daily_cross_track_summary_template": _cross_track_summary(
            policies,
            run_id="dry_run",
            bundle_id=bundle_id,
            tickers=tickers,
            run_config_hash=run_config_hash,
        ),
        "profile_guard": _profile_guard(policies, os.environ),
        **_safety_flags(external_kis_api=False),
        "secrets_printed": False,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _filter_policies(policies: list[PolicySpec], policy_id: str | None) -> list[PolicySpec]:
    if not policy_id:
        return policies
    filtered = [p for p in policies if p.policy_id == policy_id]
    if not filtered:
        raise ValueError(f"unknown policy: {policy_id}")
    return filtered


def _cross_track_summary(
    policies: list[PolicySpec],
    *,
    run_id: str,
    bundle_id: str,
    tickers: list[str],
    run_config_hash: str,
    completed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    paper_tracks = [p.policy_id for p in policies if p.mode == "paper"]
    shadow_tracks = [p.policy_id for p in policies if p.mode == "shadow"]
    evidence_by_policy: dict[str, dict[str, Any]] = {}
    for item in completed or []:
        policy_id = str(item.get("policy_id") or "")
        evidence = item.get("evidence_summary")
        if policy_id and isinstance(evidence, dict):
            evidence_by_policy[policy_id] = evidence

    track_evidence: list[dict[str, Any]] = []
    for policy in policies:
        evidence = evidence_by_policy.get(policy.policy_id, {})
        serving = evidence.get("serving_feature_readiness")
        track_evidence.append({
            "track_id": policy.policy_id,
            "mode": policy.mode,
            "policy_hash": _policy_hash(policy),
            "status": evidence.get("status"),
            "cycles_completed": int(evidence.get("cycles_completed") or 0),
            "score_nonempty_cycles": int(evidence.get("score_nonempty_cycles") or 0),
            "rankable_score_cycles": int(evidence.get("rankable_score_cycles") or 0),
            "order_delta_count": int(evidence.get("order_delta_count") or 0),
            "submitted_count": int(evidence.get("submitted_count") or 0),
            "fill_count": int(evidence.get("fill_count") or 0),
            "reject_count": int(evidence.get("reject_count") or 0),
            "matched_order_history_count": int(
                evidence.get("matched_order_history_count") or 0
            ),
            "fail_closed_count": int(evidence.get("fail_closed_count") or 0),
            "turnover_bps": evidence.get("turnover_bps"),
            "daily_return_bps": evidence.get("daily_return_bps"),
            "zero_order_false_pass": bool(evidence.get("zero_order_false_pass")),
            "serving_feature_readiness_status": (
                serving.get("status") if isinstance(serving, dict) else None
            ),
        })
    aggregate = {
        "cycles_completed": sum(row["cycles_completed"] for row in track_evidence),
        "score_nonempty_cycles": sum(row["score_nonempty_cycles"] for row in track_evidence),
        "rankable_score_cycles": sum(row["rankable_score_cycles"] for row in track_evidence),
        "order_delta_count": sum(row["order_delta_count"] for row in track_evidence),
        "submitted_count": sum(row["submitted_count"] for row in track_evidence),
        "fill_count": sum(row["fill_count"] for row in track_evidence),
        "reject_count": sum(row["reject_count"] for row in track_evidence),
        "matched_order_history_count": sum(
            row["matched_order_history_count"] for row in track_evidence
        ),
        "fail_closed_count": sum(row["fail_closed_count"] for row in track_evidence),
        "zero_order_false_pass_count": sum(
            1 for row in track_evidence if row["zero_order_false_pass"]
        ),
    }
    return {
        "run_id": run_id,
        "bundle_id": bundle_id,
        "model_positioning": _model_positioning(bundle_id),
        "run_config_hash": run_config_hash,
        "tracks": [p.policy_id for p in policies],
        "policy_hashes": {p.policy_id: _policy_hash(p) for p in policies},
        "paper_tracks": paper_tracks,
        "shadow_tracks": shadow_tracks,
        "paper_track_count": len(paper_tracks),
        "shadow_track_count": len(shadow_tracks),
        "comparison_basis": "normalized_nav_bps",
        "common_universe": True,
        "common_tickers": tickers,
        "separate_kis_profiles_required": True,
        "common_start_time": False,
        "pure_return_ranking_allowed": False,
        "pure_return_ranking_note": (
            "Track offsets reduce KIS API collisions. Compare operational stability "
            "and normalized NAV bps with this caveat."
        ),
        "launch_offsets_sec": {p.policy_id: p.launch_delay_sec for p in policies},
        "evidence_schema": _evidence_schema(),
        "track_evidence": track_evidence,
        "aggregate": aggregate,
    }


def _failure_case_cards(run_dir: Path, completed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for item in completed:
        policy_id = str(item.get("policy_id") or "")
        status = str(item.get("child_result_status") or "")
        evidence_blockers = item.get("evidence_blockers") or []
        if status == "PASS" and not evidence_blockers:
            continue
        card: dict[str, Any] = {
            "failure_case_id": f"FC-{policy_id or 'UNKNOWN'}",
            "track_id": policy_id,
            "severity": "HIGH" if status in {"FAIL", "BLOCKED"} or evidence_blockers else "MEDIUM",
            "status": status or "UNKNOWN",
            "returncode": item.get("returncode"),
            "report_path": item.get("report_path"),
            "broker_order_submitted": None,
            "fail_closed": status in {"FAIL", "BLOCKED"} or bool(evidence_blockers),
            "reason_code": None,
            "root_cause": None,
            "evidence_blockers": evidence_blockers,
            "action": "inspect child report and native paper-auto report",
        }
        path = run_dir / f"{policy_id}.json"
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            result = data.get("result") if isinstance(data, dict) else None
            if isinstance(result, dict):
                card["reason_code"] = result.get("reason") or result.get("status")
                card["root_cause"] = result.get("error") or result.get("blocker")
                evidence = result.get("evidence_summary")
                if isinstance(evidence, dict):
                    submitted = evidence.get("submitted_count")
                    if submitted is not None:
                        card["broker_order_submitted"] = int(submitted or 0) > 0
                native = result.get("native_report")
                if isinstance(native, dict):
                    orders = native.get("orders")
                    submitted = native.get("submitted_count")
                    card["broker_order_submitted"] = (
                        bool(card["broker_order_submitted"])
                        or bool(orders)
                        or bool(submitted)
                    )
        except Exception as e:
            card["root_cause"] = f"{type(e).__name__}: {e}"
        cards.append(card)
    return cards


def _run_parent(args: argparse.Namespace, config: dict[str, Any]) -> int:
    defaults = dict(config.get("run_defaults") or {})
    repo_root = _repo_root(defaults)
    registry_dir = _resolve_registry_dir(
        repo_root,
        str(args.registry_dir or defaults.get("registry_dir") or ""),
    )
    bundle_id = str(args.bundle_id or defaults.get("bundle_id"))
    _require_fixed_stable_baseline(
        repo_root=repo_root,
        bundle_id=bundle_id,
        registry_dir=registry_dir,
    )
    policies = _filter_policies(_load_policies(config), args.policy_id)
    run_id = _run_id(args.run_id)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tickers = _as_list_tickers(args.tickers or defaults.get("tickers", ""))
    cycles = int(args.cycles or defaults.get("cycles", 1))
    interval_sec = float(args.interval_sec if args.interval_sec is not None else defaults.get("interval_sec", 0))
    prelive_required = _safe_bool(defaults.get("prelive_required", True), default=True)
    run_config_hash = _run_config_hash(
        policies=policies,
        bundle_id=bundle_id,
        registry_dir=str(registry_dir or ""),
        tickers=tickers,
        cycles=cycles,
        interval_sec=interval_sec,
        prelive_required=prelive_required,
    )

    profile_guard = _profile_guard(policies, os.environ)
    if args.skip_prelive:
        index = {
            "status": "BLOCKED",
            "generated_at": datetime.now(KST).isoformat(),
            "run_id": run_id,
            "repo_root": str(repo_root),
            "bundle_id": bundle_id,
            "model_positioning": _model_positioning(bundle_id),
            "run_config_hash": run_config_hash,
            "reason": "prelive_skip_forbidden_for_runtime",
            "profile_guard": profile_guard,
            "policies": [],
            "runtime_semantics": _run_semantics(tickers),
            "daily_cross_track_summary": _cross_track_summary(
                policies,
                run_id=run_id,
                bundle_id=bundle_id,
                tickers=tickers,
                run_config_hash=run_config_hash,
            ),
            "failure_case_cards": [_prelive_skip_blocker()],
            **_safety_flags(external_kis_api=False),
            "secrets_printed": False,
        }
        _json_dump(run_dir / "index.json", index)
        print(json.dumps(index, ensure_ascii=False, indent=2))
        return 1
    if profile_guard.get("status") != "PASS":
        index = {
            "status": "BLOCKED",
            "generated_at": datetime.now(KST).isoformat(),
            "run_id": run_id,
            "repo_root": str(repo_root),
            "bundle_id": bundle_id,
            "model_positioning": _model_positioning(bundle_id),
            "run_config_hash": run_config_hash,
            "reason": "kis_profile_not_ready",
            "profile_guard": profile_guard,
            "policies": [],
            "runtime_semantics": _run_semantics(tickers),
            "daily_cross_track_summary": _cross_track_summary(
                policies,
                run_id=run_id,
                bundle_id=bundle_id,
                tickers=tickers,
                run_config_hash=run_config_hash,
            ),
            "failure_case_cards": [],
            **_safety_flags(external_kis_api=False),
            "secrets_printed": False,
        }
        _json_dump(run_dir / "index.json", index)
        print(json.dumps(index, ensure_ascii=False, indent=2))
        return 1

    python = str(args.python or defaults.get("python") or sys.executable)
    max_parallel = max(1, int(defaults.get("max_parallel", 1)))
    launched: list[tuple[PolicySpec, subprocess.Popen[Any], Path, Path]] = []
    completed: list[dict[str, Any]] = []

    def launch(policy: PolicySpec) -> None:
        env = _env_for_policy(os.environ, policy, repo_root)
        stdout_path = run_dir / f"{policy.policy_id}.stdout.log"
        stderr_path = run_dir / f"{policy.policy_id}.stderr.log"
        cmd = [
            python,
            str(SCRIPT_PATH),
            "--child",
            "--config",
            str(args.config),
            "--run-id",
            run_id,
            "--policy-id",
            policy.policy_id,
            "--bundle-id",
            bundle_id,
            "--registry-dir",
            str(registry_dir or ""),
            "--tickers",
            ",".join(tickers),
            "--cycles",
            str(cycles),
            "--interval-sec",
            str(interval_sec),
            "--end-date",
            str(args.end_date or defaults.get("end_date")),
            "--business-days",
            str(int(args.business_days or defaults.get("business_days"))),
            "--max-tickers",
            str(int(args.max_tickers or defaults.get("max_tickers", 30))),
            "--confirm-phrase",
            str(args.confirm_phrase or defaults.get("confirm_phrase")),
            "--run-config-hash",
            run_config_hash,
        ]
        out_f = stdout_path.open("w", encoding="utf-8")
        err_f = stderr_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=str(repo_root), env=env, stdout=out_f, stderr=err_f)
        launched.append((policy, proc, stdout_path, stderr_path))

    pending = list(policies)
    while pending or launched:
        while pending and len(launched) < max_parallel:
            launch(pending.pop(0))
        time.sleep(1.0)
        still_running: list[tuple[PolicySpec, subprocess.Popen[Any], Path, Path]] = []
        for policy, proc, stdout_path, stderr_path in launched:
            code = proc.poll()
            if code is None:
                still_running.append((policy, proc, stdout_path, stderr_path))
                continue
            completed.append({
                "policy_id": policy.policy_id,
                "mode": policy.mode,
                "returncode": code,
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
                **_child_report_state(run_dir / f"{policy.policy_id}.json"),
            })
        launched = still_running

    index = {
        "status": "PASS" if _parent_completed_successfully(run_dir, completed) else "BLOCKED",
        "generated_at": datetime.now(KST).isoformat(),
        "run_id": run_id,
        "repo_root": str(repo_root),
        "bundle_id": bundle_id,
        "model_positioning": _model_positioning(bundle_id),
        "run_config_hash": run_config_hash,
        "policies": completed,
        "runtime_semantics": _run_semantics(
            tickers
        ),
        "daily_cross_track_summary": _cross_track_summary(
            policies,
            run_id=run_id,
            bundle_id=bundle_id,
            tickers=tickers,
            run_config_hash=run_config_hash,
            completed=completed,
        ),
        "profile_guard": profile_guard,
        "failure_case_cards": _failure_case_cards(run_dir, completed),
        **_safety_flags(external_kis_api=bool(completed)),
        "secrets_printed": False,
    }
    _json_dump(run_dir / "index.json", index)
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0 if index["status"] == "PASS" else 1


def _child_report_state(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {
        "report_path": str(path),
        "report_exists": path.exists(),
        "report_parse_error": None,
        "child_result_status": None,
        "evidence_summary": None,
        "evidence_valid": False,
        "evidence_blockers": [],
    }
    if not path.exists():
        return state
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        result = data.get("result") if isinstance(data, dict) else None
        state["child_result_status"] = (
            result.get("status") if isinstance(result, dict) else None
        )
        if isinstance(result, dict):
            evidence = result.get("evidence_summary")
            state["evidence_summary"] = evidence if isinstance(evidence, dict) else None
            blockers = _evidence_blockers(evidence if isinstance(evidence, dict) else {})
            state["evidence_blockers"] = blockers
            state["evidence_valid"] = not blockers
    except Exception as e:
        state["report_parse_error"] = f"{type(e).__name__}: {e}"
    return state


def _evidence_blockers(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    if not evidence:
        return [{"reason": "missing_evidence_summary"}]
    blockers: list[dict[str, Any]] = []
    required = _evidence_schema()["required_run_fields"]
    missing = [key for key in required if key not in evidence]
    if missing:
        blockers.append({"reason": "missing_evidence_fields", "fields": missing})
    serving_feature_readiness = evidence.get("serving_feature_readiness")
    if (
        not isinstance(serving_feature_readiness, dict)
        or serving_feature_readiness.get("status") != "PASS"
    ):
        blockers.append({
            "reason": "serving_feature_readiness_not_pass",
            "serving_feature_readiness": serving_feature_readiness,
        })
    if evidence.get("zero_order_false_pass"):
        blockers.append({"reason": "zero_order_false_pass"})
    cycles_completed = int(evidence.get("cycles_completed") or 0)
    if cycles_completed < 1:
        blockers.append({"reason": "no_cycles_completed"})
    if int(evidence.get("score_nonempty_cycles") or 0) < 1:
        blockers.append({"reason": "no_nonempty_quant_scores"})
    if int(evidence.get("rankable_score_cycles") or 0) < 1:
        blockers.append({"reason": "no_rankable_quant_scores"})
    return blockers


def _parent_completed_successfully(run_dir: Path, completed: list[dict[str, Any]]) -> bool:
    if not completed:
        return False
    return all(
        item.get("returncode") == 0
        and item.get("report_exists") is True
        and not item.get("report_parse_error")
        and item.get("child_result_status") == "PASS"
        and item.get("evidence_valid") is True
        for item in completed
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local KIS paper/shadow multi-policy harness")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--policy-id", default="")
    parser.add_argument("--bundle-id", default="")
    parser.add_argument("--registry-dir", default="")
    parser.add_argument("--tickers", default="")
    parser.add_argument("--cycles", type=int, default=0)
    parser.add_argument("--interval-sec", type=float, default=None)
    parser.add_argument("--end-date", default="")
    parser.add_argument("--business-days", type=int, default=0)
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--confirm-phrase", default="")
    parser.add_argument("--python", default="")
    parser.add_argument("--run-config-hash", default="")
    parser.add_argument("--skip-prelive", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--child", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = _load_yaml(Path(args.config).expanduser().resolve())
        _validate_config(config)
        if args.child:
            return _run_child(args)
        if args.dry_run:
            return _dry_run(args, config)
        return _run_parent(args, config)
    except Exception as e:
        report = {
            "status": "BLOCKED",
            "action": "local_policy_lab_startup",
            "error": str(e),
            "error_type": type(e).__name__,
            **_safety_flags(external_kis_api=False),
            "secrets_printed": False,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
