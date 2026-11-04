import logging
import os
import socket
import time
from types import SimpleNamespace

import pytest

from app import extractor, invoice, media, postprocess, schema
from app.config import Settings, validate
from app.jobs import JobManager
from app.usage import UsageStore

from .conftest import login, make_client, make_video, needs_ffmpeg, upload, wait_job


def uploads_entries(settings):
    return list(settings.uploads_dir.iterdir()) if settings.uploads_dir.exists() else []


# ------------------------------------------------------------------ 認証

def test_requires_login(settings):
    with make_client(settings) as c:
        assert c.get("/api/me").status_code == 401
        assert c.post("/api/jobs", content=b"x", headers={"X-Filename": "a.mp4"}).status_code == 401


def test_wrong_code_rejected(settings):
    with make_client(settings) as c:
        assert c.post("/api/login", json={"code": "nope"}).status_code == 401
        login(c)
        assert c.get("/api/me").json()["user"] == "alice"


def test_admin_usage_requires_token(settings):
    with make_client(settings) as c:
        assert c.get("/admin/usage").status_code == 403
        r = c.get("/admin/usage", headers={"X-Admin-Token": "admin-token"})
        assert r.status_code == 200 and "rows" in r.json()


# ------------------------------------------------------------------ 一連の流れ（mock）

@needs_ffmpeg
def test_full_flow_mock_and_video_deleted(settings, short_video, monkeypatch, caplog):
    # mock モードでは外部への通信が一切起きないことも確認する
    # （イベントループ内部のループバック接続だけは許可する）
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo
    local = ("127.0.0.1", "::1", "localhost")

    def guarded_connect(self, addr, *a, **k):
        if isinstance(addr, tuple) and addr[0] not in local:
            raise AssertionError(f"network access in mock mode: {addr[0]}")
        return real_connect(self, addr, *a, **k)

    def guarded_getaddrinfo(host, *a, **k):
        if host not in local:
            raise AssertionError(f"DNS lookup in mock mode: {host}")
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)

    caplog.set_level(logging.INFO)
    with make_client(settings) as c:
        login(c)
        r = upload(c, short_video, mode="expense")
        assert r.status_code == 202, r.text
        job = wait_job(c, r.json()["job_id"])
        assert job["status"] == "done", job

    assert job["mode"] == "expense"
    docs = job["documents"]
    assert len(docs) == 2  # 重複レシートは1件に統合される
    receipt = next(d for d in docs if d["doc_type"] == "receipt")
    assert receipt["merged_count"] == 2
    assert receipt["invoice_check"] == "ok"
    assert "account_title" in receipt["low_confidence_fields"]  # 推定した勘定科目は要確認
    assert next(d for d in docs if d["doc_type"] == "invoice")["invoice_check"] == "invalid"

    # 動画（作業ディレクトリ）が残っていない
    assert uploads_entries(settings) == []
    # ログに抽出内容が出ていない
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "カフェ" not in text and "example.com" not in text and "T1234567890123" not in text


@needs_ffmpeg
def test_failure_still_deletes_video(settings, short_video):
    seen = {}

    def boom(path, mode, settings=None):
        seen["exists_during"] = os.path.exists(path)
        raise RuntimeError("api down")

    with make_client(settings, extract_fn=boom) as c:
        login(c)
        job = wait_job(c, upload(c, short_video).json()["job_id"])
    assert job["status"] == "error"
    assert seen["exists_during"] is True
    assert uploads_entries(settings) == []


@needs_ffmpeg
def test_other_user_cannot_read_job(settings, short_video):
    with make_client(settings) as c:
        login(c, "code-a")
        job_id = upload(c, short_video).json()["job_id"]
        wait_job(c, job_id)
        login(c, "code-b")
        assert c.get(f"/api/jobs/{job_id}").status_code == 404


# ------------------------------------------------------------------ 入力チェック

@needs_ffmpeg
def test_rejects_non_video_and_cleans_up(settings, tmp_path):
    with make_client(settings) as c:
        login(c)
        r = c.post("/api/jobs", content=b"not a video", headers={"X-Filename": "x.mp4"})
        assert r.status_code == 400
        assert c.get("/api/me").json()["remaining_today"] == 3  # 不正ファイルは回数に数えない
    assert uploads_entries(settings) == []


def test_rejects_extension(settings):
    with make_client(settings) as c:
        login(c)
        assert c.post("/api/jobs", content=b"x", headers={"X-Filename": "x.avi"}).status_code == 400


def test_rejects_too_large(settings):
    settings.max_upload_mb = 1
    with make_client(settings) as c:
        login(c)
        r = c.post("/api/jobs", content=b"0" * (1024 * 1024 + 10), headers={"X-Filename": "x.mp4"})
        assert r.status_code == 413
    assert uploads_entries(settings) == []


@needs_ffmpeg
def test_long_video_trimmed_and_audio_stripped(settings, tmp_path):
    settings.max_duration_sec = 6
    long_video = make_video(tmp_path / "long.mp4", 10, audio=True)
    seen = {}

    def capture(path, mode, settings=None):
        seen["probe"] = media.probe(path, settings)
        return {"documents": [], "model": "m", "usage": {}, "mock": True}

    with make_client(settings, extract_fn=capture) as c:
        login(c)
        r = upload(c, long_video)
        assert "先頭0分06秒のみ" in r.json()["notice"]
        job = wait_job(c, r.json()["job_id"])
    assert job["status"] == "done" and job["notice"]
    # 再エンコードせずに切るため、キーフレーム位置により1秒程度はみ出すことがある
    assert seen["probe"].duration_sec <= 7.5
    assert seen["probe"].has_audio is False


# ------------------------------------------------------------------ 日次上限

@needs_ffmpeg
def test_daily_limit_counts_failed_jobs(settings, short_video):
    def boom(path, mode, settings=None):
        raise RuntimeError("fail")

    with make_client(settings, extract_fn=boom) as c:
        login(c)
        for _ in range(3):
            r = upload(c, short_video)
            assert r.status_code == 202
            assert wait_job(c, r.json()["job_id"])["status"] == "error"
        assert upload(c, short_video).status_code == 429


@needs_ffmpeg
def test_total_daily_limit(settings, short_video):
    fast = lambda path, settings=None: {"documents": [], "model": "m", "usage": {}, "mock": True}
    with make_client(settings, extract_fn=fast) as c:
        login(c, "code-a")
        for _ in range(3):
            assert upload(c, short_video).status_code == 202
        login(c, "code-b")
        for _ in range(2):
            assert upload(c, short_video).status_code == 202
        r = upload(c, short_video)
        assert r.status_code == 429 and "全体" in r.json()["detail"]


# ------------------------------------------------------------------ 保存期間・掃除

@needs_ffmpeg
def test_result_expires(settings, short_video):
    settings.result_ttl_min = 0
    fast = lambda path, settings=None: {"documents": [], "model": "m", "usage": {}, "mock": True}
    with make_client(settings, extract_fn=fast) as c:
        login(c)
        job_id = upload(c, short_video).json()["job_id"]
        time.sleep(1.0)
        assert c.get(f"/api/jobs/{job_id}").status_code == 404
        mgr: JobManager = c.app.state.jobs
        assert mgr.cleanup()["results"] == 1
        assert job_id not in mgr._jobs


def test_cleanup_removes_stale_temp_files(settings):
    mgr = JobManager(settings, UsageStore(settings.usage_db_path))
    stale = settings.uploads_dir / "stale"
    stale.mkdir()
    (stale / "input.mp4").write_bytes(b"x")
    fresh = settings.uploads_dir / "fresh"
    fresh.mkdir()
    old = time.time() - 3600
    os.utime(stale, (old, old))

    mgr.cleanup()
    assert not stale.exists() and fresh.exists()
    mgr.cleanup(startup=True)  # 起動時は全部消す
    assert uploads_entries(settings) == []
    mgr.shutdown()


# ------------------------------------------------------------------ Gemini 呼び出し（偽クライアント）

class FakeFiles:
    def __init__(self):
        self.deleted = []

    def upload(self, file, config):
        return SimpleNamespace(name="files/abc", uri="https://example.invalid/files/abc",
                               mime_type="video/mp4", state=SimpleNamespace(name="ACTIVE"))

    def get(self, name):
        raise AssertionError

    def delete(self, name):
        self.deleted.append(name)


class FakeInteractions:
    def __init__(self, fail=False):
        self.fail = fail
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.fail:
            raise TimeoutError
        return SimpleNamespace(
            status="completed",
            output_text='{"documents": [{"doc_type": "receipt", "amount": 100}]}',
            usage=SimpleNamespace(total_input_tokens=1000, total_output_tokens=50,
                                  total_thought_tokens=20, total_tokens=1070),
        )


@pytest.mark.parametrize("fail", [False, True])
def test_gemini_file_deleted_after_call(settings, monkeypatch, tmp_path, fail):
    settings.gemini_mode = "live"
    settings.gemini_api_key = "dummy"
    client = SimpleNamespace(files=FakeFiles(), interactions=FakeInteractions(fail=fail))
    monkeypatch.setattr(extractor, "make_client", lambda s: client)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    if fail:
        with pytest.raises(TimeoutError):
            extractor.extract(video, "sales", settings=settings)
    else:
        out = extractor.extract(video, "sales", settings=settings)
        assert out["usage"] == {"input_tokens": 1000, "output_tokens": 50,
                                "thought_tokens": 20, "total_tokens": 1070}
        assert out["documents"][0]["amount"] == 100
    assert client.files.deleted == ["files/abc"]
    assert client.interactions.kwargs["store"] is False
    assert client.interactions.kwargs["response_format"]["schema"] == schema.response_schema("sales")
    assert "売上管理" in client.interactions.kwargs["input"][1]["text"]


# ------------------------------------------------------------------ インボイス・重複排除

@pytest.mark.parametrize("value,expected", [
    ("T1234567890123", "ok"),
    ("Ｔ１２３４５６７８９０１２３", "ok"),
    ("t-1234-5678-9012-3", "ok"),
    ("T123456789012", "invalid"),
    ("1234567890123", "invalid"),
    (None, "none"),
])
def test_invoice_format(value, expected):
    assert invoice.check_format(value) == expected


def test_dedupe_expense_merges_and_fills():
    docs = [
        {"doc_type": "receipt", "date": "2026-01-01", "payee": "店 A", "amount": 500, "invoice_number": None},
        {"doc_type": "receipt", "date": "2026-01-01", "payee": "店A", "amount": 500,
         "invoice_number": "T1111111111111", "low_confidence_fields": ["invoice_number"]},
        {"doc_type": "receipt", "date": "2026-01-02", "payee": "店A", "amount": 500},
    ]
    out = postprocess.dedupe(docs, "expense")
    assert len(out) == 2
    assert out[0]["invoice_number"] == "T1111111111111"
    assert "invoice_number" in out[0]["low_confidence_fields"]


def test_dedupe_bizcard_by_email():
    docs = [{"name": "A", "email": "a@example.com"}, {"name": "A ", "email": "A@example.com"}, {"name": "B"}]
    assert len(postprocess.dedupe(docs, "bizcard")) == 2


def test_dedupe_sales_by_receipt_no():
    docs = [{"receipt_no": "No.0101", "amount_total": 100}, {"receipt_no": "0101", "amount_total": 100},
            {"receipt_no": "No.0102", "amount_total": 100}]
    assert len(postprocess.dedupe(docs, "sales")) == 2


# ------------------------------------------------------------------ モード

@needs_ffmpeg
def test_sales_mode_flow(settings, short_video):
    with make_client(settings) as c:
        login(c)
        job = wait_job(c, upload(c, short_video, mode="sales").json()["job_id"])
    assert job["status"] == "done" and job["mode"] == "sales"
    docs = {d["receipt_no"]: d for d in job["documents"]}
    assert set(docs) == {"No.0101", "No.0102"}  # 領収書番号の一致で統合

    first = docs["No.0101"]
    assert first["merged_count"] == 2
    assert first["customer_name"] == "上様"  # 宛名はそのまま
    assert first["issuer_check"] == "ok"
    # 発行者情報は動画ではなく設定値
    assert (first["issuer_name"], first["issuer_invoice_no"], first["issuer_address"]) == \
        ("見本商店", "T1111111111111", "東京都サンプル区1-1")
    assert "sales_category" in first["low_confidence_fields"]

    second = docs["No.0102"]
    assert second["issuer_check"] == "mismatch"
    assert "issuer_name_in_video" in second["low_confidence_fields"]
    assert second["issuer_name"] == "見本商店"
    # 15000 + 1600 != 16500 なので金額3項目が要確認
    assert {"amount_total", "amount_excl_tax", "tax_amount"} <= set(second["low_confidence_fields"])


@needs_ffmpeg
def test_sales_mode_without_issuer_settings(settings, short_video):
    settings.self_name = settings.self_invoice_no = settings.self_address = ""
    with make_client(settings) as c:
        login(c)
        job = wait_job(c, upload(c, short_video, mode="sales").json()["job_id"])
    assert {d["issuer_check"] for d in job["documents"]} == {"unset"}
    assert all(d["issuer_name"] is None for d in job["documents"])


@needs_ffmpeg
def test_bizcard_mode_flow(settings, short_video):
    with make_client(settings) as c:
        login(c)
        job = wait_job(c, upload(c, short_video, mode="bizcard").json()["job_id"])
    assert job["status"] == "done" and job["mode"] == "bizcard"
    assert len(job["documents"]) == 2
    assert {d["name"] for d in job["documents"]} == {"見本 太郎", "架空 花子"}
    assert "doc_type" not in job["documents"][0]


def test_invalid_mode_rejected(settings):
    with make_client(settings) as c:
        login(c)
        r = c.post("/api/jobs?mode=payroll", content=b"x", headers={"X-Filename": "a.mp4"})
        assert r.status_code == 400
    assert uploads_entries(settings) == []


def test_me_exposes_modes_and_issuer(settings):
    with make_client(settings) as c:
        login(c)
        me = c.get("/api/me").json()
    assert me["modes"] == ["expense", "sales", "bizcard", "journal"]
    assert me["issuer"]["invoice_no"] == "T1111111111111"


@pytest.mark.parametrize("mode", schema.MODES)
def test_schema_per_mode(mode):
    s = schema.response_schema(mode)
    doc = s["properties"]["documents"]["items"]
    assert doc["properties"]["low_confidence_fields"]["items"]["enum"] == schema.fields(mode)
    assert set(doc["required"]) == set(doc["properties"])
    assert ("doc_type" in doc["properties"]) == (mode in ("expense", "sales"))
    assert ("reports" in s["properties"]) == (mode == "journal")
    text = schema.prompt(mode, Settings())
    assert "推測せず null" in text
    assert ("推定してよい" in text) == (mode in ("expense", "sales"))


def test_sales_prompt_uses_categories():
    st = Settings(sales_categories=["物販", "講座"])
    assert "物販、講座" in schema.prompt("sales", st)


def test_self_invoice_no_validated():
    with pytest.raises(ValueError):
        validate(Settings(self_invoice_no="T123"))
    st = Settings(self_invoice_no="Ｔ1111-1111-1111-1")
    validate(st)
    assert st.self_invoice_no == "T1111111111111"


def test_mock_extract_per_mode(settings, tmp_path):
    for mode in schema.MODES:
        out = extractor.extract(tmp_path / "x.mp4", mode, settings=settings)
        assert out["mock"] and out["documents"]
        allowed = set(schema.fields(mode)) | {"doc_type", "first_seen", "low_confidence_fields"}
        for d in out["documents"]:
            assert set(d) <= allowed, (mode, set(d) - allowed)


# ------------------------------------------------------------------ 学習（訂正履歴）

from cryptography.fernet import Fernet  # noqa: E402

from app import learning  # noqa: E402


@pytest.fixture
def learn_settings(settings):
    settings.learning_key = Fernet.generate_key().decode()
    return settings


def test_learning_disabled_without_key(settings):
    with make_client(settings) as c:
        login(c)
        assert c.get("/api/me").json()["learning"]["available"] is False
        assert c.put("/api/learning", json={"enabled": True}).status_code == 404
    assert not settings.learning_db_path.exists()


def test_learning_key_validated():
    with pytest.raises(ValueError):
        validate(Settings(learning_key="not-a-key"))


@needs_ffmpeg
def test_corrections_require_consent_and_store_only_changes(learn_settings, short_video):
    with make_client(learn_settings) as c:
        login(c)
        st = c.get("/api/learning").json()
        assert st["available"] and not st["enabled"] and st["count"] == 0 and st["trash"]["count"] == 0
        job = wait_job(c, upload(c, short_video).json()["job_id"])
        original = job["documents"][0]
        edits = [{"index": 0, "field": "payee", "value": "手書き商店"},           # 変更あり → 保存
                 {"index": 0, "field": "amount", "value": str(original["amount"])},  # 変更なし → 保存しない
                 {"index": 0, "field": "doc_type", "value": "invoice"},          # 対象外の項目
                 {"index": 99, "field": "payee", "value": "x"}]                  # 範囲外
        # 同意なしでは保存できない
        assert c.post(f"/api/jobs/{job['job_id']}/corrections", json={"edits": edits}).status_code == 403

        assert c.put("/api/learning", json={"enabled": True}).json()["enabled"] is True
        r = c.post(f"/api/jobs/{job['job_id']}/corrections", json={"edits": edits}).json()
        assert r["saved"] == 1 and r["count"] == 1
        # 同じジョブを送り直しても重複しない
        assert c.post(f"/api/jobs/{job['job_id']}/corrections", json={"edits": edits}).json()["count"] == 1

        # 他のユーザーからは見えず、そのジョブにも送れない
        login(c, "code-b")
        assert c.get("/api/learning").json()["count"] == 0
        assert c.post(f"/api/jobs/{job['job_id']}/corrections", json={"edits": edits}).status_code == 403
        c.put("/api/learning", json={"enabled": True})
        assert c.post(f"/api/jobs/{job['job_id']}/corrections", json={"edits": edits}).status_code == 404

    # 値は暗号化されて保存される
    raw = learn_settings.learning_db_path.read_bytes()
    assert "手書き商店".encode() not in raw


@needs_ffmpeg
def test_hints_passed_to_next_extraction(learn_settings, short_video):
    seen = []

    def capture(path, mode, settings=None, hints=None):
        seen.append(hints)
        return {"documents": [{"payee": "読み取り", "amount": 100, "low_confidence_fields": []}],
                "model": "m", "usage": {}, "mock": True}

    with make_client(learn_settings, extract_fn=capture) as c:
        login(c)
        c.put("/api/learning", json={"enabled": True})
        job = wait_job(c, upload(c, short_video).json()["job_id"])
        assert job["hints_used"] == 0
        c.post(f"/api/jobs/{job['job_id']}/corrections",
               json={"edits": [{"index": 0, "field": "payee", "value": "正しい店"}]})
        job2 = wait_job(c, upload(c, short_video).json()["job_id"])
        assert job2["hints_used"] == 1
        # 別のモードの訂正は使わない
        wait_job(c, upload(c, short_video, mode="bizcard").json()["job_id"])
    assert seen[0] is None
    assert seen[1] == [{"field": "payee", "original": "読み取り", "corrected": "正しい店"}]
    assert seen[2] is None

    # 学習をオフにすると使わない
    store = learning.LearningStore(learn_settings.learning_db_path, learn_settings.learning_key)
    store.set_enabled("alice", False)
    assert store.hints("alice", "expense", 30) == []
    store.close()


def _age(store, user, days, col="created_at"):
    store._conn.execute(f"UPDATE corrections SET {col} = {col} - ?*86400 WHERE user_label=?", (days, user))


def test_learning_two_stage_delete(tmp_path):
    key = Fernet.generate_key().decode()
    store = learning.LearningStore(tmp_path / "l.db", key)
    store.set_enabled("alice", True)
    store.save("alice", "j1", "expense", [("amount", "1700", "1100"), ("payee", "", "山田")])
    store.save("bob", "j2", "expense", [("amount", "7", "1")])
    assert store.count("alice") == 2
    assert len(store.hints("alice", "expense", 1)) == 1

    # 1段階目：削除済みになり、読み取りには使わない
    assert store.delete_all("alice") == 2
    assert store.count("alice") == 0 and store.hints("alice", "expense", 30) == []
    assert store.trash("alice")[0] == 2 and store.count("bob") == 1
    # 期限内なら戻せる
    assert store.restore("alice") == 2 and store.count("alice") == 2 and store.trash("alice")[0] == 0

    # 期限を過ぎた削除済みは完全に消える（まだ期限内のものは残る）
    store.delete_all("alice")
    _age(store, "alice", 31, "deleted_at")
    store.save("alice", "j3", "expense", [("date", "", "2026-01-01")])
    assert store.purge(365, 30) == 2
    assert store.trash("alice")[0] == 0 and store.count("alice") == 1
    assert store.restore("alice") == 0

    # 今すぐ完全に消すのは削除済みの分だけ
    store.delete_all("alice")
    store.save("alice", "j4", "expense", [("payee", "a", "b")])
    assert store.purge_now("alice") == 1 and store.count("alice") == 1

    # 最後の訂正から保存期間を過ぎたユーザーは丸ごと消える
    _age(store, "bob", 400)
    assert store.purge(365, 30) == 1 and store.count("bob") == 0

    actions = [(e["user"], e["action"], e["actor"], e["n"]) for e in reversed(store.events())]
    assert actions == [("alice", "delete", "self", 2), ("alice", "restore", "self", 2), ("alice", "delete", "self", 2),
                       ("alice", "auto_purge", "system", 2), ("alice", "restore", "self", 0),
                       ("alice", "delete", "self", 1), ("alice", "purge_now", "self", 1),
                       ("bob", "auto_purge", "system", 1)]
    store.close()


@needs_ffmpeg
def test_learning_delete_restore_api(learn_settings, short_video):
    with make_client(learn_settings) as c:
        login(c)
        c.put("/api/learning", json={"enabled": True})
        job = wait_job(c, upload(c, short_video).json()["job_id"])
        c.post(f"/api/jobs/{job['job_id']}/corrections", json={"edits": [{"index": 0, "field": "payee", "value": "店"}]})

        r = c.delete("/api/learning").json()
        assert r["deleted"] == 1 and r["count"] == 0 and r["trash"]["count"] == 1 and r["trash"]["purge_at"]
        r = c.post("/api/learning/restore").json()
        assert r["restored"] == 1 and r["count"] == 1 and r["trash"]["count"] == 0

        # 管理者による復元（トークン必須）と、操作の記録
        c.delete("/api/learning")
        assert c.post("/admin/learning/restore?user=alice").status_code == 403
        r = c.post("/admin/learning/restore?user=alice", headers={"X-Admin-Token": "admin-token"}).json()
        assert r["restored"] == 1
        ev = c.get("/admin/learning/events?user=alice", headers={"X-Admin-Token": "admin-token"}).json()["events"]
        assert [(e["action"], e["actor"]) for e in ev][:2] == [("restore", "admin"), ("delete", "self")]

        c.delete("/api/learning")
        r = c.delete("/api/learning?permanent=true").json()
        assert r["deleted"] == 1 and r["count"] == 0 and r["trash"]["count"] == 0
    raw = learn_settings.learning_db_path.read_bytes()
    assert "店".encode() not in raw


def test_diff_ignores_unchanged_and_cleared():
    docs = [{"amount": 1200, "payee": "店", "date": None}]
    edits = [{"index": 0, "field": "amount", "value": "1200"}, {"index": 0, "field": "payee", "value": ""},
             {"index": 0, "field": "date", "value": "2026-01-02"}, {"index": "0", "field": "amount", "value": "1"}]
    assert learning.diff(docs, edits, {"amount", "payee", "date"}) == [("date", "", "2026-01-02")]


def test_prompt_includes_hints():
    text = schema.prompt("sales", Settings(), [{"field": "amount_total", "original": "7000", "corrected": "1000"},
                                               {"field": "customer_name", "original": "", "corrected": "山田様"}])
    assert "読み取り 「7000」 → 正しくは「1000」" in text
    assert "空欄（読めなかった） → 正しくは「山田様」" in text
    assert "過去の訂正例" not in schema.prompt("sales", Settings())


# ------------------------------------------------------------------ レジジャーナル

@needs_ffmpeg
def test_journal_mode_flow(settings, short_video):
    with make_client(settings) as c:
        login(c)
        job = wait_job(c, upload(c, short_video, mode="journal").json()["job_id"])
    assert job["status"] == "done" and job["mode"] == "journal"
    docs = {d["receipt_no"]: d for d in job["documents"]}
    assert list(docs) == ["0001", "0002", "0003", "0004"]  # 再び映った 0001 は統合される
    assert docs["0001"]["merged_count"] == 2 and len(docs["0001"]["items"]) == 2
    assert docs["0003"]["txn_type"] == "refund"
    # 読めない品目がある取引は要確認
    assert "items" in docs["0002"]["low_confidence_fields"]
    assert "amount_total" not in docs["0002"]["low_confidence_fields"]  # 品目の合計は合っている
    # 取引の合計 1080+1700-600+720=2900・3件。レポートは 3400・4件なので「合いません」
    assert job["reports"] == [{"report_date": "2026-11-01", "report_total": 3400, "report_count": 4,
                               "read_total": 2900, "read_count": 3, "status": "mismatch"}]


def test_journal_items_total_check():
    st = Settings()
    item = lambda a: {"name": "x", "unit_price": None, "quantity": 1, "amount": a, "low_confidence": False}
    docs = postprocess.finalize([
        {"receipt_no": "1", "txn_type": "sale", "amount_total": 1100, "tax_amount": 100, "items": [item(1000)]},  # 外税
        {"receipt_no": "2", "txn_type": "sale", "amount_total": 1100, "tax_amount": 100, "items": [item(1100)]},  # 内税
        {"receipt_no": "3", "txn_type": "sale", "amount_total": 900, "tax_amount": None, "items": [item(1000)]},  # 合わない
        {"receipt_no": "4", "txn_type": "返品", "amount_total": -500, "items": []},
    ], "journal", st)
    flags = [d["low_confidence_fields"] for d in docs]
    assert flags[0] == [] and flags[1] == [] and set(flags[2]) == {"items", "amount_total"}
    assert docs[3]["txn_type"] == "sale" and docs[3]["amount_total"] == 500  # 不明な種類は sale、金額は正の数


def test_journal_summary_per_day():
    docs = [
        {"txn_datetime": "2026-11-01 10:00", "txn_type": "sale", "amount_total": 1000},
        {"txn_datetime": "2026-11-01 11:00", "txn_type": "refund", "amount_total": 200},
        {"txn_datetime": "2026-11-01 12:00", "txn_type": "void", "amount_total": 999},
        {"txn_datetime": "2026-11-02 10:00", "txn_type": "sale", "amount_total": 500},
    ]
    out = postprocess.journal_summary(docs, [
        {"report_date": "2026-11-01", "sales_total": 800, "txn_count": 1},
        {"report_date": "2026-11-02", "sales_total": 600, "txn_count": None},
        {"report_date": None, "sales_total": None, "txn_count": None},
    ])
    assert [r["status"] for r in out] == ["ok", "mismatch", "not_read"]
    assert (out[0]["read_total"], out[0]["read_count"]) == (800, 1)
    # レポートが1つで日付が読めなければ、全取引と比べる
    one = postprocess.journal_summary(docs, [{"report_date": None, "sales_total": 1300, "txn_count": 2}])
    assert one[0]["status"] == "ok"


def test_journal_prompt_and_learning_fields():
    text = schema.prompt("journal", Settings())
    assert "reports" in text and "値引" in text
    item = schema.response_schema("journal")["properties"]["documents"]["items"]["properties"]["items"]
    assert item["type"] == "array" and "low_confidence" in item["items"]["properties"]


# ------------------------------------------------------------------ エクセルへの追記

import base64  # noqa: E402
import io  # noqa: E402
import urllib.parse  # noqa: E402
import json as _json  # noqa: E402
from datetime import date as _date, datetime as _datetime  # noqa: E402

from openpyxl import Workbook, load_workbook  # noqa: E402
from openpyxl.worksheet.table import Table  # noqa: E402

from app import xlsx_export  # noqa: E402

OURS = ["種別", "日付", "勘定科目(候補)", "支払先", "金額(税込)", "登録番号", "番号チェック", "映った時刻"]


def make_book(chart=False) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "経費"
    ws["A1"] = "2026年 経費台帳"
    for c, h in enumerate(["日付", "取引先", "金額（円）", "科目", "メモ", "残高"], 1):
        ws.cell(2, c, h)
    ws.append([_date(2026, 10, 1), "前月の店", 1000, "雑費", "", "=C3"])
    ws["A3"].number_format = "yyyy/mm/dd"
    ws["C3"].number_format = "#,##0"
    ws.add_table(Table(displayName="Ledger", ref="A2:F3"))
    if chart:
        from openpyxl.chart import BarChart, Reference
        ch = BarChart()
        ch.add_data(Reference(ws, min_col=3, min_row=2, max_row=3), titles_from_data=True)
        ws.add_chart(ch, "H2")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_inspect_suggests_columns():
    out = xlsx_export.inspect(make_book(), OURS)
    sh = out["sheets"][0]
    assert sh["name"] == "経費" and sh["header_row"] == 2 and sh["last_row"] == 3
    # 日付→日付、取引先→支払先、金額（円）→金額(税込)、科目→勘定科目(候補)。メモ・残高は対応なし
    assert sh["suggested"] == {1: "日付", 2: "支払先", 3: "金額(税込)", 4: "勘定科目(候補)"}
    assert out["warnings"] == []
    assert xlsx_export.inspect(make_book(chart=True), OURS)["warnings"] == ["グラフ", "図形・画像"]


def test_xlsx_append_keeps_style_formula_and_table():
    rows = [["領収書/レシート", "2026-10-28", "会議費", "カフェ", "1,320", "T1234567890123", "形式OK", "00:03"],
            ["請求書", "2026-10-31", "外注費", "デザイン社", "55000", "", "", "00:12"]]
    data, info = xlsx_export.append(make_book(), "経費", 2, {1: "日付", 2: "支払先", 3: "金額(税込)", 4: "勘定科目(候補)"},
                                    OURS, rows)
    assert info == {"sheet": "経費", "first_row": 4, "appended": 2, "warnings": []}
    ws = load_workbook(io.BytesIO(data))["経費"]
    assert ws["A4"].value == _datetime(2026, 10, 28) and ws["A4"].number_format == "yyyy/mm/dd"
    assert (ws["B4"].value, ws["C4"].value, ws["D4"].value) == ("カフェ", 1320, "会議費")
    assert ws["C5"].value == 55000 and ws["C5"].number_format == "#,##0"
    assert ws["E4"].value is None  # 対応づけていない列は空のまま
    assert (ws["F4"].value, ws["F5"].value) == ("=C4", "=C5")  # 数式は行をずらして引き継ぐ
    assert ws.tables["Ledger"].ref == "A2:F5"


def test_xlsx_rejects_bad_input():
    with pytest.raises(xlsx_export.XlsxError):
        xlsx_export.inspect(b"not a zip", OURS)
    with pytest.raises(xlsx_export.XlsxError):
        xlsx_export.append(make_book(), "経費", 2, {}, OURS, [["x"] * len(OURS)])
    with pytest.raises(xlsx_export.XlsxError):
        xlsx_export.append(make_book(), "なし", 2, {1: "日付"}, OURS, [["x"] * len(OURS)])


def test_xlsx_numbers_and_codes():
    conv = xlsx_export._convert
    assert conv("0001") == ("0001", None)  # 番号は文字のまま
    assert conv("-50") == (-50, None)
    v, fmt = conv("40.7%")
    assert abs(v - 0.407) < 1e-9 and fmt == "0.0%"
    assert conv("03-0000-0000") == ("03-0000-0000", None)
    assert conv("2026-11-01 10:05")[0] == _datetime(2026, 11, 1, 10, 5)


def test_xlsx_api_roundtrip(settings):
    b64 = base64.b64encode(make_book()).decode()
    with make_client(settings) as c:
        assert c.post("/api/xlsx/inspect", json={"file_b64": b64, "header": OURS}).status_code == 401
        login(c)
        insp = c.post("/api/xlsx/inspect", json={"file_b64": b64, "header": OURS}).json()
        sh = insp["sheets"][0]
        r = c.post("/api/xlsx/append", json={"file_b64": b64, "filename": "台帳.xlsx", "sheet": sh["name"],
                                             "header_row": sh["header_row"], "mapping": sh["suggested"],
                                             "header": OURS, "rows": [["", "2026-10-28", "", "店", "100", "", "", ""]]})
        assert r.status_code == 200
        assert "%E5%8F%B0%E5%B8%B3_%E8%BF%BD%E8%A8%98_" in r.headers["content-disposition"]  # 台帳_追記_
        assert _json.loads(urllib.parse.unquote(r.headers["x-export-info"]))["first_row"] == 4
        assert load_workbook(io.BytesIO(r.content))["経費"]["B4"].value == "店"
        assert c.post("/api/xlsx/inspect", json={"file_b64": "@@@", "header": OURS}).status_code == 400
    # 送ったファイルは作業ディレクトリにも残らない
    assert uploads_entries(settings) == []
