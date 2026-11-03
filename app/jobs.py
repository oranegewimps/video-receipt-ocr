"""非同期ジョブの管理と、一時ファイル・期限切れ結果の掃除。

- 解析結果はメモリ上にのみ保持し、RESULT_TTL_MIN 経過後に破棄する。
- ジョブ用の作業ディレクトリは、解析の成否にかかわらず finally で削除する。
- ログにはジョブID・サイズ・所要時間・トークン数のみを出す。
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import extractor, media, postprocess
from .config import Settings
from .usage import UsageStore, estimate_cost

log = logging.getLogger("vro.jobs")


@dataclass
class Job:
    id: str
    owner: str
    mode: str
    created: float
    status: str = "queued"  # queued | processing | done | error
    notice: str | None = None
    error: str | None = None
    documents: list[dict] | None = None
    model: str | None = None
    finished: float | None = None
    expires: float = 0.0

    def public(self) -> dict:
        return {
            "job_id": self.id, "mode": self.mode, "status": self.status, "notice": self.notice, "error": self.error,
            "documents": self.documents, "model": self.model,
            "expires_in_sec": max(0, int(self.expires - time.time())),
        }


@dataclass
class JobManager:
    settings: Settings
    usage: UsageStore
    extract_fn: object = None  # テストで差し替えられるように
    _jobs: dict[str, Job] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.extract_fn = self.extract_fn or extractor.extract
        self._pool = ThreadPoolExecutor(max_workers=self.settings.max_concurrent_jobs,
                                        thread_name_prefix="vro-job")
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- 作業ディレクトリ

    def new_job_dir(self) -> tuple[str, Path]:
        job_id = uuid.uuid4().hex
        d = self.settings.uploads_dir / job_id
        d.mkdir(parents=True)
        return job_id, d

    @staticmethod
    def remove_dir(d: Path) -> None:
        shutil.rmtree(d, ignore_errors=True)
        if d.exists():  # Windows でハンドルが残っていた場合に備えて1回だけ再試行
            time.sleep(0.5)
            shutil.rmtree(d, ignore_errors=True)
        if d.exists():
            log.error("job dir remained after delete: %s", d.name)

    # ---------------------------------------------------------- ジョブ

    def submit(self, job_id: str, job_dir: Path, video: Path, owner: str, mode: str,
               probe: media.ProbeResult) -> Job:
        job = Job(id=job_id, owner=owner, mode=mode, created=time.time(),
                  expires=time.time() + self.settings.result_ttl_min * 60)
        if probe.duration_sec > self.settings.max_duration_sec:
            m, s = divmod(self.settings.max_duration_sec, 60)
            job.notice = (f"動画が{m}分{s:02d}秒を超えているため、先頭{m}分{s:02d}秒のみを解析しました。"
                          "それ以降に映っている書類は対象外です。")
        with self._lock:
            self._jobs[job_id] = job
        self._pool.submit(self._run, job, job_dir, video, probe)
        return job

    def get(self, job_id: str, owner: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner != owner or job.expires < time.time():
            return None
        return job

    def _run(self, job: Job, job_dir: Path, video: Path, probe: media.ProbeResult) -> None:
        t0 = time.monotonic()
        job.status = "processing"
        result: dict | None = None
        status = "error"
        try:
            prepared = media.prepare(video, job_dir / "prepared.mp4", probe, self.settings)
            result = self.extract_fn(prepared, job.mode, settings=self.settings)
            job.documents = postprocess.finalize(result["documents"], job.mode, self.settings)
            job.model = result.get("model")
            job.status = status = "done"
        except media.MediaError as e:
            job.error = str(e)
        except extractor.ExtractionError as e:
            job.error = f"解析に失敗しました：{e}"
        except Exception as e:
            log.error("job failed id=%s err=%s", job.id, type(e).__name__)
            job.error = "解析に失敗しました。時間をおいて再度お試しください。"
        finally:
            self.remove_dir(job_dir)
            job.finished = time.time()
            job.expires = job.finished + self.settings.result_ttl_min * 60
            if job.status != "done":
                job.status = "error"
            elapsed = time.monotonic() - t0
            usage = (result or {}).get("usage") or {}
            model = (result or {}).get("model")
            cost = estimate_cost(model, usage, self.settings.prices) if model else None
            self.usage.finish(job.id, status, model, usage, elapsed, cost)
            log.info("job finished id=%s mode=%s status=%s elapsed=%.1fs in_tok=%s out_tok=%s think_tok=%s",
                     job.id, job.mode, status, elapsed, usage.get("input_tokens"), usage.get("output_tokens"),
                     usage.get("thought_tokens"))

    # ---------------------------------------------------------- 掃除

    def cleanup(self, startup: bool = False) -> dict:
        """期限切れの結果と、古い一時ファイルを消す。起動時は作業ディレクトリを丸ごと空にする。"""
        now = time.time()
        with self._lock:
            expired = [k for k, j in self._jobs.items() if j.expires < now]
            for k in expired:
                del self._jobs[k]
            active = {k for k, j in self._jobs.items() if j.status in ("queued", "processing")}

        max_age = self.settings.temp_file_max_age_min * 60
        removed = 0
        for p in self.settings.uploads_dir.iterdir():
            if not startup:
                if p.name in active:
                    continue
                if now - p.stat().st_mtime < max_age:
                    continue
            if p.is_dir():
                self.remove_dir(p)
            else:
                p.unlink(missing_ok=True)
            removed += 1

        gemini_removed = 0
        if self.settings.is_live:
            try:
                # 解析中のファイル（別インスタンスのものを含む）を避けるため、30分以上前のものだけ消す。
                # 通常は解析直後に削除済みで、ここで消えるのは異常終了時の取り残しのみ。
                gemini_removed = extractor.purge_gemini_files(self.settings, older_than_sec=1800)
            except Exception as e:
                log.warning("gemini purge failed: %s", type(e).__name__)
        if expired or removed or gemini_removed:
            log.info("cleanup results=%d temp=%d gemini=%d", len(expired), removed, gemini_removed)
        return {"results": len(expired), "temp": removed, "gemini": gemini_removed}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
