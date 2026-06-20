from __future__ import annotations

from pathlib import Path
import pandas as pd

from src.layer1_macro.io_utils import (
    safe_read_csv,
    safe_write_csv,
    normalize_date_column,
)
from src.layer1_macro.snapshot import build_latest_snapshot
from src.layer1_macro.reports import build_missing_value_report
from src.layer1_macro.paths import (
    ensure_data_dirs,
    RAW_DIR,
    PROCESSED_DIR,
    META_DIR,
)


FRED_CACHE_PATH = RAW_DIR / "fred_cache.csv"
YAHOO_CACHE_PATH = RAW_DIR / "yahoo_cache.csv"
CBOE_PCR_CACHE_PATH = RAW_DIR / "cboe_pcr_cache.csv"

COMBINED_MACRO_MARKET_PATH = PROCESSED_DIR / "combined_macro_market.csv"
LATEST_MACRO_SNAPSHOT_PATH = PROCESSED_DIR / "latest_macro_snapshot.csv"
COMBINED_MISSING_REPORT_PATH = META_DIR / "combined_missing_value_report.csv"


def _read_source(path: Path, source_name: str) -> pd.DataFrame:
    df = safe_read_csv(path)

    if df.empty:
        return df

    df = normalize_date_column(df, source_name=source_name)

    print(f"[读取] {source_name}: {len(df)} 行, {len(df.columns)} 列")
    return df


def load_fred_cache() -> pd.DataFrame:
    return _read_source(FRED_CACHE_PATH, "FRED")


def load_yahoo_cache() -> pd.DataFrame:
    return _read_source(YAHOO_CACHE_PATH, "Yahoo")


def load_cboe_cache() -> pd.DataFrame:
    df = _read_source(CBOE_PCR_CACHE_PATH, "Cboe PCR")

    if df.empty:
        return df

    # 正式研究口径：只保留 2019-10-07 之后的同口径 Daily 数据
    df = df[df["date"] >= pd.Timestamp("2019-10-07")].copy()

    print(f"[过滤] Cboe PCR 保留 2019-10-07 之后：{len(df)} 行")
    return df


def merge_sources(dfs: list[pd.DataFrame]) -> pd.DataFrame:
    """
    外连接合并所有数据源。
    
    合并后删除“只有 date、所有指标为空”的伪日期行。
    例如美股/FRED/Cboe 全部休市的 2026-01-01，不应进入研究主表。
    """
    valid_dfs = [df for df in dfs if df is not None and not df.empty]

    if not valid_dfs:
        raise RuntimeError("没有可合并的数据。请先运行取数 notebook。")

    combined = valid_dfs[0].copy()

    for right in valid_dfs[1:]:
        overlap = set(combined.columns).intersection(set(right.columns)) - {"date"}

        if overlap:
            raise RuntimeError(
                "Source schema collision: duplicate indicator column(s) "
                f"{sorted(overlap)}. Stop the build and resolve the source "
                "definition explicitly; never create automatic '_dup' columns."
            )

        combined = combined.merge(right, on="date", how="outer")

    combined = combined.sort_values("date")
    combined = combined.drop_duplicates(subset=["date"], keep="last")

    value_columns = [col for col in combined.columns if col != "date"]
    combined = combined.dropna(subset=value_columns, how="all")

    return combined


def build_combined_macro_market() -> pd.DataFrame:
    """
    构建第一层宏观市场合并数据集。
    """
    ensure_data_dirs()

    fred = load_fred_cache()
    yahoo = load_yahoo_cache()
    cboe = load_cboe_cache()
    combined = merge_sources([fred, yahoo, cboe])

    safe_write_csv(combined, COMBINED_MACRO_MARKET_PATH)

    latest_snapshot = build_latest_snapshot(combined)
    safe_write_csv(latest_snapshot, LATEST_MACRO_SNAPSHOT_PATH)

    missing_report = build_missing_value_report(combined)
    safe_write_csv(missing_report, COMBINED_MISSING_REPORT_PATH)

    print("")
    print("[完成] 第一层合并数据集构建完成")
    print(f"行数：{len(combined)}")
    print(f"字段数：{len(combined.columns)}")
    print(f"输出：{COMBINED_MACRO_MARKET_PATH}")

    return combined


def main() -> None:
    build_combined_macro_market()


if __name__ == "__main__":
    main()