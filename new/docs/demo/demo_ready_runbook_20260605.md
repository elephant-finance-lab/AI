# Demo Ready Runbook 2026-06-05

This document defines the demo standard for the paper-safe service video. It
intentionally separates demo readiness from paper-service operation and strict
operational proof.

## Safety That Cannot Be Relaxed

- No live or real trading.
- KIS virtual or paper only.
- `live_trading_allowed=false`.
- production `active_version=null`.
- `registry_mutated=false`.
- Cached recommendations are advisory data, not order permission.
- Do not weaken `paper_auto_trade.py` or `PaperAutoTrader` paper guards.
- Do not print, commit, or screenshot `.env`, tokens, account numbers, or secrets.
- Do not describe 5-ticker evidence as 30-ticker evidence.

## Three Readiness Levels

| Level | Meaning | Demo Requirement |
| --- | --- | --- |
| Demo Ready | User-facing service flow can be recorded safely | login, recommendations or clear stale/no-cache state, natural-language reasons, detail page, readiness reason, paper start success or fail-closed reason |
| Paper Service Candidate | Server-side paper service can run safely | `/api/recommendations` works, stale start blocked, FE-BE-AI bundle alignment, AI start gate, KIS virtual, audit/report saved |
| Operational Proof | Research/operation-grade evidence | 30T 10-30 cycles, cadence and reconciliation proof, Kafka notification path, scheduler proof, evidence selector trace |

Strict 30T cadence, Kafka, BE full-day scheduler, and evidence selector
hardening are not blockers for the demo video. They remain follow-up work.

## Demo Ready Acceptance

- `/recommend` renders ten recommendations, or shows a clear stale/no-cache/degraded reason.
- Recommendation list shows fresh/stale state when available.
- Recommendation detail shows stock identity, natural-language reason, score or rank, and chart or chart fallback.
- Confirm/start flow shows readiness status and concrete disabled reason.
- Stale recommendation disables start.
- Readiness BLOCKED disables start with reason.
- Readiness PASS plus fresh recommendation may attempt KIS virtual paper start.
- Start failure is acceptable if fail-closed and reasoned.
- Live flags and registry mutation stay false throughout.

## Existing Evidence To Reference

- Real dual-source: `artifacts/dual_source/20260605.json`
- DEED submit-enabled 30T paper run: `artifacts/reports/paper_auto_trading/FRI_DEED_REAL_DS_SUBMIT_30T_30CYCLE_20260605_1222/paper_auto_trade_20260605_124901.json`
- Service readiness PASS: `artifacts/reports/service_readiness/service_readiness_BUNDLE-20260602-DEED529F_20260605_125114.json`
- Strict prelive PASS: `artifacts/reports/prelive_gate/prelive_gate_20260605_125522.json`
- Local screenshots:
  - `${PROJECT_ROOT}/artifacts/screenshots/local_fe_recommend_20260605_1230.png`
  - `${PROJECT_ROOT}/artifacts/screenshots/local_fe_recommend_detail_20260605_1231.png`
  - `${PROJECT_ROOT}/artifacts/screenshots/local_fe_chart_after_cors_20260605_1231.png`

Known caveat: local Kafka was not running in the last full-stack check. Mark
notification path as `NOT_VERIFIED`, not as a trading failure.

## Recommendation Cache Seed Rule

Use cache only to stabilize the demo screen.

1. Prefer normal FE to BE to AI recommendation refresh.
2. If market data is blocked, use the latest DEED cached recommendation as advisory display.
3. The screen must show stale/fresh state when stale metadata exists.
4. Stale or seeded cache must not enable paper start.
5. Record bundle id, generated timestamp, recommendation count, and stale status.

## Stop Conditions

Stop and preserve evidence if:

- any live/real flag becomes true,
- production active version is set,
- registry mutation appears,
- FE enables start from stale recommendation,
- BE starts paper auto while readiness is blocked,
- AI/BE returns a generic error where a known reason should be displayed,
- secrets appear in logs or screenshots.
