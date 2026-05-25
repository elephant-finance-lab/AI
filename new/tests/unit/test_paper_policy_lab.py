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


def test_policy_lab_rejects_invalid_launch_delay() -> None:
    lab = _load_policy_lab_module()

    with pytest.raises(ValueError, match="launch_delay_sec"):
        lab._load_policies(
            {
                "policies": [
                    {
                        "id": "BAD",
                        "mode": "shadow",
                        "launch_delay_sec": -1,
                        "overrides": {},
                    }
                ]
            }
        )


def test_policy_lab_policy_hash_changes_with_runtime_policy() -> None:
    lab = _load_policy_lab_module()
    base = lab.PolicySpec(
        policy_id="BASE",
        label="Base",
        mode="paper",
        profile_prefix="KIS_BASE",
        description="",
        launch_delay_sec=0,
        overrides={"ppo_min_confidence": 0.03},
    )
    changed = lab.PolicySpec(
        policy_id="BASE",
        label="Base",
        mode="paper",
        profile_prefix="KIS_BASE",
        description="",
        launch_delay_sec=10,
        overrides={"ppo_min_confidence": 0.03},
    )

    assert lab._policy_hash(base) != lab._policy_hash(changed)


def test_policy_lab_runtime_semantics_disclaims_replay_equivalence() -> None:
    lab = _load_policy_lab_module()

    semantics = lab._run_semantics(["005930", "000660"])

    assert semantics["service_policy_replay_equivalence"] is False
    assert "decision_stride_bars" in semantics["replay_only_fields_not_enforced_here"]
    assert "min_cash" in semantics["replay_only_fields_not_enforced_here"]
    assert "ppo_min_cash" in semantics["runtime_enforced_fields"]
    assert semantics["account_fairness_requirements"]["separate_kis_profile_required"] is True
    assert "policy_hash" in semantics["evidence_schema"]["required_run_fields"]


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

    assert lab._parent_completed_successfully(tmp_path, []) is False
    assert lab._parent_completed_successfully(tmp_path, [
        {
            "returncode": 0,
            "report_exists": True,
            "report_parse_error": None,
            "child_result_status": "PASS",
            "evidence_valid": True,
        }
    ]) is True
    assert lab._parent_completed_successfully(tmp_path, [
        {
            "returncode": 0,
            "report_exists": False,
            "report_parse_error": None,
            "child_result_status": None,
            "evidence_valid": False,
        }
    ]) is False

    missing_state = lab._child_report_state(tmp_path / "missing.json")
    assert missing_state["report_exists"] is False


def test_policy_lab_parent_blocks_missing_evidence(tmp_path) -> None:
    lab = _load_policy_lab_module()

    assert lab._parent_completed_successfully(tmp_path, [
        {
            "returncode": 0,
            "report_exists": True,
            "report_parse_error": None,
            "child_result_status": "PASS",
            "evidence_valid": False,
        }
    ]) is False


def test_policy_lab_profile_guard_blocks_missing_or_duplicate_profiles() -> None:
    lab = _load_policy_lab_module()
    policies = lab._load_policies(
        {
            "policies": [
                {
                    "id": "A",
                    "mode": "paper",
                    "profile_prefix": "KIS_A",
                    "overrides": {},
                },
                {
                    "id": "B",
                    "mode": "paper",
                    "profile_prefix": "KIS_B",
                    "overrides": {},
                },
            ]
        }
    )

    missing = lab._profile_guard(policies, {})
    assert missing["status"] == "BLOCKED"
    assert missing["blockers"][0]["reason"] == "profile_not_ready"

    duplicate_env = {
        "KIS_A_MODE": "virtual",
        "KIS_A_APP_KEY": "x",
        "KIS_A_APP_SECRET": "x",
        "KIS_A_ACCOUNT_NUMBER": "123",
        "KIS_A_ACCOUNT_PRODUCT_CODE": "01",
        "KIS_B_MODE": "virtual",
        "KIS_B_APP_KEY": "y",
        "KIS_B_APP_SECRET": "y",
        "KIS_B_ACCOUNT_NUMBER": "123",
        "KIS_B_ACCOUNT_PRODUCT_CODE": "01",
    }
    duplicate = lab._profile_guard(policies, duplicate_env)
    assert duplicate["status"] == "BLOCKED"
    assert duplicate["blockers"][0]["reason"] == "duplicate_account_identity"


def test_policy_lab_prelive_required_uses_safe_bool(monkeypatch, tmp_path) -> None:
    lab = _load_policy_lab_module()
    config_path = tmp_path / "policies.yaml"
    config_path.write_text(
        """
run_defaults:
  python: /opt/anaconda3/envs/elephant/bin/python
  repo_root: /Users/jangjaewon/Desktop/Elephant_Lab
  bundle_id: BUNDLE-20260521-POSTCLOSE
  registry_dir: artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE
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
    monkeypatch.setenv("KIS_SHADOW_MODE", "virtual")
    monkeypatch.setenv("KIS_SHADOW_APP_KEY", "x")
    monkeypatch.setenv("KIS_SHADOW_APP_SECRET", "x")
    monkeypatch.setenv("KIS_SHADOW_ACCOUNT_NUMBER", "123")
    monkeypatch.setenv("KIS_SHADOW_ACCOUNT_PRODUCT_CODE", "01")

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
            "run_config_hash": "",
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


def test_policy_lab_skip_prelive_blocks_runtime(capsys, tmp_path) -> None:
    lab = _load_policy_lab_module()
    config_path = tmp_path / "policies.yaml"
    config_path.write_text(
        """
run_defaults:
  repo_root: /Users/jangjaewon/Desktop/Elephant_Lab
  bundle_id: BUNDLE-20260521-POSTCLOSE
  registry_dir: artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE
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
    lab.RUNS_DIR = tmp_path / "runs"

    code = lab.main([
        "--config",
        str(config_path),
        "--skip-prelive",
    ])

    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "BLOCKED"
    assert report["reason"] == "prelive_skip_forbidden_for_runtime"
    assert report["external_kis_api"] is False


def test_policy_lab_dry_run_reports_ignored_runs_dir(capsys) -> None:
    lab = _load_policy_lab_module()
    config = {
        "run_defaults": {
            "repo_root": "/Users/jangjaewon/Desktop/Elephant_Lab",
            "bundle_id": "BUNDLE-20260521-POSTCLOSE",
            "registry_dir": "artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE",
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
    assert report["model_positioning"]["role"] == "stable_paper_baseline"
    assert report["model_positioning"]["global_optimum_claim_allowed"] is False
    assert report["policies"][0]["policy_hash"]
    assert report["daily_cross_track_summary_template"]["comparison_basis"] == "normalized_nav_bps"
    assert (
        report["daily_cross_track_summary_template"]["model_positioning"]["fixed_for_5track_lab"]
        is True
    )


def test_policy_lab_rejects_non_baseline_bundle_or_registry() -> None:
    lab = _load_policy_lab_module()
    repo_root = Path("/Users/jangjaewon/Desktop/Elephant_Lab")
    registry_dir = repo_root / "artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE"

    with pytest.raises(ValueError, match="fixed to stable baseline"):
        lab._require_fixed_stable_baseline(
            repo_root=repo_root,
            bundle_id="BUNDLE-OTHER",
            registry_dir=registry_dir,
        )
    with pytest.raises(ValueError, match="fixed to registry_dir"):
        lab._require_fixed_stable_baseline(
            repo_root=repo_root,
            bundle_id="BUNDLE-20260521-POSTCLOSE",
            registry_dir=repo_root / "artifacts/lgbm_paper_candidate/BUNDLE-OTHER",
        )


def test_policy_lab_evidence_requires_serving_feature_readiness_pass() -> None:
    lab = _load_policy_lab_module()
    evidence = {
        key: 1
        for key in lab._evidence_schema()["required_run_fields"]
    }
    evidence["serving_feature_readiness"] = {"status": "FAIL"}
    evidence["zero_order_false_pass"] = False
    blockers = lab._evidence_blockers(evidence)
    assert any(b["reason"] == "serving_feature_readiness_not_pass" for b in blockers)


def test_policy_lab_failure_card_includes_pass_with_evidence_blockers(tmp_path) -> None:
    lab = _load_policy_lab_module()
    child = {
        "result": {
            "status": "PASS",
            "evidence_summary": {
                "submitted_count": 2,
            },
        }
    }
    (tmp_path / "MAIN_BASELINE.json").write_text(
        json.dumps(child, ensure_ascii=False),
        encoding="utf-8",
    )
    cards = lab._failure_case_cards(
        tmp_path,
        [
            {
                "policy_id": "MAIN_BASELINE",
                "child_result_status": "PASS",
                "returncode": 0,
                "report_path": str(tmp_path / "MAIN_BASELINE.json"),
                "evidence_blockers": [{"reason": "zero_order_false_pass"}],
            }
        ],
    )
    assert cards
    assert cards[0]["fail_closed"] is True
    assert cards[0]["evidence_blockers"][0]["reason"] == "zero_order_false_pass"
    assert cards[0]["broker_order_submitted"] is True


def test_policy_lab_cross_track_summary_aggregates_child_evidence() -> None:
    lab = _load_policy_lab_module()
    policies = [
        lab.PolicySpec(
            policy_id="MAIN_BASELINE",
            label="Main",
            mode="paper",
            profile_prefix="KIS_MAIN",
            description="",
            launch_delay_sec=0,
            overrides={},
        ),
        lab.PolicySpec(
            policy_id="STRICT_GATE",
            label="Strict",
            mode="shadow",
            profile_prefix="KIS_STRICT",
            description="",
            launch_delay_sec=40,
            overrides={},
        ),
    ]
    completed = [
        {
            "policy_id": "MAIN_BASELINE",
            "evidence_summary": {
                "status": "PASS",
                "cycles_completed": 10,
                "score_nonempty_cycles": 9,
                "rankable_score_cycles": 9,
                "order_delta_count": 3,
                "submitted_count": 2,
                "fill_count": 1,
                "reject_count": 1,
                "matched_order_history_count": 2,
                "fail_closed_count": 0,
                "turnover_bps": 12.5,
                "daily_return_bps": 1.2,
                "serving_feature_readiness": {"status": "PASS"},
                "zero_order_false_pass": False,
            },
        },
        {
            "policy_id": "STRICT_GATE",
            "evidence_summary": {
                "status": "BLOCKED",
                "cycles_completed": 1,
                "score_nonempty_cycles": 0,
                "rankable_score_cycles": 0,
                "order_delta_count": 0,
                "submitted_count": 0,
                "fill_count": 0,
                "reject_count": 0,
                "matched_order_history_count": 0,
                "fail_closed_count": 1,
                "turnover_bps": 0.0,
                "daily_return_bps": None,
                "serving_feature_readiness": {"status": "FAIL"},
                "zero_order_false_pass": True,
            },
        },
    ]

    summary = lab._cross_track_summary(
        policies,
        run_id="run",
        bundle_id="BUNDLE-20260521-POSTCLOSE",
        tickers=["005930"],
        run_config_hash="hash",
        completed=completed,
    )

    assert summary["paper_track_count"] == 1
    assert summary["shadow_track_count"] == 1
    assert summary["aggregate"]["submitted_count"] == 2
    assert summary["aggregate"]["fail_closed_count"] == 1
    assert summary["aggregate"]["zero_order_false_pass_count"] == 1
    assert summary["track_evidence"][0]["serving_feature_readiness_status"] == "PASS"


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
