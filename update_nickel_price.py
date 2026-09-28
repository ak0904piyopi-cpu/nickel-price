import csv
import re
from datetime import date
from io import BytesIO
from pathlib import Path

import pdfplumber
import requests

NOSE_INFO_URL = "https://www.nose-sus.co.jp/information/"
FRANKFURTER_URL = "https://api.frankfurter.app"
HISTORY_FILE = Path(__file__).parent / "nickel_history.csv"
FIELDNAMES = ["date", "usd_per_lb", "jpy_per_kg"]

# 能勢鋼材(株)のお知らせ一覧ページから、年ごとのニッケル価格PDFへのリンクを抜き出す。
# 記事の並び(見出し→entrybody内のPDFリンク)というHTML構造に依存しているため、
# サイト側のテンプレートが変わると抽出できなくなる可能性がある。
NICKEL_ENTRY_PATTERN = re.compile(
    r'(\d{4})年\s*LMEニッケル価格推移</a></h2>\s*</div>\s*<div class="entrybody">\s*<p><a href="([^"]+\.pdf)"',
    re.S,
)

LB_TO_KG = 0.45359237


def _extract_pdf_links(html: str) -> list[tuple[int, str]]:
    return [(int(year), url) for year, url in NICKEL_ENTRY_PATTERN.findall(html)]


def _find_nickel_pdfs() -> list[tuple[int, str]]:
    resp = requests.get(NOSE_INFO_URL, timeout=30)
    resp.raise_for_status()
    return _extract_pdf_links(resp.text)


def _rows_from_table(table: list[list[str]], year: int) -> list[dict]:
    rows = []
    for row in table[1:]:
        label = (row[0] or "").strip()
        if not label or "平均" in label:
            continue
        try:
            day = int(label.replace("日", ""))
        except ValueError:
            continue
        for month in range(1, 13):
            col = 1 + (month - 1) * 2
            if col >= len(row):
                break
            value = (row[col] or "").strip()
            if not value or value == "-":
                continue
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            try:
                rows.append({"date": d.isoformat(), "usd_per_lb": float(value)})
            except ValueError:
                continue
    return rows


def _parse_nickel_pdf(pdf_bytes: bytes, year: int) -> list[dict]:
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        table = pdf.pages[0].extract_tables()[0]
    return _rows_from_table(table, year)


def fetch_nickel_prices_usd_lb() -> list[dict]:
    """能勢鋼材が公開する年ごとのLMEニッケル価格PDFを全て取得し、日付順にまとめる。
    古い年のリンクは能勢鋼材側でリンク切れ(PDFではなくHTMLが返る)になっていることがあるため、
    PDF以外が返ってきた年はスキップする。"""
    rows: list[dict] = []
    for year, pdf_url in _find_nickel_pdfs():
        resp = requests.get(pdf_url, timeout=30)
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            continue
        rows.extend(_parse_nickel_pdf(resp.content, year))
    rows.sort(key=lambda r: r["date"])
    return rows


def fetch_usdjpy_rates(start: str, end: str) -> dict[str, float]:
    resp = requests.get(f"{FRANKFURTER_URL}/{start}..{end}", params={"from": "USD", "to": "JPY"}, timeout=30)
    resp.raise_for_status()
    return {d: v["JPY"] for d, v in resp.json()["rates"].items()}


def _apply_fx(rows: list[dict], fx: dict[str, float]) -> list[dict]:
    """日付ごとのUSD/Lb価格に、その日以前で最も新しい為替レートを掛けて円/kgを算出する(forward-fill)。"""
    fx_dates = sorted(fx)
    result = []
    last_rate = None
    fx_idx = 0
    for row in rows:
        while fx_idx < len(fx_dates) and fx_dates[fx_idx] <= row["date"]:
            last_rate = fx[fx_dates[fx_idx]]
            fx_idx += 1
        if last_rate is None:
            continue
        jpy_per_kg = row["usd_per_lb"] / LB_TO_KG * last_rate
        result.append({"date": row["date"], "usd_per_lb": row["usd_per_lb"], "jpy_per_kg": round(jpy_per_kg, 1)})
    return result


def build_nickel_price_series() -> list[dict]:
    """能勢鋼材のPDFと為替レートから、日付ごとのニッケル価格(USD/Lb・円/kg)を新規に取得して返す。
    円/kgは為替レートで換算した値であり、ステンレス板そのものの仕入価格ではない点に注意。"""
    rows = fetch_nickel_prices_usd_lb()
    if not rows:
        return []
    fx = fetch_usdjpy_rates(rows[0]["date"], rows[-1]["date"])
    return _apply_fx(rows, fx)


def read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    with HISTORY_FILE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def merge_into_history(rows: list[dict]) -> None:
    """新しく取得した行をnickel_history.csvにマージする。既存の日付の値は上書きしない
    (取得失敗・不完全な結果で既に保存済みの良いデータを壊さないため)。"""
    existing = {r["date"]: r for r in read_history()}
    for row in rows:
        existing.setdefault(
            row["date"],
            {"date": row["date"], "usd_per_lb": row["usd_per_lb"], "jpy_per_kg": row["jpy_per_kg"]},
        )
    merged = sorted(existing.values(), key=lambda r: r["date"])
    with HISTORY_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(merged)


def main():
    rows = build_nickel_price_series()
    merge_into_history(rows)


if __name__ == "__main__":
    main()
