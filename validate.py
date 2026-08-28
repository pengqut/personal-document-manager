import re
import hashlib
import secrets

USERNAME_PATTERN = re.compile(r'^[A-Za-z0-9_]{6,10}$')


# Return an error message if the username breaks a rule, or None if it is fine.
def validate_username(username):
    if not USERNAME_PATTERN.fullmatch(username or ''):
        return 'Username must be 6 to 10 characters. Only letters, numbers and underscore are allowed.'
    return None


# Return an error message if the password breaks a rule, or None if it is fine.
def validate_password(password):
    password = password or ''
    if len(password) < 8:
        return 'Password must be at least 8 characters.'
    if not re.search(r'[A-Z]', password):
        return 'Password must include an upper case letter.'
    if not re.search(r'[a-z]', password):
        return 'Password must include a lower case letter.'
    if not re.search(r'[0-9]', password):
        return 'Password must include a number.'
    return None


# Turn a plain password into a salted hash. The plain password is never stored.
def hash_password(password):
    salt = secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode('utf-8')).hexdigest()
    return salt + '$' + digest


# Check a plain password against the salted hash saved in the database.
def check_password(password, stored_hash):
    salt, digest = stored_hash.split('$')
    return hashlib.sha256((salt + password).encode('utf-8')).hexdigest() == digest
