'''Application-user lifecycle and password ownership.'''

from __future__ import annotations

import re
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import User, UserRole
from app.models.common import utc_now
from app.core.time_presentation import serialize_utc_datetime


MIN_PASSWORD_LENGTH = 12
_HASHER = PasswordHasher()


class UserServiceError(ValueError):
    pass


def normalize_username(value: str) -> str:
    return re.sub(r'\s+', '', value or '').casefold()


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserServiceError(f'비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.')
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


class UserService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_users(self) -> tuple[dict, ...]:
        users = self.session.scalars(select(User).order_by(User.username, User.id))
        return tuple(self.project(user) for user in users)

    def find_for_login(self, username: str) -> User | None:
        normalized = normalize_username(username)
        if not normalized:
            return None
        return self.session.scalar(select(User).where(User.normalized_username == normalized))

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.find_for_login(username)
        if user is None or not user.active or not verify_password(user.password_hash, password):
            return None
        user.last_login_at = utc_now()
        self.session.commit()
        return user

    def create(self, *, username: str, password: str, role: UserRole, display_name: str | None = None) -> User:
        clean = (username or '').strip()
        normalized = normalize_username(clean)
        if not clean or not normalized or len(clean) > 150:
            raise UserServiceError('올바른 아이디를 입력하세요.')
        user = User(
            username=clean,
            normalized_username=normalized,
            display_name=(display_name or '').strip() or None,
            password_hash=hash_password(password),
            role=role,
            active=True,
        )
        self.session.add(user)
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            raise UserServiceError('이미 사용 중인 아이디입니다.') from error
        return user

    def set_password(self, user_id: uuid.UUID, password: str) -> User:
        user = self._get(user_id)
        user.password_hash = hash_password(password)
        self.session.commit()
        return user

    def set_active(self, user_id: uuid.UUID, active: bool) -> User:
        user = self._get(user_id)
        if not active and user.active and user.role is UserRole.ADMIN:
            self._require_another_active_admin(user.id)
        user.active = active
        self.session.commit()
        return user

    def set_role(self, user_id: uuid.UUID, role: UserRole) -> User:
        user = self._get(user_id)
        if user.active and user.role is UserRole.ADMIN and role is not UserRole.ADMIN:
            self._require_another_active_admin(user.id)
        user.role = role
        self.session.commit()
        return user

    def _require_another_active_admin(self, excluded_id: uuid.UUID) -> None:
        count = self.session.scalar(
            select(func.count()).select_from(User).where(
                User.active.is_(True), User.role == UserRole.ADMIN, User.id != excluded_id
            )
        ) or 0
        if count == 0:
            raise UserServiceError('마지막 활성 관리자는 비활성화하거나 역할을 변경할 수 없습니다.')

    def _get(self, user_id: uuid.UUID) -> User:
        user = self.session.get(User, user_id)
        if user is None:
            raise UserServiceError('사용자를 찾을 수 없습니다.')
        return user

    @staticmethod
    def project(user: User) -> dict:
        return {
            'id': str(user.id), 'username': user.username,
            'display_name': user.display_name, 'role': user.role.value,
            'active': user.active,
            'last_login_at': serialize_utc_datetime(user.last_login_at),
            'created_at': serialize_utc_datetime(user.created_at),
        }
