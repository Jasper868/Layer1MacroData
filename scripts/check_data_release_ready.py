from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.cboe_pcr import CboePcrUpdater  # noqa: E402
from src.layer1_macro.io_utils import safe_read_csv  # noqa: E402
from src.layer1_macro.paths import META_DIR, PROCESSED_DIR  # noqa: E402
from src.layer1_macro.release_manifest import (  # noqa: E402
    RELEASE_MANIFEST_PATH,
    sha256_file,
)


COMBINED_PATH = PROCESSED_DIR / "combined_macro_market.csv"
SOURCE_METADATA_PATH = META_DIR / "source_metadata.csv"
QUALITY_PATH = META_DIR / "data_quality_summary.csv"


def fail(message: str, errors: list[str]) -> None:
    print(f"[FAIL] {message}")
    errors.append(message)


def main() -> int:
    errors: list[str] = []

    if not RELEASE_MANIFEST_PATH.exists():
        fail("Missing data_release_manifest.json. Run the standard update flow first.", errors)
        return 1

    try:
        manifest = json.loads(RELEASE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"Cannot read data_release_manifest.json: {exc}", errors)
        return 1

    combined = safe_read_csv(COMBINED_PATH)
    if combined.empty or "date" not in combined.columns:
        fail("combined_macro_market.csv is empty or lacks date.", errors)
    else:
        dates = pd.to_datetime(combined["date"], errors="coerce")
        if dates.isna().any():
            fail("combined_macro_market.csv has invalid date values.", errors)
        if dates.duplicated().any():
            fail("combined_macro_market.csv has duplicate dates.", errors)
        value_columns = [col for col in combined.columns if col != "date"]
        if not value_columns:
            fail("combined_macro_market.csv has no indicator columns.", errors)
        elif combined[value_columns].isna().all(axis=1).any():
            fail("combined_macro_market.csv contains date-only rows.", errors)

    metadata = safe_read_csv(SOURCE_METADATA_PATH)
    if metadata.empty or "indicator_name" not in metadata.columns:
        fail("source_metadata.csv is missing or malformed.", errors)
    elif not combined.empty:
        raw_columns = set(combined.columns).difference({"date"})
        metadata_columns = set(metadata["indicator_name"].astype(str))
        if raw_columns != metadata_columns:
            missing = sorted(raw_columns.difference(metadata_columns))
            extra = sorted(metadata_columns.difference(raw_columns))
            fail(f"Source metadata coverage mismatch. missing={missing}; extra={extra}", errors)

    # The raw Cboe cache is admissible only when every cached date has an
    # accepted validation record. This protects the combined research input from
    # a parser change that produced a complete-looking but unvalidated row.
    try:
        cboe = CboePcrUpdater()
        cboe.load_caches()
        invalid_cboe_dates = [
            pd.Timestamp(date).strftime("%Y-%m-%d")
            for date in cboe.pcr_cache.index
            if not cboe.row_is_fully_validated(
                cboe.pcr_cache, cboe.volume_cache, cboe.validation_cache, pd.Timestamp(date)
            )
        ]
        if invalid_cboe_dates:
            preview = ", ".join(invalid_cboe_dates[:10])
            suffix = " ..." if len(invalid_cboe_dates) > 10 else ""
            fail(f"Cboe cache has {len(invalid_cboe_dates)} unaccepted validation date(s): {preview}{suffix}", errors)
        else:
            print("[PASS] Every cached Cboe date has an accepted validation record.")
    except Exception as exc:
        fail(f"Cannot validate Cboe cache integrity: {exc}", errors)

    quality = safe_read_csv(QUALITY_PATH)
    quality_status = None
    if not quality.empty and {"scope", "metric", "value"}.issubset(quality.columns):
        overall = quality.loc[
            quality["scope"].astype(str).eq("overall")
            & quality["metric"].astype(str).eq("overall_status")
        ]
        if not overall.empty:
            quality_status = str(overall.iloc[0]["value"])
    if quality_status is None:
        fail("Cannot determine overall data quality status.", errors)
    elif quality_status == "FAIL":
        fail("Overall data quality status is FAIL; do not publish this release.", errors)
    elif quality_status == "WARN":
        print("[WARN] Overall data quality status is WARN. Publishing is permitted, but read data_quality_alerts.csv.")
    else:
        print("[PASS] Overall data quality status is OK.")

    records = manifest.get("files", [])
    if not isinstance(records, list) or not records:
        fail("Manifest has no file records.", errors)
    else:
        for record in records:
            relative = record.get("relative_path")
            expected_hash = record.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                fail("Manifest contains an incomplete file record.", errors)
                continue
            path = META_DIR.parent / relative
            if not path.exists():
                fail(f"Manifest file is missing: {relative}", errors)
                continue
            actual_hash = sha256_file(path)
            if actual_hash != expected_hash:
                fail(f"Manifest hash mismatch: {relative}", errors)

    if errors:
        print("\nRESULT: FAIL")
        return 1

    print("[PASS] Combined data structure, source metadata, quality gate, and release hashes are consistent.")
    print("RESULT: READY_TO_PUBLISH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
