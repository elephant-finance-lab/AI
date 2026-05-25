# Paper 5-Track 운영 계획

> 범위: 개인 repo `Jang1J/Flephant` 실험 브랜치 전용.
> 팀 `ai-1`에는 검증된 default track(`MAIN_BASELINE`)만 별도 반영한다.

## 1. 목표

2026-06-12 최종 발표 전까지 가능한 모든 장중 KIS virtual/paper 운영일에 동일 모델을 고정하고 실행정책만 다르게 운용한다. 목적은 모델 재학습이 아니라, 실제 serving/주문/체결/PnL 로그를 바탕으로 어떤 실행정책이 가장 안정적인지 비교하는 것이다.

고정 모델:

- `BUNDLE-20260521-POSTCLOSE`

모델 지위:

- 전역 최적 모델: 아님
- paper default baseline: 맞음
- research benchmark anchor: 맞음
- 2026-05-26 paper 시작용 안정 후보: 맞음
- production deploy 후보: 아님

정확한 표현은 "`BUNDLE-20260521-POSTCLOSE`는 현재 paper 운영을 시작하기에 evidence chain이 가장 완성된 stable paper baseline이자 research benchmark anchor"이다. 새 `RESEARCH-*` 후보가 더 좋아 보여도 C12 real backtest, deploy dry-run, service readiness, prelive gate, paper broker evidence를 다시 통과하기 전까지 paper default로 올리지 않는다.

핵심 비교 축:

- 주문 빈도
- 매수/매도 수
- 체결/거절 수
- score non-empty cycle
- no-order cycle
- 현금 비중
- turnover
- 일별/누적 paper PnL
- MDD
- fail-closed 발생 원인

## 2. 브랜치/공유 원칙

- 개인 실험 브랜치: `jaewon/paper-5track-lab`
- 개인 원격: `origin = https://github.com/Jang1J/Flephant.git`
- 팀 브랜치: `ai-1`
- 팀 반영 대상: `MAIN_BASELINE`에 필요한 최소 변경만
- 팀 미반영 대상: 5계좌 실험용 profile, shadow 비교군, 개인 발표 리포트 초안

## 3. 5개 정책 트랙

| Track | 주문 제출 | 역할 | 핵심 차이 |
|---|---:|---|---|
| `MAIN_BASELINE` | 예 | 팀 default 후보 | 현재 paper default baseline |
| `SAFE_SLOW` | 예 | 안정성 비교 | 주문 수/종목 수 축소, cash 증가, confidence 강화 |
| `ACTIVE_SMALL` | 예 | 기회 탐색 | confidence 완화, 종목 수 증가, 수량 1주 유지 |
| `TOPK_EQUAL` | 예 | PPO 대비 비교 | Top-K를 균등 비중으로 실제 paper 주문 비교 |
| `STRICT_GATE` | 아니오(Shadow) | 강한 필터 비교 | confidence/trade-probability gate 강화, 첫날은 주문 후보만 기록 |

첫날 기본 launch offset:

| Track | Offset |
|---|---:|
| `MAIN_BASELINE` | 0초 |
| `SAFE_SLOW` | 10초 |
| `ACTIVE_SMALL` | 20초 |
| `TOPK_EQUAL` | 30초 |
| `STRICT_GATE` | 40초, shadow |

offset은 전략 차이를 만들기 위한 값이 아니라 KIS balance/order/order-history 호출이 같은 초에 몰리는 것을 줄이기 위한 운영 안전 장치다.

## 4. 계좌 준비

기준 구성은 KIS virtual/paper profile 5개다. 첫날 실제 주문 track은 `MAIN_BASELINE`, `SAFE_SLOW`, `ACTIVE_SMALL`, `TOPK_EQUAL` 4개이고, `STRICT_GATE`는 별도 profile로 balance/bars/guard만 읽는 shadow track이다. 여기서 shadow는 broker 주문 제출 금지라는 뜻이며, virtual read API는 후보와 guard를 계산하기 위해 profile/preflight 통과 후에만 허용한다. 같은 계좌를 여러 paper track이 공유하지 않으며, 계좌가 부족하거나 readiness가 불안하면 paper track도 shadow로 내려 주문 후보만 기록한다.

필수 env profile:

```bash
export KIS_MAIN_MODE=virtual
export KIS_MAIN_APP_KEY=...
export KIS_MAIN_APP_SECRET=...
export KIS_MAIN_ACCOUNT_NUMBER=...
export KIS_MAIN_ACCOUNT_PRODUCT_CODE=01

export KIS_SAFE_MODE=virtual
export KIS_SAFE_APP_KEY=...
export KIS_SAFE_APP_SECRET=...
export KIS_SAFE_ACCOUNT_NUMBER=...
export KIS_SAFE_ACCOUNT_PRODUCT_CODE=01

export KIS_ACTIVE_MODE=virtual
export KIS_ACTIVE_APP_KEY=...
export KIS_ACTIVE_APP_SECRET=...
export KIS_ACTIVE_ACCOUNT_NUMBER=...
export KIS_ACTIVE_ACCOUNT_PRODUCT_CODE=01

export KIS_TOPK_MODE=virtual
export KIS_TOPK_APP_KEY=...
export KIS_TOPK_APP_SECRET=...
export KIS_TOPK_ACCOUNT_NUMBER=...
export KIS_TOPK_ACCOUNT_PRODUCT_CODE=01

export KIS_STRICT_MODE=virtual
export KIS_STRICT_APP_KEY=...
export KIS_STRICT_APP_SECRET=...
export KIS_STRICT_ACCOUNT_NUMBER=...
export KIS_STRICT_ACCOUNT_PRODUCT_CODE=01
```

각 계좌는 가능한 한 clean 상태로 시작한다. clean 계좌가 어렵다면 2026-05-26 09:00 직전 starting equity, 보유 포지션, 현금 잔고를 snapshot으로 고정해 성능 계산 기준점으로 사용한다.

비교 공정성 기준:

- 같은 계좌를 여러 paper track이 공유하지 않는다.
- 시작 NAV 또는 현금 잔고 snapshot을 track별로 기록한다.
- 기존 보유가 있으면 holdings snapshot을 남기고, 수익률은 normalized NAV 기준으로 비교한다.
- pending order가 있으면 해당 track은 시작하지 않거나 예외 사유를 failure card에 기록한다.
- track config는 `policy_hash`로 고정한다. 장중 config 수정은 금지하고, 수정 시 새 run 또는 새 policy id로 본다.

## 5. 장전 실행 순서

1. 08:30 전후 current-day Dual-Source artifact 생성/검증
   - 예: `artifacts/dual_source/YYYYMMDD.json`
   - 모델 필수 feature `news_score_t` 존재 확인
2. env readiness 확인
   - `.env` 파일 직접 읽기 금지
   - 사용자 터미널에서 profile env export
3. dry-run

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/paper_policy_lab/run_multi_policy_lab.py --dry-run
```

4. KIS virtual evidence 확보
   - balance reconciliation
   - probe order
   - order-history match
   - prelive/service readiness
5. 09:00 이후 policy lab 실행

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/paper_policy_lab/run_multi_policy_lab.py \
  --run-id 20260526_open \
  --cycles 60 \
  --interval-sec 60
```

## 6. 장중 운영 원칙

- 장중에는 정책을 임의 변경하지 않는다.
- `quant mode=blocked`, score 0 지속, required feature missing, KIS account mismatch, real/live mode 감지 시 fail-closed.
- paper 주문 정책은 계좌별로 분리한다.
- shadow 정책은 broker 주문 제출 금지.
- production registry mutation 금지.
- live trading 금지.

## 7. 장마감 분석

각 track별로 다음 표를 만든다.

| 항목 | 의미 |
|---|---|
| `score_nonempty_cycles` | 모델 serving이 실제로 신호를 낸 cycle 수 |
| `order_delta_count` | 주문 후보 수 |
| `submitted_count` | broker 제출 주문 수 |
| `fill_count` | 체결 수 |
| `reject_count` | 거절 수 |
| `daily_return` | 당일 paper 수익률 |
| `mdd` | 장중 최대 낙폭 |
| `turnover` | 회전율 |
| `fail_closed_count` | 안전 차단 횟수 |

부가 evidence:

- `policy_hash`
- `launch_delay_sec`
- `serving_feature_readiness`
- `kis_retry_count`
- `timeout_count`
- `matched_order_history_count`
- `failure_case_cards`

일별 비교는 원화 손익보다 `normalized_nav_bps`, MDD, turnover, fill/reject, fail-closed count를 함께 본다.

## 8. 최적화 기준

백테스트는 후보 생성 도구이고, paper 운영은 최종 검증 도구다.

5-track 결과는 "수익률 1등 = 최적 정책"으로 해석하지 않는다. 모델은 고정되어 있고, 비교 대상은 실행정책의 runtime 안정성, 체결 품질, fail-closed 동작, turnover, drawdown이다.

정책 변경은 장마감 후에만 한다. 변경 근거는 다음 세 가지가 모두 있어야 한다.

1. service-policy/backtest에서 정책 후보가 의미 있음
2. paper 운영에서 주문/체결/score 로그가 정상
3. risk guard/fail-closed 로그가 설명 가능

## 9. 발표 스토리

최종 발표에서는 "한 모델을 여러 실행정책으로 paper 운용했고, 실제 운영 로그로 안정성과 수익성을 비교했다"는 구조를 사용한다.

주요 메시지:

- 단일 백테스트 수치가 아니라 실제 KIS virtual 운영 로그 기반 검증
- serving feature readiness와 zero-score fail-closed 개선
- 5-track 정책 비교로 default 정책을 선택
- 팀 `ai-1`에는 가장 보수적이고 재현 가능한 default만 반영
