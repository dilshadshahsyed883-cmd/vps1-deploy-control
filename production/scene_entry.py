"""Scene Builder production entrypoint behind the authenticated workspace."""
from __future__ import annotations

import os
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
import app as scene


class WorkspaceIdentity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in {'http', 'websocket'}:
            headers = dict(scope.get('headers', []))
            user = headers.get(b'x-auth-user', b'').decode('utf-8', errors='replace')
            if user and user == os.environ.get('WEB_USERNAME', 'admin'):
                account = scene.user_account(user)
                if account and int(account.get('is_active') or 0) and not account.get('subscription_expired'):
                    scope.setdefault('session', {}).update(authenticated=True, username=user,
                        is_admin=bool(account.get('is_admin')), app_access='both')
        await self.app(scope, receive, send)


app = scene.app
position = next(i for i, item in enumerate(app.user_middleware) if item.cls is SessionMiddleware)
app.user_middleware.insert(position + 1, Middleware(WorkspaceIdentity))
