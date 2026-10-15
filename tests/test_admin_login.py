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


class AdminLoginTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ, {
            'DATABASE_URL': '',
            'PDM_DB_PATH': os.path.join(self.temp.name, 'login.db'),
        }):
            import app
        import bottle
        self.app = app
        self.wsgi = bottle.app()
        self.db = database.Database('sqlite', os.path.join(self.temp.name, 'test.db'))
        self.db.initialize_schema()
        self.setup = admin_setup.AdminSetupFacade(self.db)
        self.setup.setup_admin('admin001', 'Abcdefg1')
        with self.db.transaction(write=True) as store:
            store.create_user('alice123', validate.hash_password('Abcdefg1'))
        patcher = patch.multiple(
            app, db=self.db, auth_service=auth.AuthService(self.db),
            admin_setup_service=self.setup,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, data=None, cookie=None):
        body = urlencode(data).encode() if data is not None else b''
        environ = {
            'REQUEST_METHOD': 'POST' if data is not None else 'GET',
            'PATH_INFO': path, 'QUERY_STRING': '',
            'SERVER_NAME': 'localhost', 'SERVER_PORT': '8080',
            'SERVER_PROTOCOL': 'HTTP/1.1', 'CONTENT_LENGTH': str(len(body)),
            'CONTENT_TYPE': 'application/x-www-form-urlencoded',
            'wsgi.url_scheme': 'http', 'wsgi.input': io.BytesIO(body),
            'wsgi.errors': io.StringIO(), 'wsgi.version': (1, 0),
            'wsgi.multithread': False, 'wsgi.multiprocess': False,
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

    def login(self, path, username):
        return self.request(path, {'username': username, 'password': 'Abcdefg1'})

    def test_login_entries_accept_only_their_role(self):
        for path, username in (
            ('/api/login', 'admin001'), ('/api/admin/login', 'alice123'),
        ):
            with self.subTest(path=path):
                result = self.login(path, username)
                self.assertEqual(result['status'], 401)
                self.assertNotIn('Set-Cookie', result['headers'])
        with self.db.transaction() as store:
            count = store._execute(database.sa.select(
                database.sa.func.count()).select_from(database.sessions_table)).scalar()
            self.assertEqual(count, 0)
        self.assertEqual(self.login('/api/login', 'alice123')['status'], 200)
        self.assertEqual(self.login('/api/admin/login', 'admin001')['status'], 200)

    def test_admin_page_and_api_require_admin_session(self):
        self.assertEqual(self.request('/admin-login.html')['status'], 200)
        self.assertEqual(self.request('/admin.html')['status'], 303)
        self.assertEqual(self.request('/api/admin/me')['status'], 401)
        normal_cookie = self.login('/api/login', 'alice123')['headers']['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('/admin.html', cookie=normal_cookie)['status'], 403)
        self.assertEqual(self.request('/api/admin/me', cookie=normal_cookie)['status'], 403)
        admin_login = self.login('/api/admin/login', 'admin001')
        self.assertIn('Path=/', admin_login['headers']['Set-Cookie'])
        admin_cookie = admin_login['headers']['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('/admin.html', cookie=admin_cookie)['status'], 200)
        result = self.request('/api/admin/me', cookie=admin_cookie)
        self.assertEqual(result['status'], 200)
        self.assertEqual(json.loads(result['body']), {'username': 'admin001', 'role': 'admin'})
        self.assertEqual(self.request('/api/logout', {}, admin_cookie)['status'], 200)
        self.assertEqual(self.request('/api/admin/me', cookie=admin_cookie)['status'], 401)


if __name__ == '__main__':
    unittest.main()
