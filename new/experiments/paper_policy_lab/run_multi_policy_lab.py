#!/usr/bin/env python
"""Run personal paper/shadow execution policy lanes.

The harness is tracked in the personal experiment branch, while per-run JSON
reports and logs stay under the ignored runs/ directory.
"""
from __future__ import annotations

import argparse
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


@dataclass(frozen=True)
class PolicySpec:
    policy_id: str
    label: str
    mode: str
    profile_prefix: str
    description: str
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
        "profile_source_presence": _profile_source_presence(env, policy.profile_prefix),
        "mapped_env_readiness": _readiness_from_env(env),
        "overrides": policy.overrides,
        "override_semantics": _override_semantics(policy),
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
    trader = _new_trader(repo_root, run_dir, policy, bundle_id)
    applied = _apply_policy_overrides(trader, policy)
    report = trader.run(
        tickers=_as_list_tickers(args.tickers or defaults.get("tickers", "")),
        cycles=int(args.cycles or defaults.get("cycles", 1)),
        interval_sec=float(args.interval_sec if args.interval_sec is not None else defaults.get("interval_sec", 0)),
        confirm_phrase=str(args.confirm_phrase or defaults.get("confirm_phrase")),
        write_report=True,
    )
    return {
        "status": report.get("status"),
        "mode": "paper",
        "policy_id": policy.policy_id,
        "policy_label": policy.label,
        **_safety_flags(external_kis_api=True),
        "runtime_semantics": _run_semantics(
            _as_list_tickers(args.tickers or defaults.get("tickers", ""))
        ),
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
    trader = _new_trader(repo_root, run_dir, policy, bundle_id)
    applied = _apply_policy_overrides(trader, policy)
    tickers = _as_list_tickers(args.tickers or defaults.get("tickers", ""))
    cycles = int(args.cycles or defaults.get("cycles", 1))
    interval_sec = float(args.interval_sec if args.interval_sec is not None else defaults.get("interval_sec", 0))
    confirm_phrase = str(args.confirm_phrase or defaults.get("confirm_phrase"))

    cycle_reports: list[dict[str, Any]] = []
    guards = {
        "start_guard": trader._start_guard(confirm_phrase),
        "market_session_guard": None,
        "mode_guard": None,
        "active_model_guard": None,
    }
    if guards["start_guard"].get("status") == "PASS":
        guards["market_session_guard"] = trader._market_session_check()
    if (guards["market_session_guard"] or {}).get("status") == "PASS":
        guards["mode_guard"] = trader._paper_mode_check()
    if (guards["mode_guard"] or {}).get("status") == "PASS":
        guards["active_model_guard"] = trader._active_model_check()

    guard_statuses = [guard.get("status") for guard in guards.values() if isinstance(guard, dict)]
    if any(status not in {"PASS", None} for status in guard_statuses):
        return {
            "status": "BLOCKED",
            "mode": "shadow",
            "policy_id": policy.policy_id,
            "policy_label": policy.label,
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
    return {
        "status": status,
        "mode": "shadow",
        "policy_id": policy.policy_id,
        "policy_label": policy.label,
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
    _prepare_repo_imports(repo_root, str(args.registry_dir or defaults.get("registry_dir") or ""))

    result: dict[str, Any] = {
        "generated_at": datetime.now(KST).isoformat(),
        "run_id": args.run_id,
        "repo_root": str(repo_root),
        "policy": _policy_overview(policy, os.environ),
        **_safety_flags(external_kis_api=True),
        "prelive": None,
        "result": None,
    }
    try:
        prelive_required = _safe_bool(defaults.get("prelive_required", True), default=True)
        if args.skip_prelive:
            prelive_required = False
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
    policies = _filter_policies(_load_policies(config), args.policy_id)
    items: list[dict[str, Any]] = []
    for policy in policies:
        env = _env_for_policy(os.environ, policy, repo_root)
        items.append(_policy_overview(policy, env))
    out = {
        "status": "PASS",
        "action": "local_policy_lab_dry_run",
        "repo_root": str(repo_root),
        "registry_dir": str(registry_dir) if registry_dir else None,
        "git_ignored_root": str(RUNS_DIR),
        "git_ignored_runs_dir": str(RUNS_DIR),
        "bundle_id": str(args.bundle_id or defaults.get("bundle_id")),
        "tickers": _as_list_tickers(args.tickers or defaults.get("tickers", "")),
        "cycles": int(args.cycles or defaults.get("cycles", 1)),
        "interval_sec": float(args.interval_sec if args.interval_sec is not None else defaults.get("interval_sec", 0)),
        "policies": items,
        "runtime_semantics": _run_semantics(
            _as_list_tickers(args.tickers or defaults.get("tickers", ""))
        ),
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


def _run_parent(args: argparse.Namespace, config: dict[str, Any]) -> int:
    defaults = dict(config.get("run_defaults") or {})
    repo_root = _repo_root(defaults)
    registry_dir = _resolve_registry_dir(
        repo_root,
        str(args.registry_dir or defaults.get("registry_dir") or ""),
    )
    policies = _filter_policies(_load_policies(config), args.policy_id)
    run_id = _run_id(args.run_id)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

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
            str(args.bundle_id or defaults.get("bundle_id")),
            "--registry-dir",
            str(registry_dir or ""),
            "--tickers",
            ",".join(_as_list_tickers(args.tickers or defaults.get("tickers", ""))),
            "--cycles",
            str(int(args.cycles or defaults.get("cycles", 1))),
            "--interval-sec",
            str(float(args.interval_sec if args.interval_sec is not None else defaults.get("interval_sec", 0))),
            "--end-date",
            str(args.end_date or defaults.get("end_date")),
            "--business-days",
            str(int(args.business_days or defaults.get("business_days"))),
            "--max-tickers",
            str(int(args.max_tickers or defaults.get("max_tickers", 30))),
            "--confirm-phrase",
            str(args.confirm_phrase or defaults.get("confirm_phrase")),
        ]
        if args.skip_prelive:
            cmd.append("--skip-prelive")
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
        "status": "PASS" if _parent_completed_successfully(completed) else "BLOCKED",
        "generated_at": datetime.now(KST).isoformat(),
        "run_id": run_id,
        "repo_root": str(repo_root),
        "bundle_id": str(args.bundle_id or defaults.get("bundle_id")),
        "policies": completed,
        "runtime_semantics": _run_semantics(
            _as_list_tickers(args.tickers or defaults.get("tickers", ""))
        ),
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
    except Exception as e:
        state["report_parse_error"] = f"{type(e).__name__}: {e}"
    return state


def _parent_completed_successfully(completed: list[dict[str, Any]]) -> bool:
    if not completed:
        return False
    return all(
        item.get("returncode") == 0
        and item.get("report_exists") is True
        and not item.get("report_parse_error")
        and item.get("child_result_status") == "PASS"
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
