from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.layer1_macro.io_utils import safe_read_csv, safe_write_text
from src.layer1_macro.paths import META_DIR, PROCESSED_DIR, RAW_DIR, ensure_data_dirs


RELEASE_MANIFEST_PATH = META_DIR / "data_release_manifest.json"

# This list is intentionally short. It contains the inputs and outputs whose
# exact content defines the formal hand-off from Data to Research.
RELEASE_FILES: tuple[Path, ...] = (
    RAW_DIR / "fred_cache.csv",
    RAW_DIR / "yahoo_cache.csv",
    RAW_DIR / "cboe_pcr_cache.csv",
    RAW_DIR / "cboe_pcr_volume_cache.csv",
    PROCESSED_DIR / "combined_macro_market.csv",
    PROCESSED_DIR / "latest_macro_snapshot.csv",
    PROCESSED_DIR / "cboe_pcr_latest_snapshot.csv",
    META_DIR / "source_metadata.csv",
    META_DIR / "data_quality_summary.csv",
    META_DIR / "data_quality_indicator_report.csv",
    META_DIR / "data_quality_alerts.csv",
)


def sha256_file(path: Path) -> str:
    """Return a SHA-256 checksum without loading the full file into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _date_bounds(df: pd.DataFrame) -> tuple[str | None, str | None]:
    if df.empty or "date" not in df.columns:
        return None, None
    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    if dates.empty:
        return None, None
    return dates.min().strftime("%Y-%m-%d"), dates.max().strftime("%Y-%m-%d")


def _csv_record(path: Path) -> dict[str, object]:
    path = Path(path)
    record: dict[str, object] = {
        "relative_path": str(path.relative_to(META_DIR.parent)).replace("\\", "/"),
        "exists": path.exists(),
    }
    if not path.exists():
        return record

    record["size_bytes"] = path.stat().st_size
    record["sha256"] = sha256_file(path)

    frame = safe_read_csv(path)
    record["row_count"] = int(len(frame))
    record["column_count"] = int(len(frame.columns))
    min_date, max_date = _date_bounds(frame)
    if min_date is not None:
        record["min_date"] = min_date
        record["max_date"] = max_date
    return record


def _overall_quality_status() -> str | None:
    path = META_DIR / "data_quality_summary.csv"
    summary = safe_read_csv(path)
    if summary.empty or not {"scope", "metric", "value"}.issubset(summary.columns):
        return None
    row = summary.loc[
        summary["scope"].astype(str).eq("overall")
        & summary["metric"].astype(str).eq("overall_status")
    ]
    if row.empty:
        return None
    return str(row.iloc[0]["value"])


def build_data_release_manifest() -> dict[str, object]:
    """Write the immutable-content manifest for one Data-to-Research release.

    It records file hashes and structural facts only. It creates no indicator,
    score, alert, or portfolio action.
    """
    ensure_data_dirs()
    records = [_csv_record(path) for path in RELEASE_FILES]

    combined = safe_read_csv(PROCESSED_DIR / "combined_macro_market.csv")
    combined_start, combined_end = _date_bounds(combined)

    manifest: dict[str, object] = {
        "manifest_schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "release_scope": "Layer1MacroData stable Data-to-Research handoff",
        "combined_macro_market": {
            "relative_path": "processed/combined_macro_market.csv",
            "row_count": int(len(combined)),
            "column_count": int(len(combined.columns)),
            "start_date": combined_start,
            "end_date": combined_end,
        },
        "overall_data_quality_status": _overall_quality_status(),
        "files": records,
        "interpretation": (
            "Hashes identify the exact stable data inputs. This manifest is provenance "
            "metadata only and must not be interpreted as a market signal or action."
        ),
    }
    safe_write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        RELEASE_MANIFEST_PATH,
    )
    return manifest


def main() -> None:
    manifest = build_data_release_manifest()
    combined = manifest["combined_macro_market"]
    print("[完成] 数据发布清单已生成")
    print(
        "combined_macro_market: "
        f"{combined['row_count']} 行, {combined['column_count']} 列, "
        f"{combined['start_date']} 至 {combined['end_date']}"
    )
    print(f"quality_status: {manifest['overall_data_quality_status']}")
    print(f"output: {RELEASE_MANIFEST_PATH}")


if __name__ == "__main__":
    main()
