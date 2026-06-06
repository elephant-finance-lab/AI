from __future__ import annotations

import json
from pathlib import Path

from src.ops import paper_service_bundle


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_repo(tmp_path: Path, *, active_version=None) -> Path:
    root = tmp_path
    config = root / "new" / "config"
    config.mkdir(parents=True, exist_ok=True)
    tickers = [f"{idx:06d}" for idx in range(1, 31)]
    stock_lines = "\n".join(
        f'      - {{ ticker: "{ticker}", name: "T{idx}", status: "active" }}'
        for idx, ticker in enumerate(tickers, start=1)
    )
    (config / "universe_config.yaml").write_text(
        "\n".join(
            [
                'version: "test"',
                "active_stock_count: 30",
                "sectors:",
                "  test_sector:",
                '    status: "confirmed"',
                "    stocks:",
                stock_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )
    (config / "paper_service_schedule.yaml").write_text(
        "\n".join(
            [
                "timezone: Asia/Seoul",
                "mode: selected-paper-service",
                "safety:",
                "  live_trading_allowed: false",
                "  safe_to_enable_live_actions: false",
                "  registry_mutated: false",
                "  production_active_version: null",
                "bundle:",
                "  primary: BUNDLE-TEST",
                "  fallback: BUNDLE-FALLBACK",
                "  allow_auto_switch: false",
                "universe:",
                "  expected_count: 30",
                '  tickers_arg: ""',
                "  max_tickers: 30",
                "execution_policy:",
                "  recommendation_universe: active_universe_30t",
                "  default_target_scope: selected_tickers",
                "  selected_tickers_required_for_user_start: true",
                "  max_selected_tickers: 10",
                "  user_selected_tickers_trade_only: true",
                "  empty_tickers_meaning: active_universe_30t_system_default",
                "preopen:",
                "  - id: service_readiness",
                '    time: "08:30"',
                "market:",
                "  - id: recommendation_refresh",
                '    window: "09:00-15:20"',
                "postmarket: []",
                "mode_b: []",
                "closeout: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_json(
        root / "artifacts" / "lgbm" / "registry.json",
        {"active_version": active_version, "versions": []},
    )
    registry_dir = root / "artifacts" / "lgbm_paper_candidate" / "BUNDLE-TEST"
    (registry_dir / "v1.pkl").parent.mkdir(parents=True)
    (registry_dir / "v1.pkl").write_bytes(b"model")
    metadata = {
        "bundle_id": "BUNDLE-TEST",
        "feature_cols": ["f1", "f2"],
        "feature_manifest": {"feature_cols": ["f1", "f2"]},
    }
    _write_json(registry_dir / "v1_metadata.json", metadata)
    _write_json(
        registry_dir / "registry.json",
        {
            "paper_only_registry": True,
            "live_trading_allowed": False,
            "production_registry_mutated": False,
            "active_version": "v1",
            "versions": [
                {
                    "version": "v1",
                    "bundle_id": "BUNDLE-TEST",
                    "model_path": "artifacts/lgbm_paper_candidate/BUNDLE-TEST/v1.pkl",
                    "metadata_path": (
                        "artifacts/lgbm_paper_candidate/BUNDLE-TEST/v1_metadata.json"
                    ),
                    "feature_cols": ["f1", "f2"],
                    "feature_manifest": {"feature_cols": ["f1", "f2"]},
                    "live_trading_allowed": False,
                }
            ],
        },
    )
    _write_json(
        root
        / "artifacts"
        / "reports"
        / "recommendation_refresh"
        / "recommendations_test.json",
        {
            "status": "PASS",
            "bundle_id": "BUNDLE-TEST",
            "generated_at": "2026-06-05T09:00:00+09:00",
            "recommendations": [{"ticker": "000001"} for _ in range(10)],
        },
    )
    return root


def _service_readiness(*, live=False) -> dict:
    return {
        "ssot_readiness": "PASS",
        "broker_evidence": "PASS",
        "deploy_quality": "PASS",
        "live_trading_allowed": live,
        "registry_mutated": False,
        "be_contract": {
            "safe_to_enable_order_actions": True,
            "safe_to_enable_live_actions": False,
        },
    }


def test_schedule_and_universe_are_loaded(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)

    schedule = paper_service_bundle.load_schedule(repo_root=root)
    universe = paper_service_bundle.resolve_active_universe(repo_root=root, max_tickers=30)

    assert schedule["mode"] == "selected-paper-service"
    assert schedule["safety"]["live_trading_allowed"] is False
    assert universe["resolved_ticker_count"] == 30
    assert len(universe["universe_config_hash"]) == 64


def test_paper_service_bundle_report_passes_for_30t_demo(monkeypatch, tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(),
    )

    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        mode="paper-service-30t",
        tickers_arg="",
        max_tickers=30,
    )

    assert report["status"] == "PASS"
    assert report["universe"]["resolved_ticker_count"] == 30
    assert report["execution_target"]["target_scope"] == "active_universe_30t"
    assert report["execution_target"]["execution_ticker_count"] == 30
    assert report["recommendations"]["recommendation_count"] == 10
    assert report["safety"]["live_trading_allowed"] is False
    assert report["safety"]["active_version"] is None
    assert report["safety"]["real_order_enabled"] is False
    assert report["safety"]["live_order_enabled"] is False
    assert report["safety"]["require_kis_virtual"] is True
    assert report["be_runtime_hint"]["AI_PAPER_BUNDLE_ID"] == "BUNDLE-TEST"


def test_paper_service_bundle_blocks_if_schedule_allows_real_orders(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = _write_repo(tmp_path)
    schedule_path = root / "new" / "config" / "paper_service_schedule.yaml"
    schedule_path.write_text(
        schedule_path.read_text(encoding="utf-8").replace(
            "  production_active_version: null",
            "  production_active_version: null\n  allow_real_order: true",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(),
    )

    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        mode="paper-service-30t",
        tickers_arg="",
        max_tickers=30,
    )

    assert report["status"] == "BLOCKED"
    assert "schedule_allow_real_order_true" in report["blockers"]


def test_paper_service_bundle_blocks_if_schedule_drops_virtual_requirement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = _write_repo(tmp_path)
    schedule_path = root / "new" / "config" / "paper_service_schedule.yaml"
    schedule_path.write_text(
        schedule_path.read_text(encoding="utf-8").replace(
            "  production_active_version: null",
            "  production_active_version: null\n  require_kis_virtual: false",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(),
    )

    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        mode="paper-service-30t",
        tickers_arg="",
        max_tickers=30,
    )

    assert report["status"] == "BLOCKED"
    assert "schedule_require_kis_virtual_false" in report["blockers"]


def test_paper_service_bundle_blocks_explicit_tickers_for_30t_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = _write_repo(tmp_path)
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(),
    )

    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        mode="paper-service-30t",
        tickers_arg="000001,000002,000003,000004,000005",
        max_tickers=30,
    )

    assert report["status"] == "BLOCKED"
    assert "explicit_tickers_not_allowed_for_30t_paper_service" in report["blockers"]


def test_paper_service_bundle_default_selected_mode_requires_tickers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = _write_repo(tmp_path)
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(),
    )

    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        tickers_arg="",
        max_tickers=30,
    )

    assert report["status"] == "BLOCKED"
    assert report["execution_target"]["target_scope"] == "selected_tickers"
    assert "selected_tickers_required" in report["blockers"]


def test_paper_service_bundle_accepts_selected_ticker_mode(monkeypatch, tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(),
    )

    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        mode="selected-paper-service",
        tickers_arg="000001",
        max_tickers=30,
    )

    assert report["status"] == "PASS"
    assert report["universe"]["resolved_ticker_count"] == 30
    assert report["execution_target"]["target_scope"] == "selected_tickers"
    assert report["execution_target"]["execution_tickers"] == ["000001"]
    assert report["execution_target"]["execution_ticker_count"] == 1
    assert report["be_runtime_hint"]["PAPER_SERVICE_TARGET_SCOPE"] == "selected_tickers"
    assert report["be_runtime_hint"]["AI_PAPER_SELECTED_TICKERS"] == "000001"


def test_paper_service_bundle_blocks_selected_ticker_outside_active_universe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = _write_repo(tmp_path)
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(),
    )

    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        mode="selected-paper-service",
        tickers_arg="999999",
        max_tickers=30,
    )

    assert report["status"] == "BLOCKED"
    assert "selected_ticker_not_active_universe" in report["blockers"]


def test_paper_service_bundle_blocks_live_readiness(monkeypatch, tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(live=True),
    )

    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
    )

    assert report["status"] == "BLOCKED"
    assert "live_trading_allowed_true" in report["blockers"]


def test_write_report_outputs_json_and_markdown(monkeypatch, tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    monkeypatch.setattr(
        paper_service_bundle,
        "build_service_status",
        lambda **kwargs: _service_readiness(),
    )
    report = paper_service_bundle.build_paper_service_bundle_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        tickers_arg="000001",
    )

    json_path, md_path = paper_service_bundle.write_report(report, repo_root=root)

    assert json_path.exists()
    assert md_path.exists()
    written = json.loads(json_path.read_text(encoding="utf-8"))
    assert written["status"] == "PASS"
    assert "Paper Service Bundle" in md_path.read_text(encoding="utf-8")
