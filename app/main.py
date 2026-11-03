"""FastAPI アプリ本体。起動: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import media
from .auth import COOKIE_NAME, Auth
from .config import PROJECT_ROOT, Settings, load_settings
from .jobs import JobManager
from .schema import MODES
from .usage import UsageStore

log = logging.getLogger("vro")
STATIC_DIR = PROJECT_ROOT / "static"


class LoginBody(BaseModel):
    code: str


def create_app(settings: Settings | None = None, extract_fn=None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx は既定でリクエストURLを INFO で出す（国税庁APIのアプリIDを含むため）ので抑止する
    logging.getLogger("httpx").setLevel(logging.WARNING)

    usage = UsageStore(settings.usage_db_path)
    jobs = JobManager(settings, usage, extract_fn=extract_fn)
    auth = Auth(settings)

    async def cleanup_loop():
        while True:
            await asyncio.sleep(settings.cleanup_interval_sec)
            try:
                await asyncio.to_thread(jobs.cleanup)
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

    @app.get("/admin/usage")
    def admin_usage(request: Request, days: int = 7):
        auth.require_admin(request)
        return usage.summary(days)

    return app
