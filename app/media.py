"""ffprobe / ffmpeg による動画の検証と前処理。"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

ALLOWED_EXTENSIONS = {".mp4", ".mov"}


class MediaError(Exception):
    """ユーザーに見せてよい日本語メッセージを持つ。"""


@dataclass
class ProbeResult:
    duration_sec: float
    has_audio: bool


def probe(path: Path, settings: Settings) -> ProbeResult:
    try:
        out = subprocess.run(
            [settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration,format_name",
             "-show_entries", "stream=codec_type", "-of", "json", str(path)],
            capture_output=True, timeout=60, check=True,
        ).stdout
        info = json.loads(out)
    except FileNotFoundError as e:
        raise MediaError("サーバに ffprobe が見つかりません") from e
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        raise MediaError("動画ファイルとして読み込めませんでした") from e

    fmt = info.get("format", {})
    if not any(n in fmt.get("format_name", "") for n in ("mov", "mp4")):
        raise MediaError("mp4 または mov 形式の動画をアップロードしてください")
    types = {s.get("codec_type") for s in info.get("streams", [])}
    if "video" not in types:
        raise MediaError("映像トラックが含まれていません")
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError) as e:
        raise MediaError("動画の長さを取得できませんでした") from e
    return ProbeResult(duration_sec=duration, has_audio="audio" in types)


def prepare(src: Path, dst: Path, probe_result: ProbeResult, settings: Settings) -> Path:
    """先頭 max_duration_sec 秒に切り詰め、必要なら音声を除去する（再エンコードなし）。

    音声は書類の読み取りに不要で、入力トークン（約32トークン/秒）を減らせる。
    """
    trim = probe_result.duration_sec > settings.max_duration_sec
    strip = settings.strip_audio and probe_result.has_audio
    if not trim and not strip:
        return src
    cmd = [settings.ffmpeg_bin, "-v", "error", "-y", "-i", str(src)]
    if trim:
        cmd += ["-t", str(settings.max_duration_sec)]
    cmd += ["-map", "0:v:0", "-c", "copy"]
    if not strip:
        cmd += ["-map", "0:a?"]
    cmd += ["-movflags", "+faststart", str(dst)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=300, check=True)
    except FileNotFoundError as e:
        raise MediaError("サーバに ffmpeg が見つかりません") from e
    except subprocess.SubprocessError as e:
        raise MediaError("動画の前処理に失敗しました") from e
    return dst
