from __future__ import annotations

from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from src.layer1_macro.fred_yahoo import main  # noqa: E402


if __name__ == "__main__":
    main()
