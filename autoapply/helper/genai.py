import os
from dotenv import load_dotenv
from google import genai
from google.genai import types
from autoapply.helper.pdfhandler import PDFHandler

load_dotenv()

class CoverLetterGenerator:
    def __init__(self, model_name="gemini-2.0-flash", api_key=None, resume_path=None):
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

        try:
            project_links = self.pdf_handler.extract_links()
            resume_text = self.pdf_handler.extract_text()

        except FileNotFoundError as error:
            raise ValueError(f"Resume file was not found: {self.resume_path}") from error

        if not resume_text.strip():
            raise ValueError("No readable text was found in the résumé PDF.")

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

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=self.generation_config,
        )

        if not response.text:
            raise RuntimeError("Gemini returned an empty cover letter.")
        return response.text.strip()
