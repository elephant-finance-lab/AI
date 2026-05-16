"""Cold Path News Agent. Kanana-o CoT 분석. S2-7 실구현.

이벤트(뉴스/공시/커뮤니티) → LLMRouter 분석 → C5 news_signal / dart_alert publish.
memory: artifacts/agent_memory/news_agent/{ticker}/{YYYYMMDD}.jsonl (micro)
        artifacts/agent_memory/macro/{YYYYMMDD}.jsonl (macro)
cache: S4-7 Persistent Cache. key=news:{ticker}:{event_id}. TTL=risk_config.yaml cache.news_ttl_seconds.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.agents._base import AgentBase
from src.cache.persistent_cache import PersistentCache
from src.data.filter_loader import load_news_filter
from src.utils.llm_parser import parse_llm_json
from src.utils.logger import get_logger

logger = get_logger("news_agent")

_KST = ZoneInfo("Asia/Seoul")

# 기본 memory_root: repo_root/artifacts/agent_memory
_DEFAULT_MEMORY_ROOT = Path(__file__).resolve().parents[4] / "artifacts" / "agent_memory"


class NewsAgent(AgentBase):
    """Cold Path 뉴스/공시/커뮤니티 분석 에이전트.

    이벤트 트리거 시 호출. Kanana-o (Cold Path) 사용.
    caller='news_agent' 고정. 하루 100회 예산 내.
    Dual-Source: 뉴스 vs 커뮤니티 divergence → uncertainty 신호 (Sprint 4 확장 예정).
    """

    ALLOWED_PUBLISH_CHANNELS: frozenset[str] = frozenset({"news_signal", "dart_alert"})
    VALID_STANCES: frozenset[str] = frozenset({"buy", "sell", "neutral"})

    # event_type → publish channel 매핑. "dart" 만 dart_alert, 나머지 news_signal.
    _EVENT_TYPE_TO_CHANNEL: dict[str, str] = {
        "news": "news_signal",
        "community": "news_signal",
        "dart": "dart_alert",
    }

    def __init__(
        self,
        llm_router: Any,
        pubsub: Any | None = None,
        memory_root: Path | str | None = None,
        cache: PersistentCache | None = None,
    ) -> None:
        """NewsAgent 생성자.

        Args:
            llm_router: LLMRouter 인스턴스 (DI).
            pubsub: PubSubBroker 인스턴스 (optional). None이면 publish skip.
            memory_root: memory JSONL 저장 루트 경로. None이면 기본 경로 사용.
            cache: PersistentCache 인스턴스 (optional). None이면 캐시 미사용.
                   TTL은 cache.news_ttl (risk_config.yaml cache.news_ttl_seconds).
        """
        self._llm_router = llm_router
        self._pubsub = pubsub
        self._memory_root = Path(memory_root) if memory_root else _DEFAULT_MEMORY_ROOT
        self._cache = cache
        # narrative 최대 길이: news_filter.yaml text_pack_settings.narrative_max_chars SSOT (하드코딩 금지)
        _settings = load_news_filter().get("text_pack_settings", {})
        self._narrative_max_chars = int(_settings.get("narrative_max_chars", 200))

    # ------------------------------------------------------------------
    # C5 report 생성
    # ------------------------------------------------------------------

    def report(self, report_type: str, payload: dict) -> dict:
        """C5 news_signal payload 검증 후 AgentReport dict 반환.

        Args:
            report_type: "news_signal" 고정 (dart_alert도 payload schema 동일).
            payload: {stance, impacted_tickers, impacted_sectors, narrative}.

        Returns:
            {report_type, payload, agent, ts}

        Raises:
            ValueError: payload 검증 실패 (stance 범위 이탈, narrative 누락 등).
        """
        if report_type != "news_signal":
            raise ValueError(
                f"[news_agent] report_type={report_type!r} 미지원. "
                "C5 AgentReportContract report_type은 news_signal 고정."
            )

        # stance 검증
        stance = payload.get("stance")
        if stance not in self.VALID_STANCES:
            raise ValueError(
                f"[news_agent] 유효하지 않은 stance: {stance!r}. "
                f"허용: {sorted(self.VALID_STANCES)}"
            )

        # narrative 필수
        narrative = payload.get("narrative")
        if not narrative:
            raise ValueError("[news_agent] payload.narrative 누락 또는 빈 문자열.")

        # impacted_tickers / impacted_sectors list 보장
        tickers = payload.get("impacted_tickers", [])
        sectors = payload.get("impacted_sectors", [])
        if not isinstance(tickers, list):
            raise ValueError("[news_agent] payload.impacted_tickers는 list이어야 합니다.")
        if not isinstance(sectors, list):
            raise ValueError("[news_agent] payload.impacted_sectors는 list이어야 합니다.")

        ts = datetime.now(_KST).isoformat()
        return {
            "report_type": report_type,
            "payload": {
                "stance": stance,
                "impacted_tickers": tickers,
                "impacted_sectors": sectors,
                "narrative": narrative,
            },
            "agent": type(self).__name__,
            "ts": ts,
        }

    # ------------------------------------------------------------------
    # 이벤트 CoT 분석
    # ------------------------------------------------------------------

    def analyze(self, event: dict) -> dict | None:
        """이벤트 CoT 분석. Kanana-o (Cold Path) 호출.

        Hot Path에서 이 메서드를 호출하면 안 됩니다 (mode='cold' 고정).
        LLM 실패 시 stance=neutral fallback.

        Args:
            event: {event_type, ticker, title, summary, event_id, ...}

        Returns:
            {channel, payload, report_type, agent, ts, llm_fallback}
            unknown event_type은 ValueError 발생.
        """
        event_type = event.get("event_type", "")
        if event_type not in self._EVENT_TYPE_TO_CHANNEL:
            raise ValueError(
                f"[news_agent] 알 수 없는 event_type: {event_type!r}. "
                f"허용: {sorted(self._EVENT_TYPE_TO_CHANNEL)}"
            )

        publish_channel = self._EVENT_TYPE_TO_CHANNEL[event_type]

        # ticker 추출 (6자리 zero-padded). C2 정규화 이벤트는 payload.ticker 또는
        # scope=ticker:{code}에 ticker를 둘 수 있으므로 모두 수용한다.
        payload = event.get("payload") or {}
        scope = str(event.get("scope") or "")
        scope_ticker = scope.split(":", 1)[1] if scope.startswith("ticker:") else None
        event_tickers = event.get("tickers")
        if isinstance(event_tickers, list):
            first_event_ticker = event_tickers[0] if event_tickers else None
        elif isinstance(event_tickers, str):
            first_event_ticker = event_tickers
        else:
            first_event_ticker = None
        raw_ticker = (
            event.get("ticker")
            or payload.get("ticker")
            or scope_ticker
            or first_event_ticker
        )
        ticker = str(raw_ticker).zfill(6) if raw_ticker else ""

        title = event.get("title", "")
        summary = event.get("summary", "")
        event_id = event.get("event_id", "")

        # S4-7 캐시 조회 (event_id 있을 때만 키 생성)
        cache_key = f"news:{ticker}:{event_id}" if event_id else None
        if cache_key and self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.info(
                    "[news_agent] 캐시 HIT. event_id=%s ticker=%s LLM 호출 skip.",
                    event_id,
                    ticker,
                )
                return cached

        # LLM 프롬프트 구성
        prompt = (
            f"[종목 {ticker}] 뉴스 분석 요청.\n"
            f"제목: {title}\n"
            f"내용: {summary}\n\n"
            "투자 관점에서 매수(buy)/매도(sell)/중립(neutral) 중 하나와 "
            "간단한 근거와 confidence(0.0~1.0)를 한국어로 답하세요.\n\n"
            'Respond strictly in JSON: {"stance": "buy|sell|neutral", '
            '"impacted_tickers": ["005930"], '
            '"impacted_sectors": ["반도체"], '
            '"narrative": "한국어 1~3문장", '
            '"confidence": 0.0}'
        )

        # Cold Path LLM 호출 (Hot Path에서 호출 금지. mode='cold' 강제.)
        llm_result = self._llm_router.call(
            prompt, mode="cold", caller="news_agent"
        )

        llm_fallback = False
        if llm_result.success and llm_result.content:
            parsed = self._parse_llm_content(llm_result.content)
        else:
            # LLM 실패 → stance=neutral fallback
            logger.warning(
                "[news_agent] LLM 호출 실패. fallback neutral. "
                "event_id=%s error=%s",
                event_id,
                getattr(llm_result, "error", None),
            )
            error_msg = getattr(llm_result, "error", "LLM 실패") or "LLM 실패"
            parsed = {
                "stance": "neutral",
                "narrative": f"LLM 호출 실패: {error_msg}"[: self._narrative_max_chars],
                "impacted_tickers": [],
                "impacted_sectors": [],
                "confidence": 0.5,
            }
            llm_fallback = True

        # ticker를 impacted_tickers에 포함 (비어있으면)
        if ticker and not parsed["impacted_tickers"]:
            parsed["impacted_tickers"] = [ticker]

        # C5 report 생성
        rpt = self.report("news_signal", parsed)
        parsed_confidence = self._parse_confidence(parsed.get("confidence", 0.5))
        message_scope = f"ticker:{ticker}" if ticker else "market"

        # micro memory 저장
        if ticker and not llm_fallback:  # fallback 결과는 memory 저장 스킵 (C18 집계 왜곡 방지)
            self._save_memory(
                ticker,
                "micro",
                {
                    "event_id": event_id,
                    "stance": parsed["stance"],
                    "narrative": parsed["narrative"],
                    "event_type": event_type,
                    "title": title,
                },
            )

        message = self.publish(publish_channel, rpt["payload"])
        message["confidence"] = parsed_confidence
        message["scope"] = message_scope
        message["timestamp"] = rpt["ts"]

        result = {
            "channel": publish_channel,
            "payload": rpt["payload"],
            "report_type": rpt["report_type"],
            "agent": rpt["agent"],
            "ts": rpt["ts"],
            "llm_fallback": llm_fallback,
            **message,
        }
        if not llm_fallback:
            result["message"] = message

        # S4-7 캐시 저장 (LLM fallback이 아닌 정상 분석 결과만)
        if cache_key and self._cache is not None and not llm_fallback:
            self._cache.set(cache_key, result, ttl_seconds=self._cache.news_ttl)
            logger.info(
                "[news_agent] 캐시 SET. event_id=%s ticker=%s ttl=%ds",
                event_id,
                ticker,
                self._cache.news_ttl,
            )

        # 2026-05-12 audit C-D2 fix: C4 auto-publish to SharedMessagePool.
        # 이전: result dict에 content/scope/confidence/reasoning 필드는 추가됐으나
        # self._pubsub.publish() 호출 자체 없음 → EventGateway 우회 직접 호출 시
        # C4 메시지 풀에 게시 안 됨 (5/11 fix incomplete).
        # llm_fallback 시 publish 안 함 (neutral 결과로 풀 오염 방지).
        if self._pubsub is not None and not llm_fallback:
            try:
                msg_id = self._pubsub.publish(publish_channel, message)
                result["message_id"] = msg_id
                result["published_by_agent"] = True
            except Exception as e:
                logger.warning(
                    "[news_agent] C4 publish 실패. event_id=%s channel=%s error=%s",
                    event_id, publish_channel, e,
                )

        return result

    # ------------------------------------------------------------------
    # TextPack 소비 인터페이스
    # ------------------------------------------------------------------

    def consume_text_pack(
        self, ticker: str, text_pack: str, context: dict | None = None
    ) -> dict | None:
        """TextPack 소비. analyze()에 위임.

        S2-6 TextPackBuilder 출력을 직접 소비하는 진입점.

        Args:
            ticker: 종목 코드 (6자리 zfill 적용).
            text_pack: TextPackBuilder가 생성한 텍스트 묶음.
            context: 추가 컨텍스트 {event_id, title, summary, event_type, ...}.

        Returns:
            analyze() 반환값과 동일.
        """
        ctx = context or {}
        event = {
            "event_type": ctx.get("event_type", "news"),
            "ticker": str(ticker).zfill(6),
            "title": ctx.get("title", ""),
            "summary": ctx.get("summary", text_pack),
            "event_id": ctx.get("event_id", ""),
            **{k: v for k, v in ctx.items() if k not in ("event_type", "ticker", "title", "summary", "event_id")},
        }
        return self.analyze(event)

    # ------------------------------------------------------------------
    # EventGateway 자동 등록
    # ------------------------------------------------------------------

    def attach_to_gateway(self, gateway: Any) -> None:
        """EventGateway에 3개 event_type 핸들러 자동 등록.

        news → news_signal, dart → dart_alert, community → news_signal.
        """
        gateway.register_handler("news", self.analyze, publish_channel="news_signal")
        gateway.register_handler("dart", self.analyze, publish_channel="dart_alert")
        gateway.register_handler("community", self.analyze, publish_channel="news_signal")
        logger.info("[news_agent] EventGateway 핸들러 등록 완료: news / dart / community")

    # ------------------------------------------------------------------
    # LLM 응답 파싱 (heuristic)
    # ------------------------------------------------------------------

    def _parse_llm_content(self, content: str) -> dict:
        """LLM 응답 파싱. 1차 JSON parse → 2차 heuristic fallback.

        1차 (JSON mode):
            json.loads() 성공 시 stance / narrative / impacted_tickers / impacted_sectors 직접 추출.
            stance 값이 VALID_STANCES 밖이면 "hold" → "neutral" 보정.

        2차 (heuristic fallback):
            JSON parse 실패 시 키워드 탐색으로 stance 추출 + regex로 6자리 ticker 추출.
            buy 키워드: 'buy', '매수', '긍정', '상승', '호재'
            sell 키워드: 'sell', '매도', '부정', '하락', '악재'
            그 외: neutral

        3차 (narrative):
            JSON/fallback 모두 narrative 미확보 시 content 첫 narrative_max_chars 자 사용.
        """
        # 1차: JSON parse 시도
        try:
            parsed_json = parse_llm_json(content)

            raw_stance = str(parsed_json.get("stance", "neutral")).lower()
            # "hold" → VALID_STANCES에 없으므로 "neutral" 보정
            if raw_stance == "hold":
                raw_stance = "neutral"
            stance = raw_stance if raw_stance in self.VALID_STANCES else "neutral"

            tickers_raw = parsed_json.get("impacted_tickers", [])
            sectors_raw = parsed_json.get("impacted_sectors", [])
            impacted_tickers = tickers_raw if isinstance(tickers_raw, list) else []
            impacted_sectors = sectors_raw if isinstance(sectors_raw, list) else []

            narrative_raw = parsed_json.get("narrative", "") or ""
            narrative = str(narrative_raw)[: self._narrative_max_chars] or content[: self._narrative_max_chars]

            return {
                "stance": stance,
                "narrative": narrative,
                "impacted_tickers": impacted_tickers,
                "impacted_sectors": impacted_sectors,
                "confidence": self._parse_confidence(
                    parsed_json.get("confidence", 0.5)
                ),
            }

        except (json.JSONDecodeError, ValueError, TypeError):
            pass  # 2차 heuristic fallback으로 진행

        # 2차: heuristic fallback
        lower = content.lower()

        buy_keywords = ["buy", "매수", "긍정", "상승", "호재"]
        sell_keywords = ["sell", "매도", "부정", "하락", "악재"]

        stance = "neutral"
        for kw in buy_keywords:
            if kw in lower:
                stance = "buy"
                break
        if stance == "neutral":
            for kw in sell_keywords:
                if kw in lower:
                    stance = "sell"
                    break

        # regex로 6자리 ticker 추출
        ticker_matches = re.findall(r"\b(\d{6})\b", content)
        impacted_tickers = list(dict.fromkeys(ticker_matches))  # 순서 유지 중복 제거

        narrative = content[: self._narrative_max_chars]

        return {
            "stance": stance,
            "narrative": narrative,
            "impacted_tickers": impacted_tickers,
            "impacted_sectors": [],
            "confidence": 0.3,  # heuristic fallback은 신뢰도 낮음을 명시
        }

    @staticmethod
    def _parse_confidence(value: Any) -> float:
        """LLM confidence를 0.0~1.0 범위 float로 정규화."""
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.5
        if not math.isfinite(confidence):
            return 0.5
        return max(0.0, min(1.0, confidence))

    # ------------------------------------------------------------------
    # Memory 저장
    # ------------------------------------------------------------------

    def _save_memory(self, ticker: str, note_type: str, content: dict) -> None:
        """micro / macro JSONL 메모리 저장.

        Args:
            ticker: 종목 코드 (6자리 zfill 적용).
            note_type: "micro" | "macro".
            content: 저장할 dict.
        """
        today = datetime.now(_KST).strftime("%Y%m%d")
        padded_ticker = str(ticker).zfill(6)

        if note_type == "micro":
            path = self._memory_root / "news_agent" / padded_ticker / f"{today}.jsonl"
        else:
            # macro: ticker 무관 공통 경로
            path = self._memory_root / "macro" / f"{today}.jsonl"

        path.parent.mkdir(parents=True, exist_ok=True)

        record = {
            "ts": datetime.now(_KST).isoformat(),
            "ticker": padded_ticker,
            "note_type": note_type,
            "content": content,
        }

        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("[news_agent] memory 저장 실패: path=%s error=%s", path, e)
