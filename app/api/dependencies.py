from collections.abc import Iterator

import secrets
import uuid

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.models import User, UserRole


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


ROLE_LEVEL = {UserRole.VIEWER: 1, UserRole.OPERATOR: 2, UserRole.ADMIN: 3}


def get_current_user_optional(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    value = getattr(request.state, 'session', {}).get('user_id')
    try:
        user_id = uuid.UUID(value) if value else None
    except ValueError:
        user_id = None
    user = session.get(User, user_id) if user_id else None
    current = user if user is not None and user.active else None
    request.state.current_user = current
    return current


def ensure_csrf_token(request: Request) -> str:
    session = request.state.session
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


def require_authenticated(user: User | None = Depends(get_current_user_optional)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='로그인이 필요합니다.')
    return user


def _validate_csrf(request: Request) -> None:
    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return
    expected = request.state.session.get('csrf_token', '')
    provided = request.headers.get('X-CSRF-Token', '')
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='요청 보안 토큰이 올바르지 않습니다.')


def _require_role(minimum: UserRole):
    def dependency(request: Request, user: User = Depends(require_authenticated)) -> User:
        if ROLE_LEVEL[user.role] < ROLE_LEVEL[minimum]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='이 작업을 수행할 권한이 없습니다.')
        _validate_csrf(request)
        return user
    return dependency


require_viewer = _require_role(UserRole.VIEWER)
require_operator = _require_role(UserRole.OPERATOR)
require_admin = _require_role(UserRole.ADMIN)

