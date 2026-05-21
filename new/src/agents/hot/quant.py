"""Hot Path Quant Agent (S1-1).

C1 MinuteBar 수신 → BarBuffer 저장 → cross-sectional ranking score 생성.
C5 publish: quant_signal / quant_alert / anomaly_detected.

불변 원칙:
  - Hot Path <100ms (동기 LLM 호출 금지)
  - 모든 수치는 risk_config.yaml quant_agent + preprocessor 섹션 로드 (하드코딩 금지)
  - 종목코드 pad_ticker (6자리 zero-padded)

아키텍처 포지션 (architecture.md L83, L373):
  "Quant Agent = 숫자 세계와 텍스트 세계의 다리. 숫자 이상 감지 시 에이전트를 깨운다."
  Risk Fast Agent와 역할 분리: 숫자 anomaly 전담 vs 커뮤니티/수급 rule 전담.

Feature 이름 규약:
  DatasetBuilder/LightGBM 훈련과 동일한 feat_ prefix 사용 (feat_1m_close_robust_z,
  feat_5m_ret, feat_30m_vol, feat_60m_trend). Preprocessor(hot path 1-shot용)는 다른 이름
  규약을 쓰므로 여기서 직접 rolling feature 계산 (S1-0 preprocessor와 중복 최소화).

Investor Flow side-channel:
  KIS/KRX 수급 이벤트는 모델 feature로 조용히 섞지 않고, 최신 snapshot만 보관한다.
  Cold Path/BE가 필요할 때 조회해 설명/이벤트 컨텍스트로 사용한다.
"""
from __future__ import annotations

import pickle
import time
from collections import deque
from datetime import datetime
import math
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np

from src.agents._base import AgentBase
from src.data.bar_buffer import BarBuffer
from src.models.registry import (
    ModelRegistry,
    PklLoadFailedError,
    RegistryCorruptedError,
    VersionNotFoundError,
)
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("quant_agent")
_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[4]


class QuantAgent(AgentBase):
    """Hot Path 퀀트 시그널 + 이상 탐지.

    사용 흐름 (Hot Runner 1분 루프):
        1. 매 bar 수신 시 `on_bar(bar)` → BarBuffer 저장 (가벼움)
        2. 매 분 cross-sectional 시점에 `score_cross_section(tickers, asof)` 호출
        3. 동시에 `detect_anomalies(tickers)` 호출 → Cold Path escalation 트리거
        4. 레이턴시 관찰: `latency_percentiles()`

    모델 미로드 시 passive mode. 학습 전에도 on_bar / anomaly 동작은 정상.
    """

    # QuantAgent publishes 3종 (api_contracts.md L53-67 publish_channels 중 QuantAgent 항목).
    # architecture.md L464 QuantAgent.publishes와 1:1 일치. AgentBase 2단계 검증(옵션 C).
    ALLOWED_PUBLISH_CHANNELS = frozenset(
        {"quant_signal", "quant_alert", "anomaly_detected"}
    )

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        bar_buffer: BarBuffer | None = None,
        dual_source_loader: Callable[[str | None], list[dict[str, Any]]] | None = None,
    ) -> None:
        # --- 설정 로드 (yaml 경유) ---
        qa_cfg = config_load("risk_config.yaml", "quant_agent")
        pp_cfg = config_load("risk_config.yaml", "preprocessor")

        self._warmup_bars: int = int(qa_cfg["warmup_bars"])
        self._anomaly_zscore_threshold: float = float(
            qa_cfg["anomaly_zscore_threshold"]
        )
        self._volume_zscore_threshold: float = float(
            qa_cfg["volume_zscore_threshold"]
        )
        self._latency_window: int = int(qa_cfg["latency_window"])
        self._latency_p95_target_ms: float = float(qa_cfg["latency_p95_target_ms"])
        self._investor_flow_stale_sec: int = int(qa_cfg["investor_flow_stale_sec"])

        self._multi_scale_windows: list[int] = list(pp_cfg["multi_scale_windows"])
        self._intraday_feature_windows: list[int] = [
            int(value)
            for value in pp_cfg.get("intraday_feature_windows", [])
            if int(value) > 0
        ]
        self._volume_z_windows: list[int] = [
            int(value)
            for value in pp_cfg.get("volume_z_windows", [])
            if int(value) > 0
        ]
        self._mad_constant: float = float(pp_cfg["mad_constant"])
        self._outlier_cap_z: float = float(pp_cfg["outlier_cap_z"])
        self._base_feature_cols: list[str] = list(pp_cfg["feature_cols"])
        self._dual_source_feature_cols: list[str] = []
        self._dual_source_defaults: dict[str, float] = {}
        ds_cfg = config_load("risk_config.yaml", "dual_source") or {}
        if ds_cfg.get("enabled_for_lgbm", False):
            self._dual_source_feature_cols = list(pp_cfg.get("dual_source_feature_cols", []))
            self._dual_source_defaults = {
                col: (1.0 if col == "community_noise_multiplier" else 0.0)
                for col in self._dual_source_feature_cols
            }
        self._exogenous_feature_cols: list[str] = []
        self._exogenous_defaults: dict[str, float] = {}
        exog_cfg = config_load("risk_config.yaml", "exogenous_features") or {}
        if exog_cfg.get("enabled_for_lgbm", False):
            self._exogenous_feature_cols = list(pp_cfg.get("exogenous_feature_cols", []))
            defaults = exog_cfg.get("neutral_defaults") or {}
            self._exogenous_defaults = {
                col: float(defaults.get(col, 0.0))
                for col in self._exogenous_feature_cols
            }
        self._feature_cols: list[str] = list(self._base_feature_cols)
        for col in self._dual_source_feature_cols:
            if col not in self._feature_cols:
                self._feature_cols.append(col)
        for col in self._exogenous_feature_cols:
            if col not in self._feature_cols:
                self._feature_cols.append(col)
        self._inference_feature_cols: list[str] = list(self._feature_cols)
        self._dual_source_loader = dual_source_loader
        self._dual_source_cache: dict[str, list[dict[str, Any]]] = {}
        self._investor_flow_snapshot: dict[str, dict[str, Any]] = {}
        self._exogenous_snapshot: dict[str, dict[str, float]] = {}
        self._exogenous_snapshot_meta: dict[str, dict[str, Any]] = {}
        self._market_exogenous_snapshot: dict[str, float] = {}
        self._market_exogenous_snapshot_meta: dict[str, Any] = {}

        # --- 의존 컴포넌트 ---
        self._registry = registry or ModelRegistry()
        self._bar_buffer = bar_buffer or BarBuffer()

        # --- 모델 로드 (실패 시 passive mode) ---
        self._booster: Any = None
        self._trade_classifier: Any = None
        self._model_metadata: dict[str, Any] | None = None
        self._try_load_model()

        # --- 레이턴시 측정 ---
        self._latency_records: deque[float] = deque(maxlen=self._latency_window)

        logger.info(
            "[quant_agent] 초기화 완료. model_loaded=%s, warmup=%d, "
            "anomaly_z=%.1f, latency_window=%d, investor_flow_stale_sec=%d",
            self.has_model, self._warmup_bars,
            self._anomaly_zscore_threshold, self._latency_window,
            self._investor_flow_stale_sec,
        )

    # ================================================================== #
    # Public properties
    # ================================================================== #

    @property
    def has_model(self) -> bool:
        """booster 로드 성공 여부."""
        return self._booster is not None

    @property
    def model_metadata(self) -> dict[str, Any] | None:
        """로드된 모델 메타데이터 (version, feature_cols, metrics, ...)."""
        return self._model_metadata

    def update_investor_flow_snapshot(
        self,
        ticker: str,
        flow: dict[str, Any] | float | int,
        received_at: datetime | str | None = None,
    ) -> None:
        """수급 side-channel snapshot 갱신.

        exogenous_features.enabled_for_lgbm=true이면 foreign/institutional/retail
        순매수 3필드가 LightGBM feature vector에도 주입된다. 그 외 수급 부가 필드는
        RiskFast/Cold Path context로 보관한다.
        """
        ticker_padded = pad_ticker(str(ticker))
        raw = flow if isinstance(flow, dict) else {"foreign_net_buy": flow}
        received_dt = self._parse_snapshot_dt(received_at) if received_at else datetime.now(_KST)
        self._investor_flow_snapshot[ticker_padded] = {
            "ticker": ticker_padded,
            "foreign_net_buy": self._float_or_none(raw.get("foreign_net_buy")),
            "institutional_net_buy": self._float_or_none(raw.get("institutional_net_buy")),
            "retail_net_buy": self._float_or_none(raw.get("retail_net_buy")),
            "foreign_net_buy_qty": self._float_or_none(raw.get("foreign_net_buy_qty")),
            "institutional_net_buy_qty": self._float_or_none(
                raw.get("institutional_net_buy_qty")
            ),
            "retail_net_buy_qty": self._float_or_none(raw.get("retail_net_buy_qty")),
            "provider": str(raw.get("provider") or raw.get("source") or "unknown"),
            "received_at": received_dt,
        }

    def update_foreign_snapshot(
        self,
        ticker: str,
        foreign_net_buy: float,
        received_at: datetime | str | None = None,
    ) -> None:
        """이전 예경님 코드 호환용 alias. 내부적으로 side-channel snapshot을 갱신한다."""
        self.update_investor_flow_snapshot(
            ticker,
            {"foreign_net_buy": foreign_net_buy, "provider": "foreign_snapshot"},
            received_at=received_at,
        )

    def update_exogenous_snapshot(
        self,
        features: dict[str, Any],
        ticker: str | None = None,
        received_at: datetime | str | None = None,
        max_age_sec: int | float | None = None,
    ) -> None:
        """US overnight / macro / investor_flow 외생 feature snapshot 갱신."""
        clean = {
            col: float(features[col])
            for col in self._exogenous_feature_cols
            if col in features and features[col] is not None
        }
        meta: dict[str, Any] = {
            "received_at": (
                self._parse_snapshot_dt(received_at)
                if received_at is not None
                else datetime.now(_KST)
            ),
            "max_age_sec": (
                float(max_age_sec)
                if max_age_sec is not None
                else float(self._investor_flow_stale_sec)
            ),
        }
        if ticker:
            ticker_padded = pad_ticker(str(ticker))
            self._exogenous_snapshot[ticker_padded] = clean
            self._exogenous_snapshot_meta[ticker_padded] = meta
        else:
            self._market_exogenous_snapshot.update(clean)
            self._market_exogenous_snapshot_meta = meta

    def get_investor_flow_snapshot(
        self,
        ticker: str,
        asof: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        """ticker별 최신 수급 snapshot + age/sec stale 여부 반환."""
        ticker_padded = pad_ticker(str(ticker))
        item = self._investor_flow_snapshot.get(ticker_padded)
        if item is None:
            return None
        asof_dt = self._parse_snapshot_dt(asof) if asof else datetime.now(_KST)
        received_dt = item["received_at"]
        raw_age_sec = (asof_dt - received_dt).total_seconds()
        is_future = raw_age_sec < 0
        age_sec = max(0.0, raw_age_sec)
        out = dict(item)
        out["received_at"] = received_dt.isoformat()
        out["age_sec"] = float(age_sec)
        out["is_future"] = bool(is_future)
        out["is_stale"] = bool(is_future or age_sec > self._investor_flow_stale_sec)
        return out

    # ================================================================== #
    # Public API
    # ================================================================== #

    def on_bar(self, bar: dict[str, Any]) -> None:
        """단일 1분봉 수신 → BarBuffer.push.

        BarBuffer가 필수 필드 검증 수행 (누락 시 ValueError 전파).
        Hot Path 최경량 (<1ms).
        """
        self._bar_buffer.push(bar)

    def score_cross_section(
        self,
        tickers: list[str],
        asof: str,
    ) -> dict[str, Any]:
        """Cross-sectional ranking score 생성.

        매 분 Hot Runner가 호출. 20 종목 한꺼번에 처리.
        warmup 부족 종목은 자동 제외.
        booster 없으면 passive mode (scores 비어있음, mode=passive).

        Returns:
            {
              "tickers": [padded ticker list, valid만],
              "scores": {ticker: ranking_score},
              "ts": asof,
              "mode": "active" | "passive" | "warmup",
              "latency_ms": float,
              "n_tickers": int,
            }
        """
        t0 = time.perf_counter()
        padded_all = [pad_ticker(str(t)) for t in tickers]
        asof_str = str(asof or "").strip()
        if not asof_str:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._latency_records.append(elapsed_ms)
            return {
                "tickers": [],
                "scores": {},
                "ts": "",
                "mode": "blocked",
                "blocker": "asof_required",
                "latency_ms": elapsed_ms,
                "n_tickers": 0,
            }

        if not self.has_model:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._latency_records.append(elapsed_ms)
            return {
                "tickers": [],
                "scores": {},
                "ts": asof_str,
                "mode": "passive",
                "latency_ms": elapsed_ms,
                "n_tickers": 0,
            }

        # BarBuffer batch load. asof 이후 bar가 buffer에 선적재되어도 추론에서 제외한다.
        max_window = self._feature_window_limit()
        bars_batch = self._bar_buffer.get_batch_asof(
            padded_all,
            max_window,
            asof=asof_str,
        )

        feature_matrix: list[list[float]] = []
        valid_tickers: list[str] = []

        requires_dual_source = self._model_requires_dual_source()

        for ticker in padded_all:
            bars = bars_batch.get(ticker, [])
            if len(bars) < self._warmup_bars:
                continue
            all_feats = self._compute_features(
                bars,
                ticker=ticker,
                asof=asof_str,
                require_dual_source=requires_dual_source,
            )
            if all_feats is None:
                continue
            try:
                feature_vec = [
                    float(all_feats[c]) for c in self._inference_feature_cols
                ]
            except KeyError as e:
                logger.warning(
                    "[quant_agent] %s feature 누락: %s. skip", ticker, e
                )
                continue
            feature_matrix.append(feature_vec)
            valid_tickers.append(ticker)

        if not valid_tickers:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._latency_records.append(elapsed_ms)
            return {
                "tickers": [],
                "scores": {},
                "ts": asof_str,
                "mode": "warmup",
                "latency_ms": elapsed_ms,
                "n_tickers": 0,
            }

        X = np.asarray(feature_matrix, dtype=float)
        try:
            preds = np.asarray(self._booster.predict(X), dtype=float)
        except Exception as e:
            logger.warning("[quant_agent] model predict 실패: %s", e)
            preds = np.asarray([], dtype=float)
        if len(preds) != len(valid_tickers) or not np.all(np.isfinite(preds)):
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._latency_records.append(elapsed_ms)
            logger.warning(
                "[quant_agent] model predict invalid: expected=%d actual=%d finite=%s",
                len(valid_tickers),
                len(preds),
                bool(np.all(np.isfinite(preds))) if len(preds) else False,
            )
            return {
                "tickers": [],
                "scores": {},
                "ts": asof_str,
                "mode": "warmup",
                "latency_ms": elapsed_ms,
                "n_tickers": 0,
                "error": "model_prediction_invalid",
            }
        scores = {t: float(s) for t, s in zip(valid_tickers, preds)}
        trade_probs, trade_classifier_error = self._predict_trade_probs(X, valid_tickers)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._latency_records.append(elapsed_ms)

        if elapsed_ms > self._latency_p95_target_ms:
            logger.warning(
                "[quant_agent] 레이턴시 초과: %.2fms > %.1fms SLA "
                "(tickers=%d)",
                elapsed_ms, self._latency_p95_target_ms, len(valid_tickers),
            )

        out = {
            "tickers": valid_tickers,
            "scores": scores,
            "ts": asof_str,
            "mode": "active",
            "latency_ms": elapsed_ms,
            "n_tickers": len(valid_tickers),
        }
        if trade_probs:
            out["trade_probs"] = trade_probs
        if trade_classifier_error:
            out["trade_classifier_error"] = trade_classifier_error
        return out

    def detect_anomalies(
        self,
        tickers: list[str],
        asof: str,
    ) -> list[dict[str, Any]]:
        """Cross-sectional anomaly 탐지.

        2종: intraday_drop (1min return z < -threshold), volume_spike (volume z > +threshold).
        한 ticker가 양쪽에 걸리면 두 entry 반환. architecture.md trigger_catalog 대응.

        Returns: list of {ticker, anomaly_type, z_score, ts}
          anomaly_type enum: "intraday_drop" | "volume_spike"
        """
        if not str(asof or "").strip():
            return []
        padded_all = [pad_ticker(str(t)) for t in tickers]
        max_window = self._feature_window_limit()
        out: list[dict[str, Any]] = []

        for ticker in padded_all:
            bars = self._bar_buffer.get_latest_asof(ticker, max_window, asof=asof)
            if len(bars) < self._warmup_bars:
                continue
            ts_close = bars[-1].get("ts_close", str(asof))
            is_drop, drop_z = self._is_intraday_drop_anomaly(bars)
            if is_drop:
                out.append({
                    "ticker": ticker,
                    "anomaly_type": "intraday_drop",
                    "z_score": drop_z,
                    "ts": ts_close,
                })
            is_spike, spike_z = self._is_volume_spike_anomaly(bars)
            if is_spike:
                out.append({
                    "ticker": ticker,
                    "anomaly_type": "volume_spike",
                    "z_score": spike_z,
                    "ts": ts_close,
                })
        return out

    def latency_percentiles(self) -> dict[str, float]:
        """p50 / p95 / p99 ms (최근 latency_window 기록 기준)."""
        if len(self._latency_records) == 0:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "n": 0}
        arr = np.asarray(list(self._latency_records), dtype=float)
        return {
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "n": int(arr.size),
        }

    def report(self, report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """QuantAgent 산출물 생성. publish channel 명 기준으로 검증.

        QuantAgent의 "report"는 publish_channels에 대응하는 구조화된 산출물.
        (C5 report_types 5종에 quant_signal만 포함되지만, quant_alert / anomaly_detected도
        같은 report() API로 처리한다. ALLOWED_PUBLISH_CHANNELS로 검증.)
        """
        if report_type not in self.ALLOWED_PUBLISH_CHANNELS:
            raise ValueError(
                f"QuantAgent.report: invalid report_type={report_type}. "
                f"허용: {sorted(self.ALLOWED_PUBLISH_CHANNELS)}"
            )
        return {
            "report_type": report_type,
            "agent": "QuantAgent",
            "payload": payload,
        }

    def reload_model(self) -> bool:
        """모델 재로드 (S3-6 야간 재학습 후 다음 날 아침 호출).

        Returns: 성공 True, 실패 False.
        """
        return self._try_load_model()

    # ================================================================== #
    # Internal: 모델 로드
    # ================================================================== #

    def _try_load_model(self) -> bool:
        try:
            booster, metadata = self._registry.load_latest()
        except (VersionNotFoundError, PklLoadFailedError, RegistryCorruptedError) as e:
            logger.warning(
                "[quant_agent] 모델 로드 실패 (%s). passive mode로 전환.", e
            )
            self._booster = None
            self._trade_classifier = None
            self._model_metadata = None
            self._inference_feature_cols = list(self._feature_cols)
            return False

        # Codex 권고 6 (2026-05-09): train/serve feature manifest 정합 검증.
        # 이전: metadata 의 feature_cols 를 단순 로드만 → train (Dual-Source 포함) vs serve
        # (preprocessor 만) feature set drift 방치. 발견 못 하면 silent prediction 오작동.
        # 현재: train feature_cols 와 serve feature_cols 비교 + diff 시 warning + passive mode 전환.
        train_feature_cols = set(metadata.get("feature_cols") or [])
        serve_feature_cols = set(self._feature_cols)
        missing_in_serve = train_feature_cols - serve_feature_cols
        extra_in_serve = serve_feature_cols - train_feature_cols
        if missing_in_serve or extra_in_serve:
            action = "passive mode 전환" if missing_in_serve else "metadata feature_cols로 추론"
            log_fn = logger.error if missing_in_serve else logger.warning
            log_fn(
                "[quant_agent] feature manifest mismatch: train→serve. "
                "missing_in_serve=%s extra_in_serve=%s. %s.",
                sorted(missing_in_serve), sorted(extra_in_serve), action,
            )
            # train 에 있지만 serve 에 없는 feature 존재 시 추론 불가 → passive
            if missing_in_serve:
                self._booster = None
                self._trade_classifier = None
                self._model_metadata = None
                self._inference_feature_cols = list(self._feature_cols)
                return False
            # serve 에 extra 만 있으면 추론 가능 (extra 무시) but warning

        self._booster = booster
        self._model_metadata = metadata
        self._inference_feature_cols = (
            list(metadata.get("feature_cols") or self._feature_cols)
        )
        self._trade_classifier = self._load_trade_classifier(metadata)
        logger.info(
            "[quant_agent] 모델 로드: version=%s, train_end=%s, feature_cols=%d (train↔serve 정합 OK)",
            metadata.get("version"), metadata.get("train_end"),
            len(metadata.get("feature_cols", [])),
        )
        return True

    def _load_trade_classifier(self, metadata: dict[str, Any]) -> Any | None:
        classifier = metadata.get("trade_no_trade_classifier")
        if not isinstance(classifier, dict) or classifier.get("status") != "PASS":
            return None
        raw_path = str(classifier.get("model_path") or "").strip()
        if not raw_path:
            return None
        path = Path(raw_path)
        if not path.is_absolute():
            path = _REPO_ROOT / path
        try:
            with path.open("rb") as fh:
                return pickle.load(fh)
        except Exception as e:
            logger.warning("[quant_agent] trade classifier load 실패: %s", e)
            return None

    def _predict_trade_probs(
        self,
        X: np.ndarray,
        tickers: list[str],
    ) -> tuple[dict[str, float], str | None]:
        if self._trade_classifier is None:
            return {}, None
        try:
            raw = np.asarray(self._trade_classifier.predict(X), dtype=float)
        except Exception as e:
            logger.warning("[quant_agent] trade classifier predict 실패: %s", e)
            return {}, "trade_classifier_predict_failed"
        if raw.ndim == 2 and raw.shape[1] >= 2:
            raw = raw[:, -1]
        raw = raw.reshape(-1)
        if len(raw) != len(tickers) or not np.all(np.isfinite(raw)):
            logger.warning(
                "[quant_agent] trade classifier output invalid: expected=%d actual=%d",
                len(tickers),
                len(raw),
            )
            return {}, "trade_classifier_prediction_invalid"
        return {
            ticker: float(np.clip(prob, 0.0, 1.0))
            for ticker, prob in zip(tickers, raw)
        }, None

    # ================================================================== #
    # Internal: Feature 계산 (DatasetBuilder rolling feature 동일 규약)
    # ================================================================== #

    def _compute_features(
        self,
        bars: list[dict[str, Any]],
        ticker: str | None = None,
        asof: str | None = None,
        require_dual_source: bool = False,
    ) -> dict[str, float] | None:
        """단일 ticker 최근 60 bars → LightGBM 추론 피처 dict.

        DatasetBuilder._compute_rolling_features와 동일한 수식.
        단건 계산 목적이므로 numpy only (pandas 미사용).

        PIT-Safety 경계: feat_1m_close_robust_z와 feat_60m_trend는 현재 bar(t) 포함
        block으로 median/slope 계산. 자기 참조 편향 있으나 DatasetBuilder 훈련과
        동일한 규약을 유지해야 feature distribution mismatch 없음 (설계 의도).
        """
        if len(bars) < self._warmup_bars:
            return None

        usable_bars = [b for b in bars if "close" in b]
        close_values = [float(b["close"]) for b in usable_bars]
        n_missing = len(bars) - len(usable_bars)
        if n_missing > 0:
            logger.warning(
                "[quant_agent] %d개 bar에 'close' 필드 누락. 필터 후 n=%d",
                n_missing, len(close_values),
            )
        closes = np.array(close_values, dtype=float)
        if len(closes) < self._warmup_bars:
            return None
        highs = np.array(
            [float(b.get("high", b["close"])) for b in usable_bars],
            dtype=float,
        )
        lows = np.array(
            [float(b.get("low", b["close"])) for b in usable_bars],
            dtype=float,
        )
        volumes = np.array(
            [float(b.get("volume", 0.0)) for b in usable_bars],
            dtype=float,
        )

        _, w5, w30, w60 = self._multi_scale_windows
        last = float(closes[-1])

        # feat_5m_ret (5분 전 close 대비 return)
        if len(closes) > w5 and closes[-w5 - 1] > 1e-8:
            feat_5m_ret = last / float(closes[-w5 - 1]) - 1.0
        else:
            feat_5m_ret = 0.0

        # feat_30m_vol (rolling std / rolling mean, last w30 window)
        if len(closes) >= w30:
            last_w30 = closes[-w30:]
            mean_30 = float(last_w30.mean())
            if mean_30 > 1e-8:
                feat_30m_vol = float(last_w30.std(ddof=1) / mean_30)
            else:
                feat_30m_vol = 0.0
        else:
            feat_30m_vol = 0.0

        # feat_60m_trend (linear slope / mean, last w60 window)
        window_len = min(w60, len(closes))
        if window_len >= 2:
            last_window = closes[-window_len:]
            x = np.arange(window_len, dtype=float)
            x_mean = x.mean()
            x_var = float(np.sum((x - x_mean) ** 2))
            y_mean = float(last_window.mean())
            if x_var > 1e-12 and y_mean > 1e-8:
                cov = float(np.sum((x - x_mean) * (last_window - y_mean)))
                slope = cov / x_var
                feat_60m_trend = slope / y_mean
            else:
                feat_60m_trend = 0.0
        else:
            feat_60m_trend = 0.0

        # feat_1m_close_robust_z (MAD robust Z wrt last w60 window)
        window_len_z = min(w60, len(closes))
        block = closes[-window_len_z:]
        med = float(np.median(block))
        mad = float(np.median(np.abs(block - med)))
        denom = mad * self._mad_constant
        if denom < 1e-8:
            denom = 1e-8
        z_raw = (last - med) / denom
        feat_1m_close_robust_z = float(
            np.clip(z_raw, -self._outlier_cap_z, self._outlier_cap_z)
        )

        feats = {
            "feat_1m_close_robust_z": feat_1m_close_robust_z,
            "feat_5m_ret": feat_5m_ret,
            "feat_30m_vol": feat_30m_vol,
            "feat_60m_trend": feat_60m_trend,
        }
        for window in self._intraday_feature_windows:
            ret_key = f"feat_{window}m_ret"
            if len(closes) > window and closes[-window - 1] > 1e-8:
                feats[ret_key] = last / float(closes[-window - 1]) - 1.0
            else:
                feats[ret_key] = 0.0

            vol_key = f"feat_{window}m_vol"
            if len(closes) >= window:
                last_window = closes[-window:]
                mean_value = float(last_window.mean())
                feats[vol_key] = (
                    float(last_window.std(ddof=1) / mean_value)
                    if mean_value > 1e-8
                    else 0.0
                )
            else:
                feats[vol_key] = 0.0

        for window in self._volume_z_windows:
            key = f"feat_volume_{window}m_z"
            if len(volumes) >= window:
                last_window = volumes[-window:]
                mean_value = float(last_window.mean())
                std_value = float(last_window.std(ddof=1))
                z_value = (float(volumes[-1]) - mean_value) / std_value if std_value > 1e-8 else 0.0
                feats[key] = float(
                    np.clip(z_value, -self._outlier_cap_z, self._outlier_cap_z)
                )
            else:
                feats[key] = 0.0

        session_indices = self._latest_session_indices(usable_bars)
        session_closes = closes[session_indices]
        session_highs = highs[session_indices]
        session_lows = lows[session_indices]
        session_volumes = volumes[session_indices]
        session_open = float(session_closes[0]) if len(session_closes) else last
        feats["feat_open_to_now_ret"] = (
            last / session_open - 1.0 if session_open > 1e-8 else 0.0
        )
        cumulative_volume = float(session_volumes.sum()) if len(session_volumes) else 0.0
        if cumulative_volume > 1e-8:
            session_vwap = float(np.sum(session_closes * session_volumes) / cumulative_volume)
            feats["feat_session_vwap_gap"] = (
                last / session_vwap - 1.0 if session_vwap > 1e-8 else 0.0
            )
        else:
            feats["feat_session_vwap_gap"] = 0.0
        session_high = float(session_highs.max()) if len(session_highs) else last
        session_low = float(session_lows.min()) if len(session_lows) else last
        session_range = session_high - session_low
        feats["feat_session_high_pos"] = (
            float(np.clip((last - session_low) / session_range, 0.0, 1.0))
            if session_range > 1e-8
            else 0.0
        )
        ds_values: dict[str, float] = {}
        if self._dual_source_feature_cols and ticker is not None and asof is not None:
            ds_values = self._load_dual_source_features(ticker=ticker, asof=asof)

        for col in self._dual_source_feature_cols:
            if col in ds_values:
                feats[col] = float(ds_values[col])
            elif require_dual_source:
                logger.warning(
                    "[quant_agent] %s Dual-Source feature 누락: %s. "
                    "active inference skip",
                    ticker,
                    col,
                )
                return None
            else:
                feats.setdefault(col, self._dual_source_defaults.get(col, 0.0))
        exog_values = self._get_exogenous_features(ticker, asof=asof)
        for col in self._exogenous_feature_cols:
            feats[col] = float(exog_values.get(col, self._exogenous_defaults.get(col, 0.0)))
        return feats

    def _feature_window_limit(self) -> int:
        feature_cols = (
            self._inference_feature_cols
            if self.has_model
            else self._feature_cols
        )
        windows = list(self._multi_scale_windows)
        for col in feature_cols:
            if not col.startswith("feat_"):
                continue
            token = col.removeprefix("feat_").split("_", 1)[0]
            if token.endswith("m"):
                try:
                    windows.append(int(token[:-1]))
                except ValueError:
                    continue
            elif col.startswith("feat_volume_") and token == "volume":
                parts = col.removeprefix("feat_volume_").split("_", 1)
                if parts and parts[0].endswith("m"):
                    try:
                        windows.append(int(parts[0][:-1]))
                    except ValueError:
                        continue
        return int(max(windows)) if windows else int(self._multi_scale_windows[-1])

    @staticmethod
    def _latest_session_indices(bars: list[dict[str, Any]]) -> np.ndarray:
        if not bars:
            return np.array([], dtype=int)
        latest_key = str(bars[-1].get("ts_close", ""))[:10]
        if not latest_key:
            return np.arange(len(bars), dtype=int)
        indices = [
            idx for idx, bar in enumerate(bars)
            if str(bar.get("ts_close", ""))[:10] == latest_key
        ]
        if not indices:
            return np.arange(len(bars), dtype=int)
        return np.array(indices, dtype=int)

    def _get_exogenous_features(
        self,
        ticker: str | None,
        asof: str | None = None,
    ) -> dict[str, float]:
        values = (
            dict(self._market_exogenous_snapshot)
            if self._exogenous_meta_usable(self._market_exogenous_snapshot_meta, asof)
            else {}
        )
        if ticker:
            flow = self.get_investor_flow_snapshot(ticker, asof=asof)
            if flow is not None and not flow.get("is_future") and not flow.get("is_stale"):
                for col in ("foreign_net_buy", "institutional_net_buy", "retail_net_buy"):
                    if flow.get(col) is not None:
                        values[col] = float(flow[col])
            ticker_padded = pad_ticker(str(ticker))
            if self._exogenous_meta_usable(
                self._exogenous_snapshot_meta.get(ticker_padded, {}),
                asof,
            ):
                values.update(self._exogenous_snapshot.get(ticker_padded, {}))
        for col, default in self._exogenous_defaults.items():
            values.setdefault(col, default)
        return values

    def _exogenous_meta_usable(self, meta: dict[str, Any], asof: str | None) -> bool:
        """Freshness guard for generic exogenous snapshots.

        Exogenous values without `received_at` provenance are blocked and fall back
        to neutral defaults. Explicit metadata is blocked if it is future/stale.
        """
        received_at = meta.get("received_at") if isinstance(meta, dict) else None
        if received_at is None:
            return False
        asof_dt = self._parse_snapshot_dt(asof) if asof else datetime.now(_KST)
        raw_age_sec = (asof_dt - received_at).total_seconds()
        if raw_age_sec < 0:
            return False
        max_age_sec = float(meta.get("max_age_sec", self._investor_flow_stale_sec))
        return raw_age_sec <= max_age_sec

    def _model_requires_dual_source(self) -> bool:
        """로드된 모델 feature manifest가 Dual-Source 피처를 실제 입력으로 요구하는지."""
        if not self._dual_source_feature_cols:
            return False
        train_cols = set(self._inference_feature_cols)
        return any(col in train_cols for col in self._dual_source_feature_cols)

    def _load_dual_source_features(self, ticker: str, asof: str) -> dict[str, float]:
        """장전 배치 C3A 점수를 date+ticker 기준으로 로드.

        파일이 없거나 해당 ticker가 없으면 빈 dict를 반환한다. Dual-Source 모델 추론에서는
        호출자가 require_dual_source=True로 빈 dict를 active inference 차단 신호로 사용한다.
        """
        date_key = self._asof_to_yyyymmdd(asof)
        if date_key not in self._dual_source_cache:
            loader = self._dual_source_loader
            if loader is None:
                records = []
            else:
                try:
                    records = loader(date_key)
                except Exception as e:
                    logger.warning(
                        "[quant_agent] Dual-Source score load 실패 date=%s: %s",
                        date_key,
                        e,
                    )
                    records = []
            self._dual_source_cache[date_key] = [
                item for item in records if isinstance(item, dict)
            ]

        ticker_map: dict[str, dict[str, float]] = {}
        for item in self._dual_source_cache.get(date_key, []):
            if not self._dual_source_record_usable(item, asof):
                continue
            padded = pad_ticker(str(item.get("ticker", "")))
            if not padded or padded == "000000":
                continue
            values: dict[str, float] = {}
            for col in self._dual_source_feature_cols:
                value = self._float_or_none(item.get(col))
                if value is not None:
                    values[col] = value
            if values:
                ticker_map[padded] = values

        return ticker_map.get(pad_ticker(ticker), {})

    def _dual_source_record_usable(self, item: dict[str, Any], asof: str) -> bool:
        """Dual-Source score metadata must not be newer than Hot Path asof."""
        asof_dt = self._parse_snapshot_dt(asof)
        for key in ("snapshot_ts", "generated_at"):
            raw = item.get(key)
            if raw in (None, ""):
                continue
            try:
                ts = self._parse_snapshot_dt(raw)
            except (TypeError, ValueError) as e:
                logger.warning(
                    "[quant_agent] Dual-Source %s 파싱 실패: %s. record skip",
                    key,
                    e,
                )
                return False
            if ts > asof_dt:
                logger.warning(
                    "[quant_agent] Dual-Source future %s=%s > asof=%s. record skip",
                    key,
                    ts.isoformat(),
                    asof_dt.isoformat(),
                )
                return False
        return True

    @staticmethod
    def _asof_to_yyyymmdd(asof: str) -> str:
        raw = str(asof)
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is not None:
                dt = dt.astimezone(_KST)
            return dt.strftime("%Y%m%d")
        except ValueError:
            digits = "".join(ch for ch in raw if ch.isdigit())
            if len(digits) >= 8:
                return digits[:8]
            return datetime.now(_KST).strftime("%Y%m%d")

    @staticmethod
    def _parse_snapshot_dt(value: datetime | str | None) -> datetime:
        if value is None:
            return datetime.now(_KST)
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_KST)
        return dt.astimezone(_KST)

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    # ================================================================== #
    # Internal: Anomaly detection
    # ================================================================== #

    def _is_intraday_drop_anomaly(
        self,
        bars: list[dict[str, Any]],
    ) -> tuple[bool, float]:
        """1min return z-score < -threshold → anomaly.

        rolling 60 bars 내 return 분포 기준. architecture.md trigger_catalog 대응.
        Returns: (is_anomaly, z_score).
        """
        if len(bars) < self._warmup_bars:
            return False, 0.0

        closes = np.array([float(b["close"]) for b in bars], dtype=float)
        returns = np.diff(closes) / np.where(closes[:-1] > 1e-8, closes[:-1], 1e-8)
        if len(returns) < 2:
            return False, 0.0

        # 최신 return 제외한 나머지 기준 mean/std (current bar를 outlier 판정)
        hist = returns[:-1]
        if len(hist) < 2:
            return False, 0.0
        mu = float(hist.mean())
        sigma = float(hist.std(ddof=0))
        if sigma < 1e-8:
            return False, 0.0

        latest_ret = float(returns[-1])
        z = (latest_ret - mu) / sigma
        return (z < -self._anomaly_zscore_threshold), z

    def _is_volume_spike_anomaly(
        self,
        bars: list[dict[str, Any]],
    ) -> tuple[bool, float]:
        """volume z-score > +threshold → anomaly.

        rolling 60 bars 내 volume 분포 기준. 최신 bar 제외 mean/std로 current bar outlier 판정.
        가격과 달리 단방향(spike만, 거래량 0은 휴장/거래 정지 별도 처리).
        Returns: (is_anomaly, z_score).
        """
        if len(bars) < self._warmup_bars:
            return False, 0.0

        volumes = np.array(
            [float(b.get("volume", 0.0)) for b in bars], dtype=float
        )
        hist = volumes[:-1]
        if len(hist) < 2:
            return False, 0.0
        mu = float(hist.mean())
        sigma = float(hist.std(ddof=0))
        if sigma < 1e-8:
            return False, 0.0

        z = (float(volumes[-1]) - mu) / sigma
        return (z > self._volume_zscore_threshold), z
