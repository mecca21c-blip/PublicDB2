'''Portable signed-session ownership and central browser security headers.'''

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import RuntimePaths, SESSION_SECRET_ENV, secure_cookie_enabled, session_max_age_seconds


SESSION_COOKIE = 'publicdb2_session'


class SecretStore:
    def __init__(self, paths: RuntimePaths) -> None:
        self.path = paths.config_root / 'security.json'
        self._secret: str | None = None

    def get(self) -> str:
        configured = os.getenv(SESSION_SECRET_ENV)
        if configured:
            if len(configured) < 32:
                raise RuntimeError('PUBLICDB2_SESSION_SECRET는 32자 이상이어야 합니다.')
            return configured
        if self._secret:
            return self._secret
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            secret = payload['session_secret']
            if not isinstance(secret, str) or len(secret) < 32:
                raise ValueError
            self._secret = secret
            return secret
        except FileNotFoundError:
            pass
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError('세션 보안 설정 파일이 올바르지 않습니다.') from error
        self.path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48)
        temporary = self.path.with_name(f'.{self.path.name}.{secrets.token_hex(8)}.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump({'session_secret': secret}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._secret = secret
        return secret


class SignedSessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, store: SecretStore) -> None:
        super().__init__(app)
        self.store = store

    async def dispatch(self, request: Request, call_next):
        session: dict[str, str] = {}
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            try:
                value = URLSafeTimedSerializer(self.store.get(), salt='publicdb2-session').loads(
                    token, max_age=session_max_age_seconds()
                )
                if isinstance(value, dict):
                    session = {str(key): str(item) for key, item in value.items()}
            except (BadSignature, SignatureExpired):
                session = {}
        request.state.session = session
        request.state.clear_session = False
        response = await call_next(request)
        if request.state.clear_session:
            response.delete_cookie(SESSION_COOKIE, path='/', httponly=True, samesite='lax')
        elif session:
            signed = URLSafeTimedSerializer(self.store.get(), salt='publicdb2-session').dumps(session)
            response.set_cookie(
                SESSION_COOKIE, signed, max_age=session_max_age_seconds(), path='/',
                httponly=True, samesite='lax', secure=secure_cookie_enabled(),
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response
