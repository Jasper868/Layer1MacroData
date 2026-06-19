# 数据版本管理策略

本项目采用“代码 + 核心日频 CSV 数据”共同进入 GitHub 的策略。

## 提交到 GitHub 的内容

允许提交：

- `data/raw/*.csv`
- `data/meta/*.csv`
- `data/processed/*.csv`
- `src/`
- `scripts/`
- `docs/`
- `README.md`
- `requirements.txt`
- `.env.example`

这些 CSV 是小体量日频数据，提交到 GitHub 可以提高研究可复现性，并降低未来 yfinance、网页结构变化、远程数据源不可用造成的数据断裂风险。

## 不提交到 GitHub 的内容

禁止提交：

- `.env`
- `*.xlsx`
- `*.parquet`
- `data/output/`
- `data/archive/`
- `data/raw/cboe_html/`
- `data/raw/cboe_bulk_csv/`
- HTML 快照、Excel 报告、临时文件、日志文件

## 多电脑工作流

每台电脑只需要：

1. `git pull`
2. 创建本机 `.env`
3. `python -m pip install -r requirements.txt`
4. 运行：
   - `python scripts/check_environment.py`
   - `python scripts/run_cboe_pcr.py --mode off --no-excel`
   - `python scripts/run_build_combined.py`

默认情况下，数据目录是仓库内的 `./data/`，所以通过 GitHub 可以直接同步核心 CSV 数据。

## 提交数据前检查

每次提交数据前运行：

```powershell
python scripts/check_data_commit_ready.py
```

然后提交：

```powershell
git add data/raw/*.csv data/meta/*.csv data/processed/*.csv
git commit -m "Update daily macro market data"
git push
```
