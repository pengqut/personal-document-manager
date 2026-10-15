import unittest
from unittest.mock import MagicMock, patch

import auth
import database


USER = {'id': 1, 'username': 'alice123', 'password_hash': 'salt$digest',
        'role': 'user', 'enabled': True, 'file_quota': 100}
ADMIN = {'id': 2, 'username': 'admin001', 'password_hash': 'salt$digest',
         'role': 'admin', 'enabled': True, 'file_quota': 100}


def make_service():
    db = MagicMock()
    store = db.transaction.return_value.__enter__.return_value
    return auth.AuthService(db), db, store


class LoginTests(unittest.TestCase):

    def setUp(self):
        self.service, self.db, self.store = make_service()
        self.store.get_user_by_username.return_value = USER
        self.store.lock_user.return_value = USER

    @patch('auth.secrets.token_hex', return_value='fixed-token')
    @patch('auth.validate.check_password', return_value=True)
    def test_login_creates_session(self, _check, _token):
        result = self.service.authenticate('alice123', 'Abcdefg1')
        self.assertEqual(result['token'], 'fixed-token')
        self.assertEqual(result['user'], USER)
        self.store.create_session.assert_called_once_with('fixed-token', 1)

    @patch('auth.validate.check_password', return_value=False)
    def test_wrong_password_cannot_login(self, _check):
        with self.assertRaises(auth.AuthenticationError):
            self.service.authenticate('alice123', 'WrongPass1')
        self.store.create_session.assert_not_called()

    @patch('auth.validate.check_password', return_value=True)
    def test_disabled_user_cannot_login(self, _check):
        self.store.lock_user.return_value = dict(USER, enabled=False)
        with self.assertRaises(auth.AuthenticationError):
            self.service.authenticate('alice123', 'Abcdefg1')
        self.store.create_session.assert_not_called()


class SessionTests(unittest.TestCase):

    def test_logout_deletes_session(self):
        service, db, store = make_service()
        service.logout('tok1')
        store.delete_session.assert_called_once_with('tok1')
        db.transaction.assert_called_once_with(write=True)


class RequireAdminTests(unittest.TestCase):

    # Admin decisions use the database role: ordinary users are rejected
    # and administrators pass.
    def test_require_admin_checks_role(self):
        with self.assertRaises(auth.PermissionDeniedError):
            auth.require_admin(USER)
        self.assertIs(auth.require_admin(ADMIN), ADMIN)


if __name__ == '__main__':
    unittest.main()