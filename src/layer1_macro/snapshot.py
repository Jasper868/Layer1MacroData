from __future__ import annotations

import pandas as pd


def build_latest_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    """
    对每个指标取最新有效值。
    注意：不是直接取最后一行，而是逐指标取自己的最新非空值。
    """
    if df.empty:
        return pd.DataFrame()

    if "date" not in df.columns:
        raise ValueError("DataFrame 中必须包含 date 列。")

    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"]).sort_values("date")

    max_date = work["date"].max()

    rows = []

    for col in work.columns:
        if col == "date":
            continue

        s = work[["date", col]].dropna()

        if s.empty:
            rows.append({
                "indicator": col,
                "latest_date": pd.NaT,
                "latest_value": None,
                "lag_days_vs_dataset_max_date": None,
                "non_null_count": 0,
                "status": "全空",
            })
            continue

        latest = s.iloc[-1]
        latest_date = latest["date"]

        rows.append({
            "indicator": col,
            "latest_date": latest_date,
            "latest_value": latest[col],
            "lag_days_vs_dataset_max_date": int((max_date - latest_date).days),
            "non_null_count": len(s),
            "status": "有效",
        })

    snapshot = pd.DataFrame(rows)
    snapshot = snapshot.sort_values(
        ["status", "lag_days_vs_dataset_max_date", "indicator"],
        na_position="last",
    )

    return snapshot