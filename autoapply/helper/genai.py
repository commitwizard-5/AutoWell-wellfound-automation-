import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from autoapply.helper.pdfhandler import PDFHandler


load_dotenv()
LOGGER = logging.getLogger(__name__)


class CoverLetterGenerator:
    MAX_PROVIDER_ATTEMPTS = 2
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    INITIAL_RETRY_DELAY_SECONDS = 1.5
    MAX_RETRY_DELAY_SECONDS = 8
    DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
    DEEPSEEK_MODEL = "deepseek-chat"

    def __init__(
        self,
        model_name="gemini-3.8-flash",
        api_key=None,
        resume_path=None,
        progress_callback: Callable[[str], None] | None = None,
        deepseek_api_key=None,
    ):
        self.model_name = model_name
        self.gemini_api_key = (
            api_key
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("API_KEY")
            or ""
        ).strip()
        self.api_key = self.gemini_api_key
        self.deepseek_api_key = (
            deepseek_api_key or os.getenv("DEEPSEEK_API_KEY") or ""
        ).strip()
        self.resume_path = resume_path or os.getenv("RESUME_PATH", "")
        self.progress_callback = progress_callback
        self.client = self._create_gemini_client(self.gemini_api_key)
        self.generation_config = types.GenerateContentConfig(max_output_tokens=2048)
        self.pdf_handler = PDFHandler(self.resume_path)
        self.last_generation_result: dict[str, Any] | None = None

    def generate_cover_letter_result(
        self,
        job: dict[str, Any] | str,
        resume: str | dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a cover letter and its provider metadata, always using the local template last."""
        job_data = job if isinstance(job, dict) else {"description": job}
        config = config or {}
        self._apply_config(config)
        configured_resume_path = config.get("resume_path")
        if configured_resume_path and str(configured_resume_path) != self.resume_path:
            self.resume_path = str(configured_resume_path)
            self.pdf_handler = PDFHandler(self.resume_path)
        role = self._clean_text(
            job_data.get("role") or job_data.get("title") or job_data.get("job_title")
        ) or "the role"
        company = self._clean_text(
            job_data.get("company") or job_data.get("company_name")
        ) or "the organization"
        description = self._clean_text(
            job_data.get("description") or job_data.get("job_description")
        )
        resume_text, project_links = self._resume_context(resume)
        if isinstance(resume, dict):
            project_links = self._clean_text(resume.get("project_links")) or project_links

        prompt = self._cover_letter_prompt(
            role, company, description, resume_text, project_links
        )
        errors_seen: list[str] = []
        letter, provider = self._generate_from_providers(
            prompt,
            self.generation_config,
            validator=self._valid_cover_letter,
            error_details=errors_seen,
        )
        if letter is None:
            letter = self._template_cover_letter(
                role, company, description, resume_text, project_links
            )
            provider = "template"

        result = {
            "cover_letter": letter,
            "provider_used": provider,
            "success": bool(letter.strip()),
            "fallback_used": provider != "gemini",
            "error_details": errors_seen or None,
        }
        self.last_generation_result = result
        return result

    def generate_cover_letter(
        self,
        job_description: str | dict[str, Any],
        resume: str | dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> str:
        """Keep the original string-returning interface for existing callers."""
        result = self.generate_cover_letter_result(job_description, resume, config)
        return result["cover_letter"]

    def answer_application_questions(
        self,
        job_description: str,
        questions: list[dict[str, object]],
        profile_answers: dict[str, str] | None = None,
    ) -> list[dict[str, str]]:
        """Answer application questions with the configured providers, without guessing facts."""
        resume_text, project_links = self._resume_context()
        prompt = f"""
Answer each job application question using only the applicant information below.
Write a relevant, truthful response. Never invent or assume personal facts. Match
radio/select answers exactly to one listed option. If a required personal fact is
not explicitly supported by the resume or confirmed profile answers, return an
empty answer. This is especially important for referrals, visa/work
authorization, relocation, and other personal eligibility questions.

Return only a JSON array:
[{{"id":"question id","answer":"answer","evidence":"verbatim resume text or empty"}}]
Evidence for resume-based facts must be copied verbatim from the resume. For
applicant-confirmed profile answers, evidence may be empty. Otherwise leave the
answer empty.

Applicant resume:
{resume_text}

Applicant links:
{project_links}

Applicant-confirmed reusable answers:
{json.dumps(profile_answers or {}, ensure_ascii=False)}

Job description:
{job_description}

Questions:
{json.dumps(questions, ensure_ascii=False)}
"""
        config = types.GenerateContentConfig(
            max_output_tokens=2048,
            response_mime_type="application/json",
        )

        def parse_answer_array(text: str) -> list:
            answer_data = json.loads(text)
            if not isinstance(answer_data, list):
                raise ValueError("Response was not a JSON array.")
            return answer_data

        answer_data, _provider = self._generate_from_providers(
            prompt,
            config,
            validator=parse_answer_array,
        )
        if answer_data is None:
            return []

        known_ids = {str(question["id"]) for question in questions}
        validated = []
        for item in answer_data:
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

    def _resume_context(
        self,
        resume: str | dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        if isinstance(resume, str):
            return resume.strip(), ""
        if isinstance(resume, dict):
            text = self._clean_text(
                resume.get("text") or resume.get("resume_text") or resume.get("content")
            )
            if not text:
                text = "\n".join(
                    f"{label}: {self._clean_text(resume.get(key))}"
                    for key, label in (
                        ("skills", "Skills"),
                        ("experience", "Experience"),
                        ("projects", "Projects"),
                        ("education", "Education"),
                    )
                    if self._clean_text(resume.get(key))
                )
            links = self._clean_text(resume.get("project_links"))
            if text or links:
                return text, links
        try:
            project_links = self.pdf_handler.extract_links() if self.resume_path else []
            resume_text = self.pdf_handler.extract_text() if self.resume_path else ""
        except Exception as error:
            LOGGER.warning("Could not read resume content for generation (%s).", type(error).__name__)
            return "", ""
        return (resume_text or "").strip(), str(project_links or "")

    def get_resume_text(self) -> str:
        return self._resume_context()[0]

    @staticmethod
    def _cover_letter_prompt(
        role: str,
        company: str,
        description: str,
        resume_text: str,
        project_links: str,
    ) -> str:
        return f"""
Write a professional, natural, concise cover letter for the applicant.
Prefer 150-250 words when the supplied facts support that length; be shorter
rather than padding with generic claims. Use only facts explicitly present in
the resume. Do not invent skills, qualifications, achievements, projects,
education, or work experience. Select relevant resume details that match the
job. If information is unavailable, omit it. Do not make exaggerated claims,
use placeholders, or add facts that are merely implied.

Job title: {role}
Company: {company}
Job description:
{description or "No detailed description was provided."}

Applicant resume:
{resume_text or "No resume text is available."}

Resume project/profile links:
{project_links or "No links are available."}

Return only the cover letter. Use a simple greeting and closing only when
supported; do not invent the applicant's name.
"""

    @classmethod
    def _valid_cover_letter(cls, text: str) -> bool:
        normalized = text.strip()
        return (
            len(normalized) >= 30
            and not any(
                marker in normalized.casefold()
                for marker in ("[insert ", "[your name]", "[company name]")
            )
        )

    @classmethod
    def _template_cover_letter(
        cls,
        role: str,
        company: str,
        description: str,
        resume_text: str,
        project_links: str,
    ) -> str:
        role = role if role != "the role" else "the opportunity"
        company = company if company != "the organization" else "your team"
        opening = f"Dear Hiring Team,\n\nI am applying for {role} at {company}."
        paragraphs = [opening]

        relevant_focus = cls._job_focus(description)
        if relevant_focus:
            paragraphs.append(
                f"The listing highlights {relevant_focus}. I am interested in "
                "contributing to this work."
            )
        else:
            paragraphs.append(
                "I would welcome the opportunity to contribute to the work described "
                "in the listing."
            )

        resume_details = cls._resume_details(resume_text, description)
        if resume_details:
            paragraphs.append(f"My resume includes {resume_details}.")
        if project_links:
            links = cls._resume_links(project_links)
            if links:
                paragraphs.append(f"Relevant links from my resume: {', '.join(links[:3])}.")
        paragraphs.append("Thank you for considering my application.")
        return "\n\n".join(paragraphs)

    @staticmethod
    def _job_focus(description: str) -> str:
        if not description:
            return ""
        sentences = [
            " ".join(sentence.split()).strip(" -•")
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", description)
        ]
        for sentence in sentences:
            if 25 <= len(sentence) <= 180:
                return sentence.rstrip(".")
        return ""

    @staticmethod
    def _resume_details(resume_text: str, job_description: str) -> str:
        if not resume_text:
            return ""
        lines = [" ".join(line.split()).strip(" •-") for line in resume_text.splitlines()]
        ignored = {"resume", "curriculum vitae", "experience", "education", "skills", "projects"}
        candidates = [
            line for line in lines
            if len(line) >= 20
            and line.casefold().strip(":") not in ignored
            and not re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", line)
            and not re.fullmatch(r"https?://\S+", line, re.IGNORECASE)
        ]
        if not candidates:
            return ""

        job_words = set(re.findall(r"[a-zA-Z][a-zA-Z+#.-]{2,}", job_description.casefold()))
        ranked = sorted(
            enumerate(candidates),
            key=lambda item: (
                -len(job_words.intersection(
                    set(re.findall(r"[a-zA-Z][a-zA-Z+#.-]{2,}", item[1].casefold()))
                )),
                item[0],
            ),
        )
        selected: list[str] = []
        for _index, line in ranked:
            if line not in selected:
                selected.append(line)
            if len(selected) == 3:
                break
        excerpt = "; ".join(selected)
        return excerpt[:500].rstrip(" ;")

    @staticmethod
    def _resume_links(project_links: str) -> list[str]:
        links = re.findall(r"https?://[^\s,;]+", project_links)
        return [link.rstrip(".)]") for link in links]

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            return ", ".join(str(item).strip() for item in value if str(item).strip())
        return str(value).strip()

    def _generate_from_providers(
        self,
        prompt: str,
        config: Any,
        *,
        validator: Callable[[str], Any],
        error_details: list[str] | None = None,
    ) -> tuple[Any | None, str | None]:
        errors_seen = error_details if error_details is not None else []
        providers = []
        if self.gemini_api_key:
            providers.append("gemini")
        if self.deepseek_api_key:
            providers.append("deepseek")

        for provider in providers:
            for attempt in range(self.MAX_PROVIDER_ATTEMPTS):
                try:
                    text = self._call_provider(provider, prompt, config)
                    if not text.strip():
                        raise ValueError("Provider returned empty content.")
                    validated = validator(text)
                    if validated is False or validated is None:
                        raise ValueError("Provider returned invalid content.")
                    return (text.strip() if validated is True else validated), provider
                except Exception as error:
                    retryable = self._is_retryable(error)
                    if retryable and attempt + 1 < self.MAX_PROVIDER_ATTEMPTS:
                        delay = self._retry_delay(error, attempt)
                        self._report(
                            f"{provider.title()} is temporarily unavailable; retrying "
                            f"once in about {round(delay)} seconds."
                        )
                        time.sleep(delay)
                        continue

                    reason = self._safe_error_summary(provider, error)
                    errors_seen.append(reason)
                    LOGGER.warning("%s generation failed: %s", provider.title(), reason)
                    if self._is_invalid_content(error):
                        LOGGER.info("%s returned invalid or empty content; trying the next fallback.", provider.title())
                    break

        return None, None

    def _apply_config(self, config: dict[str, Any]) -> None:
        gemini_key = (
            config.get("gemini_api_key")
            or config.get("api_key")
            or config.get("GEMINI_API_KEY")
        )
        if gemini_key is not None and str(gemini_key).strip() != self.gemini_api_key:
            self.gemini_api_key = str(gemini_key).strip()
            self.api_key = self.gemini_api_key
            self.client = self._create_gemini_client(self.gemini_api_key)
        deepseek_key = config.get("deepseek_api_key") or config.get("DEEPSEEK_API_KEY")
        if deepseek_key is not None:
            self.deepseek_api_key = str(deepseek_key).strip()

    @staticmethod
    def _create_gemini_client(api_key: str):
        if not api_key:
            return None
        try:
            return genai.Client(api_key=api_key)
        except Exception as error:
            LOGGER.warning("Could not initialize Gemini client (%s).", type(error).__name__)
            return None

    def _call_provider(self, provider: str, prompt: str, config: Any) -> str:
        if provider == "gemini":
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Provider returned empty content.")
            return text.strip()

        if provider == "deepseek":
            return self._call_deepseek(prompt, config)
        raise ValueError("Unknown AI provider.")

    def _call_deepseek(self, prompt: str, config: Any) -> str:
        output_limit = getattr(config, "max_output_tokens", None) or 2048
        payload = json.dumps({
            "model": self.DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": output_limit,
            "temperature": 0.4,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.DEEPSEEK_ENDPOINT,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ProviderHTTPError(error.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            raise ProviderConnectionError(type(error).__name__) from None
        try:
            text = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Provider returned an invalid response.") from None
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Provider returned empty content.")
        return text.strip()

    def _retry_delay(self, error: Exception, attempt: int) -> float:
        retry_after = self._retry_after_seconds(error)
        if retry_after is not None:
            return min(retry_after, self.MAX_RETRY_DELAY_SECONDS)
        base_delay = min(
            self.INITIAL_RETRY_DELAY_SECONDS * (2**attempt),
            self.MAX_RETRY_DELAY_SECONDS,
        )
        return min(base_delay + random.uniform(0, base_delay * 0.25), self.MAX_RETRY_DELAY_SECONDS)

    @staticmethod
    def _retry_after_seconds(error: Exception) -> float | None:
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
                return max(0.0, (retry_time - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    @classmethod
    def _is_retryable(cls, error: Exception) -> bool:
        if isinstance(error, ProviderHTTPError):
            return error.status_code in cls.RETRYABLE_STATUS_CODES
        if isinstance(error, errors.APIError):
            if error.code == 429:
                details = f"{error.message or ''} {getattr(error, 'details', '')}".casefold()
                if any(marker in details for marker in ("quota", "billing", "limit: 0")):
                    return False
            return error.code in cls.RETRYABLE_STATUS_CODES
        if isinstance(error, ProviderConnectionError):
            return True
        if isinstance(error, (TimeoutError, ConnectionError, urllib.error.URLError)):
            return True
        error_type = type(error).__name__.casefold()
        return any(marker in error_type for marker in ("timeout", "connection", "connecterror"))

    @staticmethod
    def _is_invalid_content(error: Exception) -> bool:
        return isinstance(error, (ValueError, json.JSONDecodeError))

    @staticmethod
    def _safe_error_summary(provider: str, error: Exception) -> str:
        if isinstance(error, errors.APIError):
            return f"{provider}: HTTP {error.code} {error.status or 'API error'}"
        if isinstance(error, ProviderHTTPError):
            return f"{provider}: HTTP {error.status_code}"
        if isinstance(error, (ProviderConnectionError, TimeoutError, ConnectionError, urllib.error.URLError)) or any(
            marker in type(error).__name__.casefold()
            for marker in ("timeout", "connection", "connecterror")
        ):
            return f"{provider}: connection or timeout error"
        if isinstance(error, ValueError):
            return f"{provider}: invalid or empty response"
        return f"{provider}: provider request failed ({type(error).__name__})"

    def _report(self, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(message)


class ProviderHTTPError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class ProviderConnectionError(Exception):
    pass
