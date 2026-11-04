"""ユーザーごとの訂正履歴（学習データ）を保存し、次回の読み取りのヒントにする。

- 保存するのは「AIの読み取り結果 → ユーザーが直した値」の文字だけ。動画や画像は保存しない。
- 本人が学習をオンにした場合だけ保存する。値は LEARNING_KEY で暗号化（Fernet）してから書き込む。
- 他のユーザーの訂正は、ヒントとしても使わない。
- 削除は2段階。本人が消すとまず「削除済み」になり、読み取りには使わなくなる。LEARNING_TRASH_DAYS 日以内なら
  本人（または管理者）が元に戻せ、過ぎると掃除処理で完全に消す。本人は「今すぐ完全に消す」も選べる。
- 最後の訂正から LEARNING_RETENTION_DAYS 日で自動削除する。
- 削除・復元の操作は、誰が・いつ・何件かだけを events に記録する（中身は記録しない）。
- ログには件数のみを出す。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MAX_VALUE_LEN = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consent (
    user_label TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_label TEXT NOT NULL,
    job_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    field TEXT NOT NULL,
    created_at REAL NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS corrections_user ON corrections(user_label, mode, created_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_label TEXT NOT NULL,
    action TEXT NOT NULL,   -- delete | restore | purge_now | auto_purge
    actor TEXT NOT NULL,    -- self | admin | system
    n INTEGER NOT NULL,
    at REAL NOT NULL
);
"""


def _norm(v) -> str:
    return "" if v is None else str(v).strip()


class LearningStore:
    def __init__(self, path: Path, key: str):
        self.available = bool(key)
        self._fernet = Fernet(key.encode()) if key else None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA secure_delete=ON")  # 完全に消した行をファイルの空き領域に残さない
        self._conn.executescript(_SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(corrections)")}
        if "deleted_at" not in cols:  # 2段階削除の追加前に作ったDBへの列追加
            self._conn.execute("ALTER TABLE corrections ADD COLUMN deleted_at REAL")
        self._lock = threading.Lock()

    def _event(self, user: str, action: str, actor: str, n: int) -> None:
        # 呼び出し側でロックとトランザクションを持っている前提
        self._conn.execute("INSERT INTO events(user_label, action, actor, n, at) VALUES (?,?,?,?,?)",
                           (user, action, actor, n, time.time()))

    # ---------------------------------------------------------- 同意

    def enabled(self, user: str) -> bool:
        if not self.available:
            return False
        with self._lock:
            row = self._conn.execute("SELECT enabled FROM consent WHERE user_label=?", (user,)).fetchone()
        return bool(row and row[0])

    def set_enabled(self, user: str, enabled: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO consent(user_label, enabled, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(user_label) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                (user, int(enabled), time.time()))

    # ---------------------------------------------------------- 訂正

    def save(self, user: str, job_id: str, mode: str, pairs: list[tuple[str, str, str]]) -> int:
        """(項目名, 読み取り結果, 訂正後) を保存する。同じジョブの分は置き換える。"""
        rows = []
        now = time.time()
        for field, original, corrected in pairs:
            payload = json.dumps({"o": original[:MAX_VALUE_LEN], "c": corrected[:MAX_VALUE_LEN]}, ensure_ascii=False)
            rows.append((user, job_id, mode, field, now, self._fernet.encrypt(payload.encode())))
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM corrections WHERE user_label=? AND job_id=? AND deleted_at IS NULL",
                               (user, job_id))
            self._conn.executemany(
                "INSERT INTO corrections(user_label, job_id, mode, field, created_at, payload) VALUES (?,?,?,?,?,?)",
                rows)
        return len(rows)

    def hints(self, user: str, mode: str, limit: int) -> list[dict]:
        """新しい順に、重複を除いた訂正例を最大 limit 件返す。"""
        if not self.enabled(user) or limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT field, payload FROM corrections WHERE user_label=? AND mode=? AND deleted_at IS NULL"
                " ORDER BY created_at DESC, id DESC",
                (user, mode)).fetchall()
        out, seen = [], set()
        for field, payload in rows:
            try:
                d = json.loads(self._fernet.decrypt(payload))
            except InvalidToken:  # 鍵を変えた後の古い行は読めないので使わない
                continue
            k = (field, d["o"], d["c"])
            if k in seen:
                continue
            seen.add(k)
            out.append({"field": field, "original": d["o"], "corrected": d["c"]})
            if len(out) >= limit:
                break
        return out

    def count(self, user: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM corrections WHERE user_label=? AND deleted_at IS NULL", (user,)).fetchone()[0]

    def trash(self, user: str) -> tuple[int, float | None]:
        """削除済み（まだ戻せる）の件数と、最初に完全に消える時刻の基準（いちばん古い deleted_at）。"""
        with self._lock:
            n, oldest = self._conn.execute(
                "SELECT COUNT(*), MIN(deleted_at) FROM corrections WHERE user_label=? AND deleted_at IS NOT NULL",
                (user,)).fetchone()
        return n, oldest

    def delete_all(self, user: str, actor: str = "self") -> int:
        """1段階目：削除済みにする（読み取りには使わなくなる）。"""
        with self._lock, self._conn:
            n = self._conn.execute("UPDATE corrections SET deleted_at=? WHERE user_label=? AND deleted_at IS NULL",
                                   (time.time(), user)).rowcount
            self._event(user, "delete", actor, n)
        return n

    def restore(self, user: str, actor: str = "self") -> int:
        with self._lock, self._conn:
            n = self._conn.execute("UPDATE corrections SET deleted_at=NULL WHERE user_label=? AND deleted_at IS NOT NULL",
                                   (user,)).rowcount
            self._event(user, "restore", actor, n)
        return n

    def purge_now(self, user: str) -> int:
        """本人の希望で、削除済みの分を待たずに今すぐ完全に消す（削除後に新しく貯まった分は残す）。"""
        with self._lock, self._conn:
            n = self._conn.execute("DELETE FROM corrections WHERE user_label=? AND deleted_at IS NOT NULL",
                                   (user,)).rowcount
            self._event(user, "purge_now", "self", n)
        return n

    def purge(self, retention_days: int, trash_days: int) -> int:
        """2段階目：削除から trash_days 日たった分と、最後の訂正から retention_days 日たったユーザーの分を完全に消す。"""
        now = time.time()
        trash_cutoff, retention_cutoff = now - trash_days * 86400, now - retention_days * 86400
        total = 0
        with self._lock, self._conn:
            # 対象のユーザーを先に決めてから消す（消している途中で MAX(created_at) が変わらないように）
            stale = {u for (u,) in self._conn.execute(
                "SELECT user_label FROM corrections GROUP BY user_label HAVING MAX(created_at) < ?",
                (retention_cutoff,))}
            trashed = {u for (u,) in self._conn.execute(
                "SELECT DISTINCT user_label FROM corrections WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (trash_cutoff,))}
            for user in stale | trashed:
                if user in stale:
                    n = self._conn.execute("DELETE FROM corrections WHERE user_label=?", (user,)).rowcount
                else:
                    n = self._conn.execute(
                        "DELETE FROM corrections WHERE user_label=? AND deleted_at IS NOT NULL AND deleted_at < ?",
                        (user, trash_cutoff)).rowcount
                self._event(user, "auto_purge", "system", n)
                total += n
        return total

    def events(self, user: str | None = None, limit: int = 100) -> list[dict]:
        q = "SELECT user_label, action, actor, n, at FROM events"
        args: tuple = ()
        if user:
            q, args = q + " WHERE user_label=?", (user,)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"user": u, "action": a, "actor": ac, "n": n, "at": at} for u, a, ac, n, at in rows]

    def close(self) -> None:
        self._conn.close()


def diff(original_docs: list[dict], edits: list[dict], allowed_fields: set[str]) -> list[tuple[str, str, str]]:
    """クライアントから届いた編集を、サーバが持つ元の読み取り結果と比べ、本当に変わった項目だけを返す。"""
    pairs = []
    for e in edits:
        i, field = e.get("index"), e.get("field")
        if not isinstance(i, int) or not 0 <= i < len(original_docs) or field not in allowed_fields:
            continue
        before, after = _norm(original_docs[i].get(field)), _norm(e.get("value"))
        if before != after and after:  # 空にした（消しただけ）の訂正は、癖の手がかりにならないので使わない
            pairs.append((field, before, after))
    return pairs
