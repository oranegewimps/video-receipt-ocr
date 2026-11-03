import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe が PATH にありません")


def make_video(path: Path, seconds: int, audio: bool = True) -> Path:
    """lavfi のテストパターンから小さな動画を作る（個人情報を含まない）。"""
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=2:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture(scope="session")
def short_video(tmp_path_factory) -> Path:
    return make_video(tmp_path_factory.mktemp("vid") / "short.mp4", 4)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        gemini_mode="mock",
        invite_codes={"code-a": "alice", "code-b": "bob"},
        session_secret="test-secret",
        admin_token="admin-token",
        daily_limit_per_user=3,
        daily_limit_total=5,
        work_dir=tmp_path / "work",
        usage_db_path=tmp_path / "usage.db",
        ffmpeg_bin=FFMPEG or "ffmpeg",
        ffprobe_bin=FFPROBE or "ffprobe",
        max_duration_sec=180,
        self_name="見本商店",
        self_invoice_no="T1111111111111",
        self_address="東京都サンプル区1-1",
    )


def make_client(settings, extract_fn=None) -> TestClient:
    return TestClient(create_app(settings, extract_fn=extract_fn))


def login(client: TestClient, code="code-a"):
    r = client.post("/api/login", json={"code": code})
    assert r.status_code == 200, r.text


def upload(client: TestClient, video: Path, name="clip.mp4", mode="expense"):
    return client.post(f"/api/jobs?mode={mode}", content=video.read_bytes(),
                       headers={"X-Filename": name, "Content-Type": "video/mp4"})


def wait_job(client: TestClient, job_id: str, timeout=20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")
