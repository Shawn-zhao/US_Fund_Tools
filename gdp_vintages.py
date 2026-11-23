"""BEA 名义 GDP 发布记录及费城联储的暖机数据。"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
import math
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


BEA_GDP_URL = "https://apps.bea.gov/national/xls/gdp-gdi-vintage-history.xlsx"
PHILLY_GDP_URL = ("https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/"
                  "real-time-data/data-files/xlsx/noutputMvQd.xlsx")
GDP_RELEASES = "gdp_bea_release_vintages.csv"
GDP_WARMUP = "gdp_philly_fed_warmup.csv"
FIELDS = ("available_date", "quarter", "gdp_billions_saar", "vintage")
BEA_START = "2002Q1"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MONTHS = dict(zip("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), range(1, 13)))


def _xlsx_rows(raw: bytes, sheet: int):
    """读取两个来源的 XLSX 单元格值，沿用生产环境现有依赖。"""
    with ZipFile(io.BytesIO(raw)) as book:
        strings = ET.fromstring(book.read("xl/sharedStrings.xml"))
        shared = ["".join(node.itertext()) for node in strings]
        sheet_data = ET.fromstring(book.read(f"xl/worksheets/sheet{sheet}.xml"))
    for row in sheet_data.find(NS + "sheetData"):
        values = {}
        for cell in row:
            value = cell.findtext(NS + "v")
            if value is None:
                continue
            column = cell.attrib["r"].rstrip("0123456789")
            values[column] = shared[int(value)] if cell.get("t") == "s" else value
        yield values


def _valid_gdp(value: str) -> float:
    gdp = float(value.replace(",", ""))
    if not math.isfinite(gdp) or gdp <= 0:
        raise ValueError(f"无效的名义 GDP: {value}")
    return gdp


def parse_bea(raw: bytes) -> dict[tuple[str, str], dict]:
    rows = {}
    quarter = None
    as_of = dt.datetime.now(dt.timezone.utc).date()
    for row in _xlsx_rows(raw, 2):
        if re.fullmatch(r"\d{4}Q[1-4]", row.get("A", "")):
            quarter = row["A"]
            continue
        date_match = re.match(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})\b", row.get("G", ""))
        if quarter is None or date_match is None:
            continue
        month, day, year = date_match.groups()
        available = dt.date(int(year), MONTHS[month], int(day))
        quarter_year, quarter_num = int(quarter[:4]), int(quarter[-1])
        end_month = quarter_num * 3
        quarter_end = dt.date(quarter_year, end_month, calendar.monthrange(quarter_year, end_month)[1])
        if available <= quarter_end or available > as_of:
            raise ValueError(f"BEA 发布日期异常: {quarter} / {available}")
        date = available.isoformat()
        data = dict(zip(FIELDS, (date, quarter, _valid_gdp(row["C"]), row["B"])))
        key = (date, quarter)
        if key in rows and rows[key]["gdp_billions_saar"] != data["gdp_billions_saar"]:
            raise ValueError(f"同日 GDP 发布值冲突: {key}")
        rows[key] = data
    quarters = sorted({quarter for _, quarter in rows})
    if not quarters or quarters[0] != BEA_START:
        raise ValueError("BEA 发布表缺少起始季度数据")
    quarter_numbers = [int(q[:4]) * 4 + int(q[-1]) for q in quarters]
    if quarter_numbers != list(range(quarter_numbers[0], quarter_numbers[-1] + 1)):
        raise ValueError("BEA 发布表季度覆盖存在缺口")
    first_release = {}
    for date, quarter in sorted(rows):
        first_release.setdefault(quarter, rows[date, quarter])
    if any(not item["vintage"].startswith(("Advance", "Initial")) for item in first_release.values()):
        raise ValueError("BEA 发布表缺少季度的首批估计")
    return rows


def parse_philly(raw: bytes) -> dict[tuple[str, str], dict]:
    sheet = _xlsx_rows(raw, 1)
    header = next(sheet)
    months = {}
    for column, title in header.items():
        match = re.fullmatch(r"NOUTPUT(\d{2})M(\d{1,2})", title)
        if match:
            year, month = map(int, match.groups())
            year += 1900 if year >= 65 else 2000
            if (1991, 12) <= (year, month) <= (2002, 3):
                months[column] = dt.date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    if len(months) != 124:
        raise ValueError("费城联储 GDP 暖机缺少月度 vintage")
    latest = {}
    for row in sheet:
        quarter = row.get("A", "").replace(":", "")
        if not re.fullmatch(r"\d{4}Q[1-4]", quarter):
            continue
        for column in months.keys() & row.keys():
            cell = row[column]
            if cell not in {"", "NA", "nan", "#N/A", "."} and quarter > latest.get(column, ("", 0))[0]:
                latest[column] = (quarter, _valid_gdp(cell))
    if months.keys() != latest.keys():
        raise ValueError("费城联储 GDP 暖机有空的月度 vintage")
    return {
        (months[col], quarter): dict(zip(FIELDS, (months[col], quarter, gdp, header[col])))
        for col, (quarter, gdp) in latest.items()
    }


def read_vintages(path: Path) -> dict[tuple[str, str], dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream)
        if tuple(rows.fieldnames or ()) != FIELDS:
            raise ValueError(f"{path.name} 的 GDP 表头不匹配")
        records = {}
        for row in rows:
            row["gdp_billions_saar"] = _valid_gdp(row["gdp_billions_saar"])
            key = (row["available_date"], row["quarter"])
            if key in records:
                raise ValueError(f"{path.name} 的 GDP 发布记录重复: {key}")
            records[key] = row
        return records


def vintage_csv(records) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(records[key] for key in sorted(records))
    return stream.getvalue().encode("utf-8")


if __name__ == "__main__":
    # 用官方 XLSX 重建暖机 CSV。日常更新由 fetch_data.py 负责。
    from fetch_data import _atomic_write, _get

    sys.stdout.reconfigure(encoding="utf-8")
    cache = Path(__file__).resolve().parent / "backtest_outputs" / "cache"
    data = parse_philly(_get(PHILLY_GDP_URL, timeout=20))
    _atomic_write(cache / GDP_WARMUP, vintage_csv(data))
    print(f"费城联储 GDP 暖机: {len(data)} 个月度 vintage")
