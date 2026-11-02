import os
import json
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors
from google.genai import types
from autoapply.helper.pdfhandler import PDFHandler

load_dotenv()

class CoverLetterGenerator:
    MAX_GENERATION_ATTEMPTS = 4
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, model_name="gemini-3.8-flash", api_key=None, resume_path=None):
        self.model_name = model_name
        self.api_key = api_key or os.getenv("API_KEY")
        self.resume_path = resume_path or os.getenv("RESUME_PATH")
        if not self.api_key:
            raise ValueError("API_KEY is not configured. Add it to .env.")
        if not self.resume_path:
            raise ValueError("RESUME_PATH is not configured. Add it to .env.")

        self.client = genai.Client(api_key=self.api_key)

        self.generation_config = types.GenerateContentConfig(max_output_tokens=2048)

        self.pdf_handler = PDFHandler(self.resume_path)

    def generate_cover_letter(self, job_description: str) -> str:
        """Generate a letter by sending résumé content and the job description to Gemini."""

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
            raise ValueError("No readable text was found in the résumé PDF.")
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
                if attempt == self.MAX_GENERATION_ATTEMPTS - 1:
                    raise RuntimeError(
                        "Gemini is temporarily unavailable after "
                        f"{self.MAX_GENERATION_ATTEMPTS} attempts. "
                        "No application was submitted; try again later."
                    ) from error
                time.sleep(2 ** (attempt + 1))
