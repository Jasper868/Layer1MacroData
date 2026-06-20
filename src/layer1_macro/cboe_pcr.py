from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    Holiday,
    GoodFriday,
    USMartinLutherKingJr,
    USPresidentsDay,
    USMemorialDay,
    USLaborDay,
    USThanksgivingDay,
    nearest_workday,
)
from pandas.tseries.offsets import CustomBusinessDay

from src.layer1_macro.io_utils import safe_write_csv
from src.layer1_macro.paths import (
    ensure_data_dirs,
    RAW_DIR,
    META_DIR,
    PROCESSED_DIR,
    OUTPUT_DIR,
    CBOE_HTML_DIR,
)


# =============================================================================
# Cboe PCR post-2019 daily-page module
# =============================================================================

CBOE_EFFECTIVE_START_DATE = "2019-10-07"
CBOE_DAILY_URL = "https://www.cboe.com/markets/us/options/market-statistics/daily/"

PCR_ABS_TOLERANCE = 0.02
PCR_REASONABLE_MIN = 0.0
PCR_REASONABLE_MAX = 10.0

DEFAULT_REQUEST_MAX_RETRIES = 3
DEFAULT_REQUEST_SLEEP_SECONDS = 0.8
DEFAULT_SAVE_PROGRESS_EVERY_N_DATES = 20




def _nyse_new_years_observance(dt):
    """NYSE New Year's Day observance.

    Important exception: when Jan 1 falls on Saturday, NYSE generally does not
    observe it on the prior Friday because that would fall in the previous
    calendar year. For example, 2021-12-31 was an open trading day.

    If Jan 1 falls on Sunday, the market observes it on Monday Jan 2.
    """
    ts = pd.Timestamp(dt)
    if ts.weekday() == 6:  # Sunday -> observed on Monday
        return ts + pd.Timedelta(days=1)
    return ts

class NYSEHolidayCalendarFallback(AbstractHolidayCalendar):
    """
    NYSE-like holiday calendar used only when pandas_market_calendars is unavailable.

    This fallback is designed to avoid the most common failure mode in the Cboe updater:
    pandas BDay treats US exchange holidays as business days, which causes repeated
    requests for dates such as Thanksgiving, Christmas, New Year's Day, MLK Day,
    Presidents' Day, Good Friday, Memorial Day, Juneteenth, Independence Day,
    and Labor Day.

    For the current post-2019 Cboe research window, this fallback matches the
    practical trading-date count used by the existing validated cache.
    """
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=_nyse_new_years_observance),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday, start_date="2022-01-01"),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


CBOE_PCR_META = {
    "TOTAL PUT/CALL RATIO": {
        "指标名称": "Total PCR",
        "pcr_col": "CBOE_Total_PCR",
        "volume_section": "SUM OF ALL PRODUCTS",
        "模块": "期权情绪",
        "是否核心字段": "是",
        "是否建议纳入": "观察",
        "说明": "全市场Put/Call总比例，混合指数、ETF、个股等，只适合粗看",
    },
    "INDEX PUT/CALL RATIO": {
        "指标名称": "Index PCR",
        "pcr_col": "CBOE_Index_PCR",
        "volume_section": "INDEX OPTIONS",
        "模块": "机构对冲",
        "是否核心字段": "是",
        "是否建议纳入": "建议研究",
        "说明": "指数期权Put/Call比例，更偏机构对冲需求",
    },
    "EXCHANGE TRADED PRODUCTS PUT/CALL RATIO": {
        "指标名称": "ETP PCR",
        "pcr_col": "CBOE_ETP_PCR",
        "volume_section": "EXCHANGE TRADED PRODUCTS",
        "模块": "ETF/ETP期权情绪",
        "是否核心字段": "是",
        "是否建议纳入": "建议研究",
        "说明": "交易所交易产品期权Put/Call比例，更贴近SPY、QQQ、IWM等ETF期权交易",
    },
    "EQUITY PUT/CALL RATIO": {
        "指标名称": "Equity PCR",
        "pcr_col": "CBOE_Equity_PCR",
        "volume_section": "EQUITY OPTIONS",
        "模块": "个股投机情绪",
        "是否核心字段": "是",
        "是否建议纳入": "观察",
        "说明": "个股期权Put/Call比例，更偏散户或投机情绪",
    },
    "CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO": {
        "指标名称": "VIX PCR",
        "pcr_col": "CBOE_VIX_PCR",
        "volume_section": "CBOE VOLATILITY INDEX (VIX)",
        "模块": "波动率押注",
        "是否核心字段": "是",
        "是否建议纳入": "高级观察",
        "说明": "VIX期权Put/Call比例，用于观察市场对波动率本身的押注，解释难度较高",
    },
    "SPX + SPXW PUT/CALL RATIO": {
        "指标名称": "SPX PCR",
        "pcr_col": "CBOE_SPX_PCR",
        "volume_section": "SPX + SPXW",
        "模块": "标普500对冲情绪",
        "是否核心字段": "是",
        "是否建议纳入": "建议研究",
        "说明": "当前daily页面为SPX+SPXW；本模块只使用2019-10-07之后daily页面同口径数据",
    },
}

PCR_COLS = [meta["pcr_col"] for meta in CBOE_PCR_META.values()]
REQUIRED_PCR_COLS = PCR_COLS.copy()
REQUIRED_PCR_RAW_LABELS = list(CBOE_PCR_META.keys())

VOLUME_COLS: list[str] = []
for _raw_label, _meta in CBOE_PCR_META.items():
    _base = _meta["pcr_col"].replace("_PCR", "")
    VOLUME_COLS.extend([
        f"{_base}_Call_Volume",
        f"{_base}_Put_Volume",
        f"{_base}_Total_Volume",
        f"{_base}_PCR_Calculated",
        f"{_base}_PCR_Diff",
    ])

VALIDATION_COLS = [
    "date", "数据来源类型", "源文件或URL", "source_regime",
    "网页原始名称", "指标名称", "PCR列名", "成交量分区",
    "网页PCR", "Call成交量", "Put成交量", "Total成交量",
    "成交量反算PCR", "PCR差异",
    "Total校验", "PCR交叉验证", "范围校验", "最终校验", "备注",
]

STATUS_COLS = [
    "date", "状态", "数据来源类型", "请求URL", "最终URL", "解析方法",
    "PCR目标数量", "PCR核心目标数量", "PCR解析数量", "PCR核心解析数量",
    "成交量分区解析数量", "验证通过数量", "验证部分通过数量", "验证失败数量",
    "HTML快照", "备注", "运行时间",
]

# File paths
CBOE_PCR_CACHE_PATH = RAW_DIR / "cboe_pcr_cache.csv"
CBOE_VOLUME_CACHE_PATH = RAW_DIR / "cboe_pcr_volume_cache.csv"
CBOE_VALIDATION_CACHE_PATH = META_DIR / "cboe_pcr_validation_cache.csv"
CBOE_STATUS_LATEST_RUN_PATH = META_DIR / "cboe_pcr_status_latest_run.csv"
CBOE_VALIDATION_LATEST_RUN_PATH = META_DIR / "cboe_pcr_validation_latest_run.csv"
CBOE_MISSING_REPORT_PATH = META_DIR / "cboe_pcr_missing_value_report.csv"
CBOE_DICTIONARY_PATH = META_DIR / "cboe_pcr_dictionary.csv"
CBOE_LATEST_SNAPSHOT_PATH = PROCESSED_DIR / "cboe_pcr_latest_snapshot.csv"
CBOE_YEARLY_COVERAGE_PATH = META_DIR / "cboe_pcr_yearly_coverage.csv"
CBOE_VALIDATION_SUMMARY_PATH = META_DIR / "cboe_pcr_validation_summary.csv"
CBOE_FILE_SIZE_PATH = META_DIR / "cboe_pcr_file_size_report.csv"


@dataclass
class CboeRunConfig:
    start_date: str = CBOE_EFFECTIVE_START_DATE
    end_date: str = "auto_safe"
    remote_update_mode: str = "auto"  # auto / force / off
    process_missing_only: bool = True
    max_dates_per_run: int | None = None
    run_direction: str = "oldest_first"  # oldest_first / newest_first
    raw_html_snapshot_mode: str = "failed_only"  # none / failed_only / all
    max_failed_html_snapshots_per_run: int = 20
    request_max_retries: int = DEFAULT_REQUEST_MAX_RETRIES
    request_sleep_seconds: float = DEFAULT_REQUEST_SLEEP_SECONDS
    save_progress_every_n_dates: int = DEFAULT_SAVE_PROGRESS_EVERY_N_DATES
    use_nyse_calendar: bool = True
    export_excel: bool = True


class CboePcrUpdater:
    def __init__(self, config: CboeRunConfig | None = None) -> None:
        self.config = config or CboeRunConfig()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml,text/csv,*/*;q=0.8",
        })

        self.status_rows: list[dict] = []
        self.latest_validation_rows: list[dict] = []
        self.failed_html_saved_count = 0
        self.remote_processed_count = 0

        self.pcr_cache = pd.DataFrame(columns=PCR_COLS)
        self.volume_cache = pd.DataFrame(columns=VOLUME_COLS)
        self.validation_cache = pd.DataFrame(columns=VALIDATION_COLS)

    # ------------------------------------------------------------------
    # Basic helpers
    # ------------------------------------------------------------------
    @staticmethod
    def fmt_date(x) -> str:
        if x is None or pd.isna(x):
            return ""
        return pd.Timestamp(x).strftime("%Y-%m-%d")

    @staticmethod
    def safe_file_size_mb(path: Path) -> float:
        path = Path(path)
        return round(path.stat().st_size / 1024 / 1024, 4) if path.exists() else 0.0

    @staticmethod
    def safe_display_path(path: Path) -> str:
        path = Path(path)
        try:
            resolved = path.resolve()
        except Exception:
            return str(path)
        for base in [RAW_DIR, META_DIR, PROCESSED_DIR, OUTPUT_DIR, RAW_DIR.parent]:
            try:
                return str(resolved.relative_to(Path(base).resolve()))
            except Exception:
                continue
        return str(resolved)

    @staticmethod
    def normalize_text(x: str) -> str:
        if x is None:
            return ""
        try:
            if pd.isna(x):
                return ""
        except Exception:
            pass
        x = html.unescape(str(x))
        x = x.replace("\ufeff", "")
        x = re.sub(r"\s+", " ", x).strip().upper()
        return x

    @staticmethod
    def parse_number(x):
        if x is None:
            return pd.NA
        try:
            if pd.isna(x):
                return pd.NA
        except Exception:
            pass
        x = str(x).replace(",", "").replace("%", "").strip()
        x = re.sub(r"[^0-9.\-+]", "", x)
        if x in ["", ".", "-", "+"]:
            return pd.NA
        value = pd.to_numeric(x, errors="coerce")
        if pd.isna(value):
            return pd.NA
        return float(value)

    def extract_number_tokens(self, line: str) -> list[float]:
        if line is None:
            return []
        tokens = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", str(line))
        values = []
        for token in tokens:
            value = self.parse_number(token)
            if pd.notna(value):
                values.append(float(value))
        return values

    def build_cboe_url(self, date: pd.Timestamp) -> str:
        return f"{CBOE_DAILY_URL}?dt={self.fmt_date(date)}"

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------
    def get_safe_cboe_end_date(self) -> pd.Timestamp:
        """
        New York time based safe end date. Before 21:30 New York time, use previous
        business day to avoid unpublished or unsettled same-day Cboe page.
        """
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        today_ny = pd.Timestamp(now_ny.date()).normalize()

        if today_ny.weekday() >= 5:
            return today_ny - pd.offsets.BDay(1)

        if (now_ny.hour, now_ny.minute) < (21, 30):
            return today_ny - pd.offsets.BDay(1)

        return today_ny

    def resolve_end_date(self) -> pd.Timestamp:
        if isinstance(self.config.end_date, str) and self.config.end_date.lower() == "auto_safe":
            return self.get_safe_cboe_end_date()
        return pd.Timestamp(self.config.end_date).normalize()

    def resolve_start_date(self) -> pd.Timestamp:
        start = pd.Timestamp(self.config.start_date).normalize()
        effective = pd.Timestamp(CBOE_EFFECTIVE_START_DATE).normalize()
        return max(start, effective)

    def make_trading_date_list(self, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
        start = pd.Timestamp(start).normalize()
        end = pd.Timestamp(end).normalize()
        if end < start:
            return []

        if self.config.use_nyse_calendar:
            try:
                import pandas_market_calendars as mcal

                nyse = mcal.get_calendar("NYSE")
                schedule = nyse.schedule(start_date=self.fmt_date(start), end_date=self.fmt_date(end))
                dates = [pd.Timestamp(d).normalize() for d in schedule.index]
                print(f"[日期] 使用 NYSE 交易日历生成日期：{len(dates)} 个交易日")
                return dates
            except Exception as exc:
                print(f"[提醒] NYSE 交易日历不可用，退回 pandas BDay：{exc}")

        nyse_bday = CustomBusinessDay(calendar=NYSEHolidayCalendarFallback())
        dates = [pd.Timestamp(d).normalize() for d in pd.date_range(start, end, freq=nyse_bday)]
        print(
            f"[日期] pandas_market_calendars 不可用，"
            f"使用内置 NYSE 假日日历生成日期：{len(dates)} 个交易日"
        )
        return dates

    # ------------------------------------------------------------------
    # Cache IO
    # ------------------------------------------------------------------
    @staticmethod
    def _read_date_index_csv(path: Path, expected_cols: list[str]) -> pd.DataFrame:
        path = Path(path)
        if not path.exists():
            out = pd.DataFrame(columns=expected_cols)
            out.index = pd.DatetimeIndex([], name="date")
            return out

        df = pd.read_csv(path, encoding="utf-8-sig")
        if df.empty:
            out = pd.DataFrame(columns=expected_cols)
            out.index = pd.DatetimeIndex([], name="date")
            return out

        date_col = None
        for candidate in ["date", "Date", "DATE", "Unnamed: 0"]:
            if candidate in df.columns:
                date_col = candidate
                break
        if date_col is None:
            raise ValueError(f"{path} 未找到日期列。当前字段：{list(df.columns)}")

        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col])
        df = df.rename(columns={date_col: "date"})
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        df = df.set_index("date")
        df.index.name = "date"
        df = df.reindex(columns=expected_cols)
        return df

    @staticmethod
    def _save_date_index_csv(df: pd.DataFrame, path: Path, expected_cols: list[str]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        out = df.copy()
        if out.empty:
            out = pd.DataFrame(columns=expected_cols)
            out.index.name = "date"
        else:
            out.index = pd.to_datetime(out.index).normalize()
            out.index.name = "date"
            out = out.sort_index().reindex(columns=expected_cols)
        safe_write_csv(out, path, index=True, announce=False)

    @staticmethod
    def _read_validation_cache(path: Path) -> pd.DataFrame:
        path = Path(path)
        if path.exists():
            df = pd.read_csv(path, encoding="utf-8-sig")
        else:
            df = pd.DataFrame(columns=VALIDATION_COLS)
        df = df.reindex(columns=VALIDATION_COLS)
        if "date" in df.columns:
            df["date"] = df["date"].astype(str)
        return df

    @staticmethod
    def _save_validation_cache(df: pd.DataFrame, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        df = df.reindex(columns=VALIDATION_COLS)
        safe_write_csv(df, path, announce=False)

    def load_caches(self) -> None:
        self.pcr_cache = self._read_date_index_csv(CBOE_PCR_CACHE_PATH, PCR_COLS)
        self.volume_cache = self._read_date_index_csv(CBOE_VOLUME_CACHE_PATH, VOLUME_COLS)
        self.validation_cache = self._read_validation_cache(CBOE_VALIDATION_CACHE_PATH)

        effective_start = pd.Timestamp(CBOE_EFFECTIVE_START_DATE).normalize()
        self.pcr_cache = self.pcr_cache.loc[self.pcr_cache.index >= effective_start].copy()
        self.volume_cache = self.volume_cache.loc[self.volume_cache.index >= effective_start].copy()

        if not self.validation_cache.empty:
            vd = pd.to_datetime(self.validation_cache["date"], errors="coerce")
            self.validation_cache = self.validation_cache.loc[vd >= effective_start].copy()
            if "数据来源类型" in self.validation_cache.columns:
                self.validation_cache = self.validation_cache.loc[
                    self.validation_cache["数据来源类型"].astype(str).eq("daily_page")
                ].copy()
            self.validation_cache = self.validation_cache.reindex(columns=VALIDATION_COLS)

    def save_caches_and_latest_run(self) -> None:
        self._save_date_index_csv(self.pcr_cache, CBOE_PCR_CACHE_PATH, PCR_COLS)
        self._save_date_index_csv(self.volume_cache, CBOE_VOLUME_CACHE_PATH, VOLUME_COLS)
        self._save_validation_cache(self.validation_cache, CBOE_VALIDATION_CACHE_PATH)

        status_df = pd.DataFrame(self.status_rows).reindex(columns=STATUS_COLS)
        CBOE_STATUS_LATEST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        safe_write_csv(status_df, CBOE_STATUS_LATEST_RUN_PATH, announce=False)

        latest_validation_df = pd.DataFrame(self.latest_validation_rows).reindex(columns=VALIDATION_COLS)
        CBOE_VALIDATION_LATEST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        safe_write_csv(latest_validation_df, CBOE_VALIDATION_LATEST_RUN_PATH, announce=False)

    @staticmethod
    def merge_cache_by_index(base_df: pd.DataFrame, new_df: pd.DataFrame, expected_cols: list[str]) -> pd.DataFrame:
        if base_df is None or base_df.empty:
            merged = new_df.copy()
        elif new_df is None or new_df.empty:
            merged = base_df.copy()
        else:
            merged = pd.concat([base_df, new_df])
            merged = merged[~merged.index.duplicated(keep="last")]
        merged.index = pd.to_datetime(merged.index).normalize()
        merged.index.name = "date"
        return merged.sort_index().reindex(columns=expected_cols)

    # ------------------------------------------------------------------
    # HTML fetching and parsing
    # ------------------------------------------------------------------
    def fetch_cboe_html_for_date(self, date: pd.Timestamp) -> tuple[str, str]:
        url = self.build_cboe_url(date)
        last_error = None
        for attempt in range(1, self.config.request_max_retries + 1):
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response.text, response.url
            except Exception as exc:
                last_error = exc
                time.sleep(2 * attempt)
        raise RuntimeError(f"Cboe request failed for {self.fmt_date(date)}: {last_error}")

    def save_raw_html_snapshot(self, date: pd.Timestamp, html_text: str) -> Path:
        CBOE_HTML_DIR.mkdir(parents=True, exist_ok=True)
        path = CBOE_HTML_DIR / f"cboe_daily_market_statistics_{self.fmt_date(date)}.html"
        path.write_text(html_text, encoding="utf-8")
        return path

    def should_save_html_snapshot(self, status: str) -> bool:
        mode = self.config.raw_html_snapshot_mode
        if mode == "all":
            return True
        if mode == "failed_only":
            return (not str(status).startswith("成功")) and (
                self.failed_html_saved_count < self.config.max_failed_html_snapshots_per_run
            )
        if mode == "none":
            return False
        raise ValueError("raw_html_snapshot_mode 只能是 none / failed_only / all")

    def html_to_lines(self, html_text: str) -> list[str]:
        soup = BeautifulSoup(html_text, "lxml")
        text = soup.get_text(separator="\n")
        raw_lines = [line.strip() for line in text.splitlines()]
        return [line for line in raw_lines if line]

    def parse_ratios_by_read_html(self, html_text: str) -> dict[str, float]:
        found: dict[str, float] = {}
        try:
            tables = pd.read_html(StringIO(html_text))
        except Exception:
            return found

        target_labels = set(CBOE_PCR_META.keys())
        for table in tables:
            if table is None or table.empty:
                continue
            temp = table.copy()
            temp.columns = [self.normalize_text(c) for c in temp.columns]
            table_text = self.normalize_text(temp.astype(str).to_string())
            if "PUT/CALL RATIO" not in table_text:
                continue

            ratio_col_candidates = [c for c in temp.columns if "RATIO" in c or "RATIOS" in c]
            value_col_candidates = [c for c in temp.columns if "VALUE" in c]

            if ratio_col_candidates and value_col_candidates:
                label_col = ratio_col_candidates[0]
                value_col = value_col_candidates[0]
                for _, row in temp.iterrows():
                    label = self.normalize_text(row.get(label_col, ""))
                    value = self.parse_number(row.get(value_col, None))
                    if label in target_labels and pd.notna(value):
                        found[label] = float(value)

            if len(temp.columns) >= 2:
                cols = list(temp.columns)
                for _, row in temp.iterrows():
                    for i in range(len(cols) - 1):
                        label = self.normalize_text(row.get(cols[i], ""))
                        value = self.parse_number(row.get(cols[i + 1], None))
                        if label in target_labels and pd.notna(value):
                            found[label] = float(value)
        return found

    def parse_ratios_by_text(self, html_text: str) -> dict[str, float]:
        lines = self.html_to_lines(html_text)
        normalized_lines = [self.normalize_text(line) for line in lines]
        full_text = " ".join(normalized_lines)
        found: dict[str, float] = {}

        for raw_label in CBOE_PCR_META.keys():
            label_norm = self.normalize_text(raw_label)
            pattern = re.compile(re.escape(label_norm) + r"\s+([0-9]+(?:\.[0-9]+)?)")
            match = pattern.search(full_text)
            if match:
                value = self.parse_number(match.group(1))
                if pd.notna(value):
                    found[raw_label] = float(value)
                    continue

            for i, line_norm in enumerate(normalized_lines):
                if line_norm == label_norm and i + 1 < len(normalized_lines):
                    value = self.parse_number(normalized_lines[i + 1])
                    if pd.notna(value):
                        found[raw_label] = float(value)
                        break
        return found

    def parse_cboe_ratios(self, html_text: str) -> tuple[dict[str, float], str]:
        found: dict[str, float] = {}
        methods: list[str] = []

        found_html = self.parse_ratios_by_read_html(html_text)
        if found_html:
            found.update(found_html)
            methods.append("read_html")

        missing = [label for label in CBOE_PCR_META.keys() if label not in found]
        if missing:
            found_text = self.parse_ratios_by_text(html_text)
            for label, value in found_text.items():
                if label not in found:
                    found[label] = value
            if found_text:
                methods.append("text_fallback")

        if not methods:
            methods.append("未解析到PCR")
        return found, " + ".join(methods)

    def parse_volume_sections_by_text(self, html_text: str) -> dict[str, dict[str, float]]:
        lines = self.html_to_lines(html_text)
        normalized_lines = [self.normalize_text(line) for line in lines]

        target_sections_norm_to_original = {
            self.normalize_text(meta["volume_section"]): meta["volume_section"]
            for meta in CBOE_PCR_META.values()
        }
        target_section_names_norm = set(target_sections_norm_to_original.keys())
        found: dict[str, dict[str, float]] = {}

        for i, line_norm in enumerate(normalized_lines):
            if line_norm not in target_section_names_norm:
                continue

            section_original = target_sections_norm_to_original[line_norm]
            next_section_idx = len(normalized_lines)
            for j in range(i + 1, len(normalized_lines)):
                if normalized_lines[j] in target_section_names_norm:
                    next_section_idx = j
                    break

            section_window = normalized_lines[i + 1:next_section_idx]
            for k, candidate in enumerate(section_window):
                if not candidate.startswith("VOLUME"):
                    continue
                nums = self.extract_number_tokens(candidate)
                if len(nums) < 3:
                    for later in section_window[k + 1:]:
                        later_norm = self.normalize_text(later)
                        if later_norm.startswith("OPEN INTEREST"):
                            break
                        if later_norm in ["NAME", "CALL", "PUT", "TOTAL"]:
                            continue
                        nums.extend(self.extract_number_tokens(later))
                        if len(nums) >= 3:
                            break
                if len(nums) >= 3:
                    found[section_original] = {"call": nums[0], "put": nums[1], "total": nums[2]}
                break
        return found

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate_one_indicator(
        self,
        date: pd.Timestamp,
        raw_label: str,
        observed_pcr,
        volume_info: dict | None,
        source_type: str = "",
        source_url: str = "",
        source_regime: str = "",
    ) -> dict:
        meta = CBOE_PCR_META[raw_label]
        pcr_col = meta["pcr_col"]
        section = meta["volume_section"]

        result = {
            "date": self.fmt_date(date),
            "数据来源类型": source_type,
            "源文件或URL": source_url,
            "source_regime": source_regime,
            "网页原始名称": raw_label,
            "指标名称": meta["指标名称"],
            "PCR列名": pcr_col,
            "成交量分区": section,
            "网页PCR": pd.NA,
            "Call成交量": pd.NA,
            "Put成交量": pd.NA,
            "Total成交量": pd.NA,
            "成交量反算PCR": pd.NA,
            "PCR差异": pd.NA,
            "Total校验": "未验证",
            "PCR交叉验证": "未验证",
            "范围校验": "未验证",
            "最终校验": "未通过",
            "备注": "",
        }

        notes: list[str] = []
        if observed_pcr is None or pd.isna(observed_pcr):
            notes.append("未抓到PCR")
            result["备注"] = "; ".join(notes)
            return result

        observed_pcr = float(observed_pcr)
        result["网页PCR"] = observed_pcr

        if PCR_REASONABLE_MIN <= observed_pcr <= PCR_REASONABLE_MAX:
            result["范围校验"] = "通过"
        else:
            result["范围校验"] = "警告"
            notes.append(f"PCR超出软约束范围 {PCR_REASONABLE_MIN}-{PCR_REASONABLE_MAX}")

        if volume_info is None:
            notes.append("未抓到对应成交量分区，无法反算PCR")
            result["最终校验"] = "部分通过"
            result["备注"] = "; ".join(notes)
            return result

        call_volume = volume_info.get("call", pd.NA)
        put_volume = volume_info.get("put", pd.NA)
        total_volume = volume_info.get("total", pd.NA)

        result["Call成交量"] = call_volume
        result["Put成交量"] = put_volume
        result["Total成交量"] = total_volume

        if pd.notna(call_volume) and pd.notna(put_volume) and pd.notna(total_volume):
            if abs((call_volume + put_volume) - total_volume) <= 1:
                result["Total校验"] = "通过"
            else:
                result["Total校验"] = "失败"
                notes.append("Call + Put 不等于 Total")

            if call_volume > 0:
                calculated_pcr = round(put_volume / call_volume, 2)
                diff = abs(observed_pcr - calculated_pcr)
                result["成交量反算PCR"] = calculated_pcr
                result["PCR差异"] = diff
                if diff <= PCR_ABS_TOLERANCE:
                    result["PCR交叉验证"] = "通过"
                else:
                    result["PCR交叉验证"] = "失败"
                    notes.append(f"PCR反算差异较大：网页={observed_pcr}，反算={calculated_pcr}，diff={diff:.4f}")
            else:
                result["PCR交叉验证"] = "无法验证"
                notes.append("Call成交量为0，无法反算PCR")
        else:
            notes.append("成交量字段不完整")

        if result["范围校验"] == "通过" and result["Total校验"] == "通过" and result["PCR交叉验证"] == "通过":
            result["最终校验"] = "通过"
        elif pd.notna(result["网页PCR"]):
            result["最终校验"] = "部分通过"
        else:
            result["最终校验"] = "未通过"

        result["备注"] = "; ".join(notes)
        return result

    def build_rows_for_date(self, date: pd.Timestamp, ratios: dict, volume_sections: dict, final_url: str) -> tuple[dict, dict, list[dict]]:
        pcr_row: dict = {}
        volume_row: dict = {}
        validation_rows: list[dict] = []

        for raw_label, meta in CBOE_PCR_META.items():
            pcr_col = meta["pcr_col"]
            base = pcr_col.replace("_PCR", "")
            section = meta["volume_section"]

            observed_pcr = ratios.get(raw_label, pd.NA)
            volume_info = volume_sections.get(section, None)
            pcr_row[pcr_col] = observed_pcr

            if volume_info is not None:
                call_v = volume_info.get("call", pd.NA)
                put_v = volume_info.get("put", pd.NA)
                total_v = volume_info.get("total", pd.NA)
                calc_pcr = round(put_v / call_v, 2) if pd.notna(call_v) and call_v > 0 and pd.notna(put_v) else pd.NA
                diff = abs(float(observed_pcr) - float(calc_pcr)) if pd.notna(observed_pcr) and pd.notna(calc_pcr) else pd.NA
            else:
                call_v = put_v = total_v = calc_pcr = diff = pd.NA

            volume_row[f"{base}_Call_Volume"] = call_v
            volume_row[f"{base}_Put_Volume"] = put_v
            volume_row[f"{base}_Total_Volume"] = total_v
            volume_row[f"{base}_PCR_Calculated"] = calc_pcr
            volume_row[f"{base}_PCR_Diff"] = diff

            validation_rows.append(
                self.validate_one_indicator(
                    date=date,
                    raw_label=raw_label,
                    observed_pcr=observed_pcr,
                    volume_info=volume_info,
                    source_type="daily_page",
                    source_url=final_url,
                    source_regime="daily_page_2019_now",
                )
            )

        return pcr_row, volume_row, validation_rows

    @staticmethod
    def summarize_validation_rows(validation_rows: list[dict]) -> dict[str, int]:
        return {
            "通过": sum(row["最终校验"] == "通过" for row in validation_rows),
            "部分通过": sum(row["最终校验"] == "部分通过" for row in validation_rows),
            "未通过": sum(row["最终校验"] == "未通过" for row in validation_rows),
        }

    @staticmethod
    def count_core_parsed(pcr_row: dict) -> int:
        return int(sum(pd.notna(pcr_row.get(col, pd.NA)) for col in REQUIRED_PCR_COLS))

    @staticmethod
    def core_complete(pcr_row: dict) -> bool:
        return CboePcrUpdater.count_core_parsed(pcr_row) == len(REQUIRED_PCR_COLS)

    @staticmethod
    def row_is_fully_validated(
        pcr_df: pd.DataFrame,
        volume_df: pd.DataFrame,
        validation_df: pd.DataFrame,
        date: pd.Timestamp,
    ) -> bool:
        """Return whether a cached Cboe day is accepted and needs no re-fetch.

        A row is complete only when all core PCR values exist *and* every core
        validation row is accepted. The one legitimate partial case is a zero
        Call-volume denominator: the ratio cannot be recalculated, but the
        reported PCR can remain usable when range and total-volume checks pass.
        """
        date = pd.Timestamp(date).normalize()
        if pcr_df.empty or date not in pcr_df.index:
            return False
        if not pcr_df.loc[date, REQUIRED_PCR_COLS].notna().all():
            return False
        if volume_df.empty or date not in volume_df.index:
            return False
        if validation_df.empty:
            return False

        validation = validation_df.copy()
        validation["date"] = pd.to_datetime(validation["date"], errors="coerce").dt.normalize()
        validation = validation.loc[
            validation["date"].eq(date)
            & validation["数据来源类型"].astype(str).eq("daily_page")
            & validation["网页原始名称"].isin(REQUIRED_PCR_RAW_LABELS)
        ].copy()
        if len(validation) != len(REQUIRED_PCR_RAW_LABELS):
            return False
        if validation["网页原始名称"].duplicated().any():
            return False

        for _, row in validation.iterrows():
            final_status = str(row.get("最终校验", ""))
            if final_status == "通过":
                continue

            cross_status = str(row.get("PCR交叉验证", ""))
            range_status = str(row.get("范围校验", ""))
            total_status = str(row.get("Total校验", ""))
            call_volume = pd.to_numeric(pd.Series([row.get("Call成交量")]), errors="coerce").iloc[0]
            accepted_zero_denominator_case = (
                final_status == "部分通过"
                and cross_status == "无法验证"
                and range_status == "通过"
                and total_status == "通过"
                and pd.notna(call_volume)
                and float(call_volume) == 0.0
            )
            if not accepted_zero_denominator_case:
                return False
        return True

    # ------------------------------------------------------------------
    # Main update loop
    # ------------------------------------------------------------------
    def select_dates_for_this_run(self, all_dates: list[pd.Timestamp]) -> list[pd.Timestamp]:
        selected: list[pd.Timestamp] = []
        for dt in all_dates:
            dt = pd.Timestamp(dt).normalize()
            already_complete = self.row_is_fully_validated(
                self.pcr_cache, self.volume_cache, self.validation_cache, dt
            )
            if self.config.remote_update_mode == "auto" and self.config.process_missing_only and already_complete:
                continue
            selected.append(dt)

        if self.config.run_direction == "newest_first":
            selected = list(reversed(selected))
        elif self.config.run_direction == "oldest_first":
            selected = list(selected)
        else:
            raise ValueError("run_direction 只能是 oldest_first 或 newest_first")

        if self.config.max_dates_per_run is not None and len(selected) > self.config.max_dates_per_run:
            print(
                f"[提醒] 本轮待处理日期 {len(selected)} 个，超过 max_dates_per_run="
                f"{self.config.max_dates_per_run}，只处理前 {self.config.max_dates_per_run} 个。"
            )
            selected = selected[: self.config.max_dates_per_run]
        return selected

    def process_one_date(self, dt: pd.Timestamp, n: int, total: int) -> None:
        dt = pd.Timestamp(dt).normalize()
        date_str = self.fmt_date(dt)
        print(f"\n[{n}/{total}] 开始抓取 Cboe Daily：{date_str}")

        request_url = self.build_cboe_url(dt)
        final_url = ""
        parse_method = ""
        html_snapshot_path = ""
        html_text = ""
        ratios: dict = {}
        volume_sections: dict = {}
        date_validation_rows: list[dict] = []

        try:
            html_text, final_url = self.fetch_cboe_html_for_date(dt)
            ratios, parse_method = self.parse_cboe_ratios(html_text)
            volume_sections = self.parse_volume_sections_by_text(html_text)
            pcr_row, volume_row, date_validation_rows = self.build_rows_for_date(dt, ratios, volume_sections, final_url)

            ratio_count = int(sum(pd.notna(value) for value in pcr_row.values()))
            core_ratio_count = self.count_core_parsed(pcr_row)
            volume_section_count = len(volume_sections)
            validation_summary = self.summarize_validation_rows(date_validation_rows)

            if self.core_complete(pcr_row):
                pcr_row_df = pd.DataFrame([pcr_row], index=[dt]).reindex(columns=PCR_COLS)
                volume_row_df = pd.DataFrame([volume_row], index=[dt]).reindex(columns=VOLUME_COLS)
                self.pcr_cache = self.merge_cache_by_index(self.pcr_cache, pcr_row_df, PCR_COLS)
                self.volume_cache = self.merge_cache_by_index(self.volume_cache, volume_row_df, VOLUME_COLS)

                if not self.validation_cache.empty:
                    mask_drop = (
                        self.validation_cache["date"].astype(str).eq(date_str)
                        & self.validation_cache["数据来源类型"].astype(str).eq("daily_page")
                    )
                    self.validation_cache = self.validation_cache.loc[~mask_drop].copy()
                date_validation_df = pd.DataFrame(date_validation_rows).reindex(columns=VALIDATION_COLS)
                self.validation_cache = pd.concat([self.validation_cache, date_validation_df], ignore_index=True).reindex(columns=VALIDATION_COLS)
                self.latest_validation_rows.extend(date_validation_rows)

                if validation_summary["通过"] == len(CBOE_PCR_META):
                    status = "成功_全部验证通过"
                    note = ""
                elif validation_summary["通过"] >= len(REQUIRED_PCR_RAW_LABELS):
                    status = "成功_核心完整_部分字段或验证不完整"
                    note = "核心PCR已写入缓存；部分成交量验证可能缺失。"
                else:
                    status = "成功_核心PCR完整_验证不足"
                    note = "核心PCR已写入缓存，但成交量交叉验证不足。"
            elif ratio_count > 0:
                status = "部分成功_核心PCR不完整_未写入缓存"
                note = f"核心PCR {core_ratio_count}/{len(REQUIRED_PCR_COLS)}，全部PCR {ratio_count}/{len(CBOE_PCR_META)}；本日未写入。"
                self.latest_validation_rows.extend(date_validation_rows)
            else:
                status = "失败_未解析到PCR_未写入缓存"
                note = "网页已获取，但未解析到目标PCR字段。"
                self.latest_validation_rows.extend(date_validation_rows)

            if self.should_save_html_snapshot(status) and html_text:
                html_snapshot_path = self.safe_display_path(self.save_raw_html_snapshot(dt, html_text))
                if self.config.raw_html_snapshot_mode == "failed_only" and not str(status).startswith("成功"):
                    self.failed_html_saved_count += 1

            self.status_rows.append({
                "date": date_str,
                "状态": status,
                "数据来源类型": "daily_page",
                "请求URL": request_url,
                "最终URL": final_url,
                "解析方法": parse_method,
                "PCR目标数量": len(CBOE_PCR_META),
                "PCR核心目标数量": len(REQUIRED_PCR_COLS),
                "PCR解析数量": ratio_count,
                "PCR核心解析数量": core_ratio_count,
                "成交量分区解析数量": volume_section_count,
                "验证通过数量": validation_summary["通过"],
                "验证部分通过数量": validation_summary["部分通过"],
                "验证失败数量": validation_summary["未通过"],
                "HTML快照": html_snapshot_path,
                "备注": note,
                "运行时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

            print(
                f"完成 {date_str}：{status}，"
                f"PCR={ratio_count}/{len(CBOE_PCR_META)}，"
                f"成交量分区={volume_section_count}，验证通过={validation_summary['通过']}"
            )

        except Exception as exc:
            status = "失败_请求或解析异常"
            note = str(exc)

            if self.should_save_html_snapshot(status) and html_text:
                html_snapshot_path = self.safe_display_path(self.save_raw_html_snapshot(dt, html_text))
                if self.config.raw_html_snapshot_mode == "failed_only" and not str(status).startswith("成功"):
                    self.failed_html_saved_count += 1

            self.status_rows.append({
                "date": date_str,
                "状态": status,
                "数据来源类型": "daily_page",
                "请求URL": request_url,
                "最终URL": final_url,
                "解析方法": parse_method,
                "PCR目标数量": len(CBOE_PCR_META),
                "PCR核心目标数量": len(REQUIRED_PCR_COLS),
                "PCR解析数量": len(ratios),
                "PCR核心解析数量": "",
                "成交量分区解析数量": len(volume_sections),
                "验证通过数量": "",
                "验证部分通过数量": "",
                "验证失败数量": "",
                "HTML快照": html_snapshot_path,
                "备注": note,
                "运行时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            print(f"[失败] {date_str}: {exc}")

    # ------------------------------------------------------------------
    # Reports and exports
    # ------------------------------------------------------------------
    def build_dictionary(self) -> pd.DataFrame:
        rows = []
        for raw_label, meta in CBOE_PCR_META.items():
            rows.append({
                "数据源": "Cboe Daily Market Statistics",
                "daily_page_url": CBOE_DAILY_URL,
                "有效起点": CBOE_EFFECTIVE_START_DATE,
                "网页原始名称": raw_label,
                "指标名称": meta["指标名称"],
                "PCR列名": meta["pcr_col"],
                "成交量分区": meta["volume_section"],
                "模块": meta["模块"],
                "是否核心字段": meta["是否核心字段"],
                "是否建议纳入": meta["是否建议纳入"],
                "说明": meta["说明"],
                "验证方法": "网页PCR 与 Put Volume / Call Volume 反算值交叉验证",
            })
        df = pd.DataFrame(rows)
        CBOE_DICTIONARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        safe_write_csv(df, CBOE_DICTIONARY_PATH, announce=False)
        return df

    def build_missing_report(self, panel: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for col in PCR_COLS:
            s = panel[col] if col in panel.columns else pd.Series(dtype=float)
            total_rows = len(s)
            valid_count = int(s.notna().sum())
            missing_count = int(s.isna().sum())
            first_valid = s.first_valid_index() if valid_count else pd.NaT
            latest_valid = s.last_valid_index() if valid_count else pd.NaT
            latest_value = s.dropna().iloc[-1] if valid_count else pd.NA
            rows.append({
                "指标名称": col,
                "研究口径起点": CBOE_EFFECTIVE_START_DATE,
                "总目标交易日数": total_rows,
                "有效值数量": valid_count,
                "空值数量": missing_count,
                "空值比例": missing_count / total_rows if total_rows > 0 else None,
                "是否全列为空": "是" if valid_count == 0 else "否",
                "最早有效日期": self.fmt_date(first_valid),
                "最新有效日期": self.fmt_date(latest_valid),
                "最新有效值": latest_value,
            })
        df = pd.DataFrame(rows)
        safe_write_csv(df, CBOE_MISSING_REPORT_PATH, announce=False)
        return df

    def build_latest_snapshot(self, panel: pd.DataFrame) -> pd.DataFrame:
        now_date = pd.Timestamp.now(tz="Asia/Singapore").date()
        rows = []
        for raw_label, meta in CBOE_PCR_META.items():
            col = meta["pcr_col"]
            s = panel[col].dropna() if col in panel.columns else pd.Series(dtype=float)
            if len(s) > 0:
                latest_date = s.index[-1]
                latest_value = s.iloc[-1]
                lag_days = (pd.Timestamp(now_date) - pd.Timestamp(latest_date)).days
            else:
                latest_date = pd.NaT
                latest_value = pd.NA
                lag_days = pd.NA
            rows.append({
                "数据源": "Cboe Daily Market Statistics",
                "研究口径": "Post-2019 daily page only",
                "有效起点": CBOE_EFFECTIVE_START_DATE,
                "指标名称": meta["指标名称"],
                "PCR列名": col,
                "网页原始名称": raw_label,
                "最新有效日期": self.fmt_date(latest_date),
                "最新有效值": latest_value,
                "距离今天自然日数": lag_days,
                "是否核心字段": meta["是否核心字段"],
                "是否建议纳入": meta["是否建议纳入"],
                "daily_page_url": CBOE_DAILY_URL,
            })
        df = pd.DataFrame(rows)
        safe_write_csv(df, CBOE_LATEST_SNAPSHOT_PATH, announce=False)
        return df

    def build_yearly_coverage(self, panel: pd.DataFrame) -> pd.DataFrame:
        rows = []
        if not panel.empty:
            temp = panel.copy()
            temp["year"] = temp.index.year
            for year, group in temp.groupby("year"):
                row = {"year": year, "研究口径起点": CBOE_EFFECTIVE_START_DATE, "目标交易日数": len(group)}
                for col in PCR_COLS:
                    valid = int(group[col].notna().sum()) if col in group.columns else 0
                    row[f"{col}_有效天数"] = valid
                    row[f"{col}_覆盖率"] = valid / len(group) if len(group) > 0 else None
                rows.append(row)
        df = pd.DataFrame(rows)
        safe_write_csv(df, CBOE_YEARLY_COVERAGE_PATH, announce=False)
        return df

    def build_validation_summary(self) -> pd.DataFrame:
        df = self.validation_cache.copy()
        rows = []
        if not df.empty:
            for keys, group in df.groupby(["PCR列名", "数据来源类型"], dropna=False):
                pcr_col, source_type = keys
                total = len(group)
                passed = int((group["最终校验"] == "通过").sum())
                partial = int((group["最终校验"] == "部分通过").sum())
                failed = int((group["最终校验"] == "未通过").sum())
                rows.append({
                    "PCR列名": pcr_col,
                    "数据来源类型": source_type,
                    "研究口径起点": CBOE_EFFECTIVE_START_DATE,
                    "验证记录数": total,
                    "通过数量": passed,
                    "部分通过数量": partial,
                    "未通过数量": failed,
                    "通过率": passed / total if total > 0 else None,
                    "通过或部分通过率": (passed + partial) / total if total > 0 else None,
                })
        out = pd.DataFrame(rows)
        safe_write_csv(out, CBOE_VALIDATION_SUMMARY_PATH, announce=False)
        return out

    def build_file_size_report(self) -> pd.DataFrame:
        file_map = {
            "PCR缓存": CBOE_PCR_CACHE_PATH,
            "成交量缓存": CBOE_VOLUME_CACHE_PATH,
            "验证缓存": CBOE_VALIDATION_CACHE_PATH,
            "本轮状态": CBOE_STATUS_LATEST_RUN_PATH,
            "本轮验证": CBOE_VALIDATION_LATEST_RUN_PATH,
            "缺失值统计": CBOE_MISSING_REPORT_PATH,
            "最新快照": CBOE_LATEST_SNAPSHOT_PATH,
            "年度覆盖率": CBOE_YEARLY_COVERAGE_PATH,
            "验证汇总": CBOE_VALIDATION_SUMMARY_PATH,
            "指标字典": CBOE_DICTIONARY_PATH,
        }
        rows = []
        for name, path in file_map.items():
            rows.append({
                "文件名称": name,
                "路径": self.safe_display_path(path),
                "是否存在": "是" if path.exists() else "否",
                "大小MiB": self.safe_file_size_mb(path),
            })
        df = pd.DataFrame(rows)
        safe_write_csv(df, CBOE_FILE_SIZE_PATH, announce=False)
        return df

    def export_excel_report(
        self,
        panel: pd.DataFrame,
        volume_panel: pd.DataFrame,
        dictionary_df: pd.DataFrame,
        missing_df: pd.DataFrame,
        latest_df: pd.DataFrame,
        yearly_df: pd.DataFrame,
        validation_summary_df: pd.DataFrame,
        file_size_df: pd.DataFrame,
    ) -> Path:
        run_date_str = datetime.now().strftime("%Y%m%d")
        output_path = OUTPUT_DIR / f"Cboe_PCR_观察表_POST2019_{run_date_str}.xlsx"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        status_df = pd.DataFrame(self.status_rows).reindex(columns=STATUS_COLS)
        latest_validation_df = pd.DataFrame(self.latest_validation_rows).reindex(columns=VALIDATION_COLS)

        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            panel.sort_index(ascending=False).to_excel(writer, sheet_name="Cboe_PCR_每日表")
            volume_panel.sort_index(ascending=False).to_excel(writer, sheet_name="成交量反算验证表")
            latest_df.to_excel(writer, sheet_name="最新快照表", index=False)
            yearly_df.to_excel(writer, sheet_name="年度覆盖率表", index=False)
            validation_summary_df.to_excel(writer, sheet_name="验证汇总表", index=False)
            status_df.to_excel(writer, sheet_name="本次运行状态表", index=False)
            latest_validation_df.to_excel(writer, sheet_name="本次数据验证明细", index=False)
            self.validation_cache.to_excel(writer, sheet_name="全量数据验证明细", index=False)
            missing_df.to_excel(writer, sheet_name="缺失值统计表", index=False)
            dictionary_df.to_excel(writer, sheet_name="指标字典", index=False)
            file_size_df.to_excel(writer, sheet_name="文件大小表", index=False)

            workbook = writer.book
            for ws in workbook.worksheets:
                ws.freeze_panes = "B2"
                ws.auto_filter.ref = ws.dimensions
                for col_cells in ws.columns:
                    col_letter = col_cells[0].column_letter
                    max_len = 0
                    for cell in col_cells[:200]:
                        if cell.value is not None:
                            max_len = max(max_len, len(str(cell.value)))
                    ws.column_dimensions[col_letter].width = min(max(max_len + 2, 10), 42)
        return output_path

    def build_reports(self, all_target_dates: list[pd.Timestamp]) -> dict:
        effective_start = pd.Timestamp(CBOE_EFFECTIVE_START_DATE).normalize()
        expected_index = pd.DatetimeIndex(all_target_dates, name="date")
        expected_index = expected_index[expected_index >= effective_start]

        panel = self.pcr_cache.reindex(expected_index).sort_index()
        volume_panel = self.volume_cache.reindex(expected_index).sort_index()

        dictionary_df = self.build_dictionary()
        missing_df = self.build_missing_report(panel)
        latest_df = self.build_latest_snapshot(panel)
        yearly_df = self.build_yearly_coverage(panel)
        validation_summary_df = self.build_validation_summary()
        file_size_df = self.build_file_size_report()

        excel_path = None
        if self.config.export_excel:
            excel_path = self.export_excel_report(
                panel=panel,
                volume_panel=volume_panel,
                dictionary_df=dictionary_df,
                missing_df=missing_df,
                latest_df=latest_df,
                yearly_df=yearly_df,
                validation_summary_df=validation_summary_df,
                file_size_df=file_size_df,
            )

        return {
            "panel": panel,
            "volume_panel": volume_panel,
            "dictionary": dictionary_df,
            "missing_report": missing_df,
            "latest_snapshot": latest_df,
            "yearly_coverage": yearly_df,
            "validation_summary": validation_summary_df,
            "file_size_report": file_size_df,
            "excel_path": excel_path,
        }

    def run(self) -> dict:
        ensure_data_dirs()
        CBOE_HTML_DIR.mkdir(parents=True, exist_ok=True)

        if self.config.remote_update_mode not in {"auto", "force", "off"}:
            raise ValueError("remote_update_mode 只能是 auto / force / off")
        if self.config.raw_html_snapshot_mode not in {"none", "failed_only", "all"}:
            raise ValueError("raw_html_snapshot_mode 只能是 none / failed_only / all")

        self.load_caches()
        start = self.resolve_start_date()
        end = self.resolve_end_date()
        all_target_dates = self.make_trading_date_list(start, end)

        print("\n[Cboe PCR] Post-2019 Daily 模块")
        print(f"研究口径起点：{CBOE_EFFECTIVE_START_DATE}")
        print(f"目标日期范围：{self.fmt_date(start)} 至 {self.fmt_date(end)}")
        print(f"目标交易日数：{len(all_target_dates)}")
        print(f"本地PCR缓存：{len(self.pcr_cache)} 行")
        print(f"本地成交量缓存：{len(self.volume_cache)} 行")

        if self.config.remote_update_mode == "off":
            date_list: list[pd.Timestamp] = []
            print("[模式] remote_update_mode='off'，不请求远程，只刷新报告。")
        elif self.config.remote_update_mode == "force":
            date_list = all_target_dates
            if self.config.run_direction == "newest_first":
                date_list = list(reversed(date_list))
            if self.config.max_dates_per_run is not None:
                date_list = date_list[: self.config.max_dates_per_run]
            print(f"[模式] force，本轮远程处理：{len(date_list)} 日期")
        else:
            date_list = self.select_dates_for_this_run(all_target_dates)
            print(f"[模式] auto，本轮远程待处理：{len(date_list)} 日期")

        for n, dt in enumerate(date_list, start=1):
            self.process_one_date(dt, n=n, total=len(date_list))
            self.remote_processed_count += 1
            if self.remote_processed_count % self.config.save_progress_every_n_dates == 0:
                self.save_caches_and_latest_run()
                print(f"[保存] 已阶段性保存进度：{self.remote_processed_count} 个远程日期")
            time.sleep(self.config.request_sleep_seconds)

        self.save_caches_and_latest_run()
        report_dict = self.build_reports(all_target_dates)

        print("\n[完成] Cboe PCR 模块运行完成")
        print(f"本轮远程处理日期数：{self.remote_processed_count}")
        print(f"PCR缓存：{CBOE_PCR_CACHE_PATH} ({self.safe_file_size_mb(CBOE_PCR_CACHE_PATH)} MiB)")
        print(f"成交量缓存：{CBOE_VOLUME_CACHE_PATH} ({self.safe_file_size_mb(CBOE_VOLUME_CACHE_PATH)} MiB)")
        if report_dict.get("excel_path"):
            print(f"Excel观察表：{report_dict['excel_path']}")

        return report_dict


def update_cboe_pcr(config: CboeRunConfig | None = None) -> dict:
    updater = CboePcrUpdater(config=config)
    return updater.run()
