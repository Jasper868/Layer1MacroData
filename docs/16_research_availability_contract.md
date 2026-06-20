# 16. Research Availability Contract

## Purpose

This contract converts the existing source metadata into two machine-readable
Data-to-Research handoff files. It does not create a feature, score, alert,
backtest result, portfolio weight, or trade instruction.

```text
data/meta/research_availability_contract.csv
data/meta/research_module_readiness.csv
```

## First principle

An observation date is not automatically a research-usable date. For every
indicator, Research must apply the conservative rule below before using that
observation in either a historical study or a live review:

```text
research_date >= observation_date + availability_lag_calendar_days
```

The current contract uses the lag declared in `data/meta/source_metadata.csv`.
It is a conservative point-in-time policy, not a full historical vintage
(as-of) database.

## Files

### Indicator contract

`research_availability_contract.csv` has one row for each and only each column
in `combined_macro_market.csv`, excluding `date`. It records:

- source and source code;
- availability lag and the historical research rule;
- latest valid observation date and its earliest allowed use date;
- current quality status;
- current research readiness;
- the existing admission note and prospective-alignment rule.

### Module readiness

`research_module_readiness.csv` partitions all current raw indicators into
seven mutually exclusive data-ownership modules:

1. `equity_volatility_core`
2. `credit_context`
3. `rates_inflation`
4. `fx_commodities`
5. `market_asset_proxies`
6. `volatility_curve_tail`
7. `options_positioning`

A research feature may depend on more than one module. For example, a
volatility-term-structure feature needs the VIX level from
`equity_volatility_core` and the short/mid-curve inputs from
`volatility_curve_tail`.

## Readiness meanings

| Status | Meaning | Research handling |
|---|---|---|
| `READY` | Quality is OK and the latest observation has passed its declared lag. | May be used, subject to the historical availability rule. |
| `PENDING_LATEST_OBSERVATION` | Latest observation exists but has not yet passed its lag. | Do not use the newest row yet. |
| `DEGRADED` | Quality is WARN. | Do not treat as fresh or action-capable input. |
| `BLOCKED` | Quality is FAIL, missing, or internally inconsistent. | Do not use the module. |

## Boundary

The Data repository records the policy and current readiness. The separate
Research repository must apply the historical availability rule when it builds
features or runs a backtest. This contract alone does not shift data columns or
make an investment decision.
