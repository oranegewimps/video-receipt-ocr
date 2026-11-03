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
    assert me["modes"] == ["expense", "sales", "bizcard"]
    assert me["issuer"]["invoice_no"] == "T1111111111111"


@pytest.mark.parametrize("mode", schema.MODES)
def test_schema_per_mode(mode):
    s = schema.response_schema(mode)
    doc = s["properties"]["documents"]["items"]
    assert doc["properties"]["low_confidence_fields"]["items"]["enum"] == schema.fields(mode)
    assert set(doc["required"]) == set(doc["properties"])
    assert ("doc_type" in doc["properties"]) == (mode != "bizcard")
    text = schema.prompt(mode, Settings())
    assert "推測せず null" in text
    assert ("推定してよい" in text) == (mode != "bizcard")


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
