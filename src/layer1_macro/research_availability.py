from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from src.layer1_macro.io_utils import safe_read_csv, safe_write_csv
from src.layer1_macro.paths import META_DIR, PROCESSED_DIR, ensure_data_dirs


COMBINED_PATH = PROCESSED_DIR / "combined_macro_market.csv"
SOURCE_METADATA_PATH = META_DIR / "source_metadata.csv"
QUALITY_REPORT_PATH = META_DIR / "data_quality_indicator_report.csv"

INDICATOR_CONTRACT_PATH = META_DIR / "research_availability_contract.csv"
MODULE_READINESS_PATH = META_DIR / "research_module_readiness.csv"

# This is a mutually exclusive ownership partition of the current 33 raw
# indicators. A research feature may depend on more than one module, but a raw
# indicator belongs to exactly one module here.
MODULES: dict[str, tuple[str, ...]] = {
    "equity_volatility_core": (
        "标普500波动率",
        "纳指100波动率",
        "道琼斯波动率",
        "罗素2000波动率",
    ),
    "credit_context": (
        "高收益债利差",
        "美国Baa公司债利差_10Y",
    ),
    "rates_inflation": (
        "美国10Y收益率",
        "美国10Y实际利率",
        "10Y通胀预期",
        "美国3M国债收益率_现金代理",
    ),
    "fx_commodities": (
        "广义美元指数",
        "USD_CNY",
        "EUR_USD",
        "JPY_USD",
        "WTI原油",
        "黄金期货",
    ),
    "market_asset_proxies": (
        "GLD_黄金代理",
        "SPY_标普500代理",
        "QQQ_纳指100代理",
        "DIA_道琼斯代理",
        "IWM_罗素2000代理",
        "TLT_长债代理",
        "ACWI_全球股票代理",
    ),
    "volatility_curve_tail": (
        "VIX9D_9日波动率",
        "VIX3M_3个月波动率",
        "VIX6M_6个月波动率",
        "SKEW_尾部风险指数",
    ),
    "options_positioning": (
        "CBOE_Total_PCR",
        "CBOE_Index_PCR",
        "CBOE_ETP_PCR",
        "CBOE_Equity_PCR",
        "CBOE_VIX_PCR",
        "CBOE_SPX_PCR",
    ),
}

QUALITY_OK = "OK"
QUALITY_WARN = "WARN"
QUALITY_FAIL = "FAIL"
READINESS_READY = "READY"
READINESS_PENDING = "PENDING_LATEST_OBSERVATION"
READINESS_DEGRADED = "DEGRADED"
READINESS_BLOCKED = "BLOCKED"


class AvailabilityContractError(RuntimeError):
    """Raised when the point-in-time handoff contract is incomplete."""


def _local_as_of_date() -> pd.Timestamp:
    return pd.Timestamp(date.today())


def _parse_date(value: object) -> pd.Timestamp | pd.NaT:
    return pd.to_datetime(value, errors="coerce")


def _validate_source_metadata(metadata: pd.DataFrame, indicators: list[str]) -> pd.DataFrame:
    required = {
        "source",
        "code",
        "indicator_name",
        "conservative_availability_lag_calendar_days",
        "availability_rule",
        "prospective_alignment",
        "v1_admission_note",
    }
    missing = sorted(required.difference(metadata.columns))
    if missing:
        raise AvailabilityContractError(
            f"source_metadata.csv is missing required column(s): {missing}"
        )

    work = metadata.copy()
    work["indicator_name"] = work["indicator_name"].astype(str).str.strip()
    if work["indicator_name"].duplicated().any():
        duplicates = sorted(work.loc[work["indicator_name"].duplicated(), "indicator_name"].unique())
        raise AvailabilityContractError(
            f"source_metadata.csv has duplicate indicator_name value(s): {duplicates}"
        )

    expected = set(indicators)
    actual = set(work["indicator_name"])
    missing_metadata = sorted(expected.difference(actual))
    extra_metadata = sorted(actual.difference(expected))
    if missing_metadata or extra_metadata:
        raise AvailabilityContractError(
            "Source metadata coverage mismatch. "
            f"missing={missing_metadata}; extra={extra_metadata}"
        )

    lag = pd.to_numeric(
        work["conservative_availability_lag_calendar_days"], errors="coerce"
    )
    invalid_lag = lag.isna() | (lag < 0) | (lag % 1 != 0)
    if invalid_lag.any():
        bad = work.loc[invalid_lag, "indicator_name"].tolist()
        raise AvailabilityContractError(
            f"Availability lag must be a non-negative whole number. Bad indicator(s): {bad}"
        )
    work["conservative_availability_lag_calendar_days"] = lag.astype(int)
    return work.set_index("indicator_name", drop=False)


def _quality_by_indicator(indicators: list[str]) -> pd.DataFrame:
    quality = safe_read_csv(QUALITY_REPORT_PATH)
    required = {"indicator", "status", "latest_valid_date"}
    if quality.empty or not required.issubset(quality.columns):
        raise AvailabilityContractError(
            "data_quality_indicator_report.csv is missing or malformed. "
            "Run data quality before building the availability contract."
        )

    work = quality.copy()
    work["indicator"] = work["indicator"].astype(str).str.strip()
    if work["indicator"].duplicated().any():
        duplicates = sorted(work.loc[work["indicator"].duplicated(), "indicator"].unique())
        raise AvailabilityContractError(
            f"data_quality_indicator_report.csv has duplicate indicator value(s): {duplicates}"
        )

    expected = set(indicators)
    actual = set(work["indicator"])
    missing = sorted(expected.difference(actual))
    if missing:
        raise AvailabilityContractError(
            f"Quality report lacks current indicator(s): {missing}"
        )
    return work.set_index("indicator", drop=False)


def _indicator_readiness(
    quality_status: str,
    latest_valid_date: pd.Timestamp | pd.NaT,
    latest_available_on: pd.Timestamp | pd.NaT,
    as_of_date: pd.Timestamp,
) -> tuple[str, str]:
    status = str(quality_status).strip().upper()
    if status == QUALITY_FAIL or pd.isna(latest_valid_date):
        return READINESS_BLOCKED, "Quality report is FAIL or latest valid observation is unavailable."
    if status == QUALITY_WARN:
        return READINESS_DEGRADED, "Quality report is WARN; use only as degraded context, not as fresh input."
    if status != QUALITY_OK:
        return READINESS_BLOCKED, f"Unrecognised quality status: {quality_status!r}."
    if pd.isna(latest_available_on):
        return READINESS_BLOCKED, "Latest observation has no declared available-on date."
    if latest_available_on > as_of_date:
        return (
            READINESS_PENDING,
            "Latest observation exists but is not yet usable under the conservative availability lag.",
        )
    return READINESS_READY, "Quality is OK and the latest observation has passed the declared availability lag."


def _module_membership(indicators: list[str]) -> dict[str, str]:
    membership: dict[str, str] = {}
    for module, members in MODULES.items():
        for indicator in members:
            if indicator in membership:
                raise AvailabilityContractError(
                    f"Indicator {indicator!r} belongs to more than one module: "
                    f"{membership[indicator]!r}, {module!r}"
                )
            membership[indicator] = module

    expected = set(indicators)
    actual = set(membership)
    missing = sorted(expected.difference(actual))
    extra = sorted(actual.difference(expected))
    if missing or extra:
        raise AvailabilityContractError(
            f"Module membership mismatch. missing={missing}; extra={extra}"
        )
    return membership


def build_research_availability_contract() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the machine-readable point-in-time handoff contract.

    This function does not create features, scores, alerts, or portfolio actions.
    It records the declared availability rule that the Research repository must
    apply before using an observation in any historical or live analysis.
    """
    ensure_data_dirs()
    combined = safe_read_csv(COMBINED_PATH)
    if combined.empty or "date" not in combined.columns:
        raise AvailabilityContractError(
            "combined_macro_market.csv is missing or malformed. Build the combined dataset first."
        )

    indicators = [str(column) for column in combined.columns if column != "date"]
    if not indicators:
        raise AvailabilityContractError("combined_macro_market.csv has no indicator columns.")

    metadata = _validate_source_metadata(safe_read_csv(SOURCE_METADATA_PATH), indicators)
    quality = _quality_by_indicator(indicators)
    membership = _module_membership(indicators)
    as_of_date = _local_as_of_date()

    rows: list[dict[str, object]] = []
    for indicator in indicators:
        metadata_row = metadata.loc[indicator]
        quality_row = quality.loc[indicator]
        latest_valid_date = _parse_date(quality_row["latest_valid_date"])
        lag_days = int(metadata_row["conservative_availability_lag_calendar_days"])
        latest_available_on = (
            latest_valid_date + pd.Timedelta(days=lag_days)
            if not pd.isna(latest_valid_date)
            else pd.NaT
        )
        readiness, reason = _indicator_readiness(
            str(quality_row["status"]),
            latest_valid_date,
            latest_available_on,
            as_of_date,
        )
        rows.append(
            {
                "contract_schema_version": 1,
                "as_of_local_date": as_of_date.strftime("%Y-%m-%d"),
                "module": membership[indicator],
                "indicator": indicator,
                "source": str(metadata_row["source"]),
                "source_code": str(metadata_row["code"]),
                "availability_lag_calendar_days": lag_days,
                "historical_research_rule": (
                    "usable only when research_date >= observation_date + "
                    f"{lag_days} calendar day(s); never forward-fill before availability"
                ),
                "availability_rule": str(metadata_row["availability_rule"]),
                "prospective_alignment": str(metadata_row["prospective_alignment"]),
                "admission_note": str(metadata_row["v1_admission_note"]),
                "latest_valid_observation_date": (
                    latest_valid_date.strftime("%Y-%m-%d")
                    if not pd.isna(latest_valid_date)
                    else ""
                ),
                "latest_observation_available_on": (
                    latest_available_on.strftime("%Y-%m-%d")
                    if not pd.isna(latest_available_on)
                    else ""
                ),
                "quality_status": str(quality_row["status"]),
                "current_research_readiness": readiness,
                "readiness_reason": reason,
            }
        )

    contract = pd.DataFrame(rows)
    safe_write_csv(contract, INDICATOR_CONTRACT_PATH)
    modules = build_module_readiness(contract)
    safe_write_csv(modules, MODULE_READINESS_PATH)
    return contract, modules


def _module_readiness_from_members(members: pd.DataFrame) -> tuple[str, str]:
    readiness_values = set(members["current_research_readiness"].astype(str))
    if READINESS_BLOCKED in readiness_values:
        return READINESS_BLOCKED, "At least one required indicator is blocked."
    if READINESS_DEGRADED in readiness_values:
        return READINESS_DEGRADED, "At least one required indicator is degraded."
    if READINESS_PENDING in readiness_values:
        return READINESS_PENDING, "At least one latest observation is not yet available under the declared lag."
    return READINESS_READY, "All required indicators are quality-OK and currently available."


def build_module_readiness(contract: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for module in MODULES:
        members = contract.loc[contract["module"].eq(module)].copy()
        readiness, reason = _module_readiness_from_members(members)
        non_ready = members.loc[
            ~members["current_research_readiness"].eq(READINESS_READY), "indicator"
        ].astype(str).tolist()
        rows.append(
            {
                "contract_schema_version": 1,
                "as_of_local_date": str(members.iloc[0]["as_of_local_date"]),
                "module": module,
                "required_indicator_count": int(len(members)),
                "ready_indicator_count": int(
                    members["current_research_readiness"].eq(READINESS_READY).sum()
                ),
                "pending_indicator_count": int(
                    members["current_research_readiness"].eq(READINESS_PENDING).sum()
                ),
                "degraded_indicator_count": int(
                    members["current_research_readiness"].eq(READINESS_DEGRADED).sum()
                ),
                "blocked_indicator_count": int(
                    members["current_research_readiness"].eq(READINESS_BLOCKED).sum()
                ),
                "module_research_readiness": readiness,
                "non_ready_indicators": "; ".join(non_ready),
                "readiness_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    contract, modules = build_research_availability_contract()
    print("[完成] Research availability contract generated")
    print(f"indicator_contract = {INDICATOR_CONTRACT_PATH}")
    print(f"module_readiness   = {MODULE_READINESS_PATH}")
    print("\n[Module readiness]")
    print(
        modules[
            [
                "module",
                "module_research_readiness",
                "non_ready_indicators",
            ]
        ].to_string(index=False)
    )


if __name__ == "__main__":
    main()
