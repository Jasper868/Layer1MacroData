# Stage14.4：采集尾部、历史研究与前瞻状态三层合同

## 目标

本阶段不新增数据源、不增加指标、不创建交易信号或组合动作。它只解决两个工程与研究治理问题：

1. `远程更新成功` 不再等同于 `尾部日期已推进`；
2. 历史研究可用不再等同于今天可以用于风险状态计算。

项目仍以运行电脑的本地日期 `date.today()` 作为 `as_of_local_date`，不引入时区切换逻辑。

## 1. 数据采集状态：请求、返回、尾部推进分离

`data/meta/data_status.csv` 新增字段：

| 字段 | 含义 |
|---|---|
| `目标结束日期` | 本次更新所请求的结束日期 |
| `远程响应状态` | `REMOTE_OK` / `REMOTE_EMPTY` / `REMOTE_ERROR` / `REMOTE_SKIPPED` |
| `远程最新有效日期` | 本次远程响应中该指标的最新有效观测日 |
| `尾部推进状态` | 是否真正将本地缓存的最新有效日向前推进 |
| `尾部是否推进` | `是` / `否` |
| `本地更新后距目标日期自然日数` | 仅描述自然日差；不自动代表异常或市场休市错误 |

关键解释：

- `REMOTE_OK_TAIL_ADVANCED`：远端返回有效数据，且本地最新观测日期向前推进；
- `REMOTE_OK_TAIL_UNCHANGED`：远端返回有效数据，但本地已有相同或更晚尾部；
- `REMOTE_EMPTY_USED_CACHE`：远端无有效值，旧缓存被保留；
- `REMOTE_ERROR_USED_CACHE`：远端请求或解析失败，旧缓存被保留；
- `REMOTE_SKIPPED_CACHE_ONLY`：本轮未请求远端。

因此，`远程更新成功` 只说明通信和解析有值；是否真正新增尾部由 `尾部推进状态` 判断。

## 2. 研究可用性：三层状态，不再混为一谈

`data/meta/research_availability_contract.csv` 升级为 schema version 2。

### 数据有效性层

`data_validity_status`：

- `DATA_VALID`：质量报告为 `OK`；
- `DATA_DEGRADED`：质量报告为 `WARN`；
- `DATA_INVALID`：质量报告为 `FAIL`、未知或无最新观测。

它回答：数据本身是否结构正常、范围合理、覆盖率可接受。

### 历史研究层

`historical_research_status`：

- `HISTORICAL_RESEARCH_USABLE`；
- `HISTORICAL_RESEARCH_DEGRADED`；
- `HISTORICAL_RESEARCH_UNUSABLE`。

它回答：在严格执行 `research_date >= observation_date + availability_lag` 后，数据是否可进入历史研究。

它不代表今天可以用该数据做风险状态判断。

### 前瞻状态层

`prospective_state_status`：

- `PROSPECTIVE_STATE_READY`；
- `PROSPECTIVE_STATE_PENDING`；
- `PROSPECTIVE_STATE_DEGRADED`；
- `PROSPECTIVE_STATE_STALE`；
- `PROSPECTIVE_STATE_BLOCKED`。

其判断顺序是：

```text
无有效数据 / 质量不可用             -> BLOCKED
尚未通过声明的可得性滞后            -> PENDING
观测年龄超过来源的新鲜度预算        -> STALE
质量 WARN 但仍在新鲜度预算内         -> DEGRADED
其余                               -> READY
```

其中：

```text
latest_observation_age_calendar_days
    = as_of_local_date - latest_valid_observation_date

maximum_expected_tail_delay_calendar_days
    = source_metadata.csv 中来源级新鲜度预算
```

自然日差仅用于项目的本地日期治理；它不假设交易日历，也不把周末或节假日自动解释为远端错误。

为避免“质量报告允许 10 天、前瞻合同只允许 7 天”的配置漂移，本阶段将广义美元指数、USD_CNY、EUR_USD、JPY_USD 的来源元数据新鲜度预算显式设为 10 个自然日，与既有 Stage13 FX 质量政策对齐。

原有的 `current_research_readiness` 与 `module_research_readiness` 被保留为兼容字段：

```text
READY     <- PROSPECTIVE_STATE_READY
PENDING   <- PROSPECTIVE_STATE_PENDING
DEGRADED  <- PROSPECTIVE_STATE_DEGRADED / STALE
BLOCKED   <- PROSPECTIVE_STATE_BLOCKED
```

Research 仓库在未来迁移完成前可以继续读取兼容字段；新代码应优先读取 `prospective_state_status`。

## 3. Cboe 运行审计

新增：

```text
data/meta/cboe_pcr_last_run_audit.json
data/meta/cboe_pcr_last_successful_acquisition.json
```

用途：

- `last_run_audit`：每一次 Cboe 更新都记录，包括“自动模式运行但无需补取日期”；
- `last_successful_acquisition`：仅在本轮有成功远程采集时更新；若无新成功记录，不伪造原始采集时间。

`cboe_pcr_status_latest_run.csv` 在没有待更新日期时不再是空文件，而会写入 `run_summary` 行。因此可以区分：

```text
本轮运行且无需更新
本轮明确关闭远程请求
本轮有远程处理记录
```

## 4. 非目标

本阶段明确不做：

- 不把 `BAA10Y` 拼接为高收益债 OAS；
- 不降低 VIX 期限结构的质量门槛；
- 不对缺失或过期数据补值；
- 不改变 Stage16 的 `OBSERVABILITY_ONLY` 定位；
- 不创建交易阈值、减仓、加仓、止损或抄底动作。

## 5. 验收

完成一次完整在线更新后，依次检查：

```powershell
python scripts/check_research_availability_contract.py
python scripts/check_data_release_ready.py
```

预期：

- 合同检查为 `RESULT: PASS`；
- 发布检查为 `RESULT: READY_TO_PUBLISH`；
- 如果 VIX9D / VIX3M / VIX6M 仍落后于来源新鲜度预算，模块应显示：
  - `module_prospective_state_status = PROSPECTIVE_STATE_STALE`
  - `module_research_readiness = DEGRADED`

这代表系统诚实地报告“该模块今天不可作为新鲜输入”，而不是将缺失/过期误写为“正常”。
