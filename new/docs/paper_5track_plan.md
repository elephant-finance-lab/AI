# Paper 5-Track 운영 계획

> 범위: 개인 repo `Jang1J/Flephant` 실험 브랜치 전용.
> 팀 `ai-1`에는 검증된 default track(`MAIN_BASELINE`)만 별도 반영한다.

## 1. 목표

2026-06-12 최종 발표 전까지 가능한 모든 장중 KIS virtual/paper 운영일에 동일 모델을 고정하고 실행정책만 다르게 운용한다. 목적은 모델 재학습이 아니라, 실제 serving/주문/체결/PnL 로그를 바탕으로 어떤 실행정책이 가장 안정적인지 비교하는 것이다.

고정 모델:

- `BUNDLE-20260521-POSTCLOSE`

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
| `MAIN_BASELINE` | 예 | 팀 default 후보 | 현재 승인된 기준 정책 |
| `SAFE_SLOW` | 예 | 안정성 비교 | 주문 수/종목 수 축소, cash 증가, confidence 강화 |
| `ACTIVE_SMALL` | 예 | 기회 탐색 | confidence 완화, 종목 수 증가, 수량 1주 유지 |
| `TOPK_EQUAL` | 예 | PPO 대비 비교 | Top-K를 균등 비중으로 실제 paper 주문 비교 |
| `STRICT_GATE` | 아니오(Shadow) | 강한 필터 비교 | confidence/trade-probability gate 강화, 첫날은 주문 후보만 기록 |

## 4. 계좌 준비

기준 구성은 KIS virtual/paper 계좌 5개다. 각 정책은 서로 다른 계좌에서 실제 paper 주문으로 운용한다. 계좌가 부족할 때만 일부 정책을 shadow로 내려 주문 후보만 기록한다.

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

## 8. 최적화 기준

백테스트는 후보 생성 도구이고, paper 운영은 최종 검증 도구다.

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
