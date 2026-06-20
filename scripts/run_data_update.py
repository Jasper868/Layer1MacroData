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
        help=(
            "Rebuild only from existing caches. It preserves acquisition-status "
            "and Cboe latest-run evidence; no remote request is made."
        ),
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


def run_post_merge_checks(args: argparse.Namespace) -> None:
    if args.skip_data_quality:
        print(
            "[INFO] Data quality checks were skipped; this run cannot produce a "
            "READY_TO_PUBLISH conclusion."
        )
        return

    run_command([sys.executable, "scripts/run_data_quality.py"])
    run_command([sys.executable, "scripts/run_build_research_availability_contract.py"])
    run_command([sys.executable, "scripts/check_research_availability_contract.py"])
    run_command([sys.executable, "scripts/run_build_release_manifest.py"])
    run_command([sys.executable, "scripts/check_data_release_ready.py"])

    if not args.skip_commit_check:
        run_command([sys.executable, "scripts/check_data_commit_ready.py"])


def main() -> None:
    args = parse_args()

    if args.offline:
        # Offline rebuilding must not call the source updaters with mode=off.
        # Those updaters legitimately rewrite status/latest-run files, which
        # would replace evidence of the last online acquisition with an
        # unrelated local rebuild record.
        print(
            "[INFO] Offline rebuild: preserving prior acquisition status and "
            "Cboe latest-run evidence; no source updater will run."
        )
        run_command([sys.executable, "scripts/run_build_combined.py"])
        run_post_merge_checks(args)
        return

    cboe_mode = "force" if args.cboe_mode == "full" else args.cboe_mode

    run_command(
        [sys.executable, "scripts/run_fred_yahoo.py", "--mode", args.fred_yahoo_mode]
    )

    cboe_command = [sys.executable, "scripts/run_cboe_pcr.py", "--mode", cboe_mode]
    if not args.with_excel:
        cboe_command.append("--no-excel")
    run_command(cboe_command)

    run_command([sys.executable, "scripts/run_build_combined.py"])
    run_post_merge_checks(args)


if __name__ == "__main__":
    main()
