# 14. V1 Daily Credit Proxy and Source Metadata

## Purpose

This package changes the **data-admission layer only**. It does not modify Stage16 V0
components, thresholds, event windows, evidence statuses, scores, or portfolio permissions.

It adds one independent daily, long-history credit proxy:

- `BAA10Y` → `美国Baa公司债利差_10Y`

The series is a broad Baa corporate-credit spread relative to the 10-year Treasury. It is
not high-yield OAS, and it must never be spliced to `高收益债利差` (`BAMLH0A0HYM2`).

## Why it is separate from high-yield OAS

The existing high-yield OAS remains a short-history descriptive series in the project. A
different broad-credit proxy may improve long-history event-family research, but it measures
a different credit-quality segment and cannot be used to manufacture a synthetic long history
for high-yield OAS.

## Per-series source metadata

Each data update now writes:

```text
data/meta/source_metadata.csv
```

It documents for every current raw input:

- source and source code;
- observation and update frequency;
- conservative availability lag;
- revision handling;
- maximum expected tail delay;
- prospective-alignment restriction.

The metadata is an **as-of research prerequisite**, not an instruction to forward-fill a
weekly or delayed source into a same-day state.

## New-series backfill invariant

An incremental update now calculates its start date per indicator. Therefore a newly admitted
series with no local observations fetches from the project start date instead of fetching only
the most recent global-cache lookback window.

## Guardrails

- `美国Baa公司债利差_10Y` is V1 credit-context input only.
- It must not be added to any V0 feature, resonance component, status, or action.
- `高收益债利差` remains `LIMITED_HISTORY_DESCRIPTIVE_ONLY`.
- A later weekly NFCI/NFCI-credit addition requires a dedicated as-of alignment layer because
  publication timing differs from its observation week.
