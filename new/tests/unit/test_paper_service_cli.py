from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paper_service_rehearsal  # noqa: E402
import paper_trading_smoke  # noqa: E402
import collect_kis_paper_evidence  # noqa: E402
import paper_liquidate_positions  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def _probe_pass_report() -> dict:
    return {
        "action": "submit_probe_order",
        "status": "PASS",
        "generated_at": datetime.now(_KST).isoformat(),
        "runtime": {"kis_mode": "virtual", "live_enabled": False},
        "evidence": {"broker_env_fingerprint": "fp-test"},
        "stages": {
            "execution": {
                "status": "PASS",
                "result": {
                    "execution_report": {
                        "fills": [{"broker_order_id": "OD-1"}],
                    },
                },
            },
            "order_history": {
                "status": "PASS",
                "matched_order_count": 1,
                "_mode": "virtual",
            },
        },
    }


def _load_one_liquidate_summary(tmp_path: Path) -> dict:
    summaries = list(tmp_path.glob("paper_liquidate_positions_*.json"))
    assert len(summaries) == 1
    return json.loads(summaries[0].read_text(encoding="utf-8"))


def test_paper_trading_smoke_can_assume_empty_system_positions() -> None:
    assert paper_trading_smoke._load_system_positions(  # noqa: SLF001
        None,
        assume_empty=True,
    ) == []


def test_paper_liquidate_positions_builds_one_share_sell_plan() -> None:
    plan = paper_liquidate_positions._sell_plan(  # noqa: SLF001
        [
            {"ticker": "005930", "available_qty": 2, "current_price": 70000},
            {"ticker": "42660", "qty": 1, "current_price": 112000},
            {"ticker": "bad", "available_qty": 9, "current_price": 10},
        ],
        chunk_qty=1,
    )

    assert [row["ticker"] for row in plan] == ["005930", "005930", "042660"]
    assert [row["qty"] for row in plan] == [1, 1, 1]


def test_paper_liquidate_positions_dry_run_writes_summary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeRunner:
        def run_balance_reconciliation(self, write_report=True):
            return {
                "status": "PASS",
                "stages": {
                    "balance": {
                        "positions": [
                            {
                                "ticker": "005930",
                                "available_qty": 1,
                                "current_price": 70000,
                            }
                        ]
                    }
                },
            }

    monkeypatch.setattr(paper_liquidate_positions, "PaperTradingRunner", FakeRunner)

    rc = paper_liquidate_positions.main([
        "--confirm-phrase",
        "PAPER_ORDER_OK",
        "--dry-run",
        "--output-dir",
        str(tmp_path),
    ])

    assert rc == 0
    summaries = list(tmp_path.glob("paper_liquidate_positions_*.json"))
    assert len(summaries) == 1


def test_paper_liquidate_positions_actual_submission_records_summary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    submitted: list[dict] = []

    class FakeRunner:
        def _client_mode(self):
            return "virtual"

        def run_balance_reconciliation(self, write_report=True):
            if submitted:
                return {"status": "PASS", "stages": {"balance": {"positions": []}}}
            return {
                "status": "PASS",
                "stages": {
                    "balance": {
                        "positions": [
                            {
                                "ticker": "005930",
                                "available_qty": 1,
                                "current_price": 70000,
                            }
                        ]
                    }
                },
            }

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            submitted.append({"ticker": ticker, "side": side, "qty": qty})
            return {"status": "PASS", "report_path": "artifacts/reports/order.json"}

    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setattr(paper_liquidate_positions, "PaperTradingRunner", FakeRunner)

    rc = paper_liquidate_positions.main([
        "--confirm-phrase",
        "PAPER_ORDER_OK",
        "--output-dir",
        str(tmp_path),
    ])

    assert rc == 0
    assert submitted == [{"ticker": "005930", "side": "sell", "qty": 1}]
    summary = _load_one_liquidate_summary(tmp_path)
    assert summary["status"] == "PASS"
    assert summary["submitted_order_count"] == 1
    assert summary["failure_count"] == 0
    assert summary["final_position_count"] == 0


def test_paper_liquidate_positions_blocks_when_balance_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeRunner:
        def _client_mode(self):
            return "virtual"

        def run_balance_reconciliation(self, write_report=True):
            return {"status": "FAIL", "reason": "broker_down"}

    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setattr(paper_liquidate_positions, "PaperTradingRunner", FakeRunner)

    rc = paper_liquidate_positions.main([
        "--confirm-phrase",
        "PAPER_ORDER_OK",
        "--output-dir",
        str(tmp_path),
    ])

    assert rc == 1
    summary = _load_one_liquidate_summary(tmp_path)
    assert summary["status"] == "BLOCKED"
    assert summary["reason"] == "balance_reconciliation_not_pass"


def test_paper_liquidate_positions_records_submit_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = {"balance": 0}

    class FakeRunner:
        def _client_mode(self):
            return "virtual"

        def run_balance_reconciliation(self, write_report=True):
            calls["balance"] += 1
            positions = [
                {"ticker": "005930", "available_qty": 1, "current_price": 70000}
            ] if calls["balance"] == 1 else []
            return {"status": "PASS", "stages": {"balance": {"positions": positions}}}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            return {"status": "FAIL", "report_path": "artifacts/reports/order.json"}

    monkeypatch.setenv("KIS_MODE", "virtual")
    monkeypatch.setattr(paper_liquidate_positions, "PaperTradingRunner", FakeRunner)

    rc = paper_liquidate_positions.main([
        "--confirm-phrase",
        "PAPER_ORDER_OK",
        "--output-dir",
        str(tmp_path),
    ])

    assert rc == 1
    summary = _load_one_liquidate_summary(tmp_path)
    assert summary["status"] == "BLOCKED"
    assert summary["submitted_order_count"] == 1
    assert summary["failure_count"] == 1


def test_paper_liquidate_positions_rejects_non_virtual_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeRunner:
        pass

    monkeypatch.setenv("KIS_MODE", "real")
    monkeypatch.setattr(paper_liquidate_positions, "PaperTradingRunner", FakeRunner)

    rc = paper_liquidate_positions.main([
        "--confirm-phrase",
        "PAPER_ORDER_OK",
        "--output-dir",
        str(tmp_path),
    ])

    assert rc == 1
    summary = _load_one_liquidate_summary(tmp_path)
    assert summary["status"] == "BLOCKED"
    assert summary["reason"] == "kis_virtual_mode_required"


def test_collect_kis_paper_evidence_loads_system_positions_json(tmp_path: Path) -> None:
    positions_path = tmp_path / "positions.json"
    positions_path.write_text(
        '{"positions": [{"ticker": "005930", "qty": 74}]}',
        encoding="utf-8",
    )

    assert collect_kis_paper_evidence._load_system_positions(  # noqa: SLF001
        str(positions_path),
    ) == [{"ticker": "005930", "qty": 74}]


def test_collect_kis_paper_evidence_rejects_ambiguous_system_positions(tmp_path: Path) -> None:
    positions_path = tmp_path / "positions.json"
    positions_path.write_text("[]", encoding="utf-8")

    try:
        collect_kis_paper_evidence._load_system_positions(  # noqa: SLF001
            str(positions_path),
            assume_empty=True,
        )
    except ValueError as exc:
        assert "mutually exclusive" in str(exc)
    else:
        raise AssertionError("expected mutually exclusive ValueError")


def test_collect_kis_paper_evidence_derives_registry_dir_from_bundle(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            return _probe_pass_report()

    def fake_service_rehearsal(args):
        calls["registry_dir"] = args.registry_dir
        return {"status": "PASS"}

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        fake_service_rehearsal,
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=True,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="",
            cold_risk_report="",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "PASS"
    assert report["registry_dir"] == "artifacts/lgbm_paper_candidate/BUNDLE-TEST"
    assert calls["registry_dir"] == "artifacts/lgbm_paper_candidate/BUNDLE-TEST"


def test_collect_kis_paper_evidence_forwards_cold_risk_report(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            return _probe_pass_report()

    def fake_service_rehearsal(args):
        calls["cold_risk_report"] = args.cold_risk_report
        calls["tickers"] = args.tickers
        calls["bundle_id"] = args.bundle_id
        return {"status": "PASS"}

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        fake_service_rehearsal,
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930,000660",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="artifacts/reports/community_live_risk/example.json",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "PASS"
    assert calls["cold_risk_report"] == "artifacts/reports/community_live_risk/example.json"
    assert calls["tickers"] == "005930,000660"
    assert calls["bundle_id"] == "BUNDLE-TEST"


def test_collect_kis_paper_evidence_skips_probe_when_order_path_is_fresh(
    monkeypatch,
) -> None:
    calls: dict[str, int] = {"probe": 0, "service": 0}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            calls["probe"] += 1
            raise AssertionError("probe should be skipped when order-path evidence is fresh")

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence,
        "find_fresh_paper_order_path_evidence",
        lambda **kwargs: {
            "status": "PASS",
            "evidence_type": "paper_auto_order",
            "report_path": "artifacts/reports/paper_auto_trading/MAIN/report.json",
            "matched_order_count": 1,
        },
    )
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        lambda args: calls.__setitem__("service", calls["service"] + 1)
        or {"status": "PASS"},
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            probe_mode="if-no-fresh-order-evidence",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "PASS"
    assert calls == {"probe": 0, "service": 1}
    assert report["probe_policy"]["probe_submitted"] is False
    assert report["probe_policy"]["skip_reason"] == "fresh_paper_order_path_evidence_found"
    assert report["stage_statuses"]["probe_order"] == "SKIP"
    assert report["stage_statuses"]["paper_order_path"] == "PASS"


def test_collect_kis_paper_evidence_probe_mode_never_blocks_without_order_path(
    monkeypatch,
) -> None:
    calls: dict[str, int] = {"probe": 0, "service": 0}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            calls["probe"] += 1
            raise AssertionError("probe-mode never must not submit a probe")

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence,
        "find_fresh_paper_order_path_evidence",
        lambda **kwargs: {
            "status": "BLOCKED",
            "reason": "paper_order_path_evidence_missing",
            "matched_order_count": 0,
        },
    )
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        lambda args: calls.__setitem__("service", calls["service"] + 1)
        or {"status": "PASS"},
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            probe_mode="never",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "BLOCKED"
    assert calls == {"probe": 0, "service": 0}
    assert report["probe_policy"]["probe_submitted"] is False
    assert (
        report["probe_policy"]["skip_reason"]
        == "probe_mode_never_order_path_evidence_missing"
    )
    assert report["stage_statuses"]["probe_order"] == "SKIP"
    assert report["stage_statuses"]["paper_order_path"] == "BLOCKED"
    assert report["stage_statuses"]["paper_auto_service_rehearsal"] == "SKIP"
    assert "paper_order_path" in report["blockers"]


def test_collect_kis_paper_evidence_converts_service_exception_to_blocked(
    monkeypatch,
) -> None:
    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            return _probe_pass_report()

    def fail_service_rehearsal(args):
        raise ConnectionError("dns failed")

    monkeypatch.setattr(collect_kis_paper_evidence, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        collect_kis_paper_evidence.paper_auto_service_rehearsal,
        "build_report",
        fail_service_rehearsal,
    )

    report = collect_kis_paper_evidence.collect(
        argparse.Namespace(
            system_positions_json=None,
            assume_empty_system_positions=False,
            price=70000.0,
            auto_price=False,
            order_type="00",
            ticker="005930",
            side="buy",
            qty=1,
            probe_confirm_phrase="PAPER_ORDER_OK",
            auto_confirm_phrase="PAPER_AUTO_OK",
            tickers="005930",
            cycles=1,
            interval_sec=0.0,
            registry_dir="artifacts/lgbm_paper_candidate/BUNDLE-TEST",
            cold_risk_report="artifacts/reports/community_live_risk/example.json",
            no_write_report=True,
            use_real_hot_runner=False,
            bundle_id="BUNDLE-TEST",
        )
    )

    assert report["status"] == "BLOCKED"
    assert report["stage_statuses"]["paper_auto_service_rehearsal"] == "BLOCKED"
    service = report["stages"]["paper_auto_service_rehearsal"]
    assert service["blockers"] == ["paper_auto_service_rehearsal_exception"]
    assert service["stages"]["paper_auto_cycle"]["exception_type"] == "ConnectionError"
    assert service["stages"]["paper_auto_cycle"]["fail_closed"] is True


def test_paper_service_rehearsal_auto_price_and_empty_reconciliation(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeRunner:
        def run_balance_reconciliation(self, system_positions=None, write_report=True):
            calls["system_positions"] = system_positions
            calls["balance_write_report"] = write_report
            return {"status": "PASS"}

        def submit_probe_order(
            self,
            ticker,
            side,
            qty,
            price,
            order_type,
            confirm_phrase,
            write_report=True,
        ):
            calls["probe_price"] = price
            calls["probe_write_report"] = write_report
            return {
                "status": "PASS",
                "stages": {
                    "execution": {
                        "result": {
                            "execution_report": {
                                "fills": [{"broker_order_id": "OD-1"}],
                            },
                        },
                    },
                },
            }

        def run_order_history(
            self,
            ticker,
            side,
            order_id,
            execution_filter,
            write_report=True,
        ):
            calls["order_id"] = order_id
            return {"status": "PASS"}

    monkeypatch.setattr(paper_service_rehearsal, "PaperTradingRunner", FakeRunner)
    monkeypatch.setattr(
        paper_service_rehearsal.print_env_readiness,
        "build_report",
        lambda: {"status": "PASS"},
    )
    monkeypatch.setattr(paper_service_rehearsal, "_auto_price", lambda ticker: 71000.0)

    report = paper_service_rehearsal.build_report(
        argparse.Namespace(
            include_probe=True,
            system_positions_json=None,
            assume_empty_system_positions=True,
            ticker="005930",
            side="buy",
            qty=1,
            price=None,
            auto_price=True,
            order_type="00",
            execution_filter="all",
            confirm_phrase="PAPER_ORDER_OK",
        )
    )

    assert report["status"] == "PASS"
    assert report["params"]["price"] == 71000.0
    assert report["params"]["price_source"] == "kis_current_price"
    assert report["params"]["system_positions_source"] == "assume_empty"
    assert calls["system_positions"] == []
    assert calls["probe_price"] == 71000.0
    assert calls["order_id"] == "OD-1"
