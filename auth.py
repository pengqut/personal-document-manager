import secrets

import database
import validate


class AuthenticationError(Exception):
    # Login failed because of credentials or a disabled account. The public
    # message never reveals which one it was.
    pass


class NotAuthenticatedError(Exception):
    # No user is available for the admin check.
    pass


class PermissionDeniedError(Exception):
    # The current user is not an administrator.
    pass


# Pure admin role check. Disabling deletes the user's sessions, so a
# resolvable user always comes from an active account.
def require_admin(user):
    if user is None:
        raise NotAuthenticatedError('Not logged in.')
    if user.get('role') != 'admin':
        raise PermissionDeniedError('Administrator rights are required.')
    return user


class AuthService:
    # Session lookup, login and logout on top of the shared database layer.

    def __init__(self, db):
        self._db = db

    # Resolve a session token to a user. No token means no database work.
    # Disabling deletes the sessions, so disabled users have no tokens.
    def current_user(self, token):
        if not token:
            return None
        with self._db.transaction() as store:
            return store.get_user_by_token(token)

    # Verify credentials and create a session in one write transaction.
    # Every failure raises AuthenticationError with the same public message.
    def authenticate(self, username, password, expected_role=None):
        username = (username or '').strip()
        if not username or not password:
            raise AuthenticationError('Username or password is incorrect.')
        with self._db.transaction(write=True) as store:
            user = store.get_user_by_username(username)
            if user is None:
                raise AuthenticationError('Username or password is incorrect.')
            locked = store.lock_user(user['id'])
            if locked is None:
                # The account vanished between lookup and lock.
                raise AuthenticationError('Username or password is incorrect.')
            if not locked['password_hash']:
                raise AuthenticationError('Username or password is incorrect.')
            try:
                password_ok = validate.check_password(
                    password, locked['password_hash']
                )
            except (ValueError, TypeError) as exc:
                raise database.StorageError(
                    'Stored password hash format is invalid.'
                ) from exc
            if (not password_ok or not locked['enabled']
                    or (expected_role is not None and locked['role'] != expected_role)):
                raise AuthenticationError('Username or password is incorrect.')
            token = secrets.token_hex(16)
            store.create_session(token, locked['id'])
            # The transaction commits before this value reaches the caller.
            return {'token': token, 'user': locked}

    # Remove one session. Missing tokens succeed without database work and
    # unknown tokens are a successful no-op.
    def logout(self, token):
        if not token:
            return
        with self._db.transaction(write=True) as store:
            store.delete_session(token)