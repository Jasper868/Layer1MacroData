from __future__ import annotations

from pathlib import Path
import subprocess
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.paths import (  # noqa: E402
    DATA_DIR,
    META_DIR,
    PROCESSED_DIR,
    RAW_DIR,
    is_repo_data_dir,
)


REQUIRED_FILES = (
    RAW_DIR / "fred_cache.csv",
    RAW_DIR / "yahoo_cache.csv",
    RAW_DIR / "cboe_pcr_cache.csv",
    RAW_DIR / "cboe_pcr_volume_cache.csv",
    META_DIR / "cboe_pcr_validation_cache.csv",
    PROCESSED_DIR / "combined_macro_market.csv",
    PROCESSED_DIR / "latest_macro_snapshot.csv",
    PROCESSED_DIR / "cboe_pcr_latest_snapshot.csv",
    META_DIR / "indicator_dictionary.csv",
    META_DIR / "cboe_pcr_dictionary.csv",
    META_DIR / "data_status.csv",
    META_DIR / "data_quality_summary.csv",
    META_DIR / "data_quality_indicator_report.csv",
    META_DIR / "data_quality_alerts.csv",
    META_DIR / "research_availability_contract.csv",
    META_DIR / "research_module_readiness.csv",
    META_DIR / "combined_missing_value_report.csv",
    META_DIR / "source_metadata.csv",
    META_DIR / "data_release_manifest.json",
)

FORBIDDEN_PREFIXES = (
    "data/features/",
    "data/scores/",
    "data/research/",
)

FORBIDDEN_NAMES = (
    "triangle_assets_daily.csv",
    "feature_engineering.py",
    "resonance_scoring.py",
    "resonance_calibration.py",
    "resonance_multistage.py",
    "triangle_assets.py",
)


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_DIR,
        text=True,
        capture_output=True,
        check=False,
    )


def is_forbidden_tracked_secret(path: str) -> bool:
    normalized = path.replace("\\", "/").strip()
    name = Path(normalized).name
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def main() -> None:
    print("[Data repository commit check]")
    print(f"DATA_DIR = {DATA_DIR}")

    errors: list[str] = []

    if not is_repo_data_dir():
        errors.append("DATA_DIR is not the repository data directory.")

    print("\n[Required files]")
    for path in REQUIRED_FILES:
        if path.exists():
            print(f"OK      {path.relative_to(PROJECT_DIR)}")
        else:
            print(f"MISSING {path.relative_to(PROJECT_DIR)}")
            errors.append(f"Required file is missing: {path.relative_to(PROJECT_DIR)}")

    tracked = run_git(["ls-files"])
    if tracked.returncode != 0:
        errors.append("Unable to read Git tracked files.")
        tracked_files: list[str] = []
    else:
        tracked_files = [line.strip().replace("\\", "/") for line in tracked.stdout.splitlines() if line.strip()]

    print("\n[Research content exclusion]")
    for name in tracked_files:
        if name.startswith(FORBIDDEN_PREFIXES) or Path(name).name in FORBIDDEN_NAMES:
            errors.append(f"Research-only content is still tracked: {name}")

    print("\n[Secret tracking exclusion]")
    for name in tracked_files:
        if is_forbidden_tracked_secret(name):
            errors.append(
                f"Local secret/config file is still tracked: {name}. "
                "Rotate any value it contained, then remove it with `git rm --cached .env`."
            )

    if errors:
        print("\nRESULT: FAIL")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    print("\nRESULT: PASS")
    print("Only stable raw, processed, and data-quality artifacts are required by this project.")


if __name__ == "__main__":
    main()
