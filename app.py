import json
import os
import sys
from urllib.parse import quote

from bottle import (
    HTTPError, HTTPResponse, hook, install, redirect, request, response,
    route, run, static_file,
)

import admin_setup
import auth
import classify
import database
import limits
import quota
import validate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')

with open(os.path.join(BASE_DIR, 'config.json')) as config_file:
    LIMITS = json.load(config_file)

# Configuration only; connections are opened per transaction.
db = database.Database.from_environment(BASE_DIR)
auth_service = auth.AuthService(db)
admin_setup_service = admin_setup.AdminSetupFacade(db)
INSTANCE_NAME = os.environ.get('PDM_INSTANCE_NAME', '')


@hook('after_request')
def mark_instance():
    # Set on each EC2 instance so repeated requests through the load
    # balancer show which server answered.
    if INSTANCE_NAME:
        response.set_header('X-PDM-Instance', INSTANCE_NAME)


# Wrap every route and map exceptions to uniform JSON responses, so the
# routes themselves contain only business logic. Bottle's own control
# flow exceptions pass through untouched.
def error_plugin(callback):
    def wrapper(*args, **kwargs):
        try:
            path = request.path
            setup_paths = ('/api/admin/setup', '/api/admin/setup-status')
            is_api = path.startswith('/api/')
            is_asset = path != '/' and not path.lower().endswith('.html') and '.' in path.rsplit('/', 1)[-1]
            if path != '/admin-setup.html' and path not in setup_paths and (is_api or not is_asset):
                if not admin_setup_service.get_status()['initialized']:
                    if is_api:
                        response.status = 409
                        return {'error': 'Administrator setup is required.'}
                    redirect('/admin-setup.html')
            admin_public_paths = setup_paths + ('/api/admin/login',)
            if path.startswith('/api/admin/') and path not in admin_public_paths:
                auth.require_admin(current_user())
            return callback(*args, **kwargs)
        except (HTTPResponse, HTTPError):
            raise
        except auth.NotAuthenticatedError:
            response.status = 401
            return {'error': 'Not logged in.'}
        except auth.AuthenticationError:
            response.status = 401
            return {'error': 'Username or password is incorrect.'}
        except auth.PermissionDeniedError:
            response.status = 403
            return {'error': 'Administrator rights are required.'}
        except admin_setup.SetupValidationError as error:
            response.status = 400
            return {'error': str(error)}
        except quota.QuotaValidationError as error:
            response.status = 400
            return {'error': str(error)}
        except quota.QuotaTargetError as error:
            response.status = 400
            return {'error': str(error)}
        except quota.UserNotFoundError:
            response.status = 404
            return {'error': 'User not found.'}
        except admin_setup.SetupClosedError:
            response.status = 409
            return {'error': 'Administrator setup is already complete.'}
        except database.NotFoundError:
            response.status = 404
            return {'error': 'File not found.'}
        except database.StorageError:
            response.status = 503
            return {'error': 'Service temporarily unavailable.'}
        except Exception:
            response.status = 500
            return {'error': 'Internal server error.'}
    return wrapper


install(error_plugin)


# Look up the logged in user through the shared auth service
def current_user():
    return auth_service.current_user(request.get_cookie('token'))


# Read a request field from raw JSON first, then fall back to form data.
def request_value(name, default=''):
    data = request.json
    if isinstance(data, dict) and name in data:
        value = data.get(name)
        if value is None:
            return default
        if isinstance(value, bool):
            return 'true' if value else 'false'
        return str(value)
    return request.forms.getunicode(name, default)


# Require a logged in user or raise NotAuthenticatedError (mapped to 401).
def require_logged_in():
    user = current_user()
    if user is None:
        raise auth.NotAuthenticatedError('Not logged in.')
    return user


# Redirect to the login page
@route('/')
def index():
    redirect('/login.html')


# Register a new user
@route('/api/register', method='POST')
def register():
    username = request_value('username', '').strip()
    password = request_value('password', '')

    error = validate.validate_username(username) or validate.validate_password(password)
    if error:
        response.status = 400
        return {'error': error}

    try:
        with db.transaction(write=True) as store:
            if store.get_user_by_username(username) is not None:
                response.status = 409
                return {'error': 'Username already exists.'}
            store.create_user(username, validate.hash_password(password))
    except database.ConflictError:
        response.status = 409
        return {'error': 'Username already exists.'}
    return {'ok': True}


# Show whether the one-time administrator setup is still open
@route('/api/admin/setup-status')
def admin_setup_status():
    return admin_setup_service.get_status()


# Create the first administrator account once
@route('/api/admin/setup', method='POST')
def admin_setup_submit():
    username = request_value('username', '')
    password = request_value('password', '')
    try:
        return admin_setup_service.setup_admin(username, password)
    except database.ConflictError:
        # Caught after the failed transaction rolled back, so the
        # response is built outside it.
        response.status = 409
        return {'error': 'Username already exists.'}


# Log in
@route('/api/login', method='POST')
def login():
    username = request_value('username', '')
    password = request_value('password', '')
    result = auth_service.authenticate(username, password, expected_role='user')
    response.set_cookie('token', result['token'], path='/')
    return {'ok': True, 'username': result['user']['username']}


# Administrator login is separate from ordinary user login.
@route('/api/admin/login', method='POST')
def admin_login():
    username = request_value('username', '')
    password = request_value('password', '')
    result = auth_service.authenticate(username, password, expected_role='admin')
    response.set_cookie('token', result['token'], path='/')
    return {'ok': True, 'username': result['user']['username']}


# The plugin checks administrator permission for this and other admin APIs.
@route('/api/admin/me')
def admin_me():
    user = auth.require_admin(current_user())
    return {'username': user['username'], 'role': user['role']}
    
# Return the list of normal users for admin management.
@route('/api/admin/users')
def admin_users():
    with db.transaction() as store:
        users = store.list_normal_users()
    return {'users': users}

# Enable or disable a normal user.
@route('/api/admin/users/<user_id:int>/status', method='POST')
def admin_user_status(user_id):
    enabled_value = request_value('enabled', '').strip().lower()

    if enabled_value not in ('true', 'false'):
        response.status = 400
        return {'error': 'Invalid enabled value.'}

    enabled = enabled_value == 'true'

    with db.transaction(write=True) as store:
        user = store.lock_user(user_id)

        if user is None:
            response.status = 404
            return {'error': 'User not found.'}

        if user['role'] == 'admin':
            response.status = 400
            return {'error': 'Administrator accounts cannot be changed.'}

        updated = store.set_user_enabled(user_id, enabled)

    return {
        'ok': True,
        'enabled': updated['enabled']
    }


# Set the file quota of a normal user.
@route('/api/admin/users/<user_id:int>/quota', method='POST')
def admin_user_quota(user_id):
    raw_quota = request_value('file_quota', '')
    command = quota.SetFileQuota(db, user_id, raw_quota)
    return command.execute()


# Log out
@route('/api/logout', method='POST')
def logout():
    auth_service.logout(request.get_cookie('token'))
    response.delete_cookie('token', path='/')
    return {'ok': True}


# Get the current user
@route('/api/me')
def me():
    user = require_logged_in()
    return {'username': user['username']}


# Get the upload limits
@route('/api/limits')
def get_limits():
    user = require_logged_in()
    with db.transaction() as store:
        fresh = store.get_user_by_id(user['id'])
        used = store.count_user_files(user['id'])
    return {
        'max_file_mb': LIMITS['max_file_mb'],
        'user_file_limit': fresh['file_quota'],
        'used': used,
    }


# Upload a file
@route('/api/upload', method='POST')
def upload():
    user = require_logged_in()

    upload_file = request.files.get('file')
    if not upload_file:
        response.status = 400
        return {'error': 'No file was sent.'}

    file_bytes = upload_file.file.read()
    if limits.is_file_too_big(len(file_bytes), LIMITS['max_file_mb']):
        response.status = 400
        return {'error': 'This file is over the %d MB limit.' % LIMITS['max_file_mb']}

    raw_name = upload_file.raw_filename
    if not isinstance(raw_name, str):
        raw_name = raw_name.decode('utf8', 'ignore')
    original_name = os.path.basename(raw_name.replace('\\', os.path.sep))
    if not original_name:
        response.status = 400
        return {'error': 'No file was sent.'}
    category = classify.classify(original_name)

    # The count check and the insert share one write transaction that
    # holds the user row lock, so the limit cannot be raced. Metadata
    # and bytes commit together or not at all.
    with db.transaction(write=True) as store:
        locked = store.lock_user(user['id'])
        if locked is None:
            raise auth.NotAuthenticatedError('Not logged in.')
        file_quota = locked['file_quota']
        if quota.upload_blocked(store.count_user_files(user['id']), file_quota):
            response.status = 403
            return {'error': quota.limit_message(file_quota)}
        store.insert_file(user['id'], original_name, category, file_bytes)
    return {'ok': True, 'category': category}


# List a user's files
@route('/api/files')
def get_files():
    user = require_logged_in()
    with db.transaction() as store:
        rows = store.list_user_files(user['id'])
    return {
        'files': [
            {
                'id': row['id'],
                'filename': row['filename'],
                'category': row['category'],
                'size': row['size'],
                'uploaded_at': row['uploaded_at'],
            }
            for row in rows
        ]
    }


# Download a file
@route('/api/download/<file_id:int>')
def download_file(file_id):
    user = require_logged_in()
    with db.transaction() as store:
        file_row = store.get_user_file_content(file_id, user['id'])
    if file_row is None:
        raise database.NotFoundError('File not found.')
    # The transaction is closed before the response body is built.
    headers = {
        'Content-Type': 'application/octet-stream',
        'Content-Length': str(len(file_row['content'])),
        'Content-Disposition': (
            'attachment; filename="download"; filename*=UTF-8\'\''
            + quote(file_row['filename'], safe='')
        ),
    }
    return HTTPResponse(body=file_row['content'], headers=headers)


# Change a file's category
@route('/api/files/<file_id:int>/category', method='POST')
def change_category(file_id):
    user = require_logged_in()

    new_category = request_value('category', '')
    if new_category not in classify.CATEGORIES:
        response.status = 400
        return {'error': 'Not a valid category.'}

    with db.transaction(write=True) as store:
        store.lock_user(user['id'])
        updated = store.change_file_category(file_id, user['id'], new_category)
    return {'ok': True, 'category': updated['category']}


# Delete a file
@route('/api/files/<file_id:int>/delete', method='POST')
def delete_file(file_id):
    user = require_logged_in()

    # The lock and the delete share one write transaction, so the row is
    # removed only for the owner's own file.
    with db.transaction(write=True) as store:
        store.lock_user(user['id'])
        store.delete_file(file_id, user['id'])
    return {'ok': True}


@route('/<filename:path>')
def serve_static(filename):
    if os.path.normpath(filename) == 'admin.html':
        user = current_user()
        if user is None:
            redirect('/admin-login.html')
        auth.require_admin(user)
    return static_file(filename, root=STATIC_DIR)


if __name__ == '__main__':
    # Prepare the schema before serving requests. A failed preparation
    # stops the startup with a short message, without driver tracebacks.
    try:
        db.initialize_schema()
    except database.StorageError as error:
        print('Database initialization failed: %s' % error, file=sys.stderr)
        sys.exit(1)
    run(host='0.0.0.0', port=int(os.environ.get('PDM_PORT') or 80), debug=False)