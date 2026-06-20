from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.io_utils import safe_read_csv  # noqa: E402
from src.layer1_macro.paths import META_DIR, PROCESSED_DIR  # noqa: E402
from src.layer1_macro.research_availability import (  # noqa: E402
    INDICATOR_CONTRACT_PATH,
    MODULES,
    MODULE_READINESS_PATH,
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
ALLOWED_MODULE_READINESS = ALLOWED_INDICATOR_READINESS


def fail(message: str, errors: list[str]) -> None:
    print(f"[FAIL] {message}")
    errors.append(message)


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
            "indicator",
            "module",
            "availability_lag_calendar_days",
            "latest_valid_observation_date",
            "latest_observation_available_on",
            "quality_status",
            "current_research_readiness",
        }
        missing_columns = sorted(required_contract_columns.difference(contract.columns))
        if missing_columns:
            fail(f"Indicator contract is missing required columns: {missing_columns}", errors)
        else:
            actual_indicators = contract["indicator"].astype(str).tolist()
            if len(actual_indicators) != len(set(actual_indicators)):
                fail("Indicator contract contains duplicate indicators.", errors)
            if set(actual_indicators) != set(expected_indicators):
                missing = sorted(set(expected_indicators).difference(actual_indicators))
                extra = sorted(set(actual_indicators).difference(expected_indicators))
                fail(f"Indicator contract coverage mismatch. missing={missing}; extra={extra}", errors)
            lag = pd.to_numeric(contract["availability_lag_calendar_days"], errors="coerce")
            if lag.isna().any() or (lag < 0).any() or (lag % 1 != 0).any():
                fail("Indicator contract has an invalid availability lag.", errors)
            unexpected_statuses = sorted(
                set(contract["current_research_readiness"].astype(str))
                .difference(ALLOWED_INDICATOR_READINESS)
            )
            if unexpected_statuses:
                fail(f"Unexpected indicator readiness value(s): {unexpected_statuses}", errors)

            expected_membership = {
                indicator: module
                for module, indicators in MODULES.items()
                for indicator in indicators
            }
            observed_membership = dict(
                zip(contract["indicator"].astype(str), contract["module"].astype(str))
            )
            if observed_membership != expected_membership:
                fail("Indicator contract does not match the declared one-indicator-one-module partition.", errors)

        required_module_columns = {
            "module",
            "required_indicator_count",
            "module_research_readiness",
        }
        missing_module_columns = sorted(required_module_columns.difference(modules.columns))
        if missing_module_columns:
            fail(f"Module readiness report is missing required columns: {missing_module_columns}", errors)
        else:
            observed_modules = modules["module"].astype(str).tolist()
            if len(observed_modules) != len(set(observed_modules)):
                fail("Module readiness report contains duplicate modules.", errors)
            if set(observed_modules) != set(MODULES):
                fail("Module readiness report does not cover the declared module set.", errors)
            unexpected_module_statuses = sorted(
                set(modules["module_research_readiness"].astype(str))
                .difference(ALLOWED_MODULE_READINESS)
            )
            if unexpected_module_statuses:
                fail(f"Unexpected module readiness value(s): {unexpected_module_statuses}", errors)

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

    print("[PASS] Availability contract covers every raw indicator exactly once.")
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
