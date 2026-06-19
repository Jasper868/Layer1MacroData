from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.paths import META_DIR, PROCESSED_DIR
from src.layer1_macro.io_utils import safe_read_csv

COMBINED_PATH = PROCESSED_DIR / "combined_macro_market.csv"
METADATA_PATH = META_DIR / "source_metadata.csv"

REQUIRED_COLUMNS = {
    "source",
    "code",
    "indicator_name",
    "data_type",
    "observation_frequency",
    "update_frequency",
    "availability_rule",
    "conservative_availability_lag_calendar_days",
    "revision_policy",
    "maximum_expected_tail_delay_calendar_days",
    "prospective_alignment",
    "v1_admission_note",
    "notes",
}


def main() -> int:
    combined = safe_read_csv(COMBINED_PATH)
    metadata = safe_read_csv(METADATA_PATH)

    if combined.empty or "date" not in combined.columns:
        print(f"[FAIL] Missing or invalid combined input: {COMBINED_PATH}")
        return 1
    if metadata.empty:
        print(
            f"[FAIL] Missing source metadata: {METADATA_PATH}\n"
            "Run `python scripts\\run_data_update.py --fred-yahoo-mode auto --cboe-mode auto`."
        )
        return 1

    missing_columns = sorted(REQUIRED_COLUMNS.difference(metadata.columns))
    if missing_columns:
        print(f"[FAIL] Source metadata missing fields: {missing_columns}")
        return 1
    if metadata["indicator_name"].astype(str).duplicated().any():
        duplicates = metadata.loc[
            metadata["indicator_name"].astype(str).duplicated(), "indicator_name"
        ].astype(str).tolist()
        print(f"[FAIL] Duplicate source metadata rows: {duplicates}")
        return 1

    input_columns = set(combined.columns).difference({"date"})
    metadata_columns = set(metadata["indicator_name"].astype(str))
    missing_metadata = sorted(input_columns.difference(metadata_columns))
    extra_metadata = sorted(metadata_columns.difference(input_columns))
    if missing_metadata or extra_metadata:
        print("[FAIL] Source metadata must cover every and only combined input column.")
        if missing_metadata:
            print(f"[FAIL] Missing metadata for: {missing_metadata}")
        if extra_metadata:
            print(f"[FAIL] Metadata without combined input column: {extra_metadata}")
        return 1

    text_columns = [
        "source",
        "code",
        "observation_frequency",
        "update_frequency",
        "availability_rule",
        "revision_policy",
        "prospective_alignment",
        "v1_admission_note",
    ]
    blank_fields: list[str] = []
    for column in text_columns:
        blank = metadata[column].isna() | metadata[column].astype(str).str.strip().eq("")
        if blank.any():
            blank_fields.append(f"{column}: {int(blank.sum())} blank")
    if blank_fields:
        print(f"[FAIL] Source metadata has blank contract fields: {blank_fields}")
        return 1

    for column in [
        "conservative_availability_lag_calendar_days",
        "maximum_expected_tail_delay_calendar_days",
    ]:
        values = pd.to_numeric(metadata[column], errors="coerce")
        if values.isna().any() or (values < 0).any():
            print(f"[FAIL] {column} must contain non-negative numeric values.")
            return 1

    baa = metadata.loc[metadata["indicator_name"].astype(str).eq("美国Baa公司债利差_10Y")]
    if len(baa) != 1 or str(baa.iloc[0]["code"]).strip() != "BAA10Y":
        print("[FAIL] BAA10Y long-history credit-proxy metadata is missing or malformed.")
        return 1

    values = pd.to_numeric(combined["美国Baa公司债利差_10Y"], errors="coerce")
    if values.notna().sum() < 252 * 10:
        print(
            "[FAIL] BAA10Y has not yet been fully hydrated with at least 10 years of usable data.\n"
            "Run the online Data update once. New series must backfill from the project start."
        )
        return 1

    print("[PASS] Source metadata covers every current combined input exactly once.")
    print(f"[Info] Combined raw columns: {len(input_columns)}")
    print(
        "[Info] BAA10Y usable observations: "
        f"{int(values.notna().sum())}; first="
        f"{pd.to_datetime(combined.loc[values.notna(), 'date']).min().date()}; "
        f"last={pd.to_datetime(combined.loc[values.notna(), 'date']).max().date()}"
    )
    print("[PASS] BAA10Y is admitted only as a V1 daily credit-context source; it is not a high-yield-OAS splice.")
    print("[PASS] This contract creates no feature, score, watch state, or portfolio action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
