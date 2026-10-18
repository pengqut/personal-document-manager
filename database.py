import os
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.pool import NullPool

import classify

POSTGRES_CONNECT_TIMEOUT = 5
SQLITE_TIMEOUT_SECONDS = 5
ROW_LOCK_BACKENDS = ('postgresql',)


class StorageError(Exception):
    # Driver connection, query, commit or schema failures.
    pass


class ConfigurationError(StorageError):
    # Invalid backend configuration, such as a malformed DATABASE_URL.
    pass


class ConflictError(StorageError):
    # A confirmed unique constraint conflict (username or token).
    pass


class NotFoundError(Exception):
    # Target does not exist or does not belong to the requesting user.
    pass


class InvalidTargetError(Exception):
    # Status or quota target is an administrator account.
    pass


# Validate a file quota: strict non-negative int, booleans are not allowed
def _validate_quota(file_quota):
    if isinstance(file_quota, bool) or not isinstance(file_quota, int) or file_quota < 0:
        raise ValueError('file_quota must be a non-negative integer.')
    return file_quota


# Normalize a time value to a UTC 'YYYY-MM-DD HH:MM:SS' string
def _normalize_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return value


# Build the internal User dict shared by both backends
def _user_dict(row):
    if row is None:
        return None
    return {
        'id': int(row['id']),
        'username': row['username'],
        'password_hash': row['password_hash'],
        'role': row['role'],
        'enabled': bool(row['enabled']),
        'file_quota': int(row['file_quota']),
    }


# Build the internal file metadata dict shared by both backends
def _file_dict(row):
    if row is None:
        return None
    return {
        'id': int(row['id']),
        'user_id': int(row['user_id']),
        'filename': row['filename'],
        'category': row['category'],
        'size': int(row['size']),
        'uploaded_at': _normalize_time(row['uploaded_at']),
    }


# One schema definition. SQLAlchemy compiles it to the dialect of the
# configured engine (SQLite locally, PostgreSQL in deployment).
metadata = sa.MetaData()

users_table = sa.Table(
    'users', metadata,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('username', sa.Text, nullable=False, unique=True),
    sa.Column('password_hash', sa.Text, nullable=False),
    sa.Column('role', sa.Text, nullable=False, server_default='user'),
    sa.Column('enabled', sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column('file_quota', sa.Integer, nullable=False, server_default='100'),
    sa.CheckConstraint("role IN ('user', 'admin')", name='users_role_check'),
    sa.CheckConstraint('file_quota >= 0', name='users_file_quota_check'),
)

sessions_table = sa.Table(
    'sessions', metadata,
    sa.Column('token', sa.Text, primary_key=True),
    sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
              server_default=sa.text('CURRENT_TIMESTAMP')),
)

files_table = sa.Table(
    'files', metadata,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
    sa.Column('filename', sa.Text, nullable=False),
    sa.Column('category', sa.Text, nullable=False),
    sa.Column('size', sa.BigInteger, nullable=False),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False,
              server_default=sa.text('CURRENT_TIMESTAMP')),
    sa.Column('content', sa.LargeBinary, nullable=False),
    sa.CheckConstraint(
        "category IN ('document', 'image', 'audio', 'video', 'other')",
        name='files_category_check'),
    sa.CheckConstraint('size >= 0', name='files_size_check'),
)

system_state_table = sa.Table(
    'system_state', metadata,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('admin_initialized', sa.Boolean, nullable=False,
              server_default=sa.false()),
)

sa.Index('idx_sessions_user_id', sessions_table.c.user_id)
sa.Index('idx_files_user_uploaded',
         files_table.c.user_id, files_table.c.uploaded_at)

USER_SELECT_COLUMNS = (
    users_table.c.id, users_table.c.username, users_table.c.password_hash,
    users_table.c.role, users_table.c.enabled, users_table.c.file_quota,
)
FILE_SELECT_COLUMNS = (
    files_table.c.id, files_table.c.user_id, files_table.c.filename,
    files_table.c.category, files_table.c.size,
    files_table.c.uploaded_at,
)


class Database:
    # Holds backend configuration and one engine. Connections are opened
    # per transaction; SQLAlchemy compiles the shared statements for the
    # configured dialect.

    def __init__(self, backend, location):
        if backend not in ('sqlite', 'postgresql'):
            raise ConfigurationError("backend must be 'sqlite' or 'postgresql'.")
        if backend == 'sqlite':
            if not isinstance(location, str) or not location:
                raise ConfigurationError('SQLite backend needs a file path.')
        else:
            if not isinstance(location, dict) or 'dbname' not in location:
                raise ConfigurationError('PostgreSQL backend needs connection settings.')
        self.backend = backend
        self.location = location
        self._engines = {}

    # Only server databases with MVCC provide row-level locking. Tests use
    # this capability flag; SQLite relies on its whole-database write lock.
    @property
    def supports_row_locks(self):
        return self.backend in ROW_LOCK_BACKENDS

    # Build a Database from environment variables, using base_dir for
    # relative SQLite paths so the shell working directory does not matter.
    @classmethod
    def from_environment(cls, base_dir):
        base_dir = os.path.abspath(base_dir)
        url = os.environ.get('DATABASE_URL')
        if url:
            return cls.from_url(url)
        db_path = os.environ.get('PDM_DB_PATH') or 'pdm.db'
        if not os.path.isabs(db_path):
            db_path = os.path.join(base_dir, db_path)
        return cls('sqlite', db_path)

    # Parse a postgresql:// or postgres:// URL into connection settings.
    # Any other scheme is a configuration error and never falls back to
    # SQLite.
    @classmethod
    def from_url(cls, url):
        parts = urlsplit(url)
        if parts.scheme not in ('postgresql', 'postgres'):
            raise ConfigurationError(
                'DATABASE_URL must use postgresql:// or postgres://.'
            )
        if not parts.hostname or not parts.path.lstrip('/'):
            raise ConfigurationError(
                'DATABASE_URL must include a host and a database name.'
            )
        conninfo = {
            'host': parts.hostname,
            'dbname': parts.path.lstrip('/'),
        }
        if parts.port:
            conninfo['port'] = parts.port
        if parts.username:
            conninfo['user'] = unquote(parts.username)
        if parts.password:
            conninfo['password'] = unquote(parts.password)
        return cls('postgresql', conninfo)

    # Build the SQLAlchemy engine URL for the configured backend.
    def _engine_url(self):
        if self.backend == 'sqlite':
            return 'sqlite:///' + os.path.abspath(self.location)
        info = self.location
        return sa.URL.create(
            'postgresql+psycopg',
            username=info.get('user'),
            password=info.get('password'),
            host=info.get('host'),
            port=info.get('port'),
            database=info.get('dbname'),
        )

    # Create the engine for the requested access mode once per Database.
    # SQLite uses separate read/write engines so a write transaction can
    # issue BEGIN IMMEDIATE; NullPool avoids keeping pooled connections
    # (and open database files) alive between transactions.
    def _get_engine(self, write=False):
        key = 'write' if (self.backend == 'sqlite' and write) else 'read'
        if key not in self._engines:
            self._engines[key] = self._build_engine(key)
        return self._engines[key]

    def _build_engine(self, key):
        if self.backend == 'sqlite':
            # SQLAlchemy 2.0 runs pysqlite in driver autocommit mode and
            # lets the application control transactions explicitly, so the
            # transaction flavor is decided by the begin event below.
            engine = sa.create_engine(
                self._engine_url(),
                connect_args={'isolation_level': None},
                poolclass=NullPool,
            )

            @event.listens_for(engine, 'connect')
            def configure_sqlite(dbapi_connection, connection_record):
                db_pointer = dbapi_connection.cursor()
                db_pointer.execute('PRAGMA foreign_keys = ON')
                db_pointer.execute(
                    f'PRAGMA busy_timeout = {SQLITE_TIMEOUT_SECONDS * 1000}'
                )
                db_pointer.close()

            @event.listens_for(engine, 'begin')
            def begin_transaction(conn):
                # A write transaction takes the database write lock up
                # front; a read transaction starts deferred.
                conn.exec_driver_sql(
                    'BEGIN IMMEDIATE' if key == 'write' else 'BEGIN'
                )

            return engine

        return sa.create_engine(
            self._engine_url(),
            connect_args={'connect_timeout': POSTGRES_CONNECT_TIMEOUT},
            poolclass=NullPool,
        )

    # Run one short transaction. Commit on success, rollback on failure,
    # always close the connection. Read-only work also uses a transaction.
    # The transaction flavor comes from the engine's begin event: the
    # SQLite write engine emits BEGIN IMMEDIATE so the write lock is taken
    # up front, the read engine and PostgreSQL emit a plain BEGIN.
    @contextmanager
    def transaction(self, write=False):
        try:
            conn = self._get_engine(write).connect()
        except Exception as exc:
            raise StorageError('Could not connect to the database.') from exc

        try:
            try:
                transaction = conn.begin()
            except Exception as exc:
                raise StorageError(
                    'Could not begin the database transaction.'
                ) from exc
            try:
                yield Store(conn, self.backend, write)
            except BaseException as exc:
                # The body failed: roll back and let the original error win.
                try:
                    transaction.rollback()
                except Exception as cleanup_error:
                    exc.add_note(f'Database rollback also failed: {cleanup_error!r}')
                raise
            try:
                transaction.commit()
            except Exception as exc:
                raise StorageError(
                    'Could not commit the database transaction.'
                ) from exc
        finally:
            conn.close()

    # Create a missing PostgreSQL database, then its schema. An existing database must
    # already use the current layout; old A1 files are rejected. Running
    # it twice is safe. Every failure surfaces as a StorageError so a
    # failed startup reports one clean message.
    def initialize_schema(self):
        try:
            if self.backend == 'postgresql':
                self._ensure_postgresql_database()
            self._create_schema()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                'Could not prepare the database schema.'
            ) from exc

    # Try the configured database first. If it cannot be opened, check
    # postgres before deciding whether the database actually needs creation.
    # Connection errors do not always include a PostgreSQL SQLSTATE.
    def _ensure_postgresql_database(self):
        try:
            with self._get_engine().connect():
                return
        except sa.exc.OperationalError as exc:
            connection_error = exc

        maintenance = sa.create_engine(
            self._engine_url().set(database='postgres'),
            isolation_level='AUTOCOMMIT',
            connect_args={'connect_timeout': POSTGRES_CONNECT_TIMEOUT},
            poolclass=NullPool,
        )
        try:
            with maintenance.connect() as conn:
                exists = conn.execute(
                    sa.text('SELECT 1 FROM pg_database WHERE datname = :name'),
                    {'name': self.location['dbname']},
                ).scalar()
                if exists:
                    # The database exists: do not treat authentication or
                    # access failures as a request to create another one.
                    raise StorageError(
                        'Could not connect to the existing PostgreSQL database.'
                    ) from connection_error
                name = maintenance.dialect.identifier_preparer.quote_identifier(
                    self.location['dbname']
                )
                try:
                    conn.exec_driver_sql('CREATE DATABASE ' + name)
                except sa.exc.DBAPIError as exc:
                    sqlstate = getattr(exc.orig, 'sqlstate', None)
                    if sqlstate == '42P04':
                        # Another startup created the database first.
                        return
                    if sqlstate == '42501':
                        raise StorageError(
                            'The PostgreSQL database does not exist and the '
                            'configured user cannot create it. Grant CREATEDB '
                            'or ask an administrator to create the database.'
                        ) from exc
                    raise
        finally:
            maintenance.dispose()

    def _create_schema(self):
        engine = self._get_engine()
        inspector = sa.inspect(engine)
        if 'users' in inspector.get_table_names():
            columns = {c['name'] for c in inspector.get_columns('users')}
            if 'file_quota' not in columns:
                raise StorageError(
                    'The existing pdm.db uses the old A1 layout. '
                    'Please delete pdm.db and start again.'
                )
        if 'files' in inspector.get_table_names():
            columns = {c['name'] for c in inspector.get_columns('files')}
            if 'content' not in columns or 'stored_name' in columns:
                raise StorageError(
                    'The existing files table is not compatible with '
                    'database file storage. Use an empty database and '
                    'initialize again.'
                )
        metadata.create_all(engine)
        with engine.begin() as conn:
            row = conn.execute(
                sa.select(system_state_table.c.id)
                .where(system_state_table.c.id == 1)
            ).first()
            if row is None:
                conn.execute(
                    system_state_table.insert()
                    .values(id=1, admin_initialized=False)
                )


class Store:
    # Data access for one open transaction. Only valid inside the with block.

    def __init__(self, conn, backend, write=False):
        self._conn = conn
        self._backend = backend
        self._write = write
        self._locked_user_ids = set()
        self._locked_system_state = False

    # Write methods refuse to run in read transactions.
    def _require_write(self):
        if not self._write:
            raise RuntimeError('Store write methods need transaction(write=True).')

    # Run a statement and translate driver errors into storage exceptions.
    # A confirmed unique violation becomes ConflictError; every other
    # integrity failure stays StorageError so it is never mistaken for a
    # duplicate username.
    def _execute(self, statement, params=None):
        try:
            return self._conn.execute(statement, params or {})
        except sa.exc.IntegrityError as exc:
            orig = exc.orig
            sqlstate = getattr(orig, 'sqlstate', None)
            if sqlstate == '23505' or 'UNIQUE constraint failed' in str(orig):
                raise ConflictError('A unique constraint was violated.') from exc
            raise StorageError(
                'A database integrity rule was violated.'
            ) from exc
        except sa.exc.SQLAlchemyError as exc:
            raise StorageError('A database operation failed.') from exc

    # Helper: the standard column list for a user row
    def _user_select(self):
        return sa.select(*USER_SELECT_COLUMNS)

    def get_user_by_id(self, user_id):
        row = self._execute(
            self._user_select().where(users_table.c.id == user_id)
        ).mappings().first()
        return _user_dict(row)

    def get_user_by_username(self, username):
        row = self._execute(
            self._user_select().where(users_table.c.username == username)
        ).mappings().first()
        return _user_dict(row)

    # Create an ordinary enabled user. Role is never accepted from callers.
    def create_user(self, username, password_hash, file_quota=100):
        self._require_write()
        file_quota = _validate_quota(file_quota)
        return self._insert_user(username, password_hash, 'user', file_quota)

    # Low level admin creation. Only the initialization service may call it
    # after locking system_state; normal registration must not use this.
    def create_admin(self, username, password_hash):
        self._require_write()
        if not self._locked_system_state:
            raise RuntimeError('create_admin needs lock_system_state first.')
        return self._insert_user(username, password_hash, 'admin', 100)

    # Helper: insert a user row and return the full User dict. The primary
    # key retrieval works on every backend (lastrowid or RETURNING).
    def _insert_user(self, username, password_hash, role, file_quota):
        result = self._execute(
            users_table.insert().values(
                username=username, password_hash=password_hash,
                role=role, enabled=True, file_quota=file_quota,
            )
        )
        return self.get_user_by_id(result.inserted_primary_key[0])

    # Prefer an administrator with a password; otherwise return the
    # existing reset account so setup can reuse it instead of adding one.
    def get_admin(self):
        row = self._execute(
            self._user_select().where(users_table.c.role == 'admin')
            .order_by((users_table.c.password_hash != '').desc(), users_table.c.id)
        ).mappings().first()
        return _user_dict(row)

    # Restore the reset administrator while holding the setup state lock.
    def restore_admin(self, user_id, username, password_hash):
        self._require_write()
        if not self._locked_system_state:
            raise RuntimeError('restore_admin needs lock_system_state first.')
        self._execute(
            users_table.update()
            .where(users_table.c.id == user_id, users_table.c.role == 'admin')
            .values(username=username, password_hash=password_hash, enabled=True)
        )
        self._execute(sessions_table.delete().where(sessions_table.c.user_id == user_id))

    # Ordinary users with their file counts, ordered by id, zero files kept.
    def list_normal_users(self):
        rows = self._execute(
            sa.select(
                users_table.c.id, users_table.c.username,
                users_table.c.enabled, users_table.c.file_quota,
                sa.func.count(files_table.c.id).label('file_count'),
            )
            .select_from(
                users_table.join(
                    files_table, files_table.c.user_id == users_table.c.id,
                    isouter=True,
                )
            )
            .where(users_table.c.role == 'user')
            .group_by(
                users_table.c.id, users_table.c.username,
                users_table.c.enabled, users_table.c.file_quota,
            )
            .order_by(users_table.c.id.asc())
        ).mappings().all()
        return [
            {
                'id': int(row['id']),
                'username': row['username'],
                'enabled': bool(row['enabled']),
                'file_quota': int(row['file_quota']),
                'file_count': int(row['file_count']),
            }
            for row in rows
        ]

    # Lock one user row for the rest of the transaction. Row-capable
    # backends append FOR UPDATE; SQLite relies on its write lock.
    def lock_user(self, user_id):
        self._require_write()
        stmt = self._user_select().where(users_table.c.id == user_id)
        if self._backend in ROW_LOCK_BACKENDS:
            stmt = stmt.with_for_update()
        user = _user_dict(self._execute(stmt).mappings().first())
        if user is not None:
            self._locked_user_ids.add(user['id'])
        return user

    # Set the enabled flag of an ordinary user that was locked earlier.
    # Disabling also deletes all sessions of the user in the same
    # transaction; enabling leaves sessions untouched.
    def set_user_enabled(self, user_id, enabled):
        self._require_write()
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be a strict boolean.')
        self._check_locked_target(user_id)
        self._execute(
            users_table.update()
            .where(users_table.c.id == user_id)
            .values(enabled=enabled)
        )
        if not enabled:
            self._execute(
                sessions_table.delete()
                .where(sessions_table.c.user_id == user_id)
            )
        return self.get_user_by_id(user_id)

    # Set the quota of an ordinary user that was locked earlier.
    def set_user_quota(self, user_id, file_quota):
        self._require_write()
        file_quota = _validate_quota(file_quota)
        self._check_locked_target(user_id)
        self._execute(
            users_table.update()
            .where(users_table.c.id == user_id)
            .values(file_quota=file_quota)
        )
        return self.get_user_by_id(user_id)

    # Helper: the target must be locked, existing and not an administrator
    def _check_locked_target(self, user_id):
        if user_id not in self._locked_user_ids:
            raise RuntimeError('Target user must be locked with lock_user first.')
        user = self.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError('User not found.')
        if user['role'] == 'admin':
            raise InvalidTargetError('Administrators cannot be targeted.')

    # Session lookup joined with the user row. Disabling deletes the user's
    # sessions, so disabled tokens resolve to nothing.
    def get_user_by_token(self, token):
        row = self._execute(
            self._user_select()
            .select_from(
                sessions_table.join(
                    users_table, users_table.c.id == sessions_table.c.user_id
                )
            )
            .where(sessions_table.c.token == token)
        ).mappings().first()
        return _user_dict(row)

    # Insert a session row. Duplicate tokens raise ConflictError.
    def create_session(self, token, user_id):
        self._require_write()
        self._execute(
            sessions_table.insert().values(token=token, user_id=user_id)
        )

    # Delete one session. Unknown tokens are a successful no-op.
    def delete_session(self, token):
        self._require_write()
        self._execute(
            sessions_table.delete().where(sessions_table.c.token == token)
        )

    def count_user_files(self, user_id):
        count = self._execute(
            sa.select(sa.func.count())
            .select_from(files_table)
            .where(files_table.c.user_id == user_id)
        ).scalar_one()
        return int(count)

    # Metadata of the user's own files, newest first, stable on ties by id.
    def list_user_files(self, user_id):
        rows = self._execute(
            sa.select(*FILE_SELECT_COLUMNS)
            .where(files_table.c.user_id == user_id)
            .order_by(files_table.c.uploaded_at.desc(), files_table.c.id.desc())
        ).mappings().all()
        return [_file_dict(row) for row in rows]

    # Own file metadata only; other users' files look like missing files.
    def get_user_file(self, file_id, user_id):
        row = self._execute(
            sa.select(*FILE_SELECT_COLUMNS).where(
                files_table.c.id == file_id,
                files_table.c.user_id == user_id,
            )
        ).mappings().first()
        return _file_dict(row)

    # Own file metadata plus the raw bytes, read in one query that filters
    # on id and user_id together. Only downloads use this method.
    def get_user_file_content(self, file_id, user_id):
        row = self._execute(
            sa.select(*FILE_SELECT_COLUMNS, files_table.c.content).where(
                files_table.c.id == file_id,
                files_table.c.user_id == user_id,
            )
        ).mappings().first()
        if row is None:
            return None
        file_row = _file_dict(row)
        file_row['content'] = bytes(row['content'])
        return file_row

    # Store metadata and the raw bytes in one INSERT. The size is derived
    # from the content itself, so the two can never disagree.
    def insert_file(self, user_id, filename, category, content):
        self._require_write()
        if not isinstance(filename, str) or not filename:
            raise ValueError('filename must be a non-empty string.')
        if category not in classify.CATEGORIES:
            raise ValueError('Not a valid category.')
        if not isinstance(content, bytes):
            raise ValueError('content must be bytes.')
        result = self._execute(
            files_table.insert().values(
                user_id=user_id, filename=filename, category=category,
                size=len(content), content=content,
            )
        )
        return self.get_user_file(result.inserted_primary_key[0], user_id)

    # Update the category of the user's own file.
    def change_file_category(self, file_id, user_id, category):
        self._require_write()
        if category not in classify.CATEGORIES:
            raise ValueError('Not a valid category.')
        if self.get_user_file(file_id, user_id) is None:
            raise NotFoundError('File not found.')
        self._execute(
            files_table.update()
            .where(files_table.c.id == file_id, files_table.c.user_id == user_id)
            .values(category=category)
        )
        return self.get_user_file(file_id, user_id)

    # Delete the user's own file row; metadata and content go together.
    # A zero rowcount means the file is missing or belongs to another user.
    def delete_file(self, file_id, user_id):
        self._require_write()
        result = self._execute(
            files_table.delete().where(
                files_table.c.id == file_id, files_table.c.user_id == user_id
            )
        )
        if result.rowcount == 0:
            raise NotFoundError('File not found.')

    # Read the fixed system_state row without locking or changing it. The
    # row must already exist; it is never recreated on demand.
    def get_system_state(self):
        row = self._execute(
            sa.select(
                system_state_table.c.id, system_state_table.c.admin_initialized
            ).where(system_state_table.c.id == 1)
        ).mappings().first()
        if row is None:
            raise StorageError('The system_state row is missing.')
        return {'id': 1, 'admin_initialized': bool(row['admin_initialized'])}

    # Lock the single system_state row. The row must already exist; it is
    # never recreated on demand inside a request.
    def lock_system_state(self):
        self._require_write()
        stmt = sa.select(
            system_state_table.c.id, system_state_table.c.admin_initialized
        ).where(system_state_table.c.id == 1)
        if self._backend in ROW_LOCK_BACKENDS:
            stmt = stmt.with_for_update()
        row = self._execute(stmt).mappings().first()
        if row is None:
            raise StorageError('The system_state row is missing.')
        self._locked_system_state = True
        return {'id': 1, 'admin_initialized': bool(row['admin_initialized'])}

    # Mark initialization complete. Requires the locked system state row and
    # must run in the same Store as the admin creation it belongs to.
    def mark_admin_initialized(self):
        self._require_write()
        if not self._locked_system_state:
            raise RuntimeError('System state must be locked before it is updated.')
        self._execute(
            system_state_table.update()
            .where(system_state_table.c.id == 1)
            .values(admin_initialized=True)
        )