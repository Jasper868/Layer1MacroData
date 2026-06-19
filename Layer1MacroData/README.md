# Layer1MacroData

This repository is the stable data layer for the Layer1 macro investment project.

It has one responsibility:

```text
Fetch -> cache -> validate -> merge -> publish Git-synced macro-market data
```

It does **not** contain indicator research, Stage15/Stage16 features, risk scores,
historical event studies, portfolio weights, or trading rules.

## Git policy

The following folders are intentionally committed to GitHub:

```text
data/raw/        source caches from FRED, yfinance, and Cboe
data/processed/  merged research input datasets
data/meta/       dictionaries, update status, validation, and quality reports
```

The following content is intentionally excluded:

```text
data/features/
data/scores/
data/research/
data/output/
data/archive/
```

Research outputs belong in the separate `Layer1MacroResearch` repository and stay
local by default.

## Main output contract

The formal handoff to the research project is:

```text
data/processed/combined_macro_market.csv
```

Supporting data outputs include:

```text
data/processed/latest_macro_snapshot.csv
data/processed/cboe_pcr_latest_snapshot.csv
data/meta/data_quality_summary.csv
data/meta/data_status.csv
```

## First-time setup on a computer

```powershell
git clone https://github.com/Jasper868/Layer1MacroData.git
cd Layer1MacroData
copy .env.example .env
python -m pip install -r requirements.txt
python scripts/check_environment.py
```

Only a computer that retrieves FRED data needs a local `FRED_API_KEY` in `.env`.
The `.env` file must never be committed or uploaded.

## Daily update on an internet-connected computer

```powershell
git pull
python scripts/run_data_update.py
git add data
git commit -m "Update macro data"
git push
```

## Use on a restricted/offline computer

```powershell
git pull
python scripts/check_environment.py
```

Usually no data update is necessary. The GitHub-synced `data/raw`, `data/processed`,
and `data/meta` folders already contain the latest published data.

## Offline rebuild

Use this only when you need to rebuild `combined_macro_market.csv` from existing
Git-synced caches:

```powershell
python scripts/run_data_update.py --offline
```

## Repository boundary

This repository must not reintroduce:

```text
Stage15 feature engineering
Stage16 scoring or calibration
triangle portfolio construction
investment weights or trading rules
```

Those belong to `Layer1MacroResearch`.
