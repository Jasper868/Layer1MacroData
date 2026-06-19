from __future__ import annotations

from pathlib import Path
import pandas as pd


def ensure_parent_dir(path: Path) -> None:
    """
    确保文件的父目录存在。
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def safe_read_csv(path: Path, *, encoding: str = "utf-8-sig") -> pd.DataFrame:
    """
    安全读取 CSV。
    如果文件不存在，返回空 DataFrame。
    """
    path = Path(path)

    if not path.exists():
        print(f"[跳过] 文件不存在：{path}")
        return pd.DataFrame()

    return pd.read_csv(path, encoding=encoding)


def safe_write_csv(df: pd.DataFrame, path: Path, *, encoding: str = "utf-8-sig") -> None:
    """
    安全写入 CSV。
    """
    path = Path(path)
    ensure_parent_dir(path)
    df.to_csv(path, index=False, encoding=encoding)
    print(f"[完成] 写入 CSV：{path}")


def find_date_column(df: pd.DataFrame) -> str | None:
    """
    自动识别常见日期列。
    """
    candidates = [
        "date",
        "Date",
        "DATE",
        "observation_date",
        "datetime",
        "Datetime",
        "Unnamed: 0",
    ]

    for col in candidates:
        if col in df.columns:
            return col

    return None


def normalize_date_column(df: pd.DataFrame, *, source_name: str = "") -> pd.DataFrame:
    """
    统一日期列名称为 date，并转成 datetime。
    """
    if df.empty:
        return df

    date_col = find_date_column(df)

    if date_col is None:
        raise ValueError(f"{source_name} 未找到日期列，当前字段：{list(df.columns)}")

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])
    df = df.rename(columns={date_col: "date"})
    df = df.sort_values("date")
    df = df.drop_duplicates(subset=["date"], keep="last")

    return df