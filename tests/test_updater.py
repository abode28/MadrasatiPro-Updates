# -*- coding: utf-8 -*-
import unittest
from updater import UpdateError, version_key, is_newer, validate_repository

class UpdaterHelperTests(unittest.TestCase):
    def test_version_key_accepts_v_prefix(self):
        self.assertEqual(version_key("v1.12.3"), (1, 12, 3))

    def test_version_comparison(self):
        self.assertTrue(is_newer("1.0.10", "1.0.9"))
        self.assertFalse(is_newer("v1.0.1", "1.0.1"))

    def test_repository_validation(self):
        self.assertEqual(validate_repository("SchoolOrg/MadrasatiPro"), "SchoolOrg/MadrasatiPro")
        with self.assertRaises(UpdateError):
            validate_repository("OWNER/REPOSITORY")
        with self.assertRaises(UpdateError):
            validate_repository("https://github.com/SchoolOrg/MadrasatiPro")

if __name__ == "__main__":
    unittest.main()
