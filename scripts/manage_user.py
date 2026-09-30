'''Manage PublicDB2 application users without exposing passwords in shell history.'''
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.engine import create_db_engine
from app.db.session import create_session_factory
from app.models import UserRole
from app.services.user_service import UserService, UserServiceError


def password_prompt():
    first = getpass.getpass('비밀번호: ')
    second = getpass.getpass('비밀번호 확인: ')
    if first != second:
        raise UserServiceError('비밀번호가 일치하지 않습니다.')
    return first


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--username', required=True)
    create.add_argument('--display-name')
    create.add_argument('--role', choices=[role.value for role in UserRole], required=True)
    commands.add_parser('list')
    for name in ('set-password', 'activate', 'deactivate', 'set-role'):
        command = commands.add_parser(name)
        command.add_argument('--username', required=True)
        if name == 'set-role':
            command.add_argument('--role', choices=[role.value for role in UserRole], required=True)
    return root


def main():
    args = parser().parse_args()
    engine = create_db_engine()
    session = create_session_factory(engine)()
    service = UserService(session)
    try:
        if args.command == 'create':
            user = service.create(username=args.username, display_name=args.display_name, role=UserRole(args.role), password=password_prompt())
            print(f'사용자 생성 완료: {user.username} ({user.role.value})')
        elif args.command == 'list':
            for item in service.list_users():
                print(f"{item['username']}\t{item['role']}\t{'active' if item['active'] else 'inactive'}")
        else:
            user = service.find_for_login(args.username)
            if user is None:
                raise UserServiceError('사용자를 찾을 수 없습니다.')
            if args.command == 'set-password':
                service.set_password(user.id, password_prompt())
            elif args.command == 'activate':
                service.set_active(user.id, True)
            elif args.command == 'deactivate':
                service.set_active(user.id, False)
            elif args.command == 'set-role':
                service.set_role(user.id, UserRole(args.role))
            print(f'사용자 변경 완료: {user.username}')
    except UserServiceError as error:
        session.rollback()
        raise SystemExit(str(error)) from error
    finally:
        session.close()
        engine.dispose()


if __name__ == '__main__':
    main()
