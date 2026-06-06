# Cost-Aware Service-Policy Replay Runbook

Purpose: evaluate a candidate bundle under KIS paper cash-account constraints before
any deploy-quality claim.

This replay is read-only. It does not read `.env`, does not call KIS, and does not
mutate `artifacts/lgbm/registry.json`.

## Policy

- Cash equity account only
- Long-only entries
- Sell orders reduce existing holdings only
- No naked short exposure
- `paper_auto_trading.max_orders_per_cycle`
- `paper_auto_trading.max_order_qty_per_order`
- `position_limits.max_names`
- `position_limits.max_single_name`
- `position_limits.min_cash`
- `turnover_cap.daily_max`
- `execution_cost_model.components.commission_bps`
- `execution_cost_model.components.slippage_bps`

All values are loaded from `new/config/risk_config.yaml`.

## Command

```bash
cd "$(git rev-parse --show-toplevel)"
ELEPHANT_MODE=mode_b \
PYTHONPATH=$PWD/new \
/opt/anaconda3/envs/elephant/bin/python \
  new/scripts/service_policy_replay.py \
  --bundle-id BUNDLE-20260512-0AEEE37A
```

Optional explicit replay window:

```bash
cd "$(git rev-parse --show-toplevel)"
ELEPHANT_MODE=mode_b \
PYTHONPATH=$PWD/new \
/opt/anaconda3/envs/elephant/bin/python \
  new/scripts/service_policy_replay.py \
  --bundle-id BUNDLE-20260512-0AEEE37A \
  --start-date 20260414 \
  --end-date 20260503
```

## PASS Criteria

The report `gate.status` must be `PASS`.

Deployment quality must remain blocked when any of the following appears:

- `daily_turnover_cap_violation`
- `non_positive_or_immaterial_total_return`
- `service_policy_sharpe_below_threshold`
- `naked_short_exposure`
- `order_cap_violation`
- `cash_guard_violation`

## Interpretation

`PASS` means the candidate is economically plausible under the paper-auto service
policy. It is still not a live-trading approval.

`BLOCKED` means the model can be used only for internal rehearsal or diagnostic
work. Production registry activation remains forbidden until C12/C14 deploy gates
also pass.
