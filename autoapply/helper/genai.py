import os
import json
import random
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv
from google import genai
from google.genai import errors
from google.genai import types
from autoapply.helper.pdfhandler import PDFHandler

load_dotenv()

class CoverLetterGenerator:
    MAX_GENERATION_ATTEMPTS = 5
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    INITIAL_RETRY_DELAY_SECONDS = 2
    MAX_RETRY_DELAY_SECONDS = 30

    def __init__(
        self,
        model_name="gemini-3.8-flash",
        api_key=None,
        resume_path=None,
        progress_callback: Callable[[str], None] | None = None,
    ):
        self.model_name = model_name
        self.api_key = api_key or os.getenv("API_KEY")
        self.resume_path = resume_path or os.getenv("RESUME_PATH")
        self.progress_callback = progress_callback
        if not self.api_key:
            raise ValueError("API_KEY is not configured. Add it to .env.")
        if not self.resume_path:
            raise ValueError("RESUME_PATH is not configured. Add it to .env.")

        self.client = genai.Client(api_key=self.api_key)

        self.generation_config = types.GenerateContentConfig(max_output_tokens=2048)

        self.pdf_handler = PDFHandler(self.resume_path)

    def generate_cover_letter(self, job_description: str) -> str:
        """Generate a letter by sending resume content and the job description to Gemini."""

        resume_text, project_links = self._resume_context()

        prompt = f"""
My Resume:

"{resume_text}"

Create a custom brief cover letter for the following job description:

"{job_description}"

Here are my project links and profile links:

"{project_links}"

Use actual relevant links from my resume.

Requirements:
- Maximum 5-6 lines.
- Professional and natural.
- Suitable for a college student applying for an internship.
- Use simple and clear English.
- Focus only on skills, projects, education, and experience actually present in my resume.
- Do not invent or exaggerate any experience.
- Match my relevant skills and projects to the job description.
- If you mention a project, include its actual URL from my resume.
- Do not include a subject line.
- Do not include a greeting.
- Do not include a sign-off or footer.
- Do not use placeholders.
- Do not include variables in the response.
"""

        response = self._generate_content(prompt, self.generation_config)

        if not response.text:
            raise RuntimeError("Gemini returned an empty cover letter.")
        return response.text.strip()

    def answer_application_questions(
        self,
        job_description: str,
        questions: list[dict[str, object]],
        profile_answers: dict[str, str] | None = None,
    ) -> list[dict[str, str]]:
        """Answer application questions using facts provided by the applicant."""
        resume_text, project_links = self._resume_context()
        prompt = f"""
Answer each job application question using only the applicant information below.
Write the strongest relevant and truthful response. Never invent, assume, or
exaggerate personal facts. Match radio/select answers exactly to one listed option.
If a required personal fact is not explicitly supported by the resume or the
applicant-confirmed reusable answers, return an empty answer. This is especially
important for referrals, visa/work authorization, relocation, and other personal
eligibility questions.

Return only a JSON array with this shape:
[{{"id":"question id","answer":"answer","evidence":"verbatim supporting resume text or empty"}}]
For any personal factual answer based on the resume, evidence must be copied
verbatim from the resume. If the fact comes from an applicant-confirmed reusable
answer, evidence may be empty because that answer is authoritative. Otherwise,
leave the answer empty.

Applicant resume:
{resume_text}

Applicant profile and project links:
{project_links}

Applicant-confirmed reusable application answers:
{json.dumps(profile_answers or {}, ensure_ascii=False)}
Use these answers as authoritative facts where they apply to a question.

Job description:
{job_description}

Questions:
{json.dumps(questions, ensure_ascii=False)}
"""
        config = types.GenerateContentConfig(
            max_output_tokens=2048,
            response_mime_type="application/json",
        )
        response = self._generate_content(prompt, config)
        if not response.text:
            raise RuntimeError("Gemini returned no answers to the application questions.")
        try:
            answers = json.loads(response.text)
        except json.JSONDecodeError as error:
            raise RuntimeError("Gemini returned malformed application-question answers.") from error
        if not isinstance(answers, list):
            raise RuntimeError("Gemini returned an invalid application-question response.")

        known_ids = {str(question["id"]) for question in questions}
        validated = []
        for item in answers:
            if not isinstance(item, dict):
                continue
            question_id = item.get("id")
            answer = item.get("answer")
            evidence = item.get("evidence", "")
            if (
                isinstance(question_id, str)
                and question_id in known_ids
                and isinstance(answer, str)
                and isinstance(evidence, str)
            ):
                validated.append({
                    "id": question_id,
                    "answer": answer.strip(),
                    "evidence": evidence.strip(),
                })
        return validated

    def _resume_context(self) -> tuple[str, str]:
        try:
            project_links = self.pdf_handler.extract_links()
            resume_text = self.pdf_handler.extract_text()
        except FileNotFoundError as error:
            raise ValueError(f"Resume file was not found: {self.resume_path}") from error
        if not resume_text.strip():
            raise ValueError("No readable text was found in the resume PDF.")
        return resume_text, str(project_links)

    def _generate_content(self, prompt: str, config):
        for attempt in range(self.MAX_GENERATION_ATTEMPTS):
            try:
                return self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=config,
                )
            except errors.APIError as error:
                if error.code not in self.RETRYABLE_STATUS_CODES:
                    raise
                if error.code == 429 and self._is_quota_exhausted(error):
                    raise RuntimeError(
                        "Gemini rejected the request because the API quota or billing "
                        "limit appears to be exhausted. Check the API key's project, "
                        "quota, and billing in Google AI Studio/Google Cloud. No "
                        "application was submitted."
                    ) from error
                if attempt == self.MAX_GENERATION_ATTEMPTS - 1:
                    reason = self._provider_error_summary(error)
                    raise RuntimeError(
                        f"Gemini remained unavailable after {self.MAX_GENERATION_ATTEMPTS} "
                        f"attempts ({reason}). This is usually provider load or a "
                        "temporary rate limit, not a problem with the job or resume. "
                        "No application was submitted. Wait a minute and retry; if it "
                        "keeps happening, check Google AI Studio's service status and "
                        "the API key project's quota."
                    ) from error
                delay = self._retry_delay(error, attempt)
                self._report(
                    f"Gemini is temporarily busy ({self._provider_error_summary(error)}). "
                    f"Retrying in about {round(delay)} seconds "
                    f"({attempt + 2}/{self.MAX_GENERATION_ATTEMPTS})…"
                )
                time.sleep(delay)

    @classmethod
    def _retry_delay(cls, error: errors.APIError, attempt: int) -> float:
        retry_after = cls._retry_after_seconds(error)
        if retry_after is not None:
            return min(retry_after, cls.MAX_RETRY_DELAY_SECONDS)
        base_delay = min(
            cls.INITIAL_RETRY_DELAY_SECONDS * (2**attempt),
            cls.MAX_RETRY_DELAY_SECONDS,
        )
        return min(
            base_delay + random.uniform(0, base_delay * 0.25),
            cls.MAX_RETRY_DELAY_SECONDS,
        )

    @staticmethod
    def _retry_after_seconds(error: errors.APIError) -> float | None:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        retry_after = headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return max(
                    0.0,
                    (retry_time - datetime.now(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _is_quota_exhausted(error: errors.APIError) -> bool:
        details = f"{error.message or ''} {error.details}".casefold()
        return any(
            marker in details
            for marker in (
                "exceeded your current quota",
                "quota exceeded",
                "quota has been exceeded",
                "billing",
                "limit: 0",
            )
        )

    @staticmethod
    def _provider_error_summary(error: errors.APIError) -> str:
        status = error.status or "API error"
        message = " ".join((error.message or "").split())
        if len(message) > 180:
            message = f"{message[:177]}..."
        return f"HTTP {error.code} {status}" + (f": {message}" if message else "")

    def _report(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)
