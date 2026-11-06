import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from autoapply.helper.firefox_driver import resolve_geckodriver_path
from autoapply.autoapply import JobScraper


class FirefoxDriverResolutionTests(unittest.TestCase):
    def test_job_scraper_initializes_firefox_with_resolved_driver(self):
        firefox_driver = Mock()
        with (
            patch("autoapply.autoapply.resolve_geckodriver_path", return_value="C:/tools/geckodriver.exe"),
            patch("autoapply.autoapply.FirefoxService") as service,
            patch("autoapply.autoapply.FirefoxOptions"),
            patch("autoapply.autoapply.webdriver.Firefox", return_value=firefox_driver),
        ):
            scraper = JobScraper("C:/profiles/firefox")
        service.assert_called_once_with(executable_path="C:/tools/geckodriver.exe")
        firefox_driver.get.assert_called_once_with("https://wellfound.com/jobs")
        self.assertEqual(scraper.data, [])

    def test_configured_driver_path_is_preferred(self):
        with tempfile.TemporaryDirectory() as directory:
            driver_path = Path(directory) / "geckodriver.exe"
            driver_path.write_bytes(b"driver")
            with (
                patch.dict(os.environ, {"GECKODRIVER_PATH": str(driver_path)}),
                patch("autoapply.helper.firefox_driver.shutil.which") as which,
                patch("autoapply.helper.firefox_driver.GeckoDriverManager") as manager,
            ):
                self.assertEqual(resolve_geckodriver_path(), str(driver_path))
            which.assert_not_called()
            manager.assert_not_called()

    def test_driver_on_path_avoids_github_download(self):
        with tempfile.TemporaryDirectory() as directory:
            driver_path = Path(directory) / "geckodriver.exe"
            driver_path.write_bytes(b"driver")
            with (
                patch.dict(os.environ, {"GECKODRIVER_PATH": ""}, clear=False),
                patch("autoapply.helper.firefox_driver.shutil.which", return_value=str(driver_path)),
                patch("autoapply.helper.firefox_driver.GeckoDriverManager") as manager,
            ):
                self.assertEqual(resolve_geckodriver_path(), str(driver_path.resolve()))
            manager.assert_not_called()

    def test_autowell_cache_is_used_before_attempting_download(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "geckodriver.exe"
            cache_path.write_bytes(b"cached driver")
            with (
                patch.dict(os.environ, {"GECKODRIVER_PATH": ""}, clear=False),
                patch("autoapply.helper.firefox_driver.shutil.which", return_value=None),
                patch("autoapply.helper.firefox_driver._managed_driver_path", return_value=cache_path),
                patch("autoapply.helper.firefox_driver._legacy_wdm_driver_path", return_value=None),
                patch("autoapply.helper.firefox_driver.GeckoDriverManager") as manager,
            ):
                self.assertEqual(resolve_geckodriver_path(), str(cache_path.resolve()))
            manager.assert_not_called()

    def test_downloaded_driver_is_persistently_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "download" / "geckodriver.exe"
            downloaded.parent.mkdir()
            downloaded.write_bytes(b"downloaded driver")
            cache_path = Path(directory) / "cache" / "geckodriver.exe"
            with (
                patch.dict(os.environ, {"GECKODRIVER_PATH": ""}, clear=False),
                patch("autoapply.helper.firefox_driver.shutil.which", return_value=None),
                patch("autoapply.helper.firefox_driver._managed_driver_path", return_value=cache_path),
                patch("autoapply.helper.firefox_driver._legacy_wdm_driver_path", return_value=None),
                patch("autoapply.helper.firefox_driver.GeckoDriverManager") as manager,
            ):
                manager.return_value.install.return_value = str(downloaded)
                self.assertEqual(resolve_geckodriver_path(), str(cache_path.resolve()))
                self.assertEqual(cache_path.read_bytes(), b"downloaded driver")
                self.assertEqual(resolve_geckodriver_path(), str(cache_path.resolve()))
            manager.return_value.install.assert_called_once()

    def test_missing_driver_download_error_is_actionable(self):
        with (
            patch.dict(os.environ, {"GECKODRIVER_PATH": ""}, clear=False),
            patch("autoapply.helper.firefox_driver.shutil.which", return_value=None),
            patch("autoapply.helper.firefox_driver._managed_driver_path", return_value=Path("missing-cache-driver.exe")),
            patch("autoapply.helper.firefox_driver._legacy_wdm_driver_path", return_value=None),
            patch(
                "autoapply.helper.firefox_driver.GeckoDriverManager"
            ) as manager,
        ):
            manager.return_value.install.side_effect = TimeoutError("GitHub 504")
            with self.assertRaisesRegex(RuntimeError, "set GECKODRIVER_PATH"):
                resolve_geckodriver_path()

    def test_existing_webdriver_manager_cache_works_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            wdm_root = Path(directory) / ".wdm" / "drivers" / "geckodriver"
            wdm_binary = wdm_root / "win64" / "0.37.1" / "geckodriver.exe"
            wdm_binary.parent.mkdir(parents=True)
            wdm_binary.write_bytes(b"cached driver")
            autowell_cache = Path(directory) / "Autowell" / "drivers" / "geckodriver.exe"
            with (
                patch.dict(os.environ, {"GECKODRIVER_PATH": ""}, clear=False),
                patch("autoapply.helper.firefox_driver.shutil.which", return_value=None),
                patch("autoapply.helper.firefox_driver.Path.home", return_value=Path(directory)),
                patch("autoapply.helper.firefox_driver._managed_driver_path", return_value=autowell_cache),
                patch("autoapply.helper.firefox_driver.GeckoDriverManager") as manager,
            ):
                self.assertEqual(resolve_geckodriver_path(), str(autowell_cache.resolve()))
                self.assertEqual(autowell_cache.read_bytes(), b"cached driver")
            manager.assert_not_called()

    def test_invalid_configured_path_does_not_fall_back_to_download(self):
        with (
            patch.dict(os.environ, {"GECKODRIVER_PATH": "missing-geckodriver.exe"}),
            patch("autoapply.helper.firefox_driver.GeckoDriverManager") as manager,
        ):
            with self.assertRaisesRegex(RuntimeError, "does not point to a driver file"):
                resolve_geckodriver_path()
        manager.assert_not_called()

    def test_empty_configured_driver_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            driver_path = Path(directory) / "geckodriver.exe"
            driver_path.touch()
            with (
                patch.dict(os.environ, {"GECKODRIVER_PATH": str(driver_path)}),
                patch("autoapply.helper.firefox_driver.GeckoDriverManager") as manager,
            ):
                with self.assertRaisesRegex(RuntimeError, "empty driver file"):
                    resolve_geckodriver_path()
            manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
