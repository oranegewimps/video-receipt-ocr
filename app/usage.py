"""ジョブごとの使用量（トークン数・所要時間など）を SQLite に記録する。

動画の中身や抽出結果は保存しない。日次上限の判定もこのテーブルで行い、
失敗したジョブも受け付けた時点で1回として数える。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    user_label TEXT NOT NULL,
    mode TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    size_bytes INTEGER,
    duration_sec REAL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    thought_tokens INTEGER,
    total_tokens INTEGER,
    elapsed_sec REAL,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS jobs_day ON jobs(day, user_label);
"""


def today() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def estimate_cost(model: str, usage: dict, prices: dict[str, tuple[float, float]]) -> float | None:
    """概算費用（USD）。出力単価には思考トークンも含めて計算する。単価未登録なら None。"""
    p = prices.get(model)
    if p is None:
        return None
    pin, pout = p
    out = (usage.get("output_tokens") or 0) + (usage.get("thought_tokens") or 0)
    return round((usage.get("input_tokens") or 0) * pin / 1e6 + out * pout / 1e6, 6)


class UsageStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        if "mode" not in cols:  # モード追加前に作ったDBへの列追加
            self._conn.execute("ALTER TABLE jobs ADD COLUMN mode TEXT")
        self._lock = threading.Lock()

    def _counts(self, day: str, user_label: str) -> tuple[int, int]:
        total = self._conn.execute("SELECT COUNT(*) FROM jobs WHERE day=?", (day,)).fetchone()[0]
        mine = self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE day=? AND user_label=?", (day, user_label)).fetchone()[0]
        return total, mine

    @staticmethod
    def _reason(total: int, mine: int, limit_user: int, limit_total: int) -> str | None:
        if total >= limit_total:
            return "本日の全体の解析回数の上限に達しました（明日またお試しください）"
        if mine >= limit_user:
            return "本日の解析回数の上限に達しました（明日またお試しください）"
        return None

    def limit_reason(self, user_label: str, limit_user: int, limit_total: int) -> str | None:
        with self._lock:
            return self._reason(*self._counts(today(), user_label), limit_user, limit_total)

    def try_reserve(self, job_id: str, user_label: str, limit_user: int, limit_total: int,
                    size_bytes: int, duration_sec: float, mode: str = "expense") -> str | None:
        """上限内なら行を追加して None、超えていれば理由を返す。"""
        day = today()
        with self._lock, self._conn:
            reason = self._reason(*self._counts(day, user_label), limit_user, limit_total)
            if reason:
                return reason
            self._conn.execute(
                "INSERT INTO jobs(job_id, day, user_label, mode, created_at, status, size_bytes, duration_sec)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (job_id, day, user_label, mode, datetime.now(JST).isoformat(timespec="seconds"),
                 "queued", size_bytes, duration_sec))
        return None

    def remaining(self, user_label: str, limit_user: int, limit_total: int) -> int:
        with self._lock:
            total, mine = self._counts(today(), user_label)
        return max(0, min(limit_user - mine, limit_total - total))

    def finish(self, job_id: str, status: str, model: str | None, usage: dict | None,
               elapsed_sec: float, cost_usd: float | None) -> None:
        usage = usage or {}
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status=?, model=?, input_tokens=?, output_tokens=?, thought_tokens=?,"
                " total_tokens=?, elapsed_sec=?, cost_usd=? WHERE job_id=?",
                (status, model, usage.get("input_tokens"), usage.get("output_tokens"),
                 usage.get("thought_tokens"), usage.get("total_tokens"), round(elapsed_sec, 2),
                 cost_usd, job_id))

    def summary(self, days: int = 7) -> dict:
        since = (datetime.now(JST) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, user_label, mode, status, COUNT(*), SUM(input_tokens), SUM(output_tokens),"
                " SUM(thought_tokens), SUM(cost_usd), AVG(elapsed_sec)"
                " FROM jobs WHERE day>=? GROUP BY day, user_label, mode, status ORDER BY day DESC, user_label",
                (since,)).fetchall()
        keys = ["day", "user", "mode", "status", "jobs", "input_tokens", "output_tokens",
                "thought_tokens", "cost_usd", "avg_elapsed_sec"]
        items = [dict(zip(keys, r)) for r in rows]
        return {
            "since": since,
            "rows": items,
            "total_jobs": sum(r["jobs"] for r in items),
            "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in items), 4),
        }

    def close(self) -> None:
        self._conn.close()
