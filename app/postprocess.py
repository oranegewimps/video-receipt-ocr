"""モデル出力の整形：モード別の重複排除と各種チェック。"""
from __future__ import annotations

import re
import unicodedata

from . import invoice
from .config import Settings
from .schema import fields


def _norm(s) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = re.sub(r"株式会社|有限会社|合同会社|\(株\)|\(有\)|御中|様", "", s)
    s = re.sub(r"[\s・･,.。()#№]", "", s)
    return re.sub(r"^no", "", s)  # 「No.0101」と「0101」をそろえる


def _key(doc: dict, mode: str) -> tuple | None:
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
                if base.get(f) is None and d.get(f) is not None:
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
    return docs
