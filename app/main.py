"""FastAPI アプリ本体。起動: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import learning as learning_mod
from . import media
from . import xlsx_export
from .auth import COOKIE_NAME, Auth
from .config import PROJECT_ROOT, Settings, load_settings
from .jobs import JobManager
from .learning import LearningStore
from .schema import MODES, fields
from .usage import UsageStore

log = logging.getLogger("vro")
STATIC_DIR = PROJECT_ROOT / "static"


class LoginBody(BaseModel):
    code: str


class LearningBody(BaseModel):
    enabled: bool


class Edit(BaseModel):
    index: int
    field: str
    value: str | None = None


class CorrectionsBody(BaseModel):
    edits: list[Edit]


class XlsxInspectBody(BaseModel):
    file_b64: str
    header: list[str]  # こちらの表の見出し（いま表示している表）


class XlsxAppendBody(BaseModel):
    file_b64: str
    filename: str = "book.xlsx"
    sheet: str
    header_row: int
    mapping: dict[str, str]  # 相手の列番号 → こちらの見出し
    header: list[str]
    rows: list[list[str]]


def _decode_xlsx(b64: str) -> bytes:
    if len(b64) > xlsx_export.MAX_FILE_BYTES * 4 // 3 + 16:
        raise HTTPException(413, f"エクセルファイルは{xlsx_export.MAX_FILE_BYTES // 1024 // 1024}MBまでです")
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(400, "ファイルを読み込めませんでした") from e


def create_app(settings: Settings | None = None, extract_fn=None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx は既定でリクエストURLを INFO で出す（国税庁APIのアプリIDを含むため）ので抑止する
    logging.getLogger("httpx").setLevel(logging.WARNING)

    usage = UsageStore(settings.usage_db_path)
    # 鍵が設定されていなければ学習機能は無効（DBファイルも作らない）
    learning = LearningStore(settings.learning_db_path, settings.learning_key) if settings.learning_key else None
    jobs = JobManager(settings, usage, extract_fn=extract_fn, learning=learning)
    auth = Auth(settings)

    async def cleanup_loop():
        while True:
            await asyncio.sleep(settings.cleanup_interval_sec)
            try:
                await asyncio.to_thread(jobs.cleanup)
                if learning:
                    n = await asyncio.to_thread(learning.purge, settings.learning_retention_days,
                                                settings.learning_trash_days)
                    if n:
                        log.info("learning purge corrections=%d", n)
            except Exception as e:
                log.warning("cleanup failed: %s", type(e).__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(jobs.cleanup, True)
        log.info("started mode=%s model=%s work_dir=%s", settings.gemini_mode, settings.gemini_model,
                 settings.work_dir)
        task = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            task.cancel()
            jobs.shutdown()
            usage.close()
            if learning:
                learning.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.jobs, app.state.usage = settings, jobs, usage

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        ip = request.client.host if request.client else "?"
        if auth.too_many_failures(ip):
            raise HTTPException(429, "試行回数が多すぎます。しばらくしてからお試しください")
        label = auth.check_code(body.code.strip())
        if not label:
            auth.record_failure(ip)
            raise HTTPException(401, "招待コードが正しくありません")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE_NAME, auth.issue(label), max_age=settings.session_max_age_sec,
                        httponly=True, samesite="strict", secure=settings.cookie_secure)
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/api/me")
    def me(request: Request):
        user = auth.require_user(request)
        return {
            "user": user,
            "remaining_today": usage.remaining(user, settings.daily_limit_per_user, settings.daily_limit_total),
            "max_upload_mb": settings.max_upload_mb,
            "max_duration_sec": settings.max_duration_sec,
            "result_ttl_min": settings.result_ttl_min,
            "gemini_mode": settings.gemini_mode,
            "modes": list(MODES),
            "learning": learning_status(user),
            # 売上モードで表に出す発行者情報（設定値）
            "issuer": {"name": settings.self_name or None, "invoice_no": settings.self_invoice_no or None,
                       "address": settings.self_address or None},
        }

    @app.post("/api/jobs", status_code=202)
    async def create_job(request: Request, mode: str = "expense"):
        """動画をリクエストボディにそのまま載せて送る（multipart を使わず、一時ファイルの複製を避ける）。"""
        user = auth.require_user(request)
        if mode not in MODES:
            raise HTTPException(400, "モードの指定が正しくありません")
        reason = usage.limit_reason(user, settings.daily_limit_per_user, settings.daily_limit_total)
        if reason:
            raise HTTPException(429, reason)

        filename = urllib.parse.unquote(request.headers.get("X-Filename", ""))
        ext = Path(filename).suffix.lower()
        if ext not in media.ALLOWED_EXTENSIONS:
            raise HTTPException(400, "mp4 または mov 形式の動画をアップロードしてください")
        max_bytes = settings.max_upload_mb * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise HTTPException(413, f"ファイルサイズは{settings.max_upload_mb}MBまでです")

        job_id, job_dir = jobs.new_job_dir()
        submitted = False
        try:
            video = job_dir / f"input{ext}"
            size = 0
            with open(video, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, f"ファイルサイズは{settings.max_upload_mb}MBまでです")
                    f.write(chunk)
            if size == 0:
                raise HTTPException(400, "ファイルが空です")

            try:
                probe = await asyncio.to_thread(media.probe, video, settings)
            except media.MediaError as e:
                raise HTTPException(400, str(e)) from e

            reason = usage.try_reserve(job_id, user, settings.daily_limit_per_user,
                                       settings.daily_limit_total, size, probe.duration_sec, mode)
            if reason:
                raise HTTPException(429, reason)

            job = jobs.submit(job_id, job_dir, video, user, mode, probe)
            submitted = True
            log.info("job accepted id=%s mode=%s size=%d duration=%.1fs", job_id, mode, size, probe.duration_sec)
            return {"job_id": job_id, "mode": mode, "notice": job.notice, "duration_sec": round(probe.duration_sec, 1)}
        finally:
            # 受け付けなかった場合はここで削除。受け付けた場合はジョブの finally で削除される
            if not submitted:
                jobs.remove_dir(job_dir)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        user = auth.require_user(request)
        job = jobs.get(job_id, user)
        if job is None:
            raise HTTPException(404, f"ジョブが見つからないか、保存期間（{settings.result_ttl_min}分）を過ぎました")
        return job.public()

    # ---------------------------------------------------------- 学習（訂正履歴）

    def learning_status(user: str) -> dict:
        if not learning:
            return {"available": False, "enabled": False, "count": 0}
        trash_n, oldest = learning.trash(user)
        return {"available": True, "enabled": learning.enabled(user), "count": learning.count(user),
                "retention_days": settings.learning_retention_days, "trash_days": settings.learning_trash_days,
                # 削除済みでまだ戻せる件数と、そのうち最初に完全に消える時刻（UNIX秒）
                "trash": {"count": trash_n,
                          "purge_at": int(oldest + settings.learning_trash_days * 86400) if oldest else None}}

    def require_learning() -> LearningStore:
        if not learning:
            raise HTTPException(404, "学習機能は無効です（LEARNING_KEY が未設定）")
        return learning

    @app.get("/api/learning")
    def get_learning(request: Request):
        return learning_status(auth.require_user(request))

    @app.put("/api/learning")
    def put_learning(body: LearningBody, request: Request):
        user = auth.require_user(request)
        require_learning().set_enabled(user, body.enabled)
        return learning_status(user)

    @app.delete("/api/learning")
    def delete_learning(request: Request, permanent: bool = False):
        """既定は削除済みにするだけ（trash_days 日以内なら戻せる）。permanent=true で削除済みの分を今すぐ完全に消す。"""
        user = auth.require_user(request)
        store = require_learning()
        n = store.purge_now(user) if permanent else store.delete_all(user)
        log.info("learning deleted corrections=%d permanent=%s", n, permanent)
        return {**learning_status(user), "deleted": n}

    @app.post("/api/learning/restore")
    def restore_learning(request: Request):
        user = auth.require_user(request)
        n = require_learning().restore(user)
        log.info("learning restored corrections=%d by=self", n)
        return {**learning_status(user), "restored": n}

    @app.post("/api/jobs/{job_id}/corrections")
    def post_corrections(job_id: str, body: CorrectionsBody, request: Request):
        """表で直した値を受け取り、元の読み取り結果と比べて変わった項目だけを保存する。"""
        user = auth.require_user(request)
        store = require_learning()
        if not store.enabled(user):
            raise HTTPException(403, "学習がオフになっています")
        job = jobs.get(job_id, user)
        if job is None or job.original is None:
            raise HTTPException(404, f"ジョブが見つからないか、保存期間（{settings.result_ttl_min}分）を過ぎました")
        if len(body.edits) > 1000:
            raise HTTPException(400, "訂正の件数が多すぎます")
        # 品目（items）のような入れ子の項目は対象外
        allowed = set(fields(job.mode)) - {"items"}
        pairs = learning_mod.diff(job.original, [e.model_dump() for e in body.edits], allowed)
        n = store.save(user, job_id, job.mode, pairs)
        log.info("learning saved id=%s mode=%s corrections=%d", job_id, job.mode, n)
        return {"saved": n, **learning_status(user)}

    @app.post("/admin/learning/restore")
    def admin_restore_learning(request: Request, user: str):
        """本人から頼まれたときに管理者が戻す。完全に消えた分は戻せない。"""
        auth.require_admin(request)
        n = require_learning().restore(user, actor="admin")
        log.info("learning restored corrections=%d by=admin", n)
        return {"user": user, "restored": n}

    @app.get("/admin/learning/events")
    def admin_learning_events(request: Request, user: str | None = None):
        auth.require_admin(request)
        return {"events": require_learning().events(user)}

    # ---------------------------------------------------------- エクセルへの追記
    # 送られたエクセルはメモリ上だけで扱い、保存しない。ログにはサイズと行数だけを出す

    @app.post("/api/xlsx/inspect")
    def xlsx_inspect(body: XlsxInspectBody, request: Request):
        auth.require_user(request)
        data = _decode_xlsx(body.file_b64)
        try:
            out = xlsx_export.inspect(data, body.header)
        except xlsx_export.XlsxError as e:
            raise HTTPException(400, str(e)) from e
        log.info("xlsx inspect size=%d sheets=%d", len(data), len(out["sheets"]))
        return out

    @app.post("/api/xlsx/append")
    def xlsx_append(body: XlsxAppendBody, request: Request):
        auth.require_user(request)
        data = _decode_xlsx(body.file_b64)
        try:
            mapping = {int(k): v for k, v in body.mapping.items()}
            out, info = xlsx_export.append(data, body.sheet, body.header_row, mapping, body.header, body.rows)
        except ValueError as e:
            raise HTTPException(400, "列の指定が正しくありません") from e
        except xlsx_export.XlsxError as e:
            raise HTTPException(400, str(e)) from e
        log.info("xlsx append size=%d rows=%d", len(data), info["appended"])
        stem = Path(body.filename).stem or "book"
        name = f"{stem}_追記_{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
        return Response(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
            "Content-Disposition": f"attachment; filename=\"export.xlsx\"; filename*=UTF-8''{urllib.parse.quote(name)}",
            "X-Export-Info": urllib.parse.quote(json.dumps(info, ensure_ascii=False)),
        })

    @app.get("/admin/usage")
    def admin_usage(request: Request, days: int = 7):
        auth.require_admin(request)
        return usage.summary(days)

    return app
