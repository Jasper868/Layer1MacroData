from __future__ import annotations

import os
import uuid
from pathlib import Path

import pandas as pd


def ensure_parent_dir(path: Path) -> None:
    """确保文件的父目录存在。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _temporary_sibling_path(path: Path) -> Path:
    """Return a unique temporary file path in the target file's directory."""
    path = Path(path)
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def safe_read_csv(path: Path, *, encoding: str = "utf-8-sig") -> pd.DataFrame:
    """安全读取 CSV；文件不存在时返回空 DataFrame。

    A malformed existing CSV is deliberately not hidden: it should fail loudly so
    a damaged cache cannot silently enter the research dataset.
    """
    path = Path(path)
    if not path.exists():
        print(f"[跳过] 文件不存在：{path}")
        return pd.DataFrame()
    return pd.read_csv(path, encoding=encoding)


def safe_write_csv(
    df: pd.DataFrame,
    path: Path,
    *,
    encoding: str = "utf-8-sig",
    index: bool = False,
    announce: bool = True,
) -> None:
    """Atomically write a CSV.

    The file is first written beside its destination and then replaced with one
    filesystem operation. If the process is interrupted while writing, the old
    cache remains intact instead of becoming a partial CSV.
    """
    path = Path(path)
    ensure_parent_dir(path)
    temp_path = _temporary_sibling_path(path)
    try:
        df.to_csv(temp_path, index=index, encoding=encoding)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    if announce:
        print(f"[完成] 原子写入 CSV：{path}")


def safe_write_text(text: str, path: Path, *, encoding: str = "utf-8", announce: bool = True) -> None:
    """Atomically write text using the same replace-on-success policy as CSV."""
    path = Path(path)
    ensure_parent_dir(path)
    temp_path = _temporary_sibling_path(path)
    try:
        temp_path.write_text(text, encoding=encoding)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    if announce:
        print(f"[完成] 原子写入文本：{path}")


def find_date_column(df: pd.DataFrame) -> str | None:
    """自动识别常见日期列。"""
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
    """统一日期列名称为 date，并转成 datetime。"""
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
