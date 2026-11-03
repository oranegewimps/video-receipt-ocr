"""一時ディレクトリ（と、--gemini 指定時は Gemini 側）に動画が残っていないかを確認する。

  python scripts/check_tmp.py            # ローカルの作業ディレクトリを一覧
  python scripts/check_tmp.py --gemini   # live 設定で、このアプリが上げた Gemini 上のファイルも一覧（外部通信あり）

何も残っていなければ終了コード 0、残っていれば 1。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import extractor  # noqa: E402
from app.config import load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gemini", action="store_true")
    args = ap.parse_args()
    s = load_settings()

    leftovers = 0
    print(f"作業ディレクトリ: {s.work_dir}")
    for sub in ("uploads", "eval"):
        d = s.work_dir / sub
        if not d.exists():
            print(f"  {sub}/ : (存在しない)")
            continue
        files = [p for p in d.rglob("*")]
        print(f"  {sub}/ : {len(files)} 件")
        for p in files:
            age = int(time.time() - p.stat().st_mtime)
            size = p.stat().st_size if p.is_file() else 0
            print(f"    {p.relative_to(s.work_dir)}  {size:,} bytes  {age}秒前")
        leftovers += len(files)

    if args.gemini:
        if not s.is_live:
            print("Gemini: GEMINI_MODE=live ではないため確認しません")
        else:
            client = extractor.make_client(s)
            mine = [f for f in client.files.list() if (f.display_name or "").startswith(extractor.GEMINI_FILE_PREFIX)]
            print(f"Gemini Files API 上のこのアプリのファイル: {len(mine)} 件")
            for f in mine:
                print(f"    {f.name}  {f.display_name}  作成 {f.create_time}")
            leftovers += len(mine)

    print("OK: 残っているファイルはありません" if leftovers == 0 else "注意: ファイルが残っています")
    return 0 if leftovers == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
