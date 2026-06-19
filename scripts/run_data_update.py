from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]


def run_command(command: list[str]) -> None:
    print("\n" + "=" * 78)
    print("[RUN] " + " ".join(command))
    print("=" * 78)
    result = subprocess.run(command, cwd=PROJECT_DIR)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the stable Layer1 macro data pipeline only."
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use local Git-synced caches only. No remote data requests.",
    )
    parser.add_argument(
        "--fred-yahoo-mode",
        choices=("auto", "off", "full"),
        default="auto",
        help="FRED and yfinance update mode.",
    )
    parser.add_argument(
        "--cboe-mode",
        choices=("auto", "off", "force", "full"),
        default="auto",
        help="Cboe update mode. 'full' is accepted as an alias for 'force'.",
    )
    parser.add_argument(
        "--with-excel",
        action="store_true",
        help="Allow Cboe to create a local Excel file. Excel is ignored by Git.",
    )
    parser.add_argument(
        "--skip-data-quality",
        action="store_true",
        help="Skip data quality checks.",
    )
    parser.add_argument(
        "--skip-commit-check",
        action="store_true",
        help="Skip the Git data-file validation.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    fred_mode = "off" if args.offline else args.fred_yahoo_mode
    cboe_mode = "off" if args.offline else args.cboe_mode
    if cboe_mode == "full":
        cboe_mode = "force"

    run_command([sys.executable, "scripts/run_fred_yahoo.py", "--mode", fred_mode])

    cboe_command = [sys.executable, "scripts/run_cboe_pcr.py", "--mode", cboe_mode]
    if not args.with_excel:
        cboe_command.append("--no-excel")
    run_command(cboe_command)

    run_command([sys.executable, "scripts/run_build_combined.py"])

    if not args.skip_data_quality:
        run_command([sys.executable, "scripts/run_data_quality.py"])

    if not args.skip_commit_check:
        run_command([sys.executable, "scripts/check_data_commit_ready.py"])


if __name__ == "__main__":
    main()
