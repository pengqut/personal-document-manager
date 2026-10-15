# One-time administrator initialization. The facade coordinates validation,
# the write transaction and administrator credentials, so routes stay simple.

import database
import validate


class SetupValidationError(Exception):
    # Invalid username or password input.
    pass


class SetupClosedError(Exception):
    # The administrator account has already been initialized.
    pass


class AdminSetupFacade:
    # Coordinates the one-time admin setup. Holds an injected Database
    # object and composes Store and validate, without subclassing.

    def __init__(self, db):
        self._db = db

    # Read the initialization state without locking or changing it.
    def get_status(self):
        with self._db.transaction() as store:
            admin = store.get_admin()
        return {'initialized': bool(admin and admin['password_hash'])}

    # Coordinate validation and atomic persistence. The row lock makes
    # concurrent requests serial; credential changes commit together.
    def setup_admin(self, username, password):
        with self._db.transaction(write=True) as store:
            store.lock_system_state()
            admin = store.get_admin()
            if admin and admin['password_hash']:
                raise SetupClosedError('Administrator setup is already complete.')
            username = (username or '').strip()
            error = (
                validate.validate_username(username, min_length=5)
                or validate.validate_password(password or '')
            )
            if error:
                raise SetupValidationError(error)
            existing = store.get_user_by_username(username)
            if existing is not None and (admin is None or existing['id'] != admin['id']):
                raise database.ConflictError('Username already exists.')
            password_hash = validate.hash_password(password)
            if admin is None:
                store.create_admin(username, password_hash)
            else:
                store.restore_admin(admin['id'], username, password_hash)
            store.mark_admin_initialized()
        return {'ok': True}