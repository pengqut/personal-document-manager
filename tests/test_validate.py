import unittest

import validate


class ValidateTests(unittest.TestCase):

    # A normal username should pass.
    def test_valid_username_passes(self):
        self.assertIsNone(validate.validate_username('testuser1'))

    # A username under 6 characters should fail.
    def test_short_username_fails(self):
        self.assertIsNotNone(validate.validate_username('abc'))

    # A normal password should pass.
    def test_valid_password_passes(self):
        self.assertIsNone(validate.validate_password('Abcdefg1'))

    # A password with no upper case letter or number should fail.
    def test_weak_password_fails(self):
        self.assertIsNotNone(validate.validate_password('weak'))

    # Hashing a password and checking the same password should match.
    def test_correct_password_matches_its_hash(self):
        hashed = validate.hash_password('Abcdefg1')
        self.assertTrue(validate.check_password('Abcdefg1', hashed))

    # Checking a different password against the hash should not match.
    def test_wrong_password_does_not_match_hash(self):
        hashed = validate.hash_password('Abcdefg1')
        self.assertFalse(validate.check_password('WrongPassword', hashed))


if __name__ == '__main__':
    unittest.main()
