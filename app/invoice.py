"""インボイス登録番号の形式チェックと、国税庁 適格請求書発行事業者公表システム Web-API による照合（任意）。

Web-API 仕様: https://www.invoice-kohyo.nta.go.jp/web-api/index.html
  GET https://web-api.invoice-kohyo.nta.go.jp/1/num?id=<アプリID>&number=T...&type=21&history=0
  type=21 は JSON 形式。NTA_APP_ID が未設定なら照合はスキップする。
"""
from __future__ import annotations

import logging
import re
import unicodedata

import httpx

from .config import Settings

log = logging.getLogger("vro.invoice")

NTA_URL = "https://web-api.invoice-kohyo.nta.go.jp/1/num"
INVOICE_RE = re.compile(r"^T\d{13}$")


def normalize(value: str | None) -> str | None:
    """全角→半角、空白・ハイフンを除去し、先頭の t を T にそろえる。"""
    if not value:
        return None
    v = unicodedata.normalize("NFKC", value)
    v = re.sub(r"[\s\-‐−ー]", "", v).upper()
    return v or None


def check_format(value: str | None) -> str:
    """'ok' | 'invalid' | 'none'"""
    v = normalize(value)
    if v is None:
        return "none"
    return "ok" if INVOICE_RE.match(v) else "invalid"


def lookup(numbers: list[str], settings: Settings) -> dict[str, dict]:
    """登録番号を照合し {番号: {"status": "registered"|"not_found"|"error", "name": ...}} を返す。

    外部通信を行うため、live モードかつ NTA_APP_ID がある場合だけ呼ばれる。
    """
    if not numbers:
        return {}
    result: dict[str, dict] = {}
    try:
        # 複数番号はカンマ区切りで指定できる。上限が不明なので控えめに10件ずつ送る
        for i in range(0, len(numbers), 10):
            chunk = numbers[i:i + 10]
            r = httpx.get(
                NTA_URL,
                params={"id": settings.nta_app_id, "number": ",".join(chunk), "type": "21", "history": "0"},
                timeout=settings.nta_timeout_sec,
            )
            r.raise_for_status()
            found = {a.get("registratedNumber"): a for a in r.json().get("announcement", [])}
            for n in chunk:
                a = found.get(n)
                if a is None:
                    result[n] = {"status": "not_found", "name": None}
                elif a.get("disposalDate") or a.get("expireDate"):
                    result[n] = {"status": "revoked", "name": a.get("name")}
                else:
                    result[n] = {"status": "registered", "name": a.get("name")}
    except Exception as e:
        log.warning("nta lookup failed: %s", type(e).__name__)
        for n in numbers:
            result.setdefault(n, {"status": "error", "name": None})
    return result
