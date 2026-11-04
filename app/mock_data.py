"""GEMINI_MODE=mock のときに extract() が返す固定のサンプル（架空のデータ）。

各モードに「別フレームで再検出された重複」を1件ずつ含め、重複排除で統合されることを確認できるようにしている。
"""

_NULL_EXPENSE = {"date": None, "account_title": None, "payee": None, "amount": None, "invoice_number": None}

EXPENSE = [
    {**_NULL_EXPENSE, "doc_type": "receipt", "first_seen": "00:03",
     "date": "2026-10-28", "account_title": "会議費", "payee": "カフェ・サンプル 駅前店",
     "amount": 1320, "invoice_number": "T1234567890123", "low_confidence_fields": ["account_title"]},
    # 上と同じレシートの再検出（統合される）
    {**_NULL_EXPENSE, "doc_type": "receipt", "first_seen": "00:05",
     "date": "2026-10-28", "payee": "カフェ・サンプル駅前店", "amount": 1320, "low_confidence_fields": []},
    {**_NULL_EXPENSE, "doc_type": "invoice", "first_seen": "00:12",
     "date": "2026-10-31", "account_title": "外注費", "payee": "サンプルデザイン株式会社",
     "amount": 55000, "invoice_number": "T98765", "low_confidence_fields": ["account_title", "invoice_number"]},
]

_NULL_SALES = {
    "issue_date": None, "customer_name": None, "amount_total": None, "amount_excl_tax": None,
    "tax_amount": None, "tax_rate": None, "purpose": None, "sales_category": None, "receipt_no": None,
    "payment_method": None, "issuer_name_in_video": None, "issuer_invoice_no_in_video": None,
}

SALES = [
    {**_NULL_SALES, "doc_type": "receipt", "first_seen": "00:02",
     "issue_date": "2026-10-30", "customer_name": "上様", "amount_total": 3300,
     "amount_excl_tax": 3000, "tax_amount": 300, "tax_rate": "10%", "purpose": "お品代として",
     "sales_category": "商品売上", "receipt_no": "No.0101", "payment_method": "現金",
     "issuer_name_in_video": "見本商店", "issuer_invoice_no_in_video": "T1111111111111",
     "low_confidence_fields": ["sales_category"]},
    # 上と同じ領収書の再検出（領収書番号が一致するので統合される）
    {**_NULL_SALES, "doc_type": "receipt", "first_seen": "00:04",
     "issue_date": "2026-10-30", "amount_total": 3300, "receipt_no": "No.0101",
     "low_confidence_fields": []},
    {**_NULL_SALES, "doc_type": "receipt", "first_seen": "00:09",
     "issue_date": "2026-10-31", "customer_name": "株式会社テスト商事 御中", "amount_total": 16500,
     "amount_excl_tax": 15000, "tax_amount": 1600, "tax_rate": "10%", "purpose": "施工費として",
     "sales_category": "工事売上", "receipt_no": "No.0102", "payment_method": "銀行振込",
     # 発行者名が設定値と違う（要確認になる）。税抜+税額も合計と合わない（要確認になる）
     "issuer_name_in_video": "別名義工房", "issuer_invoice_no_in_video": None,
     "low_confidence_fields": ["sales_category"]},
]

_NULL_CARD = {"company": None, "name": None, "title": None, "department": None,
              "address": None, "phone": None, "email": None}

BIZCARD = [
    {**_NULL_CARD, "first_seen": "00:01",
     "company": "株式会社テスト商事", "name": "見本 太郎", "title": "営業部長", "department": "営業本部",
     "address": "東京都千代田区サンプル町1-2-3", "phone": "03-0000-0000", "email": "taro@example.com",
     "low_confidence_fields": []},
    # 上と同じ名刺の再検出（メールが一致するので統合される）
    {**_NULL_CARD, "first_seen": "00:03",
     "company": "テスト商事", "name": "見本太郎", "email": "Taro@example.com", "low_confidence_fields": []},
    {**_NULL_CARD, "first_seen": "00:08",
     "company": "例示工業株式会社", "name": "架空 花子", "department": "総務課", "phone": "06-0000-0000",
     "low_confidence_fields": ["name"]},
]

def _item(name, unit_price, quantity, amount, low=False):
    return {"name": name, "unit_price": unit_price, "quantity": quantity, "amount": amount, "low_confidence": low}


_NULL_JOURNAL = {"txn_datetime": None, "receipt_no": None, "txn_type": "sale", "amount_total": None,
                 "tax_amount": None, "tax_rate": None, "payment_method": None, "items": []}

JOURNAL = [
    {**_NULL_JOURNAL, "first_seen": "00:02", "txn_datetime": "2026-11-01 10:05", "receipt_no": "0001",
     "amount_total": 1080, "tax_amount": 80, "tax_rate": "8%", "payment_method": "現金",
     "items": [_item("食パン", 300, 2, 600), _item("牛乳", 240, 2, 480)], "low_confidence_fields": []},
    # ロールを送る途中で同じ取引が再び映ったもの（レシート番号が一致するので統合される）
    {**_NULL_JOURNAL, "first_seen": "00:04", "txn_datetime": "2026-11-01 10:05", "receipt_no": "0001",
     "amount_total": 1080, "items": [], "low_confidence_fields": []},
    {**_NULL_JOURNAL, "first_seen": "00:07", "txn_datetime": "2026-11-01 11:42", "receipt_no": "0002",
     "amount_total": 1700, "tax_amount": 125, "tax_rate": "8%,10%", "payment_method": "電子マネー",
     "items": [_item("コーヒー豆", 1200, 1, 1200), _item("紙袋", 50, 1, 50), _item(None, None, 1, 500, True),
               _item("値引", None, None, -50)],
     "low_confidence_fields": ["items"]},
    {**_NULL_JOURNAL, "first_seen": "00:11", "txn_datetime": "2026-11-01 12:10", "receipt_no": "0003",
     "txn_type": "refund", "amount_total": 600, "payment_method": "現金",
     "items": [_item("食パン", 300, 2, 600)], "low_confidence_fields": []},
    {**_NULL_JOURNAL, "first_seen": "00:14", "txn_datetime": "2026-11-01 13:30", "receipt_no": "0004",
     "amount_total": 720, "tax_amount": 53, "tax_rate": "8%", "payment_method": "QRコード決済",
     "items": [_item("食パン", 300, 1, 300), _item("牛乳", 240, 1, 240), _item("ジャム", 180, 1, 180)],
     "low_confidence_fields": []},
]

# 取引の合計は 1080 + 1700 - 600 + 720 = 2900、件数（通常の売上）は 3。
# レポートは 3,400 円・4件と印字されている想定（読み落としがある例として、照合で「要確認」になる）
JOURNAL_REPORTS = [
    {"report_date": "2026-11-01", "sales_total": 3400, "txn_count": 4, "first_seen": "00:18"},
]

DOCUMENTS = {"expense": EXPENSE, "sales": SALES, "bizcard": BIZCARD, "journal": JOURNAL}
