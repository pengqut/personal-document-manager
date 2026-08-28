import json
import os
import secrets

from bottle import route, run, request, response, redirect, static_file

import database
import validate

DB_PATH = 'pdm.db'
STATIC_DIR = 'static'
UPLOAD_DIR = 'uploads'

db = database.connect(DB_PATH)
os.makedirs(UPLOAD_DIR, exist_ok=True)

with open('config.json') as config_file:
    LIMITS = json.load(config_file)


# Look up the logged in user
def current_user():
    token = request.get_cookie('token')
    if not token:
        return None
    return db.execute(
        'SELECT users.* FROM sessions '
        'JOIN users ON users.id = sessions.user_id '
        'WHERE sessions.token = ?',
        (token,)
    ).fetchone()


# Redirect to the login page
@route('/')
def index():
    redirect('/login.html')


# Serve static files
@route('/<filename:path>')
def serve_static(filename):
    return static_file(filename, root=STATIC_DIR)


# Register a new user
@route('/api/register', method='POST')
def register():
    username = request.forms.getunicode('username', '').strip()
    password = request.forms.getunicode('password', '')

    error = validate.validate_username(username) or validate.validate_password(password)
    if error:
        response.status = 400
        return {'error': error}

    already_exists = db.execute(
        'SELECT id FROM users WHERE username = ?', (username,)
    ).fetchone()
    if already_exists:
        response.status = 409
        return {'error': 'Username already exists.'}

    db.execute(
        'INSERT INTO users (username, password_hash) VALUES (?, ?)',
        (username, validate.hash_password(password))
    )
    db.commit()
    return {'ok': True}


# Log in
@route('/api/login', method='POST')
def login():
    username = request.forms.getunicode('username', '').strip()
    password = request.forms.getunicode('password', '')

    user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    if not user or not validate.check_password(password, user['password_hash']):
        response.status = 401
        return {'error': 'Username or password is incorrect.'}

    token = secrets.token_hex(16)
    db.execute('INSERT INTO sessions (token, user_id) VALUES (?, ?)', (token, user['id']))
    db.commit()
    response.set_cookie('token', token)
    return {'ok': True, 'username': user['username']}


# Log out
@route('/api/logout', method='POST')
def logout():
    token = request.get_cookie('token')
    if token:
        db.execute('DELETE FROM sessions WHERE token = ?', (token,))
        db.commit()
    response.delete_cookie('token')
    return {'ok': True}


# Get the current user
@route('/api/me')
def me():
    user = current_user()
    if not user:
        response.status = 401
        return {'error': 'Not logged in.'}
    return {'username': user['username']}


# Count a user's files
def count_user_files(user_id):
    row = db.execute('SELECT COUNT(*) AS c FROM files WHERE user_id = ?', (user_id,)).fetchone()
    return row['c']


# Get the upload limits
@route('/api/limits')
def get_limits():
    user = current_user()
    if not user:
        response.status = 401
        return {'error': 'Not logged in.'}
    return {
        'max_file_mb': LIMITS['max_file_mb'],
        'user_file_limit': LIMITS['user_file_limit'],
        'used': count_user_files(user['id'])
    }


# Upload a file
@route('/api/upload', method='POST')
def upload():
    user = current_user()
    if not user:
        response.status = 401
        return {'error': 'Not logged in.'}

    if count_user_files(user['id']) >= LIMITS['user_file_limit']:
        response.status = 403
        return {'error': 'You have reached your limit of %d files.' % LIMITS['user_file_limit']}

    upload_file = request.files.get('file')
    if not upload_file:
        response.status = 400
        return {'error': 'No file was sent.'}

    file_bytes = upload_file.file.read()
    max_bytes = LIMITS['max_file_mb'] * 1024 * 1024
    if len(file_bytes) > max_bytes:
        response.status = 400
        return {'error': 'This file is over the %d MB limit.' % LIMITS['max_file_mb']}

    # Only keep the plain file name, in case the browser sends a path with it.
    original_name = os.path.basename(upload_file.filename)
    stored_name = secrets.token_hex(8) + '_' + original_name
    with open(os.path.join(UPLOAD_DIR, stored_name), 'wb') as saved_file:
        saved_file.write(file_bytes)

    # Real classification by file type is added in the next Jira task.
    category = 'Unclassified'
    db.execute(
        'INSERT INTO files (user_id, filename, stored_name, category, size) VALUES (?, ?, ?, ?, ?)',
        (user['id'], original_name, stored_name, category, len(file_bytes))
    )
    db.commit()
    return {'ok': True, 'category': category}


if __name__ == '__main__':
    run(host='localhost', port=8080, debug=True)
