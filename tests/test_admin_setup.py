import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import admin_setup
import database
import validate


class AdminSetupFacadeTests(unittest.TestCase):
    # Facade logic against a temporary SQLite database, including SQLite's
    # serialized writes. PostgreSQL row locking is tested separately.

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'setup.db')
        self.db = database.Database('sqlite', self.path)
        self.db.initialize_schema()
        self.service = admin_setup.AdminSetupFacade(self.db)

    def test_admin_username_allows_five_characters(self):
        with self.assertRaises(admin_setup.SetupValidationError):
            self.service.setup_admin('admi', 'Abcdefg1')
        self.assertEqual(self.service.setup_admin('admin', 'Abcdefg1'), {'ok': True})
        self.assertIsNotNone(validate.validate_username('admin'))
        self.assertIsNotNone(validate.validate_username('admin!', min_length=5))
        self.assertIsNotNone(validate.validate_username('administrator', min_length=5))

    def create_normal_user(self, username):
        with self.db.transaction(write=True) as store:
            return store.create_user(
                username, validate.hash_password('Abcdefg1')
            )

    # The first setup creates one fixed administrator and the repeat is
    # rejected without changing anything.
    def test_first_setup_succeeds_and_repeat_is_rejected(self):
        self.assertEqual(self.service.get_status(), {'initialized': False})

        result = self.service.setup_admin('admin001', 'Abcdefg1')
        self.assertEqual(result, {'ok': True})

        with self.db.transaction() as store:
            admin = store.get_user_by_username('admin001')
            self.assertEqual(admin['role'], 'admin')
            self.assertIs(admin['enabled'], True)
            self.assertEqual(admin['file_quota'], 100)
            stored_hash = admin['password_hash']
            self.assertNotEqual(stored_hash, 'Abcdefg1')
            self.assertTrue(validate.check_password('Abcdefg1', stored_hash))
            self.assertTrue(store.get_system_state()['admin_initialized'])
        self.assertEqual(self.service.get_status(), {'initialized': True})

        with self.assertRaises(admin_setup.SetupClosedError):
            self.service.setup_admin('admin002', 'Abcdefg1')
        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_by_username('admin002'))
            self.assertTrue(store.get_system_state()['admin_initialized'])

    # Two simultaneous setup attempts create exactly one administrator.
    def test_concurrent_setup_creates_only_one_admin(self):
        barrier = threading.Barrier(2)

        def setup_once(username):
            barrier.wait(timeout=5)
            try:
                self.service.setup_admin(username, 'Abcdefg1')
            except admin_setup.SetupClosedError:
                return 'rejected', username
            return 'created', username

        with ThreadPoolExecutor(max_workers=2) as workers:
            attempts = [
                workers.submit(setup_once, username)
                for username in ('admin001', 'admin002')
            ]
            results = [attempt.result(timeout=10) for attempt in attempts]

        self.assertCountEqual(
            [outcome for outcome, username in results],
            ['created', 'rejected'],
        )
        winner = next(username for outcome, username in results if outcome == 'created')
        with self.db.transaction() as store:
            admins = store._execute(
                database.users_table.select()
                .where(database.users_table.c.role == 'admin')
            ).mappings().all()
            self.assertEqual(len(admins), 1)
            self.assertEqual(admins[0]['username'], winner)
            self.assertTrue(validate.check_password('Abcdefg1', admins[0]['password_hash']))
            self.assertTrue(store.get_system_state()['admin_initialized'])
        self.assertEqual(self.service.get_status(), {'initialized': True})

    # Invalid input and a username that belongs to a normal user leave
    # no administrator and keep the flag false.
    def test_invalid_input_and_username_conflict_change_nothing(self):
        normal = self.create_normal_user('alice123')

        for subcase, username, password in (
            ('empty username', '   ', 'Abcdefg1'),
            ('bad password', 'admin001', 'weak'),
            ('username conflict', 'alice123', 'Abcdefg1'),
        ):
            with self.subTest(subcase):
                if subcase == 'username conflict':
                    expected = database.ConflictError
                else:
                    expected = admin_setup.SetupValidationError
                with self.assertRaises(expected):
                    self.service.setup_admin(username, password)

        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_by_username('admin001'))
            self.assertFalse(store.get_system_state()['admin_initialized'])
            unchanged = store.get_user_by_username('alice123')
            self.assertEqual(unchanged, normal)

    # Clearing the password reopens setup even if the old flag is true.
    # Setup reuses the administrator ID and removes stale sessions.
    def test_reset_password_reuses_admin_and_invalidates_session(self):
        self.service.setup_admin('admin001', 'Abcdefg1')
        normal = self.create_normal_user('alice123')
        with self.db.transaction(write=True) as store:
            admin = store.get_admin()
            store.create_session('old-admin-token', admin['id'])
            store._execute(
                database.users_table.update()
                .where(database.users_table.c.id == admin['id'])
                .values(password_hash='')
            )
        self.assertEqual(self.service.get_status(), {'initialized': False})
        self.service.setup_admin('admin001', 'NewPass12')
        with self.db.transaction() as store:
            restored = store.get_admin()
            self.assertEqual(restored['id'], admin['id'])
            self.assertTrue(validate.check_password('NewPass12', restored['password_hash']))
            self.assertIsNone(store.get_user_by_token('old-admin-token'))
            self.assertEqual(store.get_user_by_id(normal['id']), normal)
        self.assertEqual(self.service.get_status(), {'initialized': True})

    # A persistence failure rolls the whole setup back, including the
    # already created administrator, and setup can be retried after.
    def test_failed_persistence_rolls_back_admin_and_flag(self):
        with patch.object(
            database.Store,
            'mark_admin_initialized',
            side_effect=database.StorageError('commit failed'),
        ):
            with self.assertRaises(database.StorageError):
                self.service.setup_admin('admin001', 'Abcdefg1')

        with self.db.transaction() as store:
            self.assertIsNone(store.get_user_by_username('admin001'))
            self.assertFalse(store.get_system_state()['admin_initialized'])

        result = self.service.setup_admin('admin001', 'Abcdefg1')
        self.assertEqual(result, {'ok': True})
        with self.db.transaction() as store:
            self.assertEqual(
                store.get_user_by_username('admin001')['role'], 'admin'
            )
            self.assertTrue(store.get_system_state()['admin_initialized'])
