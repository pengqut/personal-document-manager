import io
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import admin_setup
import auth
import database
import validate


class UploadQuotaTests(unittest.TestCase):

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

    def request(self, path, data=None, cookie=None, method=None, body=None, content_type=None):
        if body is None:
            body = urlencode(data).encode() if data is not None else b''
            if content_type is None and data is not None:
                content_type = 'application/x-www-form-urlencoded'
        if method is None:
            method = 'POST' if body else 'GET'

        environ = {
            'REQUEST_METHOD': method,
            'PATH_INFO': path,
            'QUERY_STRING': '',
            'SERVER_NAME': 'localhost',
            'SERVER_PORT': '8080',
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'CONTENT_LENGTH': str(len(body)),
            'wsgi.url_scheme': 'http',
            'wsgi.input': io.BytesIO(body),
            'wsgi.errors': io.StringIO(),
            'wsgi.version': (1, 0),
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
        }
        if content_type:
            environ['CONTENT_TYPE'] = content_type
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

    def login(self, path, username):
        result = self.request(
            path,
            {'username': username, 'password': 'Abcdefg1'}
        )
        return result['headers']['Set-Cookie'].split(';')[0]

    def create_normal_user(self, file_quota=100):
        with self.db.transaction(write=True) as store:
            return store.create_user(
                'alice123',
                validate.hash_password('Abcdefg1'),
                file_quota=file_quota,
            )

    def upload(self, cookie, content=b'hello', filename='note.txt'):
        boundary = '----pdmtest'
        body = (
            ('--%s\r\n' % boundary).encode()
            + ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % filename).encode()
            + b'Content-Type: text/plain\r\n\r\n'
            + content
            + ('\r\n--%s--\r\n' % boundary).encode()
        )
        return self.request(
            '/api/upload',
            cookie=cookie,
            body=body,
            content_type='multipart/form-data; boundary=%s' % boundary,
        )

    def file_count(self, user_id):
        with self.db.transaction() as store:
            return store.count_user_files(user_id)

    # Upload succeeds while the user is under the personal quota.
    def test_upload_below_quota(self):
        user = self.create_normal_user(file_quota=2)
        cookie = self.login('/api/login', 'alice123')

        result = self.upload(cookie)
        self.assertEqual(result['status'], 200)
        self.assertIs(json.loads(result['body'])['ok'], True)
        self.assertEqual(self.file_count(user['id']), 1)

        limits = self.request('/api/limits', cookie=cookie)
        body = json.loads(limits['body'])
        self.assertEqual(body['user_file_limit'], 2)
        self.assertEqual(body['used'], 1)

    # The next upload is refused once the personal quota is full.
    def test_upload_at_quota(self):
        user = self.create_normal_user(file_quota=1)
        cookie = self.login('/api/login', 'alice123')

        first = self.upload(cookie, filename='one.txt')
        self.assertEqual(first['status'], 200)

        second = self.upload(cookie, filename='two.txt')
        self.assertEqual(second['status'], 403)
        self.assertIn('limit of 1 files', json.loads(second['body'])['error'])
        self.assertEqual(self.file_count(user['id']), 1)

    # A quota lowered below the current count blocks further uploads.
    def test_upload_above_quota(self):
        user = self.create_normal_user(file_quota=2)
        cookie = self.login('/api/login', 'alice123')
        self.upload(cookie, filename='one.txt')
        self.upload(cookie, filename='two.txt')

        admin_cookie = self.login('/api/admin/login', 'admin001')
        updated = self.request(
            '/api/admin/users/%d/quota' % user['id'],
            {'file_quota': '1'},
            admin_cookie
        )
        self.assertEqual(updated['status'], 200)

        blocked = self.upload(cookie, filename='three.txt')
        self.assertEqual(blocked['status'], 403)
        self.assertEqual(self.file_count(user['id']), 2)

    # A zero quota rejects every upload.
    def test_upload_zero_quota(self):
        user = self.create_normal_user(file_quota=0)
        cookie = self.login('/api/login', 'alice123')

        result = self.upload(cookie)
        self.assertEqual(result['status'], 403)
        self.assertEqual(self.file_count(user['id']), 0)

    # Deleting a file frees a slot under the same quota.
    def test_upload_after_file_deleted(self):
        user = self.create_normal_user(file_quota=1)
        cookie = self.login('/api/login', 'alice123')
        self.upload(cookie, filename='one.txt')

        with self.db.transaction() as store:
            file_id = store.list_user_files(user['id'])[0]['id']

        deleted = self.request(
            '/api/files/%d/delete' % file_id,
            cookie=cookie,
            body=b'',
            method='POST',
        )
        self.assertEqual(deleted['status'], 200)

        again = self.upload(cookie, filename='two.txt')
        self.assertEqual(again['status'], 200)
        self.assertEqual(self.file_count(user['id']), 1)

    # The size limit still rejects an oversized file before the quota check.
    def test_size_limit_still_applies(self):
        user = self.create_normal_user(file_quota=5)
        cookie = self.login('/api/login', 'alice123')

        with patch.dict(self.app.LIMITS, {'max_file_mb': 0}):
            result = self.upload(cookie, content=b'x')

        self.assertEqual(result['status'], 400)
        self.assertIn('MB limit', json.loads(result['body'])['error'])
        self.assertEqual(self.file_count(user['id']), 0)

    # Two uploads that race for one remaining slot produce one success.
    def test_concurrent_uploads_keep_the_quota(self):
        user = self.create_normal_user(file_quota=1)
        cookie = self.login('/api/login', 'alice123')
        barrier = threading.Barrier(2)
        statuses = []
        lock = threading.Lock()

        def upload_once(filename):
            barrier.wait()
            result = self.upload(cookie, filename=filename)
            with lock:
                statuses.append(result['status'])

        threads = [
            threading.Thread(target=upload_once, args=('one.txt',)),
            threading.Thread(target=upload_once, args=('two.txt',)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(statuses, [200, 403])
        self.assertEqual(self.file_count(user['id']), 1)


if __name__ == '__main__':
    unittest.main()
