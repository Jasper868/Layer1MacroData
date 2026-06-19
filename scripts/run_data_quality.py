from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.data_quality import (  # noqa: E402
    STATUS_FAIL,
    STATUS_WARN,
    build_data_quality_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 13 data quality checks.")
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Always exit 0 after writing reports, even when quality status is FAIL.",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero when overall status is WARN or FAIL. Default only fails on FAIL.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = build_data_quality_reports()
    status = str(result["overall_status"])

    if args.no_fail:
        return

    if status == STATUS_FAIL:
        raise SystemExit(1)

    if args.fail_on_warning and status == STATUS_WARN:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
