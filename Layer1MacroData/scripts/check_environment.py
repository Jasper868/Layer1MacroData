from __future__ import annotations

from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.paths import (  # noqa: E402
    ARCHIVE_DIR,
    DATA_DIR,
    META_DIR,
    OUTPUT_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    ensure_data_dirs,
    is_repo_data_dir,
    show_paths,
)


CORE_FILES = (
    RAW_DIR / "fred_cache.csv",
    RAW_DIR / "yahoo_cache.csv",
    RAW_DIR / "cboe_pcr_cache.csv",
    RAW_DIR / "cboe_pcr_volume_cache.csv",
    PROCESSED_DIR / "combined_macro_market.csv",
    PROCESSED_DIR / "latest_macro_snapshot.csv",
)


def main() -> None:
    ensure_data_dirs()
    show_paths()

    print("\n[Directory check]")
    for path in (DATA_DIR, RAW_DIR, PROCESSED_DIR, META_DIR, OUTPUT_DIR, ARCHIVE_DIR):
        state = "OK" if path.exists() else "MISSING"
        print(f"{state:7} {path}")

    print("\n[Core data files]")
    for path in CORE_FILES:
        if path.exists():
            print(f"FOUND   {path} ({path.stat().st_size / 1024:.1f} KiB)")
        else:
            print(f"MISSING {path}")

    print("\n[Git policy]")
    if is_repo_data_dir():
        print("OK - Core data is stored inside this repository and can be synced with GitHub.")
    else:
        print("WARN - DATA_DIR is outside this repository. Remove LAYER1_DATA_DIR from .env for the normal workflow.")


if __name__ == "__main__":
    main()
