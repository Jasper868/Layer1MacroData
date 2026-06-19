from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.layer1_macro.io_utils import safe_read_csv, safe_write_csv, normalize_date_column
from src.layer1_macro.paths import ensure_data_dirs, META_DIR, PROCESSED_DIR


# =============================================================================
# Stage 13: data quality monitoring layer
# =============================================================================
# This module does not fetch remote data and does not change raw caches.
# It only reads the merged research dataset and metadata dictionaries, then writes
# transparent quality reports under data/meta/.
# =============================================================================

COMBINED_MACRO_MARKET_PATH = PROCESSED_DIR / "combined_macro_market.csv"
LATEST_MACRO_SNAPSHOT_PATH = PROCESSED_DIR / "latest_macro_snapshot.csv"
INDICATOR_DICTIONARY_PATH = META_DIR / "indicator_dictionary.csv"
CBOE_DICTIONARY_PATH = META_DIR / "cboe_pcr_dictionary.csv"

DATA_QUALITY_SUMMARY_PATH = META_DIR / "data_quality_summary.csv"
DATA_QUALITY_INDICATOR_REPORT_PATH = META_DIR / "data_quality_indicator_report.csv"
DATA_QUALITY_ALERTS_PATH = META_DIR / "data_quality_alerts.csv"

STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_ORDER = {STATUS_OK: 0, STATUS_WARN: 1, STATUS_FAIL: 2}


@dataclass(frozen=True)
class QualityPolicy:
    """Per-indicator quality policy.

    Lags are measured against the latest date in combined_macro_market.csv, not
    against today's wall-clock date. This avoids false alarms during weekends,
    market holidays, and normal vendor publication delays.
    """

    lag_warn_days: int
    lag_fail_days: int
    recent_window_days: int
    recent_coverage_warn: float
    recent_coverage_fail: float
    min_value: float | None = None
    max_value: float | None = None


DEFAULT_POLICIES: dict[str, QualityPolicy] = {
    "FRED": QualityPolicy(
        lag_warn_days=7,
        lag_fail_days=21,
        recent_window_days=45,
        recent_coverage_warn=0.45,
        recent_coverage_fail=0.20,
    ),
    "yfinance": QualityPolicy(
        lag_warn_days=5,
        lag_fail_days=10,
        recent_window_days=30,
        recent_coverage_warn=0.60,
        recent_coverage_fail=0.30,
        min_value=0,
    ),
    "Cboe": QualityPolicy(
        lag_warn_days=5,
        lag_fail_days=10,
        recent_window_days=30,
        recent_coverage_warn=0.55,
        recent_coverage_fail=0.30,
        min_value=0,
        max_value=10,
    ),
    "UNKNOWN": QualityPolicy(
        lag_warn_days=10,
        lag_fail_days=30,
        recent_window_days=45,
        recent_coverage_warn=0.30,
        recent_coverage_fail=0.10,
    ),
}


# Indicator-specific overrides. These are conservative sanity bounds, not signal
# thresholds. They are designed to catch parsing errors and obviously impossible
# values, not to judge whether a market reading is high or low.
INDICATOR_POLICY_OVERRIDES: dict[str, QualityPolicy] = {
    # Volatility indexes
    "标普500波动率": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=0, max_value=200),
    "纳指100波动率": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=0, max_value=200),
    "道琼斯波动率": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=0, max_value=200),
    "罗素2000波动率": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=0, max_value=200),
    # Rates and spreads
    "美国10Y收益率": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=-5, max_value=25),
    "美国10Y实际利率": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=-10, max_value=15),
    "10Y通胀预期": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=-5, max_value=15),
    "高收益债利差": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=0, max_value=50),
    # FX. FRED exchange-rate series can publish with visible lags, so warn/fail
    # windows are intentionally wider than yfinance market-price proxies.
    "USD_CNY": QualityPolicy(10, 30, 60, 0.30, 0.10, min_value=4, max_value=10),
    "EUR_USD": QualityPolicy(10, 30, 60, 0.30, 0.10, min_value=0.5, max_value=2),
    "JPY_USD": QualityPolicy(10, 30, 60, 0.30, 0.10, min_value=50, max_value=300),
    "广义美元指数": QualityPolicy(10, 30, 60, 0.30, 0.10, min_value=50, max_value=200),
    # WTI can be negative in extreme market structure events, so do not require
    # non-negative values.
    "WTI原油": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=-100, max_value=300),
    "美国3M国债收益率_现金代理": QualityPolicy(7, 21, 45, 0.45, 0.20, min_value=-5, max_value=25),
    "VIX9D_9日波动率": QualityPolicy(5, 10, 30, 0.60, 0.30, min_value=0, max_value=250),
    "VIX3M_3个月波动率": QualityPolicy(5, 10, 30, 0.60, 0.30, min_value=0, max_value=200),
    "VIX6M_6个月波动率": QualityPolicy(5, 10, 30, 0.60, 0.30, min_value=0, max_value=200),
    "SKEW_尾部风险指数": QualityPolicy(5, 10, 30, 0.60, 0.30, min_value=50, max_value=300),
}


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date_str(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return STATUS_OK
    return max(statuses, key=lambda item: STATUS_ORDER.get(item, 0))


def _status_from_threshold(value: float, *, warn: float, fail: float, direction: str) -> str:
    """Classify a metric against warning/failure thresholds.

    direction="high_bad" means larger values are worse, e.g. lag days.
    direction="low_bad" means smaller values are worse, e.g. coverage ratio.
    """
    if direction == "high_bad":
        if value > fail:
            return STATUS_FAIL
        if value > warn:
            return STATUS_WARN
        return STATUS_OK
    if direction == "low_bad":
        if value < fail:
            return STATUS_FAIL
        if value < warn:
            return STATUS_WARN
        return STATUS_OK
    raise ValueError(f"Unknown direction: {direction}")


def _load_expected_indicator_sources() -> dict[str, str]:
    """Return expected indicator -> source from metadata dictionaries."""
    result: dict[str, str] = {}

    indicator_dict = safe_read_csv(INDICATOR_DICTIONARY_PATH)
    if not indicator_dict.empty:
        required_cols = {"指标名称", "数据源"}
        if required_cols.issubset(indicator_dict.columns):
            for _, row in indicator_dict.iterrows():
                name = str(row.get("指标名称", "")).strip()
                source = str(row.get("数据源", "")).strip()
                if name:
                    result[name] = source or "UNKNOWN"

    cboe_dict = safe_read_csv(CBOE_DICTIONARY_PATH)
    if not cboe_dict.empty:
        if "PCR列名" in cboe_dict.columns:
            for _, row in cboe_dict.iterrows():
                name = str(row.get("PCR列名", "")).strip()
                if name:
                    result[name] = "Cboe"

    return result


def _classify_source(indicator: str, expected_sources: dict[str, str]) -> str:
    source = expected_sources.get(indicator)
    if source:
        return source
    if indicator.startswith("CBOE_"):
        return "Cboe"
    return "UNKNOWN"


def _policy_for_indicator(indicator: str, source: str) -> QualityPolicy:
    if indicator in INDICATOR_POLICY_OVERRIDES:
        return INDICATOR_POLICY_OVERRIDES[indicator]
    return DEFAULT_POLICIES.get(source, DEFAULT_POLICIES["UNKNOWN"])


def _range_status(series: pd.Series, policy: QualityPolicy) -> tuple[str, int, int, str]:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return STATUS_OK, 0, 0, ""

    below_count = 0
    above_count = 0

    if policy.min_value is not None:
        below_count = int((numeric < policy.min_value).sum())
    if policy.max_value is not None:
        above_count = int((numeric > policy.max_value).sum())

    if below_count or above_count:
        detail = f"超出理性范围：below_min={below_count}, above_max={above_count}"
        return STATUS_FAIL, below_count, above_count, detail

    return STATUS_OK, 0, 0, ""


def _build_structural_summary(df: pd.DataFrame, expected_sources: dict[str, str]) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    alerts: list[dict[str, object]] = []
    generated_at = _now_str()

    def add(metric: str, value: object, status: str, message: str) -> None:
        rows.append({
            "generated_at": generated_at,
            "scope": "dataset",
            "metric": metric,
            "value": value,
            "status": status,
            "message": message,
        })
        if status != STATUS_OK:
            alerts.append({
                "generated_at": generated_at,
                "scope": "dataset",
                "indicator": "",
                "source": "SYSTEM",
                "status": status,
                "problem_type": metric,
                "message": message,
            })

    if df.empty:
        add("dataset_exists", False, STATUS_FAIL, "combined_macro_market.csv 为空或不存在。")
        return pd.DataFrame(rows), alerts

    if "date" not in df.columns:
        add("date_column_exists", False, STATUS_FAIL, "主表缺少 date 列。")
        return pd.DataFrame(rows), alerts

    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    invalid_date_count = int(work["date"].isna().sum())
    duplicate_date_count = int(work["date"].duplicated().sum())
    value_cols = [col for col in work.columns if col != "date"]
    date_only_count = int(work[value_cols].isna().all(axis=1).sum()) if value_cols else len(work)

    expected_indicators = sorted(expected_sources.keys())
    actual_indicators = [col for col in work.columns if col != "date"]
    missing_expected = sorted(set(expected_indicators) - set(actual_indicators))
    extra_indicators = sorted(set(actual_indicators) - set(expected_indicators))

    min_date = _date_str(work["date"].min())
    max_date = _date_str(work["date"].max())

    add("row_count", len(work), STATUS_OK if len(work) > 0 else STATUS_FAIL, "主表行数。")
    add("indicator_count", len(actual_indicators), STATUS_OK if actual_indicators else STATUS_FAIL, "主表指标列数量，不含 date。")
    add("min_date", min_date, STATUS_OK, "主表最早日期。")
    add("max_date", max_date, STATUS_OK if max_date else STATUS_FAIL, "主表最新日期。")
    add(
        "invalid_date_count",
        invalid_date_count,
        STATUS_FAIL if invalid_date_count else STATUS_OK,
        "无法解析的 date 数量。",
    )
    add(
        "duplicate_date_count",
        duplicate_date_count,
        STATUS_FAIL if duplicate_date_count else STATUS_OK,
        "重复 date 行数量。",
    )
    add(
        "date_only_row_count",
        date_only_count,
        STATUS_FAIL if date_only_count else STATUS_OK,
        "只有 date、全部指标为空的伪日期行数量。",
    )
    add(
        "missing_expected_indicator_count",
        len(missing_expected),
        STATUS_FAIL if missing_expected else STATUS_OK,
        "; ".join(missing_expected) if missing_expected else "指标字典中的指标均已进入主表。",
    )
    add(
        "extra_indicator_count",
        len(extra_indicators),
        STATUS_WARN if extra_indicators else STATUS_OK,
        "; ".join(extra_indicators) if extra_indicators else "主表没有发现字典外指标。",
    )

    return pd.DataFrame(rows), alerts


def _build_indicator_report(df: pd.DataFrame, expected_sources: dict[str, str]) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    if df.empty or "date" not in df.columns:
        return pd.DataFrame(), []

    work = normalize_date_column(df, source_name="combined_macro_market")
    work = work.sort_values("date")
    dataset_max_date = work["date"].max()
    if pd.isna(dataset_max_date):
        return pd.DataFrame(), []

    generated_at = _now_str()
    rows: list[dict[str, object]] = []
    alerts: list[dict[str, object]] = []

    indicators = [col for col in work.columns if col != "date"]

    for indicator in indicators:
        source = _classify_source(indicator, expected_sources)
        policy = _policy_for_indicator(indicator, source)

        series = work[["date", indicator]].copy()
        non_null = series.dropna(subset=[indicator])
        non_null_count = int(len(non_null))
        total_rows = int(len(series))
        full_missing_ratio = 1 - non_null_count / total_rows if total_rows else 1.0

        if non_null.empty:
            latest_date = pd.NaT
            latest_value = None
            lag_days = None
            lag_status = STATUS_FAIL
            lag_message = "指标全空。"
        else:
            latest_row = non_null.iloc[-1]
            latest_date = latest_row["date"]
            latest_value = latest_row[indicator]
            lag_days = int((dataset_max_date - latest_date).days)
            lag_status = _status_from_threshold(
                float(lag_days),
                warn=policy.lag_warn_days,
                fail=policy.lag_fail_days,
                direction="high_bad",
            )
            lag_message = "最新有效日期正常。"
            if lag_status != STATUS_OK:
                lag_message = (
                    f"最新有效日期滞后 {lag_days} 天；"
                    f"WARN>{policy.lag_warn_days}，FAIL>{policy.lag_fail_days}。"
                )

        recent_start = dataset_max_date - pd.Timedelta(days=policy.recent_window_days)
        recent = series[series["date"] >= recent_start]
        recent_rows = int(len(recent))
        recent_non_null = int(recent[indicator].notna().sum()) if recent_rows else 0
        recent_coverage = recent_non_null / recent_rows if recent_rows else 0.0
        recent_status = _status_from_threshold(
            float(recent_coverage),
            warn=policy.recent_coverage_warn,
            fail=policy.recent_coverage_fail,
            direction="low_bad",
        )
        recent_message = "近端覆盖率正常。"
        if recent_status != STATUS_OK:
            recent_message = (
                f"近 {policy.recent_window_days} 日覆盖率 {recent_coverage:.2%}；"
                f"WARN<{policy.recent_coverage_warn:.0%}，FAIL<{policy.recent_coverage_fail:.0%}。"
            )

        range_status, below_min_count, above_max_count, range_message = _range_status(series[indicator], policy)

        full_empty_status = STATUS_FAIL if non_null_count == 0 else STATUS_OK
        status = _worst_status([full_empty_status, lag_status, recent_status, range_status])

        messages = [message for message in [lag_message, recent_message, range_message] if message]
        rows.append({
            "generated_at": generated_at,
            "indicator": indicator,
            "source": source,
            "status": status,
            "dataset_max_date": _date_str(dataset_max_date),
            "latest_valid_date": _date_str(latest_date),
            "latest_value": latest_value,
            "lag_days_vs_dataset_max_date": lag_days,
            "lag_warn_days": policy.lag_warn_days,
            "lag_fail_days": policy.lag_fail_days,
            "total_rows": total_rows,
            "non_null_count": non_null_count,
            "full_missing_ratio": full_missing_ratio,
            "recent_window_days": policy.recent_window_days,
            "recent_rows": recent_rows,
            "recent_non_null_count": recent_non_null,
            "recent_coverage_ratio": recent_coverage,
            "recent_coverage_warn": policy.recent_coverage_warn,
            "recent_coverage_fail": policy.recent_coverage_fail,
            "min_allowed": policy.min_value,
            "max_allowed": policy.max_value,
            "below_min_count": below_min_count,
            "above_max_count": above_max_count,
            "message": " | ".join(messages),
        })

        if status != STATUS_OK:
            problem_types = []
            if full_empty_status != STATUS_OK:
                problem_types.append("full_empty")
            if lag_status != STATUS_OK:
                problem_types.append("stale_latest_date")
            if recent_status != STATUS_OK:
                problem_types.append("low_recent_coverage")
            if range_status != STATUS_OK:
                problem_types.append("range_violation")

            alerts.append({
                "generated_at": generated_at,
                "scope": "indicator",
                "indicator": indicator,
                "source": source,
                "status": status,
                "problem_type": ",".join(problem_types),
                "message": " | ".join(messages),
            })

    report = pd.DataFrame(rows)
    if not report.empty:
        report["_status_rank"] = report["status"].map(STATUS_ORDER).fillna(0).astype(int)
        report = report.sort_values(
            ["_status_rank", "source", "lag_days_vs_dataset_max_date", "indicator"],
            ascending=[False, True, False, True],
            na_position="last",
        ).drop(columns=["_status_rank"])

    return report, alerts


def build_data_quality_reports() -> dict[str, pd.DataFrame | str]:
    """Build Stage 13 data quality reports and return them in memory.

    Outputs:
      - data/meta/data_quality_summary.csv
      - data/meta/data_quality_indicator_report.csv
      - data/meta/data_quality_alerts.csv
    """
    ensure_data_dirs()

    combined = safe_read_csv(COMBINED_MACRO_MARKET_PATH)
    expected_sources = _load_expected_indicator_sources()

    structural_summary, structural_alerts = _build_structural_summary(combined, expected_sources)
    indicator_report, indicator_alerts = _build_indicator_report(combined, expected_sources)

    all_alerts = structural_alerts + indicator_alerts
    alerts = pd.DataFrame(all_alerts)
    if alerts.empty:
        alerts = pd.DataFrame(columns=[
            "generated_at", "scope", "indicator", "source", "status", "problem_type", "message",
        ])
    else:
        alerts["_status_rank"] = alerts["status"].map(STATUS_ORDER).fillna(0).astype(int)
        alerts = alerts.sort_values(
            ["_status_rank", "source", "indicator"],
            ascending=[False, True, True],
        ).drop(columns=["_status_rank"])

    indicator_status_counts = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_FAIL: 0}
    if not indicator_report.empty and "status" in indicator_report.columns:
        indicator_status_counts.update(indicator_report["status"].value_counts().to_dict())

    overall_status = _worst_status(
        list(structural_summary.get("status", pd.Series(dtype=str)).astype(str))
        + list(indicator_report.get("status", pd.Series(dtype=str)).astype(str))
    )

    structural_status_counts = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_FAIL: 0}
    if not structural_summary.empty and "status" in structural_summary.columns:
        structural_status_counts.update(structural_summary["status"].value_counts().to_dict())

    overall_row = pd.DataFrame([{
        "generated_at": _now_str(),
        "scope": "overall",
        "metric": "overall_status",
        "value": overall_status,
        "status": overall_status,
        "message": (
            f"结构状态统计：OK={structural_status_counts.get(STATUS_OK, 0)}, "
            f"WARN={structural_status_counts.get(STATUS_WARN, 0)}, "
            f"FAIL={structural_status_counts.get(STATUS_FAIL, 0)}；"
            f"指标状态统计：OK={indicator_status_counts.get(STATUS_OK, 0)}, "
            f"WARN={indicator_status_counts.get(STATUS_WARN, 0)}, "
            f"FAIL={indicator_status_counts.get(STATUS_FAIL, 0)}。"
        ),
    }])

    summary = pd.concat([overall_row, structural_summary], ignore_index=True)

    safe_write_csv(summary, DATA_QUALITY_SUMMARY_PATH)
    safe_write_csv(indicator_report, DATA_QUALITY_INDICATOR_REPORT_PATH)
    safe_write_csv(alerts, DATA_QUALITY_ALERTS_PATH)

    print("")
    print("[完成] 第十三阶段数据质量监控报告已生成")
    print(f"overall_status = {overall_status}")
    print(f"summary          = {DATA_QUALITY_SUMMARY_PATH}")
    print(f"indicator_report = {DATA_QUALITY_INDICATOR_REPORT_PATH}")
    print(f"alerts           = {DATA_QUALITY_ALERTS_PATH}")

    if not alerts.empty:
        print("")
        print("[数据质量提醒]")
        display_cols = ["status", "source", "indicator", "problem_type", "message"]
        print(alerts[display_cols].head(30).to_string(index=False))

    return {
        "overall_status": overall_status,
        "summary": summary,
        "indicator_report": indicator_report,
        "alerts": alerts,
    }


def main() -> None:
    result = build_data_quality_reports()
    if result["overall_status"] == STATUS_FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
