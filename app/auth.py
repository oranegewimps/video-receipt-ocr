"""招待コード認証。正しいコードを入力すると、署名付きのセッションCookieを発行する。"""
from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import Settings

COOKIE_NAME = "vro_session"


class Auth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._ser = URLSafeTimedSerializer(settings.session_secret, salt="vro-session")
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check_code(self, code: str) -> str | None:
        """一致した招待コードのラベルを返す。比較は定数時間で行う。"""
        found = None
        for c, label in self.settings.invite_codes.items():
            if hmac.compare_digest(c.encode(), code.encode()):
                found = label
        return found

    def too_many_failures(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            self._fails[ip] = [t for t in self._fails[ip] if now - t < 600]
            return len(self._fails[ip]) >= 10

    def record_failure(self, ip: str) -> None:
        with self._lock:
            self._fails[ip].append(time.time())

    def issue(self, label: str) -> str:
        return self._ser.dumps({"u": label})

    def user_from_request(self, request: Request) -> str | None:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return None
        try:
            data = self._ser.loads(token, max_age=self.settings.session_max_age_sec)
        except BadSignature:
            return None
        label = data.get("u")
        # 招待コードを環境変数から外したユーザーは即座に無効にする
        return label if label in self.settings.invite_codes.values() else None

    def require_user(self, request: Request) -> str:
        user = self.user_from_request(request)
        if not user:
            raise HTTPException(status_code=401, detail="ログインしてください")
        return user

    def require_admin(self, request: Request) -> None:
        token = request.headers.get("X-Admin-Token", "")
        if not self.settings.admin_token or not hmac.compare_digest(token, self.settings.admin_token):
            raise HTTPException(status_code=403, detail="forbidden")
