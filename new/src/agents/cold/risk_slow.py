"""Cold Path Risk Slow Agent. Kanana-o CoT 심층 리스크 분석.

S2-8 실구현. 이벤트 발생 시 RiskAgentFast 보완. 10~30초 응답 허용.
결과: C5 risk_warning / regime_change / veto_recommendation publish.

Cold Path 전용 (mode='cold'). Kanana-o 100회/일 예산 내.
임계값/설정은 risk_config.yaml 로드 (하드코딩 금지, 불변 원칙 5).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

from src.agents._base import AgentBase
from src.utils.config_loader import load as config_load
from src.utils.llm_parser import parse_llm_json
from src.utils.logger import get_logger

logger = get_logger("risk_slow")

# 메모리 기본 경로
_DEFAULT_MEMORY_ROOT = (
    Path(__file__).resolve().parents[4] / "artifacts" / "agent_memory"
)

# LLM 응답 stance 유효값
_VALID_STANCES = frozenset(
    {"veto_recommendation", "risk_reduce", "neutral"}
)

# Kanana-o caller 식별자
_CALLER = "risk_agent"


class RiskAgentSlow(AgentBase):
    """Cold Path 심층 리스크 분석 에이전트.

    이벤트 발생 시 RiskAgentFast 보완. Kanana-o CoT.
    C5 risk_warning / regime_change / veto_recommendation publish.
    """

    ALLOWED_PUBLISH_CHANNELS: frozenset[str] = frozenset(
        {"risk_warning", "regime_change", "veto_recommendation"}
    )

    def __init__(
        self,
        llm_router: Any,
        pubsub: Any | None = None,
        memory_root: Path | str | None = None,
    ) -> None:
        """
        llm_router: LLMRouter 인스턴스 (DI).
        pubsub: PubSubBroker 인스턴스 (optional).
        memory_root: macro_notes JSONL 루트 경로.
        """
        self._llm_router = llm_router
        self._pubsub = pubsub
        self._memory_root = (
            Path(memory_root) if memory_root else _DEFAULT_MEMORY_ROOT
        )
        self._narrative_max_chars = self._load_narrative_max()

    def _load_narrative_max(self) -> int:
        """risk_config.yaml risk_fast.narrative_max_chars 로드 (불변 원칙 5)."""
        try:
            cfg = config_load("risk_config.yaml", "risk_fast")
            return int(cfg.get("narrative_max_chars", 300))
        except Exception as e:
            logger.warning("[risk_slow] 설정 로드 실패: %s. 기본값 사용", e)
            return 300

    # ------------------------------------------------------------------
    # C5 report 생성
    # ------------------------------------------------------------------

    def report(
        self,
        report_type: str,
        payload: dict[str, Any],
        channel: str | None = None,
    ) -> dict[str, Any]:
        """C5 risk_warning 리포트 생성.

        report_type: 항상 "risk_warning" (C5 VALID_REPORT_TYPES SSOT 준수).
        channel: publish 채널 (risk_warning / regime_change / veto_recommendation).
                 None이면 report_type과 동일.
        stance는 veto_recommendation/risk_reduce/neutral로 분기 의미 전달.
        """
        if report_type != "risk_warning":
            raise ValueError(
                f"[risk_slow] report_type={report_type} 미지원. "
                "C5 VALID_REPORT_TYPES SSOT: report_type은 risk_warning 고정. "
                "채널 분기는 channel 파라미터 사용."
            )
        stance = payload.get("stance")
        if stance not in _VALID_STANCES:
            raise ValueError(
                f"[risk_slow] risk_warning.stance={stance} 범위 이탈. "
                f"허용: {sorted(_VALID_STANCES)}"
            )

        publish_channel = channel or "risk_warning"
        msg = self.publish(publish_channel, payload)
        msg["report_type"] = "risk_warning"
        msg["ts"] = datetime.now(timezone.utc).isoformat()
        return msg

    # ------------------------------------------------------------------
    # 심층 분석 (Kanana-o CoT)
    # ------------------------------------------------------------------

    def analyze(
        self,
        event: dict[str, Any],
        fast_eval: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Kanana-o CoT 기반 심층 리스크 분석.

        Args:
            event: C2 EventNormalizeContract 이벤트.
            fast_eval: RiskAgentFast(Cold) evaluate() 결과 (선택).
                       포함 시 기존 fast 판단을 CoT 컨텍스트에 추가.

        Returns:
            C5 risk_warning 호환 딕셔너리 또는 None (LLM 실패 시).
        """
        event_type = event.get("event_type", "unknown")
        ticker = event.get("ticker") or event.get("payload", {}).get("ticker", "")
        payload_raw = event.get("payload", {})
        occurred_at = event.get("occurred_at", "")

        prompt = self._build_prompt(
            event_type=event_type,
            ticker=ticker,
            payload=payload_raw,
            occurred_at=occurred_at,
            fast_eval=fast_eval,
        )

        llm_result = self._llm_router.call(
            prompt, mode="cold", caller=_CALLER
        )

        if not llm_result.success:
            logger.warning(
                "[risk_slow] LLM 호출 실패. event_type=%s ticker=%s error=%s",
                event_type, ticker, llm_result.error,
            )
            # fallback: fast_eval 결과 그대로 반환
            return self._fallback_from_fast(event, fast_eval)

        parsed = self._parse_llm_content(llm_result.content)

        # publish channel 선택 (report_type은 항상 risk_warning, channel만 분기)
        publish_channel = self._choose_publish_channel(parsed)
        rpt = self.report(
            "risk_warning",
            {
                "stance": parsed["stance"],
                "risk_level": parsed.get("risk_level", "medium"),
                "macro_note_ref": None,
                "micro_note_ref": None,
                "fast_rule_match": fast_eval.get("fast_rule_match") if fast_eval else None,
                "narrative": parsed.get("narrative", "")[:self._narrative_max_chars],
                "affected_tickers": parsed.get("affected_tickers", [ticker] if ticker else []),
                "regime_signal": parsed.get("regime_signal"),
                "llm_model": llm_result.model_used,
                "llm_latency_ms": llm_result.latency_ms,
            },
            channel=publish_channel,
        )

        # macro memory 저장
        self._save_macro_memory(event_type, ticker, parsed)

        return rpt

    def _build_prompt(
        self,
        event_type: str,
        ticker: str,
        payload: dict[str, Any],
        occurred_at: str,
        fast_eval: dict[str, Any] | None,
    ) -> str:
        """Kanana-o CoT 프롬프트 구성."""
        fast_ctx = ""
        if fast_eval and fast_eval.get("triggered_rules"):
            rules = ", ".join(fast_eval["triggered_rules"])
            fast_ctx = f"\n\n[Fast Rule 사전 탐지] triggered_rules: {rules}"

        return (
            f"당신은 KOSPI 시장 퀀트 리스크 분석가입니다.\n"
            f"이벤트 유형: {event_type}\n"
            f"종목: {ticker or '없음'}\n"
            f"발생 시각: {occurred_at}\n"
            f"이벤트 페이로드: {json.dumps(payload, ensure_ascii=False)}"
            f"{fast_ctx}\n\n"
            "위 이벤트의 리스크를 단계별로 분석하세요.\n"
            "1) 시장 영향 (high/medium/low)\n"
            "2) 추천 조치 (veto_recommendation / risk_reduce / neutral)\n"
            "3) 체제 변화 여부 (regime_change: true/false)\n"
            "4) 영향 종목 리스트 (6자리 코드)\n"
            "5) 한국어 내러티브 (2~3문장)\n\n"
            "JSON으로 응답: "
            '{"stance": "...", "risk_level": "...", "regime_signal": false, '
            '"affected_tickers": ["..."], "narrative": "..."}'
        )

    def _parse_llm_content(self, content: str) -> dict[str, Any]:
        """LLM 응답 파싱. JSON 1차 → 키워드 fallback 2차."""
        # 1차: JSON parse
        try:
            parsed_json = parse_llm_json(content)
            stance = str(parsed_json.get("stance", "neutral"))
            if stance not in _VALID_STANCES:
                stance = "neutral"
            return {
                "stance": stance,
                "risk_level": str(parsed_json.get("risk_level", "medium")),
                "regime_signal": self._safe_bool(parsed_json.get("regime_signal", False)),
                "affected_tickers": self._safe_ticker_list(
                    parsed_json.get("affected_tickers", [])
                ),
                "narrative": str(parsed_json.get("narrative", content[:self._narrative_max_chars])),
            }
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # 2차: 키워드 fallback
        lower = content.lower()
        if "veto" in lower or "거부" in lower or "중단" in lower:
            stance = "veto_recommendation"
        elif "reduce" in lower or "축소" in lower or "위험" in lower:
            stance = "risk_reduce"
        else:
            stance = "neutral"

        tickers = re.findall(r"\b(\d{6})\b", content)

        return {
            "stance": stance,
            "risk_level": "high" if stance == "veto_recommendation" else "medium",
            "regime_signal": "regime" in lower or "체제" in content,
            "affected_tickers": list(dict.fromkeys(tickers)),
            "narrative": content[:self._narrative_max_chars],
        }

    @staticmethod
    def _safe_bool(value: Any, default: bool = False) -> bool:
        """LLM bool-like 값을 문자열까지 안전하게 해석한다."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "y", "1", "regime", "regime_change", "체제", "변화"}:
                return True
            if normalized in {"false", "no", "n", "0", "none", "null", "없음", "아님"}:
                return False
        return default

    @staticmethod
    def _safe_ticker_list(value: Any) -> list[str]:
        """문자열/리스트 ticker 입력을 6자리 코드 리스트로 정규화한다."""
        raw_items = value if isinstance(value, list) else [value]
        tickers: list[str] = []
        for item in raw_items:
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            match = re.search(r"\d{1,6}", text)
            if match:
                tickers.append(match.group(0).zfill(6))
        return list(dict.fromkeys(tickers))

    def _choose_publish_channel(self, parsed: dict[str, Any]) -> str:
        """분석 결과에 따라 publish channel 선택.

        report_type은 항상 "risk_warning" (C5 SSOT). channel만 분기.
        """
        if parsed.get("stance") == "veto_recommendation":
            return "veto_recommendation"
        if parsed.get("regime_signal"):
            return "regime_change"
        return "risk_warning"

    def _fallback_from_fast(
        self,
        event: dict[str, Any],
        fast_eval: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """LLM 실패 시 fast_eval 기반 fallback 응답."""
        stance = "neutral"
        risk_level = "low"
        if fast_eval:
            stance = fast_eval.get("stance", "neutral")
            risk_level = fast_eval.get("risk_level", "low")

        return self.report(
            "risk_warning",
            {
                "stance": stance,
                "risk_level": risk_level,
                "macro_note_ref": None,
                "micro_note_ref": None,
                "fast_rule_match": fast_eval.get("fast_rule_match") if fast_eval else None,
                "narrative": "[LLM 호출 실패. Fast Rule 결과 기반 fallback]",
                "affected_tickers": [],
            },
        )

    # ------------------------------------------------------------------
    # Memory 저장
    # ------------------------------------------------------------------

    def _save_macro_memory(
        self,
        event_type: str,
        ticker: str,
        parsed: dict[str, Any],
    ) -> None:
        """macro_notes JSONL 저장."""
        today = datetime.now(_KST).strftime("%Y%m%d")
        path = self._memory_root / "risk_agent" / "macro" / f"{today}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "ticker": ticker,
            "stance": parsed.get("stance"),
            "risk_level": parsed.get("risk_level"),
            "regime_signal": parsed.get("regime_signal"),
        }
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("[risk_slow] macro memory 저장 실패: path=%s error=%s", path, e)
