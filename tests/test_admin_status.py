import io
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import admin_setup
import auth
import database
import validate


class AdminStatusTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        with patch.dict(os.environ, {
            'DATABASE_URL': '',
            'PDM_DB_PATH': os.path.join(self.temp.name, 'users.db'),
        }):
            import app

        import bottle

        self.app = app
        self.wsgi = bottle.app()

        self.db = database.Database(
            'sqlite',
            os.path.join(self.temp.name, 'test.db')
        )
        self.db.initialize_schema()

        self.setup = admin_setup.AdminSetupFacade(self.db)
        self.setup.setup_admin('admin001', 'Abcdefg1')

        patcher = patch.multiple(
            app,
            db=self.db,
            auth_service=auth.AuthService(self.db),
            admin_setup_service=self.setup,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, data=None, cookie=None):
        body = urlencode(data).encode() if data is not None else b''

        environ = {
            'REQUEST_METHOD': 'POST' if data is not None else 'GET',
            'PATH_INFO': path,
            'QUERY_STRING': '',
            'SERVER_NAME': 'localhost',
            'SERVER_PORT': '8080',
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'CONTENT_LENGTH': str(len(body)),
            'CONTENT_TYPE': 'application/x-www-form-urlencoded',
            'wsgi.url_scheme': 'http',
            'wsgi.input': io.BytesIO(body),
            'wsgi.errors': io.StringIO(),
            'wsgi.version': (1, 0),
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
        }

        if cookie:
            environ['HTTP_COOKIE'] = cookie

        result = {}

        def start_response(status, headers, exc_info=None):
            result.update(
                status=int(status.split()[0]),
                headers=dict(headers)
            )

        chunks = self.wsgi(environ, start_response)

        try:
            result['body'] = b''.join(chunks)
        finally:
            if hasattr(chunks, 'close'):
                chunks.close()

        return result

    def login_admin(self):
        result = self.request(
            '/api/admin/login',
            {
                'username': 'admin001',
                'password': 'Abcdefg1'
            }
        )

        return result['headers']['Set-Cookie'].split(';')[0]

    def create_normal_user(self):
        with self.db.transaction(write=True) as store:
            return store.create_user(
                'alice123',
                validate.hash_password('Abcdefg1')
            )

    # Admin can disable a normal user.
    def test_admin_can_disable_user(self):
        user = self.create_normal_user()
        admin_cookie = self.login_admin()

        result = self.request(
            f"/api/admin/users/{user['id']}/status",
            {'enabled': 'false'},
            admin_cookie
        )

        self.assertEqual(result['status'], 200)

        with self.db.transaction() as store:
            updated = store.get_user_by_id(user['id'])
            self.assertIs(updated['enabled'], False)

    # Disable removes the user's existing session.
    def test_disable_invalidates_existing_session(self):
        user = self.create_normal_user()

        login = self.request(
            '/api/login',
            {
                'username': 'alice123',
                'password': 'Abcdefg1'
            }
        )

        user_cookie = login['headers']['Set-Cookie'].split(';')[0]

        admin_cookie = self.login_admin()

        self.request(
            f"/api/admin/users/{user['id']}/status",
            {'enabled': 'false'},
            admin_cookie
        )

        result = self.request(
            '/api/me',
            cookie=user_cookie
        )

        self.assertEqual(result['status'], 401)

    # A failure after changing status and deleting sessions restores both.
    def test_failed_disable_rolls_back_status_and_sessions(self):
        user = self.create_normal_user()
        admin_cookie = self.login_admin()
        with self.db.transaction(write=True) as store:
            other = store.create_user('bobuser1', validate.hash_password('Abcdefg1'))
            store.create_session('target-session-1', user['id'])
            store.create_session('target-session-2', user['id'])
            store.create_session('other-session', other['id'])

        set_user_enabled = database.Store.set_user_enabled

        def fail_after_disable(store, user_id, enabled):
            set_user_enabled(store, user_id, enabled)
            raise database.StorageError('Simulated failure before commit.')

        with patch.object(database.Store, 'set_user_enabled', fail_after_disable):
            result = self.request(
                f"/api/admin/users/{user['id']}/status",
                {'enabled': 'false'},
                admin_cookie,
            )

        self.assertEqual(result['status'], 503)
        with self.db.transaction() as store:
            self.assertEqual(store.get_user_by_id(user['id']), user)
            for token in ('target-session-1', 'target-session-2'):
                restored = store.get_user_by_token(token)
                self.assertIsNotNone(restored)
                self.assertEqual(restored['id'], user['id'])
            self.assertEqual(store.get_user_by_id(other['id']), other)
            self.assertIsNotNone(store.get_user_by_token('other-session'))

    # Disabled users cannot log in again.
    def test_disabled_user_cannot_login(self):
        user = self.create_normal_user()
        admin_cookie = self.login_admin()

        self.request(
            f"/api/admin/users/{user['id']}/status",
            {'enabled': 'false'},
            admin_cookie
        )

        result = self.request(
            '/api/login',
            {
                'username': 'alice123',
                'password': 'Abcdefg1'
            }
        )

        self.assertEqual(result['status'], 401)

    # Re-enabled users can log in again.
    def test_reenabled_user_can_login(self):
        user = self.create_normal_user()
        admin_cookie = self.login_admin()

        self.request(
            f"/api/admin/users/{user['id']}/status",
            {'enabled': 'false'},
            admin_cookie
        )

        self.request(
            f"/api/admin/users/{user['id']}/status",
            {'enabled': 'true'},
            admin_cookie
        )

        result = self.request(
            '/api/login',
            {
                'username': 'alice123',
                'password': 'Abcdefg1'
            }
        )

        self.assertEqual(result['status'], 200)

    # Invalid status values are rejected.
    def test_invalid_status_is_rejected(self):
        user = self.create_normal_user()
        admin_cookie = self.login_admin()

        result = self.request(
            f"/api/admin/users/{user['id']}/status",
            {'enabled': 'maybe'},
            admin_cookie
        )

        self.assertEqual(result['status'], 400)


if __name__ == '__main__':
    unittest.main()
