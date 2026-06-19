from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.cboe_pcr import CboeRunConfig, update_cboe_pcr


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Cboe Put/Call Ratio post-2019 daily-page data.")
    parser.add_argument("--start-date", default="2019-10-07", help="Start date, default 2019-10-07.")
    parser.add_argument("--end-date", default="auto_safe", help="End date, default auto_safe.")
    parser.add_argument("--mode", default="auto", choices=["auto", "force", "off"], help="Remote update mode.")
    parser.add_argument("--max-dates", type=int, default=None, help="Maximum remote dates to process in this run.")
    parser.add_argument("--direction", default="oldest_first", choices=["oldest_first", "newest_first"], help="Run direction.")
    parser.add_argument("--html-snapshot", default="failed_only", choices=["none", "failed_only", "all"], help="HTML snapshot mode.")
    parser.add_argument("--no-excel", action="store_true", help="Do not export Excel report.")
    parser.add_argument("--sleep", type=float, default=0.8, help="Sleep seconds between requests.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = CboeRunConfig(
        start_date=args.start_date,
        end_date=args.end_date,
        remote_update_mode=args.mode,
        max_dates_per_run=args.max_dates,
        run_direction=args.direction,
        raw_html_snapshot_mode=args.html_snapshot,
        export_excel=not args.no_excel,
        request_sleep_seconds=args.sleep,
    )
    update_cboe_pcr(config)


if __name__ == "__main__":
    main()
