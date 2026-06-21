from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.io_utils import safe_read_csv  # noqa: E402
from src.layer1_macro.paths import META_DIR, PROCESSED_DIR  # noqa: E402
from src.layer1_macro.research_availability import (  # noqa: E402
    CONTRACT_SCHEMA_VERSION,
    DATA_DEGRADED,
    DATA_INVALID,
    DATA_VALID,
    HISTORICAL_RESEARCH_DEGRADED,
    HISTORICAL_RESEARCH_UNUSABLE,
    HISTORICAL_RESEARCH_USABLE,
    INDICATOR_CONTRACT_PATH,
    MODULES,
    MODULE_READINESS_PATH,
    PROSPECTIVE_STATE_BLOCKED,
    PROSPECTIVE_STATE_DEGRADED,
    PROSPECTIVE_STATE_PENDING,
    PROSPECTIVE_STATE_READY,
    PROSPECTIVE_STATE_STALE,
    READINESS_BLOCKED,
    READINESS_DEGRADED,
    READINESS_PENDING,
    READINESS_READY,
)


COMBINED_PATH = PROCESSED_DIR / "combined_macro_market.csv"

ALLOWED_INDICATOR_READINESS = {
    READINESS_READY,
    READINESS_PENDING,
    READINESS_DEGRADED,
    READINESS_BLOCKED,
}
ALLOWED_DATA_VALIDITY = {DATA_VALID, DATA_DEGRADED, DATA_INVALID}
ALLOWED_HISTORICAL_RESEARCH = {
    HISTORICAL_RESEARCH_USABLE,
    HISTORICAL_RESEARCH_DEGRADED,
    HISTORICAL_RESEARCH_UNUSABLE,
}
ALLOWED_PROSPECTIVE_STATUS = {
    PROSPECTIVE_STATE_READY,
    PROSPECTIVE_STATE_PENDING,
    PROSPECTIVE_STATE_DEGRADED,
    PROSPECTIVE_STATE_STALE,
    PROSPECTIVE_STATE_BLOCKED,
}
ALLOWED_MODULE_READINESS = ALLOWED_INDICATOR_READINESS


def fail(message: str, errors: list[str]) -> None:
    print(f"[FAIL] {message}")
    errors.append(message)


def _unexpected(values: pd.Series, allowed: set[str]) -> list[str]:
    return sorted(set(values.astype(str)).difference(allowed))


def main() -> int:
    errors: list[str] = []
    combined = safe_read_csv(COMBINED_PATH)
    contract = safe_read_csv(INDICATOR_CONTRACT_PATH)
    modules = safe_read_csv(MODULE_READINESS_PATH)

    if combined.empty or "date" not in combined.columns:
        fail("combined_macro_market.csv is missing or malformed.", errors)
    if contract.empty:
        fail("research_availability_contract.csv is missing or empty.", errors)
    if modules.empty:
        fail("research_module_readiness.csv is missing or empty.", errors)

    if not errors:
        expected_indicators = [column for column in combined.columns if column != "date"]
        required_contract_columns = {
            "contract_schema_version",
            "indicator",
            "module",
            "availability_lag_calendar_days",
            "maximum_expected_tail_delay_calendar_days",
            "latest_valid_observation_date",
            "latest_observation_available_on",
            "latest_observation_age_calendar_days",
            "quality_status",
            "data_validity_status",
            "historical_research_status",
            "prospective_state_status",
            "prospective_state_ready",
            "current_research_readiness",
        }
        missing_columns = sorted(required_contract_columns.difference(contract.columns))
        if missing_columns:
            fail(f"Indicator contract is missing required columns: {missing_columns}", errors)
        else:
            versions = pd.to_numeric(contract["contract_schema_version"], errors="coerce")
            if versions.isna().any() or not versions.eq(CONTRACT_SCHEMA_VERSION).all():
                fail(
                    f"Indicator contract schema version must be {CONTRACT_SCHEMA_VERSION}.",
                    errors,
                )

            actual_indicators = contract["indicator"].astype(str).tolist()
            if len(actual_indicators) != len(set(actual_indicators)):
                fail("Indicator contract contains duplicate indicators.", errors)
            if set(actual_indicators) != set(expected_indicators):
                missing = sorted(set(expected_indicators).difference(actual_indicators))
                extra = sorted(set(actual_indicators).difference(expected_indicators))
                fail(f"Indicator contract coverage mismatch. missing={missing}; extra={extra}", errors)

            for column in (
                "availability_lag_calendar_days",
                "maximum_expected_tail_delay_calendar_days",
            ):
                numeric = pd.to_numeric(contract[column], errors="coerce")
                if numeric.isna().any() or (numeric < 0).any() or (numeric % 1 != 0).any():
                    fail(f"Indicator contract has invalid values in {column}.", errors)

            age = pd.to_numeric(contract["latest_observation_age_calendar_days"], errors="coerce")
            if age.dropna().lt(0).any():
                fail("Indicator contract has a negative latest_observation_age_calendar_days value.", errors)

            for column, allowed in (
                ("data_validity_status", ALLOWED_DATA_VALIDITY),
                ("historical_research_status", ALLOWED_HISTORICAL_RESEARCH),
                ("prospective_state_status", ALLOWED_PROSPECTIVE_STATUS),
                ("current_research_readiness", ALLOWED_INDICATOR_READINESS),
            ):
                unexpected = _unexpected(contract[column], allowed)
                if unexpected:
                    fail(f"Unexpected {column} value(s): {unexpected}", errors)

            expected_ready = contract["prospective_state_status"].astype(str).eq(
                PROSPECTIVE_STATE_READY
            )
            reported_ready = contract["prospective_state_ready"].astype(str).str.lower().eq("true")
            if not expected_ready.equals(reported_ready):
                fail(
                    "prospective_state_ready is not consistent with prospective_state_status.",
                    errors,
                )

            expected_membership = {
                indicator: module
                for module, indicators in MODULES.items()
                for indicator in indicators
            }
            observed_membership = dict(
                zip(contract["indicator"].astype(str), contract["module"].astype(str))
            )
            if observed_membership != expected_membership:
                fail(
                    "Indicator contract does not match the declared one-indicator-one-module partition.",
                    errors,
                )

        required_module_columns = {
            "contract_schema_version",
            "module",
            "required_indicator_count",
            "module_prospective_state_status",
            "module_research_readiness",
        }
        missing_module_columns = sorted(required_module_columns.difference(modules.columns))
        if missing_module_columns:
            fail(f"Module readiness report is missing required columns: {missing_module_columns}", errors)
        else:
            versions = pd.to_numeric(modules["contract_schema_version"], errors="coerce")
            if versions.isna().any() or not versions.eq(CONTRACT_SCHEMA_VERSION).all():
                fail(
                    f"Module readiness schema version must be {CONTRACT_SCHEMA_VERSION}.",
                    errors,
                )
            observed_modules = modules["module"].astype(str).tolist()
            if len(observed_modules) != len(set(observed_modules)):
                fail("Module readiness report contains duplicate modules.", errors)
            if set(observed_modules) != set(MODULES):
                fail("Module readiness report does not cover the declared module set.", errors)

            unexpected_module_statuses = _unexpected(
                modules["module_research_readiness"], ALLOWED_MODULE_READINESS
            )
            if unexpected_module_statuses:
                fail(
                    f"Unexpected module readiness value(s): {unexpected_module_statuses}",
                    errors,
                )

            unexpected_prospective = _unexpected(
                modules["module_prospective_state_status"], ALLOWED_PROSPECTIVE_STATUS
            )
            if unexpected_prospective:
                fail(
                    f"Unexpected module prospective state value(s): {unexpected_prospective}",
                    errors,
                )

    if errors:
        print("\nRESULT: FAIL")
        return 1

    degraded = modules.loc[
        modules["module_research_readiness"].astype(str).eq(READINESS_DEGRADED),
        "module",
    ].astype(str).tolist()
    pending = modules.loc[
        modules["module_research_readiness"].astype(str).eq(READINESS_PENDING),
        "module",
    ].astype(str).tolist()
    blocked = modules.loc[
        modules["module_research_readiness"].astype(str).eq(READINESS_BLOCKED),
        "module",
    ].astype(str).tolist()
    stale = modules.loc[
        modules["module_prospective_state_status"].astype(str).eq(PROSPECTIVE_STATE_STALE),
        "module",
    ].astype(str).tolist()

    print("[PASS] Availability contract covers every raw indicator exactly once.")
    if stale:
        print(f"[WARN] Prospectively stale module(s): {', '.join(stale)}")
    if degraded:
        print(f"[WARN] Degraded module(s): {', '.join(degraded)}")
    if pending:
        print(f"[INFO] Pending module(s): {', '.join(pending)}")
    if blocked:
        print(f"[WARN] Blocked module(s): {', '.join(blocked)}")
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
