import unittest

import quota


class QuotaRuleTests(unittest.TestCase):

    def test_whole_numbers_are_accepted(self):
        self.assertEqual(quota.parse_file_quota('0'), 0)
        self.assertEqual(quota.parse_file_quota(' 12 '), 12)

    def test_invalid_values_are_rejected(self):
        for raw in ('', '  ', '-1', '1.5', '10abc', 'ten', None, True):
            with self.assertRaises(quota.QuotaValidationError):
                quota.parse_file_quota(raw)

    def test_upload_block_uses_the_file_count(self):
        self.assertFalse(quota.upload_blocked(1, 2))
        self.assertTrue(quota.upload_blocked(2, 2))
        self.assertTrue(quota.upload_blocked(0, 0))

    def test_limit_message_names_the_quota(self):
        self.assertEqual(
            quota.limit_message(3),
            'You have reached your limit of 3 files.'
        )


if __name__ == '__main__':
    unittest.main()
