"""モデル出力の整形：モード別の重複排除と各種チェック。"""
from __future__ import annotations

import re
import unicodedata

from . import invoice
from .config import Settings
from .schema import TXN_TYPES, fields


def _norm(s) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = re.sub(r"株式会社|有限会社|合同会社|\(株\)|\(有\)|御中|様", "", s)
    s = re.sub(r"[\s・･,.。()#№]", "", s)
    return re.sub(r"^no", "", s)  # 「No.0101」と「0101」をそろえる


def _key(doc: dict, mode: str) -> tuple | None:
    if mode == "journal":
        day = (doc.get("txn_datetime") or "")[:10]
        if doc.get("receipt_no"):
            return ("no", day, _norm(doc["receipt_no"]))
        if doc.get("txn_datetime") and doc.get("amount_total") is not None:
            return ("bill", doc["txn_datetime"], doc["amount_total"])
        return None
    if mode == "bizcard":
        if doc.get("email"):
            return ("email", _norm(doc["email"]))
        if doc.get("name"):
            return ("name", _norm(doc["name"]), _norm(doc.get("company")))
        return None
    if mode == "sales":
        if doc.get("receipt_no"):
            return ("no", _norm(doc["receipt_no"]))
        if doc.get("amount_total") is not None and (doc.get("issue_date") or doc.get("customer_name")):
            return ("bill", doc["amount_total"], doc.get("issue_date") or "", _norm(doc.get("customer_name")))
        return None
    if doc.get("amount") is not None and (doc.get("date") or doc.get("payee")):
        return ("bill", doc["amount"], doc.get("date") or "", _norm(doc.get("payee")))
    return None


def dedupe(docs: list[dict], mode: str) -> list[dict]:
    """モデルが統合しきれなかった重複を、内容の一致でまとめる。先に出た方を残し、空欄を後の方で埋める。"""
    names = fields(mode)
    out: list[dict] = []
    index: dict[tuple, dict] = {}
    for d in docs:
        k = _key(d, mode)
        if k is not None and k in index:
            base = index[k]
            for f in names:
                if base.get(f) in (None, []) and d.get(f) not in (None, []):
                    base[f] = d[f]
                    if f in (d.get("low_confidence_fields") or []):
                        base["low_confidence_fields"].append(f)
            base["merged_count"] = base.get("merged_count", 1) + 1
            continue
        d = dict(d)
        d["low_confidence_fields"] = list(d.get("low_confidence_fields") or [])
        out.append(d)
        if k is not None:
            index[k] = d
    return out


def _flag(d: dict, field: str) -> None:
    if field not in d["low_confidence_fields"]:
        d["low_confidence_fields"].append(field)


def _check_issuer(d: dict, settings: Settings) -> str:
    """動画の発行者表示と設定値を照合する。'ok' | 'mismatch' | 'not_read' | 'unset'"""
    seen_name, seen_no = d.get("issuer_name_in_video"), d.get("issuer_invoice_no_in_video")
    if not settings.self_name and not settings.self_invoice_no:
        return "unset"
    if not seen_name and not seen_no:
        return "not_read"
    mismatch = False
    if seen_name and settings.self_name:
        a, b = _norm(seen_name), _norm(settings.self_name)
        if a not in b and b not in a:  # 「見本商店」と「見本商店 本店」のような部分一致は許容
            mismatch = True
            _flag(d, "issuer_name_in_video")
    if seen_no and settings.self_invoice_no and invoice.normalize(seen_no) != settings.self_invoice_no:
        mismatch = True
        _flag(d, "issuer_invoice_no_in_video")
    return "mismatch" if mismatch else "ok"


def finalize(docs: list[dict], mode: str, settings: Settings) -> list[dict]:
    docs = dedupe([d for d in docs if isinstance(d, dict)], mode)
    for d in docs:
        d.setdefault("merged_count", 1)

    if mode == "expense":
        for d in docs:
            d["invoice_check"] = invoice.check_format(d.get("invoice_number"))
            if d["invoice_check"] == "ok":
                d["invoice_number"] = invoice.normalize(d["invoice_number"])
            d["invoice_lookup"] = None
        # 国税庁照合は live かつアプリIDがある場合だけ（mock では外部通信しない）
        if settings.is_live and settings.nta_app_id:
            numbers = sorted({d["invoice_number"] for d in docs if d["invoice_check"] == "ok"})
            found = invoice.lookup(numbers, settings)
            for d in docs:
                if d["invoice_check"] == "ok":
                    d["invoice_lookup"] = found.get(d["invoice_number"])

    elif mode == "sales":
        for d in docs:
            d["issuer_check"] = _check_issuer(d, settings)
            # 発行者の情報は動画からではなく設定値を使う
            d["issuer_name"] = settings.self_name or None
            d["issuer_invoice_no"] = settings.self_invoice_no or None
            d["issuer_address"] = settings.self_address or None
            # 税抜＋税額＝税込 が読めた範囲で合わなければ要確認にする
            t, e, x = d.get("amount_total"), d.get("amount_excl_tax"), d.get("tax_amount")
            if None not in (t, e, x) and e + x != t:
                for f in ("amount_total", "amount_excl_tax", "tax_amount"):
                    _flag(d, f)
    elif mode == "journal":
        for d in docs:
            if d.get("txn_type") not in TXN_TYPES:
                d["txn_type"] = "sale"
            if isinstance(d.get("amount_total"), int):
                d["amount_total"] = abs(d["amount_total"])
            items = [i for i in (d.get("items") or []) if isinstance(i, dict)]
            d["items"] = items
            if any(i.get("low_confidence") for i in items):
                _flag(d, "items")
            # 品目の合計が、合計（内税）または合計−消費税（外税）と合わなければ要確認にする
            amounts = [i.get("amount") for i in items]
            total, tax = d.get("amount_total"), d.get("tax_amount")
            if items and total is not None and None not in amounts:
                s = sum(amounts)
                if s != total and (tax is None or s + tax != total):
                    _flag(d, "items")
                    _flag(d, "amount_total")
    return docs


def journal_summary(docs: list[dict], reports: list[dict]) -> list[dict]:
    """精算レポートごとに、読み取った取引の合計・件数と照合する。

    合計は 通常の売上 − 返品（取消は数えない）、件数は通常の売上の件数。
    status: ok（一致）| mismatch（読み落とし・読み間違いの可能性）| not_read（レポートの数字が読めない）
    """
    reports = [r for r in reports if isinstance(r, dict)]
    out = []
    for r in reports:
        day = r.get("report_date")
        if day:
            txns = [d for d in docs if (d.get("txn_datetime") or "")[:10] == day]
        elif len(reports) == 1:  # 日付が読めなくても、レポートが1つだけなら全取引と比べる
            txns = docs
        else:
            txns = []
        total = sum((d.get("amount_total") or 0) * {"sale": 1, "refund": -1}.get(d.get("txn_type"), 0) for d in txns)
        count = sum(1 for d in txns if d.get("txn_type") == "sale")
        rt, rc = r.get("sales_total"), r.get("txn_count")
        if rt is None and rc is None:
            status = "not_read"
        elif (rt is not None and rt != total) or (rc is not None and rc != count):
            status = "mismatch"
        else:
            status = "ok"
        out.append({"report_date": day, "report_total": rt, "report_count": rc,
                    "read_total": total, "read_count": count, "status": status})
    return out
