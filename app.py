import secrets

from bottle import route, run, request, response, redirect, static_file

import database
import validate

DB_PATH = 'pdm.db'
STATIC_DIR = 'static'

db = database.connect(DB_PATH)

# Look up the logged in user from the session cookie. Returns None if not logged in.
def current_user():
    """Look up the logged in user from the session cookie. Returns None if not logged in."""
    token = request.get_cookie('token')
    if not token:
        return None
    return db.execute(
        'SELECT users.* FROM sessions '
        'JOIN users ON users.id = sessions.user_id '
        'WHERE sessions.token = ?',
        (token,)
    ).fetchone()

# Send the browser to the login page.
@route('/')
def index():
    redirect('/login.html')

# Serve the html, css and js files from the static folder.
@route('/<filename:path>')
def serve_static(filename):
    """Serve the html, css and js files from the static folder."""
    return static_file(filename, root=STATIC_DIR)

# Create a new normal user account.
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

# Check the username and password, then start a session with a cookie.
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

# End the session and clear the cookie.
@route('/api/logout', method='POST')
def logout():
    token = request.get_cookie('token')
    if token:
        db.execute('DELETE FROM sessions WHERE token = ?', (token,))
        db.commit()
    response.delete_cookie('token')
    return {'ok': True}

# Tell the frontend who is currently logged in, if anyone.
@route('/api/me')
def me():
    user = current_user()
    if not user:
        response.status = 401
        return {'error': 'Not logged in.'}
    return {'username': user['username']}

if __name__ == '__main__':
    run(host='localhost', port=8080, debug=True)
