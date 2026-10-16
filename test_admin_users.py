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


class AdminUserListTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db_path = os.path.join(self.temp.name, 'users.db')

        with patch.dict(os.environ, {
            'DATABASE_URL': '',
            'PDM_DB_PATH': db_path,
        }):
            import app

        import bottle
        self.app = app
        self.wsgi = bottle.app()
        self.db = database.Database('sqlite', db_path)
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
            result.update(status=int(status.split()[0]), headers=dict(headers))

        chunks = self.wsgi(environ, start_response)
        try:
            result['body'] = b''.join(chunks)
        finally:
            if hasattr(chunks, 'close'):
                chunks.close()
        return result

    def create_user(self, username='alice123'):
        with self.db.transaction(write=True) as store:
            return store.create_user(
                username,
                validate.hash_password('Abcdefg1'),
            )

    def login(self, path, username):
        result = self.request(
            path,
            {'username': username, 'password': 'Abcdefg1'},
        )
        return result['headers']['Set-Cookie'].split(';')[0]

    def test_admin_can_list_normal_users(self):
        user = self.create_user()
        with self.db.transaction(write=True) as store:
            store.lock_user(user['id'])
            store.insert_file(user['id'], 'one.txt', 'document', b'test')

        admin_cookie = self.login('/api/admin/login', 'admin001')
        result = self.request('/api/admin/users', cookie=admin_cookie)

        self.assertEqual(result['status'], 200)
        self.assertEqual(json.loads(result['body']), {
            'users': [{
                'id': user['id'],
                'username': 'alice123',
                'enabled': True,
                'file_quota': 100,
                'file_count': 1,
            }]
        })

    def test_empty_normal_user_list(self):
        admin_cookie = self.login('/api/admin/login', 'admin001')
        result = self.request('/api/admin/users', cookie=admin_cookie)

        self.assertEqual(result['status'], 200)
        self.assertEqual(json.loads(result['body']), {'users': []})

    def test_unauthenticated_user_cannot_list_users(self):
        result = self.request('/api/admin/users')
        self.assertEqual(result['status'], 401)

    def test_normal_user_cannot_list_users(self):
        self.create_user()
        user_cookie = self.login('/api/login', 'alice123')
        result = self.request('/api/admin/users', cookie=user_cookie)
        self.assertEqual(result['status'], 403)


if __name__ == '__main__':
    unittest.main()
