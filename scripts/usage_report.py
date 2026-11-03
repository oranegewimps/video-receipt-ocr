"""使用量DBから、日別・ユーザー別のジョブ数・トークン数・概算費用を表示する（サーバ起動不要）。

  python scripts/usage_report.py --days 30
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.usage import UsageStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    s = load_settings()
    store = UsageStore(s.usage_db_path)
    summary = store.summary(args.days)
    store.close()

    print(f"{summary['since']} 以降")
    print(f"{'日付':<10} {'ユーザー':<10} {'モード':<8} {'状態':<6} {'件数':>4} {'入力tok':>10} {'出力tok':>9} {'思考tok':>9} {'費用USD':>9} {'平均秒':>7}")
    for r in summary["rows"]:
        print(f"{r['day']:<10} {r['user']:<10} {r['mode'] or '-':<8} {r['status']:<6} {r['jobs']:>4} {r['input_tokens'] or 0:>10,}"
              f" {r['output_tokens'] or 0:>9,} {r['thought_tokens'] or 0:>9,} {r['cost_usd'] or 0:>9.4f}"
              f" {r['avg_elapsed_sec'] or 0:>7.1f}")
    print(f"合計 {summary['total_jobs']} 件 / 概算 ${summary['total_cost_usd']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
