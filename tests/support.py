'''Test-only dependency overrides for pre-05A business regression tests.'''
from types import SimpleNamespace
from pathlib import Path

from fastapi import Request
from sqlalchemy.engine import make_url

from app.api.dependencies import require_admin, require_operator, require_viewer
from app.main import create_app
from app.models import UserRole


def regression_app(*args, **kwargs):
    database_url = args[0] if args else kwargs.get('database_url')
    if database_url and 'project_root' not in kwargs:
        database_path = make_url(database_url).database
        if database_path and database_path != ':memory:':
            kwargs['project_root'] = Path(database_path).resolve().parent
    app = create_app(*args, **kwargs)

    def authorized(request: Request):
        user = SimpleNamespace(
            id='00000000-0000-0000-0000-000000000001', username='test-admin',
            display_name='Test Admin', role=UserRole.ADMIN, active=True,
        )
        request.state.current_user = user
        request.state.session.setdefault('csrf_token', 'test-csrf')
        return user

    app.dependency_overrides[require_viewer] = authorized
    app.dependency_overrides[require_operator] = authorized
    app.dependency_overrides[require_admin] = authorized
    return app
