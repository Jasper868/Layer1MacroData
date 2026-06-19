from __future__ import annotations

from pathlib import Path
import pandas as pd


def build_missing_value_report(df: pd.DataFrame) -> pd.DataFrame:
    """
    生成缺失值报告。
    """
    if df.empty:
        return pd.DataFrame()

    total_rows = len(df)

    rows = []

    for col in df.columns:
        missing_count = int(df[col].isna().sum())
        non_null_count = int(df[col].notna().sum())
        missing_ratio = missing_count / total_rows if total_rows > 0 else None

        rows.append({
            "column": col,
            "total_rows": total_rows,
            "non_null_count": non_null_count,
            "missing_count": missing_count,
            "missing_ratio": missing_ratio,
        })

    report = pd.DataFrame(rows)
    return report.sort_values(["missing_ratio", "column"], ascending=[False, True])


def safe_file_size_mb(path: Path) -> float | None:
    """
    文件大小，单位 MiB。
    """
    path = Path(path)

    if not path.exists():
        return None

    return round(path.stat().st_size / 1024 / 1024, 4)


def build_file_size_report(file_map: dict[str, Path]) -> pd.DataFrame:
    """
    生成文件大小报告。
    """
    rows = []

    for name, path in file_map.items():
        path = Path(path)
        rows.append({
            "file_name": name,
            "path": str(path),
            "exists": path.exists(),
            "size_mib": safe_file_size_mb(path),
        })

    return pd.DataFrame(rows)