"""モードごとの出力JSONスキーマとプロンプト。

モード:
  expense  経費精算（受け取った領収書・レシート・請求書）
  sales    売上管理（自分が発行した領収書。発行者情報は設定値を使う）
  bizcard  名刺
  journal  レジのジャーナル（控えロール）。1取引＝1件で、品目は items に入れる。精算レポートは reports に入れる
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

MODES = ("expense", "sales", "bizcard", "journal")

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
    "journal": [
        ("txn_datetime", "string", "取引日時（YYYY-MM-DD HH:MM。時刻が読めなければ YYYY-MM-DD）"),
        ("receipt_no", "string", "レシート番号・取引番号"),
        ("txn_type", "string", "取引の種類（sale=通常の売上, refund=返品・返金, void=取消・訂正）"),
        ("amount_total", "integer", "合計（税込、円、整数。返品・取消でも正の数で入れる）"),
        ("tax_amount", "integer", "消費税額（円、整数。内税・外税とも印字されている場合のみ）"),
        ("tax_rate", "string", "税率（例 10% / 8% / 10%,8%）。印字されている場合のみ"),
        ("payment_method", "string", "支払方法（候補リストから1つ）"),
        ("items", "array", "品目の一覧（印字されている順）"),
    ],
}

TXN_TYPES = ["sale", "refund", "void"]

# ジャーナルの品目（1行）
_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"], "description": "品名（印字どおり）"},
        "unit_price": {"type": ["integer", "null"], "description": "単価（円、整数）。印字されている場合のみ"},
        "quantity": {"type": ["integer", "null"], "description": "数量・点数（印字がなければ null）"},
        "amount": {"type": ["integer", "null"], "description": "金額（円、整数）。値引・割引はマイナス"},
        "low_confidence": {"type": "boolean", "description": "この行の読み取りに自信がない場合 true"},
    },
    "required": ["name", "unit_price", "quantity", "amount", "low_confidence"],
}

# ジャーナル末尾などの精算レポート（日計）
_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "report_date": {"type": ["string", "null"], "description": "レポートの対象日（YYYY-MM-DD）"},
        "sales_total": {"type": ["integer", "null"], "description": "売上合計・純売上（税込、円、整数）"},
        "txn_count": {"type": ["integer", "null"], "description": "客数・取引件数"},
        "first_seen": {"type": ["string", "null"], "description": "レポートが最初に映った時刻（MM:SS）"},
    },
    "required": ["report_date", "sales_total", "txn_count", "first_seen"],
}

_DOC_TYPES: dict[str, list[str] | None] = {
    "expense": ["receipt", "invoice"],
    "sales": ["receipt", "invoice"],
    "bizcard": None,
    "journal": None,
}

# 推定してよい項目（推定した場合は low_confidence_fields に入れさせる）
INFERABLE = {"expense": ["account_title"], "sales": ["sales_category"], "bizcard": [], "journal": []}


def fields(mode: str) -> list[str]:
    return [name for name, _, _ in _FIELDS[mode]]


def response_schema(mode: str) -> dict:
    props: dict = {}
    if _DOC_TYPES[mode]:
        props["doc_type"] = {"type": "string", "enum": _DOC_TYPES[mode],
                             "description": "receipt=領収書・レシート, invoice=請求書"}
    props["first_seen"] = {"type": ["string", "null"], "description": "その書類が最初に映った動画内の時刻（MM:SS）"}
    for name, typ, desc in _FIELDS[mode]:
        if typ == "array":
            props[name] = {"type": "array", "items": _ITEM_SCHEMA, "description": desc}
        elif name == "txn_type":
            props[name] = {"type": "string", "enum": TXN_TYPES, "description": desc}
        else:
            props[name] = {"type": [typ, "null"], "description": desc}
    props["low_confidence_fields"] = {
        "type": "array",
        "items": {"type": "string", "enum": fields(mode)},
        "description": "推定した項目、または値を入れたが読み取りに自信がない項目の名前",
    }
    doc = {"type": "object", "properties": props, "required": list(props)}
    out = {
        "type": "object",
        "properties": {"documents": {"type": "array", "items": doc}},
        "required": ["documents"],
    }
    if mode == "journal":
        out["properties"]["reports"] = {"type": "array", "items": _REPORT_SCHEMA,
                                        "description": "精算レポート（日計）。映っていなければ空の配列"}
        out["required"].append("reports")
    return out


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


def _hint_rules(hints: list[dict]) -> str:
    """この利用者が過去に直した読み取り例。手書きの癖（数字や字の崩し方）の手がかりとして渡す。"""
    lines = []
    for h in hints:
        before = f"「{h['original']}」" if h["original"] else "空欄（読めなかった）"
        lines.append(f"- {h['field']}: 読み取り {before} → 正しくは「{h['corrected']}」")
    return (
        "\n\nこの利用者の過去の訂正例（新しい順）:\n" + "\n".join(lines) + "\n"
        "- これらは同じ人の書き癖を知るための参考にする。似た形の文字や数字があれば、この癖を踏まえて読む。\n"
        "- 訂正例の値を、動画に映っていない書類の値としてそのまま書き写さない。必ず実際に映っている文字を読む。"
    )


def prompt(mode: str, settings: Settings | None = None, hints: list[dict] | None = None) -> str:
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
    elif mode == "journal":
        body = f"""この動画には、レジのジャーナル（取引の控えが続けて印字されたロール紙）が映っています。ロールを送りながら撮影しているので、同じ部分が何度も映ります。印字されている取引をすべて読み取り、指定のJSONスキーマで出力してください。

ルール:
{_common_rules(None)}
- 1回の会計（レシート1枚分）を documents の1件にする。区切りは、レシート番号・取引番号・日時・「合計」の行などで判断する。
- txn_type は、通常の売上は sale、「返品」「返金」は refund、「取消」「訂正」「VOID」は void にする。
- amount_total は、その取引の合計（税込）を正の数で入れる。返品・取消でもマイナスにしない（種類は txn_type で表す）。
- items には品目を印字されている順に入れる。「小計」「合計」「預り」「お釣り」「消費税」「対象額」の行は品目に入れない。値引・割引は、金額をマイナスにした品目として入れる。
- 品目の行が読めない場合は、その行の name を null、low_confidence を true にする。読めた品目だけで済ませず、行の数は印字どおりにする。
- 精算レポート・点検レポート（「精算」「日計」「Z」「X」などと印字された、その日の合計の印字）は、取引としてではなく reports に入れる。映っていなければ reports は空にする。
- payment_method の候補: {"、".join(PAYMENT_METHODS)}"""
    else:
        raise ValueError(f"unknown mode: {mode}")
    body += "\n- 書類以外（人物・背景など）については出力しない。"
    if hints:
        body += _hint_rules(hints)
    return body + "\n"
