# Local Policy Lab

이 폴더는 2026-05-26 이후 KIS virtual/paper 장중 운영에서 같은 모델을 고정하고 실행 정책만 비교하기 위한 개인 실험용 하네스입니다.

- 개인 repo `Jang1J/Flephant` 실험 브랜치에서만 관리합니다.
- 팀 `ai-1`에는 검증된 default track만 별도로 올립니다.
- `.env`를 읽지 않습니다.
- 실계좌 주문과 production registry mutation을 하지 않습니다.
- `registry_dir`는 명시해야 하며, `--registry-dir artifacts/lgbm` 또는 그 하위 경로는 즉시 거부합니다. 개인 lab은 paper candidate/research bundle만 참조해야 합니다.
- 자식 프로세스에 `ELEPHANT_MODE=mode_b`를 강제 주입하지 않습니다. 장중 paper/shadow 정책 비교 하네스이므로 Mode B 재학습/배포 의미와 섞지 않습니다.
- dry-run/index/policy별 JSON에는 `production_registry_mutated=false`, `paper_registry_mutated=false`, `live_trading_allowed=false`, `deploy_quality=false` safety field를 명시합니다.
- `decision_stride_bars`, `min_holding_bars`, `rebalance_cooldown_bars`, `top_k_fraction`, `no_trade_score_spread`, `min_cash`는 `service_policy_replay` 전용 최적화 키입니다. 이 paper runtime이 같은 의미로 강제하지 않으므로 config override에 넣으면 즉시 오류로 막습니다.
- 기본 설정은 `MAIN_BASELINE`, `SAFE_SLOW`, `ACTIVE_SMALL`, `TOPK_EQUAL` 4개 정책을 `mode: paper`로 두고, `STRICT_GATE`는 첫날 `mode: shadow`로 둡니다.
- `STRICT_GATE`는 2026-05-24 5종목 replay에서 trade-probability threshold `0.35`가 후보 22%를 거르고 주문은 남겼지만 pass 1/8에 그쳤습니다. `0.40` 이상은 후보 100% reject, 주문 0건이므로 첫날 paper 주문 트랙에서는 제외합니다.
- 계좌가 부족하거나 장중 readiness가 불안하면 다른 정책도 `mode: shadow`로 바꿔 주문 후보만 기록할 수 있습니다.
- 실행 결과는 `runs/` 아래에 저장되며 이 폴더는 gitignore됩니다.

## Runtime Scope

주말 global search의 best policy는 30종목 service-policy replay 기준입니다. 이 하네스는 장중 paper/shadow 런타임에서 실제로 적용 가능한 주문 cap과 PPO 파라미터만 비교합니다.

실제로 적용되는 override:

- `max_orders_per_cycle`
- `max_order_qty_per_order`
- `allow_market_order`
- `ppo_max_names`
- `ppo_min_cash`
- `ppo_min_confidence`
- `trade_probability_gate_enabled`
- `min_trade_probability`
- `ppo_weighting` (`score` 또는 `equal`만 허용)

따라서 2026-05-24 산출물은 “실제 paper 운용 결과”가 아니라 5트랙 장중 운용을 준비하기 위한 replay/근사 검증입니다. 2026-05-26 이후 생성되는 `runs/<run_id>/` 결과만 실제 paper/shadow 운용 비교로 해석합니다. replay-only knob를 운영으로 옮기려면 별도 runtime 구현과 C12/service replay 정렬 검증이 필요합니다.

`min_cash`는 특히 주의합니다. paper lab의 `ppo_min_cash`는 PPO 목표 비중의 현금 여유를 조정하는 값이고, service replay의 `min_cash`처럼 주문 후 cash-ratio를 독립적으로 막는 guard와 완전히 같지 않습니다.

## Env Profile

예시로 `MAIN_BASELINE`은 `profile_prefix: KIS_MAIN`을 사용합니다. 실행 전 사용자 터미널에서 아래처럼 profile별 값을 export해야 합니다.

```bash
export KIS_MAIN_MODE=virtual
export KIS_MAIN_APP_KEY=...
export KIS_MAIN_APP_SECRET=...
export KIS_MAIN_ACCOUNT_NUMBER=...
export KIS_MAIN_ACCOUNT_PRODUCT_CODE=01
```

5개 프로필을 동시에 돌리려면 각각 `KIS_MAIN_*`, `KIS_SAFE_*`, `KIS_ACTIVE_*`, `KIS_TOPK_*`, `KIS_STRICT_*` paper 계정/key 세트를 준비하세요. 기본 첫날 설정에서는 `STRICT_GATE`가 `shadow`라 주문을 제출하지 않고 후보/guard 기록만 남깁니다.

## Dry Run

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/paper_policy_lab/run_multi_policy_lab.py --dry-run
```

## 5/26 장중 예시

```bash
/opt/anaconda3/envs/elephant/bin/python new/experiments/paper_policy_lab/run_multi_policy_lab.py \
  --run-id 20260526_open \
  --cycles 60 \
  --interval-sec 60
```

결과는 `new/experiments/paper_policy_lab/runs/<run_id>/` 아래에 policy별 JSON과 로그로 남습니다.
