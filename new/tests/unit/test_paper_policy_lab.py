from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_policy_lab_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "experiments"
        / "paper_policy_lab"
        / "run_multi_policy_lab.py"
    )
    spec = importlib.util.spec_from_file_location("run_multi_policy_lab", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_policy_lab_rejects_service_replay_only_overrides() -> None:
    lab = _load_policy_lab_module()

    for key in lab.SERVICE_REPLAY_ONLY_KEYS:
        with pytest.raises(ValueError, match="service-policy replay-only"):
            lab._validate_override_keys("BAD_POLICY", {key: 1})


def test_policy_lab_rejects_unknown_overrides() -> None:
    lab = _load_policy_lab_module()

    with pytest.raises(ValueError, match="unsupported paper runtime overrides"):
        lab._validate_override_keys("BAD_POLICY", {"unknown_policy_key": 1})


def test_policy_lab_rejects_empty_or_malformed_policies() -> None:
    lab = _load_policy_lab_module()

    with pytest.raises(ValueError, match="at least one policy"):
        lab._load_policies({"policies": []})
    with pytest.raises(ValueError, match="policy entries must be mappings"):
        lab._load_policies({"policies": ["not-a-mapping"]})


def test_policy_lab_accepts_runtime_overrides() -> None:
    lab = _load_policy_lab_module()

    lab._validate_override_keys(
        "OK_POLICY",
        {
            "max_orders_per_cycle": 2,
            "max_order_qty_per_order": 1,
            "ppo_max_names": 7,
            "ppo_min_cash": 0.15,
            "ppo_min_confidence": 0.06,
            "trade_probability_gate_enabled": False,
            "min_trade_probability": 0.55,
            "ppo_weighting": "score",
        },
    )


def test_policy_lab_rejects_invalid_ppo_weighting() -> None:
    lab = _load_policy_lab_module()

    with pytest.raises(ValueError, match="unsupported ppo_weighting"):
        lab._validate_override_keys("BAD_POLICY", {"ppo_weighting": "typo"})


def test_policy_lab_rejects_invalid_override_values() -> None:
    lab = _load_policy_lab_module()

    invalid = [
        {"max_orders_per_cycle": 0},
        {"max_order_qty_per_order": "1.5"},
        {"ppo_min_cash": 1.0},
        {"ppo_min_confidence": -0.01},
        {"min_trade_probability": 1.5},
        {"trade_probability_gate_enabled": "flase"},
        {"allow_market_order": "maybe"},
    ]
    for overrides in invalid:
        with pytest.raises(ValueError):
            lab._validate_override_values("BAD_POLICY", overrides)


def test_policy_lab_runtime_semantics_disclaims_replay_equivalence() -> None:
    lab = _load_policy_lab_module()

    semantics = lab._run_semantics(["005930", "000660"])

    assert semantics["service_policy_replay_equivalence"] is False
    assert "decision_stride_bars" in semantics["replay_only_fields_not_enforced_here"]
    assert "min_cash" in semantics["replay_only_fields_not_enforced_here"]
    assert "ppo_min_cash" in semantics["runtime_enforced_fields"]


def test_policy_lab_rejects_replay_only_run_default_keys() -> None:
    lab = _load_policy_lab_module()

    for key in lab.SERVICE_REPLAY_ONLY_KEYS:
        with pytest.raises(ValueError, match="run_defaults contains service-policy replay-only"):
            lab._validate_run_defaults({key: 1})


def test_policy_lab_rejects_unknown_run_default_keys() -> None:
    lab = _load_policy_lab_module()

    with pytest.raises(ValueError, match="run_defaults contains unsupported keys"):
        lab._validate_run_defaults({"unknown_default_key": 1})


def test_policy_lab_safe_bool_handles_quoted_false() -> None:
    lab = _load_policy_lab_module()

    assert lab._safe_bool("false", default=True) is False
    assert lab._safe_bool("0", default=True) is False
    assert lab._safe_bool("true", default=False) is True
    assert lab._safe_bool("unexpected", default=False) is False


def test_policy_lab_requires_explicit_registry_dir() -> None:
    lab = _load_policy_lab_module()
    repo_root = Path("/Users/jangjaewon/Desktop/Elephant_Lab")

    with pytest.raises(ValueError, match="explicit non-production registry_dir"):
        lab._resolve_registry_dir(repo_root, "")


def test_policy_lab_score_stats_require_rankable_nonzero_scores() -> None:
    lab = _load_policy_lab_module()

    assert lab._score_stats({})["rankable"] is False
    assert lab._score_stats({"005930": 0.0, "000660": 0.0})["rankable"] is False
    assert lab._score_stats({"005930": 1.0, "000660": 1.0})["rankable"] is False
    assert lab._score_stats({"005930": 1.0, "000660": 2.0})["rankable"] is True


def test_policy_lab_parent_requires_completed_child_report(tmp_path) -> None:
    lab = _load_policy_lab_module()

    assert lab._parent_completed_successfully([]) is False
    assert lab._parent_completed_successfully([
        {
            "returncode": 0,
            "report_exists": True,
            "report_parse_error": None,
            "child_result_status": "PASS",
        }
    ]) is True
    assert lab._parent_completed_successfully([
        {
            "returncode": 0,
            "report_exists": False,
            "report_parse_error": None,
            "child_result_status": None,
        }
    ]) is False

    missing_state = lab._child_report_state(tmp_path / "missing.json")
    assert missing_state["report_exists"] is False


def test_policy_lab_prelive_required_uses_safe_bool(monkeypatch, tmp_path) -> None:
    lab = _load_policy_lab_module()
    config_path = tmp_path / "policies.yaml"
    config_path.write_text(
        """
run_defaults:
  python: /opt/anaconda3/envs/elephant/bin/python
  repo_root: /Users/jangjaewon/Desktop/Elephant_Lab
  bundle_id: BUNDLE-TEST
  registry_dir: artifacts/lgbm_paper_candidate/BUNDLE-TEST
  tickers: 005930
  cycles: 1
  interval_sec: 0
  confirm_phrase: PAPER_AUTO_OK
  prelive_required: "false"
policies:
  - id: SHADOW
    label: Shadow
    mode: shadow
    profile_prefix: KIS_SHADOW
    overrides: {}
""",
        encoding="utf-8",
    )

    called = {"prelive": False}

    def fail_if_called(*args, **kwargs):
        called["prelive"] = True
        raise AssertionError("prelive should be skipped when quoted false")

    monkeypatch.setattr(lab, "_prelive_report", fail_if_called)
    monkeypatch.setattr(lab, "_run_shadow_child", lambda *args, **kwargs: {"status": "PASS"})
    monkeypatch.setattr(lab, "RUNS_DIR", tmp_path / "runs")

    args = type(
        "Args",
        (),
        {
            "config": str(config_path),
            "run_id": "test_run",
            "policy_id": "SHADOW",
            "registry_dir": None,
            "skip_prelive": False,
            "bundle_id": None,
            "tickers": None,
            "cycles": None,
            "interval_sec": None,
            "confirm_phrase": None,
            "end_date": None,
            "business_days": None,
            "max_tickers": None,
        },
    )()

    assert lab._run_child(args) == 0
    assert called["prelive"] is False


def test_policy_lab_dry_run_reports_ignored_runs_dir(capsys) -> None:
    lab = _load_policy_lab_module()
    config = {
        "run_defaults": {
            "repo_root": "/Users/jangjaewon/Desktop/Elephant_Lab",
            "bundle_id": "BUNDLE-TEST",
            "registry_dir": "artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            "tickers": "005930",
            "cycles": 1,
            "interval_sec": 0,
        },
        "policies": [
            {
                "id": "SHADOW",
                "label": "Shadow",
                "mode": "shadow",
                "profile_prefix": "KIS_SHADOW",
                "overrides": {},
            }
        ],
    }
    args = type(
        "Args",
        (),
        {
            "registry_dir": None,
            "policy_id": "",
            "bundle_id": None,
            "tickers": None,
            "cycles": None,
            "interval_sec": None,
        },
    )()

    assert lab._dry_run(args, config) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["git_ignored_root"].endswith("paper_policy_lab/runs")
    assert report["git_ignored_runs_dir"] == report["git_ignored_root"]


def test_policy_lab_forbidden_registry_dry_run_returns_json(capsys, tmp_path) -> None:
    lab = _load_policy_lab_module()
    config_path = tmp_path / "policies.yaml"
    config_path.write_text(
        """
run_defaults:
  repo_root: /Users/jangjaewon/Desktop/Elephant_Lab
  bundle_id: BUNDLE-TEST
  registry_dir: artifacts/lgbm_paper_candidate/BUNDLE-TEST
  tickers: 005930
  cycles: 1
  interval_sec: 0
policies:
  - id: SHADOW
    label: Shadow
    mode: shadow
    profile_prefix: KIS_SHADOW
    overrides: {}
""",
        encoding="utf-8",
    )

    code = lab.main(
        [
            "--config",
            str(config_path),
            "--dry-run",
            "--registry-dir",
            "artifacts/lgbm",
        ]
    )

    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "BLOCKED"
    assert report["error_type"] == "ValueError"
    assert "production registry" in report["error"]
    assert report["external_kis_api"] is False
