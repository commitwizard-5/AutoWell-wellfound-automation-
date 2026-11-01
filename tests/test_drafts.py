"""Tests for local cover-letter draft creation."""

from __future__ import annotations

import unittest

from autoapply.drafts import create_drafts


class _Generator:
    def generate_cover_letter(self, description: str) -> str:
        return f"Draft for: {description}"


class _Workbook:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []
        self.saved = False

    def add_application(
        self,
        link: str,
        description: str,
        cover_letter: str,
        status: str,
    ) -> None:
        self.rows.append((link, description, cover_letter, status))

    def save_excel(self) -> None:
        self.saved = True


class DraftCreationTests(unittest.TestCase):
    def test_selected_jobs_generate_local_drafts_and_are_logged(self) -> None:
        workbook = _Workbook()
        jobs = [{
            "id": "7",
            "role": "Software Engineer Intern",
            "company": "Example Co",
            "link": "https://wellfound.com/jobs/7-example",
            "description": "Build reliable software.",
        }]

        drafts, failures = create_drafts(jobs, _Generator(), workbook)

        self.assertEqual(failures, [])
        self.assertEqual(drafts, [{
            "job_id": "7",
            "role": "Software Engineer Intern",
            "company": "Example Co",
            "cover_letter": "Draft for: Build reliable software.",
        }])
        self.assertEqual(workbook.rows, [(
            "https://wellfound.com/jobs/7-example",
            "Build reliable software.",
            "Draft for: Build reliable software.",
            "Drafted (not submitted)",
        )])
        self.assertTrue(workbook.saved)


if __name__ == "__main__":
    unittest.main()
