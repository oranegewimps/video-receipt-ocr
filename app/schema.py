"""モードごとの出力JSONスキーマとプロンプト。

モード:
  expense  経費精算（受け取った領収書・レシート・請求書）
  sales    売上管理（自分が発行した領収書。発行者情報は設定値を使う）
  bizcard  名刺
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

MODES = ("expense", "sales", "bizcard")

ACCOUNT_TITLES = [
    "旅費交通費", "会議費", "接待交際費", "消耗品費", "通信費", "新聞図書費",
    "車両費", "地代家賃", "水道光熱費", "支払手数料", "広告宣伝費", "福利厚生費",
    "外注費", "研修費", "雑費",
]

PAYMENT_METHODS = ["現金", "クレジットカード", "電子マネー", "QRコード決済", "銀行振込", "その他"]

# (項目名, 型, 説明)。型はすべて null 許容にする
_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "expense": [
        ("date", "string", "発行日・取引日（YYYY-MM-DD）"),
        ("account_title", "string", "勘定科目の候補（候補リストから1つ）"),
        ("payee", "string", "支払先（発行元の店名・会社名）"),
        ("amount", "integer", "支払総額（税込、円、整数）"),
        ("invoice_number", "string", "発行元のインボイス登録番号（印字どおり。例 T1234567890123）"),
    ],
    "sales": [
        ("issue_date", "string", "発行日（YYYY-MM-DD）"),
        ("customer_name", "string", "宛名（「上様」なども書かれているとおり。空欄なら null）"),
        ("amount_total", "integer", "金額・税込（円、整数）"),
        ("amount_excl_tax", "integer", "税抜金額（円、整数）。書かれている場合のみ"),
        ("tax_amount", "integer", "消費税額（円、整数）。書かれている場合のみ"),
        ("tax_rate", "string", "税率（例 10% / 8% / 10%,8%）。書かれている場合のみ"),
        ("purpose", "string", "但し書き（「お品代として」等、書かれているとおり）"),
        ("sales_category", "string", "売上区分の候補（候補リストから1つ）"),
        ("receipt_no", "string", "領収書番号・伝票番号"),
        ("payment_method", "string", "支払方法（候補リストから1つ）"),
        ("issuer_name_in_video", "string", "動画に写っている発行者名（照合用。読めなければ null）"),
        ("issuer_invoice_no_in_video", "string", "動画に写っている発行者の登録番号（照合用。読めなければ null）"),
    ],
    "bizcard": [
        ("company", "string", "会社名"),
        ("name", "string", "氏名"),
        ("title", "string", "役職"),
        ("department", "string", "部署"),
        ("address", "string", "住所"),
        ("phone", "string", "電話番号（代表と直通があれば直通を優先）"),
        ("email", "string", "メールアドレス"),
    ],
}

_DOC_TYPES: dict[str, list[str] | None] = {
    "expense": ["receipt", "invoice"],
    "sales": ["receipt", "invoice"],
    "bizcard": None,
}

# 推定してよい項目（推定した場合は low_confidence_fields に入れさせる）
INFERABLE = {"expense": ["account_title"], "sales": ["sales_category"], "bizcard": []}


def fields(mode: str) -> list[str]:
    return [name for name, _, _ in _FIELDS[mode]]


def response_schema(mode: str) -> dict:
    props: dict = {}
    if _DOC_TYPES[mode]:
        props["doc_type"] = {"type": "string", "enum": _DOC_TYPES[mode],
                             "description": "receipt=領収書・レシート, invoice=請求書"}
    props["first_seen"] = {"type": ["string", "null"], "description": "その書類が最初に映った動画内の時刻（MM:SS）"}
    for name, typ, desc in _FIELDS[mode]:
        props[name] = {"type": [typ, "null"], "description": desc}
    props["low_confidence_fields"] = {
        "type": "array",
        "items": {"type": "string", "enum": fields(mode)},
        "description": "推定した項目、または値を入れたが読み取りに自信がない項目の名前",
    }
    doc = {"type": "object", "properties": props, "required": list(props)}
    return {
        "type": "object",
        "properties": {"documents": {"type": "array", "items": doc}},
        "required": ["documents"],
    }


def _common_rules(inferable: str | None) -> str:
    rules = [
        "- 同じ書類が複数のフレームに映っていても、1件として出力する（重複させない）。別の書類かどうかは、番号・金額・日付・名前などで判断する。",
        "- 読めない項目・書類に書かれていない項目は、推測せず null にする。",
    ]
    if inferable:
        rules.append(f"- ただし {inferable} は、業種や但し書きなどから推定してよい。推定した場合は、その項目名を必ず low_confidence_fields に入れる。判断材料がなければ null にする。")
    rules += [
        "- 値は入れたが読み取りに確信が持てない項目は、low_confidence_fields に入れる。",
        "- 日付は YYYY-MM-DD 形式にする。和暦は西暦に直す（例：令和6年→2024年）。年が読めない場合は null にする。",
        "- 金額は円単位の整数にする（カンマや「¥」「円」は付けない）。",
    ]
    return "\n".join(rules)


def prompt(mode: str, settings: Settings | None = None) -> str:
    if mode == "expense":
        body = f"""この動画には、受け取った領収書・レシート・請求書（経費精算用）が映っています。映っている書類をすべて読み取り、指定のJSONスキーマで出力してください。名刺など、それ以外の書類は出力しません。

ルール:
{_common_rules("account_title（勘定科目）")}
- amount は支払総額（税込）を入れる。小計や税額は入れない。
- invoice_number は印字されているとおりに書き写す（T＋13桁）。桁を補ったり推測したりしない。
- account_title の候補: {"、".join(ACCOUNT_TITLES)}"""
    elif mode == "sales":
        categories = settings.sales_categories if settings else []
        body = f"""この動画には、自分（発行者）が顧客に発行した領収書の控え・写しが映っています（売上管理用）。映っている領収書をすべて読み取り、指定のJSONスキーマで出力してください。

ルール:
{_common_rules("sales_category（売上区分）")}
- customer_name は宛名を書かれているとおりに入れる。「上様」などもそのまま入れる。空欄なら null にする。
- amount_total は税込の合計金額にする。amount_excl_tax・tax_amount・tax_rate は、書かれている場合だけ入れ、計算で補わない。
- purpose は但し書きを書かれているとおりに入れる。
- receipt_no は領収書番号・伝票番号を書かれているとおりに入れる。
- 発行者の情報は別に管理しているので、照合用に issuer_name_in_video と issuer_invoice_no_in_video だけを、読める場合に入れる。
- sales_category の候補: {"、".join(categories)}
- payment_method の候補: {"、".join(PAYMENT_METHODS)}"""
    elif mode == "bizcard":
        body = f"""この動画には名刺が映っています。映っている名刺をすべて読み取り、指定のJSONスキーマで出力してください。名刺以外の書類は出力しません。

ルール:
{_common_rules(None)}
- 1枚の名刺に複数の電話番号がある場合は、直通（なければ代表）を1つ入れる。"""
    else:
        raise ValueError(f"unknown mode: {mode}")
    return body + "\n- 書類以外（人物・背景など）については出力しない。\n"
