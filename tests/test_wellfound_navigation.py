"""Regression tests for Wellfound job-search navigation."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from autoapply.autoapply import JobScraper


class _Driver:
    def __init__(self) -> None:
        self.visited: list[str] = []
        self.current_url = ""

    def get(self, url: str) -> None:
        self.visited.append(url)
        self.current_url = url


class WellfoundNavigationTests(unittest.TestCase):
    def test_selected_location_is_reused_and_keyword_uses_job_title_control(self) -> None:
        scraper = JobScraper.__new__(JobScraper)
        scraper.driver = _Driver()
        scraper._has_visible_button = Mock(side_effect=lambda label: label == "India")
        scraper._apply_job_title = Mock()
        scraper._report = Mock()

        scraper._search_wellfound("India", "Software Engineer")

        self.assertEqual(scraper.driver.visited, ["https://wellfound.com/jobs"])
        scraper._apply_job_title.assert_called_once_with("Software Engineer")
        self.assertTrue(scraper.keyword_search_applied)

    def test_title_keyword_uses_the_wellfound_job_title_picker(self) -> None:
        scraper = JobScraper.__new__(JobScraper)
        scraper._has_visible_button = Mock(return_value=False)
        scraper._select_search_value = Mock()

        scraper._apply_job_title("Software Engineer")

        scraper._select_search_value.assert_called_once_with(
            "Add a job title",
            "Software Engineer",
            "job-title",
        )

    def test_default_filters_do_not_open_the_filter_drawer(self) -> None:
        scraper = JobScraper.__new__(JobScraper)
        scraper._open_filter_panel = Mock()

        scraper._apply_wellfound_filters(
            {
                "employment_type": "",
                "company_size": "",
                "remote_only": "false",
            }
        )

        scraper._open_filter_panel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
