import os
import sys
import unittest

import limits

class LimitsTests(unittest.TestCase):

    # File under the size limit
    def test_small_file_is_not_too_big(self):
        self.assertFalse(limits.is_file_too_big(1024, 1))

    # File over the size limit
    def test_large_file_is_too_big(self):
        self.assertTrue(limits.is_file_too_big(2 * 1024 * 1024, 1))

    # Used files below the quota
    def test_quota_not_reached(self):
        self.assertFalse(limits.is_quota_reached(5, 100))

    # Used files at the quota
    def test_quota_reached(self):
        self.assertTrue(limits.is_quota_reached(100, 100))


if __name__ == '__main__':
    unittest.main()
