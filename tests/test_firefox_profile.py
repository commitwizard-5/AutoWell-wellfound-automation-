"""Tests for isolated Firefox automation profiles."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autoapply.autoapply import create_automation_profile


class FirefoxProfileTests(unittest.TestCase):
    def test_copies_profile_data_without_firefox_lock_files(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(dir=project_root) as source_directory:
            source = Path(source_directory)
            (source / "cookies.sqlite").write_text("session data", encoding="utf-8")
            (source / "parent.lock").write_text("locked", encoding="utf-8")
            (source / ".parentlock").write_text("locked", encoding="utf-8")
            (source / "storage").mkdir()
            (source / "storage" / "volatile-data").write_text("ignore me", encoding="utf-8")

            profile = create_automation_profile(source, temporary_root=project_root)
            try:
                self.assertEqual((profile.path / "cookies.sqlite").read_text(encoding="utf-8"), "session data")
                self.assertFalse((profile.path / "parent.lock").exists())
                self.assertFalse((profile.path / ".parentlock").exists())
                self.assertFalse((profile.path / "storage").exists())
            finally:
                profile.cleanup()


if __name__ == "__main__":
    unittest.main()
