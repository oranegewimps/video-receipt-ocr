"""環境変数から設定を読み込む。秘密情報（APIキー等）はここでのみ扱い、ログには出さない。"""
from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SALES_CATEGORIES = ["商品売上", "サービス売上", "飲食売上", "工事売上", "手数料収入", "その他売上"]


def _int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name, "").strip()
    return float(v) if v else default


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def parse_invite_codes(raw: str) -> dict[str, str]:
    """"label:code,label2:code2" または "code1,code2" を {code: label} に変換する。"""
    codes: dict[str, str] = {}
    for i, item in enumerate(x.strip() for x in raw.split(",")):
        if not item:
            continue
        if ":" in item:
            label, code = (s.strip() for s in item.split(":", 1))
        else:
            label, code = f"user{i + 1}", item
        if code:
            codes[code] = label
    return codes


def parse_prices(raw: str) -> dict[str, tuple[float, float]]:
    """"model=入力単価/出力単価,..."（USD / 100万トークン）を辞書に変換する。"""
    prices: dict[str, tuple[float, float]] = {}
    for item in (x.strip() for x in raw.split(",")):
        if not item or "=" not in item:
            continue
        model, pair = item.split("=", 1)
        pin, pout = pair.split("/", 1)
        prices[model.strip()] = (float(pin), float(pout))
    return prices


@dataclass
class Settings:
    gemini_mode: str = "mock"  # mock | live
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-flash"
    gemini_media_resolution: str = ""  # low | medium | high | ultra_high | ""(API既定)
    gemini_fps: float = 1.0
    gemini_thinking_level: str = "low"  # minimal | low | medium | high | ""(既定)
    gemini_timeout_sec: int = 300
    gemini_max_attempts: int = 3
    gemini_file_active_timeout_sec: int = 300
    strip_audio: bool = True

    invite_codes: dict[str, str] = field(default_factory=dict, repr=False)
    session_secret: str = field(default="", repr=False)
    admin_token: str = field(default="", repr=False)
    cookie_secure: bool = False
    session_max_age_sec: int = 30 * 24 * 3600

    daily_limit_per_user: int = 10
    daily_limit_total: int = 50
    max_concurrent_jobs: int = 2

    max_upload_mb: int = 500
    max_duration_sec: int = 180

    work_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "video-receipt-ocr")
    usage_db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "usage.db")
    result_ttl_min: int = 60
    temp_file_max_age_min: int = 30
    cleanup_interval_sec: int = 300

    nta_app_id: str = field(default="", repr=False)
    nta_timeout_sec: int = 10

    prices: dict[str, tuple[float, float]] = field(default_factory=dict)

    # 売上モード（発行した領収書）の発行者情報。動画からは読まず、この値を使う
    self_name: str = ""
    self_invoice_no: str = ""
    self_address: str = ""
    sales_categories: list[str] = field(default_factory=lambda: list(DEFAULT_SALES_CATEGORIES))

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    @property
    def uploads_dir(self) -> Path:
        return self.work_dir / "uploads"

    @property
    def is_live(self) -> bool:
        return self.gemini_mode == "live"


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    s = Settings(
        gemini_mode=os.getenv("GEMINI_MODE", "mock").strip().lower(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
        gemini_media_resolution=os.getenv("GEMINI_MEDIA_RESOLUTION", "").strip().lower(),
        gemini_fps=_float("GEMINI_FPS", 1.0),
        gemini_thinking_level=os.getenv("GEMINI_THINKING_LEVEL", "low").strip().lower(),
        gemini_timeout_sec=_int("GEMINI_TIMEOUT_SEC", 300),
        gemini_max_attempts=_int("GEMINI_MAX_ATTEMPTS", 3),
        gemini_file_active_timeout_sec=_int("GEMINI_FILE_ACTIVE_TIMEOUT_SEC", 300),
        strip_audio=_bool("STRIP_AUDIO", True),
        invite_codes=parse_invite_codes(os.getenv("INVITE_CODES", "")),
        session_secret=os.getenv("SESSION_SECRET", "").strip(),
        admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
        cookie_secure=_bool("COOKIE_SECURE", False),
        daily_limit_per_user=_int("DAILY_LIMIT_PER_USER", 10),
        daily_limit_total=_int("DAILY_LIMIT_TOTAL", 50),
        max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2),
        max_upload_mb=_int("MAX_UPLOAD_MB", 500),
        max_duration_sec=_int("MAX_DURATION_SEC", 180),
        result_ttl_min=_int("RESULT_TTL_MIN", 60),
        temp_file_max_age_min=_int("TEMP_FILE_MAX_AGE_MIN", 30),
        cleanup_interval_sec=_int("CLEANUP_INTERVAL_SEC", 300),
        nta_app_id=os.getenv("NTA_APP_ID", "").strip(),
        prices=parse_prices(os.getenv(
            "GEMINI_PRICES",
            "gemini-3.8-flash=0.75/3.75,gemini-3.6-flash=0.75/3.75,"
            "gemini-3.5-flash-lite=0.30/2.50,gemini-3.1-flash-lite=0.25/1.50",
        )),
        self_name=os.getenv("SELF_NAME", "").strip(),
        self_invoice_no=os.getenv("SELF_INVOICE_NO", "").strip(),
        self_address=os.getenv("SELF_ADDRESS", "").strip(),
        sales_categories=[c.strip() for c in os.getenv("SALES_CATEGORIES", "").split(",") if c.strip()]
        or list(DEFAULT_SALES_CATEGORIES),
        ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg").strip(),
        ffprobe_bin=os.getenv("FFPROBE_BIN", "ffprobe").strip(),
    )
    if os.getenv("WORK_DIR", "").strip():
        s.work_dir = Path(os.environ["WORK_DIR"].strip())
    if os.getenv("USAGE_DB_PATH", "").strip():
        s.usage_db_path = Path(os.environ["USAGE_DB_PATH"].strip())
    validate(s)
    return s


def validate(s: Settings) -> None:
    if s.gemini_mode not in ("mock", "live"):
        raise ValueError("GEMINI_MODE は mock か live を指定してください")
    if s.is_live and not s.gemini_api_key:
        raise ValueError("GEMINI_MODE=live には GEMINI_API_KEY が必要です")
    if "onedrive" in str(s.work_dir).lower():
        raise ValueError("WORK_DIR が OneDrive 配下です。同期対象外の場所（%TEMP% 等）を指定してください")
    if s.self_invoice_no:
        import re
        import unicodedata

        s.self_invoice_no = re.sub(r"[\s\-]", "", unicodedata.normalize("NFKC", s.self_invoice_no)).upper()
        if not re.fullmatch(r"T\d{13}", s.self_invoice_no):
            raise ValueError("SELF_INVOICE_NO は T＋13桁で指定してください")
    if not s.session_secret:
        # 再起動でセッションが切れるだけなので、ローカル試用ではランダム値で動かす
        s.session_secret = secrets.token_urlsafe(32)
