"""ユーザーが持っているエクセル（.xlsx）の表の末尾に、読み取り結果の行を追記して返す。

- ファイルはメモリ上だけで扱い、ディスクには書かない。返したら捨てる。ログにはサイズと行数だけを出す。
- 見出しの名前から列を自動で対応づける（inspect）。対応は画面で直せる（append の mapping）。
- 直前の行の書式（罫線・表示形式など）をまねる。対応づけていない列に数式があれば、行をずらして引き継ぐ。
- openpyxl は、グラフ・画像・ピボットテーブルなどを保存し直すときに落とすことがある。そうした部品を
  含むファイルは warnings で知らせる。返すファイルは別名にして、元のファイルは残してもらう前提にする。

ここは「読み取り結果の表（見出し＋行）→ 書き出し先」の書き出し先の1つ。会計ソフト用CSV（B）や
Google スプレッドシート（D）も、同じ「見出し＋行」を受け取る形で足す。
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from copy import copy
from datetime import date, datetime

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024  # 圧縮爆弾よけ
MAX_ROWS = 5000
HEADER_SCAN_ROWS = 30

# 同じ意味の見出しのまとまり。こちらの列名と相手の見出しが同じまとまりに入っていれば対応づける
SYNONYMS = [
    ["日付", "日時", "発行日", "取引日", "年月日", "利用日", "購入日", "伝票日付", "date"],
    ["金額", "合計", "税込", "税込金額", "支払金額", "金額税込", "合計税込", "売上金額", "売上", "amount", "total"],
    ["税抜", "税抜金額", "本体価格", "本体"],
    ["消費税", "税額", "消費税額", "tax"],
    ["税率"],
    ["支払先", "取引先", "店名", "相手先", "購入先", "支払相手", "店舗", "お店"],
    ["勘定科目", "科目", "費目"],
    ["登録番号", "インボイス", "インボイス番号", "適格請求書", "t番号"],
    ["宛名", "顧客", "顧客名", "得意先", "お客様", "お客様名", "取引先名"],
    ["但し書き", "摘要", "内容", "備考", "用途"],
    ["売上区分", "区分", "売上種別"],
    ["領収書番号", "レシートno", "レシート番号", "伝票番号", "番号", "no", "取引番号"],
    ["支払方法", "決済方法", "支払", "支払手段", "決済"],
    ["種類", "種別", "取引種別"],
    ["品名", "商品名", "商品", "品目"],
    ["単価", "価格"],
    ["数量", "個数", "点数"],
    ["会社名", "会社", "企業名", "所属"],
    ["氏名", "名前", "担当者"],
    ["役職", "肩書"],
    ["部署", "部門"],
    ["住所", "所在地"],
    ["電話", "電話番号", "tel"],
    ["メール", "メールアドレス", "email", "e-mail"],
    ["順位"], ["構成比"], ["累計構成比"], ["ランク", "abc"],
]

# 保存し直すと失われることがある部品（zip 内のパス）
_LOSSY_PARTS = {
    "xl/charts/": "グラフ",
    "xl/drawings/": "図形・画像",
    "xl/media/": "画像",
    "xl/pivotTables/": "ピボットテーブル",
    "xl/vbaProject.bin": "マクロ",
    "xl/externalLinks/": "他のファイルへのリンク",
}


class XlsxError(Exception):
    """ユーザーに見せてよいメッセージだけを持つ。"""


def _norm(s) -> str:
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = re.sub(r"\(.*?\)|（.*?）|[\s.・:：/／]", "", s)
    return s


_GROUP = {_norm(w): i for i, words in enumerate(SYNONYMS) for w in words}


def _group(s) -> int | None:
    n = _norm(s)
    if n in _GROUP:
        return _GROUP[n]
    for w, g in _GROUP.items():  # 「金額（円）」「支払先名」のような言い回しの違い
        if len(w) >= 2 and (n.startswith(w) or n.endswith(w)):
            return g
    return None


def _match(theirs: str, ours: list[str]) -> str | None:
    n, g = _norm(theirs), _group(theirs)
    if not n:
        return None
    for o in ours:
        if _norm(o) == n:
            return o
    if g is not None:
        for o in ours:
            if _group(o) == g:
                return o
    return None


def _open(data: bytes):
    from openpyxl import load_workbook

    if len(data) > MAX_FILE_BYTES:
        raise XlsxError(f"エクセルファイルは{MAX_FILE_BYTES // 1024 // 1024}MBまでです")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED_BYTES:
                raise XlsxError("エクセルファイルの中身が大きすぎます")
        wb = load_workbook(io.BytesIO(data))
    except XlsxError:
        raise
    except Exception as e:
        raise XlsxError("エクセルファイル（.xlsx）として開けませんでした") from e
    warnings = sorted({label for part, label in _LOSSY_PARTS.items() if any(n.startswith(part) for n in names)})
    return wb, warnings


def _header_row(ws, ours: list[str]) -> int:
    best, best_score = None, 0
    fallback = None
    for r in range(1, min(ws.max_row, HEADER_SCAN_ROWS) + 1):
        texts = [c.value for c in ws[r] if isinstance(c.value, str) and c.value.strip()]
        score = sum(1 for t in texts if _match(t, ours))
        if score > best_score:
            best, best_score = r, score
        if fallback is None and len(texts) >= 2:
            fallback = r
    return best or fallback or 1


def _headers(ws, row: int) -> list[dict]:
    return [{"col": c.column, "text": str(c.value).strip()} for c in ws[row] if c.value is not None and str(c.value).strip()]


def _last_row(ws, header_row: int, cols: list[int]) -> int:
    last = header_row
    for r in range(header_row + 1, ws.max_row + 1):
        if any(ws.cell(r, c).value not in (None, "") for c in cols):
            last = r
    return last


def inspect(data: bytes, ours: list[str]) -> dict:
    """シートごとに、見出しの行・見出し・最後の行・おすすめの対応（相手の列番号 → こちらの列名）を返す。"""
    wb, warnings = _open(data)
    sheets = []
    for ws in wb.worksheets:
        hr = _header_row(ws, ours)
        headers = _headers(ws, hr)
        suggested, used = {}, set()
        for h in headers:
            m = _match(h["text"], ours)
            if m and m not in used:
                suggested[h["col"]] = m
                used.add(m)
        sheets.append({"name": ws.title, "header_row": hr, "headers": headers,
                       "last_row": _last_row(ws, hr, [h["col"] for h in headers]) if headers else hr,
                       "suggested": suggested})
    active = wb.active.title if wb.active is not None else (sheets[0]["name"] if sheets else None)
    return {"sheets": sheets, "active": active, "warnings": warnings}


_NUM = re.compile(r"^-?[\d,]+(\.\d+)?$")
_PCT = re.compile(r"^-?\d+(\.\d+)?%$")


def _convert(v: str):
    v = (v or "").strip()
    if not v:
        return None, None
    s = re.sub(r"[¥円\s]", "", v)
    if _NUM.match(s) and not (len(s) > 1 and s.startswith("0") and "." not in s):  # 「0001」のような番号は文字のまま
        n = float(s.replace(",", ""))
        return (int(n) if n.is_integer() else n), None
    if _PCT.match(s):
        return float(s[:-1]) / 100, "0.0%"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        try:
            return date.fromisoformat(v), "yyyy/mm/dd"
        except ValueError:
            pass
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", v):
        try:
            return datetime.strptime(v, "%Y-%m-%d %H:%M"), "yyyy/mm/dd hh:mm"
        except ValueError:
            pass
    return v, None


def append(data: bytes, sheet: str, header_row: int, mapping: dict[int, str], header: list[str],
           rows: list[list[str]]) -> tuple[bytes, dict]:
    """mapping（相手の列番号 → こちらの列名）に従って、表の最後の行の下に rows を書き足す。"""
    from openpyxl.formula.translate import Translator
    from openpyxl.utils import get_column_letter, range_boundaries

    if not rows:
        raise XlsxError("追記する行がありません")
    if len(rows) > MAX_ROWS:
        raise XlsxError(f"一度に追記できるのは{MAX_ROWS}行までです")
    index = {h: i for i, h in enumerate(header)}
    mapping = {int(c): o for c, o in mapping.items() if o in index and int(c) >= 1}
    if not mapping:
        raise XlsxError("書き込む列が1つも選ばれていません")
    wb, warnings = _open(data)
    if sheet not in wb.sheetnames:
        raise XlsxError("シートが見つかりません")
    ws = wb[sheet]
    header_cols = [h["col"] for h in _headers(ws, header_row)] or list(mapping)
    last = _last_row(ws, header_row, sorted(set(header_cols) | set(mapping)))
    template = last if last > header_row else None
    all_cols = range(1, max([ws.max_column, *mapping]) + 1)

    for i, row in enumerate(rows):
        r = last + 1 + i
        for c in all_cols:
            cell = ws.cell(r, c)
            src = ws.cell(template, c) if template else None
            if src is not None and src.has_style:
                cell.font, cell.border, cell.fill = copy(src.font), copy(src.border), copy(src.fill)
                cell.number_format, cell.alignment, cell.protection = src.number_format, copy(src.alignment), copy(src.protection)
            if c in mapping:
                value, fmt = _convert(row[index[mapping[c]]] if index[mapping[c]] < len(row) else "")
                cell.value = value
                if fmt and (src is None or src.number_format == "General"):
                    cell.number_format = fmt
            elif src is not None and isinstance(src.value, str) and src.value.startswith("="):
                # 残高などの数式は、行をずらして引き継ぐ
                cell.value = Translator(src.value, origin=f"{get_column_letter(c)}{template}").translate_formula(
                    f"{get_column_letter(c)}{r}")

    # 「テーブル」として書式設定された範囲は、追記した行まで広げる
    new_last = last + len(rows)
    for tbl in ws.tables.values():
        min_col, min_row, max_col, max_row = range_boundaries(tbl.ref)
        if min_row == header_row and max_row >= last:
            tbl.ref = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{max(max_row, new_last)}"
            if tbl.autoFilter is not None:
                tbl.autoFilter.ref = tbl.ref

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), {"sheet": sheet, "first_row": last + 1, "appended": len(rows), "warnings": warnings}
