# KOSPI Decision OS v3 — API Contracts

> v3.8 (2026-05-03): C15/C16 정식화 (Sprint 5 초안/보조 마크 제거 + forbidden_permissions + identity + activation_gate + weight_decision_authority + errors enum 확장 + watch_snapshot_id ID 등록 + polling_authority).
> v3.7 (2026-05-02). **Sprint 4 반영**: C13 ablation_components `dual_source` 추가 / C14 `stage_0_dqr` trigger 명시 + DQRRunner sub_component 추가 / sla.stage_timeouts.stage_0=120.
> v3.5 (2026-04-21). **PP/BUNDLE/BT/RPT/FCC/RGC 6개 ID spec UUID8 정정** (id_factory 실 구현 반영). BT 의 {tool} 컴포넌트 제거.
> v3.4 (2026-04-21). **MSG ID 포맷 정정**: MSG-{yyyymmdd}-{UUID8} (기존 {hhmm}-{seq} 대신 UUID8, seq 충돌 방지). APM ID 포맷도 동일 기준으로 정정 (APM-{yyyymmdd}-{UUID8}).
> v3.3 (2026-04-21). **C9 input non-breaking extension**: uncertainty_score 필드 추가 (Risk Fast sidecar → FDA Dual-Source 연계). message_taxonomy.publish_channels 에 uncertainty_signal 등록.
> v3.2 (2026-04-21). **C18 AgentPerformanceContract 신설** (L2 Agent 기여도 9지표 + 18:00 집계 트리거 + PIT-Safety 제약). identity 블록에 agent_performance_id 등록.
> v3.1 (2026-04-20). **C17 ModelRegistryContract 신설** (S1-0 Batch B+C). C13 BacktestEngine input에 config_ref 주석 추가. identity 블록에 model_version 등록.
> v3.0 (2026-04-10). architecture v3.0 기준. v2.2 core 위에 Dual-Source 즉시 반영 + Sprint 5 Dynamic Event Universe 계약 초안 포함.
> v2.1 대비 변경: **C12 BacktestAgentContract + C13 ValidationToolsContract 신규 추가** (Mode B 시스템 검증축).
> 이전 기준: GPT Pro 제공 (2026-04-05, v2.1).
> 연구형/모의운용형 구현용. 실거래 전환 시 OMS Phase 2 + EventAdmission 필수.

> **이 파일이 스키마/필드 정의의 SSOT(Single Source of Truth)이다.** architecture.md와 architecture_visual.md는 파생 문서. 불일치 시 이 파일이 우선한다.

> **v2.2 추가 주의**: Backtest Agent는 **Mode B 전용**이며, Shared Message Pool(Mode A 중심)과는 별도 경로로 동작한다. Backtest Agent의 publishes/subscribes는 Mode B 내부 artifact 레퍼런스이며, 장중 경로(Hot/Cold)에는 절대 개입하지 않는다. Eval Agent(Alpha Factor Engine 내부 로컬 evaluator) ≠ Backtest Agent(시스템 전체 검증자).

## 공통 규약

```yaml
time:
  format: ISO8601
  timezone: Asia/Seoul

identity:
  ticker: "6-digit KRX code"
  event_id: "EVT-{yyyymmdd}-{source}-{scope}"
  message_id: "MSG-{yyyymmdd}-{UUID8}"   # 2026-04-21 S2-1 정정: UUID8 기반 (seq 충돌 없음)
  portfolio_patch_id: "PP-{yyyymmdd}-{UUID8}"          # {seq} → {UUID8} (2026-04-21 S2-3 감사 반영: id_factory UUID8 실 구현 반영)
  decision_id: "DEC-{yyyymmdd}-{UUID8}"   # 2026-04-21 UUID8 정정 (기존 {hhmm}-{seq} 대신)
  order_plan_id: "OP-{yyyymmdd}-{UUID8}"  # 2026-04-21 UUID8 정정
  # v2.2 추가 — Backtest Agent (C12) + ValidationTools (C13) 전용 ID
  bundle_id: "BUNDLE-{yyyymmdd}-{UUID8}"               # {seq} → {UUID8} (2026-04-21 S2-3 감사 반영: id_factory UUID8 실 구현 반영)
  backtest_run_id: "BT-{yyyymmdd}-{UUID8}"             # {tool}-{seq} → {UUID8} (2026-04-21). {tool} 파라미터 제거 (실 구현에 없음)
  replay_trace_ref: "RPT-{yyyymmdd}-{UUID8}"           # {seq} → {UUID8} (2026-04-21 S2-3 감사 반영)
  # v3 추가 — FailureCaseCard + RegressionCase 산출물 ID
  failure_case_id: "FCC-{yyyymmdd}-{UUID8}"            # {seq} → {UUID8} (2026-04-21 S2-3 감사 반영)
  regression_case_id: "RGC-{yyyymmdd}-{UUID8}"         # {seq} → {UUID8} (2026-04-21 S2-3 감사 반영)
  # v3.1 추가 — C17 ModelRegistry 식별자 (2026-04-20)
  model_version: "baseline | v{n}"                   # S1-0 baseline.pkl, S3-6+ v2.pkl/v3.pkl...
  # v3.2 추가 (2026-04-21): C18 AgentPerformance 식별자
  agent_performance_id: "APM-{yyyymmdd}-{UUID8}"      # 2026-04-21 S2-1 정정: UUID8 기반. 일별 agent performance rollup ID
  # v3.6 추가 (2026-05-01): S3-11 KnowledgeBase Layer 5 식별자
  kb_message_id: "KB-{yyyymmdd}-{UUID8}"               # KB 항목 ID. S3-11 KnowledgeBase.write() 반환값. id_factory.generate_kb_id() SSOT.

modes:
  hot_path: "매 1분, 퀀트 중심 (LLM 미호출, <100ms)"
  cold_path: "이벤트 시, News/Risk/Debate/FDA(LLM) 활성 (10~30s)"
  mode_b: "장마감 자동 진화 루프 (18:00~22:00 KST). Alpha Factor Engine + Co-STEER + Backtest Agent. v2.2 Backtest Gate 포함."  # v2.2 추가

governance:
  source_of_truth:
    target_weights: PPO Allocator
    order_deltas: Portfolio Manager
    final_approval: FDA
  can_change_weight:
    FDA: false

message_taxonomy:
  # SSOT — 모든 publish name, report type, action type은 여기서 정의

  publish_channels:  # 에이전트가 Message Pool에 publish하는 채널명
    - news_signal
    - dart_alert
    - sentiment_update
    - theme_score
    - risk_warning
    - regime_change
    - veto_recommendation
    - quant_signal
    - quant_alert
    - anomaly_detected
    - investor_flow_alert
    - debate_resolution
    - pairwise_ranking
    - final_decision
    - uncertainty_signal   # 2026-04-21 추가: Risk Fast sidecar 발행. dual_source.divergence > threshold 시. C4 채널. fda_uncertainty_link.source_channel SSOT.

  action_types:  # 메시지의 action_type 필드 enum
    - signal
    - alert
    - veto_recommendation
    - regime_change
    - resolution

  report_types:  # C5 AgentReportContract의 보고서 유형
    - news_signal
    - risk_warning
    - quant_signal
    - investor_flow_alert
    - theme_score
```

## C1. MinuteBarIngestContract

```yaml
name: MinuteBarIngestContract
owner: Data Layer / KIS Gateway
transport: websocket-primary + rest-backfill
request:
  universe: [ticker]
  bar_interval: "1m"
  session_date: "YYYY-MM-DD"
response:
  batch_id: string
  source: "KIS"
  bars:
    - ticker: string
      ts_close: ISO8601
      open: float
      high: float
      low: float
      close: float
      volume: float
      vwap: float
      turnover: float
      change: float
      ingest_ts: ISO8601
      completeness: "full|partial|backfilled"
constraints:
  required_features: [open, high, low, close, volume, vwap, turnover, change]
  uniqueness_key: [ticker, ts_close]
  exactly_once_semantics: false
  idempotent_upsert: true
errors:
  - BAR_MISSING
  - DUPLICATE_BAR
  - WS_DISCONNECTED
  - BACKFILL_REQUIRED
auth:
  method: "oauth2"
  token_refresh: "access token 만료 15분 전 자동 갱신"
  refresh_failure_action: "주문 중단 + Emergency Path 진입"
  token_storage: "메모리 (파일 저장 금지)"
```

## BaseConnector 인프라 (S2-11, 2026-04-26)

> 7개 커넥터(KIS/DART/KRX/Naver/Community/ECOS/USMarket) 공통 추상 기반 클래스.
> `new/src/connectors/base.py`. C2 EventNormalizeContract를 통해 데이터를 공급하는 모든 커넥터의 전제 컴포넌트.
> 제공 기능: `_load_defaults()` (connector_defaults 로드) + `_http_get_json(url, params, headers=None)` (requests + urllib fallback).
> C-번호 별도 부여 없음. C2 소비자 커넥터 인프라 레이어로 분류.

---

## C2. EventNormalizeContract

```yaml
name: EventNormalizeContract
owner: Data Layer / Event Gateway
transport: pull + webhook + crawler
request:
  raw_event:
    source: "naver_news|dart|community|us_market|ecos|krx_investor_flow|price_snapshot"
    raw_payload_ref: string
response:
  event:
    event_id: string
    source: string
    event_type: "news|dart|macro|us_market|community|regime|investor_flow|price_snapshot"
    scope: "ticker:{code}|sector:{name}|market"
    title: string
    summary: string
    occurred_at: ISO8601
    ingest_ts: ISO8601
    priority: "urgent|normal|low"
    llm_required: bool
    ttl: int
    expires_at: ISO8601
    supersedes: string|null
    pit_safe: bool      # PIT-Safety 검증 결과 (불변 원칙 1, false 시 PITViolationError 발생)
    payload: object     # source별 원본 데이터 (하위 구조는 source 종류에 따라 다름)
constraints:
  dedupe_key: [source, occurred_at, scope, title]
  stale_drop: true
  pre_filter:
    source: "news_filter.yaml"
    method: "keyword_match(title, summary)"
    pass_condition: "match_any = true"
    no_match_action: "drop (LLM 미호출)"
    drop_log: "prefilter_drop_log (dead_letter_log와 별도)"
    drop_log_fields: [timestamp, source, title, drop_reason]
    used_by: "Mode B §8.5.1 키워드 진화 (놓친 뉴스 분석)"
errors:
  - MALFORMED_EVENT
  - UNSUPPORTED_SOURCE
  - DUPLICATE_EVENT
  - CRAWL_FAILED
  - CRAWL_TIMEOUT
  - SOURCE_UNAVAILABLE
```

## C3. PreprocessingContract

```yaml
name: PreprocessingContract
owner: Data Layer / Preprocessing Pipeline
input:
  bar_batch_ref: string
  event_batch_ref: string|null
output:
  quant_frame:
    asof: ISO8601
    universe: [ticker]
    features_ref: string
    feature_spec:
      normalization: "robust_zscore_mad"
      missing_policy: ["forward_fill", "cross_sectional_mean"]
      multiscale: ["1m", "5m", "30m", "60m"]
    feature_manifest:
      # SSOT — 모든 피처 컬럼을 명시적으로 열거

      kis_raw:  # KIS 1분봉 원천 8피처
        - open
        - high
        - low
        - close
        - volume
        - vwap
        - turnover
        - change

      multiscale_derived:  # multi-scale 파생
        scales: ["1m", "5m", "30m", "60m"]
        per_scale: ["mean", "std", "min", "max", "trend"]

      us_overnight:  # 미국 시장 (08:30 1회 수집)
        - us_sp500_change
        - us_nasdaq_change
        - us_vix
        - us_soxx_change
        availability: "08:30 KST, d-1 US close"
        time_alignment: "us_close(d-1) → krx_data(d)"

      investor_flow:  # KRX 투자자별
        - foreign_net_buy
        - institutional_net_buy
        - retail_net_buy

      macro:  # ECOS 거시
        - interest_rate
        - usd_krw

      dual_source:  # v3 즉시 반영 — 뉴스/커뮤니티 분리 점수
        - news_score_t
        - comm_score_t_1
        - comm_score_t_2
        - news_comm_divergence
        - community_noise_multiplier
        availability: "08:00~08:30 KST 장전 batch"
        time_alignment: "news_score_t → 당일 / comm_score_t_1, comm_score_t_2 → 지연 반영"
        rationale: "뉴스와 커뮤니티를 동일 텍스트로 합치지 않고, divergence를 uncertainty로 사용"

      alpha_factors:  # LLM 생성 (Mode B에서 갱신)
        source: "Alpha Factor Engine"
        dynamic: true
        manifest_ref: "별도 alpha_factor_manifest로 관리"
  agent_context:
    summaries:
      - scope: "ticker|sector|market"
        text: string
        ts: ISO8601
        pit_safe: true
rules:
  llm_raw_price_exposure: false
  future_leakage: false
errors:
  - FEATURE_BUILD_FAILED
  - PIT_SAFETY_VIOLATION
  - MISSING_BAR_THRESHOLD_EXCEEDED
```

## C3A. DualSourceScoreContract (v3 신규)

```yaml
name: DualSourceScoreContract
owner: Data Layer / DualSource Scorer
mode: "pre-market batch (08:00~08:30 KST)"
input:
  news_ref: "naver_news + dart normalized events"
  community_ref: "community normalized posts"
  ticker_universe: "active 20"
  config_ref: "risk_config.yaml dual_source"
output:
  scores:
    - ticker: string
      asof: ISO8601
      news_score_t: float
      comm_score_t_1: float
      comm_score_t_2: float
      news_comm_divergence: float
      community_noise_multiplier: float
      source_notes: string|null
rules:
  llm_budget_impact: 0
  news_model: "local finbert or equivalent classifier"
  community_model: "spam_rules + manipulation_rules + sentiment_dict + optional small model"
  no_raw_text_to_lightgbm: true
errors:
  - NEWS_SCORE_BUILD_FAILED
  - COMMUNITY_SCORE_BUILD_FAILED
  - DIVERGENCE_COMPUTE_FAILED
```

## C4. SharedMessagePoolContract

```yaml
name: SharedMessagePoolContract
owner: Agent Layer / Blackboard
operations:
  - publish(message)
  - subscribe(filter)
  - ack(message_id)
  - supersede(old_message_id, new_message_id)
  - expire(now)
message_schema:
  message_id: string
  content: string
  cause_by: string
  sent_from: string
  send_to: string|list|null
  priority: "urgent|normal|low"
  confidence: float
  reasoning: string
  evidence_ids: [string]
  uncertainty: string
  prediction: string|null
  risk_level: "low|medium|high"|null
  timestamp: ISO8601
  ttl: int
  expires_at: ISO8601
  scope: string
  event_id: string|null
  supersedes: string|null
  action_type: "signal|alert|veto_recommendation|regime_change|resolution"
  portfolio_patch_id: string|null
delivery_semantics:
  mode: "at-least-once"
  subscriber_filtering: true
  dependency_activation: true
errors:
  - MESSAGE_EXPIRED
  - INVALID_SCOPE
  - SUPERSEDE_CONFLICT
  - MESSAGE_SCHEMA_INVALID   # 2026-04-21 추가: message_schema 필수 필드 누락 또는 enum 위반
  - ACK_TARGET_NOT_FOUND     # 2026-04-21 추가: ack() 대상 message_id 미등록
  - POOL_OVERFLOW            # 2026-04-21 추가: max_pool_size 초과 후 expire 시도도 실패
```

## C5. AgentReportContract

```yaml
name: AgentReportContract
owner: Layer 4 Agents
report_types:
  news_signal:
    payload:
      stance: "buy|sell|neutral"
      confidence: "float 0.0~1.0  # C4 MessagePool propagation용, LLM confidence 보존"
      scope: "ticker:{code}|sector:{name}|market  # C4 MessagePool propagation용"
      ticker: "ticker|null  # scope가 ticker일 때 선택"
      impacted_tickers: [ticker]
      impacted_sectors: [string]
      narrative: string
  risk_warning:
    payload:
      stance: "veto_recommendation|risk_reduce|neutral"
      risk_level: "low|medium|high"
      macro_note_ref: string|null
      micro_note_ref: string|null
      fast_rule_match: "[{rule_id: string, matched_at: ISO8601}]|null"  # v3 추가 — Risk Fast Path에서 매칭된 trigger rule (Hot Path sidecar 결과)
  quant_signal:
    payload:
      scores: [{ticker: string, score: float, confidence: float}]
      anomalies: [{ticker: string, anomaly_type: string, score: float}]
      top10_candidates: [ticker]
      filter:
        min_confidence: "risk_config.yaml에서 로드. 미달 종목은 top10_candidates에서 제외"
  investor_flow_alert:
    payload:
      stance: "risk_reduce|neutral"
      flow_type: "foreign|institutional|retail"
      net_amount: float
      impacted_tickers: [ticker]
      narrative: string
  theme_score:
    payload:
      scores: [{ticker: string, theme_score: float, mention_count: int, sentiment: float}]
      trending_themes: [string]
      ttl: 86400  # 1일 이내 소멸
activation:
  hot_path:
    quant_only: true
  cold_path:
    trigger: ["news_detected", "dart_alert", "vol_spike", "regime_change", "anomaly"]
sla:
  news_agent_max: 10s
  risk_agent_max: 10s
  quant_agent_mode: "no-llm"
  llm_budget:
    source: "risk_config.yaml llm_budget"
    overflow: "gpt-4o fallback"
    tracking: "daily counter per agent"
    # 2026-04-21 추가 (S2-2): LLMRouter 공식 인터페이스
    router_interface:
      module: "new/src/orchestration/llm_router.py"
      class: "LLMRouter"
      method: "call(prompt, mode, caller, structured_schema=None, force_model=None)"
      modes: "cold (Kanana 우선) | mode_b (GPT-4o 전용) | hot (RuntimeError)"
      callers: "risk_config.yaml llm_budget.budget_allocation keys"
      returns: "LLMCallResult (success, model_used, content, latency_ms, tokens, cost_usd, error, fallback_used, circuit_state)"
      mode_validation: "exact enum only. UNKNOWN_LLM_MODE fail-closed for noncanonical mode strings."
      force_model_policy: "Cold Path force_model='gpt-4o' 금지. GPT-4o는 fallback 또는 mode_b에서만 허용."
      provider_policy:
        hot_path: "LLM API 호출 금지. call(mode='hot')는 HOT_PATH_LLM_FORBIDDEN"
        cold_path_primary: "Kanana-o 우선. OpenAI-compatible chat.completions.create(model='kanana-o', messages=[...])"
        cold_path_fallback: "Kanana 실패/한도초과/circuit OPEN 시 GPT-4o fallback"
        mode_b: "GPT-4o 전용. Kanana daily budget 미사용"
        mode_b_runtime_guard: "ELEPHANT_MODE=mode_b + mode_b_scheduler.execution_window 내부에서만 허용"
        kanana_api_url: "KANANA_API_URL은 full endpoint가 아니라 /v1 base URL. 기존 로컬 .env 호환을 위해 KANANA_BASE_URL alias도 허용하되, KANANA_API_URL이 우선한다."
        kanana_schema_policy: "json_schema 지원을 가정하지 않음. prompt-level JSON instruction + local parser fail-closed"
        gpt_storage_policy: "store=false 명시"
        gpt_schema_policy: "structured_schema가 있으면 strict response_format json_schema 사용"
      errors:
        - HOT_PATH_LLM_FORBIDDEN        # mode='hot' 시도
        - UNKNOWN_LLM_MODE              # cold|mode_b|hot 외 문자열
        - COLD_PATH_GPT_FORCE_FORBIDDEN # Cold Path GPT 직접 지정
        - MODE_B_ENV_REQUIRED           # ELEPHANT_MODE != mode_b
        - MODE_B_WINDOW_FORBIDDEN       # execution_window 밖 Mode B GPT 호출
        - MODE_B_CALLER_FORBIDDEN       # mode='mode_b' + caller not in mode_b_allowed_callers
        - DAILY_LIMIT_REACHED           # budget 소진 → fallback trigger
        - CALLER_QUOTA_EXCEEDED         # caller allocation 소진 → fallback trigger
        - KANANA_API_KEY_MISSING        # KANANA_API_KEY env var 없음
        - OPENAI_API_KEY_MISSING        # OPENAI_API_KEY env var 없음
    # 2026-04-23 S2-6 신설 → S2-7 실구현 완료 (2026-04-23)
    news_agent_interface:
      constructor:
        signature: "NewsAgent(llm_router: LLMRouter, pubsub: PubSubBroker | None = None, memory_root: Path | None = None)"
      analyze:
        signature: "analyze(event: dict) -> dict | None"
        event_types: ["news", "dart", "community"]
        publish_channel_map:
          news: news_signal
          community: news_signal
          dart: dart_alert
      consume_text_pack:
        signature: "consume_text_pack(ticker: str, text_pack: str, context: dict | None = None) -> None"
        description: "TSFresh 30분 통계의 자연어 요약을 NewsAgent가 수신. text_pack 내용은 news_filter.yaml text_pack_templates SSOT 경유. analyze() 위임."
        text_pack_ssot: "new/config/news_filter.yaml text_pack_templates (13 매핑)"
      attach_to_gateway:
        signature: "attach_to_gateway(gateway: EventGateway) -> None"
        note: "3 event_type (news/dart/community) 자동 register_handler"
      memory:
        micro_path: "artifacts/agent_memory/news_agent/{ticker}/{YYYYMMDD}.jsonl"
        macro_path: "artifacts/agent_memory/macro/{YYYYMMDD}.jsonl"
        format: "JSONL append (ensure_ascii=False)"
      allowed_publish_channels:
        - news_signal
        - dart_alert
      llm_caller: "news_agent"
      llm_mode: "cold"
      llm_parsing: "Kanana prompt-level JSON instruction + local parser fail-closed. GPT fallback은 C5 strict json_schema 정책 사용 가능."
      narrative_max_chars_source: "news_filter.yaml text_pack_settings.narrative_max_chars (SSOT, 기본 200). 코드 하드코딩 금지 (불변 원칙 5)."
      status: "done"  # S2-7 실구현 완료 (2026-04-23)
    # S2-8 실구현 완료 (2026-04-26)
    risk_agent_interface:
      fast:
        constructor:
          signature: "RiskAgentFast(pubsub: PubSubBroker | None = None)"
          module: "new/src/agents/cold/risk_fast.py"
        evaluate:
          signature: "evaluate(event: dict, context: dict[str, float] | None = None) -> dict"
          context_keys:
            - comm_volume_zscore
            - comm_sentiment_delta
            - intraday_return_zscore
            - foreign_net_sell_krw
            - news_comm_divergence
          returns: "{risk_level, stance, fast_rule_match, triggered_rules, recommended_action, latency_ms}"
          sla: "<50ms (비LLM 규칙 기반)"
        allowed_publish_channels:
          - risk_warning
          - uncertainty_signal
        report_type: "risk_warning"
        thresholds_source: "risk_config.yaml risk_fast.cold_path (SSOT)"
        sign_convention: "foreign_net_sell_krw 음수 컨벤션 (순매도 = 음수)"
        status: "done"
      slow:
        constructor:
          signature: "RiskAgentSlow(llm_router: LLMRouter, pubsub: PubSubBroker | None = None, memory_root: Path | None = None)"
          module: "new/src/agents/cold/risk_slow.py"
        analyze:
          signature: "analyze(event: dict, fast_eval: dict | None = None) -> dict | None"
          description: "Kanana-o CoT 심층 분석. fast_eval 포함 시 기존 판단 컨텍스트 추가."
          returns: "C5 risk_warning 리포트 (channel 분기 포함)"
          sla: "10s (C5 risk_agent_max)"
        publish_channel_map:
          stance_veto_recommendation: veto_recommendation
          regime_signal_true: regime_change
          otherwise: risk_warning
        report_type: "risk_warning"  # report_type은 항상 risk_warning (C5 VALID_REPORT_TYPES SSOT)
        allowed_publish_channels:
          - risk_warning
          - regime_change
          - veto_recommendation
        memory:
          macro_path: "artifacts/agent_memory/risk_agent/macro/{YYYYMMDD}.jsonl"
          format: "JSONL append (ensure_ascii=False)"
        llm_caller: "risk_agent"
        llm_mode: "cold"
        narrative_max_chars_source: "risk_config.yaml risk_fast.narrative_max_chars (SSOT, 기본 300)"
        status: "done"
    # S2-9 실구현 완료 (2026-04-26)
    debate_agent_interface:
      constructor:
        signature: "DebateAgent(llm_router: LLMRouter, pubsub: PubSubBroker | None = None, memory_root: Path | None = None)"
        module: "new/src/agents/cold/debate.py"
      run_debate:
        signature: "run_debate(signals: list[dict], candidates: list[str] | None = None) -> dict"
        description: "에이전트 신호 충돌 감지 후 pairwise CoT 실행. 충돌 없으면 skip."
        returns: "{conflict_detected, debate_id, winner_view, ranked_tickers, uncertainty_delta, comparison_count, ...}"
        activation: "충돌 감지 시만 (conflict_criteria 3패턴)"
        fallback: "LLM 실패 시 heuristic (risk 신호 우선)"
      max_pairwise_source: "risk_config.yaml debate.max_pairwise (SSOT, C6 기본 45)"
      uncertainty_threshold_source: "risk_config.yaml debate.uncertainty_threshold (SSOT, 기본 0.7)"
      allowed_publish_channels:
        - debate_resolution
        - pairwise_ranking
      memory:
        debate_history_path: "artifacts/agent_memory/debate_agent/{YYYYMMDD}.jsonl"
        format: "JSONL append (ensure_ascii=False)"
      llm_caller: "debate_agent"
      llm_mode: "cold"
      status: "done"
    # S2-9 FDA Cold Path 실구현 완료 (2026-04-26)
    fda_cold_path_interface:
      decide_cold:
        signature: "decide(..., mode='cold', debate_result: dict | None = None, agent_signals: list[dict] | None = None) -> dict"
        returns: "final_decision + mode='cold'"
        decision_flow:
          - "MISSING_PORTFOLIO_PATCH check"
          - "debate uncertainty > threshold → DEBATE_CONFLICT veto"
          - "risk_warnings veto_recommendation → RISK_FAST_TRIGGER veto"
          - "Kanana-o CoT (llm_router 없으면 fail-closed veto)"
      uncertainty_threshold_source: "risk_config.yaml debate.uncertainty_threshold (SSOT)"
      llm_caller: "fda_cold_path"
      llm_mode: "cold"
      can_change_weight: false
      status: "done"
```

## C6. DebatePairwiseRankingContract

```yaml
name: DebatePairwiseRankingContract
owner: Debate Agent
input:
  mode: "pairwise_request|conflict_detected"
  candidates: [ticker]
  reports_ref: [message_id]
output:
  debate_resolution:
    conflict_id: string|null
    winner_view: "news|risk|quant|mixed"
    reasoning: string
  pairwise_ranking:
    comparison_count: int
    wins: [{ticker: string, win_count: int}]
    ranked: [ticker]
    top_k: [ticker]
activation:
  hot_path: false  # 평상시 호출 안 됨
  cold_path:
    trigger: "conflict_detected"  # 퀀트 vs 에이전트 충돌 시만
    conflict_criteria:
      - "quant_top10 vs agent_veto_recommendation 상반"
      - "quant_buy vs risk_high 동시"
      - "news_sell vs quant_top5 동시"
    fallback_if_no_conflict: "퀀트 점수 순 랭킹 유지, Debate 미호출"
rules:
  max_pairwise_for_top10: 45
  comparator_non_transitive: true
  evidence_required: true
sla:
  max_runtime: 15s
errors:
  - TOO_MANY_CANDIDATES
  - MISSING_REPORTS
  - LATENCY_BUDGET_EXCEEDED
```

## C7. PPOAllocatorContract

```yaml
name: PPOAllocatorContract
owner: Model Layer / PPO Allocator
input:
  top_k: [ticker]
  quant_scores: [{ticker: string, score: float, confidence: float}]
  current_positions: [{ticker: string, qty: float, weight: float}]
  market_state:
    cash_ratio: float
    sector_exposure: {sector: float}
    regime_state: string|null
output:
  allocation_plan:
    target_weights: {ticker: float}
    cash_weight: float
    policy_version: string
    constraints_applied:
      max_names: int
      max_single_name: float
      max_sector: float
      min_cash: float
rules:
  state: ["quant_signal", "current_position", "market_state"]
  action: "continuous_weights_softmax"
  reward: "return_minus_transaction_cost"
  inference_only_intraday: true
  retrain_after_close: true
  turnover_cap:
    daily_max: "risk_config.yaml에서 로드. 초과 시 주문 축소"
  regime_gate:
    source: "risk_config.yaml"
    effect: "red → 신규 진입 금지, yellow → 비중 50% 감축"
config_defaults:
  max_names: 10
  max_single_name: 0.20
  max_sector: 0.40
  min_cash: 0.10
errors:
  - INVALID_WEIGHT_SUM
  - CONSTRAINT_VIOLATION
  - POLICY_NOT_LOADED
```

> **주의**: config_defaults 값은 default이며 final freeze 전. 구현 시 risk_config.yaml에서 로드해야 함. 하드코딩 금지.

## C8. PortfolioDeltaPlannerContract

```yaml
name: PortfolioDeltaPlannerContract
owner: Layer 1 / Portfolio Manager
input:
  target_weights: {ticker: float}
  current_positions: [{ticker: string, qty: float, weight: float}]
  latest_prices: {ticker: float}
  portfolio_value: float
output:
  portfolio_patch:
    portfolio_patch_id: string
    based_on_ts: ISO8601
    target_weights: {ticker: float}
    order_deltas:
      - ticker: string
        side: "buy|sell"
        qty: int
        reason: "rebalance|exit|risk_reduce|cash_raise|paper_trading_probe"
rules:
  source_of_truth: "Portfolio Manager generates order_deltas"
  fda_may_edit: false
  excluded_holding_policy: "weight=0 generates sell delta"
  cold_path_exit_trigger:
    - quant_anomaly
    - risk_veto
  order_validation:
    - price_gt_zero: true
    - qty_gt_zero: true
    - ticker_in_universe: true
    - order_value_lt_position_limit: true
    - daily_pnl_check: "daily_pnl <= -risk_config.daily_loss_threshold"
errors:
  - PRICE_UNAVAILABLE
  - LOT_SIZE_ERROR
  - NEGATIVE_QTY
```

## C9. FDADecisionContract

> v3.3 non-breaking extension (2026-04-21): uncertainty_score input 필드 추가.
> output schema (final_decision 블록) 는 변경 없음. Sprint 0 freeze 유지.
> 하드코딩 금지: uncertainty_threshold/veto_prior_boost 는 risk_config.yaml fda_uncertainty_link 에서 로드.

```yaml
name: FDADecisionContract
owner: FDA
input:
  active_reports: [message_id]
  portfolio_patch_ref: string
  dependency_status:
    news: "done|skipped|timeout"
    risk: "done|skipped|timeout"
    quant: "done|skipped|timeout"
    debate: "done|skipped|timeout"
  # v3.3 추가 (2026-04-21): Dual-Source divergence uncertainty 입력
  # PIT-Safety: divergence 는 장전 배치(08:00~08:30) 점수 기반 → 실시간 미래 참조 없음
  uncertainty_score:
    type: float
    range: [0.0, 1.0]
    default: 0.0
    source: "Risk Fast sidecar publishes when dual_source.divergence > threshold (C4 channel: uncertainty_signal)"
    effect: |
      uncertainty_score >= risk_config.yaml fda_uncertainty_link.uncertainty_threshold
      → veto_prior 가 fda_uncertainty_link.veto_prior_boost 만큼 상향
      → final_decision.veto 확률 증가 (reason_code 후보: NEWS_COMMUNITY_DIVERGENCE)
    pit_safety: "실시간 publish 가능. divergence 는 장전 배치 점수 기반 → PIT-Safe"
output:
  final_decision:
    decision_id: string
    approved: bool
    target_weights: {ticker: float}   # read-only echo
    order_deltas: [{ticker: string, side: string, qty: int, reason: string}]  # read-only echo
    veto_reason: string|null
    reason_code: string|null                # cause 분류 코드 (cause-centric 설계용). approve/veto 양쪽에서 항상 채움. S2-9 완료 (2026-04-26) + community risk sidecar 확장(2026-05-18) enum 11종: NEWS_DIVERGENCE / RISK_FAST_TRIGGER / COMMUNITY_LIVE_PROXY_RISK / NEWS_COMMUNITY_DIVERGENCE / COMMUNITY_TIMESTAMP_WEAK / COMMUNITY_MANIPULATION_FLAG / DEBATE_CONFLICT / NORMAL_APPROVE / TIMEOUT / QUANT_ANOMALY / MISSING_PORTFOLIO_PATCH. SSOT: risk_config.yaml reason_code_catalog (status=final).
    risk_overrides: [{rule: string, original: string, override: string, justification: string}]
    confidence: float
    expiry: ISO8601
    portfolio_patch_ref: string             # trace-only. C8 patch 참조. FDA가 order_deltas를 생성/수정하는 권한 아님.
    active_reports: [message_id]            # trace-only. 판단 당시 참조한 active report id 목록.
rules:
  can_change_weight: false
  must_include_reasoning: true
  veto_if_uncertain: true
  dependency_wait: "all active agents or timeout"
  reason_code_required: true                # 모든 final_decision은 reason_code 필수 (cause 중심 설계)
errors:
  - MISSING_PORTFOLIO_PATCH
  - EXPIRED_DECISION
  - ILLEGAL_DELTA_MODIFICATION_ATTEMPT
  - MISSING_REASON_CODE
```

> **주의**: risk_overrides는 audit metadata 전용. FDA가 실제 리스크 규칙을 변경하는 경로로 사용 금지. `ILLEGAL_DELTA_MODIFICATION_ATTEMPT` 에러로 order_deltas 수정 시도 시 런타임 거부.

## C10. ExecutionFeedbackContract

```yaml
name: ExecutionFeedbackContract
owner: Layer 1 / Execution Gateway
input:
  final_decision_ref: string
  approved: bool
  order_deltas: [{ticker: string, side: string, qty: int, reason: string}]
  execution_mode: "mock|paper|live"  # default: mock. live_enabled=false이면 live 무시
  live_enabled: false                # 실계좌 주문 스위치. 기본값 꺼짐
output:
  execution_report:
    order_plan_id: string
    submitted_at: ISO8601
    status: "submitted|rejected|filled|partial_filled|cancelled"
    fills:
      - ticker: string
        side: string
        qty: int
        avg_fill_price: float
        fill_ts: ISO8601
    estimated_cost: float
    realized_slippage: float
  feedback_record:
    kb_message_id: string
    pnl_contribution: float
    execution_shortfall: float
    lesson_stub: string|null
rules:
  if approved=false: "no order submission"
  mode_guard:
    if execution_mode=mock: "주문 미발송, mock 체결 결과 생성"
    if execution_mode=paper: "KIS 모의투자 서버로 주문"
    if execution_mode=live: "KIS 운영 서버로 주문 (live_enabled=true 필수)"
    if live_enabled=false AND execution_mode=live: "REJECTED — live 스위치 꺼짐"
  after_execution:
    - record_slippage
    - record_cost
    - update_memory
    - write_kb_entry
  reconciliation:
    frequency: "매 실행 사이클 후"
    compare: "system_positions vs kis_actual_positions"
    on_mismatch: "알림 + system_positions = kis_actual_positions으로 리셋"
  audit_log:
    format: "JSON Lines"
    fields: [timestamp, order_id, ticker, side, qty, price, status, latency_ms, error]
    retention: "최소 1년"
  kill_switch:
    trigger: "daily_pnl <= -daily_loss_threshold (config는 양수 절대값)"
    action: "전량 시장가 청산 + EMERGENCY_HALT"
    threshold_source: "risk_config.yaml (하드코딩 금지)"
    manual_override: "CLI 명령으로 수동 발동 가능"
known_gaps_current_phase:
  - partial_fill_logic_not_implemented
  - amend_cancel_not_implemented
  - child_order_split_not_implemented
  - order_queue_not_implemented
  - session_auction_logic_not_implemented
  - websocket_failover_not_implemented
  - api_rate_limit_governor_not_implemented
```

## C11. EventAdmissionControlContract

```yaml
name: EventAdmissionControlContract
owner: Agent Layer / Event Admission
input:
  incoming_events: [event]
output:
  admitted_events: [event_id]
  dropped_events: [{event_id: string, reason: string}]
  dead_letter_log:
    format: "JSON Lines"
    fields: [timestamp, event_id, drop_reason, original_event_ref]
    retention_days: 30
    used_by: "Mode B §8.5.1 nightly keyword evolution"
rules:
  dedupe_by: [event_id, supersedes]
  priority_order: ["market", "sector", "ticker"]
  stale_drop: "expires_at < now"
  max_cold_path_jobs_per_minute: int
  conflict_merge: true
  comparator:
    sort_key: ["priority", "trigger_type", "scope", "recency"]
    priority_order: "urgent > normal > low"
    trigger_order: "vol_spike > dart_alert > news_detected > regime_change > anomaly"
    scope_order: "market > sector > ticker"
    recency: "newest first"
  trigger_catalog_ref: "risk_config.yaml trigger_catalog"  # v3 추가 — {trigger → action} 매핑 로드. Risk Fast Path sidecar에서 사용
  trigger_catalog_mode_b_editable: true                     # Mode B 야간 갱신 대상 (§8.7 ⑨)
  backlog_overflow:
    action: "drop lowest priority → dead_letter_log"
    max_backlog_vs_jobs_per_minute: "backlog = concurrent slots, jobs_per_minute = throughput cap"
errors:
  - DUPLICATE_EVENT_ID    # dedupe cache 동일 event_id
  - SUPERSEDED            # supersedes 참조가 이미 처리된 event_id
  - STALE                 # expires_at < now
  - BACKLOG_OVERFLOW      # max_backlog 초과
  - JOBS_PER_MINUTE_CAP   # 최근 60초 처리 수 >= max_cold_path_jobs_per_minute
```

> GPT Pro "반드시 추가" 권고. architecture.md §7.2 Cold Path 이벤트 관리 정책(C-2)의 공식 계약서.

## C12. BacktestAgentContract

> **v2.2 신규**. Backtest Agent는 Mode B 전용 시스템 검증자이며, 장중 경로(Hot/Cold)에 절대 개입하지 않는다.
> 교수님(2026-04-03) "백테스트 에이전트로" + GPT Pro(2026-04-05) "validation tooling으로" CROSS-CONFLICT를
> **하이브리드**로 해소: Backtest Agent(판단 주체) + C13 ValidationTools(계산 실행자).

```yaml
name: BacktestAgentContract
owner: Agent Layer / Backtest Agent (Mode B only)
mode: "Mode B (18:00~22:00 KST)"
llm: "GPT-4o (Mode B 전용). 장중 Kanana-o 100회/일 예산 보존"

subscribes_to:
  - factor_candidate          # Alpha Factor Engine → 새 팩터 후보 (§8.3)
  - model_candidate           # Co-STEER → 재학습 모델 후보 (§8.4)
  - allocator_candidate       # PPO retrain → 새 allocator 정책
  - mode_b_performance_vector # 오늘 성과 8차원 벡터 (§8.1)
  - dual_source_feature_pack    # v3 즉시 반영 여부 (with / without ablation)

publishes:
  - backtest_report           # 시스템 검증 결과 (pass|fail|warn + diagnostic)
  - regression_flag           # 회귀 위험 감지 시 경고
  - deploy_recommendation     # 22:00 배포 게이트 권고

input:
  candidate_bundle:
    factor_ref: "string|null"
    model_ref: "string|null"
    allocator_ref: "string|null"
    source: "alpha_factor_engine|co_steer|ppo_retrain"
  baseline_ref: "string"             # 직전 배포 버전 (비교 대상)
  date_range:
    start: "YYYY-MM-DD"
    end: "YYYY-MM-DD"                # walk-forward validation 범위
  validation_tools:
    - BacktestEngine
    - ReplayRunner
    - PerformanceAnalyzer

output:
  backtest_report:
    bundle_id: "string"
    backtest_id: "BT-{yyyymmdd}-{UUID8}"   # SHIP-fix R-4 (2026-05-06): identity backtest_run_id 와 동일 의미. 코드 일관성 위해 backtest_id 채택
    started_at: "ISO8601"
    finished_at: "ISO8601"
    verdict: "pass|fail|warn"
    metrics:
      ic: "float"
      icir: "float"
      rank_ic: "float"
      arr: "float"
      ir: "float"
      mdd: "float"
      sr: "float"
    regime_breakdown:
      - regime: "bull|bear|sideways|volatile"
        sharpe: "float"
        mdd: "float"
        n_days: "int"
    ablation:
      - component: "factor|model|allocator"
        delta_sharpe: "float"
        significance: "float"
    regression_risk:
      flagged: "bool"
      evidence: "[string]"
      severity: "low|medium|high"
    diagnostic_notes: "string"
    llm_reasoning_ref: "string"      # GPT-4o CoT 요약 artifact ref
    # v3 추가 — 산출물 분리
    failure_case_cards:               # 일반 실패/교훈 카드
      - case_id: "FCC-{yyyymmdd}-{UUID8}"   # v3.5 정정 (identity 블록 SSOT 일치)
        regime: "bull|bear|sideways|volatile"
        trade_id: "string"
        expected_pnl: "float"
        actual_pnl: "float"
        root_cause: "string"
        severity: "low|medium|high"
        lesson: "string"
    regression_cases:                 # 회귀 위험 전용 증거 (FailureCaseCard와 분리)
      - case_id: "RGC-{yyyymmdd}-{UUID8}"   # v3.5 정정 (identity 블록 SSOT 일치)
        baseline_sharpe: "float"
        candidate_sharpe: "float"
        delta_sharpe: "float"
        regime: "string"
        root_cause: "string"
        severity: "low|medium|high"
    minute_bar_leakage_check:         # v3 추가 — 분봉 leakage 검증 결과
      purge_bars_used: "int"
      embargo_bars_used: "int"
      replay_unit: "1m"
      leakage_detected: "bool"
      verdict: "pass|fail"
    feature_quality:                  # C14 deploy 필수 evidence
      dual_source_rows: "int"
      dual_source_non_neutral_rows: "int"
      exogenous_rows: "int"
      exogenous_non_neutral_rows: "int"
    service_policy_replay:            # C14 deploy 필수 evidence (read-only KIS paper policy replay)
      status: "PASS|BLOCKED|MISSING|UNREADABLE"
      service_policy_report_path: "string"
      service_policy_report_sha256: "string"
      universe: "[ticker]"                 # padded, sorted final deploy universe
      universe_count: "int"                # v1 final gate requires 30
      universe_hash: "sha256"              # hash of canonical universe list
      universe_policy: "final_dataset_gate|operator_override"
      gate:
        status: "PASS|BLOCKED"
        blockers: "[string]"
      policy_checks:
        deploy_candidate_by_service_policy: "bool"
        no_naked_short_exposure: "bool"
        order_caps_respected: "bool"
        cash_guard_respected: "bool"
      order_stats:
        total_orders: "int"
        naked_short_attempts: "int"

memory:
  type: "backtest_history"
  scope: "과거 검증 bundle + 회귀 위험 기록 + 배포 후 실 성능 추적"
  decay: "없음 (전체 이력 보존)"
  performance_metrics:
    backtest_precision: "pass 판정 후 실제 prod에서 기대 성능 유지 비율 (precision of 'pass')"
    regression_catch_rate: "회귀 위험 조기 탐지율 (recall of regression)"
    verdict_accuracy: "pass|fail|warn 판정의 사후 정확도"

rules:
  can_modify_target_weights: false          # PPO Allocator 고유 권한
  can_generate_order_deltas: false          # Portfolio Manager 고유 권한
  can_bypass_fda: false                     # FDA=최종 게이트 원칙 유지
  can_intervene_hot_path: false             # 장중 실시간 매매 루프 완전 차단
  can_write_to_production: false            # 배포는 별도 게이트 (operator 승인 포함 가능)
  deploy_decision_gate:
    condition: "verdict == 'pass' AND regression_risk.flagged == false AND minute_bar_leakage_check.verdict == 'pass' AND feature_quality gate pass AND service_policy_replay.status == 'PASS'"  # v3 leakage + C14 evidence 조건 추가
    # SHIP-fix MA-4 (2026-05-06): verdict 계산 임계값 SSOT = risk_config.yaml backtest_agent.deploy_decision_gate
    # pass_sr_threshold (default 0.0): SR >= 임계값 AND IC >= pass_ic_threshold → "pass"
    # pass_ic_threshold (default 0.0)
    # warn_sr_threshold (default -0.5): SR >= 임계값 OR IC >= warn_ic_threshold → "warn", 그 외 "fail"
    # warn_ic_threshold (default -0.1)
    # severity 임계값 SSOT = risk_config.yaml backtest_agent.deploy_decision_gate (severity_*_sr_threshold 3개)
    on_fail: "Mode B 22:00 배포 차단 + dead_letter_log 기록 + 다음 날 baseline 유지"
    on_warn: "operator 수동 확인 필요 (human_approval: true)"
    on_pass: "자동 배포 승인 (human_approval: false) OR operator 승인 (human_approval: true)"
  forbidden_fields_in_output:
    - target_weights
    - order_deltas
    - approved
    - veto_reason
    - portfolio_patch_id

forbidden_permissions:
  - target_weights_modification
  - order_deltas_generation
  - fda_bypass
  - hot_path_intervention
  - shared_message_pool_publish_during_market_hours
  - production_direct_write

errors:
  - CANDIDATE_BUNDLE_INVALID
  - BASELINE_NOT_FOUND
  - VALIDATION_TOOL_TIMEOUT
  - REGRESSION_DETECTED
  - LLM_REASONING_FAILED
  - DEADLINE_MISSED

sla:
  max_runtime: 3600                # 60분 (21:00~22:00 배포 직전)
  deadline: "21:30 KST (22:00 배포 게이트 30분 전)"
  retry_policy: "1회 재시도 후 fail → baseline 유지"
```

> v2.2 설계 원칙: "Agent = 판단 주체, Tools = 계산 실행자". Backtest Agent는 GPT-4o로 검증 결과에
> CoT reasoning을 붙여 diagnostic_notes를 생성하며, 실제 walk-forward/replay/ablation 계산은 C13의 3개 tool이 수행한다.

## C13. ValidationToolsContract

> **v2.2 신규**. Backtest Agent가 사용하는 3개 validation tool의 계약서.
> 이 tool들은 에이전트가 아니며 (LLM 호출 없음), 결정론적 계산 엔진이다.

```yaml
name: ValidationToolsContract
owner: Agent Layer / Backtest Agent (sole consumer)
mode: "Mode B only"
called_by: "BacktestAgent only"
forbidden_callers:
  - FDA
  - PortfolioManager
  - QuantAgent
  - NewsAgent
  - RiskAgent
  - DebateAgent
  - ExecutionGateway
  - HotPath

tools:

  BacktestEngine:
    purpose: "후보 bundle에 대한 walk-forward 백테스트 실행"
    deterministic: true
    llm: false
    input:
      bundle_ref: "string"
      baseline_ref: "string"
      universe: "[ticker]"
      date_range:
        start: "ISO8601"
        end: "ISO8601"
      execution_cost_model: "slippage_v1"  # risk_config.yaml에서 로드
      replay_resolution: "1m"
      # v3 추가 — 분봉 leakage control. SSOT: risk_config.yaml
      #   purge_bars / embargo_bars → validation_tools.backtest_engine (현재 60 / 78, Prado 1% rule)
      #   fold / window 파라미터 → walk_forward (n_splits / train_window_days / test_window_days)
      config_ref: "risk_config.yaml: validation_tools.backtest_engine + walk_forward"
      purge_bars: "int"                # risk_config.yaml에서 로드. 분봉 수 기준.
      embargo_bars: "int"              # risk_config.yaml에서 로드. 분봉 수 기준.
      replay_unit: "1m"               # 분봉 기준 replay. 일봉("1d")에서 전환.
    output:
      run_id: "string"
      started_at: "ISO8601"
      finished_at: "ISO8601"
      metrics:
        ic: "float"
        icir: "float"
        rank_ic: "float"
        arr: "float"
        ir: "float"
        mdd: "float"
        sr: "float"
      daily_pnl: "[float]"
      trade_log: "[{ticker, side, qty, price, ts, slippage}]"
      bar_count: "int"
    errors:
      - BUNDLE_LOAD_FAILED
      - DATA_UNAVAILABLE
      - NAN_IN_METRICS
    sla:
      max_runtime: 1800           # 30분
      max_concurrent: 1

  ReplayRunner:
    purpose: "과거 N거래일 1분봉+이벤트 replay로 end-to-end 판단 재현"
    deterministic: true
    llm: false
    input:
      bundle_ref: "string"
      date_range:
        start: "ISO8601"
        end: "ISO8601"
      event_sources:
        - naver_news
        - dart
        - community
        - us_market
        - ecos
        - krx_investor_flow
      mode: "deterministic_replay"
      seed: "int"
    output:
      run_id: "string"
      replay_trace_ref: "string"  # decision_id별 전체 과정 기록 (KB artifact)
      agent_activation_count:
        news: "int"
        risk: "int"
        quant: "int"
        debate: "int"
        fda: "int"
      cold_path_latency:
        p50: "int"
        p95: "int"
        p99: "int"
      hot_path_latency:
        p50: "int"
        p95: "int"
        p99: "int"
      anomaly_count: "int"
    errors:
      - REPLAY_DIVERGENCE
      - SEED_MISMATCH
      - EVENT_REPLAY_FAILED
    sla:
      max_runtime: 2400           # 40분
      idempotent: true

  PerformanceAnalyzer:
    purpose: "regime breakdown + ablation + baseline 대비 비교"
    deterministic: true
    llm: false
    input:
      backtest_run_id: "string"
      baseline_run_id: "string"
      regime_labels: "[{date: ISO8601, regime: string}]"
      ablation_components:                                                     # v3.5: dual_source 추가 (S4-1). S4-2: description 확장.
        - name: factor
          description: "Alpha Factor Engine 기여도 (on/off)"
          measurement: "w/ vs w/o factor IC/Sharpe delta"
        - name: model
          description: "LightGBM 모델 구조 기여도"
          measurement: "baseline vs LightGBM Sharpe delta"
        - name: allocator
          description: "PPO Allocator 비중 최적화 기여도"
          measurement: "equal-weight vs PPO Sharpe delta"
        - name: dual_source
          description: "Dual-Source 5피처 (news_score_t + comm_score_t_1/t_2 + news_comm_divergence + community_noise_multiplier)"
          measurement: "w/ vs w/o dual_source ablation, IC/Sharpe/MDD delta. S4-2 baseline_with_dual_source.pkl 생성."
    output:
      run_id: "string"
      regime_breakdown:
        - regime: "bull|bear|sideways|volatile"
          sharpe: "float"
          mdd: "float"
          n_days: "int"
      ablation:
        - component: "factor|model|allocator|dual_source"
          delta_sharpe: "float"
          delta_mdd: "float"
          significance: "float"
      baseline_comparison:
        delta_sharpe: "float"
        delta_mdd: "float"
        delta_arr: "float"
        verdict: "improved|degraded|neutral"
      regression_risk:
        flagged: "bool"
        evidence: "[string]"
        severity: "low|medium|high"
    errors:
      - BASELINE_MISSING
      - REGIME_LABEL_MISSING
      - ABLATION_INFEASIBLE
    sla:
      max_runtime: 600            # 10분

rules:
  result_persistence: "KB with TTL=30days"
  audit_log: "모든 tool 호출은 run_id로 감사 추적"
  consistency: "같은 input + seed → 같은 output 보장"

global_errors:
  - TOOL_TIMEOUT
  - TOOL_CRASH
  - RESOURCE_EXHAUSTED
```

> v2.2 설계 원칙: 이 3개 tool은 **LLM을 호출하지 않는다**. 결정론적 계산만 수행한다.
> LLM reasoning은 Backtest Agent(C12)에서만 수행한다 — 검증 결과에 diagnostic_notes를 붙이는 용도.
> 이 분리로 (a) validation 재현성 확보, (b) LLM 비용 국한, (c) tool 단위 테스트 가능성을 얻는다.

## C14. ModeBSchedulerContract

> **v2.2 신규**. Mode B 전용 cron-style orchestrator. 18:00~22:00 6단계를 순차 호출하며,
> bundle_id 생성 + 상태 전이(§7.4 MODE_B_*) + mode_b_audit_log 기록을 담당한다.
> Mode B Deployer는 이 Scheduler의 sub-component이다.

```yaml
name: ModeBSchedulerContract
owner: Agent Layer / Mode B Scheduler (singleton)
mode: "mode_b only"
lifecycle: "BootStrap 시 기동, 시스템 생명주기 동안 상주"

triggers:
  cron_based:
    - time: "18:00 KST"
      action: "stage_0_dqr"
      description: "DQR 일별 측정. CRITICAL alert 시 파이프라인 차단 (S4-5)"
    - time: "18:02 KST"
      action: "stage_1_performance_analysis"       # §8.1 (stage_0 완료 후)
    - time: "18:30 KST"
      action: "stage_2_direction_selection"        # §8.2
    - time: "19:00 KST"
      action: "stage_3_factor_evolution"           # §8.3
    - time: "20:00 KST"
      action: "stage_4_model_evolution"            # §8.4 + LGBMTrainer 재학습 (C17 save())
    - time: "20:30 KST"
      action: "stage_5_agent_self_improvement"     # §8.5
    - time: "21:00 KST"
      action: "stage_6_backtest_validation"        # §8.5.2 (v2.2 게이트)
    - time: "22:00 KST"
      action: "stage_7_deploy"                     # §8.6
  weekdays: ["MON", "TUE", "WED", "THU", "FRI"]    # 평일만

responsibilities:
  - bundle_id_issuance:
      format: "BUNDLE-{yyyymmdd}-{UUID8}"   # v3.5 정정 (identity 블록 SSOT 일치)
      issued_at: "stage_3 시작 시"
      includes: [factor_ref, model_ref, allocator_ref]
  - state_transitions:
      source: "architecture.md §7.4 MODE_B_* states"
      sequence:
        - "HOT_RUNNING → MODE_B_IDLE"               # 15:30 장 마감
        - "MODE_B_IDLE → MODE_B_EVOLVING"            # 18:00
        - "MODE_B_EVOLVING → MODE_B_BACKTEST"        # 21:00
        - "MODE_B_BACKTEST → MODE_B_DEPLOY|OPERATOR_REVIEW|BLOCKED"  # 21:30 verdict
        - "MODE_B_DEPLOY → MODE_B_IDLE"              # 22:00 완료
        - "MODE_B_IDLE → BOOTSTRAP"                  # 다음 날 09:00
  - audit_log:
      file: "mode_b_audit_log.jsonl"
      format: "JSON Lines"
      fields:
        - timestamp
        - stage
        - duration_sec
        - bundle_id
        - verdict            # stage_6/7만
        - regression_severity
        - deploy_result
        - operator_approval
        - error
      retention: "최소 1년"
  - backtest_skip_condition:
      condition: "MODE_B_EVOLVING 단계에서 factor_candidate/model_candidate/allocator_candidate 모두 없음"
      action: "Backtest Agent 호출 건너뛰기 → 직접 MODE_B_IDLE 전이 (baseline 유지)"

sub_components:
  - name: DQRRunner
    role: "stage_0 데이터 품질 측정 실행자 (S4-5)"
    inputs:
      - date_str
      - "risk_config.yaml dqr 섹션"
    outputs:
      - "DQR report JSON"
      - "alerts JSONL"
    critical_block: true  # CRITICAL alert 시 파이프라인 차단
  - name: ModeBDeployer
    role: "배포 실행자 (22:00)"
    inputs:
      bundle_id: string
      backtest_verdict: "pass"
      sanity_check_result: "ok"
      candidate_bundle_root: "artifacts/bundles/{bundle_id}"
    actions:
      - validate_candidate_bundle       # live artifact 변경 전 required 4종 존재/비어있지 않음 확인
      - atomic_swap_factor_zoo
      - model_registry_replace          # LightGBM 모델 교체
      - committee_model_replace         # v3.5 추가: AlphaGAT Stage II Committee (S3-8)
      - ppo_allocator_update
      - agent_constraint_update
      - rollback_on_failure
    rules:
      called_only_if_verdict_pass: true
      candidate_source_must_not_equal_live_dest: true
      missing_candidate_blocks_before_backup: true
      rollback_on_failure: true
      human_approval_for_warn: true
  - name: EvalRunner
    role: "L3 평가 지표 일별 산출자 (W2 P1, 2026-05-09 SHIP)"
    invocation: "stage_1 이후 Mode B 18:02 KST batch (또는 별도 cron)"
    sub_modules:
      - "new/src/eval/cause_attribution.py"      # cause_attribution_accuracy
      - "new/src/eval/reason_code_stats.py"      # reason_code_distribution + Top-3 coverage
    inputs:
      - "artifacts/audit_log.jsonl (C18 18 필드, label_t5_ret post-hoc backfilled)"
      - "risk_config.yaml system_os_metrics 임계값"
    outputs:
      - "artifacts/metrics/cause_attribution_YYYYMMDD.json"
      - "artifacts/metrics/reason_code_stats_YYYYMMDD.json"
    pit_safety: "label_t5_ret None entry 자동 skip (장중 backfill 위반 방지)"
    critical_block: false  # L3 산출 실패는 alert만, 배포 차단 X

forbidden_permissions:
  - hot_path_intervention
  - fda_bypass
  - target_weights_modification
  - order_deltas_generation

errors:
  - STAGE_TIMEOUT
  - BUNDLE_INCOMPLETE
  - BACKTEST_AGENT_UNAVAILABLE
  - DEPLOYER_ROLLBACK_FAILED
  - CRON_MISFIRE

sla:
  total_window: "18:00~22:00 KST (4시간)"
  stage_timeouts:
    stage_0: 120        # DQR (S4-5)
    stage_1: 30         # 성과 분석
    stage_2: 60         # 방향 결정
    stage_3: 3600       # 팩터 진화 (1시간)
    stage_4: 1800       # 모델 진화 (30분)
    stage_5: 1800       # 에이전트 자기 개선 (30분)
    stage_6: 1800       # Backtest Agent (30분)
    stage_7: 900        # 배포 (15분)
```

> v2.2 권한 분리: Backtest Agent(C12)=판단, Mode B Deployer(C14 sub)=실행.
> 두 역할이 분리되어야 "Backtest Agent가 production에 직접 쓰기 없음"(C12 forbidden_permissions) 원칙이 강제된다.

## 구현 순서 (GPT Pro Sprint 계획)

> MVP 목표: "수익이 아니라 1분봉 입력 → 구조화된 판단 → mock execution → 피드백 저장이 안정적으로 도는 것"
> 첫 2주: 하나의 결정이 end-to-end로 흐르는 것 하나만.
> 틀린 시작: 25개 모듈 동시 개발 + live execution까지 한 번에

### Sprint 0 — 사양 동결
C4(Message Pool), C8(PM), C9(FDA), C10(Execution), C11(EventAdmission) freeze.
TTL/expiry/supersedes는 이미 SSOT로 확정.

### Sprint 1 — 최소 수직 슬라이스 (9개)
KIS Gateway, Preprocessing, Quant Agent, Top-10 filter,
Debate **stub**, PPO inference **stub**, Portfolio Manager, FDA, Mock Execution.
→ 검증: 30종목 1분봉에서 final_decision 생성 → mock execution → KB 저장

### Sprint 2 — Cold Path (5개)
Event Admission, News Agent, Risk Agent, LLM Router, Persistent Cache.
→ 검증: 이벤트 없으면 quant-only, 있으면 LLM 개입

### Sprint 3 — PPO 실전화
stable-baselines3 PPO 학습, 거래비용 reward, 현재 포지션 state,
Top-K 밖 보유종목 weight=0 청산 경로.

### Sprint 4 — Mode B
Alpha Factor Engine, Co-STEER, Thompson Sampling, 3중 정규화, Alpha Decay Monitor,
**Backtest Agent (C12) + BacktestEngine/ReplayRunner/PerformanceAnalyzer (C13) + Mode B Scheduler/Deployer (C14) — v2.2 추가**.
→ 코어 거래 루프가 돈 뒤에 붙인다. Mode B Scheduler가 18:00~22:00을 오케스트레이션하고, Backtest Agent가 21:30 검증 게이트, Mode B Deployer가 22:00 배포 실행자.

## 미루면 안 되는 것

1. C11 EventAdmission/Backpressure — 코드 시작 전에 추가
2. OMS Phase 2 — live 전환 전 **별도 epic**으로 분리 관리
3. **Contract Tests 먼저 작성** — 이 시스템은 모델보다 계약이 중요하다.
   C4/C8/C9/C10/C11에 대한 schema test, replay test, idempotency test를 구현 코드보다 먼저 작성.


## C15. DynamicUniverseContract

> Sprint 5에서 활성화. trade universe(active 20)와는 별도의 **watch universe(KOSPI200)** 를 감시하여, 이벤트 발생 시 dynamic overlay를 생성한다. Hot Path core와 PPO는 변경하지 않는다.

```yaml
name: DynamicUniverseContract
owner: Risk Agent Fast Path + Event Layer
mode: "event-driven intraday overlay"
post_hoc_evaluation_only: true
input:
  trade_universe_ref: "universe_config.yaml (active 20)"
  watch_universe_ref: "watch_universe_kospi200.yaml"
  scan_sources: [naver_news, dart, community, krx_investor_flow, price_snapshot]
  trigger_catalog_ref: "risk_config.yaml dynamic_universe.trigger_catalog"
output:
  dynamic_candidate_pool:
    max_size: 10
    # P1-2 fix (2026-05-09): ttl → ttl_sec (단위 명시, dynamic_universe_config.yaml SSOT 정합).
    current: "[{ticker, admitted_at, trigger_ids, ttl_sec}]"
  dynamic_holdings:
    max_size: 5
    current: "[{ticker, weight, admitted_at, exit_policy}]"
position_rules:
  per_stock_max_weight: 0.03
  total_dynamic_max_weight: 0.10
  allocator: "fixed_rule_only (PPO 불가)"
exit_policy:
  - market_close
  - ttl_expiry
  - stop_loss
  - spike_resolved
identity:
  admission_event_id_prefix: "ADM"
  exit_event_id_prefix: "EXT"
  format: "{PREFIX}-{yyyymmdd}-{UUID8}"
forbidden_permissions:
  - trade_universe_ssot_mutation
  - ppo_allocation_for_dynamic_overlay
  - lightgbm_inference_for_watch_universe
  - mode_b_cold_path_intervention
  - fda_weight_modification
  - direct_trade_execution_bypass_pm
activation_gate:
  requires_operator_approval: true
  enabled_field: "risk_config.yaml dynamic_universe.enabled"
  activation_command: "python -m new.src.ops.enable_dynamic_universe --approve"
weight_decision_authority:
  proposes: "DynamicUniverseManager (fixed_rule_only)"
  final_executes: "PortfolioManager (order_deltas only)"
  fda_role: "veto only on admission_event (no weight change)"
  notes: "DynamicUniverseManager는 weight를 직접 적용하지 않는다. PM이 order_delta로 변환한다. FDA can_change_weight=false 원칙 유지."
notes:
  rationale: "후보 10 / 실보유 3~5 구조로 운영하며, post-hoc evaluation으로 overlay 성과를 분리 검증"
```

## C16. WatchUniverseSnapshotContract

```yaml
name: WatchUniverseSnapshotContract
owner: Data Layer / Price Snapshot Feed
mode: "intraday polling or lightweight websocket"
input:
  universe_ref: "watch_universe_kospi200.yaml"
  interval_sec: 60
output:
  watch_snapshot_id: "WS-{yyyymmdd}-{UUID8}"
  snapshots:
    - ticker: string
      ts: ISO8601
      last_price: float
      day_change_pct: float
      volume: float
      turnover: float
use_cases:
  - dynamic_universe ±5pct trigger
  - post_hoc evaluation reference
errors:
  - SNAPSHOT_MISSING
  - RATE_LIMIT_EXCEEDED
  - TICKER_NOT_IN_WATCH_UNIVERSE
  - STALE_SNAPSHOT  # interval_sec 2배 초과 시
forbidden_permissions:
  - direct_trade_execution
  - trade_universe_mutation
  - lightgbm_inference_for_watch_universe
polling_authority:
  subject: "WatchUniversePoller (Layer 2 Data, 신규)"
  trigger: "장중 (09:00~15:30 KST) cron 60s interval"
  mode_b_role: "post_hoc evaluation 데이터 read-only 사용"
```

## C17. ModelRegistryContract (S1-0 Batch B 신설, 2026-04-20)

> **목적**: LightGBM + PPO Allocator 등 학습 모델 버전 관리 SSOT.
> S1-0 baseline.pkl 생성부터 S3-6 야간 재학습(v2, v3, ...)까지 동일 registry 재사용.
> C12 BacktestAgentContract의 `model_ref` / `baseline_ref` 값은 이 registry에서 조회한다.

```yaml
name: ModelRegistryContract
owner: Model Layer / Model Registry
mode: "on-demand (save/load/list/rollback)"
called_by:
  - LGBMTrainer (S1-0 Batch C)                       # save() — 최초 baseline.pkl 생성
  - ModeBScheduler (C14, stage_4_model_evolution)    # S3-6 야간 재학습 v{n}.pkl save() 트리거
  - CoSTEEROrchestrator (S3-5)                       # save() after 재학습 (C14 stage_4 경유)
  - QuantAgent (S1-1)                                # load_latest()
  - BacktestAgent (C12)                              # load_version() for baseline 비교
  - ModeBDeployer (C14)                              # rollback() on deploy failure

storage:
  artifacts_dir: "artifacts/lgbm"        # risk_config.yaml model_registry.artifacts_dir
  layout:
    baseline_pkl: "artifacts/lgbm/baseline.pkl"       # S1-0 최초 baseline
    version_pkl: "artifacts/lgbm/v{n}.pkl"            # S3-6 야간 버전
    registry_json: "artifacts/lgbm/registry.json"     # 인덱스 + 활성 버전
    latest_symlink: "artifacts/lgbm/latest_model.pkl" # 현재 active 모델

registry_schema:
  # registry.json 구조
  schema_version: "1.0.0"
  active_version: "string|null"             # "baseline" | "v{n}" | null (candidate only registry)
  versions:
    - version: "string"                     # "baseline" | "v2" | "v3" ...
      bundle_id: "string|null"              # C12 bundle_id (v2+ 있음)
      model_path: "string"                  # pkl 파일 절대 경로
      metadata_path: "string"               # JSON 파일 절대 경로
      created_at: "ISO8601"
      train_start: "YYYY-MM-DD"
      train_end: "YYYY-MM-DD"
      feature_cols: "[string]"
      label_horizon_bars: "int"             # 5 (S1-0 Batch B 기준)
      label_generation_version: "string"    # label 생성 알고리즘 버전 (risk_config.yaml label.generation_version)
      label_session_scope: "string"         # ticker_trading_day 등 label session 범위
      metrics:                              # C12/C13 metrics 7종 준수
        ic: "float"
        icir: "float"
        rank_ic: "float"
        arr: "float"
        ir: "float"
        mdd: "float"
        sr: "float"
      commit_hash: "string"                 # git rev-parse HEAD
      data_version: "string"                # parquet 파일 범위 hash
      status: "candidate|active|archived|rollback"

api:
  save:
    input: "(model: lgb.Booster, metadata: dict, is_latest: bool)"
    output: "Path (저장된 pkl 경로)"
    atomicity: "pkl + metadata JSON 완전 저장 후 registry.json 갱신. is_latest=true일 때만 latest symlink + active_version 갱신"
    rules:
      is_latest_false_status: "candidate"
      is_latest_false_active_version_mutation: "forbidden"

  load_latest:
    input: "()"
    output: "(model: lgb.Booster, metadata: dict)"

  load_version:
    input: "(version: str)"
    output: "(model: lgb.Booster, metadata: dict)"

  list_versions:
    input: "()"
    output: "[metadata]"                    # newest first

  rollback:
    input: "(version: str)"
    output: "None"
    side_effect: "latest_model.pkl symlink 교체 + registry.active_version 갱신"

config_ref: "risk_config.yaml: model_registry"

errors:
  - VERSION_NOT_FOUND
  - PKL_LOAD_FAILED
  - REGISTRY_CORRUPTED
  - SYMLINK_FAILED

forbidden:
  - direct_pkl_overwrite                    # save() 경유 필수
  - mode_b_auto_rollback_without_approval   # C14 deploy gate 경유

sla:
  save_latency_ms: 500
  load_latency_ms: 100
  list_latency_ms: 50

mode_b_editable: true                       # Mode B(야간)가 save() 호출 OK
human_approval_required: false              # rollback만 C14 deploy gate가 승인
```

## C18. AgentPerformanceContract (L2 Agent 기여도 지표, 2026-04-21 신설)

> **목적**: 장중 `audit_log.jsonl` 레코드 → 18:00 장마감 집계 → L2 9지표 산출의 단일 계약.
> architecture.md §4.2 (Agent Performance Metrics 9지표 표) + §5.6 (8차원 벡터 변환) 의 SSOT.
> L1 (C12/C13 metrics 7종) 과 L2 (본 C18, 9지표) 는 상보적: L1 = 모델 성능, L2 = 에이전트 기여도.

> **중요 (PIT-Safety)**: L2 9지표 중 post-hoc label 의존 지표 (`prediction_accuracy`, `anomaly_detection_precision_recall`, `veto_precision_recall`) 는
> **장중 실시간 집계 금지**. audit_log.jsonl 에 `label_t5_ret` / `price_t5_snapshot` 필드를 `null` 로 기록한 후,
> **장마감 18:00 KST 이후 배치 job 에서만 backfill + 집계**. Hot Path 에서 직접 계산 시 PIT-Safety 위반.

```yaml
name: AgentPerformanceContract
owner: Ops Layer / AuditLogger + Mode B Aggregator
mode: "append-only 장중 기록 (audit_log.jsonl) + 18:00 배치 집계"

called_by:
  - AuditLogger (Sprint 2 S2-0)                   # 장중 audit_log.jsonl append
  - ModeBScheduler (C14, stage_1_data_rollup)     # 18:00 batch aggregation trigger
  - ModeBPerformanceAggregator (Sprint 2 신설)    # 실제 집계 주체
  - BacktestAgent (C12)                           # 8차원 벡터 변환 시 Layer 2 reference
  - EvalAgent (Sprint 3)                          # cause_attribution, reason_code_distribution 연동

audit_log_schema:
  # audit_log.jsonl 단일 레코드 구조 (장중 append)
  file: "artifacts/audit_log.jsonl"
  append_only: true

  fields:
    ts: "ISO8601"                                 # KST
    decision_id: "DEC-{yyyymmdd}-{UUID8}"   # C9 참조. 2026-04-21 UUID8 정정 (기존 {hhmm}-{seq} 대신)
    agent: "string"                               # quant|news|risk_fast|risk_slow|debate|fda|pm|execution_gw
    event_type: "string"                          # signal|anomaly_trigger|veto|approve|order|fill
    ticker: "6-digit KRX code"
    reason_code: "string|null"                    # C9 SSOT (NEWS_DIVERGENCE, NORMAL_APPROVE, ...)
    signal_score: "float|null"                    # Quant only
    anomaly_flag: "bool|null"                     # Quant only
    target_weight: "float|null"                   # PM only
    actual_weight: "float|null"                   # PM only
    fill_price: "float|null"                      # EG only
    snapshot_vwap: "float|null"                   # EG only (slippage 계산용)
    slippage_bps: "float|null"                    # EG only (realized)
    sector: "string|null"                         # KRX 업종 분류
    llm_called: "bool|null"                       # News/Risk/Debate only
    llm_model: "string|null"                      # kanana-o|gpt-4o
    # PIT-Safety critical: 아래 4 필드는 장중 null, 18:00 이후 batch backfill
    label_t5_ret: "float|null"                    # t+5min forward return (post-hoc, batch only)
    price_t5_snapshot: "float|null"               # t+5min price (post-hoc, batch only)
    # P1 fix (2026-05-09): backfill 메타. C-2 cause_attribution _is_label_pit_safe() 가드 SSOT.
    label_backfilled_at: "ISO8601|null"           # backfill 실행 KST 시각. snapshot_hour(18 KST) 이후만 valid. null = 장중 미backfill
    label_backfill_source: "string|null"          # backfill 원천 (mode_b_stage_1_rollup|synth_audit_log|manual)

aggregation:
  trigger: "Mode B stage_1_data_rollup, 18:00 KST"
  window: "daily (09:00~15:30 KST 장중 레코드 전체)"
  pit_safety_check: "label_t5_ret is null → 집계 제외"

  output:
    agent_performance_id: "APM-{yyyymmdd}-{UUID8}"   # v3.4 (2026-04-21) 정정: UUID8 기반 (identity 블록 L40 SSOT 일치)
    rollup_date: "YYYY-MM-DD"
    metrics:
      # L2 9지표 (architecture.md §4.2 SSOT)
      prediction_accuracy:
        scope: "Quant"
        formula: "count(signal_score > 0 AND label_t5_ret > 0) / count(signal emitted with non-null label)"
        threshold: "risk_config.yaml agent_performance.prediction_accuracy_min"
        pit_safety: "post-hoc, 18:00 이후만 집계"

      realized_pnl_contribution:
        scope: "All agents (quant/news/risk_fast/risk_slow/debate/fda/pm/execution_gw)"
        formula: "leave-one-out ablation: PnL_full - PnL_without_agent_X"
        unit: "pct (e.g., 0.0018 = +0.18%)"
        threshold: "positive per contributing agent"
        pit_safety: "T+1 fill 기준, 장중 미체결 제외"

      slippage_execution_shortfall_bps:
        scope: "ExecutionGateway"
        formula: "mean( (fill_price - snapshot_vwap) / snapshot_vwap * 10000 )"
        unit: "bps"
        threshold: "risk_config.yaml agent_performance.slippage_bps_max"
        pit_safety: "실시간 가능 (미래 참조 없음)"

      anomaly_detection_precision:
        scope: "Quant"
        formula: "count(anomaly_flag=true AND label_t5_ret < anomaly_threshold) / count(anomaly_flag=true)"
        threshold: "risk_config.yaml agent_performance.anomaly_precision_min"
        pit_safety: "post-hoc, 18:00 이후만"

      anomaly_detection_recall:
        scope: "Quant"
        formula: "count(anomaly_flag=true AND label_t5_ret < anomaly_threshold) / count(label_t5_ret < anomaly_threshold)"
        threshold: "risk_config.yaml agent_performance.anomaly_recall_min"
        pit_safety: "post-hoc, 18:00 이후만"

      sector_exposure_tracking_error:
        scope: "PortfolioManager"
        formula: "std( w_sector_actual - w_sector_target ) across all sectors, daily"
        threshold: "risk_config.yaml agent_performance.sector_tracking_error_max"
        pit_safety: "실시간 가능 (미래 참조 없음)"

      veto_precision:
        scope: "FDA"
        formula: "count(veto AND label_t5_ret < veto_loss_threshold) / count(veto)"
        threshold: "risk_config.yaml agent_performance.veto_precision_min"
        pit_safety: "post-hoc, 18:00 이후만"

      veto_recall:
        scope: "FDA"
        formula: "count(veto AND label_t5_ret < veto_loss_threshold) / count(label_t5_ret < veto_loss_threshold)"
        threshold: "risk_config.yaml agent_performance.veto_recall_min"
        pit_safety: "post-hoc, 18:00 이후만"

      false_positive_event_trigger_rate:
        scope: "News, Risk (Fast/Slow)"
        formula: "count(llm_called=true AND reason_code='NORMAL_APPROVE') / count(llm_called=true)"
        threshold: "risk_config.yaml agent_performance.false_positive_rate_max"
        pit_safety: "FDA 판단 완료 시점 기준 (post-hoc 외)"

    performance_vector_8d:
      description: "architecture.md §8.1 8차원 성과 벡터"
      components: "[IC, ICIR, Rank(IC), Rank(ICIR), ARR, IR, -MDD, SR]"
      source: "MetricsBundle.to_performance_vector() (L1)"
      normalization: "min-max rolling 30일 window, clip [0.01, 0.99]"

    storage:
      daily_rollup: "artifacts/metrics/agent_performance_{YYYY-MM-DD}.json"
      append_to_kb: "knowledge_base/agent_performance_history.jsonl"

config_ref: "risk_config.yaml: agent_performance, evaluation"

errors:
  - MISSING_LABEL_T5_RET            # batch backfill 실패
  - INSUFFICIENT_SAMPLES             # 일별 집계 최소 샘플 수 미달
  - AGENT_ID_UNKNOWN                 # 허용된 agent enum 밖

forbidden:
  - intraday_real_time_post_hoc_calc # post-hoc 지표를 장중 실시간 계산 금지 (PIT-Safety)
  - label_backfill_before_1800_kst   # 18:00 KST 이전 backfill 금지
  - fda_reason_code_mutation_for_metric_tuning  # 지표 조정을 위해 reason_code enum 변경 금지 (FDA can_change_weight=false 정신)

sla:
  # risk_config.yaml agent_performance SSOT (audit_log_append_latency_ms / daily_rollup_duration_min)
  audit_log_append_latency_ms: 5         # hot path 내 audit 기록 허용 latency
  daily_rollup_duration_min: 10          # 18:00 이후 집계 완료 소요 시간
  kb_append_atomicity: true

mode_b_editable: false                   # Mode B 자동 수정 금지 (operator 수동만)
```

> **config_ref 참조**: `risk_config.yaml model_registry` 섹션에 `artifacts_dir / registry_file / latest_symlink / versioning_scheme / max_versions_keep / metadata_required_fields` 정의.
