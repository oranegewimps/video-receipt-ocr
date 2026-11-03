"""数本の動画で、モデルごとの精度・トークン数・概算費用を測る。

使い方:
  python scripts/evaluate.py samples/ --mode sales          # .env の GEMINI_MODEL で評価
  python scripts/evaluate.py samples/ --mode expense --models gemini-3.8-flash,gemini-3.5-flash-lite
  python scripts/evaluate.py a.mp4 --mode bizcard --gemini-mode mock   # 外部通信なしで動作確認

--mode は書類モード（expense=経費精算 / sales=売上管理 / bizcard=名刺）。

正解データ（任意）: 動画と同じ場所に <動画名>.expected.json を置くと精度も出す。
  {"documents": [{"issue_date": "2026-10-30", "customer_name": "上様", "amount_total": 3300, ...}, ...]}
  項目名は --mode に対応するスキーマ（app/schema.py）と同じ。
  正解側で値が入っている項目だけを採点する。

live モードでは「動画数 × モデル数」回の API 呼び出し（課金）が発生するので、実行前に確認する。
結果CSVには指標のみを書き、抽出内容は書かない（--save-output を付けた場合のみ JSON を保存）。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import extractor, media, postprocess  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.schema import MODES, fields  # noqa: E402
from app.usage import estimate_cost  # noqa: E402


def find_videos(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(x for x in p.iterdir() if x.suffix.lower() in media.ALLOWED_EXTENSIONS)
        elif p.suffix.lower() in media.ALLOWED_EXTENSIONS:
            out.append(p)
    return out


def _n(v) -> str:
    if v is None:
        return ""
    return unicodedata.normalize("NFKC", str(v)).replace(" ", "").replace("　", "").lower()


def score(expected: list[dict], predicted: list[dict], mode: str) -> dict:
    """正解の各書類に、予測を最も一致する順に1対1で割り当て、項目単位の一致率を出す。"""
    remaining = list(predicted)
    correct = total = matched_docs = 0
    for e in expected:
        names = [f for f in fields(mode) if e.get(f) is not None]
        total += len(names)
        if not remaining:
            continue
        best = max(remaining, key=lambda p: sum(_n(p.get(f)) == _n(e.get(f)) for f in names))
        hit = sum(_n(best.get(f)) == _n(e.get(f)) for f in names)
        remaining.remove(best)
        matched_docs += 1
        correct += hit
    return {
        "expected_docs": len(expected),
        "predicted_docs": len(predicted),
        "missed_docs": len(expected) - matched_docs,
        "extra_docs": len(remaining),
        "field_accuracy": round(correct / total, 3) if total else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="動画ファイルまたはフォルダ")
    ap.add_argument("--models", help="カンマ区切りのモデル名（既定: GEMINI_MODEL）")
    ap.add_argument("--mode", choices=MODES, default="expense", help="書類モード（既定: expense）")
    ap.add_argument("--gemini-mode", choices=["mock", "live"], help="GEMINI_MODE を上書き")
    ap.add_argument("--out", default="eval_results", help="結果CSVの保存先フォルダ")
    ap.add_argument("--save-output", action="store_true", help="抽出結果JSONも保存する（個人情報に注意）")
    ap.add_argument("--yes", action="store_true", help="live 実行前の確認を省略")
    args = ap.parse_args()

    settings = load_settings()
    if args.gemini_mode:
        settings.gemini_mode = args.gemini_mode
        if settings.is_live and not settings.gemini_api_key:
            print("GEMINI_API_KEY が設定されていません", file=sys.stderr)
            return 2
    models = [m.strip() for m in (args.models or settings.gemini_model).split(",") if m.strip()]
    videos = find_videos(args.paths)
    if not videos:
        print("動画が見つかりません（mp4/mov）", file=sys.stderr)
        return 2

    calls = len(videos) * len(models)
    print(f"gemini={settings.gemini_mode}  mode={args.mode}  動画 {len(videos)} 本 × モデル {len(models)} 個 = {calls} 回の解析")
    if settings.is_live and not args.yes:
        if input("Gemini API を呼び出します（課金が発生します）。続行しますか？ [y/N] ").strip().lower() != "y":
            print("中止しました")
            return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rows: list[dict] = []
    tmp_root = settings.work_dir / "eval"
    tmp_root.mkdir(parents=True, exist_ok=True)

    for video in videos:
        expected_path = video.with_name(video.stem + ".expected.json")
        expected = json.loads(expected_path.read_text(encoding="utf-8"))["documents"] \
            if expected_path.exists() else None
        probe = media.probe(video, settings)
        for model in models:
            work = tmp_root / uuid.uuid4().hex
            work.mkdir()
            row = {"video": video.name, "mode": args.mode, "model": model, "duration_sec": round(probe.duration_sec, 1),
                   "size_mb": round(video.stat().st_size / 1048576, 1)}
            t0 = time.monotonic()
            try:
                prepared = media.prepare(video, work / "prepared.mp4", probe, settings)
                result = extractor.extract(prepared, args.mode, settings=settings, model=model)
                docs = postprocess.finalize(result["documents"], args.mode, settings)
                u = result["usage"]
                analyzed = min(probe.duration_sec, settings.max_duration_sec)
                row.update({
                    "status": "ok",
                    "docs": len(docs),
                    "low_conf_fields": sum(len(d.get("low_confidence_fields") or []) for d in docs),
                    "input_tokens": u.get("input_tokens"),
                    "output_tokens": u.get("output_tokens"),
                    "thought_tokens": u.get("thought_tokens"),
                    "input_tokens_per_min": round(u.get("input_tokens", 0) / (analyzed / 60)) if analyzed else None,
                    "cost_usd": estimate_cost(model, u, settings.prices),
                })
                if expected is not None:
                    row.update(score(expected, docs, args.mode))
                if args.save_output:
                    (out_dir / f"{stamp}_{video.stem}_{args.mode}_{model}.json").write_text(
                        json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                row.update({"status": f"error:{type(e).__name__}"})
            finally:
                shutil.rmtree(work, ignore_errors=True)
                row["elapsed_sec"] = round(time.monotonic() - t0, 1)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False))

    keys = ["video", "mode", "model", "status", "duration_sec", "size_mb", "docs", "low_conf_fields",
            "expected_docs", "predicted_docs", "missed_docs", "extra_docs", "field_accuracy",
            "input_tokens", "output_tokens", "thought_tokens", "input_tokens_per_min",
            "cost_usd", "elapsed_sec"]
    report = out_dir / f"eval_{stamp}.csv"
    with open(report, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print("\n=== モデル別の集計 ===")
    for model in models:
        ok = [r for r in rows if r["model"] == model and r["status"] == "ok"]
        if not ok:
            print(f"{model}: 成功なし")
            continue
        costs = [r["cost_usd"] for r in ok if r.get("cost_usd") is not None]
        accs = [r["field_accuracy"] for r in ok if r.get("field_accuracy") is not None]
        avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
        print(f"{model}: 成功 {len(ok)}/{len(videos)} 本"
              f" / 入力 平均 {avg([r['input_tokens'] for r in ok]):,.0f} tok"
              f" / 出力+思考 平均 {avg([r['output_tokens'] + r['thought_tokens'] for r in ok]):,.0f} tok"
              + (f" / 概算 平均 ${avg(costs):.4f}（約{avg(costs) * 150:.1f}円, 1USD=150円）" if costs else
                 " / 単価未登録（GEMINI_PRICES）")
              + (f" / 項目一致率 平均 {avg(accs):.1%}" if accs else ""))
    print(f"\n結果: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
