# File-quota rules and the set-quota command. Routes call this module
# so the checks stay in one place.

import limits


class QuotaValidationError(Exception):
    # The submitted file_quota is not a non-negative whole number.
    pass


class QuotaTargetError(Exception):
    # The quota target is an administrator account.
    pass


class UserNotFoundError(Exception):
    # The quota target does not exist.
    pass


# Accept only a string of digits. Rejects blanks, signs, fractions and text.
def parse_file_quota(raw):
    if not isinstance(raw, str):
        raise QuotaValidationError('file_quota must be a non-negative integer.')
    text = raw.strip()
    if not text or not text.isdigit():
        raise QuotaValidationError('file_quota must be a non-negative integer.')
    return int(text)


# True when another upload would pass the user's quota.
def upload_blocked(files_used, file_quota):
    return limits.is_quota_reached(files_used, file_quota)


def limit_message(file_quota):
    return 'You have reached your limit of %d files.' % file_quota


# Command: validate one quota change and store it for a normal user.
class SetFileQuota:

    def __init__(self, db, user_id, raw_quota):
        self._db = db
        self._user_id = user_id
        self._raw_quota = raw_quota

    def execute(self):
        file_quota = parse_file_quota(self._raw_quota)
        with self._db.transaction(write=True) as store:
            user = store.lock_user(self._user_id)
            if user is None:
                raise UserNotFoundError('User not found.')
            if user['role'] == 'admin':
                raise QuotaTargetError(
                    'Administrator accounts cannot be changed.'
                )
            updated = store.set_user_quota(self._user_id, file_quota)
            file_count = store.count_user_files(self._user_id)
        return {
            'ok': True,
            'file_quota': updated['file_quota'],
            'file_count': file_count,
        }
