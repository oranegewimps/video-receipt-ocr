"""動画から書類情報を抽出する。モデル呼び出しはこのモジュールの extract() に閉じ込める。

他のモデル（Claude 等）へ差し替えるときは、extract() と同じ戻り値を返す関数を追加して
GEMINI_MODE 相当の切り替えを足せばよい。

SDK: google-genai（Interactions API + Files API）。詳細は README の「Gemini API」節を参照。
"""
from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from pathlib import Path

from .config import Settings, load_settings
from . import mock_data
from .schema import MODES, prompt, response_schema

log = logging.getLogger("vro.extractor")

# Gemini 側にアップロードするファイルの表示名プレフィックス。掃除処理でこのアプリのファイルだけを消すのに使う。
GEMINI_FILE_PREFIX = "vro-"


class ExtractionError(Exception):
    """抽出に失敗した。メッセージには動画の中身を含めない。"""


def extract(video_path: str | Path, mode: str = "expense", *, settings: Settings | None = None,
            model: str | None = None) -> dict:
    """動画を解析して書類一覧を返す。mode は expense | sales | bizcard（項目は app/schema.py）。

    戻り値:
        {
          "documents": [ {doc_type?, first_seen, <モードの項目>..., low_confidence_fields}, ... ],
          "model": "gemini-...",
          "usage": {"input_tokens", "output_tokens", "thought_tokens", "total_tokens"},
          "mock": bool,
        }
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    settings = settings or load_settings()
    if settings.is_live:
        return _extract_gemini(Path(video_path), mode, settings, model or settings.gemini_model)
    return _extract_mock(mode, model or settings.gemini_model)


# ---------------------------------------------------------------- mock

def _extract_mock(mode: str, model: str) -> dict:
    time.sleep(1.5)  # ポーリングの動作確認用
    return {
        "documents": copy.deepcopy(mock_data.DOCUMENTS[mode]),
        "model": f"mock({model})",
        "usage": {"input_tokens": 0, "output_tokens": 0, "thought_tokens": 0, "total_tokens": 0},
        "mock": True,
    }


# ---------------------------------------------------------------- Gemini

def make_client(settings: Settings):
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(
            timeout=settings.gemini_timeout_sec * 1000,
            retry_options=types.HttpRetryOptions(attempts=settings.gemini_max_attempts),
        ),
    )


def _wait_active(client, file, deadline_sec: int):
    deadline = time.monotonic() + deadline_sec
    while True:
        state = file.state.name if file.state else "STATE_UNSPECIFIED"
        if state == "ACTIVE":
            return file
        if state == "FAILED":
            raise ExtractionError("Gemini側で動画の処理に失敗しました")
        if time.monotonic() > deadline:
            raise ExtractionError("Gemini側の動画処理がタイムアウトしました")
        time.sleep(3)
        file = client.files.get(name=file.name)


def _extract_gemini(video_path: Path, mode: str, settings: Settings, model: str) -> dict:
    client = make_client(settings)
    uploaded = None
    try:
        uploaded = client.files.upload(
            file=str(video_path),
            config={
                "mime_type": "video/mp4",
                "display_name": f"{GEMINI_FILE_PREFIX}{uuid.uuid4().hex[:12]}",
                # 大きい動画のアップロードは通常のタイムアウトより長く待つ
                "http_options": {"timeout": max(settings.gemini_timeout_sec, 900) * 1000},
            },
        )
        uploaded = _wait_active(client, uploaded, settings.gemini_file_active_timeout_sec)

        video_part: dict = {"type": "video", "uri": uploaded.uri, "mime_type": uploaded.mime_type}
        video_part["processing"] = {"type": "static", "fps": settings.gemini_fps}
        if settings.gemini_media_resolution:
            video_part["resolution"] = settings.gemini_media_resolution

        generation_config: dict = {}
        if settings.gemini_thinking_level:
            generation_config["thinking_level"] = settings.gemini_thinking_level

        interaction = client.interactions.create(
            model=model,
            input=[video_part, {"type": "text", "text": prompt(mode, settings)}],
            response_mime_type="application/json",
            response_format={"type": "text", "mime_type": "application/json", "schema": response_schema(mode)},
            generation_config=generation_config,
            store=False,  # 既定は True（サーバ側に保存）なので必ず無効にする
            timeout=float(settings.gemini_timeout_sec),
        )
        if interaction.status != "completed":
            raise ExtractionError(f"モデルの応答が完了しませんでした（status={interaction.status}）")

        try:
            data = json.loads(interaction.output_text or "")
        except json.JSONDecodeError as e:
            raise ExtractionError("モデルの出力をJSONとして解釈できませんでした") from e

        u = interaction.usage
        usage = {
            "input_tokens": (u.total_input_tokens if u else None) or 0,
            "output_tokens": (u.total_output_tokens if u else None) or 0,
            "thought_tokens": (u.total_thought_tokens if u else None) or 0,
            "total_tokens": (u.total_tokens if u else None) or 0,
        }
        return {"documents": data.get("documents") or [], "model": model, "usage": usage, "mock": False}
    finally:
        if uploaded is not None:
            delete_gemini_file(client, uploaded.name)


def delete_gemini_file(client, name: str) -> None:
    try:
        client.files.delete(name=name)
    except Exception as e:  # 消せなくても48時間で自動削除されるが、掃除処理で再試行する
        log.warning("gemini file delete failed: %s", type(e).__name__)


def purge_gemini_files(settings: Settings, older_than_sec: int = 0) -> int:
    """このアプリが上げた（表示名が vro- で始まる）Gemini 上のファイルを削除する。"""
    if not settings.is_live:
        return 0
    from datetime import datetime, timezone

    client = make_client(settings)
    now = datetime.now(timezone.utc)
    n = 0
    for f in client.files.list():
        if not (f.display_name or "").startswith(GEMINI_FILE_PREFIX):
            continue
        if older_than_sec and f.create_time and (now - f.create_time).total_seconds() < older_than_sec:
            continue
        delete_gemini_file(client, f.name)
        n += 1
    return n
