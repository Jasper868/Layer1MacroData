# 15. Data Release Contract

## Purpose

This layer records the exact, validated Data-to-Research handoff. It does not
create a research feature, risk score, alert, or portfolio action.

## Output

```text
data/meta/data_release_manifest.json
```

The manifest records the SHA-256 checksum, file size, row/column counts, and
applicable date range of the raw caches and formal handoff outputs.

## Release gate

The standard command is:

```powershell
python scripts/run_data_update.py
```

After fetching, merging, and data quality checks, the command builds the
manifest and runs `scripts/check_data_release_ready.py`.

A publishable release must end with:

```text
RESULT: READY_TO_PUBLISH
```

`WARN` is a publishable degraded state. Read `data/meta/data_quality_alerts.csv`
before publishing. `FAIL` means do not publish.

## What is checked

1. The combined table has valid, unique dates and no date-only rows.
2. `source_metadata.csv` covers every and only current raw input column.
3. Each cached Cboe date has an accepted validation record. A zero Call-volume
   denominator is accepted only when range and total-volume checks pass.
4. The quality gate is not `FAIL`.
5. Every formal handoff file still matches its manifest SHA-256 checksum.

## Interpretation boundary

This contract is provenance and data-governance infrastructure only. It does
not say that a market is safe, risky, investable, or likely to decline.
