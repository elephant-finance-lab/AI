"""Hot Path Risk Fast sidecar — S2-8 실구현.

비LLM 100% 규칙 기반. <50ms 보장.
trigger_catalog + risk_fast 섹션 기반 4개 규칙 평가.

불변 원칙:
  - Hot Path <50ms. 동기 LLM 호출 금지.
  - 임계값은 risk_config.yaml risk_fast 섹션에서 로드 (하드코딩 금지).
  - 종목코드 pad_ticker (6자리 zero-padded).

C5 risk_warning payload:
  stance: veto_recommendation | risk_reduce | neutral
  risk_level: low | medium | high
  fast_rule_match: [{rule_id, matched_at}] | null

역할 분리 (architecture.md L395):
  Quant Agent = 숫자 anomaly 전담 (1분 return z-score).
  Risk Fast = 급락 % / 거래량 spike / 변동성 / top10 붕괴 전담.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from src.agents._base import AgentBase
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("risk_fast")


class RiskFastAgent(AgentBase):
    """Hot Path Risk Fast sidecar 에이전트.

    Hot Path 주 코어를 block하지 않는 sidecar.
    <50ms 목표. LLM 미호출. 규칙 4개 평가 후 C5 risk_warning publish.
    """

    # C5 risk_warning 채널. _base.py VALID_PUBLISH_CHANNELS 포함 확인.
    ALLOWED_PUBLISH_CHANNELS = frozenset({"risk_warning"})

    def __init__(self) -> None:
        super().__init__()
        rf_cfg = config_load("risk_config.yaml", "risk_fast")

        self._intraday_drop_pct: float = float(rf_cfg["intraday_drop_pct"])
        self._volume_spike_multiplier: float = float(rf_cfg["volume_spike_multiplier"])
        self._volatility_bp_threshold: float = float(rf_cfg["volatility_bp_threshold"])
        self._top10_collapse_count: int = int(rf_cfg["top10_collapse_count"])
        self._volume_lookback_bars: int = int(rf_cfg["volume_lookback_bars"])
        self._min_bars_required: int = int(rf_cfg["min_bars_required"])
        self._sla_ms: float = float(rf_cfg.get("sla_ms", 50))

        logger.info(
            "[risk_fast] 초기화. drop_pct=%.2f, vol_mult=%.1f, "
            "vola_bp=%d, top10_collapse=%d",
            self._intraday_drop_pct,
            self._volume_spike_multiplier,
            self._volatility_bp_threshold,
            self._top10_collapse_count,
        )

    # ================================================================== #
    # Public API
    # ================================================================== #

    def evaluate(
        self,
        snapshot: dict[str, Any],
        *,
        ts: datetime,
    ) -> dict[str, Any]:
        """비LLM 규칙 기반 리스크 평가.

        Args:
            snapshot: {
                'ranking': dict — PPO top10 등 랭킹 결과 (ticker: score),
                'portfolio_patch': dict — PM 패치 결과,
                'recent_bars': dict[str, list[dict]] | None — ticker별 최근 1분봉 리스트.
                              None이면 규칙 1~3 skip, risk_level=low.
            }
            ts: 루프 기준 시각 (datetime).

        Returns:
            risk_level: 'low' | 'medium' | 'high'
            fast_rule_match: list[{rule_id, matched_at}] | None  (C5 schema 정합, P0-2 fix 2026-05-09)
            triggered_rules: list[str]
            affected_tickers: list[str]
            recommended_action: 'pass' | 'reduce' | 'halt'
            rationale: str (한국어)
            latency_ms: float
        """
        t0 = time.perf_counter()

        recent_bars: dict[str, list[dict]] | None = snapshot.get("recent_bars")
        ranking: dict[str, Any] = snapshot.get("ranking") or {}

        all_triggered_rules: list[str] = []
        all_affected_tickers: list[str] = []
        rule_levels: list[str] = []
        fast_rule_match_details: list[dict[str, str]] = []

        if recent_bars is None:
            # recent_bars 없으면 규칙 1~3 skip
            logger.debug("[risk_fast] recent_bars=None. 규칙 1~3 skip. risk_level=low")
        else:
            # 규칙 1: 급락 탐지
            drop_tickers, drop_detail = self._check_intraday_drop(
                recent_bars, self._intraday_drop_pct
            )
            if drop_tickers:
                all_triggered_rules.append("rule_intraday_drop")
                all_affected_tickers.extend(drop_tickers)
                rule_levels.append("high")
                fast_rule_match_details.append({
                    "rule_id": "rule_intraday_drop",
                    "matched_at": ts.isoformat(),
                })
                logger.warning(
                    "[risk_fast] rule_intraday_drop: %s (threshold=%.1f%%)",
                    drop_tickers,
                    self._intraday_drop_pct * 100,
                )

            # 규칙 2: 거래량 spike
            vol_tickers, vol_detail = self._check_volume_spike(
                recent_bars, self._volume_spike_multiplier
            )
            if vol_tickers:
                all_triggered_rules.append("rule_volume_spike")
                all_affected_tickers.extend(vol_tickers)
                rule_levels.append("medium")
                fast_rule_match_details.append({
                    "rule_id": "rule_volume_spike",
                    "matched_at": ts.isoformat(),
                })
                logger.warning(
                    "[risk_fast] rule_volume_spike: %s (mult=%.1fx)",
                    vol_tickers,
                    self._volume_spike_multiplier,
                )

            # 규칙 3: 변동성 임계
            vola_tickers, vola_detail = self._check_volatility(
                recent_bars, self._volatility_bp_threshold
            )
            if vola_tickers:
                all_triggered_rules.append("rule_volatility")
                all_affected_tickers.extend(vola_tickers)
                rule_levels.append("medium")
                fast_rule_match_details.append({
                    "rule_id": "rule_volatility",
                    "matched_at": ts.isoformat(),
                })
                logger.warning(
                    "[risk_fast] rule_volatility: %s (bp_threshold=%d)",
                    vola_tickers,
                    self._volatility_bp_threshold,
                )

        # 규칙 4: top10 붕괴 (recent_bars 유무와 무관 — high 트리거 카운트 기반)
        # high 트리거는 drop 규칙 결과에서 카운트 (PPO top10 종목 중)
        ppo_top10 = self._extract_top10(ranking)
        drop_tickers: list[str] = []   #drop_tickers는 규칙 1에서 정의됨, 규칙 1이 실행 안 됐는데 규칙 4에서 drop_tickers를 참조하려 하면 NameError가 날 수 있어서 수정
        if recent_bars is not None and ppo_top10:
            high_count = sum(
                1 for t in ppo_top10
                if t in (drop_tickers if "rule_intraday_drop" in all_triggered_rules else [])
            )
            if high_count >= self._top10_collapse_count:
                all_triggered_rules.append("rule_top10_collapse")
                rule_levels.append("critical")
                fast_rule_match_details.append({
                    "rule_id": "rule_top10_collapse",
                    "matched_at": ts.isoformat(),
                })
                logger.critical(
                    "[risk_fast] rule_top10_collapse: Top10 중 high 트리거 %d개 (임계=%d)",
                    high_count,
                    self._top10_collapse_count,
                )

        # 종합 risk_level 결정
        internal_severity = "critical" if "critical" in rule_levels else None
        risk_level = self._aggregate_risk_level(rule_levels)
        # P0-2 fix (Critical MA-2, 2026-05-09): C5 schema fast_rule_match: [{rule_id, matched_at}]|null.
        # 이전 bool → list[dict] 또는 None. consumer (risk_slow) bool 검사는 truthy/falsy 로 호환.

        # recommended_action 결정
        recommended_action = (
            "halt" if internal_severity == "critical" else self._decide_action(risk_level)
        )

        # rationale 생성 (한국어)
        rationale = self._build_rationale(
            risk_level, all_triggered_rules, all_affected_tickers
        )

        # 중복 제거
        affected_tickers_unique = list(dict.fromkeys(all_affected_tickers))

        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        if elapsed_ms > self._sla_ms:
            logger.warning(
                "[risk_fast] SLA 초과: %.2fms > %.0fms", elapsed_ms, self._sla_ms
            )

        return {
            "risk_level": risk_level,
            "severity": internal_severity or risk_level,
            # P0-2 fix (2026-05-09): C5 정합 list[dict]|None. fast_rule_match_details 키 제거 (중복).
            "fast_rule_match": fast_rule_match_details or None,
            "triggered_rules": all_triggered_rules,
            "affected_tickers": affected_tickers_unique,
            "recommended_action": recommended_action,
            "stance": self._decide_stance(risk_level),
            "rationale": rationale,
            "latency_ms": elapsed_ms,
        }

    def report(self, report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """C5 risk_warning 리포트 생성.

        report_type은 'risk_warning'만 허용. ALLOWED_PUBLISH_CHANNELS 검증.
        """
        if report_type not in self.ALLOWED_PUBLISH_CHANNELS:
            raise ValueError(
                f"RiskFastAgent.report: invalid report_type={report_type}. "
                f"허용: {sorted(self.ALLOWED_PUBLISH_CHANNELS)}"
            )
        return {
            "report_type": report_type,
            "agent": "RiskFastAgent",
            "payload": payload,
        }

    # ================================================================== #
    # Internal: 규칙 평가
    # ================================================================== #

    def _check_intraday_drop(
        self,
        bars: dict[str, list[dict]],
        threshold: float,
    ) -> tuple[list[str], list[str]]:
        """규칙 1: recent_bars에서 1분봉 close 기준 급락 종목 탐지.

        첫 close 대비 마지막 close의 하락률이 threshold 이상인 종목.
        Returns: (affected_tickers, detail_messages)
        """
        affected: list[str] = []
        details: list[str] = []

        for ticker_raw, bar_list in bars.items():
            ticker = pad_ticker(str(ticker_raw))
            if len(bar_list) < self._min_bars_required:
                continue
            try:
                close_first = float(bar_list[0]["close"])
                close_last = float(bar_list[-1]["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if close_first < 1e-8:
                continue
            drop = (close_first - close_last) / close_first
            if drop >= threshold:
                affected.append(ticker)
                details.append(
                    f"{ticker}: {drop*100:.2f}% 급락 (임계={threshold*100:.1f}%)"
                )

        return affected, details

    def _check_volume_spike(
        self,
        bars: dict[str, list[dict]],
        multiplier: float,
    ) -> tuple[list[str], list[str]]:
        """규칙 2: 거래량 최신값 vs 이전 N봉 평균의 multiplier배 초과 종목 탐지.

        Returns: (affected_tickers, detail_messages)
        """
        affected: list[str] = []
        details: list[str] = []
        lookback = self._volume_lookback_bars

        for ticker_raw, bar_list in bars.items():
            ticker = pad_ticker(str(ticker_raw))
            if len(bar_list) < self._min_bars_required:
                continue
            try:
                volumes = [float(b["volume"]) for b in bar_list if "volume" in b]
            except (TypeError, ValueError):
                continue
            if len(volumes) < 2:
                continue
            latest_vol = volumes[-1]
            hist_vols = volumes[-(min(len(volumes), lookback + 1)):-1]
            if not hist_vols:
                continue
            avg_vol = sum(hist_vols) / len(hist_vols)
            if avg_vol < 1e-8:
                continue
            ratio = latest_vol / avg_vol
            if ratio >= multiplier:
                affected.append(ticker)
                details.append(
                    f"{ticker}: 거래량 {ratio:.1f}x (임계={multiplier:.1f}x)"
                )

        return affected, details

    def _check_volatility(
        self,
        bars: dict[str, list[dict]],
        bp_threshold: float,
    ) -> tuple[list[str], list[str]]:
        """규칙 3: 1분 return std > bp_threshold (bp 단위) 종목 탐지.

        std는 bar_list 내 전체 1분봉 return의 표준편차. 1bp = 0.01%.
        Returns: (affected_tickers, detail_messages)
        """
        affected: list[str] = []
        details: list[str] = []
        bp_as_decimal = bp_threshold / 10000.0  # bp → 소수

        for ticker_raw, bar_list in bars.items():
            ticker = pad_ticker(str(ticker_raw))
            if len(bar_list) < self._min_bars_required + 1:
                # return 계산을 위해 최소 2개 bar 필요
                continue
            try:
                closes = [float(b["close"]) for b in bar_list if "close" in b]
            except (TypeError, ValueError):
                continue
            if len(closes) < 2:
                continue
            returns = [
                (closes[i] - closes[i - 1]) / closes[i - 1]
                for i in range(1, len(closes))
                if closes[i - 1] > 1e-8
            ]
            if len(returns) < 1:
                continue
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
            std_ret = variance ** 0.5
            if std_ret >= bp_as_decimal:
                std_bp = std_ret * 10000.0
                affected.append(ticker)
                details.append(
                    f"{ticker}: std={std_bp:.1f}bp (임계={bp_threshold}bp)"
                )

        return affected, details

    # ================================================================== #
    # Internal: 집계 / 판단
    # ================================================================== #

    def _extract_top10(self, ranking: dict[str, Any]) -> list[str]:
        """ranking dict에서 상위 10 ticker 추출.

        ranking 형식: {ticker: score} 또는 {'scores': {ticker: score}}.
        """
        if not ranking:
            return []
        scores: dict[str, float] = {}
        if "scores" in ranking and isinstance(ranking["scores"], dict):
            scores = ranking["scores"]
        elif isinstance(ranking, dict):
            # {ticker: score} flat dict 형식
            scores = {k: v for k, v in ranking.items() if isinstance(v, (int, float))}
        if not scores:
            return []
        sorted_tickers = sorted(scores, key=lambda t: scores[t], reverse=True)
        return [pad_ticker(str(t)) for t in sorted_tickers[:10]]

    def _aggregate_risk_level(self, levels: list[str]) -> str:
        """복수 규칙 트리거 → 최고 severity 우선 반환.

        priority: critical > high > medium > low. C5 risk_level enum에는
        critical이 없으므로 외부 risk_level은 high로 낮추고 severity에만 보존한다.
        """
        if "critical" in levels:
            return "high"
        if "high" in levels:
            return "high"
        if "medium" in levels:
            return "medium"
        return "low"

    def _decide_action(self, risk_level: str) -> str:
        """risk_level → recommended_action 매핑.

        high → reduce, medium → reduce, low → pass.
        critical halt는 evaluate()에서 severity 기반으로 별도 처리한다.
        """
        mapping = {
            "high": "reduce",
            "medium": "reduce",
            "low": "pass",
        }
        return mapping.get(risk_level, "pass")

    def _decide_stance(self, risk_level: str) -> str:
        """risk_level → C5 stance 필드 매핑.

        high → veto_recommendation, medium → risk_reduce, low → neutral
        """
        if risk_level == "high":
            return "veto_recommendation"
        if risk_level == "medium":
            return "risk_reduce"
        return "neutral"

    def _build_rationale(
        self,
        risk_level: str,
        triggered_rules: list[str],
        affected_tickers: list[str],
    ) -> str:
        """규칙 기반 한국어 rationale 생성."""
        if not triggered_rules:
            return "위험 신호 없음. 정상."
        rule_labels = {
            "rule_intraday_drop": f"{self._intraday_drop_pct*100:.0f}% 급락",
            "rule_volume_spike": f"거래량 {self._volume_spike_multiplier:.0f}배 spike",
            "rule_volatility": f"변동성 {self._volatility_bp_threshold}bp 초과",
            "rule_top10_collapse": f"Top10 중 {self._top10_collapse_count}개 이상 급락",
        }
        triggered_desc = ", ".join(
            rule_labels.get(r, r) for r in triggered_rules
        )
        tickers_str = str(affected_tickers[:5])  # 최대 5개만 표시
        if len(affected_tickers) > 5:
            tickers_str += f"... ({len(affected_tickers)}개)"
        return (
            f"[risk_fast] {risk_level.upper()} 경고. "
            f"트리거: {triggered_desc}. "
            f"영향 종목: {tickers_str}."
        )
