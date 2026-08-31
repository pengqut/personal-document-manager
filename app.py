import json
import os
import secrets

from bottle import route, run, request, response, redirect, static_file

import classify
import database
import limits
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

    if limits.is_quota_reached(count_user_files(user['id']), LIMITS['user_file_limit']):
        response.status = 403
        return {'error': 'You have reached your limit of %d files.' % LIMITS['user_file_limit']}

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
    stored_name = secrets.token_hex(8) + '_' + upload_file.filename
    with open(os.path.join(UPLOAD_DIR, stored_name), 'wb') as saved_file:
        saved_file.write(file_bytes)

    category = classify.classify(original_name)
    db.execute(
        'INSERT INTO files (user_id, filename, stored_name, category, size) VALUES (?, ?, ?, ?, ?)',
        (user['id'], original_name, stored_name, category, len(file_bytes))
    )
    db.commit()
    return {'ok': True, 'category': category}


# List a user's files
@route('/api/files')
def get_files():
    user = current_user()
    if not user:
        response.status = 401
        return {'error': 'Not logged in.'}
    rows = db.execute(
        'SELECT id, filename, category, size, uploaded_at FROM files '
        'WHERE user_id = ? ORDER BY uploaded_at DESC',
        (user['id'],)
    ).fetchall()
    return {'files': [dict(row) for row in rows]}


# Download a file
@route('/api/download/<file_id:int>')
def download_file(file_id):
    user = current_user()
    if not user:
        response.status = 401
        return {'error': 'Not logged in.'}
    file_row = db.execute(
        'SELECT * FROM files WHERE id = ? AND user_id = ?',
        (file_id, user['id'])
    ).fetchone()
    if not file_row:
        response.status = 404
        return {'error': 'File not found.'}
    return static_file(file_row['stored_name'], root=UPLOAD_DIR, download=file_row['filename'])


# Change a file's category
@route('/api/files/<file_id:int>/category', method='POST')
def change_category(file_id):
    user = current_user()
    if not user:
        response.status = 401
        return {'error': 'Not logged in.'}

    new_category = request.forms.getunicode('category', '')
    if new_category not in classify.CATEGORIES:
        response.status = 400
        return {'error': 'Not a valid category.'}

    file_row = db.execute(
        'SELECT id FROM files WHERE id = ? AND user_id = ?',
        (file_id, user['id'])
    ).fetchone()
    if not file_row:
        response.status = 404
        return {'error': 'File not found.'}

    db.execute('UPDATE files SET category = ? WHERE id = ?', (new_category, file_id))
    db.commit()
    return {'ok': True, 'category': new_category}


# Delete a file
@route('/api/files/<file_id:int>/delete', method='POST')
def delete_file(file_id):
    user = current_user()
    if not user:
        response.status = 401
        return {'error': 'Not logged in.'}

    file_row = db.execute(
        'SELECT * FROM files WHERE id = ? AND user_id = ?',
        (file_id, user['id'])
    ).fetchone()
    if not file_row:
        response.status = 404
        return {'error': 'File not found.'}

    stored_path = os.path.join(UPLOAD_DIR, file_row['stored_name'])
    if os.path.exists(stored_path):
        os.remove(stored_path)
    db.execute('DELETE FROM files WHERE id = ?', (file_id,))
    db.commit()
    return {'ok': True}


@route('/<filename:path>')
def serve_static(filename):
    return static_file(filename, root=STATIC_DIR)


if __name__ == '__main__':
    run(host='0.0.0.0', port=80, debug=True)
