"""Create and persist reviewable cover-letter drafts without submitting them."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def create_drafts(
    jobs: Iterable[dict[str, str]],
    cover_letter_generator: Any,
    workbook: Any,
) -> tuple[list[dict[str, str]], list[str]]:
    """Generate drafts for jobs and record each successful draft locally.

    A failure for one job does not discard drafts that were successfully created
    for the others. This function never opens a browser or submits an application.
    """
    drafts: list[dict[str, str]] = []
    failures: list[str] = []

    for job in jobs:
        role = job.get("role", "Untitled role")
        try:
            description = job["description"]
            cover_letter = cover_letter_generator.generate_cover_letter(description)
            workbook.add_application(
                job["link"],
                description,
                cover_letter,
                "Drafted (not submitted)",
            )
            drafts.append({
                "job_id": job["id"],
                "role": role,
                "company": job.get("company", "Company unavailable"),
                "cover_letter": cover_letter,
            })
        except Exception as error:
            failures.append(f"{role}: {error}")

    if drafts:
        workbook.save_excel()
    return drafts, failures
