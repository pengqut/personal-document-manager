import os
import sqlite3
import tempfile
import threading
import time
import unittest
import sqlalchemy as sa
from unittest.mock import patch
from urllib.parse import urlsplit

import database
import validate
from auth import AuthenticationError, AuthService


class SQLiteComponentTests(unittest.TestCase):
    # Real SQL against a temporary SQLite file. Not a PostgreSQL proof.

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'component.db')
        self.db = database.Database('sqlite', self.path)
        self.db.initialize_schema()

    def table_names(self, path):
        conn = sqlite3.connect(path)
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        conn.close()
        return names


class SchemaTests(SQLiteComponentTests):

    # A fresh database gets all four tables.
    def test_fresh_schema_creates_four_tables(self):
        names = self.table_names(self.path)
        for table in ('users', 'sessions', 'files', 'system_state'):
            self.assertIn(table, names)


class UserStoreTests(SQLiteComponentTests):

    def create_user(self, username='alice123'):
        with self.db.transaction(write=True) as store:
            return store.create_user(
                username, validate.hash_password('Abcdefg1')
            )

    # Registration creates ordinary enabled users with the default quota.
    def test_create_user_is_ordinary_and_enabled(self):
        user = self.create_user()
        self.assertEqual(user['role'], 'user')
        self.assertIs(user['enabled'], True)
        self.assertEqual(user['file_quota'], 100)

    def test_duplicate_username_raises_conflict(self):
        self.create_user()
        with self.db.transaction(write=True) as store:
            with self.assertRaises(database.ConflictError):
                store.create_user('alice123', 'h$1')

    # Administrators can never be targeted by status or quota changes.
    def test_admin_target_is_rejected_and_unchanged(self):
        with self.db.transaction(write=True) as store:
            store.lock_system_state()
            admin = store.create_admin('admin001', 'h$1')
            store.mark_admin_initialized()
        with self.db.transaction(write=True) as store:
            store.lock_user(admin['id'])
            with self.assertRaises(database.InvalidTargetError):
                store.set_user_enabled(admin['id'], False)
        with self.db.transaction() as store:
            self.assertIs(store.get_user_by_id(admin['id'])['enabled'], True)


class SessionStoreTests(SQLiteComponentTests):

    def create_user(self, username='alice123'):
        with self.db.transaction(write=True) as store:
            return store.create_user(
                username, validate.hash_password('Abcdefg1')
            )

    def test_session_lifecycle(self):
        user = self.create_user()
        with self.db.transaction(write=True) as store:
            store.create_session('tok1', user['id'])
        with self.db.transaction() as store:
            self.assertEqual(store.get_user_by_token('tok1')['id'], user['id'])
        with self.db.transaction(write=True) as store:
            store.delete_session('tok1')
        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_by_token('tok1'))

    # Disabling deletes the user's sessions, so the old token no longer
    # resolves to a user.
    def test_disabled_user_token_resolves_to_nothing(self):
        user = self.create_user()
        with self.db.transaction(write=True) as store:
            store.create_session('tok2', user['id'])
            store.lock_user(user['id'])
            store.set_user_enabled(user['id'], False)
        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_by_token('tok2'))

    # Re-enabling the account does not bring the deleted sessions back.
    def test_reenable_does_not_restore_old_sessions(self):
        user = self.create_user()
        with self.db.transaction(write=True) as store:
            store.create_session('tok3', user['id'])
            store.lock_user(user['id'])
            store.set_user_enabled(user['id'], False)
        with self.db.transaction(write=True) as store:
            store.lock_user(user['id'])
            store.set_user_enabled(user['id'], True)
        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_by_token('tok3'))

    # Disabling one user must not affect other users' sessions.
    def test_disable_one_user_keeps_other_sessions(self):
        alice = self.create_user('alice123')
        bob = self.create_user('bobuser1')
        with self.db.transaction(write=True) as store:
            store.create_session('toka', alice['id'])
            store.create_session('tokb', bob['id'])
            store.lock_user(alice['id'])
            store.set_user_enabled(alice['id'], False)
        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_by_token('toka'))
            self.assertIsNotNone(store.get_user_by_token('tokb'))


class FileStoreTests(SQLiteComponentTests):

    def create_user(self, username):
        with self.db.transaction(write=True) as store:
            return store.create_user(username, validate.hash_password('Abcdefg1'))

    def insert(self, user_id, filename, category='document', content=b'file-bytes'):
        with self.db.transaction(write=True) as store:
            store.lock_user(user_id)
            return store.insert_file(user_id, filename, category, content)

    # Metadata of one user is invisible to another user, who also cannot
    # change or delete it.
    def test_users_cannot_see_or_touch_other_files(self):
        alice = self.create_user('alice123')
        bob = self.create_user('bobuser1')
        self.insert(alice['id'], 'a.pdf')

        with self.db.transaction() as store:
            self.assertEqual(store.count_user_files(alice['id']), 1)
            self.assertEqual(store.count_user_files(bob['id']), 0)
            alice_files = store.list_user_files(alice['id'])
        self.assertEqual(len(alice_files), 1)

        with self.db.transaction(write=True) as store:
            store.lock_user(bob['id'])
            with self.assertRaises(database.NotFoundError):
                store.change_file_category(
                    alice_files[0]['id'], bob['id'], 'image'
                )
            with self.assertRaises(database.NotFoundError):
                store.delete_file(alice_files[0]['id'], bob['id'])

    # Bytes survive a round trip unchanged, including zero-byte files,
    # and the list view never loads the content column.
    def test_content_roundtrip_and_zero_byte_file(self):
        user = self.create_user('alice123')
        payload = bytes(range(256))
        meta = self.insert(user['id'], 'a.bin', 'other', payload)
        self.assertEqual(meta['size'], len(payload))

        with self.db.transaction() as store:
            stored = store.get_user_file_content(meta['id'], user['id'])
            listing = store.list_user_files(user['id'])
        self.assertEqual(stored['content'], payload)
        self.assertEqual(stored['size'], len(payload))
        self.assertEqual(len(listing), 1)
        self.assertNotIn('content', listing[0])
        self.assertNotIn('stored_name', listing[0])

        # Zero-byte files are valid uploads.
        empty = self.insert(user['id'], 'empty.txt', 'document', b'')
        self.assertEqual(empty['size'], 0)
        with self.db.transaction() as store:
            stored_empty = store.get_user_file_content(empty['id'], user['id'])
        self.assertEqual(stored_empty['content'], b'')

    # Another user can neither read the bytes nor delete the file; the
    # owner's delete removes metadata and content in the same row.
    def test_ownership_and_delete(self):
        alice = self.create_user('alice123')
        bob = self.create_user('bobuser1')
        meta = self.insert(alice['id'], 'a.pdf', 'document', b'abc')

        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_file_content(meta['id'], bob['id']))
        with self.db.transaction(write=True) as store:
            store.lock_user(bob['id'])
            with self.assertRaises(database.NotFoundError):
                store.delete_file(meta['id'], bob['id'])
        with self.db.transaction() as store:
            self.assertEqual(store.count_user_files(alice['id']), 1)

        with self.db.transaction(write=True) as store:
            store.lock_user(alice['id'])
            store.delete_file(meta['id'], alice['id'])
        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_file(meta['id'], alice['id']))
            self.assertIsNone(store.get_user_file_content(meta['id'], alice['id']))
            self.assertEqual(store.count_user_files(alice['id']), 0)

    # A failed transaction rolls the inserted file row back completely.
    def test_insert_file_rolls_back_with_transaction(self):
        alice = self.create_user('alice123')
        with self.assertRaises(ValueError):
            with self.db.transaction(write=True) as store:
                store.lock_user(alice['id'])
                store.insert_file(alice['id'], 'a.pdf', 'document', b'abc')
                raise ValueError('boom')
        with self.db.transaction() as store:
            self.assertEqual(store.count_user_files(alice['id']), 0)


class AtomicityTests(SQLiteComponentTests):

    # Two writes in one failing transaction are rolled back together.
    def test_two_writes_roll_back_together(self):
        with self.db.transaction(write=True) as store:
            alice = store.create_user('alice123', 'h$1')
            store.create_session('tokx', alice['id'])
        with self.assertRaises(ValueError):
            with self.db.transaction(write=True) as store:
                bob = store.create_user('bobuser1', 'h$2')
                store.create_session('toky', bob['id'])
                raise ValueError('boom')
        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_by_username('bobuser1'))
            self.assertIsNone(store.get_user_by_token('toky'))
            self.assertIsNotNone(store.get_user_by_username('alice123'))


def resolve_test_database():
    # Return the database under test: a fresh temporary SQLite file by
    # default, or the disposable server database from PDM_TEST_DATABASE_URL
    # (whose name must end with _test) when it is configured.
    url = os.environ.get('PDM_TEST_DATABASE_URL')
    if not url:
        temp = tempfile.TemporaryDirectory()
        return (
            database.Database('sqlite', os.path.join(temp.name, 'test.db')),
            temp,
        )
    name = urlsplit(url).path.lstrip('/')
    if not name.endswith('_test'):
        raise RuntimeError(
            'PDM_TEST_DATABASE_URL must point to a dedicated test database '
            'whose name ends with _test.'
        )
    return database.Database.from_url(url), None


class StoreEngineTests(unittest.TestCase):
    # The same Store behaviour verified against the configured test
    # database: a temporary SQLite file by default, or the disposable
    # server database from PDM_TEST_DATABASE_URL. These tests keep working
    # unchanged when the project switches databases.

    def setUp(self):
        self.db, temp = resolve_test_database()
        if temp is not None:
            self.addCleanup(temp.cleanup)
        self.db.initialize_schema()
        self._reset_data()

    def _reset_data(self):
        with self.db._get_engine(write=True).begin() as conn:
            for table in ('sessions', 'files', 'users', 'system_state'):
                conn.execute(sa.text(f'DELETE FROM {table}'))
            conn.execute(sa.text(
                'INSERT INTO system_state (id, admin_initialized) '
                'VALUES (1, FALSE)'
            ))

    def create_user(self, username='alice123', **overrides):
        with self.db.transaction(write=True) as store:
            user = store.create_user(username, validate.hash_password('Abcdefg1'))
            if overrides:
                store.lock_user(user['id'])
                if 'enabled' in overrides:
                    store.set_user_enabled(user['id'], overrides['enabled'])
        return user

    # Identity inserts return real ids, and the row types normalize to the
    # same shapes on every backend: real bools and UTC timestamp strings.
    def test_identity_and_type_normalization(self):
        alice = self.create_user('alice123')
        bob = self.create_user('bobuser1')
        self.assertIsInstance(alice['id'], int)
        self.assertGreater(bob['id'], alice['id'])
        self.assertIs(alice['enabled'], True)
        self.assertIsInstance(alice['enabled'], bool)
        with self.db.transaction(write=True) as store:
            store.lock_user(alice['id'])
            meta = store.insert_file(alice['id'], 'a.pdf', 'document', b'pdf-bytes')
        self.assertIsInstance(meta['uploaded_at'], str)
        self.assertEqual(len(meta['uploaded_at']), 19)
        self.assertEqual(meta['uploaded_at'][4], '-')

    # Conflicts, integrity errors and rollback recovery.
    def test_unique_and_fk_errors_and_rollback_recovery(self):
        self.create_user('alice123')
        with self.db.transaction(write=True) as store:
            with self.assertRaises(database.ConflictError):
                store.create_user('alice123', 'h$1')
        with self.db.transaction(write=True) as store:
            with self.assertRaises(database.StorageError) as ctx:
                store.create_session('tokfk', 999)
            self.assertNotIsInstance(ctx.exception, database.ConflictError)
        # A failed transaction must not poison later work: new transactions
        # run normally afterwards.
        with self.db.transaction(write=True) as store:
            user = store.create_user('bobuser1', 'h$2')
            store.create_session('tokok', user['id'])
        with self.db.transaction() as store:
            self.assertIsNotNone(store.get_user_by_token('tokok'))

    # A user row locked by connection A must block connection B until A
    # commits. Requires a backend with row-level locking; SQLite
    # serializes writes with a whole-database lock instead.
    def test_user_row_lock_between_connections(self):
        if not self.db.supports_row_locks:
            self.skipTest('the test database does not support row-level locks')
        alice = self.create_user('alice123')
        release = threading.Event()
        result = {}

        def holder():
            with self.db.transaction(write=True) as store:
                store.lock_user(alice['id'])
                store.set_user_quota(alice['id'], 42)
                result['locked'] = True
                release.wait(5)

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        deadline = time.time() + 5
        while not result.get('locked') and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(result.get('locked'), 'holder never acquired the lock')

        def contender():
            start = time.time()
            with self.db.transaction(write=True) as store:
                locked = store.lock_user(alice['id'])
                result['observed_quota'] = locked['file_quota']
                store.set_user_quota(alice['id'], 7)
            result['seconds'] = time.time() - start
            result['done'] = True

        contender_thread = threading.Thread(target=contender)
        contender_thread.start()
        time.sleep(0.5)
        self.assertFalse(
            result.get('done'), 'contender finished while the row was locked'
        )
        release.set()
        holder_thread.join(5)
        contender_thread.join(5)
        self.assertTrue(result.get('done'), 'contender never finished')
        self.assertGreaterEqual(result['seconds'], 0.3)
        self.assertEqual(result['observed_quota'], 42)
        with self.db.transaction() as store:
            self.assertEqual(store.get_user_by_id(alice['id'])['file_quota'], 7)

    # The system_state row lock behaves the same way.
    def test_system_state_row_lock_between_connections(self):
        if not self.db.supports_row_locks:
            self.skipTest('the test database does not support row-level locks')
        release = threading.Event()
        result = {}

        def holder():
            with self.db.transaction(write=True) as store:
                store.lock_system_state()
                result['locked'] = True
                release.wait(5)

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        deadline = time.time() + 5
        while not result.get('locked') and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(result.get('locked'))

        def contender():
            with self.db.transaction(write=True) as store:
                store.lock_system_state()
                result['done'] = True

        contender_thread = threading.Thread(target=contender)
        contender_thread.start()
        time.sleep(0.5)
        self.assertFalse(result.get('done'))
        release.set()
        holder_thread.join(5)
        contender_thread.join(5)
        self.assertTrue(result.get('done'))

    # Disable and login use the latest locked state: a user disabled
    # before a login attempt cannot log in, and disabling deletes the
    # sessions of already logged in users.
    def test_disabled_user_cannot_login_and_loses_sessions(self):
        self.create_user('alice123', enabled=False)
        service = AuthService(self.db)
        with self.assertRaises(AuthenticationError):
            service.authenticate('alice123', 'Abcdefg1')

        bob = self.create_user('bobuser1')
        token = service.authenticate('bobuser1', 'Abcdefg1')['token']
        with self.db.transaction(write=True) as store:
            store.lock_user(bob['id'])
            store.set_user_enabled(bob['id'], False)
        # Disabling deleted the session, so the old token resolves to
        # nothing.
        self.assertIsNone(service.current_user(token))
        with self.assertRaises(AuthenticationError):
            service.authenticate('bobuser1', 'Abcdefg1')

    # On backends without row locks (SQLite), a write transaction takes
    # the database write lock up front: a second writer fails with a
    # StorageError instead of racing with the first transaction.
    def test_concurrent_writers_cannot_race_on_locked_backends(self):
        if self.db.supports_row_locks:
            self.skipTest('row-level locking is verified by the row lock tests')
        alice = self.create_user('alice123')
        release = threading.Event()
        result = {}

        def holder():
            with self.db.transaction(write=True) as store:
                store.lock_user(alice['id'])
                store.set_user_quota(alice['id'], 42)
                result['locked'] = True
                release.wait(5)

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        deadline = time.time() + 5
        while not result.get('locked') and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(result.get('locked'), 'holder never acquired the lock')

        with patch.object(database, 'SQLITE_TIMEOUT_SECONDS', 0.2):
            start = time.time()
            status = {}

            def contender():
                try:
                    with self.db.transaction(write=True) as store:
                        store.lock_user(alice['id'])
                        store.set_user_quota(alice['id'], 7)
                    status['outcome'] = 'unexpectedly succeeded'
                except database.StorageError:
                    status['outcome'] = 'storage error as expected'

            contender_thread = threading.Thread(target=contender)
            contender_thread.start()
            contender_thread.join(10)
            self.assertEqual(
                status['outcome'], 'storage error as expected',
                'a second writer must fail while the write lock is held'
            )
            self.assertLess(time.time() - start, 3)
        release.set()
        holder_thread.join(5)

        # A's committed state is intact and B left no partial write.
        with self.db.transaction() as store:
            self.assertEqual(store.get_user_by_id(alice['id'])['file_quota'], 42)