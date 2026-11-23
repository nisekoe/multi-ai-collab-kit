"""Regression coverage for the shared Codex Windows workspace path key."""
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

import os
import unittest

from codex_picker_adapter import fs_path_key


@unittest.skipUnless(os.name == "nt", "Windows workspace path syntax")
class WindowsFsPathKeyTests(unittest.TestCase):
    # Synthetic drive. The path key checks spelling and does not open files.
    drive = "F:"

    def test_drive_and_extended_drive_with_spaces(self):
        drive = self.drive
        expected = rf"{drive.lower()}\work folder\project"
        paths = (
            f"{drive}/Work Folder/Project/",
            rf"\\?\{drive}\Work Folder\.\Project",
            f"//?/{drive.lower()}/Work Folder/Project",
            f"file:///{drive}/Work%20Folder/Project",
            f"file://{drive}/Work%20Folder/Project",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(fs_path_key(path), expected)

    def test_extended_unc_retains_host_and_share(self):
        expected = r"\\server\share\work"
        paths = (
            r"\\SERVER\Share\Work",
            r"\\?\UNC\server\share\work",
            r"\\?\unc\SERVER\Share\Work",
            "//?/UNC/server/share/work",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(fs_path_key(path), expected)

    def test_host_qualified_file_uri_is_unc(self):
        expected = r"\\server\share\work folder"
        uri = "file://server/share/Work%20Folder"
        self.assertEqual(fs_path_key(uri), expected)
        self.assertEqual(fs_path_key("file:////server/share/Work%20Folder"), expected)
        self.assertNotEqual(fs_path_key(uri), fs_path_key("server/share/Work Folder"))
        self.assertNotEqual(fs_path_key(uri), fs_path_key(r"\\other\share\work folder"))

    def test_unc_share_root_with_optional_separator(self):
        expected = r"\\server\share"
        paths = (
            expected,
            expected + "\\",
            expected + "\\.",
            r"\\?\UNC\server\share",
            "file://server/share",
            "file://server/share/",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(fs_path_key(path), expected)

    def test_literal_percent_escape_remains_distinct_from_space(self):
        raw_drive = f"{self.drive}/a%20b"
        raw_unc = r"\\server\share\a%20b"
        self.assertNotEqual(fs_path_key(raw_drive), fs_path_key(f"{self.drive}/a b"))
        self.assertNotEqual(fs_path_key(raw_unc), fs_path_key(r"\\server\share\a b"))
        self.assertEqual(fs_path_key(f"file:///{self.drive}/a%2520b"), fs_path_key(raw_drive))
        self.assertEqual(fs_path_key("file://server/share/a%2520b"), fs_path_key(raw_unc))

    def test_different_interior_space_counts_remain_distinct(self):
        self.assertNotEqual(fs_path_key(f"{self.drive}/a  b"), fs_path_key(f"{self.drive}/a b"))
        self.assertNotEqual(fs_path_key("file://server/share/a%20%20b"),
                            fs_path_key(r"\\server\share\a b"))

    def test_empty_path_is_not_the_current_directory(self):
        for path in (None, "", "  ", "file://"):
            with self.subTest(path=path):
                self.assertEqual(fs_path_key(path), "")
        self.assertEqual(fs_path_key("."), ".")


if __name__ == "__main__":
    unittest.main()
