from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "serving_feature_readiness.py"
    )
    spec = importlib.util.spec_from_file_location("serving_feature_readiness", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeQuant:
    model_metadata = {
        "version": "BUNDLE-20260521-POSTCLOSE",
        "bundle_id": "BUNDLE-20260521-POSTCLOSE",
    }

    def serving_feature_readiness(self, tickers, asof):
        return {
            "status": "PASS",
            "artifact_date": "20260526",
            "checked_tickers": tickers,
            "required_dual_source_cols": ["news_score_t"],
            "loader_configured": True,
            "asof": asof,
        }


class _BlockedQuant(_FakeQuant):
    def serving_feature_readiness(self, tickers, asof):
        return {
            "status": "FAIL",
            "reason": "required_dual_source_feature_missing",
            "required_dual_source_cols": ["news_score_t"],
        }


def test_serving_feature_readiness_build_report_passes_with_matching_bundle() -> None:
    mod = _load_module()

    report = mod.build_report(
        bundle_id="BUNDLE-20260521-POSTCLOSE",
        registry_dir="",
        tickers=["5930", "000660"],
        asof="2026-05-26T08:30:00+09:00",
        quant_factory=_FakeQuant,
    )

    assert report["status"] == "PASS"
    assert report["external_kis_api"] is False
    assert report["production_registry_mutated"] is False
    assert report["tickers"] == ["005930", "000660"]
    assert report["required_artifacts"] == ["artifacts/dual_source/20260526.json"]


def test_serving_feature_readiness_blocks_missing_feature_or_wrong_bundle() -> None:
    mod = _load_module()

    missing = mod.build_report(
        bundle_id="BUNDLE-20260521-POSTCLOSE",
        registry_dir="",
        tickers=["005930"],
        asof="2026-05-26T08:30:00+09:00",
        quant_factory=_BlockedQuant,
    )
    assert missing["status"] == "FAIL"
    assert missing["blockers"][0]["reason"] == "serving_feature_readiness_not_pass"

    mismatch = mod.build_report(
        bundle_id="BUNDLE-OTHER",
        registry_dir="",
        tickers=["005930"],
        asof="2026-05-26T08:30:00+09:00",
        quant_factory=_FakeQuant,
    )
    assert mismatch["status"] == "FAIL"
    assert any(
        blocker["reason"] == "bundle_id_mismatch_or_model_missing"
        for blocker in mismatch["blockers"]
    )
