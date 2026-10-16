import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import admin_setup
import auth
import database
import validate


class AdminUsersTests(unittest.TestCase):

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

    # Admin can view normal users and required user information.
    def test_admin_can_list_normal_users(self):
        user = self.create_normal_user()

        with self.db.transaction(write=True) as store:
            store.insert_file(
                user['id'],
                'test.txt',
                'document',
                b'test'
            )

        admin_cookie = self.login_admin()

        result = self.request(
            '/api/admin/users',
            cookie=admin_cookie
        )

        self.assertEqual(result['status'], 200)

        body = json.loads(result['body'])
        self.assertEqual(len(body['users']), 1)

        listed_user = body['users'][0]

        self.assertEqual(listed_user['username'], 'alice123')
        self.assertIs(listed_user['enabled'], True)
        self.assertEqual(listed_user['file_quota'], 100)
        self.assertEqual(listed_user['file_count'], 1)

    # Empty normal-user list is returned correctly.
    def test_empty_normal_user_list(self):
        admin_cookie = self.login_admin()

        result = self.request(
            '/api/admin/users',
            cookie=admin_cookie
        )

        self.assertEqual(result['status'], 200)

        body = json.loads(result['body'])
        self.assertEqual(body['users'], [])

    # User must be logged in.
    def test_unauthenticated_user_cannot_list_users(self):
        result = self.request('/api/admin/users')

        self.assertEqual(result['status'], 401)

    # Normal users cannot access administrator APIs.
    def test_normal_user_cannot_list_users(self):
        self.create_normal_user()

        login = self.request(
            '/api/login',
            {
                'username': 'alice123',
                'password': 'Abcdefg1'
            }
        )

        normal_cookie = login['headers']['Set-Cookie'].split(';')[0]

        result = self.request(
            '/api/admin/users',
            cookie=normal_cookie
        )

        self.assertEqual(result['status'], 403)


if __name__ == '__main__':
    unittest.main()