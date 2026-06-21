from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.layer1_macro.paths import ARCHIVE_DIR, META_DIR, PROJECT_DIR, ensure_data_dirs
from src.layer1_macro.release_manifest import RELEASE_MANIFEST_PATH, sha256_file

STAGE_NAME = "STAGE14.5A"
SCHEMA_VERSION = 1
NO_ACTION = "NO_PORTFOLIO_ACTION"

POLICY_PATH = PROJECT_DIR / "configs" / "prospective_asof_release_policy.csv"
LEDGER_PATH = META_DIR / "prospective_asof_release_ledger.csv"
LOCAL_ARCHIVE_ROOT = ARCHIVE_DIR / "prospective_asof_releases"
LOCAL_OUTPUT_ROOT = PROJECT_DIR / "data" / "output" / "prospective_asof_release_ledger"

REQUIRED_RELEASE_FILES = (
    "processed/combined_macro_market.csv",
    "meta/research_availability_contract.csv",
    "meta/research_module_readiness.csv",
    "meta/source_metadata.csv",
    "meta/data_release_manifest.json",
)
REQUIRED_POLICY_COLUMNS = {
    "policy_id",
    "required_release_schema_version",
    "asof_date_rule",
    "base_git_commit_rule",
    "required_release_files",
    "allowed_outputs",
    "prohibited_activities",
    "operational_instruction",
    "notes",
}
REQUIRED_LEDGER_COLUMNS = {
    "asof_release_id",
    "asof_local_date",
    "data_git_head_before_receipt",
    "data_release_manifest_sha256",
    "combined_macro_market_sha256",
    "research_availability_contract_sha256",
    "research_module_readiness_sha256",
    "source_metadata_sha256",
    "overall_data_quality_status",
    "input_date_start",
    "input_date_end",
    "receipt_created_at_utc",
    "operational_instruction",
    "notes",
}


class ProspectiveAsOfReleaseError(RuntimeError):
    pass


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_DIR,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise ProspectiveAsOfReleaseError(
            f"Git command failed: git {' '.join(args)}\n"
            + (result.stderr.strip() or result.stdout.strip() or "unknown git error")
        )
    return result.stdout.strip()


def _clean_main_head() -> str:
    branch = _git(["branch", "--show-current"])
    if branch != "main":
        raise ProspectiveAsOfReleaseError(
            f"Run receipt creation only on main; current branch is {branch!r}."
        )
    pending = _git(["status", "--porcelain", "--untracked-files=all"])
    if pending:
        raise ProspectiveAsOfReleaseError(
            "Data worktree must be clean. Commit or discard existing Data changes before "
            f"creating a receipt.\nPending paths:\n{pending}"
        )
    head = _git(["rev-parse", "HEAD"])
    _git(["cat-file", "-e", f"{head}^{{commit}}"])
    return head


def _read_csv(path: Path, label: str, allow_empty: bool = False) -> pd.DataFrame:
    if not path.exists():
        raise ProspectiveAsOfReleaseError(f"Missing {label}: {path}")
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig", keep_default_na=False)
    except pd.errors.EmptyDataError as error:
        raise ProspectiveAsOfReleaseError(f"{label} must include a header row.") from error
    except Exception as error:
        raise ProspectiveAsOfReleaseError(f"Cannot read {label}: {error}") from error
    if frame.empty and not allow_empty:
        raise ProspectiveAsOfReleaseError(f"{label} cannot be empty.")
    return frame


def _require_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ProspectiveAsOfReleaseError(f"{label} missing columns: {missing}")


def _load_policy() -> dict[str, str]:
    frame = _read_csv(POLICY_PATH, "prospective as-of policy")
    _require_columns(frame, REQUIRED_POLICY_COLUMNS, "prospective as-of policy")
    if len(frame) != 1:
        raise ProspectiveAsOfReleaseError("Prospective as-of policy must contain exactly one row.")
    row = {str(k): str(v).strip() for k, v in frame.iloc[0].to_dict().items()}
    if any(not value for value in row.values()):
        raise ProspectiveAsOfReleaseError("Prospective as-of policy contains blank fields.")
    if row["policy_id"] != "PROSPECTIVE_ASOF_RELEASE_LEDGER":
        raise ProspectiveAsOfReleaseError("Unexpected prospective as-of policy ID.")
    if row["operational_instruction"] != NO_ACTION:
        raise ProspectiveAsOfReleaseError("Policy must retain NO_PORTFOLIO_ACTION.")
    if set(row["required_release_files"].split("|")) != set(REQUIRED_RELEASE_FILES):
        raise ProspectiveAsOfReleaseError("Policy required release-file set is invalid.")
    return row


def _load_ledger() -> pd.DataFrame:
    ledger = _read_csv(LEDGER_PATH, "prospective as-of ledger", allow_empty=True)
    _require_columns(ledger, REQUIRED_LEDGER_COLUMNS, "prospective as-of ledger")
    if ledger.empty:
        return ledger
    for column in REQUIRED_LEDGER_COLUMNS.difference({"notes"}):
        if ledger[column].astype(str).str.strip().eq("").any():
            raise ProspectiveAsOfReleaseError(f"Ledger contains blank {column} value(s).")
    if ledger["asof_release_id"].duplicated().any() or ledger["asof_local_date"].duplicated().any():
        raise ProspectiveAsOfReleaseError("Ledger release IDs and local dates must be unique.")
    if pd.to_datetime(ledger["asof_local_date"], errors="coerce").isna().any():
        raise ProspectiveAsOfReleaseError("Ledger asof_local_date values must be ISO dates.")
    if not ledger["operational_instruction"].astype(str).eq(NO_ACTION).all():
        raise ProspectiveAsOfReleaseError("Ledger must retain NO_PORTFOLIO_ACTION.")
    for commit in ledger["data_git_head_before_receipt"].astype(str):
        if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit.lower()):
            raise ProspectiveAsOfReleaseError("Ledger must use full 40-character Git commit SHA values.")
        _git(["cat-file", "-e", f"{commit}^{{commit}}"])
    return ledger.sort_values("asof_local_date").reset_index(drop=True)


def _read_manifest() -> dict[str, Any]:
    try:
        payload = json.loads(RELEASE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as error:
        raise ProspectiveAsOfReleaseError(f"Cannot parse data release manifest: {error}") from error
    if not isinstance(payload, dict):
        raise ProspectiveAsOfReleaseError("Data release manifest must be a JSON object.")
    try:
        schema = int(payload.get("manifest_schema_version", 0))
    except (TypeError, ValueError) as error:
        raise ProspectiveAsOfReleaseError("Invalid data release manifest schema.") from error
    if schema < 2:
        raise ProspectiveAsOfReleaseError("Data release manifest schema must be at least 2.")
    quality = str(payload.get("overall_data_quality_status", "")).strip().upper()
    if quality not in {"OK", "WARN"}:
        raise ProspectiveAsOfReleaseError(
            f"Data quality must be OK or WARN for a receipt, got {quality or 'MISSING'}."
        )
    return payload


def _manifest_entry(manifest: dict[str, Any], relative: str) -> dict[str, Any]:
    entries = manifest.get("files", [])
    matches = [item for item in entries if isinstance(item, dict) and item.get("relative_path") == relative]
    if len(matches) != 1:
        raise ProspectiveAsOfReleaseError(
            f"Manifest must contain exactly one record for {relative}; got {len(matches)}."
        )
    return matches[0]


def _release_file(relative: str) -> Path:
    return META_DIR.parent / relative


def _verify_release() -> dict[str, Any]:
    manifest = _read_manifest()
    files: dict[str, dict[str, Any]] = {}
    for relative in REQUIRED_RELEASE_FILES:
        path = _release_file(relative)
        if not path.exists():
            raise ProspectiveAsOfReleaseError(f"Missing release file: {relative}")
        actual = sha256_file(path)
        if relative != "meta/data_release_manifest.json":
            expected = str(_manifest_entry(manifest, relative).get("sha256", "")).strip()
            if not expected or actual != expected:
                raise ProspectiveAsOfReleaseError(
                    f"Manifest mismatch for {relative}."
                )
        files[relative] = {
            "path": path,
            "sha256": actual,
            "bytes": int(path.stat().st_size),
        }

    combined = pd.read_csv(_release_file("processed/combined_macro_market.csv"), encoding="utf-8-sig")
    if combined.empty or "date" not in combined.columns:
        raise ProspectiveAsOfReleaseError("combined_macro_market.csv is empty or lacks date.")
    dates = pd.to_datetime(combined["date"], errors="coerce")
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ProspectiveAsOfReleaseError("combined_macro_market.csv dates must be valid, unique, and ascending.")
    return {
        "manifest_sha256": sha256_file(RELEASE_MANIFEST_PATH),
        "quality": str(manifest["overall_data_quality_status"]).strip().upper(),
        "files": files,
        "input_date_start": dates.min().strftime("%Y-%m-%d"),
        "input_date_end": dates.max().strftime("%Y-%m-%d"),
    }


def validate_prospective_asof_release_contract() -> dict[str, Any]:
    return {
        "policy": _load_policy(),
        "ledger": _load_ledger(),
        "operational_instruction": NO_ACTION,
    }


def _write_csv_atomic(path: Path, frame: pd.DataFrame) -> None:
    temp = path.with_name(path.name + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig", lineterminator="\n")
    temp.replace(path)


def _write_local_archive(release_id: str, release: dict[str, Any], git_head: str) -> Path:
    target = LOCAL_ARCHIVE_ROOT / release_id
    if target.exists():
        raise ProspectiveAsOfReleaseError(f"Refusing to overwrite local archive: {target}")
    staged = target.with_name(target.name + ".tmp")
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True, exist_ok=False)
    try:
        entries: dict[str, Any] = {}
        for relative, item in release["files"].items():
            source = Path(item["path"])
            name = relative.replace("/", "__")
            destination = staged / name
            shutil.copy2(source, destination)
            entries[relative] = {
                "archive_relative_path": name,
                "sha256": sha256_file(destination),
                "bytes": int(destination.stat().st_size),
            }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE_NAME,
            "asof_release_id": release_id,
            "asof_local_date": date.today().isoformat(),
            "data_git_head_before_receipt": git_head,
            "data_release_manifest_sha256": release["manifest_sha256"],
            "files": entries,
            "purpose": "Local redundancy only; version-controlled ledger plus Git history is the cross-computer source of truth.",
            "operational_instruction": NO_ACTION,
        }
        archive_manifest = staged / "local_archive_manifest.json"
        archive_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        staged.replace(target)
        return target / "local_archive_manifest.json"
    except Exception:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        raise


def record_prospective_asof_release(*, notes: str = "", dry_run: bool = False) -> dict[str, str]:
    contract = validate_prospective_asof_release_contract()
    base_head = _clean_main_head()
    release = _verify_release()
    asof_local_date = date.today().isoformat()
    release_id = f"DATA_ASOF_{asof_local_date.replace('-', '')}"

    ledger = contract["ledger"]
    if not ledger.empty and (
        ledger["asof_release_id"].astype(str).eq(release_id).any()
        or ledger["asof_local_date"].astype(str).eq(asof_local_date).any()
    ):
        raise ProspectiveAsOfReleaseError(
            f"Receipt already exists for {asof_local_date}; receipts are append-only."
        )

    row = {
        "asof_release_id": release_id,
        "asof_local_date": asof_local_date,
        "data_git_head_before_receipt": base_head,
        "data_release_manifest_sha256": release["manifest_sha256"],
        "combined_macro_market_sha256": release["files"]["processed/combined_macro_market.csv"]["sha256"],
        "research_availability_contract_sha256": release["files"]["meta/research_availability_contract.csv"]["sha256"],
        "research_module_readiness_sha256": release["files"]["meta/research_module_readiness.csv"]["sha256"],
        "source_metadata_sha256": release["files"]["meta/source_metadata.csv"]["sha256"],
        "overall_data_quality_status": release["quality"],
        "input_date_start": release["input_date_start"],
        "input_date_end": release["input_date_end"],
        "receipt_created_at_utc": datetime.now(timezone.utc).isoformat(),
        "operational_instruction": NO_ACTION,
        "notes": notes.strip(),
    }
    if dry_run:
        return {
            "asof_release_id": release_id,
            "asof_local_date": asof_local_date,
            "data_git_head_before_receipt": base_head,
            "data_release_manifest_sha256": release["manifest_sha256"],
            "overall_data_quality_status": release["quality"],
            "input_date_end": release["input_date_end"],
            "mode": "DRY_RUN_NO_FILES_CHANGED",
            "operational_instruction": NO_ACTION,
        }

    archive_manifest = _write_local_archive(release_id, release, base_head)
    try:
        updated = pd.concat([ledger, pd.DataFrame([row])], ignore_index=True)
        _write_csv_atomic(LEDGER_PATH, updated)
        validate_prospective_asof_release_contract()
    except Exception:
        shutil.rmtree(archive_manifest.parent, ignore_errors=True)
        raise

    return {
        "asof_release_id": release_id,
        "asof_local_date": asof_local_date,
        "data_git_head_before_receipt": base_head,
        "ledger_path": str(LEDGER_PATH),
        "local_archive_manifest": str(archive_manifest),
        "mode": "RECEIPT_WRITTEN_COMMIT_LEDGER_NOW",
        "operational_instruction": NO_ACTION,
    }


def run_prospective_asof_release_inventory() -> dict[str, Path]:
    contract = validate_prospective_asof_release_contract()
    _clean_main_head()
    ensure_data_dirs()
    LOCAL_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    inventory = LOCAL_OUTPUT_ROOT / "prospective_asof_release_ledger_inventory.csv"
    summary = LOCAL_OUTPUT_ROOT / "prospective_asof_release_ledger_summary.md"
    contract["ledger"].to_csv(inventory, index=False, encoding="utf-8-sig", lineterminator="\n")
    summary.write_text(
        "# Stage14.5A Prospective As-Of Release Ledger\n\n"
        f"- Version-controlled receipts: {len(contract['ledger'])}\n"
        "- Each receipt binds a computer-local date to an already committed Data Git HEAD and handoff hashes.\n"
        "- Local archive folders are redundancy only; Git history plus the tracked receipt ledger is cross-computer evidence.\n"
        f"- Operational instruction: `{NO_ACTION}`\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"inventory_csv": inventory, "summary_markdown": summary}


def main_check() -> int:
    try:
        contract = validate_prospective_asof_release_contract()
    except ProspectiveAsOfReleaseError as error:
        print(f"[FAIL] {error}")
        return 1
    print("[PASS] Stage14.5A prospective as-of release-ledger contract is valid.")
    print(f"[Info] Version-controlled receipt rows: {len(contract['ledger'])}")
    print("[PASS] No feature, score, validation, backtest, or portfolio action is authorized.")
    print(f"[PASS] Operational instruction: {NO_ACTION}")
    return 0


def main_record() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = record_prospective_asof_release(notes=args.notes, dry_run=bool(args.dry_run))
    except ProspectiveAsOfReleaseError as error:
        print(f"[FAIL] {error}")
        return 1
    for key, value in result.items():
        print(f"[INFO] {key}: {value}")
    print(f"[PASS] Operational instruction: {NO_ACTION}")
    return 0


def main_inventory() -> int:
    try:
        result = run_prospective_asof_release_inventory()
    except ProspectiveAsOfReleaseError as error:
        print(f"[FAIL] {error}")
        return 1
    print(f"[PASS] Wrote Stage14.5A receipt inventory: {result['inventory_csv']}")
    print(f"[PASS] Wrote Stage14.5A receipt summary: {result['summary_markdown']}")
    print(f"[PASS] Operational instruction: {NO_ACTION}")
    return 0
