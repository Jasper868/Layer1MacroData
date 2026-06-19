from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.layer1_macro.io_utils import normalize_date_column, safe_read_csv, safe_write_csv
from src.layer1_macro.paths import ensure_data_dirs, RAW_DIR, META_DIR
from src.layer1_macro.reports import build_missing_value_report, build_file_size_report


# -----------------------------------------------------------------------------
# Stage 12: FRED + yfinance production data updater
# -----------------------------------------------------------------------------
# Design goals:
#   1. Read existing Git-tracked CSV caches first.
#   2. Incrementally update from remote sources when possible.
#   3. Never destroy existing cache when a remote call fails or returns empty.
#   4. Keep a transparent status table for every indicator.
# -----------------------------------------------------------------------------

DEFAULT_START_DATE = "2006-06-13"
DEFAULT_LOOKBACK_DAYS = 10

FRED_CACHE_PATH = RAW_DIR / "fred_cache.csv"
YAHOO_CACHE_PATH = RAW_DIR / "yahoo_cache.csv"

INDICATOR_DICTIONARY_PATH = META_DIR / "indicator_dictionary.csv"
DATA_STATUS_PATH = META_DIR / "data_status.csv"
MISSING_VALUE_REPORT_PATH = META_DIR / "missing_value_report.csv"
FILE_SIZE_REPORT_PATH = META_DIR / "file_size_report.csv"


@dataclass(frozen=True)
class IndicatorSpec:
    source: str
    code: str
    name: str
    data_type: str
    note: str = ""


FRED_SPECS: list[IndicatorSpec] = [
    IndicatorSpec("FRED", "VIXCLS", "标普500波动率", "宏观/市场指标"),
    IndicatorSpec("FRED", "VXNCLS", "纳指100波动率", "宏观/市场指标"),
    IndicatorSpec("FRED", "VXDCLS", "道琼斯波动率", "宏观/市场指标"),
    IndicatorSpec("FRED", "RVXCLS", "罗素2000波动率", "宏观/市场指标"),
    IndicatorSpec("FRED", "BAMLH0A0HYM2", "高收益债利差", "宏观/市场指标"),
    IndicatorSpec("FRED", "DGS10", "美国10Y收益率", "宏观/市场指标"),
    IndicatorSpec("FRED", "DFII10", "美国10Y实际利率", "宏观/市场指标"),
    IndicatorSpec("FRED", "T10YIE", "10Y通胀预期", "宏观/市场指标"),
    IndicatorSpec("FRED", "DTWEXBGS", "广义美元指数", "宏观/市场指标"),
    IndicatorSpec("FRED", "DEXCHUS", "USD_CNY", "宏观/市场指标"),
    IndicatorSpec("FRED", "DEXUSEU", "EUR_USD", "宏观/市场指标"),
    IndicatorSpec("FRED", "DEXJPUS", "JPY_USD", "宏观/市场指标"),
    IndicatorSpec("FRED", "DCOILWTICO", "WTI原油", "宏观/市场指标"),
    IndicatorSpec("FRED", "DGS3MO", "美国3M国债收益率_现金代理", "三资产组合/现金代理", "用于防御金字塔现金腿日收益近似：DGS3MO / 100 / 252。"),
]

YAHOO_SPECS: list[IndicatorSpec] = [
    IndicatorSpec("yfinance", "GC=F", "黄金期货", "ETF/期货价格代理"),
    IndicatorSpec("yfinance", "GLD", "GLD_黄金代理", "ETF/期货价格代理"),
    IndicatorSpec("yfinance", "SPY", "SPY_标普500代理", "ETF/期货价格代理"),
    IndicatorSpec("yfinance", "QQQ", "QQQ_纳指100代理", "ETF/期货价格代理"),
    IndicatorSpec("yfinance", "DIA", "DIA_道琼斯代理", "ETF/期货价格代理"),
    IndicatorSpec("yfinance", "IWM", "IWM_罗素2000代理", "ETF/期货价格代理"),
    IndicatorSpec("yfinance", "TLT", "TLT_长债代理", "ETF/期货价格代理"),
    IndicatorSpec("yfinance", "ACWI", "ACWI_全球股票代理", "ETF/期货价格代理"),
    IndicatorSpec("yfinance", "^VIX9D", "VIX9D_9日波动率", "波动率期限结构", "Cboe 9-Day Volatility Index；用于观察短端恐慌。"),
    IndicatorSpec("yfinance", "^VIX3M", "VIX3M_3个月波动率", "波动率期限结构", "Cboe 3-Month Volatility Index；用于与VIXCLS构建期限结构。"),
    IndicatorSpec("yfinance", "^VIX6M", "VIX6M_6个月波动率", "波动率期限结构", "Cboe 6-Month Volatility Index；用于观察中端压力。"),
    IndicatorSpec("yfinance", "^SKEW", "SKEW_尾部风险指数", "尾部风险定价", "Cboe SKEW Index；仅作为尾部风险辅助指标，不单独触发调仓。"),
]

ALL_SPECS: list[IndicatorSpec] = FRED_SPECS + YAHOO_SPECS


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _run_time_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_date_str(value: str | pd.Timestamp | None) -> str | None:
    if value is None or pd.isna(value):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _start_from_cache(cache: pd.DataFrame, *, default_start: str, lookback_days: int) -> str:
    if cache.empty or "date" not in cache.columns:
        return default_start

    dates = pd.to_datetime(cache["date"], errors="coerce").dropna()
    if dates.empty:
        return default_start

    latest = dates.max()
    start = latest - pd.Timedelta(days=lookback_days)
    return start.strftime("%Y-%m-%d")


def _count_non_null(df: pd.DataFrame, column: str) -> int:
    if df.empty or column not in df.columns:
        return 0
    return int(df[column].notna().sum())


def _latest_valid_date(df: pd.DataFrame, column: str) -> str | None:
    if df.empty or "date" not in df.columns or column not in df.columns:
        return None
    work = df[["date", column]].copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date", column]).sort_values("date")
    if work.empty:
        return None
    return work.iloc[-1]["date"].strftime("%Y-%m-%d")


def _standardize_cache(df: pd.DataFrame, specs: Iterable[IndicatorSpec]) -> pd.DataFrame:
    columns = ["date"] + [spec.name for spec in specs]
    value_columns = columns[1:]

    if df.empty:
        return pd.DataFrame(columns=columns)

    df = normalize_date_column(df, source_name="cache")
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA

    df = df[columns]

    # A source-cache row with only a date and no indicator value is not usable
    # market data. Keeping it creates fake trading/calendar rows such as
    # 2026-01-01 with every indicator empty.
    df = df.dropna(subset=value_columns, how="all")

    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    return df


def _merge_cache(existing: pd.DataFrame, update: pd.DataFrame, specs: Iterable[IndicatorSpec]) -> pd.DataFrame:
    """
    Merge remote single-indicator updates into a wide local cache.

    Important invariant: a new update may contain only one indicator column.
    Therefore missing columns in the update must NOT overwrite existing values
    for the same date. Only non-null remote values are allowed to replace or
    append cache values.
    """
    columns = ["date"] + [spec.name for spec in specs]
    value_columns = columns[1:]

    existing = _standardize_cache(existing, specs)

    if update.empty:
        return existing[columns]

    update = normalize_date_column(update, source_name="remote_update")
    for col in columns:
        if col not in update.columns:
            update[col] = pd.NA

    update = update[columns]
    update = update.dropna(subset=value_columns, how="all")
    if update.empty:
        return existing[columns]

    existing = existing.copy()
    update = update.copy()
    existing["date"] = pd.to_datetime(existing["date"], errors="coerce")
    update["date"] = pd.to_datetime(update["date"], errors="coerce")
    existing = existing.dropna(subset=["date"]).set_index("date")
    update = update.dropna(subset=["date"]).set_index("date")

    all_dates = existing.index.union(update.index)
    combined = existing.reindex(all_dates)

    # DataFrame.update overwrites with non-null values from update and ignores
    # nulls, which preserves existing values when a single-series update does
    # not carry the other indicators for the same date.
    combined.update(update)

    combined = combined.reset_index().rename(columns={"index": "date"})
    combined = combined.sort_values("date")
    combined = combined.dropna(subset=value_columns, how="all")
    combined["date"] = combined["date"].dt.strftime("%Y-%m-%d")
    return combined[columns]


def _status_row(
    *,
    spec: IndicatorSpec,
    cache_file: Path,
    before_df: pd.DataFrame,
    after_df: pd.DataFrame,
    attempted: bool,
    request_start: str | None,
    remote_rows: int,
    status: str,
    note: str = "",
) -> dict[str, object]:
    return {
        "数据源": spec.source,
        "代码": spec.code,
        "指标名称": spec.name,
        "缓存文件": str(cache_file),
        "本地更新前有效行数": _count_non_null(before_df, spec.name),
        "本地更新前最新日期": _latest_valid_date(before_df, spec.name),
        "是否尝试远程更新": "是" if attempted else "否",
        "远程请求起始日期": request_start,
        "远程返回有效行数": remote_rows,
        "状态": status,
        "本地更新后有效行数": _count_non_null(after_df, spec.name),
        "本地更新后最新日期": _latest_valid_date(after_df, spec.name),
        "备注": note,
        "运行时间": _run_time_str(),
    }


# -----------------------------------------------------------------------------
# FRED fetching
# -----------------------------------------------------------------------------


def _get_fred_key() -> str | None:
    value = os.getenv("FRED_API_KEY")
    if value:
        value = value.strip().strip('"').strip("'")
    return value or None


def _fetch_fred_with_fredapi(series_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    from fredapi import Fred  # optional runtime dependency

    api_key = _get_fred_key()
    fred = Fred(api_key=api_key) if api_key else Fred()
    series = fred.get_series(series_id, observation_start=start_date, observation_end=end_date)
    if series is None or len(series) == 0:
        return pd.DataFrame(columns=["date", series_id])
    df = series.rename(series_id).reset_index()
    df.columns = ["date", series_id]
    return df


def _fetch_fred_with_csv(series_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    # Public FRED graph CSV endpoint. This is a useful fallback when fredapi or
    # API-key handling fails. It does not require the API key.
    import requests
    from io import StringIO

    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    response = requests.get(url, params={"id": series_id}, timeout=30)
    response.raise_for_status()
    df = pd.read_csv(StringIO(response.text))
    if df.empty:
        return pd.DataFrame(columns=["date", series_id])

    date_col = "observation_date" if "observation_date" in df.columns else df.columns[0]
    value_col = series_id if series_id in df.columns else df.columns[-1]
    df = df.rename(columns={date_col: "date", value_col: series_id})[["date", series_id]]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df[series_id] = pd.to_numeric(df[series_id].replace(".", pd.NA), errors="coerce")
    df = df.dropna(subset=["date"])
    mask = (df["date"] >= pd.Timestamp(start_date)) & (df["date"] <= pd.Timestamp(end_date))
    return df.loc[mask].copy()


def fetch_fred_series(spec: IndicatorSpec, start_date: str, end_date: str) -> pd.DataFrame:
    last_error: Exception | None = None

    # First try fredapi; if it fails, use public CSV fallback.
    try:
        raw = _fetch_fred_with_fredapi(spec.code, start_date, end_date)
    except Exception as exc:  # pragma: no cover - depends on network/API availability
        last_error = exc
        raw = pd.DataFrame()

    if raw.empty:
        try:
            raw = _fetch_fred_with_csv(spec.code, start_date, end_date)
        except Exception as exc:  # pragma: no cover - depends on network availability
            if last_error is not None:
                raise RuntimeError(f"fredapi失败: {last_error}; CSV fallback失败: {exc}") from exc
            raise

    if raw.empty:
        return pd.DataFrame(columns=["date", spec.name])

    raw = raw.rename(columns={spec.code: spec.name})
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw[spec.name] = pd.to_numeric(raw[spec.name], errors="coerce")
    raw = raw.dropna(subset=["date"])
    raw = raw[["date", spec.name]].sort_values("date")
    raw["date"] = raw["date"].dt.strftime("%Y-%m-%d")
    return raw


def update_fred_cache(
    *,
    mode: str,
    start_date: str,
    end_date: str,
    lookback_days: int,
    sleep_seconds: float,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    before = _standardize_cache(safe_read_csv(FRED_CACHE_PATH), FRED_SPECS)
    cache = before.copy()
    status_rows: list[dict[str, object]] = []

    if mode == "off":
        for spec in FRED_SPECS:
            status_rows.append(_status_row(
                spec=spec,
                cache_file=FRED_CACHE_PATH,
                before_df=before,
                after_df=cache,
                attempted=False,
                request_start=None,
                remote_rows=0,
                status="跳过远程_mode_off",
            ))
        return cache, status_rows

    request_start = start_date if mode == "full" else _start_from_cache(cache, default_start=start_date, lookback_days=lookback_days)
    print(f"[FRED] 请求区间：{request_start} 至 {end_date}")

    for idx, spec in enumerate(FRED_SPECS, start=1):
        print(f"[FRED {idx}/{len(FRED_SPECS)}] {spec.code} -> {spec.name}")
        spec_before = cache.copy()
        try:
            update = fetch_fred_series(spec, request_start, end_date)
            remote_rows = int(update[spec.name].notna().sum()) if spec.name in update.columns else 0

            if remote_rows > 0:
                cache = _merge_cache(cache, update, FRED_SPECS)
                status = "远程更新成功"
                note = ""
            else:
                status = "远程返回空_使用本地缓存"
                note = "FRED远程返回为空；未覆盖本地缓存。"

        except Exception as exc:
            remote_rows = 0
            status = "远程更新失败_使用本地缓存"
            note = str(exc)[:500]
            print(f"  [失败] {note}")

        status_rows.append(_status_row(
            spec=spec,
            cache_file=FRED_CACHE_PATH,
            before_df=spec_before,
            after_df=cache,
            attempted=True,
            request_start=request_start,
            remote_rows=remote_rows,
            status=status,
            note=note,
        ))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return cache, status_rows


# -----------------------------------------------------------------------------
# yfinance fetching
# -----------------------------------------------------------------------------


def _extract_yfinance_close(raw: pd.DataFrame, ticker: str) -> pd.Series:
    if raw.empty:
        return pd.Series(dtype="float64")

    # yfinance sometimes returns MultiIndex columns, especially when multiple
    # tickers are requested. We fetch one ticker at a time, but this guard makes
    # the function robust across versions.
    if isinstance(raw.columns, pd.MultiIndex):
        if ("Close", ticker) in raw.columns:
            s = raw[("Close", ticker)]
        elif (ticker, "Close") in raw.columns:
            s = raw[(ticker, "Close")]
        elif "Close" in raw.columns.get_level_values(0):
            s = raw.xs("Close", axis=1, level=0).iloc[:, 0]
        elif "Adj Close" in raw.columns.get_level_values(0):
            s = raw.xs("Adj Close", axis=1, level=0).iloc[:, 0]
        else:
            raise ValueError(f"yfinance返回数据缺少Close列：{ticker}, columns={raw.columns}")
    else:
        if "Close" in raw.columns:
            s = raw["Close"]
        elif "Adj Close" in raw.columns:
            s = raw["Adj Close"]
        else:
            raise ValueError(f"yfinance返回数据缺少Close列：{ticker}, columns={list(raw.columns)}")

    return pd.to_numeric(s, errors="coerce")


def fetch_yahoo_series(spec: IndicatorSpec, start_date: str, end_date: str) -> pd.DataFrame:
    import yfinance as yf

    # yfinance end is exclusive, so add one day to include end_date.
    end_exclusive = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    raw = yf.download(
        spec.code,
        start=start_date,
        end=end_exclusive,
        auto_adjust=False,
        progress=False,
        threads=False,
    )

    if raw is None or raw.empty:
        return pd.DataFrame(columns=["date", spec.name])

    close = _extract_yfinance_close(raw, spec.code)
    df = close.rename(spec.name).reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df[spec.name] = pd.to_numeric(df[spec.name], errors="coerce")
    df = df.dropna(subset=["date"])
    df = df[["date", spec.name]].sort_values("date")
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df


def update_yahoo_cache(
    *,
    mode: str,
    start_date: str,
    end_date: str,
    lookback_days: int,
    sleep_seconds: float,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    before = _standardize_cache(safe_read_csv(YAHOO_CACHE_PATH), YAHOO_SPECS)
    cache = before.copy()
    status_rows: list[dict[str, object]] = []

    if mode == "off":
        for spec in YAHOO_SPECS:
            status_rows.append(_status_row(
                spec=spec,
                cache_file=YAHOO_CACHE_PATH,
                before_df=before,
                after_df=cache,
                attempted=False,
                request_start=None,
                remote_rows=0,
                status="跳过远程_mode_off",
            ))
        return cache, status_rows

    request_start = start_date if mode == "full" else _start_from_cache(cache, default_start=start_date, lookback_days=lookback_days)
    print(f"[Yahoo] 请求区间：{request_start} 至 {end_date}")

    for idx, spec in enumerate(YAHOO_SPECS, start=1):
        print(f"[Yahoo {idx}/{len(YAHOO_SPECS)}] {spec.code} -> {spec.name}")
        spec_before = cache.copy()
        try:
            update = fetch_yahoo_series(spec, request_start, end_date)
            remote_rows = int(update[spec.name].notna().sum()) if spec.name in update.columns else 0

            if remote_rows > 0:
                cache = _merge_cache(cache, update, YAHOO_SPECS)
                status = "远程更新成功"
                note = ""
            else:
                status = "远程返回空_使用本地缓存"
                note = "yfinance远程返回为空；未覆盖本地缓存。"

        except Exception as exc:
            remote_rows = 0
            status = "远程更新失败_使用本地缓存"
            note = str(exc)[:500]
            print(f"  [失败] {note}")

        status_rows.append(_status_row(
            spec=spec,
            cache_file=YAHOO_CACHE_PATH,
            before_df=spec_before,
            after_df=cache,
            attempted=True,
            request_start=request_start,
            remote_rows=remote_rows,
            status=status,
            note=note,
        ))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return cache, status_rows


# -----------------------------------------------------------------------------
# Reports / orchestration
# -----------------------------------------------------------------------------


def build_indicator_dictionary() -> pd.DataFrame:
    rows = [
        {
            "数据源": spec.source,
            "代码": spec.code,
            "指标名称": spec.name,
            "数据类型": spec.data_type,
            "说明": spec.note,
        }
        for spec in ALL_SPECS
    ]
    return pd.DataFrame(rows)


def build_fred_yahoo_missing_report(fred: pd.DataFrame, yahoo: pd.DataFrame) -> pd.DataFrame:
    fred_report = build_missing_value_report(fred)
    if not fred_report.empty:
        fred_report.insert(0, "source_table", "fred_cache")

    yahoo_report = build_missing_value_report(yahoo)
    if not yahoo_report.empty:
        yahoo_report.insert(0, "source_table", "yahoo_cache")

    return pd.concat([fred_report, yahoo_report], ignore_index=True)


def write_fred_yahoo_outputs(
    *,
    fred_cache: pd.DataFrame,
    yahoo_cache: pd.DataFrame,
    status_rows: list[dict[str, object]],
) -> None:
    safe_write_csv(fred_cache, FRED_CACHE_PATH)
    safe_write_csv(yahoo_cache, YAHOO_CACHE_PATH)

    indicator_dict = build_indicator_dictionary()
    safe_write_csv(indicator_dict, INDICATOR_DICTIONARY_PATH)

    status = pd.DataFrame(status_rows)
    safe_write_csv(status, DATA_STATUS_PATH)

    missing = build_fred_yahoo_missing_report(fred_cache, yahoo_cache)
    safe_write_csv(missing, MISSING_VALUE_REPORT_PATH)

    file_report = build_file_size_report({
        "fred_cache": FRED_CACHE_PATH,
        "yahoo_cache": YAHOO_CACHE_PATH,
        "indicator_dictionary": INDICATOR_DICTIONARY_PATH,
        "data_status": DATA_STATUS_PATH,
        "missing_value_report": MISSING_VALUE_REPORT_PATH,
    })
    safe_write_csv(file_report, FILE_SIZE_REPORT_PATH)


def update_fred_yahoo(
    *,
    mode: str = "auto",
    start_date: str = DEFAULT_START_DATE,
    end_date: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    sleep_seconds: float = 0.2,
    fred_only: bool = False,
    yahoo_only: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ensure_data_dirs()

    if mode not in {"auto", "off", "full"}:
        raise ValueError("mode must be one of: auto, off, full")

    end_date = end_date or _today_str()

    print("[FRED + yfinance] Stage 12 updater")
    print(f"mode={mode}, start_date={start_date}, end_date={end_date}, lookback_days={lookback_days}")

    status_rows: list[dict[str, object]] = []

    if yahoo_only:
        fred_cache = _standardize_cache(safe_read_csv(FRED_CACHE_PATH), FRED_SPECS)
    else:
        fred_cache, fred_status = update_fred_cache(
            mode=mode,
            start_date=start_date,
            end_date=end_date,
            lookback_days=lookback_days,
            sleep_seconds=sleep_seconds,
        )
        status_rows.extend(fred_status)

    if fred_only:
        yahoo_cache = _standardize_cache(safe_read_csv(YAHOO_CACHE_PATH), YAHOO_SPECS)
    else:
        yahoo_cache, yahoo_status = update_yahoo_cache(
            mode=mode,
            start_date=start_date,
            end_date=end_date,
            lookback_days=lookback_days,
            sleep_seconds=sleep_seconds,
        )
        status_rows.extend(yahoo_status)

    # If one side was skipped by fred_only/yahoo_only, still add transparent status rows.
    if yahoo_only:
        for spec in FRED_SPECS:
            status_rows.append(_status_row(
                spec=spec,
                cache_file=FRED_CACHE_PATH,
                before_df=fred_cache,
                after_df=fred_cache,
                attempted=False,
                request_start=None,
                remote_rows=0,
                status="跳过_remote_yahoo_only",
            ))
    if fred_only:
        for spec in YAHOO_SPECS:
            status_rows.append(_status_row(
                spec=spec,
                cache_file=YAHOO_CACHE_PATH,
                before_df=yahoo_cache,
                after_df=yahoo_cache,
                attempted=False,
                request_start=None,
                remote_rows=0,
                status="跳过_remote_fred_only",
            ))

    write_fred_yahoo_outputs(
        fred_cache=fred_cache,
        yahoo_cache=yahoo_cache,
        status_rows=status_rows,
    )

    status_df = pd.DataFrame(status_rows)

    print("")
    print("[完成] FRED + yfinance 更新流程完成")
    print(f"FRED cache: {len(fred_cache)} 行, {len(fred_cache.columns)} 列 -> {FRED_CACHE_PATH}")
    print(f"Yahoo cache: {len(yahoo_cache)} 行, {len(yahoo_cache.columns)} 列 -> {YAHOO_CACHE_PATH}")
    if not status_df.empty:
        print("状态汇总：")
        print(status_df["状态"].value_counts(dropna=False).to_string())

    return fred_cache, yahoo_cache, status_df


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update FRED and yfinance daily CSV caches.")
    parser.add_argument("--mode", choices=["auto", "off", "full"], default="auto", help="auto=incremental update; off=use cache only; full=refetch from start-date")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Start date for full refresh or empty cache, YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="End date, YYYY-MM-DD. Default: today")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="Incremental update lookback days")
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Sleep between remote requests")
    parser.add_argument("--fred-only", action="store_true", help="Only update FRED cache")
    parser.add_argument("--yahoo-only", action="store_true", help="Only update yfinance cache")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.fred_only and args.yahoo_only:
        raise SystemExit("--fred-only 和 --yahoo-only 不能同时使用。")

    update_fred_yahoo(
        mode=args.mode,
        start_date=args.start_date,
        end_date=args.end_date,
        lookback_days=args.lookback_days,
        sleep_seconds=args.sleep_seconds,
        fred_only=args.fred_only,
        yahoo_only=args.yahoo_only,
    )


if __name__ == "__main__":
    main()
