# Stage 12: FRED + yfinance 模块化取数层

本阶段把 FRED 与 yfinance 的生产取数逻辑从 notebook 中抽出，形成正式 Python 模块：

```powershell
python scripts/run_fred_yahoo.py
```

## 生产入口

日常完整流程：

```powershell
python scripts/run_all.py
```

离线或公司网络受限时：

```powershell
python scripts/run_all.py --offline
```

手动分步运行：

```powershell
python scripts/run_fred_yahoo.py
python scripts/run_cboe_pcr.py --no-excel
python scripts/run_build_combined.py
```

## FRED/yfinance 运行模式

```powershell
python scripts/run_fred_yahoo.py --mode auto
python scripts/run_fred_yahoo.py --mode off
python scripts/run_fred_yahoo.py --mode full --start-date 2006-06-13
```

| mode | 含义 |
|---|---|
| `auto` | 默认模式。读取现有缓存，并从最新日期向前回看若干天增量更新。 |
| `off` | 不远程取数，只读取现有缓存并刷新状态、缺失值报告和指标字典。 |
| `full` | 从 `--start-date` 开始全量刷新。谨慎使用。 |

## 安全原则

远程请求失败或返回空数据时，脚本不会覆盖已有缓存，只会在 `data/meta/data_status.csv` 中记录状态。

核心缓存文件位于仓库内：

```text
data/raw/fred_cache.csv
data/raw/yahoo_cache.csv
data/meta/data_status.csv
data/meta/missing_value_report.csv
data/meta/indicator_dictionary.csv
```

这些 CSV 是小体量日频数据，可以进入 GitHub 版本管理。

## 建议工作流

1. `git pull origin main`
2. `python scripts/run_all.py`
3. `git status`
4. 确认只出现 CSV / 文档 / 代码变更
5. `git add -A && git commit -m "Update daily macro data" && git push`

如果网络受限，用：

```powershell
python scripts/run_all.py --offline
```
