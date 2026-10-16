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


class AdminQuotaTests(unittest.TestCase):

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

    def add_file(self, user_id, name='note.txt'):
        with self.db.transaction(write=True) as store:
            store.insert_file(user_id, name, 'document', b'note')

    # Admin can set a non-negative quota, including zero.
    def test_admin_can_set_quota(self):
        user = self.create_normal_user()
        admin_cookie = self.login_admin()

        for value in ('5', '0'):
            result = self.request(
                '/api/admin/users/%d/quota' % user['id'],
                {'file_quota': value},
                admin_cookie
            )
            self.assertEqual(result['status'], 200)
            body = json.loads(result['body'])
            self.assertIs(body['ok'], True)
            self.assertEqual(body['file_quota'], int(value))
            self.assertEqual(body['file_count'], 0)

        with self.db.transaction() as store:
            self.assertEqual(store.get_user_by_id(user['id'])['file_quota'], 0)

    # A quota below the current file count is stored and files stay.
    def test_quota_below_current_usage_keeps_files(self):
        user = self.create_normal_user()
        self.add_file(user['id'], 'one.txt')
        self.add_file(user['id'], 'two.txt')
        admin_cookie = self.login_admin()

        result = self.request(
            '/api/admin/users/%d/quota' % user['id'],
            {'file_quota': '1'},
            admin_cookie
        )

        self.assertEqual(result['status'], 200)
        body = json.loads(result['body'])
        self.assertEqual(body['file_quota'], 1)
        self.assertEqual(body['file_count'], 2)

        with self.db.transaction() as store:
            self.assertEqual(store.count_user_files(user['id']), 2)

    # Invalid quota values are rejected and the stored quota is unchanged.
    def test_invalid_quota_is_rejected(self):
        user = self.create_normal_user()
        admin_cookie = self.login_admin()

        for value in ('-1', '1.5', 'abc', ''):
            result = self.request(
                '/api/admin/users/%d/quota' % user['id'],
                {'file_quota': value},
                admin_cookie
            )
            self.assertEqual(result['status'], 400, value)

        with self.db.transaction() as store:
            self.assertEqual(store.get_user_by_id(user['id'])['file_quota'], 100)

    # Administrator accounts cannot have a quota set.
    def test_admin_target_is_rejected(self):
        admin_cookie = self.login_admin()
        with self.db.transaction() as store:
            admin = store.get_user_by_username('admin001')

        result = self.request(
            '/api/admin/users/%d/quota' % admin['id'],
            {'file_quota': '3'},
            admin_cookie
        )

        self.assertEqual(result['status'], 400)
        with self.db.transaction() as store:
            self.assertEqual(store.get_user_by_id(admin['id'])['file_quota'], 100)

    def test_missing_user_is_rejected(self):
        admin_cookie = self.login_admin()
        result = self.request(
            '/api/admin/users/99999/quota',
            {'file_quota': '3'},
            admin_cookie
        )
        self.assertEqual(result['status'], 404)

    def test_unauthenticated_user_cannot_set_quota(self):
        user = self.create_normal_user()
        result = self.request(
            '/api/admin/users/%d/quota' % user['id'],
            {'file_quota': '3'}
        )
        self.assertEqual(result['status'], 401)

    def test_normal_user_cannot_set_quota(self):
        user = self.create_normal_user()
        login = self.request(
            '/api/login',
            {
                'username': 'alice123',
                'password': 'Abcdefg1'
            }
        )
        cookie = login['headers']['Set-Cookie'].split(';')[0]
        result = self.request(
            '/api/admin/users/%d/quota' % user['id'],
            {'file_quota': '3'},
            cookie
        )
        self.assertEqual(result['status'], 403)


if __name__ == '__main__':
    unittest.main()
