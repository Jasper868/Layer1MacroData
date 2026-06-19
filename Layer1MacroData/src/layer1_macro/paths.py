from __future__ import annotations

import os
from pathlib import Path


def _project_root_from_this_file() -> Path:
    # Expected location: <project>/src/layer1_macro/paths.py
    return Path(__file__).resolve().parents[2]


def _read_env_file(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _normalize_path(value: str | None, *, base_dir: Path) -> Path | None:
    if value is None:
        return None
    value = value.strip().strip('"').strip("'")
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


_PROJECT_ROOT_DEFAULT = _project_root_from_this_file()

for _env_path in (_PROJECT_ROOT_DEFAULT / ".env", Path.cwd() / ".env"):
    for _key, _value in _read_env_file(_env_path).items():
        os.environ.setdefault(_key, _value)

PROJECT_DIR = _normalize_path(
    os.getenv("LAYER1_PROJECT_DIR"),
    base_dir=_PROJECT_ROOT_DEFAULT,
) or _PROJECT_ROOT_DEFAULT

_DATA_DIR_ENV = os.getenv("LAYER1_DATA_DIR")
if _DATA_DIR_ENV:
    DATA_DIR = _normalize_path(_DATA_DIR_ENV, base_dir=PROJECT_DIR)  # type: ignore[assignment]
    DATA_SOURCE = "LAYER1_DATA_DIR"
else:
    DATA_DIR = (PROJECT_DIR / "data").resolve()
    DATA_SOURCE = "PROJECT_DIR/data default"

RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
META_DIR = DATA_DIR / "meta"

# Local-only folders. They are ignored by Git.
OUTPUT_DIR = DATA_DIR / "output"
ARCHIVE_DIR = DATA_DIR / "archive"
CBOE_HTML_DIR = RAW_DIR / "cboe_html"
CBOE_BULK_DIR = RAW_DIR / "cboe_bulk_csv"


def ensure_data_dirs() -> None:
    for path in (
        DATA_DIR,
        RAW_DIR,
        PROCESSED_DIR,
        META_DIR,
        OUTPUT_DIR,
        ARCHIVE_DIR,
        CBOE_HTML_DIR,
        CBOE_BULK_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def is_repo_data_dir() -> bool:
    try:
        return DATA_DIR.resolve() == (PROJECT_DIR / "data").resolve()
    except Exception:
        return False


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_DIR.resolve()))
    except Exception:
        return str(path)


def show_paths() -> None:
    print("[Data project paths]")
    print(f"PROJECT_DIR = {PROJECT_DIR}")
    print(f"DATA_DIR    = {DATA_DIR}")
    print(f"DATA_SOURCE = {DATA_SOURCE}")
    print(f"REPO_DATA   = {'YES' if is_repo_data_dir() else 'NO'}")
