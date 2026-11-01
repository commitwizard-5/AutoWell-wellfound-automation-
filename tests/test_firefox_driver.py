"""Tests for stable Firefox WebDriver startup."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from autoapply.autoapply import _cached_geckodriver_path


class FirefoxDriverTests(unittest.TestCase):
    def test_uses_the_cached_driver_without_calling_install(self) -> None:
        manager = Mock()
        manager.driver = object()
        manager._get_driver_binary_path.return_value = r"C:\drivers\geckodriver.exe"

        with patch("autoapply.autoapply.GeckoDriverManager", return_value=manager):
            path = _cached_geckodriver_path()

        self.assertEqual(path, r"C:\drivers\geckodriver.exe")
        manager.install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
